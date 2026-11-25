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


def _session_meta(path: Path) -> dict:
    """Read just enough of the transcript for identity: summary, cwd, model, message count."""
    summary = cwd = model = None
    count = 0
    for obj in read_jsonl_bounded(path):
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
        if obj.get("type") in ("user", "assistant") and not (obj.get("isSidechain") or obj.get("isMeta")):
            count += 1
    return {"summary": summary, "cwd": cwd, "model": model, "message_count": count}


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
        meta = _session_meta(path)
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
    """Read one live page of the native Claude transcript as safe display rows."""
    from hermes_cli.claude_linked_history import display_claude_messages

    session_id = foreign_session_uuid(session_id)
    params = page_params(cursor, limit)
    projects = resolve_claude_projects(profile_home)
    path = _session_path(projects, session_id)
    rows = display_claude_messages(path)
    offset = 0
    if params["cursor"] is not None:
        try:
            offset = int(params["cursor"])
        except ValueError:
            raise ValueError("Invalid Claude page cursor") from None
        if offset < 0 or offset > len(rows):
            raise ValueError("Invalid Claude page cursor")
    page = rows[offset:offset + params["limit"]]
    next_offset = offset + params["limit"]
    return {"messages": page, "next_cursor": str(next_offset) if next_offset < len(rows) else None}


def link_claude_thread(session_id, store, profile, profile_home=None):
    """Create one profile-scoped pointer per native session, atomically and idempotently."""
    from persistence.conversation.sessions import NewSession
    from persistence.core.identity import SessionId

    session_id = foreign_session_uuid(session_id)
    projects = resolve_claude_projects(profile_home)
    path = _session_path(projects, session_id)
    meta = _session_meta(path)
    if meta["message_count"] == 0:
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
