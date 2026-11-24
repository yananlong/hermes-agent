"""Tests for the optional codex app-server runtime gate.

These are unit tests for the api_mode rewriter and the wire-level transport
module. They do NOT require the `codex` CLI to be installed — that's
covered by a separate live test gated on `codex --version`.
"""

from __future__ import annotations

import sys
import threading

import pytest

from hermes_cli.runtime_provider import (
    _VALID_API_MODES,
    _maybe_apply_codex_app_server_runtime,
)


class TestApiModeRegistration:
    """The new api_mode must be registered or downstream parsing rejects it."""

    def test_codex_app_server_is_a_valid_api_mode(self) -> None:
        assert "codex_app_server" in _VALID_API_MODES



class TestMaybeApplyCodexAppServerRuntime:
    """The opt-in helper that rewrites api_mode → codex_app_server."""

    @pytest.mark.parametrize(
        "model_cfg",
        [
            None,
            {},
            {"openai_runtime": ""},
            {"openai_runtime": "auto"},
            {"openai_runtime": "AUTO"},
            {"other_key": "codex_app_server"},  # wrong key
        ],
    )
    def test_default_off_for_openai(self, model_cfg) -> None:
        """Default behavior is preserved when the flag is unset/auto."""
        got = _maybe_apply_codex_app_server_runtime(
            provider="openai", api_mode="chat_completions", model_cfg=model_cfg
        )
        assert got == "chat_completions"

    def test_opt_in_rewrites_openai(self) -> None:
        got = _maybe_apply_codex_app_server_runtime(
            provider="openai",
            api_mode="chat_completions",
            model_cfg={"openai_runtime": "codex_app_server"},
        )
        assert got == "codex_app_server"



    @pytest.mark.parametrize(
        "provider",
        [
            "anthropic",
            "openrouter",
            "xai",
            "qwen-oauth",
            "opencode-zen",
            "bedrock",
            "",
        ],
    )
    def test_other_providers_never_rerouted(self, provider) -> None:
        """Non-OpenAI providers MUST NOT be rerouted even with the flag set —
        codex's app-server can only run OpenAI/Codex auth flows."""
        got = _maybe_apply_codex_app_server_runtime(
            provider=provider,
            api_mode="anthropic_messages",
            model_cfg={"openai_runtime": "codex_app_server"},
        )
        assert got == "anthropic_messages", (
            f"provider={provider!r} should not be rerouted to codex_app_server"
        )


class TestCodexAppServerModule:
    """Module-surface tests for the JSON-RPC speaker. Don't require codex CLI."""




    def test_check_binary_handles_missing_executable(self) -> None:
        from agent.transports.codex_app_server import check_codex_binary

        ok, msg = check_codex_binary(codex_bin="/nonexistent/codex/binary/path")
        assert ok is False
        assert msg



class TestCodexAppServerClose:
    """Lifecycle tests for retiring the optional Codex app-server transport."""

    @pytest.mark.live_system_guard_bypass
    @pytest.mark.platforms("posix")  # start_new_session/setsid is POSIX-only
    def test_close_reaps_independent_descendant_process_group(self, tmp_path):
        """A Codex-owned MCP child that calls setsid must not survive close().

        Killing only the app-server root lets independently grouped stdio MCP
        descendants live past client retirement. The fake codex binary below
        spawns a long-lived child in its own session, records its PID, and exits
        promptly on root SIGTERM; close() must still reap the child.
        """
        import os
        import stat
        import time

        import psutil

        from agent.transports.codex_app_server import CodexAppServerClient

        child_pid_file = tmp_path / "child.pid"
        fake_codex = tmp_path / "fake_codex.py"
        fake_codex.write_text(
            f"#!{sys.executable}\n"
            """
import os
import signal
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    start_new_session=True,
)
with open(os.environ["CHILD_PID_FILE"], "w", encoding="utf-8") as fh:
    fh.write(str(child.pid))

def _exit(_sig, _frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, _exit)
while True:
    time.sleep(1)
""".lstrip()
        )
        fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)

        client = CodexAppServerClient(
            codex_bin=str(fake_codex),
            env={"CHILD_PID_FILE": str(child_pid_file)},
        )
        try:
            deadline = time.time() + 5
            while not child_pid_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            assert child_pid_file.exists(), "fake codex did not report child PID"
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            assert psutil.pid_exists(child_pid)

            client.close(timeout=1.0)

            deadline = time.time() + 5
            while psutil.pid_exists(child_pid) and time.time() < deadline:
                time.sleep(0.05)
            assert not psutil.pid_exists(child_pid), (
                "Codex app-server close() left an independently grouped "
                f"descendant alive: pid={child_pid}"
            )
        finally:
            client.close(timeout=0.1)
            if child_pid_file.exists():
                try:
                    os.kill(int(child_pid_file.read_text(encoding="utf-8")), 9)
                except ProcessLookupError:
                    pass

    def test_close_escalates_to_tree_kill_when_root_ignores_sigterm(self, monkeypatch):
        """When the root outlives the graceful wait, close() must kill the tree AND
        run the post-kill wait so a codex ignoring SIGTERM cannot leak."""
        import subprocess
        from unittest import mock

        from agent.transports import codex_app_server as mod

        proc = mock.MagicMock()
        proc.pid = 4242
        proc.stdin = None
        proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="codex", timeout=0.01), 0]
        killed: list[int] = []
        monkeypatch.setattr(mod, "kill_process_tree", lambda pid, **kw: killed.append(pid) or True)
        monkeypatch.setattr(mod, "_snapshot_descendants", lambda pid: [])

        client = mod.CodexAppServerClient.__new__(mod.CodexAppServerClient)
        client._proc = proc
        client._closed = False
        client._pending, client._pending_lock = {}, threading.Lock()
        client.close(timeout=0.01)

        assert killed == [4242]
        assert proc.wait.call_args_list == [mock.call(timeout=0.01), mock.call(timeout=1.0)]


class TestSpawnEnvIsolation:
    """The codex spawn must NOT rewrite HOME — codex's shell tool spawns
    subprocesses (gh, git, npm, aws, gcloud, ...) that need to find their
    config in the real user $HOME. CODEX_HOME isolates codex's own state,
    HOME stays unchanged.

    OpenClaw hit this footgun (openclaw/openclaw#81562) — they were
    rewriting HOME to a synthetic per-agent dir alongside CODEX_HOME,
    and then `gh auth status` / git config / etc. all broke inside codex
    shell calls. We avoid the same bug by only overlaying CODEX_HOME and
    RUST_LOG on top of os.environ.copy().
    """


    def test_spawn_env_sets_CODEX_HOME_when_provided(self, monkeypatch):
        """CODEX_HOME isolation must still work — that's the whole point
        of the codex_home arg."""
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")

        client = cas.CodexAppServerClient(
            codex_bin="codex", codex_home="/tmp/profile/codex"
        )
        client._closed = True

        assert captured["env"].get("CODEX_HOME") == "/tmp/profile/codex"
        # And HOME still passes through unchanged
        assert captured["env"].get("HOME") == "/users/alice"

    def test_kanban_worker_adds_only_kanban_writable_root(self, monkeypatch):
        """Codex-runtime Kanban workers need to write board state outside
        their scratch/worktree workspace, but should not fall back to
        danger-full-access. Hermes passes a narrow app-server config override
        for the Kanban root only.
        """
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setenv("HOME", "/users/alice")
        monkeypatch.setenv("HERMES_HOME", "/users/alice/.hermes/profiles/backend-worker")
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_smoke")
        monkeypatch.setenv(
            "HERMES_KANBAN_DB",
            "/users/alice/.hermes/kanban/boards/smoke/kanban.db",
        )

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True

        cmd = captured["cmd"]
        assert cmd[:2] == ["codex", "app-server"]
        assert 'sandbox_mode="workspace-write"' in cmd
        assert (
            'sandbox_workspace_write.writable_roots=["/users/alice/.hermes/kanban/boards/smoke"]'
            in cmd
        )
        assert "sandbox_workspace_write.network_access=false" in cmd
        assert all("danger" not in part for part in cmd)


class TestSpawnEnvSecretStripping:
    """codex app-server routes its spawn env through hermes_subprocess_env(
    inherit_credentials=True) instead of a raw os.environ.copy().

    codex is a model-driving CLI executor: it legitimately needs LLM provider
    credentials to authenticate, but it must NOT inherit Tier-1 Hermes secrets
    (gateway bot tokens, GitHub/infra auth, dashboard session token) or the
    dynamic-internal secrets (AUXILIARY_*_API_KEY / _BASE_URL side-LLM keys,
    GATEWAY_RELAY_* relay-auth) — a coding subprocess has no use for those and
    a model-controlled action could exfiltrate them. This closes the #29157
    sibling spawn-site gap (copilot_acp_client already routes through the
    helper; codex app-server predated it).
    """

    @staticmethod
    def _capture_spawn_env(monkeypatch):
        import subprocess
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = None
                self.stdout = None
                self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True
        return captured["env"]

    def test_tier1_and_internal_secrets_stripped_from_spawn_env(self, monkeypatch):
        for var, val in {
            "GH_TOKEN": "ghp-secret",
            "TELEGRAM_BOT_TOKEN": "bot-secret",
            "MODAL_TOKEN_SECRET": "modal-secret",
            "HERMES_DASHBOARD_SESSION_TOKEN": "dash-secret",
            "AUXILIARY_VISION_API_KEY": "aux-secret",
            "GATEWAY_RELAY_SECRET": "relay-secret",
            "GATEWAY_RELAY_ID": "relay-id",
            "GATEWAY_RELAY_DELIVERY_KEY": "relay-delivery",
        }.items():
            monkeypatch.setenv(var, val)

        env = self._capture_spawn_env(monkeypatch)
        for var in (
            "GH_TOKEN", "TELEGRAM_BOT_TOKEN", "MODAL_TOKEN_SECRET",
            "HERMES_DASHBOARD_SESSION_TOKEN", "AUXILIARY_VISION_API_KEY",
            "GATEWAY_RELAY_SECRET", "GATEWAY_RELAY_ID", "GATEWAY_RELAY_DELIVERY_KEY",
        ):
            assert var not in env, f"{var} leaked into codex app-server spawn env"

    def test_provider_credentials_still_reach_codex(self, monkeypatch):
        """codex authenticates against the model endpoint — provider keys must
        still flow through (inherit_credentials=True)."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-codex-needs-this")
        env = self._capture_spawn_env(monkeypatch)
        assert env.get("OPENAI_API_KEY") == "sk-codex-needs-this"

    def test_routed_profile_uses_its_credentials_and_codex_home(self, monkeypatch, tmp_path):
        """A routed profile's Codex child must not inherit launch-profile secrets/settings."""
        import os
        import subprocess
        import threading
        from agent.transports import codex_app_server as cas
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = self.stdout = self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        launch_home = tmp_path / "launch-profile"
        target_home = tmp_path / "target-profile"
        os_home = tmp_path / "os-home"
        launch_home.mkdir()
        target_home.mkdir()
        os_home.mkdir()
        (launch_home / ".env").write_text(
            "OPENAI_API_KEY=launch-dotenv-key\nGH_TOKEN=launch-github-token\n"
        )
        (target_home / ".env").write_text(
            f"OPENAI_API_KEY=target-profile-key\nCODEX_HOME={target_home / 'codex'}\n"
        )

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
        monkeypatch.setenv("HOME", str(os_home))
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        # Simulate a supervisor-injected launch value that is not named by the launch .env file.
        monkeypatch.setenv("CODEX_HOME", str(launch_home / "supervisor-codex"))
        monkeypatch.setenv("OPENAI_API_KEY", "launch-supervisor-key")
        monkeypatch.setenv("GH_TOKEN", "launch-supervisor-token")

        home_token = set_hermes_home_override(str(target_home))
        try:
            client = cas.CodexAppServerClient(codex_bin="codex")
            client._closed = True
        finally:
            reset_hermes_home_override(home_token)

        env = captured["env"]
        assert env["OPENAI_API_KEY"] == "target-profile-key"
        assert "GH_TOKEN" not in env
        assert env["CODEX_HOME"] == str(target_home / "codex")
        assert env["HOME"] == str(os_home)

    def test_routed_profile_without_codex_home_fails_closed(self, monkeypatch, tmp_path):
        """A routed secondary cannot read the launch user's shared Codex history."""
        import os
        import subprocess
        from agent.transports import codex_app_server as cas
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        launch_home = tmp_path / "launch-profile"
        target_home = tmp_path / "target-profile"
        os_home = tmp_path / "os-home"
        launch_home.mkdir()
        target_home.mkdir()
        os_home.mkdir()
        (launch_home / ".env").write_text("CODEX_HOME=launch-dotenv-codex\n")
        (target_home / ".env").write_text("OPENAI_API_KEY=target-profile-key\n")

        def unexpected_spawn(*_args, **_kwargs):
            pytest.fail("Codex app-server must not spawn without a routed profile CODEX_HOME")

        monkeypatch.setattr(subprocess, "Popen", unexpected_spawn)
        monkeypatch.setenv("HOME", str(os_home))
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        monkeypatch.setenv("CODEX_HOME", str(launch_home / "supervisor-codex"))
        monkeypatch.setenv("OPENAI_API_KEY", "launch-supervisor-key")

        home_token = set_hermes_home_override(str(target_home))
        try:
            with pytest.raises(ValueError, match="configure its own CODEX_HOME"):
                cas.CodexAppServerClient(codex_bin="codex")
        finally:
            reset_hermes_home_override(home_token)

    def test_launch_profile_without_codex_home_keeps_shared_default(self, monkeypatch, tmp_path):
        """Only the profile that launched Hermes retains the legacy shared home by default."""
        import os
        import subprocess
        import threading
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = self.stdout = self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        launch_home = tmp_path / "launch-profile"
        os_home = tmp_path / "os-home"
        launch_home.mkdir()
        os_home.mkdir()
        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
        monkeypatch.setenv("HOME", str(os_home))
        monkeypatch.setenv("HERMES_HOME", str(launch_home))
        monkeypatch.delenv("CODEX_HOME", raising=False)

        client = cas.CodexAppServerClient(codex_bin="codex")
        client._closed = True

        assert captured["env"]["CODEX_HOME"] == str(os_home / ".codex")
        assert captured["env"]["HOME"] == str(os_home)

    def test_read_only_client_uses_explicit_codex_home_and_strips_all_credentials(self, monkeypatch, tmp_path):
        import subprocess
        import threading
        from agent.transports import codex_app_server as cas

        captured = {}

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                captured["cmd"] = list(cmd)
                captured["env"] = kwargs.get("env", {}).copy()
                self.stdin = self.stdout = self.stderr = None
                self.pid = 1
                self.returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(threading.Thread, "start", lambda _self: None)
        client = cas.CodexAppServerClient(
            codex_bin="codex",
            codex_home=str(tmp_path / "profile-codex"),
            base_env={
                "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path / "profile"),
                "CODEX_HOME": str(tmp_path / "launch-codex"),
                "OPENAI_API_KEY": "provider-secret", "GH_TOKEN": "github-secret",
                "HERMES_KANBAN_TASK": "task-secret", "HERMES_KANBAN_DB": str(tmp_path / "board.db"),
            },
            inherit_credentials=False, read_only=True,
        )
        client._closed = True

        env = captured["env"]
        assert env["CODEX_HOME"] == str(tmp_path / "profile-codex")
        assert env["HERMES_HOME"] == str(tmp_path / "profile")
        assert not {"OPENAI_API_KEY", "GH_TOKEN", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB"} & env.keys()
        assert captured["cmd"] == ["codex", "app-server"]

    def test_read_only_client_rejects_credential_overlay(self):
        from agent.transports.codex_app_server import CodexAppServerClient

        with pytest.raises(ValueError, match="cannot inherit or overlay credentials"):
            CodexAppServerClient(read_only=True, inherit_credentials=False, env={"OPENAI_API_KEY": "secret"})

