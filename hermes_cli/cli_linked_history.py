"""Native display history for a linked Codex pointer on the classic CLI's late resume path.

``cli_agent_setup_mixin._load_resumed_history_late`` is the branch ``hermes chat -q --resume``
takes: ``run()`` (and so ``_preload_resumed_session``) never executes for a single query, so the
recap history has to be loaded here. An ordinary session reads the tip's stored rows
(``get_messages_as_conversation``); a linked Codex pointer has no stored rows at all — its
transcript lives in the native thread — so reading the mirror is what produced
"found but has no messages. Starting fresh." on a linked session.

A pointer is display-only. Its native rows go to the recap (``_resume_display_history``) and are
never placed in ``conversation_history``: that list is the live-replay transcript the agent sends
to the model and flushes back to the store, and a pointer's row must keep its own empty mirror
(``agent/linked_foreign_turn.py`` routes the turn on the ROW FLAG, never on this list). Keeping the
two apart is what stops a native recap from being replayed as Hermes-owned history.

Codex being unavailable is a normal state (it may be running elsewhere, or not at all), so a
failed native read answers one honest notice row instead of raising: the caller opens the session
either way.
"""

from __future__ import annotations

#: ``display_kind`` of the single notice row a failed native read yields. The recap renderer
#: prints it as one dim event line (``_collect_resume_entries``).
UNAVAILABLE_DISPLAY_KIND = "codex_history_unavailable"


def linked_display_rows(store, session_id: str) -> list[dict] | None:
    """Recap rows for a linked Codex pointer, or ``None`` when *session_id* is an ordinary session.

    ``None`` is the load-bearing answer: the caller must then keep its ordinary store read. The
    store owns the pointer test (``linked_thread_id`` on the physical row) and the paging, so this
    module never re-derives the link marker or reads Codex itself — ``GatewaySessionStore`` opens
    no app-server process for it, it asks the owner that already has the client open.
    """
    history = getattr(store, "linked_history", None)
    if not callable(history):
        return None
    try:
        window = history(session_id, refresh=True)
    except Exception as exc:  # health: allow BLE001 -- a history read must never block resume
        # A refusal, a dropped gateway or a bad page is reported as one line, never a traceback:
        # the session still opens and the next turn reports any real connection problem itself.
        return [{"role": "system", "display_kind": UNAVAILABLE_DISPLAY_KIND,
                 "content": f"Linked Codex history could not be loaded: {exc}"}]
    if window is None:
        return None
    from hermes_cli.linked_history.pages import conversation_rows
    # The recap reads ``content`` (``_collect_resume_entries``), while canonical rows carry
    # ``text``; ``conversation_rows`` is the same mapping ``get_resume_conversations`` uses, so
    # both resume branches feed the panel one row shape.
    return conversation_rows(list(window.rows))


def unavailable_notice(rows) -> str | None:
    """The honest one-liner a failed native read carries, or ``None`` when the read succeeded."""
    for row in rows or ():
        if isinstance(row, dict) and row.get("display_kind") == UNAVAILABLE_DISPLAY_KIND:
            content = row.get("content")
            return content if isinstance(content, str) and content else (
                "Linked Codex history could not be loaded.")
    return None
