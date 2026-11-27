"""The classic CLI skips its own provider-credential check only for a linked Codex thread.

Codex authenticates a linked thread itself, so the profile provider's credential must not refuse
that session's turns; every other session keeps the check, or a missing key would reach the
gateway as a turn instead of the CLI's sign-in guidance.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.fixtures.storage_seed import seed_session

THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"

ROWS = {
    "linked": ({"codex_linked_thread": True, "codex_thread_id": THREAD, "api_mode": "codex_app_server"}, True),
    "ordinary": ({"provider": "openai-codex", "api_mode": "codex_app_server"}, False),
    "linked-http-wire": ({"codex_linked_thread": True, "codex_thread_id": THREAD, "api_mode": "codex_responses"}, False),
    "claude-pointer": ({"claude_linked_thread": True, "claude_session_id": THREAD, "api_mode": "codex_app_server"},
                       False),
}


class _CliStore:
    """The classic CLI's ``get_session`` shape (``GatewaySessionStore``): the stored row as a dict."""

    def __init__(self, persistence):
        self._persistence = persistence

    def get_session(self, session_id):
        record = self._persistence.sessions.get(session_id)
        return None if record is None else record.as_session_row()


@pytest.mark.parametrize("name", sorted(ROWS))
def test_only_a_linked_codex_thread_skips_the_cli_credential_check(tmp_path, name):
    from hermes_cli.cli_agent_setup_mixin import _bound_to_linked_codex_thread
    from persistence_assembly import open_persistence, profile_scope

    model_config, skips = ROWS[name]
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, name, "cli", model="gpt-6-luna", model_config=model_config)
        cli = SimpleNamespace(_session_db=_CliStore(db), session_id=name)
        assert _bound_to_linked_codex_thread(cli) is skips
