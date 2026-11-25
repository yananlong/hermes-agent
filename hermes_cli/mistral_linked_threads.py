"""Discover and link native Mistral Vibe (``vibe`` CLI) sessions without importing their transcript.

Mistral Vibe is the authority for session identity and history. Sessions live under the session
root (``MISTRAL_VIBE_SESSION_ROOT``, else ``$VIBE_HOME/logs/session``, else ``~/.vibe/logs/session``):

- unified layout (vibe 3.x): ``unified/<session-uuid>/`` with ``meta.json`` plus an append-only
  ``journal/<seq>.jsonl`` event log; ``vibe --resume <uuid>`` continues that same store.
- legacy layout: ``session_<timestamp>_<short8id>/`` with ``meta.json`` + ``messages.jsonl``;
  ``vibe --resume`` accepts the partial id.

The Hermes row only stores a durable pointer; history is read live from the native store and no
second transcript is ever created. Limitation: Vibe has no persistent app-server protocol, so a
linked Mistral session is read-oriented — Hermes cannot append a turn to the native store from
inside a linked session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hermes_cli.foreign_linked_store import (
    foreign_session_uuid, page_params, resolve_foreign_store, safe_native_text,
)
from session_rules.session_ids import new_session_id

_SOURCE_LABEL = "mistral-vibe"
_TITLE_MAX = 180
_LEGACY_DIR_RE = re.compile(r"^session_\d{8}T?\d{0,6}_[0-9a-f]{8}$")
_SHORT_ID_RE = re.compile(r"^[0-9a-f]{8}$")


def resolve_vibe_session_root(profile_home=None) -> Path:
    """Resolve the Vibe session root owned by the active profile."""
    from agent.secret_scope import get_secret

    configured = get_secret("MISTRAL_VIBE_SESSION_ROOT")
    if isinstance(configured, str) and configured.strip():
        return resolve_foreign_store("MISTRAL_VIBE_SESSION_ROOT", (), "", profile_home)
    vibe_home = get_secret("VIBE_HOME")
    if isinstance(vibe_home, str) and vibe_home.strip():
        root = Path(vibe_home).expanduser() / "logs" / "session"
        from hermes_cli.foreign_linked_store import check_owning_profile
        check_owning_profile(profile_home)
        if not root.is_absolute():
            raise ValueError("VIBE_HOME must resolve to an absolute directory")
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("The owning profile's Vibe session store is unavailable") from exc
        if not root.is_dir():
            raise ValueError("The owning profile's Vibe session store is unavailable")
        return root
    return resolve_foreign_store("MISTRAL_VIBE_SESSION_ROOT", (".vibe", "logs"), "session", profile_home)


def _session_dirs(root: Path) -> list[tuple[float, Path, str]]:
    """All session directories as ``(mtime, path, session_id)``, newest first.

    Unified dirs are keyed by full UUID; legacy ``session_*`` dirs by their 8-char suffix, which
    ``vibe --resume`` accepts as a partial id.
    """
    rows = []
    unified = root / "unified"
    if unified.is_dir():
        for path in unified.iterdir():
            if not path.is_dir():
                continue
            try:
                session_id = foreign_session_uuid(path.name)
            except ValueError:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            rows.append((mtime, path, session_id))
    for path in root.iterdir():
        if not path.is_dir() or not _LEGACY_DIR_RE.match(path.name):
            continue
        short = path.name.rsplit("_", 1)[-1]
        if not _SHORT_ID_RE.match(short):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rows.append((mtime, path, short))
    rows.sort(key=lambda row: row[0], reverse=True)
    return rows


def _read_meta(session_dir: Path) -> dict:
    meta_path = session_dir / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _session_info(session_dir: Path, session_id: str) -> dict:
    from hermes_cli.mistral_linked_history import count_vibe_messages

    meta = _read_meta(session_dir)
    environment = meta.get("environment")
    cwd = environment.get("working_directory") if isinstance(environment, dict) else None
    if not isinstance(cwd, str) or not cwd:
        cwd = meta.get("cwd")
    title = meta.get("title")
    model = meta.get("model")
    bumped = meta.get("bumped_at") or meta.get("end_time") or meta.get("start_time")
    return {"id": session_id, "title": title if isinstance(title, str) else None,
            "cwd": cwd if isinstance(cwd, str) else None,
            "model": model if isinstance(model, str) else None,
            "bumped_at": bumped if isinstance(bumped, (int, float)) else None,
            "message_count": count_vibe_messages(session_dir)}


def list_mistral_threads(cursor=None, limit=25, profile_home=None):
    """Page the Vibe session store newest-first; previews stay empty by policy."""
    params = page_params(cursor, limit)
    root = resolve_vibe_session_root(profile_home)
    offset = 0
    if params["cursor"] is not None:
        try:
            offset = int(params["cursor"])
        except ValueError:
            raise ValueError("Invalid Mistral page cursor") from None
        if offset < 0:
            raise ValueError("Invalid Mistral page cursor")

    dirs = _session_dirs(root)
    rows = []
    for mtime, path, session_id in dirs[offset:offset + params["limit"]]:
        info = _session_info(path, session_id)
        raw_title = info["title"]
        title = safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Mistral session")[:_TITLE_MAX]
        cwd = info["cwd"]
        if not isinstance(cwd, str) or len(cwd) > 4096:
            cwd = None
        updated_at = int((info["bumped_at"] or mtime) * 1000)
        if not 0 <= updated_at < 2**63:
            updated_at = None
        rows.append({"id": session_id, "title": title or "Mistral session",
                     "cwd": cwd, "updated_at": updated_at,
                     "preview": "", "source": _SOURCE_LABEL})
    next_offset = offset + params["limit"]
    return {"threads": rows, "next_cursor": str(next_offset) if next_offset < len(dirs) else None}


def _contained_session_dir(root: Path, path: Path) -> Path:
    """Resolve one session dir and refuse it when it escapes the store root.

    A session directory is attacker-influenced input (it is a directory name in the user's Vibe
    store), so a symlink there could otherwise redirect a read — or a resume — outside the store.
    Mirrors ``claude_linked_threads._session_path``: resolve first, then require the result to stay
    under the store root.
    """
    try:
        store = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Mistral Vibe did not return the requested session") from exc
    if store != resolved and store not in resolved.parents:
        raise ValueError("The Mistral session path escapes the Vibe session store")
    if not resolved.is_dir():
        raise ValueError("Mistral Vibe did not return the requested session")
    return resolved


def _find_session_dir(root: Path, session_id: str) -> Path:
    """Locate one session dir by full UUID (unified) or 8-char partial id (legacy)."""
    if _SHORT_ID_RE.match(session_id) and len(session_id) == 8:
        for path in root.iterdir():
            if path.is_dir() and _LEGACY_DIR_RE.match(path.name) and path.name.rsplit("_", 1)[-1] == session_id:
                return _contained_session_dir(root, path)
        raise ValueError("Mistral Vibe did not return the requested session")
    session_id = foreign_session_uuid(session_id)
    return _contained_session_dir(root, root / "unified" / session_id)


def read_mistral_history(session_id, cursor=None, limit=50, profile_home=None):
    """Read one live page of the native Vibe transcript as safe display rows."""
    from hermes_cli.mistral_linked_history import display_vibe_messages

    params = page_params(cursor, limit)
    root = resolve_vibe_session_root(profile_home)
    session_dir = _find_session_dir(root, session_id)
    rows = display_vibe_messages(session_dir)
    offset = 0
    if params["cursor"] is not None:
        try:
            offset = int(params["cursor"])
        except ValueError:
            raise ValueError("Invalid Mistral page cursor") from None
        if offset < 0 or offset > len(rows):
            raise ValueError("Invalid Mistral page cursor")
    page = rows[offset:offset + params["limit"]]
    next_offset = offset + params["limit"]
    return {"messages": page, "next_cursor": str(next_offset) if next_offset < len(rows) else None}


def link_mistral_thread(session_id, store, profile, profile_home=None):
    """Create one profile-scoped pointer per native session, atomically and idempotently."""
    from persistence.conversation.sessions import NewSession
    from persistence.core.identity import SessionId

    if not isinstance(session_id, str) or not session_id:
        raise ValueError("A Mistral session ID is required")
    root = resolve_vibe_session_root(profile_home)
    session_dir = _find_session_dir(root, session_id)
    info = _session_info(session_dir, session_id)
    if info["message_count"] == 0:
        raise ValueError("This Mistral session has no readable conversation messages")
    cwd = info["cwd"]
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("The Mistral session's working directory is unavailable")
    model = info["model"]
    if model is not None and len(model) > 256:
        model = None
    new_id = new_session_id(hex_len=12)
    raw_title = info["title"]
    title = safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Mistral session")[:_TITLE_MAX].strip()
    title = title or "Mistral session"
    config = {"mistral_session_id": info["id"], "mistral_linked_thread": True,
              "api_mode": "mistral_linked_readonly"}
    spec = NewSession(session_id=SessionId(new_id), source="desktop", cwd=cwd,
                      model=model, model_config=config, profile_name=profile)
    linked_id, already_linked = store.sessions.link_foreign_session(
        spec, title, flag="mistral_linked_thread", id_key="mistral_session_id",
        native_id=info["id"], fallback_title="Mistral session")
    return {"session_id": linked_id, "already_linked": already_linked}
