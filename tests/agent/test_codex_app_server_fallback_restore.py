"""A native Codex session's fallback remains turn-scoped when it is not a linked pointer."""

from agent.turn_recovery import activate_codex_app_server_fallback
from run_agent import AIAgent
from tests.fakes.providers.chat_variants import FakeChatVariantServer


def test_unlinked_codex_app_server_falls_back_and_restores_primary():
    # Both endpoints are loopback: a failure in the routing or credential path cannot reach a service.
    with FakeChatVariantServer(default_text="fallback response") as fallback:
        agent = AIAgent(
            model="gpt-5.5",
            provider="openai-codex",
            api_mode="codex_app_server",
            api_key="primary-test-key",
            base_url="http://127.0.0.1:9/v1",
            fallback_model={
                "provider": "custom",
                "model": "fake-model",
                "base_url": fallback.base_url,
                "api_key": "fallback-test-key",
                "api_mode": "chat_completions",
            },
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            enabled_toolsets=[],
        )
        try:
            assert activate_codex_app_server_fallback(agent, {"error": "429 Too Many Requests"})
            stream = agent.client.chat.completions.create(
                model=agent.model,
                messages=[{"role": "user", "content": "hello"}],
                tools=[{"type": "function", "function": {"name": "check", "parameters": {"type": "object"}}}],
                stream=True,
            )
            text = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
            assert text == "fallback response"

            # End the retry cooldown so the next turn may restore its original runtime.
            agent._rate_limited_until = 0
            assert agent._restore_primary_runtime()
            assert agent.api_mode == "codex_app_server"
            assert agent.model == "gpt-5.5"
        finally:
            agent.close()
