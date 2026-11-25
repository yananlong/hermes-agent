"""The linked-Claude write path: resolve the native thread, lock it, append to it, prove it.

The fake ``claude`` here is driven through the environment (``FAKE_CLAUDE_*``) and appends to the
temp transcript exactly like the native CLI appends to ``~/.claude/projects/<slug>/<id>.jsonl``.
Nothing touches a real Claude Code install, ``~/.claude`` or the network; ``HERMES_HOME`` is the
per-test temp dir, so the thread lock lands there too.
"""

import json
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import claude_linked_resume as claude_resume
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session

pytestmark = pytest.mark.platforms("posix")

HERMES_SID = "hermes-claude-linked"
SESSION_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
OTHER_SESSION_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"

_FAKE_CLAUDE = r'''
import json, os, sys, uuid

MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")
SESSION_FILE = os.environ["FAKE_CLAUDE_SESSION_FILE"]
SESSION_ID = os.environ["FAKE_CLAUDE_SESSION_ID"]
PROJECTS = os.environ["FAKE_CLAUDE_PROJECTS"]
LOG = os.environ.get("FAKE_CLAUDE_LOG", "")

argv = sys.argv[1:]
prompt = argv[1] if len(argv) > 1 and argv[0] == "-p" and not argv[1].startswith("-") else None
if prompt is None:
    prompt = sys.stdin.read().strip()
if LOG:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": prompt}) + "\n")


def append(path, session_id, prompt, reply):
    parent = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    parent = json.loads(line).get("uuid") or parent
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "sessionId": session_id, "uuid": first,
                                 "parentUuid": parent, "cwd": os.getcwd(),
                                 "message": {"role": "user", "content": prompt}}) + "\n")
        handle.write(json.dumps({"type": "assistant", "sessionId": session_id, "uuid": second,
                                 "parentUuid": first, "cwd": os.getcwd(),
                                 "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                                             "content": [{"type": "text", "text": reply}]}}) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


target, reported = SESSION_FILE, SESSION_ID
if MODE == "fork":
    reported = str(uuid.uuid4())
    target = os.path.join(os.path.dirname(SESSION_FILE), reported + ".jsonl")
elif MODE == "split":
    target = os.path.join(PROJECTS, "-elsewhere", SESSION_ID + ".jsonl")
    os.makedirs(os.path.dirname(target), exist_ok=True)
elif MODE == "silent":
    target = ""
elif MODE == "foreign":
    with open(SESSION_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "sessionId": "ffffffff-ffff-4fff-8fff-ffffffffffff",
                                 "message": {"role": "user", "content": "other terminal"}}) + "\n")

reply = "echo: " + prompt
if target:
    append(target, reported, prompt, reply)
emit({"type": "system", "subtype": "init", "session_id": reported, "cwd": os.getcwd()})
emit({"type": "assistant", "session_id": reported,
      "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                  "content": [{"type": "text", "text": reply}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": reply,
      "session_id": reported})
sys.exit(0)
'''


class Env:
    """One linked thread on disk: its workspace, its native transcript, the fake CLI, a log."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.tmp_path = tmp_path
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.projects = tmp_path / "claude" / "projects"
        self.project_dir = self.projects / claude_resume.project_slugs(str(self.workspace))[0]
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.project_dir / f"{SESSION_ID}.jsonl"
        self.log_path = tmp_path / "fake-claude.log"
        self.binary = tmp_path / "fake-bin" / "claude"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_text(f"#!{sys.executable}\n{_FAKE_CLAUDE}", encoding="utf-8")
        self.binary.chmod(0o755)
        record = {"type": "user", "sessionId": SESSION_ID, "uuid": "seed-user", "parentUuid": None,
                  "cwd": str(self.workspace), "message": {"role": "user", "content": "earlier turn"}}
        self.session_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        monkeypatch.setattr(claude_resume, "resolve_claude_projects",
                            lambda profile_home=None: self.projects)
        self.monkeypatch = monkeypatch
        self.set_mode("ok")

    def set_mode(self, mode: str) -> None:
        self.monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)

    def configure(self) -> None:
        """Publish the fake CLI's knobs to the child env (read by the transport's inheritance)."""
        env = {"FAKE_CLAUDE_SESSION_FILE": str(self.session_path),
               "FAKE_CLAUDE_SESSION_ID": SESSION_ID,
               "FAKE_CLAUDE_PROJECTS": str(self.projects),
               "FAKE_CLAUDE_LOG": str(self.log_path)}
        for key, value in env.items():
            self.monkeypatch.setenv(key, value)

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def identity(self) -> tuple[frozenset, tuple[Path, ...]]:
        """The native thread's observable identity: its session ids and its session files."""
        files = tuple(sorted(self.projects.glob("*/*.jsonl")))
        ids = frozenset(record.get("sessionId") for path in files
                        for record in records(path) if record.get("sessionId"))
        return ids, files

    def run(self, prompt: str = "add a docstring", **kwargs):
        self.configure()
        options = {"native_session_id": SESSION_ID, "claude_bin": str(self.binary),
                   "timeout": 30.0}
        options.update(kwargs)
        return claude_resume.run_linked_claude_turn(prompt=prompt, **options)


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)


@pytest.fixture
def linked(env, tmp_path):
    """A real linked row in a real store: pointer-only, legacy api_mode, cwd captured at link time."""
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, HERMES_SID, "desktop", cwd=str(env.workspace), model="claude-sonnet-4-5",
                     model_config={"claude_session_id": SESSION_ID, "claude_linked_thread": True,
                                   "api_mode": "claude_linked_readonly"})
        yield db
    finally:
        db.close()


def test_target_resolves_the_native_id_and_the_workspace_that_owns_the_project_dir(env, linked):
    target = claude_resume.resolve_linked_claude_target(store=linked, session_id=HERMES_SID)
    assert target.native_session_id == SESSION_ID
    assert target.session_path == env.session_path.resolve()
    assert target.cwd == str(env.workspace)
    assert target.slug_verified is True  # the transcript's cwd encodes this project dir


def test_legacy_readonly_row_is_resume_capable(env, tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, "legacy-readonly", "desktop", cwd=str(env.workspace),
                     model_config={"claude_session_id": SESSION_ID,
                                   "api_mode": "claude_linked_readonly"})
        assert claude_resume.linked_claude_native_id(db, "legacy-readonly") == SESSION_ID
    finally:
        db.close()


def test_a_row_that_is_not_linked_is_refused(env, tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, "plain", "desktop", cwd=str(env.workspace), model_config={})
        assert claude_resume.linked_claude_native_id(db, "plain") is None
        with pytest.raises(claude_resume.LinkedClaudeError) as caught:
            claude_resume.run_linked_claude_turn(prompt="hi", store=db, session_id="plain",
                                                claude_bin=str(env.binary))
        assert caught.value.code == claude_resume.CLAUDE_NOT_LINKED
    finally:
        db.close()


def test_resume_appends_to_the_same_native_thread_and_leaves_exactly_one_session_file(
        env, monkeypatch):
    """No fork: the native session id and the set of session files are identical before and after.

    The fake ``claude`` is found through ``PATH`` alone (the install-prefix fallback would fail the
    test), so this is the real resolution path a gateway/service launch takes.
    """
    from agent.transports import claude_cli_resume as transport

    monkeypatch.setattr(transport, "_install_prefix_binary",
                        lambda name: pytest.fail("PATH must win before install prefixes"))
    monkeypatch.setenv("PATH", str(env.binary.parent))
    before = env.identity()
    env.configure()
    turn = claude_resume.run_linked_claude_turn(prompt="add a docstring", store=None,
                                               native_session_id=SESSION_ID, timeout=30.0)
    after = env.identity()

    assert after == before
    assert before[0] == {SESSION_ID}
    assert len(after[1]) == 1
    assert turn.native_session_id == SESSION_ID
    assert turn.cwd == str(env.workspace)
    assert turn.slug_verified is True
    assert turn.bytes_appended > 0
    assert [(row["role"], row["content"]) for row in turn.rows] == [
        ("assistant", "echo: add a docstring")]
    assert turn.argv == (str(env.binary), "-p", "add a docstring", "--resume", SESSION_ID,
                         "--output-format", "stream-json", "--verbose")
    assert env.calls()[0]["cwd"] == str(env.workspace)

    appended = records(env.session_path)[1:]
    assert [record["type"] for record in appended] == ["user", "assistant"]
    assert appended[-1]["message"]["content"][0]["text"] == "echo: add a docstring"


def test_a_turn_through_the_linked_row_uses_the_row_pointer(env, linked):
    env.configure()
    turn = claude_resume.run_linked_claude_turn(prompt="hi", store=linked, session_id=HERMES_SID,
                                               claude_bin=str(env.binary), timeout=30.0)
    assert turn.native_session_id == SESSION_ID
    assert records(env.session_path)[-1]["sessionId"] == SESSION_ID


def test_a_second_turn_on_a_busy_thread_is_a_typed_conflict(env):
    with claude_resume.native_thread_lock(SESSION_ID):
        with pytest.raises(claude_resume.LinkedClaudeError) as caught:
            env.run("hi")
    assert caught.value.code == claude_resume.CLAUDE_LINKED_BUSY
    assert env.calls() == []  # nothing was spawned for the refused turn


def test_another_writers_record_is_reported_as_a_conflict(env):
    env.set_mode("foreign")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        env.run("hi")
    assert caught.value.code == claude_resume.CLAUDE_WRITER_CONFLICT
    assert OTHER_SESSION_ID in caught.value.detail


def test_a_turn_that_never_reaches_the_store_is_refused(env):
    env.set_mode("silent")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        env.run("hi")
    assert caught.value.code == claude_resume.CLAUDE_NOT_RECORDED


def test_a_second_transcript_for_the_same_session_is_a_fork(env):
    env.set_mode("split")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        env.run("hi")
    assert caught.value.code == claude_resume.CLAUDE_FORK_DETECTED
    assert SESSION_ID in caught.value.detail


def test_a_failed_run_reports_the_phantom_session_it_left_behind(env):
    env.set_mode("fork")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        env.run("hi")
    assert caught.value.code == "claude_session_mismatch"
    assert caught.value.upstream_code == "claude_session_mismatch"
    assert "new Claude session file" in caught.value.detail
    # The linked thread itself is untouched: no record of the failed turn landed on it.
    assert {record["sessionId"] for record in records(env.session_path)} == {SESSION_ID}


def test_missing_binary_degrades_to_a_typed_refusal_without_touching_the_transcript(
        env, monkeypatch):
    from agent.transports import claude_cli_resume as transport

    monkeypatch.setattr(transport, "_install_prefix_binary", lambda name: None)
    monkeypatch.setenv("PATH", "")
    before = env.session_path.read_bytes()
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        env.run("hi", claude_bin=None)
    assert caught.value.code == transport.CLAUDE_MISSING_BINARY
    assert env.session_path.read_bytes() == before


def test_a_workspace_that_does_not_encode_the_project_dir_is_refused(env):
    elsewhere = env.tmp_path / "somewhere-else"
    elsewhere.mkdir()
    orphan = env.projects / "-another-project" / f"{OTHER_SESSION_ID}.jsonl"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(json.dumps({"type": "user", "sessionId": OTHER_SESSION_ID,
                                  "cwd": str(elsewhere),
                                  "message": {"role": "user", "content": "hi"}}) + "\n",
                      encoding="utf-8")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        claude_resume.resolve_linked_claude_target(native_session_id=OTHER_SESSION_ID)
    assert caught.value.code == claude_resume.CLAUDE_CWD_MISMATCH


def test_a_missing_workspace_is_a_typed_refusal(env):
    gone = env.tmp_path / "gone"
    orphan = env.projects / "-gone" / f"{OTHER_SESSION_ID}.jsonl"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(json.dumps({"type": "user", "sessionId": OTHER_SESSION_ID,
                                  "cwd": str(gone),
                                  "message": {"role": "user", "content": "hi"}}) + "\n",
                      encoding="utf-8")
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        claude_resume.resolve_linked_claude_target(native_session_id=OTHER_SESSION_ID)
    assert caught.value.code == claude_resume.CLAUDE_CWD_UNAVAILABLE


def test_an_unknown_native_session_is_a_typed_refusal(env):
    with pytest.raises(claude_resume.LinkedClaudeError) as caught:
        claude_resume.resolve_linked_claude_target(native_session_id=OTHER_SESSION_ID)
    assert caught.value.code == claude_resume.CLAUDE_NATIVE_SESSION_MISSING


def test_a_damaged_pointer_is_a_typed_refusal(env, tmp_path):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, "damaged", "desktop", cwd=str(env.workspace),
                     model_config={"claude_linked_thread": True, "claude_session_id": "../../etc"})
        with pytest.raises(claude_resume.LinkedClaudeError) as caught:
            claude_resume.linked_claude_native_id(db, "damaged")
        assert caught.value.code == claude_resume.CLAUDE_STORE_UNAVAILABLE
    finally:
        db.close()


def test_the_thread_lock_is_released_after_a_turn(env):
    env.run("hi")
    # A turn that kept the lock would make this second acquisition fail as BUSY.
    with claude_resume.native_thread_lock(SESSION_ID):
        assert env.session_path.exists()
    assert env.run("again").final_text == "echo: again"


def test_native_lock_path_stays_under_the_hermes_home(env):
    path = claude_resume.native_lock_path(SESSION_ID)
    from hermes_constants import get_hermes_home

    assert path.parent.parent == Path(get_hermes_home())
    assert SESSION_ID in path.name and os.sep not in path.name
