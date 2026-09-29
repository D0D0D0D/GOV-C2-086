"""Standalone adapter startup and public-envelope policy integration."""

import importlib

from fastapi.testclient import TestClient


def test_testclient_startup_wires_manifest_config_repository_and_payload_store(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-jwt-for-testing")
    monkeypatch.setenv("STG_DEFAULT_DISASTER_EVENT_ID", "event-startup")
    import src.api.server as server

    importlib.reload(server)
    with TestClient(server.app) as client:
        assert server.app.state.runtime_config["urgency_rules"] == server.agent.config["urgency_rules"]
        assert server.app.state.repository is server.agent.config["repository"]
        response = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-jwt-for-testing"},
            json={"mode": "invoke"},
        )
    assert response.status_code == 200
    assert response.json()["output"]["mode"] == "invoke"
    assert not any(route.path == "/resume" for route in server.app.routes)


def test_public_unknown_scope_is_rejected_and_auth_failure_writes_no_payload(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-jwt-for-testing")
    monkeypatch.setenv("STG_DEFAULT_DISASTER_EVENT_ID", "event-startup")
    import src.api.server as server

    importlib.reload(server)
    before = server._payload_store.envelope_count()
    with TestClient(server.app) as client:
        unknown = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-jwt-for-testing"},
            json={"mode": "invoke", "scope": {"disaster_event_id": "attacker"}},
        )
        denied_write = client.post(
            "/invoke",
            headers={"authorization": "Bearer mock-jwt-for-testing"},
            json={
                "mode": "ingest", "record_kind": "damage_report",
                "records": [{
                    "report_id": "REP-A", "text": "中央第一小学校で浸水を確認",
                    "reported_at": "2026-08-20T00:00:00Z", "channel": "field_memo",
                }],
            },
        )
    assert unknown.status_code == 400 and unknown.json()["detail"] == "E_UNKNOWN_KEY"
    assert denied_write.status_code == 403
    assert server._payload_store.envelope_count() == before
