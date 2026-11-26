"""A linked foreign pointer row resumes on its own structural binding, not a nested HTTP route.

The link paths (``hermes_cli/*_linked_threads.py``) store a native transcript binding at the TOP
level of ``model_config`` — ``codex_linked_thread`` + ``api_mode: codex_app_server``, or the
Claude/Mistral pointer flags with their row modes. A later gateway turn or /model switch stamps an
ordinary per-model route under ``gateway_runtime`` beside it (verified on a live row: the binding
said ``codex_app_server`` while the nested route said ``codex_responses``). ``resume_route`` used to
give the nested route precedence, so every resume rebuilt the agent on the HTTP wire and the
linked-turn guard refused the row ("Linked Codex sessions require the Codex app-server runtime").

The projection is the one choke point both resume surfaces read (CLI ``--resume`` /
``_restore_session_model`` and TUI/desktop ``session.resume`` via
``_stored_session_runtime_overrides``), so the contract lives here: for a row carrying a linked
flag, the stored binding wins over the nested route — the binding is structural (which runtime owns
the transcript), the nested route is a per-model routing preference.
"""

from __future__ import annotations

import json

from persistence.gateway.routing import GatewayRuntime, gateway_runtime, resume_route


def _codex_pointer_row(**extra):
    """The exact live shape (session 20261126_102327_*, title 'algos'): the binding plus a
    per-model HTTP route a later turn stamped under ``gateway_runtime``."""
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


def test_gateway_runtime_keeps_the_linked_binding_over_a_stamped_http_route():
    # CONTRACT: a linked Codex pointer row's api_mode stays codex_app_server even when a nested
    # gateway_runtime route disagrees; the nested provider/base_url do not win the row either.
    assert gateway_runtime(_codex_pointer_row()) == GatewayRuntime(provider="openai", api_mode="codex_app_server")


def test_resume_route_returns_the_binding_api_mode():
    # CONTRACT: resume pairs the row's own model with the binding's route; the nested HTTP route
    # neither picks the model nor carries the wire.
    model, route = resume_route(_codex_pointer_row(), model="gpt-6-luna")
    assert model == "gpt-6-luna"
    assert route.api_mode == "codex_app_server"
    assert route.provider == "openai"


def test_resume_route_reads_a_json_text_pointer_row():
    # CONTRACT: the CLI reads model_config as its JSON text; the binding survives that decode.
    model, route = resume_route(json.dumps(_codex_pointer_row()), model="gpt-6-luna")
    assert route.api_mode == "codex_app_server"


def test_a_pointer_row_without_a_stamped_route_is_unchanged():
    # CONTRACT: the fix must not regress a freshly linked row, which carries no nested route.
    fresh = {k: v for k, v in _codex_pointer_row().items() if k != "gateway_runtime"}
    assert resume_route(fresh, model="gpt-6-luna") == ("gpt-6-luna", GatewayRuntime(
        provider="openai", api_mode="codex_app_server"))


def test_a_pointer_row_without_its_own_api_mode_still_routes_sensibly():
    # CONTRACT: a damaged pointer row (flag, no stored binding) must not fall back to the nested
    # HTTP route; it resumes with the binding empty so the turn guards fail closed, not on a wire.
    damaged = {k: v for k, v in _codex_pointer_row().items() if k != "api_mode"}
    assert gateway_runtime(damaged) == GatewayRuntime(provider="openai")


def test_claude_and_mistral_pointer_rows_keep_their_row_modes():
    # CONTRACT: the same class of fix covers the linked Claude/Mistral pointer rows — their row
    # api_mode is the legacy pointer shape the send path reads, and a nested HTTP route stamped
    # beside it must not overwrite it either.
    for flag, row_mode in (("claude_linked_thread", "claude_linked_readonly"),
                           ("mistral_linked_thread", "mistral_linked_readonly")):
        config = {
            flag: True, "api_mode": row_mode,
            "gateway_runtime": {"provider": "custom", "base_url": "https://x/v1",
                                "api_mode": "chat_completions", "model": "some-model"},
        }
        assert gateway_runtime(config).api_mode == row_mode, flag


def test_an_ordinary_rows_nested_route_precedence_is_unchanged():
    # CONTRACT: the nested gateway_runtime route keeps winning on ordinary rows (the #125942 /
    # legacy precedence); only linked pointer rows route from their own binding.
    ordinary = {
        "provider": "anthropic", "api_mode": "anthropic_messages",
        "gateway_runtime": {"provider": "openai-codex", "api_mode": "codex_responses"},
    }
    assert gateway_runtime(ordinary) == GatewayRuntime(provider="openai-codex", api_mode="codex_responses")
    assert resume_route(ordinary, model="gpt-6-sol")[1].api_mode == "codex_responses"
