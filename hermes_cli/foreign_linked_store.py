"""Shared helpers for pointer-style links to foreign CLI session stores.

A linked foreign session (Codex, Claude Code, Mistral Vibe) is a durable pointer only: the Hermes
row stores the native session ID and an empty message list, and history is always read live from
the foreign tool's own on-disk store. A Hermes send APPENDS to that native thread through the
agent's own resume transport (``agent/linked_foreign_turn.py``) and still writes no second
transcript here, so the row's own message list stays empty either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

# model_config flags that mark a Hermes session row as a pointer to a foreign-owned transcript.
LINKED_FOREIGN_FLAGS = ("codex_linked_thread", "claude_linked_thread", "mistral_linked_thread")

MAX_STORE_FILE_BYTES = 32 * 1024 * 1024
MAX_STORE_LINE_BYTES = 2 * 1024 * 1024
# Identity metadata (summary/cwd/model) lives in the first records of a Claude transcript, so a
# listing reads this much of the head instead of the whole file.
MAX_STORE_HEAD_BYTES = 256 * 1024


def foreign_session_uuid(value):
    """Canonicalize a foreign session UUID (Claude sessionId / Vibe session id)."""
    if not isinstance(value, str):
        raise ValueError("A foreign session ID is required")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError("Invalid foreign session ID") from None
    if value != canonical:
        raise ValueError("Invalid foreign session ID")
    return canonical


def page_params(cursor, limit):
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("Invalid page cursor")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid page size")
    return {"cursor": cursor, "limit": limit}


def check_owning_profile(profile_home=None) -> tuple[Path, bool]:
    """Resolve the profile whose foreign store may be read, mirroring the Codex scope rules.

    Returns ``(target_home, routed)``: the profile home the caller may read, and whether a routed
    (multiplexed / secondary) profile is active, which is what forces that profile to name its own
    store instead of leaning on the account-wide default.

    The account-wide ``~/.claude`` / ``~/.vibe`` fallback is a launch-profile default only; a routed
    secondary profile must name its own store via its secret scope, otherwise session IDs could
    cross profile scope.
    """
    from agent.secret_scope import (
        current_secret_scope_home, is_multiplex_active, serves_routed_profile,
    )
    from hermes_constants import get_hermes_home, hermes_home_key

    active_home = Path(get_hermes_home()).expanduser()
    target_home = Path(profile_home).expanduser() if profile_home is not None else active_home
    if not target_home.is_absolute() or not active_home.is_absolute():
        raise ValueError("Cannot resolve foreign history without an absolute owning profile")
    try:
        target_key, active_key = hermes_home_key(target_home), hermes_home_key(active_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Cannot resolve the owning profile for foreign history") from exc
    if target_key != active_key or not target_home.is_dir():
        raise ValueError("Foreign history request is outside the active owning profile")

    routed = is_multiplex_active() or serves_routed_profile()
    if routed:
        scoped_home = current_secret_scope_home()
        if not scoped_home or hermes_home_key(scoped_home) != target_key:
            raise ValueError("Foreign history has no secret scope for the owning profile")
    return target_home, routed


def resolve_foreign_store(env_var, default_parts, subdir, profile_home=None) -> Path:
    """Resolve a foreign session store owned by the active profile.

    *env_var* is the tool's own relocation variable (``CLAUDE_CONFIG_DIR`` for Claude Code);
    *default_parts* is the account-wide fallback under ``~``; *subdir* is appended to either.
    """
    from agent.secret_scope import get_secret
    from hermes_constants import get_process_hermes_home

    target_home, routed = check_owning_profile(profile_home)

    configured = get_secret(env_var)
    if isinstance(configured, str) and configured.strip():
        root = Path(configured).expanduser()
    else:
        try:
            is_launch_profile = target_home == Path(get_process_hermes_home()).expanduser()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("Cannot resolve the launch profile for foreign history") from exc
        if routed and not is_launch_profile:
            raise ValueError(f"The owning profile must configure its own {env_var} for foreign history")
        root = Path.home().joinpath(*default_parts)
    if subdir:
        root = root / subdir
    if not root.is_absolute():
        raise ValueError(f"{env_var} must resolve to an absolute directory")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The owning profile's foreign session store is unavailable") from exc
    if not root.is_dir():
        raise ValueError("The owning profile's foreign session store is unavailable")
    return root


def safe_native_text(value: str) -> str:
    """Redact and cap native foreign text before it reaches any display surface."""
    from hermes_cli.codex_linked_history import _safe_native_text
    return _safe_native_text(value)


class LinkedConfigUnreadable(ValueError):
    """A row carries a ``model_config`` this reader cannot parse or validate.

    A pointer row cannot be told apart from an ordinary Hermes session when its configuration is
    unreadable, and "not linked" is the dangerous answer: it detaches the row from its native
    thread and makes it look freely writable (branch it, rewind it, answer it from a new Hermes
    conversation). Callers therefore refuse instead. Subclasses ``ValueError`` so the gateway's
    existing ``except ValueError`` wrappers (``gateway/session_linked_codex.linked_thread_id`` →
    ``AuthorityRefusal('storage_unavailable')``) keep converting it into a typed refusal.
    """


#: ``model_config`` ``api_mode`` values that mark a legacy pointer row written before the flags
#: existed. Only ever set together with the flag by the link paths (``*_linked_threads.py``), so an
#: api_mode-only row is either that older shape or a damaged row — both must read as linked.
LEGACY_LINKED_ROW_MODES = {
    "claude_linked_readonly": "claude",
    "claude_linked_resume": "claude",
    "mistral_linked_readonly": "mistral",
    "mistral_linked_resume": "mistral",
    "codex_linked_readonly": "codex",
}


def store_row_reader(store) -> bool:
    """Whether this store exposes any way to read one session row.

    Both shapes in the tree are tolerated (the persistence handle's ``sessions.get``, and
    ``SessionDB.get_session``). A handle that cannot even be inspected answers ``False`` rather
    than raising: a store nobody can read is a store whose pointer status is unknown, and callers
    treat that as "not provably ordinary" (``agent/linked_foreign_turn.py``).
    """
    try:
        return (callable(getattr(getattr(store, "sessions", None), "get", None))
                or callable(getattr(store, "get_session", None)))
    except Exception:  # health: allow BLE001 -- a store that cannot be inspected has no reader
        return False


def row_model_config(store, session_id: str) -> dict:
    """The parsed ``model_config`` of one Hermes session row, tolerating both store shapes.

    ``SessionDB`` exposes ``get_session`` (a dict row whose ``model_config`` may be JSON text); the
    persistence handle exposes the ``sessions`` repository (a record whose ``model_config`` is
    already a dict). Duck-typed doubles appear in tests, so every access is guarded.

    Returns ``{}`` only when there is no readable row at all AND no way to ask for one — a pointer
    cannot exist without a row, and a store with no row reader is refused further down the call
    (``agent/linked_foreign_turn.py``). Raises :class:`LinkedConfigUnreadable` when the row DOES
    carry a configuration this reader cannot validate, and also when the store raises while the row
    is being read: a ``SQLITE_BUSY`` or an I/O error proves nothing about the row, and "provably an
    ordinary session" is the one answer that must never be guessed.
    """
    if store is None or not isinstance(session_id, str) or not session_id:
        return {}
    sessions = getattr(store, "sessions", None)
    getter = getattr(sessions, "get", None)
    if callable(getter):
        try:
            record = getter(session_id)
        except Exception as exc:
            raise LinkedConfigUnreadable(
                f"session {session_id} could not be read from its store") from exc
        if record is None:
            return {}
        raw = getattr(record, "model_config", None)
    else:
        get_session = getattr(store, "get_session", None)
        if not callable(get_session):
            return {}
        try:
            row = get_session(session_id)
        except Exception as exc:
            raise LinkedConfigUnreadable(
                f"session {session_id} could not be read from its store") from exc
        if not isinstance(row, dict):
            return {}
        raw = row.get("model_config")
    return _validated_row_config(raw, session_id)


def _validated_row_config(raw, session_id: str) -> dict:
    """Decode one stored ``model_config`` cell strictly; raise :class:`LinkedConfigUnreadable`."""
    import json

    if raw is None:
        return {}
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except (ValueError, RecursionError):
            raise LinkedConfigUnreadable(
                f"session {session_id} has a model configuration that cannot be parsed") from None
        raw = decoded
    if isinstance(raw, Mapping):
        return dict(raw)
    raise LinkedConfigUnreadable(
        f"session {session_id} has a model configuration that is not a JSON object")


def linked_foreign_kind(store, session_id: str) -> str | None:
    """Which native tool owns this row's transcript, ``"foreign"`` for an unknown owner, else None.

    ``None`` means "an ordinary Hermes session" and is only returned for a row that is provably not
    a pointer. A row whose configuration cannot be validated answers ``"foreign"``: refusing a
    mutation on an ordinary session is recoverable, silently rewriting a linked thread is not
    (branching or rewinding a pointer row creates the second transcript the link exists to avoid).
    """
    try:
        config = row_model_config(store, session_id)
    except LinkedConfigUnreadable:
        return "foreign"
    except Exception:  # health: allow BLE001 -- an unreadable store is not "provably not a pointer"
        return "foreign"
    for flag in LINKED_FOREIGN_FLAGS:
        if config.get(flag) is True:
            return flag.removesuffix("_linked_thread")
    row_mode = config.get("api_mode")
    if isinstance(row_mode, str) and row_mode in LEGACY_LINKED_ROW_MODES:
        return LEGACY_LINKED_ROW_MODES[row_mode]
    return None


def linked_foreign_label(kind: str) -> str:
    """Display name of the tool that owns a :func:`linked_foreign_kind` row."""
    return {"codex": "Codex", "claude": "Claude Code", "mistral": "Mistral Vibe"}.get(
        kind, "the native tool")


def read_jsonl_head(path: Path, max_bytes: int = MAX_STORE_HEAD_BYTES,
                    max_line_bytes: int = MAX_STORE_LINE_BYTES):
    """Yield parsed JSON objects from the FIRST ``max_bytes`` of a foreign transcript.

    The bounded-head twin of :func:`read_jsonl_bounded`, for identity metadata: a listing needs a
    few leading records, never the whole file. Same per-line guard; a malformed or oversized record
    is skipped rather than failing the read, because one damaged line must not hide session identity.
    """
    import json

    if not path.is_file():
        raise ValueError("The foreign session transcript is unavailable")
    read_bytes = 0
    with path.open("rb") as handle:
        for raw in handle:
            read_bytes += len(raw)
            if read_bytes > max_bytes:
                break
            if len(raw) > max_line_bytes:
                continue
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, ValueError):
                continue


def read_jsonl_bounded(path: Path, max_bytes: int = MAX_STORE_FILE_BYTES,
                       max_line_bytes: int = MAX_STORE_LINE_BYTES):
    """Yield parsed JSON lines from an append-only foreign transcript, bounded per line and total."""
    import json

    if not path.is_file():
        raise ValueError("The foreign session transcript is unavailable")
    total = 0
    with path.open("rb") as handle:
        for raw in handle:
            total += len(raw)
            if total > max_bytes:
                raise ValueError("The foreign session transcript exceeds the safe read limit")
            if len(raw) > max_line_bytes:
                raise ValueError("The foreign session transcript contains an oversized record")
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", errors="strict"))
            except (ValueError, UnicodeDecodeError):
                continue  # a torn final line from a live writer is skipped, never fatal
            if isinstance(obj, dict):
                yield obj
