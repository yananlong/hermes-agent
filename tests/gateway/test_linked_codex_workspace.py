"""Canonical workspace mutations for linked Codex threads keep the native thread identity fenced."""

from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import session_row

from agent.transports.codex_app_server_session import CodexThreadResumeError
from tests.gateway.test_session_policy_verbs import _mutation, _owner

THREAD_ID = "11111111-1111-4111-8111-111111111111"
UNOPENED_THREAD_ID = "22222222-2222-4222-8222-222222222222"


class FakeCodexSession:
    plans = []
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.plan = self.plans.pop(0) if self.plans else {}
        self.effective_cwd = None
        self.closed = False
        self.instances.append(self)

    def ensure_started(self):
        if error := self.plan.get("error"):
            raise error
        self.effective_cwd = self.plan.get("cwd", self.kwargs["cwd"])
        return self.kwargs["thread_id"]

    def close(self):
        self.closed = True


def _linked_owner(tmp_path, monkeypatch):
    from tui_gateway import server

    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, None)
    old = Path(session_row(db, ref.session_id)["cwd"])
    target = tmp_path / "project-b"
    target.mkdir()
    db.sessions.patch_model_config(ref.session_id, {
        "api_mode": "codex_app_server",
        "codex_linked_thread": True,
        "codex_thread_id": THREAD_ID,
        "provider": "custom",
    })

    FakeCodexSession.plans = []
    FakeCodexSession.instances = []

    @contextmanager
    def fake_resume(thread_id, cwd, profile_home, *, override, accept_native=False):
        native = FakeCodexSession(thread_id=thread_id, cwd=cwd, profile_home=profile_home,
                                  override=override, accept_native=accept_native)
        try:
            native.ensure_started()
            yield native
        finally:
            native.close()

    monkeypatch.setattr(server, "_linked_codex_resume", fake_resume)
    return store, db, authority, owner, ref, old, target


def _unopened_linked_owner(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from gateway.session_contract import SessionRef
    from hermes_cli import codex_linked_threads
    from tui_gateway import server
    from gateway.session_local_migration import bind_native_transport

    store, db, authority, owner, _ = _owner(tmp_path, monkeypatch, None)
    bind_native_transport(authority, owner.actor,
                         {"provider": "local", "profile_id": "owned", "instance_id": "owner"})
    old = tmp_path / "project-a"
    target = tmp_path / "project-unopened"
    target.mkdir()

    @contextmanager
    def fake_read_client(*_args, **_kwargs):
        yield SimpleNamespace(request=lambda *_a, **_kw: {"thread": {
            "id": UNOPENED_THREAD_ID, "name": "Unopened fixture", "cwd": str(old),
            "model": "frozen", "modelProvider": "openai",
        }})

    native_calls = []

    @contextmanager
    def fake_resume(thread_id, cwd, profile_home, *, override, accept_native=False):
        native = SimpleNamespace(thread_id=thread_id, cwd=cwd, profile_home=profile_home,
                                 override=override, accept_native=accept_native,
                                 effective_cwd=cwd, closed=False)
        native_calls.append(native)
        try:
            yield native
        finally:
            native.closed = True

    monkeypatch.setattr(codex_linked_threads, "_codex_client", fake_read_client)
    monkeypatch.setattr(server, "_linked_codex_resume", fake_resume)
    monkeypatch.setattr(server, "_cwd_is_remote", lambda _profile: False)
    monkeypatch.setattr(server, "_workspace_cwd", lambda _profile, cwd: str(Path(cwd).resolve()))
    monkeypatch.setattr(server.git_probe, "branch", lambda cwd: f"branch-{Path(cwd).name}")
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: None)

    linked = codex_linked_threads.link_codex_thread(UNOPENED_THREAD_ID, db, "default")
    ref = SessionRef("owned", linked["session_id"])
    return store, db, authority, owner, ref, old, target, native_calls


@pytest.mark.asyncio
async def test_canonical_workspace_mutation_resumes_the_same_linked_thread_and_mounts_cold_row(
    tmp_path, monkeypatch,
):
    store, db, authority, owner, ref, _old, target = _linked_owner(tmp_path, monkeypatch)
    before = session_row(db, ref.session_id)
    request = _mutation(db, ref, "workspace-1", "workspace", {"cwd": str(target)})
    # A stored linked session must be mounted by canonical authorization before the workspace gates.
    authority.sessions.pop(ref.session_id)
    try:
        response = await owner.dispatch(request)

        assert "result" in response, response
        assert response["result"]["cwd"] == str(target)
        assert ref.session_id in authority.sessions
        assert session_row(db, ref.session_id)["cwd"] == str(target)
        policy = db.bindings.receipt(ref.session_id).policy
        assert policy["cwd"] == str(target)
        assert json.loads(policy["terminal_json"])["TERMINAL_CWD"] == str(target)
        assert policy["model"] == "frozen"
        assert db.sessions.get_model_config_value(ref.session_id, "codex_thread_id") == THREAD_ID
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert session_row(db, ref.session_id)["runtime_revision"] == before["runtime_revision"] + 1
        assert session_row(db, ref.session_id)["runtime_generation"] == before["runtime_generation"] + 1

        native = FakeCodexSession.instances[0]
        assert native.kwargs["thread_id"] == THREAD_ID
        assert native.kwargs["override"] is True
        assert native.kwargs["accept_native"] is False
        assert native.closed
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_canonical_workspace_mutation_refuses_a_native_active_writer_without_row_changes(
    tmp_path, monkeypatch,
):
    store, db, _authority, owner, ref, old, target = _linked_owner(tmp_path, monkeypatch)
    before = session_row(db, ref.session_id)
    prior_policy = db.bindings.receipt(ref.session_id).policy
    FakeCodexSession.plans = [{"error": CodexThreadResumeError(THREAD_ID, "already has an active writer")}]
    request = _mutation(db, ref, "workspace-busy", "workspace", {"cwd": str(target)})
    try:
        response = await owner.dispatch(request)

        assert response.get("error", {}).get("data", {}).get("reason") == "session_busy", response
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        assert db.bindings.receipt(ref.session_id).policy == prior_policy
        assert session_row(db, ref.session_id)["runtime_revision"] == before["runtime_revision"]
        assert session_row(db, ref.session_id)["runtime_generation"] == before["runtime_generation"]
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert len(FakeCodexSession.instances) == 1 and FakeCodexSession.instances[0].closed
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pending_workspace_recovery_reports_busy_and_preserves_intent(tmp_path, monkeypatch):
    store, db, _authority, owner, ref, old, target = _linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "workspace-recovery-busy", "workspace", {"cwd": str(target)})
    intent = {
        "thread_id": THREAD_ID,
        "from": str(old),
        "to": str(target),
        "request_id": "workspace-recovery-busy",
        "principal_id": owner.actor.subject,
    }
    assert db.sessions.claim_linked_codex_workspace_change(ref.session_id, intent)
    before = session_row(db, ref.session_id)
    FakeCodexSession.plans = [{"error": CodexThreadResumeError(THREAD_ID, "already has an active writer")}]
    try:
        response = await owner.dispatch(request)

        assert response.get("error", {}).get("data", {}).get("reason") == "session_busy", response
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") == intent
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        assert session_row(db, ref.session_id)["runtime_revision"] == before["runtime_revision"]
        assert session_row(db, ref.session_id)["runtime_generation"] == before["runtime_generation"]
        native = FakeCodexSession.instances[0]
        assert native.kwargs["override"] is False
        assert native.kwargs["accept_native"] is True
        assert native.closed
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_pending_workspace_recovery_can_sync_codex_current_directory_without_override(tmp_path, monkeypatch):
    store, db, _authority, owner, ref, old, pending_target = _linked_owner(tmp_path, monkeypatch)
    external = tmp_path / "project-codex-selected"
    external.mkdir()
    intent = {
        "thread_id": THREAD_ID,
        "from": str(old),
        "to": str(pending_target),
        "request_id": "workspace-abandoned",
        "principal_id": owner.actor.subject,
    }
    assert db.sessions.claim_linked_codex_workspace_change(ref.session_id, intent)
    FakeCodexSession.plans = [{"cwd": str(external)}]
    request = _mutation(db, ref, "workspace-sync-native", "workspace", {"cwd": str(external)})
    try:
        response = await owner.dispatch(request)

        assert "result" in response, response
        assert response["result"]["cwd"] == str(external)
        assert session_row(db, ref.session_id)["cwd"] == str(external)
        policy = db.bindings.receipt(ref.session_id).policy
        assert policy["cwd"] == str(external)
        assert json.loads(policy["terminal_json"])["TERMINAL_CWD"] == str(external)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert len(FakeCodexSession.instances) == 1
        native = FakeCodexSession.instances[0]
        assert native.kwargs["override"] is False
        assert native.kwargs["accept_native"] is True
        assert native.closed
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_linked_workspace_refuses_frozen_ssh_terminal_profile(tmp_path, monkeypatch):
    from persistence.sqlite.conversation._store.local import POLICY_PREFIX

    store, db, _authority, owner, ref, old, target = _linked_owner(tmp_path, monkeypatch)
    receipt = json.loads(db.state_meta.get(POLICY_PREFIX + ref.session_id))
    terminal = json.loads(receipt["policy"]["terminal_json"])
    terminal["TERMINAL_ENV"] = "ssh"
    receipt["policy"]["terminal_json"] = json.dumps(terminal)
    db.state_meta.set(POLICY_PREFIX + ref.session_id, json.dumps(receipt))
    before = session_row(db, ref.session_id)
    request = _mutation(db, ref, "workspace-ssh", "workspace", {"cwd": str(target)})
    try:
        response = await owner.dispatch(request)

        assert response.get("error", {}).get("data", {}).get("reason") == "unsupported_operation", response
        assert "local to the gateway" in response["error"]["data"]["detail"]
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        assert session_row(db, ref.session_id)["runtime_revision"] == before["runtime_revision"]
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert FakeCodexSession.instances == []
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_exact_committed_workspace_retry_does_not_resume_or_override_again(tmp_path, monkeypatch):
    store, db, _authority, owner, ref, _old, target = _linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "workspace-retry", "workspace", {"cwd": str(target)})
    try:
        first = await owner.dispatch(request)
        assert "result" in first, first
        revision = session_row(db, ref.session_id)["runtime_revision"]
        retry = await owner.dispatch(request)

        assert retry == first
        assert session_row(db, ref.session_id)["runtime_revision"] == revision
        assert len(FakeCodexSession.instances) == 1
        assert FakeCodexSession.instances[0].kwargs["override"] is True
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_linked_workspace_moves_row_without_fabricating_local_policy_and_replays_exactly(
    tmp_path, monkeypatch,
):
    from tests.gateway.test_session_policy_verbs import _mutation

    store, db, authority, owner, ref, _old, target, native_calls = _unopened_linked_owner(tmp_path, monkeypatch)
    before = session_row(db, ref.session_id)
    request = _mutation(db, ref, "unopened-workspace", "workspace", {"cwd": str(target)})
    try:
        first = await owner.dispatch(request)
        assert first.get("result", {}).get("cwd") == str(target), first
        assert session_row(db, ref.session_id)["cwd"] == str(target)
        assert session_row(db, ref.session_id)["git_branch"] == f"branch-{target.name}"
        assert session_row(db, ref.session_id)["runtime_revision"] == before["runtime_revision"] + 1
        assert session_row(db, ref.session_id)["runtime_generation"] == before["runtime_generation"] + 1
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert not db.bindings.binding_marks(ref.session_id).policy
        assert ref.session_id not in authority.sessions
        assert len(native_calls) == 1
        assert (native_calls[0].thread_id, native_calls[0].override, native_calls[0].closed) == (
            UNOPENED_THREAD_ID, True, True)

        retry = await owner.dispatch(request)
        assert retry == first
        assert len(native_calls) == 1
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_linked_workspace_recovers_native_change_on_exact_retry(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from tests.gateway.test_session_policy_verbs import _mutation
    from tui_gateway import server

    store, db, _authority, owner, ref, old, target, native_calls = _unopened_linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "unopened-workspace-recovery", "workspace", {"cwd": str(target)})
    holder = raw_holder(db)
    original_execute = holder._execute_write
    fail_commit = {"armed": False}

    def fail_once(callback, **kwargs):
        if fail_commit["armed"]:
            fail_commit["armed"] = False
            raise OSError("synthetic receipt commit failure")
        return original_execute(callback, **kwargs)

    @contextmanager
    def resume_then_fail_commit(thread_id, cwd, profile_home, *, override, accept_native=False):
        native = SimpleNamespace(thread_id=thread_id, cwd=cwd, profile_home=profile_home,
                                 override=override, accept_native=accept_native,
                                 effective_cwd=cwd, closed=False)
        native_calls.append(native)
        if override:
            fail_commit["armed"] = True
        try:
            yield native
        finally:
            native.closed = True

    monkeypatch.setattr(holder, "_execute_write", fail_once)
    monkeypatch.setattr(server, "_linked_codex_resume", resume_then_fail_commit)
    try:
        failed = await owner.dispatch(request)
        assert failed.get("error", {}).get("data", {}).get("reason") == "storage_unavailable", failed
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        pending = db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd")
        assert pending["request_id"] == "unopened-workspace-recovery"
        assert not db.bindings.binding_marks(ref.session_id).policy

        recovered = await owner.dispatch(request)
        assert recovered.get("result", {}).get("cwd") == str(target), recovered
        assert session_row(db, ref.session_id)["cwd"] == str(target)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert [(call.override, call.accept_native, call.closed) for call in native_calls] == [
            (True, False, True), (False, True, True)]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_linked_workspace_keeps_ambiguous_native_path_pending_until_exact_recovery(
    tmp_path, monkeypatch,
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from tests.gateway.test_session_policy_verbs import _mutation
    from tui_gateway import server

    store, db, _authority, owner, ref, old, target, native_calls = _unopened_linked_owner(tmp_path, monkeypatch)
    external = tmp_path / "project-external"
    external.mkdir()
    request = _mutation(db, ref, "unopened-workspace-ambiguous", "workspace", {"cwd": str(target)})
    holder = raw_holder(db)
    original_execute = holder._execute_write
    fail_commit = {"armed": False, "once": True}
    observed = [str(target), str(external), str(old), str(target)]

    def fail_once(callback, **kwargs):
        if fail_commit["armed"]:
            fail_commit["armed"] = False
            raise OSError("synthetic receipt commit failure")
        return original_execute(callback, **kwargs)

    @contextmanager
    def resume_with_observed_cwd(thread_id, cwd, profile_home, *, override, accept_native=False):
        native = SimpleNamespace(thread_id=thread_id, cwd=cwd, profile_home=profile_home,
                                 override=override, accept_native=accept_native,
                                 effective_cwd=observed.pop(0), closed=False)
        native_calls.append(native)
        if override and fail_commit["once"]:
            fail_commit["once"] = False
            fail_commit["armed"] = True
        try:
            yield native
        finally:
            native.closed = True

    monkeypatch.setattr(holder, "_execute_write", fail_once)
    monkeypatch.setattr(server, "_linked_codex_resume", resume_with_observed_cwd)
    try:
        failed = await owner.dispatch(request)
        assert failed.get("error", {}).get("data", {}).get("reason") == "storage_unavailable", failed
        pending = db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd")
        assert session_row(db, ref.session_id)["cwd"] == str(old)

        ambiguous = await owner.dispatch(request)
        assert ambiguous.get("error", {}).get("data", {}).get("reason") == "revision_conflict", ambiguous
        assert "Restore its original or requested folder" in ambiguous["error"]["data"]["detail"]
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") == pending

        different = await owner.dispatch(_mutation(db, ref, "unopened-workspace-other", "workspace",
                                                   {"cwd": str(external)}))
        assert different.get("error", {}).get("data", {}).get("reason") == "revision_conflict", different
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") == pending
        assert len(native_calls) == 2  # a second request cannot inspect or replace the held intent

        # The operator restores the native thread to the recorded original folder, then retries the
        # exact pending mutation. Recovery clears the old intent, reapplies its target, and commits.
        recovered = await owner.dispatch(request)
        assert recovered.get("result", {}).get("cwd") == str(target), recovered
        assert session_row(db, ref.session_id)["cwd"] == str(target)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert [(call.override, call.accept_native, call.closed) for call in native_calls] == [
            (True, False, True), (False, True, True), (False, True, True), (True, False, True)]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_linked_workspace_refuses_stale_revision_before_native_resume(tmp_path, monkeypatch):
    from tests.gateway.test_session_policy_verbs import _mutation

    store, db, _authority, owner, ref, _old, _target, native_calls = _unopened_linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "unopened-workspace-stale", "workspace", {"cwd": str(_target)})
    raw_holder(db)._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET runtime_revision=runtime_revision+1 WHERE id=?", (ref.session_id,)))
    try:
        response = await owner.dispatch(request)
        assert response.get("error", {}).get("data", {}).get("reason") == "revision_conflict", response
        assert session_row(db, ref.session_id)["cwd"] == str(_old)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert native_calls == []
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_linked_workspace_native_writer_refusal_clears_its_intent(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent.transports.codex_app_server_session import CodexThreadResumeError
    from tests.gateway.test_session_policy_verbs import _mutation
    from tui_gateway import server

    store, db, _authority, owner, ref, old, target, native_calls = _unopened_linked_owner(tmp_path, monkeypatch)

    @contextmanager
    def active_writer(*_args, **_kwargs):
        native_calls.append("active-writer")
        raise CodexThreadResumeError(UNOPENED_THREAD_ID, "already has an active writer")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "_linked_codex_resume", active_writer)
    try:
        response = await owner.dispatch(_mutation(db, ref, "unopened-workspace-busy", "workspace",
                                                  {"cwd": str(target)}))
        assert response.get("error", {}).get("data", {}).get("reason") == "session_busy", response
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert native_calls == ["active-writer"]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_unopened_workspace_intent_blocks_legacy_adoption_in_the_same_write_transaction(tmp_path, monkeypatch):
    from persistence.sqlite.authority._local_migration import bind_legacy_target
    from persistence.core.errors import AuthorityRefusal

    store, db, _authority, owner, ref, old, target, _native_calls = _unopened_linked_owner(tmp_path, monkeypatch)
    intent = {"thread_id": UNOPENED_THREAD_ID, "from": str(old), "to": str(target),
              "request_id": "adoption-race", "principal_id": owner.actor.subject,
              "expected_revision": session_row(db, ref.session_id)["runtime_revision"],
              "expected_generation": session_row(db, ref.session_id)["runtime_generation"]}
    assert db.sessions.claim_linked_codex_workspace_change(ref.session_id, intent)
    receipt = {"legacy_session_id": ref.session_id, "session_id": ref.session_id,
               "principal_id": owner.actor.subject}
    try:
        with pytest.raises(AuthorityRefusal) as exc_info:
            raw_holder(db)._execute_write(lambda conn: bind_legacy_target(raw_holder(db), conn, receipt))
        assert getattr(exc_info.value, "reason", None) == "session_busy", exc_info.value
        assert session_row(db, ref.session_id)["session_key"] is None
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") == intent
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_cold_exact_workspace_receipt_replay_does_not_resume_again(tmp_path, monkeypatch):
    store, db, authority, owner, ref, _old, target = _linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "workspace-cold-retry", "workspace", {"cwd": str(target)})
    try:
        first = await owner.dispatch(request)
        assert "result" in first, first
        revision = session_row(db, ref.session_id)["runtime_revision"]
        authority.sessions.pop(ref.session_id)

        retry = await owner.dispatch(request)

        assert retry == first
        assert session_row(db, ref.session_id)["runtime_revision"] == revision
        assert len(FakeCodexSession.instances) == 1
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_native_resume_commit_failure_recovers_on_exact_retry_without_override(tmp_path, monkeypatch):
    from gateway.session_policy import restore_policy
    from persistence.core.errors import AuthorityRefusal

    store, db, _authority, owner, ref, old, target = _linked_owner(tmp_path, monkeypatch)
    request = _mutation(db, ref, "workspace-recover", "workspace", {"cwd": str(target)})
    original_commit = _authority.persistence.runtime.mutate
    attempts = 0

    def fail_first_canonical_commit(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AuthorityRefusal("storage_unavailable")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(_authority.persistence.runtime, "mutate", fail_first_canonical_commit)
    try:
        failed = await owner.dispatch(request)
        assert failed.get("error", {}).get("data", {}).get("reason") == "storage_unavailable", failed
        intent = db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd")
        assert intent == {
            "thread_id": THREAD_ID,
            "from": str(old),
            "to": str(target),
            "request_id": "workspace-recover",
            "principal_id": owner.actor.subject,
        }
        assert session_row(db, ref.session_id)["cwd"] == str(old)
        assert db.bindings.receipt(ref.session_id).policy["cwd"] == str(old)

        recovered = await owner.dispatch(request)
        assert "result" in recovered, recovered
        assert recovered["result"]["cwd"] == str(target)
        assert session_row(db, ref.session_id)["cwd"] == str(target)
        policy = restore_policy(db.bindings.receipt(ref.session_id).policy)
        assert policy.cwd == str(target)
        assert db.sessions.get_model_config_value(ref.session_id, "codex_pending_cwd") is None
        assert db.runtime.has_mutation_receipt(owner.actor.subject, ref.session_id, "workspace-recover")
        assert len(FakeCodexSession.instances) == 2
        first, retry = FakeCodexSession.instances
        assert first.kwargs["override"] is True
        assert retry.kwargs["override"] is False
        assert retry.kwargs["accept_native"] is True
        assert first.kwargs["thread_id"] == retry.kwargs["thread_id"] == THREAD_ID
    finally:
        await owner.close()
        store.close_all_db_handles()
