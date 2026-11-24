"""Linked sessions store native thread IDs and never import their history."""

from contextlib import contextmanager
import json

import pytest

from hermes_cli import codex_linked_threads as linked
from persistence_assembly import open_persistence, profile_scope
from persistence.conversation.sessions import SessionQuery, SessionWindow
from persistence.conversation.messages import TranscriptRead

ROOT = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CHILD = "01a1c702-d451-7cc0-85f2-9a35815f56b0"


class Client:
    def __init__(self):
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": [{"id": CHILD, "sessionId": ROOT, "cwd": "/home", "name": "A child",
                              "preview": "raw private assistant reply"}],
                    "nextCursor": "next"}
        if method == "thread/read":
            return {"thread": {"id": CHILD, "sessionId": ROOT, "cwd": "/home", "name": "A child",
                               "model": "gpt-6.1-sol", "modelProvider": "openai"}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn1", "items": [{"type": "userMessage", "id": "item1"}]}],
                    "nextCursor": "older"}
        raise AssertionError(method)


@pytest.fixture
def codex(monkeypatch):
    client = Client()

    @contextmanager
    def opened():
        yield client

    monkeypatch.setattr(linked, "_codex_client", opened)
    return client


def test_discovery_and_history_use_individual_thread_id(codex):
    result = linked.list_codex_threads(limit=1)
    assert result["threads"][0]["id"] == CHILD
    assert result["threads"][0]["preview"] == ""
    assert result["next_cursor"] == "next"
    result = linked.read_codex_history(CHILD, cursor="page", limit=20, sort_direction="desc")
    assert result["turns"][0]["id"] == "turn1"
    assert result["next_cursor"] == "older"
    assert codex.calls[-1] == ("thread/turns/list", {"threadId": CHILD, "cursor": "page", "limit": 20,
                                                       "sortDirection": "desc", "itemsView": "full"})
    with pytest.raises(ValueError):
        linked.read_codex_history("../../bad")


def test_read_client_uses_routed_profiles_codex_home_without_inheriting_credentials(
    tmp_path, monkeypatch,
):
    from agent.secret_scope import (
        reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch_home = tmp_path / "launch"
    routed_home = tmp_path / "profiles" / "work"
    launch_codex = tmp_path / "launch-codex"
    routed_codex = tmp_path / "profiles" / "work-codex"
    for path in (launch_home, routed_home, launch_codex, routed_codex):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("CODEX_HOME", str(launch_codex))
    monkeypatch.setenv("OPENAI_API_KEY", "launch-provider-secret")
    monkeypatch.setenv("GH_TOKEN", "launch-github-secret")
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: launch_home)

    home_token = set_hermes_home_override(routed_home)
    secret_token = set_secret_scope({
        "CODEX_HOME": str(routed_codex),
        "OPENAI_API_KEY": "routed-provider-secret",
        "GH_TOKEN": "routed-github-secret",
    }, profile_home=str(routed_home))
    multiplex_token = set_multiplex_context(True)
    captured = {}

    class ReadClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def initialize(self, **_kwargs):
            pass

    monkeypatch.setattr("agent.transports.codex_app_server.CodexAppServerClient", ReadClient)
    monkeypatch.setattr("hermes_cli.codex_runtime_switch.get_configured_codex_binary",
                        lambda _config: "profile-codex")
    try:
        with linked._codex_client(profile_home=routed_home):
            pass
    finally:
        reset_multiplex_context(multiplex_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)

    assert captured["codex_bin"] == "profile-codex"
    assert captured["codex_home"] == str(routed_codex)
    assert captured["read_only"] is True
    assert captured["inherit_credentials"] is False
    assert captured["base_env"]["HERMES_HOME"] == str(routed_home)
    assert "CODEX_HOME" not in captured["base_env"]
    assert "OPENAI_API_KEY" not in captured["base_env"]
    assert "GH_TOKEN" not in captured["base_env"]


def test_read_client_fails_closed_without_routed_profile_scope(tmp_path, monkeypatch):
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch_home, routed_home = tmp_path / "launch", tmp_path / "profiles" / "work"
    launch_home.mkdir()
    routed_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: launch_home)
    home_token = set_hermes_home_override(routed_home)
    multiplex_token = set_multiplex_context(True)
    try:
        with pytest.raises(ValueError, match="no secret scope"):
            with linked._codex_client(profile_home=routed_home):
                pytest.fail("an unscoped routed read must never start Codex")
    finally:
        reset_multiplex_context(multiplex_token)
        reset_hermes_home_override(home_token)


def test_secondary_profile_without_codex_home_cannot_fall_back_to_launch_store(tmp_path, monkeypatch):
    from agent.secret_scope import (
        reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch_home = tmp_path / "launch"
    routed_home = launch_home / "profiles" / "work"
    launch_codex = tmp_path / ".codex"
    routed_home.mkdir(parents=True)
    launch_codex.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setenv("CODEX_HOME", str(launch_codex))
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: launch_home)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    home_token = set_hermes_home_override(routed_home)
    secret_token = set_secret_scope({}, profile_home=str(routed_home))
    multiplex_token = set_multiplex_context(True)
    try:
        with pytest.raises(ValueError, match="configure its own CODEX_HOME"):
            linked.resolve_codex_home(routed_home)
    finally:
        reset_multiplex_context(multiplex_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def test_launch_profile_keeps_the_documented_codex_home_default(tmp_path, monkeypatch):
    from agent.secret_scope import (
        reset_multiplex_context, reset_secret_scope, set_multiplex_context, set_secret_scope,
    )
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch_home = tmp_path / "launch"
    launch_codex = tmp_path / ".codex"
    launch_home.mkdir()
    launch_codex.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr("hermes_constants.get_routing_process_hermes_home", lambda: launch_home)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    home_token = set_hermes_home_override(launch_home)
    secret_token = set_secret_scope({}, profile_home=str(launch_home))
    multiplex_token = set_multiplex_context(True)
    try:
        assert linked.resolve_codex_home(launch_home) == launch_codex
    finally:
        reset_multiplex_context(multiplex_token)
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def test_link_is_idempotent_and_keeps_only_pointer(tmp_path, codex):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        first = linked.link_codex_thread(CHILD, db, "default")
        second = linked.link_codex_thread(CHILD, db, "default")
        assert first["session_id"] == second["session_id"]
        assert not first["already_linked"] and second["already_linked"]
        row = db.sessions.get(first["session_id"])
        config = row.model_config
        assert config["codex_thread_id"] == CHILD
        assert config["codex_linked_thread"] is True
        assert config["api_mode"] == "codex_app_server"
        assert config["provider"] == "openai"
        assert row.model == "gpt-6.1-sol"
        assert row.cwd == "/home"
        assert list(db.messages.read_transcript(first["session_id"], TranscriptRead())) == []
        listed1 = {r.session.session_id for r in db.sessions.list_sessions(
            SessionQuery(min_message_count=1), SessionWindow(limit=100))}
        listed2 = {r.session.session_id for r in db.sessions.list_sessions(
            SessionQuery(min_message_count=2), SessionWindow(limit=100))}
        assert first["session_id"] in listed1
        assert first["session_id"] not in listed2
        assert all(method != "thread/resume" for method, _ in codex.calls)
    finally:
        db.close()


def test_linked_title_does_not_collide_with_bot_chat_adoption(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    class BotChatClient:
        def request(self, method, params):
            assert method == "thread/read"
            return {"thread": {"id": params["threadId"], "ephemeral": False, "cwd": str(cwd),
                               "name": "Bot Chat", "model": "codex-model"}}

    @contextmanager
    def opened():
        yield BotChatClient()

    monkeypatch.setattr(linked, "_codex_client", opened)
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        linked_session = linked.link_codex_thread(CHILD, db, "default")
        row = db.sessions.get(linked_session["session_id"])
        assert row.title == f"Bot Chat (Codex {CHILD[-6:]})"
        assert list(db.messages.read_transcript(linked_session["session_id"], TranscriptRead())) == []
    finally:
        db.close()


def test_link_and_discovery_never_use_raw_preview_as_a_title(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()

    class PreviewOnlyClient:
        def request(self, method, params):
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "ephemeral": False, "cwd": str(cwd),
                                   "preview": "raw private assistant reply"}}
            if method == "thread/list":
                return {"data": [{"id": CHILD, "name": None,
                                  "preview": "raw private assistant reply", "cwd": str(cwd)}],
                        "nextCursor": None}
            raise AssertionError(method)

    @contextmanager
    def opened():
        yield PreviewOnlyClient()

    monkeypatch.setattr(linked, "_codex_client", opened)
    listed = linked.list_codex_threads(limit=1)
    assert listed["threads"][0]["title"] == "Codex thread"
    assert listed["threads"][0]["preview"] == ""
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        linked_session = linked.link_codex_thread(CHILD, db, "default")
        assert db.sessions.get(linked_session["session_id"]).title == "Codex thread"
    finally:
        db.close()
