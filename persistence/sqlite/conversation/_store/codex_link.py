"""Atomic creation and workspace recovery for a native Codex session pointer."""

import json
from typing import Any, Optional

from persistence.conversation.sessions import CANONICAL_BOT_CHAT_TITLE, NewSession, TitleSource, sanitize_title
from persistence.core.errors import AuthorityRefusal
from persistence.core.identity import SessionId

from ..._schema_ddl import _sql_json_extract
from .retirement import create_conflict
from .sessions import _parse_model_config
from .worker_lifecycle import insert_session_row_in_transaction


def link_codex_session(holder, spec: NewSession, title: str, thread_id: str) -> tuple[SessionId, bool]:
    """Return the existing pointer or create one under the profile's SQLite writer lock."""
    title = sanitize_title(title) or "Codex thread"
    if title == CANONICAL_BOT_CHAT_TITLE:
        title = "Codex: Bot Chat"

    def write(conn):
        existing_id = _sql_json_extract("model_config", "$.codex_thread_id")
        valid_json = "(CASE WHEN json_valid(model_config) THEN model_config ELSE json_object() END)"
        existing = conn.execute(
            f"SELECT id FROM sessions WHERE json_type({valid_json}, '$.codex_linked_thread') "
            f"= 'true' AND {existing_id} = ? ORDER BY started_at, id LIMIT 1",
            (thread_id,),
        ).fetchone()
        if existing is not None:
            return SessionId(existing["id"]), True
        if create_conflict(conn, spec.session_id):
            raise AuthorityRefusal("revision_conflict", field="session_id")
        label = title
        if conn.execute("SELECT 1 FROM sessions WHERE title=?", (title,)).fetchone():
            label = f"{title[:180]} ({spec.session_id[-12:]})"
        config = dict(spec.model_config or {})
        config.update(codex_linked_thread=True, codex_thread_id=thread_id)
        insert_session_row_in_transaction(
            holder, conn, spec.session_id, spec.source, model=spec.model, model_config=config,
            cwd=spec.cwd, profile_name=spec.profile_name,
        )
        conn.execute("UPDATE sessions SET title=?, title_source=? WHERE id=?",
                     (label, TitleSource.DERIVED.value, spec.session_id))
        return spec.session_id, False

    return holder._execute_write(write)


def claim_linked_codex_workspace_change(holder, session_id: str, intent: dict[str, Any]) -> bool:
    """Durably claim one explicit linked-thread cwd change without changing the visible cwd row.

    The compare-and-set prevents concurrent workspace pickers from replacing an in-flight intent before
    Codex's per-thread writer lock is acquired.
    """
    if not session_id or not isinstance(intent, dict):
        return False
    thread_id, source, target = (intent.get(key) for key in ("thread_id", "from", "to"))
    if not all(isinstance(value, str) and value for value in (thread_id, source, target)):
        return False

    def _do(conn):
        row = conn.execute("SELECT cwd, model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or row[0] != source:
            return False
        config = _parse_model_config(row[1])
        if (config.get("codex_linked_thread") is not True or config.get("codex_thread_id") != thread_id
                or config.get("codex_pending_cwd") is not None):
            return False
        config["codex_pending_cwd"] = intent
        conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?", (json.dumps(config), session_id))
        return True

    return bool(holder._execute_write(_do))

def finish_linked_codex_workspace_change(
    holder, session_id: str, intent: dict[str, Any], cwd: str,
    git_branch: Optional[str] = None, git_repo_root: Optional[str] = None,
) -> Optional[int]:
    """Commit the observed native cwd while retaining a recoverable committed intent.

    The caller reads the row back while the intent is still present, then clears the exact committed
    marker. If storage or the process fails between those steps, the next workspace operation can
    reconcile the row against Codex's still-authoritative cwd.
    """
    if not session_id or not cwd or not isinstance(intent, dict):
        return None
    branch, repo_root = (git_branch or "").strip(), (git_repo_root or "").strip()

    def _do(conn):
        row = conn.execute("SELECT cwd, model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        config = _parse_model_config(row[1])
        pending = config.get("codex_pending_cwd")
        prior_effective = pending.get("effective") if isinstance(pending, dict) else None
        expected_cwds = {intent.get("from"), prior_effective}
        if (pending != intent or row[0] not in expected_cwds or config.get("codex_linked_thread") is not True
                or config.get("codex_thread_id") != intent.get("thread_id")):
            return None
        committed_intent = {**intent, "phase": "committed", "effective": cwd}
        config["codex_pending_cwd"] = committed_intent
        conn.execute(
            "UPDATE sessions SET cwd = ?, git_branch = ?, git_repo_root = ?, "
            "git_metadata_generation = COALESCE(git_metadata_generation, 0) + 1, model_config = ? WHERE id = ?",
            (cwd, branch or None, repo_root or None, json.dumps(config), session_id),
        )
        row = conn.execute("SELECT git_metadata_generation FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else int(row[0])

    return holder._execute_write(_do)

def clear_linked_codex_workspace_change(holder, session_id: str, intent: dict[str, Any]) -> bool:
    """Clear an un-applied intent only when it is still the exact durable request."""
    if not session_id or not isinstance(intent, dict):
        return False

    def _do(conn):
        row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return False
        config = _parse_model_config(row[0])
        if config.get("codex_pending_cwd") != intent:
            return False
        config.pop("codex_pending_cwd", None)
        conn.execute("UPDATE sessions SET model_config = ? WHERE id = ?",
                     (json.dumps(config) if config else None, session_id))
        return True

    return bool(holder._execute_write(_do))
