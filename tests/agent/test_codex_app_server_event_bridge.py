"""Regression tests for the codex_app_server → Hermes UI event bridge.

Pin the translation of codex JSON-RPC notifications into agent callbacks
(`tool_progress_callback`, `_fire_stream_delta`, `_fire_reasoning_delta`,
`_emit_interim_assistant_message`) so Discord/Telegram/TUI continue to
surface live tool-progress bubbles and interim assistant commentary when
the active provider runs on `openai_runtime: codex_app_server` (#33200).

Each test drives `make_codex_app_server_event_bridge(agent)` directly with
fixture notifications that mirror codex 0.130.0's `item/*` shape and
asserts the right agent callback fired with the right arguments. The
bridge is deliberately small (~150 lines of pure dict mapping) so the
tests can stay focused on the wire format ↔ callback contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import (
    _codex_item_completion_payload,
    _codex_item_to_args,
    _codex_item_to_preview,
    _codex_item_to_tool_name,
    make_codex_app_server_event_bridge,
)

def _make_stub_agent() -> SimpleNamespace:
    """Minimal stand-in for AIAgent that records every callback fire."""
    return SimpleNamespace(
        tool_progress_callback=MagicMock(name="tool_progress_callback"),
        tool_start_callback=MagicMock(name="tool_start_callback"),
        tool_complete_callback=MagicMock(name="tool_complete_callback"),
        _fire_stream_delta=MagicMock(name="_fire_stream_delta"),
        _fire_reasoning_delta=MagicMock(name="_fire_reasoning_delta"),
        _emit_interim_assistant_message=MagicMock(
            name="_emit_interim_assistant_message"
        ),
    )

def _item_started(item: dict) -> dict:
    return {"method": "item/started", "params": {"item": item}}

def _item_completed(item: dict) -> dict:
    return {"method": "item/completed", "params": {"item": item}}

# ---------- name / args / preview / result mapping ----------

class TestCodexItemToToolName:

    def test_dynamic_tool_call_uses_tool_field(self):
        assert _codex_item_to_tool_name(
            {"type": "dynamicToolCall", "tool": "web_search"}
        ) == "web_search"

    def test_collaboration_tool_name_includes_operation(self):
        assert _codex_item_to_tool_name(
            {"type": "collabAgentToolCall", "tool": "sendMessage"}
        ) == "collab.sendMessage"

    def test_hermes_tools_mcp_server_emits_bare_tool_name(self):
        """The hermes-tools MCP server wraps Hermes' own tools for codex;
        the inner dispatch subprocess can't fire native progress events,
        so the codex-level event IS the display event — shown without the
        mcp.hermes-tools.* namespacing (from #26541 by @simpolism)."""
        assert _codex_item_to_tool_name(
            {"type": "mcpToolCall", "server": "hermes-tools", "tool": "web_search"}
        ) == "web_search"
        assert _codex_item_to_tool_name(
            {"type": "mcpToolCall", "server": "hermes-tools", "tool": "browser_navigate"}
        ) == "browser_navigate"

class TestCodexItemToArgs:

    def test_file_change_args_normalize_changes(self):
        args = _codex_item_to_args({
            "type": "fileChange",
            "changes": [
                {"path": "/a.py", "kind": {"type": "add"}},
                {"path": "/b.py", "kind": {"type": "update"}},
                "not-a-dict",
            ],
        })
        assert args == {
            "changes": [
                {"kind": "add", "path": "/a.py"},
                {"kind": "update", "path": "/b.py"},
            ]
        }

    def test_non_dict_arguments_get_wrapped(self):
        args = _codex_item_to_args({
            "type": "dynamicToolCall", "arguments": ["a", "b"],
        })
        assert args == {"arguments": ["a", "b"]}

    def test_collaboration_args_show_prompt_without_receiver_ids(self):
        args = _codex_item_to_args({
            "type": "collabAgentToolCall", "tool": "spawnAgent", "prompt": "Compute 7 + 5.",
            "receiverThreadIds": ["private-receiver"],
        })
        assert args == {"prompt": "Compute 7 + 5."}
        assert "private-receiver" not in str(args)

class TestCodexItemToPreview:
    def test_command_preview_truncated(self):
        long_cmd = "echo " + "x" * 500
        preview = _codex_item_to_preview({
            "type": "commandExecution", "command": long_cmd
        })
        assert preview is not None
        assert len(preview) <= 120

    def test_file_change_preview_lists_first_three_paths(self):
        preview = _codex_item_to_preview({
            "type": "fileChange",
            "changes": [
                {"path": f"/p{i}.py", "kind": {"type": "update"}}
                for i in range(5)
            ],
        })
        assert "/p0.py" in preview and "/p2.py" in preview
        assert "+2 more" in preview

class TestCodexItemCompletionPayload:
    def test_command_success_returns_aggregated_output(self):
        result, is_error = _codex_item_completion_payload({
            "type": "commandExecution",
            "exitCode": 0,
            "aggregatedOutput": "hello\nworld\n",
        })
        assert result == "hello\nworld\n"
        assert is_error is False

    def test_mcp_tool_error_is_error(self):
        result, is_error = _codex_item_completion_payload({
            "type": "mcpToolCall",
            "error": {"message": "nope"},
        })
        assert "[error]" in result
        assert is_error is True

    def test_collaboration_completion_shows_available_agent_message(self):
        result, is_error = _codex_item_completion_payload({
            "type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent",
            "status": "completed", "prompt": "Compute 7 + 5.",
            "senderThreadId": "private-sender", "receiverThreadIds": ["private-receiver"],
            "agentsStates": {"private-receiver": {"status": "completed", "message": "12"}},
        })
        assert '"message": "12"' in result
        assert "private-receiver" not in result and "private-sender" not in result
        assert is_error is False

    def test_file_change_completion_matches_redacted_stored_diff_projection(self):
        from agent.transports.codex_event_projector import CodexEventProjector

        secret = "ghp_zyxwvutsrqponm9876"
        item = {
            "type": "fileChange", "id": "file-1", "status": "applied",
            "changes": [{"kind": {"type": "update"}, "path": "/tmp/example.py",
                         "diff": f"-old\n+new\n+# {secret}"}],
        }

        live_result, is_error = _codex_item_completion_payload(item)
        call, stored_result = CodexEventProjector().project(
            {"method": "item/completed", "params": {"item": item}}
        ).messages

        assert is_error is False
        assert live_result == stored_result["content"]
        assert "update: /tmp/example.py\n-old\n+new" in live_result
        assert secret not in live_result
        args = call["tool_calls"][0]["function"]["arguments"]
        assert secret not in args

    def test_large_file_change_diff_is_bounded_with_marker(self):
        from agent.transports.codex_event_projector import _MAX_PROJECTED_TOOL_TEXT

        result, _is_error = _codex_item_completion_payload({
            "type": "fileChange", "status": "applied",
            "changes": [{"kind": {"type": "update"}, "path": "/tmp/large.py",
                         "diff": "+" * (_MAX_PROJECTED_TOOL_TEXT + 100)}],
        })
        assert len(result) <= _MAX_PROJECTED_TOOL_TEXT
        assert result.endswith("[truncated by Hermes display limit]")

    def test_large_live_result_is_bounded_with_marker(self):
        from agent.transports.codex_event_projector import _MAX_PROJECTED_TOOL_TEXT

        result, _is_error = _codex_item_completion_payload({
            "type": "commandExecution", "exitCode": 0,
            "aggregatedOutput": "x" * (_MAX_PROJECTED_TOOL_TEXT + 100),
        })
        assert len(result) <= _MAX_PROJECTED_TOOL_TEXT
        assert result.endswith("[truncated by Hermes display limit]")

# ---------- bridge: dispatch contracts ----------

class TestStreamDeltaDispatch:
    def test_agent_message_delta_fires_stream_delta(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge({"method": "item/agentMessage/delta",
                "params": {"delta": "hello "}})
        bridge({"method": "item/agentMessage/delta",
                "params": {"delta": "world"}})
        assert agent._fire_stream_delta.call_count == 2
        assert agent._fire_stream_delta.call_args_list[0].args == ("hello ",)
        assert agent._fire_stream_delta.call_args_list[1].args == ("world",)

    def test_reasoning_delta_fires_reasoning_callback(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({"type": "reasoning", "id": "reason-1"}))
        bridge({"method": "item/reasoning/delta",
                "params": {"itemId": "reason-1", "delta": "thinking..."}})
        agent._fire_reasoning_delta.assert_not_called()
        bridge(_item_completed({"type": "reasoning", "id": "reason-1", "summary": ["thinking..."], "content": []}))
        agent._fire_reasoning_delta.assert_called_once_with("thinking...")
        agent._fire_stream_delta.assert_not_called()

    @pytest.mark.parametrize("method", ["item/reasoning/summaryTextDelta", "item/reasoning/textDelta"])
    def test_codex_reasoning_summary_and_text_deltas_reach_reasoning_callback(self, method):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        item_id = "reason-2"
        bridge(_item_started({"type": "reasoning", "id": item_id}))
        bridge({"method": method, "params": {"itemId": item_id, "delta": "Codex exposed text"}})
        assert agent._fire_reasoning_delta.call_count == 0
        field = "summary" if "summary" in method else "content"
        bridge(_item_completed({"type": "reasoning", "id": item_id, field: ["Codex exposed text"]}))
        agent._fire_reasoning_delta.assert_called_once_with("Codex exposed text")
        agent._fire_stream_delta.assert_not_called()

    def test_reasoning_delta_redacts_credential_split_across_chunks(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        item_id = "reason-secret"
        bridge(_item_started({"type": "reasoning", "id": item_id}))
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "ghp_"}})
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "abcdefghijk1234"}})
        agent._fire_reasoning_delta.assert_not_called()
        bridge(_item_completed({"type": "reasoning", "id": item_id,
                                "summary": ["ghp_abcdefghijk1234"], "content": [],
                                "encryptedContent": "must not be displayed"}))
        shown = agent._fire_reasoning_delta.call_args.args[0]
        assert "ghp_abcdefghijk1234" not in shown
        assert "must not be displayed" not in shown

    def test_reasoning_delta_fallback_redacts_full_buffer_when_item_has_no_text_fields(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        item_id = "reason-fallback"
        bridge(_item_started({"type": "reasoning", "id": item_id}))
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "ghp_"}})
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "abcdefghijk1234"}})
        agent._fire_reasoning_delta.assert_not_called()
        bridge(_item_completed({"type": "reasoning", "id": item_id,
                                "encryptedContent": "not decoded"}))
        shown = agent._fire_reasoning_delta.call_args.args[0]
        assert "ghp_abcdefghijk1234" not in shown
        assert "not decoded" not in shown

    def test_reasoning_buffer_overflow_discards_content_instead_of_streaming_partial_text(self):
        from agent.transports.codex_event_projector import _MAX_REASONING_DISPLAY_CHARS

        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        item_id = "reason-overflow"
        bridge(_item_started({"type": "reasoning", "id": item_id}))
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "x" * _MAX_REASONING_DISPLAY_CHARS}})
        bridge({"method": "item/reasoning/summaryTextDelta",
                "params": {"itemId": item_id, "delta": "ghp_abcdefghijk1234"}})
        agent._fire_reasoning_delta.assert_not_called()
        bridge(_item_completed({"type": "reasoning", "id": item_id}))
        shown = agent._fire_reasoning_delta.call_args.args[0]
        assert "ghp_abcdefghijk1234" not in shown
        assert "omitted" in shown

    def test_distinct_reasoning_items_are_separated_in_live_stream(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        for item_id, text in (("reason-prefix", "ghp_"), ("reason-suffix", "abcdefghijk1234")):
            bridge(_item_started({"type": "reasoning", "id": item_id}))
            bridge({"method": "item/reasoning/summaryTextDelta",
                    "params": {"itemId": item_id, "delta": text}})
            bridge(_item_completed({"type": "reasoning", "id": item_id, "summary": [text]}))
        rendered = "".join(call.args[0] for call in agent._fire_reasoning_delta.call_args_list)
        assert "ghp_abcdefghijk1234" not in rendered
        assert rendered == "ghp_\nabcdefghijk1234"

    def test_unkeyed_reasoning_delta_is_dropped_when_multiple_items_are_active(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({"type": "reasoning", "id": "reason-a"}))
        bridge(_item_started({"type": "reasoning", "id": "reason-b"}))
        bridge({"method": "item/reasoning/summaryTextDelta", "params": {"delta": "ambiguous"}})
        bridge(_item_completed({"type": "reasoning", "id": "reason-a"}))
        bridge(_item_completed({"type": "reasoning", "id": "reason-b"}))
        agent._fire_reasoning_delta.assert_not_called()

class TestToolProgressDispatch:
    def test_command_started_fires_tool_started(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({
            "type": "commandExecution",
            "id": "exec-1",
            "command": "ls /tmp",
            "cwd": "/tmp",
        }))
        agent.tool_progress_callback.assert_called_once()
        call = agent.tool_progress_callback.call_args
        assert call.args[0] == "tool.started"
        assert call.args[1] == "exec_command"
        assert "ls /tmp" in call.args[2]  # preview
        assert call.args[3] == {"command": "ls /tmp", "cwd": "/tmp"}

    def test_command_completed_fires_tool_completed_with_result(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({
            "type": "commandExecution",
            "id": "exec-2",
            "command": "echo hi",
            "cwd": "/tmp",
        }))
        bridge(_item_completed({
            "type": "commandExecution",
            "id": "exec-2",
            "exitCode": 0,
            "aggregatedOutput": "hi\n",
            "durationMs": 42,
        }))
        # tool.started then tool.completed
        assert agent.tool_progress_callback.call_count == 2
        completed = agent.tool_progress_callback.call_args_list[1]
        assert completed.args[0] == "tool.completed"
        assert completed.args[1] == "exec_command"
        assert completed.args[2] is None  # preview unused on completion
        assert completed.args[3] is None  # args unused on completion
        assert completed.kwargs["duration"] == pytest.approx(0.042)
        assert completed.kwargs["is_error"] is False
        assert completed.kwargs["result"] == "hi\n"

    def test_web_search_builtin_fires_started_and_completed(self):
        """Codex's built-in webSearch produces a start/complete bubble pair
        with the query as preview and args (#26541)."""
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({
            "type": "webSearch",
            "id": "ws-1",
            "query": "hermes agent docs",
        }))
        bridge(_item_completed({
            "type": "webSearch",
            "id": "ws-1",
            "query": "hermes agent docs",
        }))
        calls = agent.tool_progress_callback.call_args_list
        assert [c.args[0] for c in calls] == ["tool.started", "tool.completed"]
        assert calls[0].args[1] == "web_search"
        assert calls[0].args[2] == "hermes agent docs"
        assert calls[0].args[3] == {"query": "hermes agent docs"}

    def test_collaboration_item_fires_correlated_start_and_completion(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        started = {
            "type": "collabAgentToolCall", "id": "collab-1", "tool": "spawnAgent",
            "prompt": "Compute 7 + 5.", "receiverThreadIds": ["private-receiver"],
        }
        completed = {
            **started, "status": "completed", "senderThreadId": "private-sender",
            "agentsStates": {"private-receiver": {"status": "completed", "message": "12"}},
        }
        bridge(_item_started(started))
        bridge(_item_completed(completed))
        start = agent.tool_start_callback.call_args.args
        done = agent.tool_complete_callback.call_args.args
        from agent.codex_runtime import _stable_call_id
        from agent.transports.codex_event_projector import CodexEventProjector
        projected = CodexEventProjector().project(_item_completed(completed)).messages[0]["tool_calls"][0]
        assert start[0] == done[0]
        assert start[0] == projected["id"] == _stable_call_id(completed, "collab.spawnAgent")
        assert start[1] == done[1] == "collab.spawnAgent"
        assert start[2] == done[2] == {"prompt": "Compute 7 + 5."}
        assert "12" in done[3]
        assert "private-receiver" not in str(start + done)
        assert "private-sender" not in str(start + done)

    def test_subagent_activity_live_callbacks_match_persisted_call_and_do_not_claim_a_result(self):
        from agent.transports.codex_event_projector import CodexEventProjector

        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        thread_id = "019e1a94-352b-71e1-b214-e5c67c9ec190"
        started = {
            "type": "subAgentActivity", "id": "activity-1", "kind": "started",
            "agentThreadId": thread_id, "agentPath": "research/repo-helper", "model": "gpt-5.5",
        }
        bridge(_item_started(started))
        bridge(_item_completed(started))

        start_id, name, args = agent.tool_start_callback.call_args.args
        done_id, done_name, done_args, result = agent.tool_complete_callback.call_args.args
        projected_call, projected_result = CodexEventProjector().project(
            _item_completed(started)
        ).messages
        assert (name, done_name) == ("codex_subagent_activity", "codex_subagent_activity")
        assert start_id == done_id == projected_call["tool_calls"][0]["id"]
        assert args == done_args == {
            "kind": "started", "agent_path": "research/repo-helper", "model": "gpt-5.5",
            "agent_thread_id": thread_id,
        }
        assert result == projected_result["content"] == ""
        calls = agent.tool_progress_callback.call_args_list
        assert [call.args[0] for call in calls] == ["tool.started", "tool.completed"]
        assert [call.args[1] for call in calls] == [name, name]

    def test_live_native_tool_input_and_output_are_redacted(self):
        agent = _make_stub_agent()
        agent.tool_start_callback = MagicMock()
        agent.tool_complete_callback = MagicMock()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_started({
            "type": "mcpToolCall", "id": "mcp-secret", "server": "demo", "tool": "lookup",
            "arguments": {"token": "ghp_abcdefghijk1234"},
        }))
        bridge(_item_completed({
            "type": "mcpToolCall", "id": "mcp-secret", "server": "demo", "tool": "lookup",
            "arguments": {"token": "ghp_abcdefghijk1234"},
            "result": {"token": "ghp_zyxwvutsrqponm9876"}, "error": None,
        }))
        start = agent.tool_start_callback.call_args.args
        complete = agent.tool_complete_callback.call_args.args
        assert "ghp_abcdefghijk1234" not in str(start)
        assert "ghp_zyxwvutsrqponm9876" not in str(complete)

    def test_live_projection_redacts_every_native_tool_item_type(self):
        agent = _make_stub_agent()
        agent.tool_start_callback = MagicMock()
        agent.tool_complete_callback = MagicMock()
        bridge = make_codex_app_server_event_bridge(agent)
        secret_in, secret_out = "ghp_abcdefghijk1234", "ghp_zyxwvutsrqponm9876"
        items = [
            {"type": "commandExecution", "id": "c", "command": f"printf {secret_in}", "cwd": "/tmp",
             "aggregatedOutput": secret_out, "exitCode": 0},
            {"type": "fileChange", "id": "f", "status": "applied",
             "changes": [{"path": f"/tmp/{secret_in}", "kind": {"type": "update"}}]},
            {"type": "mcpToolCall", "id": "m", "server": "demo", "tool": "read",
             "arguments": {"token": secret_in}, "result": {"token": secret_out}},
            {"type": "dynamicToolCall", "id": "d", "tool": "lookup",
             "arguments": {"token": secret_in},
             "contentItems": [{"type": "text", "text": secret_out}], "success": True},
            {"type": "collabAgentToolCall", "id": "a", "tool": "sendMessage", "status": "completed",
             "prompt": secret_in, "agentsStates": {"x": {"status": "completed", "message": secret_out}}},
            {"type": "subAgentActivity", "id": "sa", "kind": "started",
             "agentThreadId": "019e1a94-352b-71e1-b214-e5c67c9ec190",
             "agentPath": secret_in, "model": secret_out},
            {"type": "webSearch", "id": "w", "query": secret_in},
        ]
        for item in items:
            bridge(_item_started(item))
            bridge(_item_completed(item))
        emitted = str((agent.tool_start_callback.call_args_list, agent.tool_complete_callback.call_args_list))
        assert secret_in not in emitted
        assert secret_out not in emitted
        assert agent.tool_start_callback.call_count == len(items)
        assert agent.tool_complete_callback.call_count == len(items)

class TestAgentMessageInterimDispatch:
    def test_completed_agent_message_emits_interim(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_completed({
            "type": "agentMessage",
            "id": "am-1",
            "text": "I'll check the config first.",
        }))
        agent._emit_interim_assistant_message.assert_called_once_with(
            {"role": "assistant", "content": "I'll check the config first."}
        )

    def test_show_commentary_off_suppresses_interim(self):
        """display.show_commentary=false silences agentMessage interim
        delivery on the app-server runtime (same contract as the
        codex_responses commentary channel)."""
        agent = _make_stub_agent()
        agent.show_commentary = False
        bridge = make_codex_app_server_event_bridge(agent)
        bridge(_item_completed({
            "type": "agentMessage", "id": "am-5", "text": "I'll check config.",
        }))
        agent._emit_interim_assistant_message.assert_not_called()
        # Tool progress is unaffected by the commentary toggle.
        bridge(_item_started({
            "type": "commandExecution", "id": "cmd-1", "command": "ls",
        }))
        agent.tool_progress_callback.assert_called_once()

class TestBridgeRobustness:

    def test_missing_params_is_ignored(self):
        agent = _make_stub_agent()
        bridge = make_codex_app_server_event_bridge(agent)
        bridge({"method": "item/started"})
        bridge({"method": "item/completed"})
        agent.tool_progress_callback.assert_not_called()

    def test_callback_exceptions_do_not_propagate(self):
        # Buggy display callbacks must not tear down the codex turn loop.
        agent = _make_stub_agent()
        agent.tool_progress_callback.side_effect = RuntimeError("boom")
        agent._fire_stream_delta.side_effect = RuntimeError("boom")
        agent._emit_interim_assistant_message.side_effect = RuntimeError("boom")
        bridge = make_codex_app_server_event_bridge(agent)
        # All three paths must swallow exceptions silently.
        bridge(_item_started({
            "type": "commandExecution", "id": "exec-x", "command": "ls",
        }))
        bridge({"method": "item/agentMessage/delta",
                "params": {"delta": "x"}})
        bridge(_item_completed({
            "type": "agentMessage", "id": "am-x", "text": "hi",
        }))

# ---------- end-to-end: bridge is wired in run_codex_app_server_turn ----------


@pytest.mark.parametrize("note", [
    {"method": method, "params": {"delta": "progress"}}
    for method in (
        "item/agentMessage/delta", "item/reasoning/delta", "item/reasoning/summaryDelta",
        "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
        "item/commandExecution/outputDelta", "item/fileChange/outputDelta",
    )
] + [
    {"method": method, "params": {"item": {"id": "i1", "type": kind, "text": "progress"}}}
    for method in ("item/started", "item/completed")
    for kind in ("agentMessage", "reasoning", "commandExecution", "mcpToolCall", "dynamicToolCall",
                 "collabAgentToolCall", "subAgentActivity")
])
def test_real_progress_keeps_watchdog_alive_but_silence_still_aborts(monkeypatch, note):
    """Display hooks are optional; real progress must reach the watchdog regardless."""
    import threading
    from agent import activity_tracking, turn_liveness

    clock = SimpleNamespace(now=1000.0)
    timer = SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now)
    monkeypatch.setattr(activity_tracking, "time", timer)
    monkeypatch.setattr(turn_liveness, "time", timer)
    agent = activity_tracking.ActivityTrackingMixin()
    agent.show_commentary = False
    agent._touch_activity("starting new turn")
    abort = MagicMock(return_value=True)
    watchdog = turn_liveness.TurnLivenessWatchdog(
        agent, session_id="test", timeout_s=600, poll_s=1,
        stop_event=threading.Event(), activity_lock=agent._liveness_activity_lock(),
        is_turn_active=lambda: True, commit_abort=abort, deactivate_turn=MagicMock(),
    )
    bridge = make_codex_app_server_event_bridge(agent)
    # More than 600 seconds of total runtime, with progress every 300 seconds.
    for _ in range(4):
        clock.now += 300
        bridge(note)
        watchdog._tick()
        abort.assert_not_called()
    last_activity = agent._last_activity_ts
    # Empty deltas / unrelated transport frames must not manufacture progress.
    clock.now += 601
    for idle_note in (
        {"method": "item/agentMessage/delta", "params": {"delta": ""}},
        {"method": "thread/tokenUsage/updated", "params": {}},
        {"method": "unknown/keepalive", "params": {}},
        {"method": "item/started", "params": {}},
    ):
        bridge(idle_note)
    assert agent._last_activity_ts == last_activity
    assert watchdog._tick() is False
    abort.assert_called_once()


@pytest.mark.parametrize("completion_only", [False, True])
def test_native_command_secret_is_redacted_in_display_but_not_execution(monkeypatch, completion_only):
    """#72298: an API key in a native codex command must be masked in the progress preview and both
    tool cards (including completion-only items), while the raw item that codex executes stays intact."""
    from agent import redact

    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    secret = "sk-ant-api03-" + "A1b2C3d4E5" * 4
    agent = _make_stub_agent()
    agent.tool_start_callback = MagicMock(name="tool_start_callback")
    agent.tool_complete_callback = MagicMock(name="tool_complete_callback")
    item = {"type": "commandExecution", "id": "cmd", "command": "echo " + secret, "cwd": ".", "exitCode": 0}
    bridge = make_codex_app_server_event_bridge(agent)
    if not completion_only:
        bridge(_item_started(item))
    bridge(_item_completed(item))

    shown = [repr(c) for cb in (agent.tool_progress_callback, agent.tool_start_callback,
                                agent.tool_complete_callback) for c in cb.call_args_list]
    assert agent.tool_complete_callback.call_args.args[2]["command"].startswith("echo ")
    assert all(secret not in s for s in shown), shown
    assert item["command"] == "echo " + secret
