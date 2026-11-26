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


def handle_link_thread_command(cli, cmd_original: str) -> None:
    """Handle /link-thread [agent] [thread-id] — link a native Codex/Claude/Vibe thread and
    continue it here.

    The classic CLI is a thin client of the gateway owner, so linking goes through the same
    ``session.<agent>.link`` RPC the TUI uses: the pointer row is created by the process that
    owns the store, never written from here (``GatewaySessionStore`` refuses writes by design).
    A bare ``/link-thread`` browses: linking is a write, so it stays a second step.
    """
    if getattr(cli, "_agent_running", False):
        return _cp(f"  {_t('shared.agent_busy', command='/link-thread')}")
    agent, thread_id = parse_link_arg(_command_arg(cmd_original))
    if not thread_id:
        return _show_linked_thread_picker(cli, agent)
    # `/link-thread 2` right after a bare `/link-thread` selects that row: the picker already
    # named its agent, so asking again would be ceremony. Native ids are UUIDs, never bare
    # integers, so a number here is unambiguously an index.
    if _pending_pick_armed(cli, thread_id):
        picked = _take_pending_pick(cli, thread_id)
        if picked is None:
            return _cp("  " + _t("link_thread.pick_out_of_range", index=thread_id))
        agent, thread_id = picked["agent"], picked["id"]
    store = getattr(cli, "_session_db", None)
    call = getattr(store, "call", None)
    if not callable(call):
        return _cp(f"  {_t('link_thread.owner_unavailable')}")
    states = linked_agent_states(call)
    if states is None:
        return _cp(f"  {_t('link_thread.status_unavailable')}")
    if not agent:
        writable = [state for state in states if state["writable"]]
        if not writable:
            return _cp("  " + _t("link_thread.no_agent", reasons="; ".join(
                f"{state['label']}: {state['reason']}" for state in states)))
        # One writable agent needs no disambiguation: the id can only belong to it, and a wrong
        # id fails on the link itself. With several, the stores are asked rather than the user —
        # a unique match is a resolution, anything else stays a question, because a pointer row
        # on the wrong agent would fail on the next send instead of here.
        agent = writable[0]["agent"]
        if len(writable) > 1:
            agent = _agent_owning_thread(call, writable, thread_id)
            if agent is None:
                return _cp("  " + _t("link_thread.name_the_agent", thread_id=thread_id,
                                      agents=", ".join(state["agent"] for state in writable)))
    else:
        state = next((row for row in states if row["agent"] == agent), None)
        if not state or not state["writable"]:
            return _cp("  " + _t("link_thread.unavailable", label=_LINK_AGENT_LABELS[agent],
                                 reason=(state or {}).get("reason") or _t("link_thread.not_reported")))
    try:
        created = call(link_method(agent), **link_params(agent, thread_id))
    except Exception as exc:  # health: allow BLE001 -- a gateway refusal is reported, never retried blind
        return _cp(f"  {_t('link_thread.failed', agent=_LINK_AGENT_LABELS[agent], error=exc)}")
    session_id = (created or {}).get("session_id")
    if not session_id:
        return _cp("  " + _t("link_thread.invalid_response", method=link_method(agent)))
    if (created or {}).get("already_linked"):
        _cp("  " + _t("link_thread.already_linked", label=_LINK_AGENT_LABELS[agent], thread_id=thread_id))
    cli._handle_resume_command(f"/resume {session_id}")


def _pending_pick_armed(cli, thread_id: str) -> bool:
    """Whether *thread_id* is a picker index at all: a bare number with a pending list armed."""
    return bool(getattr(cli, "_pending_link_threads", None)) and thread_id.isdigit()


def _take_pending_pick(cli, thread_id: str) -> dict | None:
    """The row a bare ``/link-thread <number>`` selects, consuming the one-shot pending list.

    Returns ``None`` when the number is outside the list, so the caller can report it as an
    out-of-range pick rather than treating it as a thread id.
    """
    pending = getattr(cli, "_pending_link_threads", None)
    if not pending or not thread_id.isdigit():
        return None
    index = int(thread_id)
    if not 1 <= index <= len(pending):
        return None
    cli._pending_link_threads = None
    return pending[index - 1]


def _agent_owning_thread(call, writable: list[dict], thread_id: str) -> str | None:
    """The one writable agent whose store lists *thread_id*, or ``None`` when that is not unique.

    One page per agent: a thread old enough to fall off the first page stays a question rather
    than a guess, because a pointer row on the wrong agent fails on the next send.
    """
    found: list[str] = []
    for state in writable:
        try:
            page = call(f"session.{state['agent']}.list", limit=25) or {}
        except Exception:  # health: allow BLE001 -- an unreadable store cannot own the id
            continue
        if any(str(row.get("id")) == thread_id for row in (page.get("threads") or [])):
            found.append(state["agent"])
    return found[0] if len(found) == 1 else None


def _show_linked_thread_picker(cli, agent: str | None = None) -> None:
    """List what this host can link, and the native threads it can offer, then one-shot select.

    Mirrors the TUI overlay's browse step without rendering it: the capability answer decides
    which agents are listed at all, so an unavailable agent is one line and never a row that
    would fail on submit.
    """
    store = getattr(cli, "_session_db", None)
    call = getattr(store, "call", None)
    if not callable(call):
        return _cp(f"  {_t('link_thread.owner_unavailable')}")
    states = linked_agent_states(call)
    if states is None:
        return _cp(f"  {_t('link_thread.status_unavailable')}")
    usable = [state for state in states if state["writable"] and (not agent or state["agent"] == agent)]
    if not usable:
        return _cp("  " + _t("link_thread.no_agent", reasons="; ".join(
            f"{state['label']}: {state['reason']}" for state in states)))
    rows: list[dict] = []
    for state in usable:
        try:
            page = call(f"session.{state['agent']}.list", limit=10) or {}
        except Exception as exc:  # health: allow BLE001 -- one agent's store failing must not hide the others
            _cp("  " + _t("link_thread.list_failed", label=state["label"], error=exc))
            continue
        for row in (page.get("threads") or [])[:10]:
            rows.append({"agent": state["agent"], "label": state["label"], "id": row.get("id"),
                         "title": row.get("title") or ""})
    if not rows:
        return _cp(f"  {_t('link_thread.none_found')}")
    _cp(f"  {_t('link_thread.list_header')}")
    for index, row in enumerate(rows, 1):
        _cp(f"  {index:>2}. [{row['label']}] {row['title'][:72]}  {row['id']}")
    cli._pending_link_threads = rows
    _cp(f"  {_t('link_thread.pick_hint')}")