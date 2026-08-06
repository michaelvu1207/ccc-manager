from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cccswitcher import claude, codex


class FakeHTTPResponse:
    def __init__(self, value: dict):
        self.data = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


def jwt(claims: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class ProviderCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home),
            "CCC_CLAUDE_HOME": str(self.home / "claude-swap"),
            "CLAUDE_CONFIG_DIR": str(self.home / "claude"),
            "CODEX_HOME": str(self.home / "codex"),
            "CCC_CODEX_REGISTRY": str(self.home / "codex" / "accounts" / "registry.json"),
            "CCC_DISABLE_KEYCHAIN": "1",
            "CCC_DISABLE_LEGACY_MIGRATION": "1",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _claude_slot(self, num: str, email: str, active=False):
        root = claude.backup_dir()
        registry = claude._registry()
        registry["accounts"][num] = {
            "email": email, "organizationUuid": "", "organizationName": "",
            "future_field": {"preserve": True},
        }
        registry["sequence"] = sorted({int(n) for n in registry["sequence"]} | {int(num)})
        claude._write_backup_credentials(num, email, json.dumps({"claudeAiOauth": {"accessToken": f"token-{num}"}}))
        claude._config_backup_path(num, email).parent.mkdir(parents=True, exist_ok=True)
        claude._config_backup_path(num, email).write_text(json.dumps({
            "oauthAccount": {"emailAddress": email, "organizationUuid": ""},
            "future_config": num,
        }))
        if active:
            claude.global_config_path().parent.mkdir(parents=True, exist_ok=True)
            claude.global_config_path().write_text(json.dumps({
                "oauthAccount": {"emailAddress": email, "organizationUuid": ""}
            }))
            claude.credentials_path().parent.mkdir(parents=True, exist_ok=True)
            claude.credentials_path().write_text(json.dumps({"claudeAiOauth": {"accessToken": f"token-{num}"}}))
            registry["activeAccountNumber"] = int(num)
        claude.sequence_file().parent.mkdir(parents=True, exist_ok=True)
        claude.sequence_file().write_text(json.dumps(registry))

    def test_claude_reads_legacy_schema_and_switches_internally(self):
        self._claude_slot("1", "one@example.test", active=True)
        self._claude_slot("2", "two@example.test")
        accounts, _ = claude.load_accounts(refresh=False)
        self.assertEqual([a.num for a in accounts], ["1", "2"])
        self.assertTrue(accounts[0].active)

        switched = claude.switch_account("2")
        self.assertEqual(switched.email, "two@example.test")
        self.assertEqual(json.loads(claude.credentials_path().read_text())["claudeAiOauth"]["accessToken"], "token-2")
        saved = json.loads(claude.sequence_file().read_text())
        self.assertTrue(saved["accounts"]["2"]["future_field"]["preserve"])

    def test_claude_distinguishes_login_failures_from_usage_outages(self):
        account = claude.ClaudeAccount("1", "one@example.test", "", "", True)
        account.usage = None
        self.assertTrue(claude.needs_login(account))
        account.usage = "authentication expired"
        self.assertTrue(claude.needs_login(account))
        account.usage = "no credentials"
        self.assertTrue(claude.needs_login(account))
        account.usage = "usage temporarily unavailable"
        self.assertFalse(claude.needs_login(account))
        account.usage = {"five_hour": {"pct": 0}}
        self.assertFalse(claude.needs_login(account))

    def test_claude_transient_refresh_preserves_last_verified_usage(self):
        self._claude_slot("1", "one@example.test", active=True)
        cached_usage = {"five_hour": {"pct": 25, "countdown": "1h"}}
        claude.usage_cache_path().parent.mkdir(parents=True, exist_ok=True)
        claude.usage_cache_path().write_text(json.dumps({
            "timestamp": 1, "data": {"1": cached_usage}
        }))

        with patch.object(
            claude, "_fetch_one", return_value=("1", "usage temporarily unavailable")
        ):
            self.assertTrue(claude.refresh_usage())

        saved = json.loads(claude.usage_cache_path().read_text())
        self.assertEqual(saved["data"]["1"], cached_usage)

    def test_save_current_claude_login_is_identity_checked(self):
        self._claude_slot("1", "one@example.test", active=True)
        fresh = json.dumps({"claudeAiOauth": {"accessToken": "fresh-token"}})
        claude._write_active_credentials(fresh)

        saved = claude.save_current_account("1")

        self.assertEqual(saved.email, "one@example.test")
        self.assertEqual(claude._read_backup_credentials("1", saved.email), fresh)

        config = json.loads(claude.global_config_path().read_text())
        config["oauthAccount"]["emailAddress"] = "wrong@example.test"
        claude.global_config_path().write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "sign in as one@example.test"):
            claude.save_current_account("1")
        self.assertEqual(claude._read_backup_credentials("1", saved.email), fresh)

    def test_claude_refresh_syncs_changed_active_token_only_to_matching_slot(self):
        self._claude_slot("1", "one@example.test", active=True)
        self._claude_slot("2", "two@example.test")
        fresh = json.dumps({"claudeAiOauth": {"accessToken": "fresh-active-token"}})
        claude._write_active_credentials(fresh)
        second_before = claude._read_backup_credentials("2", "two@example.test")

        self.assertTrue(claude.sync_active_credentials())
        self.assertEqual(claude._read_backup_credentials("1", "one@example.test"), fresh)
        self.assertEqual(claude._read_backup_credentials("2", "two@example.test"), second_before)
        self.assertFalse(claude.sync_active_credentials())

    def test_claude_managed_api_key_slot_switches_on_the_managed_axis(self):
        self._claude_slot("1", "one@example.test", active=True)
        registry = claude._registry()
        registry["accounts"]["2"] = {
            "email": "api-key-2@token.local", "organizationUuid": "",
            "organizationName": "", "kind": "api_key",
        }
        registry["sequence"].append(2)
        claude.sequence_file().write_text(json.dumps(registry))
        key = "sk-ant-api03-" + "x" * 32
        claude._write_backup_credentials("2", "api-key-2@token.local", key)
        claude._config_backup_path("2", "api-key-2@token.local").parent.mkdir(parents=True, exist_ok=True)
        claude._config_backup_path("2", "api-key-2@token.local").write_text(json.dumps({
            "primaryApiKey": key,
            "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
        }))

        claude.switch_account("2")
        self.assertEqual(json.loads(claude.global_config_path().read_text())["primaryApiKey"], key)
        self.assertFalse(claude.credentials_path().exists())
        self.assertEqual(claude._read_backup_credentials("2", "api-key-2@token.local"), key)
        api_account = next(a for a in claude.load_accounts(False)[0] if a.num == "2")
        self.assertTrue(api_account.active)
        self.assertFalse(claude.needs_login(api_account))

    def test_legacy_keychain_migration_is_verified_recorded_and_non_destructive(self):
        self._claude_slot("1", "one@example.test", active=True)
        before = claude.sequence_file().read_bytes()
        exported = {
            "accounts": [{
                "number": 1,
                "metadata": {"email": "one@example.test"},
                "credentials": {"claudeAiOauth": {"accessToken": "synthetic"}},
            }]
        }
        keychain = {}

        def keychain_set(service, account, value):
            keychain[(service, account)] = value

        def keychain_get(service, account):
            return keychain.get((service, account), "")

        with (
            patch.dict(os.environ, {"CCC_DISABLE_LEGACY_MIGRATION": "0"}),
            patch.object(claude, "_use_keychain", return_value=True),
            patch.object(claude.shutil, "which", return_value="/fake/cswap"),
            patch.object(claude.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(exported).encode())),
            patch.object(claude, "_keychain_set", side_effect=keychain_set),
            patch.object(claude, "_keychain_get", side_effect=keychain_get),
        ):
            self.assertEqual(claude.migrate_legacy_credentials(), 1)

        self.assertEqual(claude.sequence_file().read_bytes(), before)
        state = json.loads((claude.backup_dir() / ".ccc-migrations.json").read_text())
        self.assertTrue(state["applied"]["legacy_cswap_keyring_v1"]["sourcePreserved"])

    def _codex_auth(self, user: str, account: str, email: str) -> dict:
        return {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": jwt({
                    "email": email,
                    "https://api.openai.com/auth": {
                        "chatgpt_user_id": user,
                        "chatgpt_account_id": account,
                        "chatgpt_plan_type": "plus",
                    },
                }),
                "access_token": "not-a-real-token",
                "refresh_token": "not-a-real-refresh",
                "account_id": account,
            },
        }

    def test_codex_import_switch_and_unknown_registry_fields_roundtrip(self):
        registry = {
            "schema_version": 3, "active_account_key": None,
            "api": {"usage": False}, "auto_switch": {"enabled": True},
            "accounts": [], "future": {"preserve": True},
        }
        codex.registry_file().parent.mkdir(parents=True, exist_ok=True)
        codex.registry_file().write_text(json.dumps(registry))
        paths = []
        for index in (1, 2):
            path = self.home / f"auth{index}.json"
            path.write_text(json.dumps(self._codex_auth(f"user-{index}", f"acct-{index}", f"u{index}@example.test")))
            paths.append(path)
            codex.import_auth(path, alias=f"slot-{index}", activate=index == 1)

        switched = codex.switch_account("slot-2")
        self.assertEqual(switched.email, "u2@example.test")
        saved = json.loads(codex.registry_file().read_text())
        self.assertEqual(saved["schema_version"], 3)
        self.assertTrue(saved["future"]["preserve"])
        self.assertTrue(saved["auto_switch"]["enabled"])
        self.assertEqual(saved["previous_active_account_key"], "user-1::acct-1")
        self.assertEqual(codex.parse_auth(json.loads(codex.active_auth_path().read_text()))["account_key"], "user-2::acct-2")

        stored = codex.account_auth_path("user-2::acct-2").read_bytes()
        replacement = json.loads(paths[1].read_text())
        replacement["tokens"]["access_token"] = "replacement-not-imported"
        paths[1].write_text(json.dumps(replacement))
        codex.import_auth(paths[1], force=False)
        self.assertEqual(codex.account_auth_path("user-2::acct-2").read_bytes(), stored)

    def test_codex_switch_reads_legacy_base64_account_key_snapshot(self):
        account_key = "legacy-user::legacy-account"
        auth = self._codex_auth("legacy-user", "legacy-account", "legacy@example.test")
        encoded = base64.urlsafe_b64encode(account_key.encode()).decode().rstrip("=")
        legacy_path = codex.registry_file().parent / f"{encoded}.auth.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps(auth))
        codex.registry_file().write_text(json.dumps({
            "schema_version": 3,
            "active_account_key": None,
            "accounts": [{
                "account_key": account_key,
                "email": "legacy@example.test",
                "alias": "legacy",
                "plan": "pro",
            }],
        }))

        switched = codex.switch_account("legacy")

        self.assertEqual(switched.email, "legacy@example.test")
        active = json.loads(codex.active_auth_path().read_text())
        self.assertEqual(codex.parse_auth(active)["account_key"], account_key)

    def test_codex_same_email_accounts_require_unambiguous_selector(self):
        codex.registry_file().parent.mkdir(parents=True, exist_ok=True)
        codex.registry_file().write_text(json.dumps({"schema_version": 4, "accounts": []}))
        for index in (1, 2):
            path = self.home / f"same{index}.json"
            path.write_text(json.dumps(self._codex_auth("same-user", f"workspace-{index}", "same@example.test")))
            codex.import_auth(path, alias=f"workspace-{index}", activate=False)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            codex.switch_account("same@example.test")
        self.assertEqual(codex.switch_account("workspace-2").account_key, "same-user::workspace-2")

    def test_codex_preserves_consumable_reset_credit_count(self):
        usage = codex._normalize_usage({
            "rate_limit": {},
            "rate_limit_reset_credits": {"available_count": 3},
            "plan_type": "pro",
        })

        self.assertEqual(usage["reset_credits_available"], 3)
        self.assertEqual(usage["plan_type"], "pro")

        clamped = codex._normalize_usage({
            "rate_limit": {},
            "rate_limit_reset_credits": {"available_count": -1},
        })
        self.assertEqual(clamped["reset_credits_available"], 0)

    def _stored_codex_reset_account(self):
        auth_path = self.home / "reset-auth.json"
        auth_path.write_text(json.dumps(self._codex_auth(
            "reset-user", "reset-account", "reset@example.test"
        )))
        return codex.import_auth(auth_path, alias="reset-slot", activate=False)

    def test_codex_reset_details_are_sanitized_to_available_credits(self):
        normalized = codex._normalize_reset_credits({
            "available_count": 2,
            "credits": [
                {"id": "credit-a", "status": "available", "title": "First", "expires_at": "2026-08-01T00:00:00Z", "private": "drop"},
                {"id": "credit-b", "status": "redeemed", "title": "Used"},
                {"id": "credit-c", "status": "available"},
            ],
        })

        self.assertEqual(normalized, {
            "available_count": 2,
            "credits": [
                {"id": "credit-a", "title": "First", "expires_at": "2026-08-01T00:00:00Z"},
                {"id": "credit-c", "title": "Usage reset", "expires_at": None},
            ],
        })

    def test_codex_reset_requires_confirmation_before_any_network_request(self):
        self._stored_codex_reset_account()
        with patch.object(codex.urllib.request, "urlopen") as urlopen:
            with self.assertRaisesRegex(ValueError, "explicit confirmation"):
                codex.consume_reset_credit("reset-slot")
        urlopen.assert_not_called()

    def test_codex_reset_consumes_selected_credit_with_idempotency_key(self):
        self._stored_codex_reset_account()
        responses = [
            FakeHTTPResponse({
                "available_count": 2,
                "credits": [
                    {"id": "credit-a", "status": "available", "title": "First"},
                    {"id": "credit-b", "status": "available", "title": "Second"},
                ],
            }),
            FakeHTTPResponse({"code": "reset", "credit": {"id": "credit-b"}}),
        ]
        with patch.object(codex.urllib.request, "urlopen", side_effect=responses) as urlopen:
            result = codex.consume_reset_credit(
                "reset-slot", credit_id="credit-b", confirmed=True,
                redeem_request_id="request-unique-1",
            )

        self.assertEqual(result["remaining_count"], 1)
        self.assertEqual(len(urlopen.call_args_list), 2)
        request = urlopen.call_args_list[1].args[0]
        self.assertEqual(request.full_url, codex.RESET_CONSUME_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "credit_id": "credit-b", "redeem_request_id": "request-unique-1"
        })
        self.assertEqual(request.headers["Chatgpt-account-id"], "reset-account")

    def test_codex_reset_rejects_ambiguous_or_stale_selection_without_consuming(self):
        self._stored_codex_reset_account()
        details = FakeHTTPResponse({
            "available_count": 2,
            "credits": [
                {"id": "credit-a", "status": "available"},
                {"id": "credit-b", "status": "available"},
            ],
        })
        with patch.object(codex.urllib.request, "urlopen", return_value=details) as urlopen:
            with self.assertRaisesRegex(ValueError, "multiple usage resets"):
                codex.consume_reset_credit("reset-slot", confirmed=True)
        self.assertEqual(urlopen.call_count, 1)

    def test_codex_refresh_syncs_changed_active_token_to_matching_snapshot(self):
        account = self._stored_codex_reset_account()
        stored_path = codex.account_auth_path(account.account_key)
        stored = json.loads(stored_path.read_text())
        active = json.loads(stored_path.read_text())
        active["tokens"]["access_token"] = "fresh-active-token"
        codex.active_auth_path().parent.mkdir(parents=True, exist_ok=True)
        codex.active_auth_path().write_text(json.dumps(active))

        self.assertTrue(codex.sync_active_credentials())
        self.assertEqual(
            json.loads(stored_path.read_text())["tokens"]["access_token"],
            "fresh-active-token",
        )
        self.assertFalse(codex.sync_active_credentials())
        self.assertNotEqual(stored["tokens"]["access_token"], "fresh-active-token")


if __name__ == "__main__":
    unittest.main()
