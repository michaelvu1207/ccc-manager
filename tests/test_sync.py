import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request  # noqa: F401 (urllib.error used below)
from pathlib import Path

from cccswitcher import claude, claude_locks, sync_agent, sync_config, sync_hub

try:
    from .sync_fakes import FakeOAuth, machine_env, on_machine, seed_login
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import FakeOAuth, machine_env, on_machine, seed_login

REPO = Path(__file__).resolve().parents[1]
SECRET = "test-secret-" + "x" * 20
EMAIL, ORG = "alice@example.com", "org-1"
KEY = sync_config.account_key("claude", EMAIL, ORG)


def run_agent(env: dict, url: str, machine: str) -> dict:
    """One agent round in its own process (its own HOME), like a real machine."""
    code = ("import json,sys; from cccswitcher import sync_agent as a; "
            f"c=a.HubClient({url!r},{SECRET!r},{machine!r}); print(json.dumps(a.run_once(c, lock_timeout=1)))")
    proc = subprocess.run([sys.executable, "-c", code], env={**os.environ, **env, "PYTHONPATH": str(REPO)},
                          capture_output=True, text=True, timeout=60, cwd=REPO)
    if proc.returncode:
        raise AssertionError(proc.stderr[-2000:])
    return json.loads(proc.stdout.strip().splitlines()[-1])


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.oauth = FakeOAuth()
        self.store = sync_hub.HubStore(
            path=self.root / "hub" / "store.json",
            refresh_fn=lambda v: claude._refresh_credentials(v, self.oauth.url))
        self.hub = sync_hub.Hub(self.store, SECRET, port=0, hub_machine="hubbox", interval=3600)
        self.hub.start()
        self.envs = {name: machine_env(self.root, name, keychain=(name == "mac"))
                     for name in ("hubbox", "mac", "linux")}

    def tearDown(self):
        self.hub.stop()
        self.oauth.close()
        self.tmp.cleanup()

    def slot_credential(self, machine: str, email: str = EMAIL) -> dict | None:
        with on_machine(self.envs[machine]):
            slot = next((s for s in claude.slots() if s["email"] == email), None)
            return sync_config.parse(claude.read_slot_credential(slot["num"], email)) if slot else None

    def live(self, machine: str) -> dict | None:
        with on_machine(self.envs[machine]):
            return sync_config.parse(claude.read_active_credential())


class SingleRefresher(SyncBase):
    def test_login_spreads_hub_refreshes_once_agents_never(self):
        now = time.time()
        cred = self.oauth.issue("g1", expires_in_s=3600, now=now)  # < 2h left: due for refresh
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        # mac pushes its new login; the other machines get a slot created.
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["pushed"], [KEY])
        for name in ("linux", "hubbox"):
            self.assertEqual(run_agent(self.envs[name], self.hub.url, name)["created"], [KEY])
        self.assertEqual(self.oauth.calls, [])  # nobody refreshed yet

        # The hub is the only refresher: one refresh, a new generation.
        self.assertEqual(self.store.refresh_due(), [KEY])
        self.assertEqual(self.store.refresh_due(), [])  # fresh now: nothing to do
        self.assertEqual(self.oauth.calls, ["g1"])

        # Several concurrent agent rounds on every machine: all converge, no refresh calls.
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda n: run_agent(self.envs[n], self.hub.url, n),
                          ["mac", "linux", "hubbox"] * 2))
        hub_token = sync_config.token_id(self.store.credentials("x", False, "hubbox")[0]["credential"])
        for name in ("mac", "linux", "hubbox"):
            self.assertEqual(sync_config.token_id(self.slot_credential(name)), hub_token, name)
        self.assertEqual(sync_config.token_id(self.live("mac")), hub_token)  # active login updated
        self.assertEqual(self.oauth.calls, ["g1"])
        self.assertEqual(self.oauth.revoked, set())

        # The next rotation still works: the hub holds the only valid refresh token.
        entry = self.store._load()["accounts"][KEY]
        entry["credential"]["claudeAiOauth"]["expiresAt"] = int((time.time() + 60) * 1000)
        entry["last_refresh_attempt"] = 0
        self.store._save({**self.store._load(), "accounts": {KEY: entry}})
        self.assertEqual(self.store.refresh_due(), [KEY])
        self.assertEqual(self.oauth.revoked, set())

    def test_machine_that_refreshed_itself_is_adopted(self):
        cred = self.oauth.issue("g1", expires_in_s=8 * 3600, now=time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        run_agent(self.envs["mac"], self.hub.url, "mac")
        run_agent(self.envs["linux"], self.hub.url, "linux")
        gen = self.store._load()["accounts"][KEY]["generation"]

        # Claude Code on mac refreshes on its own (rotates the refresh token).
        rotated = self.oauth.refresh_as_client(cred)
        rotated["claudeAiOauth"]["expiresAt"] += 1000  # later than the hub copy
        with on_machine(self.envs["mac"]):
            claude.write_active_credential(json.dumps(rotated))
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["pushed"], [KEY])
        self.assertEqual(self.store._load()["accounts"][KEY]["generation"], gen + 1)
        run_agent(self.envs["linux"], self.hub.url, "linux")
        self.assertEqual(sync_config.token_id(self.slot_credential("linux")), sync_config.token_id(rotated))
        # Hub now refreshes with the adopted (valid) refresh token: no reuse, no revoke.
        entry = self.store._load()
        entry["accounts"][KEY]["credential"]["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000)
        self.store._save(entry)
        self.assertEqual(self.store.refresh_due(), [KEY])
        self.assertEqual(self.oauth.revoked, set())

    def test_older_copy_is_never_adopted(self):
        now = time.time()
        new = self.oauth.issue("g1", 8 * 3600, now)
        old = json.loads(json.dumps(new))
        old["claudeAiOauth"]["accessToken"] = "at-old"
        old["claudeAiOauth"]["expiresAt"] -= 3_600_000
        payload = {"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG}
        self.assertTrue(self.store.offer({**payload, "credential": new})["adopted"])
        answer = self.store.offer({**payload, "credential": old})
        self.assertEqual((answer["adopted"], answer["reason"]), (False, "not newer"))
        self.assertFalse(self.store.offer({**payload, "credential": new})["adopted"])  # same token
        self.assertFalse(self.store.offer({**payload, "credential": {"x": 1}})["adopted"])
        self.assertFalse(self.store.offer({**payload, "provider": "codex", "credential": new})["adopted"])

    def test_fake_endpoint_punishes_two_refreshers(self):
        """Control: two parties refreshing one login kills it — what the hub prevents."""
        cred = self.oauth.issue("g9", 60, time.time())
        self.assertIsNotNone(self.oauth.refresh_as_client(cred))  # first refresher wins
        self.assertIsNone(self.oauth.refresh_as_client(cred))     # second reuses the old token
        self.assertEqual(self.oauth.revoked, {"g9"})

    def test_failed_refresh_is_recorded_and_backed_off(self):
        cred = self.oauth.issue("g1", 60, time.time())
        self.oauth.revoked.add("g1")
        self.store.offer({"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": cred})
        self.assertEqual(self.store.refresh_due(), [])
        self.assertIn("refresh failed", self.store.status()["accounts"][0]["refresh_error"])
        self.store.refresh_due()  # within the retry window: no second call
        self.assertEqual(self.oauth.calls, [])


class LocksSharingOffline(SyncBase):
    def _push_from_mac(self, expires_in=8 * 3600):
        cred = self.oauth.issue("g1", expires_in, time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        run_agent(self.envs["mac"], self.hub.url, "mac")
        return cred

    def test_claude_code_lock_blocks_live_write_but_not_slot(self):
        self._push_from_mac()
        # linux has alice as its ACTIVE login with an older copy
        stale = self.oauth.issue("g2", 3600, time.time() - 7200)
        stale["claudeAiOauth"]["expiresAt"] = int((time.time() - 10) * 1000)
        with on_machine(self.envs["linux"]):
            seed_login(EMAIL, ORG, stale, active=True)
            lock_dirs = claude_locks.lock_dirs()
        lock_dirs[0].mkdir(parents=True)  # a live Claude Code refresh holds the lock
        summary = run_agent(self.envs["linux"], self.hub.url, "linux")
        self.assertTrue(summary["skipped_busy"])
        self.assertEqual(sync_config.token_id(self.live("linux")), sync_config.token_id(stale))
        self.assertNotEqual(sync_config.token_id(self.slot_credential("linux")), sync_config.token_id(stale))
        lock_dirs[0].rmdir()
        summary = run_agent(self.envs["linux"], self.hub.url, "linux")
        self.assertTrue(summary["active_updated"])
        self.assertFalse(any(p.exists() for p in lock_dirs))  # released

    def test_withhold_refresh_token_option(self):
        self._push_from_mac()
        creds = self.store.credentials("linux", withhold=True, hub_machine="hubbox")
        self.assertNotIn("refreshToken", creds[0]["credential"]["claudeAiOauth"])
        creds = self.store.credentials("hubbox", withhold=True, hub_machine="hubbox")
        self.assertIn("refreshToken", creds[0]["credential"]["claudeAiOauth"])

    def test_stop_sharing(self):
        self._push_from_mac()
        self.store.set_shared(KEY, False)
        self.assertNotIn("credential", self.store.credentials("linux", False, "hubbox")[0])
        summary = run_agent(self.envs["mac"], self.hub.url, "mac")  # does not re-upload
        self.assertEqual(summary["pushed"], [])
        self.assertEqual(run_agent(self.envs["linux"], self.hub.url, "linux")["created"], [])

    def test_offline_hub(self):
        with on_machine(self.envs["mac"]):
            client = sync_agent.HubClient("http://127.0.0.1:9", SECRET, "mac", timeout=2)
            start = time.time()
            with self.assertRaises(sync_agent.HubError) as err:
                sync_agent.run_once(client)
            self.assertLess(time.time() - start, 5)
            self.assertIn("unreachable", str(err.exception))

    def test_http_auth_and_limits(self):
        def call(path, secret=SECRET, body=None):
            req = urllib.request.Request(self.hub.url + path, data=body, method="POST" if body else "GET",
                                         headers={"Authorization": f"Bearer {secret}"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        import urllib.error
        self.assertEqual(call("/v1/health", secret=""), 200)
        self.assertEqual(call("/v1/credentials", secret="wrong"), 401)
        self.assertEqual(call("/v1/credentials"), 200)
        self.assertEqual(call("/v1/share", body=b'{"key":"bad"}'), 400)
        self.assertFalse(sync_hub._peer_allowed("8.8.8.8", loopback_only=False))
        self.assertTrue(sync_hub._peer_allowed("100.101.1.2", loopback_only=False))
        self.assertFalse(sync_hub._peer_allowed("100.101.1.2", loopback_only=True))
        self.assertTrue(sync_hub._peer_allowed("127.0.0.1", loopback_only=True))

    def test_files_are_private_and_nothing_leaks(self):
        self._push_from_mac()
        mode = stat.S_IMODE(self.store.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        with on_machine(self.envs["linux"]):
            sync_config.save({"hub_url": self.hub.url, "secret": SECRET, "machine": "linux"})
            self.assertEqual(stat.S_IMODE(sync_config.config_path().stat().st_mode), 0o600)
        run_agent(self.envs["linux"], self.hub.url, "linux")
        state = (Path(self.envs["linux"]["CCC_SYNC_FILE"]).with_name("sync-state.json"))
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        text = json.dumps(self.store.status())
        self.assertNotIn("at-", text)
        self.assertNotIn("rt-", text)
        self.assertEqual(sync_config.redact("boom sk-ant-oat01-abcdef123 and eyJhbGciOi.x.y"),
                         "boom [redacted] and [redacted]")


if __name__ == "__main__":
    unittest.main()
