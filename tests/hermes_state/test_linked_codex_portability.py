"""Linked foreign rows (Codex/Claude/Mistral pointers) are not complete Hermes snapshots."""

import json
import pytest

from persistence.core.errors import LinkedCodexExportUnavailable
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session


def test_linked_foreign_exports_refuse_instead_of_returning_empty_history(tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        for sid, config in (
            ("linked-codex", {"codex_thread_id": "native-thread", "codex_linked_thread": True}),
            ("linked-claude", {"claude_session_id": "native-claude", "claude_linked_thread": True}),
            ("linked-mistral", {"mistral_session_id": "native-mistral", "mistral_linked_thread": True}),
        ):
            seed_session(db, sid, "desktop", model_config=config)
            db.sessions.set_title(sid, f"Native session {sid}")
            with pytest.raises(LinkedCodexExportUnavailable):
                db.maintenance.export_session(sid)
            with pytest.raises(LinkedCodexExportUnavailable):
                db.maintenance.export_session_lineage(sid)
        with pytest.raises(LinkedCodexExportUnavailable):
            db.maintenance.export_all()
    finally:
        db.close()


def test_import_refuses_linked_pointer_but_keeps_ordinary_codex_snapshot_behavior(
    tmp_path,
):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        linked = {
            "id": "linked",
            "source": "desktop",
            "title": "Native thread",
            "messages": [],
            "model_config": {
                "codex_thread_id": "native-thread",
                "codex_linked_thread": True,
            },
        }
        result = db.maintenance.import_sessions([linked])
        assert not result.ok
        assert "native history" in result.errors[0]["error"]
        assert db.sessions.get("linked") is None

        linked_foreign = {
            **linked,
            "id": "linked-foreign",
            "model_config": {
                "claude_session_id": "native-claude",
                "claude_linked_thread": True,
            },
        }
        foreign_result = db.maintenance.import_sessions([linked_foreign])
        assert not foreign_result.ok
        assert "native history" in foreign_result.errors[0]["error"]
        assert db.sessions.get("linked-foreign") is None

        linked_json = {
            **linked,
            "id": "linked-json",
            "model_config": json.dumps(linked["model_config"]),
        }
        json_result = db.maintenance.import_sessions([linked_json])
        assert not json_result.ok
        assert "native history" in json_result.errors[0]["error"]
        assert db.sessions.get("linked-json") is None

        ordinary = {
            "id": "ordinary",
            "source": "desktop",
            "title": "Codex chat",
            "model_config": {
                "codex_thread_id": "native-thread",
                "api_mode": "codex_app_server",
            },
            "messages": [{"role": "user", "content": "saved locally"}],
        }
        imported = db.maintenance.import_sessions([ordinary])
        assert imported.imported == 1 and not imported.errors
        saved = db.sessions.get("ordinary")
        assert transcript_rows(db, "ordinary")[0]["content"] == "saved locally"
        assert saved.model_config["codex_thread_id"] == "native-thread"
    finally:
        db.close()
