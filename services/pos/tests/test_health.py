from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "pos"}


def test_ready_without_adot_sidecar_is_503(client: TestClient) -> None:
    # No ADOT collector is running in tests, so /ready must report not-ready
    # rather than lying about sidecar health.
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "adot_unhealthy"


def test_version_reports_commit_and_digest(client: TestClient, monkeypatch) -> None:
    resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "pos"
    assert "commit" in body
    assert "image_digest" in body
