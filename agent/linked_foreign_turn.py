"""Run one Hermes send as an APPEND to a linked Claude Code / Mistral Vibe thread.

A linked row (``model_config`` flag ``claude_linked_thread`` / ``mistral_linked_thread``, plus the
legacy ``api_mode: claude_linked_readonly`` / ``mistral_linked_readonly`` pointer shape) is a
locator: the native CLI owns the transcript and Hermes stores no second copy of it. So a turn on
such a row is only ever ``claude -p … --resume <id>`` / ``vibe -p … --resume <id>`` driven through
:func:`hermes_cli.claude_linked_resume.run_linked_claude_turn` /
:func:`hermes_cli.mistral_linked_resume.run_linked_vibe_turn`, which verify that the native
transcript is the one that grew.

THIS MODULE IS THE NO-FORK BOUNDARY. ``agent/conversation_loop.py::_turn_without_loop`` asks it
first and returns whatever it returns, including a refusal: a linked row never reaches the generic
model loop, the fallback chain, or a retry, because any of those would answer the user from a new
Hermes-owned conversation while the native thread stayed untouched — the exact fork the link exists
to prevent. Every failure therefore leaves here as a typed turn result
(``completed=False`` + a ``linked_error_code``) and never as ``None``.

Pointer semantics: the rows the native store gained are returned for this turn's live display and
are NOT written to the Hermes transcript. The result carries ``agent_persisted=True`` so a surface
skips its own write too — a linked row's own message list stays empty, and ``session.resume``
re-reads the native store (``hermes_cli/{claude,mistral}_linked_history.py``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Linked agents this route owns. Codex is NOT here: its app-server runtime
#: (``agent/codex_runtime.py``) already appends with ``resume_thread_id``.
LINKED_FOREIGN_AGENTS = ("claude", "mistral")

#: ``model_config`` flag that marks each agent's pointer row.
LINKED_FOREIGN_FLAGS = {"claude": "claude_linked_thread", "mistral": "mistral_linked_thread"}

#: How the row's own ``api_mode`` spells the same pointer (older rows, and the read-only era).
#: Read from the ROW, never from ``agent.api_mode``: row values are not runtime modes, so agent
#: init resolves them to an ordinary wire mode and the runtime alone can never identify a pointer.
LINKED_FOREIGN_ROW_MODES = {
    "claude": ("claude_linked_readonly", "claude_linked_resume"),
    "mistral": ("mistral_linked_readonly", "mistral_linked_resume"),
}

#: User-facing name of the native tool, used in refusals.
LINKED_FOREIGN_LABELS = {"claude": "Claude Code", "mistral": "Mistral Vibe",
                         "foreign": "foreign"}

#: Linked agents whose transcript — and therefore whose compaction — lives in the native CLI.
#: Codex is NOT here: its app-server runtime exposes compaction through Hermes
#: (``agent/conversation_compression.py::_route_codex_compaction``).
LINKED_FOREIGN_COMPACTION_OWNED = ("claude", "mistral")


def linked_native_compaction_message(agent_name: str) -> str:
    """Why ``/compress`` cannot run on a linked Claude/Mistral row, in the native tool's terms.

    A pointer row holds no messages of its own, so the generic summarizer would make a billable LLM
    call over a projection nobody keeps and then split a Hermes session that has no transcript.
    """
    label = LINKED_FOREIGN_LABELS.get(agent_name, agent_name)
    return (f"Linked {label} threads compact in {label} itself — Hermes cannot summarize a "
            f"transcript it does not own")


def linked_compaction_refusal(agent_name: str | None) -> str | None:
    """The refusal for a linked row THIS module owns, or ``None`` when compression may proceed.

    ``None`` is returned for an ordinary session, and for a linked Codex row: Codex compacts its
    own thread through Hermes, which is the one native compaction this build can drive. A
    ``"foreign"`` (unreadable) pointer is refused as well — an owner that cannot be named cannot be
    asked to compact, and running the local summarizer on it is the same guess the turn route
    refuses to make.
    """
    if agent_name in LINKED_FOREIGN_COMPACTION_OWNED:
        return linked_native_compaction_message(agent_name)
    if agent_name == "foreign":
        return ("This session's stored configuration cannot be read, so its native thread cannot "
                "be compacted — re-link the session from the foreign session list")
    return None


#: What the user can do about each typed refusal. Codes come from the transports / resume
#: adapters; an unlisted one falls back to the transport's own detail sentence.
LINKED_FOREIGN_FIXES = {
    "claude_missing_binary": "install the Claude Code CLI on the gateway host (or set model.claude_bin)",
    "claude_auth_required": "sign in to Claude Code on the gateway host by running `claude` once",
    "claude_permission_required": "run the tool Claude Code asked for in the native CLI",
    "claude_session_missing": "reopen the thread in Claude Code — its transcript is gone",
    "claude_session_mismatch": "reopen the thread in Claude Code — it reported a different session",
    "claude_linked_busy": "close this thread in the other client and send again",
    "claude_linked_fork_detected": "check ~/.claude/projects for a second copy of this session",
    "claude_native_session_missing": "reopen the thread in Claude Code — its transcript is gone",
    "claude_cwd_unavailable": "restore the project directory the Claude Code thread ran in",
    "claude_cwd_mismatch": "open this session in Claude Code once from its own project directory",
    "claude_timeout": "send again — the turn exceeded its time limit",
    "claude_cancelled": "send again",
    "claude_not_linked": "re-link the session from the foreign session list",
    "claude_linked_store_unavailable": "check the profile's ~/.claude scope on the gateway host",
    "vibe_binary_missing": "install the Vibe CLI on the gateway host (or set model.vibe_bin)",
    "vibe_linked_unavailable": "reopen the thread in Vibe — its store or workspace is unavailable",
    "vibe_linked_conflict": "close this thread in the other client and send again",
    "vibe_approval_required": "run the tool Vibe asked for in the native CLI",
    "vibe_run_timeout": "send again — the turn exceeded its time limit",
    "vibe_run_cancelled": "send again",
    "vibe_run_failed": "check the Vibe CLI on the gateway host",
    "vibe_output_unreadable": "check the Vibe CLI version on the gateway host",
    "linked_foreign_config_unreadable": (
        "repair this session's stored configuration, or re-link the session from the foreign "
        "session list"),
}

#: Transport exception class → stable wire code for the errors that carry no ``code`` attribute
#: (the Vibe transport and resume adapter refuse by exception type). Matched by name so this
#: module never imports a transport just to classify a failure it already caught.
_VIBE_CLASS_CODES = {
    "VibeBinaryMissing": "vibe_binary_missing",
    "VibeApprovalRequired": "vibe_approval_required",
    "VibeRunTimeout": "vibe_run_timeout",
    "VibeRunCancelled": "vibe_run_cancelled",
    "VibeRunFailed": "vibe_run_failed",
    "VibeOutputUnreadable": "vibe_output_unreadable",
    "VibeResumeError": "vibe_resume_failed",
    "LinkedVibeUnavailable": "vibe_linked_unavailable",
    "LinkedVibeConflict": "vibe_linked_conflict",
    "LinkedVibeError": "vibe_linked_failed",
}


def _agent_store(agent: Any) -> Any:
    """The store this turn's row lives in: the agent's writer handle, else its persistence handle.

    ``session_db`` is only one of the shapes a surface binds. The gateway builds its turn agent
    with ``persistence=`` and no ``session_db=`` (``gateway/session_execution.py::_build_fresh_agent``),
    so keying detection on ``agent._session_db`` alone made every gateway turn over a linked row
    look ordinary — the fork this module exists to prevent. Read the same handles the rest of the
    agent does, in their own precedence: the writer interface (both handles, store writable), the
    persistence handle, a plain ``store`` attribute, then the session DB.
    """
    from agent.agent_init_session import agent_interface

    return (agent_interface(agent) or getattr(agent, "persistence", None)
            or getattr(agent, "store", None) or getattr(agent, "_session_db", None))


def linked_foreign_agent(agent: Any) -> str | None:
    """Which linked agent owns this turn's row: ``"claude"``, ``"mistral"``, ``"foreign"``, ``None``.

    ``None`` is only returned for a row that is provably an ordinary Hermes session. ``"foreign"``
    means the row carries a ``model_config`` that cannot be validated — or that the store bound to
    this agent cannot be read at all: the route refuses such a turn (fail closed) because an
    unreadable pointer cannot be told apart from an ordinary row, and running the generic loop on
    it is exactly the fork this module exists to prevent.

    An agent with NO store bound at all is still ``None``: it has no row of its own to point at
    (the background-review fork, the curator and prompt inspection all build one deliberately),
    so there is no pointer to honour and no native thread to fork.

    The flag is authoritative; a row that only carries the read-only ``api_mode`` is still a
    pointer (the shape the read-only era wrote), so it is honoured too. The row's own ``api_mode``
    is read — never ``agent.api_mode``, which init has already resolved to an ordinary wire mode.
    """
    store, session_id = _agent_store(agent), getattr(agent, "session_id", None)
    if store is None or not isinstance(session_id, str) or not session_id:
        return None
    from hermes_cli.foreign_linked_store import (
        LinkedConfigUnreadable, row_model_config, store_row_reader)

    if not store_row_reader(store):
        # Bound, but with no way to read a row: the pointer cannot be ruled out, so this turn is
        # not "provably ordinary" and the route refuses it rather than answer it generically.
        logger.warning("linked foreign turn: the bound store exposes no session row reader")
        return "foreign"
    try:
        config = row_model_config(store, session_id)
    except LinkedConfigUnreadable:
        return "foreign"
    except Exception:  # health: allow BLE001 -- an unreadable store must refuse, never crash a turn
        logger.warning("linked foreign turn: the session row could not be read", exc_info=True)
        return "foreign"
    for name in LINKED_FOREIGN_AGENTS:
        if config.get(LINKED_FOREIGN_FLAGS[name]) is True:
            return name
    row_mode = config.get("api_mode")
    if isinstance(row_mode, str):
        for name in LINKED_FOREIGN_AGENTS:
            if row_mode in LINKED_FOREIGN_ROW_MODES[name]:
                return name
    return None


class _AgentInterruptCancel:
    """``cancel_event`` (``is_set()``) over the agent's own interrupt latches.

    The turn runner sets ``_interrupt_requested`` / ``_hard_interrupt_requested`` when the user
    presses Stop; the transports kill their child process group when they see it, so a stopped
    linked turn stops the native CLI instead of running on unattended.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def is_set(self) -> bool:
        hard = getattr(self._agent, "_hard_interrupt_requested", None)
        if hard is not None and callable(getattr(hard, "is_set", None)) and hard.is_set():
            return True
        return bool(getattr(self._agent, "_interrupt_requested", False))


def _brief(value: Any, limit: int = 400) -> str:
    """One bounded, printable line of diagnostic text."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isprintable())[:limit]


def _failure_code(exc: BaseException, agent_name: str) -> str:
    """Stable wire code for a refusal from the linked-claude / linked-vibe write path."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    for klass in type(exc).__mro__:
        if klass.__name__ in _VIBE_CLASS_CODES:
            return _VIBE_CLASS_CODES[klass.__name__]
    return f"{agent_name}_linked_turn_failed"


def _failure_detail(exc: BaseException) -> str:
    """The transport's own sentence, without re-printing the code we already carry."""
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, str) or not detail:
        detail = str(exc)
    prefix = getattr(exc, "code", None)
    if isinstance(prefix, str) and prefix and detail.startswith(f"{prefix}:"):
        detail = detail[len(prefix) + 1:]
    return _brief(detail) or type(exc).__name__


def _refusal_text(agent_name: str, code: str, detail: str) -> str:
    """User-facing sentence for a refused linked turn: what failed, in the native tool's terms."""
    label = LINKED_FOREIGN_LABELS.get(agent_name, agent_name)
    fix = LINKED_FOREIGN_FIXES.get(code)
    head = f"This linked {label} thread could not be continued"
    if fix:
        return f"{head}: {fix}. ({code})"
    return f"{head}: {detail}. ({code})"


def _redact_row(row: dict) -> dict:
    """Scrub one projected assistant row, exactly as the codex projection does before display.

    Native transcripts are another tool's file: whatever it printed (a key pasted into a prompt, a
    model's reasoning block) is redacted on the way to Hermes' display rows too.
    """
    from agent.reasoning_summaries import redact_reasoning_fields
    from agent.redact_phone_policy import redact_assistant_reply

    if row.get("role") == "assistant":
        content = row.get("content")
        if isinstance(content, str) and content:
            row["content"] = redact_assistant_reply(content)
        redact_reasoning_fields(row)
    return row


def _project_rows(rows: list[dict], *, assistant_text: str) -> list[dict]:
    """The native rows worth showing live, minus a leading echo of the submitted prompt.

    The turn's own user row is already in ``messages`` (persisted by the caller's turn start), so
    re-appending the native echo would show the prompt twice. Rows stay in memory only.
    """
    projected = [dict(row) for row in rows if isinstance(row, dict) and row.get("role")]
    if projected and projected[0].get("role") == "user":
        projected = projected[1:]
    if not projected and assistant_text:
        projected = [{"role": "assistant", "content": assistant_text}]
    return projected


def _linked_config() -> Any:
    """The profile's merged config, for ``model.claude_bin`` / ``model.vibe_bin``.

    Resolved at the spawn site: a gateway or service launch has a bare PATH that often lacks the
    native CLI, so the owner's configured binary is the only way the send can find its tool (the
    same reason ``model.codex_bin`` exists). A broken config is not fatal here — the transport then
    does its own PATH lookup and raises its typed "missing binary" refusal.
    """
    try:
        from hermes_cli.config import load_config

        return load_config()
    except Exception:  # health: allow BLE001 -- a config read must not become the send's failure
        logger.debug("linked foreign turn: config could not be loaded", exc_info=True)
        return None


def _run_claude(agent: Any, store: Any, session_id: str, prompt: str) -> Any:
    from hermes_cli.claude_linked_resume import run_linked_claude_turn

    return run_linked_claude_turn(
        prompt=prompt, store=store, session_id=session_id,
        cwd=getattr(agent, "session_cwd", None), config=_linked_config(),
        cancel_event=_AgentInterruptCancel(agent),
    )


def _run_mistral(agent: Any, store: Any, session_id: str, prompt: str) -> Any:
    from hermes_cli.mistral_linked_resume import run_linked_vibe_turn

    # ``run_linked_vibe_turn`` does not load config itself, so ``model.vibe_bin`` only reaches the
    # spawn when it is passed here.
    return run_linked_vibe_turn(
        store=store, session_id=session_id, prompt=prompt, config=_linked_config(),
        cancel_event=_AgentInterruptCancel(agent),
    )


def run_linked_foreign_turn(agent: Any, *, user_message: Any, original_user_message: Any = None,
                            messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Append this turn to the linked native thread and return the chat-completions result shape.

    ``user_message`` is ALREADY the last user row of ``messages`` — it is only the text the native
    CLI receives. The result is never ``None``: a linked row has no second way to run a turn, so a
    refusal is the answer rather than a signal for the caller to look elsewhere.

    Post-turn memory sync / background review are deliberately NOT run: they summarize a Hermes
    transcript this row does not have, and the native tool owns the turn's history.
    """
    from agent.codex_runtime import _consume_user_interrupt, _turn_result

    agent_name = linked_foreign_agent(agent)
    messages = messages if isinstance(messages, list) else []
    if agent_name == "foreign":
        # Fail closed: the row exists but its configuration cannot be validated, so there is no way
        # to prove it is safe to run — and running it as an ordinary session would fork a thread.
        return _refused(agent, messages, agent_name, "linked_foreign_config_unreadable",
                        "the session's stored model configuration cannot be read")
    if agent_name is None:  # pragma: no cover - the caller only routes linked rows here
        error = "This session is not a linked foreign session"
        return _turn_result(_consume_user_interrupt(agent, False), messages, api_calls=0,
                            completed=False, error=error, final_response=error,
                            failure_reason="linked_foreign_not_linked", agent_persisted=True)
    prompt = user_message if isinstance(user_message, str) else str(user_message or "")
    session_id = getattr(agent, "session_id", None)
    store = _agent_store(agent)
    try:
        if not isinstance(session_id, str) or not session_id or store is None:
            raise _LinkedTurnUnavailable("the linked session has no readable row on this host")
        turn = (_run_claude if agent_name == "claude" else _run_mistral)(
            agent, store, session_id, prompt)
        if agent_name == "claude":
            rows, final_text = list(turn.rows or []), str(turn.final_text or "")
            native_id = turn.native_session_id
        else:
            rows, final_text = list(turn.messages or []), str(turn.assistant_text or "")
            native_id = turn.native_session_id
    except _refusal_types_cached() as exc:
        code = _failure_code(exc, agent_name)
        detail = _failure_detail(exc)
        logger.warning("linked %s turn refused (%s): %s", agent_name, code, detail)
        return _refused(agent, messages, agent_name, code, detail)
    # Pointer semantics: the native store is the transcript. Splice the projection into THIS turn's
    # in-memory messages for live display only, and tell every surface the transcript is its own.
    from agent.message_metadata import append_message

    for row in _project_rows(rows, assistant_text=final_text):
        append_message(messages, _redact_row(row))
    if not final_text and not rows:
        return _refused(agent, messages, agent_name, f"{agent_name}_empty_result",
                        "the native CLI reported a turn but returned no text")
    return _turn_result(
        _consume_user_interrupt(agent),
        messages,
        api_calls=1,
        completed=True,
        error=None,
        final_response=final_text,
        # The gateway must skip its own transcript write: these rows belong to the native store.
        agent_persisted=True,
        linked_foreign_agent=agent_name,
        linked_native_session_id=native_id,
    )


def _refused(agent: Any, messages: list[dict[str, Any]], agent_name: str, code: str,
             detail: str) -> dict[str, Any]:
    """A typed refusal in the turn-result shape: an error card, never an empty success."""
    from agent.codex_runtime import _consume_user_interrupt, _turn_result

    text = _refusal_text(agent_name, code, detail)
    return _turn_result(
        _consume_user_interrupt(agent), messages, api_calls=0, completed=False,
        error=f"{code}: {detail}", final_response=text, failure_reason=code,
        agent_persisted=True, linked_foreign_agent=agent_name, linked_error_code=code,
    )


class _LinkedTurnUnavailable(RuntimeError):
    """This row cannot be resumed at all on this host (no owning store / session row)."""

    code = "linked_foreign_unavailable"


def _linked_turn_refusals() -> tuple[type[BaseException], ...]:
    """The typed refusals the two resume adapters and their transports raise.

    Resolved lazily and tolerantly: a build can ship only one of the two adapters, and a missing
    module must not turn an unrelated refusal into an ImportError on the send path.
    """
    kinds: list[type[BaseException]] = [_LinkedTurnUnavailable, ValueError, OSError]
    for module_name, names in (("hermes_cli.claude_linked_resume", ("LinkedClaudeError",)),
                               ("agent.transports.claude_cli_resume", ("ClaudeCliError",)),
                               ("hermes_cli.mistral_linked_resume", ("LinkedVibeError",)),
                               ("agent.transports.vibe_cli_resume", ("VibeResumeError",))):
        try:
            module = __import__(module_name, fromlist=list(names))
        except Exception:  # pragma: no cover - a build without that adapter
            continue
        kinds.extend(getattr(module, name) for name in names if isinstance(getattr(module, name, None), type))
    return tuple(dict.fromkeys(kinds))


_refusal_types: tuple[type[BaseException], ...] | None = None


def _refusal_types_cached() -> tuple[type[BaseException], ...]:
    """Every refusal the route converts into a typed result; anything else is a bug and propagates."""
    global _refusal_types
    if _refusal_types is None:
        _refusal_types = _linked_turn_refusals()
    return _refusal_types
