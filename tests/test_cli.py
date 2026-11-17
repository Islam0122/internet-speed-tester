"""Tests for the command-line interface in app.cli.

All network traffic is replaced by ``httpx.MockTransport`` through the
``tester_factory`` hook, so these tests are deterministic and offline.
The one exception is the SSRF test, which uses an IP literal and therefore
needs no DNS lookup.
"""

from __future__ import annotations

import httpx
import pytest

from app.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, build_parser, main
from app.services.speed_tester import SpeedTester

URL = "https://example.com/large-file.bin"
ONE_MIB = b"a" * (1024 * 1024)


def _factory(handler):
    """Build a tester_factory that uses a mock transport and skips DNS-based SSRF checks."""

    def make() -> SpeedTester:
        return SpeedTester(transport=httpx.MockTransport(handler), skip_ssrf_check=True)

    return make


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=ONE_MIB)


def test_default_requests_count_is_ten():
    args = build_parser().parse_args([URL])
    assert args.requests_count == 10


def test_runs_exactly_ten_sequential_requests_by_default(capsys):
    calls = {"n": 0, "concurrent": 0, "max_concurrent": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["concurrent"] += 1
        calls["max_concurrent"] = max(calls["max_concurrent"], calls["concurrent"])
        calls["concurrent"] -= 1
        return httpx.Response(200, content=ONE_MIB)

    exit_code = main([URL], tester_factory=_factory(handler))

    assert exit_code == EXIT_OK
    assert calls["n"] == 10
    assert calls["max_concurrent"] == 1
    out = capsys.readouterr().out
    assert "Request  1:" in out
    assert "Request 10:" in out
    assert "Requests: 10 total, 10 successful, 0 failed" in out


def test_custom_request_count_with_short_flag(capsys):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x" * 100)

    exit_code = main([URL, "-n", "3"], tester_factory=_factory(handler))

    assert exit_code == EXIT_OK
    assert calls["n"] == 3
    assert "Requests: 3 total" in capsys.readouterr().out


def test_custom_request_count_with_long_flag(capsys):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x" * 100)

    exit_code = main([URL, "--requests", "5"], tester_factory=_factory(handler))

    assert exit_code == EXIT_OK
    assert calls["n"] == 5


def test_output_contains_per_request_times_average_total_and_mib_speed(capsys):
    exit_code = main([URL], tester_factory=_factory(_ok_handler))
    out = capsys.readouterr().out

    assert exit_code == EXIT_OK
    # Per-request line: time in seconds, size in MiB, speed in MiB/s.
    assert "Request  1:" in out and " s, 1.00 MiB, " in out and " MiB/s" in out
    # Summary lines.
    assert "Average request time:" in out
    assert "Total downloaded: 10.00 MiB" in out  # 10 requests x 1 MiB
    assert "Average speed:" in out
    assert "MiB/s" in out


def test_output_reports_units_as_mib_per_second(capsys):
    main([URL, "-n", "1"], tester_factory=_factory(_ok_handler))
    out = capsys.readouterr().out
    assert "Average speed:" in out
    assert "MiB/s" in out
    assert "MB/s" not in out


def test_all_requests_failing_returns_nonzero_and_shows_na(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    exit_code = main([URL, "-n", "2"], tester_factory=_factory(handler))
    out = capsys.readouterr().out

    assert exit_code == EXIT_FAILURE
    assert "FAILED (HTTP error: 500)" in out
    assert "Requests: 2 total, 0 successful, 2 failed" in out
    assert "Average request time: n/a s" in out
    assert "Average speed: n/a MiB/s" in out


def test_partial_failure_returns_nonzero(capsys):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            return httpx.Response(500)
        return httpx.Response(200, content=ONE_MIB)

    exit_code = main([URL, "-n", "3"], tester_factory=_factory(handler))
    out = capsys.readouterr().out

    assert exit_code == EXIT_FAILURE
    assert calls["n"] == 3  # a single failure must not stop the run
    assert "Requests: 3 total, 2 successful, 1 failed" in out


def test_connection_error_returns_nonzero(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated connection failure", request=request)

    exit_code = main([URL, "-n", "1"], tester_factory=_factory(handler))

    assert exit_code == EXIT_FAILURE
    assert "Connection error" in capsys.readouterr().out


def test_timeout_returns_nonzero(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    exit_code = main([URL, "-n", "1"], tester_factory=_factory(handler))

    assert exit_code == EXIT_FAILURE
    assert "Timeout" in capsys.readouterr().out


def test_unexpected_exception_from_tester_returns_nonzero(capsys):
    class _BrokenTester:
        async def run(self, url, requests_count):
            raise RuntimeError("disk on fire")

    exit_code = main([URL], tester_factory=lambda: _BrokenTester())  # type: ignore[arg-type,return-value]

    assert exit_code == EXIT_FAILURE
    assert "speed test could not complete: disk on fire" in capsys.readouterr().err


def test_ssrf_blocked_target_returns_nonzero_without_network(capsys):
    # 127.0.0.1 is a loopback IP literal: the SSRF guard rejects it without DNS.
    exit_code = main(["http://127.0.0.1/file.bin", "-n", "1"])

    assert exit_code == EXIT_FAILURE
    assert "URL is not allowed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad_url",
    ["not-a-url", "ftp://example.com/file.bin", "http://"],
)
def test_invalid_url_returns_usage_error(bad_url, capsys):
    exit_code = main([bad_url, "-n", "1"], tester_factory=_factory(_ok_handler))

    assert exit_code == EXIT_USAGE
    assert "Error:" in capsys.readouterr().err


@pytest.mark.parametrize("count", ["0", "-1", "51"])
def test_requests_count_out_of_range_returns_usage_error(count, capsys):
    exit_code = main([URL, "-n", count], tester_factory=_factory(_ok_handler))

    assert exit_code == EXIT_USAGE
    assert "Error:" in capsys.readouterr().err


def test_non_integer_requests_count_returns_usage_error(capsys):
    exit_code = main([URL, "-n", "ten"], tester_factory=_factory(_ok_handler))

    assert exit_code == EXIT_USAGE


def test_missing_url_returns_usage_error(capsys):
    exit_code = main([])

    assert exit_code == EXIT_USAGE
    assert "url" in capsys.readouterr().err.lower()


def test_help_exits_zero(capsys):
    exit_code = main(["--help"])

    assert exit_code == EXIT_OK
    assert "requests" in capsys.readouterr().out
