"""Native history for any linked foreign row: Codex, Claude Code or Mistral Vibe.

A linked row is a locator, not a transcript authority. Each canonical read (``session.resume``,
``session.history``, ``session.transcript``) resolves the row to the native store that owns its
turns and answers those turns; the local mirror is empty by design, so falling back to it answers
an empty transcript. Codex speaks the app-server protocol, Claude and Vibe are read off their
on-disk stores — one resolution, three readers, one profile scope.
"""

from __future__ import annotations

from persistence.core.errors import AuthorityRefusal


def linked_native_target(store, session_id: str) -> tuple[str, str] | None:
    """``(kind, native_id)`` for a linked row, ``None`` for an ordinary Hermes session.

    An unusable pointer configuration becomes the typed ``storage_unavailable`` refusal the Codex
    reader already raised: answering "not linked" would detach the row from its native thread.
    """
    from hermes_cli.foreign_linked_store import linked_foreign_target

    try:
        return linked_foreign_target(store, session_id)
    except ValueError as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                               detail="linked native thread identity is invalid") from exc


def _read_display(store, session_id: str, target: tuple[str, str], *, limit: int | None,
                  offset: int, latest: bool) -> list[dict]:
    kind, native_id = target
    row = store.sessions.get(session_id)
    if kind == "codex":
        from gateway.session_linked_codex import read_display_history
        return read_display_history(
            native_id, session_id=session_id, model=row.model if row is not None else None,
            limit=limit, offset=offset, latest=latest, profile_home=store.scope.profile_dir,
            session_db=store, platform=row.source if row is not None else "")
    rows = _read_store_rows(kind, native_id, store)
    if latest:
        end = len(rows) - offset
        start = max(0, end - limit) if limit is not None else 0
        return rows[start:max(0, end)]
    return rows[offset:] if limit is None else rows[offset:offset + limit]


def _read_store_rows(kind: str, native_id: str, store) -> list[dict]:
    """The read-oriented kinds' whole native transcript, failing closed on an unreadable store."""
    from hermes_cli.foreign_linked_store import read_linked_foreign_display

    try:
        return read_linked_foreign_display(kind, native_id, profile_home=store.scope.profile_dir)
    except (OSError, TimeoutError, ValueError) as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                               detail=f"linked {kind} history is unavailable") from exc


def linked_display_history(authority, session_id: str, *, limit: int | None = None,
                           offset: int = 0, latest: bool = False) -> list[dict] | None:
    """Native display rows for a linked row, or ``None`` when the row is an ordinary session.

    ``None`` (rather than an empty list) is what tells a caller to read its own transcript.
    Runs under the OWNING profile's scope, like every read on the RPC dispatch path: one process
    serves many profiles, so the native store is resolved for this authority's home, never the
    launch profile's ambient one.
    """
    from gateway.session_authorities import owner_scope
    from gateway.session_persistence import authority_persistence

    store = authority_persistence(authority)
    target = linked_native_target(store, session_id)
    if target is None:
        return None
    with owner_scope(authority):
        return _read_display(store, session_id, target, limit=limit, offset=offset, latest=latest)


def linked_transcript_page(connection, session_id: str, target: tuple[str, str], *,
                           cursor, limit: int, order: str, inline_images: bool,
                           owner_session_id: str) -> dict:
    """One transcript page of a linked native history, in ``session.transcript``'s wire shape.

    Both kinds page through ``transcript_page`` so one digest cursor bounds the whole conversation:
    Codex reads its app-server page, Claude and Vibe hand over the rows their own stores hold.
    """
    from gateway.session_authorities import owner_scope
    from gateway.session_persistence import authority_persistence
    from gateway.session_linked_codex import transcript_page

    kind, native_id = target
    store = authority_persistence(connection.authority)
    with owner_scope(connection.authority):
        history = None if kind == "codex" else _read_store_rows(kind, native_id, store)
        row = store.sessions.get(owner_session_id)
        return transcript_page(connection, session_id, native_id, cursor=cursor, limit=limit,
                               order=order, inline_images=inline_images, history=history,
                               output_model=row.model if row is not None else None,
                               owner_session_id=owner_session_id)
