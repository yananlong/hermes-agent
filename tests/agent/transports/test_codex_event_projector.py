"""Tests for CodexEventProjector — codex item/* events → Hermes messages list.

Drives projection against fixture notifications captured from codex 0.130.0
plus synthetic ones for item types we couldn't auth-test live."""

from __future__ import annotations

import json

import pytest

from agent.transports.codex_event_projector import (
    CodexEventProjector,
    _MAX_REASONING_DISPLAY_CHARS,
    _bounded_reasoning_text,
    _deterministic_call_id,
    _format_tool_args,
)


# --- Fixture: real `commandExecution` notification captured from codex 0.130.0
COMMAND_EXEC_COMPLETED = {
    "method": "item/completed",
    "params": {
        "item": {
            "type": "commandExecution",
            "id": "f8a75c66-a89e-4fd7-8bcf-2d58e664fa9e",
            "command": "/bin/bash -lc 'echo hello && ls /tmp | head -3'",
            "cwd": "/tmp",
            "processId": None,
            "source": "userShell",
            "status": "completed",
            "commandActions": [
                {"type": "listFiles", "command": "ls /tmp", "path": "tmp"}
            ],
            "aggregatedOutput": "hello\naa_lang.json\n",
            "exitCode": 0,
            "durationMs": 10,
        },
        "threadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
        "turnId": "019e1a94-3553-7940-8af3-4ca57142deb7",
        "completedAtMs": 1778562381151,
    },
}


class TestProjectionInvariants:
    """Universal invariants that must hold across all projection paths."""

    def test_streaming_deltas_dont_materialize(self) -> None:
        p = CodexEventProjector()
        for delta_method in (
            "item/commandExecution/outputDelta",
            "item/agentMessage/delta",
            "item/reasoning/delta",
        ):
            r = p.project({"method": delta_method, "params": {"delta": "x"}})
            assert r.messages == [], (
                f"{delta_method} should NOT produce messages — only "
                f"item/completed materializes"
            )
            assert r.is_tool_iteration is False
            assert r.final_text is None

    def test_turn_started_and_completed_are_silent(self) -> None:
        p = CodexEventProjector()
        for method in ("turn/started", "turn/completed", "thread/started"):
            r = p.project({"method": method, "params": {}})
            assert r.messages == []

    def test_unknown_method_silent(self) -> None:
        p = CodexEventProjector()
        r = p.project({"method": "totally/unknown", "params": {}})
        assert r.messages == []


class TestCommandExecutionProjection:
    """Real captured notification → assistant tool_call + tool result."""


    def test_first_message_is_assistant_tool_call(self) -> None:
        p = CodexEventProjector()
        msgs = p.project(COMMAND_EXEC_COMPLETED).messages
        assistant = msgs[0]
        assert assistant["role"] == "assistant"
        assert assistant["content"] is None
        assert len(assistant["tool_calls"]) == 1
        tc = assistant["tool_calls"][0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "exec_command"
        args = json.loads(tc["function"]["arguments"])
        assert "echo hello" in args["command"]
        assert args["cwd"] == "/tmp"
        assert assistant["display_metadata"]["codex_native"] is True

    def test_second_message_is_tool_result_correlating_by_id(self) -> None:
        p = CodexEventProjector()
        msgs = p.project(COMMAND_EXEC_COMPLETED).messages
        assistant, tool = msgs
        assert tool["role"] == "tool"
        assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]
        assert "hello" in tool["content"]
        assert tool["display_metadata"]["codex_native"] is True




def test_successful_marker_ending_output_survives_persisted_replay(tmp_path):
    """exitCode=0 is preserved in the persisted envelope, so a success whose last line is an
    executor interrupt marker replays as data instead of an UNKNOWN-effect recovery notice."""
    from types import SimpleNamespace

    from agent.codex_runtime import _persist_projected_messages
    from agent.replay_cleanup import canonicalize_replay_history
    from agent.session_persistence import SessionPersistenceMixin
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session

    output = "Documentation example:\n[Command interrupted]\n"
    item = {"type": "commandExecution", "id": "child", "command": "cat doc", "cwd": str(tmp_path),
            "status": "completed", "exitCode": 0, "aggregatedOutput": output}
    projected = CodexEventProjector().project({"method": "item/completed", "params": {"item": item}}).messages
    store = SessionPersistenceMixin()
    store._session_db = store.persistence = open_persistence(profile_scope(home=tmp_path))
    store.session_id = "command-replay"
    seed_session(store.persistence, store.session_id, "cli")
    store._session_db_created = True
    store._last_flushed_db_idx = 0
    turn = SimpleNamespace(projected_messages=projected + [{"role": "assistant", "content": "Done."}],
                           submitted_user_text=None)
    try:
        _persist_projected_messages(store, turn, [{"role": "user", "content": "Print documentation."}])
        resumed = store.persistence.messages.read_resume_conversations(store.session_id)
        raw = [m.as_openai_message() for m in resumed.model]
    finally:
        store._session_db.close()
    replay_tool = next(m for m in canonicalize_replay_history(raw) if m["role"] == "tool")
    assert replay_tool.get("effect_disposition") is None
    assert json.loads(replay_tool["content"]) == {"exit_code": 0, "output": output}


def test_native_reasoning_and_tool_results_survive_session_db_reload_and_history_projection(tmp_path):
    from types import SimpleNamespace

    from agent.codex_runtime import _persist_projected_messages
    from agent.session_persistence import SessionPersistenceMixin
    from persistence_assembly import open_persistence, profile_scope
    from tests.fixtures.storage_seed import seed_session
    from tui_gateway import server

    projector = CodexEventProjector()
    projected = projector.project({"method": "item/completed", "params": {"item": {
        "type": "reasoning", "id": "reason-tool", "summary": ["Check the native result."],
    }}}).messages
    projected.extend(projector.project({"method": "item/completed", "params": {"item": {
        "type": "commandExecution", "id": "command-1", "command": "pwd", "cwd": "/tmp",
        "aggregatedOutput": "/tmp", "exitCode": 0,
    }}}).messages)
    projected.extend(projector.project({"method": "item/completed", "params": {"item": {
        "type": "fileChange", "id": "file-change-1", "status": "applied",
        "changes": [{"kind": {"type": "update"}, "path": "/tmp/example.py", "diff": "-old\n+new"}],
    }}}).messages)
    projected.extend(projector.project({"method": "item/completed", "params": {"item": {
        "type": "reasoning", "id": "reason-final", "summary": ["The turn ended before another message."],
    }}}).messages)
    projected.extend(projector.project({"method": "turn/completed", "params": {}}).messages)

    store = SessionPersistenceMixin()
    store._session_db = store.persistence = open_persistence(profile_scope(home=tmp_path))
    store.session_id = "codex-display"
    seed_session(store.persistence, store.session_id, "cli")
    store._session_db_created = True
    store._last_flushed_db_idx = 0
    turn = SimpleNamespace(projected_messages=projected, submitted_user_text=None)
    try:
        assert _persist_projected_messages(store, turn, [{"role": "user", "content": "Run pwd."}])
        resume = store.persistence.messages.read_resume_conversations(store.session_id)
        restored = [row.as_openai_message() for row in resume.display]
    finally:
        store._session_db.close()

    messages = server._history_to_messages(restored)
    native_call = next(message for message in messages if message.get("tool_calls"))
    native_result = next(message for message in messages if message.get("role") == "tool")
    native_patch_call = next(message for message in messages if any(
        call["function"]["name"] == "apply_patch" for call in message.get("tool_calls", [])
    ))
    native_patch_result = next(message for message in messages
                               if message.get("role") == "tool" and message.get("name") == "apply_patch")
    interrupted_reasoning = next(message for message in messages if message.get("reasoning") ==
                                 "The turn ended before another message.")

    assert native_call["reasoning"] == "Check the native result."
    assert native_call["tool_calls"][0]["function"]["name"] == "exec_command"
    assert native_result["content"] == '{"exit_code": 0, "output": "/tmp"}'
    patch_args = json.loads(next(call for call in native_patch_call["tool_calls"]
                                 if call["function"]["name"] == "apply_patch")["function"]["arguments"])
    assert patch_args["changes"][0]["diff"] == "-old\n+new"
    assert "update: /tmp/example.py\n-old\n+new" in native_patch_result["content"]
    assert interrupted_reasoning["text"] == ""


@pytest.mark.parametrize("exit_fields", [{"exitCode": 130}, {}])
def test_interrupted_or_unknown_exit_stays_conservative(exit_fields):
    """A real interrupt (non-zero exit) or a missing exit status is still recovered as UNKNOWN."""
    from agent.replay_cleanup import canonicalize_replay_history

    item = {"type": "commandExecution", "id": "interrupted", "command": "sleep 100", "cwd": "/tmp",
            "status": "failed", "aggregatedOutput": "[Command interrupted]\n", **exit_fields}
    projected = CodexEventProjector().project({"method": "item/completed", "params": {"item": item}}).messages
    assert canonicalize_replay_history(projected)[1]["effect_disposition"] == "unknown"


class TestAgentMessageProjection:
    """assistant text → final_text + assistant message."""

    def test_agent_message_projects_to_assistant(self) -> None:
        p = CodexEventProjector()
        r = p.project({
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "x",
                                "text": "hi there"}},
        })
        assert r.final_text == "hi there"
        assert r.messages == [{"role": "assistant", "content": "hi there"}]
        assert r.is_tool_iteration is False

    def test_pending_reasoning_attaches_to_next_assistant_message(self) -> None:
        p = CodexEventProjector()
        # First a reasoning item lands
        r1 = p.project({
            "method": "item/completed",
            "params": {"item": {"type": "reasoning", "id": "r1",
                                "summary": ["thinking..."],
                                "content": ["step 1", "step 2"]}},
        })
        assert r1.messages == []  # reasoning alone produces no message
        # Then the assistant message
        r2 = p.project({
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "a1",
                                "text": "ok"}},
        })
        assistant = r2.messages[0]
        assert "reasoning" in assistant
        assert "thinking" in assistant["reasoning"]
        assert "step 1" in assistant["reasoning"]

    def test_reasoning_consumed_after_attaching(self) -> None:
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1", "summary": ["once"], "content": []}}})
        first = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a", "text": "first"}}}).messages[0]
        second = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "b", "text": "second"}}}).messages[0]
        assert "reasoning" in first
        assert "reasoning" not in second

    def test_only_codex_exposed_reasoning_text_is_projected(self) -> None:
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1", "summary": ["summary"],
            "content": ["visible content", {"not": "text"}],
            "encryptedContent": "must never be decoded or displayed",
        }}})
        message = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a1", "text": "answer",
        }}}).messages[0]
        assert message["reasoning"] == "summary\nvisible content"
        assert "must never" not in message["reasoning"]

    def test_typed_reasoning_summary_and_content_parts_are_projected(self) -> None:
        """Codex documents these arrays as typed parts, not strings (``ReasoningItemReasoningSummary``)."""
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1",
            "summary": [{"type": "summary_text", "text": "checked the recipient"}],
            "content": [{"type": "reasoning_text", "text": "the address is a fixture"},
                        {"type": "text", "text": "so sending is safe"}],
        }}})
        message = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a1", "text": "answer",
        }}}).messages[0]
        assert message["reasoning"] == ("checked the recipient\n"
                                        "the address is a fixture\nso sending is safe")

    def test_encrypted_only_reasoning_yields_no_text_and_is_never_decoded(self, monkeypatch) -> None:
        """An item carrying only ``encrypted_content`` is not displayable: it is skipped, not decoded."""
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1", "summary": [], "content": [],
            "encrypted_content": "gAAAAA-opaque-carrier",
        }}})
        message = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a1", "text": "answer",
        }}}).messages[0]
        assert "reasoning" not in message

    def test_reasoning_parts_that_are_neither_strings_nor_text_dicts_are_skipped(self) -> None:
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1",
            "summary": [None, 7, {"type": "summary_text"}, {"type": "summary_text", "text": ""},
                        {"type": "summary_text", "text": "only this"}],
            "content": "not a list",
        }}})
        message = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a1", "text": "answer",
        }}}).messages[0]
        assert message["reasoning"] == "only this"

    def test_reasoning_without_a_following_item_flushes_at_turn_boundary(self) -> None:
        p = CodexEventProjector()
        p.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "r1", "summary": ["I was interrupted while checking the result."],
        }}})

        ended = p.project({"method": "turn/completed", "params": {"turn": {"status": "interrupted"}}})
        next_turn = p.project({"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "a2", "text": "A later answer.",
        }}})

        assert ended.messages == [{"role": "assistant", "content": None,
                                   "reasoning": "I was interrupted while checking the result."}]
        assert next_turn.messages == [{"role": "assistant", "content": "A later answer."}]


def test_reasoning_display_uses_a_separate_larger_bounded_redaction_path():
    projected = _bounded_reasoning_text("x" * (_MAX_REASONING_DISPLAY_CHARS + 20))

    assert len(projected) == _MAX_REASONING_DISPLAY_CHARS
    assert projected.endswith("[truncated by Hermes reasoning display limit]")


class TestFileChangeProjection:
    def test_file_change_projects_available_diffs_into_call_and_result(self) -> None:
        item = {
            "type": "fileChange",
            "id": "fc1",
            "status": "applied",
            "changes": [
                {"kind": {"type": "add"}, "path": "/tmp/new.py", "diff": "+print('new')"},
                {"kind": {"type": "update"}, "path": "/tmp/old.py", "diff": "-old\n+new"},
            ],
        }
        p = CodexEventProjector()
        msgs = p.project({"method": "item/completed",
                          "params": {"item": item}}).messages
        assert len(msgs) == 2
        tc = msgs[0]["tool_calls"][0]
        assert tc["function"]["name"] == "apply_patch"
        args = json.loads(tc["function"]["arguments"])
        assert len(args["changes"]) == 2
        assert all("kind" in c and "path" in c for c in args["changes"])
        assert args["changes"][0]["diff"] == "+print('new')"
        assert args["changes"][1]["diff"] == "-old\n+new"
        assert "applied" in msgs[1]["content"]
        assert "update: /tmp/old.py\n-old\n+new" in msgs[1]["content"]


class TestMcpToolCallProjection:
    def test_mcp_tool_call_namespaced(self) -> None:
        item = {
            "type": "mcpToolCall",
            "id": "m1",
            "server": "obsidian",
            "tool": "search_notes",
            "status": "completed",
            "arguments": {"query": "hermes"},
            "result": {"content": [{"text": "found"}]},
            "error": None,
        }
        msgs = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert msgs[0]["tool_calls"][0]["function"]["name"] == "mcp.obsidian.search_notes"
        assert "found" in msgs[1]["content"]

    def test_mcp_error_surfaced(self) -> None:
        item = {
            "type": "mcpToolCall", "id": "m2",
            "server": "x", "tool": "y", "status": "failed",
            "arguments": {}, "result": None,
            "error": {"code": -1, "message": "no"},
        }
        msgs = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert "error" in msgs[1]["content"]

    def test_native_tool_result_is_marked_for_display_adapter_and_secrets_are_redacted(self) -> None:
        item = {
            "type": "mcpToolCall", "id": "m-secret", "server": "demo", "tool": "lookup",
            "arguments": {"query": "ghp_abcdefghijk1234"},
            "result": {"text": "ghp_zyxwvutsrqponm9876"},
        }
        assistant, tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert assistant["codex_native"] is True
        assert tool["codex_native"] is True
        assert tool["tool_name"] == assistant["tool_calls"][0]["function"]["name"]
        assert "ghp_abcdefghijk1234" not in assistant["tool_calls"][0]["function"]["arguments"]
        assert "ghp_zyxwvutsrqponm9876" not in tool["content"]


class TestWebSearchProjection:
    def test_web_search_is_recorded_under_the_live_card_id(self) -> None:
        # The live bubble and the stored history must share name + call id, or a
        # refreshed transcript drops the card and shows a raw JSON note instead.
        from agent.codex_runtime import _codex_item_to_args, _codex_item_to_tool_name, _stable_call_id

        item = {"type": "webSearch", "id": "ws-1", "query": "Hermes Agent docs"}
        result = CodexEventProjector().project({"method": "item/completed", "params": {"item": item}})

        assert result.is_tool_iteration is True
        assistant, tool = result.messages
        call = assistant["tool_calls"][0]
        live_name = _codex_item_to_tool_name(item)
        assert call["function"]["name"] == live_name
        assert call["id"] == tool["tool_call_id"] == _stable_call_id(item, live_name)
        assert json.loads(call["function"]["arguments"]) == _codex_item_to_args(item)
        assert json.loads(tool["content"])["provider"] == "codex"


class TestUserAndOpaqueProjection:
    def test_user_message_text_fragments_only(self) -> None:
        item = {
            "type": "userMessage", "id": "u1",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "url": "http://x/y"},
                {"type": "text", "text": "world"},
            ],
        }
        msgs = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert msgs[0]["role"] == "user"
        assert "hello" in msgs[0]["content"]
        assert "world" in msgs[0]["content"]

    def test_opaque_item_recorded_without_fabricated_tool_calls(self) -> None:
        item = {"type": "plan", "id": "p1", "text": "do the thing"}
        msgs = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert "plan" in msgs[0]["content"].lower()
        assert "tool_calls" not in msgs[0]

    def test_opaque_item_hides_encrypted_payload_and_private_thread_ids(self) -> None:
        item = {
            "type": "futureReasoningCarrier", "id": "i1", "threadId": "private-thread",
            "encryptedContent": "opaque-ciphertext",
            "summary": "Codex-visible short summary",
        }
        msg = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages[0]
        assert "Codex-visible short summary" in msg["content"]
        assert "private-thread" not in msg["content"]
        assert "opaque-ciphertext" not in msg["content"]

    def test_opaque_item_also_consumes_pending_reasoning(self) -> None:
        projector = CodexEventProjector()
        projector.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "reason-opaque", "summary": ["Still readable."],
        }}})
        [message] = projector.project({"method": "item/completed", "params": {"item": {
            "type": "futureItem", "id": "future-1",
        }}}).messages
        assert message["reasoning"] == "Still readable."


class TestCollaborationProjection:
    def test_collaboration_projects_only_available_prompt_and_agent_messages(self) -> None:
        item = {
            "type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent",
            "status": "completed", "prompt": "Compute 7 + 5.",
            "senderThreadId": "sender-private", "receiverThreadIds": ["receiver-private"],
            "agentsStates": {"receiver-private": {"status": "completed", "message": "12"}},
        }
        assistant, tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        call = assistant["tool_calls"][0]
        assert call["function"]["name"] == "collab.spawnAgent"
        assert json.loads(call["function"]["arguments"]) == {"prompt": "Compute 7 + 5."}
        assert tool["tool_name"] == "collab.spawnAgent"
        assert tool["codex_native"] is True
        assert "12" in tool["content"]
        assert "sender-private" not in json.dumps([assistant, tool])
        assert "receiver-private" not in json.dumps([assistant, tool])

    def test_collaboration_without_details_says_codex_did_not_supply_them(self) -> None:
        item = {
            "type": "collabAgentToolCall", "id": "wait-1", "tool": "wait",
            "status": "completed", "receiverThreadIds": [], "senderThreadId": "private",
            "agentsStates": {},
        }
        assistant, tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {}
        assert "did not provide" in tool["content"]
        assert "private" not in json.dumps([assistant, tool])


class TestSubagentActivityProjection:
    def test_activity_is_a_clickable_native_tool_call_without_a_fake_result(self) -> None:
        item = {
            "type": "subAgentActivity", "id": "activity-1", "kind": "started",
            "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
            "agentPath": "research/repo-helper", "model": "gpt-5.5", "reasoningEffort": "high",
        }
        assistant, tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        call = assistant["tool_calls"][0]
        assert call["id"] == "codex_subagent_activity_activity-1"
        assert call["function"]["name"] == "codex_subagent_activity"
        assert json.loads(call["function"]["arguments"]) == {
            "kind": "started", "agent_path": "research/repo-helper", "model": "gpt-5.5",
            "reasoning_effort": "high", "agent_thread_id": item["agentThreadId"],
        }
        assert tool["role"] == "tool" and tool["tool_call_id"] == call["id"]
        assert tool["tool_name"] == "codex_subagent_activity" and tool["content"] == ""
        assert assistant["display_metadata"]["codex_native"] is True
        assert tool["display_metadata"]["codex_native"] is True

    def test_activity_sanitizes_local_paths_and_redacts_fields(self) -> None:
        item = {
            "type": "subAgentActivity", "id": "activity-2", "kind": "interacted",
            "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
            "agentPath": "/Users/private/.codex/agents/research.toml", "model": "ghp_abcdefghijk1234",
        }
        assistant, _tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        args = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
        assert args["kind"] == "interacted"
        assert args["model"] != item["model"]
        assert "ghp_abcdefghijk1234" not in json.dumps(assistant)
        assert args["agent_thread_id"] == item["agentThreadId"]
        assert "agent_path" not in args
        assert "/Users/private" not in json.dumps(assistant)

    def test_activity_keeps_pending_reasoning_on_the_assistant_call(self) -> None:
        projector = CodexEventProjector()
        projector.project({"method": "item/completed", "params": {"item": {
            "type": "reasoning", "id": "reason-1", "summary": ["Native reasoning."],
        }}})
        assistant, _tool = projector.project({"method": "item/completed", "params": {"item": {
            "type": "subAgentActivity", "id": "activity-3", "kind": "completed",
            "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
        }}}).messages
        assert assistant["reasoning"] == "Native reasoning."
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {
            "kind": "completed", "agent_thread_id": "019e1a94-352b-71e1-b214-e5c67c9ec190",
        }

    def test_unexpected_large_metadata_cannot_truncate_the_trace_pointer(self) -> None:
        item = {
            "type": "subAgentActivity", "id": "activity-4", "kind": "started",
            "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
            "model": "m" * 70_000, "reasoningEffort": "r" * 200,
        }
        assistant, _tool = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        args = json.loads(assistant["tool_calls"][0]["function"]["arguments"])
        assert args == {
            "kind": "started", "agent_thread_id": item["agentThreadId"],
        }


@pytest.mark.parametrize("item", [
    {"type": "commandExecution", "id": "c", "command": "printf ghp_abcdefghijk1234", "cwd": "/tmp",
     "aggregatedOutput": "ghp_zyxwvutsrqponm9876", "exitCode": 0},
    {"type": "fileChange", "id": "f", "status": "applied",
     "changes": [{"path": "/tmp/ghp_abcdefghijk1234", "kind": {"type": "update"},
                  "diff": "-ghp_zyxwvutsrqponm9876"}]},
    {"type": "mcpToolCall", "id": "m", "server": "demo", "tool": "read",
     "arguments": {"token": "ghp_abcdefghijk1234"}, "result": {"token": "ghp_zyxwvutsrqponm9876"}},
    {"type": "dynamicToolCall", "id": "d", "tool": "lookup",
     "arguments": {"token": "ghp_abcdefghijk1234"},
     "contentItems": [{"type": "text", "text": "ghp_zyxwvutsrqponm9876"}], "success": True},
    {"type": "collabAgentToolCall", "id": "a", "tool": "sendMessage", "status": "completed",
     "prompt": "ghp_abcdefghijk1234", "agentsStates": {"x": {"status": "completed", "message": "ghp_zyxwvutsrqponm9876"}}},
    {"type": "subAgentActivity", "id": "sa", "kind": "started",
     "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190", "agentPath": "ghp_abcdefghijk1234",
     "model": "ghp_zyxwvutsrqponm9876"},
    {"type": "webSearch", "id": "w", "query": "ghp_abcdefghijk1234"},
])
def test_every_native_tool_projection_redacts_credential_like_input_and_output(item):
    projected = CodexEventProjector().project(
        {"method": "item/completed", "params": {"item": item}}
    ).messages
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "ghp_abcdefghijk1234" not in serialized
    assert "ghp_zyxwvutsrqponm9876" not in serialized


def test_tool_args_and_results_have_explicit_display_bounds():
    from agent.transports.codex_event_projector import _MAX_PROJECTED_TOOL_TEXT, _bounded_tool_text

    args = json.loads(_format_tool_args({"payload": "x" * (_MAX_PROJECTED_TOOL_TEXT + 100)}))
    assert args["_truncated"] is True
    result = _bounded_tool_text("x" * (_MAX_PROJECTED_TOOL_TEXT + 100))
    assert len(result) <= _MAX_PROJECTED_TOOL_TEXT
    assert result.endswith("[truncated by Hermes display limit]")


class TestHelpers:
    def test_deterministic_call_id_stable(self) -> None:
        assert _deterministic_call_id("exec", "abc") == _deterministic_call_id("exec", "abc")
        assert _deterministic_call_id("exec", "abc") != _deterministic_call_id("exec", "xyz")

    def test_deterministic_call_id_handles_missing_id(self) -> None:
        # Should not raise, should be stable for same item type
        a = _deterministic_call_id("exec", "")
        b = _deterministic_call_id("exec", "")
        assert a == b

    def test_format_tool_args_sorted_keys(self) -> None:
        # Sorted keys = deterministic across replays = prefix cache stays valid
        a = _format_tool_args({"b": 1, "a": 2})
        b = _format_tool_args({"a": 2, "b": 1})
        assert a == b


class TestRoleAlternationInvariant:
    """The project must never emit two assistant messages back-to-back from
    one item — that breaks Hermes' message alternation invariant."""

    @pytest.mark.parametrize(
        "item",
        [
            {"type": "commandExecution", "id": "c1", "command": "x",
             "cwd": "/", "status": "completed", "aggregatedOutput": "",
             "exitCode": 0, "commandActions": []},
            {"type": "fileChange", "id": "f1", "status": "applied",
             "changes": []},
            {"type": "mcpToolCall", "id": "m1", "server": "s", "tool": "t",
             "status": "completed", "arguments": {}, "result": None,
             "error": None},
            {"type": "dynamicToolCall", "id": "d1", "tool": "x",
             "arguments": {}, "status": "completed",
             "contentItems": [], "success": True},
            {"type": "collabAgentToolCall", "id": "ca1", "tool": "wait",
             "prompt": None, "agentsStates": {}, "status": "completed"},
            {"type": "subAgentActivity", "id": "activity-1", "kind": "started",
             "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190"},
            {"type": "webSearch", "id": "w1", "query": "x"},
        ],
    )
    def test_tool_items_emit_assistant_then_tool(self, item) -> None:
        msgs = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages
        assert len(msgs) == 2
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == msgs[0]["tool_calls"][0]["id"]
