"""Map Codex's native user-input requests onto Hermes's interactive clarify surface."""

from __future__ import annotations

from typing import Any, Callable

from agent.redact import redact_approval_display


class CodexUserInputUnavailable(ValueError):
    """A native question cannot be answered faithfully on this surface."""


def _prepare_question(raw: Any) -> tuple[dict, dict[str, str], bool]:
    if not isinstance(raw, dict) or raw.get("isSecret") is True:
        raise CodexUserInputUnavailable("Secret or invalid Codex questions are unavailable")
    qid, question = raw.get("id"), raw.get("question")
    if not isinstance(qid, str) or not qid or len(qid) > 128:
        raise CodexUserInputUnavailable("Invalid Codex question ID")
    if not isinstance(question, str) or not question.strip() or len(question) > 20_000:
        raise CodexUserInputUnavailable("Invalid Codex question text")
    header = raw.get("header") or ""
    if not isinstance(header, str) or len(header) > 8_000:
        raise CodexUserInputUnavailable("Invalid Codex question header")
    options = raw.get("options")
    if options is not None and (not isinstance(options, list) or len(options) > 4):
        raise CodexUserInputUnavailable("Unsupported Codex question options")
    shown_to_raw: dict[str, str] = {}
    details = []
    for option in options or []:
        if not isinstance(option, dict):
            raise CodexUserInputUnavailable("Invalid Codex question option")
        label, description = option.get("label"), option.get("description")
        if (not isinstance(label, str) or not label.strip() or len(label) > 8_000
                or not isinstance(description, str) or len(description) > 8_000):
            raise CodexUserInputUnavailable("Invalid Codex question option")
        shown = redact_approval_display(label, prose=True)
        if not shown or shown in shown_to_raw:
            raise CodexUserInputUnavailable("Codex question options cannot be displayed distinctly")
        shown_to_raw[shown] = label
        details.append(f"{shown}: {redact_approval_display(description, prose=True)}")
    text = "\n".join(part for part in (header, question, *details) if part)
    rendered = {"qid": qid, "question": redact_approval_display(text, prose=True),
                "choices": list(shown_to_raw) or None, "multi_select": False}
    return rendered, shown_to_raw, bool(raw.get("isOther"))


def request_codex_user_input(params: dict, callback: Callable | None) -> dict:
    """Return only explicit answers; cancel, missing UI and unsupported questions fail closed."""
    if not callable(callback) or params.get("isBlocking") is False:
        raise CodexUserInputUnavailable("Interactive Codex input is unavailable")
    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 5:
        raise CodexUserInputUnavailable("Unsupported Codex question count")
    prepared = [_prepare_question(question) for question in raw_questions]
    ids = [question[0]["qid"] for question in prepared]
    if len(ids) != len(set(ids)):
        raise CodexUserInputUnavailable("Duplicate Codex question IDs")
    try:
        reply = callback([question[0] for question in prepared])
    except Exception as exc:
        raise CodexUserInputUnavailable("Could not show the Codex question") from exc
    if not isinstance(reply, dict) or reply.get("outcome") != "submitted" or not isinstance(reply.get("answers"), dict):
        raise CodexUserInputUnavailable("Codex question was not answered")
    answers = {}
    for rendered, shown_to_raw, allow_other in prepared:
        answer = reply["answers"].get(rendered["qid"])
        if not isinstance(answer, str) or not answer.strip():
            raise CodexUserInputUnavailable("Codex question was not answered")
        if shown_to_raw and answer not in shown_to_raw and not allow_other:
            raise CodexUserInputUnavailable("Answer is not one of the Codex question options")
        answers[rendered["qid"]] = {"answers": [shown_to_raw.get(answer, answer)]}
    return {"answers": answers}
