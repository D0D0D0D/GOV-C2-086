"""Unit coverage for the Cat2 boundary node."""

import inspect

import yaml

from src.nodes.main_node import MainNode
from src.payload_store.payload_store import PayloadStore
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository import SQLiteFacilityStatusRepository


def _configured_node():
    config = yaml.safe_load(open("config/config.yaml"))
    config["repository"] = SQLiteFacilityStatusRepository(":memory:")
    config["payload_store"] = ScopedPayloadBroker(
        PayloadStore(scope_keys=("disaster_event_id",), ttl_seconds=300)
    )
    config["llm"] = None
    return MainNode(inner_config=config), config["payload_store"]


def test_upstream_error_is_passed_through_without_subgraph_build():
    node, _ = _configured_node()
    assert node.execute({"status": "error"}) == {}
    assert node._subgraph is None


def test_subgraph_is_lazy_and_cached():
    node, _ = _configured_node()
    first = node.get_subgraph()
    assert node.get_subgraph() is first


def test_extract_input_returns_inner_envelope_reference():
    node, store = _configured_node()
    clock = "2026-08-20T00:00:00Z"
    state = {
        "request_mode": "invoke",
        "scope": {"disaster_event_id": "event-alpha"},
        "request_clock": clock,
        "caller_id": "caller-alpha",
        "report_refs": [],
        "facility_id_snapshot": [],
        "as_of": clock,
        "decisions": [],
        "record_kind": None,
        "rejected": [],
        "session_id": "session-alpha",
    }
    ref = node.extract_input(state)
    assert len(ref) == 32 and ref.isalpha()
    resolved = store.resolve_envelope(ref, session_id="session-alpha")
    assert resolved["mode"] == "invoke"


def test_execute_contract_and_no_invoke_impl():
    signature = inspect.signature(MainNode.execute)
    assert list(signature.parameters)[:2] == ["self", "state"]
    assert "_invoke_impl" not in MainNode.__dict__


def test_llm_is_threaded_through_constructor():
    sentinel = object()
    node = MainNode(llm=sentinel)
    assert node._llm is sentinel
