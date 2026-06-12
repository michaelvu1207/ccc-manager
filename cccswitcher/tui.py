"""Interactive curses TUI — the default `ccc` view.

Arrow keys move between accounts across both providers. Enter activates the
selected account; if its credentials are dead it launches the provider's
login page instead (and keeps it active afterwards). `l` re-authenticates the
selection while restoring your current active account, `r` refreshes usage,
`q` quits. Account emails are copied to the clipboard before login flows so
they can be pasted straight into the provider's account picker.
"""

from __future__ import annotations

import curses
import re
import threading
from dataclasses import dataclass

from . import actions, claude, codex, monthly
from .render import fmt_money, fmt_tokens

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

KEY_HINTS = "↑↓ move · enter switch/login · l re-login · r refresh · q quit"


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class Entry:
    provider: str  # "claude" | "codex"
    acct: object  # ClaudeAccount | CodexAccount
    needs_login: bool

    @property
    def email(self) -> str:
        return self.acct.email

    @property
    def active(self) -> bool:
        return self.acct.active

    def usage_text(self) -> str:
        if self.provider == "claude":
            cells = actions.claude_usage_cells(self.acct.usage)
        else:
            cells = actions.codex_usage_cells(self.acct)
        return _plain("  ".join(cells))


class App:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.claude_entries: list[Entry] = []
        self.codex_entries: list[Entry] = []
        self.stats: dict[str, monthly.ProviderStats | None] = {}
        self.status = "loading..."
        self.refreshing = False
        self.dirty = True
        self.sel = 0

    # ---------- data ----------

    def _load(self, refresh: bool, then_refresh: bool = False) -> None:
        """Fetch accounts and usage stats, publishing each as it lands.

        Plain daemon threads, not ThreadPoolExecutor: concurrent.futures
        joins its workers at interpreter exit, so `q` would hang on any
        in-flight cswap/codex-auth/ccusage subprocess. Accounts (fast) are
        published as soon as both providers load; ccusage stats (slow,
        possibly npx) fill in afterwards.
        """
        results: dict = {}

        def runner(key, fn):
            try:
                results[key] = fn()
            except Exception:
                results[key] = None

        jobs = {
            "claude": lambda: claude.load_accounts(refresh),
            "codex": lambda: codex.load_accounts(refresh),
            "claude_stats": lambda: monthly.fetch_stats("claude"),
            "codex_stats": lambda: monthly.fetch_stats("codex"),
        }
        threads = {
            k: threading.Thread(target=runner, args=(k, fn), daemon=True)
            for k, fn in jobs.items()
        }
        for t in threads.values():
            t.start()

        threads["claude"].join()
        threads["codex"].join()
        claude_accounts = (results.get("claude") or ([], None))[0]
        codex_accounts = results.get("codex") or []
        claude_entries = [
            Entry("claude", a, claude.needs_login(a)) for a in claude_accounts
        ]
        codex_entries = [Entry("codex", a, codex.needs_login(a)) for a in codex_accounts]
        with self.lock:
            self.claude_entries = claude_entries
            self.codex_entries = codex_entries
            self.status = "loading usage stats..." if not self.stats else ""
            self.dirty = True

        threads["claude_stats"].join()
        threads["codex_stats"].join()
        with self.lock:
            self.stats = {
                "claude": results.get("claude_stats"),
                "codex": results.get("codex_stats"),
            }
            self.refreshing = False
            self.status = "" if refresh else "cached data — refreshing live usage..."
            self.dirty = True
        if then_refresh:
            self.refresh_async(refresh=True)

    def refresh_async(self, refresh: bool = True, then_refresh: bool = False) -> None:
        with self.lock:
            if self.refreshing:
                return
            self.refreshing = True
            if refresh:
                self.status = "refreshing live usage..."
        threading.Thread(
            target=self._load, args=(refresh, then_refresh), daemon=True
        ).start()

    def entries(self) -> list[Entry]:
        return self.claude_entries + self.codex_entries

    # ---------- actions ----------

    def _run_in_terminal(self, stdscr, fn) -> None:
        curses.endwin()
        print("\n" + "=" * 64)
        try:
            fn()
        except Exception as e:  # keep the TUI alive whatever the flow does
            print(f"error: {e}")
        try:
            input("\npress Enter to return to ccc...")
        except EOFError:
            pass
        stdscr.clear()
        curses.curs_set(0)
        self.refresh_async(refresh=True)

    def _login(self, stdscr, entry: Entry, stay: bool) -> None:
        actions.copy_to_clipboard(entry.email)
        if entry.provider == "claude":
            self._run_in_terminal(
                stdscr, lambda: actions.login_claude(entry.acct.num, stay=stay)
            )
        else:
            self._run_in_terminal(stdscr, lambda: actions.login_codex(stay=stay))

    def _activate(self, stdscr, entry: Entry) -> None:
        if entry.needs_login:
            self._login(stdscr, entry, stay=True)
            return
        if entry.active:
            self.status = f"{entry.email} is already the active {entry.provider} account"
            self.dirty = True
            return
        query = entry.acct.num if entry.provider == "claude" else entry.email
        rc, message = actions.switch_account(entry.provider, query)
        self.status = message
        self.dirty = True
        if rc == 0:
            self.refresh_async(refresh=False)

    # ---------- drawing ----------

    @staticmethod
    def _addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            stdscr.addstr(y, x, text[: w - x - 1], attr)
        except curses.error:
            pass

    def _draw_accounts(self, stdscr, y: int) -> int:
        sections = [
            ("Claude (cswap)", self.claude_entries),
            ("Codex (codex-auth)", self.codex_entries),
        ]
        flat_index = 0
        for title, entries in sections:
            self._addstr(stdscr, y, 1, title, curses.A_BOLD)
            y += 1
            if not entries:
                self._addstr(stdscr, y, 3, "no accounts found", curses.A_DIM)
                y += 1
            width = max((len(e.email) for e in entries), default=0)
            for entry in entries:
                selected = flat_index == self.sel
                marker = "*" if entry.active else " "
                badge = "  ⚠ needs login" if entry.needs_login else ""
                line = f" {marker} {entry.acct.num:>2}  {entry.email:<{width}}  {entry.usage_text()}{badge}"
                attr = curses.A_REVERSE if selected else 0
                if entry.active and not selected:
                    attr |= curses.color_pair(1)
                if entry.needs_login and not selected:
                    attr |= curses.color_pair(2)
                self._addstr(stdscr, y, 1, line.ljust(stdscr.getmaxyx()[1] - 3), attr)
                y += 1
                flat_index += 1
            y += 1
        return y

    def _draw_stats(self, stdscr, y: int) -> int:
        self._addstr(
            stdscr,
            y,
            1,
            "Usage — local logs, all accounts combined",
            curses.A_BOLD,
        )
        y += 1

        month_keys: list[str] = sorted(
            {m.month for s in self.stats.values() if s for m in s.months}
        )[-3:]
        header = f"  {'':<8}{'last 7d':>20}{'last 30d':>20}" + "".join(
            f"{mk:>12}" for mk in month_keys
        )
        self._addstr(stdscr, y, 1, header, curses.A_DIM)
        y += 1

        totals = {"7d": 0.0, "30d": 0.0}
        month_totals = {mk: 0.0 for mk in month_keys}
        for name, key in (("Claude", "claude"), ("Codex", "codex")):
            stats = self.stats.get(key)
            if stats is None:
                self._addstr(stdscr, y, 1, f"  {name:<8}{'(ccusage unavailable)':>20}", curses.A_DIM)
                y += 1
                continue
            totals["7d"] += stats.last7_cost
            totals["30d"] += stats.last30_cost
            by_month = {m.month: m for m in stats.months}
            cells = (
                f"  {name:<8}"
                f"{fmt_money(stats.last7_cost) + ' · ' + fmt_tokens(stats.last7_tokens):>20}"
                f"{fmt_money(stats.last30_cost) + ' · ' + fmt_tokens(stats.last30_tokens):>20}"
            )
            for mk in month_keys:
                row = by_month.get(mk)
                if row:
                    month_totals[mk] += row.cost
                cells += f"{fmt_money(row.cost) if row else '--':>12}"
            self._addstr(stdscr, y, 1, cells)
            y += 1

        total_line = (
            f"  {'Total':<8}"
            f"{fmt_money(totals['7d']):>20}"
            f"{fmt_money(totals['30d']):>20}"
        ) + "".join(f"{fmt_money(month_totals[mk]):>12}" for mk in month_keys)
        self._addstr(stdscr, y, 1, total_line, curses.A_BOLD)
        return y + 1

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        h, _ = stdscr.getmaxyx()
        self._addstr(stdscr, 0, 1, "CCCSwitcher", curses.A_BOLD | curses.color_pair(1))
        self._addstr(stdscr, 0, 14, KEY_HINTS, curses.A_DIM)

        y = self._draw_accounts(stdscr, 2)
        y = self._draw_stats(stdscr, y)

        with self.lock:
            status = self.status
            refreshing = self.refreshing
        if refreshing and not status:
            status = "refreshing..."
        if status:
            attr = curses.color_pair(3) if refreshing else curses.A_DIM
            self._addstr(stdscr, h - 1, 1, status, attr)
        stdscr.noutrefresh()
        curses.doupdate()

    # ---------- main loop ----------

    def main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.timeout(150)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)

        # Paint cached data immediately, then refresh live usage behind it.
        self.refresh_async(refresh=False, then_refresh=True)

        while True:
            with self.lock:
                if self.dirty:
                    self.dirty = False
                    redraw = True
                else:
                    redraw = False
            if redraw:
                self._draw(stdscr)

            ch = stdscr.getch()
            if ch == -1:
                continue

            entries = self.entries()
            if ch in (ord("q"), 27):
                return
            elif ch in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
                self.dirty = True
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.sel = min(max(len(entries) - 1, 0), self.sel + 1)
                self.dirty = True
            elif ch == curses.KEY_RESIZE:
                self.dirty = True
            elif ch == ord("r"):
                self.refresh_async(refresh=True)
                self.dirty = True
            elif entries and ch in (curses.KEY_ENTER, 10, 13):
                self._activate(stdscr, entries[min(self.sel, len(entries) - 1)])
            elif entries and ch == ord("l"):
                self._login(stdscr, entries[min(self.sel, len(entries) - 1)], stay=False)


def run() -> int:
    curses.wrapper(App().main)
    return 0
