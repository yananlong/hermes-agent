"""Editing a local projection must not alter a linked Codex conversation."""

from contextlib import contextmanager
import threading
from types import SimpleNamespace

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_messages, seed_session
from tui_gateway import server


def test_linked_submit_truncation_is_rejected_before_any_write(tmp_path, monkeypatch):
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, "linked", "desktop", model_config={
        "codex_linked_thread": True, "codex_thread_id": "01a1c4a4-7a29-7011-8493-04445af16d6e"})
    seed_messages(db, "linked", {"role": "user", "content": "keep"})

    @contextmanager
    def owner_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", owner_db)
    session = {"session_key": "linked", "history": [{"role": "user", "content": "keep"}],
               "history_lock": threading.Lock()}
    try:
        error, fields = server._truncate_history_for_submit("request", "sid", session, {}, None)
        assert "error" in error
        assert "linked Codex" in error["error"]["message"]
        assert fields == {}
        assert transcript_rows(db, "linked")[0]["content"] == "keep"
        assert session["history"][0]["content"] == "keep"
    finally:
        db.close()


def test_linked_runtime_and_workspace_controls_are_not_misleading(tmp_path, monkeypatch):
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, "linked", "desktop", cwd=str(tmp_path), model_config={
        "codex_linked_thread": True, "codex_thread_id": "01a1c4a4-7a29-7011-8493-04445af16d6e"})

    @contextmanager
    def owner_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", owner_db)

    # Pin the native half: with a Codex CLI installed on the host this test runs on, a real
    # ``turn``-style resume would genuinely succeed and the workspace WOULD move (which is the
    # supported behaviour, not what this test is about). The property here is that a workspace
    # control whose native change cannot be applied does not silently rewrite the Hermes row.
    from tui_gateway.session_workdir import LinkedCodexWorkspaceError

    @contextmanager
    def no_native(*_args, **_kwargs):
        raise LinkedCodexWorkspaceError("native resume unavailable in this test")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "_linked_codex_resume", no_native)
    session = {"session_key": "linked", "running": True, "history": [], "history_lock": threading.Lock()}
    server._sessions["linked-live"] = session
    try:
        for key, value in (("model", "gpt-6-luna"), ("reasoning", "high"), ("fast", "fast")):
            response = server.handle_request({"id": "set", "method": "config.set", "params": {
                "session_id": "linked-live", "key": key, "value": value}})
            assert "error" in response, (key, response)
        session["running"] = False
        response = server.handle_request({"id": "cwd", "method": "session.cwd.set", "params": {
            "session_id": "linked-live", "cwd": "/home"}})
        assert "error" in response
        assert db.sessions.get("linked").cwd == str(tmp_path)
        assert "pending_model_switch" not in session
    finally:
        server._sessions.pop("linked-live", None)
        db.close()


def test_linked_compression_runs_even_when_local_mirror_is_empty(monkeypatch):
    import agent.conversation_compression_manual as manual

    called = []
    monkeypatch.setattr(server, "_session_is_codex_linked", lambda _session: True)
    monkeypatch.setattr(server, "_set_session_context", lambda *a, **k: None)
    monkeypatch.setattr(server, "_clear_session_context", lambda *_: None)
    monkeypatch.setattr(server, "_get_usage", lambda *_: {})
    monkeypatch.setattr(manual, "compress_now", lambda _agent, history, *_args, **_kwargs: (
        called.append(list(history)) or SimpleNamespace(status="compressed", after_messages=[], removed=0)))
    session = {"session_key": "linked", "cwd": "/home", "history": [], "history_version": 0,
               "history_lock": threading.Lock(), "agent": object()}
    server._compress_session_history(session)
    assert called == [[]]
