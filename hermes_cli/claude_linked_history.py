"""Display the native history of an explicitly linked Claude Code session.

The Hermes session is a locator, not a transcript authority. Reading never resumes or mutates the
Claude session file; every row is redacted and capped before it reaches a display surface.

Paging reads BACKWARDS from the end of the transcript, the way a chat surface scrolls: the first
page is the newest slice, and each ``next_cursor`` walks towards older rows. That keeps a large
transcript (tens of MiB) viewable without loading it whole — the display never needs the entire
file, and the newest work is what a reader wants first.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.foreign_linked_store import read_jsonl_bounded, safe_native_text
from hermes_cli.foreign_sessions import _message_turn

_MAX_ROWS = 10_000
_MAX_PAGE_BYTES = 8 * 1024 * 1024
# Bytes scanned per backwards page. Bounds the work a single page can do on a huge transcript;
# a page that needs more rows than fit in this window simply returns fewer (the cursor walks on).
_SCAN_WINDOW_BYTES = 8 * 1024 * 1024


def _timestamp(obj) -> float | None:
    value = obj.get("timestamp")
    if isinstance(value, str) and value:
        from hermes_cli.timefmt import coerce_epoch
        coerced = coerce_epoch(value, field="timestamp")
        if coerced is not None:
            return coerced
    return None


def _display_row(obj, *, redact=safe_native_text) -> dict | None:
    """Project one native record into a display row, or ``None`` when it is not displayable."""
    if obj.get("type") not in ("user", "assistant"):
        return None
    if obj.get("isSidechain") or obj.get("isMeta"):
        return None
    turn = _message_turn(obj.get("message"))
    if turn is None:
        return None
    role, text = turn
    return {"role": role, "content": redact(text), "timestamp": _timestamp(obj)}


def display_claude_messages(path: Path) -> list[dict]:
    """Project a whole Claude JSONL transcript into safe ``{role, content, timestamp}`` rows.

    Bounded and fail-closed: this is the no-cursor shape kept for callers that want everything a
    transcript legitimately holds. Display surfaces page with :func:`display_claude_messages_page`
    so a large transcript is never required in memory at once.
    """
    rows: list[dict] = []
    total_bytes = 0
    for obj in read_jsonl_bounded(path):
        if len(rows) >= _MAX_ROWS:
            raise ValueError("Claude history exceeded the safe native row limit")
        row = _display_row(obj)
        if row is None:
            continue
        total_bytes += len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if total_bytes > _MAX_PAGE_BYTES:
            raise ValueError("Claude history exceeded the safe display byte limit")
        rows.append(row)
    return rows


def display_claude_messages_page(path: Path, *, before: int | None = None,
                                 limit: int = 50) -> tuple[list[dict], int | None]:
    """Return ``(rows, next_cursor)`` for one backwards page of the transcript.

    ``before`` is the byte offset the page ends at (``None`` = end of file), so the first call
    returns the newest ``limit`` rows. ``next_cursor`` is the offset of the oldest row returned —
    pass it back to load the rows that precede this page, or ``None`` when the transcript start is
    reached. Rows inside a page stay in file order (oldest first), which is reading order.

    Reading is bounded, not fail-closed: a malformed or oversized single record is skipped instead
    of failing the page, because one damaged line must not hide the rest of a transcript (the
    corruption class Claude Code itself can leave behind on a killed append).
    """
    if limit < 1 or limit > _MAX_ROWS:
        raise ValueError("Invalid Claude page size")
    size = path.stat().st_size
    end = size if before is None else min(before, size)
    if end < 0:
        raise ValueError("Invalid Claude page cursor")
    if end == 0:
        return [], None

    start = max(0, end - _SCAN_WINDOW_BYTES)
    rows: list[dict] = []
    offsets: list[int] = []
    with path.open("rb") as handle:
        handle.seek(start)
        if start > 0:
            handle.readline()  # discard a record cut by the window edge
        while True:
            line_start = handle.tell()
            if line_start >= end:
                break
            raw = handle.readline()
            if not raw:
                break
            if handle.tell() > end:
                break
            if len(raw) > _MAX_PAGE_BYTES:
                continue
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            row = _display_row(obj)
            if row is None:
                continue
            rows.append(row)
            offsets.append(line_start)

    if not rows:
        # Nothing displayable in this window: walk further back rather than reporting the start.
        return [], (start if start > 0 else None)
    if len(rows) > limit:
        rows = rows[-limit:]
        offsets = offsets[-limit:]
    # The page is exhausted when its oldest row is the first displayable row in the file. Leading
    # non-displayable records (a `summary` header, compaction markers) are not reachable by scrolling,
    # so an offset above zero is not by itself proof that older conversation remains.
    next_cursor = offsets[0] if not _is_first_displayable_row(path, offsets[0]) else None
    return rows, next_cursor


def _is_first_displayable_row(path: Path, offset: int) -> bool:
    """True when no displayable row precedes ``offset`` (i.e. the reader has reached the top)."""
    import json

    with path.open("rb") as handle:
        scanned = 0
        for raw in handle:
            if handle.tell() > offset:
                return True
            scanned += len(raw)
            if scanned > _SCAN_WINDOW_BYTES and handle.tell() < offset:
                # Far from the top: definitely more history above.
                return False
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and _display_row(obj) is not None:
                return handle.tell() > offset
    return True


def linked_claude_session_id(store, session_id: str) -> str | None:
    """Return the native identity, rejecting a damaged linked-session record."""
    row = store.sessions.get(session_id)
    if row is None or row.model_config.get("claude_linked_thread") is not True:
        return None
    native_id = row.model_config.get("claude_session_id")
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("Linked Claude session has no session ID")
    return native_id

