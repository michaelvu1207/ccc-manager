import json
import unittest

from cccswitcher import accounts_view, cli, machines, remote, snapshot

try:
    from .fakes import FakeWorld
except ImportError:  # `unittest discover -s tests`
    from fakes import FakeWorld


def acct(num, email, *, active=False, needs_login=False, disabled=False, org="", pct=None,
         stale=False, at=None, provider="claude"):
    windows = [] if pct is None else [{"id": "5h", "pct_used": pct, "resets_in": "1h"}]
    return {"num": num, "email": email, "label": email, "alias": "", "org_uuid": org, "org_name": "",
            "active": active, "needs_login": needs_login, "disabled": disabled, "plan": "",
            "status_message": "", "windows": windows, "usage_stale": stale, "usage_at": at}


def machine(mid, claude=(), codex=(), *, online=True, local=False, dup=None, has_data=True):
    return {"id": mid, "display_name": mid, "local": local, "platform": "Linux", "online": online,
            "has_data": has_data, "duplicate_of": dup, "engine": "cswap",
            "providers": [{"id": "claude", "accounts": list(claude)}, {"id": "codex", "accounts": list(codex)}]}


class Aggregation(unittest.TestCase):
    def test_same_account_on_several_machines(self):
        view = accounts_view.build([
            machine("local", [acct("1", "A@x.com", active=True, org="o1", pct=30, at=100)], local=True),
            machine("box", [acct("4", "a@x.com", org="o1", pct=10, at=200), acct("2", "b@x.com")]),
            machine("gpu", [acct("2", "a@x.com", needs_login=True, org="o1")]),
        ])
        a = next(x for x in view["accounts"] if x["email"].lower() == "a@x.com")
        self.assertEqual({k: v["state"] for k, v in a["machines"].items()},
                         {"local": "active", "box": "stored", "gpu": "needs_login"})
        self.assertEqual(a["machines"]["box"]["num"], "4")  # slot differs per machine
        self.assertEqual(a["usage_from"], "box")  # freshest live report
        self.assertEqual(a["windows"][0]["pct_used"], 10)
        self.assertEqual(a["active_on"], ["local"])
        b = next(x for x in view["accounts"] if x["email"] == "b@x.com")
        self.assertEqual(b["machines"]["local"]["state"], "absent")
        self.assertEqual(view["accounts"][0]["email"].lower(), "a@x.com")  # active first
        self.assertEqual(view["machines"][0]["short_name"], "this Mac")

    def test_live_beats_newer_stale_and_org_split(self):
        view = accounts_view.build([
            machine("m1", [acct("1", "a@x.com", org="o1", pct=50, at=100)]),
            machine("m2", [acct("1", "a@x.com", org="o1", pct=5, at=900, stale=True),
                           acct("2", "a@x.com", org="o2")]),
        ])
        o1 = next(x for x in view["accounts"] if x["key"].endswith("|o1"))
        self.assertEqual(o1["usage_from"], "m1")
        self.assertEqual(len([x for x in view["accounts"] if x["email"] == "a@x.com"]), 2)

    def test_mixed_disabled_offline_duplicates(self):
        view = accounts_view.build([
            machine("m1", [acct("1", "a@x.com", disabled=True)]),
            machine("m2", [acct("3", "a@x.com")]),
            machine("down", [acct("1", "a@x.com", active=True)], online=False),
            machine("never", [], online=False, has_data=False),
            machine("twin", [acct("9", "a@x.com", active=True)], dup="m1"),
        ])
        (a,) = view["accounts"]
        self.assertFalse(a["disabled"])  # mixed -> per machine
        self.assertEqual(a["disabled_on"], ["m1"])
        self.assertTrue(a["machines"]["m1"]["disabled"])
        self.assertEqual(a["machines"]["down"], {"state": "offline", "num": "1", "query": "1",
                                                 "disabled": False, "last_state": "active"})
        self.assertEqual(a["machines"]["never"]["last_state"], "unknown")
        self.assertNotIn("twin", a["machines"])
        self.assertEqual([m["id"] for m in view["machines"]], ["m1", "m2", "down", "never"])
        view = accounts_view.build([machine("m1", [acct("1", "a@x.com", disabled=True)]),
                                    machine("m2", [acct("3", "a@x.com", disabled=True)])])
        self.assertTrue(view["accounts"][0]["disabled"])

    def test_codex_query_is_email(self):
        view = accounts_view.build([machine("m1", codex=[acct("2", "c@x.com", provider="codex")])])
        self.assertEqual(view["accounts"][0]["machines"]["m1"]["query"], "c@x.com")


class AssignWithFakes(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld().__enter__()
        self.world.add_home("box")  # alice (slot 1, disabled) + bob (slot 2, active)
        machines.save([machines.Machine(id="local", name="Here"),
                       machines.Machine(id="box", name="Box", alias="box")])
        remote.probe(machines.find(machines.load(), "box"))

    def tearDown(self):
        self.world.__exit__(None, None, None)

    def test_snapshot_has_accounts_view(self):
        snap = snapshot.build_snapshot(refresh=False, include_costs=False)
        view = snap["accounts_view"]
        self.assertEqual([m["id"] for m in view["machines"]], ["local", "box"])
        alice = next(a for a in view["accounts"] if a["email"] == "alice@example.com")
        self.assertEqual(alice["machines"]["box"]["state"], "stored")
        self.assertEqual(alice["machines"]["local"]["state"], "absent")
        self.assertTrue(alice["disabled"])
        # a machine-limited snapshot still carries the full view (cached others)
        view = snapshot.build_snapshot(refresh=False, machine_ids=["local"], include_costs=False)["accounts_view"]
        self.assertEqual([m["id"] for m in view["machines"]], ["local", "box"])

    def test_assign_stored_switches_that_machines_slot(self):
        self.assertEqual(cli.main(["assign", "alice@example.com", "--machine", "box"]), 0)
        self.assertEqual(self.world.cswap_log("box")[-1], ["switch", "1"])
        cached = remote.cached_state(machines.find(machines.load(), "box"))
        self.assertTrue(cached.accounts("claude")[0]["active"])

    def test_assign_active_absent_and_codex(self):
        calls = len(self.world.cswap_log("box"))
        self.assertEqual(cli.main(["assign", "bob@example.com", "--machine", "box"]), 0)  # already active
        # absent on this Mac, no terminal under the runner: prints the login command
        self.assertEqual(cli.main(["assign", "bob@example.com", "--machine", "local"]), 1)
        self.assertEqual(len(self.world.cswap_log("box")), calls)  # nothing ran
        self.assertEqual(cli.main(["assign", "c1@example.com", "--machine", "box"]), 0)
        with self.assertRaises(SystemExit):
            cli.main(["assign", "nobody@example.com", "--machine", "box"])

    def test_disable_everywhere(self):
        self.assertEqual(cli.main(["enable", "--everywhere", "alice@example.com"]), 0)
        self.assertEqual(self.world.cswap_log("box")[-1], ["enable", "1"])

    def test_accounts_json(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["accounts", "--no-refresh", "--json"]), 0)
        view = json.loads(buf.getvalue())
        self.assertIn("accounts", view)


if __name__ == "__main__":
    unittest.main()


class SyncState(unittest.TestCase):
    def test_chip_sync_state(self):
        key = "claude|a@x.com|o1"
        sync = {"enabled": True,
                "accounts": {key: {"shared": True, "generation": 5},
                             "claude|b@x.com|": {"shared": False, "generation": 2}},
                "machines": {"seablue": {"generations": {key: 5}, "last_sync": 10},
                             "box": {"generations": {key: 4}, "last_sync": 9}}}
        view = accounts_view.build([
            machine("local", [acct("1", "a@x.com", org="o1", active=True), acct("2", "b@x.com")], local=True),
            machine("box", [acct("1", "a@x.com", org="o1")]),
            machine("gpu", []),
        ], sync=sync, sync_names={"local": "seablue", "box": "box", "gpu": "gpu"})
        a = next(x for x in view["accounts"] if x["email"] == "a@x.com")
        self.assertTrue(a["shared"])
        self.assertEqual(a["hub_generation"], 5)
        self.assertEqual(a["machines"]["local"]["sync"]["state"], "in_sync")
        self.assertEqual(a["machines"]["box"]["sync"]["state"], "behind")
        self.assertEqual(a["machines"]["gpu"]["sync"]["state"], "no_agent")
        b = next(x for x in view["accounts"] if x["email"] == "b@x.com")
        self.assertFalse(b["shared"])
        self.assertNotIn("sync", b["machines"]["local"])
        off = accounts_view.build([machine("local", [acct("1", "a@x.com")], local=True)])
        self.assertIsNone(off["accounts"][0]["shared"])

    def test_pending_copy_of_a_dead_shared_login_needs_login(self):
        key = "claude|a@x.com|o1"
        pending = dict(acct("1", "a@x.com", org="o1"), usage_pending=True)
        machines_ = [machine("local", [pending], local=True),
                     machine("box", [acct("1", "a@x.com", org="o1", needs_login=True)])]
        alive = {"enabled": True, "accounts": {key: {"shared": True, "generation": 1}}, "machines": {}}
        dead = {"enabled": True, "machines": {},
                "accounts": {key: {"shared": True, "generation": 1, "refresh_error": "refresh failed"}}}
        a = accounts_view.build(machines_, sync=alive)["accounts"][0]
        self.assertEqual(a["machines"]["local"]["state"], "pending")
        self.assertTrue(a["usage_pending"])
        a = accounts_view.build(machines_, sync=dead)["accounts"][0]
        self.assertEqual(a["machines"]["local"]["state"], "needs_login")
        self.assertFalse(a["usage_pending"])
