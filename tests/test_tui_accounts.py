"""TUI account view: Enter acts on the selected machine chip, never on the row."""

import unittest
from unittest import mock

from cccswitcher import accounts_view, machines, tui

try:
    from .test_accounts_view import acct, machine
except ImportError:  # `unittest discover -s tests`
    from test_accounts_view import acct, machine


class EnterActsOnChipOnly(unittest.TestCase):
    def setUp(self):
        self.app = tui.App()
        self.app.view = accounts_view.build([
            machine("local", [acct("1", "a@x.com", active=True)], local=True),
            machine("box", [acct("3", "a@x.com")]),
        ])
        self.account = self.app.view["accounts"][0]
        fleet = [machines.Machine(id="local", name="this Mac"),
                 machines.Machine(id="box", name="box", alias="box")]
        patcher = mock.patch.object(tui.machines, "load", return_value=fleet)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_enter_on_active_chip_switches_nothing(self):
        self.app.chip = 0  # this Mac: the account is already active here
        with mock.patch.object(tui.actions, "account_action") as action:
            self.app._chip_action(None, self.account, "enter")
        action.assert_not_called()
        self.assertIn("already active", self.app.status)

    def test_enter_on_stored_chip_switches_that_machine_only(self):
        self.app.chip = 1
        with mock.patch.object(tui.actions, "account_action", return_value=(0, "ok")) as action, \
                mock.patch.object(self.app, "refresh_async"):
            self.app._chip_action(None, self.account, "enter")
        action.assert_called_once()
        target, provider, verb, query = action.call_args.args
        self.assertEqual((target.id, provider, verb, query), ("box", "claude", "switch", "3"))


if __name__ == "__main__":
    unittest.main()
