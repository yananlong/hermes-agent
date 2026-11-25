"""Daemon-join transport selection and wire behaviour for the Codex app-server.

The daemon's control socket is a WebSocket endpoint over a Unix-domain socket; a plain
JSON-lines client cannot speak to it, so ``codex app-server proxy`` is not a path. These
contracts pin the two things every caller depends on:

* **Selection** — a reachable daemon answers as a peer; every other case (no socket,
  stale socket file, handshake refused, tests under ``HERMES_TEST_ISOLATION``) falls
  back to spawning ``codex app-server`` unchanged.
* **Wire parity** — the peer answers ``request``/``respond``/``take_notification`` with
  the same shapes the spawned client produces, and concurrent peers subscribed to one
  thread both observe the daemon's fan-out.

A real WebSocket server on a UDS in ``tmp_path`` plays the daemon (no network: the test
fence only guards off-host lookups).
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest

import agent.transports.codex_app_server_daemon as daemon_mod
from agent.transports.codex_app_server import (
    CodexAppServerError,
    CodexAppServerTransportError,
)
from agent.transports.codex_app_server_daemon import (
    CodexDaemonPeerClient,
    join_codex_daemon,
)
from tests.waiting import wait_until

TARGET_THREAD = "01a212d9-09be-7fb3-b8c7-90082e4fb805"


class _FakeDaemon:
    """A WebSocket-over-UDS JSON-RPC daemon: records requests, scripts replies, fans
    every notification out to all connected peers."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.codex_home = socket_path.parent.parent
        self.requests: list[tuple[str, dict]] = []
        self._clients: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.refuse_initialize = False

    def start(self) -> None:
        import websockets.asyncio.server  # noqa: F401 - registers the asyncio subpackage

        self._thread = threading.Thread(target=self._run, daemon=True, name="fake-codex-daemon")
        self._thread.start()
        assert self._ready.wait(timeout=10), "fake daemon did not start"

    def _run(self) -> None:
        from websockets.asyncio.server import serve

        async def handler(ws) -> None:
            self._clients.append(ws)
            try:
                async for raw in ws:
                    await self._handle(ws, raw)
            except Exception:  # connection closed
                pass

        async def amain() -> None:
            async with serve(handler, path=str(self.socket_path), unix=True) as server:
                self._loop = asyncio.get_running_loop()
                self._ready.set()
                await server.serve_forever()

        asyncio.run(amain())

    async def _handle(self, ws, raw: str) -> None:
        msg = json.loads(raw)
        if "id" in msg and "method" in msg:
            method, params, rid = msg["method"], msg.get("params") or {}, msg["id"]
            self.requests.append((method, params))
            if method == "initialize" and self.refuse_initialize:
                await ws.send(json.dumps({"id": rid, "error": {"code": -32600, "message": "no"}}))
                return
            if method == "initialize":
                await ws.send(json.dumps({"id": rid, "result": {"userAgent": "codex-tui/0-test"}}))
                return
            if method == "thread/resume":
                await ws.send(json.dumps({
                    "id": rid,
                    "result": {"thread": {"id": params["threadId"], "name": "linked"},
                               "cwd": "/tmp", "model": "gpt-5.5-codex"},
                }))
                # Subscription is a resume side effect; the daemon greets its new
                # subscriber so a second peer can observe fan-out.
                await self.notify_all({"method": "thread/status/changed",
                                       "params": {"threadId": params["threadId"]}})
                return
            if method == "turn/start":
                await ws.send(json.dumps({
                    "id": rid, "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
                }))
                await self.notify_all({"method": "turn/started",
                                       "params": {"threadId": params["threadId"], "turn": {"id": "turn-1"}}})
                await self.notify_all({"method": "turn/completed", "params": {
                    "threadId": params["threadId"], "turn": {"id": "turn-1", "status": "completed"}}})
                return
            await ws.send(json.dumps({"id": rid, "result": {}}))
        elif "id" in msg:  # a response to a server-initiated request
            pass
        else:
            # A client notification (e.g. ``initialized``) is not a daemon event; record it.
            self.requests.append((msg.get("method", ""), msg.get("params") or {}))

    async def notify_all(self, notification: dict) -> None:
        payload = json.dumps(notification)
        for client in list(self._clients):
            try:
                await client.send(payload)
            except Exception:  # a peer dropped mid-broadcast
                self._clients.remove(client)

    def drop_connections(self) -> None:
        """Simulate the daemon dying: close every peer connection from the loop thread."""
        assert self._loop is not None

        async def close_all():
            for client in list(self._clients):
                await client.close()
            self._clients.clear()

        asyncio.run_coroutine_threadsafe(close_all(), self._loop).result(timeout=10)

    def stop(self) -> None:
        pass


@pytest.fixture()
def fake_daemon(tmp_path):
    # AF_UNIX socket paths cap at ~108 bytes; pytest's tmp_path is deeper than that,
    # so the fake CODEX_HOME (with its real app-server-control/ layout) lives at a
    # short OS-tmp root and is cleaned up with the test's own tmp_path.
    import os
    import secrets

    codex_home = Path(os.environ.get("TMPDIR", "/tmp")) / f"cx-fake-{secrets.token_hex(6)}"
    codex_home.mkdir(parents=True)
    daemon = _FakeDaemon(codex_home / "app-server-control" / "app-server-control.sock")
    daemon.socket_path.parent.mkdir(parents=True, exist_ok=True)
    daemon.start()
    yield daemon
    daemon.stop()
    with suppress(OSError):
        daemon.socket_path.unlink(missing_ok=True)
        codex_home.rmdir()


@contextmanager
def _daemon_probe_enabled():
    """Join selection off in tests by default; a peer test opts in explicitly."""
    saved = dict()
    import os

    saved["HERMES_TEST_ISOLATION"] = os.environ.pop("HERMES_TEST_ISOLATION", None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def short_home(tmp_path):
    """A CODEX_HOME at a shallow OS-tmp root (AF_UNIX socket paths cap at ~108 bytes)."""
    import os
    import secrets

    home = Path(os.environ.get("TMPDIR", "/tmp")) / f"cx-home-{secrets.token_hex(6)}"
    home.mkdir(parents=True)
    yield home
    with suppress(OSError):
        home.rmdir()


class TestDaemonJoinSelection:
    def test_daemon_present_is_joined_and_handshakes(self, fake_daemon):
        codex_home = fake_daemon.codex_home
        with _daemon_probe_enabled():
            peer = join_codex_daemon(str(codex_home))
        assert isinstance(peer, CodexDaemonPeerClient)
        try:
            result = peer.request("thread/list", {"limit": 5})
            assert result == {}
            assert ("initialize", {"clientInfo": {"name": "hermes", "title": "Hermes Agent",
                                                  "version": "0.1"}, "capabilities": {}}) in fake_daemon.requests
        finally:
            peer.close()

    def test_absent_socket_falls_back_to_none(self, short_home):
        with _daemon_probe_enabled():
            assert join_codex_daemon(str(short_home / "nowhere")) is None

    def test_stale_socket_file_falls_back_to_none(self, short_home):
        stale = short_home / "app-server-control" / "app-server-control.sock"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"")  # a crashed daemon's leftover: exists, refuses nothing, answers nothing
        with _daemon_probe_enabled():
            assert join_codex_daemon(str(short_home)) is None

    def test_test_isolation_never_probes_the_daemon(self, fake_daemon, monkeypatch):
        monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
        probed = []

        def boom(home):  # pragma: no cover - must never run
            probed.append(home)
            raise AssertionError("tests must not probe a real daemon socket")

        monkeypatch.setattr(daemon_mod, "connect_daemon_peer", boom)
        assert join_codex_daemon(str(fake_daemon.codex_home)) is None
        assert probed == []

    def test_session_default_factory_prefers_the_daemon(self, fake_daemon, short_home, monkeypatch):
        """The session's default factory: peer when the daemon answers, spawned child otherwise."""
        from types import SimpleNamespace

        from agent.transports.codex_app_server_session import CodexAppServerSession

        spawned = []
        monkeypatch.setattr(
            "agent.transports.codex_app_server_session.CodexAppServerClient",
            lambda **kwargs: spawned.append(kwargs) or SimpleNamespace(closed_spawn=True),
        )
        with _daemon_probe_enabled():
            joined = CodexAppServerSession(codex_home=str(fake_daemon.codex_home))
            peer = joined._client_factory(codex_bin="codex", codex_home=str(fake_daemon.codex_home))
            assert isinstance(peer, CodexDaemonPeerClient)
            peer.close()
            no_daemon = CodexAppServerSession(codex_home=str(short_home))
            fallback = no_daemon._client_factory(codex_bin="codex", codex_home=str(short_home))
            assert spawned and list(fallback.__dict__) == ["closed_spawn"], \
                "absent daemon must fall back to the spawn path"
            assert {"codex_bin": "codex", "codex_home": str(short_home)} in [
                {k: v for k, v in call.items()} for call in spawned]


class TestPeerWire:
    def test_two_peers_receive_the_same_thread_events(self, fake_daemon):
        """Fan-out is the reason to join: two connections subscribed to one thread both
        observe its notifications, so Hermes and the native TUI stay concurrent."""
        with _daemon_probe_enabled():
            first = join_codex_daemon(fake_daemon.codex_home)
            second = join_codex_daemon(fake_daemon.codex_home)
        assert first is not None and second is not None
        try:
            resume = first.request("thread/resume", {"threadId": TARGET_THREAD}, timeout=10)
            assert resume["thread"]["id"] == TARGET_THREAD
            second.request("thread/resume", {"threadId": TARGET_THREAD}, timeout=10)
            seen_a = first.take_notification(timeout=5)
            seen_b = second.take_notification(timeout=5)
            assert seen_a is not None and seen_b is not None
            assert seen_a["method"] == seen_b["method"] == "thread/status/changed"
            assert seen_a["params"]["threadId"] == seen_b["params"]["threadId"] == TARGET_THREAD
        finally:
            first.close()
            second.close()

    def test_turn_lifecycle_reaches_both_subscribers(self, fake_daemon):
        with _daemon_probe_enabled():
            first = join_codex_daemon(fake_daemon.codex_home)
            second = join_codex_daemon(fake_daemon.codex_home)
        assert first is not None and second is not None
        try:
            first.request("thread/resume", {"threadId": TARGET_THREAD}, timeout=10)
            second.request("thread/resume", {"threadId": TARGET_THREAD}, timeout=10)
            # Every resume notifies every subscriber, so drain both queues empty before
            # the turn: what follows is then unambiguously the turn's own events.
            for peer in (first, second):
                while peer.take_notification(timeout=0.2) is not None:
                    continue
            started = first.request("turn/start", {"threadId": TARGET_THREAD, "input": [
                {"type": "text", "text": "hi"},
                {"type": "image", "url": "data:image/png;base64,abc"},
            ]}, timeout=10)
            assert started["turn"]["id"] == "turn-1"
            collected = {"a": [], "b": []}
            peers = (("a", first), ("b", second))
            # One turn yields exactly two notifications; take them in lockstep.
            for _ in range(2):
                for key, peer in peers:
                    note = peer.take_notification(timeout=5)
                    assert note is not None
                    collected[key].append(note["method"])
            assert collected["a"] == ["turn/started", "turn/completed"]
            assert collected["b"] == ["turn/started", "turn/completed"]
            sent = next(params for method, params in fake_daemon.requests if method == "turn/start")
            assert sent["input"] == [{"type": "text", "text": "hi"},
                                     {"type": "image", "url": "data:image/png;base64,abc"}]
        finally:
            first.close()
            second.close()

    def test_jsonrpc_error_and_transport_loss(self, fake_daemon, tmp_path):
        with _daemon_probe_enabled():
            peer = join_codex_daemon(fake_daemon.codex_home)
        assert peer is not None
        peer.close()
        with pytest.raises(CodexAppServerTransportError):
            peer.request("thread/list", {}, timeout=2)
        assert peer.is_alive() is False

    def test_attach_liveness_marks_a_dropped_socket(self, fake_daemon):
        """A daemon that dies mid-session must fail visibly, not wedge the turn."""
        with _daemon_probe_enabled():
            peer = join_codex_daemon(fake_daemon.codex_home)
        assert peer is not None
        try:
            assert peer.is_alive()
            fake_daemon.drop_connections()
            wait_until(lambda: not peer.is_alive(), timeout=10, what="peer to observe the dropped socket")
            with pytest.raises(CodexAppServerTransportError):
                peer.request("thread/list", {}, timeout=3)
            assert any("connection" in line for line in peer.stderr_tail(5))
        finally:
            peer.close()


class TestServerRequestsQueue:
    def test_server_initiated_request_is_routed_to_the_queue(self, fake_daemon, tmp_path):
        """Approvals/ceremonies arrive as id-carrying frames; the session loop must be able
        to pop them and answer by id."""
        with _daemon_probe_enabled():
            peer = join_codex_daemon(fake_daemon.codex_home)
        assert peer is not None
        try:
            def push_request():
                asyncio.run_coroutine_threadsafe(
                    fake_daemon.notify_all({"id": "server-1", "method": "item/commandExecution/requestApproval",
                                            "params": {"command": "rm -rf /"}}),
                    fake_daemon._loop,
                )

            threading.Thread(target=push_request, daemon=True).start()
            sreq = wait_until(lambda: peer.take_server_request(timeout=0.5), timeout=10,
                              what="server request to reach the queue")
            assert sreq is not None
            assert sreq["id"] == "server-1"
            assert sreq["method"] == "item/commandExecution/requestApproval"
            peer.respond("server-1", {"decision": "decline"})
        finally:
            peer.close()


class TestImageInputRouting:
    """A composer attachment on a codex_app_server turn must ride the turn/start wire as a
    real image input, even when no catalog knows the codex slug."""

    @staticmethod
    def _agent(api_mode: str):
        from types import SimpleNamespace

        return SimpleNamespace(api_mode=api_mode)

    def _decide(self, agent, provider, model, cfg):
        from agent.image_routing import decide_image_input_mode_for_agent

        return decide_image_input_mode_for_agent(agent, provider, model, cfg)

    def test_unknown_catalog_verdict_is_native_on_the_codex_transport(self, monkeypatch):
        monkeypatch.setattr("agent.image_routing._lookup_supports_vision", lambda *a, **k: None)
        cfg = {"agent": {"image_input_mode": "auto"}}
        assert self._decide(self._agent("codex_app_server"), "openai-codex", "gpt-5.5-codex", cfg) == "native"
        # Other transports keep today's unknown -> text contract.
        assert self._decide(self._agent("chat_completions"), "openai-codex", "gpt-5.5-codex", cfg) == "text"

    def test_known_false_and_config_override_still_win(self, monkeypatch):
        monkeypatch.setattr("agent.image_routing._lookup_supports_vision", lambda *a, **k: False)
        assert self._decide(self._agent("codex_app_server"), "openai", "gpt-3", {"agent": {}}) == "text"
        monkeypatch.setattr("agent.image_routing._lookup_supports_vision", lambda *a, **k: True)
        forced = {"agent": {"image_input_mode": "text"}}
        assert self._decide(self._agent("codex_app_server"), "openai-codex", "gpt-5.5-codex", forced) == "text"
        forced_native = {"agent": {"image_input_mode": "native"}}
        assert self._decide(self._agent("chat_completions"), "whatever", "m", forced_native) == "native"
