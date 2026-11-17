"""Command-line interface for the sequential download speed test.

The CLI is a thin layer over the existing service: downloads, timing and
statistics all come from ``SpeedTester`` in ``app.services.speed_tester``, and
URL / request-count validation reuses ``SpeedTestRequest`` from
``app.schemas.speed_test``. The same code path therefore serves both this CLI
and the ``POST /api/speed-test`` endpoint.

Usage::

    python -m app.cli https://example.com/large-image.jpg
    python -m app.cli https://example.com/large-image.jpg -n 10

Exit codes:
    0  every request succeeded
    1  at least one request failed, or the test could not run (SSRF block, unexpected error)
    2  invalid arguments (malformed URL, requests count out of range)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence

from pydantic import ValidationError

from app.config import settings
from app.schemas.speed_test import SpeedTestRequest
from app.services.speed_tester import SpeedTester, SpeedTestSummary, SSRFError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

TesterFactory = Callable[[], SpeedTester]


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser. Kept separate so it can be tested directly."""
    parser = argparse.ArgumentParser(
        prog="speed-test",
        description=(
            "Measure download speed by performing sequential HTTP GET requests "
            "against a URL. Speeds are reported in MiB/s (1 MiB = 1024 * 1024 bytes)."
        ),
    )
    parser.add_argument("url", help="HTTP or HTTPS URL of the file to download.")
    parser.add_argument(
        "-n",
        "--requests",
        dest="requests_count",
        type=int,
        default=settings.default_requests_count,
        help=(
            "number of sequential requests to perform "
            f"(default: {settings.default_requests_count}, max: {settings.max_requests_count})"
        ),
    )
    return parser


def _fmt_or_na(value: float, fmt: str, has_data: bool) -> str:
    return format(value, fmt) if has_data else "n/a"


def format_report(summary: SpeedTestSummary) -> list[str]:
    """Render a SpeedTestSummary as human-readable console lines."""
    has_data = summary.successful_requests > 0

    lines = [f"URL: {summary.url}", ""]

    for result in summary.results:
        prefix = f"Request {result.request_number:>2}:"
        if result.success:
            lines.append(
                f"{prefix} {result.duration_seconds:.3f} s, "
                f"{result.size_mib:.2f} MiB, {result.speed_mib_s:.2f} MiB/s"
            )
        else:
            lines.append(f"{prefix} FAILED ({result.error})")

    lines += [
        "",
        f"Requests: {summary.total_requests} total, "
        f"{summary.successful_requests} successful, {summary.failed_requests} failed",
        f"Average request time: {_fmt_or_na(summary.average_time_seconds, '.3f', has_data)} s",
        f"Total downloaded: {summary.total_size_mib:.2f} MiB ({summary.total_bytes} bytes)",
        f"Total download time: {summary.total_time_seconds:.3f} s",
        f"Average speed: {_fmt_or_na(summary.average_speed_mib_s, '.2f', has_data)} MiB/s",
        f"Overall speed: {_fmt_or_na(summary.overall_speed_mib_s, '.2f', has_data)} MiB/s",
    ]
    return lines


def main(
    argv: Sequence[str] | None = None,
    *,
    tester_factory: TesterFactory | None = None,
) -> int:
    """Run the CLI and return the process exit code.

    Args:
        argv: Arguments without the program name. Defaults to ``sys.argv[1:]``.
        tester_factory: Builds the ``SpeedTester`` to use. Tests inject one with a
            mock transport; the default uses the real network and SSRF guard.
    """
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits on --help (0) and on usage errors (2)
        return int(exc.code or EXIT_OK)

    try:
        request = SpeedTestRequest(url=args.url, requests_count=args.requests_count)
    except ValidationError as exc:
        for error in exc.errors():
            message = str(error["msg"]).removeprefix("Value error, ")
            print(f"Error: {message}", file=sys.stderr)
        return EXIT_USAGE

    tester = tester_factory() if tester_factory is not None else SpeedTester()

    try:
        summary = asyncio.run(tester.run(request.url, request.requests_count))
    except SSRFError as exc:
        print(f"Error: URL is not allowed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:
        print(f"Error: speed test could not complete: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    for line in format_report(summary):
        print(line)

    return EXIT_FAILURE if summary.failed_requests > 0 else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
