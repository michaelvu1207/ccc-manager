from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cccswitcher import autoswitch, claude

ORG = "org-1"
REGISTRY = {"sequence": [4], "activeAccountNumber": 4,
            "accounts": {"4": {"email": "a@test", "organizationUuid": ORG}}}


def status(usable: bool, next_id: str | None = "g2") -> dict:
    return {"cedar_ember": {
        "eligible": True, "next_grant_id": next_id,
        "grants": [
            {"id": "g1", "label": "Old reset", "resets_left": 0, "usable_now": False},
            {"id": "g2", "label": "One Fable reset", "resets_left": 1, "usable_now": usable,
             "ends_at": "2099-01-01T00:00:00Z"},
        ],
    }}


class FakeResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class ClaudeResetTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        patches = [
            patch.object(claude, "_registry", return_value=REGISTRY),
            patch.object(claude, "_active_identity", return_value=("a@test", ORG)),
            patch.object(claude, "_shared_nums", return_value=set()),
            patch.object(claude, "_access_token", return_value=("tok", "")),
            patch.object(claude, "refresh_usage_for", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def serve(self, *answers):
        answers = list(answers)

        def urlopen(req, timeout=None):
            self.requests.append(req)
            return FakeResponse(answers.pop(0))
        return patch("urllib.request.urlopen", side_effect=urlopen)

    def test_refuses_without_confirmation_before_any_request(self):
        with self.serve():
            with self.assertRaises(ValueError):
                claude.consume_reset_credit("4")
        self.assertEqual(self.requests, [])

    def test_does_not_claim_when_the_grant_is_not_usable_now(self):
        with self.serve(status(usable=False)):
            with self.assertRaisesRegex(ValueError, "cannot be used now"):
                claude.consume_reset_credit("4", confirmed=True)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].get_method(), "GET")

    def test_claims_the_next_grant_as_claude_code(self):
        with self.serve(status(usable=True), {"result": "reset", "resets_left": 0, "cleared": ["fable"]}):
            result = claude.consume_reset_credit("4", confirmed=True, request_id="req-1")
        claim = self.requests[1]
        self.assertEqual(claim.get_method(), "POST")
        self.assertEqual(claim.full_url, f"https://api.anthropic.com/api/organizations/{ORG}/reset_rate_limits")
        self.assertEqual(json.loads(claim.data), {"program": "cedar_ember", "grant_id": "g2", "request_id": "req-1"})
        self.assertEqual(claim.get_header("User-agent"), claude.CLAUDE_CODE_USER_AGENT)
        self.assertEqual(claim.get_header("Authorization"), "Bearer tok")
        self.assertEqual(result["result"], "reset")
        self.assertEqual(result["resets_left"], 0)

    def test_server_refusal_is_an_error(self):
        with self.serve(status(usable=True), {"result": "not_limited", "reason": "not_limited"}):
            with self.assertRaisesRegex(ValueError, "not_limited"):
                claude.consume_reset_credit("4", confirmed=True)

    def test_no_next_grant(self):
        with self.serve(status(usable=True, next_id=None)):
            with self.assertRaisesRegex(ValueError, "no Claude limit reset is ready"):
                claude.consume_reset_credit("4", confirmed=True)
        self.assertEqual(len(self.requests), 1)

    def test_grant_list_marks_only_the_next_usable_grant(self):
        left, credits = claude._reset_grants(status(usable=True)["cedar_ember"])
        self.assertEqual(left, 1)
        self.assertEqual([(c["id"], c["usable_now"]) for c in credits], [("g2", True)])
        _, credits = claude._reset_grants(status(usable=True, next_id="other")["cedar_ember"])
        self.assertFalse(credits[0]["usable_now"])


class SwitchConfigTests(unittest.TestCase):
    def test_switch_keeps_machine_settings_and_takes_the_account_identity(self):
        live = json.dumps({"oauthAccount": {"emailAddress": "old@test"}, "userID": "u1",
                           "mcpServers": {"composio": {"type": "http"}}, "projects": {"/p": {}}})
        saved = json.dumps({"oauthAccount": {"emailAddress": "new@test"}, "mcpServers": {"stale": {}}})
        out = json.loads(claude._switched_config(live, saved))
        self.assertEqual(out["oauthAccount"], {"emailAddress": "new@test"})
        self.assertEqual(out["mcpServers"], {"composio": {"type": "http"}})
        self.assertEqual(out["projects"], {"/p": {}})
        self.assertEqual(out["userID"], "u1")

    def test_unreadable_or_empty_live_config_uses_the_saved_copy(self):
        saved = json.dumps({"oauthAccount": {"emailAddress": "new@test"}})
        self.assertEqual(claude._switched_config("{torn", saved), saved)
        self.assertEqual(claude._switched_config("{}", saved), saved)


class AutoSwitchReadTests(unittest.TestCase):
    def setUp(self):
        # Never read this machine's own auto-switch settings (leftover mode, ...).
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HOME": self.tmp.name, "CCC_CLAUDE_HOME": str(Path(self.tmp.name) / "c")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_cache_reads_only_the_active_account(self):
        accounts = [claude.ClaudeAccount("1", "a@test", "", "", True, {"seven_day": {"pct": 10}}),
                    claude.ClaudeAccount("2", "b@test", "", "", False, {"seven_day": {"pct": 0}})]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, 1000)) as load,
            patch.object(claude, "refresh_usage_for") as one,
        ):
            event = autoswitch.tick("claude", dry_run=True, now=1100)
        self.assertEqual(event["reason"], "below-threshold")
        one.assert_called_once_with(["1"])
        self.assertTrue(all(call.args == (False,) for call in load.call_args_list))

    def test_switch_due_rereads_every_account_first(self):
        accounts = [claude.ClaudeAccount("1", "a@test", "", "", True, {"seven_day": {"pct": 95}}),
                    claude.ClaudeAccount("2", "b@test", "", "", False, {"seven_day": {"pct": 0}})]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, 1000)) as load,
            patch.object(claude, "refresh_usage_for"),
            patch.object(autoswitch, "state_path", return_value=__import__("pathlib").Path("/nonexistent/x.json")),
        ):
            event = autoswitch.tick("claude", dry_run=True, now=1100)
        self.assertEqual(event["event"], "switch")
        self.assertIn(((True,), {}), [(c.args, c.kwargs) for c in load.call_args_list])


if __name__ == "__main__":
    unittest.main()
