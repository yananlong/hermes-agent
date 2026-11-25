"""Display the native history of an explicitly linked Claude Code session.

The Hermes session is a locator, not a transcript authority. Reading never resumes or mutates the
Claude session file; every row is redacted and capped before it reaches a display surface.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.foreign_linked_store import (
    read_jsonl_bounded, row_model_config, safe_native_text,
)
from hermes_cli.foreign_sessions import _message_turn

_MAX_ROWS = 10_000
_MAX_PAGE_BYTES = 8 * 1024 * 1024


def _timestamp(obj) -> float | None:
    value = obj.get("timestamp")
    if isinstance(value, str) and value:
        from hermes_cli.timefmt import coerce_epoch
        coerced = coerce_epoch(value, field="timestamp")
        if coerced is not None:
            return coerced
    return None


def display_claude_messages(path: Path) -> list[dict]:
    """Project one Claude JSONL transcript into safe ``{role, content, timestamp}`` rows."""
    rows: list[dict] = []
    total_bytes = 0
    for obj in read_jsonl_bounded(path):
        if len(rows) >= _MAX_ROWS:
            raise ValueError("Claude history exceeded the safe native row limit")
        if obj.get("type") not in ("user", "assistant"):
            continue
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        turn = _message_turn(obj.get("message"))
        if turn is None:
            continue
        role, text = turn
        row = {"role": role, "content": safe_native_text(text), "timestamp": _timestamp(obj)}
        total_bytes += len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if total_bytes > _MAX_PAGE_BYTES:
            raise ValueError("Claude history exceeded the safe display byte limit")
        rows.append(row)
    return rows


def linked_claude_session_id(store, session_id: str) -> str | None:
    """Return the native identity, rejecting a damaged linked-session record.

    Reads through the shared shape-tolerant row reader: ``SessionDB.get_session`` hands back a dict
    row whose ``model_config`` may be JSON text, while the persistence handle's record carries a
    dict. Reading ``row.model_config`` directly made every ordinary row on the dict-shaped store
    raise ``AttributeError`` — which every canonical read turned into a resume failure.

    Raises:
        LinkedConfigUnreadable: the row's ``model_config`` exists but is unusable.
        ValueError: the row is flagged as a linked Claude session but records no session id.
    """
    config = row_model_config(store, session_id)
    if config.get("claude_linked_thread") is not True:
        return None
    native_id = config.get("claude_session_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("Linked Claude session has no session ID")
    return native_id

