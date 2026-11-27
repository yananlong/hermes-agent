"""A linked Codex thread runs through a real gateway and Desktop RPC connection."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

import pytest

from tests.e2e.core.parity._drive_rpc import first_stdout_line, spawn_serve, ws_rpc
from tests.e2e.core.parity._helpers import ParityHome, stop_profile_gateway, terminate
from tests.e2e.core.providers._native_helpers import REPO_ROOT, make_home
from tests.fakes.providers.codex_app_server import FakeCodex
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.local_recovery_probe import daemon, rpc, websocket


pytestmark = pytest.mark.platforms("posix")

THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
SID = "20261126_000000_deadbeef0001"


def _linked_home(tmp_path):
    from persistence_assembly import open_persistence, profile_scope

    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    native = make_home(tmp_path, {"provider": "openai", "default": "gpt-6-luna", "codex_bin": str(fake.bin)})
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(native.project), "turns": []}},
        "turn_cursor": 0,
    }))
    with open_persistence(profile_scope(home=native.hermes_home)) as db:
        seed_session(db, SID, "desktop", cwd=str(native.project), model="gpt-6-luna", model_config={
            "codex_linked_thread": True, "codex_thread_id": THREAD,
            "provider": "openai", "api_mode": "codex_app_server",
        })
    return native, fake


def test_legacy_resume_with_unavailable_native_history(tmp_path):
    """The legacy WebSocket history fallback does not crash while logging an unavailable native store."""
    native, _fake = _linked_home(tmp_path)

    ph = ParityHome(root=tmp_path, home=native.home, hermes_home=native.hermes_home,
                    project=native.project, markers=tmp_path / "markers", pid_log=tmp_path / "pids",
                    tag=uuid.uuid4().hex)
    sp = None
    try:
        sp = spawn_serve(ph)
        deadline = time.monotonic() + 90
        port = None
        while port is None and time.monotonic() < deadline:
            line = first_stdout_line(sp, timeout=max(0.1, deadline - time.monotonic()))
            match = re.search(r"HERMES_BACKEND_READY port=(\d+)", line)
            port = int(match.group(1)) if match else None
        assert port is not None, f"backend did not report a port: {sp.cap.stderr[-3000:]}"
        rpc, close = ws_rpc(port, sp.token)
        try:
            rpc.wait_event("gateway.ready", timeout=90)
            resumed = rpc.call("session.resume", {"session_id": SID}, timeout=90)
            assert resumed["resumed"] == SID
            assert resumed["messages"] == []
        finally:
            close()
    finally:
        if sp is not None:
            terminate(sp.proc, timeout=20)
        stop_profile_gateway(ph, timeout=30)


def test_linked_thread_resume_and_submit_through_real_gateway_without_api_key(tmp_path):
    """The authority admits one send into the original native thread, with no HTTP API key."""
    native, fake = _linked_home(tmp_path)
    (native.home / ".codex").mkdir()
    env = native.env()

    async def exercise(desc):
        async with websocket(native.hermes_home, desc) as ws:
            resumed = await rpc(ws, "session.resume", session_id=SID)
            assert resumed.get("result", {}).get("session_id") == SID, resumed
            await ws.send(json.dumps({"id": "submit", "method": "prompt.submit", "params": {
                "session_id": SID, "input_id": "native-input", "text": "hello"}}))
            receipt = complete = None
            async with asyncio.timeout(90):
                while receipt is None or complete is None:
                    frame = json.loads(await ws.recv())
                    if frame.get("id") == "submit":
                        receipt = frame
                    event = frame.get("params") or {}
                    if frame.get("method") == "event" and event.get("type") == "message.complete":
                        if event.get("session_id") == SID:
                            complete = event
            assert receipt.get("result", {}).get("status") in {"streaming", "queued"}, receipt
            assert (complete.get("payload") or {}).get("text") == "native answer", complete

    with daemon(REPO_ROOT, native.hermes_home, env, barrier=False) as (proc, desc):
        try:
            asyncio.run(exercise(desc))
            assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("thread/resume")] == [THREAD]
            assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("turn/start")] == [THREAD]
            assert not fake.requests("thread/start")
            assert len(fake.threads()[THREAD]["turns"]) == 1
            fake.assert_wire_clean()
        finally:
            proc.terminate()
            proc.wait(timeout=30)


def _serve_port(sp) -> int:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        line = first_stdout_line(sp, timeout=max(0.1, deadline - time.monotonic()))
        if match := re.search(r"HERMES_BACKEND_READY port=(\d+)", line):
            return int(match.group(1))
    raise AssertionError(f"backend did not report a port: {sp.cap.stderr[-3000:]}")


def test_serve_sends_into_a_linked_thread_whose_codex_model_is_unknown(tmp_path):
    """Electron's in-process backend: Codex's thread/read may report no model (nullable in its schema),
    so the pointer is linked with none; the send must still run on the app-server, in the same thread."""
    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    native = make_home(tmp_path, {"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
                                  "default": "gpt-6-luna", "codex_bin": str(fake.bin)})
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(native.project), "turns": []}},
        "turn_cursor": 0,
    }))
    (native.home / ".codex").mkdir()
    ph = ParityHome(root=tmp_path, home=native.home, hermes_home=native.hermes_home,
                    project=native.project, markers=tmp_path / "markers", pid_log=tmp_path / "pids",
                    tag=uuid.uuid4().hex)
    sp = None
    try:
        sp = spawn_serve(ph)
        rpc, close = ws_rpc(_serve_port(sp), sp.token)
        try:
            rpc.wait_event("gateway.ready", timeout=90)
            sid = rpc.call("session.codex.link", {"thread_id": THREAD}, timeout=90)["session_id"]
            live = rpc.call("session.resume", {"session_id": sid}, timeout=90).get("session_id") or sid
            rpc.call("prompt.submit", {"session_id": live, "text": "hello"}, timeout=90)
            done = rpc.wait_event("message.complete", lambda e: e.get("session_id") == live, timeout=90)
            assert (done.get("payload") or {}).get("text") == "native answer", done
        finally:
            close()
        assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("turn/start")] == [THREAD]
        assert not fake.requests("thread/start")
    finally:
        if sp is not None:
            terminate(sp.proc, timeout=20)
        stop_profile_gateway(ph, timeout=30)


@pytest.mark.parametrize(("profile_provider", "env_file", "native_model"), [
    # Picks the wire per model, so a profile-resolved route would rewrite the app-server wire.
    ("opencode-go", {"OPENCODE_GO_API_KEY": "placeholder-not-a-real-key"}, None),
    # Has no Hermes login, so profile resolution alone would refuse before any agent exists.
    ("nous", {}, "gpt-5.5"),
])
def test_serve_never_resolves_a_linked_thread_through_the_profile_provider(tmp_path, profile_provider, env_file,
                                                                            native_model):
    """A thread whose Codex provider is not ``openai`` is linked with no Hermes provider at all; the
    serve backend must still run it on the app-server, whatever the profile's own provider needs."""
    from persistence_assembly import open_persistence, profile_scope

    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    native = make_home(tmp_path, {"provider": profile_provider, "default": "profile-model",
                                  "codex_bin": str(fake.bin)}, env_file=env_file)
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(native.project), "turns": []}},
        "turn_cursor": 0,
    }))
    with open_persistence(profile_scope(home=native.hermes_home)) as db:
        seed_session(db, SID, "desktop", cwd=str(native.project), model=native_model, model_config={
            "codex_linked_thread": True, "codex_thread_id": THREAD, "api_mode": "codex_app_server"})
    (native.home / ".codex").mkdir()
    ph = ParityHome(root=tmp_path, home=native.home, hermes_home=native.hermes_home,
                    project=native.project, markers=tmp_path / "markers", pid_log=tmp_path / "pids",
                    tag=uuid.uuid4().hex)
    sp = None
    try:
        sp = spawn_serve(ph)
        rpc, close = ws_rpc(_serve_port(sp), sp.token)
        try:
            rpc.wait_event("gateway.ready", timeout=90)
            live = rpc.call("session.resume", {"session_id": SID}, timeout=90).get("session_id") or SID
            rpc.call("prompt.submit", {"session_id": live, "text": "hello"}, timeout=90)
            done = rpc.wait_event("message.complete", lambda e: e.get("session_id") == live, timeout=90)
            assert (done.get("payload") or {}).get("text") == "native answer", (done, sp.cap.stderr[-3000:])
        finally:
            close()
        assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("turn/start")] == [THREAD]
        assert not fake.requests("thread/start")
    finally:
        if sp is not None:
            terminate(sp.proc, timeout=20)
        stop_profile_gateway(ph, timeout=30)


def test_compress_compacts_the_linked_thread_itself(tmp_path):
    """/compact on a linked session runs Codex's own compaction of the original thread: no HTTP
    summary over the Hermes transcript, and never a new native thread."""
    native, fake = _linked_home(tmp_path)
    (native.home / ".codex").mkdir()
    data = json.loads((fake.state_dir / "threads.json").read_text())
    data["threads"][THREAD]["turns"] = [{"id": "t1", "status": "completed", "items": [
        {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": "q"}]},
        {"type": "agentMessage", "id": "a1", "text": "a"}]}]
    (fake.state_dir / "threads.json").write_text(json.dumps(data))

    async def exercise(desc):
        async with websocket(native.hermes_home, desc) as ws:
            resumed = await rpc(ws, "session.resume", session_id=SID)
            assert resumed.get("result", {}).get("session_id") == SID, resumed
            reply = await rpc(ws, "session.compress", session_id=SID)
            assert reply.get("result", {}).get("status") == "compressed", reply

    with daemon(REPO_ROOT, native.hermes_home, native.env(), barrier=False) as (proc, desc):
        try:
            asyncio.run(exercise(desc))
            assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("thread/compact/start")] == [THREAD]
            assert not fake.requests("thread/start")
            fake.assert_wire_clean()
        finally:
            proc.terminate()
            proc.wait(timeout=30)


def test_serve_rebuilds_a_draft_sessions_agent(tmp_path):
    """A draft has no row until its first prompt; rebuilding its agent (a tool toggle) must not be
    refused by the linked-thread lookup, since a pointer cannot exist without a row."""
    native = make_home(tmp_path, {"provider": "custom", "base_url": "http://127.0.0.1:9/v1",
                                  "default": "gpt-6-luna"})
    ph = ParityHome(root=tmp_path, home=native.home, hermes_home=native.hermes_home,
                    project=native.project, markers=tmp_path / "markers", pid_log=tmp_path / "pids",
                    tag=uuid.uuid4().hex)
    sp = None
    try:
        sp = spawn_serve(ph)
        rpc, close = ws_rpc(_serve_port(sp), sp.token)
        try:
            rpc.wait_event("gateway.ready", timeout=90)
            sid = rpc.call("session.create", {}, timeout=90)["session_id"]
            result = rpc.call("tools.configure", {"session_id": sid, "action": "disable", "names": ["web"]},
                              timeout=90)
            assert result.get("info"), result
        finally:
            close()
    finally:
        if sp is not None:
            terminate(sp.proc, timeout=20)
        stop_profile_gateway(ph, timeout=30)
