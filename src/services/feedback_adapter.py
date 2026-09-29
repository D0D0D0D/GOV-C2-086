"""Non-persisting ledger-shaped ownership adapter for feedback-intake v0.2.0."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from src.services.domain_utils import stable_id


class RepositoryFeedbackLedgerAdapter:
    """Expose repository targets to the part without adopting supersede writes.

    ``write`` only returns a staged receipt.  The owning FeedbackApplyNode
    commits all accepted decisions together through repository.apply_feedback,
    retaining the domain's atomic and both-observations-preserved contract.
    """

    def __init__(self, repository) -> None:
        self._repository = repository

    def get(
        self, record_id: str, scope: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any] | None:
        for item in self._repository.load_review_queue(scope, [record_id]):
            return {**copy.deepcopy(item), "record_id": record_id, "scope": dict(scope)}
        for status in self._repository.load_status(scope):
            for conflict in status.get("conflicts", []):
                if conflict["conflict_id"] == record_id:
                    return {**copy.deepcopy(conflict), "record_id": record_id, "scope": dict(scope)}
        return None

    def write(
        self, scope: Mapping[str, Any], record: Mapping[str, Any], state: dict | None = None
    ) -> dict[str, Any]:
        staged = copy.deepcopy(dict(record))
        staged["record_id"] = stable_id(
            tuple(sorted(scope.items())), staged.get("supersedes_record_id", ""),
            staged.get("decided_at", ""), staged.get("verdict_code", ""),
        )
        staged["scope"] = dict(scope)
        return staged
