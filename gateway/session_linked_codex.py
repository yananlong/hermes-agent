"""Native history helpers for Hermes rows that point at an existing Codex thread.

The local session row is only a locator. Codex remains the authority for its turns, so
owner reads must not quietly return the empty/local mirror when the native read fails.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from pathlib import Path

from persistence.core.errors import AuthorityRefusal

# Native trace pages carry at most this many turns (``read_codex_history(..., limit=...)``),
# and one turn can project to any number of display rows — or none — so windowed reads count
# rows and turns in separate units.
_NATIVE_PAGE_TURNS = 100
# Hard turn ceiling for one windowed read, matching ``read_display_turns``' 10_000-turn cap.
_MAX_WINDOW_TURNS = 10_000

_CURSOR_VERSION = "codex-transcript-v1"
_CURSOR_MAX_CHARS = 2048
_INT_MAX = 2**63
_MAX_SUBAGENT_ANCESTRY = 16
_MAX_PARENT_HISTORY_PAGES = 100
_MAX_TRACE_REFRESH_PAGES = 16
_TRACE_CACHE_TTL_SECONDS = 120
_TRACE_CACHE_MAX_ENTRIES = 8
_TRACE_CACHE_MAX_BYTES = 24 * 1024 * 1024
_TRACE_CACHE_LOCK = threading.RLock()
_TRACE_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_REFERENCE_CACHE: OrderedDict[tuple, float] = OrderedDict()
_REFERENCE_CACHE_TTL_SECONDS = 10
_REFERENCE_CACHE_MAX_ENTRIES = 512


def linked_thread_id(store, session_id: str) -> str | None:
    """Return the thread pointer for a linked row, validating the flag/id pair."""
    from hermes_cli.codex_linked_history import linked_thread_id as stored_thread_id

    try:
        return stored_thread_id(store, session_id)
    except ValueError as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="linked Codex thread identity is invalid") from exc


def session_thread_id(store, session_id: str) -> str | None:
    """Return a native app-server thread owned by this session, linked or started by Hermes."""
    row = store.sessions.get(session_id)
    if row is None:
        return None
    config = row.model_config
    linked = config.get("codex_linked_thread") is True
    if not linked and config.get("api_mode") != "codex_app_server":
        return None
    thread_id = config.get("codex_thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        if linked:
            raise AuthorityRefusal("storage_unavailable", field="session_id",
                                    detail="linked Codex thread identity is invalid")
        return None
    from hermes_cli.codex_linked_threads import _thread_id
    try:
        return _thread_id(thread_id)
    except ValueError as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="Codex thread identity is invalid") from exc


def linked_session_cwd(store, session_id: str | None) -> str | None:
    """The linked thread's persisted workspace, or None for an ordinary Hermes session.

    Native Codex resumes are bound to both a thread ID and its original cwd. The linked row's cwd
    was captured from ``thread/read`` when it was linked; fail closed if that durable identity is
    damaged instead of borrowing the gateway process cwd.
    """
    if store is None or not session_id:
        return None
    thread_id = linked_thread_id(store, session_id)
    if thread_id is None:
        return None
    row = store.sessions.get(session_id)
    cwd = row.cwd if row is not None else None
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise RuntimeError("Linked Codex session has no available original working directory")
    return cwd


def read_display_history(thread_id: str, *, session_id: str, model: str | None = None,
                         profile_home=None, client=None, limit: int | None = None,
                         offset: int = 0, latest: bool = False, session_db=None,
                         display_overlays: dict[str, dict] | None = None,
                         platform: str = "") -> list[dict]:
    """Read safely projected linked history through a bounded, profile-owned snapshot cache."""
    from agent.transports.codex_app_server import CodexAppServerError
    from hermes_cli.codex_linked_history import display_native_turns
    from hermes_cli.codex_linked_threads import _codex_client
    from hermes_constants import get_hermes_home, hermes_home_key

    if (not isinstance(session_id, str) or not session_id or type(offset) is not int or offset < 0
            or (limit is not None and (type(limit) is not int or limit < 0))
            or not isinstance(latest, bool)):
        raise AuthorityRefusal("invalid_params", field="session_id")
    if limit == 0:
        return []
    owning_home = profile_home if profile_home is not None else get_hermes_home()
    try:
        cache_profile = hermes_home_key(owning_home)
        with (_codex_client(profile_home=owning_home) if client is None else nullcontext(client)) as active_client:
            cache_key = (str(cache_profile), session_id, thread_id, thread_id, model or "")
            records = _read_trace_turns(active_client, thread_id, cache_key, running=True)
            overlays = display_overlays
            if overlays is None and session_db is not None:
                from hermes_cli.codex_linked_history import linked_turn_display_overlays
                overlays = linked_turn_display_overlays(
                    session_db, session_id, thread_id, [record["id"] for record in records])
            rows = display_native_turns(records, session_id=session_id, thread_id=thread_id,
                                        model=model, max_bytes=32 * 1024 * 1024, overlays=overlays,
                                        require_overlay=True, platform=platform)
            _trace_cache_save_display(cache_key, records)
    except (CodexAppServerError, OSError, TimeoutError, ValueError) as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="linked Codex history is unavailable") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="Codex returned invalid linked history")
    if latest:
        end = len(rows) - offset
        start = max(0, end - limit) if limit is not None else 0
        return rows[start:max(0, end)]
    return rows[offset:] if limit is None else rows[offset:offset + limit]


def _page_records(turns, *, collected: int, total_bytes: int, max_bytes: int,
                  latest: bool) -> tuple[list, int, bool]:
    """One native page as display records under the byte budget.

    Returns ``(records, total_bytes, budget_hit)``. A turn that would cross the budget stops
    the page (``budget_hit``); when nothing has been collected yet, that one oversized turn
    is truncated (tail for ``latest``, head otherwise) inside the full budget instead of
    blanking the window — its stub (budget below record overhead) projects to no rows.
    """
    from hermes_cli.codex_linked_history import native_turn_record, truncate_record_tail

    records: list = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
            raise ValueError("Codex returned an invalid history turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
            raise ValueError("Codex returned a native turn without a stable ID")
        record = native_turn_record(turn)
        try:
            size = len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            raise ValueError("Codex returned an invalid history turn") from None
        if total_bytes + size > max_bytes:
            if collected == 0 and not records:
                record = truncate_record_tail(record, max_bytes - total_bytes,
                                              keep="tail" if latest else "head")
                if record.get("rows"):
                    total_bytes += len(json.dumps(record, ensure_ascii=False,
                                                  default=str).encode("utf-8"))
                    records.append(record)
            return records, total_bytes, True
        total_bytes += size
        records.append(record)
    return records, total_bytes, False


def read_display_history_window(thread_id: str, *, session_id: str, model: str | None = None,
                                profile_home=None, client=None, limit: int = 500, offset: int = 0,
                                latest: bool = True, session_db=None, platform: str = "",
                                max_bytes: int = 8 * 1024 * 1024) -> list[dict]:
    """Page the native trace itself so any window of an oversized thread can load.

    Unlike :func:`read_display_history` (which materializes one bounded snapshot), this walks
    native cursor pages in the requested direction, skips ``offset`` turns, collects up to
    ``limit`` turns under a per-request byte budget, then applies display policy. ``latest``
    reads newest-first and returns the window in ascending order, so the whole trace is
    reachable page by page instead of failing closed past the full-read budget. A single
    first turn larger than the whole budget is truncated (tail for ``latest``, head
    otherwise) instead of yielding an empty window.
    """
    from agent.transports.codex_app_server import CodexAppServerError
    from hermes_cli.codex_linked_history import display_native_turns, linked_turn_display_overlays, _next_cursor
    from hermes_cli.codex_linked_threads import _codex_client, read_codex_history
    from hermes_constants import get_hermes_home

    if (not isinstance(session_id, str) or not session_id or type(offset) is not int or offset < 0
            or type(limit) is not int or limit < 0 or not isinstance(latest, bool)
            or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024):
        raise AuthorityRefusal("invalid_params", field="session_id")
    if limit == 0:
        return []
    owning_home = profile_home if profile_home is not None else get_hermes_home()
    direction = "desc" if latest else "asc"
    try:
        with (_codex_client(profile_home=owning_home) if client is None else nullcontext(client)) as active_client:
            pages, cursor, seen = [], None, set()
            skipped, collected, total_bytes = 0, 0, 0
            turns_seen, page_count, budget_hit = 0, 0, False
            # Row/turn accounting: ``skipped``/``collected`` count display ROWS and bound the
            # window (``collected < limit`` above plus the final ``rows[-limit:]`` slice), while
            # ``turns_seen`` counts native TURNS. One page carries up to ``_NATIVE_PAGE_TURNS``
            # turns but a turn may project to several rows (or none), so the page cap is derived
            # from the turn-space budget, never from the row window — deriving it from
            # ``limit + offset`` would miscount turns as rows and cut the window short on
            # traces with row-less turns.
            max_pages = (_MAX_WINDOW_TURNS + _NATIVE_PAGE_TURNS - 1) // _NATIVE_PAGE_TURNS + 1
            while collected < limit and not budget_hit:
                page_count += 1
                if page_count > max_pages or turns_seen >= _MAX_WINDOW_TURNS:
                    break
                page = read_codex_history(thread_id, cursor=cursor, limit=_NATIVE_PAGE_TURNS,
                                          sort_direction=direction, client=active_client)
                turns = page.get("turns")
                if not isinstance(turns, list):
                    raise ValueError("Codex returned an invalid history page")
                turns_seen += len(turns)
                records, total_bytes, budget_hit = _page_records(
                    turns, collected=collected, total_bytes=total_bytes, max_bytes=max_bytes,
                    latest=latest)
                if records:
                    if direction == "desc":
                        records.reverse()  # keep rows ascending within each page
                    overlays = None
                    if session_db is not None:
                        overlays = linked_turn_display_overlays(
                            session_db, session_id, thread_id, [record["id"] for record in records])
                    page_rows = display_native_turns(records, session_id=session_id, thread_id=thread_id,
                                                     model=model, max_bytes=max_bytes, overlays=overlays,
                                                     require_overlay=True, platform=platform)
                    if skipped < offset and page_rows:
                        # offset is row-space: for latest it skips the newest rows (the tail
                        # of the ascending page), otherwise the oldest (the head).
                        cut = min(offset - skipped, len(page_rows))
                        page_rows = page_rows[:len(page_rows) - cut] if latest else page_rows[cut:]
                        skipped += cut
                    if page_rows:
                        # For latest, pages stay newest-first so the final reversal yields
                        # ascending rows and rows[-limit:] is the newest window.
                        pages.append(page_rows)
                        collected += len(page_rows)
                cursor = _next_cursor(page)
                if cursor is None or not isinstance(cursor, str) or cursor in seen:
                    break
                seen.add(cursor)
            ordered = pages[::-1] if latest else pages
            rows = [row for page_rows in ordered for row in page_rows]
            return rows[-limit:] if latest else rows[:limit]
    except (CodexAppServerError, OSError, TimeoutError, ValueError) as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="linked Codex history is unavailable") from exc


def _history_digest(rows, end: int) -> str:
    encoded = json.dumps(rows[:end], ensure_ascii=False, sort_keys=True, default=str,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_trace_bytes(turns: list[dict]) -> int:
    return len(json.dumps(turns, ensure_ascii=False, sort_keys=True, default=str,
                          separators=(",", ":")).encode("utf-8"))


def _trace_cache_get(key: tuple) -> dict | None:
    now = time.monotonic()
    with _TRACE_CACHE_LOCK:
        entry = _TRACE_CACHE.get(key)
        if entry is None:
            return None
        if now - entry["updated"] > _TRACE_CACHE_TTL_SECONDS:
            _TRACE_CACHE.pop(key, None)
            return None
        _TRACE_CACHE.move_to_end(key)
        result = copy.deepcopy(entry)
        result.pop("updated", None)
        return result


def _trace_cache_put(key: tuple, entry: dict) -> None:
    try:
        size = _native_trace_bytes(entry["turns"])
    except (TypeError, ValueError):
        return
    if size > _TRACE_CACHE_MAX_BYTES:
        return
    stored = copy.deepcopy(entry)
    stored["size"] = size
    stored["updated"] = time.monotonic()
    with _TRACE_CACHE_LOCK:
        _TRACE_CACHE.pop(key, None)
        _TRACE_CACHE[key] = stored
        total = sum(row.get("size", 0) for row in _TRACE_CACHE.values())
        while _TRACE_CACHE and (len(_TRACE_CACHE) > _TRACE_CACHE_MAX_ENTRIES or total > _TRACE_CACHE_MAX_BYTES):
            _, removed = _TRACE_CACHE.popitem(last=False)
            total -= removed.get("size", 0)


def _trace_cache_save_display(key: tuple, turns: list[dict]) -> None:
    with _TRACE_CACHE_LOCK:
        current = _TRACE_CACHE.get(key)
        full_refreshed = current.get("full_refreshed", 0) if current else 0
    _trace_cache_put(key, {"turns": turns, "full_refreshed": full_refreshed})


def _reference_cache_has(key: tuple) -> bool:
    now = time.monotonic()
    with _TRACE_CACHE_LOCK:
        expires = _REFERENCE_CACHE.get(key)
        if expires is None:
            return False
        if expires <= now:
            _REFERENCE_CACHE.pop(key, None)
            return False
        _REFERENCE_CACHE.move_to_end(key)
        return True


def _reference_cache_put(key: tuple) -> None:
    with _TRACE_CACHE_LOCK:
        _REFERENCE_CACHE[key] = time.monotonic() + _REFERENCE_CACHE_TTL_SECONDS
        _REFERENCE_CACHE.move_to_end(key)
        while len(_REFERENCE_CACHE) > _REFERENCE_CACHE_MAX_ENTRIES:
            _REFERENCE_CACHE.popitem(last=False)


def _decode_cursor(cursor: str, *, session_id: str, thread_id: str, order: str) -> int:
    from binascii import Error as Base64Error

    try:
        if len(cursor) > _CURSOR_MAX_CHARS:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        version, saved_session, saved_thread, saved_order, index, digest = value
    except (TypeError, ValueError, Base64Error):
        raise AuthorityRefusal("invalid_params", field="cursor") from None
    if ((version, saved_session, saved_thread, saved_order) !=
            (_CURSOR_VERSION, session_id, thread_id, order)
            or type(index) is not int or not 0 <= index < _INT_MAX
            or not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)):
        raise AuthorityRefusal("invalid_params", field="cursor")
    return index


def _encode_cursor(session_id: str, thread_id: str, order: str, index: int, digest: str) -> str:
    value = [_CURSOR_VERSION, session_id, thread_id, order, index, digest]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _display_native_turns(turns: list[dict], *, session_id: str, thread_id: str,
                          model: str | None, platform: str = "") -> list[dict]:
    """Apply the shared linked-history privacy policy and flatten for existing transcript clients."""
    from hermes_cli.codex_linked_history import display_native_turns

    return display_native_turns(turns, session_id=session_id, thread_id=thread_id, model=model,
                                platform=platform)


def transcript_page(connection, session_id: str, thread_id: str, *, cursor: str | None, limit: int,
                    order: str, inline_images: bool, history: list[dict] | None = None,
                    output_model: str | None = None, owner_session_id: str | None = None) -> dict:
    """Page a linked native history using a prefix digest as its durable cursor boundary.

    New turns do not shift a cursor: oldest cursors continue after the rows already read, and
    latest cursors continue before their original boundary. If an earlier part of the native
    history changes, the cursor resets to the relevant edge.
    """
    from gateway.session_stored_reads import PAGE_BYTES, _bounded, _size
    from gateway.session_controls import display_projection

    if history is None:
        owner_session_id = owner_session_id or session_id
        from gateway.session_persistence import authority_persistence
        store = authority_persistence(connection.authority)
        owner_row = store.sessions.get(owner_session_id)
        if output_model is None and owner_row is not None:
            output_model = owner_row.model
        history = read_display_history(
            thread_id, session_id=owner_session_id, model=output_model,
            profile_home=store.scope.profile_dir, session_db=store,
            platform=owner_row.source if owner_row else "")
    rows = display_projection(history, connection.authority.profile_id, image_urls=inline_images)
    if cursor is None:
        boundary = len(rows) if order == "latest" else 0
        reset = True
    else:
        boundary = _decode_cursor(cursor, session_id=session_id, thread_id=thread_id, order=order)
        prior_digest = _history_digest(rows, boundary)
        # A compaction, rewind or rewrite before this cursor changes its prefix. Appends after an
        # oldest cursor, and new turns after a latest cursor, preserve the covered prefix.
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            expected_digest = token[5]
        except (TypeError, ValueError):
            raise AuthorityRefusal("invalid_params", field="cursor") from None
        reset = prior_digest != expected_digest or boundary > len(rows)
        if reset:
            boundary = len(rows) if order == "latest" else 0

    if order == "oldest":
        start, end = boundary, min(len(rows), boundary + limit)
        page = rows[start:end]
        taken = len(page)
        while taken > 1 and _size(page[:taken]) > PAGE_BYTES:
            taken = max(1, taken // 2)
        page = page[:taken]
        if page and _size(page) > PAGE_BYTES:
            page = [_bounded(page[0], PAGE_BYTES - 128)]
        next_boundary = start + len(page)
        next_cursor = _encode_cursor(session_id, thread_id, order, next_boundary,
                                     _history_digest(rows, next_boundary))
    else:
        end, start = boundary, max(0, boundary - limit)
        page = rows[start:end]
        taken = len(page)
        while taken > 1 and _size(page[len(page) - taken:]) > PAGE_BYTES:
            taken = max(1, taken // 2)
        page = page[len(page) - taken:]
        if page and _size(page) > PAGE_BYTES:
            page = [_bounded(page[-1], PAGE_BYTES - 128)]
        next_boundary = end - len(page)
        next_cursor = (_encode_cursor(session_id, thread_id, order, next_boundary,
                                      _history_digest(rows, next_boundary)) if next_boundary else None)
    display_rows = [{**row, "row_kind": "message"} for row in page]
    return {"rows": display_rows,
            "next_cursor": next_cursor, "reset": reset}


def _trace_turn_record(turn: dict) -> dict:
    from hermes_cli.codex_linked_history import native_turn_record

    return native_turn_record(turn)


def _read_all_trace_turns(client, thread_id: str) -> list[dict]:
    from hermes_cli.codex_linked_history import read_display_turns, read_latest_display_turns

    try:
        return read_display_turns(thread_id, client=client, max_turns=10_000, max_bytes=32 * 1024 * 1024)
    except ValueError:
        # An oversized native trace must not blank the transcript: fall back to the most
        # recent turns that fit a smaller display budget (newest-first read, ascending out).
        return read_latest_display_turns(thread_id, client=client, max_turns=10_000,
                                         max_bytes=16 * 1024 * 1024)


def _read_trace_turns(client, thread_id: str, cache_key: tuple, *, running: bool) -> list[dict]:
    """Reuse a bounded snapshot and refresh only recent native turns during live polling."""
    from hermes_cli.codex_linked_history import _next_cursor
    from hermes_cli.codex_linked_threads import read_codex_history

    now = time.monotonic()
    entry = _trace_cache_get(cache_key)
    cached_status = (entry.get("turns", [])[-1].get("status") if entry and entry.get("turns") else None)
    cached_running = isinstance(cached_status, str) and cached_status.lower() in {
        "inprogress", "in_progress", "active", "running",
    }
    if entry is None or ((running or cached_running) and now - entry.get("full_refreshed", 0) > 30):
        turns = _read_all_trace_turns(client, thread_id)
        _trace_cache_put(cache_key, {"turns": turns, "full_refreshed": now})
        return turns
    cached_turns = entry.get("turns")
    if not isinstance(cached_turns, list) or not cached_turns:
        turns = _read_all_trace_turns(client, thread_id)
        _trace_cache_put(cache_key, {"turns": turns, "full_refreshed": now})
        return turns

    anchor = cached_turns[-1].get("id")
    cursor, seen, newest_first, found = None, set(), [], False
    for _ in range(_MAX_TRACE_REFRESH_PAGES):
        page = read_codex_history(thread_id, cursor=cursor, limit=100,
                                  sort_direction="desc", client=client)
        raw_turns = page.get("turns")
        if not isinstance(raw_turns, list):
            raise ValueError("Codex returned an invalid history page")
        for raw_turn in raw_turns:
            record = _trace_turn_record(raw_turn)
            newest_first.append(record)
            if record["id"] == anchor:
                found = True
                break
        if found:
            break
        cursor = _next_cursor(page)
        if cursor is None:
            break
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Codex returned an invalid history cursor")
        seen.add(cursor)
    if not found:
        # A long gap, old-history rewrite, or compaction invalidated the cheap tail refresh. A single
        # bounded full reload gives the existing digest cursor a chance to reset correctly.
        turns = _read_all_trace_turns(client, thread_id)
        _trace_cache_put(cache_key, {"turns": turns, "full_refreshed": now})
        return turns

    # The native tail page is newest first. Replace the cached anchor (it may still be active), then
    # append any newer turns in chronological order. Identical anchors keep their cached filter result.
    anchor_record = newest_first[-1]
    current_anchor = cached_turns[-1]
    if anchor_record["id"] == current_anchor.get("id") and anchor_record != {
            key: value for key, value in current_anchor.items()
            if key not in {"_display_rows", "_display_signature"}}:
        cached_turns[-1] = anchor_record
    newer = list(reversed(newest_first[:-1]))
    if newer:
        cached_turns.extend(newer)
    entry["turns"] = cached_turns
    _trace_cache_put(cache_key, entry)
    return cached_turns


def _thread_parent_id(thread: dict) -> str | None:
    parent = thread.get("parentThreadId", thread.get("parent_thread_id"))
    source = thread.get("source")
    source_parent = None
    if isinstance(source, dict):
        subagent = source.get("subAgent", source.get("sub_agent"))
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn", subagent.get("threadSpawn"))
            if isinstance(spawn, dict):
                source_parent = spawn.get("parent_thread_id", spawn.get("parentThreadId"))
    # Both metadata locations must independently describe the same parent. This avoids accepting a
    # partial/malformed child record as ancestry proof.
    if not isinstance(parent, str) or not parent or not isinstance(source_parent, str) or not source_parent:
        return None
    from hermes_cli.codex_linked_threads import _thread_id
    try:
        canonical_parent, canonical_source = _thread_id(parent), _thread_id(source_parent)
    except ValueError:
        return None
    return canonical_parent if canonical_parent == canonical_source else None


def _read_thread(client, thread_id: str) -> dict | None:
    from agent.transports.codex_app_server import CodexAppServerTransportError
    from hermes_cli.codex_linked_threads import _thread_id

    try:
        canonical_id = _thread_id(thread_id)
        response = client.request("thread/read", {"threadId": canonical_id, "includeTurns": False})
    except CodexAppServerTransportError:
        raise
    except Exception:  # health: allow BLE001 -- malformed native metadata must fail closed
        # A stale/deleted child and an unavailable native record both fail closed. Transport loss
        # remains an error so a running reader can retry instead of mistaking it for no trace.
        return None
    thread = response.get("thread") if isinstance(response, dict) else None
    if (not isinstance(thread, dict) or thread.get("id") != canonical_id
            or thread.get("ephemeral") is not False):
        return None
    return thread


def _parent_references_child(client, parent_thread_id: str, child_thread_id: str, *,
                             reference_scope: tuple | None = None) -> bool:
    """Require a parent history item to name the requested child before exposing its transcript."""
    from hermes_cli.codex_linked_history import _next_cursor
    from hermes_cli.codex_linked_threads import read_codex_history

    reference_key = (*reference_scope, parent_thread_id, child_thread_id) if reference_scope else None
    if reference_key is not None and _reference_cache_has(reference_key):
        return True
    cursor, seen = None, set()
    for _ in range(_MAX_PARENT_HISTORY_PAGES):
        page = read_codex_history(parent_thread_id, cursor=cursor, limit=100,
                                  sort_direction="desc", client=client)
        turns = page.get("turns")
        if not isinstance(turns, list):
            raise ValueError("Codex returned an invalid parent history page")
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("items", []), list):
                raise ValueError("Codex returned an invalid parent history turn")
            for item in turn.get("items", []):
                if not isinstance(item, dict):
                    continue
                if (item.get("type") == "subAgentActivity"
                        and item.get("agentThreadId", item.get("agent_thread_id")) == child_thread_id):
                    if reference_key is not None:
                        _reference_cache_put(reference_key)
                    return True
                # V1 collaboration history used receiverThreadIds before subAgentActivity became
                # the canonical visible item.
                if item.get("type") == "collabAgentToolCall":
                    receivers = item.get("receiverThreadIds", item.get("receiver_thread_ids"))
                    if isinstance(receivers, list) and child_thread_id in receivers:
                        if reference_key is not None:
                            _reference_cache_put(reference_key)
                        return True
        cursor = _next_cursor(page)
        if cursor is None:
            return False
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Codex returned an invalid parent history cursor")
        seen.add(cursor)
    return False


def _verified_subagent(client, root_thread_id: str, child_thread_id: str, *,
                       reference_scope: tuple | None = None) -> dict | None:
    """Verify the child is a non-ephemeral descendant of the authorized root and named by its parent."""
    from agent.transports.codex_app_server import CodexAppServerTransportError
    from hermes_cli.codex_linked_threads import _thread_id

    try:
        root = _thread_id(root_thread_id)
        child = _thread_id(child_thread_id)
    except ValueError:
        return None
    if child == root:
        return None
    target = _read_thread(client, child)
    if target is None:
        return None
    parent_id = _thread_parent_id(target)
    if parent_id is None:
        return None
    seen = {child}
    current = parent_id
    for _ in range(_MAX_SUBAGENT_ANCESTRY):
        if current == root:
            break
        if current in seen:
            return None
        seen.add(current)
        try:
            ancestor = _read_thread(client, current)
        except CodexAppServerTransportError:
            raise
        if ancestor is None:
            return None
        current = _thread_parent_id(ancestor)
        if current is None:
            return None
    else:
        return None
    if current != root:
        return None
    if not _parent_references_child(client, parent_id, child, reference_scope=reference_scope):
        return None
    return target


def subagent_transcript_page(connection, session_id: str, root_thread_id: str, child_thread_id: str, *,
                             cursor: str | None, limit: int, order: str, inline_images: bool) -> dict:
    """Read one authorized child trace page without loading or mutating its native thread."""
    from agent.transports.codex_app_server import CodexAppServerError, CodexAppServerTransportError
    from gateway.session_persistence import authority_persistence
    from hermes_cli.codex_linked_threads import _codex_client

    store = authority_persistence(connection.authority)
    profile_home = store.scope.profile_dir
    unavailable = {"rows": [], "next_cursor": None, "reset": True, "available": False, "running": False}
    try:
        with _codex_client(profile_home=profile_home) as client:
            reference_scope = (str(profile_home), session_id, root_thread_id)
            thread = _verified_subagent(client, root_thread_id, child_thread_id,
                                        reference_scope=reference_scope)
            if thread is None:
                return unavailable
            status = thread.get("status")
            running = isinstance(status, dict) and status.get("type") == "active"
            child_model = thread.get("model") if isinstance(thread.get("model"), str) else None
            owner_row = store.sessions.get(session_id)
            cache_key = (*reference_scope, child_thread_id, child_model or "")
            native_turns = _read_trace_turns(client, child_thread_id, cache_key, running=running)
            last_status = native_turns[-1].get("status") if native_turns else None
            if isinstance(last_status, str):
                running = running or last_status.lower() in {"inprogress", "in_progress", "active", "running"}
                if last_status.lower() in {"completed", "interrupted", "failed", "cancelled", "canceled"}:
                    running = False
            history = _display_native_turns(native_turns, session_id=session_id,
                                            thread_id=child_thread_id, model=child_model,
                                            platform=owner_row.source if owner_row else "")
            _trace_cache_save_display(cache_key, native_turns)
            page = transcript_page(connection, session_id, child_thread_id, cursor=cursor, limit=limit,
                                   order=order, inline_images=inline_images, history=history)
    except CodexAppServerTransportError as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="Codex subagent history is unavailable") from exc
    except (CodexAppServerError, OSError, TimeoutError, ValueError) as exc:
        raise AuthorityRefusal("storage_unavailable", field="session_id",
                                detail="Codex subagent history is unavailable") from exc
    return {**page, "available": True, "running": running}
