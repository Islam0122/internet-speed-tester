"""API routes for /health and /api/speed-test."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.config import settings
from app.schemas.speed_test import RequestResult, SpeedTestRequest, SpeedTestResponse
from app.services.speed_tester import SpeedTester, SpeedTestTimeoutError, SSRFError

logger = logging.getLogger("app.api.speed_test")

router = APIRouter()


class ConcurrencyLimiter:
    """Admits at most ``limit`` speed tests at once and refuses the rest.

    Requests are not queued. A queue would grow without bound under load, and
    each test can take up to TOTAL_TIMEOUT_SECONDS, so waiting clients would
    pile up. Refused requests get HTTP 429 immediately.

    No lock is needed: asyncio runs on one thread, and ``try_acquire`` does not
    await, so two coroutines cannot both take the last slot.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active -= 1


# One process-wide limiter. The Docker image runs a single uvicorn worker, so
# the limit applies to the whole service.
_test_limiter = ConcurrencyLimiter(settings.max_concurrent_tests)


def get_speed_tester() -> SpeedTester:
    """FastAPI dependency provider for SpeedTester.

    Overridden in tests to inject a mock HTTPX transport instead of hitting
    the real network.
    """
    return SpeedTester()


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Simple liveness check."""
    return {"status": "ok"}


@router.post(
    "/api/speed-test",
    response_model=SpeedTestResponse,
    tags=["speed-test"],
    summary="Run a sequential download speed test against a URL",
)
async def run_speed_test(
    payload: SpeedTestRequest,
    tester: SpeedTester = Depends(get_speed_tester),
) -> SpeedTestResponse:
    """Execute ``payload.requests_count`` sequential downloads of ``payload.url``.

    Requests run one after another (not in parallel). The response contains
    both the aggregated statistics and each individual request's result.
    """
    if not _test_limiter.try_acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Another speed test is already running. Try again later.",
        )
    try:
        summary = await tester.run(payload.url, payload.requests_count)
    except SSRFError as exc:
        # Do not leak internal resolution details beyond what's useful to the client.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"URL is not allowed: {exc}",
        ) from exc
    except SpeedTestTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # noqa: BLE001 - guard against any unexpected failure.
        logger.exception("Unexpected error while running speed test for %s", payload.url)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while running the speed test.",
        ) from exc
    finally:
        # Release on every path: success, error, timeout and cancellation.
        _test_limiter.release()

    return SpeedTestResponse(
        url=summary.url,
        total_requests=summary.total_requests,
        successful_requests=summary.successful_requests,
        failed_requests=summary.failed_requests,
        total_bytes=summary.total_bytes,
        total_size_mib=round(summary.total_size_mib, 4),
        average_time_seconds=round(summary.average_time_seconds, 4),
        total_time_seconds=round(summary.total_time_seconds, 4),
        average_speed_mib_s=round(summary.average_speed_mib_s, 4),
        overall_speed_mib_s=round(summary.overall_speed_mib_s, 4),
        results=[
            RequestResult(
                request_number=r.request_number,
                success=r.success,
                status_code=r.status_code,
                duration_seconds=round(r.duration_seconds, 4),
                bytes_downloaded=r.bytes_downloaded,
                size_mib=round(r.size_mib, 4),
                speed_mib_s=round(r.speed_mib_s, 4),
                error=r.error,
            )
            for r in summary.results
        ],
    )
