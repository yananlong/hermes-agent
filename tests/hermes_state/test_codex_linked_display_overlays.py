"""Linked native history retains only the settled, redacted display decision."""

import json

from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_holder import raw_holder


THREAD = "native-thread"


def linked_db(path):
    db = open_persistence(profile_scope(home=path), eager=True)
    db.sessions.create(NewSession("linked", "desktop", model_config={
        "codex_linked_thread": True,
        "codex_thread_id": THREAD,
    }))
    return db


def test_overlay_is_owner_bound_redacted_and_retained_as_settled_state(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.redact.redact_sensitive_text",
                        lambda value, force=False: value.replace("PRIVATE", "[redacted]") if force else value)
    db = linked_db(tmp_path)
    try:
        assert db.sessions.save_linked_codex_display_overlay(
            "linked", THREAD, "turn-1", final_text="PRIVATE filtered reply", held=True,
            platform="local")
        overlays = db.sessions.load_linked_codex_display_overlays("linked", THREAD, ["turn-1"])
        assert overlays["turn-1"] == {
            "version": 1,
            "thread_id": THREAD,
            "turn_id": "turn-1",
            "held": True,
            "final_visible": True,
            "final_text": "[redacted] filtered reply",
            "platform": "local",
            "created_at": overlays["turn-1"]["created_at"],
        }
        assert not db.sessions.save_linked_codex_display_overlay(
            "linked", "another-thread", "turn-2", final_text="wrong owner", held=False,
            platform="local")
        assert "another-thread" not in db.sessions.load_linked_codex_display_overlays(
            "linked", "another-thread", ["turn-2"])
        assert "PRIVATE" not in json.dumps(overlays)
    finally:
        db.close()


def test_safe_overlay_survives_past_turn_25_with_page_scoped_reads(tmp_path, monkeypatch):
    ticks = iter(float(value) for value in range(1, 30))
    monkeypatch.setattr("persistence.sqlite.conversation._store.codex_display.time.time", lambda: next(ticks))
    db = linked_db(tmp_path)
    try:
        for index in range(25):
            assert db.sessions.save_linked_codex_display_overlay(
                "linked", THREAD, f"turn-{index:02}", final_text=f"display {index}",
                held=False, platform="local")
        overlays = db.sessions.load_linked_codex_display_overlays(
            "linked", THREAD, [f"turn-{index:02}" for index in range(25)])
        assert len(overlays) == 25
        assert overlays["turn-00"]["final_text"] == "display 0"
        assert overlays["turn-24"]["final_text"] == "display 24"
    finally:
        db.close()


def test_overlay_storage_prunes_by_byte_cap_without_returning_out_of_page_rows(tmp_path, monkeypatch):
    from persistence.sqlite.conversation._store import codex_display as display

    monkeypatch.setattr(display, "_MAX_OVERLAY_BYTES", 700)
    db = linked_db(tmp_path)
    ticks = iter((1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(display.time, "time", lambda: next(ticks))
    try:
        for index in range(4):
            assert db.sessions.save_linked_codex_display_overlay(
                "linked", THREAD, f"turn-{index}", final_text=f"reply {index} " + "x" * 100,
                held=False, platform="local")
        storage_bytes = raw_holder(db)._read_one(
            "SELECT COALESCE(SUM(length(CAST(overlay_json AS BLOB))), 0) "
            "FROM codex_linked_display_overlays WHERE session_id = ? AND thread_id = ?",
            ("linked", THREAD),
        )[0]
        assert storage_bytes <= display._MAX_OVERLAY_BYTES
        overlays = db.sessions.load_linked_codex_display_overlays("linked", THREAD, ["turn-0", "turn-3"])
        assert "turn-0" not in overlays
        assert overlays["turn-3"]["final_text"].startswith("reply 3")
        assert len(db.sessions.load_linked_codex_display_overlays("linked", THREAD, ["turn-3"])) == 1
    finally:
        db.close()


def test_escaped_overlay_text_round_trips_within_record_byte_bound(tmp_path, monkeypatch):
    from persistence.sqlite.conversation._store import codex_display as display

    monkeypatch.setattr("agent.redact.redact_sensitive_text", lambda value, force=False: value)
    db = linked_db(tmp_path)
    text = "\x01" * 50_000
    try:
        assert len(text.encode("utf-8")) <= display._MAX_OVERLAY_TEXT_BYTES
        assert db.sessions.save_linked_codex_display_overlay(
            "linked", THREAD, "control-heavy", final_text=text, held=False, platform="local")
        stored_bytes = raw_holder(db)._read_one(
            "SELECT length(CAST(overlay_json AS BLOB)) FROM codex_linked_display_overlays "
            "WHERE session_id = ? AND thread_id = ? AND turn_id = ?",
            ("linked", THREAD, "control-heavy"),
        )[0]
        assert stored_bytes > display._MAX_OVERLAY_TEXT_BYTES + 4096
        assert stored_bytes <= display._MAX_OVERLAY_RECORD_BYTES
        overlays = db.sessions.load_linked_codex_display_overlays("linked", THREAD, ["control-heavy"])
        assert overlays["control-heavy"]["final_text"] == text
    finally:
        db.close()


def test_overlay_rows_follow_session_delete_foreign_key(tmp_path):
    db = linked_db(tmp_path)
    try:
        db.sessions.save_linked_codex_display_overlay(
            "linked", THREAD, "turn-1", final_text="display", held=False, platform="local")
        db.sessions.delete("linked", media_root=str(tmp_path / "media"))
        assert raw_holder(db)._read_one(
            "SELECT 1 FROM codex_linked_display_overlays WHERE session_id = ?", ("linked",)) is None
    finally:
        db.close()
