"""Delete an account everywhere: hub tombstone, agents, the CLI without sync."""

import argparse
import io
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cccswitcher import account_delete, claude, cli, machines, sync_agent, sync_config

try:
    from .sync_fakes import on_machine, seed_login
    from .test_accounts_view import acct, machine
    from .test_sync import EMAIL, KEY, ORG, SECRET, SyncBase, run_agent
    from . import test_sync_login
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import on_machine, seed_login
    from test_accounts_view import acct, machine
    from test_sync import EMAIL, KEY, ORG, SECRET, SyncBase, run_agent
    import test_sync_login

from cccswitcher import accounts_view

OTHER = "bob@example.com"


class HubTombstone(SyncBase):
    def _shared_from_mac(self):
        cred = self.oauth.issue("g1", 8 * 3600, time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["pushed"], [KEY])
        return cred

    def test_delete_over_http_rejects_reupload_until_login(self):
        cred = self._shared_from_mac()
        client = sync_agent.HubClient(self.hub.url, SECRET, "mac")
        self.assertEqual(client.delete(KEY), {"key": KEY, "deleted": True, "existed": True})
        self.assertEqual(client.delete(KEY)["existed"], False)  # idempotent
        listed = client.credentials()[0]
        # What a 0.21 agent sees: not shared, no credential -> it neither pulls nor pushes.
        self.assertEqual((listed["shared"], listed["deleted"]), (False, True))
        self.assertNotIn("credential", listed)
        newer = self.oauth.refresh_as_client(cred)
        payload = {"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": newer}
        answer = client.offer(payload)
        self.assertEqual((answer["adopted"], answer["reason"]), (False, "deleted on hub"))
        self.assertEqual(client.share(KEY, True)["shared"], False)  # sharing does not undelete
        self.assertEqual(self.store.refresh_due(), [])  # nothing left to refresh
        # A deliberate login (sync login: replace/undelete) brings it back.
        answer = client.offer({**payload, "replace": True, "undelete": True})
        self.assertTrue(answer["adopted"])
        listed = client.credentials()[0]
        self.assertEqual((listed["shared"], listed["deleted"]), (True, False))
        self.assertIn("credential", listed)
        with self.assertRaises(sync_agent.HubError):  # key must be provider|email|org
            client._request("POST", "/v1/delete", {"key": "nope"})

    def test_agents_remove_copies_but_leave_the_live_login(self):
        self._shared_from_mac()
        self.assertEqual(run_agent(self.envs["linux"], self.hub.url, "linux")["created"], [KEY])
        with on_machine(self.envs["mac"]):
            seed_login(OTHER, ORG, self.oauth.issue("g2", 8 * 3600, time.time()), active=False)
        live_before = self.live("mac")
        self.store.delete(KEY)

        linux = run_agent(self.envs["linux"], self.hub.url, "linux")
        self.assertEqual((linux["removed"], linux["errors"]), ([KEY], []))
        self.assertIsNone(self.slot_credential("linux"))

        mac = run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertEqual((mac["kept_active"], mac["removed"], mac["errors"]), ([KEY], [], []))
        self.assertNotIn(KEY, mac["pushed"])  # (bob, a new local login, is pushed)
        self.assertEqual(self.live("mac"), live_before)          # live login untouched
        self.assertIsNotNone(self.slot_credential("mac"))        # its slot stays while active
        # Claude Code refreshes the live login by itself: still never uploaded.
        newer = self.oauth.refresh_as_client(live_before)
        with on_machine(self.envs["mac"]):
            claude.write_active_credential(__import__("json").dumps(newer))
        mac = run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertEqual((KEY in mac["pushed"], mac["errors"]), (False, []))
        self.assertTrue(self.store._load()["accounts"][KEY]["deleted"])
        self.assertNotIn("credential", self.store._load()["accounts"][KEY])

        # After Claude Code switches away, the next round removes the slot.
        with on_machine(self.envs["mac"]):
            claude.switch_account(OTHER)
        mac = run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertEqual(mac["removed"], [KEY])
        self.assertIsNone(self.slot_credential("mac"))
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["removed"], [])  # once

        # The hub's report shows no generation for the deleted login.
        self.assertNotIn(KEY, self.store._load()["machines"]["mac"]["generations"])

    def test_a_copy_added_again_by_hand_is_left_alone(self):
        self._shared_from_mac()
        run_agent(self.envs["linux"], self.hub.url, "linux")
        self.store.delete(KEY)
        run_agent(self.envs["linux"], self.hub.url, "linux")
        with on_machine(self.envs["linux"]):
            seed_login(EMAIL, ORG, self.oauth.issue("g3", 8 * 3600, time.time()), active=False)
        summary = run_agent(self.envs["linux"], self.hub.url, "linux")
        self.assertEqual((summary["removed"], summary["pushed"]), ([], []))
        self.assertIsNotNone(self.slot_credential("linux"))


class LoginClearsTombstone(test_sync_login.OneButtonLogin):
    def test_sync_login_email_undeletes(self):
        old = self.oauth.issue("g1", 8 * 3600, time.time())
        self.store.offer({"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": old})
        self.store.delete(KEY)
        rc, out, _, _ = self.run_login("linux", email=EMAIL, token="at-back")
        self.assertEqual(rc, 0, out)
        entry = self.store._load()["accounts"][KEY]
        self.assertFalse(entry.get("deleted"))
        self.assertTrue(entry["shared"])
        self.assertEqual(entry["credential"]["claudeAiOauth"]["accessToken"], "at-back")
        created = run_agent(self.envs["mac"], self.hub.url, "mac")["created"]
        self.assertEqual(created, [KEY])  # every machine gets it again


def _fleet():
    return [machines.Machine(id="local", name="Mac"),
            machines.Machine(id="box", name="path-brower", alias="box"),
            machines.Machine(id="gpu", name="simforge-a100", alias="gpu"),
            machines.Machine(id="old", name="old-box", alias="old")]


def _view():
    return accounts_view.build([
        machine("local", [acct("1", EMAIL, org=ORG)], local=True),
        machine("box", [acct("4", EMAIL, org=ORG, active=True)]),
        machine("gpu", [acct("2", EMAIL, org=ORG, active=True)]),
        machine("old", [acct("7", EMAIL, org=ORG)], online=False),
    ])


class DeletePlan(unittest.TestCase):
    def test_without_sync_removes_per_machine_after_confirmation(self):
        calls = []

        def fake_action(target, provider, verb, query):
            calls.append((target.id, provider, verb, query))
            return 0, f"{target.name}: removed {provider} account {query}"

        args = argparse.Namespace(email=EMAIL, provider=None, yes=False)
        real_execute = account_delete.execute
        with mock.patch.object(cli, "_view", return_value=_view()), \
                mock.patch("cccswitcher.snapshot.sync_status", return_value={"enabled": False}), \
                mock.patch.object(cli.machines, "load", return_value=_fleet()), \
                mock.patch.object(account_delete.actions, "account_action", side_effect=fake_action), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n") as ask:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.cmd_delete(args), 1)  # cancelled
            ask.assert_called_once()
            self.assertEqual(calls, [])
            text = out.getvalue()
            self.assertIn("Claude Code on path-brower and simforge-a100 are using this account; "
                          "it keeps working until it switches.", text)
            self.assertNotIn("stops syncing", text)

            ask.return_value = "y"
            out = io.StringIO()
            with redirect_stdout(out), \
                    mock.patch.object(account_delete, "execute",
                                      side_effect=lambda p, f, client=None: real_execute(
                                          p, f, client, account_action=fake_action)):
                self.assertEqual(cli.cmd_delete(args), 0)
        self.assertEqual(calls, [("local", "claude", "remove", "1"), ("box", "claude", "remove", "4"),
                                 ("gpu", "claude", "remove", "2")])
        self.assertIn("old-box: offline, its copy stays", out.getvalue())

    def test_no_terminal_needs_yes(self):
        args = argparse.Namespace(email=EMAIL, provider="claude", yes=False)
        with mock.patch.object(cli, "_view", return_value=_view()), \
                mock.patch("cccswitcher.snapshot.sync_status", return_value={"enabled": False}), \
                mock.patch.object(cli.machines, "load", return_value=_fleet()), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch.object(account_delete, "execute") as execute, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_delete(args), 2)
        execute.assert_not_called()

    def test_with_sync_agents_handle_new_machines_ssh_for_the_rest(self):
        sync = {"enabled": True, "machine": "mac",
                "machines": {"mac": {"version": "0.22.0"}, "box": {"version": "0.22.0"},
                             "gpu": {"version": "0.21.1"}}}
        view = _view()
        the_plan = account_delete.plan(view["accounts"][0], _fleet(), sync)
        self.assertEqual({s["machine"]: s["how"] for s in the_plan["steps"]},
                         {"local": "here", "box": "agent", "gpu": "remove", "old": "offline"})
        text = account_delete.describe(the_plan)
        self.assertIn("Claude Code on path-brower and simforge-a100 are using this account; "
                      "it keeps working until it switches, but CCC stops syncing it.", text)
        self.assertIn(f"ccc sync login --email {EMAIL}", text)

        client = mock.Mock()
        client.delete.side_effect = sync_agent.HubError("hub answered 404 on /v1/delete")
        action = mock.Mock(return_value=(0, "ok"))
        lines = []
        self.assertEqual(account_delete.execute(the_plan, _fleet(), client, lines.append, action), 1)
        action.assert_not_called()  # hub failed: nothing removed anywhere
        self.assertIn("0.22 or newer", lines[0])

        client.delete.side_effect = None
        with mock.patch.object(sync_agent, "apply_hub_deletions",
                               return_value={"removed": [the_plan["key"]], "kept_active": [], "errors": []}):
            lines = []
            self.assertEqual(account_delete.execute(the_plan, _fleet(), client, lines.append, action), 0)
        client.delete.assert_called_with(KEY)
        action.assert_called_once()
        self.assertEqual(action.call_args.args[1:], ("claude", "remove", "2"))
        self.assertIn("path-brower: its sync agent removes it after Claude Code switches away", lines)

    def test_codex_on_cswap_machine_is_reported_not_removed(self):
        view = accounts_view.build([machine("box", codex=[acct("a@x.com", "a@x.com", provider="codex")])])
        codex_acct = view["accounts"][0]
        the_plan = account_delete.plan(codex_acct, _fleet(), {"enabled": True})
        self.assertFalse(the_plan["hub"])  # only Claude logins are on the hub
        self.assertEqual(the_plan["steps"][0]["how"], "unsupported")

    def test_deleted_hub_account_has_no_share_toggle(self):
        sync = {"enabled": True, "accounts": {KEY: {"shared": False, "deleted": True, "generation": 3}}}
        view = accounts_view.build([machine("box", [acct("4", EMAIL, org=ORG, active=True)])], sync=sync)
        a = view["accounts"][0]
        self.assertEqual((a["shared"], a["hub_deleted"]), (None, True))
        self.assertNotIn("sync", a["machines"]["box"])


if __name__ == "__main__":
    unittest.main()
