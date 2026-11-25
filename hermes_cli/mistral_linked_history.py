"""Display the native history of an explicitly linked Mistral Vibe session.

The Hermes session is a locator, not a transcript authority. Reading never resumes or mutates the
Vibe session store; every row is redacted and capped before it reaches a display surface.

Two on-disk shapes are supported: the unified layout (``journal/*.jsonl`` event records whose
``projection_delta`` payloads carry ``delta[].entry`` messages) and the legacy layout
(``messages.jsonl`` with plain ``role``/``content`` records).
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.foreign_linked_store import read_jsonl_bounded, safe_native_text
from hermes_cli.foreign_sessions import _flatten_blocks

_MAX_ROWS = 10_000
_MAX_PAGE_BYTES = 8 * 1024 * 1024


def _entry_turn(entry) -> tuple[str, str] | None:
    """Normalize one Vibe message entry (or legacy record) into a ``(role, text)`` turn."""
    if not isinstance(entry, dict):
        return None
    role = entry.get("role")
    if role not in ("user", "assistant"):
        return None
    content = entry.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = _flatten_blocks(content)
    return (role, text) if text else None


def _iter_records(session_dir: Path):
    """Yield transcript records from either layout, oldest first."""
    journal = session_dir / "journal"
    if journal.is_dir():
        for path in sorted(journal.glob("*.jsonl")):
            yield from read_jsonl_bounded(path)
        return
    messages = session_dir / "messages.jsonl"
    if messages.is_file():
        yield from read_jsonl_bounded(messages)


def _record_turns(obj: dict) -> list[tuple[str, str]]:
    """Extract conversation turns from one journal record or legacy message line."""
    payload = obj.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("delta"), list):
        turns = []
        for delta in payload["delta"]:
            if not isinstance(delta, dict):
                continue
            turn = _entry_turn(delta.get("entry"))
            if turn is not None:
                turns.append(turn)
        return turns
    turn = _entry_turn(obj)
    return [turn] if turn is not None else []


def display_vibe_messages(session_dir: Path) -> list[dict]:
    """Project one Vibe session store into safe ``{role, content}`` rows."""
    rows: list[dict] = []
    total_bytes = 0
    for obj in _iter_records(session_dir):
        if len(rows) >= _MAX_ROWS:
            raise ValueError("Mistral history exceeded the safe native row limit")
        for role, text in _record_turns(obj):
            row = {"role": role, "content": safe_native_text(text)}
            total_bytes += len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
            if total_bytes > _MAX_PAGE_BYTES:
                raise ValueError("Mistral history exceeded the safe display byte limit")
            rows.append(row)
    return rows


def count_vibe_messages(session_dir: Path) -> int:
    """Count readable conversation rows without materializing redacted text."""
    count = 0
    try:
        for obj in _iter_records(session_dir):
            count += len(_record_turns(obj))
            if count > _MAX_ROWS:
                return _MAX_ROWS
    except ValueError:
        return count
    return count


def linked_mistral_session_id(store, session_id: str) -> str | None:
    """Return the native identity, rejecting a damaged linked-session record."""
    row = store.sessions.get(session_id)
    if row is None or row.model_config.get("mistral_linked_thread") is not True:
        return None
    native_id = row.model_config.get("mistral_session_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("Linked Mistral session has no session ID")
    return native_id

