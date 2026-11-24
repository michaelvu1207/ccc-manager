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


class FakeScreen:
    def __init__(self):
        self.cells = {}

    def addstr(self, y, x, text, attr=0):
        for i, ch in enumerate(text):
            self.cells[(y, x + i)] = ch

    def addnstr(self, y, x, text, n, attr=0):
        self.addstr(y, x, text[:n], attr)

    def getmaxyx(self):
        return (40, 120)

    def line(self, y):
        xs = [x for (yy, x) in self.cells if yy == y]
        return "".join(self.cells.get((y, x), " ") for x in range(max(xs) + 1)) if xs else ""


class MatrixDrawing(unittest.TestCase):
    def test_markers_line_up_under_machine_columns(self):
        app = tui.App()
        app.view = accounts_view.build([
            dict(machine("local", [acct("1", "a@x.com", active=True)], local=True), platform="Darwin"),
            dict(machine("box", [acct("3", "a@x.com"), acct("4", "b@x.com", needs_login=True)]),
                 display_name="studio-a100"),
            dict(machine("lap", []), display_name="studio-laptop"),
        ])
        screen = FakeScreen()
        with mock.patch.object(tui.theme, "attr", return_value=0), mock.patch.object(tui.curses, "A_REVERSE", 0):
            app._draw_account_view(screen, 0, 0, 80, 12)
        header, row_a, row_b = screen.line(1), screen.line(2), screen.line(4)
        for name, marker, row in (("Mac", "●", row_a), ("a100", "○", row_a), ("laptop", "+", row_a),
                                  ("a100", "!", row_b)):
            start = header.index(name)
            self.assertIn(marker, row[start - 1:start + len(name) + 1], (name, header, row))
        self.assertNotIn("●", row_a[:row_a.index("a@x.com")])  # no leading active dot


class CliMatrix(unittest.TestCase):
    def test_accounts_prints_header_and_one_marker_per_column(self):
        import argparse
        import io
        import re
        from contextlib import redirect_stdout

        from cccswitcher import cli
        view = accounts_view.build([
            dict(machine("local", [acct("1", "a@x.com", active=True)], local=True), platform="Darwin"),
            dict(machine("box", [acct("3", "a@x.com")]), display_name="work-desktop"),
        ])
        out = io.StringIO()
        with mock.patch.object(cli, "_view", return_value=view), redirect_stdout(out):
            cli.cmd_accounts(argparse.Namespace(no_refresh=True, json=False))
        lines = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in out.getvalue().splitlines()]
        header = next(ln for ln in lines if ln.startswith("Claude"))
        row = next(ln for ln in lines if "a@x.com" in ln)
        self.assertEqual(row[header.index("Mac") + 1], "●")
        self.assertEqual(row[header.index("work") + 1:header.index("work") + 3].strip(), "○")


if __name__ == "__main__":
    unittest.main()
