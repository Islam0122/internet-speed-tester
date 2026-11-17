"""Application configuration.

All values can be overridden through environment variables (see .env.example).
Kept intentionally small: this is a technical-assignment-scale project, not a
production service, so configuration stays explicit and easy to reason about.
"""

from __future__ import annotations

import os


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """Runtime settings for the speed tester service.

    Attributes:
        default_requests_count: Number of sequential requests performed when
            the client does not specify ``requests_count`` explicitly.
        max_requests_count: Hard upper bound accepted by the API to keep a
            single test from running forever.
        connect_timeout_seconds: Timeout for establishing the TCP/TLS
            connection to the target host.
        read_timeout_seconds: Timeout for reading each chunk of the response
            body. HTTPX resets this timer on each chunk received, so a slow
            but progressing download will not be killed by it.
        total_timeout_seconds: Deadline for one complete test run (all
            requests together). Exceeding it aborts the run with an error.
        max_concurrent_tests: How many speed tests may run at the same time
            through the API. Further requests get HTTP 429 without waiting.
        max_response_bytes: Hard cap on how many bytes will be read from a
            single response. Protects the server from being used to download
            unbounded amounts of data. The download is aborted (and the
            request marked as failed) if this limit is exceeded.
        chunk_size_bytes: Size of each chunk read while streaming the
            response body.
        enable_ssrf_protection: Whether resolved IP addresses are checked
            against private/loopback/link-local/metadata ranges before a
            connection is made. See README "Security" section for the
            documented limitations of this check.
    """

    default_requests_count: int = _get_int("DEFAULT_REQUESTS_COUNT", 10)
    max_requests_count: int = _get_int("MAX_REQUESTS_COUNT", 50)

    connect_timeout_seconds: float = _get_float("CONNECT_TIMEOUT_SECONDS", 5.0)
    read_timeout_seconds: float = _get_float("READ_TIMEOUT_SECONDS", 15.0)
    total_timeout_seconds: float = _get_float("TOTAL_TIMEOUT_SECONDS", 120.0)
    max_concurrent_tests: int = _get_int("MAX_CONCURRENT_TESTS", 1)

    max_response_bytes: int = _get_int("MAX_RESPONSE_BYTES", 200 * 1024 * 1024)  # 200 MiB
    chunk_size_bytes: int = _get_int("CHUNK_SIZE_BYTES", 64 * 1024)  # 64 KiB

    enable_ssrf_protection: bool = _get_bool("ENABLE_SSRF_PROTECTION", True)

    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = _get_int("APP_PORT", 8000)


settings = Settings()
