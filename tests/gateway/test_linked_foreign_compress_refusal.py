"""``session.mutate(compress)`` refuses a linked Claude/Mistral row as a typed refusal.

The gateway's authority path used to key on ``linked_thread_id``, which only ever answers for a
Codex pointer: a linked Claude Code / Mistral Vibe row fell through to the local context compressor,
i.e. a billable summary of a transcript Hermes does not own plus a session split of a row that has
no messages. The kind decides now, and the codex route is unchanged behind it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway import session_mutation_compress as compress
from persistence.core.errors import AuthorityRefusal
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session

CLAUDE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MISTRAL_ID = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
CODEX_THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"


@pytest.fixture
def store(tmp_path):
    handle = open_persistence(profile_scope(home=tmp_path))
    try:
        yield handle
    finally:
        handle.close()


def _authority(store):
    return SimpleNamespace(persistence=store, profile_id=str(store.scope.profile_dir),
                           runner=SimpleNamespace(_resolve_session_agent_runtime=lambda **_kw: ("test/model", {})))


def _prepared(target):
    return {"snapshot": {"receipt": {"policy": {}}, "target": target}}


@pytest.mark.parametrize("kind", ["claude", "mistral"])
def test_a_linked_foreign_row_is_refused_before_any_summarizer_runs(kind, store, monkeypatch):
    from agent.context_compressor import ContextCompressor

    def _never(self, *_args, **_kwargs):  # pragma: no cover - the refusal must come first
        raise AssertionError("the local summarizer must not run for a linked native row")

    monkeypatch.setattr(ContextCompressor, "compress", _never)
    native_id = CLAUDE_ID if kind == "claude" else MISTRAL_ID
    seed_session(store, "row", "desktop", model_config={
        f"{kind}_linked_thread": True, f"{kind}_session_id": native_id,
        "api_mode": f"{kind}_linked_readonly"})
    live = SimpleNamespace(source=SimpleNamespace(platform="cli"), route="agent:main:cli")

    with pytest.raises(AuthorityRefusal) as caught:
        import asyncio
        asyncio.run(compress.prepare_compress(_authority(store), live, {"focus": ""}, _prepared("row")))

    assert caught.value.reason == "unsupported_compress_options"
    label = "Claude Code" if kind == "claude" else "Mistral Vibe"
    assert label in (caught.value.detail or "")


def test_a_linked_codex_row_keeps_its_native_compaction_path(store, monkeypatch):
    calls = []

    async def _native(authority, live, request, prepared, linked_id):
        calls.append(linked_id)
        return {"status": "compressed", "messages": []}

    monkeypatch.setattr(compress, "_prepare_linked_codex_compress", _native)
    seed_session(store, "row", "desktop", model_config={
        "codex_linked_thread": True, "codex_thread_id": CODEX_THREAD})
    live = SimpleNamespace(source=SimpleNamespace(platform="cli"), route="agent:main:cli")

    import asyncio
    prepared = asyncio.run(compress.prepare_compress(_authority(store), live, {"focus": ""}, _prepared("row")))

    assert calls == [CODEX_THREAD]
    assert prepared == {"status": "compressed", "messages": []}


def test_an_ordinary_row_still_takes_the_local_route(store, monkeypatch):
    """The refusal is scoped to pointer rows: an ordinary session keeps its summarizer."""
    seed_session(store, "plain", "desktop", model_config={"api_mode": "chat_completions"})
    monkeypatch.setattr(compress, "_prepare_linked_codex_compress",
                        lambda *_a, **_k: pytest.fail("an ordinary row has no native thread"))

    import asyncio
    with pytest.raises(Exception) as caught:
        asyncio.run(compress.prepare_compress(_authority(store), SimpleNamespace(
            source=SimpleNamespace(platform="cli"), route="agent:main:cli"), {"preview": True}, _prepared("plain")))
    # It reaches the local path (which needs a policy receipt this stub does not carry) instead of
    # being refused as linked.
    assert not isinstance(caught.value, AuthorityRefusal) or caught.value.reason != "unsupported_compress_options"
