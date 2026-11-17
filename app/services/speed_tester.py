"""Core business logic: measure download speed with sequential HTTP requests.

Design notes
------------
* Requests are executed strictly one after another (no asyncio.gather, no
  threading) as required by the assignment.
* Timing uses ``time.perf_counter()`` around the *entire* streamed download,
  not just the time to receive headers, so ``duration_seconds`` reflects the
  full transfer.
* ``Accept-Encoding: identity`` is requested and the bytes are counted from
  the raw body (``aiter_raw``), so the count is the number of bytes that
  actually crossed the network, never ``Content-Length`` and never the size
  after decompression.
* Redirects are not followed. A 3xx response is reported as a failed request.
  This keeps the SSRF check meaningful: every request targets exactly the URL
  that was validated.
* A best-effort SSRF guard resolves the host without blocking the event loop
  and rejects non-public IP ranges. It does not protect against DNS rebinding,
  because the name is resolved again when the connection is opened. See the
  README "Security" section.
* ``TOTAL_TIMEOUT_SECONDS`` bounds the whole test, not only each request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from contextlib import aclosing
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit

import httpx

from app.config import settings

BYTES_PER_MIB = 1024 * 1024

logger = logging.getLogger("app.services.speed_tester")


def redact_url(url: str) -> str:
    """Return the URL without userinfo, query string and fragment, for logs.

    Query strings often carry signed tokens, so they are never logged.
    """
    parts = urlsplit(url)
    host_and_port = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{host_and_port}{parts.path}"


# Ranges that must not be treated as public. ``ipaddress.is_private`` does not
# cover all of them (for example the carrier-grade NAT range, which hosts the
# Alibaba Cloud metadata endpoint 100.100.100.200).
_EXTRA_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24", "240.0.0.0/4")
)


class SSRFError(Exception):
    """Raised when a target host resolves to a disallowed IP address."""


class SpeedTestTimeoutError(Exception):
    """Raised when the whole test exceeds ``TOTAL_TIMEOUT_SECONDS``."""


@dataclass
class SingleRequestOutcome:
    """Internal result of performing one HTTP download."""

    success: bool
    status_code: int | None
    duration_seconds: float
    bytes_downloaded: int
    error: str | None


def _is_disallowed_ip(ip_str: str) -> bool:
    """Return True if the given IP address must not be reached from the server."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # Unparsable address: fail closed.

    # ::ffff:a.b.c.d must be judged by its IPv4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    return any(ip in network for network in _EXTRA_BLOCKED_NETWORKS)


async def check_host_is_public(url: str) -> None:
    """Resolve the URL's host and raise SSRFError if it is not publicly routable.

    Name resolution uses the event loop's executor, so a slow DNS server does
    not block other requests. No-op when ``settings.enable_ssrf_protection``
    is False.
    """
    if not settings.enable_ssrf_protection:
        return

    hostname = urlparse(url).hostname
    if not hostname:
        raise SSRFError("URL has no resolvable host.")

    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise SSRFError(f"Could not resolve host '{hostname}': {exc}") from exc

    if not infos:
        raise SSRFError(f"Could not resolve host '{hostname}'.")

    for info in infos:
        ip_str = info[4][0]
        if _is_disallowed_ip(ip_str):
            raise SSRFError(f"Host '{hostname}' resolves to a disallowed address ({ip_str}).")


class MaxResponseSizeExceeded(Exception):
    """Raised internally when a download exceeds the configured byte cap."""


async def _perform_single_download(client: httpx.AsyncClient, url: str) -> SingleRequestOutcome:
    """Perform one full GET download and measure it. Never raises."""
    start = time.perf_counter()
    bytes_downloaded = 0
    status_code: int | None = None

    try:
        async with client.stream("GET", url) as response:
            status_code = response.status_code

            if 300 <= status_code < 400:
                # Redirects are deliberately not followed (see module docstring).
                return SingleRequestOutcome(
                    success=False,
                    status_code=status_code,
                    duration_seconds=time.perf_counter() - start,
                    bytes_downloaded=0,
                    error=f"Redirect not followed: HTTP {status_code}",
                )

            response.raise_for_status()

            # aclosing() releases the body generator even when we stop early
            # (size cap exceeded) instead of leaving it to the garbage collector.
            async with aclosing(response.aiter_raw(settings.chunk_size_bytes)) as chunks:
                async for chunk in chunks:
                    bytes_downloaded += len(chunk)
                    if bytes_downloaded > settings.max_response_bytes:
                        raise MaxResponseSizeExceeded(
                            f"Response exceeded the maximum allowed size of "
                            f"{settings.max_response_bytes} bytes."
                        )

        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=True,
            status_code=status_code,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error=None,
        )

    except MaxResponseSizeExceeded as exc:
        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=False,
            status_code=status_code,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error=str(exc),
        )
    except httpx.HTTPStatusError as exc:
        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=False,
            status_code=exc.response.status_code,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error=f"HTTP error: {exc.response.status_code}",
        )
    except httpx.TimeoutException as exc:
        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=False,
            status_code=None,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error=f"Timeout: {exc}",
        )
    except httpx.RequestError as exc:
        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=False,
            status_code=None,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error=f"Connection error: {exc}",
        )
    except Exception:  # noqa: BLE001 - a single failed request must not crash the run.
        # Internal details stay in the server log. The client gets a generic message.
        logger.exception("Unexpected error while downloading %s", redact_url(url))
        duration = time.perf_counter() - start
        return SingleRequestOutcome(
            success=False,
            status_code=status_code,
            duration_seconds=duration,
            bytes_downloaded=bytes_downloaded,
            error="Unexpected error",
        )


class SpeedTester:
    """Runs a sequential download speed test against a single URL.

    Args:
        transport: Optional custom ``httpx`` transport. Used by tests to
            substitute a mock/local transport instead of hitting the network.
        skip_ssrf_check: Used by tests to bypass the DNS-resolution based
            SSRF guard when exercising it would require real DNS lookups.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        skip_ssrf_check: bool = False,
    ) -> None:
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.read_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )
        self._transport = transport
        self._skip_ssrf_check = skip_ssrf_check

    async def run(self, url: str, requests_count: int) -> SpeedTestSummary:
        """Execute ``requests_count`` sequential GET downloads against ``url``.

        Raises:
            SSRFError: if the host resolves to a disallowed IP address.
            SpeedTestTimeoutError: if the whole test exceeds TOTAL_TIMEOUT_SECONDS.
        """
        outcomes: list[SingleRequestOutcome] = []

        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        try:
            # The DNS check is inside the deadline: a slow resolver must not
            # extend the run beyond TOTAL_TIMEOUT_SECONDS.
            async with asyncio.timeout(settings.total_timeout_seconds):
                if not self._skip_ssrf_check:
                    await check_host_is_public(url)

                async with httpx.AsyncClient(
                    timeout=self._timeout,
                    limits=limits,
                    headers={"Accept-Encoding": "identity"},
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    for _ in range(requests_count):
                        outcome = await _perform_single_download(client, url)
                        outcomes.append(outcome)
        except TimeoutError as exc:
            raise SpeedTestTimeoutError(
                f"Speed test exceeded the total time limit of {settings.total_timeout_seconds:g} s."
            ) from exc

        return summarize(url, outcomes)


@dataclass
class RequestResultData:
    request_number: int
    success: bool
    status_code: int | None
    duration_seconds: float
    bytes_downloaded: int
    size_mib: float
    speed_mib_s: float
    error: str | None


@dataclass
class SpeedTestSummary:
    url: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_bytes: int
    total_size_mib: float
    average_time_seconds: float
    total_time_seconds: float
    average_speed_mib_s: float
    overall_speed_mib_s: float
    results: list[RequestResultData]


def _speed_mib_s(bytes_downloaded: int, duration_seconds: float) -> float:
    """Safely compute MiB/s, guarding against division by zero."""
    if duration_seconds <= 0:
        return 0.0
    return (bytes_downloaded / BYTES_PER_MIB) / duration_seconds


def summarize(url: str, outcomes: list[SingleRequestOutcome]) -> SpeedTestSummary:
    """Turn raw per-request outcomes into the aggregated summary.

    Calculation methodology:
    * average_time_seconds: mean of duration_seconds over SUCCESSFUL requests
      only. Failed requests do not contribute a meaningful download duration.
    * average_speed_mib_s: mean of the speed_mib_s of successful requests that
      downloaded at least one byte. Empty successful bodies are excluded
      because they have no meaningful speed.
    * overall_speed_mib_s: total_size_mib / total_time_seconds - i.e. all
      successful downloads treated as one continuous transfer. This differs
      from average_speed_mib_s when request sizes/durations are uneven, and
      is generally the more representative "real world throughput" figure.
    """
    results: list[RequestResultData] = []
    for idx, outcome in enumerate(outcomes, start=1):
        size_mib = outcome.bytes_downloaded / BYTES_PER_MIB
        speed = (
            _speed_mib_s(outcome.bytes_downloaded, outcome.duration_seconds)
            if outcome.success
            else 0.0
        )
        results.append(
            RequestResultData(
                request_number=idx,
                success=outcome.success,
                status_code=outcome.status_code,
                duration_seconds=outcome.duration_seconds,
                bytes_downloaded=outcome.bytes_downloaded,
                size_mib=size_mib,
                speed_mib_s=speed,
                error=outcome.error,
            )
        )

    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]

    total_bytes = sum(r.bytes_downloaded for r in successful)
    total_time = sum(r.duration_seconds for r in successful)

    average_time = (total_time / len(successful)) if successful else 0.0

    # A successful request with an empty body has no meaningful speed. Counting
    # its 0 MiB/s would pull the average down, so it is left out of this mean.
    with_data = [r for r in successful if r.bytes_downloaded > 0]
    average_speed = (sum(r.speed_mib_s for r in with_data) / len(with_data)) if with_data else 0.0
    overall_speed = _speed_mib_s(total_bytes, total_time)

    return SpeedTestSummary(
        url=url,
        total_requests=len(results),
        successful_requests=len(successful),
        failed_requests=len(failed),
        total_bytes=total_bytes,
        total_size_mib=total_bytes / BYTES_PER_MIB,
        average_time_seconds=average_time,
        total_time_seconds=total_time,
        average_speed_mib_s=average_speed,
        overall_speed_mib_s=overall_speed,
        results=results,
    )
