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
