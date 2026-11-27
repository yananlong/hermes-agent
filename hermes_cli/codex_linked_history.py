"""Display the native history of an explicitly linked Codex thread.

The Hermes session is a locator, not a transcript authority. Reading never
resumes or mutates the Codex thread; unsupported item types remain visible as
opaque notes through the same projector used for live Codex events.
"""

import json
import math
from contextlib import nullcontext
from copy import deepcopy

from agent.transports.codex_event_projector import CodexEventProjector


def _positive_epoch_ms(value) -> int | float | None:
    if type(value) is int:
        return value if 0 < value <= 10**16 else None
    if type(value) is float and math.isfinite(value) and value <= 10**16:
        return value if value > 0 else None
    return None


def _positive_epoch_seconds(value) -> int | float | None:
    if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10**11:
        return value
    return None


def _native_timestamp_ms_or_seconds(value_ms, value_seconds) -> int | float | None:
    if timestamp_ms := _positive_epoch_ms(value_ms):
        return timestamp_ms / 1000
    return _positive_epoch_seconds(value_seconds)


def project_codex_turn(turn: dict, *, thread_id: str | None = None) -> list[dict]:
    """Project one already-read native turn with the live item's projector semantics."""
    projector = CodexEventProjector(force_reasoning_redaction=True)
    rows = []
    turn_id = turn.get("id")
    scope = {"threadId": thread_id, "turnId": turn_id}
    turn_timestamp = (
        _native_timestamp_ms_or_seconds(turn.get("startedAtMs"), turn.get("startedAt"))
        or _native_timestamp_ms_or_seconds(turn.get("completedAtMs"), turn.get("completedAt"))
    )
    for item in turn.get("items", []):
        if not isinstance(item, dict):
            raise ValueError("Codex returned an invalid history item")
        result = projector.project({"method": "item/completed", "params": {**scope, "item": item}})
        timestamp = (
            _native_timestamp_ms_or_seconds(item.get("startedAtMs"), item.get("startedAt"))
            or turn_timestamp
        )
        for row in result.messages:
            if timestamp is not None:
                row["timestamp"] = timestamp
            rows.append(row)
    completed = projector.project({"method": "turn/completed", "params": {**scope, "turn": turn}})
    timestamp = _native_timestamp_ms_or_seconds(turn.get("completedAtMs"), turn.get("completedAt"))
    if timestamp is None:
        timestamp = next((_native_timestamp_ms_or_seconds(item.get("startedAtMs"), item.get("startedAt"))
                          for item in reversed(turn.get("items", []))
                          if isinstance(item, dict) and _native_timestamp_ms_or_seconds(
                              item.get("startedAtMs"), item.get("startedAt")) is not None), None)
    for row in completed.messages:
        if timestamp is not None:
            row["timestamp"] = timestamp
        rows.append(row)
    return rows


def native_turn_record(turn: dict, *, thread_id: str | None = None) -> dict:
    """Project one validated native turn while retaining the boundary needed by output policy."""
    if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
        raise ValueError("Codex returned an invalid history turn")
    turn_id = turn.get("id")
    if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
        raise ValueError("Codex returned a native turn without a stable ID")
    final_text = next((item.get("text") for item in reversed(turn["items"])
                       if isinstance(item, dict) and item.get("type") == "agentMessage"
                       and isinstance(item.get("text"), str) and item["text"]), None)
    status = turn.get("status")
    if not isinstance(status, str) or len(status) > 64:
        status = None
    return {"id": turn_id, "status": status, "final_text": final_text,
            "rows": project_codex_turn(turn, thread_id=thread_id)}


def _next_cursor(page: dict) -> str | None:
    cursor = page.get("next_cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("Codex returned an invalid history cursor")
    return cursor


def _transform_signature(manager, held: bool, held_callbacks, platform: str) -> tuple:
    """Detect profile plugin reloads so cached display rows are rebuilt with current filters."""
    callbacks = held_callbacks
    lock = getattr(manager, "_discovery_lock", None)
    hooks = getattr(manager, "_hooks", None)
    if not held and isinstance(hooks, dict):
        if lock is None:
            callbacks = tuple(hooks.get("transform_llm_output", ()))
        else:
            with lock:
                callbacks = tuple(hooks.get("transform_llm_output", ()))
    return held, tuple(callbacks), id(manager) if not callbacks else None, platform


def _hide_assistant_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        if row.get("role") == "assistant":
            row["display_kind"] = "hidden"
            row["content"] = None
            row.pop("reasoning", None)
            row.pop("reasoning_content", None)
    return rows


def _unknown_policy_rows(rows: list[dict]) -> list[dict]:
    has_assistant_output = any(row.get("role") == "assistant" and
                               (row.get("content") or row.get("reasoning") or row.get("reasoning_content"))
                               for row in rows)
    _hide_assistant_rows(rows)
    if has_assistant_output:
        rows.append({
            "role": "system",
            "content": "Earlier Codex output is hidden because its saved display policy is unavailable.",
            "display_kind": "codex_history_unavailable",
            "codex_native": True,
        })
    return rows


def _overlay_display_rows(record: dict, overlay: dict | None, *, required: bool) -> list[dict]:
    rows = deepcopy(record["rows"])
    if not isinstance(overlay, dict):
        return _unknown_policy_rows(rows) if required else rows
    final_text = overlay.get("final_text")
    native_final = record.get("final_text")
    final_row = next((row for row in reversed(rows)
                      if row.get("role") == "assistant" and row.get("content") == native_final), None) \
        if isinstance(native_final, str) and native_final else None
    if overlay.get("held") is True:
        for row in rows:
            if row.get("role") == "assistant" and row is not final_row:
                row["display_kind"] = "hidden"
                row["content"] = None
                row.pop("reasoning", None)
                row.pop("reasoning_content", None)
    if final_row is not None:
        if overlay.get("final_visible") is True and isinstance(final_text, str):
            final_row["content"] = final_text
        else:
            final_row["display_kind"] = "hidden"
            final_row["content"] = None
            final_row.pop("reasoning", None)
            final_row.pop("reasoning_content", None)
    return rows


def _hide_held_interim_rows(rows: list[dict], final_row: dict | None) -> None:
    for row in rows:
        if row.get("role") == "assistant" and row.get("content") and row is not final_row:
            row["display_kind"] = "hidden"
            row["content"] = None
            row.pop("reasoning", None)


def _rewrite_native_final(final_row: dict, final_text: str, *, held: bool, callbacks, manager,
                          session_id: str, thread_id: str, turn_id: str,
                          model: str | None, platform: str) -> None:
    from agent.turn_finalizer import HELD_TRANSFORM_FAILED_REPLY

    original = _safe_native_text(final_text)
    payload = {
        "response_text": original,
        "session_id": session_id,
        "model": model or "codex-native",
        "platform": platform,
        "turn_id": f"codex-native:{thread_id}:{turn_id}",
    }
    if held:
        rewrite, failed = manager.run_held_transforms(callbacks, **payload) if callbacks else (None, True)
    else:
        rewrite = next((value for value in manager.invoke_hook("transform_llm_output", **payload)
                        if isinstance(value, str) and value), None)
        failed = False
    final_row["content"] = (HELD_TRANSFORM_FAILED_REPLY if failed else
                            _safe_native_text(rewrite) if isinstance(rewrite, str) and rewrite else original)


def _redact_projected_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        if row.get("role") in {"user", "assistant"}:
            for key in ("content", "reasoning"):
                value = row.get(key)
                if isinstance(value, str):
                    row[key] = _safe_native_text(value)
    return rows


def _current_policy_rows(record: dict, *, manager, held: bool, callbacks, session_id: str,
                         thread_id: str, model: str | None, platform: str) -> list[dict]:
    rows = deepcopy(record["rows"])
    final_text = record.get("final_text")
    final_row = next((row for row in reversed(rows)
                      if row.get("role") == "assistant" and row.get("content") == final_text), None) \
        if isinstance(final_text, str) and final_text else None
    if held:
        _hide_held_interim_rows(rows, final_row)
    status = record.get("status")
    interrupted = isinstance(status, str) and ("interrupt" in status.lower() or "cancel" in status.lower())
    if final_row is not None and (held or not interrupted):
        _rewrite_native_final(final_row, final_text, held=held, callbacks=callbacks, manager=manager,
                              session_id=session_id, thread_id=thread_id, turn_id=record["id"],
                              model=model, platform=platform)
    return _redact_projected_rows(rows)


def _display_record_signature(overlay, require_overlay, manager, held, callbacks, platform):
    if overlay is not None or require_overlay:
        return "settled", json.dumps(overlay, sort_keys=True, ensure_ascii=False)
    return _transform_signature(manager, held, callbacks, platform)


def _cached_display_rows(record: dict, *, overlay, require_overlay, manager, held, callbacks,
                         session_id: str, thread_id: str, model: str | None, platform: str) -> list[dict]:
    signature = _display_record_signature(overlay, require_overlay, manager, held, callbacks, platform)
    cached = record.get("_display_rows")
    if record.get("_display_signature") == signature and isinstance(cached, list):
        return cached
    rows = (_overlay_display_rows(record, overlay, required=require_overlay)
            if overlay is not None or require_overlay else
            _current_policy_rows(record, manager=manager, held=held, callbacks=callbacks,
                                 session_id=session_id, thread_id=thread_id, model=model, platform=platform))
    _redact_projected_rows(rows)
    record["_display_rows"], record["_display_signature"] = rows, signature
    return rows


def _validate_projected_turn(record) -> None:
    if (not isinstance(record, dict) or not isinstance(record.get("id"), str)
            or not record["id"] or len(record["id"]) > 256 or not isinstance(record.get("rows"), list)):
        raise ValueError("Codex returned an invalid projected history turn")


def _safe_display_record(record: dict, rows: list[dict], max_bytes: int) -> dict:
    safe = {"id": record["id"],
            "status": record.get("status") if isinstance(record.get("status"), str) else None,
            "rows": deepcopy(rows)}
    if len(json.dumps(safe, ensure_ascii=False, default=str).encode("utf-8")) > max_bytes:
        raise ValueError("Codex history exceeded the safe display byte limit")
    return safe


def _safe_native_text(value: str) -> str:
    """Redact and cap native text before it reaches any linked-history display surface."""
    try:
        from agent.redact import redact_sensitive_text
        value = redact_sensitive_text(value, force=True)
    except Exception:  # health: allow BLE001 -- redaction failures must fail closed before display
        return "[content hidden because redaction failed]"
    if not isinstance(value, str):
        return "[content hidden because redaction failed]"
    maximum = 256 * 1024
    if len(value) > maximum:
        suffix = "\n[truncated by Hermes display limit]"
        value = value[:maximum - len(suffix)] + suffix
    return value


def display_native_turn_records(records: list[dict], *, session_id: str, thread_id: str,
                                model: str | None, max_bytes: int = 8 * 1024 * 1024,
                                overlays: dict[str, dict] | None = None, require_overlay: bool = False,
                                platform: str = "") -> list[dict]:
    """Apply profile output filters and redaction once per native turn, then return safe records.

    Held-stream profiles hide interim assistant text and run their held transforms on the final
    response. Other profiles retain redacted commentary and transform only the final response.
    A session identity is required so hooks receive the same stable per-turn context as live use.
    """
    if not isinstance(session_id, str) or not session_id or not isinstance(thread_id, str) or not thread_id:
        raise ValueError("Linked Codex display requires its owning session and thread")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
        raise ValueError("Invalid Codex display byte limit")
    if model is not None and (not isinstance(model, str) or len(model) > 256):
        model = None

    overlays = overlays if isinstance(overlays, dict) else {}
    needs_current_policy = not require_overlay and any(record.get("id") not in overlays for record in records
                                                        if isinstance(record, dict))
    manager = None
    held, callbacks = False, ()
    if needs_current_policy:
        from hermes_cli.plugins import get_plugin_manager
        manager = get_plugin_manager()
        held = manager.holds_stream()
        callbacks = manager.held_transforms() if held else ()
    safe_records = []
    total_bytes = 0
    for record in records:
        _validate_projected_turn(record)
        overlay = overlays.get(record["id"])
        rows = _cached_display_rows(
            record, overlay=overlay, require_overlay=require_overlay, manager=manager,
            held=held, callbacks=callbacks, session_id=session_id, thread_id=thread_id,
            model=model, platform=platform)
        safe_record = _safe_display_record(record, rows, max_bytes - total_bytes)
        total_bytes += len(json.dumps(safe_record, ensure_ascii=False, default=str).encode("utf-8"))
        safe_records.append(safe_record)
    return safe_records


def display_native_turns(records: list[dict], *, session_id: str, thread_id: str,
                         model: str | None, max_bytes: int = 8 * 1024 * 1024,
                         overlays: dict[str, dict] | None = None, require_overlay: bool = False,
                         platform: str = "") -> list[dict]:
    """Flatten safely filtered native turns into the ordinary Hermes transcript row shape."""
    safe_records = display_native_turn_records(records, session_id=session_id, thread_id=thread_id,
                                               model=model, max_bytes=max_bytes, overlays=overlays,
                                               require_overlay=require_overlay, platform=platform)
    return [row for record in safe_records for row in record["rows"]]


def persist_linked_turn_display(db, *, session_id: str, thread_id: str, turn_id: str,
                                final_text: str | None, held: bool, platform: str) -> bool:
    """Store the redacted settled output and hold decision before a linked turn is reloaded."""
    return db.sessions.save_linked_codex_display_overlay(
        session_id, thread_id, turn_id, final_text=final_text, held=held, platform=platform)


def linked_turn_display_overlays(
    db, session_id: str, thread_id: str, turn_ids: list[str],
) -> dict[str, dict]:
    return db.sessions.load_linked_codex_display_overlays(session_id, thread_id, turn_ids)


def read_display_turns(thread_id: str, *, client=None, max_turns: int = 2_000,
                       max_bytes: int = 8 * 1024 * 1024,
                       sort_direction: str = "asc") -> list[dict]:
    """Read a bounded native trace while preserving turn boundaries for display policy.

    Output transforms and held-stream hiding are turn-scoped in Hermes. Keeping each native turn
    grouped lets stored Codex history follow the same rule without guessing which assistant row is
    final after the history has been flattened.
    """
    from hermes_cli.codex_linked_threads import _codex_client, read_codex_history

    if (type(max_turns) is not int or not 1 <= max_turns <= 10_000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024
            or sort_direction not in {"asc", "desc"}):
        raise ValueError("Invalid Codex history bounds")
    context = _codex_client() if client is None else nullcontext(client)
    records, cursor, seen, total_bytes, page_count = [], None, set(), 0, 0
    # Codex pages at most 100 turns. Include one empty terminal page for the exact-limit case,
    # but do not let a broken server spin forever by returning an empty page and fresh cursor.
    max_pages = (max_turns + 99) // 100 + 1
    with context as active_client:
        while True:
            page_count += 1
            if page_count > max_pages:
                raise ValueError("Codex history exceeded the safe native page limit")
            page = read_codex_history(thread_id, cursor=cursor, limit=100,
                                      sort_direction=sort_direction, client=active_client)
            turns = page.get("turns")
            if not isinstance(turns, list):
                raise ValueError("Codex returned an invalid history page")
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
                    raise ValueError("Codex returned an invalid history turn")
                if len(records) >= max_turns:
                    raise ValueError("Codex history exceeded the safe native turn limit")
                turn_id = turn.get("id")
                if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
                    raise ValueError("Codex returned a native turn without a stable ID")
                record = native_turn_record(turn)
                try:
                    total_bytes += len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))
                except (TypeError, ValueError):
                    raise ValueError("Codex returned an invalid history turn") from None
                if total_bytes > max_bytes:
                    raise ValueError("Codex history exceeded the safe display byte limit")
                records.append(record)
            cursor = _next_cursor(page)
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Codex returned an invalid history cursor")
            seen.add(cursor)
    if sort_direction == "desc":
        records.reverse()
    return records


def _record_bytes(record) -> int:
    return len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))


def _record_string_refs(node):
    """Yield (container, key) for every string leaf of a record, in walk order."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                yield node, key
            else:
                yield from _record_string_refs(value)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, str):
                yield node, index
            else:
                yield from _record_string_refs(value)


def truncate_record_tail(record: dict, max_bytes: int, keep: str = "tail") -> dict:
    """Deep-copy a projected turn and shrink it to fit ``max_bytes``, preserving one end.

    ``keep="tail"`` (latest/newest reads) cuts characters from the FRONT of the largest
    string payloads so the tail of the text survives; ``keep="head"`` (oldest reads) cuts
    from the END. Cuts are guided by the serialized byte overflow: every character encodes
    to at least one UTF-8 byte, so removing ``overflow`` characters always frees at least
    ``overflow`` bytes. Strings are cut largest-first until the record fits; if even empty
    strings cannot fit the budget, a minimal stub record is returned instead of raising.
    """
    if keep not in {"tail", "head"}:
        raise ValueError("Invalid truncation direction")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Invalid truncation byte limit")
    truncated = deepcopy(record)
    original_bytes = _record_bytes(truncated)
    if original_bytes <= max_bytes:
        return truncated
    # Stamp the flags before cutting so their own bytes count against the budget.
    truncated["truncated"] = True
    truncated["truncated_from_bytes"] = original_bytes
    rows = truncated.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                row["truncated"] = True
    refs = sorted(_record_string_refs(truncated),
                  key=lambda ref: len(ref[0][ref[1]].encode("utf-8")), reverse=True)
    size = _record_bytes(truncated)
    for container, key in refs:
        if size <= max_bytes:
            break
        value = container[key]
        cut = min(size - max_bytes, len(value))
        container[key] = value[cut:] if keep == "tail" else value[:len(value) - cut]
        size = _record_bytes(truncated)
    if size > max_bytes:
        return {"id": record.get("id"),
                "status": record.get("status") if isinstance(record.get("status"), str) else None,
                "final_text": None, "rows": [], "truncated": True,
                "truncated_from_bytes": original_bytes}
    return truncated


def read_latest_display_turns(thread_id: str, *, client=None, max_turns: int = 2_000,
                              max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """Read the most recent native turns that fit the display budget.

    Unlike :func:`read_display_turns` (which fails closed past its bounds), this pages
    newest-first and stops at the budget, so an oversized native trace still yields its
    latest turns for display instead of an empty transcript. A single newest turn larger
    than the whole budget is tail-truncated to fit rather than dropped. Returned in
    ascending order.
    """
    from hermes_cli.codex_linked_threads import _codex_client, read_codex_history

    if (type(max_turns) is not int or not 1 <= max_turns <= 10_000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024):
        raise ValueError("Invalid Codex history bounds")
    context = _codex_client() if client is None else nullcontext(client)
    records, cursor, seen, total_bytes, page_count = [], None, set(), 0, 0
    max_pages = (max_turns + 99) // 100 + 1
    with context as active_client:
        while True:
            page_count += 1
            if page_count > max_pages:
                break
            page = read_codex_history(thread_id, cursor=cursor, limit=100,
                                      sort_direction="desc", client=active_client)
            turns = page.get("turns")
            if not isinstance(turns, list):
                raise ValueError("Codex returned an invalid history page")
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
                    raise ValueError("Codex returned an invalid history turn")
                if len(records) >= max_turns:
                    records.reverse()
                    return records
                turn_id = turn.get("id")
                if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
                    raise ValueError("Codex returned a native turn without a stable ID")
                record = native_turn_record(turn)
                try:
                    size = len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))
                except (TypeError, ValueError):
                    raise ValueError("Codex returned an invalid history turn") from None
                if total_bytes + size > max_bytes:
                    if not records:
                        # A single turn larger than the whole budget must not blank the
                        # transcript: keep its tail within the full budget instead of
                        # dropping the turn (older content stays reachable via window paging).
                        record = truncate_record_tail(record, max_bytes, keep="tail")
                        total_bytes += _record_bytes(record)
                        records.append(record)
                    records.reverse()
                    return records
                total_bytes += size
                records.append(record)
            cursor = _next_cursor(page)
            if cursor is None or not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
    records.reverse()
    return records

def linked_thread_id(store, session_id: str) -> str | None:
    """Return the native identity, rejecting a damaged linked-session record.

    Tolerates duck-typed store doubles: ``SessionDB`` exposes ``get_session`` (a dict row whose
    ``model_config`` may be a JSON string), while the persistence handle exposes the ``sessions``
    repository (a record whose ``model_config`` is already a dict). A store that cannot be read at
    all yields ``None`` — a linked pointer row cannot exist without a readable row, and
    store-unavailable cases are refused loudly downstream.

    A row that DOES carry a configuration this reader cannot parse or validate is refused
    (:class:`~hermes_cli.foreign_linked_store.LinkedConfigUnreadable`, a ``ValueError``) instead of
    answering ``None``: "not linked" is the dangerous answer, because it detaches the row from its
    native thread and makes it look like a freely writable Hermes session.

    Raises:
        LinkedConfigUnreadable: the row's ``model_config`` exists but is unusable.
        ValueError: the row is flagged as a linked Codex thread but records no thread id.
    """
    from hermes_cli.foreign_linked_store import row_model_config

    config = row_model_config(store, session_id)
    if config.get("codex_linked_thread") is not True:
        return None
    thread_id = config.get("codex_thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("Linked Codex session has no thread ID")
    return thread_id


def display_native_turn_page(thread_id: str, *, session_id: str, model: str | None,
                             cursor: str | None = None, limit: int = 50, client=None,
                             max_bytes: int = 2 * 1024 * 1024, session_db=None,
                             display_overlays: dict[str, dict] | None = None,
                             platform: str = "") -> dict:
    """Read and safely project one native cursor page for the profile-scoped history RPC."""
    from hermes_cli.codex_linked_threads import _codex_client, read_codex_history

    if (not isinstance(session_id, str) or not session_id or type(limit) is not int
            or not 1 <= limit <= 100):
        raise ValueError("Invalid linked Codex history request")
    context = _codex_client() if client is None else nullcontext(client)
    with context as active_client:
        page = read_codex_history(thread_id, cursor=cursor, limit=limit,
                                  sort_direction="asc", client=active_client)
    turns = page.get("turns")
    if not isinstance(turns, list):
        raise ValueError("Codex returned an invalid history page")
    records = [native_turn_record(turn) for turn in turns]
    overlays = display_overlays
    if overlays is None and session_db is not None:
        overlays = linked_turn_display_overlays(
            session_db, session_id, thread_id, [record["id"] for record in records])
    safe_turns = display_native_turn_records(records, session_id=session_id, thread_id=thread_id,
                                             model=model, max_bytes=max_bytes, overlays=overlays,
                                             require_overlay=True, platform=platform)
    return {"turns": safe_turns, "next_cursor": _next_cursor(page)}


def read_display_history(thread_id: str, *, session_id: str, model: str | None = None,
                         limit: int | None = None, offset: int = 0, latest: bool = False,
                         client=None, max_turns: int = 2_000,
                         max_bytes: int = 8 * 1024 * 1024, session_db=None,
                         display_overlays: dict[str, dict] | None = None,
                         platform: str = "") -> list[dict]:
    """Read bounded native history and apply the owning profile's display policy.

    The session identity is mandatory. Only a settled Hermes display overlay can authorize assistant
    output; turns created before overlays existed fail closed instead of replaying unknown policy.
    Linked history is never returned as raw Codex items.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Linked Codex history requires its owning session")
    if (type(offset) is not int or offset < 0 or not isinstance(latest, bool)
            or (limit is not None and (type(limit) is not int or limit < 0))):
        raise ValueError("Invalid linked Codex history page")
    if limit == 0:
        return []
    records = read_display_turns(thread_id, client=client, max_turns=max_turns,
                                 max_bytes=max_bytes, sort_direction="asc")
    overlays = display_overlays
    if overlays is None and session_db is not None:
        overlays = linked_turn_display_overlays(
            session_db, session_id, thread_id, [record["id"] for record in records])
    rows = display_native_turns(records, session_id=session_id, thread_id=thread_id,
                                model=model, max_bytes=max_bytes, overlays=overlays,
                                require_overlay=True, platform=platform)
    if latest:
        end = len(rows) - offset
        start = max(0, end - limit) if limit is not None else 0
        return rows[start:max(0, end)]
    return rows[offset:] if limit is None else rows[offset:offset + limit]
