import json
import os
import tempfile
import unittest
from pathlib import Path

from cccswitcher import proxy


def usage(*windows):
    return {"windows": [{"kind": k, "name": n, "pct": p, "resets_at": r} for k, n, p, r in windows]}


class Decide(unittest.TestCase):
    config = {"caps": {"claude": 95.0, "codex": 99.0}, "short_cap": 98.0}

    def test_under_every_cap_stays_on(self):
        want = proxy.decide("claude", usage(("week", "week", 94.9, 100), ("short", "5h", 97, 50)), self.config)
        self.assertEqual((want["disable"], want["exclude"]), (False, []))

    def test_week_cap_turns_the_account_off_until_the_reset(self):
        want = proxy.decide("claude", usage(("week", "week", 95, 1000)), self.config)
        self.assertTrue(want["disable"])
        self.assertEqual(want["until"], 1000)

    def test_codex_uses_its_own_cap(self):
        self.assertFalse(proxy.decide("codex", usage(("week", "week", 98, 1)), self.config)["disable"])
        self.assertTrue(proxy.decide("codex", usage(("week", "week", 99, 1)), self.config)["disable"])

    def test_short_window_has_its_own_cap(self):
        self.assertFalse(proxy.decide("claude", usage(("short", "5h", 97.9, 1)), self.config)["disable"])
        self.assertTrue(proxy.decide("claude", usage(("short", "5h", 98, 1)), self.config)["disable"])

    def test_model_cap_only_excludes_that_model(self):
        want = proxy.decide("claude", usage(("week", "week", 40, 1), ("model", "Fable", 96, 1)), self.config)
        self.assertFalse(want["disable"])
        self.assertEqual(want["exclude"], ["claude-fable-*"])

    def test_model_pattern(self):
        self.assertEqual(proxy.model_pattern("Fable 5"), "claude-fable-*")
        self.assertEqual(proxy.model_pattern(""), "")


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "CCC_PROXY_FILE")}
        os.environ["HOME"] = self.tmp.name
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "CCC_PROXY_FILE"):
            os.environ.pop(key, None)
        self.auth = Path(self.tmp.name) / "auth"
        self.auth.mkdir()
        self.config = {**proxy.DEFAULTS, "auth_dir": str(self.auth)}
        self.calls = []
        self.readings = {}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def account(self, name, provider="claude", **extra):
        (self.auth / name).write_text(json.dumps({"type": provider, "email": name, "access_token": "t", **extra}))

    def apply(self, config, path, body):
        """The fake management API: record the call and change the auth file as the proxy does."""
        self.calls.append((path, body))
        file = self.auth / body["name"]
        data = json.loads(file.read_text())
        data.update({k: v for k, v in body.items() if k != "name"})
        file.write_text(json.dumps(data))

    def round(self, now):
        return proxy.guard_round(self.config, now=now, fetch=lambda a: self.readings[a["name"]], apply=self.apply)

    def test_off_at_the_cap_and_on_after_the_reset(self):
        self.account("a.json")
        self.readings["a.json"] = usage(("week", "week", 96, 5000))
        state = self.round(1000)
        self.assertEqual(self.calls, [("auth-files/status", {"name": "a.json", "disabled": True})])
        self.assertTrue(state["accounts"]["a.json"]["disabled_by_guard"])
        self.round(1100)
        self.assertEqual(len(self.calls), 1)  # already off: nothing more to do
        self.readings["a.json"] = usage(("week", "week", 0, 9000))
        state = self.round(6000)
        self.assertEqual(self.calls[-1], ("auth-files/status", {"name": "a.json", "disabled": False}))
        self.assertFalse(state["accounts"]["a.json"]["disabled"])

    def test_an_account_turned_off_by_hand_stays_off(self):
        self.account("a.json", disabled=True)
        self.readings["a.json"] = usage(("week", "week", 1, 5000))
        self.round(1000)
        self.assertEqual(self.calls, [])

    def test_model_limit_excludes_and_restores(self):
        self.account("a.json")
        self.readings["a.json"] = usage(("week", "week", 10, 5000), ("model", "Fable", 99, 5000))
        self.round(1000)
        self.assertEqual(self.calls, [("auth-files/fields", {"name": "a.json", "excluded_models": ["claude-fable-*"]})])
        self.readings["a.json"] = usage(("week", "week", 10, 5000), ("model", "Fable", 2, 9000))
        self.round(2000)
        self.assertEqual(self.calls[-1], ("auth-files/fields", {"name": "a.json", "excluded_models": []}))

    def test_exclusions_set_by_someone_else_are_left_alone(self):
        self.account("a.json", excluded_models=["claude-opus-*"])
        self.readings["a.json"] = usage(("model", "Fable", 99, 5000))
        self.round(1000)
        self.assertEqual(self.calls, [])

    def test_unknown_usage_turns_the_account_off_only_after_the_wait(self):
        self.account("x.json", provider="codex")
        self.readings["x.json"] = usage(("week", "week", 10, 5000))
        self.round(1000)
        self.readings["x.json"] = {"error": "usage read failed"}
        self.round(1000 + 600)
        self.assertEqual(self.calls, [])
        state = self.round(1000 + 901)
        self.assertEqual(self.calls, [("auth-files/status", {"name": "x.json", "disabled": True})])
        self.assertEqual(state["accounts"]["x.json"]["reason"], "usage unknown")
        self.readings["x.json"] = usage(("week", "week", 10, 5000))
        self.round(3000)
        self.assertEqual(self.calls[-1], ("auth-files/status", {"name": "x.json", "disabled": False}))

    def test_state_file_holds_no_token(self):
        self.account("a.json")
        self.readings["a.json"] = usage(("week", "week", 10, 5000))
        self.round(1000)
        self.assertNotIn("access_token", proxy.state_path().read_text())
        self.assertIn("a.json", proxy.render(json.loads(proxy.state_path().read_text()), now=1010))


class Clients(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        home = Path(self.tmp.name)
        os.environ.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"), CODEX_HOME=str(home / ".codex"))
        (home / ".claude").mkdir()
        (home / ".codex").mkdir()
        self.settings, self.toml = home / ".claude" / "settings.json", home / ".codex" / "config.toml"

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def test_on_then_off_restores_both_files(self):
        self.settings.write_text(json.dumps({"model": "m", "env": {"KEEP": "1"}}))
        before = 'model = "gpt"\n\n[features]\ngoals = true\n'
        self.toml.write_text(before)
        proxy.point_on("http://127.0.0.1:8317/", "secret-key")
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["env"], {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                                           "ANTHROPIC_AUTH_TOKEN": "secret-key"})
        text = self.toml.read_text()
        self.assertTrue(text.startswith('model_provider = "cliproxyapi"'))  # before the first table
        self.assertIn('base_url = "http://127.0.0.1:8317/v1"', text)
        self.assertEqual(proxy.pointed(), {"claude": "http://127.0.0.1:8317", "codex": True})
        proxy.point_on("http://other:1", "key2")  # a second `on` replaces, never doubles
        self.assertEqual(self.toml.read_text().count("[model_providers.cliproxyapi]"), 1)
        proxy.point_off()
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "m", "env": {"KEEP": "1"}})
        self.assertEqual(self.toml.read_text().strip(), before.strip())
        self.assertEqual(proxy.pointed(), {"claude": None, "codex": False})

    def test_an_own_model_provider_is_not_overwritten(self):
        self.toml.write_text('model_provider = "mine"\n')
        with self.assertRaises(ValueError):
            proxy.patch_codex(self.toml.read_text(), "http://x", "k")

    def test_login_command_tunnels_the_callback_port(self):
        argv = proxy.login_command({**proxy.DEFAULTS, "host": "box"}, "claude")
        self.assertEqual(argv[:4], ["ssh", "-t", "-L", "54545:127.0.0.1:54545"])
        self.assertIn("-claude-login -no-browser", argv[-1])
        self.assertNotIn("-no-browser", proxy.login_command(dict(proxy.DEFAULTS), "codex")[-1])


if __name__ == "__main__":
    unittest.main()
