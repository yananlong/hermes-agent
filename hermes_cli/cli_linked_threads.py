"""Client-side vocabulary for ``/link-thread`` in the classic CLI.

The TUI's ``ui-tui/src/app/slash/linkedThreads.ts`` and this module answer the same three
questions — what the argument means, which agent owns a thread id, and what the host's
capability answer allows — so the two surfaces cannot disagree about a method name or about
when an agent counts as linkable. Every rule here is the Python twin of that file:

* :func:`parse_link_arg` mirrors ``parseLinkArg``
* :func:`link_method` / :func:`link_params` mirror ``linkMethod`` / ``linkParams``
* :func:`linked_agent_states` mirrors ``agentLinkStates``

Only ``continue_status.writable is True`` counts as usable. A missing answer, a failed probe,
or a gateway that names no row for an agent is UNAVAILABLE — never optimistically writable —
because linking a thread the host then refuses to continue fails on the next submit instead of
here, which is exactly what the reason line prevents.
"""

from __future__ import annotations

from typing import Any, Callable

#: Native tools a Hermes row can be linked to, in the order every linked surface lists them.
LINK_AGENTS: tuple[str, ...] = ("codex", "claude", "mistral")

#: Product names, matching the ``label`` the gateway sends so a client can answer before it does.
_LINK_AGENT_LABELS: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "mistral": "Mistral Vibe",
}


def _label(agent: str) -> str:
    return _LINK_AGENT_LABELS[agent]


def parse_link_arg(arg: str) -> tuple[str | None, str]:
    """``(agent, thread_id)`` from a ``/link-thread`` argument.

    ``/link-thread codex 0a1b`` names both; ``/link-thread 0a1b`` names only a thread, and which
    agent owns it is a capability question the caller resolves (never a guess here). A bare
    ``/link-thread`` or ``/link-thread codex`` carries no thread id: they browse.
    """
    parts = [part for part in arg.strip().split() if part]
    first = parts[0] if parts else ""
    second = parts[1] if len(parts) > 1 else ""
    lowered = first.lower()
    if lowered in LINK_AGENTS:
        return lowered, second
    return None, first


def link_method(agent: str) -> str:
    """``session.codex.link`` / ``session.claude.link`` / ``session.mistral.link``."""
    return f"session.{agent}.link"


def link_params(agent: str, thread_id: str) -> dict[str, str]:
    """The id parameter each link RPC documents.

    Codex threads travel as ``thread_id``; the Claude/Vibe siblings as ``session_id`` (their
    handlers accept ``thread_id`` as an alias, Codex does not).
    """
    return {"thread_id": thread_id} if agent == "codex" else {"session_id": thread_id}


def _unavailable_reason(row: dict | None, label: str) -> str:
    if not row:
        return f"This gateway did not report {label} capability."
    for key in ("continue_status", "store", "cli"):
        section = row.get(key)
        if isinstance(section, dict) and section.get("reason"):
            return str(section["reason"])
    return f"This host cannot continue {label} threads."


def linked_agent_states(call: Callable[..., Any]) -> list[dict] | None:
    """One row per agent from ``session.foreign.status``, or ``None`` when the host cannot answer.

    ``None`` is the "this backend is older than the feature" verdict, which the caller reports as
    a stale owner rather than as three unexplained unavailable agents — the distinction a user
    needs, because the fix (restart the owner from the checkout under test) is different.
    """
    try:
        status = call("session.foreign.status", probe_version=False) or {}
    except Exception:  # health: allow BLE001 -- an owner that cannot answer is reported as such
        return None
    agents = status.get("agents")
    if not isinstance(agents, list):
        return None
    rows = [row for row in agents if isinstance(row, dict)]
    states: list[dict] = []
    for agent in LINK_AGENTS:
        label = _label(agent)
        row = next((candidate for candidate in rows if candidate.get("agent") == agent), None)
        writable = isinstance(row, dict) and (row.get("continue_status") or {}).get("writable") is True
        states.append({"agent": agent, "label": row.get("label") if isinstance(row, dict) else label,
                       "reason": "" if writable else _unavailable_reason(row, label),
                       "writable": writable})
    return states
