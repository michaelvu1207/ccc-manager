import json
import unittest
import unittest.mock

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
        self.assertEqual(view["machines"][0]["short_name"], "here")  # fixtures say Linux

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


class Order(unittest.TestCase):
    def _windows(self, weekly_at):
        return [{"id": "5h", "pct_used": 10, "resets_in": "1h", "resets_at": weekly_at - 50_000},
                {"id": "wk", "pct_used": 20, "resets_in": "", "resets_at": weekly_at}]

    def test_soonest_weekly_reset_first_and_stable(self):
        def build(active):
            late = acct("1", "late@x.com", active=active == "late")
            soon = acct("2", "soon@x.com", active=active == "soon")
            mid = acct("3", "mid@x.com")
            late["windows"], soon["windows"], mid["windows"] = (
                self._windows(3_000_000_000), self._windows(1_000_000_000), self._windows(2_000_000_000))
            late["pct"] = soon["pct"] = 0
            codex = acct("c", "c@x.com", provider="codex")
            codex["windows"] = self._windows(900_000_000)
            nousage = acct("4", "a-none@x.com", needs_login=True)
            return accounts_view.build([machine("local", [late, nousage, mid, soon], [codex], local=True)])
        order = [a["email"] for a in build("late")["accounts"]]
        self.assertEqual(order, ["soon@x.com", "mid@x.com", "late@x.com", "a-none@x.com", "c@x.com"])
        # Switching the active account or new usage numbers do not move rows.
        self.assertEqual([a["email"] for a in build("soon")["accounts"]], order)

    def test_reset_time_from_text_when_no_timestamp(self):
        now = 1_000.0
        self.assertEqual(accounts_view.weekly_reset(
            [{"id": "5h", "resets_in": "2h 9m"}, {"id": "wk", "resets_in": "4d 3h"}], now), now + 4 * 86400 + 3 * 3600)
        self.assertEqual(accounts_view.weekly_reset([{"id": "wk", "resets_in": "now"}], now), now)
        self.assertIsNone(accounts_view.weekly_reset([{"id": "wk", "resets_in": ""}], now))


class ShortNames(unittest.TestCase):
    def test_derived_column_labels(self):
        shown = [dict(machine("local", local=True), platform="Darwin", display_name="Michael's MacBook Pro"),
                 dict(machine("box"), display_name="path-brower"),
                 dict(machine("gpu"), display_name="simforge-a100"),
                 dict(machine("lap"), display_name="simforge-laptop"),
                 dict(machine("x"), display_name="gpu", short="big-gpu")]
        self.assertEqual(accounts_view.short_names(shown),
                         {"local": "Mac", "box": "path", "gpu": "a100", "lap": "laptop", "x": "big-gpu"})
        view = accounts_view.build(shown)
        self.assertEqual([m["short_name"] for m in view["machines"]], ["Mac", "path", "a100", "laptop", "big-gpu"])

    def test_labels_stay_unique_and_short(self):
        shown = [dict(machine("a"), display_name="path-pc"), dict(machine("b"), display_name="path-brower"),
                 dict(machine("c"), display_name="averyveryverylongname")]
        labels = accounts_view.short_names(shown)
        self.assertEqual(labels, {"a": "pc", "b": "brower", "c": "averyver"})
        self.assertEqual(len(set(labels.values())), 3)

    def test_user_short_name_is_saved(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.dict(os.environ, {"CCC_MACHINES_FILE": tmp + "/m.json"}):
            fleet = [machines.Machine(id="local", name="Mac"), machines.Machine(id="b", name="b", alias="b")]
            machines.set_short(fleet, "b", " a100 ")
            machines.save(fleet)
            self.assertEqual(next(m for m in machines.load() if m.id == "b").short, "a100")
            with self.assertRaises(machines.ConfigError):
                machines.set_short(fleet, "b", "x" * 11)


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
                     machine("box", [acct("1", "a@x.com", org="o1", needs_login=True)]),
                     machine("lap", [acct("3", "a@x.com", org="o1")])]
        alive = {"enabled": True, "accounts": {key: {"shared": True, "generation": 1}}, "machines": {}}
        dead = {"enabled": True, "machines": {},
                "accounts": {key: {"shared": True, "generation": 1, "refresh_error": "refresh failed"}}}
        a = accounts_view.build(machines_, sync=alive)["accounts"][0]
        self.assertEqual(a["machines"]["local"]["state"], "pending")
        self.assertTrue(a["usage_pending"])
        self.assertEqual(a["machines"]["lap"]["state"], "stored")
        a = accounts_view.build(machines_, sync=dead)["accounts"][0]
        self.assertEqual(a["machines"]["local"]["state"], "needs_login")
        self.assertEqual(a["machines"]["lap"]["state"], "needs_login")
        self.assertTrue(a["needs_login"])
        self.assertIn("hub cannot refresh", a["status_message"])
        self.assertFalse(a["usage_pending"])
