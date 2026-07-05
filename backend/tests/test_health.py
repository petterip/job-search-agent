from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_service_status() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "job-search-agent"
    assert body["embedding_dimension"] == 1536
    assert body["eval_model"]
    assert "transit_distance_enabled" in body
