"""A linked Mistral Vibe session is writable: a Hermes send appends to the SAME native thread.

The stub ``vibe`` CLI below is a real executable (a ``/bin/sh`` shim that execs this test's
interpreter) that behaves the way the native CLI does on ``--resume``: it locates the *existing*
unified session directory through ``$VIBE_HOME``, appends chained journal records to its
``journal/NNNN.jsonl``, and prints the turn as JSON. It refuses to create a session, which is what
makes the no-fork assertions meaningful:

* one ``unified/<id>`` directory before and after two sends,
* the same session id in the pointer row, the argv, the new journal entries and the returned turn,
* a grown journal whose new rows are exactly the ones the linked-history reader shows.

No network and no real ``vibe`` binary are involved.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from hermes_cli import mistral_linked_history, mistral_linked_threads
from hermes_cli import mistral_linked_resume as resume
from agent.transports import vibe_cli_resume
from persistence.conversation.messages import TranscriptRead
from persistence_assembly import open_persistence, profile_scope
from tests.waiting import wait_until

SESSION_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
OTHER_SESSION = "9f8e7d6c-5b4a-4392-8170-6f5e4d3c2b1a"
LEGACY_SHORT = "ab12cd34"
LEGACY_FULL = "7a6b5c4d-3e2f-4a1b-8c9d-0e1f2a3b4c5d"
BASELINE = [("user", "fix the flaky test"), ("assistant", "Which one?")]


# --------------------------------------------------------------------------------------------------
# stub CLI
# --------------------------------------------------------------------------------------------------
_STUB_BODY = r'''
import glob
import hashlib
import json
import os
import sys
import time


def parse(argv):
    parsed = {"prompt": None, "resume": None, "workdir": None, "output": None}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-p", "--prompt"):
            parsed["prompt"] = argv[index + 1]
            index += 2
        elif item == "--resume":
            parsed["resume"] = argv[index + 1]
            index += 2
        elif item == "--workdir":
            parsed["workdir"] = argv[index + 1]
            index += 2
        elif item == "--output":
            parsed["output"] = argv[index + 1]
            index += 2
        else:
            index += 1
    return parsed


def session_dir(store, session_id):
    unified = os.path.join(store, "unified", session_id)
    if os.path.isdir(unified):
        return unified, "unified"
    for candidate in sorted(glob.glob(os.path.join(store, "session_*_" + session_id))):
        if os.path.isdir(candidate):
            return candidate, "legacy"
    return None, None


def records(path):
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    except OSError:
        pass
    return out


def last_record(directory):
    paths = sorted(glob.glob(os.path.join(directory, "journal", "*.jsonl")))
    for path in reversed(paths):
        found = records(path)
        if found:
            return found[-1]
    return {}


def append_records(directory, mode, entries, base_sequence):
    paths = sorted(glob.glob(os.path.join(directory, "journal", "*.jsonl")))
    if mode == "new_file" or not paths:
        path = os.path.join(directory, "journal", "%016d.jsonl" % (base_sequence + 1))
        previous = str(last_record(directory).get("record_sha256") or "")
    else:
        path = paths[-1]
        previous = str(last_record(directory).get("record_sha256") or "")
    if mode == "interleave":
        previous = "0" * 64                      # a second writer that does not chain onto the tail
    with open(path, "a", encoding="utf-8") as handle:
        sequence = base_sequence
        for entry in entries:
            sequence += 1
            body = {"payload": {"delta": [{"entry": entry}]}, "type": "projection_delta",
                    "previous_record_sha256": previous, "sequence": sequence}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
            body["record_sha256"] = digest
            handle.write(json.dumps(body) + "\n")
            previous = digest
    return os.path.basename(path)


def main():
    argv = sys.argv[1:]
    parsed = parse(argv)
    record = {"argv": argv, "cwd": os.getcwd(), "parsed": parsed,
              "vibe_home": os.environ.get("VIBE_HOME"), "pid": os.getpid()}
    log = os.environ.get("FAKE_CALL_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    mode = os.environ.get("FAKE_MODE", "same_file")
    reply = os.environ.get("FAKE_REPLY", "Fixed the flaky test.")
    session_id = parsed["resume"] or ""
    store = os.path.join(os.environ.get("VIBE_HOME", ""), "logs", "session")
    directory, layout = session_dir(store, session_id)
    if directory is None:
        sys.stderr.write("Failed to resume session: malformed response.\n")
        return 2
    if mode == "sleep":
        time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
        return 0
    if mode == "fail":
        sys.stderr.write(os.environ.get("FAKE_STDERR", "the provider rejected the run"))
        return int(os.environ.get("FAKE_EXIT", "1"))
    reported = session_id
    if mode == "fork":
        reported = os.environ.get("FAKE_FORK_SESSION", "")
    if layout == "legacy":
        if mode != "noreply":
            with open(os.path.join(directory, "messages.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": parsed["prompt"]}) + "\n")
                handle.write(json.dumps({"role": "assistant", "content": reply}) + "\n")
        print(json.dumps({"sessionId": reported, "turnId": "turn-legacy",
                          "messages": [{"role": "assistant",
                                        "content": [{"type": "text", "text": reply}]}]}))
        return 0
    base_sequence = int(last_record(directory).get("sequence") or 0)
    stamp = int(time.time() * 1000)
    entries = [
        {"id": "user-%d" % (base_sequence + 1), "sessionId": session_id,
         "turnId": "turn-%d" % (base_sequence + 1), "role": "user", "type": "message",
         "createdAt": stamp, "content": [{"type": "text", "text": parsed["prompt"]}]},
        {"id": "assistant-%d" % (base_sequence + 2), "sessionId": session_id,
         "turnId": "turn-%d" % (base_sequence + 1), "role": "assistant", "type": "message",
         "createdAt": stamp + 1, "content": [{"type": "text", "text": reply}]},
    ]
    if mode != "noreply":
        append_records(directory, mode, entries, base_sequence)
    print(json.dumps({"sessionId": reported, "turnId": "turn-%d" % (base_sequence + 1),
                      "messages": entries}))
    return 0


sys.exit(main())
'''


def _stub_cli(tmp_path: Path) -> str:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    script = binary_dir / "vibe-stub.py"
    script.write_text(_STUB_BODY, encoding="utf-8")
    shim = binary_dir / "vibe"
    shim.write_text(
        '#!/bin/sh\n'
        'if [ -n "$FAKE_PID_FILE" ]; then printf "%s" "$$" > "$FAKE_PID_FILE"; fi\n'
        f'exec "{sys.executable}" "{script}" "$@"\n',
        encoding="utf-8")
    shim.chmod(0o755)
    return str(shim)


# --------------------------------------------------------------------------------------------------
# store fixtures
# --------------------------------------------------------------------------------------------------
def _journal_record(sequence: int, previous: str, role: str, text: str, session_id: str) -> dict:
    entry = {"id": f"{role}-{sequence}", "sessionId": session_id, "turnId": f"turn-{sequence}",
             "role": role, "type": "message", "createdAt": sequence,
             "content": [{"type": "text", "text": text}]}
    body = {"payload": {"delta": [{"entry": entry}]}, "type": "projection_delta",
            "previous_record_sha256": previous, "sequence": sequence}
    body["record_sha256"] = f"sha{sequence}"
    return body


def _write_unified(root: Path, session_id: str, cwd: Path, baseline=BASELINE) -> Path:
    session = root / "unified" / session_id
    (session / "journal").mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({
        "session_id": session_id, "title": "Vibe refactor", "model": "devstral-2512",
        "environment": {"working_directory": str(cwd)}, "bumped_at": 1_700_000_000,
    }), encoding="utf-8")
    lines, previous = [], ""
    for index, (role, text) in enumerate(baseline, start=1):
        record = _journal_record(index, previous, role, text, session_id)
        previous = record["record_sha256"]
        lines.append(json.dumps(record))
    (session / "journal" / "0000000000000001.jsonl").write_text("\n".join(lines) + "\n",
                                                                encoding="utf-8")
    return session


def _write_legacy(root: Path, short: str, cwd: Path) -> Path:
    session = root / f"session_20260101T120000_{short}"
    session.mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({
        "session_id": LEGACY_FULL, "title": "Legacy vibe chat", "cwd": str(cwd),
        "model": "mistral-large-latest",
    }), encoding="utf-8")
    (session / "messages.jsonl").write_text("\n".join(
        json.dumps({"role": role, "content": text}) for role, text in BASELINE) + "\n",
        encoding="utf-8")
    return session


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def store_root(tmp_path, workspace):
    root = tmp_path / "vibe" / "logs" / "session"
    _write_unified(root, SESSION_ID, workspace)
    _write_legacy(root, LEGACY_SHORT, workspace)
    return root


@pytest.fixture
def root_patch(monkeypatch, store_root):
    monkeypatch.setattr(mistral_linked_threads, "resolve_vibe_session_root",
                        lambda profile_home=None: store_root)
    return store_root


@pytest.fixture
def cli(tmp_path):
    return _stub_cli(tmp_path)


@pytest.fixture
def call_log(tmp_path):
    return tmp_path / "calls.jsonl"


@pytest.fixture
def config(cli):
    return {"model": {"vibe_bin": cli}}


@pytest.fixture
def db(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path / "profile"))
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def linked(db, root_patch):
    return mistral_linked_threads.link_mistral_thread(SESSION_ID, db, "default")["session_id"]


def _run(store, session_id, prompt, *, config, call_log, tmp_path,
         mode=None, reply=None, timeout=120.0, **kwargs):
    base_env = {"FAKE_CALL_LOG": str(call_log)}
    if mode:
        base_env["FAKE_MODE"] = mode
    if reply:
        base_env["FAKE_REPLY"] = reply
    base_env["FAKE_FORK_SESSION"] = OTHER_SESSION
    return resume.run_linked_vibe_turn(
        store=store, session_id=session_id, prompt=prompt, config=config,
        lock_dir=tmp_path / "locks", base_env=base_env, timeout=timeout, **kwargs)


def _calls(call_log) -> list[dict]:
    if not call_log.exists():
        return []
    return [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines() if line]


def _journal_files(session_dir: Path) -> list[Path]:
    return sorted((session_dir / "journal").glob("*.jsonl"))


def _journal_rel(session_dir: Path) -> list[str]:
    return [str(path.relative_to(session_dir)) for path in _journal_files(session_dir)]


def _journal_records(session_dir: Path) -> list[dict]:
    rows = []
    for path in _journal_files(session_dir):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _session_dirs(root: Path) -> list[str]:
    unified = root / "unified"
    if not unified.is_dir():
        return []
    return sorted(path.name for path in unified.iterdir() if path.is_dir())


# --------------------------------------------------------------------------------------------------
# no fork: the same native thread gains the turn
# --------------------------------------------------------------------------------------------------
def test_send_appends_to_the_same_native_thread(db, linked, store_root, config, root_patch,
                                                call_log, tmp_path, workspace):
    session_dir = store_root / "unified" / SESSION_ID
    before_files = _journal_files(session_dir)
    before_records = _journal_records(session_dir)
    assert _session_dirs(store_root) == [SESSION_ID]
    assert mistral_linked_history.linked_mistral_session_id(db, linked) == SESSION_ID

    prompt = "fix the flaky test in tests/test_api.py"
    turn = _run(db, linked, prompt, config=config, call_log=call_log,
                tmp_path=tmp_path, reply="Patched the retry loop.")

    # 1. The native identity is an input and an output: same id, before and after, in every place.
    assert turn.native_session_id == SESSION_ID
    assert mistral_linked_history.linked_mistral_session_id(db, linked) == SESSION_ID
    calls = _calls(call_log)
    assert len(calls) == 1
    assert calls[0]["parsed"]["resume"] == SESSION_ID
    assert calls[0]["argv"] == ["-p", prompt, "--resume", SESSION_ID,
                               "--workdir", str(workspace), "--output", "json"]
    assert calls[0]["cwd"] == str(workspace)
    assert calls[0]["vibe_home"] == str(store_root.parent.parent)

    # 2. No fork: still exactly one session directory, and the existing journal grew.
    assert _session_dirs(store_root) == [SESSION_ID]
    after_records = _journal_records(session_dir)
    assert len(after_records) == len(before_records) + 2
    assert all(record["payload"]["delta"][0]["entry"]["sessionId"] == SESSION_ID
               for record in after_records[len(before_records):])
    assert turn.journal_files == (str(before_files[-1].relative_to(session_dir)),)
    assert turn.appended_records == 2

    # 3. The returned rows are the native rows, not a Hermes copy.
    assert turn.messages == [{"role": "user", "content": prompt},
                             {"role": "assistant", "content": "Patched the retry loop."}]
    assert turn.assistant_text == "Patched the retry loop."
    assert turn.turn_id == "turn-3"
    history = mistral_linked_threads.read_mistral_history(SESSION_ID, limit=50)
    assert [(row["role"], row["content"]) for row in history["messages"]] == [
        *BASELINE, ("user", prompt), ("assistant", "Patched the retry loop.")]
    assert list(db.messages.read_transcript(linked, TranscriptRead())) == []


def test_the_cli_is_discovered_on_path_and_still_appends_in_place(
        db, linked, store_root, cli, root_patch, call_log, tmp_path, monkeypatch):
    # The requirement is a fake `vibe` on PATH, so this run resolves a bare `vibe` through
    # shutil.which exactly the way a real install would, and still must land in the same journal.
    monkeypatch.setenv("PATH", os.pathsep.join([str(Path(cli).parent), os.environ.get("PATH", "")]))
    session_dir = store_root / "unified" / SESSION_ID
    before = len(_journal_records(session_dir))
    turn = resume.run_linked_vibe_turn(
        store=db, session_id=linked, prompt="found on PATH", config={},
        lock_dir=tmp_path / "locks",
        base_env={"FAKE_CALL_LOG": str(call_log)}, timeout=120)
    assert vibe_cli_resume.resolve_vibe_binary({}) == cli
    assert _calls(call_log)[0]["parsed"]["resume"] == SESSION_ID
    assert turn.native_session_id == SESSION_ID
    assert _session_dirs(store_root) == [SESSION_ID]
    assert len(_journal_records(session_dir)) == before + 2


def test_two_sends_keep_one_session_and_one_transcript(db, linked, store_root, config, root_patch,
                                                       call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    first = _run(db, linked, "first message", config=config, 
                 call_log=call_log, tmp_path=tmp_path, reply="first reply")
    records_after_first = len(_journal_records(session_dir))
    second = _run(db, linked, "second message", config=config, 
                  call_log=call_log, tmp_path=tmp_path, reply="second reply")

    assert [turn.native_session_id for turn in (first, second)] == [SESSION_ID, SESSION_ID]
    assert _session_dirs(store_root) == [SESSION_ID]
    assert len(_journal_records(session_dir)) == records_after_first + 2
    assert len(_calls(call_log)) == 2
    assert [call["parsed"]["resume"] for call in _calls(call_log)] == [SESSION_ID, SESSION_ID]
    assert list(db.messages.read_transcript(linked, TranscriptRead())) == []


def test_a_new_journal_file_in_the_same_session_dir_is_not_a_fork(db, linked, store_root, config,
                                                                  root_patch, call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    turn = _run(db, linked, "compact and continue", config=config, 
                call_log=call_log, tmp_path=tmp_path, mode="new_file")
    assert _session_dirs(store_root) == [SESSION_ID]
    assert len(_journal_files(session_dir)) == 2
    assert turn.journal_files == (_journal_rel(session_dir)[-1],)
    assert turn.messages[-1] == {"role": "assistant", "content": "Fixed the flaky test."}


def test_legacy_readonly_row_becomes_resumable_without_changing_its_mode(
        db, store_root, config, root_patch, call_log, tmp_path):
    linked = mistral_linked_threads.link_mistral_thread(LEGACY_SHORT, db, "default")["session_id"]
    row = db.sessions.get(linked)
    assert row.model_config["api_mode"] == "mistral_linked_readonly"
    before = dict(row.model_config)

    turn = _run(db, linked, "still resumable", config=config, 
                call_log=call_log, tmp_path=tmp_path, reply="Yes, legacy too.")

    assert turn.native_session_id == LEGACY_SHORT
    assert turn.messages == [{"role": "user", "content": "still resumable"},
                             {"role": "assistant", "content": "Yes, legacy too."}]
    assert _calls(call_log)[0]["parsed"]["resume"] == LEGACY_SHORT
    assert not (store_root / "unified").joinpath(LEGACY_SHORT).exists()
    assert db.sessions.get(linked).model_config == before
    legacy_dir = store_root / f"session_20260101T120000_{LEGACY_SHORT}"
    assert [json.loads(line)["content"] for line in
            (legacy_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()[2:]] == [
        "still resumable", "Yes, legacy too."]
    history = mistral_linked_threads.read_mistral_history(LEGACY_SHORT, limit=50)
    assert len(history["messages"]) == 4


# --------------------------------------------------------------------------------------------------
# conflicts: another writer, nothing appended, a forked answer
# --------------------------------------------------------------------------------------------------
def test_a_foreign_writer_breaks_the_journal_chain_and_is_a_typed_conflict(
        db, linked, store_root, config, root_patch, call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    before = _journal_records(session_dir)
    with pytest.raises(resume.LinkedVibeConflict) as conflict:
        _run(db, linked, "racing send", config=config, call_log=call_log,
             tmp_path=tmp_path, mode="interleave")
    assert "Another writer appended" in str(conflict.value)
    assert isinstance(conflict.value, vibe_cli_resume.VibeResumeError)
    # The stub still wrote records -- they simply do not chain onto our fingerprint.
    assert len(_journal_records(session_dir)) == len(before) + 2
    assert list(db.messages.read_transcript(linked, TranscriptRead())) == []


def test_a_run_that_appends_nothing_is_a_conflict_not_a_phantom_turn(
        db, linked, store_root, config, root_patch, call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    before = len(_journal_records(session_dir))
    with pytest.raises(resume.LinkedVibeConflict) as conflict:
        _run(db, linked, "go nowhere", config=config, call_log=call_log,
             tmp_path=tmp_path, mode="noreply")
    assert "without appending" in str(conflict.value)
    assert len(_journal_records(session_dir)) == before
    assert list(db.messages.read_transcript(linked, TranscriptRead())) == []


def test_a_run_that_reports_another_session_is_refused_as_a_fork(
        db, linked, store_root, config, root_patch, call_log, tmp_path):
    with pytest.raises(resume.LinkedVibeConflict) as conflict:
        _run(db, linked, "fork me", config=config, call_log=call_log,
             tmp_path=tmp_path, mode="fork")
    assert "refusing a forked thread" in str(conflict.value)
    assert _session_dirs(store_root) == [SESSION_ID]


def test_the_native_lease_blocks_a_session_still_open_in_a_vibe_terminal(
        db, linked, store_root, config, root_patch, call_log, tmp_path):
    active = store_root / "active"
    active.mkdir()
    lease = active / f"{SESSION_ID}.lock"
    lease.write_text("", encoding="utf-8")
    (active / f"{SESSION_ID}.lock.json").write_text(json.dumps(
        {"session_id": SESSION_ID, "process_id": 4242}), encoding="utf-8")
    handle = os.open(lease, os.O_RDWR)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resume.LinkedVibeConflict) as conflict:
            _run(db, linked, "hello", config=config, call_log=call_log,
                 tmp_path=tmp_path)
        assert "open in another Vibe process" in str(conflict.value)
        assert "4242" in str(conflict.value)
        assert _calls(call_log) == []                       # nothing was spawned
        ready, reason = resume.check_linked_vibe_send(db, linked, config=config)
        assert ready is False and "another Vibe process" in reason
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    # Once the terminal releases the lease the same send goes through.
    turn = _run(db, linked, "hello", config=config, call_log=call_log,
                tmp_path=tmp_path)
    assert turn.native_session_id == SESSION_ID


def test_the_hermes_turn_lock_serializes_sends_on_one_thread(db, linked, store_root, config,
                                                             root_patch, call_log, tmp_path):
    thread = resume.linked_vibe_thread(db, linked)
    lock_path = resume._turn_lock_path(tmp_path / "locks", thread.lease_key)
    lock_path.parent.mkdir(parents=True)
    held = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(resume.LinkedVibeConflict) as conflict:
            _run(db, linked, "concurrent send", config=config, 
                 call_log=call_log, tmp_path=tmp_path, lock_wait_seconds=0.0)
        assert "already running on this Vibe thread" in str(conflict.value)
        assert _calls(call_log) == []
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


# --------------------------------------------------------------------------------------------------
# failures: typed, no crash, no phantom session
# --------------------------------------------------------------------------------------------------
def test_a_cli_failure_surfaces_typed_and_leaves_the_store_untouched(
        db, linked, store_root, config, root_patch, call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    before = (len(_journal_records(session_dir)), session_dir.stat().st_mtime_ns)
    base_env = {"FAKE_CALL_LOG": str(call_log), "FAKE_MODE": "fail", "FAKE_EXIT": "2",
                "FAKE_STDERR": "provider said no"}
    with pytest.raises(vibe_cli_resume.VibeRunFailed) as failed:
        resume.run_linked_vibe_turn(store=db, session_id=linked, prompt="do it", config=config,
                                    lock_dir=tmp_path / "locks", base_env=base_env, timeout=120)
    assert failed.value.returncode == 2
    assert "exited 2" in str(failed.value)
    assert "provider said no" in str(failed.value)
    assert (len(_journal_records(session_dir)), session_dir.stat().st_mtime_ns) == before
    assert _session_dirs(store_root) == [SESSION_ID]
    assert list(db.messages.read_transcript(linked, TranscriptRead())) == []


def test_a_run_needing_an_approval_fails_instead_of_approving(
        db, linked, config, root_patch, call_log, tmp_path):
    base_env = {"FAKE_CALL_LOG": str(call_log), "FAKE_MODE": "fail", "FAKE_EXIT": "1",
                "FAKE_STDERR": "needs approval for tool 'bash', which a headless run cannot give"}
    with pytest.raises(vibe_cli_resume.VibeApprovalRequired):
        resume.run_linked_vibe_turn(store=db, session_id=linked, prompt="rm -rf build",
                                    config=config, lock_dir=tmp_path / "locks",
                                    base_env=base_env, timeout=120)
    argv = _calls(call_log)[0]["argv"]
    assert not {"--auto-approve", "--yolo", "--smart-approve"} & set(argv)
    assert "--approval-policy" not in argv


def test_a_missing_cli_is_a_typed_refusal_and_spawns_nothing(db, linked, call_log, tmp_path):
    with pytest.raises(resume.LinkedVibeUnavailable) as unavailable:
        resume.run_linked_vibe_turn(
            store=db, session_id=linked, prompt="hello",
            config={"model": {"vibe_bin": str(tmp_path / "missing-vibe")}},
            lock_dir=tmp_path / "locks", base_env={"FAKE_CALL_LOG": str(call_log)}, timeout=30)
    assert "was not found" in str(unavailable.value)
    assert _calls(call_log) == []


def test_a_timeout_kills_the_child_and_appends_nothing(db, linked, store_root, config, root_patch,
                                                       call_log, tmp_path):
    session_dir = store_root / "unified" / SESSION_ID
    before = len(_journal_records(session_dir))
    pid_file = tmp_path / "pid"
    base_env = {"FAKE_CALL_LOG": str(call_log), "FAKE_MODE": "sleep",
                "FAKE_PID_FILE": str(pid_file)}
    with pytest.raises(vibe_cli_resume.VibeRunTimeout):
        resume.run_linked_vibe_turn(store=db, session_id=linked, prompt="slow one",
                                    config=config, lock_dir=tmp_path / "locks",
                                    base_env=base_env, timeout=1.5)
    assert len(_journal_records(session_dir)) == before
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        wait_until(lambda: _pid_gone(pid), timeout=15, what="the timed-out Vibe child to be reaped")
    except AssertionError:  # pragma: no cover - would mean we leaked a child
        pytest.fail("the timed-out Vibe child was left running")


def _pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_blank_prompt_is_refused_before_any_spawn(db, linked, config, call_log, tmp_path):
    with pytest.raises(resume.LinkedVibeUnavailable, match="message is required"):
        resume.run_linked_vibe_turn(store=db, session_id=linked, prompt="   ", config=config,
                                    lock_dir=tmp_path / "locks",
                                    base_env={"FAKE_CALL_LOG": str(call_log)})
    assert _calls(call_log) == []


def test_an_unlinked_row_is_refused(db, config):
    ready, reason = resume.check_linked_vibe_send(db, "not-a-linked-session", config=config)
    assert ready is False and "not a linked Mistral Vibe thread" in reason
    with pytest.raises(resume.LinkedVibeUnavailable):
        resume.run_linked_vibe_turn(store=db, session_id="not-a-linked-session", prompt="hi",
                                    config=config)


def test_check_linked_vibe_send_reports_readiness(db, linked, config):
    ready, reason = resume.check_linked_vibe_send(db, linked, config=config)
    assert ready is True and SESSION_ID in reason


# --------------------------------------------------------------------------------------------------
# store resolution and the symlink guard (P3)
# --------------------------------------------------------------------------------------------------
def test_the_session_root_comes_from_vibe_home_and_pins_the_child(
        tmp_path, workspace, db, monkeypatch):
    vibe_home = tmp_path / "vibe-home"
    store_root = vibe_home / "logs" / "session"
    _write_unified(store_root, SESSION_ID, workspace)

    def fake_secret(name, default=None):
        return str(vibe_home) if name == "VIBE_HOME" else (None if name == "MISTRAL_VIBE_SESSION_ROOT" else default)

    monkeypatch.setattr("agent.secret_scope.get_secret", fake_secret)
    assert mistral_linked_threads.resolve_vibe_session_root() == store_root.resolve()
    assert resume.child_env_for_vibe_store(store_root)["VIBE_HOME"] == str(store_root.resolve().parent.parent)

    linked = mistral_linked_threads.link_mistral_thread(SESSION_ID, db, "default")["session_id"]
    call_log = tmp_path / "calls.jsonl"
    turn = resume.run_linked_vibe_turn(
        store=db, session_id=linked, prompt="through the real resolver",
        config={"model": {"vibe_bin": _stub_cli(tmp_path)}}, lock_dir=tmp_path / "locks",
        base_env={"FAKE_CALL_LOG": str(call_log)}, timeout=120)
    assert turn.native_session_id == SESSION_ID
    assert _calls(call_log)[0]["vibe_home"] == str(store_root.resolve().parent.parent)
    assert _session_dirs(store_root) == [SESSION_ID]


def test_find_session_dir_refuses_a_symlink_that_escapes_the_store(tmp_path, workspace):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    (outside / "unified" / SESSION_ID / "journal").mkdir(parents=True)
    (outside / "unified" / SESSION_ID / "meta.json").write_text(
        json.dumps({"session_id": SESSION_ID, "environment": {"working_directory": str(workspace)}}),
        encoding="utf-8")
    (root / "unified").mkdir(parents=True)
    (root / "unified" / SESSION_ID).symlink_to(outside / "unified" / SESSION_ID)

    with pytest.raises(ValueError, match="escapes the Vibe session store"):
        mistral_linked_threads._find_session_dir(root, SESSION_ID)


def test_find_session_dir_refuses_an_escaping_legacy_symlink(tmp_path):
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / f"session_20260101T120000_{LEGACY_SHORT}").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes the Vibe session store"):
        mistral_linked_threads._find_session_dir(root, LEGACY_SHORT)


def test_find_session_dir_allows_a_symlink_that_stays_inside_the_store(tmp_path):
    root = tmp_path / "store"
    real = root / "unified" / OTHER_SESSION
    (real / "journal").mkdir(parents=True)
    (root / "unified" / SESSION_ID).symlink_to(real)
    found = mistral_linked_threads._find_session_dir(root, SESSION_ID)
    assert found == real.resolve()


def test_find_session_dir_rejects_a_missing_session(tmp_path):
    root = tmp_path / "store"
    (root / "unified").mkdir(parents=True)
    with pytest.raises(ValueError, match="did not return the requested session"):
        mistral_linked_threads._find_session_dir(root, SESSION_ID)


def test_the_write_path_uses_the_same_symlink_guard(db, linked, store_root, workspace, config,
                                                    call_log, tmp_path):
    outside = tmp_path / "outside"
    _write_unified(outside, SESSION_ID, workspace)
    escaping = store_root / "unified" / SESSION_ID
    shutil.rmtree(escaping)
    escaping.symlink_to(outside / "unified" / SESSION_ID)

    with pytest.raises(resume.LinkedVibeUnavailable) as unavailable:
        _run(db, linked, "escape", config=config, call_log=call_log, tmp_path=tmp_path)
    assert "escapes the Vibe session store" in str(unavailable.value)
    assert _calls(call_log) == []
