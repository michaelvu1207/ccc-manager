"""Interactive curses TUI — the default `ccc` view.

Default view: one row per account (Claude, then Codex) with its usage and a
line of machine chips (● active, ○ stored, ! needs login, + not on that
machine, × offline). ↑/↓ pick an account, ←/→ pick a machine chip, Enter acts
on that chip (never on the row as a whole): switch that machine to its stored
login, re-login, or start a fresh login there. `l` re-logs in, `d` toggles disable on that machine, `m`
switches to the machine-grouped view, `r` refreshes, `q` quits. Credentials
never move between machines.
"""

from __future__ import annotations

import curses
import locale
import threading
from dataclasses import dataclass

from . import accounts_view, actions, machines, monthly, remote, remote_parse, snapshot, theme
from .render import fmt_age, fmt_duration

HINTS = [
    ("↑/↓", "move"),
    ("←/→", "machine"),
    ("m", "view"),
    ("enter", "switch / login"),
    ("l", "re-login"),
    ("d", "disable"),
    ("a", "add account"),
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
    provider: str  # "claude" | "codex" | "add" (placeholder row: add an account)
    acct: dict | None  # snapshot-shaped account dict
    state: remote.MachineState

    @property
    def machine(self) -> machines.Machine:
        return self.state.machine

    @property
    def num(self) -> str:
        return self.acct["num"] if self.acct else ""

    @property
    def email(self) -> str:
        return self.acct["email"] if self.acct else ""

    @property
    def disabled(self) -> bool:
        return bool(self.acct and self.acct.get("disabled"))

    @property
    def needs_login(self) -> bool:
        return bool(self.acct and self.acct.get("needs_login"))

    @property
    def label(self) -> str:
        if not self.acct:
            return "+ add a Claude account"
        return self.acct["label"] + (" (disabled)" if self.disabled else "")

    @property
    def active(self) -> bool:
        return bool(self.acct and self.acct.get("active"))

    @property
    def note(self) -> str:
        return (self.acct or {}).get("status_message") or ""

    def windows(self) -> list[Window]:
        out = []
        for raw in (self.acct or {}).get("windows") or []:
            w = remote_parse.render_window(raw)
            out.append(Window(w["id"], w["pct_used"], (w["resets_in"] or "").replace(" ", "")))
        return out


def build_entries(states: list[remote.MachineState]) -> list[list[Entry]]:
    """Selectable rows per machine; duplicates of another machine are skipped."""
    groups = []
    for state in states:
        if state.duplicate_of:
            groups.append([])
            continue
        claude_rows = state.accounts("claude")
        rows = [Entry("claude", a, state) for a in claude_rows]
        rows += [Entry("codex", a, state) for a in state.accounts("codex")]
        claude_available = (state.provider("claude") or {}).get("available", False)
        if not claude_rows and claude_available and (state.online or state.has_data):
            rows.insert(0, Entry("add", None, state))
        groups.append(rows)
    return groups


def _local_state(machine: machines.Machine, refresh: bool) -> remote.MachineState:
    import platform

    return remote.MachineState(
        machine=machine, online=True, platform=platform.system(),
        fingerprint=remote.local_fingerprint(), providers=snapshot._local_providers(refresh),
        has_data=True,
    )


CHIPS = {"active": "●", "stored": "○", "pending": "◌", "needs_login": "!", "absent": "+", "offline": "×"}


class App:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: list[remote.MachineState] = []
        self.groups: list[list[Entry]] = []
        # Account-centric view (default): one row per account, machine chips.
        self.mode = "accounts"  # | "machines"
        self.view: dict = {"machines": [], "accounts": []}
        self.chip = 0  # selected machine chip in accounts mode
        self.stats: dict[str, monthly.ProviderStats | None] = {}
        self.status = "loading..."
        self.refreshing = False
        self.dirty = True
        self.sel = 0
        self._last_shape: tuple | None = None
        self._icon_mode = "ascii"  # "image" | "art" | "ascii"
        self._art_size = (0, 0)

    # ---------- data ----------

    def _publish(self, states: list[remote.MachineState]) -> None:
        all_machines = machines.load()
        remote.mark_duplicates(states, all_machines)
        groups = build_entries(states)
        view = accounts_view.build([snapshot.machine_payload(s) for s in states])
        with self.lock:
            self.states = states
            self.groups = groups
            self.view = view
            self.dirty = True

    def _load(self, refresh: bool, then_refresh: bool = False, force_stats: bool = False) -> None:
        """Fetch machines and usage stats, publishing each as it lands.

        Plain daemon threads, not ThreadPoolExecutor: concurrent.futures
        joins its workers at interpreter exit, so `q` would hang on any
        in-flight ssh/cswap/ccusage subprocess. Machines publish one by one
        as they answer, so a slow host never holds up the others.
        """
        targets = [m for m in machines.load() if m.enabled]
        states: list[remote.MachineState] = [
            remote.MachineState(machine=m, online=True) if m.local else remote.cached_state(m)
            for m in targets
        ]
        # This Mac's cached accounts first (fast, local files only).
        for i, m in enumerate(targets):
            if m.local:
                try:
                    states[i] = _local_state(m, refresh=False)
                except Exception as exc:
                    states[i].error = str(exc)
        self._publish(list(states))

        stats: dict = {}

        def stats_runner(key):
            try:
                stats[key] = monthly.fetch_stats(key, force=force_stats)
            except Exception:
                stats[key] = None

        def machine_runner(i, machine):
            try:
                state = (_local_state(machine, refresh=True) if machine.local
                         else remote.probe(machine, refresh=True))
            except Exception as exc:  # keep the TUI alive whatever happens
                state = states[i]
                state.error = str(exc)
            states[i] = state
            self._publish(list(states))

        threads = [threading.Thread(target=stats_runner, args=(k,), daemon=True)
                   for k in ("claude", "codex")]
        if refresh:
            threads += [threading.Thread(target=machine_runner, args=(i, m), daemon=True)
                        for i, m in enumerate(targets)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with self.lock:
            self.stats = {"claude": stats.get("claude"), "codex": stats.get("codex")}
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
                self.status = "refreshing every machine…"
        threading.Thread(
            target=self._load, args=(refresh, then_refresh, force_stats), daemon=True
        ).start()

    def entries(self) -> list:
        if self.mode == "accounts":
            return self.account_rows()
        return [e for group in self.groups for e in group]

    def account_rows(self) -> list[dict]:
        rows = self.view["accounts"]
        return [a for a in rows if a["provider"] == "claude"] + [a for a in rows if a["provider"] == "codex"]

    # ---------- account-centric actions ----------

    def _chip_machine(self) -> tuple[dict, machines.Machine] | None:
        ms = self.view["machines"]
        if not ms:
            return None
        info = ms[min(self.chip, len(ms) - 1)]
        machine = machines.find(machines.load(), info["id"])
        return (info, machine) if machine else None

    def _chip_action(self, stdscr, account: dict, kind: str = "enter") -> None:
        picked = self._chip_machine()
        if picked is None:
            return
        info, machine = picked
        entry = account["machines"].get(info["id"], {"state": "offline"})
        state, provider = entry["state"], account["provider"]
        if state == "offline":
            self._say(f"{machine.name} is offline")
            return
        if kind == "disable":
            if provider != "claude" or state == "absent":
                self._say("only a stored Claude account can be disabled")
                return
            action = "enable" if entry.get("disabled") else "disable"
            rc, message = actions.account_action(machine, "claude", action, entry["num"])
            self._say(message)
            if rc == 0:
                self.refresh_async(refresh=False)
            return
        if kind == "login" or state == "needs_login":
            if state == "absent":
                kind = "enter"
            elif provider == "codex" and not machine.local:
                self._say(f"codex login opens a browser; run it on {machine.name}")
                return
            elif provider == "codex":
                self._run_in_terminal(stdscr, lambda: actions.login_codex(stay=True))
                return
            elif machine.local:
                self._run_in_terminal(stdscr, lambda: actions.login_claude(entry["num"], stay=True))
                return
            else:
                self._run_in_terminal(
                    stdscr, lambda: actions.login_claude_remote(machine, entry["num"], True))
                return
        if state == "active":
            self._say(f"{account['email']} is already active on {machine.name}")
        elif state in ("stored", "pending"):
            self._say(f"switching {machine.name}…")
            rc, message = actions.account_action(machine, provider, "switch", entry["query"])
            self._say(message)
            if rc == 0:
                self.refresh_async(refresh=False)
        elif provider == "claude":  # absent: fresh login there
            actions.copy_to_clipboard(account["email"])
            self._run_in_terminal(stdscr, lambda: actions.add_account(machine))
        else:
            self._say(f"sign in to Codex as {account['email']} on {machine.name} (codex login)")

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

    def _say(self, message: str) -> None:
        with self.lock:
            self.status = message
            self.dirty = True

    def _login(self, stdscr, entry: Entry, stay: bool) -> None:
        if entry.provider == "add":
            self._add_account(stdscr, entry)
        elif entry.provider == "claude" and entry.machine.local:
            actions.copy_to_clipboard(entry.email)
            self._run_in_terminal(stdscr, lambda: actions.login_claude(entry.num, stay=stay))
        elif entry.provider == "claude":
            self._run_in_terminal(
                stdscr, lambda: actions.login_claude_remote(entry.machine, entry.num, stay)
            )
        elif entry.machine.local:
            actions.copy_to_clipboard(entry.email)
            self._run_in_terminal(stdscr, lambda: actions.login_codex(stay=stay))
        else:
            self._say(f"codex login opens a browser; run `codex-auth login` on {entry.machine.name}")

    def _add_account(self, stdscr, entry: Entry) -> None:
        self._run_in_terminal(stdscr, lambda: actions.add_account(entry.machine))

    def _toggle_disabled(self, entry: Entry) -> None:
        if entry.provider != "claude":
            self._say("only Claude accounts can be disabled")
            return
        action = "enable" if entry.disabled else "disable"
        rc, message = actions.account_action(entry.machine, "claude", action, entry.num)
        if rc == 0:
            self.refresh_async(refresh=False)
        self._say(message)

    def _activate(self, stdscr, entry: Entry) -> None:
        if entry.provider == "add":
            self._add_account(stdscr, entry)
            return
        if entry.needs_login:
            self._login(stdscr, entry, stay=True)
            return
        if entry.active:
            self._say(f"{entry.email} is already the active {entry.provider} account on {entry.machine.name}")
            return
        query = entry.num if entry.provider == "claude" else entry.email
        self._say(f"switching on {entry.machine.name}…")
        rc, message = actions.account_action(entry.machine, entry.provider, "switch", query)
        if rc == 0:
            self.refresh_async(refresh=False)
        self._say(message)

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

    def _row_count(self) -> int:
        """Lines the account panel needs."""
        if self.mode == "accounts":
            rows = self.account_rows()
            providers = {a["provider"] for a in rows}
            return max(1, 2 * len(rows) + len(providers))
        rows = 0
        for state, group in zip(self.states, self.groups):
            rows += 1  # machine header
            if state.duplicate_of:
                continue
            rows += len(group)
            rows += bool(state.accounts("claude")) + bool(state.accounts("codex"))  # provider labels
            if not group:
                rows += 1  # "no accounts" / error line
        return rows

    def _machine_header(self, state: remote.MachineState) -> list[tuple[str, int]]:
        m = state.machine
        platform = {"Darwin": "macOS", "Linux": "Linux"}.get(state.platform, state.platform)
        dot_role = "good" if state.online else "danger"
        if (not state.has_data and not state.error) or state.duplicate_of:
            dot_role = "muted"
        segs = [(theme.DOT + " ", theme.attr(dot_role)),
                (m.name.upper(), theme.attr("title", bold=True))]
        details = []
        if m.local:
            details.append("this machine")
        if platform:
            details.append(platform)
        if state.duplicate_of:
            twin = next(("this Mac" if x.machine.local else x.machine.name
                         for x in self.states if x.machine.id == state.duplicate_of), state.duplicate_of)
            details.append(f"same machine as {twin}; accounts shown there")
        elif not state.online and state.has_data:
            details.append(f"offline · last seen {fmt_age(state.fetched_at)} ago")
        elif not state.online:
            details.append("offline")
        elif not state.has_data:
            details.append("connecting…")
        if details:
            segs.append(("  " + " · ".join(details), theme.attr("muted")))
        return segs

    def _draw_account_view(self, stdscr, top, left, width, height):
        """Account-centric panel: usage line + a line of machine chips."""
        self._box(stdscr, top, left, width, height, "Accounts · all machines")
        inner, right = left + 2, left + width - 2
        rows = self.account_rows()
        label_w = min(max((len(a["label"]) for a in rows), default=10), max(12, right - inner - 70))
        y, bottom = top + 1, top + height - 1
        if not rows:
            self._put(stdscr, y, inner, "no accounts yet", theme.attr("muted"))
            return
        ms = self.view["machines"]
        last = None
        for idx, a in enumerate(rows):
            if y >= bottom:
                break
            if a["provider"] != last:
                self._put(stdscr, y, inner, a["provider"].upper(), theme.attr("muted", bold=True))
                y += 1
                last = a["provider"]
            selected = idx == self.sel
            self._put(stdscr, y, inner - 1, theme.ACCENT if selected else " ", theme.attr("accent", bold=True))
            prole = "claude" if a["provider"] == "claude" else "codex"
            name_attr = theme.attr(prole, bold=True) if selected else theme.attr(
                "muted" if a["disabled"] else "text", bold=bool(a["active_on"]))
            label = a["label"] if len(a["label"]) <= label_w else a["label"][: label_w - 1] + "…"
            # No leading "active" marker: the chips below say where it is active.
            segs = [(f"{label:<{label_w}}  ", name_attr)]
            if a["disabled"]:
                segs.append(("disabled  ", theme.attr("muted")))
            if a["needs_login"]:
                segs += [(theme.WARN + " ", theme.attr("danger", bold=True)), ("needs login", theme.attr("danger"))]
            else:
                for raw in a["windows"]:
                    w = remote_parse.render_window(raw)
                    remaining = None if w["pct_used"] is None else 100 - w["pct_used"]
                    bar_str, role = theme.bar(remaining)
                    pct = f"{round(remaining):>3}%" if remaining is not None else "  ?"
                    segs += [(f"{w['id']} ", theme.attr("label")), (bar_str + " ", theme.attr(role)),
                             (pct, theme.attr(role, bold=True)), ("  ", 0)]
                if a["usage_stale"]:
                    segs.append(("(last known)", theme.attr("muted")))
                elif not a["windows"]:
                    segs.append((a["status_message"] or "usage unavailable", theme.attr("muted")))
            self._segs_clip(stdscr, y, inner + 1, right, segs)
            y += 1
            if y >= bottom:
                break
            chips = []
            for ci, m in enumerate(ms):
                entry = a["machines"].get(m["id"], {"state": "offline"})
                state = entry["state"]
                role = {"active": "good", "stored": "text", "pending": "text",
                        "needs_login": "danger"}.get(state, "muted")
                text = f"[{CHIPS[state]} {m['short_name']}{' ⊘' if entry.get('disabled') else ''}]"
                attr = theme.attr(role, bold=state == "active")
                if selected and ci == min(self.chip, len(ms) - 1):
                    attr |= curses.A_REVERSE
                chips += [(text, attr), (" ", 0)]
            self._segs_clip(stdscr, y, inner + 1, right, chips)
            y += 1

    def _draw_accounts(self, stdscr, top, left, width, height):
        """Top-right account switcher, grouped by machine."""
        if self.mode == "accounts":
            self._draw_account_view(stdscr, top, left, width, height)
            return
        self._box(stdscr, top, left, width, height, "Accounts · by machine")
        inner = left + 2
        right = left + width - 2
        avail = right - inner
        email_w = max((len(e.label) for e in self.entries()), default=10)
        email_w = min(email_w, max(12, avail - 86))  # leave room for bars + "resets in" text
        y = top + 1
        flat = 0
        bottom = top + height - 1
        for state, group in zip(self.states, self.groups):
            if y >= bottom:
                break
            self._segs_clip(stdscr, y, inner, right, self._machine_header(state))
            y += 1
            if state.duplicate_of:
                continue
            if not group:
                claude_p = state.provider("claude") or {}
                note = claude_p.get("error") or state.error or (
                    "no data yet" if not state.has_data else "no accounts")
                self._put(stdscr, y, inner + 2, note[: max(0, avail - 2)], theme.attr("muted"))
                y += 1
                continue
            last_provider = None
            for e in group:
                if y >= bottom:
                    break
                if e.provider in ("claude", "codex") and e.provider != last_provider:
                    self._put(stdscr, y, inner + 2, e.provider.upper(), theme.attr("muted", bold=True))
                    y += 1
                    last_provider = e.provider
                    if y >= bottom:
                        break
                self._draw_account_row(stdscr, y, inner + 2, right, e, flat == self.sel, email_w)
                y += 1
                flat += 1

    def _draw_account_row(self, stdscr, y, inner, right, e: Entry, selected, email_w):
        self._put(stdscr, y, inner - 1, theme.ACCENT if selected else " ",
                  theme.attr("accent", bold=True))
        if e.provider == "add":
            role = "accent" if selected else "muted"
            self._put(stdscr, y, inner + 1, e.label + ("  (press enter)" if selected else ""),
                      theme.attr(role, bold=selected))
            return
        prole = "claude" if e.provider == "claude" else "codex"
        offline = not e.state.online
        # Keep colour quiet: only the highlighted row wears its provider tint
        # (Claude orange / Codex blue). Active account is marked by a green dot.
        dot_attr = theme.attr("good") if e.active else theme.attr("muted")
        if selected:
            email_attr = theme.attr(prole, bold=True)
        elif offline or e.disabled:
            email_attr = theme.attr("muted", bold=e.active)
        else:
            email_attr = theme.attr("text", bold=e.active)

        label = e.label if len(e.label) <= email_w else e.label[: email_w - 1] + "…"
        segs = [
            (theme.DOT + " ", dot_attr),
            (f"{e.num:>2}  ", theme.attr("muted")),
            (f"{label:<{email_w}}  ", email_attr),
        ]
        if e.needs_login:
            segs += [(theme.WARN + " ", theme.attr("danger", bold=True)),
                     ("needs login", theme.attr("danger"))]
            self._segs_clip(stdscr, y, inner + 1, right, segs)
            return
        windows = e.windows()
        if not windows:
            segs.append((e.note or "usage unavailable", theme.attr("muted")))
            self._segs_clip(stdscr, y, inner + 1, right, segs)
            return
        for w in windows:
            remaining = None if w.pct is None else 100 - w.pct
            bar_str, role = theme.bar(remaining)
            if offline:
                role = "muted"
            pct = f"{round(remaining):>3}% left" if remaining is not None else "   ? left"
            reset = f" · resets in {w.resets}" if w.resets else ""
            segs += [
                (f"{w.label} ", theme.attr("label")),
                (bar_str + " ", theme.attr(role)),
                (pct, theme.attr(role, bold=True)),
                (reset, theme.attr("muted")),
                ("   ", 0),
            ]
        if e.acct.get("usage_stale"):
            segs.append(("(last known)", theme.attr("muted")))
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
            line = f"{monthly.cost_label(s.last7_cost, s.status):>{col}}{monthly.cost_label(s.last30_cost, s.status):>{col}}"
            for mk in month_keys:
                row = by_month.get(mk)
                if row:
                    month_totals[mk] += row.cost
                line += f"{(monthly.cost_label(row.cost, row.status) if row else '—'):>{col}}"
            self._segs(stdscr, y, left + 2, [
                (f"{name:<{name_w}}", theme.attr("text", bold=True)),
                (line, theme.attr("value"))])
            y += 1

        self._put(stdscr, y, left, theme.HZ * (width - 2), theme.attr("frame"))
        y += 1
        self._put(stdscr, y, left, theme.DOTTED, theme.attr("accent"))
        total_status = monthly.combine_status([s.status for s in self.stats.values() if s])
        tline = f"{monthly.cost_label(totals['7d'], total_status):>{col}}{monthly.cost_label(totals['30d'], total_status):>{col}}"
        for mk in month_keys:
            tline += f"{monthly.cost_label(month_totals[mk], total_status):>{col}}"
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
        acc_rows = self._row_count()
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
        shape = (self._row_count(), bool(self.stats), stdscr.getmaxyx())
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
            elif ch == ord("m"):
                self.mode = "machines" if self.mode == "accounts" else "accounts"
                self.sel = 0
                self.dirty = True
            elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT) and self.mode == "accounts":
                count = max(1, len(self.view["machines"]))
                self.chip = (self.chip + (1 if ch == curses.KEY_RIGHT else -1)) % count
                self.dirty = True
            elif entries and self.mode == "accounts" and ch in (curses.KEY_ENTER, 10, 13, ord("l"), ord("d"), ord("a")):
                kind = {ord("l"): "login", ord("d"): "disable"}.get(ch, "enter")
                self._chip_action(stdscr, entries[min(self.sel, len(entries) - 1)], kind)
            elif entries and ch in (curses.KEY_ENTER, 10, 13):
                self._activate(stdscr, entries[min(self.sel, len(entries) - 1)])
            elif entries and ch == ord("l"):
                self._login(stdscr, entries[min(self.sel, len(entries) - 1)], stay=False)
            elif entries and ch == ord("d"):
                self._toggle_disabled(entries[min(self.sel, len(entries) - 1)])
            elif entries and ch == ord("a"):
                self._add_account(stdscr, entries[min(self.sel, len(entries) - 1)])


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
