# server.py must build its secrets provider
# before Graph() and read the API key with .get() (never .require()), so a
# missing ANTHROPIC_API_KEY degrades to config={"llm": None} instead of
# crashing the process at import — deploy-stg provisions no key today.

import importlib


class TestServerBootsWithoutAnthropicKey:
    def test_server_imports_and_app_constructs_with_no_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        import src.api.server as server

        importlib.reload(server)

        assert server.app is not None
        assert server.agent is not None


class TestServerConstructsLlmWithKey:
    def test_llm_is_constructed_and_reaches_main_node(self, monkeypatch):
        from shared.secrets.inmemory_provider import InMemoryProvider

        monkeypatch.setattr(
            "shared.secrets.factory",
            lambda namespace, agent_name: InMemoryProvider({"ANTHROPIC_API_KEY": "dummy-test-key"}),
        )

        import src.api.server as server

        importlib.reload(server)

        from shared.services.llm.anthropic_client import AnthropicClient

        assert isinstance(server._llm, AnthropicClient)
        assert server.agent._nodes["main"]._llm is server._llm


class TestStandaloneTrustPromotion:
    def test_external_bearer_never_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert server._resolve_standalone_trust(
            TrustLevel.ANONYMOUS, "Bearer external", "external", "runner"
        ) is TrustLevel.VERIFIED_EXTERNAL

    def test_runner_bearer_promotes_to_internal(self):
        import src.api.server as server
        from framework.schemas.trust_level import TrustLevel

        assert server._resolve_standalone_trust(
            TrustLevel.ANONYMOUS, "Bearer runner", "external", "runner"
        ) is TrustLevel.INTERNAL

    def test_wrong_or_missing_bearer_is_rejected_when_auth_is_enabled(self):
        import pytest
        import src.api.server as server
        from fastapi import HTTPException
        from framework.schemas.trust_level import TrustLevel

        for authorization in ("", "Bearer wrong"):
            with pytest.raises(HTTPException) as exc:
                server._resolve_standalone_trust(TrustLevel.ANONYMOUS, authorization, "external", "runner")
            assert exc.value.status_code == 401
