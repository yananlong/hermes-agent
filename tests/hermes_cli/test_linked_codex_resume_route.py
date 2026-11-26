"""A resumed linked Codex pointer row runs on the codex app-server runtime.

The classic CLI resume path (``_restore_session_model``) must leave the CLI — and therefore the
agent ``_init_agent`` builds from its runtime snapshot (`api_mode=...`) — on the pointer row's
stored binding ``api_mode: codex_app_server``, whatever per-model HTTP route a later turn stamped
under ``gateway_runtime``. The turn guard in ``agent/conversation_loop.py`` refuses
``_is_linked_codex_thread(agent) and agent.api_mode != "codex_app_server"``, so this is the seam
that decides whether a linked session can be continued at all.

Both CLI resume branches are covered: the ordinary restore, and the early-return branch
(``stored_session_route`` answers None when the stored model and provider already match the
ambient ones) that previously skipped the binding entirely.
"""

from __future__ import annotations

import json

import cli as cli_mod
from hermes_cli.cli_model_switch_mixin import linked_row_binding_api_mode, stored_session_route
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import session_row
from tests.fixtures.storage_seed import seed_session


def _pointer_config(**extra):
    """The live row shape: the codex binding plus a per-model HTTP route stamped beside it."""
    config = {
        "codex_thread_id": "01a2153d-f5a6-7632-bf24-a0d21f7f4594",
        "codex_linked_thread": True,
        "api_mode": "codex_app_server",
        "provider": "openai",
        "gateway_runtime": {
            "provider": "custom", "base_url": "https://api.openai.com/v1",
            "api_mode": "codex_responses", "model": "gpt-6-luna", "fallback_active": False,
        },
    }
    config.update(extra)
    return config


def _cli(**ambient):
    """Bare HermesCLI the way the resume paths see it (no __init__)."""
    stub = object.__new__(cli_mod.HermesCLI)
    stub.model = "ambient-model"
    stub.provider = "ambient-provider"
    stub.requested_provider = "ambient-provider"
    stub.base_url = "https://ambient/v1"
    stub.api_key = "ambient-key"
    stub.api_mode = "chat_completions"
    stub.agent = None
    stub._console_print = lambda s: None
    stub._explicit_model_override = False
    for key, value in ambient.items():
        setattr(stub, key, value)
    return stub


def test_restore_session_model_ends_on_codex_app_server():
    stub = _cli()
    stub._restore_session_model({"model": "gpt-6-luna", "model_config": _pointer_config()})
    assert stub.model == "gpt-6-luna"
    assert stub.provider == "openai"
    # The binding, not the nested HTTP route: the rebuilt agent would pass codex_app_server to
    # AIAgent and the linked-turn guard admits the row.
    assert stub.api_mode == "codex_app_server"


def test_restore_session_model_keeps_the_binding_when_route_is_already_current():
    # CONTRACT: stored_model == current_model and the provider did not change make
    # stored_session_route return None; the binding api_mode must still be restored, or the
    # linked-turn guard refuses on the ambient wire.
    stub = _cli(model="gpt-6-luna", provider="openai", requested_provider="openai",
                base_url="https://api.openai.com/v1", api_mode="codex_responses")
    stub._restore_session_model({"model": "gpt-6-luna", "model_config": _pointer_config()})
    assert stub.api_mode == "codex_app_server"


def test_linked_row_binding_api_mode_reads_only_pointer_rows():
    # CONTRACT: the helper answers the binding only for linked pointer rows; an ordinary row's
    # api_mode keeps its ordinary per-model meaning.
    assert linked_row_binding_api_mode({"model_config": _pointer_config()}) == "codex_app_server"
    assert linked_row_binding_api_mode({"model_config": {"api_mode": "codex_app_server"}}) is None
    assert linked_row_binding_api_mode({}) is None
    assert linked_row_binding_api_mode({"model_config": "{not json"}) is None
    # text form stored by SessionDB rows decodes too
    assert linked_row_binding_api_mode({"model_config": json.dumps(_pointer_config())}) == "codex_app_server"


def test_stored_session_route_carries_the_binding(tmp_path):
    # CONTRACT: through a real persistence row (the storage interface, not a hand-built dict),
    # the route a resume reads carries the binding wire.
    with open_persistence(profile_scope(home=tmp_path)) as db:
        seed_session(db, "s1", "desktop", model="gpt-6-luna")
        db.sessions.patch_model_config("s1", _pointer_config())
        row = session_row(db, "s1")
    route = stored_session_route(row, current_model="ambient-model", current_provider="ambient-provider")
    assert route is not None and route[3] == "codex_app_server"


def test_a_claude_pointer_row_resumes_as_a_pointer_not_as_an_http_route():
    """A linked Claude row's binding is the ROW FLAG, and a nested HTTP route must not outrank it.

    The row's ``api_mode`` value (``claude_linked_readonly``) is NOT a runtime mode: agent init
    resolves it to an ordinary wire mode by design, and ``agent/linked_foreign_turn.py`` routes on
    the row flag precisely so the runtime mode can never identify a pointer
    (``LINKED_FOREIGN_ROW_MODES`` documents this). So the contract to pin is that the pointer
    binding still reaches the resume — ``linked_row_binding_api_mode`` answers from the row — while
    the nested route's HTTP wire does not become the row's route.
    """
    from hermes_cli.cli_model_switch_mixin import linked_row_binding_api_mode
    config = {"claude_linked_thread": True, "claude_session_id": "a-b-c",
              "api_mode": "claude_linked_readonly",
              "gateway_runtime": {"provider": "custom", "api_mode": "chat_completions", "model": "m"}}
    row = {"model": "claude-opus-4", "model_config": config}
    assert linked_row_binding_api_mode(row) == "claude_linked_readonly"
    # And the projection never hands back the nested HTTP wire as this row's mode.
    from persistence.gateway.routing import gateway_runtime
    assert gateway_runtime(config).api_mode == "claude_linked_readonly"


def test_codec_custom_provider_reports_no_model_derived_mode_stomp():
    # CONTRACT: after the restore, the CLI's per-model re-derivation (opencode/nous style
    # overrides) must not rewrite the binding: model_derived_api_mode('openai', ...) answers None
    # today, and if a provider ever claims a per-model wire for openai rows the binding still
    # wins because the row is a pointer (see test_gateway_runtime...). Pin the direct fact here:
    from hermes_cli.model_switch import model_derived_api_mode
    assert model_derived_api_mode("openai", "gpt-6-luna") is None


def test_the_link_output_names_the_thread_it_linked(monkeypatch, tmp_path):
    """Bug: linking printed only an id. The row's title is stored, so the link names the thread.

    Drives the real command handler against a scripted gateway owner; the title comes from the row
    the link created (a read back through the CLI's store view), never from a widened RPC.
    """
    import hermes_cli.cli_link_thread_command as link_cmd

    printed: list[str] = []
    monkeypatch.setattr(link_cmd, "_cp", lambda *lines: printed.extend(lines))

    class Owner:
        def __init__(self):
            self.calls = []

        def call(self, method, **params):
            self.calls.append((method, params))
            if method == "session.foreign.status":
                return {"agents": [{"agent": "codex", "label": "Codex",
                                    "continue_status": {"writable": True}}]}
            if method == "session.codex.link":
                return {"session_id": "20261126_102327_040dcc999418", "already_linked": False}
            raise AssertionError(f"unexpected {method}")

        def get_session(self, session_id):
            assert session_id == "20261126_102327_040dcc999418"
            return {"title": "algos"}

    class CLI:
        _agent_running = False
        _pending_link_threads = None

        def __init__(self):
            self._session_db = Owner()
            self.resumed = []

        def _handle_resume_command(self, cmd):
            self.resumed.append(cmd)

    cli = CLI()
    link_cmd.handle_link_thread_command(cli, "/link-thread codex 01a2153d-f5a6-7632-bf24-a0d21f7f4594")
    joined = " ".join(printed)
    assert "algos" in joined, joined
    assert cli.resumed == ["/resume 20261126_102327_040dcc999418"]


def test_an_already_linked_row_names_the_thread_too(monkeypatch):
    import hermes_cli.cli_link_thread_command as link_cmd

    printed: list[str] = []
    monkeypatch.setattr(link_cmd, "_cp", lambda *lines: printed.extend(lines))

    class Owner:
        def call(self, method, **params):
            if method == "session.foreign.status":
                return {"agents": [{"agent": "codex", "continue_status": {"writable": True}}]}
            return {"session_id": "s-1", "already_linked": True}

        def get_session(self, session_id):
            return {"title": "algos"}

    class CLI:
        _agent_running = False
        _pending_link_threads = None
        _session_db = Owner()

        def _handle_resume_command(self, cmd):
            pass

    link_cmd.handle_link_thread_command(CLI(), "/link-thread codex 01a2153d")
    assert "algos" in " ".join(printed)


def test_a_link_still_succeeds_when_the_title_cannot_be_read(monkeypatch):
    """A failed title read must never undo a link that already happened."""
    import hermes_cli.cli_link_thread_command as link_cmd

    printed: list[str] = []
    monkeypatch.setattr(link_cmd, "_cp", lambda *lines: printed.extend(lines))

    class Owner:
        def call(self, method, **params):
            if method == "session.foreign.status":
                return {"agents": [{"agent": "codex", "continue_status": {"writable": True}}]}
            return {"session_id": "s-2", "already_linked": False}

        def get_session(self, session_id):
            raise RuntimeError("store unavailable")

    class CLI:
        _agent_running = False
        _pending_link_threads = None
        _session_db = Owner()

        def _handle_resume_command(self, cmd):
            pass

    link_cmd.handle_link_thread_command(CLI(), "/link-thread codex 01a2153d")
    assert any("Codex" in line for line in printed)


def test_the_live_agent_follows_the_binding_when_nothing_else_changed():
    """The guard reads ``agent.api_mode``, not ``cli.api_mode``.

    ``/link`` resumes onto a CLI whose agent was already built for the previous session, and
    ``stored_session_route`` answers None when the stored model and provider already match. Setting
    only the CLI field left that live agent on the ambient wire, so the no-fork guard refused with
    'Linked Codex sessions require the Codex app-server runtime' — a real, reported failure.
    """
    from hermes_cli.cli_model_switch_mixin import _apply_linked_binding_api_mode

    class Agent:
        api_mode = "chat_completions"

        def __init__(self):
            self.switched = None

        def switch_model(self, **kwargs):
            self.switched = kwargs
            self.api_mode = kwargs["api_mode"]

    class CLI:
        model = "gpt-6-luna"
        provider = "openai"
        api_mode = "codex_app_server"
        api_key = "k"
        base_url = "https://api.openai.com/v1"

        def __init__(self):
            self.agent = Agent()

    cli = CLI()
    _apply_linked_binding_api_mode(cli, {"model_config": _pointer_config()})
    assert cli.agent.api_mode == "codex_app_server"
    assert cli.agent.switched["api_mode"] == "codex_app_server"


def test_a_missing_credential_shape_does_not_silently_skip_the_swap():
    """A CLI lacking api_key/base_url must still reach switch_model.

    The first cut read ``cli.api_key`` directly and raised AttributeError; the surrounding except
    swallowed it, so the binding never reached the agent and the guard refused with no diagnostic.
    """
    from hermes_cli.cli_model_switch_mixin import _apply_linked_binding_api_mode

    class Agent:
        api_mode = "chat_completions"

        def __init__(self):
            self.switched = None

        def switch_model(self, **kwargs):
            self.switched = kwargs
            self.api_mode = kwargs["api_mode"]

    class Bare:  # deliberately no api_key / base_url attributes
        model = "gpt-6-luna"
        provider = "openai"
        api_mode = "codex_app_server"

        def __init__(self):
            self.agent = Agent()

    cli = Bare()
    _apply_linked_binding_api_mode(cli, {"model_config": _pointer_config()})
    assert cli.agent.api_mode == "codex_app_server"
    assert cli.agent.switched["api_key"] == ""


def test_an_ordinary_row_does_not_touch_the_live_agent():
    """Only pointer rows carry the binding; an ordinary row's agent must be left alone."""
    from hermes_cli.cli_model_switch_mixin import _apply_linked_binding_api_mode

    class Agent:
        api_mode = "chat_completions"

        def switch_model(self, **kwargs):
            raise AssertionError("an ordinary row must not rewire the agent")

    class CLI:
        agent = Agent()
        api_mode = "chat_completions"

    cli = CLI()
    _apply_linked_binding_api_mode(cli, {"model_config": {"provider": "openai"}})
    assert cli.agent.api_mode == "chat_completions"
