from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cccswitcher import autoswitch, claude


def account(num: str, email: str, active: bool, used: float, fable: float | None = None) -> claude.ClaudeAccount:
    usage = {"five_hour": {"pct": used}, "seven_day": {"pct": used}}
    if fable is not None:
        usage["scoped"] = [{"name": "Fable", "pct": fable}]
    return claude.ClaudeAccount(num, email, "", "", active, usage)


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


    def test_fable_below_five_percent_left_switches_to_account_with_fable(self):
        accounts = [
            account("1", "active@test", True, 40, fable=96),
            account("2", "nofable@test", False, 5, fable=100),
            account("3", "okay@test", False, 30, fable=50),
            account("4", "best@test", False, 10, fable=20),
        ]
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, 0)),
            patch.object(claude, "switch_account") as switch,
        ):
            event = autoswitch.tick("claude", now=1000)
        self.assertEqual(event["event"], "switch")
        self.assertEqual(event["to_num"], "4")
        self.assertEqual(event["reason"], "fable-limit")
        switch.assert_called_once_with("4")

    def test_fable_with_more_than_five_percent_left_does_not_switch(self):
        accounts = [account("1", "active@test", True, 40, fable=94), account("2", "b@test", False, 0, fable=0)]
        with patch.object(claude, "load_accounts", return_value=(accounts, 0)):
            event = autoswitch.tick("claude", dry_run=True, now=1000)
        self.assertEqual(event["reason"], "below-threshold")

    def test_fable_used_up_everywhere_blocks(self):
        accounts = [account("1", "active@test", True, 40, fable=100), account("2", "b@test", False, 0, fable=90)]
        with patch.object(claude, "load_accounts", return_value=(accounts, 0)):
            event = autoswitch.tick("claude", dry_run=True, now=1000)
        self.assertEqual(event["event"], "blocked")

    def test_used_up_fable_ignores_cooldown(self):
        autoswitch.state_path().parent.mkdir(parents=True, exist_ok=True)
        autoswitch.state_path().write_text('{"providers":{"claude":{"lastSwitchAt":900}}}')
        accounts = [account("1", "active@test", True, 40, fable=100), account("2", "b@test", False, 0, fable=0)]
        with patch.object(claude, "load_accounts", return_value=(accounts, 0)):
            event = autoswitch.tick("claude", dry_run=True, now=1000)
        self.assertEqual(event["event"], "switch")

    def test_model_threshold_setting(self):
        self.assertEqual(autoswitch.load_settings().model_threshold, 95.0)
        autoswitch.set_value("autoswitch.modelThreshold", "90")
        self.assertEqual(autoswitch.load_settings().model_threshold, 90.0)

    def test_best_strategy_counts_fable(self):
        self.assertEqual(claude._most_used({"five_hour": {"pct": 10}, "seven_day": {"pct": 20},
                                            "scoped": [{"name": "Fable", "pct": 99}]}), 99)
        self.assertEqual(claude._most_used({"five_hour": {"pct": 10}}), 10)


    def test_cli_parses_bare_auto(self):
        from cccswitcher import cli
        with patch.object(autoswitch, "tick", return_value={"event": "no-switch", "reason": "x",
                                                            "provider": "claude"}) as tick:
            self.assertEqual(cli.main(["auto", "--once"]), 2)
        self.assertEqual([c.args[0] for c in tick.call_args_list], ["claude", "codex"])


    def test_status_reports_service_and_thresholds(self):
        from cccswitcher import services
        with patch.dict(os.environ, {"CCC_SERVICE_PLATFORM": "linux", "XDG_CONFIG_HOME": str(self.root / "cfg")}):
            self.assertFalse(autoswitch.service_status()["installed"])
            services.install(services.AUTOSWITCH, load=False)
            status = autoswitch.service_status()
        self.assertTrue(status["installed"])
        self.assertEqual((status["threshold"], status["model_threshold"]), (90.0, 95.0))

    def test_cli_machine_action_reports_each_machine(self):
        from cccswitcher import cli, machines, remote
        local = machines.Machine(id="local", name="Mac")
        far = machines.Machine(id="far", name="far", alias="far")
        old = machines.Machine(id="old", name="old", alias="old")
        off = machines.Machine(id="off", name="off", alias="off")

        def run_script(machine, script, timeout):
            if machine.id == "far":
                assert "--install" in script
                return remote.RunResult(0, '{"installed": true, "threshold": 90}\n', "")
            if machine.id == "old":
                return remote.RunResult(2, "", "ccc: error: unrecognized arguments: --status")
            return remote.RunResult(255, "", "timeout", unreachable=True)

        with (
            patch.object(machines, "load", return_value=[local, far, old, off]),
            patch.object(remote, "run_script", side_effect=run_script),
            patch.object(cli, "_auto_service_here", return_value={"installed": True}) as here,
            patch("builtins.print") as out,
        ):
            rc = cli.main(["auto", "--install", "--json", "-m", "local", "-m", "far", "-m", "old", "-m", "off"])
        here.assert_called_once_with("install", None, None)
        result = {m["id"]: m for m in __import__("json").loads(out.call_args.args[0])["machines"]}
        self.assertEqual(rc, 0)
        self.assertTrue(result["local"]["installed"] and result["far"]["installed"])
        self.assertEqual(result["old"]["error"], "needs ccc 0.22.7 or later there")
        self.assertEqual(result["off"]["error"], "offline")



def wk(num, email, active, used, fable, reset_days):
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(1000 + reset_days * 86400, timezone.utc).isoformat()
    return claude.ClaudeAccount(num, email, "", "", active, {
        "seven_day": {"pct": used, "resets_at": at},
        "scoped": [{"name": "Fable", "pct": fable, "resets_at": at}]})


class LeftoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HOME": self.tmp.name, "CCC_CLAUDE_HOME": str(Path(self.tmp.name) / "c")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        autoswitch.set_value("autoswitch.leftover", "1")

    def run_tick(self, accounts):
        with (
            patch.object(claude, "load_accounts", return_value=(accounts, 0)),
            patch.object(claude, "switch_account"),
        ):
            return autoswitch.tick("claude", dry_run=True, now=1000)

    def test_stays_on_a_leftover_until_empty(self):
        event = self.run_tick([wk("1", "a", True, 50, 97, 3), wk("2", "b", False, 50, 96, 1)])
        self.assertEqual(event["reason"], "using-leftover")

    def test_empty_moves_to_the_leftover_that_expires_first(self):
        event = self.run_tick([wk("1", "a", True, 100, 100, 3), wk("2", "b", False, 50, 96, 4),
                               wk("3", "c", False, 92, 40, 2), wk("4", "d", False, 10, 10, 1)])
        self.assertEqual((event["to_num"], event["reason"]), ("3", "leftover"))

    def test_moves_from_a_fresh_account_to_a_leftover(self):
        event = self.run_tick([wk("1", "a", True, 10, 10, 3), wk("2", "b", False, 50, 96, 4)])
        self.assertEqual((event["to_num"], event["reason"]), ("2", "leftover-available"))

    def test_fresh_account_kept_when_no_leftovers(self):
        event = self.run_tick([wk("1", "a", True, 10, 10, 3), wk("2", "b", False, 100, 100, 4)])
        self.assertEqual(event["reason"], "no-leftovers")

    def test_empty_with_no_leftovers_falls_back_to_a_fresh_account(self):
        event = self.run_tick([wk("1", "a", True, 100, 100, 3), wk("2", "b", False, 100, 100, 1),
                               wk("3", "c", False, 20, 30, 5)])
        self.assertEqual((event["to_num"], event["reason"]), ("3", "empty-no-leftovers"))

    def test_fable_mode_treats_no_fable_left_as_empty(self):
        event = self.run_tick([wk("1", "a", True, 57, 100, 1), wk("2", "b", False, 50, 96, 3)])
        self.assertEqual((event["to_num"], event["reason"]), ("2", "leftover"))

    def test_off_by_default(self):
        autoswitch.set_value("autoswitch.leftover", "0")
        event = self.run_tick([wk("1", "a", True, 10, 10, 3), wk("2", "b", False, 50, 96, 4)])
        self.assertEqual(event["reason"], "below-threshold")


class WeeklyLeftoverTests(LeftoverTests):
    """leftover = weekly: the Fable limit does not make an account empty."""

    def setUp(self):
        super().setUp()
        autoswitch.set_value("autoswitch.leftover", "2")

    # Inherited tests whose accounts have Fable at 100% mean something else here.
    test_fable_mode_treats_no_fable_left_as_empty = None

    def test_no_fable_left_is_a_leftover_until_weekly_is_used_up(self):
        event = self.run_tick([wk("1", "a", True, 57, 100, 1), wk("2", "b", False, 10, 10, 3)])
        self.assertEqual(event["reason"], "using-leftover")
        event = self.run_tick([wk("1", "a", True, 10, 10, 3), wk("2", "b", False, 57, 100, 1)])
        self.assertEqual((event["to_num"], event["reason"]), ("2", "leftover-available"))
        event = self.run_tick([wk("1", "a", True, 99.6, 100, 1), wk("2", "b", False, 60, 100, 2)])
        self.assertEqual((event["to_num"], event["reason"]), ("2", "leftover"))

    def test_accounts_with_no_fable_left_go_first(self):
        # b still has Fable a Fable-mode machine can use; c has none.
        event = self.run_tick([wk("1", "a", True, 100, 100, 1), wk("2", "b", False, 92, 50, 2),
                               wk("3", "c", False, 60, 100, 4)])
        self.assertEqual((event["to_num"], event["reason"]), ("3", "leftover"))

    def test_accounts_with_a_reset_left_go_first(self):
        with_reset = wk("3", "c", False, 60, 100, 4)
        with_reset.usage["reset_credits_available"] = 1
        event = self.run_tick([wk("1", "a", True, 100, 100, 1), wk("2", "b", False, 60, 100, 2), with_reset])
        self.assertEqual((event["to_num"], event["reason"]), ("3", "leftover"))

    def test_status_reports_the_mode(self):
        status = autoswitch.service_status()
        self.assertEqual((status["leftover"], status["leftover_mode"]), (True, "weekly"))


if __name__ == "__main__":
    unittest.main()
