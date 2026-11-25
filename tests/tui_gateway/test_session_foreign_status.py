"""``session.foreign.status`` serves the linked views' host capability on the SERVING backend.

The Desktop may be connected to a gateway over the network, so the linked list/link/history views
cannot answer "is Claude Code installed" from the client. This RPC answers it for the host, per
agent, profile-scoped like its ``session.{codex,claude,mistral}.*`` siblings — and it must stay
purely additive: a client that predates it keeps the read-only linked behaviour, so the handler
never refuses a request because a store or CLI is missing (each agent carries its own reason).

Also pinned here: the legacy-fallback allowlist. Session verbs live on the session authority, and
a verb missing from that allowlist answers ``-32601`` through the authority — which a remote
Desktop can only read as "this gateway predates the feature".
"""

from __future__ import annotations

import types

import pytest

from tui_gateway import server

METHOD = "session.foreign.status"
#: The unpatched resolver: the profile-scope test needs the real "no such profile" refusal.
_REAL_PROFILE_HOME = server._profile_home


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(server, "_hermes_home", home)
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def _call(method: str, **params):
    result = server._methods[method](1, params)
    assert "error" not in result, result
    return result["result"]


def test_status_is_registered_and_reports_every_agent(profile):
    assert METHOD in server._methods
    result = _call(METHOD)
    assert isinstance(result["host"], str) and result["host"]
    assert [row["agent"] for row in result["agents"]] == ["codex", "claude", "mistral"]
    for row in result["agents"]:
        assert set(row) == {"agent", "label", "store", "cli", "continue_status"}
        assert set(row["store"]) == {"available", "readable", "reason_code", "reason"}
        assert set(row["cli"]) >= {"available", "command", "path", "version", "source",
                                   "reason_code", "reason"}
        assert set(row["continue_status"]) == {"supported", "writable", "reason_code", "reason"}


def test_status_answers_on_a_host_with_no_foreign_tools(profile):
    """No store, no CLI here — still a 200-shaped answer with reasons, never an error."""
    result = _call(METHOD)
    for row in result["agents"]:
        assert row["store"]["available"] is False
        assert row["store"]["reason_code"] == "store_missing"
        assert row["continue_status"]["writable"] is False
        assert row["continue_status"]["reason_code"] == "store_missing"
        assert "this host" not in row["store"]["reason"] or result["host"] in row["store"]["reason"]


def test_status_never_leaks_an_absolute_host_path(profile):
    import json

    payload = json.dumps(_call(METHOD))
    assert str(profile) not in payload


def test_status_is_profile_scoped_through_the_request_dispatcher(profile, monkeypatch):
    """A named profile that does not exist is the typed 4064 refusal, not a launch-profile read."""
    monkeypatch.setattr(server, "_profile_home", _REAL_PROFILE_HOME)
    response = server.handle_request({"id": "p", "jsonrpc": "2.0", "method": METHOD,
                                      "params": {"profile": "no-such-profile"}})
    assert response["error"]["code"] == 4064


def test_status_never_blocks_the_ws_reader():
    """Store/CLI resolution can spawn bounded version probes: it belongs in the slow pool."""
    assert METHOD in server._LONG_HANDLERS


@pytest.mark.parametrize("method", [
    METHOD,
    "session.claude.list", "session.claude.history", "session.claude.link",
    "session.mistral.list", "session.mistral.history", "session.mistral.link",
    "session.codex.list", "session.codex.history", "session.codex.link",
    "session.foreign.list", "session.foreign.preview", "session.foreign.import",
])
def test_linked_verbs_survive_the_authority_boundary(method):
    """An authority-attached (remote) client must reach the legacy foreign handlers.

    ``session.*`` verbs are owned by the session authority; anything it answers ``-32601`` for
    only falls back to the sidecar handler when the allowlist names it. The Claude/Vibe twins of
    the Codex verbs were missing, so every linked Claude/Vibe browse on a remote Desktop answered
    "unknown method" while the same view worked over a local socket.
    """
    from gateway.runtime_bootstrap import _PURPOSE_CAPABILITIES
    from tui_gateway.ws_legacy_fallback import legacy_fallback_allowed

    viewer = types.SimpleNamespace(capabilities=_PURPOSE_CAPABILITIES["interactive"])
    worker = types.SimpleNamespace(capabilities={"worker:adopt"})
    assert legacy_fallback_allowed(viewer, method) is True
    # A worker-adoption ticket does not widen into the interactive grant.
    assert legacy_fallback_allowed(worker, method) is False
