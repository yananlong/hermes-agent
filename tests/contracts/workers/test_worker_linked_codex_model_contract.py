"""A worker may publish a model only for the assigned, still-matching Codex pointer."""

from __future__ import annotations

import pytest

from persistence import AuthorityEpoch, ExecutionGeneration, SessionId
from persistence.conversation.sessions import NewSession
from persistence.workers.executions import WorkerExecutionId
from tests.contracts._reference._authority import InjectedFailure, authority_state
from tests.contracts._reference.workers.receipt import WRITE_FAILPOINT
from tests.contracts.conftest import require_failpoints
from tests.contracts.workers._support import SESSION, ident, last_sequence, refused, revision, worker


THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
OTHER_THREAD = "02a1c4a4-7a29-7011-8493-04445af16d6e"
OPERATION = "session.linked_codex_model"


def _config(thread=THREAD, **extra):
    return {"codex_linked_thread": True, "codex_thread_id": thread,
            "api_mode": "codex_app_server", **extra}


def _apply(store, epoch, *, model="new-model", thread_id=THREAD, **identity):
    return store.worker_executions.set_linked_codex_model(
        **ident(epoch, **identity), model=model, thread_id=thread_id)


def _raw(store, epoch, payload, **identity):
    return store.worker_executions.persist_operation(
        **ident(epoch, **identity), operation=OPERATION, payload=payload)


def test_native_model_publication_is_fenced_receipted_and_replayable(backend, scope_a):
    config = _config(model="old-model", browser_model_lock={"locked": True},
                     gateway_runtime={"provider": "custom", "api_mode": "codex_responses"})
    store, epoch = worker(backend, scope_a, model="old-model", model_config=config)
    before = revision(store)
    assert _apply(store, epoch) is True
    row = store.sessions.get(SESSION)
    assert row.model == "new-model"
    assert row.model_config["model"] == "new-model"
    assert "browser_model_lock" not in row.model_config
    assert row.model_config["codex_thread_id"] == THREAD
    assert row.model_config["gateway_runtime"] == config["gateway_runtime"]
    assert revision(store) == before + 1
    assert _raw(store, epoch, {"model": "new-model", "thread_id": THREAD}) == {"value": True}
    assert revision(store) == before + 1
    with refused("admission_conflict"):
        _apply(store, epoch, model="different")
    assert last_sequence(backend, scope_a, epoch) == 1
    # An exact retry after a later retarget replays the receipt without changing the new pointer.
    store.sessions.patch_model_config(SESSION, {"codex_thread_id": OTHER_THREAD})
    assert _apply(store, epoch) is True
    assert store.sessions.get(SESSION).model_config["codex_thread_id"] == OTHER_THREAD


def test_malformed_payload_is_refused_without_advancing_the_worker(backend, scope_a):
    store, epoch = worker(backend, scope_a, model="old-model", model_config=_config())
    bad = [
        {}, {"model": "m"}, {"thread_id": THREAD},
        {"model": "m", "thread_id": THREAD, "extra": True},
        {"model": "", "thread_id": THREAD}, {"model": " \n", "thread_id": THREAD},
        {"model": None, "thread_id": THREAD}, {"model": 123, "thread_id": THREAD},
        {"model": "x" * 4097, "thread_id": THREAD},
        {"model": "m", "thread_id": None}, {"model": "m", "thread_id": ""},
        {"model": "m", "thread_id": "not-a-uuid"},
        {"model": "m", "thread_id": THREAD.upper()},
    ]
    for payload in bad:
        with refused("invalid_params"):
            _raw(store, epoch, payload)
    assert last_sequence(backend, scope_a, epoch) == 0
    assert store.sessions.get(SESSION).model == "old-model"
    _apply(store, epoch)


@pytest.mark.parametrize("config", [
    {}, {"codex_thread_id": THREAD}, _config(codex_linked_thread=False),
    _config(thread=OTHER_THREAD), _config(claude_linked_thread=True),
    _config(mistral_linked_thread=True), _config(claude_linked_thread="yes"),
    _config(codex_linked_thread=1),
], ids=["ordinary", "missing-flag", "flag-off", "other-thread", "claude-conflict",
        "mistral-conflict", "invalid-claude-flag", "invalid-codex-flag"])
def test_owner_refuses_wrong_or_ambiguous_pointer_without_changes(backend, scope_a, config):
    store, epoch = worker(backend, scope_a, model="old-model", model_config=config)
    assert _apply(store, epoch) is False
    assert store.sessions.get(SESSION).model == "old-model"
    assert last_sequence(backend, scope_a, epoch) == 1


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", None], ids=["invalid-json", "array", "null", "absent"])
def test_owner_refuses_an_unreadable_or_nonpointer_row(backend, scope_a, raw):
    store, epoch = worker(backend, scope_a, model="old-model", model_config=_config())
    backend.scope_store(scope_a)["sessions"][SESSION]["model_config"] = raw
    assert _apply(store, epoch) is False
    assert store.sessions.get(SESSION).model == "old-model"
    assert last_sequence(backend, scope_a, epoch) == 1


def test_retarget_after_resume_refuses_the_old_thread_and_allows_the_new_one(backend, scope_a):
    store, epoch = worker(backend, scope_a, model="old-model", model_config=_config())
    store.sessions.patch_model_config(SESSION, {"codex_thread_id": OTHER_THREAD})
    assert _apply(store, epoch, thread_id=THREAD) is False
    assert store.sessions.get(SESSION).model == "old-model"
    assert last_sequence(backend, scope_a, epoch) == 1
    assert _apply(store, epoch, thread_id=OTHER_THREAD, sequence=2) is True
    assert store.sessions.get(SESSION).model == "new-model"


def test_matching_model_still_checks_the_pointer_without_rewriting_the_row(backend, scope_a):
    store, epoch = worker(backend, scope_a, model="new-model", model_config=_config(model="new-model"))
    before = revision(store)
    assert _apply(store, epoch, thread_id=OTHER_THREAD) is False
    assert revision(store) == before and last_sequence(backend, scope_a, epoch) == 1
    assert _apply(store, epoch, sequence=2) is True
    assert revision(store) == before and last_sequence(backend, scope_a, epoch) == 2


def test_worker_assignment_epoch_generation_and_session_fences_precede_the_write(backend, scope_a):
    store, epoch = worker(backend, scope_a, model="old-model", model_config=_config())
    other = SessionId("other-session")
    store.sessions.create(NewSession(session_id=other, source="contract", model="untouched", model_config=_config()))
    with refused("permission_denied"):
        _apply(store, epoch, session_id=other)
    with refused("not_found"):
        _apply(store, epoch, execution_id=WorkerExecutionId("unknown-worker"))
    with refused("stale_epoch"):
        _apply(store, AuthorityEpoch(int(epoch) + 1))
    with refused("stale_generation"):
        _apply(store, epoch, generation=ExecutionGeneration(1))
    assert store.sessions.get(SESSION).model == "old-model"
    assert store.sessions.get(other).model == "untouched"
    assert last_sequence(backend, scope_a, epoch) == 0


def test_same_ids_in_another_profile_do_not_authorize_the_other_thread(backend, scope_a, scope_b):
    first, epoch_a = worker(backend, scope_a, model="model-a", model_config=_config())
    second, epoch_b = worker(backend, scope_b, model="model-b", model_config=_config(thread=OTHER_THREAD))
    _apply(first, epoch_a, model="new-a")
    assert _apply(second, epoch_b, model="wrong", thread_id=THREAD) is False
    assert second.sessions.get(SESSION).model == "model-b"
    _apply(second, epoch_b, model="new-b", thread_id=OTHER_THREAD, sequence=2)
    assert first.sessions.get(SESSION).model == "new-a"
    assert second.sessions.get(SESSION).model == "new-b"


def test_publication_rolls_back_if_the_receipt_cannot_be_committed(backend, scope_a):
    require_failpoints(backend)
    store, epoch = worker(backend, scope_a, model="old-model", model_config=_config())
    authority_state(backend, scope_a).failpoints.arm(WRITE_FAILPOINT)
    with pytest.raises(InjectedFailure):
        _apply(store, epoch)
    assert store.sessions.get(SESSION).model == "old-model"
    assert last_sequence(backend, scope_a, epoch) == 0
    _apply(store, epoch)
    assert store.sessions.get(SESSION).model == "new-model"
