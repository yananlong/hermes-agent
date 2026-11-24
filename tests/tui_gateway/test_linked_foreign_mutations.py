"""Linked pointer rows refuse the mutations that would fork them — for ALL THREE agents.

Sends are admitted now, so the pointer-row guards are the only thing standing between a user and a
second transcript: ``session.create`` on copied parent history, ``session.branch``, a rewind /
regenerate, and a workspace move all read or rewrite the Hermes side of the row. Each guard used to
ask ``codex_linked_history.linked_thread_id`` and therefore only ever saw Codex; they now ask the
shared ``hermes_cli.foreign_linked_store.linked_foreign_kind`` helper, which knows all three flags,
the legacy ``*_linked_readonly`` api_mode shape, and an unreadable ``model_config`` (fail closed).

The linked-Codex workspace move is the one intentional exception: Codex can re-home its own thread
(``gateway/session_linked_codex.py``), so that path is kept and asserted here.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_messages, seed_session
from tui_gateway import server

CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"

ROWS = {
    "codex": ("Codex", {"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD}),
    "claude": ("Claude Code", {"claude_linked_thread": True, "claude_session_id": CLAUDE_ID}),
    "mistral": ("Mistral Vibe", {"mistral_linked_thread": True,
                                 "mistral_session_id": MISTRAL_ID}),
}


@pytest.fixture
def db(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        for key, (_label, config) in ROWS.items():
            seed_session(handle, key, "desktop", model_config=config)
            seed_messages(handle, key, {"role": "user", "content": f"native {key} turn"})
        seed_session(handle, "plain", "desktop")
        seed_messages(handle, "plain", {"role": "user", "content": "held first"})
        yield handle
    finally:
        handle.close()


def _live(key: str) -> dict:
    return {"session_key": key, "history": [], "history_lock": threading.Lock(),
            "running": False, "agent": None}


def _transcript(store, key: str) -> list[tuple[str, str]]:
    """The pointer row's OWN Hermes transcript as ``(role, content)`` pairs — it must not grow."""
    return [(row["role"], row["content"]) for row in transcript_rows(store, key)]


# ── session.create on copied parent history ───────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(ROWS))
def test_session_create_refuses_to_copy_a_linked_parent(db, monkeypatch, key):
    monkeypatch.setattr(server, "_profile_db", lambda _params, writer=False: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_persistence_for", lambda handle: contextlib.nullcontext(handle))
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sessions", {})

    # ``copy_parent_history`` is the handler flag behind ``session.branch_stored`` (the desktop's
    # whole-session branch), not a client param of ``session.create``.
    response = server.handle_request({"id": "create", "method": "session.branch_stored", "params": {
        "parent_session_id": key}})

    label = ROWS[key][0]
    assert response["error"]["code"] == 4008, response
    assert response["error"]["message"] == f"Branching a linked {label} thread is not supported"
    assert server._sessions == {}, "a refused branch must not create a runtime session"


def test_session_create_still_branches_an_ordinary_parent(db, monkeypatch):
    """The guard is scoped to pointer rows: the ordinary copy path still runs."""
    monkeypatch.setattr(server, "_profile_db", lambda _params, writer=False: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_persistence_for", lambda handle: contextlib.nullcontext(handle))
    monkeypatch.setattr(server, "_start_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sessions", {})

    response = server.handle_request({"id": "create", "method": "session.branch_stored", "params": {
        "parent_session_id": "plain"}})

    assert "is not supported" not in str(response), response


# ── session.branch ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(ROWS))
def test_session_branch_refuses_a_linked_parent(db, monkeypatch, key):
    session = _live(key)
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_sessions", {"live": session})

    response = server._branch_live("branch", {"session_id": "live"}, session)

    label = ROWS[key][0]
    assert response["error"]["code"] == 4008, response
    assert response["error"]["message"] == f"Branching a linked {label} thread is not supported"
    assert _transcript(db, key) == [("user", f"native {key} turn")]


def test_session_branch_still_branches_an_ordinary_parent(db, monkeypatch):
    session = _live("plain")
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_sessions", {"live": session})

    response = server._branch_live("branch", {"session_id": "live"}, session)

    # It is allowed past the linked guard (whatever later gate it hits is not this one).
    assert "linked" not in response.get("error", {}).get("message", "")


# ── workspace move: Codex keeps its native same-thread path ──────────────────────────────────


@pytest.mark.parametrize("key", ["claude", "mistral"])
def test_workspace_move_refuses_a_linked_claude_or_mistral_row(db, monkeypatch, tmp_path, key):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(server, "_profile_db", lambda _params, writer=False: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_workspace_cwd", lambda _home, cwd: str(target))
    monkeypatch.setattr(server, "_sessions", {})

    response = server.handle_request({"id": "move", "method": "session.workspace.move", "params": {
        "session_key": key, "cwd": str(target)}})

    label = ROWS[key][0]
    assert response["error"]["code"] == 4008, response
    assert response["error"]["message"] == (
        f"Moving the workspace of a linked {label} thread is not supported — its working "
        f"directory belongs to {label}")
    assert str(db.sessions.get(key).cwd) != str(target)


def test_workspace_move_routes_a_linked_codex_row_to_the_native_path(db, monkeypatch, tmp_path):
    """The linked-Codex feature needs this path: it re-homes the SAME native thread."""
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(server, "_profile_db", lambda _params, writer=False: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_workspace_cwd", lambda _home, cwd: str(target))
    monkeypatch.setattr(server, "_sessions", {})
    calls = []

    def _fake_linked_move(rid, key, cwd, home, live_sid, live, store):
        calls.append((key, cwd))
        return server._ok(rid, {"cwd": cwd, "branch": "", "git_repo_root": ""})

    monkeypatch.setattr(server, "_move_linked_workspace", _fake_linked_move)

    response = server.handle_request({"id": "move", "method": "session.workspace.move", "params": {
        "session_key": "codex", "cwd": str(target)}})

    assert "error" not in response, response
    assert calls == [("codex", str(target))]


# ── pointer semantics on the submit path ─────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(ROWS))
def test_submit_does_not_write_the_user_row_into_a_pointer_row(db, monkeypatch, key):
    """The native tool owns the transcript: no Hermes message row is stamped on the pointer."""
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_persistence_for", lambda handle: contextlib.nullcontext(handle))

    assert server._write_submit_user_row(_live(key), "hello there", None) is None
    assert _transcript(db, key) == [("user", f"native {key} turn")]


def test_submit_still_writes_the_user_row_for_an_ordinary_session(db, monkeypatch):
    monkeypatch.setattr(server, "_session_db", lambda _session: contextlib.nullcontext(db))
    monkeypatch.setattr(server, "_persistence_for", lambda handle: contextlib.nullcontext(handle))

    staged = server._write_submit_user_row(_live("plain"), "hello there", None)

    assert staged is not None and staged["content"] == "hello there"
