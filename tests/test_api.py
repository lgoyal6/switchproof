from fastapi.testclient import TestClient

from switchproof.api import app


def test_local_api_errors_and_origin_gate():
    with TestClient(app) as client:
        assert client.get("/api/runs/not-a-run").status_code == 404
        assert client.post("/api/runs", json={"provider": "shell"}).status_code == 422
        assert (
            client.post("/api/runs", json={}, headers={"origin": "https://other.example"}).status_code == 403
        )
        assert client.get("/api/runs", headers={"host": "untrusted.example"}).status_code == 400


def test_health_readiness_status(monkeypatch):
    from types import SimpleNamespace

    from switchproof import api

    monkeypatch.setattr(api, "neo_query", lambda _: [])
    monkeypatch.setattr(api, "falkor", lambda: SimpleNamespace(connection=SimpleNamespace(ping=lambda: True)))
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

        def unavailable(_):
            raise ConnectionError("database unavailable")

        monkeypatch.setattr(api, "neo_query", unavailable)
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["databases"] == {"neo4j": False, "falkordb": True}


def test_public_run_budget_rejects_without_leaking_lock(monkeypatch, tmp_path):
    import json
    from datetime import UTC, datetime

    from switchproof import api

    monkeypatch.setattr(api, "RUNS", tmp_path)
    monkeypatch.setattr(api, "PUBLIC_RUN_LIMIT", 1)
    (tmp_path / "existing.json").write_text(json.dumps({"created_at": datetime.now(UTC).isoformat()}))
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post("/api/runs", json={})
            assert response.status_code == 429
            assert not api.busy.locked()
