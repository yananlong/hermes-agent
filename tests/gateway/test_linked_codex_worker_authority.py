"""The owner's worker RPC fences native-model writes to the assigned, authenticated worker."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli.process_identity import worker_birth
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle


THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
OTHER_THREAD = "02a1c4a4-7a29-7011-8493-04445af16d6e"


@pytest.mark.asyncio
async def test_worker_model_receipt_requires_the_same_profile_process_and_native_thread(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_contract import SessionActor, SessionRef
    from gateway.session_worker import worker_request
    from persistence.core.errors import AuthorityRefusal

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = storage_handle(tmp_path)
    seed_session(db, "owned", "desktop", model="old", model_config={
        "codex_linked_thread": True, "codex_thread_id": THREAD, "api_mode": "codex_app_server"})
    seed_session(db, "other", "desktop", model="other", model_config={
        "codex_linked_thread": True, "codex_thread_id": THREAD, "api_mode": "codex_app_server"})
    runner = SimpleNamespace(_draining=False, config=SimpleNamespace(multiplex_profiles=False),
                             _adapter_for_source=lambda _source: None)
    authority = SessionAuthority(runner, profile_id="profile", instance_id="owner",
                                 epoch=db.runtime.begin_epoch(instance_id="owner"), persistence=db)
    authority.sessions["owned"] = LiveSession(SimpleNamespace(platform=Platform.TELEGRAM, user_id="human"), "route")
    ref = SessionRef("profile", "owned")
    worker = SimpleNamespace(authority=authority, actor=SessionActor(
        "human", "profile", frozenset({"worker:adopt"}), "transport"))
    interactive = SimpleNamespace(authority=authority, actor=SessionActor(
        "human", "profile", frozenset({"session:control"}), "transport"))
    wrong_principal = SimpleNamespace(authority=authority, actor=SessionActor(
        "other-human", "profile", frozenset({"worker:adopt"}), "transport"))
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,  # noqa: ASYNC220 -- Popen returns immediately; stdin controls the child's lifetime
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        scope = {"profile_id": "profile", "session_id": "owned", "execution_id": "linked-worker",
                 "generation": 0, "pid": child.pid, "birth": worker_birth(psutil.Process(child.pid)),
                 "secret": "worker-secret-" * 3}
        registered = await worker_request(worker, ref, scope | {"kind": "compute"}, operation="register")
        params = scope | {"epoch": registered["owner_epoch"], "sequence": 1,
                          "operation": "session.linked_codex_model",
                          "payload": {"model": "new", "thread_id": THREAD}}
        for caller, override, reason in (
            (interactive, {}, "permission_denied"),
            (wrong_principal, {}, "permission_denied"),
            (worker, {"profile_id": "other-profile"}, "profile_mismatch"),
            (worker, {"session_id": "other"}, "permission_denied"),
            (worker, {"secret": "wrong-worker-secret-" * 2}, "permission_denied"),
            (worker, {"generation": 1}, "stale_generation"),
            (worker, {"payload": {"model": "new", "thread_id": "invalid"}}, "invalid_params"),
        ):
            with pytest.raises(AuthorityRefusal) as refused:
                await worker_request(caller, ref, params | override, operation="persist")
            assert refused.value.reason == reason
        assert db.sessions.get("owned").model == "old"
        assert db.sessions.get("other").model == "other"
        wrong = params | {"payload": {"model": "new", "thread_id": OTHER_THREAD}}
        assert await worker_request(worker, ref, wrong, operation="persist") == {"value": False}
        assert db.sessions.get("owned").model == "old"
        correct = params | {"sequence": 2}
        assert await worker_request(worker, ref, correct, operation="persist") == {"value": True}
        assert await worker_request(worker, ref, correct, operation="persist") == {"value": True}
        assert db.sessions.get("owned").model == "new"
        assert db.sessions.get("other").model == "other"
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        db.close()
