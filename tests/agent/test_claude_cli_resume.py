"""The linked-Claude resume transport: same session id, same file, typed refusals, no real CLI.

Every test here drives a **fake** ``claude`` executable — a small Python script with the real CLI's
print-mode surface — so nothing touches the network, a real Claude Code install, or ``~/.claude``.
The fake appends to the temp transcript it is pointed at, exactly like the native CLI appends to
``~/.claude/projects/<slug>/<session>.jsonl``, which is what makes the no-fork assertion testable:
the session id and the file count are compared before and after a turn.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from agent.transports import claude_cli_resume as transport
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("posix")

SESSION_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MODEL = "claude-sonnet-4-5"

# The fake CLI. Modes are chosen with FAKE_CLAUDE_MODE so one script covers every failure the real
# binary produces: a logged-out result with exit 0, an unknown session, a rejected output format,
# a fork, a permission denial, an empty success, and a child that never finishes.
_FAKE_CLAUDE = r'''
import json, os, sys, time, uuid

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")
SESSION_FILE = os.environ.get("FAKE_CLAUDE_SESSION_FILE", "")
FORK_DIR = os.environ.get("FAKE_CLAUDE_FORK_DIR", "")


def log(entry):
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def append(path, session_id, prompt):
    """Append the two records a resumed turn writes: the user message, then the reply."""
    uuid4 = lambda: str(uuid.uuid4())
    parent = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    parent = json.loads(line).get("uuid") or parent
    first, second = uuid4(), uuid4()
    records = [
        {"type": "user", "sessionId": session_id, "uuid": first, "parentUuid": parent,
         "cwd": os.getcwd(), "timestamp": "2026-01-01T00:00:00.000Z",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "sessionId": session_id, "uuid": second, "parentUuid": first,
         "cwd": os.getcwd(), "timestamp": "2026-01-01T00:00:01.000Z",
         "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                     "content": [{"type": "text", "text": "echo: " + prompt}]}},
    ]
    with open(path, "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


argv = sys.argv[1:]
prompt, session_id, output_format = None, None, "text"
index = 0
while index < len(argv):
    token = argv[index]
    following = argv[index + 1] if index + 1 < len(argv) else ""
    if token == "-p":
        if following and not following.startswith("-"):
            prompt = following
            index += 2
            continue
    elif token == "--resume":
        session_id = following
        index += 2
        continue
    elif token == "--output-format":
        output_format = following
        index += 2
        continue
    index += 1
if prompt is None:
    prompt = sys.stdin.read().strip()

cwd = os.getcwd()
log({"pid": os.getpid(), "argv": argv, "cwd": cwd, "prompt": prompt,
     "mode": MODE, "output_format": output_format})

if MODE == "hang":
    time.sleep(30)
    sys.exit(0)
if MODE == "not_found":
    sys.stderr.write("No conversation found with session ID: %s\n" % session_id)
    sys.exit(1)
if MODE == "junk":
    sys.stderr.write("boom: internal failure while starting\n")
    sys.exit(3)
if MODE == "reject_stream_json" and output_format == "stream-json":
    sys.stderr.write("error: unknown option '--output-format'\n")
    sys.exit(2)

init = {"type": "system", "subtype": "init", "session_id": session_id, "cwd": cwd, "model": "claude-sonnet-4-5"}
if MODE == "auth":
    emit(init)
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": "Not logged in \u00b7 Please run /login", "session_id": session_id})
    sys.exit(0)
if MODE == "permission":
    emit(init)
    emit({"type": "result", "subtype": "error_permission", "is_error": True,
          "result": "Tool use was denied: this action requires approval",
          "permission_denials": [{"tool_name": "Bash"}], "session_id": session_id})
    sys.exit(0)
if MODE == "empty":
    emit(init)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "",
          "session_id": session_id})
    sys.exit(0)

target, reported = SESSION_FILE, session_id
if MODE == "fork":
    reported = str(uuid.uuid4())
    target = os.path.join(FORK_DIR, reported + ".jsonl")
if MODE == "split":
    target = os.path.join(FORK_DIR, session_id + ".jsonl")
if MODE == "silent":
    target = ""

if target:
    append(target, reported, prompt)
if output_format == "json":
    sys.stdout.write(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "echo: " + prompt, "session_id": reported, "model": "claude-sonnet-4-5",
        "usage": {"input_tokens": 3, "output_tokens": 4}}) + "\n")
    sys.stdout.flush()
    sys.exit(0)

# A forked run echoes the id it actually wrote, in every event.
emit({"type": "system", "subtype": "init", "session_id": reported, "cwd": cwd,
      "model": "claude-sonnet-4-5"})
emit({"type": "assistant", "session_id": reported, "timestamp": "2026-01-01T00:00:01.000Z",
      "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                  "content": [{"type": "text", "text": "echo: " + prompt}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "echo: " + prompt,
      "session_id": reported, "usage": {"input_tokens": 3, "output_tokens": 4}})
sys.exit(0)
'''


def install_fake_claude(tmp_path: Path) -> Path:
    """Write the fake CLI as an executable script with this interpreter as its shebang."""
    path = tmp_path / "fake-bin" / "claude"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{_FAKE_CLAUDE}", encoding="utf-8")
    path.chmod(0o755)
    return path


def seed_session(path: Path, session_id: str, cwd: str) -> Path:
    """One native transcript with a prior turn, so a resume has something to append to."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "user", "sessionId": session_id, "uuid": "seed-user", "parentUuid": None,
         "cwd": cwd, "message": {"role": "user", "content": "earlier turn"}},
        {"type": "assistant", "sessionId": session_id, "uuid": "seed-assistant",
         "parentUuid": "seed-user", "cwd": cwd,
         "message": {"role": "assistant", "model": MODEL,
                     "content": [{"type": "text", "text": "earlier answer"}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def session_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Harness:
    """A temp project dir + native transcript + fake CLI, wired for one transport call."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.project_dir = tmp_path / "projects" / "-workspace"
        self.session_path = seed_session(self.project_dir / f"{SESSION_ID}.jsonl",
                                        SESSION_ID, str(self.workspace))
        self.fork_dir = tmp_path / "forked"
        self.fork_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = tmp_path / "fake-claude.log"
        self.binary = install_fake_claude(tmp_path)

    def env(self, mode: str = "ok") -> dict:
        return {"FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_SESSION_FILE": str(self.session_path),
                "FAKE_CLAUDE_FORK_DIR": str(self.fork_dir), "FAKE_CLAUDE_LOG": str(self.log_path)}

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def run(self, prompt: str = "add a docstring", **kwargs) -> transport.ClaudeCliTurn:
        options = {"claude_bin": str(self.binary), "cwd": str(self.workspace),
                   "native_session_id": SESSION_ID, "timeout": 30.0,
                   "env_overrides": self.env(kwargs.pop("mode", "ok"))}
        options.update(kwargs)
        return transport.run_claude_resume_turn(prompt, **options)


@pytest.fixture
def harness(tmp_path) -> Harness:
    return Harness(tmp_path)


def test_resume_argv_is_the_documented_shape_and_carries_no_permission_flags():
    argv = transport.build_resume_argv("/usr/local/bin/claude", "add a docstring", SESSION_ID)
    assert argv == ["/usr/local/bin/claude", "-p", "add a docstring", "--resume", SESSION_ID,
                    "--output-format", "stream-json", "--verbose"]
    forbidden = ("--permission-mode", "--dangerously-skip-permissions", "--allowedTools",
                 "--allowed-tools", "--session-id", "--fork-session", "--add-dir")
    assert not [token for token in argv if token in forbidden]


def test_dash_leading_prompt_is_piped_on_stdin(harness):
    # `claude -p` takes an optional value, so a prompt starting with "-" would be read as a flag.
    turn = harness.run("--explain the diff")
    call = harness.calls()[0]
    assert turn.prompt_via_stdin is True
    assert call["argv"][:2] == ["-p", "--resume"]
    assert call["prompt"] == "--explain the diff"
    assert session_records(harness.session_path)[-2]["message"]["content"] == "--explain the diff"


def test_an_oversized_prompt_is_piped_on_stdin_too(harness):
    prompt = "context " + "x" * (transport._MAX_ARGV_PROMPT_CHARS + 1)
    turn = harness.run(prompt)
    call = harness.calls()[0]
    assert turn.prompt_via_stdin is True
    assert prompt not in call["argv"]  # no single argv element that big
    assert call["prompt"] == prompt
    assert session_records(harness.session_path)[-2]["message"]["content"] == prompt


def test_turn_appends_to_the_same_file_and_reports_display_rows(harness):
    before = session_records(harness.session_path)
    turn = harness.run("add a docstring")

    assert turn.native_session_id == SESSION_ID
    assert turn.requested_session_id == SESSION_ID
    assert turn.output_format == "stream-json"
    assert turn.final_text == "echo: add a docstring"
    assert [(row["role"], row["content"]) for row in turn.rows] == [
        ("assistant", "echo: add a docstring")]
    # The event's ISO timestamp lands as epoch seconds, like a re-read of the native store.
    assert turn.rows[0]["timestamp"] == pytest.approx(1767225601.0)
    call = harness.calls()[0]
    assert call["cwd"] == str(harness.workspace)
    assert call["argv"] == ["-p", "add a docstring", "--resume", SESSION_ID,
                            "--output-format", "stream-json", "--verbose"]

    after = session_records(harness.session_path)
    assert after[:len(before)] == before  # appended, never rewritten
    assert {record["sessionId"] for record in after} == {SESSION_ID}
    assert after[-2]["message"]["content"] == "add a docstring"
    assert list(harness.project_dir.glob("*.jsonl")) == [harness.session_path]


def test_stream_json_falls_back_to_json_when_the_build_rejects_it(harness):
    turn = harness.run("add a docstring", mode="reject_stream_json")
    formats = [call["output_format"] for call in harness.calls()]
    assert formats == ["stream-json", "json"]
    assert turn.output_format == "json"
    assert turn.final_text == "echo: add a docstring"
    assert turn.usage == {"input_tokens": 3, "output_tokens": 4}
    assert session_records(harness.session_path)[-1]["sessionId"] == SESSION_ID


def test_logged_out_result_with_exit_zero_is_a_typed_auth_error(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="auth")
    assert caught.value.code == transport.CLAUDE_AUTH_REQUIRED
    assert "login" in caught.value.detail.lower()


def test_a_different_echoed_session_id_is_a_fork_refusal(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="fork")
    assert caught.value.code == transport.CLAUDE_SESSION_MISMATCH
    assert SESSION_ID in caught.value.detail
    # The transport never adopts the id the CLI invented: the linked thread's id is unchanged.
    assert {record["sessionId"] for record in session_records(harness.session_path)} == {SESSION_ID}


def test_missing_native_conversation_is_a_typed_refusal(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="not_found")
    assert caught.value.code == transport.CLAUDE_SESSION_MISSING
    assert caught.value.exit_code == 1


def test_permission_denial_is_a_typed_refusal_never_an_auto_approval(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="permission")
    assert caught.value.code == transport.CLAUDE_PERMISSION_REQUIRED
    argv = harness.calls()[0]["argv"]
    assert not [token for token in argv if "permission" in token.lower()]


def test_empty_success_is_not_an_answer(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="empty")
    assert caught.value.code == transport.CLAUDE_EMPTY_RESULT


def test_cli_failure_surfaces_stderr_as_a_typed_error(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="junk")
    assert caught.value.code == transport.CLAUDE_EXIT_ERROR
    assert "boom" in caught.value.stderr_tail


def test_timeout_kills_the_child_and_reports_a_typed_timeout(harness):
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="hang", timeout=0.5)
    assert caught.value.code == transport.CLAUDE_TIMEOUT
    pid = wait_until(lambda: harness.calls() and harness.calls()[0]["pid"], what="the fake CLI started")
    wait_until(lambda: not _alive(pid), what="the timed-out child to be gone")


def test_an_already_set_cancel_event_kills_the_child(harness):
    import threading

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(transport.ClaudeCliError) as caught:
        harness.run(mode="hang", cancel_event=cancel)
    assert caught.value.code == transport.CLAUDE_CANCELLED
    pid = wait_until(lambda: harness.calls() and harness.calls()[0]["pid"], what="the fake CLI started")
    wait_until(lambda: not _alive(pid), what="the cancelled child to be gone")


def test_missing_binary_is_a_typed_refusal_not_a_crash(monkeypatch):
    monkeypatch.setattr(transport, "_install_prefix_binary", lambda name: None)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(transport.ClaudeCliError) as caught:
        transport.resolve_claude_binary()
    assert caught.value.code == transport.CLAUDE_MISSING_BINARY


def test_binary_resolution_prefers_path_over_install_prefixes(harness, monkeypatch):
    monkeypatch.setattr(transport, "_install_prefix_binary",
                        lambda name: pytest.fail("PATH must win before install prefixes"))
    monkeypatch.setenv("PATH", str(harness.binary.parent))
    assert transport.resolve_claude_binary() == str(harness.binary)


def test_configured_binary_override_wins(harness):
    assert transport.resolve_claude_binary({"model": {"claude_bin": str(harness.binary)}}) == str(
        harness.binary)
    with pytest.raises(transport.ClaudeCliError):
        transport.resolve_claude_binary({"model": {"claude_bin": str(harness.tmp_path / "nope")}})


def _alive(pid: int) -> bool:
    """True while *pid* exists (a reaped child is gone, so this is the death check)."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - another user's live process
        return True
    return True


def test_a_turn_runs_the_fake_cli_resolved_from_path(harness, monkeypatch):
    """End to end through PATH: the fake `claude` is found, executed and appends to the thread."""
    monkeypatch.setattr(transport, "_install_prefix_binary",
                        lambda name: pytest.fail("PATH must win before install prefixes"))
    monkeypatch.setenv("PATH", str(harness.binary.parent))
    turn = transport.run_claude_resume_turn(
        "hi", native_session_id=SESSION_ID, cwd=str(harness.workspace), timeout=30.0,
        env_overrides=harness.env())
    assert turn.final_text == "echo: hi"
    assert session_records(harness.session_path)[-1]["sessionId"] == SESSION_ID
