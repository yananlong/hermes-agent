"""Claude Code live-session peer messaging: append to a RUNNING claude thread.

Empirically verified protocol (claude 2.1.328, Linux — decompiled from the shipped bundle and
probed against a disposable live session; see ``tests/hermes_cli/test_claude_peer_messaging.py``):
Claude Code exposes one Unix-domain socket per live session
(``~/.claude/sessions/<pid>.json::messagingSocketPath``) speaking newline-delimited JSON with a
1 MiB line cap and a 30 s first-line deadline. A frame
``{"type":"user","message":{"role":"user","content":...}}`` is queued into that session's own
prompt queue **without forking its transcript** — which is why a second ``claude -p --resume``
against a live session is forbidden (anthropics/claude-code#97833); the peer socket is the only
concurrent append path. Receipts (``peer_message_status``: held/denied/delivered/refused/dropped/
expired) arrive as control frames on OUR reply socket, named by the ``orig_msg_id`` we minted,
when the receiver holds or refuses the message; a plain accept is silent and the native
transcript's growth is the proof of delivery.

Auth: on Linux the receiver accepts unauthenticated peer frames by default (``authRequired`` is
Windows-only in 2.1.328); sending the receiver's own ``peerToken`` is unnecessary and would bind
us to an undocumented key-file layout, so we don't.

Design rules that hold everywhere in this module:
* The registry and the transcript are another process's files: every read is tolerant (torn JSON,
  a mid-rewrite registry, a vanished process) and the only write in this module is to OUR OWN
  reply socket dir, never to ``~/.claude``.
* Liveness is kernel truth, not a filename: a registry row counts only while its PID exists.
* The reply socket lives in Claude's own allowed reply dirs (``/tmp/cc-socks-<uid>``,
  ``/run/user/<uid>/cc-socks``) and is named ``<our pid>.sock``, or the receiver cannot route
  receipts back; outside those dirs delivery still works but every hold/refusal would be silent,
  so this module refuses rather than send into a void we cannot hear back from.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: Wire values the receiver's ``peer_message_status`` control frames carry (verified 2.1.328).
RECEIPT_HELD = "held"
RECEIPT_DELIVERED = "delivered"
RECEIPT_DENIED = "denied"
RECEIPT_EXPIRED = "expired"
RECEIPT_REFUSED = "refused"
RECEIPT_DROPPED = "dropped"

#: Typed error codes this module raises; ``linked_foreign_turn`` surfaces them to the user.
PEER_NOT_LIVE = "claude_peer_not_live"
PEER_SOCKET_MISSING = "claude_peer_socket_missing"
PEER_CONNECT_FAILED = "claude_peer_connect_failed"
PEER_HELD = "claude_peer_held"
PEER_REFUSED = "claude_peer_refused"
PEER_DROPPED = "claude_peer_dropped"
PEER_NO_REPLY_SOCKET = "claude_peer_no_reply_socket"
PEER_SEND_FAILED = "claude_peer_send_failed"

#: Receipt outcomes that mean the message did NOT reach the agent, mapped to a typed error code
#: (a refusal family collapsed: ``denied``/``refused``/``expired`` report one outcome in the
#: receiver's own ``peer_message_status`` handling — ``expired(refused)`` is refused).
_RECEIPT_ERROR_CODES: dict[str, str] = {
    RECEIPT_HELD: PEER_HELD,
    RECEIPT_DENIED: PEER_REFUSED,
    RECEIPT_REFUSED: PEER_REFUSED,
    RECEIPT_EXPIRED: PEER_REFUSED,
    RECEIPT_DROPPED: PEER_DROPPED,
}

#: Wire frame cap (the receiver destroys connections over 1 MiB; keep a margin for JSON overhead).
_MAX_FRAME_BYTES = 1024 * 1024 - 2048

#: How long the receiver may take to answer our frame with a receipt. A held message waits for
#: the receiving user's interactive approval, so this spans one hold decision, not just a network RTT.
_RECEIPT_DEADLINE_S = 12.0

#: How long one receipt CONNECTION may idle before we stop reading it (frame-per-connection).
_RECEIPT_CONN_IDLE_S = 6.0

#: Bound on the bytes one receipt connection may deliver us before we stop reading it.
_RECEIPT_MAX_BYTES = 4 * 1024 * 1024

#: Reply socket namespace: only these dirs let the receiver route receipts back (the receiver's
#: own path allowlist: ``/tmp/cc-socks*``, ``/run/user/<uid>/cc-socks``; bundle constant ``qJ``).
#: The basename shape ``<pid>.sock`` is what its validator expects. This is Claude Code's fixed
#: namespace, not Hermes scratch space.
_REPLY_DIR_CANDIDATES = ("/tmp/cc-socks-{uid}", "/run/user/{uid}/cc-socks")  # no-tmp: ok -- Claude Code's own receipt reply dirs (bundle-allowlisted), not Hermes scratch


class ClaudePeerError(RuntimeError):
    """A typed refusal from the peer path; ``code`` is a stable wire value.

    ``receipt`` carries the final ``peer_message_status`` verdict when one arrived, so the
    linked-turn caller can surface the real outcome ("held for the user's approval") instead of
    pretending the send succeeded.
    """

    def __init__(self, code: str, detail: str, *, receipt: str | None = None) -> None:
        self.code = code
        self.detail = detail
        self.receipt = receipt
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class LiveClaudeSession:
    """One live Claude Code session as its registry row describes it."""

    pid: int
    session_id: str
    socket_path: str
    cwd: str | None
    status: str | None
    started_at_ms: int | None
    registry_mtime: float


def resolve_claude_sessions_dir(profile_home: Any = None) -> Path:
    """The live-session registry dir owned by this profile's Claude install.

    The registry is Claude's own state dir (``CLAUDE_CONFIG_DIR`` / ``~/.claude``) + ``sessions``,
    never written by Hermes. Profile scoping matches
    :func:`hermes_cli.foreign_linked_store.resolve_foreign_store`, so a routed profile reads only
    the Claude config dir it configured.
    """
    from hermes_cli.foreign_linked_store import resolve_foreign_store

    return resolve_foreign_store("CLAUDE_CONFIG_DIR", (".claude",), "sessions", profile_home)


def _read_registry_row(path: Path) -> dict | None:
    """Parse one registry JSON file, tolerant of a mid-rewrite torn state."""
    try:
        with path.open("rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        obj = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _pid_alive(pid: int) -> bool:
    """PID liveness by psutil (the repo's footgun-safe answer to ``os.kill(pid, 0)``, bpo-14484).

    The registry only exists on POSIX claude installs, but the reader stays import-safe on
    Windows: the module has no side effects at import, and a host with no registry dir simply
    lists no live sessions.
    """
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except (ImportError, ValueError, OSError):
        return False


def _current_uid() -> int:
    """This process's uid for the reply-socket namespace (POSIX-only path component)."""
    getter = getattr(os, "getuid", None)
    uid = getter() if callable(getter) else None
    return uid if isinstance(uid, int) else -1  # -1 never matches a real uid template fill


def list_live_claude_sessions(profile_home: Any = None) -> list[LiveClaudeSession]:
    """Every live Claude session this profile's registry knows, newest first.

    A registry file only proves a session WAS live; the PID must still exist. Claude removes the
    row on a clean exit, so a remaining row for a dead PID is a crashed session and must not be
    messaged.
    """
    try:
        sessions_dir = resolve_claude_sessions_dir(profile_home)
    except (OSError, ValueError):
        return []
    rows: list[LiveClaudeSession] = []
    try:
        entries = list(sessions_dir.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        row = _read_registry_row(path)
        if row is None:
            continue
        pid = row.get("pid")
        session_id = row.get("sessionId")
        sock = row.get("messagingSocketPath")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        if not _pid_alive(pid):
            continue
        if not isinstance(session_id, str) or not session_id:
            continue
        if not isinstance(sock, str) or not sock:
            continue
        started = row.get("startedAt")
        started_ms = started if isinstance(started, int) and not isinstance(started, bool) and started > 0 else None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append(LiveClaudeSession(
            pid=pid, session_id=session_id, socket_path=sock,
            cwd=row.get("cwd") if isinstance(row.get("cwd"), str) else None,
            status=row.get("status") if isinstance(row.get("status"), str) else None,
            started_at_ms=started_ms, registry_mtime=mtime))
    rows.sort(key=lambda r: (r.started_at_ms or 0, r.registry_mtime), reverse=True)
    return rows


def live_claude_session(session_id: str, profile_home: Any = None) -> LiveClaudeSession | None:
    """The live registry row for one Claude session id, or ``None`` when it is not live."""
    from hermes_cli.foreign_linked_store import foreign_session_uuid

    native = foreign_session_uuid(session_id)
    for row in list_live_claude_sessions(profile_home):
        if row.session_id == native:
            return row
    return None


class _ReplyServer:
    """Our receipts endpoint: a UDS in one of the receiver's allowed reply dirs.

    The receiver dials us back on a NEW connection to this path for each receipt. The path must
    be ``<pid>.sock`` in an allowlisted dir owned by this uid; otherwise the receiver logs
    ``hold-receipt skipped: reply address ... outside our socket namespace`` and we would never
    learn a hold or refusal happened.
    """

    def __init__(self) -> None:
        self.path: Path | None = None
        self.server: socket.socket | None = None

    def open(self) -> Path:
        last_error: OSError | None = None
        for template in _REPLY_DIR_CANDIDATES:
            directory = Path(template.format(uid=_current_uid()))
            server: socket.socket | None = None
            try:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(directory, 0o700)
                _sweep_stale_reply_sockets(directory)
                path = directory / f"{os.getpid()}.sock"
                with contextlib.suppress(OSError):
                    path.unlink()
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(path))
                os.chmod(path, 0o600)
                server.listen(8)
                server.settimeout(0.2)
                self.path, self.server = path, server
                return path
            except OSError as exc:
                last_error = exc
                if server is not None:
                    with contextlib.suppress(OSError):
                        server.close()
        detail = f"cannot bind a reply socket ({_errno_token(last_error)})" if last_error \
            else "no allowed reply directory was usable"
        raise ClaudePeerError(PEER_NO_REPLY_SOCKET, detail)

    def close(self) -> None:
        if self.server is not None:
            with contextlib.suppress(OSError):
                self.server.close()
            self.server = None
        if self.path is not None:
            with contextlib.suppress(OSError):
                self.path.unlink()
            self.path = None


_REPLY_SOCK_NAME = re.compile(r"^(\d+)\.sock$")


def _sweep_stale_reply_sockets(directory: Path) -> None:
    """Drop ``<pid>.sock`` leftovers whose process is gone (a crashed sender leaves them)."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for path in entries:
        match = _REPLY_SOCK_NAME.match(path.name)
        if match is None:
            continue
        pid = int(match.group(1))
        if pid == os.getpid() or _pid_alive(pid):
            continue
        with contextlib.suppress(OSError):
            path.unlink()


def _drain_receipts(server: socket.socket, deadline_s: float, on_frame: Callable[[dict], None]) -> None:
    """Accept receipt connections and dispatch complete NDJSON frames until the deadline."""
    while time.monotonic() < deadline_s:
        try:
            conn, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            return  # the receiver's dir went away; nothing more can arrive
        with conn:
            conn.settimeout(0.2)
            buf = b""
            idle_cutoff = time.monotonic() + _RECEIPT_CONN_IDLE_S
            while time.monotonic() < min(deadline_s, idle_cutoff):
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _RECEIPT_MAX_BYTES:
                    break
                while True:
                    cut = buf.find(b"\n")
                    if cut < 0:
                        break
                    line, buf = buf[:cut], buf[cut + 1:]
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line.decode("utf-8", errors="strict"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(frame, dict):
                        on_frame(frame)
                        idle_cutoff = time.monotonic() + _RECEIPT_CONN_IDLE_S


def _receipt_matches(frame: dict, msg_id: str) -> bool:
    """The receiver names our send in ``orig_msg_id``; its own ``msg_id`` is theirs, not ours."""
    return (frame.get("type") == "control" and frame.get("action") == "peer_message_status"
            and str(frame.get("orig_msg_id") or "") == msg_id)


@dataclass(frozen=True)
class PeerSendResult:
    """How one peer send ended."""

    msg_id: str
    #: ``sent`` — accepted, no refusal receipt (the transcript is the delivery proof);
    #: ``delivered`` — the receiver explicitly receipted it delivered (e.g. after a hold lift).
    status: str
    receipt: dict | None
    #: The registry row the send went through (display: pid/status of the live session).
    live: LiveClaudeSession


def send_peer_prompt(session_id: str, prompt: str, *, profile_home: Any = None,
                     reply_wait_s: float = _RECEIPT_DEADLINE_S,
                     cancel_event: Any = None) -> PeerSendResult:
    """Append *prompt* to a LIVE Claude session through its messaging socket.

    Returns :class:`PeerSendResult` when the frame was accepted (``status="sent"`` — the
    receiver holds/refusals always receipt, so silence after a clean connect means queued; the
    caller proves landing from the transcript — or ``status="delivered"`` when an explicit
    receipt arrived). Raises :class:`ClaudePeerError` for a held/denied/refused/dropped/expired
    receipt and for transport failures (no live session, socket gone, connect refused).

    Never writes the transcript, the registry, or any ``~/.claude`` file: the live claude owns
    the append. That is the concurrency this channel exists for.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ClaudePeerError(PEER_SEND_FAILED, "a peer prompt must be non-empty text")
    live = live_claude_session(session_id, profile_home)
    if live is None:
        raise ClaudePeerError(PEER_NOT_LIVE,
                              f"Claude session {session_id} has no live process on this host")
    try:
        st = os.stat(live.socket_path)
    except OSError:
        raise ClaudePeerError(PEER_SOCKET_MISSING,
                              "the live session's messaging socket is gone") from None
    if not stat.S_ISSOCK(st.st_mode):
        raise ClaudePeerError(PEER_SOCKET_MISSING, "the messaging path is not a socket")

    msg_id = str(uuid.uuid4())
    frame = {"type": "user", "message": {"role": "user", "content": prompt},
             "priority": "next", "msg_id": msg_id, "msgV": 1}
    reply = _ReplyServer()
    receipts: list[dict] = []
    try:
        path = reply.open()
        server = reply.server
        assert server is not None  # open() binds before it returns
        frame["from"] = f"uds:{path}"
        encoded = (json.dumps(frame, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        if len(encoded) > _MAX_FRAME_BYTES:
            raise ClaudePeerError(PEER_SEND_FAILED, "the prompt exceeds the peer frame size limit")

        def on_frame(rec: dict) -> None:
            if _receipt_matches(rec, msg_id):
                receipts.append(rec)

        # HX012: the reader thread must inherit this turn's profile scope, not the default one.
        from agent.memory_provider import spawn_context_thread

        reader = spawn_context_thread(
            _drain_receipts, name="claude-peer-receipts",
            args=(server, time.monotonic() + reply_wait_s, on_frame))

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10.0)
        try:
            client.connect(live.socket_path)
            reader.start()
            client.sendall(encoded)
        except OSError as exc:
            raise ClaudePeerError(
                PEER_CONNECT_FAILED,
                f"cannot reach the live session's messaging socket ({_errno_token(exc)})") from None
        finally:
            with contextlib.suppress(OSError):
                client.close()

        deadline = time.monotonic() + reply_wait_s
        while time.monotonic() < deadline and not receipts:
            if cancel_event is not None and callable(getattr(cancel_event, "is_set", None)) \
                    and cancel_event.is_set():
                raise ClaudePeerError(PEER_SEND_FAILED, "send cancelled before the receiver answered")
            time.sleep(0.05)
    finally:
        reply.close()

    if not receipts:
        return PeerSendResult(msg_id=msg_id, status="sent", receipt=None, live=live)
    receipt = receipts[0]
    status = receipt.get("status")
    if status == RECEIPT_DELIVERED:
        return PeerSendResult(msg_id=msg_id, status=RECEIPT_DELIVERED, receipt=receipt, live=live)
    reported = status if isinstance(status, str) else "unknown"
    code = _RECEIPT_ERROR_CODES.get(reported, PEER_REFUSED)
    raise ClaudePeerError(
        code, str(receipt.get("reason") or f"the receiving session reported {reported}"),
        receipt=reported)


def _errno_token(exc: OSError) -> str:
    """A stable errno token (``EACCES``) without leaking host paths into a refusal."""
    import errno as _errno

    if isinstance(exc.errno, int):
        token = _errno.errorcode.get(exc.errno)
        if token:
            return token
        return str(exc.errno)
    return str(exc.errno or "unknown")
