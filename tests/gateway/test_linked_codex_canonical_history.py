"""Canonical owner reads and edits treat linked Codex history as native state."""

import pytest

from tests.fixtures.storage_holder import raw_holder
from tests.fixtures.storage_read import session_row, transcript_rows
from types import SimpleNamespace

from tests.gateway.test_session_mutation_model_busy import _owner


pytestmark = pytest.mark.usefixtures("authority_cwd_in_tmp")

def _linked(db, session_id):
    db.sessions.patch_model_config(session_id, {
        "codex_linked_thread": True,
        "codex_thread_id": "01a1c4a4-7a29-7011-8493-04445af16d6e",
    })


def test_ordinary_linked_history_reuses_snapshot_and_refreshes_only_the_tail(tmp_path):
    from gateway.session_linked_codex import read_display_history

    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    turns = [{"id": "turn-1", "status": "completed", "items": [
        {"type": "userMessage", "id": "user-1", "content": [{"type": "text", "text": "first"}]},
        {"type": "agentMessage", "id": "answer-1", "text": "initial answer"},
    ]}]

    class Client:
        calls = []

        def request(self, method, params):
            assert method == "thread/turns/list"
            self.calls.append(params["sortDirection"])
            snapshot = list(turns)
            if params["sortDirection"] == "desc":
                snapshot.reverse()
            return {"data": snapshot, "nextCursor": None}

    client = Client()
    overlays = {
        "turn-1": {"version": 1, "thread_id": thread_id, "turn_id": "turn-1", "held": False,
                   "final_visible": True, "final_text": "initial answer", "platform": "local"},
    }
    kwargs = {"session_id": "linked-cache-test", "profile_home": tmp_path, "client": client,
              "display_overlays": overlays}
    first = read_display_history(thread_id, **kwargs)
    turns.append({"id": "turn-2", "status": "completed", "items": [
        {"type": "userMessage", "id": "user-2", "content": [{"type": "text", "text": "second"}]},
        {"type": "agentMessage", "id": "answer-2", "text": "new answer"},
    ]})
    overlays["turn-2"] = {"version": 1, "thread_id": thread_id, "turn_id": "turn-2", "held": False,
                          "final_visible": True, "final_text": "new answer", "platform": "local"}
    second = read_display_history(thread_id, **kwargs)

    assert [row["content"] for row in first] == ["first", "initial answer"]
    assert [row["content"] for row in second] == ["first", "initial answer", "second", "new answer"]
    assert client.calls == ["asc", "desc"]


@pytest.mark.asyncio
async def test_canonical_resume_history_and_transcript_read_codex_native_turns(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    native = [
        {"role": "user", "content": "from Codex CLI", "timestamp": 10},
        {"role": "assistant", "content": "native answer", "timestamp": 11},
        {"role": "user", "content": "second prompt", "timestamp": 12},
    ]
    _linked(db, ref.session_id)
    from gateway.session_linked_codex import linked_thread_id
    assert linked_thread_id(db, ref.session_id) == "01a1c4a4-7a29-7011-8493-04445af16d6e"
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history",
                        lambda _thread, **_kwargs: list(native))
    try:
        resumed = await owner.dispatch({"id": 1, "method": "session.resume", "params": {
            "session_id": ref.session_id, "display_projection": True}})
        assert "result" in resumed, resumed
        assert resumed["result"]["message_count"] == 3, resumed
        assert [row["content"] for row in resumed["result"]["messages"]] == [
            "from Codex CLI", "native answer", "second prompt"]
        assert [row["text"] for row in resumed["result"]["display_messages"]] == [
            "from Codex CLI", "native answer", "second prompt"]

        history = await owner.dispatch({"id": 2, "method": "session.history", "params": {
            "session_id": ref.session_id}})
        assert [row["text"] for row in history["result"]["messages"]] == [
            "from Codex CLI", "native answer", "second prompt"]

        first = await owner.dispatch({"id": 3, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "limit": 2}})
        assert [row["text"] for row in first["result"]["rows"]] == ["from Codex CLI", "native answer"]
        cursor = first["result"]["next_cursor"]
        native.append({"role": "assistant", "content": "appended in Codex", "timestamp": 13})
        appended = await owner.dispatch({"id": 4, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "limit": 2, "cursor": cursor}})
        assert appended["result"]["reset"] is False
        assert [row["text"] for row in appended["result"]["rows"]] == [
            "second prompt", "appended in Codex"]

        cursor = appended["result"]["next_cursor"]
        native[0] = {**native[0], "content": "rewritten by Codex"}
        reset = await owner.dispatch({"id": 5, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "limit": 2, "cursor": cursor}})
        assert reset["result"]["reset"] is True
        assert [row["text"] for row in reset["result"]["rows"]] == [
            "rewritten by Codex", "native answer"]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_linked_resume_can_attach_without_reading_native_history(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    reads = []

    def native_history(_thread, **_kwargs):
        reads.append(1)
        return [{"role": "user", "content": "native message"}]

    monkeypatch.setattr("gateway.session_linked_codex.read_display_history", native_history)
    try:
        resumed = await owner.dispatch({"id": 1, "method": "session.resume", "params": {
            "session_id": ref.session_id, "omit_messages": True}})
        assert "result" in resumed, resumed
        assert resumed["result"]["messages_omitted"] is True
        assert resumed["result"]["messages"] == []
        assert resumed["result"]["message_count"] is None
        assert not reads

    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_latest_native_history_pages_without_reading_the_entire_thread(tmp_path, monkeypatch):
    from contextlib import nullcontext

    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)

    def turn(number):
        return {"id": f"turn-{number}", "status": "completed", "items": [
            {"type": "userMessage", "id": f"user-{number}", "content": [
                {"type": "text", "text": f"question {number}"}]},
        ]}

    class Native:
        def __init__(self):
            self.turns = [turn(number) for number in range(83)]
            self.returned = 0

        def request(self, method, params):
            assert method == "thread/turns/list"
            data = list(reversed(self.turns)) if params["sortDirection"] == "desc" else list(self.turns)
            start = int(params.get("cursor") or 0)
            end = min(len(data), start + params["limit"])
            self.returned += end - start
            return {"data": data[start:end], "nextCursor": str(end) if end < len(data) else None}

    native = Native()
    monkeypatch.setattr("hermes_cli.codex_linked_threads._codex_client", lambda **_kwargs: nullcontext(native))
    try:
        params = {"session_id": ref.session_id, "order": "latest", "limit": 7}
        page = await owner.dispatch({"id": 1, "method": "session.transcript", "params": params})
        assert "result" in page, page
        assert [row["text"] for row in page["result"]["rows"]] == [f"question {n}" for n in range(76, 83)]
        assert native.returned <= 14, "the initial page must not read the full thread"

        # The fake's native cursor is an offset, which shifts when a new turn is appended.
        # Earlier pages must still walk the original snapshot without dropping or duplicating rows.
        native.turns.extend([turn(83), turn(84), turn(85)])
        pages = [page["result"]["rows"]]
        cursor = page["result"]["next_cursor"]
        for request_id in range(2, 30):
            if cursor is None:
                break
            page = await owner.dispatch({"id": request_id, "method": "session.transcript", "params": {
                **params, "cursor": cursor}})
            assert "result" in page, page
            assert page["result"]["reset"] is False
            pages.insert(0, page["result"]["rows"])
            cursor = page["result"]["next_cursor"]
        assert cursor is None
        assert [row["text"] for group in pages for row in group] == [f"question {n}" for n in range(83)]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_canonical_linked_mutations_refuse_local_forks_and_policy_edits(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    try:
        row = session_row(db, ref.session_id)
        for operation, payload in (
            ("branch", {}), ("rewind", {"target_message_id": 1}),
            ("reset", {}), ("model", {"model": "other"}),
        ):
            reply = await owner.dispatch({"id": operation, "method": "session.mutate", "params": {
                "session_id": ref.session_id, "request_id": f"request-{operation}",
                "expected_revision": row["runtime_revision"],
                "expected_generation": row["runtime_generation"],
                "operation": operation, "payload": payload}})
            assert reply["error"]["data"]["reason"] == "unsupported_operation", reply
        assert session_row(db, ref.session_id)["cwd"] == str(tmp_path)
        assert session_row(db, ref.session_id)["model"] == row["model"]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_linked_resume_fails_closed_when_codex_history_is_unavailable(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)

    def unavailable(*_args, **_kwargs):
        raise OSError("Codex app-server unavailable")

    monkeypatch.setattr("gateway.session_linked_codex._read_trace_turns", unavailable)
    try:
        reply = await owner.dispatch({"id": 1, "method": "session.resume", "params": {
            "session_id": ref.session_id}})
        assert reply["error"]["data"]["reason"] == "storage_unavailable", reply
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_desktop_session_compress_uses_the_canonical_native_mutation_path(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history", lambda _thread, **_kwargs: [
        {"role": "user", "content": "Codex-owned history"},
        {"role": "assistant", "content": "Codex-owned answer"},
    ])
    resident = SimpleNamespace(api_mode="codex_app_server", _session_db=db, persistence=db,
                               session_id=ref.session_id, context_compressor=object())
    _authority.runner._cached_agent_for = lambda _route: resident
    monkeypatch.setattr("agent.conversation_compression_manual.compress_now",
                        lambda *_args: SimpleNamespace(status="compressed"))
    monkeypatch.setattr("agent.conversation_compression.finalize_context_engine_compression_notification",
                        lambda *_args, **_kwargs: None)
    try:
        initial_generation = session_row(db, ref.session_id)["runtime_generation"]
        reply = await owner.dispatch({"id": 1, "method": "session.compress", "params": {
            "session_id": ref.session_id}})
        assert "result" in reply, reply
        assert reply["result"]["status"] == "compressed"
        assert [row["text"] for row in reply["result"]["messages"]] == [
            "Codex-owned history", "Codex-owned answer"]
        assert transcript_rows(db, ref.session_id) == []
        assert session_row(db, ref.session_id)["runtime_generation"] == initial_generation + 1
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_native_compaction_summary_does_not_report_a_false_noop(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    native_history = [
        {"role": "user", "content": "native question"},
        {"role": "assistant", "content": "native answer"},
    ]
    resident = SimpleNamespace(api_mode="codex_app_server", _session_db=db, persistence=db,
                               session_id=ref.session_id, context_compressor=object())
    authority.runner._cached_agent_for = lambda _route: resident
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history",
                        lambda _thread, **_kwargs: native_history)

    monkeypatch.setattr("agent.conversation_compression.finalize_context_engine_compression_notification",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.conversation_compression_manual.compress_now", lambda *_args, **_kwargs:
                        SimpleNamespace(status="compressed", summary={
                            "noop": True,
                            "aborted": True,
                            "headline": "No changes from compression: 2 messages",
                            "token_line": "Approx request size: unchanged",
                            "note": None,
                        }))
    try:
        row = session_row(db, ref.session_id)
        result = await owner.dispatch({"id": 1, "method": "session.mutate", "params": {
            "session_id": ref.session_id, "request_id": "native-summary",
            "expected_revision": row["runtime_revision"],
            "expected_generation": row["runtime_generation"],
            "operation": "compress", "payload": {}}})
        assert "result" in result, result
        assert result["result"]["status"] == "compressed"
        assert result["result"]["summary"]["noop"] is False
        assert result["result"]["summary"]["aborted"] is False
        assert result["result"]["summary"]["headline"] == "Codex compacted the native thread"
        assert "not rewritten" in result["result"]["summary"]["token_line"]
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_linked_native_compression_refuses_a_non_codex_provider_before_reading_history(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    reads, compressions = [], []
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history",
                        lambda *_args, **_kwargs: reads.append(True) or [])
    resident = SimpleNamespace(api_mode="chat_completions", _session_db=db, persistence=db,
                               session_id=ref.session_id, context_compressor=object())
    authority.runner._cached_agent_for = lambda _route: resident
    monkeypatch.setattr("agent.conversation_compression_manual.compress_now",
                        lambda *_args: compressions.append(True))
    try:
        row = session_row(db, ref.session_id)
        reply = await owner.dispatch({"id": 1, "method": "session.mutate", "params": {
            "session_id": ref.session_id, "request_id": "wrong-provider",
            "expected_revision": row["runtime_revision"],
            "expected_generation": row["runtime_generation"],
            "operation": "compress", "payload": {}}})
        assert reply["error"]["data"]["reason"] == "unsupported_operation", reply
        assert reads == [] and compressions == []
        from persistence.sqlite.authority._native_codex_compaction import native_codex_compaction_key
        with raw_holder(db)._read_ctx() as conn:
            assert conn.execute("SELECT 1 FROM state_meta WHERE key=?",
                                (native_codex_compaction_key(ref.session_id),)).fetchone() is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_fresh_session_compress_retry_uses_saved_outcome_if_receipt_write_fails(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace as NS

    async def handle(_event):
        return "ok"

    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    native_history = [{"role": "user", "content": "native prompt"}]
    history_reads, compact_calls = [], []
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history",
                        lambda _thread, **_kwargs: history_reads.append(True) or native_history)
    resident = SimpleNamespace(api_mode="codex_app_server", _session_db=db, persistence=db,
                               session_id=ref.session_id, context_compressor=object())
    authority.runner._cached_agent_for = lambda _route: resident
    monkeypatch.setattr("agent.conversation_compression_manual.compress_now",
                        lambda *_args: compact_calls.append(True) or SimpleNamespace(status="compressed"))
    monkeypatch.setattr("agent.conversation_compression.finalize_context_engine_compression_notification",
                        lambda *_args, **_kwargs: None)
    request_ids = iter(("compress-first-id", "compress-wrong-digest-id", "compress-fresh-retry-id"))
    monkeypatch.setattr("uuid.uuid4", lambda: NS(hex=next(request_ids)))

    from persistence.sqlite.authority._native_codex_compaction import complete_native_codex_compaction as complete
    fail_next_write = [False]
    holder = raw_holder(db)
    execute_write = holder._execute_write

    def fail_receipt_commit(fn, patience_s=None):
        def injected(conn):
            result = fn(conn)
            if fail_next_write[0]:
                fail_next_write[0] = False
                raise sqlite3.OperationalError("injected final receipt failure")
            return result
        return execute_write(injected, patience_s=patience_s)

    holder._execute_write = fail_receipt_commit

    def record_completion(*args, **kwargs):
        result = complete(*args, **kwargs)
        fail_next_write[0] = True
        return result

    monkeypatch.setattr("persistence.sqlite.authority._native_codex_compaction.complete_native_codex_compaction", record_completion)
    try:
        row = session_row(db, ref.session_id)
        params = {"session_id": ref.session_id}
        first = await owner.dispatch({"id": 1, "method": "session.compress", "params": params})
        assert "error" in first, first
        assert compact_calls == [True]
        from persistence.sqlite.authority._native_codex_compaction import native_codex_compaction_key
        with raw_holder(db)._read_ctx() as conn:
            marker = conn.execute("SELECT value FROM state_meta WHERE key=?",
                                  (native_codex_compaction_key(ref.session_id),)).fetchone()
        assert marker is not None and '"state":"completed"' in marker[0]
        assert '"request_id":"compress-first-id"' in marker[0]
        assert session_row(db, ref.session_id)["runtime_generation"] == row["runtime_generation"]

        mismatched = await owner.dispatch({"id": 2, "method": "session.compress", "params": {
            "session_id": ref.session_id, "focus_topic": "different operation digest"}})
        assert mismatched["error"]["data"]["reason"] == "runtime_coordination_required", mismatched
        assert compact_calls == [True]

        retried = await owner.dispatch({"id": 3, "method": "session.compress", "params": params})
        assert "result" in retried, retried
        assert retried["result"]["status"] == "compressed"
        assert compact_calls == [True], "a fresh-ID receipt recovery repeated the native compaction"
        assert len(history_reads) == 2, "recovery should only read history once more to render the committed result"
        assert session_row(db, ref.session_id)["runtime_generation"] == row["runtime_generation"] + 1
        assert db.runtime.has_mutation_receipt(owner.actor.subject, ref.session_id, "compress-fresh-retry-id")
        assert not db.runtime.has_mutation_receipt(owner.actor.subject, ref.session_id, "compress-first-id")
        with raw_holder(db)._read_ctx() as conn:
            assert conn.execute("SELECT 1 FROM state_meta WHERE key=?",
                                (native_codex_compaction_key(ref.session_id),)).fetchone() is None
    finally:
        holder._execute_write = execute_write
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_uncertain_native_compact_keeps_retry_fenced(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, db, authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    _linked(db, ref.session_id)
    history_reads, compact_calls = [], []
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history",
                        lambda _thread, **_kwargs: history_reads.append(True) or [])
    resident = SimpleNamespace(api_mode="codex_app_server", _session_db=db, persistence=db,
                               session_id=ref.session_id, context_compressor=object())
    authority.runner._cached_agent_for = lambda _route: resident

    def uncertain(*_args):
        compact_calls.append(True)
        raise OSError("connection closed after request submission")

    monkeypatch.setattr("agent.conversation_compression_manual.compress_now", uncertain)
    try:
        row = session_row(db, ref.session_id)
        request = {"id": 1, "method": "session.mutate", "params": {
            "session_id": ref.session_id, "request_id": "native-unknown-outcome",
            "expected_revision": row["runtime_revision"],
            "expected_generation": row["runtime_generation"],
            "operation": "compress", "payload": {}}}
        first = await owner.dispatch(request)
        assert first["error"]["data"]["reason"] == "runtime_coordination_required", first
        assert "outcome is uncertain" in first["error"]["data"]["detail"]
        retried = await owner.dispatch(request)
        assert retried["error"]["data"]["reason"] == "runtime_coordination_required", retried
        assert "verify history read-only" in retried["error"]["data"]["detail"].lower()
        assert compact_calls == [True], "an ambiguous native result must never be compacted twice"
        assert len(history_reads) == 1

        from persistence.core.errors import AuthorityRefusal
        from persistence.sqlite.authority._admission_store import admit_session_input
        with pytest.raises(AuthorityRefusal) as blocked_input:
            admit_session_input(raw_holder(db), epoch=authority.epoch, principal_id="human", session_id=ref.session_id,
                                request_id="input-during-compact-recovery", payload={"text": "wait"})
        assert getattr(blocked_input.value, "reason", None) == "runtime_coordination_required"

        rename = await owner.dispatch({"id": 2, "method": "session.mutate", "params": {
            "session_id": ref.session_id, "request_id": "rename-during-compact-recovery",
            "expected_revision": row["runtime_revision"],
            "expected_generation": row["runtime_generation"],
            "operation": "rename", "payload": {"title": "should wait"}}})
        assert rename["error"]["data"]["reason"] == "runtime_coordination_required", rename

        from persistence.sqlite.authority._native_codex_compaction import (
            pending_native_codex_compaction, resolve_native_codex_compaction,
        )
        pending = pending_native_codex_compaction(raw_holder(db), ref.session_id)
        assert pending["request_id"] == "native-unknown-outcome"
        assert resolve_native_codex_compaction(
            raw_holder(db), epoch=authority.epoch, target_session_id=ref.session_id,
            thread_id="01a1c4a4-7a29-7011-8493-04445af16d6e",
            request_key=pending["request_key"], request_digest=pending["request_digest"],
            verified_status="compacted") == "completed"
        recovered = await owner.dispatch(request)
        assert "result" in recovered, recovered
        assert recovered["result"]["status"] == "compressed"
        assert compact_calls == [True], "operator recovery must reuse the verified outcome"
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_codex_subagent_trace_requires_parent_reference_and_polls_read_only(tmp_path, monkeypatch):
    from contextlib import contextmanager

    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    root_thread = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    child_thread = "019e1a94-352b-71e1-b214-e5c67c9ec190"
    _linked(db, ref.session_id)

    class ReadOnlyCodex:
        def __init__(self):
            self.child_items = [
                {"type": "userMessage", "id": "child-user", "content": [
                    {"type": "text", "text": "Inspect this subagent trace."}]},
                {"type": "agentMessage", "id": "child-answer", "text": "The child finished its first step."},
            ]
            self.methods = []

        def request(self, method, params):
            self.methods.append((method, params))
            if method == "thread/read":
                assert params == {"threadId": child_thread, "includeTurns": False}
                return {"thread": {"id": child_thread, "parentThreadId": root_thread,
                                   "ephemeral": False, "status": {"type": "active"},
                                   "source": {"subAgent": {"thread_spawn": {
                                       "parent_thread_id": root_thread, "depth": 1,
                                       "agent_path": "research/repo-helper"}}}}}
            if method == "thread/turns/list":
                if params["threadId"] == root_thread:
                    items = [{"type": "subAgentActivity", "id": "activity-1", "kind": "started",
                              "agentThreadId": child_thread}]
                else:
                    assert params["threadId"] == child_thread
                    items = list(self.child_items)
                return {"data": [{"id": "turn-1", "status": "inProgress", "items": items}],
                        "nextCursor": None}
            raise AssertionError(f"unexpected native Codex method: {method}")

    native = ReadOnlyCodex()

    @contextmanager
    def client(**_kwargs):
        yield native

    monkeypatch.setattr("hermes_cli.codex_linked_threads._codex_client", client)
    try:
        first = await owner.dispatch({"id": 1, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "codex_subagent_thread_id": child_thread,
            "limit": 1, "order": "oldest"}})
        assert first["result"]["available"] and first["result"]["running"], first
        assert [row["text"] for row in first["result"]["rows"]] == ["Inspect this subagent trace."], first
        cursor = first["result"]["next_cursor"]
        native.child_items.append({"type": "agentMessage", "id": "child-answer-2", "text": "A later live update."})
        previous_calls = len(native.methods)
        later = await owner.dispatch({"id": 2, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "codex_subagent_thread_id": child_thread,
            "limit": 10, "order": "oldest", "cursor": cursor}})
        assert later["result"]["available"] and later["result"]["running"], later
        assert later["result"]["reset"] is False, later
        assert [row["text"] for row in later["result"]["rows"]] == [
            "The child finished its first step.", "A later live update."]
        refresh_calls = native.methods[previous_calls:]
        assert not any(method == "thread/turns/list" and params["threadId"] == root_thread
                       for method, params in refresh_calls), "verified parent reference should be cached briefly"
        assert any(method == "thread/turns/list" and params["threadId"] == child_thread
                   and params["sortDirection"] == "desc" for method, params in refresh_calls)
        assert all(method in {"thread/read", "thread/turns/list"} for method, _ in native.methods)
        assert not any(method in {"thread/resume", "thread/fork", "turn/start"} for method, _ in native.methods)

        # A valid UUID from another native thread is not enough: the authorized parent's history must
        # name the child before any child turns are returned.
        foreign_thread = "019e1a94-352b-71e1-b214-e5c67c9ec191"
        original = native.request

        def foreign_request(method, params):
            if method == "thread/read" and params["threadId"] == foreign_thread:
                return {"thread": {"id": foreign_thread, "parentThreadId": root_thread,
                                   "ephemeral": False, "status": {"type": "active"}}}
            if method == "thread/turns/list" and params["threadId"] == root_thread:
                return {"data": [{"id": "turn-1", "items": []}], "nextCursor": None}
            return original(method, params)

        native.request = foreign_request
        refused = await owner.dispatch({"id": 3, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "codex_subagent_thread_id": foreign_thread,
            "limit": 10, "order": "oldest"}})
        assert refused["result"]["available"] is False, refused
        assert refused["result"]["rows"] == [] and refused["result"]["next_cursor"] is None
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_codex_subagent_trace_accepts_nested_child_only_through_native_ancestry(tmp_path, monkeypatch):
    from contextlib import contextmanager

    async def handle(_event):
        return "ok"

    store, db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    root_thread = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    parent_thread = "019e1a94-352b-71e1-b214-e5c67c9ec190"
    child_thread = "019e1a94-352b-71e1-b214-e5c67c9ec191"
    _linked(db, ref.session_id)

    class ReadOnlyCodex:
        def request(self, method, params):
            if method == "thread/read":
                thread_id = params["threadId"]
                parent = {parent_thread: root_thread, child_thread: parent_thread}.get(thread_id)
                return {"thread": {"id": thread_id, "parentThreadId": parent, "ephemeral": False,
                                   "status": {"type": "idle"},
                                   "source": {"subAgent": {"thread_spawn": {"parent_thread_id": parent}}}}}
            if method == "thread/turns/list":
                thread_id = params["threadId"]
                items = ([{"type": "subAgentActivity", "id": "child-activity", "kind": "started",
                           "agentThreadId": child_thread}]
                         if thread_id == parent_thread else
                         [{"type": "agentMessage", "id": "nested-answer", "text": "Nested trace."}]
                         if thread_id == child_thread else [])
                return {"data": [{"id": "turn-1", "status": "completed", "items": items}], "nextCursor": None}
            raise AssertionError(f"unexpected native Codex method: {method}")

    native = ReadOnlyCodex()

    @contextmanager
    def client(**_kwargs):
        yield native

    monkeypatch.setattr("hermes_cli.codex_linked_threads._codex_client", client)
    try:
        reply = await owner.dispatch({"id": 1, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "codex_subagent_thread_id": child_thread,
            "limit": 10, "order": "oldest"}})
        assert reply["result"]["available"] is True, reply
        assert [row["text"] for row in reply["result"]["rows"]] == ["Nested trace."], reply
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
async def test_codex_subagent_trace_is_unavailable_without_a_native_parent_pointer(tmp_path, monkeypatch):
    async def handle(_event):
        return "ok"

    store, _db, _authority, owner, ref = _owner(tmp_path, monkeypatch, handle)
    child_thread = "019e1a94-352b-71e1-b214-e5c67c9ec190"

    def must_not_connect():
        raise AssertionError("a Hermes-only session must not query an arbitrary Codex thread")

    monkeypatch.setattr("hermes_cli.codex_linked_threads._codex_client", must_not_connect)
    try:
        reply = await owner.dispatch({"id": 1, "method": "session.transcript", "params": {
            "session_id": ref.session_id, "codex_subagent_thread_id": child_thread,
            "limit": 10, "order": "oldest"}})
        assert reply["result"]["available"] is False, reply
        assert reply["result"]["rows"] == []
        assert reply["result"]["next_cursor"] is None
    finally:
        await owner.close()
        store.close_all_db_handles()


def test_codex_subagent_trace_fails_closed_when_ephemeral_flag_is_missing():
    from gateway.session_linked_codex import _read_thread

    class IncompleteThread:
        def request(self, method, _params):
            assert method == "thread/read"
            return {"thread": {"id": "019e1a94-352b-71e1-b214-e5c67c9ec190"}}

    assert _read_thread(IncompleteThread(), "019e1a94-352b-71e1-b214-e5c67c9ec190") is None


def test_codex_subagent_trace_applies_transform_once_to_final_row_and_hides_held_commentary(monkeypatch):
    from gateway.session_linked_codex import _display_native_turns

    class ProfileHooks:
        def __init__(self, *, held):
            self.held = held
            self.calls = []
            self._hooks = {"transform_llm_output": [self._rewrite]}

        def _rewrite(self, **payload):
            self.calls.append(payload)
            return f"filtered: {payload['response_text']}"

        def holds_stream(self):
            return self.held

        def held_transforms(self):
            return tuple(self._hooks["transform_llm_output"]) if self.held else ()

        def run_held_transforms(self, callbacks, **payload):
            return callbacks[0](**payload), False

        def invoke_hook(self, _name, **payload):
            return [self._hooks["transform_llm_output"][0](**payload)]

    held = ProfileHooks(held=True)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: held)
    turns = [{"id": "native-turn-7", "status": "completed", "final_text": "final child answer",
              "rows": [{"role": "assistant", "content": "interim commentary with private marker"},
                       {"role": "assistant", "content": "final child answer"},
                       {"role": "tool", "content": "tool result"}]}]
    rows = _display_native_turns(turns, session_id="parent-session", thread_id="child-thread",
                                 model="codex-model")
    assert rows[0]["display_kind"] == "hidden"
    assert rows[0]["content"] is None
    assert rows[1]["content"] == "filtered: final child answer"
    assert rows[2]["content"] == "tool result"
    from tui_gateway import server
    visible_rows = server._history_to_messages(rows)
    assert all("private marker" not in row.get("content", "") for row in visible_rows)
    assert held.calls[0]["session_id"] == "parent-session"
    assert held.calls[0]["model"] == "codex-model"
    assert held.calls[0]["turn_id"] == "codex-native:child-thread:native-turn-7"
    _display_native_turns(turns, session_id="parent-session", thread_id="child-thread", model="codex-model")
    assert len(held.calls) == 1, "the cached per-turn transform must not run on every cursor page"

    plain = ProfileHooks(held=False)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: plain)
    plain_turns = [{"id": "native-turn-8", "status": "completed", "final_text": "final child answer",
                    "rows": [{"role": "assistant", "content": "visible commentary"},
                             {"role": "assistant", "content": "final child answer"}]}]
    plain_rows = _display_native_turns(plain_turns, session_id="parent-session", thread_id="child-thread",
                                       model="codex-model")
    assert plain_rows[0]["content"] == "visible commentary"
    assert plain_rows[0].get("display_kind") is None
    assert plain_rows[1]["content"] == "filtered: final child answer"
    assert len(plain.calls) == 1, "ordinary transforms apply to only the final row of a turn"


def test_codex_subagent_trace_withholds_final_when_held_transform_fails(monkeypatch):
    from gateway.session_linked_codex import _display_native_turns
    from agent.turn_finalizer import HELD_TRANSFORM_FAILED_REPLY

    class ProfileHooks:
        def holds_stream(self):
            return True

        def held_transforms(self):
            return (object(),)

        def run_held_transforms(self, _callbacks, **_payload):
            return None, True

    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: ProfileHooks())
    turns = [{"id": "native-turn-9", "status": "completed", "final_text": "must not be shown raw",
              "rows": [{"role": "assistant", "content": "must not be shown raw"}]}]
    rows = _display_native_turns(turns, session_id="parent-session", thread_id="child-thread", model=None)
    assert rows[0]["content"] == HELD_TRANSFORM_FAILED_REPLY

def test_oversized_native_trace_falls_back_to_latest_turns_instead_of_blanking(tmp_path):
    """A native trace over the full-read byte budget must still display its latest turns."""
    from gateway.session_linked_codex import read_display_history

    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    huge = {"id": "turn-huge", "status": "completed", "items": [
        {"type": "agentMessage", "id": "a-huge", "text": "x" * (33 * 1024 * 1024)},
    ]}
    recent = [
        {"id": "turn-old", "status": "completed", "items": [
            {"type": "userMessage", "id": "u-old", "content": [{"type": "text", "text": "old q"}]},
            {"type": "agentMessage", "id": "a-old", "text": "old a"},
        ]},
        {"id": "turn-new", "status": "completed", "items": [
            {"type": "userMessage", "id": "u-new", "content": [{"type": "text", "text": "new q"}]},
            {"type": "agentMessage", "id": "a-new", "text": "new a"},
        ]},
    ]

    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            if params["sortDirection"] == "asc":
                return {"data": [huge], "nextCursor": None}
            snapshot = list(recent)
            snapshot.reverse()
            return {"data": snapshot, "nextCursor": None}

    overlays = {t["id"]: {"version": 1, "thread_id": thread_id, "turn_id": t["id"], "held": False,
                          "final_visible": True, "final_text": t["items"][1]["text"], "platform": "local"}
                for t in recent}
    rows = read_display_history(thread_id, session_id="linked-huge-test", profile_home=tmp_path,
                                client=Client(), display_overlays=overlays)
    assert [row.get("content") for row in rows] == ["old q", "old a", "new q", "new a"]



def test_display_history_window_pages_native_trace_in_row_space(tmp_path):
    """Oversized threads load window-by-window: native cursor paging, row-space offset/limit."""
    from gateway.session_linked_codex import read_display_history_window
    import hermes_cli.codex_linked_history as clh

    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"

    def turn(n):
        return {"id": f"turn-{n}", "status": "completed", "items": [
            {"type": "userMessage", "id": f"u-{n}", "content": [{"type": "text", "text": f"q{n}"}]},
            {"type": "agentMessage", "id": f"a-{n}", "text": f"a{n}"},
        ]}

    all_turns = [turn(n) for n in range(1, 6)]  # 5 turns, 10 display rows

    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            data = list(all_turns)
            if params["sortDirection"] == "desc":
                data.reverse()
            start = int(params["cursor"]) if params.get("cursor") else 0
            page = data[start:start + 2]
            nxt = start + 2
            return {"data": page, "nextCursor": str(nxt) if nxt < len(data) else None}

    overlays = {t["id"]: {"version": 1, "thread_id": thread_id, "turn_id": t["id"], "held": False,
                          "final_visible": True, "final_text": t["items"][1]["text"], "platform": "local"}
                for t in all_turns}
    old = clh.linked_turn_display_overlays
    clh.linked_turn_display_overlays = lambda db, sid, tid, ids: {i: overlays[i] for i in ids}
    try:
        kwargs = dict(session_id="linked-window-test", profile_home=tmp_path, client=Client(),
                      session_db=object(), model=None)
        latest = read_display_history_window(thread_id, limit=4, offset=0, latest=True, **kwargs)
        oldest = read_display_history_window(thread_id, limit=4, offset=0, latest=False, **kwargs)
        mid = read_display_history_window(thread_id, limit=3, offset=2, latest=False, **kwargs)
    finally:
        clh.linked_turn_display_overlays = old
    assert [r["content"] for r in latest] == ["q4", "a4", "q5", "a5"]
    assert [r["content"] for r in oldest] == ["q1", "a1", "q2", "a2"]
    assert [r["content"] for r in mid] == ["q2", "a2", "q3"]



def _window_fixture(monkeypatch, turns, page_size=2):
    """Shared fake Codex client + overlays for window tests; returns (thread_id, kwargs)."""
    import hermes_cli.codex_linked_history as clh

    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"

    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            data = list(turns)
            if params["sortDirection"] == "desc":
                data.reverse()
            start = int(params["cursor"]) if params.get("cursor") else 0
            page = data[start:start + page_size]
            nxt = start + page_size
            return {"data": page, "nextCursor": str(nxt) if nxt < len(data) else None}

    overlays = {t["id"]: {"version": 1, "thread_id": thread_id, "turn_id": t["id"], "held": False,
                          "final_visible": True, "final_text": t["items"][-1].get("text"),
                          "platform": "local"}
                for t in turns}
    monkeypatch.setattr(clh, "linked_turn_display_overlays",
                        lambda db, sid, tid, ids: {i: overlays[i] for i in ids})
    kwargs = dict(session_id="linked-window-adv", profile_home=None, client=Client(),
                  session_db=object(), model=None)
    return thread_id, kwargs



def _two_row_turn(n):
    return {"id": f"turn-{n}", "status": "completed", "items": [
        {"type": "userMessage", "id": f"u-{n}", "content": [{"type": "text", "text": f"q{n}"}]},
        {"type": "agentMessage", "id": f"a-{n}", "text": f"a{n}"},
    ]}



def test_display_history_window_latest_spans_multiple_pages_in_ascending_order(monkeypatch):
    """A latest window wider than one native page must stay ascending and keep the NEWEST rows."""
    from gateway.session_linked_codex import read_display_history_window

    turns = [_two_row_turn(n) for n in range(1, 8)]  # 7 turns, 14 rows, 2-turn pages
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    rows = read_display_history_window(thread_id, limit=6, offset=0, latest=True, **kwargs)
    assert [r["content"] for r in rows] == ["q5", "a5", "q6", "a6", "q7", "a7"]
    rows = read_display_history_window(thread_id, limit=12, offset=0, latest=True, **kwargs)
    assert [r["content"] for r in rows] == [c for n in range(2, 8) for c in (f"q{n}", f"a{n}")]



def test_display_history_window_latest_row_offset_skips_newest_rows_at_page_boundaries(monkeypatch):
    """Row-space offset with latest=True skips the newest rows, mid-page and at a page boundary."""
    from gateway.session_linked_codex import read_display_history_window

    turns = [_two_row_turn(n) for n in range(1, 8)]
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    # offset=2 lands mid-page (the first desc page holds turns 7,6 = rows q6..q7).
    rows = read_display_history_window(thread_id, limit=2, offset=2, latest=True, **kwargs)
    assert [r["content"] for r in rows] == ["q6", "a6"]
    # offset=4 lands exactly on a page boundary (one 2-turn page skipped).
    rows = read_display_history_window(thread_id, limit=2, offset=4, latest=True, **kwargs)
    assert [r["content"] for r in rows] == ["q5", "a5"]
    # offset spanning several pages plus a limit that spans another page: skip the 4 newest
    # rows (q7,a7,q6,a6), then take the newest 4 of the rest.
    rows = read_display_history_window(thread_id, limit=4, offset=4, latest=True, **kwargs)
    assert [r["content"] for r in rows] == ["q4", "a4", "q5", "a5"]
    # offset past the end yields no rows, like read_display_history's slicing.
    rows = read_display_history_window(thread_id, limit=5, offset=100, latest=True, **kwargs)
    assert rows == []



def test_display_history_window_budget_hit_mid_page_returns_newest_fitting_rows(monkeypatch):
    """A byte budget cut mid-page keeps the partial page, stops paging, and stays ascending."""
    import json

    from gateway.session_linked_codex import read_display_history_window
    from hermes_cli.codex_linked_history import native_turn_record

    turns = [_two_row_turn(n) for n in range(1, 8)]
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    # Budget fits exactly the two newest turns, so the third-newest turn trips the budget
    # mid-page and the outer loop must stop instead of paging older turns.
    one = len(json.dumps(native_turn_record(turns[0]), ensure_ascii=False, default=str).encode())
    rows = read_display_history_window(thread_id, limit=100, offset=0, latest=True,
                                       max_bytes=2 * one + one // 2, **kwargs)
    contents = [r["content"] for r in rows]
    assert contents == ["q6", "a6", "q7", "a7"]
    assert len(rows) < 100, "budget hit must stop the outer paging loop"



def test_display_history_window_empty_thread_and_zero_limit(monkeypatch):
    from gateway.session_linked_codex import read_display_history_window

    thread_id, kwargs = _window_fixture(monkeypatch, [])

    class EmptyClient:
        def request(self, method, params):
            return {"data": [], "nextCursor": None}

    kwargs = {**kwargs, "client": EmptyClient()}
    assert read_display_history_window(thread_id, limit=10, offset=0, latest=True, **kwargs) == []
    assert read_display_history_window(thread_id, limit=10, offset=0, latest=False, **kwargs) == []
    turns = [_two_row_turn(1)]
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    assert read_display_history_window(thread_id, limit=0, offset=0, latest=True, **kwargs) == []



def _huge_text_turn(n, text):
    return {"id": f"turn-huge-{n}", "status": "completed", "items": [
        {"type": "userMessage", "id": f"u-huge-{n}", "content": [{"type": "text", "text": f"q{n}"}]},
        {"type": "agentMessage", "id": f"a-huge-{n}", "text": text},
    ]}



def test_display_history_window_truncates_single_oversized_turn_latest_keeps_tail(monkeypatch):
    """One turn bigger than the whole window budget must render its tail, not an empty window."""
    from gateway.session_linked_codex import read_display_history_window

    text = ("alpha-" * 2000) + "TAIL-MARKER-END"
    thread_id, kwargs = _window_fixture(monkeypatch, [_huge_text_turn(1, text)])
    rows = read_display_history_window(thread_id, limit=100, offset=0, latest=True,
                                       max_bytes=4000, **kwargs)
    assert rows, "an oversized single turn must not drop the whole window"
    assert all(row.get("truncated") is True for row in rows)
    biggest = max((row["content"] for row in rows if isinstance(row.get("content"), str)), key=len)
    assert text.endswith(biggest) and "TAIL-MARKER-END" in biggest
    assert len(biggest) < len(text)



def test_display_history_window_truncates_single_oversized_turn_oldest_keeps_head(monkeypatch):
    """Scroll-up (asc) reads of one oversized turn keep the head of the text instead."""
    from gateway.session_linked_codex import read_display_history_window

    text = "HEAD-MARKER-START" + ("omega-" * 2000)
    thread_id, kwargs = _window_fixture(monkeypatch, [_huge_text_turn(1, text)])
    rows = read_display_history_window(thread_id, limit=100, offset=0, latest=False,
                                       max_bytes=4000, **kwargs)
    assert rows and all(row.get("truncated") is True for row in rows)
    biggest = max((row["content"] for row in rows if isinstance(row.get("content"), str)), key=len)
    assert text.startswith(biggest) and biggest.startswith("HEAD-MARKER-START")



def test_display_history_window_mid_list_oversized_turn_stops_fail_soft(monkeypatch):
    """An oversized turn after already-collected rows keeps the fail-soft stop, no truncation."""
    from gateway.session_linked_codex import read_display_history_window

    turns = [_huge_text_turn(1, "x" * 9000), _two_row_turn(2)]  # newest is turn-2
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    rows = read_display_history_window(thread_id, limit=100, offset=0, latest=True,
                                       max_bytes=4000, **kwargs)
    assert [row["content"] for row in rows] == ["q2", "a2"]
    assert all(row.get("truncated") is None for row in rows)



def test_display_history_window_budget_below_overhead_yields_no_rows_not_an_error(monkeypatch):
    from gateway.session_linked_codex import read_display_history_window

    thread_id, kwargs = _window_fixture(monkeypatch, [_huge_text_turn(1, "x" * 9000)])
    assert read_display_history_window(thread_id, limit=10, offset=0, latest=True,
                                       max_bytes=1, **kwargs) == []
    assert read_display_history_window(thread_id, limit=10, offset=0, latest=False,
                                       max_bytes=1, **kwargs) == []



def test_display_history_window_unchanged_when_all_turns_fit(monkeypatch):
    from gateway.session_linked_codex import read_display_history_window

    turns = [_two_row_turn(n) for n in range(1, 4)]
    thread_id, kwargs = _window_fixture(monkeypatch, turns)
    rows = read_display_history_window(thread_id, limit=100, offset=0, latest=True,
                                       max_bytes=32 * 1024 * 1024, **kwargs)
    assert [row["content"] for row in rows] == ["q1", "a1", "q2", "a2", "q3", "a3"]
    assert all(row.get("truncated") is None for row in rows)



def test_display_history_window_matches_read_display_history_slicing_for_small_threads(monkeypatch, tmp_path):
    """For threads that fit one full read, the window must reproduce the old offset/limit/latest slicing."""
    from gateway.session_linked_codex import read_display_history, read_display_history_window

    turns = [_two_row_turn(n) for n in range(1, 8)]  # 14 rows, single native page
    thread_id, kwargs = _window_fixture(monkeypatch, turns, page_size=100)
    client = kwargs["client"]
    for latest in (True, False):
        for limit in (None, 1, 3, 14, 20):
            for offset in (0, 2, 13, 100):
                # The fixture patched linked_turn_display_overlays, so session_db routes
                # read_display_history through the same overlays as the window read.
                legacy = read_display_history(
                    thread_id, session_id="linked-window-adv", profile_home=tmp_path, client=client,
                    limit=limit, offset=offset, latest=latest, session_db=object(),
                    display_overlays=None, model=None)
                window = read_display_history_window(
                    thread_id, limit=500 if limit is None else limit, offset=offset,
                    latest=latest, **kwargs)
                assert [r["content"] for r in window] == [r["content"] for r in legacy], (
                    latest, limit, offset)


def _empty_turn(n):
    """A reasoning-only turn: projects to ZERO display rows but still consumes a native turn."""
    return {"id": f"turn-empty-{n}", "status": "completed",
            "items": [{"type": "reasoning", "text": f"thinking {n}"}]}


def test_display_history_window_counts_rows_and_turns_in_separate_units(monkeypatch):
    """Row-less turns must not eat the window's page budget (the old turn/row miscount).

    The page cap is turn-space (native pages of 100 turns), while ``limit``/``offset`` are
    row-space: a run of turns that project to zero rows must not make the window stop early,
    and a turn projecting several rows must not overfill it.
    """
    from gateway.session_linked_codex import read_display_history_window

    turns = [_empty_turn(n) for n in range(1, 7)] + [_two_row_turn(n) for n in range(7, 11)]
    thread_id, kwargs = _window_fixture(monkeypatch, turns, page_size=2)
    # 6 row-less turns + 4 two-row turns: the oldest 8 rows live past the first 3 pages,
    # so a page cap derived from the row window (treating rows as turns) would return [].
    rows = read_display_history_window(thread_id, limit=8, offset=0, latest=False, **kwargs)
    assert [r["content"] for r in rows] == [c for n in range(7, 11) for c in (f"q{n}", f"a{n}")]
    rows = read_display_history_window(thread_id, limit=8, offset=0, latest=True, **kwargs)
    assert [r["content"] for r in rows] == [c for n in range(7, 11) for c in (f"q{n}", f"a{n}")]
    # The window still bounds messages: asking for 4 rows returns exactly 4, not every row.
    rows = read_display_history_window(thread_id, limit=4, offset=0, latest=False, **kwargs)
    assert [r["content"] for r in rows] == [c for n in range(7, 9) for c in (f"q{n}", f"a{n}")]
