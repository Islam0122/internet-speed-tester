"""API-level tests for /health and /api/speed-test.

The speed tester's HTTPX client is swapped for a mock transport via
FastAPI's dependency override system, so no real network access is needed.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes.speed_test import get_speed_tester
from app.main import app
from app.services.speed_tester import SpeedTester


def _mock_tester(handler) -> SpeedTester:
    transport = httpx.MockTransport(handler)
    return SpeedTester(transport=transport, skip_ssrf_check=True)


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.clear()


def test_health_endpoint():
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_speed_test_success_response_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(handler)
    client = TestClient(app)

    response = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 3},
    )

    assert response.status_code == 200
    data = response.json()

    for key in [
        "url",
        "total_requests",
        "successful_requests",
        "failed_requests",
        "total_bytes",
        "total_size_mib",
        "average_time_seconds",
        "total_time_seconds",
        "average_speed_mib_s",
        "overall_speed_mib_s",
        "results",
    ]:
        assert key in data

    assert data["total_requests"] == 3
    assert data["successful_requests"] == 3
    assert data["failed_requests"] == 0
    assert len(data["results"]) == 3

    first_result = data["results"][0]
    for key in [
        "request_number",
        "success",
        "status_code",
        "duration_seconds",
        "bytes_downloaded",
        "size_mib",
        "speed_mib_s",
        "error",
    ]:
        assert key in first_result


def test_speed_test_default_requests_count_is_ten():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"y" * 100)

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(handler)
    client = TestClient(app)

    response = client.post("/api/speed-test", json={"url": "https://example.com/file.bin"})

    assert response.status_code == 200
    assert response.json()["total_requests"] == 10
    assert calls["n"] == 10


def test_speed_test_invalid_url_rejected():
    client = TestClient(app)
    response = client.post("/api/speed-test", json={"url": "not-a-url", "requests_count": 1})
    assert response.status_code == 422


def test_speed_test_disallowed_scheme_rejected():
    client = TestClient(app)
    response = client.post(
        "/api/speed-test", json={"url": "ftp://example.com/file.bin", "requests_count": 1}
    )
    assert response.status_code == 422


def test_speed_test_requests_count_out_of_range_rejected():
    client = TestClient(app)

    too_many = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 999},
    )
    assert too_many.status_code == 422

    too_few = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 0},
    )
    assert too_few.status_code == 422


def test_speed_test_missing_url_rejected():
    client = TestClient(app)
    response = client.post("/api/speed-test", json={"requests_count": 5})
    assert response.status_code == 422


def test_speed_test_handles_failed_requests_without_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"unavailable")

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(handler)
    client = TestClient(app)

    response = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 2},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["successful_requests"] == 0
    assert data["failed_requests"] == 2
    assert all(r["success"] is False for r in data["results"])


def test_index_page_served():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "Internet Speed Tester" in response.text
