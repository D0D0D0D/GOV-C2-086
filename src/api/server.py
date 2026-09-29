"""Standalone authenticated adapter for the three public request modes."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from framework.errors import SecurityViolationError
from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_config
from shared.secrets import factory as secrets_factory

from src.entry_adapter import auth as entry_auth
from src.entry_adapter.auth import Operation, authenticate
from src.graph.graph import Graph
from src.payload_store.payload_store import PayloadStore
from src.services.config_validation import validate_domain_config
from src.services.envelope_validation import validate_internal_envelope
from src.services.payload_broker import ScopedPayloadBroker
from src.services.repository_factory import create_repository


_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
_config = load_config(str(_CONFIG_PATH)) if _CONFIG_PATH.exists() else {}
_secrets_provider = secrets_factory(namespace="gov", agent_name="FacilityInspectionIntakeAgent")
_anthropic_key = _secrets_provider.get("ANTHROPIC_API_KEY")
_llm = None
if _anthropic_key:
    from shared.services.llm.anthropic_client import AnthropicClient

    _llm = AnthropicClient(config={"api_key": _anthropic_key})
else:
    logging.getLogger(__name__).warning("LLM provider key is not set; deterministic fallback is active.")
_config = dict(_config)
_config["llm"] = _llm
_validated = validate_domain_config(_config)
_repository = create_repository(_validated)
_payload_store = ScopedPayloadBroker(
    PayloadStore(
        scope_keys=("disaster_event_id",),
        ttl_seconds=_validated["payload_ttl_seconds"],
    )
)
_validated["repository"] = _repository
_validated["payload_store"] = _payload_store
agent = Graph(config=_validated)
agent.provision_secrets(_secrets_provider)
agent.compile(checkpointer=None)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.runtime_config = dict(agent.config)
    app.state.repository = agent.config["repository"]
    app.state.payload_store = agent.config["payload_store"]
    yield


app = FastAPI(title="FacilityInspectionIntakeAgent", lifespan=_lifespan)


def _resolve_standalone_trust(current, authorization, invoke_auth_token, internal_runner_token):
    """Backward-compatible unit seam; production requests use entry_adapter."""

    from framework.schemas.trust_level import TrustLevel

    if current is not TrustLevel.ANONYMOUS:
        return current
    supplied = authorization.encode()
    if internal_runner_token and secrets.compare_digest(supplied, f"Bearer {internal_runner_token}".encode()):
        return TrustLevel.INTERNAL
    if invoke_auth_token and secrets.compare_digest(supplied, f"Bearer {invoke_auth_token}".encode()):
        return TrustLevel.VERIFIED_EXTERNAL
    if internal_runner_token or invoke_auth_token:
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.ANONYMOUS


@app.post("/invoke")
async def invoke(request: Request):
    # Identity is resolved before mode parsing. Authorization happens only
    # after the operation has been derived from the validated mode.
    trust, _source, caller_id = entry_auth._resolve_identity(request)
    entry_auth._require_verified_trust(trust)
    try:
        public = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="E_TYPE") from exc
    validated = _validate_public_envelope(public, agent.config)
    operation = {
        "invoke": Operation.READ_INVOKE,
        "ingest": Operation.WRITE_INGEST,
        "feedback": Operation.WRITE_FEEDBACK,
    }[validated["mode"]]
    auth = authenticate(request, operation=operation)
    session_id = getattr(request.state, "session_id", "") or str(uuid4())
    request_clock = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    internal = _build_internal_envelope(
        validated,
        scope=auth.scope,
        caller_id=auth.principal["caller_id"] or caller_id,
        session_id=session_id,
        request_clock=request_clock,
    )
    validate_internal_envelope(internal)
    envelope_ref = _payload_store.put(
        internal, scope=auth.scope, session_id=session_id, envelope=True
    )
    ctx = InvocationContext(
        session_id=session_id,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"] or caller_id,
        hitl_allowed=False,
    )
    try:
        with bound_secrets(agent._secrets_provider):
            return agent.invoke(envelope_ref, ctx=ctx)
    except SecurityViolationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health():
    return {"status": "ok", "agent": "FacilityInspectionIntakeAgent"}


def _validate_public_envelope(value, config: dict) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="E_TYPE")
    mode = value.get("mode")
    if mode is None:
        raise HTTPException(status_code=400, detail="E_MODE_REQUIRED")
    if mode not in {"ingest", "invoke", "feedback"}:
        raise HTTPException(status_code=400, detail="E_MODE_UNKNOWN")
    allowed = {
        "ingest": {"mode", "record_kind", "records"},
        "invoke": {"mode", "as_of", "facility_ids"},
        "feedback": {"mode", "decisions"},
    }[mode]
    if set(value) - allowed:
        raise HTTPException(status_code=400, detail="E_UNKNOWN_KEY")
    result = dict(value)
    if mode == "ingest":
        if "record_kind" not in value:
            raise HTTPException(status_code=400, detail="E_RECORD_KIND_REQUIRED")
        if value["record_kind"] != "damage_report":
            raise HTTPException(status_code=400, detail="E_RECORD_KIND_UNKNOWN")
        records = value.get("records")
        if not isinstance(records, list) or not records:
            raise HTTPException(status_code=400, detail="E_RECORDS_REQUIRED")
        if len(records) > config["max_records_per_request"]:
            raise HTTPException(status_code=400, detail="E_RECORDS_TOO_MANY")
        for record in records:
            _validate_record(record, config)
    elif mode == "invoke":
        as_of = value.get("as_of")
        if as_of is not None and not _timestamp_with_offset(as_of):
            raise HTTPException(status_code=400, detail="E_TIMESTAMP_FORMAT")
        facility_ids = value.get("facility_ids")
        if facility_ids is not None and (
            not isinstance(facility_ids, list)
            or any(not _identifier(item) for item in facility_ids)
            or len(facility_ids) != len(set(facility_ids))
        ):
            raise HTTPException(status_code=400, detail="E_TYPE")
    else:
        decisions = value.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise HTTPException(status_code=400, detail="E_DECISIONS_REQUIRED")
        for decision in decisions:
            _validate_public_decision(decision, config)
    return result


def _validate_record(value, config: dict) -> None:
    if not isinstance(value, dict) or set(value) != {"report_id", "text", "reported_at", "channel"}:
        raise HTTPException(status_code=400, detail="E_UNKNOWN_KEY" if isinstance(value, dict) else "E_TYPE")
    if not _identifier(value["report_id"]):
        raise HTTPException(status_code=400, detail="E_REPORT_ID_REQUIRED")
    if not isinstance(value["text"], str) or not value["text"]:
        raise HTTPException(status_code=400, detail="E_TYPE")
    if len(value["text"]) > config["max_report_chars"]:
        raise HTTPException(status_code=400, detail="E_REPORT_TOO_LONG")
    if not _timestamp_with_offset(value["reported_at"]):
        raise HTTPException(status_code=400, detail="E_TIMESTAMP_FORMAT")
    if value["channel"] not in {"phone_transcript", "field_memo", "written_report", "photo_caption"}:
        raise HTTPException(status_code=400, detail="E_ENUM_UNKNOWN")


def _validate_public_decision(value, config: dict) -> None:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="E_TYPE")
    allowed = {"queue_id", "conflict_id", "action", "facility_id", "keep_observation_ids", "note"}
    if set(value) - allowed:
        raise HTTPException(status_code=400, detail="E_UNKNOWN_KEY")
    action = value.get("action")
    if action not in {"accept_candidate", "reject_all", "assign_facility", "resolve_conflict"}:
        raise HTTPException(status_code=400, detail="E_ENUM_UNKNOWN")
    if bool(value.get("queue_id")) == bool(value.get("conflict_id")):
        raise HTTPException(status_code=400, detail="E_DECISION_TARGET")
    target = value.get("queue_id") or value.get("conflict_id")
    if not _identifier(target):
        raise HTTPException(status_code=400, detail="E_DECISION_TARGET")
    needs_facility = action in {"accept_candidate", "assign_facility"}
    if needs_facility != ("facility_id" in value):
        raise HTTPException(status_code=400, detail="E_DECISION_FIELD")
    if needs_facility and not _identifier(value.get("facility_id")):
        raise HTTPException(status_code=400, detail="E_DECISION_FIELD")
    needs_keep = action == "resolve_conflict"
    if needs_keep != ("keep_observation_ids" in value):
        raise HTTPException(status_code=400, detail="E_DECISION_FIELD")
    if needs_keep and (
        not isinstance(value.get("keep_observation_ids"), list)
        or not value["keep_observation_ids"]
        or any(not _identifier(item) for item in value["keep_observation_ids"])
    ):
        raise HTTPException(status_code=400, detail="E_DECISION_FIELD")
    note = value.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > config["max_note_chars"]):
        raise HTTPException(status_code=400, detail="E_NOTE_TOO_LONG")


def _build_internal_envelope(
    public: dict,
    *,
    scope: dict[str, str],
    caller_id: str,
    session_id: str,
    request_clock: str,
) -> dict:
    mode = public["mode"]
    report_refs = []
    decisions = []
    facilities: list[str] = []
    if mode == "ingest":
        for index, record in enumerate(public["records"]):
            ref = _payload_store.put(dict(record), scope=scope, session_id=session_id)
            report_refs.append({"row_index": index, "report_id": record["report_id"], "payload_ref": ref})
    elif mode == "invoke":
        registry_ids = {item["facility_id"] for item in _repository.load_registry()}
        requested = public.get("facility_ids")
        if requested is None:
            facilities = sorted(item["facility_id"] for item in _repository.load_status(scope))
        else:
            if any(item not in registry_ids for item in requested):
                raise HTTPException(status_code=400, detail="E_FACILITY_UNKNOWN")
            facilities = sorted(requested)
    else:
        for decision in public["decisions"]:
            safe = {key: value for key, value in decision.items() if key != "note"}
            if "note" in decision:
                safe["note_ref"] = _payload_store.put(decision["note"], scope=scope, session_id=session_id)
            decisions.append(safe)
    return {
        "mode": mode,
        "scope": dict(scope),
        "request_clock": request_clock,
        "caller_id": caller_id,
        "report_refs": report_refs,
        "facility_id_snapshot": facilities,
        "as_of": public.get("as_of", request_clock),
        "decisions": decisions,
        "record_kind": public.get("record_kind"),
        "rejected": [],
    }


def _identifier(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and value == value.strip() and "\n" not in value and "\r" not in value


def _timestamp_with_offset(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None
