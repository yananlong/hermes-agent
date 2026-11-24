"""Safe display decisions for Codex-owned turns linked to a Hermes session."""

import json
import time

from persistence.conversation.sessions import parse_model_config

_MAX_OVERLAY_TEXT_BYTES = 256 * 1024
_MAX_OVERLAY_BYTES = 8 * 1024 * 1024
_MAX_OVERLAY_RECORD_BYTES = _MAX_OVERLAY_TEXT_BYTES * 6 + 8192
_MAX_OVERLAYS_PER_THREAD = 2_000
_REDACTION_FAILURE = "[content hidden because redaction failed]"


def _linked_identity(raw, thread_id: str) -> bool:
    config = parse_model_config(raw)
    return config.get("codex_linked_thread") is True and config.get("codex_thread_id") == thread_id


def _safe_overlay_text(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        from agent.redact import redact_sensitive_text
        safe = redact_sensitive_text(value, force=True)
    except Exception:  # health: allow BLE001 -- an unknown redactor failure must not persist native text
        return _REDACTION_FAILURE
    if not isinstance(safe, str):
        return _REDACTION_FAILURE
    encoded = safe.encode("utf-8")
    if len(encoded) <= _MAX_OVERLAY_TEXT_BYTES:
        return safe
    suffix = b"\n[truncated by Hermes display limit]"
    prefix = encoded[:_MAX_OVERLAY_TEXT_BYTES - len(suffix)].decode("utf-8", errors="ignore")
    return prefix + suffix.decode("ascii")


def _prune_overlays(conn, session_id: str, thread_id: str) -> None:
    rows = conn.execute(
        "SELECT turn_id, length(CAST(overlay_json AS BLOB)) FROM codex_linked_display_overlays "
        "WHERE session_id = ? AND thread_id = ? ORDER BY created_at DESC, turn_id DESC",
        (session_id, thread_id),
    ).fetchall()
    total_bytes, stale = 0, []
    for index, row in enumerate(rows):
        row_size = row[1]
        if index < _MAX_OVERLAYS_PER_THREAD and total_bytes + row_size <= _MAX_OVERLAY_BYTES:
            total_bytes += row_size
        else:
            stale.append(row[0])
    if stale:
        conn.executemany(
            "DELETE FROM codex_linked_display_overlays WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
            [(session_id, thread_id, turn_id) for turn_id in stale],
        )


def save_linked_codex_display_overlay(
    holder, session_id: str, thread_id: str, turn_id: str, *, final_text: str | None,
    held: bool, platform: str,
) -> bool:
    """Persist only the settled display text and hold decision, never the native raw reply."""
    if (not all(isinstance(value, str) and 0 < len(value) <= 512
                for value in (session_id, thread_id, turn_id))
            or (final_text is not None and not isinstance(final_text, str))
            or type(held) is not bool or not isinstance(platform, str) or len(platform) > 128):
        raise ValueError("Invalid linked Codex display overlay")
    created_at = time.time()
    overlay = {
        "version": 1,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "held": held,
        "final_visible": bool(final_text),
        "final_text": _safe_overlay_text(final_text) if final_text else None,
        "platform": platform,
        "created_at": created_at,
    }
    encoded = json.dumps(overlay, ensure_ascii=False, separators=(",", ":"))

    def write(conn):
        row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or not _linked_identity(row[0], thread_id):
            return False
        conn.execute(
            "INSERT INTO codex_linked_display_overlays(session_id, thread_id, turn_id, overlay_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, thread_id, turn_id) DO UPDATE SET "
            "overlay_json = excluded.overlay_json, created_at = excluded.created_at",
            (session_id, thread_id, turn_id, encoded, created_at),
        )
        _prune_overlays(conn, session_id, thread_id)
        return True

    return bool(holder._execute_write(write))


def load_linked_codex_display_overlays(
    holder, session_id: str, thread_id: str, turn_ids: list[str],
) -> dict[str, dict]:
    """Return bounded decisions for one native page and exact linked owner/thread pair."""
    if (not isinstance(session_id, str) or not session_id or not isinstance(thread_id, str) or not thread_id):
        return {}
    if (not isinstance(turn_ids, list) or len(turn_ids) > _MAX_OVERLAYS_PER_THREAD
            or any(not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256 for turn_id in turn_ids)):
        return {}
    selected = list(dict.fromkeys(turn_ids))
    rows, total_bytes = [], 0
    for start in range(0, len(selected), 500):
        remaining = _MAX_OVERLAY_BYTES - total_bytes
        if remaining <= 0:
            break
        batch = selected[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        batch_rows = holder._read_all(
            "SELECT turn_id, overlay_json FROM (SELECT overlay.turn_id, overlay.overlay_json, overlay.created_at, "
            "SUM(length(CAST(overlay.overlay_json AS BLOB))) "
            "OVER (ORDER BY overlay.created_at DESC, overlay.turn_id DESC) AS total_bytes "
            "FROM codex_linked_display_overlays AS overlay JOIN sessions ON sessions.id = overlay.session_id "
            "WHERE overlay.session_id = ? AND overlay.thread_id = ? "
            "AND json_type(CASE WHEN json_valid(sessions.model_config) THEN sessions.model_config ELSE '{}' END, "
            "'$.codex_linked_thread') = 'true' "
            "AND json_extract(CASE WHEN json_valid(sessions.model_config) THEN sessions.model_config ELSE '{}' END, "
            "'$.codex_thread_id') = overlay.thread_id "
            f"AND overlay.turn_id IN ({placeholders}) AND length(CAST(overlay.overlay_json AS BLOB)) <= ?) "
            "WHERE total_bytes <= ? ORDER BY created_at DESC, turn_id DESC",
            (session_id, thread_id, *batch, _MAX_OVERLAY_RECORD_BYTES, remaining),
        )
        rows.extend(batch_rows)
        total_bytes += sum(_overlay_row_size(row) for row in batch_rows)
    overlays = {}
    for row in rows:
        turn_id = row["turn_id"] if isinstance(row, dict) else row[0]
        raw_overlay = row["overlay_json"] if isinstance(row, dict) else row[1]
        try:
            item = json.loads(raw_overlay)
        except (TypeError, json.JSONDecodeError):
            continue
        if (not isinstance(item, dict) or item.get("version") != 1 or item.get("thread_id") != thread_id
                or item.get("turn_id") != turn_id or type(item.get("held")) is not bool
                or type(item.get("final_visible")) is not bool
                or (item.get("final_text") is not None and not isinstance(item.get("final_text"), str))):
            continue
        overlays[turn_id] = item
    return overlays


def _overlay_row_size(row) -> int:
    raw_overlay = row.get("overlay_json") if isinstance(row, dict) else row[1]
    return len(raw_overlay.encode("utf-8")) if isinstance(raw_overlay, str) else 0
