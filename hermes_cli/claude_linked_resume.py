"""Drive a linked Claude Code thread from Hermes: resolve it, lock it, append to it, prove it.

The pointer model (``hermes_cli/claude_linked_threads.py``) says the Hermes row holds a native
session id and nothing else; Claude Code keeps the single transcript. This module is the write half
of that contract, and every step exists to keep the pointer honest:

* **Resolve, don't assume.** The native id comes from the row's ``model_config`` (legacy
  ``api_mode: claude_linked_readonly`` rows carry it too), the project dir from
  ``~/.claude/projects/<slug>``, and the workspace from the transcript's own ``cwd`` records —
  accepted only when that cwd is the one the project dir's slug encodes. A cwd that does not encode
  the session's project dir would let the CLI open *a second* project dir for one thread, so it is
  refused (:data:`CLAUDE_CWD_MISMATCH`) rather than spawned.
* **Lock the thread.** A per-native-thread cross-process lock (``pm.filesystem.lock_path``) is held
  for the whole turn. A second writer — another Hermes surface, another process — gets
  :data:`CLAUDE_LINKED_BUSY` instead of two turns interleaving in one file.
* **Prove it landed, once.** Before/after snapshots of the native file (inode, size, prefix digest)
  catch a rewrite, a truncation or a torn tail from another writer
  (:data:`CLAUDE_WRITER_CONFLICT`), a turn that never reached the store
  (:data:`CLAUDE_NOT_RECORDED`), and a second session file appearing under the projects root
  (:data:`CLAUDE_FORK_DETECTED`). The transport separately asserts the id the CLI echoes.

A failure never leaves a phantom behind silently: on any transport refusal the post-run scan lists
the session files that appeared while we ran, so a fresh jsonl the CLI created for a *different*
session is named in the error the user sees.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from hermes_cli.claude_linked_threads import _session_path, resolve_claude_projects
from hermes_cli.foreign_linked_store import foreign_session_uuid, read_jsonl_bounded

# api_mode values a linked Claude pointer may carry: the read-only era and any later rename.
LINKED_CLAUDE_API_MODES = ("claude_linked_readonly", "claude_linked_resume")

CLAUDE_NOT_LINKED = "claude_not_linked"
CLAUDE_STORE_UNAVAILABLE = "claude_linked_store_unavailable"
CLAUDE_NATIVE_SESSION_MISSING = "claude_native_session_missing"
CLAUDE_CWD_UNAVAILABLE = "claude_cwd_unavailable"
CLAUDE_CWD_MISMATCH = "claude_cwd_mismatch"
CLAUDE_LINKED_BUSY = "claude_linked_busy"
CLAUDE_WRITER_CONFLICT = "claude_native_writer_conflict"
CLAUDE_NOT_RECORDED = "claude_linked_turn_not_recorded"
CLAUDE_FORK_DETECTED = "claude_linked_fork_detected"
#: The live-session transport this module owns: waiting for a prompt to be cancelled mid-poll.
CLAUDE_CANCELLED = "claude_cancelled"
#: The native session is LIVE in Claude Code's own registry: the sequential ``-p --resume`` path
#: would fork its transcript (anthropics/claude-code#97833), so the turn goes through the peer
#: messaging socket instead (``hermes_cli/claude_peer_messaging.py``).
CLAUDE_LIVE_TURN = "claude_live_peer_turn"

_PREFIX_BYTES = 64 * 1024
_MAX_PHANTOM_NAMES = 3


class LinkedClaudeError(RuntimeError):
    """A typed refusal from the linked-Claude write path; ``code`` is the stable wire value.

    ``upstream_code`` carries the transport's own code when this wraps a
    :class:`agent.transports.claude_cli_resume.ClaudeCliError`, so a caller can branch on either
    the linked-session vocabulary or the CLI's.
    """

    def __init__(self, code: str, detail: str, native_session_id: str | None = None, *,
                 upstream_code: str | None = None, stderr_tail: str = "") -> None:
        self.code = code
        self.detail = _brief(detail)
        self.native_session_id = native_session_id
        self.upstream_code = upstream_code
        self.stderr_tail = _brief(stderr_tail, 800)
        super().__init__(f"{code}: {self.detail}")


@dataclass(frozen=True)
class LinkedClaudeTarget:
    """Where one linked Claude thread lives: its identity, its file, and the cwd that owns both."""

    native_session_id: str
    session_path: Path
    projects_root: Path
    cwd: str
    slug_verified: bool


@dataclass
class LinkedClaudeTurn:
    """One appended turn. ``rows`` is for live display; the native store stays the transcript."""

    native_session_id: str
    session_path: Path
    cwd: str
    prompt: str
    rows: list[dict] = field(default_factory=list)
    final_text: str = ""
    output_format: str = ""
    argv: tuple[str, ...] = ()
    bytes_appended: int = 0
    slug_verified: bool = False
    turn: Any = None
    #: ``"peer"`` — delivered through the live session's messaging socket while that session ran
    #: (``run_linked_claude_turn`` never spawns a second ``claude --resume`` against it);
    #: ``"resume"`` — the sequential headless resume used when the session is not live.
    transport: str = "resume"


@dataclass(frozen=True)
class NativeSnapshot:
    """What a native transcript looked like before (and after) a turn.

    ``prefix_bytes`` is the exact length ``prefix_digest`` covers, so the same span can be re-hashed
    after the turn: hashing ``min(size, cap)`` twice would call every legitimate append a rewrite.
    """

    path: Path
    size: int
    mtime_ns: int
    inode: int
    prefix_digest: str
    prefix_bytes: int


def project_slugs(cwd: str | os.PathLike[str]) -> tuple[str, ...]:
    """Project-dir names Claude Code may encode *cwd* as (any non-alphanumeric → ``-``)."""
    text = str(cwd)
    return tuple(dict.fromkeys((
        "".join(ch if ch.isalnum() and ch.isascii() else "-" for ch in text),
        text.replace("/", "-").replace("\\", "-"),
    )))


def linked_claude_native_id(store: Any, session_id: str) -> str | None:
    """The pointer's native session id, or ``None`` when the row is not a linked Claude thread.

    Tolerates both row shapes in the tree: the persistence handle (``store.sessions.get`` returning
    a record whose ``model_config`` is a dict) and the legacy ``SessionDB`` (``get_session``
    returning a dict whose ``model_config`` may be JSON text).
    """
    config, _cwd = _row_fields(store, session_id)
    if not _is_linked_claude_config(config):
        return None
    native = config.get("claude_session_id")
    try:
        return foreign_session_uuid(native)
    except ValueError as exc:
        raise LinkedClaudeError(
            CLAUDE_STORE_UNAVAILABLE, "the linked Claude row has no valid session id") from exc


def _is_linked_claude_config(config: Mapping[str, Any]) -> bool:
    """A Claude pointer: the flag, or a legacy row that only carries its read-only api_mode."""
    return (config.get("claude_linked_thread") is True
            or config.get("api_mode") in LINKED_CLAUDE_API_MODES)


def _row_fields(store: Any, session_id: str) -> tuple[dict, str | None]:
    """``(model_config, cwd)`` for one Hermes session row, or ``({}, None)`` when unreadable."""
    if store is None or not session_id:
        return {}, None
    row = None
    sessions = getattr(store, "sessions", None)
    getter = getattr(sessions, "get", None)
    if callable(getter):
        row = getter(session_id)
    elif callable(getattr(store, "get_session", None)):
        row = store.get_session(session_id)
    if row is None:
        return {}, None
    raw_config = _field(row, "model_config")
    if isinstance(raw_config, str):
        try:
            raw_config = json.loads(raw_config)
        except ValueError:
            raw_config = {}
    cwd = _field(row, "cwd")
    return (raw_config if isinstance(raw_config, dict) else {}), (cwd if isinstance(cwd, str) else None)


def _field(row: Any, name: str) -> Any:
    """One attribute or mapping entry of a session row (records and dict rows both appear)."""
    if isinstance(row, Mapping):
        return row.get(name)
    return getattr(row, name, None)


def resolve_linked_claude_target(
    *, store: Any = None, session_id: str | None = None, native_session_id: str | None = None,
    cwd: str | None = None, profile_home: Any = None,
) -> LinkedClaudeTarget:
    """Locate the linked thread's native file and the workspace that owns it.

    Supply *store* + *session_id* (the Hermes turn loop) or *native_session_id* directly (a caller
    that already resolved the pointer). *cwd* is only ever a candidate: a workspace is accepted
    when it encodes the session's own project dir, never just because someone passed it.
    """
    try:
        projects = resolve_claude_projects(profile_home)
    except ValueError as exc:
        raise LinkedClaudeError(CLAUDE_STORE_UNAVAILABLE, str(exc)) from exc
    native = _resolve_native_id(store, session_id, native_session_id)
    try:
        session_path = _session_path(projects, native)
    except ValueError as exc:
        raise LinkedClaudeError(
            CLAUDE_NATIVE_SESSION_MISSING,
            f"Claude Code has no session {native} in this profile's store", native) from exc
    row_cwd = _row_fields(store, session_id)[1] if session_id else None
    chosen, verified = _choose_cwd(session_path, _cwd_candidates(session_path) + [row_cwd, cwd], native)
    return LinkedClaudeTarget(native_session_id=native, session_path=session_path,
                              projects_root=projects, cwd=chosen, slug_verified=verified)


def _resolve_native_id(store: Any, session_id: str | None,
                       native_session_id: str | None) -> str:
    """The native id from the explicit argument, else from the linked row (typed refusals).

    Both may be supplied by a caller that resolved the pointer itself; they must agree, otherwise
    the turn would append to a session the row does not point at.
    """
    if native_session_id:
        try:
            explicit = foreign_session_uuid(native_session_id)
        except ValueError as exc:
            raise LinkedClaudeError(CLAUDE_STORE_UNAVAILABLE,
                                    "not a Claude session id") from exc
        stored = linked_claude_native_id(store, session_id) if session_id else None
        if stored is not None and stored != explicit:
            raise LinkedClaudeError(
                CLAUDE_STORE_UNAVAILABLE,
                f"the linked row points at {stored}, not at the supplied {explicit}", stored)
        return explicit
    if not session_id:
        raise LinkedClaudeError(CLAUDE_NOT_LINKED,
                                "a linked Hermes session id or a native Claude session id is required")
    native = linked_claude_native_id(store, session_id)
    if native is None:
        raise LinkedClaudeError(
            CLAUDE_NOT_LINKED,
            f"Hermes session {session_id} is not linked to a Claude Code session")
    return native


def _cwd_candidates(session_path: Path) -> list[str]:
    """Every ``cwd`` the transcript recorded, most recent first (a thread can move workspaces)."""
    seen: list[str] = []
    for obj in read_jsonl_bounded(session_path):
        value = obj.get("cwd")
        if isinstance(value, str) and value and value not in seen:
            seen.append(value)
    return list(reversed(seen))


def _choose_cwd(session_path: Path, candidates: list[str | None], native: str) -> tuple[str, bool]:
    """First candidate that is a real directory encoding this session's project dir."""
    project_dir = session_path.parent.name
    usable: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute() or not path.is_dir():
            continue
        usable.append(candidate)
        if project_dir in project_slugs(candidate):
            return candidate, True
    if usable:
        raise LinkedClaudeError(
            CLAUDE_CWD_MISMATCH,
            f"none of the linked thread's recorded workspaces ({', '.join(usable[:2])}) encode its "
            f"Claude project directory {project_dir!r}; resuming there could open a second "
            "transcript for the same session", native)
    raise LinkedClaudeError(
        CLAUDE_CWD_UNAVAILABLE,
        f"the linked Claude session's workspace is no longer available (project {project_dir!r})",
        native)


def native_lock_path(native_session_id: str, *, home: str | os.PathLike[str] | None = None) -> Path:
    """The per-native-thread lock file under the owning profile's Hermes home."""
    from hermes_constants import get_hermes_home

    root = Path(home) if home is not None else Path(get_hermes_home())
    return root / "locks" / f"claude-linked-{native_session_id}.lock"


@contextlib.contextmanager
def native_thread_lock(native_session_id: str, *, timeout: float = 0.0,
                       home: str | os.PathLike[str] | None = None) -> Iterator[Path]:
    """Hold the linked thread's cross-process lock for the enclosing turn.

    ``timeout`` is how long to wait for a concurrent writer; the default is "do not wait" so a
    second sender gets :data:`CLAUDE_LINKED_BUSY` immediately instead of queueing behind a turn
    that may edit files for minutes. The lock is a kernel byte lock, so a killed holder releases it.
    """
    from pm.filesystem import lock_path

    path = native_lock_path(native_session_id, home=home)
    fd = lock_path(path, timeout=timeout, create_dir=True)
    if fd is None:
        raise LinkedClaudeError(
            CLAUDE_LINKED_BUSY,
            "another Hermes turn is already appending to this Claude thread",
            native_session_id)
    try:
        yield path
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def native_snapshot(path: Path) -> NativeSnapshot:
    """Stat plus a prefix digest: enough to notice a rewrite, a truncation or an inode swap."""
    stat = path.stat()
    prefix_bytes = min(stat.st_size, _PREFIX_BYTES)
    return NativeSnapshot(path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
                          inode=stat.st_ino, prefix_digest=_prefix_digest(path, prefix_bytes),
                          prefix_bytes=prefix_bytes)


def _prefix_digest(path: Path, length: int) -> str:
    """SHA-256 of the file's first *length* bytes (the span a snapshot recorded)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        digest.update(handle.read(length))
    return digest.hexdigest()


def verify_native_append(before: NativeSnapshot, native_session_id: str) -> NativeSnapshot:
    """Re-snapshot the file and refuse a turn that did not land, or landed beside another writer."""
    try:
        after = native_snapshot(before.path)
    except OSError as exc:
        raise LinkedClaudeError(
            CLAUDE_WRITER_CONFLICT,
            f"the linked transcript vanished during the turn: {before.path.name}",
            native_session_id) from exc
    if after.inode != before.inode:
        raise LinkedClaudeError(
            CLAUDE_WRITER_CONFLICT,
            "another writer replaced the linked transcript while this turn ran", native_session_id)
    if after.size < before.size:
        raise LinkedClaudeError(
            CLAUDE_WRITER_CONFLICT,
            "another writer truncated the linked transcript while this turn ran", native_session_id)
    if _prefix_digest(before.path, before.prefix_bytes) != before.prefix_digest:
        raise LinkedClaudeError(
            CLAUDE_WRITER_CONFLICT,
            "another writer rewrote the linked transcript while this turn ran", native_session_id)
    if after.size == before.size:
        raise LinkedClaudeError(
            CLAUDE_NOT_RECORDED,
            "Claude answered but the native transcript did not grow: the turn did not land on the "
            "linked thread", native_session_id)
    _check_appended_records(before, after, native_session_id)
    return after


def _check_appended_records(before: NativeSnapshot, after: NativeSnapshot, native: str) -> None:
    """The appended region must be complete records of this session, not another writer's."""
    with before.path.open("rb") as handle:
        handle.seek(before.size)
        chunk = handle.read(after.size - before.size)
    if not chunk.endswith(b"\n"):
        raise LinkedClaudeError(
            CLAUDE_WRITER_CONFLICT,
            "the linked transcript ends mid-record: another writer is appending to it",
            native)
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError):
            raise LinkedClaudeError(
                CLAUDE_WRITER_CONFLICT,
                "the linked transcript gained an unparseable record while this turn ran", native) from None
        if not isinstance(record, dict):
            continue
        recorded = record.get("sessionId")
        if isinstance(recorded, str) and recorded != native:
            raise LinkedClaudeError(
                CLAUDE_WRITER_CONFLICT,
                f"another Claude session ({recorded}) appended to this transcript while the turn ran",
                native)


def project_session_files(projects_root: Path) -> dict[Path, tuple[int, int]]:
    """``{resolved session file: (mtime_ns, size)}`` for the projects root, for before/after diffs."""
    files: dict[Path, tuple[int, int]] = {}
    for path in projects_root.glob("*/*.jsonl"):
        try:
            resolved = path.resolve()
            stat = resolved.stat()
        except OSError:
            continue
        if resolved.is_file():
            files[resolved] = (stat.st_mtime_ns, stat.st_size)
    return files


def _new_session_files(before: Mapping[Path, tuple[int, int]], projects_root: Path) -> list[Path]:
    """Session files that did not exist before the turn (a new one means a new native session)."""
    return sorted(path for path in project_session_files(projects_root) if path not in before)


def _phantoms_note(phantoms: list[Path]) -> str:
    """One clause naming the new session files a failed run left behind, or "" when there are none."""
    if not phantoms:
        return ""
    names = ", ".join(path.name for path in phantoms[:_MAX_PHANTOM_NAMES])
    more = "" if len(phantoms) <= _MAX_PHANTOM_NAMES else f" (+{len(phantoms) - _MAX_PHANTOM_NAMES} more)"
    return f"; a new Claude session file appeared and is not the linked thread: {names}{more}"


def run_linked_claude_turn(
    *,
    prompt: str,
    store: Any = None,
    session_id: str | None = None,
    native_session_id: str | None = None,
    cwd: str | None = None,
    profile_home: Any = None,
    claude_bin: str | None = None,
    config: Mapping[str, Any] | None = None,
    timeout: float | None = None,
    cancel_event: Any = None,
    on_event: Any = None,
    lock_timeout: float = 0.0,
) -> LinkedClaudeTurn:
    """Append *prompt* to the linked Claude thread and return the rows to display.

    The entry point a turn loop calls. A session that is LIVE in Claude Code's registry is
    continued through its messaging socket (concurrent, no second CLI process — see
    :func:`_run_live_peer_turn`); only a session with no live process falls through to the
    sequential headless ``claude -p --resume`` transport.

    Args:
        prompt: the user's message; passed to the CLI as ``-p``'s value, or on stdin when it starts
            with ``-`` or is too long for one argv element.
        store: the persistence handle owning the pointer row (``store.sessions.get``), or ``None``
            when the caller already resolved *native_session_id*.
        session_id: the *Hermes* session id of the linked row.
        native_session_id: the Claude session id, when the caller holds it; if *session_id* is also
            given, the row's pointer must agree with it.
        cwd: an extra workspace candidate; it is only used when it encodes the thread's project dir.
        profile_home: owning profile for foreign-store resolution (``None`` = the active profile).
        claude_bin: an explicit ``claude`` executable; ``None`` resolves ``model.claude_bin`` then
            ``PATH`` then Claude Code's install prefixes.
        config: merged Hermes config, for ``model.claude_bin`` (loaded lazily when both this and
            *claude_bin* are ``None``).
        timeout: hard bound for the turn; the child's process group is killed when it expires.
        cancel_event: anything with ``is_set()``; cancelling kills the child and refuses the turn.
        on_event: receives each parsed CLI event as it arrives (display only; failures are logged).
        lock_timeout: how long to wait for a concurrent writer of this thread (default: no wait).

    Returns:
        :class:`LinkedClaudeTurn` — the redacted display *rows* this turn produced,
        *final_text*, the exact *argv* spawned, *bytes_appended* to the native transcript, and the
        transport's own :class:`~agent.transports.claude_cli_resume.ClaudeCliTurn` in ``turn``.
        The caller renders :attr:`LinkedClaudeTurn.rows`; it must NOT persist them — the native
        store stays the transcript (pointer semantics), and history re-reads it.

    Raises:
        :class:`LinkedClaudeError` with a stable ``code``: the linked-session codes defined in this
        module, or the transport's ``claude_*`` code passed through in ``code``/``upstream_code``.
    """
    target = resolve_linked_claude_target(store=store, session_id=session_id,
                                          native_session_id=native_session_id, cwd=cwd,
                                          profile_home=profile_home)

    if config is None and claude_bin is None:
        # A gateway/service launch has a bare PATH; `model.claude_bin` is the operator's override
        # for it (the same reason `model.codex_bin` exists), so resolve config at the spawn site.
        from hermes_cli.config import load_config

        config = load_config()

    with native_thread_lock(target.native_session_id, timeout=lock_timeout):
        live = _live_peer_target(target, profile_home)
        if live is not None:
            return _run_live_peer_turn(prompt, target, live, timeout=timeout,
                                       cancel_event=cancel_event)
        return _run_sequential_resume_turn(
            prompt, target, claude_bin=claude_bin, config=config, timeout=timeout,
            cancel_event=cancel_event, on_event=on_event)


def _run_sequential_resume_turn(prompt, target, *, claude_bin, config, timeout, cancel_event,
                                on_event) -> LinkedClaudeTurn:
    """The headless ``claude -p --resume`` path, for a session with NO live process."""
    from agent.transports.claude_cli_resume import (
        DEFAULT_TURN_TIMEOUT_SECONDS, ClaudeCliError, run_claude_resume_turn,
    )

    env_overrides = {"CLAUDE_CONFIG_DIR": str(target.projects_root.parent)}
    before = _snapshot_or_refuse(target)
    projects_before = project_session_files(target.projects_root)
    try:
        turn = run_claude_resume_turn(
            prompt, native_session_id=target.native_session_id, cwd=target.cwd,
            claude_bin=claude_bin, config=config,
            timeout=DEFAULT_TURN_TIMEOUT_SECONDS if timeout is None else timeout,
            cancel_event=cancel_event, on_event=on_event, env_overrides=env_overrides)
    except ClaudeCliError as exc:
        raise _transport_failure(exc, target, projects_before) from exc
    # A second transcript is the headline failure, so it is checked before "did ours grow":
    # a turn that wrote the thread somewhere else also left this file untouched.
    _assert_single_transcript(target, projects_before)
    after = verify_native_append(before, target.native_session_id)
    return LinkedClaudeTurn(
        native_session_id=target.native_session_id, session_path=target.session_path,
        cwd=target.cwd, prompt=prompt, rows=turn.rows, final_text=turn.final_text,
        output_format=turn.output_format, argv=turn.argv,
        bytes_appended=after.size - before.size, slug_verified=target.slug_verified, turn=turn)


def _live_peer_target(target: LinkedClaudeTarget, profile_home: Any):
    """The live registry row for this native session, or ``None`` when it is not live.

    Registry access failures are TRANSPORT failures for a live send only when they prove
    liveness; a missing registry (older claude, another platform) just means "not live", and the
    sequential resume path decides the rest. This is the #97833 fork boundary: a live row here is
    the ONLY thing that reroutes the turn away from the resume transport.
    """
    from hermes_cli.claude_peer_messaging import live_claude_session

    return live_claude_session(target.native_session_id, profile_home)


#: How long a live peer turn may wait for the native session to write our prompt (the receiving
#: claude answers its queue after its current turn; a long current turn is still "live delivery").
_LIVE_TURN_PROMPT_TIMEOUT_S = 90.0
#: Poll cadence while waiting for the native file to grow (a bound, never a fixed sleep).
_LIVE_TURN_POLL_S = 0.5


def _run_live_peer_turn(prompt: str, target: LinkedClaudeTarget, live,
                        *, timeout: float | None, cancel_event: Any) -> LinkedClaudeTurn:
    """Deliver *prompt* through the live session's messaging socket and prove it landed.

    The live claude is the ONLY writer: we read the transcript before and after and never spawn
    a second CLI against the session (that is the #97833 fork). Rows for display are projected
    from the records the live session itself appended.
    """
    from hermes_cli import claude_peer_messaging as peer

    before = _snapshot_or_refuse(target)
    try:
        peer.send_peer_prompt(target.native_session_id, prompt, cancel_event=cancel_event)
    except peer.ClaudePeerError as exc:
        raise LinkedClaudeError(exc.code, exc.detail, target.native_session_id) from None
    deadline = time.monotonic() + (timeout if timeout is not None else _LIVE_TURN_PROMPT_TIMEOUT_S)
    appended = _wait_for_native_append(before, target, deadline, cancel_event)
    rows, final_text = _project_live_append(prompt, appended)
    if not rows and not final_text:
        raise LinkedClaudeError(
            CLAUDE_NOT_RECORDED,
            "the live Claude session accepted the message but has not written the turn yet "
            "(it may still be finishing its current task); send again in a moment",
            target.native_session_id)
    return LinkedClaudeTurn(
        native_session_id=target.native_session_id, session_path=target.session_path,
        cwd=target.cwd, prompt=prompt, rows=rows, final_text=final_text,
        output_format="live-peer", argv=(live.socket_path,),
        bytes_appended=len(json.dumps(appended, default=str).encode()) if appended else 0,
        slug_verified=target.slug_verified, transport="peer")


def _wait_for_native_append(before: NativeSnapshot, target: LinkedClaudeTarget,
                            deadline: float, cancel_event: Any) -> list[dict]:
    """Read-only poll until the live session wrote new records (or the bound expires)."""
    appended: list[dict] = []
    while time.monotonic() < deadline:
        if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) \
                and cancel_event.is_set():
            raise LinkedClaudeError(CLAUDE_CANCELLED, "the live turn was cancelled",
                                   target.native_session_id)
        try:
            after = native_snapshot(target.session_path)
            if after.size > before.size:
                appended = _read_appended_records(before, after)
                if appended:
                    return appended
        except OSError:
            pass  # a live writer may stat mid-append; the next poll re-reads
        time.sleep(_LIVE_TURN_POLL_S)
    return appended


def _read_appended_records(before: NativeSnapshot, after: NativeSnapshot) -> list[dict]:
    """Parse the bytes the live session appended, tolerating a torn in-flight tail."""
    records: list[dict] = []
    with before.path.open("rb") as handle:
        handle.seek(before.size)
        chunk = handle.read(after.size - before.size)
    for raw in chunk.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError):
            continue  # a live writer may be mid-line; only complete records count
        if isinstance(record, dict):
            records.append(record)
    return records


def _project_live_append(prompt: str, records: list[dict]) -> tuple[list[dict], str]:
    """Display rows from the records the LIVE session wrote for this turn.

    The receiving session writes its own user row for the delivered prompt, then its assistant
    reply. Both are projected (redacted) for display; nothing is written to the Hermes transcript.
    A turn still running (no assistant text yet) shows the user row, so the surface sees delivery
    instead of an invented failure.
    """
    from hermes_cli.claude_linked_history import _timestamp
    from hermes_cli.foreign_sessions import _message_turn
    from hermes_cli.foreign_linked_store import safe_native_text

    rows: list[dict] = []
    for obj in records:
        if obj.get("type") not in ("user", "assistant"):
            continue
        if obj.get("isSidechain") or obj.get("isMeta"):
            continue
        turn = _message_turn(obj.get("message"))
        if turn is None:
            continue
        role, text = turn
        rows.append({"role": role, "content": safe_native_text(text), "timestamp": _timestamp(obj)})
    final_text = ""
    for row in reversed(rows):
        if row.get("role") == "assistant" and row.get("content"):
            final_text = row["content"]
            break
    return rows, final_text


def _snapshot_or_refuse(target: LinkedClaudeTarget) -> NativeSnapshot:
    """The pre-turn snapshot; a transcript that vanished since resolution is a typed refusal."""
    try:
        return native_snapshot(target.session_path)
    except OSError as exc:
        raise LinkedClaudeError(
            CLAUDE_NATIVE_SESSION_MISSING,
            f"the linked transcript disappeared before the turn: {target.session_path.name}",
            target.native_session_id) from exc


def _transport_failure(exc: Any, target: LinkedClaudeTarget,
                       projects_before: Mapping[Path, tuple[int, int]]) -> LinkedClaudeError:
    """A transport refusal, carrying the CLI's code and any session file the failed run created."""
    phantoms = _new_session_files(projects_before, target.projects_root)
    return LinkedClaudeError(
        exc.code, f"{exc.detail}{_phantoms_note(phantoms)}",
        native_session_id=target.native_session_id, upstream_code=exc.code,
        stderr_tail=exc.stderr_tail)


def _assert_single_transcript(target: LinkedClaudeTarget,
                              projects_before: Mapping[Path, tuple[int, int]]) -> None:
    """Refuse a turn that wrote the same session id into a second project dir."""
    for path in _new_session_files(projects_before, target.projects_root):
        if path.stem == target.native_session_id:
            raise LinkedClaudeError(
                CLAUDE_FORK_DETECTED,
                f"the turn wrote a second transcript for session {target.native_session_id}: {path}",
                target.native_session_id)


def _brief(value: Any, limit: int = 400) -> str:
    """One bounded line of diagnostic text."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isprintable())[:limit]
