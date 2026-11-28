"""``session.mutate`` refuses history/policy edits on a linked row — for ALL THREE agents.

The authority's guard used to ask ``gateway.session_linked_codex.linked_thread_id`` and therefore
only ever saw Codex. It now asks the shared ``hermes_cli.foreign_linked_store.linked_foreign_kind``
helper, so a linked Claude Code or Mistral Vibe row refuses a branch/rewind/reset/model/reasoning/
editor-policy change exactly like a linked Codex one — those would either write a second
Hermes-owned transcript beside the native one (a fork) or claim settings the native tool owns.

``workspace`` is the deliberate exception, and only for Codex: its cwd rides on the native thread,
which ``gateway/session_linked_codex.py`` re-homes in place. For Claude Code and Vibe the working
directory lives in their own store, so a Hermes-side move would only make the row disagree with
the thread the next send resumes.
"""

from __future__ import annotations

import pytest

import gateway.session_mutations as mutations
from persistence.core.errors import AuthorityRefusal

CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"

ROWS = {
    "codex": ("Codex", {"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD}),
    "claude": ("Claude Code", {"claude_linked_thread": True, "claude_session_id": CLAUDE_ID}),
    "mistral": ("Mistral Vibe", {"mistral_linked_thread": True, "mistral_session_id": MISTRAL_ID}),
}

RESTRICTED = ("branch", "rewind", "reset", "model", "reasoning", "editor_mcp", "edit_policy")


class _Record:
    def __init__(self, model_config):
        self.model_config = model_config


class _Store:
    """The one call the guard makes on the store: ``sessions.get``."""

    def __init__(self, rows=None, *, corrupt=()):
        self._rows = {key: _Record(config) for key, config in (rows or {}).items()}
        self._corrupt = set(corrupt)
        self.sessions = type("Sessions", (), {"get": lambda _self, key: self._rows.get(key)})()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"unexpected store access: {name}")


def _guard(monkeypatch, store, session_id, operation):
    monkeypatch.setattr(mutations, "authority_persistence", lambda _authority: store)
    mutations._guard_linked_mutation(object(), session_id, operation)


@pytest.mark.parametrize("key", sorted(ROWS))
@pytest.mark.parametrize("operation", RESTRICTED)
def test_every_linked_agent_refuses_history_and_policy_edits(monkeypatch, key, operation):
    label = ROWS[key][0]
    with pytest.raises(AuthorityRefusal) as caught:
        _guard(monkeypatch, _Store({key: ROWS[key][1]}), key, operation)

    assert caught.value.reason == "unsupported_operation"
    assert caught.value.field == "operation"
    assert caught.value.detail == f"change this linked thread’s history or settings in {label}"


@pytest.mark.parametrize("key", ["claude", "mistral"])
def test_a_linked_claude_or_mistral_workspace_move_is_refused(monkeypatch, key):
    label = ROWS[key][0]
    with pytest.raises(AuthorityRefusal) as caught:
        _guard(monkeypatch, _Store({key: ROWS[key][1]}), key, "workspace")

    assert caught.value.reason == "unsupported_operation"
    assert caught.value.field == "workspace"
    assert caught.value.detail == f"this linked thread’s working directory belongs to {label}"


def test_a_linked_codex_workspace_move_keeps_its_native_path(monkeypatch):
    """The linked-Codex feature needs this: Codex re-homes the SAME thread (session_linked_codex)."""
    _guard(monkeypatch, _Store({"codex": ROWS["codex"][1]}), "codex", "workspace")


@pytest.mark.parametrize("operation", [*RESTRICTED, "workspace"])
def test_an_ordinary_row_is_never_restricted(monkeypatch, operation):
    _guard(monkeypatch, _Store({"plain": {"api_mode": "chat_completions"}}), "plain", operation)
    _guard(monkeypatch, _Store(), "absent", operation)


@pytest.mark.parametrize("operation", [*RESTRICTED, "workspace"])
def test_a_row_whose_config_cannot_be_parsed_is_restricted(monkeypatch, operation):
    """Fail closed: a pointer nobody can validate must not be edited as if it were ordinary."""
    store = _Store()
    store._rows["damaged"] = _Record("{not json")
    with pytest.raises(AuthorityRefusal):
        _guard(monkeypatch, store, "damaged", operation)
