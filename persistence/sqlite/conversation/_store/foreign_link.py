"""Atomic creation of a pointer row for a foreign CLI-owned session (Claude Code, Mistral Vibe)."""

from typing import Any

from persistence.conversation.sessions import CANONICAL_BOT_CHAT_TITLE, NewSession, TitleSource, sanitize_title
from persistence.core.errors import AuthorityRefusal
from persistence.core.identity import SessionId

from ..._schema_ddl import _sql_json_extract
from .retirement import create_conflict
from .worker_lifecycle import insert_session_row_in_transaction


def link_foreign_session(holder, spec: NewSession, title: str, *, flag: str, id_key: str,
                         native_id: str, fallback_title: str) -> tuple[SessionId, bool]:
    """Return the existing pointer or create one under the profile's SQLite writer lock.

    ``flag``/``id_key`` are the ``model_config`` pair marking the pointer (e.g.
    ``claude_linked_thread``/``claude_session_id``); ``native_id`` is the foreign tool's own
    session identifier.
    """
    title = sanitize_title(title) or fallback_title
    if title == CANONICAL_BOT_CHAT_TITLE:
        title = f"{fallback_title}: Bot Chat"

    def write(conn):
        existing_id = _sql_json_extract("model_config", f"$.{id_key}")
        valid_json = "(CASE WHEN json_valid(model_config) THEN model_config ELSE json_object() END)"
        existing = conn.execute(
            f"SELECT id FROM sessions WHERE json_type({valid_json}, '$.{flag}') "
            f"= 'true' AND {existing_id} = ? ORDER BY started_at, id LIMIT 1",
            (native_id,),
        ).fetchone()
        if existing is not None:
            return SessionId(existing["id"]), True
        if create_conflict(conn, spec.session_id):
            raise AuthorityRefusal("revision_conflict", field="session_id")
        label = title
        if conn.execute("SELECT 1 FROM sessions WHERE title=?", (title,)).fetchone():
            label = f"{title[:180]} ({spec.session_id[-12:]})"
        config = dict(spec.model_config or {})
        config.update({flag: True, id_key: native_id})
        insert_session_row_in_transaction(
            holder, conn, spec.session_id, spec.source, model=spec.model, model_config=config,
            cwd=spec.cwd, profile_name=spec.profile_name,
        )
        conn.execute("UPDATE sessions SET title=?, title_source=? WHERE id=?",
                     (label, TitleSource.DERIVED.value, spec.session_id))
        return spec.session_id, False

    return holder._execute_write(write)


def linked_foreign_config_flags() -> tuple[str, ...]:
    """The ``model_config`` boolean flags that mark any foreign-owned transcript pointer."""
    return ("codex_linked_thread", "claude_linked_thread", "mistral_linked_thread")


def is_linked_foreign_config(model_config: Any) -> bool:
    """Whether a persisted ``model_config`` marks a pointer to any foreign-owned transcript."""
    from persistence.conversation.sessions import parse_model_config

    config = parse_model_config(model_config)
    return any(config.get(flag) is True for flag in linked_foreign_config_flags())


def linked_foreign_message_count_filter(min_message_count: int) -> tuple[str, list[int]]:
    """Keep linked pointer rows visible when a list requests at least one transcript message."""
    if min_message_count == 1:
        clauses = " OR ".join(
            _sql_json_extract("s.model_config", f"$.{flag}") + " = 1"
            for flag in linked_foreign_config_flags()
        )
        return f"(s.message_count >= ? OR {clauses})", [min_message_count]
    return "s.message_count >= ?", [min_message_count] if min_message_count > 0 else []


def linked_pointer_sql(alias: str = "s") -> str:
    """``alias`` predicate matching any foreign-transcript pointer row (Codex, Claude, Mistral).

    A pointer row carries only a native thread id; its transcript lives in the other tool. The
    surface that LINKED it stamps its own ``source``, so a listing scoped to a different surface
    would otherwise drop a session it can resume perfectly well. This predicate is what lets one
    listing include every pointer without also including that surface's ordinary sessions.
    """
    return "(" + " OR ".join(
        _sql_json_extract(f"{alias}.model_config", f"$.{flag}") + " = 1"
        for flag in linked_foreign_config_flags()
    ) + ")"
