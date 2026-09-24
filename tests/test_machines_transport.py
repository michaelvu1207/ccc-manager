import json
import shlex
import time
import unittest

from cccswitcher import actions, cache, claude, cli, machines, remote, snapshot

from .fakes import FakeWorld


def m(alias, mid=None, engine="auto"):
    return machines.Machine(id=mid or alias, name=alias.title(), alias=alias, engine=engine)


class Quoting(unittest.TestCase):
    def test_validate_query(self):
        for good in ("2", "a.b+c@example.com", "work_1"):
            self.assertEqual(actions.validate_query(good), good)
        for bad in ("", "-rf", "--help", "1; rm -rf ~", "$(id)", "a b", "`x`", "a'b", "x\ny"):
            with self.assertRaises(actions.ActionError, msg=bad):
                actions.validate_query(bad)

    def test_ssh_argv_shape(self):
        argv = remote.ssh_argv("host-1", "echo 'hi' $HOME")
        self.assertEqual(argv[1:5], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=6"])
        self.assertEqual(argv[5:7], ["--", "host-1"])
        self.assertEqual(shlex.split(argv[7]), ["sh", "-c", "echo 'hi' $HOME"])
        tty = remote.ssh_argv("h", "x", interactive=True)
        self.assertIn("-t", tty)
        self.assertNotIn("BatchMode=yes", tty)
        for script in (remote.CSWAP_PROBE, remote.CCC_PROBE, remote.PATH_SETUP):
            self.assertNotIn("\\", script)  # fish-safe single quoting

    def test_remote_commands_per_engine(self):
        box, cc = m("box"), m("cc", engine="ccc")
        self.assertEqual(actions.remote_command(box, "claude", "switch", "3"), (["cswap", "switch", "3"], None))
        self.assertEqual(actions.remote_command(box, "claude", "remove", "a@b.c"), (["cswap", "remove", "a@b.c"], "y"))
        self.assertEqual(actions.remote_command(box, "codex", "switch", "c@d.e")[0], ["codex-auth", "switch", "c@d.e"])
        self.assertEqual(actions.remote_command(box, "claude", "switch", None, "best")[0],
                         ["cswap", "switch", "--strategy", "best"])
        self.assertEqual(actions.remote_command(cc, "claude", "disable", "2")[0], ["ccc", "disable", "2"])
        self.assertEqual(actions.remote_command(cc, "codex", "remove", "x@y.z")[0],
                         ["ccc", "account", "remove", "codex", "x@y.z", "--yes"])
        with self.assertRaises(actions.ActionError):
            actions.remote_command(box, "codex", "disable", "1")
        with self.assertRaises(actions.ActionError):
            actions.remote_command(box, "claude", "purge", "1")
        script = remote.command_script(["cswap", "remove", "a+b@x.com"], "y")
        self.assertTrue(script.endswith("echo y | cswap remove a+b@x.com"))

    def test_interactive_scripts(self):
        s = actions.relogin_script("2", switch_first=True, restore="1")
        self.assertIn("cswap switch 2 || exit 1", s)
        self.assertLess(s.index("claude /login"), s.index("cswap switch 1 ||"))
        self.assertNotIn("cswap switch", actions.relogin_script("2", False, None))
        add = actions.add_account_script("cswap add")
        self.assertLess(add.index("claude /login"), add.index("cswap add"))
        with self.assertRaises(actions.ActionError):
            actions.relogin_script("1;id", True, None)


class WithFakes(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld().__enter__()

    def tearDown(self):
        self.world.__exit__(None, None, None)


class Transport(WithFakes):
    def test_probe_remote(self):
        self.world.add_home("box")
        state = remote.probe(m("box"))
        self.assertTrue(state.online, state.error)
        self.assertEqual(len(state.accounts("claude")), 2)
        self.assertEqual(len(state.accounts("codex")), 2)
        self.assertTrue(state.accounts("claude")[0]["disabled"])
        self.assertEqual(self.world.cswap_log("box"), [["list", "--json"]])

    def test_ccc_engine_probe(self):
        snap = {"version": 2, "providers": [
            {"id": "claude", "available": True, "accounts": [
                {"num": "1", "email": "z@x", "label": "z@x", "active": True, "needs_login": False,
                 "windows": [{"id": "5h", "pct_used": 5, "resets_in": "1h"}], "usage_stale": False}]},
            {"id": "codex", "available": True, "accounts": []}]}
        self.world.add_home("cc", cswap_fixture=None, ccc_snapshot=snap)
        state = remote.probe(m("cc", engine="ccc"))
        self.assertTrue(state.online, state.error)
        self.assertEqual(state.accounts("claude")[0]["email"], "z@x")
        self.assertEqual(self.world.cswap_log("cc", "ccc"), [["snapshot", "--no-costs"]])

    def test_offline_and_timeout_never_block(self):
        self.world.add_home("warm")
        self.assertTrue(remote.probe(m("warm")).online)
        gone = machines.Machine(id="warm", name="x", alias="down")  # same cache id, unreachable
        start = time.time()
        down, slow, nohost = remote.probe_all([gone, m("slow"), m("nohost")], timeout=2)
        self.assertLess(time.time() - start, 8)
        self.assertFalse(down.online)
        self.assertTrue(down.error.startswith("offline:"))
        self.assertEqual(len(down.accounts("claude")), 2)  # last known accounts
        self.assertIn("timed out", slow.error)
        self.assertFalse(nohost.has_data)

    def test_duplicate_detection(self):
        self.world.add_home("twin")
        a, b = m("twin", "a"), m("twin", "b")
        states = remote.probe_all([a, b])
        remote.mark_duplicates(states, [a, b])
        self.assertIsNone(states[0].duplicate_of)
        self.assertEqual(states[1].duplicate_of, "a")

    def test_remote_actions_are_quoted_and_cached(self):
        self.world.add_home("box")
        box = m("box")
        remote.probe(box)
        for provider, action, query in (("claude", "switch", "alice@example.com"),
                                        ("claude", "enable", "1"), ("claude", "remove", "2"),
                                        ("codex", "switch", "c1@example.com")):
            rc, msg = actions.account_action(box, provider, action, query)
            self.assertEqual(rc, 0, msg)
        calls = self.world.cswap_log("box")
        self.assertEqual(calls[1:], [["switch", "alice@example.com"], ["enable", "1"], ["remove", "2", "stdin:y"]])
        state = remote.cached_state(box)
        (alice,) = state.accounts("claude")  # bob removed from cache
        self.assertTrue(alice["active"])
        self.assertFalse(alice["disabled"])
        self.assertTrue(state.accounts("codex")[0]["active"])

    def test_action_on_offline_machine(self):
        rc, msg = actions.account_action(m("down"), "claude", "switch", "1")
        self.assertEqual(rc, 255)
        self.assertIn("offline", msg)

    def test_snapshot_machines_array_keeps_v2(self):
        self.world.add_home("box")
        machines.save([machines.Machine(id="local", name="Here"), m("box"), m("down", "gone"),
                       machines.Machine(id="off", name="Off", alias="box", enabled=False)])
        snap = snapshot.build_snapshot(refresh=False)
        self.assertEqual(snap["version"], 2)
        self.assertIn("costs", snap)
        self.assertEqual([x["id"] for x in snap["machines"]], ["local", "box", "gone"])
        local = snap["machines"][0]
        self.assertEqual(local["providers"], snap["providers"])
        self.assertEqual(local["engine"], "ccc")
        snap = snapshot.build_snapshot(refresh=True, machine_ids=["box", "gone"])
        box, gone = snap["machines"]
        self.assertNotIn("costs", snap)
        self.assertEqual(snap["providers"], [])
        self.assertEqual(box["engine"], "cswap")
        self.assertFalse(gone["online"])
        accts = box["providers"][0]["accounts"]
        self.assertTrue(accts[0]["disabled"])
        self.assertEqual([w["id"] for w in accts[0]["windows"]], ["5h", "wk", "fable"])
        self.assertIsNotNone(cache.read("machine-box"))


class LocalDisabled(WithFakes):
    def _slot(self, num, email):
        reg = claude._registry()
        reg["accounts"][num] = {"email": email, "organizationUuid": "", "organizationName": ""}
        reg["sequence"] = sorted({int(n) for n in reg["sequence"]} | {int(num)})
        reg["activeAccountNumber"] = 1
        from cccswitcher.storage import atomic_write_json
        atomic_write_json(claude.sequence_file(), reg)

    def test_disable_marks_and_rotation_skips(self):
        for num, email in (("1", "a@x.com"), ("2", "b@x.com"), ("3", "c@x.com")):
            self._slot(num, email)
        rc, msg = actions.account_action(actions.local_machine(), "claude", "disable", "2")
        self.assertEqual(rc, 0, msg)
        accounts, _ = claude.load_accounts(refresh=False)
        self.assertEqual([a.disabled for a in accounts], [False, True, False])
        self.assertEqual(claude._resolve(None, claude._registry()), "3")
        payload = snapshot._claude_provider(False)
        self.assertTrue(payload["accounts"][1]["disabled"])
        claude.set_disabled("b@x.com", False)
        self.assertEqual(claude._resolve(None, claude._registry()), "2")


class MachinesConfig(WithFakes):
    def setUp(self):
        super().setUp()
        (self.world.root / "starline.json").write_text(json.dumps([
            {"alias": "__starline_local__", "displayName": "Local"},
            {"alias": "path-pc", "displayName": "path-brower"},
            {"alias": "gpu", "displayName": ""},
            {"alias": "-oProxyCommand=x", "displayName": "evil"},
        ]))

    def test_defaults_from_starline(self):
        ms = machines.load()
        self.assertFalse(machines.is_customized())
        self.assertEqual([(x.id, x.alias) for x in ms], [("local", None), ("path-pc", "path-pc"), ("gpu", "gpu")])
        self.assertEqual(ms[1].name, "path-brower")
        self.assertEqual([x.resolved_engine for x in ms], ["ccc", "cswap", "cswap"])

    def test_edit_roundtrip(self):
        ms = machines.load()
        machines.add(ms, "new-box", "New Box")
        machines.rename(ms, "path-pc", "Path PC")
        machines.set_enabled(ms, "gpu", False)
        machines.set_engine(ms, "gpu", "ccc")
        machines.remove(ms, "New Box")
        machines.save(ms)
        again = machines.load()
        self.assertEqual([x.name for x in again][1:], ["Path PC", "gpu"])
        self.assertFalse(again[2].enabled)
        self.assertEqual(again[2].resolved_engine, "ccc")
        for bad in (lambda: machines.remove(again, "local"),
                    lambda: machines.add(again, "-oProxyCommand=evil"),
                    lambda: machines.add(again, "gpu"),
                    lambda: machines.set_engine(again, "local", "cswap")):
            with self.assertRaises(machines.ConfigError):
                bad()

    def test_import_merges_only_new(self):
        ms = [machines.Machine(id="local", name="here")]
        self.assertEqual([a.alias for a in machines.merge_starline(ms)], ["path-pc", "gpu"])
        self.assertEqual(machines.merge_starline(ms), [])

    def test_cli(self):
        self.assertEqual(cli.main(["machines", "add", "box", "--name", "Box"]), 0)
        self.assertEqual(cli.main(["machines", "disable", "box"]), 0)
        self.assertEqual(cli.main(["machines", "engine", "box", "ccc"]), 0)
        self.assertEqual(cli.main(["machines", "rename", "box", "Big Box"]), 0)
        saved = json.loads((self.world.root / "machines.json").read_text())
        entry = next(x for x in saved["machines"] if x["alias"] == "box")
        self.assertEqual((entry["name"], entry["enabled"], entry["engine"]), ("Big Box", False, "ccc"))
        self.assertEqual(cli.main(["machines", "remove", "Big Box"]), 0)
        self.assertEqual(cli.main(["machines", "remove", "local"]), 2)

    def test_cli_remote_actions(self):
        self.world.add_home("box")
        cli.main(["machines", "add", "box"])
        # stdin is not a TTY under the runner: remote remove refuses without --yes
        self.assertEqual(cli.main(["account", "remove", "claude", "1", "--machine", "box"]), 2)
        self.assertEqual(cli.main(["account", "remove", "claude", "1", "--machine", "box", "--yes"]), 0)
        self.assertEqual(self.world.cswap_log("box")[-1], ["remove", "1", "stdin:y"])
        self.assertEqual(cli.main(["switch", "claude", "2", "--machine", "box"]), 0)
        self.assertEqual(cli.main(["disable", "--machine", "box", "--", "2"]), 0)
        self.assertEqual(self.world.cswap_log("box")[-2:], [["switch", "2"], ["disable", "2"]])
        self.assertEqual(cli.main(["switch", "--machine", "box", "claude", "--", "-rf"]), 2)


if __name__ == "__main__":
    unittest.main()
