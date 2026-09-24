"""Delete an account everywhere (`ccc delete`, the app's "Delete account…").

With shared-login sync (Claude): the hub drops its copy and keeps a
tombstone (sync_hub.HubStore.delete). Machines whose agent is 0.22 or newer
remove their own copies; this machine applies the deletion at once. Machines
without such an agent get the per-machine remove (`cswap remove` over ssh,
or CCC's own engine), the same action as "Remove from this machine".

The live Claude Code login is never touched: it keeps working on a machine
where it is active until Claude Code switches away (an agent then removes
the slot; a per-machine remove only removes the stored copy anyway).
Nothing here prints a token.
"""

from __future__ import annotations

from . import actions, machines

AGENT_DELETES = (0, 22)  # agents from this version remove deleted logins themselves


def _version(text: str | None) -> tuple[int, ...]:
    parts = []
    for piece in str(text or "").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def report_name(machine: machines.Machine, sync: dict) -> str:
    """The name a machine's agent reports to the hub under (as in snapshot)."""
    if machine.local:
        return sync.get("machine") or machine.id
    return machine.alias or machine.id


def plan(account: dict, fleet: list[machines.Machine], sync: dict | None) -> dict:
    """What `delete` will do, machine by machine (no side effects).

    Steps: "here" (this machine, applied now from the hub's tombstone),
    "agent" (that machine's 0.22+ agent removes it), "remove" (per-machine
    remove now), "offline" (unreachable: its copy stays), "unsupported".
    """
    provider = account["provider"]
    hub = bool(sync and sync.get("enabled")) and provider == "claude"
    reports = (sync or {}).get("machines") or {}
    steps = []
    for mid in account.get("stored_on") or []:
        machine = machines.find(fleet, mid)
        if machine is None:
            continue
        entry = (account.get("machines") or {}).get(mid) or {}
        step = {"machine": mid, "name": machine.name, "active": mid in (account.get("active_on") or []),
                "query": entry.get("query")}
        report = reports.get(report_name(machine, sync or {}))
        if hub and machine.local:
            step["how"] = "here"
        elif hub and report is not None and _version(report.get("version")) >= AGENT_DELETES:
            step["how"] = "agent"
        elif entry.get("state") == "offline":
            step["how"] = "offline"
        elif not step["query"]:
            step["how"] = "unsupported"
            step["reason"] = "no stored copy to remove"
        elif provider == "codex" and not machine.local and machine.resolved_engine != "ccc":
            step["how"] = "unsupported"
            step["reason"] = "remove it there with codex-auth remove"
        else:
            step["how"] = "remove"
        steps.append(step)
    return {"key": account["key"], "provider": provider, "email": account["email"], "hub": hub,
            "steps": steps}


def describe(the_plan: dict) -> str:
    """The confirmation text (the app shows the same facts)."""
    lines = [f"Delete {the_plan['provider'].capitalize()} account {the_plan['email']} everywhere?"]
    active = [s["name"] for s in the_plan["steps"] if s["active"]]
    if active:
        tool = "Claude Code" if the_plan["provider"] == "claude" else "Codex"
        text = f"{tool} on {_join(active)} {'is' if len(active) == 1 else 'are'} using this account; " \
               "it keeps working until it switches"
        lines.append(text + (", but CCC stops syncing it." if the_plan["hub"] else "."))
    if the_plan["hub"]:
        lines.append("The hub forgets the login and refuses it from every machine until you log in to "
                     "it again with `ccc sync login --email " + the_plan["email"] + "`.")
    for step in the_plan["steps"]:
        lines.append(f"  {step['name']}: " + {
            "here": "removed now",
            "agent": "its sync agent removes it",
            "remove": "removed now (" + ("CCC" if step["machine"] == machines.LOCAL_ID else "over ssh") + ")",
            "offline": "offline, its copy stays",
            "unsupported": step.get("reason", "cannot remove it from here"),
        }[step["how"]] + (" (the live login stays)" if step["active"] and step["how"] != "offline" else ""))
    return "\n".join(lines)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def execute(the_plan: dict, fleet: list[machines.Machine], client=None, out=print,
            account_action=actions.account_action) -> int:
    """Run a confirmed plan. Returns 0 when every reachable copy is handled."""
    from . import sync_agent

    rc = 0
    if the_plan["hub"]:
        try:
            client.delete(the_plan["key"])
        except sync_agent.HubError as exc:
            # Stop here: while the hub keeps the login, agents would restore
            # every copy removed below.
            hint = " (the hub must run CCC 0.22 or newer)" if "404" in str(exc) else ""
            out(f"Could not delete on the hub: {exc}{hint}. Nothing was changed.")
            return 1
        out(f"Deleted {the_plan['email']} on the hub.")
    for step in the_plan["steps"]:
        how, name = step["how"], step["name"]
        if how == "here":
            try:
                summary = sync_agent.apply_hub_deletions(client)
            except sync_agent.HubError as exc:
                out(f"{name}: {exc}; the sync agent removes it on its next round")
                continue
            if the_plan["key"] in summary["kept_active"]:
                out(f"{name}: kept while Claude Code uses it; removed after it switches")
            elif summary["errors"]:
                rc = 1
                out(f"{name}: " + "; ".join(summary["errors"]))
            else:
                out(f"{name}: removed")
        elif how == "agent":
            out(f"{name}: its sync agent removes it"
                + (" after Claude Code switches away" if step["active"] else ""))
        elif how == "remove":
            machine = machines.find(fleet, step["machine"])
            code, message = account_action(machine, the_plan["provider"], "remove", step["query"])
            out(f"{name}: {message}" if not message.startswith(name) else message)
            rc = rc or code
        elif how == "offline":
            out(f"{name}: offline, its copy stays (later: ccc remove {step['query'] or the_plan['email']} "
                f"--provider {the_plan['provider']} --machine {step['machine']})")
        else:
            out(f"{name}: {step.get('reason', 'not removed')}")
            rc = rc or 1
    return rc
