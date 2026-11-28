"""Discover and link native Claude Code sessions without importing their transcript.

Claude Code is the authority for session identity and history: every session is one append-only
JSONL under ``~/.claude/projects/<encoded-project>/<session-uuid>.jsonl`` (relocated wholesale by
``CLAUDE_CONFIG_DIR``), and ``claude --resume <uuid>`` continues that same file. The Hermes row
only stores a durable pointer; opening a history page never resumes or changes the session.

Limitation: unlike Codex there is no persistent app-server protocol, so Hermes cannot append a
turn to the native file from inside a linked session. A linked Claude session is therefore
read-oriented — its history is always read live from the JSONL, and Hermes writes no second
transcript for it.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli.foreign_linked_store import (
    foreign_session_uuid, page_params, read_jsonl_bounded, resolve_foreign_store, safe_native_text,
)
from session_rules.session_ids import new_session_id

_SOURCE_LABEL = "claude-code"
_TITLE_MAX = 180


def resolve_claude_projects(profile_home=None) -> Path:
    """Resolve the Claude Code projects store owned by the active profile."""
    return resolve_foreign_store("CLAUDE_CONFIG_DIR", (".claude",), "projects", profile_home)


def _session_path(projects: Path, session_id: str) -> Path:
    """Locate one session file by UUID under the projects root; never trust a raw path."""
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    if not matches:
        raise ValueError("Claude Code did not return the requested session")
    resolved = matches[0].resolve()
    if projects.resolve() not in resolved.parents:
        raise ValueError("The Claude session path escapes the projects store")
    return resolved


def _has_displayable_message(path: Path) -> bool:
    """True when the transcript's head holds at least one displayable user/assistant row.

    A bounded HEAD scan, not a full count: linking only needs to reject an empty or unreadable
    conversation, and a transcript can be tens of MiB.
    """
    from hermes_cli.claude_linked_history import _display_row
    from hermes_cli.foreign_linked_store import MAX_STORE_HEAD_BYTES, read_jsonl_head

    for obj in read_jsonl_head(path, MAX_STORE_HEAD_BYTES):
        if _display_row(obj) is not None:
            return True
    return False


def _session_meta(path: Path) -> dict:
    """Read just enough of the transcript for identity: summary, cwd, model.

    Bounded HEAD read, not a whole-file parse: a listing needs four fields, and a transcript can be
    tens of MiB. ``message_count`` was dropped from the contract for the same reason — the picker
    never rendered it, and computing it required scanning the entire file.
    """
    from hermes_cli.foreign_linked_store import MAX_STORE_HEAD_BYTES, read_jsonl_head

    summary = cwd = model = None
    for obj in read_jsonl_head(path, MAX_STORE_HEAD_BYTES):
        if obj.get("type") == "summary" and summary is None:
            value = obj.get("summary")
            if isinstance(value, str) and value.strip():
                summary = value.strip()
        if cwd is None and isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]
        if model is None:
            message = obj.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                model = message["model"]
        if summary is not None and cwd is not None and model is not None:
            break
    return {"summary": summary, "cwd": cwd, "model": model}


def list_claude_threads(cursor=None, limit=25, profile_home=None):
    """Page the Claude Code projects store newest-first; previews stay empty by policy."""
    params = page_params(cursor, limit)
    projects = resolve_claude_projects(profile_home)
    offset = 0
    if params["cursor"] is not None:
        try:
            offset = int(params["cursor"])
        except ValueError:
            raise ValueError("Invalid Claude page cursor") from None
        if offset < 0:
            raise ValueError("Invalid Claude page cursor")

    candidates = []
    for path in projects.glob("*/*.jsonl"):
        try:
            resolved = path.resolve()
            if projects.resolve() not in resolved.parents or not resolved.is_file():
                continue
            stat = resolved.stat()
        except OSError:
            continue
        candidates.append((stat.st_mtime, resolved))
    candidates.sort(key=lambda row: row[0], reverse=True)

    rows = []
    for mtime, path in candidates[offset:offset + params["limit"]]:
        try:
            session_id = foreign_session_uuid(path.stem)
        except ValueError:
            continue  # non-session JSONL files in a project dir are skipped
        try:
            meta = _session_meta(path)
        except (OSError, ValueError):
            # One unreadable or damaged transcript must not abort the whole picker: it degrades to
            # a single row with default identity instead of returning no threads at all.
            meta = {"summary": None, "cwd": None, "model": None}
        raw_title = meta["summary"]
        title = safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Claude session")[:_TITLE_MAX]
        cwd = meta["cwd"]
        if not isinstance(cwd, str) or len(cwd) > 4096:
            cwd = None
        updated_at = int(mtime * 1000)
        if not 0 <= updated_at < 2**63:
            updated_at = None
        rows.append({"id": session_id, "title": title or "Claude session",
                     "cwd": cwd, "updated_at": updated_at,
                     "preview": "", "source": _SOURCE_LABEL})
    next_offset = offset + params["limit"]
    return {"threads": rows, "next_cursor": str(next_offset) if next_offset < len(candidates) else None}


def read_claude_history(session_id, cursor=None, limit=50, profile_home=None):
    """Read one BACKWARDS page of the native Claude transcript as safe display rows.

    Newest slice first: the first call (no cursor) returns the most recent ``limit`` rows and each
    ``next_cursor`` walks towards older rows, matching how a chat surface loads history as the user
    scrolls up. A large transcript is never read whole, so a 36 MiB session opens as fast as a small
    one. Rows within a page stay in reading order (oldest first).
    """
    from hermes_cli.claude_linked_history import display_claude_messages_page

    session_id = foreign_session_uuid(session_id)
    params = page_params(cursor, limit)
    projects = resolve_claude_projects(profile_home)
    path = _session_path(projects, session_id)
    before = None
    if params["cursor"] is not None:
        try:
            before = int(params["cursor"])
        except ValueError:
            raise ValueError("Invalid Claude page cursor") from None
        if before < 0:
            raise ValueError("Invalid Claude page cursor")
    rows, next_cursor = display_claude_messages_page(path, before=before, limit=params["limit"])
    return {"messages": rows, "next_cursor": None if next_cursor is None else str(next_cursor)}


def link_claude_thread(session_id, store, profile, profile_home=None):
    """Create one profile-scoped pointer per native session, atomically and idempotently."""
    from persistence.conversation.sessions import NewSession
    from persistence.core.identity import SessionId

    session_id = foreign_session_uuid(session_id)
    projects = resolve_claude_projects(profile_home)
    path = _session_path(projects, session_id)
    meta = _session_meta(path)
    if not _has_displayable_message(path):
        raise ValueError("This Claude session has no readable conversation messages")
    cwd = meta["cwd"]
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("The Claude session's working directory is unavailable")
    model = meta["model"]
    if model is not None and (not isinstance(model, str) or len(model) > 256):
        model = None
    new_id = new_session_id(hex_len=12)
    raw_title = meta["summary"]
    title = safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Claude session")[:_TITLE_MAX].strip()
    title = title or "Claude session"
    config = {"claude_session_id": session_id, "claude_linked_thread": True, "api_mode": "claude_linked_readonly"}
    spec = NewSession(session_id=SessionId(new_id), source="desktop", cwd=cwd,
                      model=model, model_config=config, profile_name=profile)
    linked_id, already_linked = store.sessions.link_foreign_session(
        spec, title, flag="claude_linked_thread", id_key="claude_session_id",
        native_id=session_id, fallback_title="Claude session")
    return {"session_id": linked_id, "already_linked": already_linked}
