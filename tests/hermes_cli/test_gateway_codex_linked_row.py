import json

from hermes_cli.web_server_gateway import _strip_session_list_rows


def test_session_list_marks_linked_codex_without_exposing_native_thread_id():
    rows = [{
        "id": "stored-session",
        "model_config": json.dumps({
            "codex_linked_thread": True,
            "codex_thread_id": "private-native-thread-id",
        }),
        "system_prompt": "private prompt",
    }]

    result = _strip_session_list_rows(rows)

    assert result[0]["codex_linked"] is True
    assert "model_config" not in result[0]
    assert "system_prompt" not in result[0]
    assert "private-native-thread-id" not in repr(result)


def test_session_list_does_not_mark_an_unlinked_or_damaged_config():
    rows = [
        {"id": "plain", "model_config": json.dumps({"provider": "openai"})},
        {"id": "damaged", "model_config": "not-json"},
        {"id": "missing-thread", "model_config": json.dumps({"codex_linked_thread": True})},
    ]

    result = _strip_session_list_rows(rows)

    assert all("codex_linked" not in row for row in result)
    assert all("model_config" not in row for row in result)


def test_session_list_marks_linked_claude_and_mistral_without_exposing_native_ids():
    rows = [{
        "id": "claude-pointer",
        "model_config": json.dumps({
            "claude_linked_thread": True,
            "claude_session_id": "private-claude-uuid",
        }),
    }, {
        "id": "mistral-pointer",
        "model_config": {  # already a dict, not a JSON string
            "mistral_linked_thread": True,
            "mistral_session_id": "private-mistral-uuid",
        },
    }]

    result = _strip_session_list_rows(rows)

    assert result[0]["claude_linked"] is True
    assert result[1]["mistral_linked"] is True
    assert "codex_linked" not in result[0] and "codex_linked" not in result[1]
    assert "mistral_linked" not in result[0] and "claude_linked" not in result[1]
    # Same policy as codex: the native id never leaks into the list projection.
    assert "private-claude-uuid" not in repr(result)
    assert "private-mistral-uuid" not in repr(result)
    assert all("model_config" not in row for row in result)


def test_session_list_omits_claude_and_mistral_flags_for_unlinked_or_damaged_configs():
    rows = [
        {"id": "plain", "model_config": json.dumps({"provider": "openai"})},
        {"id": "damaged", "model_config": "not-json"},
        {"id": "claude-missing-id", "model_config": json.dumps({"claude_linked_thread": True})},
        {"id": "mistral-missing-id", "model_config": json.dumps({"mistral_linked_thread": True})},
        {"id": "claude-flag-off", "model_config": json.dumps({
            "claude_linked_thread": False, "claude_session_id": "some-uuid"})},
    ]

    result = _strip_session_list_rows(rows)

    assert all("claude_linked" not in row for row in result)
    assert all("mistral_linked" not in row for row in result)
    assert all("codex_linked" not in row for row in result)
