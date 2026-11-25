"""Codex app-server daemon peer: WebSocket over the daemon's Unix-domain control socket.

``codex app-server daemon`` (codex 0.140+) hosts ONE app-server for every client on the
machine: the TUI, the VS Code extension, exec's one-shot runs. The daemon's control
socket is a WebSocket endpoint over a UDS (``<CODEX_HOME>/app-server-control/
app-server-control.sock`` — NOT raw JSON lines; ``codex app-server proxy`` cannot serve a
plain stdio client). Joining it instead of spawning a private ``codex app-server`` makes
Hermes one more modality on the SAME thread: ``thread/resume`` on an already-open thread
succeeds, while a second app-server process would fail the rollout's flock with     
``-32600 "already has an active writer"`` and could never append to a thread the TUI
is working on.

Wire surface mirrors :class:`CodexAppServerClient` exactly (request / notify / respond /
respond_error / take_notification / take_server_request / stderr_tail / is_alive /
close / context manager / initialize), so the session adapter and the linked-history
readers never learn which transport answered. There is no child process here: no
stderr exists (``stderr_tail`` returns the socket-diagnostic tail instead — a dropped
socket MUST stay visible, it is the daemon-died signal), and ``is_alive`` reports the
WebSocket connection.

Synchronous like the spawned client: the session poll loop blocks on queues, so this
client runs one asyncio loop on a daemon thread and shuttles frames between asyncio and
``queue.Queue``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any, Optional, Protocol

from agent.transports.codex_app_server import (
    CodexAppServerError,
    CodexAppServerTransportError,
    _TRANSPORT_LOST_CODE,
)

logger = logging.getLogger(__name__)


class CodexAppServerWire(Protocol):
    """The surface both app-server transports implement (spawned child, daemon peer).

    Declared here so the session adapter and the linked-history readers can hold
    "the client" without caring which transport answered — and so type checkers
    narrow ``Optional[...]`` on the same seam the runtime uses.
    """

    def initialize(self, client_name: str = ..., client_title: str = ...,
                   client_version: str = ..., capabilities: Optional[dict] = ...,
                   timeout: float = ...) -> dict: ...
    def request(self, method: str, params: Optional[dict] = None, timeout: float = ...) -> dict: ...
    def notify(self, method: str, params: Optional[dict] = None) -> None: ...
    def respond(self, request_id: Any, result: dict) -> None: ...
    def respond_error(self, request_id: Any, code: int, message: str,
                      data: Optional[Any] = None) -> None: ...
    def take_notification(self, timeout: float = ...) -> Optional[dict]: ...
    def take_server_request(self, timeout: float = ...) -> Optional[dict]: ...
    def stderr_tail(self, n: int = ...) -> list[str]: ...
    def is_alive(self) -> bool: ...
    def close(self, timeout: float = ...) -> None: ...


#: ``codex-rs/app-server-transport/src/transport/mod.rs::app_server_control_socket_path``
#: lays the control socket here, under the CODEX_HOME it serves.
_CONTROL_DIR = "app-server-control"
_CONTROL_SOCKET = "app-server-control.sock"

#: Handshake + initialize must land well inside any caller's request timeout; a daemon
#: that cannot answer these is not a daemon this client may join.
_CONNECT_TIMEOUT = 10.0

_WS_MAX_FRAME_BYTES = 512 * 1024 * 1024  # multi-megabyte history pages ride one frame


def app_server_control_socket_path(codex_home) -> Path:
    """The daemon's WebSocket control socket path for ``codex_home``."""
    return Path(codex_home) / _CONTROL_DIR / _CONTROL_SOCKET


def _try_error(exc: BaseException, prefix: str) -> CodexAppServerTransportError:
    detail = " ".join(str(exc).split()) or type(exc).__name__
    return CodexAppServerTransportError(code=_TRANSPORT_LOST_CODE, message=f"{prefix}: {detail}")


class _LoopThread:
    """One asyncio loop on a daemon thread, callable from any thread.

    The session's poll loop is synchronous; this bridge is the ONLY async surface here.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> asyncio.AbstractEventLoop:
        if self._thread is not None:
            return self._loop  # type: ignore[return-value]
        self._thread = threading.Thread(target=self._run, name="codex-daemon-peer", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=_CONNECT_TIMEOUT):
            raise CodexAppServerTransportError(
                code=_TRANSPORT_LOST_CODE, message="codex daemon peer loop did not start")
        return self._loop  # type: ignore[return-value]

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def call(self, coro, timeout: Optional[float] = None):
        """Block until ``coro`` (scheduled onto the peer loop) resolves/raises."""
        assert self._loop is not None and self._thread is not None
        if not self._thread.is_alive() or self._loop.is_closed():
            raise CodexAppServerTransportError(
                code=_TRANSPORT_LOST_CODE, message="codex daemon peer loop is stopped")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stop(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_CONNECT_TIMEOUT)
        self._loop = None


class CodexDaemonPeerClient:
    """JSON-RPC 2.0 peer on the daemon's WebSocket-over-UDS control connection.

    One background reader task routes replies to pending queues and notifications /
    server requests to queues, exactly like the spawned client's stdout reader thread.
    """

    def __init__(self, socket_path: Optional[str] = None, *, codex_home: Optional[str] = None,
                 spill_lines: int = 500) -> None:
        if socket_path is None:
            if not codex_home:
                raise ValueError("CodexDaemonPeerClient needs a socket path or a codex home")
            socket_path = str(app_server_control_socket_path(codex_home))
        self.socket_path = str(socket_path)
        self._spill_lines = spill_lines
        self._next_id = 1
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue = queue.Queue()
        self._server_requests: queue.Queue = queue.Queue()
        self._diagnostics: list[str] = []
        self._closed = False
        self._connection_open = True  # reader flips it when the socket actually drops
        self._initialized = False
        self._reader_task: Optional[asyncio.Task] = None  # loop-thread state
        self._loop = _LoopThread()
        self._diag_lock = threading.Lock()
        # Connect + reader now: construction failing IS the "daemon absent" signal
        # the caller falls back on. start() spins the loop, _connect() performs the
        # WebSocket handshake, then the reader task is created ON that loop.
        self._loop.start()
        self._ws = self._loop.call(self._connect(), timeout=_CONNECT_TIMEOUT)
        self._reader_task = self._loop.call(_spawn_reader(self), timeout=_CONNECT_TIMEOUT)

    # -- lifecycle ---------------------------------------------------------------

    async def _connect(self):
        import websockets

        try:
            ws = await asyncio.wait_for(
                websockets.unix_connect(self.socket_path, max_size=_WS_MAX_FRAME_BYTES),
                timeout=_CONNECT_TIMEOUT)
        except Exception as exc:
            raise _try_error(exc, "could not connect to the codex daemon control socket") from exc
        return ws

    def initialize(self, client_name: str = "hermes", client_title: str = "Hermes Agent",
                   client_version: str = "0.1", capabilities: Optional[dict] = None,
                   timeout: float = 10.0) -> dict:
        """Same handshake as the spawned client (initialize + initialized)."""
        if self._initialized:
            raise RuntimeError("already initialized")
        params = {
            "clientInfo": {"name": client_name, "title": client_title, "version": client_version},
            "capabilities": capabilities or {},
        }
        result = self.request("initialize", params, timeout=timeout)
        self.notify("initialized")
        self._initialized = True
        return result

    def close(self, timeout: float = 3.0) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending_requests("codex daemon peer is closing")
        task = self._reader_task
        ws = self._ws
        self._reader_task = None
        self._ws = None
        try:
            if ws is not None or task is not None:
                self._loop.call(self._teardown(ws, task), timeout=timeout)
        except Exception:  # pragma: no cover - teardown best-effort
            pass
        self._loop.stop()

    async def _teardown(self, ws, task) -> None:
        if task is not None:
            task.cancel()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover - teardown best-effort
                pass

    def __enter__(self) -> "CodexDaemonPeerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- wire --------------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        if self._closed or self._ws is None:
            raise CodexAppServerTransportError(
                code=_TRANSPORT_LOST_CODE, message="codex daemon peer is closed")
        try:
            self._loop.call(self._ws_send(obj), timeout=_CONNECT_TIMEOUT)
        except CodexAppServerTransportError:
            raise
        except Exception as exc:
            raise _try_error(exc, "codex daemon peer write failed") from exc

    async def _ws_send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj))

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        rid, self._next_id = self._next_id, self._next_id + 1
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = q
        try:
            self._send({"id": rid, "method": method, "params": params or {}})
        except CodexAppServerTransportError:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"codex daemon method {method!r} timed out after {timeout}s")
        if "error" in msg:
            err = msg["error"]
            cls = CodexAppServerTransportError if msg.get("transportLost") else CodexAppServerError
            raise cls(code=err.get("code", -1), message=err.get("message", ""), data=err.get("data"))
        return msg.get("result", {})

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._send({"method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict) -> None:
        self._send({"id": request_id, "result": result})

    def respond_error(self, request_id: Any, code: int, message: str, data: Optional[Any] = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"id": request_id, "error": err})

    # -- intake ------------------------------------------------------------------

    def _fail_pending_requests(self, reason: str) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.items())
            self._pending.clear()
        if not pending_items:
            return
        synthetic = {"error": {"code": _TRANSPORT_LOST_CODE, "message": reason}, "transportLost": True}
        for _rid, pending in pending_items:
            pending.put_nowait(synthetic)

    def _dispatch(self, msg: dict) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._pending_lock:
                pending = self._pending.pop(msg["id"], None)
            if pending is not None:
                pending.put_nowait(msg)
        elif "method" in msg:
            (self._server_requests if "id" in msg else self._notifications).put(msg)

    def _note_diagnostic(self, line: str) -> None:
        with self._diag_lock:
            self._diagnostics.append(line)
            if len(self._diagnostics) > self._spill_lines:
                self._diagnostics = self._diagnostics[-self._spill_lines:]

    def take_notification(self, timeout: float = 0.0) -> Optional[dict]:
        return _take(self._notifications, timeout)

    def take_server_request(self, timeout: float = 0.0) -> Optional[dict]:
        return _take(self._server_requests, timeout)

    def stderr_tail(self, n: int = 20) -> list[str]:
        """No child process exists; the socket-diagnostic tail stands in (daemon lost etc.)."""
        with self._diag_lock:
            return list(self._diagnostics[-n:])

    def is_alive(self) -> bool:
        """The WebSocket connection is the transport's liveness (a closed socket is a
        dead peer: the session loop must retire instead of waiting on the deadline)."""
        return self._connection_open and not self._closed


def _take(q: queue.Queue, timeout: float) -> Optional[dict]:
    try:
        return q.get_nowait() if timeout <= 0 else q.get(timeout=timeout)
    except queue.Empty:
        return None


async def _spawn_reader(peer: "CodexDaemonPeerClient") -> asyncio.Task:
    """Create the reader task on the peer's loop; returns it for close() to cancel."""
    return asyncio.create_task(_read_peer_frames(peer))


async def _read_peer_frames(peer: "CodexDaemonPeerClient") -> None:
    """Pump every WebSocket frame into the dispatch queues; EOF fails pending requests."""
    try:
        async for raw in peer._ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError) as exc:
                peer._note_diagnostic(f"<non-json frame> {str(raw)[:200]!r}")
                logger.debug("codex daemon peer frame not JSON: %s", exc)
                continue
            peer._dispatch(msg)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        peer._note_diagnostic(f"<peer connection lost> {exc}")
        logger.info("codex daemon peer connection ended: %s", exc)
    finally:
        # close() tears its own connection down; only an unexpected drop marks the
        # transport dead so the session loop retires on the next poll.
        if not peer._closed:
            peer._connection_open = False
        peer._fail_pending_requests("codex daemon peer connection closed")


def connect_daemon_peer(codex_home: Path | str, *,
                        client_version: str = "0.1") -> Optional[CodexDaemonPeerClient]:
    """Join the daemon at ``codex_home`` or return None (hidden daemon, old codex, dead socket).

    The one probe that matters is the real wire handshake: the socket file can be a
    leftover from a crashed daemon, so ``initialize`` must succeed before a caller
    trusts the peer. None always means "spawn instead" — never raise to a caller that
    is allowed to fall back.
    """
    socket_path: Optional[str] = None
    try:
        candidate = app_server_control_socket_path(codex_home)
        if candidate.exists():
            socket_path = str(candidate)
    except OSError:
        return None
    if socket_path is None:
        return None
    peer = CodexDaemonPeerClient(socket_path=socket_path)
    try:
        peer.initialize(client_name="hermes", client_title="Hermes Agent", client_version=client_version)
        logger.info("joined the codex app-server daemon: socket=%s", socket_path)
        return peer
    except Exception as exc:
        logger.debug("codex daemon peer probe failed (%s); spawning instead", exc)
        try:
            peer.close()
        except Exception:  # pragma: no cover - diagnostic best-effort
            pass
        return None


def resolve_probe_codex_home(codex_home: Optional[str] = None) -> Optional[Path]:
    """The CODEX_HOME the spawned client would use, for daemon probing.

    Same precedence as ``CodexAppServerClient``: explicit argument, then the active
    profile scope's configured ``CODEX_HOME``, then the environment, then the shared
    ``~/.codex`` default. A ROUTED profile that has not named its own home returns None
    (fail closed, matching the spawn site's own "routed profile must configure
    CODEX_HOME" refusal) instead of silently probing the launch profile's Codex store.
    """
    explicit = str(codex_home or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    configured: object = None
    try:
        from agent.secret_scope import get_secret

        configured = get_secret("CODEX_HOME")
    except Exception:  # unscoped multiplex context: the spawn site raises the real error
        configured = None
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip()).expanduser()
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    try:
        from hermes_constants import get_hermes_home
        from tools.environments.local import _is_routed_home

        if _is_routed_home(get_hermes_home()):
            return None
    except Exception:  # pragma: no cover - launch-scope resolution must not block probing
        pass
    return Path.home() / ".codex"


def join_codex_daemon(codex_home: Optional[str] = None, *,
                      client_version: str = "0.1") -> Optional[CodexDaemonPeerClient]:
    """The daemon-join selection used by every app-server call site.

    Tests run hermetically under ``HERMES_TEST_ISOLATION`` and must never join a
    developer's real daemon: the conftest does not blank CODEX_HOME, so without this
    guard a daemon left running on the dev box would route test traffic into a live
    app-server. A test that wants the peer clears the var and points ``codex_home``
    at its own fake daemon socket.
    """
    if os.environ.get("HERMES_TEST_ISOLATION"):
        return None
    home = resolve_probe_codex_home(codex_home)
    if home is None:
        return None
    return connect_daemon_peer(home, client_version=client_version)
