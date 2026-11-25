"""Canonical reads answer a linked Claude/Vibe row's own native turns.

Desktop's Watch windows skip the HTTP transcript prefetch, so ``session.resume``,
``session.history`` and ``session.transcript`` are the only doors a linked thread comes through.
A linked row is a pointer: its Hermes message list is empty by design, so a read that falls back
to the local mirror answers an empty conversation even though the native store is full.

These tests drive the real canonical RPC dispatch with real native transcript fixtures (a Claude
Code JSONL under ``CLAUDE_CONFIG_DIR/projects`` and a Vibe journal under
``MISTRAL_VIBE_SESSION_ROOT``) inside a temp ``HERMES_HOME``, and assert all three RPCs return the
native turns in the wire shape their surfaces read.
"""

import json

import pytest

from tests.fixtures.storage_read import session_row
from tests.gateway.test_session_mutation_model_busy import _owner


pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"


def _write_claude_transcript(claude_home, session_id):
    """One native Claude Code transcript, as ``claude`` writes it: JSONL per session UUID."""
    project = claude_home / "projects" / "-home-workspace"
    project.mkdir(parents=True)
    lines = [{"type": "summary", "summary": "linked Claude chat"}]
    for index in range(2):
        lines.append({"type": "user", "sessionId": session_id,
                      "timestamp": f"2026-01-01T00:00:0{index}Z",
                      "message": {"role": "user", "content": f"claude question {index}"}})
        lines.append({"type": "assistant", "sessionId": session_id,
                      "timestamp": f"2026-01-01T00:00:0{index + 1}Z",
                      "message": {"role": "assistant", "model": "claude-opus-4-5",
                                  "content": [{"type": "text", "text": f"claude answer {index}"}]}})
    (project / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _write_vibe_store(vibe_root, session_id):
    """One native Vibe session store (vibe 3.x unified layout: ``unified/<id>/journal/*.jsonl``)."""
    journal = vibe_root / "unified" / session_id / "journal"
    journal.mkdir(parents=True)
    (vibe_root / "unified" / session_id / "meta.json").write_text(
        json.dumps({"session_id": session_id, "title": "linked Vibe chat"}), encoding="utf-8")
    entries = [{"role": "user", "content": "vibe question 0"},
               {"role": "assistant", "content": "vibe answer 0"}]
    (journal / "000001.jsonl").write_text("\n".join(
        json.dumps({"payload": {"delta": [{"entry": entry}]}}) for entry in entries) + "\n",
        encoding="utf-8")


@pytest.fixture
def native_stores(tmp_path, monkeypatch):
    """Point the profile's own native stores at real fixtures, through their documented vars."""
    claude_home = tmp_path / "claude"
    vibe_root = tmp_path / "vibe"
    _write_claude_transcript(claude_home, CLAUDE_ID)
    _write_vibe_store(vibe_root, MISTRAL_ID)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.setenv("MISTRAL_VIBE_SESSION_ROOT", str(vibe_root))
    return claude_home, vibe_root


def _link_claude(db, session_id):
    db.sessions.patch_model_config(session_id, {
        "claude_linked_thread": True, "claude_session_id": CLAUDE_ID,
        "api_mode": "claude_linked_readonly",
    })


def _link_mistral(db, session_id):
    db.sessions.patch_model_config(session_id, {
        "mistral_linked_thread": True, "mistral_session_id": MISTRAL_ID,
        "api_mode": "mistral_linked_readonly",
    })


async def _handle(_event):
    return "ok"


def _turns(messages, key):
    return [(row["role"], row[key]) for row in messages]


# ``session.resume`` answers raw display rows (``content``); ``session.history`` runs them through
# the transcript projection (``text``). Both are pre-existing wire shapes each surface reads.
def _resumed_turns(messages):
    return _turns(messages, "content")


def _history_turns(messages):
    return _turns(messages, "text")


@pytest.mark.asyncio
async def test_canonical_reads_return_a_linked_claude_transcript(tmp_path, monkeypatch, native_stores):
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _handle)
    _link_claude(db, ref.session_id)
    expected = [("user", "claude question 0"), ("assistant", "claude answer 0"),
                ("user", "claude question 1"), ("assistant", "claude answer 1")]
    try:
        resumed = await owner.dispatch({"id": 1, "method": "session.resume",
                                        "params": {"session_id": ref.session_id}})
        assert "result" in resumed, resumed
        assert resumed["result"]["message_count"] == 4, resumed
        assert _resumed_turns(resumed["result"]["messages"]) == expected

        history = await owner.dispatch({"id": 2, "method": "session.history",
                                        "params": {"session_id": ref.session_id}})
        assert _history_turns(history["result"]["messages"]) == expected

        transcript = await owner.dispatch({"id": 3, "method": "session.transcript",
                                           "params": {"session_id": ref.session_id}})
        assert [(row["row_kind"], row["text"]) for row in transcript["result"]["rows"]] == [
            ("message", text) for _role, text in expected]
        # A pointer row writes no second transcript: the local mirror stays empty.
        assert session_row(db, ref.session_id) is not None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_canonical_reads_return_a_linked_vibe_transcript(tmp_path, monkeypatch, native_stores):
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _handle)
    _link_mistral(db, ref.session_id)
    expected = [("user", "vibe question 0"), ("assistant", "vibe answer 0")]
    try:
        resumed = await owner.dispatch({"id": 1, "method": "session.resume",
                                        "params": {"session_id": ref.session_id}})
        assert "result" in resumed, resumed
        assert _resumed_turns(resumed["result"]["messages"]) == expected

        history = await owner.dispatch({"id": 2, "method": "session.history",
                                        "params": {"session_id": ref.session_id}})
        assert _history_turns(history["result"]["messages"]) == expected

        transcript = await owner.dispatch({"id": 3, "method": "session.transcript",
                                           "params": {"session_id": ref.session_id}})
        assert [row["text"] for row in transcript["result"]["rows"]] == [text for _r, text in expected]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_linked_transcript_pages_by_cursor_in_both_directions(tmp_path, monkeypatch, native_stores):
    """Paging must walk the native turns one window at a time, newest-first included."""
    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _handle)
    _link_claude(db, ref.session_id)
    try:
        first = await owner.dispatch({"id": 1, "method": "session.transcript",
                                      "params": {"session_id": ref.session_id, "limit": 2}})
        assert [row["text"] for row in first["result"]["rows"]] == [
            "claude question 0", "claude answer 0"]
        rest = await owner.dispatch({"id": 2, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "limit": 2, "cursor": first["result"]["next_cursor"]}})
        assert rest["result"]["reset"] is False
        assert [row["text"] for row in rest["result"]["rows"]] == [
            "claude question 1", "claude answer 1"]

        newest = await owner.dispatch({"id": 3, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "limit": 2, "order": "latest"}})
        assert [row["text"] for row in newest["result"]["rows"]] == [
            "claude question 1", "claude answer 1"]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unreadable_native_store_is_a_typed_refusal_not_an_empty_transcript(
        tmp_path, monkeypatch, native_stores):
    """A vanished native store must fail closed: an empty list would read as "no conversation"."""
    claude_home, _vibe = native_stores
    (claude_home / "projects" / "-home-workspace" / f"{CLAUDE_ID}.jsonl").unlink()

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _handle)
    _link_claude(db, ref.session_id)
    try:
        reply = await owner.dispatch({"id": 1, "method": "session.history",
                                      "params": {"session_id": ref.session_id}})
        assert reply["error"]["data"]["reason"] == "storage_unavailable", reply
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_ordinary_session_reads_are_unchanged(tmp_path, monkeypatch, native_stores):
    """The linked resolution must not touch a row that is provably not a pointer."""
    from tests.fixtures.storage_seed import seed_messages

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, _handle)
    seed_messages(db, ref.session_id, {"role": "user", "content": "plain question"},
                  {"role": "assistant", "content": "plain answer"})
    try:
        history = await owner.dispatch({"id": 1, "method": "session.history",
                                        "params": {"session_id": ref.session_id}})
        assert _history_turns(history["result"]["messages"]) == [
            ("user", "plain question"), ("assistant", "plain answer")]
        transcript = await owner.dispatch({"id": 2, "method": "session.transcript",
                                           "params": {"session_id": ref.session_id}})
        assert [row["text"] for row in transcript["result"]["rows"]] == [
            "plain question", "plain answer"]
    finally:
        await owner.close()
        store.close_all_db_handles()
