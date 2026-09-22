"""Unit tests for app.services.speed_tester.

Uses httpx.MockTransport to simulate a server without any real network
access, so tests are deterministic and offline-safe.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.speed_tester import (
    SingleRequestOutcome,
    SpeedTester,
    _is_disallowed_ip,
    _speed_mib_s,
    summarize,
)

PAYLOAD_1MIB = b"a" * (1024 * 1024)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=PAYLOAD_1MIB)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=b"")


def _error_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, content=b"server error")


def _timeout_handler(request: httpx.Request):
    raise httpx.ReadTimeout("simulated timeout", request=request)


def _connection_error_handler(request: httpx.Request):
    raise httpx.ConnectError("simulated connection failure", request=request)


class _CallCounter:
    """Helper to assert requests happen one at a time (no concurrency)."""

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_concurrent = 0
        self.total_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.total_calls += 1
        # No await/sleep here on purpose: MockTransport handlers are sync,
        # which is fine for asserting call ordering in this test.
        self.concurrent -= 1
        return httpx.Response(200, content=PAYLOAD_1MIB)


@pytest.mark.asyncio
async def test_single_successful_download():
    transport = httpx.MockTransport(_ok_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)

    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.total_requests == 1
    assert summary.successful_requests == 1
    assert summary.failed_requests == 0
    assert summary.results[0].bytes_downloaded == len(PAYLOAD_1MIB)
    assert summary.results[0].success is True
    assert summary.results[0].duration_seconds >= 0


@pytest.mark.asyncio
async def test_ten_sequential_requests():
    counter = _CallCounter()
    transport = httpx.MockTransport(counter.handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)

    summary = await tester.run("https://example.com/file.bin", requests_count=10)

    assert summary.total_requests == 10
    assert summary.successful_requests == 10
    assert counter.total_calls == 10
    # Because the handler decrements before returning, max_concurrent being 1
    # confirms requests were not overlapped/parallelized.
    assert counter.max_concurrent == 1


@pytest.mark.asyncio
async def test_no_parallel_request_execution():
    """Explicitly assert sequential (not concurrent) execution."""
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append("start")
        order.append("end")
        return httpx.Response(200, content=b"x" * 100)

    transport = httpx.MockTransport(handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    await tester.run("https://example.com/file.bin", requests_count=5)

    # Each request must fully start and end before the next begins.
    assert order == ["start", "end"] * 5


@pytest.mark.asyncio
async def test_byte_counting_ignores_content_length_header():
    """Bytes downloaded must reflect actual body bytes, not Content-Length."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Deliberately wrong Content-Length header.
        return httpx.Response(200, headers={"Content-Length": "999999"}, content=b"short body")

    transport = httpx.MockTransport(handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.results[0].bytes_downloaded == len(b"short body")


@pytest.mark.asyncio
async def test_average_request_time_calculation():
    transport = httpx.MockTransport(_ok_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=3)

    expected_avg = sum(r.duration_seconds for r in summary.results) / 3
    assert summary.average_time_seconds == pytest.approx(expected_avg)


def test_speed_calculation_known_values():
    # 1 MiB downloaded in 2 seconds => 0.5 MiB/s.
    speed = _speed_mib_s(1024 * 1024, 2.0)
    assert speed == pytest.approx(0.5)


def test_zero_duration_protection():
    # Zero (or negative) duration must never raise ZeroDivisionError.
    assert _speed_mib_s(1024, 0.0) == 0.0
    assert _speed_mib_s(1024, -1.0) == 0.0


@pytest.mark.asyncio
async def test_failed_http_request_marked_unsuccessful():
    transport = httpx.MockTransport(_error_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.successful_requests == 0
    assert summary.failed_requests == 1
    assert summary.results[0].success is False
    assert summary.results[0].status_code == 500
    assert summary.results[0].error is not None


@pytest.mark.asyncio
async def test_connection_timeout_handled_gracefully():
    transport = httpx.MockTransport(_timeout_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.results[0].success is False
    assert "Timeout" in summary.results[0].error


@pytest.mark.asyncio
async def test_connection_error_handled_gracefully():
    transport = httpx.MockTransport(_connection_error_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.results[0].success is False
    assert "Connection error" in summary.results[0].error


@pytest.mark.asyncio
async def test_empty_response_body_counts_zero_bytes_but_succeeds():
    transport = httpx.MockTransport(_empty_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.results[0].success is True
    assert summary.results[0].bytes_downloaded == 0
    assert summary.results[0].speed_mib_s == 0.0


@pytest.mark.asyncio
async def test_a_single_failure_does_not_crash_the_whole_run():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok-data")

    transport = httpx.MockTransport(handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=3)

    assert summary.total_requests == 3
    assert summary.successful_requests == 2
    assert summary.failed_requests == 1


def test_summarize_excludes_failed_requests_from_averages():
    outcomes = [
        SingleRequestOutcome(True, 200, 1.0, 1024 * 1024, None),  # 1 MiB in 1s => 1 MiB/s
        SingleRequestOutcome(False, 500, 0.5, 0, "server error"),
        SingleRequestOutcome(True, 200, 2.0, 2 * 1024 * 1024, None),  # 2 MiB in 2s => 1 MiB/s
    ]
    summary = summarize("https://example.com/file.bin", outcomes)

    assert summary.successful_requests == 2
    assert summary.failed_requests == 1
    # average_time only over the two successful requests: (1.0 + 2.0) / 2
    assert summary.average_time_seconds == pytest.approx(1.5)
    # average_speed: mean of [1.0, 1.0] MiB/s
    assert summary.average_speed_mib_s == pytest.approx(1.0)
    # overall_speed: total 3 MiB over total 3s = 1.0 MiB/s
    assert summary.overall_speed_mib_s == pytest.approx(1.0)


def test_is_disallowed_ip_blocks_private_and_loopback():
    assert _is_disallowed_ip("127.0.0.1") is True
    assert _is_disallowed_ip("10.0.0.5") is True
    assert _is_disallowed_ip("192.168.1.1") is True
    assert _is_disallowed_ip("169.254.169.254") is True  # cloud metadata
    assert _is_disallowed_ip("::1") is True


def test_is_disallowed_ip_allows_public_address():
    assert _is_disallowed_ip("8.8.8.8") is False


def test_is_disallowed_ip_fails_closed_on_garbage_input():
    assert _is_disallowed_ip("not-an-ip") is True
