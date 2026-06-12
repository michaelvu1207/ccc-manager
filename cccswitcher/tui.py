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
import locale
import threading
from dataclasses import dataclass

from . import actions, claude, codex, monthly, theme
from .render import fmt_countdown, fmt_duration, fmt_money, fmt_tokens

HINTS = [
    ("↑/↓", "move"),
    ("enter", "switch / login"),
    ("l", "re-login"),
    ("r", "refresh"),
    ("q", "quit"),
]


@dataclass
class Window:
    label: str
    pct: float | None
    left: str  # human countdown, e.g. "2h 15m left"


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

    def windows(self) -> list[Window]:
        """Two rate-limit windows (5h + weekly) as structured data for bars."""
        usage = getattr(self.acct, "usage", None)
        if not isinstance(usage, dict):
            return []
        if self.provider == "claude":
            out = []
            for key, label in (("five_hour", "5h"), ("seven_day", "wk")):
                w = usage.get(key)
                if w:
                    cd = w.get("countdown", "")
                    out.append(Window(label, w.get("pct"), f"{cd} left" if cd else ""))
            return out
        out = []
        for key, label, style in (("primary", "5h", "hm"), ("secondary", "wk", "dh")):
            w = usage.get(key)
            if w:
                cd = fmt_countdown(w.get("resets_at"), style=style)
                out.append(Window(label, w.get("used_percent"), f"{cd} left" if cd else ""))
        return out


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
        self._last_shape: tuple | None = None

    # ---------- data ----------

    def _load(self, refresh: bool, then_refresh: bool = False, force_stats: bool = False) -> None:
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
            "claude_stats": lambda: monthly.fetch_stats("claude", force=force_stats),
            "codex_stats": lambda: monthly.fetch_stats("codex", force=force_stats),
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
            self.status = "loading usage stats…" if not self.stats else ""
            self.dirty = True

        threads["claude_stats"].join()
        threads["codex_stats"].join()
        with self.lock:
            self.stats = {
                "claude": results.get("claude_stats"),
                "codex": results.get("codex_stats"),
            }
            self.refreshing = False
            self.status = "" if refresh else "cached data — refreshing live usage…"
            self.dirty = True
        if then_refresh:
            self.refresh_async(refresh=True)

    def refresh_async(
        self, refresh: bool = True, then_refresh: bool = False, force_stats: bool = False
    ) -> None:
        with self.lock:
            if self.refreshing:
                return
            self.refreshing = True
            if refresh:
                self.status = "refreshing live usage…"
        threading.Thread(
            target=self._load, args=(refresh, then_refresh, force_stats), daemon=True
        ).start()

    def entries(self) -> list[Entry]:
        return self.claude_entries + self.codex_entries

    # ---------- actions ----------

    def _run_in_terminal(self, stdscr, fn) -> None:
        curses.endwin()
        print("\n" + "═" * 64)
        try:
            fn()
        except Exception as e:  # keep the TUI alive whatever the flow does
            print(f"error: {e}")
        try:
            input("\npress Enter to return to ccc…")
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

    # ---------- low-level drawing ----------

    @staticmethod
    def _put(stdscr, y: int, x: int, text: str, attr: int = 0) -> int:
        """Draw clipped text, returning the x just past it."""
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w - 1:
            return x
        text = text[: w - 1 - x]
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass
        return x + len(text)

    def _segs(self, stdscr, y: int, x: int, segments: list[tuple[str, int]]) -> int:
        for text, attr in segments:
            x = self._put(stdscr, y, x, text, attr)
        return x

    def _box(self, stdscr, top: int, left: int, width: int, height: int,
             title: str, title_role: str = "title") -> None:
        """Draw a rounded box with an embedded title."""
        b = theme.attr("border")
        right = left + width - 1
        bottom = top + height - 1
        # corners + edges
        self._put(stdscr, top, left, theme.TL + theme.HZ * (width - 2) + theme.TR, b)
        self._put(stdscr, bottom, left, theme.BL + theme.HZ * (width - 2) + theme.BR, b)
        for yy in range(top + 1, bottom):
            self._put(stdscr, yy, left, theme.VT, b)
            self._put(stdscr, yy, right, theme.VT, b)
        if title:
            self._segs(stdscr, top, left + 2, [
                (theme.HZ + " ", b),
                (title, theme.attr(title_role, bold=True)),
                (" " + theme.HZ, b),
            ])

    # ---------- panels ----------

    def _draw_header(self, stdscr, top: int, left: int, width: int) -> int:
        self._box(stdscr, top, left, width, 3, "CCC · Claude + Codex Switcher")
        inner = left + 2
        c, l = self._n_claude(), self._n_codex()
        active = self._active_summary()
        segs = [
            (theme.DOT + " ", theme.attr("good")),
            (f"{c} Claude", theme.attr("text", bold=True)),
            ("    " + theme.DOT + " ", theme.attr("value")),
            (f"{l} Codex", theme.attr("text", bold=True)),
        ]
        if active:
            segs += [("    " + theme.DIAMOND + " active: ", theme.attr("label")),
                     (active, theme.attr("value"))]
        self._segs(stdscr, top + 1, inner, segs)
        return top + 3

    def _n_claude(self) -> int:
        return len(self.claude_entries)

    def _n_codex(self) -> int:
        return len(self.codex_entries)

    def _active_summary(self) -> str:
        names = []
        for e in self.entries():
            if e.active:
                names.append(e.email.split("@")[0])
        return " / ".join(names)

    def _draw_accounts(self, stdscr, top: int, left: int, width: int) -> tuple[int, int]:
        sections = [("CLAUDE", self.claude_entries), ("CODEX", self.codex_entries)]
        rows = sum(len(e) for _, e in sections) + len(sections)
        height = rows + 2
        self._box(stdscr, top, left, width, height, "Accounts")

        inner = left + 2
        email_w = max((len(e.email) for e in self.entries()), default=10)
        email_w = min(email_w, max(12, width - 40))
        y = top + 1
        flat = 0
        for label, entries in sections:
            self._put(stdscr, y, inner, label, theme.attr("muted", bold=True))
            y += 1
            for e in entries:
                self._draw_account_row(stdscr, y, inner, width, e, flat == self.sel, email_w)
                y += 1
                flat += 1
        return top + height, height

    def _draw_account_row(self, stdscr, y, inner, width, e: Entry, selected, email_w):
        accent_attr = theme.attr("accent", bold=True)
        self._put(stdscr, y, inner - 1, theme.ACCENT if selected else " ", accent_attr)

        dot_attr = theme.attr("good") if e.active else theme.attr("muted")
        email_attr = theme.attr("text", bold=True) if selected else theme.attr("text")
        if e.active:
            email_attr = theme.attr("good", bold=selected)

        email = e.email if len(e.email) <= email_w else e.email[: email_w - 1] + "…"
        x = self._segs(stdscr, y, inner + 1, [
            (theme.DOT + " ", dot_attr),
            (f"{e.acct.num:>2}  ", theme.attr("muted")),
            (f"{email:<{email_w}}  ", email_attr),
        ])

        if e.needs_login:
            self._segs(stdscr, y, x, [
                (theme.WARN + " ", theme.attr("danger", bold=True)),
                ("needs login", theme.attr("danger")),
            ])
            return

        windows = e.windows()
        if not windows:
            self._put(stdscr, y, x, "usage unavailable", theme.attr("muted"))
            return
        for w in windows:
            bar_str, role = theme.bar(w.pct)
            pct = f"{round(w.pct):>3}%" if w.pct is not None else "  ?"
            x = self._segs(stdscr, y, x, [
                (f"{w.label} ", theme.attr("label")),
                (bar_str + " ", theme.attr(role)),
                (pct + "  ", theme.attr(role, bold=True)),
                (f"{w.left:<13}" if w.left else " " * 13, theme.attr("muted")),
            ])
            x += 1

    def _draw_stats(self, stdscr, top: int, left: int, width: int) -> int:
        month_keys = sorted(
            {m.month for s in self.stats.values() if s for m in s.months}
        )[-3:]
        stale = next((s for s in self.stats.values() if s and s.stale), None)
        height = 4 + 2 + (1 if stale else 0)
        self._box(stdscr, top, left, width, height, "Usage · all accounts, this machine")
        inner = left + 2

        col = 11
        mcol = 11
        lablabel = f"{'':<10}"
        header = f"{lablabel}{'last 7d':>{col}}{'last 30d':>{col}}"
        for mk in month_keys:
            header += f"{_month_short(mk):>{mcol}}"
        self._put(stdscr, top + 1, inner, header, theme.attr("label"))

        totals = {"7d": 0.0, "30d": 0.0}
        month_totals = {mk: 0.0 for mk in month_keys}
        y = top + 2
        for name, key, role in (("Claude", "claude", "value"), ("Codex", "codex", "value")):
            s = self.stats.get(key)
            if s is None:
                self._segs(stdscr, y, inner, [
                    (f"{name:<10}", theme.attr("text")),
                    ("collecting…", theme.attr("muted")),
                ])
                y += 1
                continue
            totals["7d"] += s.last7_cost
            totals["30d"] += s.last30_cost
            by_month = {m.month: m for m in s.months}
            line = f"{fmt_money(s.last7_cost):>{col}}{fmt_money(s.last30_cost):>{col}}"
            for mk in month_keys:
                row = by_month.get(mk)
                if row:
                    month_totals[mk] += row.cost
                line += f"{(fmt_money(row.cost) if row else '—'):>{mcol}}"
            self._segs(stdscr, y, inner, [
                (f"{name:<10}", theme.attr("text", bold=True)),
                (line, theme.attr(role)),
            ])
            y += 1

        tline = f"{fmt_money(totals['7d']):>{col}}{fmt_money(totals['30d']):>{col}}"
        for mk in month_keys:
            tline += f"{fmt_money(month_totals[mk]):>{mcol}}"
        self._segs(stdscr, y, inner, [
            (f"{'Total':<10}", theme.attr("title", bold=True)),
            (tline, theme.attr("title", bold=True)),
        ])
        y += 1
        if stale is not None:
            self._put(stdscr, y, inner,
                      f"cached {fmt_duration(stale.age_seconds)} ago · recompute failed, press r to retry",
                      theme.attr("warn"))
        return top + height

    def _draw_footer(self, stdscr, y: int, left: int, width: int) -> None:
        with self.lock:
            status, refreshing = self.status, self.refreshing
        if status:
            role = "warn" if refreshing else "muted"
            self._put(stdscr, y - 1, left, status, theme.attr(role))
        x = left
        for i, (key, desc) in enumerate(HINTS):
            if i:
                x = self._put(stdscr, y, x, f"  {theme.BULLET}  ", theme.attr("border"))
            x = self._put(stdscr, y, x, key, theme.attr("value", bold=True))
            x = self._put(stdscr, y, x, " " + desc, theme.attr("muted"))

    def _draw(self, stdscr) -> None:
        # Full clear only when the layout shape changes (accounts/stats arriving
        # after the first paint), so growing panels don't leave stale cells;
        # steady-state frames use erase() to avoid flicker.
        shape = (len(self.claude_entries), len(self.codex_entries), bool(self.stats),
                 stdscr.getmaxyx())
        if shape != self._last_shape:
            stdscr.clear()
            self._last_shape = shape
        else:
            stdscr.erase()
        h, w = stdscr.getmaxyx()
        left, width = 1, max(20, w - 2)
        y = self._draw_header(stdscr, 0, left, width) + 1
        y, _ = self._draw_accounts(stdscr, y, left, width)
        y += 1
        if self.stats:
            y = self._draw_stats(stdscr, y, left, width) + 1
        self._draw_footer(stdscr, h - 1, left, width)
        stdscr.noutrefresh()
        curses.doupdate()

    # ---------- main loop ----------

    def main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.timeout(150)
        if curses.has_colors():
            theme.setup()

        # Paint cached data immediately, then refresh live usage behind it.
        self.refresh_async(refresh=False, then_refresh=True)

        while True:
            with self.lock:
                redraw = self.dirty
                self.dirty = False
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
                self.refresh_async(refresh=True, force_stats=True)
                self.dirty = True
            elif entries and ch in (curses.KEY_ENTER, 10, 13):
                self._activate(stdscr, entries[min(self.sel, len(entries) - 1)])
            elif entries and ch == ord("l"):
                self._login(stdscr, entries[min(self.sel, len(entries) - 1)], stay=False)


def _month_short(mk: str) -> str:
    """'2026-06' -> 'Jun'."""
    months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        return months[int(mk.split("-")[1])]
    except (IndexError, ValueError):
        return mk


def run() -> int:
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(App().main)
    return 0
