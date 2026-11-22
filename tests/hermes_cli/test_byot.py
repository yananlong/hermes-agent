"""BYOT (bring your own terminal) tests: /api/byot/host registration, the
/api/byot/sessions listing, and /api/pty?byot=<id> proxying."""

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import web_server
import hermes_cli.web_server_chat as _web_server_chat
from hermes_cli.web_routers import byot as _byot


@pytest.fixture
def byot_harness(monkeypatch):
    monkeypatch.setattr(
        _web_server_chat, "_ws_auth_reason", lambda ws, **_kwargs: (None, "test")
    )
    monkeypatch.setattr(_web_server_chat, "_ws_host_origin_reason", lambda ws: None)
    monkeypatch.setattr(_web_server_chat, "_ws_client_reason", lambda ws: None)

    yield

    _byot.BYOT_REGISTRY._hosts.clear()


def _authed_client() -> TestClient:
    client = TestClient(web_server.app)
    client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def _connect_host(client, name="iterm-zsh", **meta):
    hello = {
        "type": "hello",
        "name": name,
        "shell": "/bin/zsh",
        "cwd": "/tmp",
        "pid": 4242,
        "cols": 120,
        "rows": 40,
        **meta,
    }
    ws = client.websocket_connect("/api/byot/host")
    ws.__enter__()
    ws.send_text(json.dumps(hello))
    registered = json.loads(ws.receive_text())
    assert registered["type"] == "registered"
    return ws, registered["id"]


def test_byot_host_registers_and_lists(byot_harness):
    client = _authed_client()
    ws, byot_id = _connect_host(client)
    try:
        resp = client.get("/api/byot/sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["id"] == byot_id
        assert sessions[0]["name"] == "iterm-zsh"
        assert sessions[0]["shell"] == "/bin/zsh"
    finally:
        ws.__exit__(None, None, None)
    # After the host socket closes, it is no longer listed.
    assert client.get("/api/byot/sessions").json()["sessions"] == []


def test_byot_viewer_proxies_bytes_both_ways(byot_harness):
    client = TestClient(web_server.app)
    host, byot_id = _connect_host(client)
    try:
        with client.websocket_connect(f"/api/pty?byot={byot_id}") as viewer:
            # The viewer first gets the byot control frame naming the terminal.
            control = json.loads(viewer.receive_text())
            assert control["type"] == "byot"
            assert control["id"] == byot_id
            # Host output is broadcast to the viewer.
            host.send_bytes(b"hello from zsh\r\n")
            assert viewer.receive_bytes() == b"hello from zsh\r\n"
            # Viewer keystrokes reach the host.
            viewer.send_bytes(b"ls\r")
            assert host.receive_bytes() == b"ls\r"
            # A resize escape becomes a JSON control frame, not raw bytes.
            viewer.send_text("\x1b[RESIZE:100;30]")
            control = json.loads(host.receive_text())
            assert control == {"type": "resize", "cols": 100, "rows": 30}
    finally:
        host.__exit__(None, None, None)


def test_byot_unknown_id_closes_1011(byot_harness):
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/pty?byot=nope") as viewer:
        text = viewer.receive_text()
        assert "not connected" in text
        with pytest.raises(WebSocketDisconnect) as exc_info:
            viewer.receive_text()
    assert exc_info.value.code == 1011


def test_byot_host_disconnect_closes_viewer_4410(byot_harness):
    client = TestClient(web_server.app)
    host, byot_id = _connect_host(client)
    viewer = client.websocket_connect(f"/api/pty?byot={byot_id}")
    viewer.__enter__()
    viewer.receive_text()  # byot control frame
    host.__exit__(None, None, None)  # external terminal went away
    with pytest.raises(WebSocketDisconnect) as exc_info:
        viewer.receive_bytes()
    assert exc_info.value.code == 4410
    viewer.__exit__(None, None, None)


def test_byot_host_requires_hello_first(byot_harness):
    client = TestClient(web_server.app)
    with client.websocket_connect("/api/byot/host") as ws:
        ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as exc_info:
            ws.receive_text()
    assert exc_info.value.code == 4400
