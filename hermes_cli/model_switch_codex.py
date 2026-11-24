"""Codex app-server routing for the OpenAI model alias."""

from __future__ import annotations

from typing import Any


def openai_provider_def(state: Any):
    """Resolve the keyless OpenAI alias only for an explicit Codex app-server configuration."""
    requested = getattr(state, "explicit_provider", "") or getattr(state, "current_provider", "")
    if str(requested or "").strip().lower() != "openai":
        return None

    from hermes_cli.runtime_provider import _get_model_config

    model_cfg = _get_model_config()
    if not isinstance(model_cfg, dict) or str(model_cfg.get("openai_runtime") or "").strip().lower() != "codex_app_server":
        return None

    # A provider explicitly named ``openai`` has its own route contract, which the runtime overlay also honors.
    from hermes_cli.providers import ProviderDef, resolve_custom_provider, resolve_user_provider

    if (resolve_user_provider("openai", state.user_providers or {})
            or resolve_custom_provider("openai", state.custom_providers or [])):
        return None

    return ProviderDef(
        id="openai", name="OpenAI Codex CLI", transport="codex_responses",
        api_key_env_vars=(), base_url="https://api.openai.com/v1", source="codex-app-server",
    )
