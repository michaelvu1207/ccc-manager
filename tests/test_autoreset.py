from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from cccswitcher import autoreset, autoswitch, claude

NOW = 1_800_000_000.0
FULL = ["seven_day", "seven_day_overage_included"]
FABLE_ONLY = ["seven_day_overage_included"]


def iso(hours: float) -> str:
    return datetime.fromtimestamp(NOW + hours * 3600, timezone.utc).isoformat()


def account(num, *, wk=50.0, fable=50.0, wk_reset_h=72.0, grant_h=500.0, clears=FULL,
            resets=1, usable=True, cooldown_h=None, disabled=False):
    usage = {
        "seven_day": {"pct": wk, "resets_at": iso(wk_reset_h)},
        "scoped": [{"name": "Fable", "pct": fable, "resets_at": iso(wk_reset_h)}],
    }
    if resets:
        usage["reset_credits_available"] = resets
        usage["reset_credits"] = [{"id": f"g{num}", "title": "reset", "expires_at": iso(grant_h),
                                   "usable_now": usable, "resets_left": resets, "clears": clears}]
        usage["reset_status"] = {"next_grant_id": f"g{num}",
                                 "cooldown_until": iso(cooldown_h) if cooldown_h is not None else None}
    return claude.ClaudeAccount(str(num), f"a{num}@test", "", "", False, usage, disabled=disabled)


def settings(**kw):
    return autoswitch.Settings(auto_reset=True, **kw)


class ChooseTests(unittest.TestCase):
    def test_not_needed_while_any_account_has_quota(self):
        accounts = [account(1, fable=97), account(2, wk=40, fable=40)]
        self.assertEqual(autoreset.choose(accounts, settings(), NOW), (None, "not-needed"))

    def test_all_used_up_picks_the_grant_that_expires_soonest(self):
        accounts = [account(1, fable=97, grant_h=900, resets=3),
                    account(2, fable=98, grant_h=300, resets=1)]
        option, reason = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual((option["account"].num, reason), ("2", "all-used-up"))

    def test_same_expiry_prefers_the_account_with_more_resets(self):
        accounts = [account(1, fable=97, resets=1), account(2, fable=98, resets=3), account(3, fable=99, resets=1)]
        option, _ = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual(option["account"].num, "2")

    def test_skips_an_account_whose_limit_resets_within_two_hours(self):
        accounts = [account(1, fable=97, grant_h=100, wk_reset_h=1.5), account(2, fable=98, grant_h=900)]
        option, _ = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual(option["account"].num, "2")

    def test_skips_cooldown_unusable_and_grants_that_do_not_clear_the_block(self):
        accounts = [account(1, wk=95, fable=50, clears=["five_hour"]),  # weekly blocks; a 5h grant cannot help
                    account(2, fable=97, cooldown_h=3),
                    account(3, fable=97, usable=False)]
        self.assertEqual(autoreset.choose(accounts, settings(), NOW), (None, "no-usable-reset"))

    def test_a_fable_reset_also_gives_back_the_weekly_limit(self):
        # Seen 2026-11-13: a grant that lists only seven_day_overage_included reset the weekly limit too.
        accounts = [account(1, wk=95, fable=50, clears=FABLE_ONLY)]
        option, reason = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual((option["account"].num, reason, option["value"]), ("1", "all-used-up", 95.0))

    def test_disabled_accounts_neither_count_as_quota_nor_get_resets(self):
        accounts = [account(1, fable=97), account(2, wk=10, fable=10, disabled=True)]
        option, _ = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual(option["account"].num, "1")

    def test_expiring_grant_is_used_even_when_not_needed(self):
        accounts = [account(1, fable=60, grant_h=30), account(2, wk=10, fable=10, resets=0)]
        option, reason = autoreset.choose(accounts, settings(), NOW)
        self.assertEqual((option["account"].num, reason), ("1", "expiring"))

    def test_expiring_grant_that_gives_nothing_back_is_left(self):
        accounts = [account(1, wk=0, fable=0, grant_h=30), account(2, wk=10, fable=10, resets=0)]
        self.assertEqual(autoreset.choose(accounts, settings(), NOW), (None, "not-needed"))


class TickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HOME": self.tmp.name, "CCC_CLAUDE_HOME": str(Path(self.tmp.name) / "c")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        autoswitch.set_value("autoswitch.autoReset", "1")

    def test_claims_once_then_waits(self):
        accounts = [account(1, fable=97), account(2, fable=98, resets=2)]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, NOW)),
            patch.object(claude, "consume_reset_credit", return_value={"grant": "reset", "resets_left": 1}) as use,
        ):
            first = autoreset.tick(now=NOW)
            second = autoreset.tick(now=NOW + 60)
        self.assertEqual((first["event"], first["email"]), ("reset", "a2@test"))
        use.assert_called_once_with("2", confirmed=True)
        self.assertEqual(second, {"event": "idle", "provider": "claude", "reason": "interval"})
        self.assertEqual(autoswitch.service_status()["last_reset"]["email"], "a2@test")

    def test_dry_run_claims_nothing(self):
        accounts = [account(1, fable=97)]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, NOW)),
            patch.object(claude, "consume_reset_credit") as use,
        ):
            event = autoreset.tick(dry_run=True, now=NOW)
        self.assertTrue(event["dry_run"])
        use.assert_not_called()


if __name__ == "__main__":
    unittest.main()
