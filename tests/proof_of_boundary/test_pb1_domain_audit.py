"""PB-1: patch each domain module binding, not framework lifecycle events."""

from __future__ import annotations

import pytest

from src.nodes.inner import (
    brief_draft, dispatch, entity_resolve, feedback_apply, intake_extract, reconcile_persist,
    status_load, urgency_evaluate,
)
from src.nodes import main_node, post_process_node, pre_process_node


CASES = (
    (pre_process_node, pre_process_node.PreProcessNode),
    (main_node, main_node.MainNode),
    (post_process_node, post_process_node.PostProcessNode),
    (dispatch, dispatch.DispatchNode),
    (intake_extract, intake_extract.IntakeExtractNode),
    (entity_resolve, entity_resolve.EntityResolveNode),
    (reconcile_persist, reconcile_persist.ReconcilePersistNode),
    (status_load, status_load.StatusLoadNode),
    (urgency_evaluate, urgency_evaluate.UrgencyEvaluateNode),
    (brief_draft, brief_draft.BriefDraftNode),
    (feedback_apply, feedback_apply.FeedbackApplyNode),
)


@pytest.mark.parametrize(("module", "node_cls"), CASES)
def test_pb1_each_node_binding_emits_domain_event_on_failure_branch(monkeypatch, module, node_cls):
    events = []
    monkeypatch.setattr(module, "emit_trace_event", lambda event, payload, state: events.append((event, payload)))
    node_cls().execute({"status": "error", "user_input": "田中太郎 090-1111-2222"})
    assert events, node_cls.__name__
    assert all("田中太郎" not in str(payload) and "090-1111-2222" not in str(payload) for _, payload in events)
