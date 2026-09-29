"""PB-2: end-to-end output/state projection remains JSON-safe and raw-free."""

import json

from tests.domain_fixtures import SCOPE, build_runtime, ingest_one


def test_pb2_raw_report_never_appears_in_graph_output_or_repository_record():
    graph, repository, store, context = build_runtime()
    sentinel = "RAW-REPORT-SENTINEL 氏名: 山田太郎 090-1111-2222"
    output, _ = ingest_one(
        graph,
        store,
        context,
        text=f"中央第一小学校で浸水を確認。{sentinel}",
    )
    persisted = repository.load_status(SCOPE)
    serialised = json.dumps({"output": output, "persisted": persisted}, ensure_ascii=False)
    assert sentinel not in serialised
    assert "090-1111-2222" not in serialised
    json.dumps(output)
    json.dumps(persisted)
