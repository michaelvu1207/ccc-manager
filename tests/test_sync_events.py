import json
import os
import subprocess
import sys
import time
import unittest

from cccswitcher import sync_config, sync_hub

try:
    from .sync_fakes import on_machine, seed_login
    from .test_sync import EMAIL, KEY, ORG, REPO, SECRET, SyncBase, run_agent
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import on_machine, seed_login
    from test_sync import EMAIL, KEY, ORG, REPO, SECRET, SyncBase, run_agent


def wait_until(predicate, timeout=15.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


class EventSync(SyncBase):
    def setUp(self):
        super().setUp()
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        super().tearDown()

    def start_agent(self, name: str, local_interval=1.0, wait_timeout=2.0):
        env = {**os.environ, **self.envs[name], "PYTHONPATH": str(REPO)}
        with on_machine(self.envs[name]):
            sync_config.save({"hub_url": self.hub.url, "secret": SECRET, "machine": name})
        proc = subprocess.Popen(
            [sys.executable, "-m", "cccswitcher.cli", "sync", "agent", "--local-interval", str(local_interval),
             "--wait-timeout", str(wait_timeout), "--full-interval", "3600"],
            env=env, cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def token(self, machine):
        return sync_config.token_id(self.slot_credential(machine))

    def hub_offer_newer(self):
        """Another machine uploads a newer login (bumps the hub version)."""
        newer = self.oauth.issue("g1", 9 * 3600, time.time())
        answer = self.store.offer({"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG,
                                   "credential": newer, "machine": "elsewhere"})
        self.assertTrue(answer["adopted"])
        return sync_config.token_id(newer)

    def test_change_propagates_idle_is_quiet_local_login_detected(self):
        cred = self.oauth.issue("g1", 8 * 3600, time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        run_agent(self.envs["mac"], self.hub.url, "mac")
        self.start_agent("linux")
        self.assertTrue(wait_until(lambda: self.slot_credential("linux") is not None), "initial full sync")

        # (a) hub change reaches the agent through the long-poll
        new_token = self.hub_offer_newer()
        start = time.time()
        self.assertTrue(wait_until(lambda: self.token("linux") == new_token, timeout=5))
        self.assertLess(time.time() - start, 3.5)

        # (d) idle: only long-poll requests, no pulls, pushes or reports
        time.sleep(1.5)
        self.hub.counts.clear()
        time.sleep(5)
        busy = {k: v for k, v in self.hub.counts.items() if k != "GET /v1/credentials/wait"}
        self.assertEqual(busy, {})
        self.assertLessEqual(self.hub.counts.get("GET /v1/credentials/wait", 0), 4)

        # (b) a new login on linux is found by the local check and uploaded
        other = self.oauth.issue("g2", 8 * 3600, time.time())
        with on_machine(self.envs["linux"]):
            seed_login("bob@example.com", "org-2", other, active=False)
        bob = sync_config.account_key("claude", "bob@example.com", "org-2")
        self.assertTrue(wait_until(lambda: bob in self.store._load()["accounts"], timeout=5))
        self.assertEqual(self.oauth.calls, [])  # agents never refresh

    def test_agent_reconnects_after_hub_restart(self):
        cred = self.oauth.issue("g1", 8 * 3600, time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        run_agent(self.envs["mac"], self.hub.url, "mac")
        self.start_agent("linux")
        self.assertTrue(wait_until(lambda: self.slot_credential("linux") is not None))
        port = self.hub.server.server_address[1]
        self.hub.stop()
        time.sleep(2)  # agent sees errors and backs off
        self.store = sync_hub.HubStore(path=self.store.path, refresh_fn=self.store.refresh_fn)
        self.hub = sync_hub.Hub(self.store, SECRET, port=port, hub_machine="hubbox", interval=3600)
        self.hub.start()
        new_token = self.hub_offer_newer()
        self.assertTrue(wait_until(lambda: self.token("linux") == new_token, timeout=20))

    def test_report_only_on_change(self):
        cred = self.oauth.issue("g1", 8 * 3600, time.time())
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, cred, active=True)
        first = run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertTrue(first["reported"])
        self.assertFalse(run_agent(self.envs["mac"], self.hub.url, "mac")["reported"])  # nothing changed
        self.hub_offer_newer()
        self.assertTrue(run_agent(self.envs["mac"], self.hub.url, "mac")["reported"])

    def test_long_poll_endpoint(self):
        import urllib.error
        import urllib.request

        def get(since, timeout):
            req = urllib.request.Request(f"{self.hub.url}/v1/credentials/wait?since={since}&timeout={timeout}",
                                         headers={"Authorization": f"Bearer {SECRET}"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        start = time.time()
        self.assertEqual(get(0, 1)[0], 304)
        self.assertGreaterEqual(time.time() - start, 0.9)
        self.hub_offer_newer()
        code, body = get(0, 30)
        self.assertEqual((code, body["hub_version"]), (200, 1))
        self.assertEqual(get("x", 1)[0], 400)


class ReplaceFlag(SyncBase):
    def test_fresh_login_replaces_dead_hub_copy(self):
        now = time.time()
        live = self.oauth.issue("g1", 9 * 3600, now)
        self.store.offer({"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": live})
        relogin = self.oauth.issue("g2", 8 * 3600, now)  # expires earlier, but it is a fresh login
        base = {"key": KEY, "provider": "claude", "email": EMAIL, "org_uuid": ORG, "credential": relogin}
        self.assertFalse(self.store.offer(base)["adopted"])
        self.assertEqual(self.store.offer({**base, "replace": True})["reason"], "replaced")


if __name__ == "__main__":
    unittest.main()
