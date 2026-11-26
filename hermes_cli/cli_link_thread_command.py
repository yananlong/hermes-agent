"""``/link-thread`` command body for the classic CLI, kept out of the mixin module's budget.

The mixin is already over its file-length cap, so each new command lives in its own sibling here
and the mixin exposes a thin method that delegates. The behaviour is the TUI's, restated in
Python: ask the serving host what it can do, link through the process that owns the store, then
resume the pointer row.

``cli`` is the ``HermesCLI`` instance (these take it explicitly rather than a mixin ``self`` so the
module has no import cycle with ``cli_commands_mixin``).
"""

from __future__ import annotations

from hermes_cli.cli_commands_session_tools import _command_arg, _cp, _t
from hermes_cli.cli_linked_threads import (
    _LINK_AGENT_LABELS, link_method, link_params, linked_agent_states, parse_link_arg)

#: How many native threads one agent contributes to a browse list / an id lookup.
_PAGE = 10
_LOOKUP_PAGE = 25


def handle_link_thread_command(cli, cmd_original: str) -> None:
    """Handle /link-thread [agent] [thread-id] — link a native Codex/Claude/Vibe thread and
    continue it here.

    The classic CLI is a thin client of the gateway owner, so linking goes through the same
    ``session.<agent>.link`` RPC the TUI uses: the pointer row is created by the process that
    owns the store, never written from here (``GatewaySessionStore`` refuses writes by design).

    A bare ``/link-thread`` browses, and a bare NUMBER always means "that row of the list" —
    whether or not the list was printed in this session, because a native thread id is a UUID
    and never a bare integer. Linking is still a second step: the number selects, it does not
    guess a store.
    """
    if getattr(cli, "_agent_running", False):
        return _cp(f"  {_t('shared.agent_busy', command='/link-thread')}")
    agent, target = parse_link_arg(_command_arg(cmd_original))
    store = getattr(cli, "_session_db", None)
    call = getattr(store, "call", None)
    if not callable(call):
        return _cp(f"  {_t('link_thread.owner_unavailable')}")
    states = linked_agent_states(call)
    if states is None:
        return _cp(f"  {_t('link_thread.status_unavailable')}")

    if not target:
        return _browse(cli, call, states, agent)

    if target.isdigit():
        row = _row_at(cli, call, states, agent, int(target))
        if row is None:
            return
        agent, target = row["agent"], row["id"]
    else:
        agent = _agent_for(call, states, agent, target)
        if agent is None:
            return

    try:
        created = call(link_method(agent), **link_params(agent, target))
    except Exception as exc:  # health: allow BLE001 -- a gateway refusal is reported, never retried blind
        return _cp(f"  {_t('link_thread.failed', agent=_LINK_AGENT_LABELS[agent], error=exc)}")
    session_id = (created or {}).get("session_id")
    if not session_id:
        return _cp("  " + _t("link_thread.invalid_response", method=link_method(agent)))
    if (created or {}).get("already_linked"):
        _cp("  " + _t("link_thread.already_linked", label=_LINK_AGENT_LABELS[agent], thread_id=target))
    cli._handle_resume_command(f"/resume {session_id}")


def _agent_for(call, states: list[dict], agent: str | None, thread_id: str) -> str | None:
    """The agent to link *thread_id* through, or ``None`` after printing why it is not settled."""
    if agent:
        state = next((row for row in states if row["agent"] == agent), None)
        if not state or not state["writable"]:
            _cp("  " + _t("link_thread.unavailable", label=_LINK_AGENT_LABELS[agent],
                          reason=(state or {}).get("reason") or _t("link_thread.not_reported")))
            return None
        return agent
    writable = [state for state in states if state["writable"]]
    if not writable:
        _cp("  " + _t("link_thread.no_agent", reasons="; ".join(
            f"{state['label']}: {state['reason']}" for state in states)))
        return None
    if len(writable) == 1:
        # One candidate needs no disambiguation: the id can only belong to it, and a wrong id
        # fails on the link itself rather than silently landing in the wrong store.
        return writable[0]["agent"]
    # Several candidates: ask the stores instead of the user. A unique match is a resolution;
    # anything else stays a question, because a pointer row on the wrong agent fails later.
    owner = _agent_owning_thread(call, writable, thread_id)
    if owner is None:
        _cp("  " + _t("link_thread.name_the_agent", thread_id=thread_id,
                      agents=", ".join(state["agent"] for state in writable)))
    return owner


def _agent_owning_thread(call, writable: list[dict], thread_id: str) -> str | None:
    """The one writable agent whose store lists *thread_id*, or ``None`` when that is not unique."""
    found: list[str] = []
    for state in writable:
        try:
            page = call(f"session.{state['agent']}.list", limit=_LOOKUP_PAGE) or {}
        except Exception:  # health: allow BLE001 -- an unreadable store cannot own the id
            continue
        if any(str(row.get("id")) == thread_id for row in (page.get("threads") or [])):
            found.append(state["agent"])
    return found[0] if len(found) == 1 else None


def _row_at(cli, call, states: list[dict], agent: str | None, index: int) -> dict | None:
    """The list row *index* selects, printing what it refers to.

    Uses the armed picker when there is one — that is exactly the list the user just saw — and
    otherwise fetches the same list now, so a first-command ``/link 2`` behaves like
    ``/link`` followed by ``2``.
    """
    rows = getattr(cli, "_pending_link_threads", None)
    cli._pending_link_threads = None
    if not rows:
        usable = _usable(states, agent)
        if not usable:
            return _refuse_no_agent(states)
        rows = _thread_rows(call, usable)
        if not rows:
            _cp(f"  {_t('link_thread.none_found')}")
            return None
        _report(rows)
    if not 1 <= index <= len(rows):
        _cp("  " + _t("link_thread.pick_out_of_range", index=index))
        _cp("  " + _t("link_thread.pick_hint"))
        return None
    return rows[index - 1]


def _browse(cli, call, states: list[dict], agent: str | None) -> None:
    """Print what this host can link, and the native threads it can offer, then arm the picker."""
    usable = _usable(states, agent)
    if not usable:
        return _refuse_no_agent(states)
    rows = _thread_rows(call, usable)
    if not rows:
        return _cp(f"  {_t('link_thread.none_found')}")
    _report(rows)
    cli._pending_link_threads = rows
    _cp(f"  {_t('link_thread.pick_hint')}")


def _usable(states: list[dict], agent: str | None) -> list[dict]:
    """The agents this host can actually continue, narrowed to *agent* when one was named."""
    return [state for state in states
            if state["writable"] and (not agent or state["agent"] == agent)]


def _refuse_no_agent(states: list[dict]) -> None:
    _cp("  " + _t("link_thread.no_agent", reasons="; ".join(
        f"{state['label']}: {state['reason']}" for state in states)))


def _thread_rows(call, usable: list[dict]) -> list[dict]:
    """One row per native thread across *usable* agents; an unreadable store is one line."""
    rows: list[dict] = []
    for state in usable:
        try:
            page = call(f"session.{state['agent']}.list", limit=_PAGE) or {}
        except Exception as exc:  # health: allow BLE001 -- one agent's store must not hide the rest
            _cp("  " + _t("link_thread.list_failed", label=state["label"], error=exc))
            continue
        for row in (page.get("threads") or [])[:_PAGE]:
            rows.append({"agent": state["agent"], "label": state["label"],
                         "id": row.get("id"), "title": row.get("title") or ""})
    return rows


def _report(rows: list[dict]) -> None:
    _cp(f"  {_t('link_thread.list_header')}")
    for index, row in enumerate(rows, 1):
        _cp(f"  {index:>2}. [{row['label']}] {row['title'][:72]}  {row['id']}")
