"""The streaming session export route must not return an empty linked-thread transcript."""

import pytest
from fastapi import HTTPException

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session
from hermes_cli.web_routers import sessions as sessions_router


@pytest.mark.asyncio
async def test_streaming_export_refuses_linked_codex_pointer(monkeypatch, tmp_path):
    with open_persistence(profile_scope(home=tmp_path)) as store:
        seed_session(store, "linked", "desktop", title="Native thread",
                     model_config={"codex_thread_id": "native-thread", "codex_linked_thread": True})
        monkeypatch.setattr(sessions_router, "_with_db", lambda profile, fn, *, read_only: fn(store))

        with pytest.raises(HTTPException) as excinfo:
            await sessions_router.export_session_endpoint("linked")

    assert excinfo.value.status_code == 409
    assert "history is stored in Codex" in excinfo.value.detail
