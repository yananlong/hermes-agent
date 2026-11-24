"""A linked Claude/Mistral send APPENDS to the native thread, and can never fork it.

The turn route (``agent/linked_foreign_turn.py``) is the single place a linked pointer row's turn
can run: ``conversation_loop._turn_without_loop`` asks it first and returns whatever it returns —
including a refusal. These tests pin both halves of that contract:

* the happy path really appends to the native store and leaves the Hermes row empty (pointer
  semantics: the native tool owns the transcript, display re-reads it),
* every refusal is a typed turn result, never ``None`` and never a generic LLM turn — the fork the
  feature exists to prevent.

The two fake CLIs are the transports' own test harness shape: an executable that appends to the
native transcript exactly like ``claude --resume`` / ``vibe --resume`` do, driven by env vars.
No real ``claude``/``vibe``/``codex`` binary and no network is involved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import conversation_loop
from agent import linked_foreign_turn as route
from hermes_cli import claude_linked_resume as claude_resume
from hermes_cli import mistral_linked_threads
from hermes_cli.foreign_linked_store import LinkedConfigUnreadable
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session

pytestmark = pytest.mark.platforms("posix")

HERMES_SID = "hermes-linked-row"
CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"

_FAKE_CLAUDE = r'''
import json, os, sys, uuid

SESSION_FILE = os.environ["FAKE_CLAUDE_SESSION_FILE"]
SESSION_ID = os.environ["FAKE_CLAUDE_SESSION_ID"]
LOG = os.environ.get("FAKE_CLAUDE_LOG", "")
MODE = os.environ.get("FAKE_CLAUDE_MODE", "ok")

argv = sys.argv[1:]
prompt = argv[1] if len(argv) > 1 and argv[0] == "-p" else sys.stdin.read().strip()
if LOG:
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv}) + "\n")

reply = "echo: " + prompt
if MODE == "ok":
    parent = None
    with open(SESSION_FILE, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                parent = json.loads(line).get("uuid") or parent
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with open(SESSION_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "user", "sessionId": SESSION_ID, "uuid": first,
                                 "parentUuid": parent, "cwd": os.getcwd(),
                                 "message": {"role": "user", "content": prompt}}) + "\n")
        handle.write(json.dumps({"type": "assistant", "sessionId": SESSION_ID, "uuid": second,
                                 "parentUuid": first, "cwd": os.getcwd(),
                                 "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                                             "content": [{"type": "text", "text": reply}]}}) + "\n")

for event in (
    {"type": "system", "subtype": "init", "session_id": SESSION_ID, "cwd": os.getcwd()},
    {"type": "assistant", "session_id": SESSION_ID,
     "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                 "content": [{"type": "text", "text": reply}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": reply,
     "session_id": SESSION_ID},
):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
sys.exit(0)
'''

_FAKE_VIBE = r'''
import hashlib, json, os, sys

argv = sys.argv[1:]
FLAGS = {"-p": "prompt", "--prompt": "prompt", "--resume": "resume",
         "--workdir": "workdir", "--output": "output"}
opts, index = {}, 0
while index < len(argv):
    if argv[index] in FLAGS:
        opts[FLAGS[argv[index]]] = argv[index + 1]
        index += 2
    else:
        index += 1
prompt, session_id = opts.get("prompt") or "", opts.get("resume") or ""
root = os.path.join(os.environ.get("VIBE_HOME", ""), "logs", "session")
directory = os.path.join(root, "unified", session_id)
log = os.environ.get("FAKE_CALL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "workdir": opts.get("workdir")}) + "\n")
if not os.path.isdir(directory):
    sys.stderr.write("Failed to resume session\n")
    sys.exit(2)


def records(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


sequence, previous = 0, ""
for name in sorted(os.listdir(os.path.join(directory, "journal"))):
    found = records(os.path.join(directory, "journal", name))
    if found:
        sequence = int(found[-1].get("sequence") or 0)
        previous = str(found[-1].get("record_sha256") or "")

reply = "echo: " + prompt
entries = []
with open(os.path.join(directory, "journal", "%016d.jsonl" % (sequence + 1)), "a",
          encoding="utf-8") as handle:
    for role, text in (("user", prompt), ("assistant", reply)):
        sequence += 1
        entry = {"id": "%s-%d" % (role, sequence), "sessionId": session_id,
                 "turnId": "turn-%d" % sequence, "role": role, "type": "message",
                 "createdAt": sequence, "content": [{"type": "text", "text": text}]}
        body = {"payload": {"delta": [{"entry": entry}]}, "type": "projection_delta",
                "previous_record_sha256": previous, "sequence": sequence}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
        body["record_sha256"] = digest
        handle.write(json.dumps(body) + "\n")
        previous = digest
        entries.append(entry)
print(json.dumps({"sessionId": session_id, "turnId": "turn-%d" % sequence,
                  "messages": entries}))
sys.exit(0)
'''


class _FakeAgent:
    """The attributes the turn route reads off a real ``AIAgent`` (and nothing else)."""

    def __init__(self, store, session_id=HERMES_SID, *, api_mode="chat_completions", cwd=None):
        self.persistence = store
        self._session_db = store
        self.session_id = session_id
        self.api_mode = api_mode
        self.session_cwd = cwd
        self._interrupt_requested = False


def _row_state(messages=None, text="add a docstring"):
    """The slice of ``_LoopState`` the early-turn dispatch reads."""
    return SimpleNamespace(messages=list(messages or [{"role": "user", "content": text}]),
                           user_message=text, original_user_message=text)


def _cli(tmp_path: Path, name: str, body: str) -> str:
    binary = tmp_path / "bin" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Keep every lock / cache this route takes inside the test's own Hermes home."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    return tmp_path / "home"


@pytest.fixture
def store(tmp_path, home):
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        yield handle
    finally:
        handle.close()


# ── Claude Code: the native transcript grows, the Hermes row stays empty ────────────────────────


@pytest.fixture
def claude_env(tmp_path, monkeypatch, store):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    projects = tmp_path / "claude" / "projects"
    project_dir = projects / claude_resume.project_slugs(str(workspace))[0]
    project_dir.mkdir(parents=True)
    transcript = project_dir / f"{CLAUDE_ID}.jsonl"
    transcript.write_text(json.dumps({
        "type": "user", "sessionId": CLAUDE_ID, "uuid": "seed", "parentUuid": None,
        "cwd": str(workspace), "message": {"role": "user", "content": "earlier turn"},
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_resume, "resolve_claude_projects",
                        lambda profile_home=None: projects)
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_FILE", str(transcript))
    monkeypatch.setenv("FAKE_CLAUDE_SESSION_ID", CLAUDE_ID)
    binary = _cli(tmp_path, "claude", _FAKE_CLAUDE)
    monkeypatch.setattr(route, "_linked_config", lambda: {"model": {"claude_bin": binary}})
    seed_session(store, HERMES_SID, "desktop", cwd=str(workspace),
                 model_config={"claude_linked_thread": True, "claude_session_id": CLAUDE_ID,
                               "api_mode": "claude_linked_readonly"})
    return SimpleNamespace(workspace=workspace, transcript=transcript, binary=binary)


def test_claude_send_appends_to_the_native_thread_and_leaves_the_row_empty(claude_env, store):
    agent = _FakeAgent(store, cwd=str(claude_env.workspace))
    state = _row_state(text="add a docstring")
    before = claude_env.transcript.read_text(encoding="utf-8").count("\n")

    result = route.run_linked_foreign_turn(
        agent, user_message=state.user_message, original_user_message=state.original_user_message,
        messages=state.messages)

    # 1. The native store is the transcript, and it is the thing that grew.
    after = claude_env.transcript.read_text(encoding="utf-8").count("\n")
    assert after == before + 2, "the send must append the user turn and its answer natively"
    assert "add a docstring" in claude_env.transcript.read_text(encoding="utf-8")

    # 2. Pointer semantics: the linked row gained NO rows of its own.
    assert transcript_rows(store, HERMES_SID) == []

    # 3. A normal turn result, produced from the native reply, with the surfaces told to skip
    #    their own transcript write.
    assert result["completed"] is True and result["error"] is None
    assert result["final_response"] == "echo: add a docstring"
    assert result["agent_persisted"] is True
    assert result["linked_foreign_agent"] == "claude"
    assert result["linked_native_session_id"] == CLAUDE_ID
    # The projection rides along for live display, and only once (no duplicated user row).
    assert [row["role"] for row in state.messages] == ["user", "assistant"]
    assert state.messages[-1]["content"] == "echo: add a docstring"


def test_claude_send_is_dispatched_even_when_the_runtime_says_codex(claude_env, store, monkeypatch):
    """The row flag is authoritative: a stale codex api_mode must not route a Claude row anywhere."""
    monkeypatch.setattr(conversation_loop, "_codex_app_server_turn",
                        lambda *a, **k: pytest.fail("a linked Claude row must never use app-server"))
    agent = _FakeAgent(store, cwd=str(claude_env.workspace), api_mode="codex_app_server")
    state = _row_state()

    result = conversation_loop._turn_without_loop(agent, state, "")

    assert result is not None and result["completed"] is True
    assert result["linked_foreign_agent"] == "claude"
    assert result["final_response"] == "echo: add a docstring"


# ── Mistral Vibe: same contract through the Vibe journal ───────────────────────────────────────


@pytest.fixture
def vibe_env(tmp_path, monkeypatch, store):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "vibe" / "logs" / "session"
    session_dir = root / "unified" / MISTRAL_ID
    (session_dir / "journal").mkdir(parents=True)
    (session_dir / "meta.json").write_text(json.dumps({
        "session_id": MISTRAL_ID, "title": "Vibe chat",
        "environment": {"working_directory": str(workspace)}, "bumped_at": 1_700_000_000,
    }), encoding="utf-8")
    (session_dir / "journal" / "0000000000000001.jsonl").write_text(json.dumps({
        "payload": {"delta": [{"entry": {
            "id": "user-1", "sessionId": MISTRAL_ID, "turnId": "turn-1", "role": "user",
            "type": "message", "createdAt": 1,
            "content": [{"type": "text", "text": "earlier turn"}]}}]},
        "type": "projection_delta", "previous_record_sha256": "", "sequence": 1,
        "record_sha256": "sha1",
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(mistral_linked_threads, "resolve_vibe_session_root",
                        lambda profile_home=None: root)
    binary = _cli(tmp_path, "vibe", _FAKE_VIBE)
    monkeypatch.setattr(route, "_linked_config", lambda: {"model": {"vibe_bin": binary}})
    seed_session(store, HERMES_SID, "desktop", cwd=str(workspace),
                 model_config={"mistral_linked_thread": True,
                               "mistral_session_id": MISTRAL_ID,
                               "api_mode": "mistral_linked_readonly"})
    return SimpleNamespace(workspace=workspace, session_dir=session_dir, root=root)


def test_mistral_send_appends_to_the_native_thread_and_leaves_the_row_empty(vibe_env, store):
    agent = _FakeAgent(store, cwd=str(vibe_env.workspace))
    state = _row_state(text="fix the flaky test")
    journals = sorted(vibe_env.session_dir.glob("journal/*.jsonl"))
    before = sum(path.read_text(encoding="utf-8").count("\n") for path in journals)

    result = route.run_linked_foreign_turn(
        agent, user_message=state.user_message, original_user_message=state.original_user_message,
        messages=state.messages)

    journals = sorted(vibe_env.session_dir.glob("journal/*.jsonl"))
    after = sum(path.read_text(encoding="utf-8").count("\n") for path in journals)
    assert after == before + 2
    assert [path.name for path in journals] == ["0000000000000001.jsonl", "0000000000000002.jsonl"]
    assert sorted(path.name for path in (vibe_env.root / "unified").iterdir()) == [MISTRAL_ID]

    assert transcript_rows(store, HERMES_SID) == []
    assert result["completed"] is True
    assert result["final_response"] == "echo: fix the flaky test"
    assert result["linked_foreign_agent"] == "mistral"
    assert result["linked_native_session_id"] == MISTRAL_ID
    assert result["agent_persisted"] is True


# ── no fork: every refusal is a typed result, never a fall-through to the generic loop ─────────


def _claude_refusal(_agent, _store, _session_id, _prompt):
    from hermes_cli.claude_linked_resume import LinkedClaudeError
    raise LinkedClaudeError("claude_missing_binary", "Claude Code is not installed")


def _vibe_refusal(_agent, _store, _session_id, _prompt):
    from agent.transports.vibe_cli_resume import VibeBinaryMissing
    raise VibeBinaryMissing("Vibe is not installed")


@pytest.mark.parametrize("agent_key, runner, expected", [
    ("claude", "_run_claude", "claude_missing_binary"),
    ("mistral", "_run_mistral", "vibe_binary_missing"),
])
def test_a_refused_linked_turn_is_a_typed_result_not_a_generic_turn(
        agent_key, runner, expected, store, monkeypatch):
    monkeypatch.setattr(route, runner, _claude_refusal if agent_key == "claude" else _vibe_refusal)
    config = {"claude_linked_thread": True, "claude_session_id": CLAUDE_ID} \
        if agent_key == "claude" else {"mistral_linked_thread": True,
                                       "mistral_session_id": MISTRAL_ID}
    seed_session(store, "row", "desktop", model_config=config)
    agent = _FakeAgent(store, "row")
    state = _row_state()

    result = conversation_loop._turn_without_loop(agent, state, "")

    # Non-None is the whole point: None would hand this turn to the generic model loop.
    assert result is not None
    assert result["completed"] is False and result["partial"] is True
    assert result["failure_reason"] == expected
    assert result["error"].startswith(f"{expected}:")
    assert expected in result["final_response"]
    assert result["agent_persisted"] is True, "a refusal must still not write to the pointer row"
    assert result["linked_foreign_agent"] == agent_key
    assert transcript_rows(store, "row") == []


def test_a_linked_row_with_unreadable_configuration_fails_closed(store, monkeypatch):
    """A pointer row nobody can validate is refused rather than run as an ordinary session."""
    monkeypatch.setattr(route, "_linked_config", lambda: None)
    seed_session(store, "corrupt", "desktop", model_config={"claude_linked_thread": True})
    agent = _FakeAgent(store, "corrupt")
    monkeypatch.setattr("hermes_cli.foreign_linked_store.row_model_config",
                        lambda *_a, **_k: (_ for _ in ()).throw(
                            LinkedConfigUnreadable("session corrupt is unreadable")))

    assert route.linked_foreign_agent(agent) == "foreign"
    result = conversation_loop._turn_without_loop(agent, _row_state(), "")

    assert result is not None and result["completed"] is False
    assert result["linked_error_code"] == "linked_foreign_config_unreadable"
    assert "re-link" in result["final_response"]


def test_a_plain_row_is_not_detected_as_linked(store):
    seed_session(store, "plain", "desktop", model_config={"api_mode": "chat_completions"})
    agent = _FakeAgent(store, "plain")

    assert route.linked_foreign_agent(agent) is None
    # ... and the early-turn dispatch therefore leaves it to the ordinary runtime.
    assert conversation_loop._turn_without_loop(agent, _row_state(), "") is None


def test_a_legacy_readonly_row_is_still_detected(store):
    """Rows written before the flag existed carry only the api_mode — still a pointer."""
    seed_session(store, "legacy", "desktop",
                 model_config={"claude_session_id": CLAUDE_ID,
                               "api_mode": "claude_linked_readonly"})
    assert route.linked_foreign_agent(_FakeAgent(store, "legacy")) == "claude"


def test_the_linked_codex_guard_still_owns_codex(store):
    """Codex keeps its existing shape: the app-server runtime or a refusal, never the generic loop."""
    seed_session(store, "codex-row", "desktop",
                 model_config={"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})
    agent = _FakeAgent(store, "codex-row", api_mode="chat_completions")

    assert route.linked_foreign_agent(agent) is None
    result = conversation_loop._turn_without_loop(agent, _row_state(), "")
    assert result is not None and result["completed"] is False
    assert "Codex app-server runtime" in result["final_response"]
