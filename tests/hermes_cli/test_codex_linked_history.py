"""Native Codex history remains authoritative for linked session display."""

from contextlib import contextmanager
import asyncio
import json
import pytest

from hermes_cli import codex_linked_threads, codex_linked_history


def _overlay(turn_id, final_text, *, held=False, platform="local"):
    return {turn_id: {"version": 1, "thread_id": "native-thread", "turn_id": turn_id,
                      "held": held, "final_visible": bool(final_text), "final_text": final_text,
                      "platform": platform}}


def test_read_display_turns_stops_after_bounded_pages_even_with_fresh_empty_cursors():
    class Client:
        calls = 0

        def request(self, method, params):
            assert method == "thread/turns/list"
            assert params["limit"] == 100
            self.calls += 1
            return {"data": [], "nextCursor": f"fresh-{self.calls}"}

    client = Client()
    try:
        codex_linked_history.read_display_turns(
            "00000000-0000-0000-0000-000000000001", client=client, max_turns=1,
        )
    except ValueError as exc:
        assert "safe native page limit" in str(exc)
    else:
        raise AssertionError("history reader accepted an unbounded stream of empty pages")
    assert client.calls == 2  # ceil(1 turn / 100) plus one terminal page


def test_display_history_pages_newest_first_and_displays_chronologically(monkeypatch):
    pages = {
        None: {"turns": [{"id": "t1", "items": [
            {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "first"}]},
            {"id": "a1", "type": "agentMessage", "text": "response one"},
        ]}], "next_cursor": "newer"},
        "newer": {"turns": [{"id": "t2", "items": [
            {"id": "u2", "type": "userMessage", "content": [{"type": "text", "text": "second"}]},
            {"id": "a2", "type": "agentMessage", "text": "response two"},
        ]}], "next_cursor": None},
    }

    @contextmanager
    def client():
        yield object()

    def history(_thread_id, cursor, limit, sort_direction, client):
        assert sort_direction == "asc"
        return pages[cursor]

    monkeypatch.setattr(codex_linked_threads, "_codex_client", client)
    monkeypatch.setattr(codex_linked_threads, "read_codex_history", history)
    rows = codex_linked_history.read_display_history("thread", session_id="test-session",
                                                     limit=3, latest=True, display_overlays={
                                                         **_overlay("t1", "response one"),
                                                         **_overlay("t2", "response two"),
                                                     })
    assert [row["content"] for row in rows] == ["response one", "second", "response two"]
    assert [row["role"] for row in rows] == ["assistant", "user", "assistant"]


def test_display_history_flushes_reasoning_at_each_native_turn_boundary(monkeypatch):
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    turns = [
        {"id": "interrupted", "completedAtMs": 2000, "items": [
            {"id": "reason-only", "type": "reasoning", "startedAtMs": 1900,
             "summary": ["The native turn ended while I checked the output."]},
        ]},
        {"id": "next", "items": [
            {"id": "user-next", "type": "userMessage",
             "content": [{"type": "text", "text": "Continue."}]},
            {"id": "answer-next", "type": "agentMessage", "text": "Continuing now."},
        ]},
    ]

    @contextmanager
    def client():
        class Client:
            def request(self, method, params):
                assert method == "thread/turns/list"
                assert params["threadId"] == thread_id
                return {"data": turns, "nextCursor": None}

        yield Client()

    monkeypatch.setattr(codex_linked_threads, "_codex_client", client)
    rows = codex_linked_history.read_display_history(thread_id, session_id="test-session", display_overlays={
        **_overlay("interrupted", None), **_overlay("next", "Continuing now."),
    })

    assert rows[0]["role"] == "assistant"
    assert rows[0]["reasoning"] == "The native turn ended while I checked the output."
    assert rows[1]["content"] == "Continue."
    assert rows[2]["role"] == "assistant"
    assert rows[2]["content"] == "Continuing now."


def test_unrecorded_linked_turns_are_hidden_only_while_the_strict_policy_is_asked_for(monkeypatch):
    """The fail-closed path still EXISTS (``trust_unrecorded=False``); it is no longer the default.

    A turn with no stored overlay cannot have run a profile output transform — overlays are written
    only by Hermes' own turn path — so the production call sites pass ``trust_unrecorded=True`` and
    the turn renders under the current policy. The strict contract is preserved for any caller that
    must not guess; this test pins BOTH halves so neither can be deleted silently.
    """
    turn = codex_linked_history.native_turn_record({
        "id": "pre-overlay-turn",
        "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "question"}]},
            {"type": "agentMessage", "text": "answer with no stored policy"},
        ],
    })
    strict = codex_linked_history.display_native_turn_records(
        [turn], session_id="stored-session", thread_id="native-thread", model="codex-model",
        require_overlay=True)
    strict_rows = strict[0]["rows"]
    assert strict_rows[0]["content"] == "question"
    assert strict_rows[1]["display_kind"] == "hidden"
    assert strict_rows[1]["content"] is None
    assert strict_rows[2]["role"] == "system"
    assert strict_rows[2]["display_kind"] == "codex_history_unavailable"
    assert "saved display policy is unavailable" in strict_rows[2]["content"]

    trusted = codex_linked_history.display_native_turn_records(
        [turn], session_id="stored-session", thread_id="native-thread", model="codex-model",
        require_overlay=True, trust_unrecorded=True)
    trusted_rows = trusted[0]["rows"]
    assert trusted_rows[0]["content"] == "question"
    assert trusted_rows[1]["content"] == "answer with no stored policy"
    assert all(row.get("display_kind") != "codex_history_unavailable" for row in trusted_rows)


def test_a_trusted_unrecorded_turn_renders_its_whole_native_transcript(monkeypatch):
    """The user's acceptance standard: user rows, CoT, tool call+args+result, final answer — no notice.

    No profile transform can have applied to a turn Hermes never ran, so the trusted path projects
    the native transcript as-is (redacted) instead of hiding it behind ``codex_history_unavailable``.
    """
    turn = codex_linked_history.native_turn_record({
        "id": "native-cli-turn",
        "status": "completed",
        "items": [
            {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "list /tmp"}]},
            {"id": "r", "type": "reasoning",
             "summary": [{"type": "summary_text", "text": "I should read the directory."}]},
            {"id": "c", "type": "commandExecution", "command": "ls /tmp", "cwd": "/tmp",
             "aggregatedOutput": "alpha\nbeta\n", "exitCode": 0},
            {"id": "a", "type": "agentMessage", "text": "Two entries: alpha and beta."},
        ],
    }, thread_id="native-thread")

    rows = codex_linked_history.display_native_turn_records(
        [turn], session_id="stored-session", thread_id="native-thread", model="codex-model",
        require_overlay=True, trust_unrecorded=True)[0]["rows"]

    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == "list /tmp"
    assert rows[1]["reasoning"] == "I should read the directory."
    assert rows[1]["tool_calls"][0]["function"]["name"] == "exec_command"
    assert json.loads(rows[1]["tool_calls"][0]["function"]["arguments"]) == {
        "command": "ls /tmp", "cwd": "/tmp"}
    assert json.loads(rows[2]["content"]) == {"exit_code": 0, "output": "alpha\nbeta\n"}
    assert rows[2]["tool_call_id"] == rows[1]["tool_calls"][0]["id"]
    assert rows[3]["content"] == "Two entries: alpha and beta."
    assert all(row.get("display_kind") != "codex_history_unavailable" for row in rows)


def test_a_trusted_turn_still_obeys_a_stored_overlay_strictly(monkeypatch):
    """Trusting an UNRECORDED turn must not loosen a turn that DOES have a stored policy.

    ``held`` hides the interim rows, ``final_visible=False`` hides the final, and a missing
    ``final_text`` keeps the native text instead of inventing one.
    """
    turn = codex_linked_history.native_turn_record({
        "id": "recorded-turn",
        "items": [
            {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "hi"}]},
            {"id": "interim", "type": "agentMessage", "text": "working on it"},
            {"id": "final", "type": "agentMessage", "text": "native final"},
        ],
    }, thread_id="native-thread")

    held_rows = codex_linked_history.display_native_turn_records(
        [turn], session_id="s", thread_id="native-thread", model=None,
        overlays={"recorded-turn": {"held": True, "final_visible": True, "final_text": "stored final"}},
        require_overlay=True, trust_unrecorded=True)[0]["rows"]
    assert held_rows[1]["display_kind"] == "hidden" and held_rows[1]["content"] is None
    assert held_rows[2]["content"] == "stored final"

    hidden_final = codex_linked_history.display_native_turn_records(
        [turn], session_id="s", thread_id="native-thread", model=None,
        overlays={"recorded-turn": {"held": False, "final_visible": False, "final_text": None}},
        require_overlay=True, trust_unrecorded=True)[0]["rows"]
    assert hidden_final[1]["content"] == "working on it"
    assert hidden_final[2]["display_kind"] == "hidden" and hidden_final[2]["content"] is None

    # ``final_visible=True`` without ``final_text`` is an overlay that claims output it cannot
    # produce: it still hides, because the stored policy is the authority, not the native text.
    missing_text = codex_linked_history.display_native_turn_records(
        [turn], session_id="s", thread_id="native-thread", model=None,
        overlays={"recorded-turn": {"held": False, "final_visible": True}},
        require_overlay=True, trust_unrecorded=True)[0]["rows"]
    assert missing_text[2]["display_kind"] == "hidden" and missing_text[2]["content"] is None


def test_the_trusted_path_still_redacts_reasoning_and_tool_content(monkeypatch):
    """Rendering an unrecorded turn must not turn the display path into a secret leak."""
    from agent import redact as redact_module

    secret = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWX"
    monkeypatch.setattr(redact_module, "redact_sensitive_text",
                        lambda value, force=False: value.replace(secret, "[redacted]"))
    turn = codex_linked_history.native_turn_record({
        "id": "leaky-turn",
        "items": [
            {"id": "r", "type": "reasoning",
             "summary": [{"type": "summary_text", "text": f"the key is {secret}"}]},
            {"id": "c", "type": "commandExecution", "command": f"export KEY={secret}",
             "cwd": "/tmp", "aggregatedOutput": f"exported {secret}", "exitCode": 0},
            {"id": "a", "type": "agentMessage", "text": f"used {secret}"},
        ],
    }, thread_id="native-thread")

    rendered = codex_linked_history.display_native_turn_records(
        [turn], session_id="s", thread_id="native-thread", model=None,
        require_overlay=True, trust_unrecorded=True)

    assert secret not in json.dumps(rendered)
    rows = rendered[0]["rows"]
    assert rows[0]["reasoning"] == "the key is [redacted]"
    assert json.loads(rows[1]["content"])["output"] == "exported [redacted]"
    assert rows[2]["content"] == "used [redacted]"


def test_display_history_projects_full_native_items_with_tools_and_reasoning(monkeypatch):
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    items = [
        {"id": "user-1", "type": "userMessage", "startedAtMs": 1000,
         "content": [{"type": "text", "text": "Send a test message."}]},
        {"id": "reason-1", "type": "reasoning", "startedAtMs": 1100,
         "summary": ["I should verify the recipient first."], "content": ["The request names the test address."]},
        {"id": "command-1", "type": "commandExecution", "startedAtMs": 1200,
         "command": "pwd", "cwd": "/tmp/project", "aggregatedOutput": "/tmp/project", "exitCode": 0},
        {"id": "answer-1", "type": "agentMessage", "startedAtMs": 1300,
         "text": "The workspace is /tmp/project."},
        {"id": "reason-2", "type": "reasoning", "startedAtMs": 1400,
         "summary": ["Now I can send the requested message."]},
        {"id": "mcp-1", "type": "mcpToolCall", "startedAtMs": 1500,
         "server": "messaging", "tool": "send_message",
         "arguments": {"to": "fixture@example.invalid", "body": "fixture body"},
         "result": {"delivered": True}},
        {"id": "collab-1", "type": "collabAgentToolCall", "startedAtMs": 1550,
         "tool": "send_message", "prompt": "Send a message to fixture@example.invalid.",
         "status": "completed", "agentsStates": {
             "worker-private-id": {"status": "completed", "message": "Message delivered."},
         }},
        {"id": "answer-2", "type": "agentMessage", "startedAtMs": 1600,
         "text": "The test message was sent."},
    ]

    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            assert params == {"cursor": None, "limit": 100, "threadId": thread_id,
                              "sortDirection": "asc", "itemsView": "full"}
            return {"data": [{"id": "turn-1", "items": items}], "nextCursor": None}

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(codex_linked_threads, "_codex_client", client)
    rows = codex_linked_history.read_display_history(thread_id, session_id="test-session",
                                                     display_overlays=_overlay("turn-1", "The test message was sent."))

    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant",
                                             "assistant", "tool", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == "Send a test message."
    assert rows[1]["reasoning"] == (
        "I should verify the recipient first.\nThe request names the test address.")
    assert rows[1]["codex_native"] is True
    assert rows[1]["tool_calls"][0]["function"]["name"] == "exec_command"
    assert json.loads(rows[1]["tool_calls"][0]["function"]["arguments"]) == {
        "command": "pwd", "cwd": "/tmp/project"}
    assert rows[2]["content"] == '{"exit_code": 0, "output": "/tmp/project"}'
    assert rows[2]["codex_native"] is True
    assert rows[2]["tool_name"] == "exec_command"
    assert rows[3]["content"] == "The workspace is /tmp/project."
    assert rows[4]["reasoning"] == "Now I can send the requested message."
    assert rows[4]["tool_calls"][0]["function"]["name"] == "mcp.messaging.send_message"
    assert json.loads(rows[4]["tool_calls"][0]["function"]["arguments"]) == {
        "body": "fixture body", "to": "fixture@example.invalid"}
    assert rows[5]["content"] == '{"delivered": true}'
    assert rows[5]["tool_call_id"] == rows[4]["tool_calls"][0]["id"]
    assert rows[6]["tool_calls"][0]["function"]["name"] == "collab.send_message"
    collab_args = json.loads(rows[6]["tool_calls"][0]["function"]["arguments"])
    assert collab_args["prompt"] == "Send a message to fixture@example.invalid."
    collab_result = json.loads(rows[7]["content"])
    assert collab_result["status"] == "completed"
    assert collab_result["agents"] == [{"status": "completed", "message": "Message delivered."}]
    assert "worker-private-id" not in rows[7]["content"]
    assert rows[8]["content"] == "The test message was sent."

    from tui_gateway import server
    display = server._history_to_messages(rows)
    assert display[1]["tool_calls"] == rows[1]["tool_calls"]
    assert display[1]["reasoning"] == rows[1]["reasoning"]
    assert display[2]["content"] == rows[2]["content"]
    assert display[4]["tool_calls"] == rows[4]["tool_calls"]
    assert display[5]["content"] == rows[5]["content"]
    assert display[6]["tool_calls"] == rows[6]["tool_calls"]
    assert display[7]["content"] == rows[7]["content"]


def test_history_ask_is_the_richest_view_codex_serves_and_makes_reasoning_optional(monkeypatch):
    """The history read already sends the maximum ``itemsView``; reasoning may still be absent.

    Codex's ``TurnItemsView`` has only ``notLoaded`` / ``summary`` / ``full``: there is no deeper
    reasoning flag to ask for. When the model ran with reasoning summaries off, an item is recorded
    with ``summary: []``/``content: []`` and every view — live item/completed, thread/items/read —
    returns it empty. The display layer must therefore treat empty reasoning as normal, and must
    still show the turn's user row, tool call, tool result and final answer.
    """
    thread_id = "01a212d9-09be-7fb3-b8c7-90082e4fb805"
    seen = {}

    class Client:
        def request(self, method, params):
            seen["params"] = params
            return {"data": [{"id": "t", "status": "completed", "items": [
                {"id": "u", "type": "userMessage",
                 "content": [{"type": "text", "text": "what is in /tmp"}]},
                # The real shape observed on the user's thread: both arrays empty.
                {"id": "r", "type": "reasoning", "summary": [], "content": [],
                 "encrypted_content": "opaque"},
                {"id": "c", "type": "commandExecution", "command": "ls /tmp",
                 "cwd": "/tmp", "aggregatedOutput": "a\nb\n", "exitCode": 0},
                {"id": "a", "type": "agentMessage", "text": "two entries"},
            ]}], "nextCursor": None}

    assert codex_linked_threads.read_codex_history(thread_id, client=Client())["turns"]
    assert seen["params"]["itemsView"] == "full"
    assert set(seen["params"]) == {"cursor", "limit", "threadId", "sortDirection", "itemsView"}, \
        "the request must not grow a parameter Codex does not serve"

    @contextmanager
    def client():
        yield Client()

    monkeypatch.setattr(codex_linked_threads, "_codex_client", client)
    rows = codex_linked_history.read_display_history(
        thread_id, session_id="s", display_overlays=_overlay("t", "two entries"))

    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[0]["content"] == "what is in /tmp"
    assert rows[1]["tool_calls"][0]["function"]["name"] == "exec_command"
    assert "reasoning" not in rows[1], "an empty reasoning item must not invent CoT"
    assert json.loads(rows[2]["content"])["output"] == "a\nb\n"
    assert rows[3]["content"] == "two entries"


def test_native_projection_preserves_fallback_timestamps_and_policy_filtered_rows(monkeypatch):
    class ProfileHooks:
        def __init__(self):
            self.calls = []

        def holds_stream(self):
            return True

        def held_transforms(self):
            return (self.rewrite,)

        def rewrite(self, **payload):
            self.calls.append(payload)
            return "filtered final reply"

        def run_held_transforms(self, callbacks, **payload):
            return callbacks[0](**payload), False

    hooks = ProfileHooks()
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: hooks)
    monkeypatch.setattr("agent.redact.redact_sensitive_text",
                        lambda value, force=False: value.replace("PRIVATE", "[redacted]") if force else value)
    turn = {
        "id": "stable-turn",
        "status": "completed",
        "startedAtMs": 1_700_000_000_000,
        "items": [
            {"type": "userMessage", "id": "user", "content": [{"type": "text", "text": "PRIVATE prompt"}]},
            {"type": "agentMessage", "id": "commentary", "text": "PRIVATE interim"},
            {"type": "agentMessage", "id": "answer", "text": "PRIVATE answer"},
        ],
    }
    record = codex_linked_history.native_turn_record(turn)
    safe_turns = codex_linked_history.display_native_turn_records(
        [record], session_id="stored-session", thread_id="native-thread", model="codex-model")

    assert [row["timestamp"] for row in record["rows"]] == [1_700_000_000.0] * 3
    assert safe_turns[0].keys() == {"id", "status", "rows"}
    assert "items" not in safe_turns[0] and "final_text" not in safe_turns[0]
    assert safe_turns[0]["rows"][0]["content"] == "[redacted] prompt"
    assert safe_turns[0]["rows"][1]["display_kind"] == "hidden"
    assert safe_turns[0]["rows"][1]["content"] is None
    assert safe_turns[0]["rows"][2]["content"] == "filtered final reply"
    assert hooks.calls[0]["turn_id"] == "codex-native:native-thread:stable-turn"
    assert hooks.calls[0]["session_id"] == "stored-session"
    monkeypatch.setattr(codex_linked_history, "read_display_turns",
                        lambda *_args, **_kwargs: [record])

    def disabled_or_reloaded_registry():
        raise AssertionError("settled history must not consult the current plugin registry")

    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", disabled_or_reloaded_registry)
    reloaded_rows = codex_linked_history.read_display_history(
        "native-thread", session_id="stored-session", model="codex-model",
        display_overlays=_overlay("stable-turn", "filtered final reply", held=True))
    assert reloaded_rows[0]["content"] == "[redacted] prompt"
    assert reloaded_rows[1]["content"] is None
    assert reloaded_rows[2]["content"] == "filtered final reply"
    assert len(hooks.calls) == 1


def test_native_turn_seconds_timestamps_are_mapped_without_millisecond_scaling():
    record = codex_linked_history.native_turn_record({
        "id": "seconds-turn",
        "startedAt": 1_700_000_000,
        "completedAt": 1_700_000_003,
        "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "question"}]},
            {"type": "agentMessage", "text": "answer"},
            {"type": "reasoning", "summary": ["post-answer boundary reasoning"]},
        ],
    })
    assert [row.get("timestamp") for row in record["rows"]] == [
        1_700_000_000, 1_700_000_000, 1_700_000_003,
    ]


def test_display_page_is_bounded_and_never_returns_raw_native_items(monkeypatch):
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"

    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            assert params["threadId"] == thread_id
            return {"data": [{"id": "native-turn", "status": "completed", "items": [
                {"type": "agentMessage", "id": "answer", "text": "safe answer", "secretField": "PRIVATE"},
            ]}], "nextCursor": "next-page"}

    result = codex_linked_history.display_native_turn_page(
        thread_id, session_id="stored-session", model="codex-model", limit=1, client=Client(),
        display_overlays=_overlay("native-turn", "safe answer"))
    assert result["next_cursor"] == "next-page"
    # The row carries its stable identity stamp (desktop row keying / cross-page dedupe) and nothing
    # from the raw native item — ``secretField`` must never survive the projection.
    assert [(t["id"], t["status"], t["rows"]) for t in result["turns"]] == [
        ("native-turn", "completed", [{"role": "assistant", "content": "safe answer",
                                       "codex_native_row_id":
                                           result["turns"][0]["rows"][0]["codex_native_row_id"]}])]
    assert result["turns"][0]["rows"][0]["codex_native_row_id"].startswith("codex:")
    assert "secretField" not in json.dumps(result)

    with pytest.raises(ValueError, match="display byte limit"):
        codex_linked_history.display_native_turn_page(
            thread_id, session_id="stored-session", model="codex-model", limit=1,
            client=Client(), max_bytes=1, display_overlays=_overlay("native-turn", "safe answer"))


def test_display_page_loads_only_its_native_turn_overlay_ids(monkeypatch):
    selected_ids = []
    turns = [
        {"id": "page-turn-1", "items": [{"type": "agentMessage", "text": "native one"}]},
        {"id": "page-turn-2", "items": [{"type": "agentMessage", "text": "native two"}]},
    ]

    monkeypatch.setattr(codex_linked_threads, "read_codex_history",
                        lambda *_args, **_kwargs: {"turns": turns, "next_cursor": "next"})

    def load_overlays(_db, session_id, thread_id, turn_ids):
        assert (session_id, thread_id) == ("stored-session", "thread")
        selected_ids.extend(turn_ids)
        return {**_overlay("page-turn-1", "safe one"), **_overlay("page-turn-2", "safe two")}

    monkeypatch.setattr(codex_linked_history, "linked_turn_display_overlays", load_overlays)
    result = codex_linked_history.display_native_turn_page(
        "thread", session_id="stored-session", model=None, limit=2, client=object(), session_db=object())

    assert selected_ids == ["page-turn-1", "page-turn-2"]
    assert [row["rows"][0]["content"] for row in result["turns"]] == ["safe one", "safe two"]
    assert result["next_cursor"] == "next"


def test_browser_messages_use_native_history_not_stale_local_rows(monkeypatch):
    from hermes_cli.web_routers import sessions
    from tui_gateway import server

    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"

    from types import SimpleNamespace

    def unexpected_read(*args, **kwargs):
        raise AssertionError("linked sessions must not read the stale Hermes transcript")

    row = SimpleNamespace(model=None, source="desktop", model_config={"codex_linked_thread": True, "codex_thread_id": thread_id})
    store = SimpleNamespace(sessions=SimpleNamespace(get=lambda sid: row),
                            lineage=SimpleNamespace(resolve_resume_target=lambda sid: sid),
                            messages=SimpleNamespace(read_transcript=unexpected_read))
    monkeypatch.setattr(sessions, "_with_db", lambda profile, fn, read_only: fn(store))
    monkeypatch.setattr(sessions, "_resolve_session_id", lambda db, sid: sid)
    monkeypatch.setattr(sessions, "_history_profile_home", lambda profile: "/tmp/profile")
    monkeypatch.setattr(sessions, "_serving_profile", lambda profile: profile or "default")
    monkeypatch.setattr(sessions, "_project_for_display", lambda rows, **kwargs: rows)

    @contextmanager
    def scope(session):
        assert session["profile_home"] == "/tmp/profile"
        yield

    monkeypatch.setattr(server, "_session_profile_runtime_scope", scope)
    call = {"id": "codex_exec_read", "type": "function",
            "function": {"name": "exec_command", "arguments": '{"command": "pwd"}'}}
    native = [
        {"role": "user", "content": "native question"},
        {"role": "assistant", "content": None, "codex_native": True,
         "reasoning": "Check the linked workspace.",
         "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "tool_name": "exec_command",
         "codex_native": True, "content": "/tmp/native-workspace"},
        {"role": "assistant", "content": "native answer", "reasoning": "The result answers the question."},
    ]
    monkeypatch.setattr("gateway.session_linked_codex.read_display_history_window",
                        lambda got_id, **kwargs: native if got_id == thread_id else [])
    page = asyncio.run(sessions.get_session_messages("linked", profile="default", limit=50,
                                                     offset=0, order="latest", include_compacted=True,
                                                     inline_images=False))
    assert page["messages"] == native

def _huge_turn(text, turn_id="turn-huge"):
    return {"id": turn_id, "status": "completed", "items": [
        {"type": "userMessage", "id": f"u-{turn_id}", "content": [{"type": "text", "text": "small q"}]},
        {"type": "agentMessage", "id": f"a-{turn_id}", "text": text},
    ]}



def _single_page_client(turns):
    class Client:
        def request(self, method, params):
            assert method == "thread/turns/list"
            data = list(turns)
            if params["sortDirection"] == "desc":
                data.reverse()
            return {"data": data, "nextCursor": None}

    return Client()



def test_truncate_record_tail_cuts_largest_strings_front_for_tail_and_end_for_head():
    text = ("alpha-" * 1000) + "TAIL-MARKER-END"
    record = codex_linked_history.native_turn_record(_huge_turn(text))
    original_bytes = len(json.dumps(record, ensure_ascii=False, default=str).encode("utf-8"))
    max_bytes = original_bytes // 3

    tail = codex_linked_history.truncate_record_tail(record, max_bytes, keep="tail")
    assert len(json.dumps(tail, ensure_ascii=False, default=str).encode("utf-8")) <= max_bytes
    assert tail["truncated"] is True and tail["truncated_from_bytes"] == original_bytes
    assert all(row["truncated"] is True for row in tail["rows"])
    biggest = max((row["content"] for row in tail["rows"] if isinstance(row.get("content"), str)),
                  key=len)
    assert text.endswith(biggest) and len(biggest) < len(text)

    head = codex_linked_history.truncate_record_tail(record, max_bytes, keep="head")
    assert len(json.dumps(head, ensure_ascii=False, default=str).encode("utf-8")) <= max_bytes
    biggest = max((row["content"] for row in head["rows"] if isinstance(row.get("content"), str)),
                  key=len)
    assert biggest.startswith("alpha-") and text.startswith(biggest)

    # The original record is never mutated, and a fitting record comes back unflagged.
    assert "truncated" not in record and record["final_text"] == text
    fits = codex_linked_history.truncate_record_tail(record, original_bytes + 1)
    assert "truncated" not in fits and fits == record



def test_truncate_record_tail_returns_stub_when_budget_cannot_fit_overhead():
    record = codex_linked_history.native_turn_record(_huge_turn("x" * 5000))
    stub = codex_linked_history.truncate_record_tail(record, 1, keep="tail")
    assert stub["id"] == "turn-huge" and stub["status"] == "completed"
    assert stub["final_text"] is None and stub["rows"] == []
    assert stub["truncated"] is True and stub["truncated_from_bytes"] > 1
    with pytest.raises(ValueError, match="truncation"):
        codex_linked_history.truncate_record_tail(record, 10, keep="middle")



def test_read_latest_display_turns_truncates_single_oversized_turn_to_tail():
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    text = ("alpha-" * 2000) + "TAIL-MARKER-END"
    records = codex_linked_history.read_latest_display_turns(
        thread_id, client=_single_page_client([_huge_turn(text)]), max_bytes=4000)
    assert len(records) == 1 and records[0]["id"] == "turn-huge"
    assert records[0]["truncated"] is True and records[0]["truncated_from_bytes"] > 4000
    assert records[0]["rows"], "an oversized turn must not be dropped for an empty transcript"
    assert len(json.dumps(records[0], ensure_ascii=False, default=str).encode("utf-8")) <= 4000
    biggest = max((row["content"] for row in records[0]["rows"]
                   if isinstance(row.get("content"), str)), key=len)
    assert text.endswith(biggest) and "TAIL-MARKER-END" in biggest
    assert all(row["truncated"] is True for row in records[0]["rows"])



def test_read_latest_display_turns_stub_when_budget_smaller_than_record_overhead():
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    records = codex_linked_history.read_latest_display_turns(
        thread_id, client=_single_page_client([_huge_turn("x" * 5000)]), max_bytes=1)
    assert len(records) == 1
    assert records[0]["truncated"] is True and records[0]["rows"] == []
    assert records[0]["final_text"] is None and records[0]["id"] == "turn-huge"



def test_read_latest_display_turns_mid_list_oversized_stops_fail_soft_without_truncation():
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    small = {"id": "turn-small", "status": "completed", "items": [
        {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "q2"}]},
        {"type": "agentMessage", "id": "a2", "text": "a2"},
    ]}
    # desc order serves the small newest turn first; the older huge turn then trips the budget.
    records = codex_linked_history.read_latest_display_turns(
        thread_id, client=_single_page_client([_huge_turn("x" * 9000), small]), max_bytes=4000)
    assert [record["id"] for record in records] == ["turn-small"]
    assert all("truncated" not in record for record in records)



def test_read_latest_display_turns_unchanged_when_everything_fits():
    thread_id = "01a1c4a4-7a29-7011-8493-04445af16d6e"
    turns = [_huge_turn("short answer", turn_id="turn-1"),
             _huge_turn("another answer", turn_id="turn-2")]
    records = codex_linked_history.read_latest_display_turns(
        thread_id, client=_single_page_client(turns), max_bytes=32 * 1024 * 1024)
    assert [record["id"] for record in records] == ["turn-1", "turn-2"]
    assert all("truncated" not in record for record in records)
    assert records[1]["final_text"] == "another answer"


