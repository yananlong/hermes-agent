"""Linked Mistral Vibe sessions store native session IDs and never import their history."""

import json

import pytest

from hermes_cli import mistral_linked_threads as linked
from persistence_assembly import open_persistence, profile_scope
from persistence.conversation.sessions import SessionQuery, SessionWindow
from persistence.conversation.messages import TranscriptRead

SESSION_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
LEGACY_SHORT = "ab12cd34"


def _write_unified(root, session_id, cwd):
    session = root / "unified" / session_id
    (session / "journal").mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({
        "session_id": session_id,
        "title": "Vibe refactor",
        "model": "devstral-2512",
        "environment": {"working_directory": cwd},
        "bumped_at": 1_700_000_000,
    }), encoding="utf-8")
    (session / "journal" / "0000000000000001.jsonl").write_text("\n".join([
        json.dumps({"type": "projection_delta", "payload": {"delta": [
            {"entry": {"role": "user", "content": [{"type": "text", "text": "fix the flaky test"}]}}]}}),
        json.dumps({"type": "projection_delta", "payload": {"delta": [
            {"entry": {"role": "assistant", "content": [{"type": "text", "text": "Fixed."}]}}]}}),
    ]) + "\n", encoding="utf-8")
    return session


def _write_legacy(root, short, cwd):
    session = root / f"session_20260101T120000_{short}"
    session.mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({
        "title": "Legacy vibe chat", "cwd": cwd, "model": "mistral-large-latest",
    }), encoding="utf-8")
    (session / "messages.jsonl").write_text("\n".join([
        json.dumps({"role": "user", "content": "hello"}),
        json.dumps({"role": "assistant", "content": "hi there"}),
    ]) + "\n", encoding="utf-8")
    return session


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "session-root"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    _write_unified(root, SESSION_ID, str(cwd))
    _write_legacy(root, LEGACY_SHORT, str(cwd))
    monkeypatch.setattr(linked, "resolve_vibe_session_root", lambda profile_home=None: root)
    return root, cwd


def test_list_covers_unified_and_legacy_sessions_without_previews(store):
    root, _ = store
    page = linked.list_mistral_threads(limit=10)
    ids = {row["id"] for row in page["threads"]}
    assert ids == {SESSION_ID, LEGACY_SHORT}
    by_id = {row["id"]: row for row in page["threads"]}
    assert by_id[SESSION_ID]["title"] == "Vibe refactor"
    assert by_id[SESSION_ID]["updated_at"] == 1_700_000_000_000
    assert by_id[LEGACY_SHORT]["title"] == "Legacy vibe chat"
    assert all(row["preview"] == "" for row in page["threads"])
    assert page["next_cursor"] is None


def test_history_reads_unified_journal_and_legacy_messages(store):
    page = linked.read_mistral_history(SESSION_ID, limit=10)
    assert [(r["role"], r["content"]) for r in page["messages"]] == [
        ("user", "fix the flaky test"), ("assistant", "Fixed.")]
    legacy = linked.read_mistral_history(LEGACY_SHORT, limit=10)
    assert [(r["role"], r["content"]) for r in legacy["messages"]] == [
        ("user", "hello"), ("assistant", "hi there")]
    with pytest.raises(ValueError):
        linked.read_mistral_history("not-a-session")


def test_link_is_idempotent_and_keeps_only_pointer(tmp_path, store):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        first = linked.link_mistral_thread(SESSION_ID, db, "default")
        second = linked.link_mistral_thread(SESSION_ID, db, "default")
        assert first["session_id"] == second["session_id"]
        assert not first["already_linked"] and second["already_linked"]
        row = db.sessions.get(first["session_id"])
        config = row.model_config
        assert config["mistral_session_id"] == SESSION_ID
        assert config["mistral_linked_thread"] is True
        assert config["api_mode"] == "mistral_linked_readonly"
        assert row.title == "Vibe refactor"
        assert row.model == "devstral-2512"
        assert list(db.messages.read_transcript(first["session_id"], TranscriptRead())) == []
        listed1 = {r.session.session_id for r in db.sessions.list_sessions(
            SessionQuery(min_message_count=1), SessionWindow(limit=100))}
        assert first["session_id"] in listed1
        legacy = linked.link_mistral_thread(LEGACY_SHORT, db, "default")
        assert db.sessions.get(legacy["session_id"]).model_config["mistral_session_id"] == LEGACY_SHORT
    finally:
        db.close()


def test_link_rejects_unknown_session(tmp_path, store):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        with pytest.raises(ValueError):
            linked.link_mistral_thread("3d4e5f6a-7b8c-4d9e-8f0a-1b2c3d4e5f6a", db, "default")
        with pytest.raises(ValueError):
            linked.link_mistral_thread("", db, "default")
    finally:
        db.close()


def test_import_payload_rejects_mistral_link(tmp_path):
    from persistence.sqlite.housekeeping._portability import _validate_import_payload

    payload = [{"id": "abc123", "source": "desktop", "title": "t", "cwd": "/tmp",
                "model_config": {"mistral_linked_thread": True, "mistral_session_id": SESSION_ID},
                "messages": []}]
    normalized, errors = _validate_import_payload(payload)
    assert errors and "native history" in errors[0]["error"]
    assert not normalized
