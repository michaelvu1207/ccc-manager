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
    pct: float | None   # percent *used*
    resets: str         # compact countdown to reset, e.g. "2h15m" / "3d4h"


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
                    cd = (w.get("countdown") or "").replace(" ", "")
                    out.append(Window(label, w.get("pct"), cd))
            return out
        out = []
        for key, label, style in (("primary", "5h", "hm"), ("secondary", "wk", "dh")):
            w = usage.get(key)
            if w:
                cd = fmt_countdown(w.get("resets_at"), style=style).replace(" ", "")
                out.append(Window(label, w.get("used_percent"), cd))
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
        self._icon_mode = "ascii"  # "image" | "art" | "ascii"
        self._art_size = (0, 0)

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

    def _frame(self, stdscr, top, left, width, height, title=""):
        """Outer container: a quiet rounded frame in the 'frame' colour."""
        f = theme.attr("frame")
        right, bottom = left + width - 1, top + height - 1
        self._put(stdscr, top, left, theme.TL + theme.HZ * (width - 2) + theme.TR, f)
        self._put(stdscr, bottom, left, theme.BL + theme.HZ * (width - 2) + theme.BR, f)
        for yy in range(top + 1, bottom):
            self._put(stdscr, yy, left, theme.VT, f)
            self._put(stdscr, yy, right, theme.VT, f)
        if title:
            self._segs(stdscr, top, left + 2,
                       [(theme.HZ + " ", f), (title, theme.attr("title", bold=True)), (" " + theme.HZ, f)])

    # ---------- panels ----------

    def _active_summary(self) -> str:
        return " / ".join(e.email.split("@")[0] for e in self.entries() if e.active)

    def _draw_icon(self, stdscr, top, left, width, height):
        """Top-left app icon: an inline image where supported, else ASCII."""
        self._box(stdscr, top, left, width, height, "")
        if self._icon_mode in ("image", "art"):
            return  # emitted after doupdate, into this box
        midy = top + height // 2
        for dy, text, role, bold in (
            (-2, "꒰ ◕‿◕ ꒱", "danger", True),
            (0, "C C C", "value", True),
            (2, "Claude + Codex", "muted", False),
        ):
            tx = left + max(1, (width - len(text)) // 2)
            self._put(stdscr, midy + dy, tx, text, theme.attr(role, bold=bold))

    def _draw_accounts(self, stdscr, top, left, width, height):
        """Top-right account switcher."""
        self._box(stdscr, top, left, width, height, "Accounts")
        inner = left + 2
        right = left + width - 2
        avail = right - inner
        email_w = max((len(e.email) for e in self.entries()), default=10)
        email_w = min(email_w, max(12, avail - 86))  # leave room for bars + "resets in" text
        y = top + 1
        flat = 0
        sections = [("CLAUDE", "claude", self.claude_entries),
                    ("CODEX", "codex", self.codex_entries)]
        for label, prole, entries in sections:
            if y >= top + height - 1:
                break
            self._put(stdscr, y, inner, label, theme.attr("muted", bold=True))
            y += 1
            for e in entries:
                if y >= top + height - 1:
                    break
                self._draw_account_row(stdscr, y, inner, right, e, flat == self.sel, email_w)
                y += 1
                flat += 1

    def _draw_account_row(self, stdscr, y, inner, right, e: Entry, selected, email_w):
        prole = "claude" if e.provider == "claude" else "codex"
        self._put(stdscr, y, inner - 1, theme.ACCENT if selected else " ",
                  theme.attr("accent", bold=True))
        # Keep colour quiet: only the highlighted row wears its provider tint
        # (Claude orange / Codex blue). Active account is marked by a green dot.
        dot_attr = theme.attr("good") if e.active else theme.attr("muted")
        if selected:
            email_attr = theme.attr(prole, bold=True)
        else:
            email_attr = theme.attr("text", bold=e.active)

        email = e.email if len(e.email) <= email_w else e.email[: email_w - 1] + "…"
        segs = [
            (theme.DOT + " ", dot_attr),
            (f"{e.acct.num:>2}  ", theme.attr("muted")),
            (f"{email:<{email_w}}  ", email_attr),
        ]
        if e.needs_login:
            segs += [(theme.WARN + " ", theme.attr("danger", bold=True)),
                     ("needs login", theme.attr("danger"))]
            self._segs_clip(stdscr, y, inner + 1, right, segs)
            return
        windows = e.windows()
        if not windows:
            segs.append(("usage unavailable", theme.attr("muted")))
            self._segs_clip(stdscr, y, inner + 1, right, segs)
            return
        for w in windows:
            remaining = None if w.pct is None else 100 - w.pct
            bar_str, role = theme.bar(remaining)
            pct = f"{round(remaining):>3}% left" if remaining is not None else "   ? left"
            reset = f" · resets in {w.resets}" if w.resets else ""
            segs += [
                (f"{w.label} ", theme.attr("label")),
                (bar_str + " ", theme.attr(role)),
                (pct, theme.attr(role, bold=True)),
                (reset, theme.attr("muted")),
                ("   ", 0),
            ]
        self._segs_clip(stdscr, y, inner + 1, right, segs)

    def _segs_clip(self, stdscr, y, x, right, segments):
        for text, attr in segments:
            if x >= right:
                break
            if x + len(text) > right:
                text = text[: right - x]
            x = self._put(stdscr, y, x, text, attr)

    def _draw_usage_table(self, stdscr, top, left, width) -> int:
        month_keys = sorted({m.month for s in self.stats.values() if s for m in s.months})[-3:]
        self._put(stdscr, top, left, "USAGE  ·  all accounts on this machine",
                  theme.attr("title", bold=True))
        y = top + 2

        name_w, col = 12, 13
        header = f"{'PROVIDER':<{name_w}}{'LAST 7D':>{col}}{'LAST 30D':>{col}}"
        for mk in month_keys:
            header += f"{_month_short(mk).upper():>{col}}"
        self._put(stdscr, y, left + 2, header, theme.attr("label"))
        y += 1
        self._put(stdscr, y, left, theme.HZ * (width - 2), theme.attr("frame"))
        y += 1

        totals = {"7d": 0.0, "30d": 0.0}
        month_totals = {mk: 0.0 for mk in month_keys}
        rows = [("Claude", "claude"), ("Codex", "codex")]
        for i, (name, key) in enumerate(rows):
            s = self.stats.get(key)
            self._put(stdscr, y, left, theme.DOTTED, theme.attr("accent"))
            if s is None:
                self._segs(stdscr, y, left + 2, [
                    (f"{name:<{name_w}}", theme.attr("text", bold=True)),
                    ("collecting…", theme.attr("muted"))])
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
                line += f"{(fmt_money(row.cost) if row else '—'):>{col}}"
            self._segs(stdscr, y, left + 2, [
                (f"{name:<{name_w}}", theme.attr("text", bold=True)),
                (line, theme.attr("value"))])
            y += 1

        self._put(stdscr, y, left, theme.HZ * (width - 2), theme.attr("frame"))
        y += 1
        self._put(stdscr, y, left, theme.DOTTED, theme.attr("accent"))
        tline = f"{fmt_money(totals['7d']):>{col}}{fmt_money(totals['30d']):>{col}}"
        for mk in month_keys:
            tline += f"{fmt_money(month_totals[mk]):>{col}}"
        self._segs(stdscr, y, left + 2, [
            (f"{'Total':<{name_w}}", theme.attr("title", bold=True)),
            (tline, theme.attr("title", bold=True))])
        y += 1

        stale = next((s for s in self.stats.values() if s and s.stale), None)
        if stale is not None:
            self._put(stdscr, y, left + 2,
                      f"cached {fmt_duration(stale.age_seconds)} ago · recompute failed, press r",
                      theme.attr("warn"))
            y += 1
        return y

    def _draw_footer(self, stdscr, y: int, left: int) -> None:
        with self.lock:
            status, refreshing = self.status, self.refreshing
        if status:
            self._put(stdscr, y - 1, left, status,
                      theme.attr("warn" if refreshing else "muted"))
        x = left
        for i, (key, desc) in enumerate(HINTS):
            if i:
                x = self._put(stdscr, y, x, f"   {theme.BULLET}   ", theme.attr("frame"))
            x = self._put(stdscr, y, x, key, theme.attr("value", bold=True))
            x = self._put(stdscr, y, x, " " + desc, theme.attr("muted"))

    def _layout(self, w: int) -> dict:
        acc_rows = len(self.claude_entries) + len(self.codex_entries) + 2
        art_cols, art_rows = self._art_size
        # Icon box hugs the art (+2 border) but always leaves room for accounts.
        icon_w = min(art_cols + 2, max(24, w - 44)) if art_cols else 22
        box_h = max(acc_rows + 2, (art_rows + 2) if art_rows else 12)
        acc_left = 2 + icon_w + 2
        return {
            "box_top": 2, "box_h": box_h, "icon_left": 2, "icon_w": icon_w,
            "acc_left": acc_left, "acc_w": max(20, w - acc_left - 2),
            "cont_bottom": 2 + box_h + 1,
        }

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
        L = self._layout(w)

        # Outer container wrapping icon + account switcher.
        self._frame(stdscr, 0, 0, w, L["cont_bottom"] + 1, "CCC · Claude + Codex Switcher")
        self._draw_icon(stdscr, L["box_top"], L["icon_left"], L["icon_w"], L["box_h"])
        self._draw_accounts(stdscr, L["box_top"], L["acc_left"], L["acc_w"], L["box_h"])

        # Usage table below the container.
        if self.stats:
            self._draw_usage_table(stdscr, L["cont_bottom"] + 2, 2, w - 4)

        self._draw_footer(stdscr, h - 1, 2)
        stdscr.noutrefresh()
        curses.doupdate()

        # Icon, after curses has painted the frame (curses doesn't know these
        # pixels, so re-emit each repaint — repaints only happen on input).
        from . import image
        if self._icon_mode == "image":
            image.emit(L["box_top"] + 2, L["icon_left"] + 2, L["icon_w"] - 2, L["box_h"] - 2)
        elif self._icon_mode == "art":
            image.emit_art(L["box_top"] + 2, L["icon_left"] + 2,
                           max_cols=L["icon_w"] - 2, max_rows=L["box_h"] - 2)

    # ---------- main loop ----------

    def main(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.timeout(150)
        if curses.has_colors():
            theme.setup()
        from . import image
        self._art_size = image.art_size()
        if image.supported():
            self._icon_mode = "image"      # real PNG on iTerm2 / WezTerm
        elif image.art_lines():
            self._icon_mode = "art"        # half-block art everywhere else
        else:
            self._icon_mode = "ascii"      # last-ditch text face

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
