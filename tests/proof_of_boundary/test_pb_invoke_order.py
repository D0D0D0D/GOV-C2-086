# PB-6: Invoke Execution Order and S-1 Denial Verification
# Verifies BaseNode.__call__() enforces the authorised path: S-1 trust gate ->
# S-4 node_start -> S-2 _security_gate_input() -> execute() -> S-3
# _security_gate_output() -> S-4 node_complete, for every concrete node under
# src/nodes/. It also verifies an under-privileged caller is denied at S-1
# before execute() or normal lifecycle events can run.

import importlib
import inspect
import pkgutil
from copy import deepcopy
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode
from tests.domain_fixtures import (
    CALLER,
    CLOCK,
    SCOPE,
    SESSION,
    build_runtime,
    internal_envelope,
)


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ and collect concrete BaseNode subclasses."""
    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


def _base_state(**overrides) -> dict:
    state = {
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "pb6-correlation",
        "session_id": SESSION,
        "thread_id": "pb6-thread",
        "trace_id": "pb6-trace",
        "caller_id": CALLER,
        "hitl_allowed": False,
        "status": "success",
        "user_input": "",
        "input_context": {},
        "node_history": [],
        "error_log": [],
        "execution_time": {},
    }
    state.update(overrides)
    return state


def _build_node_cases() -> dict[type, tuple[BaseNode, dict]]:
    """Return configured real nodes and the smallest state each accepts."""

    graph, _repository, store, _context = build_runtime()
    main = graph._nodes["main"]
    inner = main.get_subgraph()

    outer_envelope_ref = store.put(
        internal_envelope("invoke"), scope=SCOPE, session_id=SESSION, envelope=True
    )
    inner_envelope_ref = store.put(
        internal_envelope("invoke"), scope=SCOPE, session_id=SESSION, envelope=True
    )
    workflow_state = _base_state(
        request_mode="invoke",
        mode="invoke",
        scope=dict(SCOPE),
        request_clock=CLOCK,
        report_refs=[],
        facility_id_snapshot=[],
        as_of=CLOCK,
        decisions=[],
        record_kind=None,
        rejected=[],
        degradation_reason=[],
        review_queue_delta=[],
        unresolved=[],
    )
    cases = {
        type(graph._nodes["pre_process"]): (
            graph._nodes["pre_process"],
            _base_state(user_input=outer_envelope_ref),
        ),
        type(main): (main, deepcopy(workflow_state)),
        type(graph._nodes["post_process"]): (
            graph._nodes["post_process"],
            deepcopy(workflow_state)
            | {
                "facility_status_snapshot": [],
                "urgency_evaluations": [],
                "briefs": [],
            },
        ),
        type(inner._nodes["dispatch"]): (
            inner._nodes["dispatch"],
            _base_state(user_input=inner_envelope_ref),
        ),
        type(inner._nodes["intake_extract"]): (
            inner._nodes["intake_extract"],
            deepcopy(workflow_state),
        ),
        type(inner._nodes["entity_resolve"]): (
            inner._nodes["entity_resolve"],
            deepcopy(workflow_state) | {"extracted_observations": []},
        ),
        type(inner._nodes["reconcile_persist"]): (
            inner._nodes["reconcile_persist"],
            deepcopy(workflow_state) | {"resolved_observations": []},
        ),
        type(inner._nodes["status_load"]): (
            inner._nodes["status_load"],
            deepcopy(workflow_state),
        ),
        type(inner._nodes["urgency_evaluate"]): (
            inner._nodes["urgency_evaluate"],
            deepcopy(workflow_state) | {"facility_status_snapshot": []},
        ),
        type(inner._nodes["brief_draft"]): (
            inner._nodes["brief_draft"],
            deepcopy(workflow_state) | {"facility_status_snapshot": [], "urgency_evaluations": []},
        ),
        type(inner._nodes["feedback_apply"]): (
            inner._nodes["feedback_apply"],
            deepcopy(workflow_state) | {"mode": "feedback", "request_mode": "feedback"},
        ),
    }
    return cases


def _assert_real_result_contract(node: BaseNode, result: dict) -> None:
    """PB-6 postconditions that make real gate execution observable."""

    assert result.get("status") != "error", f"{type(node).__name__} real invocation failed: {result}"
    assert type(node).__name__ in result.get("node_history", [])
    if isinstance(node, PostProcessNode):
        assert "_guard_context" not in result, "PostProcessNode S-3 hook did not consume _guard_context"


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(
                    _PrivilegedTrustGateFixture.required_trust_level
                ).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        import framework.nodes.base_node as base_node_module

        cases = _build_node_cases()
        assert set(node_classes) == set(cases), "PB-6 fixture must cover every concrete src.nodes class"
        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            instance, state = cases[node_cls]
            with monkeypatch.context() as scoped:
                scoped.setattr(
                    base_node_module,
                    "emit_trace_event",
                    lambda event_type, payload, _state, _o=order, _name=node_cls.__name__: (
                        _o.append(f"event:{event_type}") if payload.get("node") == _name else None
                    ),
                )

                for method_name, label in (
                    ("_security_gate_input", "security_gate_input"),
                    ("execute", "execute"),
                    ("_security_gate_output", "security_gate_output"),
                ):
                    original = getattr(node_cls, method_name)

                    def spy(self, arg, _o=order, _label=label, _orig=original):
                        _o.append(_label)
                        return _orig(self, arg)

                    scoped.setattr(node_cls, method_name, spy)

                result = instance(deepcopy(state))
                try:
                    _assert_real_result_contract(instance, result)
                except AssertionError as exc:
                    failures.append(f"{node_cls.__name__}: {exc}")

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)

    def test_pb6_mutation_noop_s3_extra_hook_is_killed(self, monkeypatch):
        """Mutation proof: removing the real S-3 hook must violate PB-6's postcondition."""

        cases = _build_node_cases()
        node, state = cases[PostProcessNode]
        monkeypatch.setattr(PostProcessNode, "_extra_security_gate_output", lambda self, result: result)
        result = node(deepcopy(state))

        with pytest.raises(AssertionError, match="did not consume _guard_context"):
            _assert_real_result_contract(node, result)
