"""#100531 — the codex app-server thread survives an AIAgent rebuild (API-server restart, per-request agents).

``CodexAppServerSession`` keeps the codex thread id in memory only, so every new ``AIAgent`` for the same
Hermes session used to ``thread/start`` an empty thread while Hermes' own transcript continued. The runtime
now publishes ``codex_thread_id`` into the session row's ``model_config`` once the turn's projected rows are
durable, the next agent for that session issues ``thread/resume`` for it, and a stored id codex cannot hand
back fails closed: fresh thread, binding dropped, one status-rail notice.
"""

from pathlib import Path

import pytest

from agent.transports import codex_app_server_session as session_mod
from agent.transports.codex_app_server import CodexAppServerError
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session

SID = "sess-codex-restart"


class _WireClient:
    """Minimal app-server stand-in: answers thread/start with a fresh id, thread/resume with the requested
    id (or refuses ids in ``dead``), and records every JSON-RPC method it saw."""

    dead: set[str] = set()
    cwd = "/tmp"
    model = "gpt-6.1-sol"
    instances: "list[_WireClient]" = []
    counter = 0

    def __init__(self, **kwargs):
        self.requests: list[tuple[str, dict]] = []
        self.closed = False
        _WireClient.instances.append(self)

    def initialize(self, **kwargs):
        return {}

    def request(self, method, params=None, timeout=30.0):
        params = params or {}
        self.requests.append((method, params))
        if method == "thread/resume":
            if params["threadId"] in _WireClient.dead:
                raise CodexAppServerError(code=-32600, message=f"no rollout found for thread id {params['threadId']}")
            return {"thread": {"id": params["threadId"]}, "cwd": _WireClient.cwd, "model": _WireClient.model}
        _WireClient.counter += 1
        return {"thread": {"id": f"thread-{_WireClient.counter}"}}

    def close(self):
        self.closed = True


def _agent(db, **kwargs):
    from run_agent import AIAgent
    agent = AIAgent(api_key="stub", base_url="https://stub.invalid", provider="openai", api_mode="codex_app_server",
                    quiet_mode=True, skip_context_files=True, skip_memory=True, persistence=db, session_id=SID, **kwargs)
    agent._spawn_background_review = lambda **kw: None
    return agent


def _run_turn(self, user_input, **kwargs):
    return TurnResult(final_text=f"echo {user_input}", thread_id=self._thread_id, turn_id="turn-1",
                      projected_messages=[{"role": "assistant", "content": f"echo {user_input}"}])


def test_rebuilt_agent_resumes_the_stored_codex_thread_and_an_unresumable_one_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    monkeypatch.setattr(CodexAppServerSession, "run_turn", _run_turn)
    _WireClient.instances, _WireClient.dead, _WireClient.counter = [], set(), 0
    db = open_persistence(profile_scope(home=Path(tmp_path)))
    try:
        # Process 1: first turn publishes the binding only after the transcript is durable.
        first = _agent(db)
        assert first.run_conversation("Remember the word amber.")["completed"]
        assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "thread-1"
        assert [r["content"] for r in transcript_rows(db, SID)][-1] == "echo Remember the word amber."

        # Process 2 (restart): a new AIAgent for the same session resumes that thread before turn/start.
        notices: list[str] = []
        def status_callback(kind, message):  # the lifecycle rail every surface renders; other kinds are noise
            if kind == "lifecycle":
                notices.append(str(message))
        second = _agent(db)
        second.status_callback = status_callback
        assert second.run_conversation("Which word?", conversation_history=[])["completed"]
        assert [m for m, _ in _WireClient.instances[1].requests] == ["thread/resume"]
        assert _WireClient.instances[1].requests[0][1]["threadId"] == "thread-1"
        assert notices == []

        # Control: the stored thread is gone on the codex side -> fresh thread, binding rotated, one notice.
        _WireClient.dead = {"thread-1"}
        third = _agent(db)
        third.status_callback = status_callback
        assert third.run_conversation("And now?", conversation_history=[])["completed"]
        assert [m for m, _ in _WireClient.instances[2].requests] == ["thread/resume", "thread/start"]
        assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "thread-2"
        assert notices == ["Codex thread could not be resumed; starting a new one."]
    finally:
        db.close()


def test_the_binding_is_stored_and_read_through_the_writer_handle(tmp_path):
    """An agent with a writer's storage handle beside its store keeps the binding through the handle."""
    from types import SimpleNamespace

    import persistence_assembly
    from agent.codex_runtime import _store_codex_thread_id, _stored_codex_thread_id

    db = open_persistence(profile_scope(home=Path(tmp_path)))
    seed_session(db, SID, "cli")
    try:
        with persistence_assembly.open_persistence(persistence_assembly.profile_scope(home=tmp_path)) as handle:
            agent = SimpleNamespace(_session_db=db, session_id=SID, persistence=handle)
            _store_codex_thread_id(agent, "thread-9")
            assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "thread-9"
            assert _stored_codex_thread_id(agent) == "thread-9"
            _store_codex_thread_id(agent, None)
            assert _stored_codex_thread_id(agent) is None
    finally:
        db.close()


def test_linked_thread_never_branches_when_resume_fails_or_settings_change(monkeypatch, tmp_path):
    from agent.codex_runtime import _close_codex_session, _ensure_codex_session, _start_codex_thread
    from agent.transports.codex_app_server_session import CodexThreadResumeError

    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    _WireClient.instances, _WireClient.dead, _WireClient.counter = [], {"original"}, 0
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, SID, "desktop")
        db.sessions.patch_model_config(SID, {"codex_thread_id": "original", "codex_linked_thread": True})
        agent = _agent(db, cwd="/tmp")
        agent._cached_system_prompt = "A Hermes prompt that must not replace the Codex instructions"
        _ensure_codex_session(agent)
        for _ in range(2):
            with pytest.raises(CodexThreadResumeError):
                _start_codex_thread(agent)
        assert [method for method, _ in _WireClient.instances[0].requests] == ["thread/resume", "thread/resume"]
        assert _WireClient.instances[0].requests[0][1] == {"threadId": "original"}
        assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "original"

        _WireClient.dead.clear()
        _WireClient.cwd = "/somewhere-else"
        with pytest.raises(CodexThreadResumeError, match="working directory"):
            _start_codex_thread(agent)
        _WireClient.cwd = "/tmp"
        _WireClient.model = "gpt-6-luna"
        assert _start_codex_thread(agent) == "original"
        assert agent.model == "gpt-6-luna"
        assert db.sessions.get(SID).model == "gpt-6-luna"
        agent._cached_system_prompt = "changed"
        with pytest.raises(RuntimeError, match="settings changed"):
            _ensure_codex_session(agent)
        assert all(method != "thread/start" for method, _ in _WireClient.instances[0].requests)
        _close_codex_session(agent)
        _ensure_codex_session(agent)
        assert _start_codex_thread(agent) == "original"
        assert _WireClient.instances[1].requests == [("thread/resume", {"threadId": "original"})]
    finally:
        _WireClient.cwd = "/tmp"
        _WireClient.model = "gpt-6.1-sol"
        db.close()


def test_linked_thread_cannot_be_used_without_original_id(monkeypatch, tmp_path):
    from agent.codex_runtime import _ensure_codex_session

    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, SID, "desktop")
        db.sessions.patch_model_config(SID, {"codex_linked_thread": True})
        with pytest.raises(RuntimeError, match="no original thread ID"):
            _ensure_codex_session(_agent(db))
    finally:
        db.close()


def test_linked_turn_releases_native_writer_and_resumes_again(monkeypatch, tmp_path):
    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    monkeypatch.setattr(CodexAppServerSession, "run_turn", _run_turn)
    _WireClient.instances, _WireClient.dead, _WireClient.model = [], set(), "gpt-6.1-sol"
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, SID, "desktop", model="gpt-6.1-sol")
        db.sessions.patch_model_config(SID, {"codex_thread_id": "original", "codex_linked_thread": True})
        agent = _agent(db, cwd="/tmp")
        def assert_writer_released_before_memory_sync(**_kwargs):
            assert _WireClient.instances[-1].closed is True
        agent._sync_external_memory_for_turn = assert_writer_released_before_memory_sync
        assert agent.run_conversation("First linked turn")["completed"]
        assert _WireClient.instances[0].closed is True and agent._codex_session is None
        assert agent.run_conversation("Second linked turn")["completed"]
        assert _WireClient.instances[1].closed is True and agent._codex_session is None
        assert [c.requests for c in _WireClient.instances] == [
            [("thread/resume", {"threadId": "original"})],
            [("thread/resume", {"threadId": "original"})],
        ]
        assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "original"
        rows = transcript_rows(db, SID)
        # The gateway's turn-start crash-recovery row records accepted user input; native
        # assistant/tool history remains in Codex and is never mirrored to Hermes.
        assert [row["role"] for row in rows] == ["user", "user"]
        assert db.sessions.get(SID).message_count == 2
    finally:
        db.close()


def test_linked_thread_refuses_turn_when_native_model_cannot_be_persisted(monkeypatch, tmp_path):
    from agent.codex_runtime import _ensure_codex_session, _start_codex_thread

    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    _WireClient.instances, _WireClient.dead = [], set()
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, SID, "desktop", model="old-model")
        db.sessions.patch_model_config(SID, {"codex_thread_id": "original", "codex_linked_thread": True})
        def fail_write(*_args):
            raise OSError("write failed")
        monkeypatch.setattr(db.sessions, "update_model", fail_write)
        agent = _agent(db, cwd="/tmp")
        _ensure_codex_session(agent)
        with pytest.raises(RuntimeError, match="Could not save.*effective model"):
            _start_codex_thread(agent)
        assert db.sessions.get_model_config_value(SID, "codex_thread_id") == "original"
        assert [m for m, _ in _WireClient.instances[0].requests] == ["thread/resume"]
    finally:
        db.close()


def test_linked_thread_rejects_another_runtime_and_rate_limit_fallback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.conversation_loop import _codex_app_server_turn, _turn_without_loop
    import agent.turn_recovery as recovery

    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, SID, "desktop")
        db.sessions.patch_model_config(SID, {"codex_thread_id": "original", "codex_linked_thread": True})
        agent = _agent(db)
        agent.api_mode = "chat_completions"
        result = _turn_without_loop(agent, SimpleNamespace(messages=[]), "")
        assert result["completed"] is False
        assert "require the Codex app-server runtime" in result["error"]

        agent._run_codex_app_server_turn = lambda **kwargs: {"error": "rate limit exceeded", "completed": False}
        monkeypatch.setattr(recovery, "activate_codex_app_server_fallback", lambda *_: pytest.fail("must not fall back"))
        state = SimpleNamespace(user_message="hello", original_user_message="hello", messages=[],
                                effective_task_id="test", _should_review_memory=False, turn_id="turn")
        assert _codex_app_server_turn(agent, state)["error"] == "rate limit exceeded"
    finally:
        db.close()


def test_linked_codex_approvals_use_session_gateway_and_never_auto_approve(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent.codex_runtime import _ensure_codex_session
    from gateway.session_context import clear_session_vars, set_session_vars
    from tools import approval
    from tools.approval_context import reset_current_session_key, set_current_session_key

    monkeypatch.setattr(session_mod, "CodexAppServerClient", _WireClient)
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None)
    monkeypatch.setattr("tools.approval.is_approval_bypass_active", lambda: True)
    db = open_persistence(profile_scope(home=tmp_path))
    session_key, cards = "desktop:linked-codex", []
    tokens = set_session_vars(platform="desktop", session_key=session_key, cwd="/tmp")
    key_token = set_current_session_key(session_key)
    def answer(data):
        cards.append(dict(data))
        approval.resolve_gateway_approval(session_key, "once", request_id=data["request_id"])
    approval.register_gateway_notify(session_key, answer)
    try:
        seed_session(db, SID, "desktop")
        db.sessions.patch_model_config(SID, {"codex_thread_id": "original", "codex_linked_thread": True})
        agent = _agent(db, cwd="/tmp")
        _ensure_codex_session(agent)
        session = agent._codex_session
        assert session._decide_exec_approval({"command": "echo approved", "cwd": "/tmp"}) == "accept"
        session._track_pending_file_change({"method": "item/started", "params": {"item": {
            "id": "file-1", "type": "fileChange", "changes": [
                {"kind": {"type": "update", "move_path": "/tmp/moved.py"},
                 "path": "/tmp/changed.py", "diff": "-old\n+new"},
                {"kind": {"type": "add"}, "path": "/tmp/added.py", "diff": "+added"},
            ]}}})
        assert session._decide_apply_patch_approval({"itemId": "file-1", "grantRoot": "/tmp"}) == "accept"
        assert session._decide_exec_approval({"networkApprovalContext": {"host": "example.test", "protocol": "https"}}) == "decline"
        assert len(cards) == 2
        assert cards[0]["command"] == "echo approved" and "/tmp" in cards[0]["description"]
        assert all(text in cards[1]["command"] for text in ("/tmp/changed.py", "/tmp/moved.py", "-old", "+new", "/tmp/added.py", "+added"))
        assert "grants write to /tmp" in cards[1]["description"]
        assert all(not card["allow_session"] and not card["allow_permanent"] for card in cards)
        assert session._decide_exec_approval({"command": "echo x", "additionalPermissions": {"network": {}}}) == "decline"
        assert session._decide_exec_approval({"command": "echo x", "kind": "writeStdin"}) == "decline"
        assert session._decide_apply_patch_approval({"itemId": "unknown"}) == "decline"
        session._track_pending_file_change({"method": "item/started", "params": {"item": {
            "id": "incomplete", "type": "fileChange", "changes": [{"kind": {"type": "update"}, "path": "/tmp/a"}]}}})
        assert session._decide_apply_patch_approval({"itemId": "incomplete"}) == "decline"
        session._track_pending_file_change({"method": "item/started", "params": {"item": {
            "id": "too-large", "type": "fileChange", "changes": [
                {"kind": {"type": "add"}, "path": "/tmp/a", "diff": "+" * 65_537}]}}})
        assert session._decide_apply_patch_approval({"itemId": "too-large"}) == "decline"
        replies = []
        session._client = SimpleNamespace(respond=lambda *args: replies.append(args))
        session._handle_server_request({"id": "permissions", "method": "item/permissions/requestApproval"})
        assert replies == [("permissions", {"permissions": {}, "scope": "turn"})]
        assert session._respond_elicitation({"serverName": session_mod.HERMES_TOOLS_MCP_SERVER_NAME})["action"] == "decline"
        approval.unregister_gateway_notify(session_key)
        assert session._decide_exec_approval({"command": "echo no viewer"}) == "decline"
        monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", lambda *a, **k: "cancel")
        assert session._decide_exec_approval({"command": "echo timed out"}) == "decline"
    finally:
        approval.unregister_gateway_notify(session_key)
        reset_current_session_key(key_token)
        clear_session_vars(tokens)
        db.close()
