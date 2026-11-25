"""BYOT (bring your own terminal) routes.

An external terminal (e.g. iTerm on macOS running zsh) runs ``hermes byot``,
which opens the ``/api/byot/host`` WebSocket and streams its local PTY bytes.
The dashboard lists live external terminals at ``GET /api/byot/sessions``; the
Chat tab attaches to one through the ordinary ``/api/pty?byot=<id>`` socket,
which proxies the browser stream to the registered host instead of spawning
the embedded ``hermes --tui`` PTY (see ``web_routers/chat_ws.pty_ws``).

Wire protocol (same conventions as ``/api/pty``):

* host -> server: first text frame is a JSON ``{"type": "hello", ...}``
  registration; afterwards binary (or UTF-8 text) frames are terminal output
  and are broadcast to every attached viewer. A text frame that parses as a
  JSON object with ``"type"`` is a control frame (``resize`` updates the
  advertised size) and is NOT forwarded to viewers.
* server -> host: binary/text frames are keystrokes from viewers; a JSON
  ``{"type": "resize", "cols", "rows"}`` text frame asks the host to resize
  its local PTY (a stdio bridge may ignore it).
* viewer -> server (``/api/pty?byot=<id>``): raw input bytes, plus the usual
  ``\\x1b[RESIZE:<cols>;<rows>]`` escape which the server converts to a
  ``resize`` control frame for the host. Output from the host is forwarded
  verbatim as binary frames.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from hermes_cli.web_deps import late
from hermes_cli.web_server_chat import _RESIZE_RE, _ws_close_reason, _ws_gate

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_require_token = late("_require_token")

# Close code a viewer sees when the external terminal disconnects — same
# "process exited" code the keep-alive PTY path uses, so the Chat tab renders
# its "session ended" overlay.
BYOT_CLOSE_HOST_GONE = 4410

_HELLO_TIMEOUT_SECONDS = 10.0
_NAME_RE = re.compile(r"^[\w .@:-]{1,64}$", re.UNICODE)


class ByotHost:
    """One registered external terminal: its host socket plus attached viewers."""

    def __init__(self, ws: WebSocket, meta: dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex
        self.ws = ws
        self.meta = meta
        self.created_at = time.time()
        self.viewers: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.meta.get("name") or self.id,
            "shell": self.meta.get("shell"),
            "cwd": self.meta.get("cwd"),
            "pid": self.meta.get("pid"),
            "cols": self.meta.get("cols"),
            "rows": self.meta.get("rows"),
            "viewers": len(self.viewers),
            "created_at": self.created_at,
        }


class ByotRegistry:
    """Process-local registry of connected external terminals."""

    def __init__(self) -> None:
        self._hosts: dict[str, ByotHost] = {}
        self._lock = asyncio.Lock()

    async def register(self, host: ByotHost) -> None:
        async with self._lock:
            self._hosts[host.id] = host

    async def unregister(self, byot_id: str) -> Optional[ByotHost]:
        async with self._lock:
            return self._hosts.pop(byot_id, None)

    async def get(self, byot_id: str) -> Optional[ByotHost]:
        async with self._lock:
            return self._hosts.get(byot_id)

    async def list(self) -> builtins.list[ByotHost]:
        async with self._lock:
            return sorted(self._hosts.values(), key=lambda h: h.created_at)

    async def attach(self, host: ByotHost, ws: WebSocket) -> None:
        async with host.lock:
            host.viewers.add(ws)

    async def detach(self, host: ByotHost, ws: WebSocket) -> None:
        async with host.lock:
            host.viewers.discard(ws)

    async def broadcast(self, host: ByotHost, data: bytes) -> None:
        """Send terminal output to every attached viewer; drop dead ones."""
        async with host.lock:
            viewers = list(host.viewers)
        for viewer in viewers:
            try:
                await viewer.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError, OSError):
                await self.detach(host, viewer)

    async def send_to_host(self, host: ByotHost, data: bytes) -> bool:
        try:
            async with host.send_lock:
                await host.ws.send_bytes(data)
            return True
        except (WebSocketDisconnect, RuntimeError, OSError):
            return False

    async def send_control_to_host(
        self, host: ByotHost, control: dict[str, Any]
    ) -> bool:
        try:
            async with host.send_lock:
                await host.ws.send_text(json.dumps(control))
            return True
        except (WebSocketDisconnect, RuntimeError, OSError):
            return False


BYOT_REGISTRY = ByotRegistry()


def _sanitize_meta(hello: dict[str, Any]) -> dict[str, Any]:
    """Coerce the host hello frame into a safe metadata dict."""

    def _str(key: str, limit: int = 512) -> Optional[str]:
        value = hello.get(key)
        if value is None:
            return None
        value = str(value)[:limit]
        return value or None

    def _int(key: str) -> Optional[int]:
        value = hello.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    name = _str("name", 64)
    if name is not None and not _NAME_RE.match(name):
        name = None
    return {
        "name": name or f"terminal-{os.getpid()}",
        "shell": _str("shell", 256),
        "cwd": _str("cwd", 1024),
        "pid": _int("pid"),
        "cols": _int("cols"),
        "rows": _int("rows"),
    }


@router.get("/api/byot/sessions")
async def list_byot_sessions(request: Request) -> dict[str, Any]:
    """Live external terminals the Chat tab can attach to via ``?byot=<id>``."""
    _require_token(request)
    hosts = await BYOT_REGISTRY.list()
    return {"sessions": [h.public() for h in hosts]}


@router.websocket("/api/byot/host")
async def byot_host_ws(ws: WebSocket) -> None:
    """Host socket for ``hermes byot`` running in the external terminal."""
    gate = await _ws_gate(ws, "byot")
    if gate is None:
        return
    peer, mode, cred = gate
    await ws.accept()

    try:
        hello_raw = await asyncio.wait_for(
            ws.receive_text(), timeout=_HELLO_TIMEOUT_SECONDS
        )
        hello = json.loads(hello_raw)
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            raise ValueError("first frame must be a JSON hello object")
    except WebSocketDisconnect:
        return
    except (TimeoutError, ValueError) as exc:
        await ws.close(code=4400, reason=_ws_close_reason(f"byot hello: {exc}"))
        return

    meta = _sanitize_meta(hello)
    host = ByotHost(ws, meta)
    await BYOT_REGISTRY.register(host)
    _log.info(
        "byot host registered id=%s name=%s shell=%s cwd=%s peer=%s mode=%s cred=%s",
        host.id,
        host.meta.get("name"),
        host.meta.get("shell"),
        host.meta.get("cwd"),
        peer,
        mode,
        cred,
    )
    await ws.send_text(json.dumps({"type": "registered", "id": host.id}))

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is not None:
                await BYOT_REGISTRY.broadcast(host, raw)
                continue
            text = msg.get("text")
            if not isinstance(text, str) or not text:
                continue
            # Text frames that parse as JSON objects are host control frames
            # (e.g. the bridge reporting its window size); anything else is
            # terminal output.
            try:
                control = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                control = None
            if isinstance(control, dict) and control.get("type"):
                if control.get("type") == "resize":
                    for key in ("cols", "rows"):
                        if isinstance(control.get(key), int):
                            host.meta[key] = control[key]
                continue
            await BYOT_REGISTRY.broadcast(host, text.encode("utf-8"))
    except WebSocketDisconnect:
        pass
    finally:
        await BYOT_REGISTRY.unregister(host.id)
        async with host.lock:
            viewers = list(host.viewers)
            host.viewers.clear()
        for viewer in viewers:
            try:
                await viewer.close(
                    code=BYOT_CLOSE_HOST_GONE, reason="External terminal disconnected"
                )
            except (WebSocketDisconnect, RuntimeError, OSError):
                _log.debug("BYOT viewer already closed")
        _log.info("byot host disconnected id=%s", host.id)


async def handle_byot_viewer(ws: WebSocket, byot_id: str) -> None:
    """Proxy a ``/api/pty?byot=<id>`` browser socket to an external terminal.

    Runs the viewer side of the BYOT stream: input (and resize escapes) from
    the browser go to the host; host output comes back as binary frames. The
    host session itself outlives this socket, so the browser can reconnect.
    """
    host = await BYOT_REGISTRY.get(byot_id)
    if host is None:
        await ws.send_text(
            "\r\n\x1b[31mExternal terminal is not connected. "
            "Run `hermes byot` in your terminal (e.g. iTerm), then attach again.\x1b[0m\r\n"
        )
        await ws.close(code=1011)
        return
    await BYOT_REGISTRY.attach(host, ws)
    await ws.send_json({"type": "byot", **host.public()})
    _log.info("byot viewer attached id=%s", host.id)
    try:
        while True:
            try:
                msg = await ws.receive()
            except RuntimeError:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue
            # Resize escapes become a control frame for the host (never raw
            # bytes into the external terminal), matching /api/pty's rule
            # that the escape is consumed locally.
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                await BYOT_REGISTRY.send_control_to_host(
                    host,
                    {
                        "type": "resize",
                        "cols": int(match.group(1)),
                        "rows": int(match.group(2)),
                    },
                )
                continue
            if not await BYOT_REGISTRY.send_to_host(host, raw):
                break
    except WebSocketDisconnect:
        pass
    finally:
        await BYOT_REGISTRY.detach(host, ws)
