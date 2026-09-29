from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any

import pytest

import cli
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.services.llm.base_llm import BaseLLM
from src.graph.graph import Graph
from src.nodes.pre_process_node import PreProcessNode
from src.payload_store.payload_store import is_reference
from src.services.repository import SQLiteFacilityStatusRepository


_SCOPE_ENV = "STG_DEFAULT_DISASTER_EVENT_ID"
_AZURE_ENV = {
    "AZURE_OPENAI_API_KEY": "unit-key",
    "AZURE_OPENAI_ENDPOINT": "https://unit-resource.invalid",
    "AZURE_OPENAI_DEPLOYMENT": "unit-deployment",
}
_REGISTRY = [
    {
        "facility_id": "FAC-A",
        "name": "中央第一小学校",
        "aliases": ["中央第一小"],
        "importance": "critical",
    }
]


class _CountingLlm(BaseLLM):
    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.calls.append(messages)
        return {
            "content": json.dumps({"briefs": []}),
            "tool_calls": [],
            "model": "unit-model",
            "usage": {},
        }

    def stream(self, messages: list[Any]):
        del messages
        yield ""

    def bind_tools(self, tools: list[Any]) -> "_CountingLlm":
        del tools
        return self


def _set_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SCOPE_ENV, "event-marketplace")
    for key, value in _AZURE_ENV.items():
        monkeypatch.setenv(key, value)


def _runtime_config() -> dict[str, Any]:
    return cli.load_agent_config(cli._ROOT)


def _construct(
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm: BaseLLM | None = None,
    include_llm_key: bool = True,
) -> tuple[cli.MarketplaceGraph, SQLiteFacilityStatusRepository]:
    _set_environment(monkeypatch)
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=_REGISTRY)
    monkeypatch.setattr(cli, "create_repository", lambda config: repository)
    config = _runtime_config()
    if include_llm_key:
        config["llm"] = llm
    agent = cli.MarketplaceGraph(config=config)
    return agent, repository


def _context() -> InvocationContext:
    return InvocationContext(
        session_id="marketplace-session",
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )


def test_runner_order_uses_ref_discards_history_and_returns_output(monkeypatch):
    _set_environment(monkeypatch)
    repository = SQLiteFacilityStatusRepository(":memory:", registry_seed=_REGISTRY)
    monkeypatch.setattr(cli, "create_repository", lambda config: repository)
    calls: list[list[Any]] = []

    def complete(_self, messages):
        calls.append(messages)
        return {
            "content": json.dumps({"briefs": []}),
            "tool_calls": [],
            "model": "unit-model",
            "usage": {},
        }

    monkeypatch.setattr(cli.MarketplaceAzureLLM, "complete", complete)
    captured: dict[str, Any] = {}
    original_invoke = Graph.invoke

    def capture_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(user_input=user_input, input_context=input_context)
        return original_invoke(
            self,
            user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )

    monkeypatch.setattr(Graph, "invoke", capture_invoke)
    agent = cli.MarketplaceGraph(config=_runtime_config())
    agent.compile()
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            json.dumps({"mode": "ingest", "facility_ids": ["FAC-A"]}),
            ctx=_context(),
            input_context={"conversation_history": [{"role": "user", "content": "MARKETPLACE_CANARY"}]},
        )

    assert result["status"] == "success"
    assert isinstance(result["output"], dict) and result["output"]
    assert result["output"]["mode"] == "invoke"
    assert "facility_result_empty" in result["output"]["degradation_reason"]
    assert "MARKETPLACE_CANARY" not in json.dumps(result, ensure_ascii=False)
    assert captured["input_context"] == {}
    assert is_reference(captured["user_input"])
    assert len(captured["user_input"]) == 32
    assert calls
    assert agent.config["repository"] is repository


@pytest.mark.parametrize("requested_mode", ["ingest", "feedback", "invoke", "unexpected"])
def test_every_supplied_mode_is_forced_to_invoke(monkeypatch, requested_mode):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    try:
        agent.compile()
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                json.dumps({"mode": requested_mode, "facility_ids": ["FAC-A"]}),
                ctx=_context(),
            )
    finally:
        repository.close()

    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"


@pytest.mark.parametrize("requested_mode", ["ingest", "feedback"])
def test_write_mode_is_invoke_in_stored_envelope_and_processed_state(monkeypatch, requested_mode):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    stored_envelopes: list[dict[str, Any]] = []
    processed_modes: list[tuple[str | None, str | None]] = []
    original_put = agent._marketplace_payload_store.put
    original_execute = PreProcessNode.execute

    def capture_put(value, *, scope, session_id, envelope=False):
        if envelope:
            stored_envelopes.append(dict(value))
        return original_put(value, scope=scope, session_id=session_id, envelope=envelope)

    def capture_execute(self, state):
        delta = original_execute(self, state)
        processed_modes.append((delta.get("request_mode"), delta.get("mode")))
        return delta

    monkeypatch.setattr(agent._marketplace_payload_store, "put", capture_put)
    monkeypatch.setattr(PreProcessNode, "execute", capture_execute)
    try:
        agent.compile()
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(json.dumps({"mode": requested_mode}), ctx=_context())
    finally:
        repository.close()

    assert stored_envelopes
    assert stored_envelopes[0]["mode"] == "invoke"
    assert processed_modes == [("invoke", "invoke")]
    assert result["status"] == "success"
    assert result["output"]["mode"] == "invoke"


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "ingest", "record_kind": "damage_report", "records": []},
        {"mode": "feedback", "decisions": []},
    ],
)
def test_write_mode_fields_are_rejected_before_storage(monkeypatch, payload):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    try:
        agent.compile()
        with pytest.raises(ValueError, match="E_UNKNOWN_KEY"):
            agent.invoke(json.dumps(payload), ctx=_context())
        assert agent._marketplace_payload_store.envelope_count() == 0
    finally:
        repository.close()


@pytest.mark.parametrize("value", [None, "", "   ", " padded", "padded ", "line\nbreak"])
def test_scope_misconfiguration_fails_during_construction(monkeypatch, value):
    _set_environment(monkeypatch)
    if value is None:
        monkeypatch.delenv(_SCOPE_ENV, raising=False)
    else:
        monkeypatch.setenv(_SCOPE_ENV, value)

    with pytest.raises(ValueError, match=_SCOPE_ENV):
        cli.MarketplaceGraph(config=_runtime_config())


@pytest.mark.parametrize("key", ["scope", "disaster_event_id"])
def test_caller_supplied_scope_is_rejected(monkeypatch, key):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    try:
        agent.compile()
        with pytest.raises(ValueError, match="E_UNKNOWN_KEY"):
            agent.invoke(json.dumps({"mode": "invoke", key: "caller-scope"}), ctx=_context())
    finally:
        repository.close()


def test_runner_config_receives_default_llm_before_base_initialization(monkeypatch):
    agent, repository = _construct(monkeypatch, include_llm_key=False)
    try:
        assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
        assert vars(agent.config["llm"]) == {}
    finally:
        repository.close()


def test_explicit_none_llm_is_replaced_without_setdefault(monkeypatch):
    agent, repository = _construct(monkeypatch, llm=None)
    try:
        assert isinstance(agent.config["llm"], cli.MarketplaceAzureLLM)
    finally:
        repository.close()


def test_existing_llm_is_preserved(monkeypatch):
    injected = _CountingLlm()
    agent, repository = _construct(monkeypatch, llm=injected)
    try:
        assert agent.config["llm"] is injected
    finally:
        repository.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://unit-resource.invalid/openai",
        "https://unit-resource.invalid/v1",
        "https://unit-resource.invalid?api-version=1",
        "https://",
    ],
)
def test_invalid_azure_endpoint_fails_during_provision(monkeypatch, endpoint):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    try:
        with pytest.raises(ValueError, match="bare Azure resource endpoint"):
            agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    finally:
        repository.close()


@pytest.mark.parametrize("key", sorted(_AZURE_ENV))
def test_missing_azure_value_fails_during_provision(monkeypatch, key):
    agent, repository = _construct(monkeypatch, llm=_CountingLlm())
    monkeypatch.delenv(key)
    try:
        with pytest.raises(ValueError, match=key):
            agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    finally:
        repository.close()


def test_azure_client_resolves_values_only_when_called(monkeypatch):
    _set_environment(monkeypatch)
    captured: dict[str, Any] = {}
    module = ModuleType("shared.services.llm.azure_openai_client")

    class AzureOpenAIClient:
        def __init__(self, config):
            captured.update(config)

        def complete(self, messages):
            return {"content": "{}", "messages": messages}

        def stream(self, messages):
            del messages
            yield ""

        def bind_tools(self, tools):
            del tools
            return self

    module.AzureOpenAIClient = AzureOpenAIClient
    monkeypatch.setitem(sys.modules, module.__name__, module)
    llm = cli.MarketplaceAzureLLM()
    assert vars(llm) == {}
    provider = cli._AllowlistedEnvironmentProvider()
    with bound_secrets(provider):
        response = llm.complete([{"role": "user", "content": "hello"}])

    assert response["content"] == "{}"
    assert captured == {
        "api_key": _AZURE_ENV["AZURE_OPENAI_API_KEY"],
        "azure_endpoint": _AZURE_ENV["AZURE_OPENAI_ENDPOINT"],
        "azure_deployment": _AZURE_ENV["AZURE_OPENAI_DEPLOYMENT"],
    }


def test_bound_llm_returns_a_new_wrapper():
    llm = cli.MarketplaceAzureLLM()
    bound = llm.bind_tools([{"name": "unit-tool"}])
    assert bound is not llm
    assert getattr(bound, "_tools") == ({"name": "unit-tool"},)


def test_main_forwards_loaded_config(monkeypatch):
    captured: dict[str, Any] = {}

    def record(agent_cls, *, agent_name, namespace, config):
        captured.update(
            agent_cls=agent_cls,
            agent_name=agent_name,
            namespace=namespace,
            config=config,
        )

    monkeypatch.setattr(cli, "run_agent_marketplace", record)
    cli.main()

    assert captured["agent_cls"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "GOV-C2-086"
    assert captured["namespace"] == "gov"
    assert captured["config"] == _runtime_config()
