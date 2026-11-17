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


SEPARATOR = "─" * 29


def format_report(summary: SpeedTestSummary) -> list[str]:
    """Render a SpeedTestSummary as the console report.

    Download speed is the overall throughput (total MiB / total seconds),
    matching the ``overall_speed_mib_s`` field of the API.
    """
    total = summary.total_requests
    has_data = summary.successful_requests > 0

    lines = ["INTERNET SPEED TEST", SEPARATOR]

    for result in summary.results:
        label = f"[{result.request_number}/{total}]"
        if result.success:
            lines.append(
                f"{label:<8}Downloaded {result.size_mib:.2f} MiB in {result.duration_seconds:.3f} s"
            )
        else:
            lines.append(f"{label:<8}FAILED ({result.error})")

    status = "SUCCESS" if summary.failed_requests == 0 else "FAILED"

    lines += [
        "",
        SEPARATOR,
        "RESULTS",
        "",
        f"Successful requests: {summary.successful_requests}/{total}",
        f"Average request time: {_fmt_or_na(summary.average_time_seconds, '.3f', has_data)} s",
        f"Total downloaded: {summary.total_size_mib:.2f} MiB",
        f"Total download time: {summary.total_time_seconds:.2f} s",
        f"Download speed: {_fmt_or_na(summary.overall_speed_mib_s, '.2f', has_data)} MiB/s",
        "",
        f"Status: {status}",
        SEPARATOR,
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
