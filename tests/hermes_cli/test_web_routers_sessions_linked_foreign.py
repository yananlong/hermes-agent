"""HTTP session routes for linked Claude/Mistral pointer rows, mirroring the Codex branch.

A linked foreign session is a pointer row whose transcript stays in the native tool's store;
``/api/sessions/{id}/messages`` must hydrate it live from that store, while timeline navigation
and export stay refused (the Hermes store holds no transcript to page or snapshot).
"""

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
CODEX_THREAD = "codex-thread-abc123"


def _write_claude_transcript(projects, session_id):
    project = projects / "-home-workspace"
    project.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "summary", "summary": "Claude pointer chat"},
        {"type": "user", "cwd": "/tmp", "sessionId": session_id, "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "hello claude"}},
        {"type": "assistant", "cwd": "/tmp", "sessionId": session_id, "timestamp": "2026-01-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-opus-4-5",
                     "content": [{"type": "text", "text": "hi from claude"}]}},
        {"type": "user", "isSidechain": True, "sessionId": session_id,
         "message": {"role": "user", "content": "sidechain noise"}},
    ]
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _write_mistral_store(root, session_id):
    session = root / "unified" / session_id
    (session / "journal").mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({
        "session_id": session_id, "title": "Vibe pointer chat",
        "environment": {"working_directory": "/tmp"}, "bumped_at": 1_700_000_000,
    }), encoding="utf-8")
    (session / "journal" / "0000000000000001.jsonl").write_text("\n".join([
        json.dumps({"type": "projection_delta", "payload": {"delta": [
            {"entry": {"role": "user", "content": [{"type": "text", "text": "hello vibe"}]}}]}}),
        json.dumps({"type": "projection_delta", "payload": {"delta": [
            {"entry": {"role": "assistant", "content": [{"type": "text", "text": "hi from vibe"}]}}]}}),
    ]) + "\n", encoding="utf-8")


@pytest.fixture
def web_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    projects = tmp_path / "claude-projects"
    vibe_root = tmp_path / "vibe-root"
    _write_claude_transcript(projects, CLAUDE_ID)
    _write_mistral_store(vibe_root, MISTRAL_ID)
    from hermes_cli import claude_linked_threads, mistral_linked_threads
    monkeypatch.setattr(claude_linked_threads, "resolve_claude_projects",
                        lambda profile_home=None: projects)
    monkeypatch.setattr(mistral_linked_threads, "resolve_vibe_session_root",
                        lambda profile_home=None: vibe_root)

    store = open_persistence(profile_scope(home=tmp_path))
    seed_session(store, "normal-session", "desktop")
    seed_messages(store, "normal-session",
                  {"role": "user", "content": "normal ask", "timestamp": 1},
                  {"role": "assistant", "content": "normal answer", "timestamp": 2})
    seed_session(store, "claude-pointer", "desktop", model_config={
        "claude_linked_thread": True, "claude_session_id": CLAUDE_ID,
        "api_mode": "claude_linked_readonly"})
    seed_session(store, "mistral-pointer", "desktop", model_config={
        "mistral_linked_thread": True, "mistral_session_id": MISTRAL_ID,
        "api_mode": "mistral_linked_readonly"})
    seed_session(store, "codex-pointer", "desktop", model_config={
        "codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})

    from hermes_cli.web_routers import sessions as sessions_router

    monkeypatch.setattr(sessions_router, "_with_db",
                        lambda profile, fn, *, read_only: fn(store))

    @contextmanager
    def profile_store(profile, *, read_only):
        yield store

    monkeypatch.setattr(sessions_router, "_profile_store", profile_store)

    app = FastAPI()
    app.include_router(sessions_router.manage_router)
    with TestClient(app) as client:
        yield store, client, tmp_path
    store.close()


def test_messages_route_hydrates_linked_claude_history(web_store):
    _, client, _ = web_store
    response = client.get("/api/sessions/claude-pointer/messages")
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "claude-pointer"
    assert [(m["role"], m["content"]) for m in payload["messages"]] == [
        ("user", "hello claude"), ("assistant", "hi from claude")]
    assert payload["pagination"]["returned"] == 2


def test_messages_route_hydrates_linked_mistral_history(web_store):
    _, client, _ = web_store
    response = client.get("/api/sessions/mistral-pointer/messages")
    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == "mistral-pointer"
    assert [(m["role"], m["content"]) for m in payload["messages"]] == [
        ("user", "hello vibe"), ("assistant", "hi from vibe")]


def test_messages_route_pages_linked_claude_history_like_normal_sessions(web_store):
    _, client, _ = web_store
    oldest = client.get("/api/sessions/claude-pointer/messages",
                        params={"order": "oldest", "limit": 1}).json()
    assert [m["content"] for m in oldest["messages"]] == ["hello claude"]
    assert oldest["pagination"]["returned"] == 1
    latest = client.get("/api/sessions/claude-pointer/messages",
                        params={"order": "latest", "limit": 1}).json()
    assert [m["content"] for m in latest["messages"]] == ["hi from claude"]


def test_timeline_and_around_refuse_linked_foreign_sessions(web_store):
    _, client, _ = web_store
    for sid in ("claude-pointer", "mistral-pointer"):
        assert client.get(f"/api/sessions/{sid}/timeline").status_code == 409
        assert client.get(f"/api/sessions/{sid}/messages/around?row_id=1").status_code == 409
    # The Codex refusal is unchanged.
    codex = client.get("/api/sessions/codex-pointer/timeline")
    assert codex.status_code == 409
    assert "Codex" in codex.json()["detail"]


def test_export_refuses_linked_foreign_sessions(web_store):
    _, client, _ = web_store
    for sid in ("claude-pointer", "mistral-pointer"):
        response = client.get(f"/api/sessions/{sid}/export")
        assert response.status_code == 409
    codex = client.get("/api/sessions/codex-pointer/export")
    assert codex.status_code == 409
    assert "Codex" in codex.json()["detail"]


def test_normal_session_history_routes_unchanged(web_store):
    _, client, _ = web_store
    response = client.get("/api/sessions/normal-session/messages")
    assert response.status_code == 200
    payload = response.json()
    assert [(m["role"], m["content"]) for m in payload["messages"]] == [
        ("user", "normal ask"), ("assistant", "normal answer")]
    timeline = client.get("/api/sessions/normal-session/timeline")
    assert timeline.status_code == 200
    assert [e["preview"] for e in timeline.json()["entries"]] == ["normal ask"]
    export = client.get("/api/sessions/normal-session/export")
    assert export.status_code == 200
    assert "normal answer" in export.text


# Resolving WHICH linked row this is can itself refuse: a damaged ``model_config``, or a flag
# without its native id. Those lookups used to sit outside the endpoint's ``try``, so the refusal
# escaped the handler as an unhandled 500 and a client could not tell it apart from a gateway
# crash. Each now answers with the same typed linked-history error the rest of the branch uses.


def test_a_refused_codex_linked_lookup_is_a_typed_error_not_a_500(web_store, monkeypatch):
    _, client, _ = web_store
    monkeypatch.setattr("hermes_cli.codex_linked_history.linked_thread_id",
                        lambda _store, _sid: (_ for _ in ()).throw(
                            ValueError("Linked Codex session has no thread ID")))

    response = client.get("/api/sessions/codex-pointer/messages")

    assert response.status_code == 503, response.text
    assert "linked history" in response.json()["detail"]


def test_a_refused_foreign_linked_lookup_is_a_typed_error_not_a_500(web_store, monkeypatch):
    from hermes_cli import claude_linked_history

    _, client, _ = web_store
    monkeypatch.setattr(claude_linked_history, "linked_claude_session_id",
                        lambda _store, _sid: (_ for _ in ()).throw(
                            ValueError("Linked Claude session has no session ID")))

    response = client.get("/api/sessions/claude-pointer/messages")

    assert response.status_code == 503, response.text
    assert "linked history" in response.json()["detail"]


def test_read_linked_foreign_history_caps_the_walk_like_the_codex_window():
    """F5: the claude/mistral REST history read is windowed, not a whole-transcript drain.

    A huge native thread must not be paged to the cursor end for a small window: the walk
    uses the same 100-row native page size as the Codex windowed reader and stops once the
    requested ``offset + limit`` window is covered, while ``latest`` retains only the
    trailing window — so the returned payload is always bounded by ``limit``.
    """
    from hermes_cli.web_routers.sessions import _read_linked_foreign_history

    total_rows = 1000
    all_rows = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"row-{i}"}
                for i in range(total_rows)]
    calls = []

    def reader(native_id, cursor=None, limit=50, profile_home=None):
        assert limit == 100, "must page with the same 100-row window the Codex reader uses"
        calls.append(cursor)
        start = int(cursor) if cursor else 0
        page = all_rows[start:start + limit]
        nxt = start + limit
        return {"messages": page, "next_cursor": str(nxt) if nxt < total_rows else None}

    # Oldest-first: one 100-row page already covers the 10-row window, so paging stops.
    rows = _read_linked_foreign_history(reader, "native", limit=10, offset=0, latest=False,
                                        profile_home=None)
    assert [r["content"] for r in rows] == [f"row-{i}" for i in range(10)]
    assert len(calls) == 1, "the walk must stop once the requested window is covered"

    # Offset beyond the first page still only pages as far as the window needs.
    calls.clear()
    rows = _read_linked_foreign_history(reader, "native", limit=10, offset=150, latest=False,
                                        profile_home=None)
    assert [r["content"] for r in rows] == [f"row-{i}" for i in range(150, 160)]
    assert len(calls) == 2

    # Latest: the newest window is returned (native cursors only move forward, so the walk
    # runs to the end, but only the trailing offset+limit rows are retained/served).
    calls.clear()
    rows = _read_linked_foreign_history(reader, "native", limit=10, offset=0, latest=True,
                                        profile_home=None)
    assert [r["content"] for r in rows] == [f"row-{i}" for i in range(total_rows - 10, total_rows)]
    rows = _read_linked_foreign_history(reader, "native", limit=10, offset=20, latest=True,
                                        profile_home=None)
    assert [r["content"] for r in rows] == [f"row-{i}" for i in range(total_rows - 30, total_rows - 20)]

    # Zero limit short-circuits without paging at all.
    calls.clear()
    assert _read_linked_foreign_history(reader, "native", limit=0, offset=0, latest=True,
                                        profile_home=None) == []
    assert calls == []


def test_messages_route_caps_linked_foreign_history_payload(web_store, monkeypatch):
    """A huge linked claude thread serves a bounded window, not the whole transcript."""
    from hermes_cli import claude_linked_threads

    _store, client, home = web_store
    projects = home / "claude-projects"
    pairs = 300  # 600 display rows, far beyond one REST page
    project = projects / "-home-workspace"
    lines = [{"type": "summary", "summary": "big claude chat"}]
    for i in range(pairs):
        lines.append({"type": "user", "cwd": "/tmp", "sessionId": CLAUDE_ID,
                      "message": {"role": "user", "content": f"q{i}"}})
        lines.append({"type": "assistant", "cwd": "/tmp", "sessionId": CLAUDE_ID,
                      "message": {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}})
    (project / f"{CLAUDE_ID}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    real_reader = claude_linked_threads.read_claude_history
    calls = []

    def counting_reader(*args, **kwargs):
        calls.append(kwargs.get("cursor"))
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(claude_linked_threads, "read_claude_history", counting_reader)

    # Oldest window: one 100-row native page already covers 10 rows, so paging stops early
    # instead of draining all 600 rows page by page.
    oldest = client.get("/api/sessions/claude-pointer/messages",
                        params={"order": "oldest", "limit": 10, "offset": 0})
    assert oldest.status_code == 200
    assert [m["content"] for m in oldest.json()["messages"]] == [
        c for i in range(5) for c in (f"q{i}", f"a{i}")]
    assert oldest.json()["pagination"]["returned"] == 10
    assert len(calls) == 1, "oldest window must stop paging once covered (100-row native pages)"

    # Latest window: the newest 10 rows of the huge thread, still bounded by the limit.
    calls.clear()
    latest = client.get("/api/sessions/claude-pointer/messages",
                        params={"order": "latest", "limit": 10})
    assert latest.status_code == 200
    assert [m["content"] for m in latest.json()["messages"]] == [
        c for i in range(pairs - 5, pairs) for c in (f"q{i}", f"a{i}")]
    assert latest.json()["pagination"]["returned"] == 10
