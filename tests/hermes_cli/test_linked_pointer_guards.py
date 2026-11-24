"""The shared linked-pointer readers fail CLOSED, and they cover all three agents.

``linked_thread_id`` used to answer ``None`` when a row's ``model_config`` could not be parsed. That
answer DETACHES the row from its native thread: history falls back to the (empty) local transcript
and every mutation guard reads the row as an ordinary, freely writable session — so a corrupt cell
silently turned a pointer row into a branchable, rewindable, re-answerable one.

The readers pinned here are the shared ones every surface now calls
(``hermes_cli/foreign_linked_store.py``), so the contract is stated once:

* a row that is missing or unreadable yields ``None`` (a pointer cannot exist without a row, and a
  store that is genuinely unavailable is refused loudly further down),
* a row whose ``model_config`` EXISTS but cannot be validated raises
  :class:`LinkedConfigUnreadable` (a ``ValueError``, so the gateway's existing wrappers keep turning
  it into a typed ``storage_unavailable`` refusal) or, for the boolean guards, reads as
  ``"foreign"`` — linked with an unknown owner, which refuses rather than permits.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import codex_linked_history
from hermes_cli.foreign_linked_store import (
    LinkedConfigUnreadable, linked_foreign_kind, linked_foreign_label, row_model_config,
)
from persistence.core.errors import AuthorityRefusal
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session

CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"


class _Record:
    def __init__(self, model_config):
        self.model_config = model_config


class _HandleStore:
    """The persistence-handle shape: ``sessions.get`` returns a record with a dict config."""

    def __init__(self, rows):
        self.sessions = type("Sessions", (), {"get": lambda _self, key: rows.get(key)})()


class _SpyDB:
    """The legacy ``SessionDB`` shape: ``get_session`` returns a dict row (config may be text)."""

    def __init__(self, rows):
        self._rows = rows
        self.reads = 0

    def get_session(self, key):
        self.reads += 1
        return self._rows.get(key)


@pytest.fixture
def store(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        yield handle
    finally:
        handle.close()


# ── the reader: tolerance where it is safe, refusal where it is not ────────────────────────────


def test_reader_tolerates_both_store_shapes():
    handle = _HandleStore({"row": _Record({"codex_linked_thread": True})})
    spy = _SpyDB({"row": {"model_config": json.dumps({"claude_linked_thread": True})}})
    assert row_model_config(handle, "row") == {"codex_linked_thread": True}
    assert row_model_config(spy, "row") == {"claude_linked_thread": True}
    assert spy.reads == 1


def test_reader_returns_empty_for_a_row_it_cannot_reach():
    """No row / no reader / a raising getter is "not a pointer", not a refusal."""
    assert row_model_config(_HandleStore({}), "missing") == {}
    assert row_model_config(_HandleStore({"row": _Record(None)}), "row") == {}
    assert row_model_config(object(), "row") == {}
    assert row_model_config(None, "row") == {}

    class _Broken:
        def get_session(self, _key):
            raise OSError("state.db is gone")

    assert row_model_config(_Broken(), "row") == {}


@pytest.mark.parametrize("raw", ["{not json", '["a list"]', "3", "null-ish"])
def test_reader_refuses_a_configuration_it_cannot_validate(raw):
    with pytest.raises(LinkedConfigUnreadable):
        row_model_config(_SpyDB({"row": {"model_config": raw}}), "row")


def test_a_corrupt_cell_is_a_value_error_so_the_gateway_wrapper_still_types_it():
    store = _SpyDB({"row": {"model_config": "{not json"}})
    with pytest.raises(ValueError):
        codex_linked_history.linked_thread_id(store, "row")
    from gateway.session_linked_codex import linked_thread_id as gateway_linked_thread_id

    with pytest.raises(AuthorityRefusal) as caught:
        gateway_linked_thread_id(store, "row")
    assert caught.value.reason == "storage_unavailable"


# ── linked_thread_id: the closed hole ──────────────────────────────────────────────────────────


def test_linked_thread_id_returns_the_pointer_for_a_valid_row(store):
    seed_session(store, "codex", "desktop",
                 model_config={"codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})
    assert codex_linked_history.linked_thread_id(store, "codex") == CODEX_THREAD


def test_linked_thread_id_keeps_the_flag_without_id_refusal(store):
    seed_session(store, "broken", "desktop", model_config={"codex_linked_thread": True})
    with pytest.raises(ValueError, match="no thread ID"):
        codex_linked_history.linked_thread_id(store, "broken")


def test_linked_thread_id_refuses_a_row_whose_config_cannot_be_parsed():
    """THE fix: this used to answer None, which read the row as an ordinary writable session."""
    store = _SpyDB({"row": {"model_config": "{not json"}})
    with pytest.raises(LinkedConfigUnreadable):
        codex_linked_history.linked_thread_id(store, "row")
    # A plain row and an absent row still answer None, exactly as before.
    assert codex_linked_history.linked_thread_id(_SpyDB({}), "row") is None
    assert codex_linked_history.linked_thread_id(
        _SpyDB({"plain": {"model_config": json.dumps({"api_mode": "chat_completions"})}}),
        "plain") is None


# ── linked_foreign_kind: every guard's single question ─────────────────────────────────────────


@pytest.mark.parametrize("flag, agent", [("codex_linked_thread", "codex"),
                                         ("claude_linked_thread", "claude"),
                                         ("mistral_linked_thread", "mistral")])
def test_kind_names_the_owning_tool_for_all_three_agents(flag, agent):
    assert linked_foreign_kind(_HandleStore({"row": _Record({flag: True})}), "row") == agent
    assert linked_foreign_label(agent) in ("Codex", "Claude Code", "Mistral Vibe")


def test_kind_reads_a_legacy_api_mode_only_row_as_linked():
    """The read-only era wrote only ``api_mode``; such a row is still a pointer, not a session."""
    for mode, agent in (("claude_linked_readonly", "claude"), ("mistral_linked_readonly", "mistral"),
                        ("claude_linked_resume", "claude")):
        assert linked_foreign_kind(_HandleStore({"row": _Record({"api_mode": mode})}), "row") == agent


def test_kind_is_none_only_for_a_row_it_can_prove_is_ordinary(store):
    seed_session(store, "plain", "desktop", model_config={"api_mode": "chat_completions"})
    assert linked_foreign_kind(store, "plain") is None
    assert linked_foreign_kind(store, "absent") is None


def test_kind_fails_closed_for_an_unreadable_row():
    assert linked_foreign_kind(_SpyDB({"row": {"model_config": "{not json"}}), "row") == "foreign"
    assert linked_foreign_label("foreign") == "the native tool"


def test_kind_follows_a_model_config_rewrite(store):
    """The guards must see the CURRENT row, not a cached flag: unlink restores ordinary handling."""
    seed_session(store, "row", "desktop",
                 model_config={"claude_linked_thread": True, "claude_session_id": CLAUDE_ID})
    assert linked_foreign_kind(store, "row") == "claude"
    store.sessions.patch_model_config("row", {"claude_linked_thread": None,
                                              "claude_session_id": None,
                                              "mistral_linked_thread": True,
                                              "mistral_session_id": MISTRAL_ID})
    assert linked_foreign_kind(store, "row") == "mistral"
