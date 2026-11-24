"""Whether a Hermes send may be admitted onto a linked foreign pointer row.

A linked session (``model_config`` flag ``codex_linked_thread`` / ``claude_linked_thread`` /
``mistral_linked_thread``) stores only a pointer: the native tool owns the transcript. Sending a
turn into one therefore means APPENDING to that native thread (Codex through app-server
``turn/start`` with ``resume_thread_id``; Claude Code / Vibe through a headless ``--resume`` run),
never starting a second Hermes-owned conversation that would fork the thread.

The wire contract keeps that decision in ONE place so the refuse path
(``tui_gateway/methods_prompt.py::prompt.submit``) and the capability path
(``session.foreign.status`` → :mod:`hermes_cli.foreign_host_capability`) can never disagree about
whether a send is possible on this host: a client that was told "read-only" must not be able to
send, and a client that was told "writable" must not be refused as read-only.

:data:`LINKED_SEND_ADMITTED` is the switch that had to agree with the turn path: a name may only be
listed here once a resume adapter actually APPENDS a turn for that agent, because admitting it
enables the send and the probe's ``continue_status.writable`` at the same time. All three agents
are listed: Codex appends through app-server ``turn/start`` + ``resume_thread_id``, Claude Code and
Mistral Vibe through their ``--resume`` transports, and
:mod:`agent.linked_foreign_turn` is the turn route that can never fall back to a generic LLM turn
on one of their rows. An agent missing from this set is still refused with the same reason the
probe reports (:data:`~hermes_cli.foreign_host_capability.REASON_SEND_READ_ONLY`).
"""

from __future__ import annotations

#: Agents whose linked rows a send may be admitted to. Adding a name here enables BOTH the send and
#: the probe's ``continue_status.writable``, because both read :func:`linked_send_refusal` — so an
#: entry is only correct when the turn path owns an append for that agent.
LINKED_SEND_ADMITTED: frozenset[str] = frozenset({"codex", "claude", "mistral"})

AGENT_LABELS = {"codex": "Codex", "claude": "Claude", "mistral": "Mistral"}


def agent_label(agent: str) -> str:
    """Display label for a linked agent id (``foreign`` when the flag names no known agent)."""
    return AGENT_LABELS.get(agent, agent)


def linked_send_refusal(agent: str) -> str | None:
    """Refusal text for a send onto a linked foreign row of *agent*, or ``None`` when admitted.

    The text is user-facing (Desktop renders it verbatim under the composer), so it names the
    native tool instead of the flag and points at where the turn can be written today.
    """
    if agent in LINKED_SEND_ADMITTED:
        return None
    label = agent_label(agent)
    return (f"linked foreign sessions are read-only — this is a linked {label} session; "
            f"send new messages in {label} instead")
