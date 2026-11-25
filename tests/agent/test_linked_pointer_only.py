"""A linked row is a POINTER: a turn must never write its transcript into Hermes SQLite.

A linked Claude Code / Mistral Vibe session row holds a native session id and nothing else —
the transcript lives in that tool's own store on the gateway host. So the ONLY thing a send may
do is append to the native thread; the pointer row's own message list stays empty, and display
re-reads the native store (``hermes_cli/{claude,mistral}_linked_history.py``).

These tests drive a real ``AIAgent.run_conversation`` (the production send entry point, turn
prologue included) with the native transport stubbed at the point where it spawns the CLI: no
durable write may land on a pointer row, and the prompt must still reach the native transport.

Regression: the turn prologue persisted the accepted user row before the linked route dispatched
(``agent/conversation_loop.py::_turn_without_loop``), so every linked send left a second, unseen
copy of the prompt in the pointer row — the local transcript the pointer model says must not exist.
The fix is at the one durable-write funnel (``agent/session_persistence.py``), so the turn-start
crash persist, the turn-end flush and a surface's close persist are all covered.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
PROMPT = "summarize the flaky test"


class _Transport:
    """The native CLI's write: records the prompts it was handed, answers with one turn."""

    def __init__(self):
        self.prompts: list[str] = []

    def claude(self, agent, store, session_id, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(
            native_session_id=CLAUDE_ID,
            rows=[{"role": "user", "content": prompt},
                  {"role": "assistant", "content": "echo: " + prompt}],
            final_text="echo: " + prompt,
        )

    def mistral(self, agent, store, session_id, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(
            native_session_id=MISTRAL_ID,
            messages=[{"role": "user", "content": prompt},
                      {"role": "assistant", "content": "echo: " + prompt}],
            assistant_text="echo: " + prompt,
        )


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        yield handle
    finally:
        handle.close()


def _agent(store, session_id):
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="stub", base_url="https://stub.invalid", provider="openai", model="test/model",
        quiet_mode=True, skip_context_files=True, skip_memory=True,
        persistence=store, session_id=session_id,
    )
    agent._skip_mcp_refresh = True
    agent._spawn_background_review = lambda **kw: None
    return agent


@pytest.fixture(params=[("claude", CLAUDE_ID), ("mistral", MISTRAL_ID)], ids=["claude", "mistral"])
def linked(request, store, monkeypatch):
    kind, native_id = request.param
    from agent import linked_foreign_turn

    seed_session(store, "linked", "desktop", title="linked chat",
                 model_config={f"{kind}_linked_thread": True, f"{kind}_session_id": native_id,
                               "api_mode": f"{kind}_linked_readonly"})
    transport = _Transport()
    monkeypatch.setattr(linked_foreign_turn, "_run_claude", transport.claude)
    monkeypatch.setattr(linked_foreign_turn, "_run_mistral", transport.mistral)
    monkeypatch.setattr(linked_foreign_turn, "_linked_config", lambda: None)
    return SimpleNamespace(kind=kind, native_id=native_id, transport=transport)


def test_a_linked_turn_appends_to_the_native_thread_and_writes_no_local_transcript(linked, store):
    agent = _agent(store, "linked")

    result = agent.run_conversation(PROMPT, conversation_history=[])

    assert linked.transport.prompts == [PROMPT], "the prompt must reach the native transport"
    assert result["completed"] is True and result["error"] is None
    assert result["final_response"] == "echo: " + PROMPT
    assert result["linked_foreign_agent"] == linked.kind
    assert result["linked_native_session_id"] == linked.native_id
    assert result["agent_persisted"] is True
    # Pointer semantics: the native store is the transcript, so the row keeps NO local copy.
    assert transcript_rows(store, "linked") == [], \
        "a linked turn must not persist its transcript into the pointer row"


def test_a_second_linked_turn_does_not_accumulate_local_rows(linked, store):
    agent = _agent(store, "linked")

    agent.run_conversation(PROMPT, conversation_history=[])
    agent.run_conversation("and the second one", conversation_history=[])

    assert linked.transport.prompts == [PROMPT, "and the second one"]
    assert transcript_rows(store, "linked") == []


def test_a_refused_linked_turn_also_writes_nothing_to_the_pointer_row(linked, store, monkeypatch):
    """The prologue's persistence is what leaked, so a REFUSED send must not write either."""
    from agent import linked_foreign_turn
    from agent.transports.vibe_cli_resume import VibeBinaryMissing
    from hermes_cli.claude_linked_resume import LinkedClaudeError

    def refuse(agent, store, session_id, prompt):
        raise (LinkedClaudeError("claude_missing_binary", "Claude Code is not installed")
               if linked.kind == "claude" else VibeBinaryMissing("Vibe is not installed"))

    monkeypatch.setattr(linked_foreign_turn, "_run_claude", refuse)
    monkeypatch.setattr(linked_foreign_turn, "_run_mistral", refuse)

    result = _agent(store, "linked").run_conversation(PROMPT, conversation_history=[])

    assert result["completed"] is False
    assert result["linked_error_code"] == (
        "claude_missing_binary" if linked.kind == "claude" else "vibe_binary_missing")
    assert transcript_rows(store, "linked") == []


def test_a_surface_close_persist_also_writes_nothing_to_the_pointer_row(linked, store):
    """The turn's own result rows are display-only: a surface's close flush must not mirror them.

    ``hermes_cli/cli_session_mixin._persist_active_session_before_close`` and the gateway's
    shutdown drain both re-persist ``result["messages"]``. Those rows are the native projection,
    so the durable-write funnel must drop them exactly like the turn's own flushes.
    """
    agent = _agent(store, "linked")
    result = agent.run_conversation(PROMPT, conversation_history=[])
    assert [row["role"] for row in result["messages"]] == ["user", "assistant"]

    # The close path, verbatim: the snapshot the agent holds, persisted.
    agent._persist_session(result["messages"], [])

    assert transcript_rows(store, "linked") == []
