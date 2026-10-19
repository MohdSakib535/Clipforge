from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_endpoint():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert "database" in body
    assert body["transcriber"] in {"openai", "local"}


def test_dashboard_is_revalidated_not_cached():
    client = TestClient(create_app())
    for path in ("/ui/", "/ui/app.js"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
