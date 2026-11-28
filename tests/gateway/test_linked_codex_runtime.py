"""Linked Codex pointers resume on their native runtime through the gateway's saved policy."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from tests.fakes.providers.codex_app_server import FakeCodex
from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles
from tests.gateway.test_session_follow_profile_config import _owner


pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")
THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
SID = "linked-codex"


def _adopt_pointer(monkeypatch, home, *, flags=None, codex_bin=None):
    """Create a real profile-local pointer and adopt it into the gateway's authority."""
    from gateway import run
    from gateway.session_local_migration import resolve_local_target

    home.mkdir(parents=True, exist_ok=True)
    model = {"provider": "openai", "default": "gpt-6-luna"}
    if codex_bin is not None:
        model["codex_bin"] = str(codex_bin)
    (home / "config.yaml").write_text(json.dumps({"model": model}))
    monkeypatch.setattr(run, "_hermes_home", home)
    store, runner, authority, owner = _owner(home)
    runner._peek_session_state = lambda _key: None
    seed_session(authority.persistence, SID, "desktop", cwd=str(home), model="gpt-6-luna", model_config={
        "codex_linked_thread": True, "codex_thread_id": THREAD,
        "provider": "openai", "api_mode": "codex_app_server", **(flags or {}),
    })
    authority._native_legacy_transports = {owner.actor.transport_id: owner.actor}
    ref = resolve_local_target(authority, owner.actor, SID)
    return store, runner, authority, owner, ref


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("receipt_mode", ["codex_app_server", "codex_responses"])
@pytest.mark.parametrize("have_api_key", [True, False], ids=["api-key", "no-api-key"])
async def test_adopted_linked_codex_policy_runs_the_original_thread(
    monkeypatch, tmp_path, provider, receipt_mode, have_api_key,
):
    """A saved app-server binding must survive real gateway resolution and reach the native thread."""
    from gateway import run
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.session_local_migration import resolve_local_target
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(home), "turns": []}},
        "turn_cursor": 0,
    }))
    (home / "config.yaml").write_text(json.dumps({"model": {
        "provider": provider, "default": "gpt-6-luna", "codex_bin": str(fake.bin),
    }}))
    monkeypatch.setattr(run, "_hermes_home", home)
    if have_api_key:
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store, runner, authority, owner = _owner(home)
    runner._peek_session_state = lambda _key: None
    seed_session(authority.persistence, SID, "desktop", cwd=str(home), model="gpt-6-luna", model_config={
        "codex_linked_thread": True, "codex_thread_id": THREAD,
        "provider": provider, "api_mode": "codex_app_server",
    })
    authority._native_legacy_transports = {owner.actor.transport_id: owner.actor}
    try:
        ref = resolve_local_target(authority, owner.actor, SID)
        live = authority.sessions[ref.session_id]
        if receipt_mode != "codex_app_server":
            # A policy frozen by an earlier build may already contain the wrong HTTP route. Update
            # it through the owner's fenced policy write, then cold-restore that actual receipt.
            from gateway.session_local_recovery import restore_local_session
            saved = dict(authority.persistence.bindings.receipt(SID).policy)
            config = json.loads(saved["config_json"])
            config["model"]["api_mode"] = receipt_mode
            replacement = {**saved, "config_json": json.dumps(config)}
            generation = session_row(authority.persistence, SID)["runtime_generation"]
            authority.persistence.runtime.commit_refreshed_policy(
                authority.epoch, session_id=SID, old=saved, new=replacement, generation=generation)
            authority.sessions.pop(SID)
            ref = restore_local_session(authority, SID)
            live = authority.sessions[ref.session_id]
        # Ensure the test is using the already-adopted receipt shape from affected sessions.
        policy = authority.persistence.bindings.receipt(SID).policy
        model_config = json.loads(policy["config_json"])["model"]
        assert model_config["api_mode"] == receipt_mode
        assert "openai_runtime" not in model_config
        model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(runner, source=live.source)
        agent = AIAgent(model=model, **runtime, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        skip_background_review=True, persistence=authority.persistence,
                        session_id=SID, cwd=str(home))
        result = agent.run_conversation("hello", conversation_history=[])
        assert result["completed"], result
        assert result["final_response"] == "native answer"
        resumed = fake.requests("thread/resume")
        assert len(resumed) == 1
        assert resumed[0]["msg"]["params"]["threadId"] == THREAD
        assert not fake.requests("thread/start")
        sent = fake.requests("turn/start")
        assert len(sent) == 1 and sent[0]["msg"]["params"]["threadId"] == THREAD
        # Ask for a reasoning summary, or Codex records reasoning items with empty
        # summary/content and the transcript replays as if it never thought (#46527).
        # Asserted even though a linked turn sends no `effort`: `summary` asks Codex to RECORD
        # reasoning text, not to reason differently, so it must ride every turn/start.
        params = sent[0]["msg"]["params"]
        assert params.get("summary") == "auto", params
        assert len(fake.threads()[THREAD]["turns"]) == 1
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_managed_worker_uses_the_pointer_in_its_own_store(monkeypatch):
    """The independent worker reconstruction sees the same native binding without API credentials."""
    from gateway.session_policy import policy_for_source
    from gateway.session_worker_construct import worker_route
    from hermes_constants import get_hermes_home

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        policy = policy_for_source(runner, authority.sessions[ref.session_id].source)
        runtime = worker_route(policy, {"api_key": None, "session_db": authority.persistence, "session_id": SID})
        assert runtime["api_mode"] == "codex_app_server"
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("retarget", ["none", "after-resume", "after-check-same-model", "after-receipt"])
async def test_managed_worker_constructs_and_resumes_the_native_thread(monkeypatch, tmp_path, retarget):
    """The worker's real owner-RPC store and agent preserve the pointer's cwd and model."""
    from agent.managed_worker import _construct_agent
    from agent.runtime_session_store import RuntimeSessionStore
    from gateway.session_policy import policy_for_source, policy_scope
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent

    home = get_hermes_home()
    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "worker answer"}]}])
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(home), "turns": []}},
        "turn_cursor": 0,
    }))
    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, home, codex_bin=fake.bin)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    worker = agent = None
    try:
        if retarget == "after-check-same-model":
            # FakeCodex reports gpt-fake: make the durable model already match it so the worker
            # cannot skip verifying the pointer on a seemingly unnecessary model write.
            authority.persistence.sessions.update_model(SID, "gpt-fake")
        if retarget == "after-resume":
            from agent.transports.codex_app_server_session import CodexAppServerSession
            ensure_started = CodexAppServerSession.ensure_started

            def retarget_after_resume(native):
                native_id = ensure_started(native)
                authority.persistence.sessions.patch_model_config(SID, {
                    "codex_thread_id": "02a1c4a4-7a29-7011-8493-04445af16d6e"})
                return native_id

            monkeypatch.setattr(CodexAppServerSession, "ensure_started", retarget_after_resume)
        policy = policy_for_source(runner, authority.sessions[ref.session_id].source)
        generation = session_row(authority.persistence, SID)["runtime_generation"]
        scope = dict(epoch=authority.epoch, execution_id="linked-worker", session_id=SID, generation=generation)
        authority.persistence.worker_executions.register(**scope, kind="compute", adoption_secret="private")
        def owner_rpc(_method, **params):
            if retarget == "after-check-same-model" and params["operation"] == "session.linked_codex_model":
                # The worker already verified the old pointer; a retarget before its model write
                # lands must make the owner refuse that write, whatever the worker read before.
                authority.persistence.sessions.patch_model_config(SID, {
                    "codex_thread_id": "02a1c4a4-7a29-7011-8493-04445af16d6e"})
            result = authority.persistence.worker_executions.persist_operation(**params)
            if retarget == "after-receipt" and params["operation"] == "session.linked_codex_model":
                # The receipt was committed but its response was lost. A binding change before the
                # retry leaves that exact receipt replayable; the worker must still re-check it.
                authority.persistence.sessions.patch_model_config(SID, {
                    "codex_thread_id": "02a1c4a4-7a29-7011-8493-04445af16d6e"})
                assert authority.persistence.worker_executions.persist_operation(**params) == result
            return result

        worker = RuntimeSessionStore(owner_rpc, scope, tmp_path / "worker-outbox")
        # Same inputs the real managed worker gives its constructor: no cwd or API key.
        with policy_scope(policy, home=str(home)):
            agent = _construct_agent({"policy": {"kanban_json": None}}, AIAgent, policy,
                model=policy.model, provider=policy.provider, base_url=policy.base_url, api_key=None,
                session_db=worker, session_id=SID, quiet_mode=True, skip_context_files=True,
                skip_memory=True, skip_background_review=True)
            result = agent.run_conversation("hello from worker", conversation_history=[])
        if retarget != "none":
            assert not result["completed"], result
            assert len(fake.requests("thread/resume")) == 1
            assert not fake.requests("turn/start")
            assert not fake.requests("thread/start")
            expected_model = "gpt-fake" if retarget in ("after-check-same-model", "after-receipt") else "gpt-6-luna"
            assert authority.persistence.sessions.get(SID).model == expected_model
            return
        assert result["completed"], result
        assert result["final_response"] == "worker answer"
        assert len(fake.requests("thread/resume")) == 1
        assert fake.requests("thread/resume")[0]["msg"]["params"]["threadId"] == THREAD
        assert len(fake.requests("turn/start")) == 1
        assert not fake.requests("thread/start")
        assert authority.persistence.sessions.get(SID).model == "gpt-fake"
    finally:
        if agent is not None:
            agent._end_session_on_close = False
            agent.close()
        if worker is not None:
            worker.close()
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_an_ordinary_session_keeps_its_http_route(monkeypatch):
    """A profile containing one native pointer must not convert its other sessions to Codex."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.session_local_migration import resolve_local_target
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    home = get_hermes_home()
    store, runner, authority, owner, _ref = _adopt_pointer(monkeypatch, home)
    try:
        seed_session(authority.persistence, "ordinary", "desktop", cwd=str(home), model="gpt-6-luna", model_config={
            "provider": "openai", "api_mode": "codex_responses",
        })
        ref = resolve_local_target(authority, owner.actor, "ordinary")
        _model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=authority.sessions[ref.session_id].source)
        assert runtime["api_mode"] == "codex_responses"
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_a_linked_turn_resolves_the_native_model_in_the_ordinary_runtime_shape(monkeypatch):
    """The turn is labelled with the model Codex last reported for the thread, not the profile's
    frozen pick, and its runtime has the keys every other resolved route has."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.session_local_migration import resolve_local_target
    from hermes_constants import get_hermes_home

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    home = get_hermes_home()
    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, home)
    try:
        authority.persistence.sessions.update_model(SID, "gpt-native")
        model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=authority.sessions[ref.session_id].source)
        assert (model, runtime["api_mode"], runtime["api_key"]) == ("gpt-native", "codex_app_server", None)
        seed_session(authority.persistence, "ordinary", "desktop", cwd=str(home), model="gpt-6-luna",
                     model_config={"provider": "openai", "api_mode": "codex_responses"})
        ordinary = resolve_local_target(authority, owner.actor, "ordinary")
        _model, ordinary_runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=authority.sessions[ordinary.session_id].source)
        assert set(runtime) == set(ordinary_runtime)
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_one_turn_override_cannot_switch_a_linked_pointer_to_another_provider(monkeypatch):
    """A queued /model --once override cannot replace the native thread's route."""
    from gateway.session_cli_turn import cli_turn_scope, one_turn_route
    from gateway.config import Platform
    from gateway.run_turn_runner import _resolution_failure_reply
    from gateway.session_policy import policy_for_source
    from hermes_constants import get_hermes_home
    from persistence.core.errors import AuthorityRefusal

    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        source = authority.sessions[ref.session_id].source
        policy = policy_for_source(runner, source)
        turn = SimpleNamespace(_runner=runner, _ctx=SimpleNamespace(
            source=source, inbound_message_id="admission", user_config={}))
        with cli_turn_scope("admission", {"model_override": {"model": "other", "provider": "anthropic"}}):
            with pytest.raises(AuthorityRefusal) as exc:
                one_turn_route(turn, policy, "gpt-6-luna", {"api_mode": "codex_app_server"})
        assert exc.value.reason == "unsupported_operation"
        text, reason, retryable = _resolution_failure_reply(exc.value, Platform.LOCAL)
        assert reason == "unsupported_operation" and not retryable
        assert "model" in text and "Codex" in text
        assert "credential" not in text.lower() and "new session" not in text.lower()
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.parametrize("raw", ["{not valid json", "[]", "null"], ids=["invalid-json", "array", "null"])
def test_a_real_corrupt_row_is_not_mistaken_for_an_ordinary_session(tmp_path, raw):
    """SQLite's decoded row must preserve the corruption signal at the pointer guard."""
    from hermes_cli.foreign_linked_store import LinkedConfigUnreadable, linked_foreign_kind, row_model_config
    from persistence_assembly import open_persistence, profile_scope

    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, SID, "desktop", model_config={"codex_linked_thread": True, "codex_thread_id": THREAD})
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET model_config=? WHERE id=?", (raw, SID)))
        assert db.sessions.get(SID).model_config_json == raw
        with pytest.raises(LinkedConfigUnreadable):
            row_model_config(db, SID)
        assert linked_foreign_kind(db, SID) == "foreign"


@pytest.mark.asyncio
async def test_conflicting_pointer_flags_refuse_instead_of_guessing_an_owner(monkeypatch):
    """A row claiming two native owners must not run on either one's runtime."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from hermes_constants import get_hermes_home
    from persistence.core.errors import AuthorityRefusal

    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        authority.persistence.sessions.patch_model_config(SID, {"claude_linked_thread": True})
        with pytest.raises((AuthorityRefusal, ValueError)):
            GatewayTurnPrepareMixin._resolve_session_agent_runtime(runner, source=authority.sessions[ref.session_id].source)
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_id", [None, "", "not-a-thread-id"], ids=["missing", "empty", "invalid"])
async def test_a_pointer_with_no_valid_thread_id_cannot_run(monkeypatch, native_id):
    """A damaged pointer never falls back to a fresh Hermes or Codex thread."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from hermes_constants import get_hermes_home
    from persistence.core.errors import AuthorityRefusal

    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        authority.persistence.sessions.patch_model_config(SID, {"codex_thread_id": native_id})
        with pytest.raises(AuthorityRefusal) as exc:
            GatewayTurnPrepareMixin._resolve_session_agent_runtime(runner, source=authority.sessions[ref.session_id].source)
        assert exc.value.reason == "storage_unavailable"
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_an_unavailable_native_thread_never_falls_back_or_starts_a_new_thread(monkeypatch, tmp_path):
    """Even with a fallback configured, a failed resume cannot fork the linked conversation."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent

    fake = FakeCodex(tmp_path, [], forget_threads=True)
    home = get_hermes_home()
    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, home, codex_bin=fake.bin)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=authority.sessions[ref.session_id].source)
        agent = AIAgent(model=model, **runtime, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        skip_background_review=True, persistence=authority.persistence,
                        session_id=SID, cwd=str(home), fallback_model={
                            "provider": "custom", "model": "fallback",
                            "base_url": "http://127.0.0.1:1/v1", "api_key": "test-key"})
        result = agent.run_conversation("hello", conversation_history=[])
        assert not result["completed"], result
        assert len(fake.requests("thread/resume")) == 1
        assert not fake.requests("thread/start")
        assert not fake.requests("turn/start")
        assert authority.persistence.sessions.get_model_config_value(SID, "codex_thread_id") == THREAD
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_an_ordinary_session_with_unreadable_settings_is_refused_with_its_own_copy(monkeypatch):
    """A damaged ``model_config`` cannot be proven ordinary, so its turn is refused like a damaged
    pointer; the reply names the row's settings and /new, not a linked thread or credentials."""
    from gateway.config import Platform
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.run_turn_runner import _resolution_failure_reply
    from gateway.session_local_migration import resolve_local_target
    from hermes_constants import get_hermes_home
    from persistence.core.errors import AuthorityRefusal

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    store, runner, authority, owner, _ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        seed_session(authority.persistence, "ordinary", "desktop", cwd=str(get_hermes_home()), model="gpt-6-luna",
                     model_config={"provider": "openai", "api_mode": "codex_responses"})
        ref = resolve_local_target(authority, owner.actor, "ordinary")
        raw_holder(authority.persistence)._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET model_config=? WHERE id=?", ("{not json", "ordinary")))
        with pytest.raises(AuthorityRefusal) as exc:
            GatewayTurnPrepareMixin._resolve_session_agent_runtime(
                runner, source=authority.sessions[ref.session_id].source)
        text, reason, retryable = _resolution_failure_reply(exc.value, Platform.LOCAL)
        assert (reason, retryable) == ("storage_unavailable", False)
        assert "/new" in text and "settings" in text
        assert "linked" not in text.lower() and "credential" not in text.lower()
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.parametrize("damage", ["readonly-mode-without-flag", "non-bool-flag", "row-gone"])
def test_a_damaged_pointer_shape_never_resolves_as_an_ordinary_session(tmp_path, damage):
    """Each shape that cannot prove it is ordinary refuses instead of answering "not linked"."""
    from gateway.session_linked_codex import linked_codex_runtime
    from persistence.core.errors import AuthorityRefusal
    from persistence_assembly import open_persistence, profile_scope

    config = {"readonly-mode-without-flag": {"api_mode": "codex_linked_readonly", "codex_thread_id": THREAD},
              "non-bool-flag": {"codex_linked_thread": 1, "codex_thread_id": THREAD,
                                "api_mode": "codex_app_server"},
              "row-gone": None}[damage]
    with open_persistence(profile_scope(home=tmp_path)) as db:
        if config is not None:
            seed_session(db, SID, "desktop", model_config=config)
        with pytest.raises(AuthorityRefusal) as exc:
            linked_codex_runtime(db, SID)
        assert exc.value.reason == "storage_unavailable"


@pytest.mark.asyncio
async def test_a_pointer_retargeted_during_an_in_process_resume_keeps_its_model(monkeypatch, tmp_path):
    """The in-process agent re-checks the pointer before it writes Codex's model: a retarget
    between resume and that write refuses the turn and leaves the row's model untouched."""
    from agent.transports.codex_app_server_session import CodexAppServerSession
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from hermes_constants import get_hermes_home
    from run_agent import AIAgent

    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    home = get_hermes_home()
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(home), "turns": []}}, "turn_cursor": 0}))
    store, runner, authority, owner, ref = _adopt_pointer(monkeypatch, home, codex_bin=fake.bin)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ensure_started = CodexAppServerSession.ensure_started

    def retarget_after_resume(native):
        native_id = ensure_started(native)
        authority.persistence.sessions.patch_model_config(SID, {
            "codex_thread_id": "02a1c4a4-7a29-7011-8493-04445af16d6e"})
        return native_id

    monkeypatch.setattr(CodexAppServerSession, "ensure_started", retarget_after_resume)
    try:
        model, runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime(
            runner, source=authority.sessions[ref.session_id].source)
        agent = AIAgent(model=model, **runtime, quiet_mode=True, skip_context_files=True, skip_memory=True,
                        skip_background_review=True, persistence=authority.persistence,
                        session_id=SID, cwd=str(home))
        result = agent.run_conversation("hello", conversation_history=[])
        assert not result["completed"], result
        assert not fake.requests("turn/start") and not fake.requests("thread/start")
        assert authority.persistence.sessions.get(SID).model == "gpt-6-luna"
    finally:
        await owner.close()
        store.close_all_db_handles()


def test_a_session_with_unreadable_settings_still_builds_and_its_turn_is_refused(tmp_path, monkeypatch):
    """Agent construction is not where an unprovable row is judged: the agent (and the serve
    factory's route) build as ordinary, so the session stays resumable, and the turn's pointer
    guard refuses it before any model call or native thread."""
    from persistence_assembly import open_persistence, profile_scope
    from run_agent import AIAgent
    from tui_gateway.agent_factory import _linked_codex_agent_runtime

    with open_persistence(profile_scope(home=tmp_path / "home")) as db:
        seed_session(db, "damaged", "desktop", cwd=str(tmp_path), model="gpt-6-luna",
                     model_config={"provider": "openai-codex", "api_mode": "codex_app_server"})
        raw_holder(db)._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET model_config=? WHERE id=?", ("{not json", "damaged")))
        assert _linked_codex_agent_runtime(db, db, "damaged") is None
        agent = AIAgent(model="gpt-6-luna", provider="openai", api_mode="codex_app_server",
                        base_url="http://127.0.0.1:9/v1", api_key="test-key", quiet_mode=True, skip_context_files=True, skip_memory=True,
                        skip_background_review=True, persistence=db, session_id="damaged", cwd=str(tmp_path))
        result = agent.run_conversation("hello", conversation_history=[])
        assert not result["completed"], result
        assert result.get("linked_error_code") == "linked_foreign_config_unreadable", result
        assert getattr(agent, "_codex_session", None) is None


@pytest.mark.asyncio
async def test_a_turn_never_stamps_its_policy_model_onto_a_linked_pointer(monkeypatch):
    """After a failed resume the agent still carries the frozen policy's model; the post-turn sync
    must leave the pointer's column on the model Codex last reported."""
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from hermes_constants import get_hermes_home

    store, _runner, authority, owner, _ref = _adopt_pointer(monkeypatch, get_hermes_home())
    try:
        authority.persistence.sessions.update_model(SID, "gpt-native")
        agent = SimpleNamespace(model="gpt-6-luna", provider="openai", requested_provider="openai",
                                base_url=None, api_mode="codex_app_server", _fallback_activated=False)
        GatewayTurnPrepareMixin._sync_session_model_from_agent(
            SimpleNamespace(persistence=authority.persistence), SID, agent)
        assert authority.persistence.sessions.get(SID).model == "gpt-native"
    finally:
        await owner.close()
        store.close_all_db_handles()
