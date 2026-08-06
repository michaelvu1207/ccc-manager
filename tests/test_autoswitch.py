from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cccswitcher import autoswitch, claude


def account(num: str, email: str, active: bool, used: float) -> claude.ClaudeAccount:
    return claude.ClaudeAccount(
        num, email, "", "", active,
        {"five_hour": {"pct": used}, "seven_day": {"pct": used}},
    )


class AutoSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.root), "CCC_CLAUDE_HOME": str(self.root / "claude")
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_threshold_hysteresis_selects_best_target_without_switching_on_dry_run(self):
        accounts = [
            account("1", "active@test", True, 92),
            account("2", "okay@test", False, 65),
            account("3", "best@test", False, 20),
        ]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, 0)),
            patch.object(claude, "switch_account") as switch,
        ):
            event = autoswitch.tick("claude", dry_run=True, now=1000)
        self.assertEqual(event["event"], "switch")
        self.assertEqual(event["to_num"], "3")
        switch.assert_not_called()

    def test_cooldown_blocks_repeat_switch(self):
        autoswitch.state_path().parent.mkdir(parents=True, exist_ok=True)
        autoswitch.state_path().write_text(
            '{"providers":{"claude":{"lastSwitchAt":900}}}'
        )
        accounts = [account("1", "active@test", True, 95), account("2", "best@test", False, 10)]
        with patch.object(claude, "load_accounts", return_value=(accounts, 0)):
            event = autoswitch.tick("claude", now=1000)
        self.assertEqual(event, {"event": "no-switch", "reason": "cooldown", "provider": "claude"})


if __name__ == "__main__":
    unittest.main()
