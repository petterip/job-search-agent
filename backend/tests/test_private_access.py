from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import _private_request_needs_auth, app


class _EmptyResult:
    def mappings(self) -> "_EmptyResult":
        return self

    def one_or_none(self) -> None:
        return None

    def one(self) -> dict[str, int]:
        return {"commutable": 0, "commutable_or_full_remote": 0, "nationwide": 0}

    def scalar_one_or_none(self) -> None:
        return None

    def scalar_one(self) -> int:
        return 0

    def __iter__(self) -> Any:
        return iter(())


class _EmptyConnection:
    def __enter__(self) -> "_EmptyConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *_args: Any, **_kwargs: Any) -> _EmptyResult:
        return _EmptyResult()

    def commit(self) -> None:
        return None


class _EmptyEngine:
    def connect(self) -> _EmptyConnection:
        return _EmptyConnection()

    def begin(self) -> _EmptyConnection:
        return _EmptyConnection()


def test_private_request_classification() -> None:
    assert _private_request_needs_auth("GET", "/recommendations") is True
    assert _private_request_needs_auth("GET", "/recommendations/7/feedback") is True
    assert _private_request_needs_auth("GET", "/health") is False
    assert _private_request_needs_auth("GET", "/jobs") is False
    assert _private_request_needs_auth("GET", "/jobs/123") is True
    assert _private_request_needs_auth("POST", "/jobs") is True
    assert _private_request_needs_auth("DELETE", "/anything") is True


@pytest.fixture()
def token_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("OPERATOR_API_TOKEN", "test-operator-token")
    monkeypatch.setattr(main_module, "get_engine", lambda: _EmptyEngine())
    main_module.get_settings.cache_clear()
    yield
    main_module.get_settings.cache_clear()


def test_health_stays_public_when_token_configured(token_env: Any) -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200


def test_private_read_requires_token(token_env: Any) -> None:
    response = TestClient(app).get("/recommendations")

    assert response.status_code == 401
    assert response.json()["detail"] == "unauthorized"


def test_mutation_requires_token(token_env: Any) -> None:
    response = TestClient(app).post("/recommendations/7/feedback", json={"rating": 4})

    assert response.status_code == 401


def test_wrong_token_is_rejected(token_env: Any) -> None:
    response = TestClient(app).get(
        "/recommendations", headers={"Authorization": "Bearer wrong-token"}
    )

    assert response.status_code == 401


def test_valid_token_is_accepted_for_private_read(token_env: Any) -> None:
    response = TestClient(app).get(
        "/recommendations",
        headers={"Authorization": "Bearer test-operator-token"},
    )

    # Auth passed; the empty fake engine yields an empty result set.
    assert response.status_code == 200


def test_x_operator_token_header_is_accepted(token_env: Any) -> None:
    response = TestClient(app).get(
        "/recommendations",
        headers={"X-Operator-Token": "test-operator-token"},
    )

    assert response.status_code == 200


def test_without_configured_token_private_reads_are_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATOR_API_TOKEN", raising=False)
    monkeypatch.setattr(main_module, "get_engine", lambda: _EmptyEngine())
    main_module.get_settings.cache_clear()
    try:
        response = TestClient(app).get("/recommendations")
        assert response.status_code == 200
    finally:
        main_module.get_settings.cache_clear()
