"""0.21.1: CCC's local engine never refreshes a shared login; fresh slots are
"usage pending", not "needs login".

Everything runs in-process on fake machines (on_machine) against a real hub
object, the fake OAuth token endpoint, and a fake usage endpoint.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cccswitcher import accounts_view, claude, snapshot, sync_agent, sync_config, sync_hub

try:
    from .sync_fakes import FakeOAuth, FakeUsage, machine_env, on_machine, seed_login, write_sync_config
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import FakeOAuth, FakeUsage, machine_env, on_machine, seed_login, write_sync_config

SECRET = "test-secret-" + "y" * 20
EMAIL, ORG = "alice@example.com", "org-1"
KEY = sync_config.account_key("claude", EMAIL, ORG)


class SharedRefreshBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.oauth = FakeOAuth()
        self.usage = FakeUsage()
        self.store = sync_hub.HubStore(
            path=self.root / "hub" / "store.json",
            refresh_fn=lambda v: claude._refresh_credentials(v, self.oauth.url))
        self.hub = sync_hub.Hub(self.store, SECRET, port=0, hub_machine="hubbox", interval=3600)
        self.hub.start()
        self.envs = {name: machine_env(self.root, name, keychain=(name == "mac"))
                     for name in ("mac", "linux", "solo")}
        # Every local token/usage call goes to the fakes, never the network.
        self.patches = [patch.object(claude, "TOKEN_URL", self.oauth.url),
                        patch.object(claude, "USAGE_API_URL", self.usage.url)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.hub.stop()
        self.oauth.close()
        self.usage.close()
        self.tmp.cleanup()

    def share_on_hub(self, credential: dict) -> None:
        answer = self.store.offer({
            "machine": "elsewhere", "key": KEY, "provider": "claude", "email": EMAIL,
            "org_uuid": ORG, "org_name": "Alice's Organization",
            "oauth_account": {"emailAddress": EMAIL, "organizationUuid": ORG}, "credential": credential,
        })
        self.assertTrue(answer["adopted"], answer)

    def client(self, machine: str) -> sync_agent.HubClient:
        return sync_agent.HubClient(self.hub.url, SECRET, machine)

    def join(self, machine: str) -> None:
        with on_machine(self.envs[machine]):
            write_sync_config(self.hub.url, SECRET, machine)

    def account(self, refresh: bool) -> claude.ClaudeAccount:
        accounts, _ = claude.load_accounts(refresh=refresh)
        return next(a for a in accounts if a.email == EMAIL)

    def local_record(self) -> dict:
        provider = snapshot._claude_provider(False)
        return next(a for a in provider["accounts"] if a["email"] == EMAIL)


class NeverRefreshSharedLocally(SharedRefreshBase):
    def test_expired_shared_slot_waits_for_hub_then_gets_usage(self):
        self.share_on_hub(self.oauth.issue("g1", expires_in_s=-60, now=time.time()))  # already expired
        self.join("mac")
        with on_machine(self.envs["mac"]):
            summary = sync_agent.run_once(self.client("mac"), lock_timeout=1)
            self.assertEqual(summary["created"], [KEY])
            self.assertEqual(summary["usage_updated"], [])  # expired copy: no usage call

            # A full local refresh (the app's snapshot path, also after wake).
            acct = self.account(refresh=True)
            self.assertEqual(self.oauth.calls, [])          # token endpoint never called
            self.assertEqual(self.usage.calls, [])          # expired token: usage skipped
            self.assertEqual(acct.usage, claude.WAITING_FOR_HUB)
            self.assertTrue(acct.shared)
            self.assertFalse(claude.needs_login(acct))
            record = self.local_record()
            self.assertEqual(record["status"], "pending")
            self.assertEqual(record["status_message"], "waiting for hub")
            self.assertFalse(record["needs_login"])

        # The hub, the one refresher, refreshes it.
        self.assertEqual(self.store.refresh_due(), [KEY])
        self.assertEqual(self.oauth.calls, ["g1"])

        with on_machine(self.envs["mac"]):
            # The engine sees the stale copy, pulls once from the hub, reads usage.
            acct = self.account(refresh=True)
            self.assertIsInstance(acct.usage, dict)
            self.assertEqual(acct.usage["five_hour"]["pct"], 12.0)
        self.assertEqual(self.oauth.calls, ["g1"])  # still only the hub's refresh
        self.assertEqual(self.oauth.revoked, set())

    def test_near_expiry_shared_slot_reads_usage_without_refresh(self):
        self.share_on_hub(self.oauth.issue("g1", expires_in_s=120, now=time.time()))  # < 5 min left
        self.join("mac")
        with on_machine(self.envs["mac"]):
            sync_agent.run_once(self.client("mac"), lock_timeout=1)
            acct = self.account(refresh=True)
        self.assertEqual(self.oauth.calls, [])
        self.assertIsInstance(acct.usage, dict)

    def test_old_agent_state_and_agent_created_slot_count_as_shared(self):
        # A 0.21.0 sync-state.json (no "hub_shared") and a slot the agent made.
        self.join("mac")
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, self.oauth.issue("g1", expires_in_s=-60, now=time.time()), active=False)
            sync_agent.save_state({"accounts": {}})
            acct = self.account(refresh=True)
        self.assertEqual(self.oauth.calls, [])
        self.assertEqual(acct.usage, claude.WAITING_FOR_HUB)

    def test_live_copy_older_than_pulled_slot_is_not_saved_over_it(self):
        old = self.oauth.issue("g1", expires_in_s=600, now=time.time())
        self.join("mac")
        with on_machine(self.envs["mac"]):
            seed_login(EMAIL, ORG, old, active=True)
            sync_agent.run_once(self.client("mac"), lock_timeout=1)  # pushes: now hub-shared
        self.assertEqual(self.store.refresh_due(), [KEY])
        newer = self.store._load()["accounts"][KEY]["credential"]
        with on_machine(self.envs["mac"]):
            num = next(s["num"] for s in claude.slots() if s["email"] == EMAIL)
            claude.write_slot_credential(num, EMAIL, json.dumps(newer))  # pulled; live not yet replaced
            claude.sync_active_credentials()
            self.assertEqual(sync_config.token_id(claude.read_slot_credential(num, EMAIL)),
                             sync_config.token_id(newer))


class UnsharedKeepsRefreshing(SharedRefreshBase):
    def test_sync_off_refreshes_as_before(self):
        with on_machine(self.envs["solo"]):
            seed_login(EMAIL, ORG, self.oauth.issue("g9", expires_in_s=60, now=time.time()), active=False)
            acct = self.account(refresh=True)
        self.assertEqual(self.oauth.calls, ["g9"])
        self.assertIsInstance(acct.usage, dict)
        self.assertFalse(acct.shared)

    def test_sync_on_but_account_not_shared_refreshes_as_before(self):
        self.join("solo")
        with on_machine(self.envs["solo"]):
            seed_login(EMAIL, ORG, self.oauth.issue("g9", expires_in_s=60, now=time.time()), active=False)
            sync_agent.save_state({"accounts": {}, "hub_shared": []})  # hub shares nothing
            acct = self.account(refresh=True)
        self.assertEqual(self.oauth.calls, ["g9"])
        self.assertIsInstance(acct.usage, dict)


class UsagePending(SharedRefreshBase):
    def test_new_slot_is_usage_pending_then_real_usage_after_pull(self):
        self.share_on_hub(self.oauth.issue("g1", expires_in_s=3600, now=time.time()))
        self.join("mac")
        with on_machine(self.envs["mac"]):
            sync_agent.run_once(self.client("mac"), lock_timeout=1, fetch_usage=False)
            acct = self.account(refresh=False)
            self.assertEqual(acct.usage, claude.USAGE_PENDING)
            self.assertFalse(claude.needs_login(acct))
            record = self.local_record()
            self.assertEqual((record["status"], record["status_message"], record["needs_login"]),
                             ("pending", "usage pending", False))
            view = accounts_view.build([{"id": "local", "local": True, "online": True,
                                         "providers": [snapshot._claude_provider(False)]}])
            chip = view["accounts"][0]["machines"]["local"]
            self.assertEqual(chip["state"], "pending")
            self.assertEqual(chip["note"], "usage pending")

        # The hub refreshes; the next pull updates the slot and reads usage.
        self.store.refresh_due()
        with on_machine(self.envs["mac"]):
            summary = sync_agent.run_once(self.client("mac"), lock_timeout=1)
            self.assertEqual(summary["pulled"], [KEY])
            self.assertEqual(summary["usage_updated"], [KEY])
            acct = self.account(refresh=False)
            self.assertIsInstance(acct.usage, dict)
            self.assertEqual(self.local_record()["status"], "ok")
        self.assertEqual(self.oauth.calls, ["g1"])  # only the hub refreshed

    def test_pull_that_creates_a_slot_fetches_its_usage(self):
        self.share_on_hub(self.oauth.issue("g1", expires_in_s=3600, now=time.time()))
        self.join("linux")
        with on_machine(self.envs["linux"]):
            summary = sync_agent.run_once(self.client("linux"), lock_timeout=1)
            self.assertEqual(summary["usage_updated"], [KEY])
            self.assertIsInstance(self.account(refresh=False).usage, dict)
            state = sync_agent.load_state()
            self.assertEqual(state["hub_shared"], [KEY])
        self.assertEqual(len(self.usage.calls), 1)
        self.assertEqual(self.oauth.calls, [])

    def test_cswap_owned_usage_file_is_left_alone(self):
        # cswap 0.26 owns cache/usage.json (schemaVersion 2); CCC keeps its own file.
        with on_machine(self.envs["solo"]):
            seed_login(EMAIL, ORG, self.oauth.issue("g9", expires_in_s=3600, now=time.time()), active=False)
            legacy = claude.backup_dir() / "cache" / "usage.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps({"schemaVersion": 2, "accounts": {}}))
            self.assertIsInstance(self.account(refresh=True).usage, dict)
            self.assertEqual(json.loads(legacy.read_text()), {"schemaVersion": 2, "accounts": {}})
            self.assertIsInstance(self.account(refresh=False).usage, dict)


class WakePullsFirst(unittest.TestCase):
    def test_start_and_wake_run_a_full_round(self):
        rounds: list[bool] = []
        now = [1000.0]

        class Client:
            def wait(self, since, timeout):
                time.sleep(0.02)
                return None

        stop = threading.Event()
        with patch.object(sync_agent, "WAKE_TICK_S", 0.05), \
                patch.object(sync_agent, "run_once", side_effect=lambda c, **kw: rounds.append(kw["push"]) or {}), \
                patch.object(sync_agent, "local_fingerprint", return_value="same"):
            worker = threading.Thread(target=sync_agent.run_events, args=(Client(),), kwargs={
                "local_interval": 100.0, "full_interval": 1e9, "wait_timeout": 0.1,
                "stop": stop, "clock": lambda: now[0]})
            worker.start()
            deadline = time.time() + 5
            while not rounds and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(rounds, [True])      # at start: a full round (pull first)
            time.sleep(0.2)
            self.assertEqual(rounds, [True])      # idle: nothing more
            now[0] += 3600                        # the machine slept an hour
            deadline = time.time() + 5
            while len(rounds) < 2 and time.time() < deadline:
                time.sleep(0.01)
            stop.set()
            worker.join(timeout=5)
        self.assertEqual(rounds[:2], [True, True])  # woke: full round within a tick


if __name__ == "__main__":
    unittest.main()
