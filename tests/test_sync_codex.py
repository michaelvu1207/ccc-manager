"""Shared Codex (ChatGPT) logins: the hub is the one refresher (0.23)."""

import base64
import json
import secrets
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cccswitcher import codex, sync_agent, sync_config, sync_hub

try:
    from .sync_fakes import machine_env, on_machine
    from .test_sync import SECRET, run_agent
except ImportError:  # `unittest discover -s tests`
    from sync_fakes import machine_env, on_machine
    from test_sync import SECRET, run_agent

EMAIL, USER, ACCOUNT = "bob@example.com", "user-bob", "acct-bob"
KEY = sync_config.account_key("codex", EMAIL, ACCOUNT)
DAY = 86400


def jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


def auth_json(rt: str, expires_in_s: float, email: str = EMAIL, user: str = USER, account: str = ACCOUNT) -> dict:
    now = time.time()
    return {"OPENAI_API_KEY": None, "auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z", "tokens": {
        "id_token": jwt({"email": email, "exp": now + 3600, "https://api.openai.com/auth": {
            "chatgpt_user_id": user, "chatgpt_account_id": account, "chatgpt_plan_type": "pro"}}),
        "access_token": jwt({"exp": int(now + expires_in_s), "jti": secrets.token_hex(6)}),
        "refresh_token": rt, "account_id": account}}


class FakeChatGPTAuth:
    """auth.openai.com/oauth/token: rotates refresh tokens; reuse revokes the login."""

    def __init__(self):
        self.lock = threading.Lock()
        self.valid: dict[str, str] = {}
        self.used: dict[str, str] = {}
        self.revoked: set[str] = set()
        self.calls: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                code, answer = outer._refresh(body)
                data = json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/oauth/token"

    def issue(self, grant: str, expires_in_s: float) -> dict:
        rt = "rt-" + secrets.token_hex(8)
        with self.lock:
            self.valid[rt] = grant
        return auth_json(rt, expires_in_s)

    def _refresh(self, body: dict):
        with self.lock:
            self.calls.append({k: body.get(k) for k in ("grant_type", "client_id")})
            rt = body.get("refresh_token", "")
            if rt in self.used:
                self.revoked.add(self.used[rt])
                return 400, {"error": {"code": "refresh_token_reused"}}
            grant = self.valid.pop(rt, None)
            if grant is None or grant in self.revoked:
                return 400, {"error": "invalid_grant"}
            self.used[rt] = grant
            new = auth_json("rt-" + secrets.token_hex(8), 10 * DAY)["tokens"]
            self.valid[new["refresh_token"]] = grant
            return 200, {k: new[k] for k in ("id_token", "access_token", "refresh_token")}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def seed_codex_login(auth: dict, active: bool = True) -> None:
    path = codex.codex_home() / "seed-auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(auth))
    codex.import_auth(path, force=True, activate=active)
    path.unlink()


class HelperTests(unittest.TestCase):
    def test_provider_expiry_and_ids(self):
        auth = auth_json("rt-1", 5 * DAY)
        self.assertEqual(sync_config.provider_of(auth), "codex")
        self.assertIsNone(sync_config.parse(auth))  # the default provider is still Claude
        self.assertIs(sync_config.parse(auth, "codex"), auth)
        self.assertAlmostEqual(sync_config.expires_at(auth), time.time() + 5 * DAY, delta=5)
        self.assertTrue(sync_config.token_id(auth))
        self.assertTrue(sync_config.is_newer(auth_json("rt-2", 9 * DAY), auth))
        self.assertFalse(sync_config.is_newer(auth, auth))
        self.assertIsNone(sync_config.provider_of({"OPENAI_API_KEY": "sk-x", "tokens": auth["tokens"]}))

    def test_withheld_refresh_token_keeps_the_field(self):
        stripped = sync_config.without_refresh_token(auth_json("rt-1", DAY))
        self.assertEqual(stripped["tokens"]["refresh_token"], "")  # Codex fails to read auth.json without it

    def test_refresh_rotates_tokens_like_the_codex_cli(self):
        fake = FakeChatGPTAuth()
        self.addCleanup(fake.close)
        old = fake.issue("g1", DAY)
        new = json.loads(codex.refresh_credentials(json.dumps(old), fake.url))
        self.assertEqual(fake.calls, [{"grant_type": "refresh_token", "client_id": codex.CLIENT_ID}])
        self.assertNotEqual(new["tokens"]["refresh_token"], old["tokens"]["refresh_token"])
        self.assertGreater(sync_config.expires_at(new), sync_config.expires_at(old))
        self.assertEqual(new["tokens"]["account_id"], ACCOUNT)
        self.assertIsNone(codex.refresh_credentials(json.dumps(old), fake.url))  # reused: refused


class UsageTests(unittest.TestCase):
    def test_refresh_usage_handles_an_account_never_read(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, on_machine(machine_env(Path(tmp), "m")):
            seed_codex_login(auth_json("rt-1", 5 * DAY))
            key = codex.parse_auth(auth_json("rt-1", DAY))["account_key"]
            with patch.object(codex, "_fetch_usage", return_value=(key, {"primary": {"used_percent": 5.0}})):
                self.assertTrue(codex.refresh_usage())  # last_usage_at was None: no TypeError
            self.assertEqual(codex.load_accounts(False)[0].usage["primary"]["used_percent"], 5.0)


class CodexSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.oauth = FakeChatGPTAuth()
        self.store = sync_hub.HubStore(path=self.root / "hub" / "store.json",
                                       refresh_fn=lambda v: codex.refresh_credentials(v, self.oauth.url))
        self.hub = sync_hub.Hub(self.store, SECRET, port=0, hub_machine="hubbox", interval=3600)
        self.hub.start()
        self.envs = {name: machine_env(self.root, name) for name in ("mac", "linux")}

    def tearDown(self):
        self.hub.stop()
        self.oauth.close()
        self.tmp.cleanup()

    def snapshot(self, machine: str) -> dict | None:
        with on_machine(self.envs[machine]):
            login = codex.shareable_logins().get(KEY)
            return login["credential"] if login else None

    def live(self, machine: str) -> dict | None:
        path = Path(self.envs[machine]["CODEX_HOME"]) / "auth.json"
        return json.loads(path.read_text()) if path.exists() else None

    def test_login_spreads_and_only_the_hub_refreshes(self):
        with on_machine(self.envs["mac"]):
            seed_codex_login(self.oauth.issue("g1", DAY))  # 1 day left: inside the 2-day margin
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["pushed"], [KEY])
        self.assertEqual(run_agent(self.envs["linux"], self.hub.url, "linux")["created"], [KEY])
        self.assertEqual(sync_config.token_id(self.snapshot("linux")), sync_config.token_id(self.snapshot("mac")))
        self.assertEqual(self.oauth.calls, [])  # agents never refresh

        self.assertEqual(self.store.refresh_due(), [KEY])
        self.assertEqual(self.store.refresh_due(), [])  # 10 days left now
        self.assertEqual(len(self.oauth.calls), 1)

        for name in ("mac", "linux", "mac"):
            run_agent(self.envs[name], self.hub.url, name)
        hub_token = sync_config.token_id(self.store.credentials("x", False, "hubbox")[0]["credential"])
        for name in ("mac", "linux"):
            self.assertEqual(sync_config.token_id(self.snapshot(name)), hub_token, name)
        self.assertEqual(sync_config.token_id(self.live("mac")), hub_token)  # the active login too
        self.assertIsNone(self.live("linux"))  # not active there: auth.json is left alone
        self.assertEqual(self.oauth.revoked, set())
        self.assertEqual(len(self.oauth.calls), 1)

    def test_a_login_with_days_left_is_not_refreshed(self):
        with on_machine(self.envs["mac"]):
            seed_codex_login(self.oauth.issue("g1", 5 * DAY))
        run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertEqual(self.store.refresh_due(), [])
        self.assertEqual(self.oauth.calls, [])

    def test_codex_refreshing_here_is_pushed(self):
        with on_machine(self.envs["mac"]):
            seed_codex_login(self.oauth.issue("g1", 5 * DAY))
        run_agent(self.envs["mac"], self.hub.url, "mac")
        # Codex on mac refreshed by itself (it writes auth.json): the newer copy goes to the hub.
        newer = auth_json("rt-local", 10 * DAY)
        (Path(self.envs["mac"]["CODEX_HOME"]) / "auth.json").write_text(json.dumps(newer))
        self.assertEqual(run_agent(self.envs["mac"], self.hub.url, "mac")["pushed"], [KEY])
        hub_copy = self.store.credentials("x", False, "hubbox")[0]["credential"]
        self.assertEqual(sync_config.token_id(hub_copy), sync_config.token_id(newer))

    def test_hub_refuses_a_login_under_another_key(self):
        other = sync_config.account_key("codex", "eve@example.com", ACCOUNT)
        answer = self.store.offer({"key": other, "provider": "codex", "email": "eve@example.com",
                                   "org_uuid": ACCOUNT, "credential": auth_json("rt-1", DAY)})
        self.assertEqual(answer["reason"], "key does not match identity")
        self.assertEqual(self.store.status()["accounts"], [])

    def test_withhold_sends_an_empty_refresh_token(self):
        self.store.offer({"key": KEY, "provider": "codex", "email": EMAIL, "org_uuid": ACCOUNT,
                          "credential": auth_json("rt-1", DAY)})
        sent = self.store.credentials("linux", True, "hubbox")[0]["credential"]
        self.assertEqual(sent["tokens"]["refresh_token"], "")

    def test_deleted_on_hub_removes_copies_but_not_the_active_login(self):
        with on_machine(self.envs["mac"]):
            seed_codex_login(self.oauth.issue("g1", 5 * DAY))
        run_agent(self.envs["mac"], self.hub.url, "mac")
        run_agent(self.envs["linux"], self.hub.url, "linux")
        self.store.delete(KEY)
        self.assertEqual(run_agent(self.envs["linux"], self.hub.url, "linux")["removed"], [KEY])
        self.assertIsNone(self.snapshot("linux"))
        mac = run_agent(self.envs["mac"], self.hub.url, "mac")
        self.assertEqual((mac["kept_active"], mac["pushed"]), ([KEY], []))  # kept, and not pushed back
        self.assertIsNotNone(self.snapshot("mac"))

    def test_a_fresh_login_replaces_the_hub_copy(self):
        with on_machine(self.envs["mac"]):
            seed_codex_login(self.oauth.issue("g1", 9 * DAY))
        run_agent(self.envs["mac"], self.hub.url, "mac")
        with on_machine(self.envs["linux"]):
            sync_config.save({"hub_url": self.hub.url, "secret": SECRET, "machine": "linux"})
            seed_codex_login(self.oauth.issue("g2", 8 * DAY))  # expires earlier, but a deliberate login
            note = sync_agent.share_codex_login(codex.parse_auth(self.live("linux"))["account_key"])
        self.assertEqual(note, "shared with every machine")
        hub_copy = self.store.credentials("x", False, "hubbox")[0]["credential"]
        self.assertEqual(sync_config.token_id(hub_copy), sync_config.token_id(self.live("linux")))


if __name__ == "__main__":
    unittest.main()
