"""B2: a linked Codex pointer row must appear in the classic CLI's session list.

``link_codex_thread`` stamps the pointer with the surface that linked it (``source="desktop"``,
``hermes_cli/codex_linked_threads.py:201``), while in-chat ``/sessions`` and ``/resume`` query
``source="cli"`` (``hermes_cli/cli_session_mixin.py:383-386``). A pointer row holds only a native
thread id — its transcript lives in Codex — so no surface's ordinary source filter is the right
answer for it: every surface can open it.

The field is opt-in per listing (``include_linked_pointers``) so the fix cannot widen a
source-scoped list into showing another surface's ordinary sessions. Both proofs are pinned here:
the pointer appears, and a desktop row WITHOUT a pointer marker does not.
"""

from __future__ import annotations

from types import SimpleNamespace

from hermes_cli import session_listing
from hermes_cli.session_listing import query_session_listing
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_session

CODEX_CONFIG = {"codex_thread_id": "01a212d9-09be-7fb3-b8c7-90082e4fb805",
                "codex_linked_thread": True, "api_mode": "codex_app_server"}
CLAUDE_CONFIG = {"claude_session_id": "3f6a1c2e-0000-4000-8000-000000000001",
                 "claude_linked_thread": True}


def _seed(home):
    """A CLI session, a linked Codex pointer (stamped ``desktop``), a plain desktop row."""
    with open_persistence(profile_scope(home=home)) as store:
        seed_session(store, "cli-session", "cli", title="cli chat")
        seed_session(store, "codex-pointer", "desktop", title="Codex thread",
                     model_config=dict(CODEX_CONFIG))
        seed_session(store, "claude-pointer", "desktop", title="Claude thread",
                     model_config=dict(CLAUDE_CONFIG))
        seed_session(store, "desktop-only", "desktop", title="desktop chat")
        store.sessions._store.holder()._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET message_count=1, created_source=? "
            "WHERE id IN ('cli-session', 'desktop-only')", ("cli",)))


def _ids(home) -> list[str]:
    with open_persistence(profile_scope(home=home)) as store:
        rows = query_session_listing(
            store, source="cli", current_session_id="current", include_unnamed=True,
            limit=10, include_linked_pointers=True, persistence=store)
    return sorted(row["id"] for row in rows)


def _ids_without_pointers(home) -> list[str]:
    with open_persistence(profile_scope(home=home)) as store:
        rows = query_session_listing(
            store, source="cli", current_session_id="current", include_unnamed=True, limit=10,
            persistence=store)
    return sorted(row["id"] for row in rows)


def test_the_cli_list_includes_linked_pointers_from_any_surface(tmp_path):
    """The CLI list shows the Codex and Claude pointers, whatever surface linked them."""
    _seed(tmp_path)
    assert _ids(tmp_path) == ["claude-pointer", "cli-session", "codex-pointer"]


def test_widening_for_pointers_never_pulls_in_an_ordinary_desktop_row(tmp_path):
    """Only the pointer markers widen the list: a plain desktop session stays out."""
    _seed(tmp_path)
    listed = _ids(tmp_path)
    assert "desktop-only" not in listed
    assert "desktop-only" not in _ids_without_pointers(tmp_path)


def test_the_widening_is_opt_in_so_other_source_scoped_lists_keep_their_shape(tmp_path):
    """Not asking for pointers is still exactly the old source-scoped page."""
    _seed(tmp_path)
    assert _ids_without_pointers(tmp_path) == ["cli-session"]


def test_the_shared_listing_seam_reaches_a_gateway_reader(tmp_path, monkeypatch):
    """The gateway-backed reader (the path ``/sessions`` actually uses in a gateway CLI) too."""
    from hermes_cli.gateway_session_store import GatewaySessionStore

    _seed(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = GatewaySessionStore(connection=SimpleNamespace(final=None))
    try:
        reader = store._db()
        assert reader is not None
        rows = query_session_listing(
            reader, source="cli", current_session_id="current", include_unnamed=True,
            limit=10, include_linked_pointers=True)
    finally:
        store.close_reader()
    assert sorted(row["id"] for row in rows) == ["claude-pointer", "cli-session", "codex-pointer"]
