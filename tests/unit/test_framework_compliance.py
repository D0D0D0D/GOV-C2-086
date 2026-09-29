"""TC-01 through TC-05 and TC-08 framework compliance."""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import is_typeddict

import pytest

from framework.errors import SecurityViolationError
from framework.schemas.trust_level import TrustLevel

from src.nodes.inner.brief_draft import BriefDraftNode
from src.nodes.inner.dispatch import DispatchNode
from src.nodes.inner.entity_resolve import EntityResolveNode
from src.nodes.inner.feedback_apply import FeedbackApplyNode
from src.nodes.inner.intake_extract import IntakeExtractNode
from src.nodes.inner.reconcile_persist import ReconcilePersistNode
from src.nodes.inner.status_load import StatusLoadNode
from src.nodes.inner.urgency_evaluate import UrgencyEvaluateNode
from src.nodes.main_node import MainNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import DisasterIntakeWorkflowState, State


NODE_CLASSES = (
    PreProcessNode, MainNode, PostProcessNode, DispatchNode, IntakeExtractNode, EntityResolveNode,
    ReconcilePersistNode, StatusLoadNode, UrgencyEvaluateNode, BriefDraftNode, FeedbackApplyNode,
)


def test_tc01_state_is_flat_typed_dict_contract():
    assert is_typeddict(State) and is_typeddict(DisasterIntakeWorkflowState)
    source = pathlib.Path("src/schemas/state.py").read_text()
    assert "BaseModel" not in source and "@dataclass" not in source


def test_tc02_security_violation_error_fires_for_unresolvable_envelope():
    with pytest.raises(SecurityViolationError, match="E_INTERNAL_ENVELOPE"):
        PreProcessNode()._extra_security_gate_input({"user_input": "a" * 32, "session_id": "session"})


def test_tc03_state_has_no_credential_fields():
    forbidden = ("jwt", "token", "api_key", "secret", "password", "credential")
    assert not [name for name in State.__annotations__ if any(term in name.casefold() for term in forbidden)]


def test_tc04_nodes_never_construct_invocation_context_directly():
    violations = []
    for path in pathlib.Path("src/nodes").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InvocationContext":
                violations.append((path, node.lineno))
    assert violations == []


def test_tc05_no_domain_execute_duplicates_framework_lifecycle_events():
    forbidden = {"node_start", "node_complete", "node_error"}
    for cls in NODE_CLASSES:
        source = inspect.getsource(cls.execute)
        assert not any(f'"{event}"' in source for event in forbidden), cls.__name__


def test_nodes_never_generate_their_own_request_time():
    for path in pathlib.Path("src/nodes").rglob("*.py"):
        source = path.read_text()
        assert "datetime.now(" not in source
        assert "date.today(" not in source


@pytest.mark.parametrize("node_cls", NODE_CLASSES)
def test_tc08_all_eleven_nodes_deny_anonymous_before_execute(monkeypatch, node_cls):
    assert node_cls.__dict__["required_trust_level"] is TrustLevel.VERIFIED_EXTERNAL
    called = []

    def spy(self, state):
        called.append(state)
        return {"status": "success"}

    monkeypatch.setattr(node_cls, "execute", spy)
    result = node_cls()({"caller_trust_level": TrustLevel.ANONYMOUS.value})
    assert result["status"] == "error"
    assert called == []
