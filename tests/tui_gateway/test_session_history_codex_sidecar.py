"""RPC history must retain visible Responses-API assistant sidecars (#68321)."""

from __future__ import annotations

import threading

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session
from tui_gateway import server


def test_session_history_preserves_codex_message_items(tmp_path, monkeypatch):
    message_items = [
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Persisted answer"}],
        }
    ]
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(db, "stored-session", "desktop")
    seed_messages(db, "stored-session", {
        "role": "assistant",
        "content": "",
        "codex_message_items": message_items,
        "reasoning": "Thinking before the tool",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}],
    })
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    previous_db = server._db
    server._db = db
    server._sessions["runtime-session"] = {
        "session_key": "stored-session",
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "agent": None,
    }

    try:
        response = server.handle_request(
            {"id": "1", "method": "session.history", "params": {"session_id": "runtime-session"}}
        )
    finally:
        server._sessions.pop("runtime-session", None)
        server._db = previous_db
        db.close()

    assert isinstance(response, dict)
    assert "error" not in response
    assert response["result"]["count"] == 1
    messages = response["result"]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "assistant"
    assert messages[0]["text"] == ""
    assert messages[0]["codex_message_items"] == message_items


def test_session_history_keeps_linked_codex_tool_calls_results_and_reasoning():
    call = {"id": "codex_mcp__messaging__send_message_m1", "type": "function",
            "function": {"name": "mcp.messaging.send_message",
                         "arguments": '{"body": "fixture", "to": "fixture@example.invalid"}'}}
    native_history = [
        {"role": "user", "content": "Send the fixture message."},
        {"role": "assistant", "content": None, "codex_native": True,
         "reasoning": "The recipient is the fixture address.",
         "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "tool_name": call["function"]["name"],
         "codex_native": True, "content": "{\"delivered\": true}"},
        {"role": "assistant", "content": "The fixture message was sent.",
         "reasoning": "The tool returned a successful result."},
        {"role": "assistant", "content": None, "codex_native": True, "tool_calls": [
            {"id": "codex_exec_second", "type": "function",
             "function": {"name": "exec_command", "arguments": "{\"command\": \"pwd\"}"}},
        ]},
        {"role": "tool", "tool_call_id": "codex_exec_second", "tool_name": "exec_command",
         "codex_native": True, "content": ""},
    ]

    messages = server._history_to_messages(native_history)

    assert messages[1]["tool_calls"] == [call]
    assert messages[1]["reasoning"] == "The recipient is the fixture address."
    assert messages[2]["name"] == "mcp.messaging.send_message"
    assert messages[2]["content"] == "{\"delivered\": true}"
    assert messages[2]["tool_call_id"] == call["id"]
    assert messages[3]["reasoning"] == "The tool returned a successful result."
    assert messages[4]["tool_calls"][0]["id"] == "codex_exec_second"
    assert messages[5]["name"] == "exec_command"
    assert "content" in messages[5] and messages[5]["content"] == ""

    ordinary = server._history_to_messages([
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": "ordinary result"},
    ])
    assert len(ordinary) == 1
    assert ordinary[0]["role"] == "tool"
    assert "content" not in ordinary[0]
