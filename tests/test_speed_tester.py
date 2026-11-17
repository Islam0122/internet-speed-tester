"""Unit tests for app.services.speed_tester.

Most tests use ``httpx.MockTransport`` to simulate a server without real network
access. Sequencing tests use an async transport that records how many requests
are in flight at the same time, so they can actually fail if requests overlap.
"""

from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from app.config import settings
from app.services.speed_tester import (
    SingleRequestOutcome,
    SpeedTester,
    SpeedTestTimeoutError,
    SSRFError,
    _is_disallowed_ip,
    _speed_mib_s,
    check_host_is_public,
    summarize,
)
from tests.helpers import streamed_response

PAYLOAD_1MIB = b"a" * (1024 * 1024)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return streamed_response(200, content=PAYLOAD_1MIB)


def _empty_handler(request: httpx.Request) -> httpx.Response:
    return streamed_response(200, content=b"")


def _error_handler(request: httpx.Request) -> httpx.Response:
    return streamed_response(500, content=b"server error")


def _timeout_handler(request: httpx.Request):
    raise httpx.ReadTimeout("simulated timeout", request=request)


def _connection_error_handler(request: httpx.Request):
    raise httpx.ConnectError("simulated connection failure", request=request)


class _InFlightTransport(httpx.AsyncBaseTransport):
    """Async transport that tracks how many requests are in progress at once.

    Each request yields to the event loop while "in flight". If two requests
    were issued concurrently, ``max_in_flight`` would be 2 or more.
    """

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return streamed_response(200, content=b"x" * 1000, request=request)


class _SlowTransport(httpx.AsyncBaseTransport):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self.delay)
        return streamed_response(200, content=b"late", request=request)


class _DropMidBody(httpx.AsyncByteStream):
    """Response body that sends some bytes and then the connection is cut."""

    async def __aiter__(self):
        yield b"x" * 1000
        raise httpx.RemoteProtocolError("peer closed connection")


# --- basic download behaviour -------------------------------------------------


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
    transport = _InFlightTransport()
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)

    summary = await tester.run("https://example.com/file.bin", requests_count=10)

    assert summary.total_requests == 10
    assert summary.successful_requests == 10
    assert transport.calls == 10
    assert transport.max_in_flight == 1


@pytest.mark.asyncio
async def test_no_parallel_request_execution():
    """Requests must not overlap, which the in-flight counter proves."""
    transport = _InFlightTransport(delay=0.005)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)

    await tester.run("https://example.com/file.bin", requests_count=5)

    assert transport.calls == 5
    assert transport.max_in_flight == 1


@pytest.mark.asyncio
async def test_in_flight_detector_catches_concurrent_requests():
    """Negative control: the detector must fail when requests really overlap.

    Without this test, the sequencing test above could pass even if the
    detector were broken.
    """
    transport = _InFlightTransport(delay=0.01)
    async with httpx.AsyncClient(transport=transport) as client:
        await asyncio.gather(
            client.get("https://example.com/a"),
            client.get("https://example.com/b"),
        )

    assert transport.max_in_flight == 2


@pytest.mark.asyncio
async def test_byte_counting_ignores_content_length_header():
    """Bytes downloaded must reflect actual body bytes, not Content-Length."""

    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(200, headers={"Content-Length": "999999"}, content=b"short body")

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


# --- identity encoding and gzip ----------------------------------------------


@pytest.mark.asyncio
async def test_request_asks_server_for_identity_encoding():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return streamed_response(200, content=b"plain")

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    await tester.run("https://example.com/file.bin", requests_count=1)

    assert seen == ["identity"]


@pytest.mark.asyncio
async def test_gzip_response_counts_transferred_bytes_not_decompressed_size():
    """A compressed body must be counted by the bytes sent, not the decoded size."""
    raw = b"a" * 1_000_000
    compressed = gzip.compress(raw)
    assert len(compressed) < len(raw)  # sanity: the body really is compressed

    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(200, headers={"Content-Encoding": "gzip"}, content=compressed)

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.results[0].success is True
    assert summary.results[0].bytes_downloaded == len(compressed)
    assert summary.results[0].bytes_downloaded != len(raw)


# --- redirects ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_redirect_is_reported_as_failure_and_not_followed():
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return streamed_response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
        )

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert seen_urls == ["https://example.com/file.bin"]  # target never requested
    result = summary.results[0]
    assert result.success is False
    assert result.status_code == 302
    assert result.error == "Redirect not followed: HTTP 302"
    assert result.bytes_downloaded == 0


@pytest.mark.asyncio
async def test_redirect_to_internal_ip_is_never_requested_with_ssrf_guard_on():
    """Full path with the SSRF guard enabled: the public start URL passes the
    check, the redirect target (metadata IP) must never be contacted."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return streamed_response(302, headers={"Location": "http://169.254.169.254/"})

    tester = SpeedTester(transport=httpx.MockTransport(handler))  # SSRF check ON
    summary = await tester.run("http://8.8.8.8/file.bin", requests_count=1)

    assert seen_urls == ["http://8.8.8.8/file.bin"]
    assert summary.results[0].success is False


@pytest.mark.asyncio
async def test_redirect_responses_count_as_failed_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(301, headers={"Location": "https://example.com/other"})

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=2)

    assert summary.successful_requests == 0
    assert summary.failed_requests == 2


# --- failures and partial transfers ------------------------------------------


@pytest.mark.asyncio
async def test_failed_http_request_marked_unsuccessful():
    transport = httpx.MockTransport(_error_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    assert summary.successful_requests == 0
    assert summary.failed_requests == 1
    assert summary.results[0].success is False
    assert summary.results[0].status_code == 500
    assert summary.results[0].error == "HTTP error: 500"


@pytest.mark.asyncio
async def test_all_requests_failing_gives_zero_averages():
    transport = httpx.MockTransport(_error_handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=3)

    assert summary.successful_requests == 0
    assert summary.failed_requests == 3
    assert summary.average_time_seconds == 0.0
    assert summary.average_speed_mib_s == 0.0
    assert summary.overall_speed_mib_s == 0.0
    assert summary.total_bytes == 0


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
async def test_connection_dropped_mid_body_is_a_failed_request_not_a_partial_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_DropMidBody())

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    result = summary.results[0]
    assert result.success is False
    assert "Connection error" in result.error
    # Partial bytes are not counted in the totals of a failed transfer.
    assert summary.total_bytes == 0
    assert summary.total_time_seconds == 0.0


@pytest.mark.asyncio
async def test_response_larger_than_max_bytes_is_aborted(monkeypatch):
    monkeypatch.setattr(settings, "max_response_bytes", 1000)
    monkeypatch.setattr(settings, "chunk_size_bytes", 100)

    def handler(request: httpx.Request) -> httpx.Response:
        return streamed_response(200, content=b"x" * 5000)

    tester = SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=1)

    result = summary.results[0]
    assert result.success is False
    assert "maximum allowed size of 1000 bytes" in result.error
    assert result.bytes_downloaded > 1000
    assert summary.total_bytes == 0  # an aborted transfer is not counted


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
            return streamed_response(500)
        return streamed_response(200, content=b"ok-data")

    transport = httpx.MockTransport(handler)
    tester = SpeedTester(transport=transport, skip_ssrf_check=True)
    summary = await tester.run("https://example.com/file.bin", requests_count=3)

    assert summary.total_requests == 3
    assert summary.successful_requests == 2
    assert summary.failed_requests == 1


# --- total deadline -----------------------------------------------------------


@pytest.mark.asyncio
async def test_total_deadline_aborts_the_run(monkeypatch):
    monkeypatch.setattr(settings, "total_timeout_seconds", 0.05)

    tester = SpeedTester(transport=_SlowTransport(delay=0.5), skip_ssrf_check=True)

    with pytest.raises(SpeedTestTimeoutError, match="total time limit"):
        await tester.run("https://example.com/file.bin", requests_count=3)


# --- summary math -------------------------------------------------------------


def test_speed_calculation_known_values():
    # 1 MiB downloaded in 2 seconds => 0.5 MiB/s.
    speed = _speed_mib_s(1024 * 1024, 2.0)
    assert speed == pytest.approx(0.5)


def test_zero_duration_protection():
    # Zero (or negative) duration must never raise ZeroDivisionError.
    assert _speed_mib_s(1024, 0.0) == 0.0
    assert _speed_mib_s(1024, -1.0) == 0.0


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


# --- SSRF guard ---------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata (AWS, GCP, Azure)
        "100.100.100.200",  # cloud metadata (Alibaba Cloud, inside 100.64.0.0/10)
        "100.64.0.1",  # carrier-grade NAT
        "100.127.255.254",  # end of 100.64.0.0/10
        "198.18.0.1",  # benchmarking
        "198.19.255.254",  # end of 198.18.0.0/15
        "192.0.0.192",  # IETF protocol assignments
        "240.0.0.1",  # reserved (class E)
        "::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:100.64.0.1",  # IPv4-mapped CGNAT
        "fe80::1",  # IPv6 link-local
        "fd00::1",  # IPv6 unique local
    ],
)
def test_non_public_addresses_are_blocked(ip):
    assert _is_disallowed_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "100.63.255.255", "198.20.0.1"])
def test_public_addresses_are_allowed(ip):
    """Addresses just outside the blocked ranges must still be allowed."""
    assert _is_disallowed_ip(ip) is False


def test_is_disallowed_ip_fails_closed_on_garbage_input():
    assert _is_disallowed_ip("not-an-ip") is True


@pytest.mark.asyncio
async def test_check_host_is_public_rejects_loopback_literal():
    with pytest.raises(SSRFError):
        await check_host_is_public("http://127.0.0.1/file.bin")


@pytest.mark.asyncio
async def test_check_host_is_public_rejects_cgnat_literal():
    with pytest.raises(SSRFError):
        await check_host_is_public("http://100.100.100.200/latest/meta-data/")


@pytest.mark.asyncio
async def test_check_host_is_public_accepts_public_literal():
    await check_host_is_public("http://8.8.8.8/file.bin")  # must not raise


@pytest.mark.asyncio
async def test_check_host_is_public_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_ssrf_protection", False)
    await check_host_is_public("http://127.0.0.1/file.bin")  # must not raise


@pytest.mark.asyncio
async def test_run_refuses_private_target_before_any_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return streamed_response(200, content=b"secret")

    tester = SpeedTester(transport=httpx.MockTransport(handler))  # SSRF check ON
    with pytest.raises(SSRFError):
        await tester.run("http://10.0.0.5/admin", requests_count=1)

    assert calls["n"] == 0
