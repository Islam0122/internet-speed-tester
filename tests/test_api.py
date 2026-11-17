"""API-level tests for /health and /api/speed-test.

The speed tester's HTTPX client is swapped for a mock transport via
FastAPI's dependency override system, so no real network access is needed.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.routes import speed_test as speed_route
from app.api.routes.speed_test import get_speed_tester
from app.main import app
from app.services.speed_tester import SpeedTester
from tests.helpers import streamed_response


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
        return streamed_response(200, content=b"x" * 1024)

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
        return streamed_response(200, content=b"y" * 100)

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
        return streamed_response(503, content=b"unavailable")

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


def test_speed_test_redirect_is_reported_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return streamed_response(302, headers={"Location": "http://169.254.169.254/"})

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(handler)
    client = TestClient(app)

    response = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 1},
    )

    assert response.status_code == 200
    assert seen == ["https://example.com/file.bin"]
    assert response.json()["results"][0]["error"] == "Redirect not followed: HTTP 302"


def test_speed_test_total_deadline_returns_504(monkeypatch):
    from app.config import settings

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            import asyncio

            await asyncio.sleep(0.5)
            return streamed_response(200, content=b"late", request=request)

    monkeypatch.setattr(settings, "total_timeout_seconds", 0.05)
    app.dependency_overrides[get_speed_tester] = lambda: SpeedTester(
        transport=_SlowTransport(), skip_ssrf_check=True
    )
    client = TestClient(app)

    response = client.post(
        "/api/speed-test",
        json={"url": "https://example.com/file.bin", "requests_count": 2},
    )

    assert response.status_code == 504
    assert "total time limit" in response.json()["detail"]


# --- concurrency limit --------------------------------------------------------


def test_busy_limiter_returns_429_and_does_not_run_the_test(monkeypatch):
    limiter = speed_route.ConcurrencyLimiter(1)
    assert limiter.try_acquire() is True  # another request holds the only slot
    monkeypatch.setattr(speed_route, "_test_limiter", limiter)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return streamed_response(200, content=b"x")

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(handler)
    client = TestClient(app)

    response = client.post(
        "/api/speed-test", json={"url": "https://example.com/file.bin", "requests_count": 1}
    )

    assert response.status_code == 429
    assert "already running" in response.json()["detail"]
    assert calls["n"] == 0  # the refused request never touched the network
    assert limiter.active == 1  # and it did not release the other request's slot


def test_slot_is_released_after_a_successful_run(monkeypatch):
    limiter = speed_route.ConcurrencyLimiter(1)
    monkeypatch.setattr(speed_route, "_test_limiter", limiter)

    app.dependency_overrides[get_speed_tester] = lambda: _mock_tester(
        lambda request: streamed_response(200, content=b"x")
    )
    client = TestClient(app)

    response = client.post(
        "/api/speed-test", json={"url": "https://example.com/file.bin", "requests_count": 1}
    )

    assert response.status_code == 200
    assert limiter.active == 0


def test_slot_is_released_after_a_timeout(monkeypatch):
    from app.config import settings

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.5)
            return streamed_response(200, content=b"late", request=request)

    limiter = speed_route.ConcurrencyLimiter(1)
    monkeypatch.setattr(speed_route, "_test_limiter", limiter)
    monkeypatch.setattr(settings, "total_timeout_seconds", 0.05)
    app.dependency_overrides[get_speed_tester] = lambda: SpeedTester(
        transport=_SlowTransport(), skip_ssrf_check=True
    )
    client = TestClient(app)

    response = client.post(
        "/api/speed-test", json={"url": "https://example.com/file.bin", "requests_count": 1}
    )

    assert response.status_code == 504
    assert limiter.active == 0  # released even though the run failed


def test_limiter_unit_behaviour():
    limiter = speed_route.ConcurrencyLimiter(2)
    assert limiter.try_acquire() and limiter.try_acquire()
    assert limiter.try_acquire() is False  # full: refuse, never queue
    assert limiter.active == 2
    limiter.release()
    assert limiter.try_acquire() is True


@pytest.mark.asyncio
async def test_two_simultaneous_requests_one_is_refused(monkeypatch):
    """Real concurrency through the ASGI app: the second request gets 429 at once."""

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.2)
            return streamed_response(200, content=b"data", request=request)

    limiter = speed_route.ConcurrencyLimiter(1)
    monkeypatch.setattr(speed_route, "_test_limiter", limiter)
    app.dependency_overrides[get_speed_tester] = lambda: SpeedTester(
        transport=_SlowTransport(), skip_ssrf_check=True
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {"url": "https://example.com/file.bin", "requests_count": 2}
        first, second = await asyncio.gather(
            client.post("/api/speed-test", json=body),
            client.post("/api/speed-test", json=body),
        )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 429]
    assert limiter.active == 0


# --- URL validation and error exposure ---------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://exa mple.com/file.bin",  # space inside the host
        "https://example.com/my file.bin",  # space in the path
        "https://user:secret@example.com/file.bin",  # username and password
        "https://user@example.com/file.bin",  # username only
        "https://@example.com/file.bin",  # empty userinfo
    ],
)
def test_url_with_whitespace_or_userinfo_is_rejected(bad_url):
    client = TestClient(app)
    response = client.post("/api/speed-test", json={"url": bad_url, "requests_count": 1})

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "url"]


def test_ssrf_detail_does_not_reveal_resolved_address():
    client = TestClient(app)
    response = client.post(
        "/api/speed-test", json={"url": "http://127.0.0.1/file.bin", "requests_count": 1}
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "127.0.0.1" not in detail
    assert "resolves" not in detail
    assert detail.startswith("URL is not allowed")


def test_unexpected_error_response_and_log_do_not_leak_the_query_token(caplog):
    secret = "tok_SECRET_123"

    class _ExplodingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise RuntimeError("internal failure detail")

    app.dependency_overrides[get_speed_tester] = lambda: SpeedTester(
        transport=_ExplodingTransport(), skip_ssrf_check=True
    )
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level("ERROR"):
        response = client.post(
            "/api/speed-test",
            json={"url": f"https://example.com/file.bin?token={secret}", "requests_count": 1},
        )

    # The per-request error is generic: the exception text stays on the server.
    # (The response's "url" field echoes the URL the client sent, so the client
    # still sees its own token there; only logs are redacted.)
    assert response.status_code == 200
    assert response.json()["results"][0]["error"] == "Unexpected error"
    assert "internal failure detail" not in response.text
    # The server log has the URL without its query string.
    assert secret not in caplog.text
    assert "https://example.com/file.bin" in caplog.text


def test_redact_url_removes_userinfo_query_and_fragment():
    from app.services.speed_tester import redact_url

    assert redact_url("https://example.com/a/b.bin?token=SECRET#frag") == (
        "https://example.com/a/b.bin"
    )
    assert redact_url("http://user:pw@example.com:8080/f") == "http://example.com:8080/f"
