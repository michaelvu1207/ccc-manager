import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cccswitcher import claude, services, sync_agent, sync_config, sync_login

try:
    from .sync_fakes import on_machine, seed_login
    from .test_sync import EMAIL, KEY, ORG, REPO, SECRET, SyncBase
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import on_machine, seed_login
    from test_sync import EMAIL, KEY, ORG, REPO, SECRET, SyncBase

FAKE_CLAUDE = """#!{python}
# Fake Claude Code: `auth login` saves a login for CLAUDE_CONFIG_DIR, like the real one.
import json, os, sys
sys.path.insert(0, {repo!r})
args = sys.argv[1:]
if args[:3] == ["auth", "login", "--help"]:
    sys.exit(0)
if os.environ.get("FAKE_CLAUDE_FAIL"):
    sys.exit(1)
cfg = os.environ["CLAUDE_CONFIG_DIR"]
email = args[args.index("--email") + 1] if "--email" in args else os.environ["FAKE_CLAUDE_EMAIL"]
with open(os.path.join(cfg, ".claude.json"), "w") as fh:
    json.dump({{"oauthAccount": {{"emailAddress": email, "organizationUuid": "org-1",
                                "organizationName": "Org", "accountUuid": "u-1"}}}}, fh)
cred = json.dumps({{"claudeAiOauth": {{"accessToken": os.environ["FAKE_CLAUDE_AT"],
                   "refreshToken": "rt-fresh", "expiresAt": {expires}, "scopes": []}}}})
if os.environ.get("CCC_FAKE_KEYCHAIN_DIR"):
    from cccswitcher import claude, sync_login
    claude._keychain_set(sync_login.keychain_service_for(cfg), sync_login.keychain_account(), cred)
else:
    open(os.path.join(cfg, ".credentials.json"), "w").write(cred)
"""


class OneButtonLogin(SyncBase):
    def fake_claude(self) -> str:
        path = self.root / "fake-claude"
        path.write_text(FAKE_CLAUDE.format(python=sys.executable, repo=str(REPO),
                                           expires=int((time.time() + 8 * 3600) * 1000)))
        path.chmod(0o755)
        return str(path)

    def run_login(self, machine: str, email=None, token="at-fresh-1", fail=False):
        env = {**self.envs[machine], "FAKE_CLAUDE_EMAIL": "new@example.com", "FAKE_CLAUDE_AT": token}
        if fail:
            env["FAKE_CLAUDE_FAIL"] = "1"
        out = io.StringIO()
        with on_machine(env):
            client = sync_agent.HubClient(self.hub.url, SECRET, machine)
            with redirect_stdout(out):
                rc = sync_login.login(client, email=email, claude_bin=self.fake_claude(), wait_s=0, poll_s=0.1)
            leftovers = list((Path(env["HOME"]) / ".cache" / "ccc").glob("login-*"))
            keychain = list(Path(env["CCC_FAKE_KEYCHAIN_DIR"]).glob("*")) if env.get("CCC_FAKE_KEYCHAIN_DIR") else []
        return rc, out.getvalue(), leftovers, keychain

    def test_login_uploads_and_cleans_up_keychain_machine(self):
        live = self.oauth.issue("g0", 8 * 3600, time.time())
        with on_machine(self.envs["mac"]):
            seed_login("current@example.com", "org-9", live, active=True)
            before = claude.read_active_credential()
            kept_items = set(Path(self.envs["mac"]["CCC_FAKE_KEYCHAIN_DIR"]).glob("*"))
        rc, out, leftovers, keychain = self.run_login("mac")
        self.assertEqual(rc, 0, out)
        key = sync_config.account_key("claude", "new@example.com", "org-1")
        entry = self.store._load()["accounts"][key]
        self.assertEqual(entry["credential"]["claudeAiOauth"]["accessToken"], "at-fresh-1")
        self.assertEqual(entry["oauth_account"]["emailAddress"], "new@example.com")
        self.assertIn("Added new@example.com. Syncing to", out)
        self.assertNotIn("at-fresh-1", out)
        self.assertNotIn("rt-fresh", out)
        self.assertEqual(leftovers, [])                      # temp profile deleted
        self.assertEqual(set(keychain), kept_items)          # temp keychain item deleted
        with on_machine(self.envs["mac"]):
            self.assertEqual(claude.read_active_credential(), before)  # active login untouched

    def test_login_file_store_and_replace_existing(self):
        old = self.oauth.issue("g1", 9 * 3600, time.time())  # expires later than the fresh login
        self.store.offer({"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": old})
        rc, out, leftovers, _ = self.run_login("linux", email=EMAIL, token="at-relogin")
        self.assertEqual(rc, 0, out)
        entry = self.store._load()["accounts"][KEY]
        self.assertEqual(entry["credential"]["claudeAiOauth"]["accessToken"], "at-relogin")
        self.assertEqual(entry["generation"], 2)
        self.assertEqual(leftovers, [])

    def test_failed_login_uploads_nothing_and_cleans_up(self):
        rc, out, leftovers, _ = self.run_login("linux", fail=True)
        self.assertEqual(rc, 1)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(self.store._load()["accounts"], {})
        self.assertEqual(leftovers, [])

    def test_offline_hub_discards_login(self):
        env = {**self.envs["linux"], "FAKE_CLAUDE_EMAIL": "x@example.com", "FAKE_CLAUDE_AT": "at-x"}
        out = io.StringIO()
        with on_machine(env), redirect_stdout(out):
            client = sync_agent.HubClient("http://127.0.0.1:9", SECRET, "linux", timeout=2)
            rc = sync_login.login(client, claude_bin=self.fake_claude(), wait_s=0)
            leftovers = list((Path(env["HOME"]) / ".cache" / "ccc").glob("login-*"))
        self.assertEqual(rc, 1)
        self.assertIn("discarded", out.getvalue())
        self.assertEqual(leftovers, [])

    def test_waits_for_machines(self):
        self.store.report({"machine": "path-pc", "generations": {}})
        key = sync_config.account_key("claude", "new@example.com", "org-1")

        def later_report():
            time.sleep(0.5)
            self.store.report({"machine": "path-pc", "generations": {key: 1}})
        import threading
        threading.Thread(target=later_report, daemon=True).start()
        env = {**self.envs["linux"], "FAKE_CLAUDE_EMAIL": "new@example.com", "FAKE_CLAUDE_AT": "at-w"}
        out = io.StringIO()
        with on_machine(env), redirect_stdout(out):
            client = sync_agent.HubClient(self.hub.url, SECRET, "linux")
            rc = sync_login.login(client, claude_bin=self.fake_claude(), wait_s=5, poll_s=0.1)
        self.assertEqual(rc, 0)
        self.assertIn("Syncing to 1 machine", out.getvalue())
        self.assertIn("✓ path-pc", out.getvalue())
        self.assertIn("Every machine has it.", out.getvalue())


class Helpers(unittest.TestCase):
    def test_keychain_service_matches_claude_code(self):
        import hashlib
        path = "/Users/x/.cache/ccc/login-abc"
        self.assertEqual(sync_login.keychain_service_for(path),
                         "Claude Code-credentials-" + hashlib.sha256(path.encode()).hexdigest()[:8])

    def test_systemctl_env_filled_when_missing(self):
        saved = {k: os.environ.pop(k, None) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        try:
            env = services.user_bus_env()
            runtime = f"/run/user/{os.getuid()}"
            if Path(runtime).is_dir():
                self.assertEqual(env["XDG_RUNTIME_DIR"], runtime)
            else:
                self.assertNotIn("XDG_RUNTIME_DIR", env)  # nothing invented on this host
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_source_root_from_env(self):
        from cccswitcher import remote_install
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text("[project]\n")
            os.environ["CCC_SOURCE_DIR"] = tmp
            try:
                self.assertEqual(remote_install.source_root(), Path(tmp))
                self.assertEqual(remote_install.source_root(Path(tmp)), Path(tmp))
            finally:
                os.environ.pop("CCC_SOURCE_DIR")


if __name__ == "__main__":
    unittest.main()
