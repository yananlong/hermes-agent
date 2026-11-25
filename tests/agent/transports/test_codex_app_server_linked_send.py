"""Linked-thread sends through the Codex session: append semantics + native attachments.

A linked row is a pointer: the native Codex rollout owns the transcript, so Hermes'
send path must (1) resume the ORIGINAL thread id (never branch to thread/start),
(2) keep the native thread's own settings (model/cwd come back from ``thread/resume``),
and (3) carry composer attachments as real ``UserInput`` image parts — a data: URL as
``{type: image, url}``, a local file as ``{type: localImage, path}`` — instead of
flattening them into text markers.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult

LINKED_THREAD = "01a212d9-09be-7fb3-b8c7-90082e4fb805"


class _RecordingClient:
    """The app-server wire as the linked send path needs it: resume answers the native
    thread's settings; turn/start acks and streams a completed turn."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self._notifications: list[dict] = []
        self.closed = False

    def initialize(self, **_kwargs) -> dict:
        return {}

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        params = params or {}
        self.requests.append((method, params))
        if method == "thread/resume":
            assert params["threadId"] == LINKED_THREAD, "a linked send must resume the original thread"
            return {"thread": {"id": LINKED_THREAD}, "cwd": "/home/proj", "model": "gpt-5.5-codex"}
        if method == "turn/start":
            self._notifications.append({"method": "item/completed", "params": {
                "threadId": params["threadId"],
                "turn": {"id": "turn-1"},
                "item": {"id": "item-1", "type": "agentMessage", "text": "done"},
            }})
            self._notifications.append({"method": "turn/completed", "params": {
                "threadId": params["threadId"], "turn": {"id": "turn-1", "status": "completed"}}})
            return {"turn": {"id": "turn-1"}}
        return {}

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        pass

    def respond(self, request_id: Any, result: dict) -> None:
        self.requests.append(("respond", {"id": request_id, "result": result}))

    def respond_error(self, request_id: Any, code: int, message: str, data: Optional[Any] = None) -> None:
        self.requests.append(("respond_error", {"id": request_id, "code": code, "message": message}))

    def take_notification(self, timeout: float = 0.0) -> Optional[dict]:
        return self._notifications.pop(0) if self._notifications else None

    def take_server_request(self, timeout: float = 0.0) -> Optional[dict]:
        return None

    def stderr_tail(self, n: int = 20) -> list[str]:
        return []

    def is_alive(self) -> bool:
        return not self.closed

    def close(self, timeout: float = 3.0) -> None:
        self.closed = True


def _linked_session(client: _RecordingClient) -> CodexAppServerSession:
    return CodexAppServerSession(
        cwd="/home/proj",
        client_factory=lambda **_kw: client,
        resume_thread_id=LINKED_THREAD,
        preserve_thread_settings=True,
    )


def test_linked_send_resumes_the_native_thread_and_appends():
    client = _RecordingClient()
    session = _linked_session(client)
    assert session.ensure_started() == LINKED_THREAD
    result: TurnResult = session.run_turn("continue the work")
    assert result.final_text == "done"
    assert result.thread_id == LINKED_THREAD and result.turn_id == "turn-1"
    methods = [method for method, _ in client.requests]
    # The whole send is exactly: resume the pointer's thread, then one turn on it.
    # A spawn-path thread/start here would fork the native thread — the one failure
    # the pointer row exists to make impossible.
    assert methods == ["thread/resume", "turn/start"]
    assert client.requests[1][1]["threadId"] == LINKED_THREAD
    assert client.requests[1][1]["input"] == [{"type": "text", "text": "continue the work"}]


def test_linked_send_carries_attachments_as_native_image_inputs():
    """A composer attach (data: URL) plus a local file ride turn/start as real
    app-server image inputs, never as base64 text and never dropped (#51053)."""
    client = _RecordingClient()
    session = _linked_session(client)
    session.ensure_started()
    session.run_turn([
        {"type": "text", "text": "what do you see?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "/home/proj/shot.png"}},
    ])
    _method, params = next(r for r in client.requests if r[0] == "turn/start")
    assert params["input"] == [
        {"type": "text", "text": "what do you see?"},
        {"type": "image", "url": "data:image/png;base64,QUJD"},
        {"type": "localImage", "path": "/home/proj/shot.png"},
    ]
    # The submit's text echo stays pure: the image part did not smear into the prompt.
    assert params["input"][0]["text"] == "what do you see?"


def test_linked_send_with_image_only_turn_uses_the_default_prompt():
    client = _RecordingClient()
    session = _linked_session(client)
    session.ensure_started()
    session.run_turn([{"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}])
    _method, params = next(r for r in client.requests if r[0] == "turn/start")
    assert params["input"] == [
        {"type": "text", "text": "What do you see in this image?"},
        {"type": "image", "url": "data:image/png;base64,QUJD"},
    ]
