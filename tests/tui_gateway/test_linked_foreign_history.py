"""Linked CLAUDE and MISTRAL sessions hydrate session.history / resume like linked Codex threads.

A linked foreign row (``model_config`` flag + native id) is a pointer only: the native
tool's on-disk store owns the transcript. These tests prove the gateway reads the full
native transcript for claude/mistral rows, keeps codex and plain sessions unchanged, and
refuses model switches on any linked row.
"""

import contextlib
import json
import threading

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session
from tui_gateway import server
from hermes_cli import claude_linked_threads, mistral_linked_threads
import gateway.session_linked_codex as codex_gateway

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"
CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"


def _write_claude_transcript(projects, session_id, pairs):
    """Write one native Claude JSONL transcript with ``pairs`` user/assistant turns."""
    project = projects / "-home-workspace"
    project.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "summary", "summary": "Claude fixture"}]
    for i in range(pairs):
        lines.append({"type": "user", "sessionId": session_id,
                      "timestamp": "2024-01-01T00:00:00.000Z",
                      "message": {"role": "user", "content": f"claude question {i}"}})
        lines.append({"type": "assistant", "sessionId": session_id,
                      "timestamp": "2024-01-01T00:00:01.000Z",
                      "message": {"role": "assistant", "model": "claude-opus-4-5",
                                  "content": [{"type": "text", "text": f"claude answer {i}"}]}})
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _write_vibe_transcript(root, session_id, entries):
    """Write one native Vibe (unified layout) session store with journal ``entries``."""
    journal = root / "unified" / session_id / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    (root / "unified" / session_id / "meta.json").write_text("{}", encoding="utf-8")
    path = journal / "000001.jsonl"
    path.write_text("\n".join(
        json.dumps({"payload": {"delta": [{"entry": entry}]}}) for entry in entries) + "\n",
        encoding="utf-8")
    return path


@pytest.fixture
def foreign_stores(tmp_path, monkeypatch):
    """Point the native store resolvers at tmp-dir fixtures (same pattern as tests/hermes_cli)."""
    projects = tmp_path / "claude-projects"
    _write_claude_transcript(projects, CLAUDE_ID, pairs=2)
    vibe_root = tmp_path / "vibe-root"
    _write_vibe_transcript(vibe_root, MISTRAL_ID, [
        {"role": "user", "content": "vibe question 0"},
        {"role": "assistant", "content": "vibe answer 0"},
    ])
    monkeypatch.setattr(claude_linked_threads, "resolve_claude_projects",
                        lambda profile_home=None: projects)
    monkeypatch.setattr(mistral_linked_threads, "resolve_vibe_session_root",
                        lambda profile_home=None: vibe_root)
    return projects, vibe_root


@pytest.fixture
def db(tmp_path):
    store = open_persistence(profile_scope(home=tmp_path))
    seed_session(store, "claude-linked", "claude-code", model_config={
        "claude_linked_thread": True, "claude_session_id": CLAUDE_ID})
    seed_session(store, "mistral-linked", "mistral-vibe", model_config={
        "mistral_linked_thread": True, "mistral_session_id": MISTRAL_ID})
    seed_session(store, "codex-linked", "desktop", model_config={
        "codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})
    seed_session(store, "plain", "desktop")
    seed_messages(store, "plain", {"role": "user", "content": "plain question"},
                  {"role": "assistant", "content": "plain answer"})
    yield store
    store.close()


@contextlib.contextmanager
def _live(server, db, key, monkeypatch):
    """Register one runtime session bound to ``key`` on the shared store, like the sidecar tests."""

    @contextlib.contextmanager
    def owner_db(_session):
        yield db

    monkeypatch.setattr(server, "_session_db", owner_db)
    sid = f"runtime-{key}"
    server._sessions[sid] = {"session_key": key, "history": [], "history_lock": threading.Lock(),
                             "running": False, "agent": None}
    try:
        yield sid
    finally:
        server._sessions.pop(sid, None)


def _history_rpc(server, sid):
    return server.handle_request(
        {"id": "1", "method": "session.history", "params": {"session_id": sid}})


class _Resume:
    """Minimal stand-in for methods_session._Resume around read_resume_history."""

    def __init__(self, persistence, target, profile_home=None):
        self.persistence, self.target = persistence, target
        self.omit_messages = False
        self.profile_home = profile_home

    def child_history(self, repair=False):
        from persistence.conversation.messages import ConversationRequest
        return [m.as_openai_message() for m in self.persistence.messages.read_conversation(
            self.target, ConversationRequest(repair_alternation=repair, include_row_ids=True))]


def _resume_projection(store, target):
    """The exact ``(model, display)`` pair read_resume_history falls back to."""
    stored = store.messages.read_resume_conversations(target)
    return ([m.as_openai_message() for m in stored.model],
            [m.as_openai_message() for m in stored.display])


def test_session_history_hydrates_linked_claude_from_native_transcript(foreign_stores, db, monkeypatch):
    with _live(server, db, "claude-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert "error" not in response, response
    result = response["result"]
    assert result["count"] == 4
    texts = [(m["role"], m["text"]) for m in result["messages"]]
    assert texts == [("user", "claude question 0"), ("assistant", "claude answer 0"),
                     ("user", "claude question 1"), ("assistant", "claude answer 1")]


def test_session_history_hydrates_linked_mistral_from_native_transcript(foreign_stores, db, monkeypatch):
    with _live(server, db, "mistral-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert "error" not in response, response
    result = response["result"]
    assert result["count"] == 2
    texts = [(m["role"], m["text"]) for m in result["messages"]]
    assert texts == [("user", "vibe question 0"), ("assistant", "vibe answer 0")]


def test_session_history_fetches_all_pages_of_a_long_claude_transcript(tmp_path, monkeypatch, db):
    # 60 turns = 120 display rows; read_claude_history pages at most 100 rows, so hydration
    # must loop the cursor to render the full transcript.
    projects = tmp_path / "claude-projects"
    _write_claude_transcript(projects, CLAUDE_ID, pairs=60)
    monkeypatch.setattr(claude_linked_threads, "resolve_claude_projects",
                        lambda profile_home=None: projects)
    with _live(server, db, "claude-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert "error" not in response, response
    assert response["result"]["count"] == 120
    assert response["result"]["messages"][-1]["text"] == "claude answer 59"


def test_session_history_codex_linked_behavior_unchanged(monkeypatch, db):
    monkeypatch.setattr(codex_gateway, "read_display_history", lambda thread_id, **kwargs: [
        {"role": "user", "content": "codex question"},
        {"role": "assistant", "content": "codex answer"},
    ])
    with _live(server, db, "codex-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert "error" not in response, response
    assert response["result"]["count"] == 2
    assert response["result"]["messages"][1]["text"] == "codex answer"


def test_session_history_codex_failure_still_returns_5000(monkeypatch, db):
    def _boom(thread_id, **kwargs):
        raise RuntimeError("codex app server down")

    monkeypatch.setattr(codex_gateway, "read_display_history", _boom)
    with _live(server, db, "codex-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert response["error"]["code"] == 5000
    assert response["error"]["message"] == "Unable to load linked Codex history"


def test_session_history_non_linked_session_unchanged(db, monkeypatch):
    with _live(server, db, "plain", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert "error" not in response, response
    texts = [(m["role"], m["text"]) for m in response["result"]["messages"]]
    assert texts == [("user", "plain question"), ("assistant", "plain answer")]


def test_session_history_linked_claude_failure_returns_5000(monkeypatch, foreign_stores, db):
    def _boom(session_id, **kwargs):
        raise ValueError("transcript gone")

    monkeypatch.setattr(claude_linked_threads, "read_claude_history", _boom)
    with _live(server, db, "claude-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert response["error"]["code"] == 5000
    assert response["error"]["message"] == "Unable to load linked Claude history"


def test_session_history_linked_mistral_failure_returns_5000(monkeypatch, foreign_stores, db):
    def _boom(session_id, **kwargs):
        raise ValueError("store gone")

    monkeypatch.setattr(mistral_linked_threads, "read_mistral_history", _boom)
    with _live(server, db, "mistral-linked", monkeypatch) as sid:
        response = _history_rpc(server, sid)
    assert response["error"]["code"] == 5000
    assert response["error"]["message"] == "Unable to load linked Mistral history"


def test_read_resume_history_hydrates_linked_claude_and_mistral(foreign_stores, db):
    raw, display = server.read_resume_history(_Resume(db, "claude-linked"), contextlib.nullcontext)
    assert raw == _Resume(db, "claude-linked").child_history(repair=True)
    assert [row["content"] for row in display] == [
        "claude question 0", "claude answer 0", "claude question 1", "claude answer 1"]

    _, display = server.read_resume_history(_Resume(db, "mistral-linked"), contextlib.nullcontext)
    assert [row["content"] for row in display] == ["vibe question 0", "vibe answer 0"]


def test_read_resume_history_falls_back_to_db_path_when_linked_read_fails(
        monkeypatch, foreign_stores, db):
    def _boom(session_id, **kwargs):
        raise ValueError("transcript gone")

    monkeypatch.setattr(claude_linked_threads, "read_claude_history", _boom)
    # Must not raise: falls back to the normal store projection (empty for a pointer row).
    assert server.read_resume_history(
        _Resume(db, "claude-linked"), contextlib.nullcontext) == _resume_projection(db, "claude-linked")


def test_read_resume_history_non_linked_session_unchanged(db):
    raw, display = server.read_resume_history(_Resume(db, "plain"), contextlib.nullcontext)
    model, display_rows = _resume_projection(db, "plain")
    assert raw == model
    assert [row.get("content") for row in display] == ["plain question", "plain answer"]
    assert [row.get("content") for row in display_rows] == ["plain question", "plain answer"]


def test_linked_model_switch_is_refused_for_all_foreign_agents(db, monkeypatch):
    with _live(server, db, "codex-linked", monkeypatch) as sid:
        with pytest.raises(ValueError, match="Change the model in Codex before opening this linked thread"):
            server._refuse_linked_model_switch(server._sessions[sid])
        assert server._session_is_codex_linked(server._sessions[sid]) is True
    with _live(server, db, "claude-linked", monkeypatch) as sid:
        with pytest.raises(ValueError, match="Change the model in Claude Code"):
            server._refuse_linked_model_switch(server._sessions[sid])
        assert server._session_is_codex_linked(server._sessions[sid]) is True
    with _live(server, db, "mistral-linked", monkeypatch) as sid:
        with pytest.raises(ValueError, match="Change the model in Mistral Vibe"):
            server._refuse_linked_model_switch(server._sessions[sid])
        assert server._session_is_codex_linked(server._sessions[sid]) is True
    with _live(server, db, "plain", monkeypatch) as sid:
        server._refuse_linked_model_switch(server._sessions[sid])  # must not raise
        assert server._session_is_codex_linked(server._sessions[sid]) is False
