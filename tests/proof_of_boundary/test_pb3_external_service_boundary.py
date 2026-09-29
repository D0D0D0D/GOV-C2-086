"""PB-3: repository calls cross an injectable port with strict scope/IDs."""

from src.nodes.inner.status_load import StatusLoadNode
from tests.domain_fixtures import CLOCK, SCOPE


class FakeRepository:
    def __init__(self):
        self.calls = []

    def load_registry(self):
        return [{"facility_id": "FAC-A", "name": "中央第一小学校", "aliases": [], "importance": "critical"}]

    def load_status(self, scope, facility_ids):
        self.calls.append((dict(scope), list(facility_ids)))
        return [{
            "facility_id": "FAC-A", "disaster_event_id": SCOPE["disaster_event_id"],
            "damage_observations": [], "conflicts": [], "updated_at": CLOCK,
        }]


def test_pb3_fake_repository_receives_frozen_scope_and_facility_snapshot():
    repository = FakeRepository()
    result = StatusLoadNode(repository=repository).execute(
        {"status": "success", "scope": SCOPE, "facility_id_snapshot": ["FAC-A"], "as_of": CLOCK}
    )
    assert result["status"] == "success"
    assert repository.calls == [(SCOPE, ["FAC-A"])]
