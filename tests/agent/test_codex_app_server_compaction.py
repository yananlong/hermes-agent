import time
from types import SimpleNamespace

import pytest

from agent.codex_runtime import _record_codex_app_server_compaction
from agent.conversation_compression import COMPACTION_DONE_STATUS, COMPACTION_STATUS, compress_context
from agent.transports.codex_app_server_session import TurnResult


class FakeCodexSession:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.closed = False

    def compact_thread(self):
        self.calls += 1
        return self.result

    def close(self):
        self.closed = True


class SlowCodexSession(FakeCodexSession):
    def __init__(self, result, touch_calls):
        super().__init__(result)
        self.touch_calls = touch_calls

    def compact_thread(self):
        self.calls += 1
        _wait_for_touch(self.touch_calls, "context compression in progress")
        return self.result


def _wait_for_touch(touch_calls, desc, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if desc in touch_calls:
            return
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for touch {desc!r}; saw {touch_calls!r}")


class DummyAgent:
    def __init__(
        self,
        result,
        *,
        auto_compaction="native",
    ):
        self.api_mode = "codex_app_server"
        self.codex_app_server_auto_compaction = auto_compaction
        self.session_id = "hermes-session-1"
        self.platform = "cli"
        self._cached_system_prompt = "cached prompt"
        self._codex_session = FakeCodexSession(result)
        self.context_compressor = SimpleNamespace(
            compression_count=0,
            last_compression_rough_tokens=0,
            last_prompt_tokens=123,
            last_completion_tokens=45,
            awaiting_real_usage_after_compression=False,
        )
        self.statuses = []
        self.status_events = []
        self.status_callback = lambda kind, text: self.status_events.append((kind, text))
        self.warnings = []
        self.events = []
        self.built_prompts = []
        self.touch_calls = []
        self.touch_provenances = []
        self._compression_activity_heartbeat_interval = 0.1

    def _touch_activity(self, desc, *, provenance=None, force_persist=False):
        self.touch_calls.append(desc)
        self.touch_provenances.append(provenance)

    def _emit_status(self, message):
        self.statuses.append(message)
        self.status_callback("lifecycle", message)

    def _emit_warning(self, message):
        self.warnings.append(message)
        self.status_callback("warn", message)

    def _build_system_prompt(self, system_message):
        self.built_prompts.append(system_message)
        return "built prompt"

    def event_callback(self, name, payload):
        self.events.append((name, payload))


def test_codex_app_server_native_auto_mode_leaves_thread_compaction_to_codex():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1")
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        task_id="test",
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 0
    assert agent.context_compressor.compression_count == 0
    assert agent.events == []


def test_manual_compaction_opens_linked_thread_before_first_turn(monkeypatch):
    import agent.codex_runtime as runtime

    agent = DummyAgent(TurnResult(thread_id="linked-thread", turn_id="compact-turn", compacted=True))
    linked_session = agent._codex_session
    agent._codex_session = None
    agent._session_db = SimpleNamespace(get_session_model_config_value=lambda _sid, key: {
        "codex_linked_thread": True, "codex_thread_id": "linked-thread"
    }.get(key))
    opened = []

    def open_linked(candidate, messages):
        opened.append(messages)
        candidate._codex_session = linked_session

    monkeypatch.setattr(runtime, "_ensure_codex_session", open_linked)
    monkeypatch.setattr(runtime, "_start_codex_thread", lambda candidate: opened.append("linked-thread"))
    messages = [{"role": "user", "content": "hello"}]
    compress_context(agent, messages, "system", force=True)
    assert opened == [messages, "linked-thread"]
    assert linked_session.calls == 1
    assert linked_session.closed is True and agent._codex_session is None


def test_manual_compaction_releases_linked_writer_when_startup_fails(monkeypatch):
    import agent.codex_runtime as runtime

    agent = DummyAgent(TurnResult(thread_id="linked-thread"))
    linked_session = agent._codex_session
    agent._codex_session = None
    agent._session_db = SimpleNamespace(get_session_model_config_value=lambda _sid, key: {
        "codex_linked_thread": True, "codex_thread_id": "linked-thread"
    }.get(key))
    monkeypatch.setattr(runtime, "_ensure_codex_session", lambda candidate, _messages: setattr(candidate, "_codex_session", linked_session))
    def fail_start(_candidate):
        raise RuntimeError("could not resume")
    monkeypatch.setattr(runtime, "_start_codex_thread", fail_start)
    with pytest.raises(RuntimeError, match="could not resume"):
        compress_context(agent, [], "system", force=True)
    assert linked_session.closed is True and agent._codex_session is None


@pytest.mark.parametrize("result", [
    TurnResult(thread_id="linked-thread", error="native compact failed"),
    TurnResult(thread_id="linked-thread", interrupted=True),
    TurnResult(thread_id="linked-thread", compacted=False),
    TurnResult(thread_id="different-thread", compacted=True),
])
def test_linked_compaction_rejects_failures_and_unconfirmed_or_wrong_thread(result):
    agent = DummyAgent(result)
    agent._session_db = SimpleNamespace(get_session_model_config_value=lambda _sid, key: {
        "codex_linked_thread": True, "codex_thread_id": "linked-thread"
    }.get(key))
    with pytest.raises(RuntimeError):
        compress_context(agent, [], "system", force=True)


def test_codex_app_server_compaction_heartbeat_refreshes_activity_while_waiting():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1")
    )
    agent._codex_session = SlowCodexSession(
        agent._codex_session.result,
        agent.touch_calls,
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        task_id="test",
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 1
    assert "context compression started" in agent.touch_calls
    assert "context compression in progress" in agent.touch_calls
    assert agent.touch_calls[-1] == "context compression completed"
    from agent.session_activity import ActivityProvenance

    assert agent.touch_provenances
    assert all(
        p is ActivityProvenance.AGENT_COMPRESSION for p in agent.touch_provenances
    )






def test_codex_app_server_compression_failure_preserves_bookkeeping():
    agent = DummyAgent(TurnResult(error="compact failed"))
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 1
    assert agent.context_compressor.compression_count == 0
    assert agent.context_compressor.last_prompt_tokens == 123
    assert agent.warnings
    assert agent.touch_calls[0] == "context compression started"
    assert agent.touch_calls[-1] == "context compression failed"
    assert agent.status_events == [
        ("lifecycle", COMPACTION_STATUS),
        ("warn", "⚠ Codex app-server compaction failed: compact failed"),
    ]





def test_codex_native_boundary_clears_stale_hermes_fallback_streak():
    from unittest.mock import patch

    from agent.context_compressor import ContextCompressor

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        compressor = ContextCompressor(model="test-model", quiet_mode=True)
    compressor._fallback_compression_streak = 1
    compressor._last_summary_fallback_used = True

    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="normal-turn-1")
    )
    agent.context_compressor = compressor
    turn = TurnResult(
        thread_id="thread-1",
        turn_id="normal-turn-1",
        compacted=True,
    )

    assert _record_codex_app_server_compaction(agent, turn) is True
    assert compressor._fallback_compression_streak == 0
    assert compressor._verify_compaction_cleared_threshold is True


class RecordingCooldownCompressor(SimpleNamespace):
    """Compressor stub exposing the real cooldown API surface."""

    def __init__(self, remaining=0.0):
        super().__init__(
            compression_count=0,
            last_compression_rough_tokens=0,
            last_prompt_tokens=123,
            last_completion_tokens=45,
            awaiting_real_usage_after_compression=False,
        )
        self.remaining = remaining
        self.recorded = []

    def get_active_compression_failure_cooldown(self, *, refresh=False):
        if self.remaining <= 0:
            return None
        return {"remaining_seconds": self.remaining, "error": "prior failure"}

    def _record_compression_failure_cooldown(self, seconds, error):
        self.recorded.append((seconds, error))
        self.remaining = float(seconds)


def test_interrupted_codex_compaction_arms_the_failure_cooldown():
    """Regression: the codex path returned unchanged with no brake, so the
    session stayed above threshold and the next turn retried immediately."""
    from agent.context_compressor import _SUMMARY_FAILURE_COOLDOWN_SECONDS

    agent = DummyAgent(
        TurnResult(
            thread_id="thread-1",
            turn_id="compact-turn-1",
            interrupted=True,
            error="compact turn interrupted",
        ),
        auto_compaction="hermes",
    )
    agent.context_compressor = RecordingCooldownCompressor()
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent, messages, "system", approx_tokens=100000, task_id="test"
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent.context_compressor.recorded == [
        (_SUMMARY_FAILURE_COOLDOWN_SECONDS, "compact turn interrupted")
    ]


def test_codex_compaction_error_without_interrupt_also_arms_cooldown():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1", error="boom"),
        auto_compaction="hermes",
    )
    agent.context_compressor = RecordingCooldownCompressor()
    messages = [{"role": "user", "content": "hi"}]

    compress_context(
        agent, messages, "system", approx_tokens=100000, task_id="test"
    )

    assert len(agent.context_compressor.recorded) == 1
    assert agent.context_compressor.recorded[0][1] == "boom"


def test_active_cooldown_blocks_automatic_codex_compaction():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1"),
        auto_compaction="hermes",
    )
    agent.context_compressor = RecordingCooldownCompressor(remaining=120.0)
    session = agent._codex_session
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent, messages, "system", approx_tokens=100000, task_id="test"
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert session.calls == 0, "compaction ran despite an active cooldown"


def test_force_bypasses_the_codex_compaction_cooldown():
    """An explicit /compress is a user decision and must not be braked by a
    failure it did not cause."""
    agent = DummyAgent(TurnResult(thread_id="thread-1", turn_id="compact-turn-1"))
    agent.context_compressor = RecordingCooldownCompressor(remaining=120.0)
    session = agent._codex_session
    messages = [{"role": "user", "content": "hi"}]

    compress_context(
        agent, messages, "system", approx_tokens=100000, task_id="test", force=True
    )

    assert session.calls == 1


def test_successful_codex_compaction_arms_no_cooldown():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1"),
        auto_compaction="hermes",
    )
    agent.context_compressor = RecordingCooldownCompressor()
    messages = [{"role": "user", "content": "hi"}]

    compress_context(
        agent, messages, "system", approx_tokens=100000, task_id="test"
    )

    assert agent.context_compressor.recorded == []
