"""Same-thread linked Codex workspace changes are fenced and recoverable."""

from contextlib import nullcontext
from pathlib import Path
import ntpath
import posixpath
import threading
from types import SimpleNamespace

import pytest

from agent.transports.codex_app_server_session import CodexAppServerSession as RealCodexAppServerSession
from agent.transports.codex_app_server_session import CodexThreadResumeError
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session
from tui_gateway import server

SID = "linked-workspace"
THREAD_ID = "01a1c4a4-7a29-7011-8493-04445af16d6e"


def test_linked_workspace_absolute_path_validation_uses_host_path_dialect():
    windows_path = r"C:\work\project"

    assert server._is_absolute_host_path(windows_path, ntpath)
    assert not server._is_absolute_host_path(windows_path, posixpath)
    assert server._is_absolute_host_path("/srv/work", posixpath)


class FakeNativeSession:
    plans = []
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.plan = self.plans.pop(0) if self.plans else {}
        self._client = self.plan.get("client")
        self.effective_cwd = None
        self.effective_model = None
        self.closed = False
        self.instances.append(self)

    def ensure_started(self):
        if entered := self.plan.get("entered"):
            entered.set()
        if release := self.plan.get("release"):
            assert release.wait(5), "test did not release fake native resume"
        if error := self.plan.get("error"):
            raise error
        self.effective_cwd = self.plan.get("cwd", self.kwargs["cwd"])
        self.effective_model = "gpt-6.1-sol"
        return self.kwargs["resume_thread_id"]

    def close(self):
        self.closed = True


@pytest.fixture
def linked_workspace(tmp_path, monkeypatch):
    import agent.transports.codex_app_server_session as transport
    import tools.terminal_tool_lifecycle as terminal_lifecycle

    old, target, external = (tmp_path / name for name in ("old", "target", "external"))
    for path in (old, target, external):
        path.mkdir()
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, SID, "desktop", cwd=str(old), model_config={
        "api_mode": "codex_app_server", "codex_linked_thread": True, "codex_thread_id": THREAD_ID,
    })
    FakeNativeSession.plans = []
    FakeNativeSession.instances = []
    monkeypatch.setattr(transport, "CodexAppServerSession", FakeNativeSession)
    monkeypatch.setattr(server, "_session_profile_runtime_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(server, "_cwd_is_remote", lambda _profile: False)
    monkeypatch.setattr(server, "_workspace_cwd", lambda _profile, cwd: str(Path(cwd).resolve()))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: setattr(
        session.get("agent"), "session_cwd", session.get("cwd")))
    monkeypatch.setattr(terminal_lifecycle, "cleanup_vm", lambda *_args: None)
    monkeypatch.setattr(server.git_probe, "branch", lambda cwd: f"branch-{Path(cwd).name}")
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: None)
    yield SimpleNamespace(db=db, external=external, old=old, target=target, tmp_path=tmp_path)
    db.close()


def test_linked_workspace_pick_resumes_same_thread_and_commits_live_and_durable_cwd(linked_workspace):
    env = linked_workspace
    agent = SimpleNamespace(session_cwd=str(env.old))
    live = {"session_key": SID, "cwd": str(env.old), "agent": agent}

    result = server._move_linked_codex_workspace(env.db, SID, str(env.target), live_session=live)

    row = env.db.sessions.get(SID)
    adapter = FakeNativeSession.instances[0]
    assert result["cwd"] == str(env.target)
    assert row.cwd == str(env.target)
    assert env.db.sessions.get_model_config_value(SID, "codex_thread_id") == THREAD_ID
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None
    assert live["cwd"] == agent.session_cwd == str(env.target)
    assert adapter.kwargs["resume_thread_id"] == THREAD_ID
    assert adapter.kwargs["preserve_thread_settings"] is True
    assert adapter.kwargs["resume_cwd_override"] is True
    assert adapter.kwargs["allow_native_cwd_mismatch"] is False
    assert "model" not in adapter.kwargs and "model_provider" not in adapter.kwargs
    assert adapter.closed


def test_workspace_move_rpc_routes_linked_row_through_native_same_thread_change(linked_workspace, monkeypatch):
    env = linked_workspace
    monkeypatch.setattr(server, "_sessions", {})
    monkeypatch.setattr(server, "_profile_home", lambda _profile: None)
    monkeypatch.setattr(server, "_profile_db", lambda _params, writer=False: nullcontext(env.db))

    response = server._methods["session.workspace.move"](
        "move-linked",
        {"session_key": SID, "cwd": str(env.target)},
    )

    assert "result" in response, response
    assert response["result"]["cwd"] == str(env.target)
    assert env.db.sessions.get(SID).cwd == str(env.target)
    adapter = FakeNativeSession.instances[0]
    assert adapter.kwargs["resume_thread_id"] == THREAD_ID
    assert adapter.kwargs["resume_cwd_override"] is True
    assert adapter.closed


def test_missing_codex_binary_clears_only_its_unapplied_workspace_intent(linked_workspace, monkeypatch):
    import agent.transports.codex_app_server as codex_client
    import agent.transports.codex_app_server_session as codex_session
    from hermes_cli import config
    import hermes_cli.codex_runtime_switch as runtime_switch

    env = linked_workspace
    empty_path = env.tmp_path / "empty-bin"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.setattr(codex_session, "CodexAppServerSession", RealCodexAppServerSession)
    monkeypatch.setattr(config, "load_config", dict)
    monkeypatch.setattr(runtime_switch, "get_configured_codex_binary", lambda _config: "codex")

    popen_calls = []
    created_children = []
    real_popen = codex_client.subprocess.Popen

    def record_popen(*args, **kwargs):
        popen_calls.append(args[0])
        process = real_popen(*args, **kwargs)
        created_children.append(process)
        return process

    monkeypatch.setattr(codex_client.subprocess, "Popen", record_popen)

    with pytest.raises(server.LinkedCodexWorkspaceError, match="No native child was started"):
        server._move_linked_codex_workspace(env.db, SID, str(env.target))

    assert popen_calls and popen_calls[0][0] == "codex"
    assert created_children == []
    assert env.db.sessions.get(SID).cwd == str(env.old)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None


def test_uncertain_codex_startup_failure_keeps_workspace_intent(linked_workspace):
    env = linked_workspace
    FakeNativeSession.plans = [{
        "client": object(),
        "error": FileNotFoundError(2, "simulated post-spawn file failure", "codex"),
    }]

    with pytest.raises(server.LinkedCodexWorkspaceError, match="startup state is uncertain"):
        server._move_linked_codex_workspace(env.db, SID, str(env.target))

    intent = env.db.sessions.get_model_config_value(SID, "codex_pending_cwd")
    assert intent == {"thread_id": THREAD_ID, "from": str(env.old), "to": str(env.target)}
    assert env.db.sessions.get(SID).cwd == str(env.old)
    assert FakeNativeSession.instances[0].closed


def test_native_active_writer_refusal_keeps_row_and_live_session_unchanged(linked_workspace):
    env = linked_workspace
    FakeNativeSession.plans = [{"error": CodexThreadResumeError(THREAD_ID, "already has an active writer")}]
    agent = SimpleNamespace(session_cwd=str(env.old))
    live = {"session_key": SID, "cwd": str(env.old), "agent": agent}

    with pytest.raises(server.LinkedCodexWorkspaceBusy, match="active in another Codex client"):
        server._move_linked_codex_workspace(env.db, SID, str(env.target), live_session=live)

    assert env.db.sessions.get(SID).cwd == str(env.old)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None
    assert live["cwd"] == agent.session_cwd == str(env.old)
    assert FakeNativeSession.instances[0].closed


def test_sidecar_workspace_picker_does_not_commit_a_canonical_mutation_intent(linked_workspace):
    env = linked_workspace
    intent = {"thread_id": THREAD_ID, "from": str(env.old), "to": str(env.target),
              "request_id": "canonical-workspace", "principal_id": "fixture-owner"}
    assert env.db.sessions.claim_linked_codex_workspace_change(SID, intent)

    with pytest.raises(server.LinkedCodexWorkspaceBusy, match="canonical linked Codex workspace request"):
        server._move_linked_codex_workspace(env.db, SID, str(env.external))

    assert env.db.sessions.get(SID).cwd == str(env.old)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") == intent
    assert FakeNativeSession.instances == []


def test_second_picker_cannot_recover_or_replace_an_inflight_intent(linked_workspace):
    env = linked_workspace
    entered, release = threading.Event(), threading.Event()
    FakeNativeSession.plans = [{"entered": entered, "release": release}]
    outcomes = []

    def first_move():
        try:
            outcomes.append(server._move_linked_codex_workspace(env.db, SID, str(env.target)))
        except Exception as exc:  # surfaced in the main test thread below
            outcomes.append(exc)

    worker = threading.Thread(target=first_move)
    worker.start()
    try:
        assert entered.wait(5), "first native resume did not start"
        pending = env.db.sessions.get_model_config_value(SID, "codex_pending_cwd")
        assert pending == {"from": str(env.old), "thread_id": THREAD_ID, "to": str(env.target)}

        with pytest.raises(server.LinkedCodexWorkspaceBusy, match="Another Hermes workspace change"):
            server._move_linked_codex_workspace(env.db, SID, str(env.external))

        assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") == pending
        assert env.db.sessions.get(SID).cwd == str(env.old)
        assert len(FakeNativeSession.instances) == 1  # the second caller never probes/recoveries the native thread
    finally:
        release.set()
        worker.join(5)

    assert not worker.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], dict)
    assert outcomes[0]["cwd"] == str(env.target)
    assert env.db.sessions.get(SID).cwd == str(env.target)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None


def test_committed_intent_recovers_after_readback_failure_without_resending_override(linked_workspace, monkeypatch):
    env = linked_workspace
    original_get_session = env.db.sessions.get
    original_finish = env.db.sessions.finish_linked_codex_workspace_change
    committed = False

    def finish_then_arm_readback_failure(*args, **kwargs):
        nonlocal committed
        result = original_finish(*args, **kwargs)
        committed = True
        return result

    def fail_after_commit_readback(session_id):
        nonlocal committed
        if committed:
            committed = False
            raise OSError("simulated readback interruption")
        return original_get_session(session_id)

    monkeypatch.setattr(env.db.sessions, "finish_linked_codex_workspace_change", finish_then_arm_readback_failure)
    monkeypatch.setattr(env.db.sessions, "get", fail_after_commit_readback)
    with pytest.raises(server.LinkedCodexWorkspacePersistError, match="could not verify"):
        server._move_linked_codex_workspace(env.db, SID, str(env.target))

    marker = env.db.sessions.get_model_config_value(SID, "codex_pending_cwd")
    assert marker == {"from": str(env.old), "thread_id": THREAD_ID, "to": str(env.target),
                      "phase": "committed", "effective": str(env.target)}
    assert original_get_session(SID).cwd == str(env.target)
    assert FakeNativeSession.instances[0].kwargs["resume_cwd_override"] is True

    monkeypatch.setattr(env.db.sessions, "get", original_get_session)
    recovered = server._move_linked_codex_workspace(env.db, SID, str(env.target))

    assert recovered["cwd"] == str(env.target)
    assert env.db.sessions.get(SID).cwd == str(env.target)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None
    assert len(FakeNativeSession.instances) == 2
    assert FakeNativeSession.instances[1].kwargs["resume_cwd_override"] is False


def test_pending_recovery_preserves_a_native_workspace_change(linked_workspace):
    env = linked_workspace
    intent = {"from": str(env.old), "thread_id": THREAD_ID, "to": str(env.target)}
    assert env.db.sessions.claim_linked_codex_workspace_change(SID, intent)
    FakeNativeSession.plans = [{"cwd": str(env.external)}]

    with pytest.raises(server.LinkedCodexWorkspaceExternalChange, match="choose the desired folder again"):
        server._move_linked_codex_workspace(env.db, SID, str(env.target))

    assert env.db.sessions.get(SID).cwd == str(env.external)
    assert env.db.sessions.get_model_config_value(SID, "codex_pending_cwd") is None
    assert FakeNativeSession.instances[0].kwargs["resume_cwd_override"] is False
