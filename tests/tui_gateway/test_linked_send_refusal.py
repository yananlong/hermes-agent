"""prompt.submit ADMITS a send onto a linked foreign pointer row (Codex/Claude/Mistral).

A linked row (``model_config`` flag ``codex_linked_thread`` / ``claude_linked_thread`` /
``mistral_linked_thread``) is a pointer: the native tool owns the transcript. An admitted send is
therefore the one the turn path can APPEND to that native thread — Codex through the app-server
(``turn/start`` + ``resume_thread_id``), Claude Code and Mistral Vibe through
``agent/linked_foreign_turn.py`` — and never a second Hermes conversation that would fork it.

Admission and the ``session.foreign.status`` probe read the same verdict
(``hermes_cli/linked_send_admission.py``), so a client told "writable" is never refused here and a
client told "read-only" can never send. The refusal machinery stays load-bearing — a build that
loses an agent's append path drops that agent from ``LINKED_SEND_ADMITTED`` — so the partial
admission below keeps that path exercised.

Two more properties are pinned here because enabling sends made them reachable: a linked row's
transcript is never rewritten (rewind/regenerate refuse for every agent), and a refusal leaves the
session untouched.
"""

import contextlib
import threading
import types

import pytest

from hermes_state import SessionDB
from hermes_cli import linked_send_admission
from tui_gateway import server

CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"

LINKED_ROWS = {
    "codex-linked": ("codex", {"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD}),
    "claude-linked": ("claude", {"claude_linked_thread": True, "claude_session_id": CLAUDE_ID}),
    "mistral-linked": ("mistral", {"mistral_linked_thread": True, "mistral_session_id": MISTRAL_ID}),
}

LINKED_LABELS = {"codex": "Codex", "claude": "Claude Code", "mistral": "Mistral Vibe"}


class _Store:
    """Persistence-handle double: these paths may only read ``sessions.get`` on it."""

    def __init__(self, rows):
        self.sessions = types.SimpleNamespace(
            get=lambda key: types.SimpleNamespace(model_config=dict(rows[key]))
            if key in rows else None)
        # The submit path's admission gate reads this; nothing here may write through the handle.
        self.runtime = types.SimpleNamespace(pending_ledger_status=lambda _key: None)

    def __getattr__(self, name):  # a linked guard must never write through the row
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"unexpected store access: {name}")


@pytest.fixture
def db(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    rows = {}
    for key, (_agent, config) in LINKED_ROWS.items():
        db.create_session(key, source="desktop", model_config=config)
        rows[key] = config
    db.create_session("plain", source="desktop")
    db.append_message("plain", "user", "plain question")
    store = _Store(rows)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_persistence_for", lambda _db: contextlib.nullcontext(store))
    yield db
    db.close()


def _live(key):
    """Register one runtime session bound to stored row ``key``, like the sidecar tests."""
    sid = f"runtime-{key}"
    server._sessions[sid] = {"session_key": key, "history": [], "history_lock": threading.Lock(),
                             "running": False, "agent": None}
    return sid


def _submit(sid, **extra):
    params = {"session_id": sid, "text": "hello there"}
    params.update(extra)
    return server.handle_request({"id": "s", "method": "prompt.submit", "params": params})


def _accepting_submit(monkeypatch):
    """Stub the post-admission turn machinery: the RPC answer is what these tests read."""
    monkeypatch.setattr(server, "_persist_session_row_for_submit", lambda *args: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *args: None)
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *args: False)
    monkeypatch.setattr(server, "_run_after_agent_ready", lambda *args: None)

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            pass  # never run the turn body; the RPC answer is what matters here

    monkeypatch.setattr(server.threading, "Thread", InlineThread)


# ── the admission contract ─────────────────────────────────────────────────────────────────────


def test_send_admission_admits_every_linked_agent():
    """All three agents append to their native thread in this build, so all three are admitted."""
    assert linked_send_admission.LINKED_SEND_ADMITTED == frozenset({"codex", "claude", "mistral"})
    for agent in ("codex", "claude", "mistral"):
        assert linked_send_admission.linked_send_refusal(agent) is None
    # An agent with no append path still gets the read-only sentence the Desktop renders verbatim.
    assert linked_send_admission.linked_send_refusal("other").startswith(
        "linked foreign sessions are read-only — this is a linked other session")


@pytest.mark.parametrize("key", sorted(LINKED_ROWS))
def test_prompt_submit_is_accepted_for_a_linked_row(monkeypatch, db, key):
    _accepting_submit(monkeypatch)
    sid = _live(key)
    try:
        response = _submit(sid)
        assert "error" not in response, response
        assert response["result"]["status"] == "streaming"
    finally:
        server._sessions.pop(sid, None)


def test_prompt_submit_refuses_an_agent_whose_append_path_is_gone(monkeypatch, db):
    """Dropping an agent from the admission refuses the send again — with the same reason code."""
    _accepting_submit(monkeypatch)
    monkeypatch.setattr(linked_send_admission, "LINKED_SEND_ADMITTED", frozenset({"codex"}))
    for key in sorted(LINKED_ROWS):
        agent, _config = LINKED_ROWS[key]
        sid = _live(key)
        try:
            response = _submit(sid)
            if agent == "codex":
                assert "error" not in response, response
                continue
            assert "result" not in response, response
            error = response["error"]
            assert error["code"] == 4008
            assert error["message"].lower().startswith("linked foreign sessions are read-only")
            assert error.get("data", {}).get("reason") == "linked_session_read_only"
            assert error.get("data", {}).get("linked_agent") == agent
            # Refused before any state change: no turn started, no durable write.
            session = server._sessions[sid]
            assert session["running"] is False
            assert "_run_thread" not in session
            assert "_submit_user_row" not in session
            assert db.get_messages(key) == []
        finally:
            server._sessions.pop(sid, None)


def test_refusal_helper_returns_none_for_normal_row(db):
    sid = _live("plain")
    try:
        assert server._linked_foreign_submit_refusal("r", server._sessions[sid]) is None
    finally:
        server._sessions.pop(sid, None)


def test_prompt_submit_still_works_for_normal_row(monkeypatch, db):
    # The plain row carries no linked flag, so the guard must fail open and the
    # submit must proceed to the streaming turn path.
    plain_handle = types.SimpleNamespace(
        sessions=types.SimpleNamespace(get=lambda _key: None),
        get_session=lambda _key: {"model_config": {}},
        runtime=types.SimpleNamespace(pending_ledger_status=lambda _key: None),
    )
    monkeypatch.setattr(server, "_get_db", lambda: plain_handle)
    monkeypatch.setattr(server, "_persistence_for", lambda _db: contextlib.nullcontext(plain_handle))
    _accepting_submit(monkeypatch)
    sid = _live("plain")
    try:
        assert server._linked_foreign_submit_refusal("r", server._sessions[sid]) is None
        response = _submit(sid)
        assert "error" not in response, response
        assert response["result"]["status"] == "streaming"
    finally:
        server._sessions.pop(sid, None)


# ── a linked row's transcript is not editable, for any agent ───────────────────────────────────


@pytest.mark.parametrize("key", sorted(LINKED_ROWS))
def test_rewind_or_regenerate_on_a_linked_row_is_refused(db, key):
    """Enabling sends must not unlock rewind/edit: that would rewrite a native-owned thread."""
    agent, _config = LINKED_ROWS[key]
    sid = _live(key)
    try:
        error, fields = server._truncate_history_for_submit("request", sid, server._sessions[sid],
                                                            {"truncate_before_user_ordinal": 0}, None)
        assert error["error"]["code"] == 4008
        assert f"linked {LINKED_LABELS[agent]} thread" in error["error"]["message"]
        assert fields == {}
        assert db.get_messages(key) == []
    finally:
        server._sessions.pop(sid, None)


def test_rewind_on_a_plain_row_is_not_refused_by_the_linked_guard(db):
    """The guard is scoped to pointer rows: an ordinary row still reaches the truncation gate."""
    sid = _live("plain")
    try:
        error, _fields = server._truncate_history_for_submit("request", sid, server._sessions[sid],
                                                             {"truncate_before_user_ordinal": 0}, None)
        assert "is not supported" not in str(error)
    finally:
        server._sessions.pop(sid, None)
