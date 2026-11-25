"""Send one Hermes turn into a linked Mistral Vibe thread — same native session, never a fork.

The Hermes row is a pointer (``model_config.mistral_linked_thread`` + ``mistral_session_id``). This
module is the write side of that pointer: it resolves the native session, takes a per-thread lock,
spawns ``vibe -p … --resume <SESSION_ID> --workdir <cwd> --output json`` (see
``agent.transports.vibe_cli_resume`` for the argv and the process plumbing), then verifies the native
journal grew by exactly this turn.

What Hermes writes: nothing. No message row, no copied transcript, no "sidecar" session. The native
store stays the single transcript, so every display path keeps reading it
(``hermes_cli.mistral_linked_threads.read_mistral_history``). The returned
:class:`LinkedVibeTurn` is a *view* of the rows the native store gained, not a place they were
saved.

Guarantees, in the order they are enforced:

1. **No fork.** The native id comes from the pointer row and is the only session selector on the
   command line; the returned :class:`LinkedVibeTurn` carries a ``native_session_id`` that must equal
   it, and a run whose entries report a different ``sessionId`` is refused. Nothing here can create a
   session.
2. **No escalation.** No approval/permission flag is ever passed (the transport rejects them), so the
   thread runs under the policy configured in Vibe itself. A call that policy refuses shows up as a
   ``Tool call denied: …`` warning in :attr:`LinkedVibeTurn.warnings`; a run that would have needed
   an interactive approval fails with ``VibeApprovalRequired`` instead of being auto-approved.
3. **One writer at a time.** A ``flock`` is held for the native thread for the whole turn, and the
   native store's own lease (``<store>/active/<session_id>.lock``) is probed first: a session still
   open in a Vibe terminal is a typed :class:`LinkedVibeConflict`, not interleaved writes.
4. **The turn is confirmed from the native store.** Before spawning, every journal file is
   fingerprinted (size, mtime, tail hash, last record hash/sequence). After the run, the pre-existing
   prefix must be byte-identical (no in-place rewrite) and the appended records must chain linearly
   onto the fingerprint (``previous_record_sha256`` → ``record_sha256``, strictly increasing
   ``sequence``). A break means another writer appended while we ran: :class:`LinkedVibeConflict`,
   and the caller learns the turn is untrustworthy instead of showing interleaved history.

Wiring (the turn loop owns this)::

    from hermes_cli.mistral_linked_resume import LinkedVibeError, run_linked_vibe_turn

    turn = run_linked_vibe_turn(store=store, session_id=hermes_session_id, prompt=text)
    deliver(turn.messages)      # [{role, content}] rows the native store just gained

Every refusal carries a user-facing message — never a partial turn and never a phantom session.
``LinkedVibeError`` (store-level policy) extends the transport's ``VibeResumeError`` (process and
output failures), so one ``except VibeResumeError`` covers both.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from agent.transports.vibe_cli_resume import (
    DEFAULT_OUTPUT_FORMAT,
    DEFAULT_TIMEOUT_SECONDS,
    VibeOutputUnreadable,
    VibeResumeError,
    VibeTurnOutput,
    parse_vibe_output,
    raise_for_vibe_failure,
    resolve_vibe_binary,
    run_vibe_resume,
    vibe_denials,
)
from hermes_cli import mistral_linked_history, mistral_linked_threads
from hermes_cli.foreign_linked_store import safe_native_text

__all__ = [
    "LinkedVibeConflict",
    "LinkedVibeError",
    "LinkedVibeThread",
    "LinkedVibeTurn",
    "LinkedVibeUnavailable",
    "check_linked_vibe_send",
    "child_env_for_vibe_store",
    "linked_vibe_thread",
    "run_linked_vibe_turn",
]

_TAIL_GUARD_BYTES = 4096
_LOCK_POLL_SECONDS = 0.05
#: ``flock`` reports an already-held lock as EACCES/EWOULDBLOCK; anything else is a real failure.
_LOCK_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)})
_DEFAULT_LOCK_WAIT_SECONDS = 2.0


# --------------------------------------------------------------------------------------------------
# typed refusals
# --------------------------------------------------------------------------------------------------
class LinkedVibeError(VibeResumeError):
    """Base for every linked-session refusal; the turn loop catches this one type.

    It extends the transport's :class:`VibeResumeError`, so catching either covers both the
    store-level policy failures here and the process/output failures from the transport.
    """


class LinkedVibeUnavailable(LinkedVibeError):
    """The linked thread cannot be resumed at all (no pointer, stale store, missing cwd/binary)."""


class LinkedVibeConflict(LinkedVibeError):
    """Another writer owns the native thread, or the native store disagrees with this turn."""


# --------------------------------------------------------------------------------------------------
# resolved thread
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkedVibeThread:
    """Everything a writable turn needs to know about one linked native session."""

    session_id: str
    native_session_id: str
    session_dir: Path
    cwd: str
    store_root: Path
    lease_key: str


@dataclass(frozen=True)
class LinkedVibeTurn:
    """The rows the native store gained from one Hermes send.

    ``messages`` is a projection of the native journal, redacted and capped exactly like linked
    history display; it is never persisted. ``native_session_id`` is asserted to be the id the send
    resumed, so a caller can never see a forked thread.
    """

    session_id: str
    native_session_id: str
    turn_id: str | None
    assistant_text: str
    messages: list[dict]
    journal_files: tuple[str, ...]
    appended_records: int
    duration_s: float
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------------------------------
def linked_vibe_thread(store, session_id: str, profile_home=None) -> LinkedVibeThread:
    """Resolve the pointer row to its native session directory, cwd and lease key.

    The session root comes from :func:`hermes_cli.mistral_linked_threads.resolve_vibe_session_root`
    (``MISTRAL_VIBE_SESSION_ROOT`` → ``VIBE_HOME`` → ``~/.vibe/logs/session``) and the directory from
    the shared ``_find_session_dir`` resolver, so the write path inherits the same symlink-escape
    guard as the read path. The thread's own cwd comes from its ``meta.json`` and is what the child
    receives as ``--workdir``.
    """
    if not isinstance(session_id, str) or not session_id:
        raise LinkedVibeUnavailable("A Hermes session ID is required")
    native_id = mistral_linked_history.linked_mistral_session_id(store, session_id)
    if native_id is None:
        raise LinkedVibeUnavailable("This Hermes session is not a linked Mistral Vibe thread")
    try:
        root = mistral_linked_threads.resolve_vibe_session_root(profile_home)
        session_dir = mistral_linked_threads._find_session_dir(root, native_id)
    except ValueError as exc:
        raise LinkedVibeUnavailable(str(exc)) from exc
    info = mistral_linked_threads._session_info(session_dir, native_id)
    cwd = info.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise LinkedVibeUnavailable("The Mistral session's working directory is unavailable")
    if not Path(cwd).is_dir():
        raise LinkedVibeUnavailable("The Mistral session's working directory no longer exists")
    meta = mistral_linked_threads._read_meta(session_dir)
    meta_id = meta.get("session_id")
    lease_key = meta_id if isinstance(meta_id, str) and meta_id else session_dir.name
    return LinkedVibeThread(session_id=session_id, native_session_id=native_id,
                            session_dir=Path(session_dir), cwd=cwd, store_root=Path(root),
                            lease_key=lease_key)


def check_linked_vibe_send(store, session_id: str, *, profile_home=None,
                           config: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Can a send run right now? ``(ok, reason)`` for a surface that gates the composer.

    Read-only: resolves the thread, checks the CLI is present, and probes the native lease. It takes
    no lock, so it is only a hint — :func:`run_linked_vibe_turn` re-checks everything under the lock.
    """
    try:
        thread = linked_vibe_thread(store, session_id, profile_home=profile_home)
    except (LinkedVibeError, ValueError) as exc:
        return False, str(exc)
    try:
        binary = resolve_vibe_binary(config)
    except VibeResumeError as exc:
        return False, str(exc)
    holder = _native_lease_holder(thread.store_root, thread.lease_key)
    if holder is not None:
        return False, _lease_conflict_message(thread, holder)
    return True, f"Resume {thread.native_session_id} with {binary}"


# --------------------------------------------------------------------------------------------------
# child environment
# --------------------------------------------------------------------------------------------------
def child_env_for_vibe_store(store_root: Path, base_env: Mapping[str, str] | None = None) -> dict:
    """The child's environment: Hermes' usual sanitized one, pinned to the *resolved* store.

    ``VIBE_HOME`` is set from the store root the pointer was resolved against whenever that root has
    the ``<VIBE_HOME>/logs/session`` shape, so the child appends to the same store this module will
    verify. Otherwise the child keeps its own resolution and the journal check below decides.
    """
    try:
        from tools.environments.local import hermes_subprocess_env
        env = hermes_subprocess_env(
            inherit_credentials=True,
            base_env=dict(base_env) if base_env is not None else None)
    except Exception:  # health: allow BLE001 -- a sanitizer failure must not block the send
        env = dict(base_env) if base_env is not None else dict(os.environ)
    vibe_home = _vibe_home_for_store(store_root)
    if vibe_home is not None:
        env["VIBE_HOME"] = str(vibe_home)
    return env


def _vibe_home_for_store(store_root: Path) -> Path | None:
    try:
        root = Path(store_root).resolve()
    except OSError:
        return None
    if root.name == "session" and root.parent.name == "logs":
        return root.parent.parent
    return None


# --------------------------------------------------------------------------------------------------
# locks and leases
# --------------------------------------------------------------------------------------------------
def _flock_module():
    try:
        import fcntl  # POSIX only
    except ImportError:  # pragma: no cover - windows
        return None
    return fcntl


def _lock_dir_default() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "linked-vibe-locks"


def _turn_lock_path(lock_dir: Path, lease_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", lease_key)[:96].strip(".") or "session"
    return Path(lock_dir) / f"{safe}.lock"


@contextmanager
def _turn_lock(lock_dir: Path | None, lease_key: str, wait_seconds: float) -> Iterator[None]:
    """Exclusive flock for one native thread, held for the whole turn.

    Two Hermes sends on the same thread serialize here; the second one waits ``wait_seconds`` and
    then gets a typed conflict instead of racing the first. Without ``fcntl`` (Windows) the lock is
    best effort — the native lease probe and the journal verification still guard the turn.
    """
    flock = _flock_module()
    directory = Path(lock_dir) if lock_dir is not None else _lock_dir_default()
    path = _turn_lock_path(directory, lease_key)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LinkedVibeError(f"Could not open the linked-session lock: {exc}") from exc
    try:
        if flock is None:  # pragma: no cover - windows
            yield
            return
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            try:
                flock.flock(fd, flock.LOCK_EX | flock.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                    raise LinkedVibeError(
                        f"Could not lock the linked Vibe thread: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise LinkedVibeConflict(
                        "Another Hermes turn is already running on this Vibe thread") from None
                time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            with suppress(OSError):
                flock.flock(fd, flock.LOCK_UN)
    finally:
        with suppress(OSError):
            os.close(fd)


def _native_lease_owner_info(store_root: Path, lease_key: str) -> dict:
    path = Path(store_root) / "active" / f"{lease_key}.lock.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _native_lease_holder(store_root: Path, lease_key: str) -> dict | None:
    """The live owner record when a Vibe process holds this session's lease, else ``None``.

    Vibe takes an exclusive ``flock`` on ``<store>/active/<session_id>.lock`` for as long as a
    process owns the session, and records the owner pid beside it. Trying the same lock
    non-blocking is the only reliable "is a terminal driving this thread right now" signal, and it
    is released the instant that process exits.
    """
    path = Path(store_root) / "active" / f"{lease_key}.lock"
    if not path.exists():
        return None
    flock = _flock_module()
    if flock is None:  # pragma: no cover - windows
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
    try:
        try:
            flock.flock(fd, flock.LOCK_EX | flock.LOCK_NB)
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                return None  # flock unsupported here: the journal check below still guards us
            return _native_lease_owner_info(store_root, lease_key) or {"session_id": lease_key}
        with suppress(OSError):
            flock.flock(fd, flock.LOCK_UN)
        return None
    finally:
        with suppress(OSError):
            os.close(fd)


def _lease_conflict_message(thread: LinkedVibeThread, holder: Mapping[str, Any]) -> str:
    pid = holder.get("process_id") or holder.get("pid")
    where = f" (process {pid})" if isinstance(pid, int) else ""
    return (f"Vibe session {thread.native_session_id} is open in another Vibe process{where}; "
            "close it there before sending from Hermes")


# --------------------------------------------------------------------------------------------------
# journal fingerprint / verification
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _FileSnapshot:
    size: int
    mtime_ns: int
    tail_hash: str | None
    last_sequence: int | None
    last_record_sha: str | None


def _transcript_files(session_dir: Path) -> list[Path]:
    """Append-only transcript files for a session: unified ``journal/*.jsonl``, else legacy."""
    journal = Path(session_dir) / "journal"
    if journal.is_dir():
        return sorted(journal.glob("*.jsonl"))
    messages = Path(session_dir) / "messages.jsonl"
    return [messages] if messages.is_file() else []


def _snapshot_file(path: Path) -> _FileSnapshot:
    stat = path.stat()
    size = stat.st_size
    offset = max(0, size - _TAIL_GUARD_BYTES)
    with path.open("rb") as handle:
        handle.seek(offset)
        tail = handle.read(size - offset)
    last_sequence = last_record_sha = None
    for record in _records_from_bytes(tail, partial_ok=False):
        sha = record.get("record_sha256")
        if isinstance(sha, str):
            last_record_sha = sha
        sequence = record.get("sequence")
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            last_sequence = sequence
    return _FileSnapshot(size=size, mtime_ns=stat.st_mtime_ns,
                         tail_hash=hashlib.sha256(tail).hexdigest() if tail else None,
                         last_sequence=last_sequence, last_record_sha=last_record_sha)


def _snapshot(session_dir: Path) -> dict[str, _FileSnapshot]:
    """Fingerprint every transcript file, keyed by its path relative to the session dir."""
    snapshot: dict[str, _FileSnapshot] = {}
    for path in _transcript_files(session_dir):
        try:
            snapshot[str(path.relative_to(session_dir))] = _snapshot_file(path)
        except OSError:
            continue
    return snapshot


def _records_from_bytes(raw: bytes, *, partial_ok: bool = True) -> list[dict]:
    """Parse complete JSON lines; the first line of a tail window is dropped unless it starts one."""
    lines = raw.split(b"\n")
    records: list[dict] = []
    for index, line in enumerate(lines):
        if index == 0 and not partial_ok:
            continue  # continues an earlier line: not a record boundary
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line.decode("utf-8", errors="strict"))
        except (ValueError, UnicodeDecodeError):
            continue  # a torn final line from a live writer is skipped, never fatal
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _appended_records(path: Path, offset: int) -> list[dict]:
    """Records appended after ``offset``; the offset is always a record boundary (a file size)."""
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(max(0, path.stat().st_size - offset))
    return _records_from_bytes(raw, partial_ok=True)


def _verify_and_project(session_dir: Path, before: Mapping[str, _FileSnapshot]):
    """Verify the journal grew by exactly this turn; return ``(records, turns, turn_id, files)``.

    Raises :class:`LinkedVibeConflict` when a pre-existing journal file was rewritten or truncated,
    when the appended records do not chain onto the pre-turn fingerprint, or when ``sequence``
    regresses — each of which means another writer interleaved with this turn.
    """
    after = _snapshot(session_dir)
    for name, snap in before.items():
        current = after.get(name)
        if current is None:
            raise LinkedVibeConflict(
                "The Vibe session journal changed while this turn ran; reopen the thread")
        if current.size < snap.size:
            raise LinkedVibeConflict(
                "The Vibe session journal was truncated while this turn ran; reopen the thread")
        if _prefix_hash(session_dir / name, snap.size) != snap.tail_hash:
            raise LinkedVibeConflict(
                "The Vibe session journal was rewritten in place while this turn ran; "
                "reopen the thread")
    grown: list[str] = []
    turns: list[tuple[str, str]] = []
    appended = 0
    turn_id: str | None = None
    baseline_sequence = max(
        (snap.last_sequence for snap in before.values()
         if isinstance(snap.last_sequence, int)), default=None)
    for name, current in after.items():
        snap = before.get(name)
        offset = snap.size if snap is not None else 0
        if offset == current.size:
            continue
        try:
            records = _appended_records(session_dir / name, offset)
        except OSError as exc:
            raise LinkedVibeConflict(
                "The Vibe session journal became unreadable while this turn ran") from exc
        if not records:
            continue
        grown.append(name)
        expected_sha = snap.last_record_sha if snap is not None else None
        expected_sequence = snap.last_sequence if snap is not None else baseline_sequence
        for record in records:
            appended += 1
            previous_sha = record.get("previous_record_sha256")
            record_sha = record.get("record_sha256")
            sequence = record.get("sequence")
            if isinstance(previous_sha, str) and expected_sha is not None \
                    and previous_sha != expected_sha:
                raise LinkedVibeConflict(
                    "Another writer appended to the Vibe session while this turn ran; "
                    "reopen the thread to read the merged history")
            if isinstance(sequence, int) and not isinstance(sequence, bool) \
                    and expected_sequence is not None and sequence <= expected_sequence:
                raise LinkedVibeConflict(
                    "The Vibe session journal sequence repeated while this turn ran; "
                    "reopen the thread")
            if isinstance(record_sha, str):
                expected_sha = record_sha
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                expected_sequence = sequence
            for role, text in _record_turns(record):
                turns.append((role, text))
            record_turn = record.get("turnId")
            if isinstance(record_turn, str) and record_turn:
                turn_id = record_turn
    return appended, turns, turn_id, tuple(grown)


def _prefix_hash(path: Path, size: int) -> str | None:
    """Hash of the last ``_TAIL_GUARD_BYTES`` bytes *ending at* the pre-turn size."""
    if size <= 0:
        return None
    offset = max(0, size - _TAIL_GUARD_BYTES)
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            tail = handle.read(size - offset)
    except OSError:
        return "unreadable"
    return hashlib.sha256(tail).hexdigest() if tail else None


def _record_turns(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(role, text)`` message turns from one journal record.

    Delegates to the linked-history projection so the write path confirms exactly the rows the
    display path would read back: one set of rules for ``projection_delta`` payloads and plain rows.
    """
    return mistral_linked_history._record_turns(record)


# --------------------------------------------------------------------------------------------------
# the turn
# --------------------------------------------------------------------------------------------------
def run_linked_vibe_turn(
    *,
    store,
    session_id: str,
    prompt: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    profile_home=None,
    config: Mapping[str, Any] | None = None,
    output: str = DEFAULT_OUTPUT_FORMAT,
    cancel_event: Any = None,
    lock_dir: Path | None = None,
    lock_wait_seconds: float = _DEFAULT_LOCK_WAIT_SECONDS,
    probe_lease: bool = True,
    base_env: Mapping[str, str] | None = None,
) -> LinkedVibeTurn:
    """Append ``prompt`` to the linked native Vibe thread and return the rows it gained.

    Args:
        store: the persistence handle owning the pointer row (``store.sessions.get``).
        session_id: the *Hermes* session id of the linked row.
        prompt: the user's message; passed to Vibe as one argv element (``-p``).
        timeout: hard bound for the child; the process group is killed when it expires.
        profile_home: owning profile for foreign-store resolution (``None`` = active profile).
        config: merged Hermes config, for ``model.vibe_bin``; ``None`` means no override
            (a bare ``vibe`` resolved on PATH).
        output: ``json`` (default) or ``streaming``; both are parsed by the transport.
        cancel_event: anything with ``is_set()``; cancelling kills the child and raises
            ``VibeRunCancelled``.
        lock_dir: where the per-thread lock file lives (defaults under the Hermes home).
        lock_wait_seconds: how long a second concurrent send waits before a typed conflict.
        probe_lease: refuse when a live Vibe process owns the session (disable only for tooling).
        base_env: base environment for the child (defaults to Hermes' sanitized environment).

    Returns:
        :class:`LinkedVibeTurn` — the redacted rows the native journal gained, the echoed turn id,
        the appended file names/record count, and any ``Tool call denied:`` warnings.

    Raises:
        LinkedVibeUnavailable: no pointer, unusable store/cwd, or a missing Vibe CLI.
        LinkedVibeConflict: another writer owns the thread, or the journal did not grow by this
            turn (including a repeated sequence or a rewritten/truncated journal file).
        VibeRunFailed / VibeApprovalRequired / VibeRunTimeout / VibeRunCancelled /
        VibeOutputUnreadable: the run itself failed; nothing was persisted by Hermes.
        ValueError: the pointer row itself is damaged (e.g. no native session id recorded).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise LinkedVibeUnavailable("A message is required to send to the Vibe thread")
    thread = linked_vibe_thread(store, session_id, profile_home=profile_home)
    try:
        binary = resolve_vibe_binary(config)
    except VibeResumeError as exc:
        raise LinkedVibeUnavailable(str(exc)) from exc
    warnings: list[str] = []
    with _turn_lock(lock_dir, thread.lease_key, lock_wait_seconds):
        if probe_lease:
            holder = _native_lease_holder(thread.store_root, thread.lease_key)
            if holder is not None:
                raise LinkedVibeConflict(_lease_conflict_message(thread, holder))
        before = _snapshot(thread.session_dir)
        if not before:
            raise LinkedVibeUnavailable("The Mistral session has no readable transcript yet")
        env = child_env_for_vibe_store(thread.store_root, base_env)
        run = run_vibe_resume(binary=binary, prompt=prompt,
                              session_id=thread.native_session_id, workdir=thread.cwd,
                              output=output, timeout=timeout, env=env,
                              cancel_event=cancel_event)
        raise_for_vibe_failure(run, session_id=thread.native_session_id)
        warnings.extend(vibe_denials(run.stderr))
        appended, turns, journal_turn_id, grown = _verify_and_project(thread.session_dir, before)

    reported = _parse_reported_turn(run.stdout, thread, warnings)
    if appended == 0:
        raise LinkedVibeConflict(
            f"Vibe exited without appending this turn to session {thread.native_session_id}; "
            "the native transcript is unchanged")
    if not turns:
        raise LinkedVibeConflict(
            f"Vibe appended {appended} record(s) to session {thread.native_session_id} but none "
            "was a message; the turn is not readable from the native store")
    messages = [{"role": role, "content": safe_native_text(text)} for role, text in turns]
    assistant = [row["content"] for row in messages if row["role"] == "assistant"]
    return LinkedVibeTurn(
        session_id=thread.session_id,
        native_session_id=thread.native_session_id,
        turn_id=reported.turn_id or journal_turn_id,
        assistant_text=assistant[-1] if assistant else "",
        messages=messages,
        journal_files=grown,
        appended_records=appended,
        duration_s=run.duration_s,
        warnings=tuple(warnings),
    )


def _parse_reported_turn(stdout: str, thread: LinkedVibeThread, warnings: list[str]) -> VibeTurnOutput:
    """Parse what the run said about the turn and enforce the no-fork identity check."""
    try:
        reported = parse_vibe_output(stdout)
    except VibeOutputUnreadable as exc:
        # The native journal is authoritative for rows, so an unparsable envelope is a warning,
        # not a failed turn -- but the no-fork check then has nothing to compare, so say so.
        warnings.append(f"Could not parse the Vibe output: {exc}")
        return VibeTurnOutput()
    accepted = {thread.native_session_id, thread.lease_key}
    foreign = {sid for sid in reported.session_ids if sid not in accepted}
    if foreign:
        raise LinkedVibeConflict(
            f"Vibe answered session {min(foreign)!r} instead of "
            f"{thread.native_session_id!r}; refusing a forked thread")
    return reported
