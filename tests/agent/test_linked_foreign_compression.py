"""``/compress`` on a linked Claude/Mistral row must never run the local summarizer.

A linked pointer row (``claude_linked_thread`` / ``mistral_linked_thread``) stores the native session
id and nothing else: the transcript lives in the native CLI's own store, and the Hermes row's own
message list stays empty. Compaction is therefore not something Hermes can do for those two — a
"compression" here is a billable LLM summary over a projection nobody keeps, plus a SQLite session
split of a row that has no transcript.

Codex is the one exception and stays exactly as it was: its app-server runtime exposes ``compact``,
so ``_route_codex_compaction`` keeps owning that row (dispatched on ``api_mode``, as before).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.conversation_compression import compress_context
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"


class _TodoStore:
    def format_for_injection(self):
        return ""


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test/main-model", provider="test-provider", threshold_percent=0.50, quiet_mode=True,
            config_context_length=100_000,
        )
    compressor.tail_token_budget = 10
    return compressor


class _Agent:
    """The attributes ``compress_context`` reads before it dispatches (the stub ``_Agent`` of the
    attempt-telemetry tests, plus the store handle the linked check resolves)."""

    def __init__(self, store, session_id, *, api_mode="chat_completions"):
        self.persistence = store
        self._session_db = store
        self.session_id = session_id
        self.api_mode = api_mode
        self.context_compressor = _compressor()
        self.platform = "cli"
        self.model = "test/main-model"
        self.provider = "test-provider"
        self.tools = []
        self._compression_feasibility_checked = True
        self.compression_in_place = False
        self._memory_manager = None
        self._todo_store = _TodoStore()
        self._cached_system_prompt = None

    def _emit_status(self, _message):
        pass

    def _emit_warning(self, _message):
        pass

    def _invalidate_system_prompt(self):
        self._cached_system_prompt = None

    def _build_system_prompt(self, system_message):
        return system_message

    def commit_memory_session(self, _messages):
        pass


def _messages():
    rows = [{"role": "user", "content": "add a docstring"}]
    rows.append({"role": "assistant", "content": "done"})
    return rows


@pytest.fixture
def store(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture(params=[("claude", CLAUDE_ID), ("mistral", MISTRAL_ID)], ids=["claude", "mistral"])
def linked_row(store, request):
    kind, native_id = request.param
    flag, id_key = f"{kind}_linked_thread", f"{kind}_session_id"
    seed_session(store, "linked", "desktop",
                 model_config={flag: True, id_key: native_id, "api_mode": f"{kind}_linked_readonly"})
    return SimpleNamespace(kind=kind, native_id=native_id)


def test_a_manual_compress_of_a_linked_row_is_refused_before_the_summarizer(store, linked_row):
    """``/compress`` (``force=True``) raises the clear ValueError the surface error paths render."""
    agent = _Agent(store, "linked")
    label = "Claude Code" if linked_row.kind == "claude" else "Mistral Vibe"
    with patch.object(agent.context_compressor, "_generate_summary") as summary:
        with pytest.raises(ValueError) as caught:
            compress_context(agent, _messages(), "system prompt", force=True)
    assert summary.called is False, "no billable summary may be requested for a pointer row"
    assert "compact in" in str(caught.value)
    assert label in str(caught.value)


def test_an_automatic_compress_of_a_linked_row_is_a_no_op(store, linked_row):
    """The turn paths never reach here for a linked row; if one does, it must not summarize either.

    A refusal there would be worse than useless: the turn would report a failed compression of a
    transcript that is not in trouble — it simply lives in the native tool.
    """
    agent = _Agent(store, "linked")
    messages = _messages()
    with patch.object(agent.context_compressor, "_generate_summary") as summary:
        out_messages, out_prompt = compress_context(agent, messages, "system prompt", trigger="pre_api")
    assert summary.called is False
    assert out_messages is messages, "an automatic pass returns its input untouched"
    assert out_prompt == "system prompt"


def test_a_linked_codex_row_keeps_its_native_compaction_route(store):
    """Codex is untouched: the guard must not shadow the app-server route it already had."""
    seed_session(store, "codex-row", "desktop",
                 model_config={"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})
    agent = _Agent(store, "codex-row", api_mode="codex_app_server")
    sentinel = (["compacted"], "prompt")
    routed = []
    with patch("agent.conversation_compression._route_codex_compaction",
               lambda *_a, **_k: (routed.append(1), sentinel)[1]), \
            patch.object(agent.context_compressor, "_generate_summary") as summary:
        result = compress_context(agent, _messages(), "system prompt", force=True)
    assert routed == [1]
    assert summary.called is False
    assert result == sentinel


def test_an_ordinary_row_still_compresses(store):
    """The guard is scoped to pointer rows: an ordinary session keeps the local summarizer."""
    seed_session(store, "ordinary", "desktop", model_config={"api_mode": "chat_completions"})
    agent = _Agent(store, "ordinary")
    rows = [{"role": "system", "content": "system prompt"}]
    for idx in range(10):
        rows.append({"role": "user", "content": f"user message {idx} " + "context " * 200})
        rows.append({"role": "assistant", "content": f"assistant reply {idx} " + "context " * 200})
    with patch.object(agent.context_compressor, "_generate_summary", return_value="SUMMARY") as summary:
        compress_context(agent, rows, "system prompt", approx_tokens=80_000, force=True)
    assert summary.called is True
