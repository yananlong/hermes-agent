"""Linked Claude sessions store native session IDs and never import their history."""

import json

import pytest

from hermes_cli import claude_linked_threads as linked
from persistence_assembly import open_persistence, profile_scope
from persistence.conversation.sessions import SessionQuery, SessionWindow
from persistence.conversation.messages import TranscriptRead

SESSION_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"


def _write_session(projects, session_id, cwd, lines):
    project = projects / "-home-workspace"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _lines(cwd, with_summary=True):
    lines = []
    if with_summary:
        lines.append({"type": "summary", "summary": "Refactor the parser"})
    lines += [
        {"type": "user", "cwd": cwd, "sessionId": SESSION_ID,
         "message": {"role": "user", "content": "please refactor the parser"}},
        {"type": "assistant", "cwd": cwd, "sessionId": SESSION_ID,
         "message": {"role": "assistant", "model": "claude-opus-4-5",
                     "content": [{"type": "text", "text": "Done, see the diff."}]}},
        {"type": "user", "isSidechain": True, "sessionId": SESSION_ID,
         "message": {"role": "user", "content": "sidechain noise"}},
    ]
    return lines


@pytest.fixture
def store(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    _write_session(projects, SESSION_ID, str(cwd), _lines(str(cwd)))
    _write_session(projects, OTHER_ID, str(cwd), _lines(str(cwd), with_summary=False))
    monkeypatch.setattr(linked, "resolve_claude_projects", lambda profile_home=None: projects)
    return projects, cwd


def test_list_pages_sessions_newest_first_without_previews(store):
    projects, _ = store
    (projects / "-home-workspace" / f"{OTHER_ID}.jsonl").touch()
    first = linked.list_claude_threads(limit=1)
    assert first["threads"][0]["id"] == OTHER_ID
    assert first["threads"][0]["preview"] == ""
    assert first["next_cursor"] == "1"
    second = linked.list_claude_threads(cursor=first["next_cursor"], limit=25)
    assert second["threads"][0]["id"] == SESSION_ID
    assert second["threads"][0]["title"] == "Refactor the parser"
    assert second["next_cursor"] is None
    with pytest.raises(ValueError):
        linked.list_claude_threads(cursor="bogus")


def test_history_reads_live_jsonl_as_safe_rows(store):
    _, _cwd = store
    page = linked.read_claude_history(SESSION_ID, limit=10)
    assert [row["role"] for row in page["messages"]] == ["user", "assistant"]
    assert page["messages"][1]["content"] == "Done, see the diff."
    assert page["next_cursor"] is None
    with pytest.raises(ValueError):
        linked.read_claude_history("../../etc/passwd")


def test_link_is_idempotent_and_keeps_only_pointer(tmp_path, store):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        first = linked.link_claude_thread(SESSION_ID, db, "default")
        second = linked.link_claude_thread(SESSION_ID, db, "default")
        assert first["session_id"] == second["session_id"]
        assert not first["already_linked"] and second["already_linked"]
        row = db.sessions.get(first["session_id"])
        config = row.model_config
        assert config["claude_session_id"] == SESSION_ID
        assert config["claude_linked_thread"] is True
        assert config["api_mode"] == "claude_linked_readonly"
        assert row.title == "Refactor the parser"
        assert row.model == "claude-opus-4-5"
        assert list(db.messages.read_transcript(first["session_id"], TranscriptRead())) == []
        listed1 = {r.session.session_id for r in db.sessions.list_sessions(
            SessionQuery(min_message_count=1), SessionWindow(limit=100))}
        listed2 = {r.session.session_id for r in db.sessions.list_sessions(
            SessionQuery(min_message_count=2), SessionWindow(limit=100))}
        assert first["session_id"] in listed1
        assert first["session_id"] not in listed2
    finally:
        db.close()


def test_link_rejects_unknown_session_and_missing_cwd(tmp_path, store, monkeypatch):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        with pytest.raises(ValueError):
            linked.link_claude_thread("9c8d7e6f-5a4b-4c3d-8e2f-1a0b9c8d7e6f", db, "default")
        projects, _cwd = store
        missing = tmp_path / "gone"
        _write_session(projects, OTHER_ID, str(missing), _lines(str(missing)))
        with pytest.raises(ValueError, match="working directory"):
            linked.link_claude_thread(OTHER_ID, db, "default")
    finally:
        db.close()


def test_import_payload_rejects_linked_foreign_sessions(tmp_path):
    from persistence.sqlite.housekeeping._portability import _validate_import_payload

    payload = [{"id": "abc123", "source": "desktop", "title": "t", "cwd": "/tmp",
                "model_config": {"claude_linked_thread": True, "claude_session_id": SESSION_ID},
                "messages": []}]
    normalized, errors = _validate_import_payload(payload)
    assert errors and "native history" in errors[0]["error"]
    assert not normalized


def test_history_pages_backwards_from_the_newest_rows(tmp_path, store):
    """A transcript larger than one page opens on its newest rows and walks towards the top.

    This is the shape a scroll-up surface needs: the first call shows recent work, and each
    ``next_cursor`` loads the rows above it, so the whole-file read (and its size ceiling) is
    never required just to open a long conversation.
    """
    projects, cwd = store
    cwd = str(cwd)
    turns = [{"type": "summary", "summary": "Long conversation"}]
    for i in range(6):
        turns.append({"type": "user", "cwd": cwd, "message": {"role": "user", "content": f"question {i}"}})
        turns.append({"type": "assistant", "cwd": cwd, "message": {"role": "assistant", "content": f"answer {i}"}})
    _write_session(projects, OTHER_ID, cwd, turns)

    first = linked.read_claude_history(OTHER_ID, limit=4)
    newest = [row["content"] for row in first["messages"]]
    assert "answer 5" in newest, "the newest turn must be on the first page"
    assert "question 0" not in newest, "the oldest turn must not be on the first page"
    assert first["next_cursor"] is not None

    seen = list(newest)
    cursor = first["next_cursor"]
    while cursor is not None:
        page = linked.read_claude_history(OTHER_ID, cursor=cursor, limit=4)
        assert page["messages"], "a non-final page must return rows"
        seen = [row["content"] for row in page["messages"]] + seen
        cursor = page["next_cursor"]

    # Every turn appears exactly once, in reading order, newest last.
    assert len(seen) == 12
    assert seen[0] == "question 0" and seen[-1] == "answer 5"
    assert len(set(seen)) == 12, "paging must not repeat or skip a row"


def test_history_page_survives_a_damaged_record(store):
    """One torn line must not hide the rest of the transcript (the killed-append class)."""
    projects, cwd = store
    cwd = str(cwd)
    path = _write_session(projects, OTHER_ID, cwd, _lines(cwd, with_summary=False))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"user","message":{"role":"user","conte\n')
    page = linked.read_claude_history(OTHER_ID, limit=10)
    assert [row["role"] for row in page["messages"]] == ["user", "assistant"]
