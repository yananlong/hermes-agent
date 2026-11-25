"""Codex's native questions use explicit answers from Hermes's interactive surface."""

import pytest

from agent.transports.codex_user_input import CodexUserInputUnavailable, request_codex_user_input


def _params():
    return {"isBlocking": True, "questions": [
        {"id": "choice", "header": "Pick", "question": "Which route?", "isOther": False, "isSecret": False,
         "options": [{"label": "One", "description": "First path"}, {"label": "Two", "description": "Second path"}]},
        {"id": "text", "header": "Details", "question": "Why?", "isOther": True, "isSecret": False, "options": None},
    ]}


def test_preserves_question_ids_option_descriptions_and_explicit_answers():
    seen = []
    def answer(questions):
        seen.extend(questions)
        return {"outcome": "submitted", "answers": {"choice": "Two", "text": "My explanation"}}

    result = request_codex_user_input(_params(), answer)
    assert result == {"answers": {"choice": {"answers": ["Two"]}, "text": {"answers": ["My explanation"]}}}
    assert seen[0]["choices"] == ["One", "Two"]
    assert "First path" in seen[0]["question"] and "Second path" in seen[0]["question"]


@pytest.mark.parametrize("reply", [
    {"outcome": "cancelled", "answers": {}},
    {"outcome": "submitted", "answers": {"choice": "One"}},
    {"outcome": "submitted", "answers": {"choice": "unoffered", "text": "x"}},
])
def test_unanswered_cancelled_and_unoffered_answers_fail_closed(reply):
    with pytest.raises(CodexUserInputUnavailable):
        request_codex_user_input(_params(), lambda _questions: reply)


def test_secret_and_nonblocking_questions_do_not_reach_the_browser():
    params, seen = _params(), []
    params["questions"][0]["isSecret"] = True
    with pytest.raises(CodexUserInputUnavailable):
        request_codex_user_input(params, seen.append)
    params = _params()
    params["isBlocking"] = False
    with pytest.raises(CodexUserInputUnavailable):
        request_codex_user_input(params, seen.append)
    assert seen == []
