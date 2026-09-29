"""Network-free runtime fixtures shared by GOV-C2-086 tests."""

from __future__ import annotations

from typing import Any

import yaml

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.payload_store.payload_store import PayloadStore
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository import SQLiteFacilityStatusRepository


CLOCK = "2026-08-20T00:00:00Z"
SCOPE = {"disaster_event_id": "event-alpha"}
SESSION = "session-alpha"
CALLER = "caller-alpha"
REGISTRY = [
    {"facility_id": "FAC-A", "name": "中央第一小学校", "aliases": ["中央第一小"], "importance": "critical"},
    {"facility_id": "FAC-B", "name": "東部第二中学校", "aliases": ["東部第二中"], "importance": "normal"},
]


def build_runtime(path=":memory:", *, llm=None, registry=REGISTRY):
    config = yaml.safe_load(open("config/config.yaml"))
    repository = SQLiteFacilityStatusRepository(str(path), registry_seed=registry)
    payload_store = ScopedPayloadBroker(
        PayloadStore(scope_keys=("disaster_event_id",), ttl_seconds=config["payload_ttl_seconds"])
    )
    config.update(repository=repository, payload_store=payload_store, llm=llm)
    graph = Graph(config=config)
    graph.compile()
    context = invocation_context()
    return graph, repository, payload_store, context


def invocation_context(session_id: str = SESSION) -> InvocationContext:
    return InvocationContext(
        session_id=session_id,
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id=CALLER,
        hitl_allowed=False,
    )


def internal_envelope(mode: str, **overrides: Any) -> dict[str, Any]:
    value = {
        "mode": mode,
        "scope": dict(SCOPE),
        "request_clock": CLOCK,
        "caller_id": CALLER,
        "report_refs": [],
        "facility_id_snapshot": [],
        "as_of": CLOCK,
        "decisions": [],
        "record_kind": None,
        "rejected": [],
    }
    value.update(overrides)
    return value


def invoke_envelope(graph, store, context, envelope: dict[str, Any]):
    ref = store.put(envelope, scope=envelope["scope"], session_id=context.session_id, envelope=True)
    return graph.invoke(ref, ctx=context)


def ingest_one(graph, store, context, *, text="中央第一小学校の体育館で浸水被害を確認しました。", report_id="REP-A"):
    raw = {"report_id": report_id, "text": text, "reported_at": CLOCK, "channel": "field_memo"}
    payload_ref = store.put(raw, scope=SCOPE, session_id=context.session_id)
    envelope = internal_envelope(
        "ingest",
        record_kind="damage_report",
        report_refs=[{"row_index": 0, "report_id": report_id, "payload_ref": payload_ref}],
    )
    return invoke_envelope(graph, store, context, envelope), payload_ref
