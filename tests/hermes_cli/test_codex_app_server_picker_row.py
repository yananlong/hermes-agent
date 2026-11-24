"""The Desktop model picker exposes the keyless Codex app-server alias when opted in."""

from __future__ import annotations

from hermes_cli.inventory import ConfigContext, build_model_options_payload


def _context(*, provider="anthropic", model="claude-sonnet", runtime="", excluded=None):
    return ConfigContext(
        current_provider=provider,
        current_model=model,
        current_base_url="",
        user_providers={},
        custom_providers=[],
        excluded_providers=excluded or [],
        openai_runtime=runtime,
    )


def _isolate_inventory(monkeypatch, *, providers=None, codex_models=None):
    """Stub discovery and expensive decorators while keeping the real picker payload builder."""
    from hermes_cli import inventory

    rows = providers or [{
        "slug": "anthropic", "name": "Anthropic", "models": ["claude-sonnet"],
        "total_models": 1, "is_current": True, "is_user_defined": False,
        "source": "built-in", "authenticated": True, "auth_type": "api_key",
    }, {
        "slug": "openrouter", "name": "OpenRouter", "models": ["vendor/model"],
        "total_models": 1, "is_current": False, "is_user_defined": False,
        "source": "built-in", "authenticated": True, "auth_type": "api_key",
    }]
    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", lambda **_kwargs: [dict(r) for r in rows])
    monkeypatch.setattr("hermes_cli.auth.is_provider_explicitly_configured", lambda _slug: True)
    monkeypatch.setattr(inventory, "_local_runtime_row", lambda _ctx: None)
    monkeypatch.setattr(inventory, "_moa_provider_row", lambda _provider: None)
    monkeypatch.setattr(inventory, "_codex_app_server_model_ids", lambda: None)
    monkeypatch.setattr(inventory, "_strip_aggregator_overlaps", lambda _rows: None)
    monkeypatch.setattr(inventory, "_apply_pricing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory, "_apply_capabilities", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory, "_apply_featured", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(inventory, "_apply_custom_aliases", lambda _rows: None)
    monkeypatch.setattr(inventory, "_apply_limits", lambda _rows: None)
    monkeypatch.setattr(inventory, "_apply_usage", lambda _rows: None)
    monkeypatch.setattr(inventory, "_prewarm_pricing_async", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.models_validate.drop_unofferable_model_ids", lambda _rows: None)
    if codex_models is not None:
        monkeypatch.setattr("hermes_cli.codex_models.get_codex_model_ids", codex_models)


def test_picker_context_reads_the_runtime_opt_in(monkeypatch):
    from hermes_cli import inventory

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
        "model": {"provider": "openai", "default": "gpt-6.1-terra", "openai_runtime": "CODEX_APP_SERVER"},
        "providers": {}, "custom_providers": [],
    })

    context = inventory.load_picker_context()

    assert context.current_provider == "openai"
    assert context.openai_runtime == "codex_app_server"


def test_picker_shows_codex_alias_only_when_codex_runtime_is_opted_in(monkeypatch):
    _isolate_inventory(monkeypatch)
    monkeypatch.setattr("hermes_cli.inventory._codex_app_server_model_ids", lambda: [
        "gpt-6-luna", "gpt-5.6-sol", "gpt-6.1-terra", "gpt-6-astra",
        "gpt-6.1-sol", "gpt-6-astra-900k", "gpt-6.1-sol-900k",
    ])

    normal = build_model_options_payload(_context(), explicit_only=True)
    assert "openai" not in {row["slug"] for row in normal["providers"]}

    opted_in = build_model_options_payload(
        _context(runtime="codex_app_server"), explicit_only=True,
    )
    by_slug = {row["slug"]: row for row in opted_in["providers"]}
    codex = by_slug["openai"]
    assert codex["name"] == "OpenAI Codex CLI"
    assert codex["models"] == ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"]
    assert codex["source"] == "codex-app-server"
    assert codex["authenticated"] is True
    assert codex["auth_type"] == "codex_app_server"
    assert codex["key_env"] == ""
    assert codex["warning"] is None
    assert codex["is_current"] is False
    assert by_slug["anthropic"]["models"] == ["claude-sonnet"]
    assert by_slug["openrouter"]["models"] == ["vendor/model"]


def test_picker_fallback_keeps_only_supported_current_base_model(monkeypatch):
    _isolate_inventory(monkeypatch)
    calls = []

    def codex_models(*args, **kwargs):
        calls.append((args, kwargs))
        return ["gpt-6-luna"]

    monkeypatch.setattr("hermes_cli.codex_models.get_codex_model_ids", codex_models)
    payload = build_model_options_payload(
        _context(provider="openai", model="gpt-6-astra", runtime="codex_app_server"),
        explicit_only=True,
    )

    row = next(row for row in payload["providers"] if row["slug"] == "openai")
    assert calls == [((), {})]
    assert row["models"] == ["gpt-6-astra", "gpt-6-luna"]
    assert row["is_current"] is True
    assert row["authenticated"] is False
    assert row["warning"] == "Could not verify the Codex account model list; displayed models may be incomplete."


def test_live_catalog_is_authoritative_and_hides_unavailable_models_and_context_aliases(monkeypatch):
    _isolate_inventory(monkeypatch)
    monkeypatch.setattr("hermes_cli.inventory._codex_app_server_model_ids", lambda: [
        "gpt-6-luna", "gpt-6-astra-900k", "gpt-6-sol", "gpt-6.1-terra-900k",
    ])

    payload = build_model_options_payload(
        _context(provider="openai", model="gpt-6.1-sol", runtime="codex_app_server"),
        explicit_only=True,
    )

    row = next(row for row in payload["providers"] if row["slug"] == "openai")
    assert row["models"] == ["gpt-6-luna"]


def test_codex_app_server_catalog_reader_returns_visible_model_ids_and_closes_client(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from pathlib import Path

    from hermes_cli import inventory

    class FakeClient:
        closed = False
        initialized = False

        def request(self, method, params, timeout):
            assert self.initialized
            assert (method, params, timeout) == ("model/list", {"limit": 200}, 8)
            return {"data": [
                {"model": "gpt-6-astra", "hidden": False},
                {"model": "gpt-6.1-sol", "hidden": False},
                {"model": "gpt-6.1-sol-900k", "hidden": False},
                {"model": "gpt-6-astra-hidden", "hidden": True},
            ]}

        def close(self):
            self.closed = True

    clients = []
    homes = []
    codex_home = tmp_path / "codex-home"

    @contextmanager
    def make_client(*, profile_home):
        assert Path(profile_home) == active_home[0]
        client = FakeClient()
        client.initialized = True
        clients.append(client)
        homes.append(Path(profile_home))
        try:
            yield client
        finally:
            client.close()

    monkeypatch.setattr(inventory, "_codex_catalog_cache", {})
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    monkeypatch.setattr("hermes_cli.codex_runtime_switch.get_configured_codex_binary", lambda _cfg: "/test/codex")
    monkeypatch.setattr("hermes_cli.codex_linked_threads._codex_client", make_client)
    monkeypatch.setattr("agent.secret_scope.get_secret", lambda name: str(codex_home) if name == "CODEX_HOME" else None)
    codex_home.mkdir()
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir()
    profile_b.mkdir()
    active_home = [profile_a]
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: active_home[0])

    assert inventory._codex_app_server_model_ids() == [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-sol-900k",
    ]
    assert clients[0].closed is True
    active_home[0] = profile_b
    assert inventory._codex_app_server_model_ids() == [
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-sol-900k",
    ]
    assert homes == [profile_a, profile_b]


def test_codex_app_server_catalog_reader_fails_closed_when_profile_codex_home_is_unavailable(monkeypatch):
    from hermes_cli import inventory

    def unavailable_codex_home(**_kwargs):
        raise ValueError("profile has no Codex home")

    monkeypatch.setattr(inventory, "_codex_catalog_cache", {})
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    monkeypatch.setattr("hermes_cli.codex_runtime_switch.get_configured_codex_binary", lambda _cfg: "/test/codex")
    monkeypatch.setattr("hermes_cli.codex_linked_threads.resolve_codex_home", unavailable_codex_home)

    assert inventory._codex_app_server_model_ids() is None


def test_picker_honors_openai_exclusion_for_the_codex_alias(monkeypatch):
    _isolate_inventory(monkeypatch, codex_models=lambda: ["gpt-6-sol"])

    payload = build_model_options_payload(
        _context(runtime="codex_app_server", excluded=["OpenAI"]), explicit_only=True,
    )

    assert "openai" not in {row["slug"] for row in payload["providers"]}
    assert {"anthropic", "openrouter"} <= {row["slug"] for row in payload["providers"]}


def test_picking_openai_keeps_the_codex_route_and_validates_as_codex(monkeypatch):
    """The Desktop choice becomes `/model … --provider openai`; that must not normalize to OpenRouter."""
    from hermes_cli import model_switch

    monkeypatch.setattr("hermes_cli.runtime_provider._get_model_config", lambda: {
        "provider": "openai", "default": "gpt-6.1-terra", "openai_runtime": "codex_app_server",
    })
    runtime_calls = []

    def resolve_runtime(**kwargs):
        runtime_calls.append(kwargs)
        return {
            "provider": "custom", "api_key": "no-key-required",
            "base_url": "https://api.openai.com/v1", "api_mode": "codex_app_server",
        }

    validation_calls = []

    def validate(model, provider, **_kwargs):
        validation_calls.append((model, provider))
        return {"accepted": True, "persist": True, "recognized": True, "message": None}

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", resolve_runtime)
    monkeypatch.setattr("hermes_cli.model_switch.resolve_alias", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("hermes_cli.models_validate.validate_requested_model", validate)
    monkeypatch.setattr(model_switch, "get_model_capabilities", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_switch, "get_model_info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.native_compaction.resolve_native_compaction_capabilities", lambda **_kwargs: {})
    monkeypatch.setattr("hermes_cli.model_picker_usage.large_model_notice", lambda *_args, **_kwargs: None)

    result = model_switch.switch_model(
        raw_input="gpt-6.1-terra", current_provider="anthropic", current_model="claude-sonnet",
        explicit_provider="openai", user_providers={}, custom_providers=[],
    )

    assert result.success, result.error_message
    assert result.target_provider == "openai"
    assert result.new_model == "gpt-6.1-terra"
    assert result.api_mode == "codex_app_server"
    assert runtime_calls == [{
        "target_model": "gpt-6.1-terra", "requested": "openai", "explicit_base_url": None,
    }]
    assert validation_calls == [("gpt-6.1-terra", "openai-codex")]
    updates = model_switch.model_selection_config_updates(result, {"provider": "anthropic"})
    assert updates["provider"] == "openai"
