"""The Vibe resume transport: exact argv, typed refusals, hygiene, and entry parsing.

Everything here runs against a stub CLI generated on disk (a ``/bin/sh`` shim that execs this test
interpreter), so no real ``vibe`` binary, network or model call is involved. The transport's job is
narrow — build one argv, spawn it, bound it, kill it on timeout/cancel, and parse what it printed —
so each test pins one of those properties rather than the end-to-end turn (that lives in
``tests/hermes_cli/test_mistral_linked_resume.py``).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

from agent.transports import vibe_cli_resume as vibe

SESSION_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"


# --------------------------------------------------------------------------------------------------
# stub CLI
# --------------------------------------------------------------------------------------------------
_STUB_BODY = r'''
import json
import os
import sys
import time


def main():
    argv = sys.argv[1:]
    payload = {"argv": argv, "cwd": os.getcwd(),
               "vibe_home": os.environ.get("VIBE_HOME")}
    target = os.environ.get("FAKE_ARGV_FILE")
    if target:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    mode = os.environ.get("FAKE_MODE", "ok")
    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
        return 0
    if mode == "fail":
        sys.stderr.write(os.environ.get("FAKE_STDERR", "boom"))
        return int(os.environ.get("FAKE_EXIT", "1"))
    if mode == "denied":
        sys.stderr.write("Tool call denied: bash (no approval possible in a headless run)\n")
        print(json.dumps({"sessionId": os.environ.get("FAKE_SESSION", "s"),
                          "messages": [{"role": "assistant",
                                        "content": [{"type": "text", "text": "done"}]}]}))
        return 0
    if mode == "version":
        print(os.environ.get("FAKE_VERSION", "3.2.1"))
        return int(os.environ.get("FAKE_EXIT", "0"))
    print(json.dumps({"sessionId": os.environ.get("FAKE_SESSION", "s"),
                      "turnId": "turn-1",
                      "messages": [
                          {"role": "user",
                           "content": [{"type": "text", "text": argv[1] if len(argv) > 1 else ""}]},
                          {"role": "assistant",
                           "content": [{"type": "text", "text": "ok"}]}]}))
    return 0


sys.exit(main())
'''


def _stub_cli(tmp_path, name: str = "vibe") -> str:
    """Write an executable stub CLI and return its absolute path."""
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    script = binary_dir / f"{name}-stub.py"
    script.write_text(_STUB_BODY, encoding="utf-8")
    shim = binary_dir / name
    shim.write_text(
        '#!/bin/sh\n'
        'if [ -n "$FAKE_PID_FILE" ]; then printf "%s" "$$" > "$FAKE_PID_FILE"; fi\n'
        f'exec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not our process
        return True
    return True


def _wait_pid_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _wait_for_pid(path, timeout: float = 15.0):
    """The shim writes its pid before exec'ing the interpreter; poll it, then fail loudly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        time.sleep(0.02)
    pytest.fail("the stub CLI never recorded its pid")


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    return path


# --------------------------------------------------------------------------------------------------
# binary resolution
# --------------------------------------------------------------------------------------------------
def test_configured_binary_prefers_model_vibe_bin_and_defaults_to_vibe():
    assert vibe.configured_vibe_binary({"model": {"vibe_bin": " /opt/vibe/bin/vibe "}}) == "/opt/vibe/bin/vibe"
    assert vibe.configured_vibe_binary({"model": {"vibe_bin": ""}}) == "vibe"
    assert vibe.configured_vibe_binary({"model": {}}) == "vibe"
    assert vibe.configured_vibe_binary(None) == "vibe"


def test_resolve_binary_uses_the_configured_absolute_path(tmp_path):
    binary = _stub_cli(tmp_path)
    assert vibe.resolve_vibe_binary({"model": {"vibe_bin": binary}}) == binary


def test_resolve_binary_canonicalizes_a_path_lookup(tmp_path):
    resolved = vibe.resolve_vibe_binary({}, which=lambda name: sys.executable)
    assert os.path.isabs(resolved) and resolved == sys.executable
    relative = vibe.resolve_vibe_binary(
        {}, which=lambda name: os.path.relpath(sys.executable, os.getcwd()))
    assert os.path.isabs(relative) and os.path.exists(relative)
    plain = tmp_path / "vibe"
    plain.write_text("not a program", encoding="utf-8")
    plain.chmod(0o644)
    with pytest.raises(vibe.VibeBinaryMissing, match="not executable"):
        vibe.resolve_vibe_binary({}, which=lambda _name: str(plain))


def test_resolve_binary_degrades_to_a_typed_refusal():
    with pytest.raises(vibe.VibeBinaryMissing) as missing:
        vibe.resolve_vibe_binary({}, which=lambda _name: None)
    assert "PATH" in str(missing.value)
    with pytest.raises(vibe.VibeBinaryMissing, match="absolute path"):
        vibe.resolve_vibe_binary({"model": {"vibe_bin": "bin/vibe"}})
    with pytest.raises(vibe.VibeBinaryMissing, match="was not found"):
        vibe.resolve_vibe_binary({"model": {"vibe_bin": "/nonexistent/vibe"}})


def test_resolve_binary_rejects_a_non_executable_file(tmp_path):
    plain = tmp_path / "vibe"
    plain.write_text("not a program", encoding="utf-8")
    plain.chmod(0o644)
    with pytest.raises(vibe.VibeBinaryMissing, match="not executable"):
        vibe.resolve_vibe_binary({"model": {"vibe_bin": str(plain)}})


def test_check_vibe_binary_reports_a_version_or_a_refusal(tmp_path):
    ok, message = vibe.check_vibe_binary(_stub_cli(tmp_path),
                                        config={"model": {"vibe_bin": _stub_cli(tmp_path)}})
    # the stub only prints a version in "version" mode; default mode prints JSON, which is still a
    # successful probe (the exit code is what matters)
    assert ok is True and message
    ok, message = vibe.check_vibe_binary(str(tmp_path / "missing"))
    assert ok is False and "not found" in message.lower()


# --------------------------------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------------------------------
def test_build_resume_argv_is_the_documented_invocation(workdir):
    argv = vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="fix the flaky test",
                                  session_id=SESSION_ID, workdir=workdir)
    assert argv == [
        "/opt/vibe/bin/vibe",
        "-p", "fix the flaky test",
        "--resume", SESSION_ID,
        "--workdir", str(workdir),
        "--output", "json",
    ]


def test_build_resume_argv_accepts_streaming_output(workdir):
    argv = vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                                  workdir=workdir, output="streaming")
    assert argv[-2:] == ["--output", "streaming"]


@pytest.mark.parametrize("flag", ["--auto-approve", "--yolo", "--smart-approve", "--trust",
                                  "--approval-policy=auto-approve", "--add-dir", "-c",
                                  "--continue", "--worktree"])
def test_build_resume_argv_refuses_to_fork_or_escalate(workdir, flag):
    # A linked resume must never start a different session or loosen the user's own policy.
    with pytest.raises(ValueError, match="must not be passed"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                               workdir=workdir, extra_args=[flag])


def test_build_resume_argv_rejects_unsafe_inputs(workdir, tmp_path):
    with pytest.raises(ValueError, match="absolute path"):
        vibe.build_resume_argv(binary="vibe", prompt="hi", session_id=SESSION_ID, workdir=workdir)
    with pytest.raises(ValueError, match="prompt is required"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="   ", session_id=SESSION_ID,
                               workdir=workdir)
    with pytest.raises(ValueError, match="session ID"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id="",
                               workdir=workdir)
    with pytest.raises(ValueError, match="Invalid native Vibe session ID"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id="a\nb",
                               workdir=workdir)
    with pytest.raises(ValueError, match="must be absolute"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                               workdir="relative/dir")
    with pytest.raises(ValueError, match="working directory is unavailable"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                               workdir=tmp_path / "gone")
    with pytest.raises(ValueError, match="Unsupported Vibe output format"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                               workdir=workdir, output="yaml")
    with pytest.raises(ValueError, match="argv budget"):
        vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt="x" * (vibe.MAX_PROMPT_CHARS + 1),
                               session_id=SESSION_ID, workdir=workdir)


def test_the_prompt_budget_fits_the_kernels_per_argument_cap(workdir):
    """The cap must be under ``MAX_ARG_STRLEN`` (128 KiB), not merely under ``ARG_MAX``.

    The prompt is one argv string (``-p <prompt>``) with ``stdin=DEVNULL`` and no fallback, so a
    prompt inside the old 384 KiB budget passed this check and then died with E2BIG inside
    ``execvp`` — after the native session had already been claimed.
    """
    linux_max_arg_strlen = 128 * 1024
    assert vibe.MAX_PROMPT_CHARS <= linux_max_arg_strlen - 16 * 1024

    # The boundary is inclusive: exactly the budget still builds an argv with the prompt intact.
    prompt = "x" * vibe.MAX_PROMPT_CHARS
    argv = vibe.build_resume_argv(binary="/opt/vibe/bin/vibe", prompt=prompt,
                                  session_id=SESSION_ID, workdir=workdir)
    assert argv[1:3] == ["-p", prompt]
    assert len(argv[1:3][1].encode()) == vibe.MAX_PROMPT_CHARS


# --------------------------------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------------------------------
def test_run_vibe_resume_spawns_the_thread_cwd_with_a_plain_argv(tmp_path, workdir):
    argv_file = tmp_path / "argv.json"
    binary = _stub_cli(tmp_path)
    prompt = "please fix $(touch pwned) ; rm -rf / `id`"
    run = vibe.run_vibe_resume(
        binary=binary, prompt=prompt, session_id=SESSION_ID, workdir=workdir,
        env={"FAKE_ARGV_FILE": str(argv_file)}, timeout=60)
    assert run.ok and run.returncode == 0
    assert run.duration_s >= 0
    spawned = _read_json(argv_file)
    # argv list, no shell: the metacharacters arrive verbatim as ONE argument and nothing ran.
    assert spawned["argv"][1] == prompt
    assert spawned["argv"][:2] == ["-p", prompt]
    assert "--resume" in spawned["argv"]
    assert spawned["argv"][spawned["argv"].index("--resume") + 1] == SESSION_ID
    assert spawned["cwd"] == str(workdir)
    assert not (workdir / "pwned").exists()


def test_run_vibe_resume_requires_an_explicit_timeout(workdir):
    with pytest.raises(ValueError, match="timeout"):
        vibe.run_vibe_resume(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                             workdir=workdir, timeout=0)
    with pytest.raises(ValueError, match="timeout"):
        vibe.run_vibe_resume(binary="/opt/vibe/bin/vibe", prompt="hi", session_id=SESSION_ID,
                             workdir=workdir, timeout=True)


def test_run_vibe_resume_captures_stderr_and_a_nonzero_exit(tmp_path, workdir):
    argv_file = tmp_path / "argv.json"
    run = vibe.run_vibe_resume(
        binary=_stub_cli(tmp_path), prompt="hi", session_id=SESSION_ID, workdir=workdir,
        env={"FAKE_ARGV_FILE": str(argv_file), "FAKE_MODE": "fail", "FAKE_EXIT": "3",
             "FAKE_STDERR": "session store is locked"}, timeout=60)
    assert run.returncode == 3
    assert "session store is locked" in run.stderr
    with pytest.raises(vibe.VibeRunFailed) as failed:
        vibe.raise_for_vibe_failure(run, session_id=SESSION_ID)
    assert failed.value.returncode == 3
    assert "exited 3" in str(failed.value)
    assert "session store is locked" in str(failed.value)
    assert "session store is locked" in failed.value.stderr


def test_raise_for_vibe_failure_maps_a_missing_approval_to_its_own_refusal(tmp_path, workdir):
    run = vibe.run_vibe_resume(
        binary=_stub_cli(tmp_path), prompt="hi", session_id=SESSION_ID, workdir=workdir,
        env={"FAKE_MODE": "fail", "FAKE_EXIT": "1",
             "FAKE_STDERR": "vibe: needs approval for tool 'bash', which a headless run cannot give"},
        timeout=60)
    with pytest.raises(vibe.VibeApprovalRequired) as rejected:
        vibe.raise_for_vibe_failure(run, session_id=SESSION_ID)
    message = str(rejected.value)
    assert "headless resume cannot answer" in message
    assert "approval policy" in message


def test_run_vibe_resume_with_a_missing_binary_is_a_typed_refusal(workdir):
    with pytest.raises(vibe.VibeBinaryMissing):
        vibe.run_vibe_resume(binary="/nonexistent/vibe", prompt="hi", session_id=SESSION_ID,
                             workdir=workdir, timeout=5)


def test_run_vibe_resume_kills_the_process_group_on_timeout(tmp_path, workdir):
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(vibe.VibeRunTimeout) as timed_out:
        vibe.run_vibe_resume(
            binary=_stub_cli(tmp_path), prompt="hi", session_id=SESSION_ID, workdir=workdir,
            env={"FAKE_MODE": "sleep", "FAKE_SLEEP": "30", "FAKE_PID_FILE": str(pid_file)},
            timeout=1.5)
    assert "within 1.5s" in str(timed_out.value)
    assert time.monotonic() - started < 20
    pid = _wait_for_pid(pid_file)
    assert _wait_pid_gone(pid), "the timed-out Vibe child was left running"


def test_run_vibe_resume_kills_the_child_on_cancel(tmp_path, workdir):
    pid_file = tmp_path / "pid"
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    with pytest.raises(vibe.VibeRunCancelled):
        vibe.run_vibe_resume(
            binary=_stub_cli(tmp_path), prompt="hi", session_id=SESSION_ID, workdir=workdir,
            env={"FAKE_MODE": "sleep", "FAKE_SLEEP": "30", "FAKE_PID_FILE": str(pid_file)},
            timeout=60, cancel_event=cancel)
    pid = _wait_for_pid(pid_file)
    assert _wait_pid_gone(pid), "the cancelled Vibe child was left running"


def test_vibe_denials_surfaces_policy_refusals_without_failing_the_turn(tmp_path, workdir):
    run = vibe.run_vibe_resume(
        binary=_stub_cli(tmp_path), prompt="hi", session_id=SESSION_ID, workdir=workdir,
        env={"FAKE_MODE": "denied"}, timeout=60)
    assert run.ok
    denials = vibe.vibe_denials(run.stderr)
    assert denials == ["Tool call denied: bash (no approval possible in a headless run)"]
    assert vibe.vibe_denials("") == []


# --------------------------------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------------------------------
def test_parse_vibe_output_reads_an_entry_array():
    stdout = json.dumps([
        {"sessionId": SESSION_ID, "turnId": "turn-1", "role": "user", "createdAt": 10,
         "content": [{"type": "text", "text": "fix the flaky test"}]},
        {"sessionId": SESSION_ID, "turnId": "turn-1", "role": "assistant", "createdAt": 20,
         "content": [{"type": "text", "text": "Fixed."},
                     {"type": "image", "data": "iVBORw0KGgo="}]},
    ])
    parsed = vibe.parse_vibe_output(stdout)
    assert parsed.rows() == [{"role": "user", "content": "fix the flaky test"},
                             {"role": "assistant", "content": "Fixed."}]
    assert parsed.session_ids == (SESSION_ID,)
    assert parsed.turn_id == "turn-1"
    assert parsed.assistant_text == "Fixed."
    assert parsed.shape == "json"


def test_parse_vibe_output_reads_an_envelope_object():
    stdout = json.dumps({
        "sessionId": SESSION_ID,
        "messages": [{"role": "assistant", "createdAt": 1,
                      "content": [{"type": "text", "text": "hi"}]}],
        "costUsd": 0.01,
    })
    parsed = vibe.parse_vibe_output(stdout)
    assert parsed.rows() == [{"role": "assistant", "content": "hi"}]


def test_parse_vibe_output_reads_streaming_frames_and_dedupes_repeats():
    entry = {"id": "assistant-1", "sessionId": SESSION_ID, "turnId": "turn-2", "role": "assistant",
             "createdAt": 5, "type": "message",
             "content": [{"type": "text", "text": "streaming answer"}]}
    stdout = "\n".join([
        json.dumps({"type": "turn_started", "sessionId": SESSION_ID}),
        json.dumps({"type": "entry", "entry": entry}),
        json.dumps({"type": "entry", "entry": entry}),          # replayed frame, same entry id
        "not json at all",                                        # a frame we cannot parse
    ])
    parsed = vibe.parse_vibe_output(stdout)
    assert parsed.shape == "ndjson"
    assert parsed.rows() == [{"role": "assistant", "content": "streaming answer"}]


def test_parse_vibe_output_accepts_plain_string_content_and_orders_by_created_at():
    stdout = json.dumps([
        {"role": "assistant", "createdAt": 200, "content": "second"},
        {"role": "user", "createdAt": 100, "content": "first"},
    ])
    parsed = vibe.parse_vibe_output(stdout)
    assert parsed.rows() == [{"role": "user", "content": "first"},
                             {"role": "assistant", "content": "second"}]


def test_parse_vibe_output_ignores_non_message_entries():
    stdout = json.dumps({"entries": [
        {"type": "reasoning", "text": "thinking", "role": "assistant"},
        {"type": "effect", "title": "bash", "role": "tool"},
        {"role": "user", "content": [{"type": "text", "text": "real question"}]},
    ]})
    parsed = vibe.parse_vibe_output(stdout)
    assert parsed.rows() == [{"role": "user", "content": "real question"}]


@pytest.mark.parametrize("stdout", ["", "   ", "not json", json.dumps({"sessionId": SESSION_ID}),
                                    json.dumps([{"role": "user", "content": "   "}])])
def test_parse_vibe_output_degrades_to_a_typed_refusal(stdout):
    with pytest.raises(vibe.VibeOutputUnreadable):
        vibe.parse_vibe_output(stdout)
