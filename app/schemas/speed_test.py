"""Pydantic schemas for the /api/speed-test endpoint."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.config import settings

ALLOWED_SCHEMES = {"http", "https"}


class SpeedTestRequest(BaseModel):
    """Incoming request body for POST /api/speed-test."""

    url: str = Field(
        ...,
        min_length=1,
        description="HTTP or HTTPS URL of a downloadable resource.",
        examples=["https://example.com/sample-file.jpg"],
    )
    requests_count: int = Field(
        default=settings.default_requests_count,
        ge=1,
        le=settings.max_requests_count,
        description="Number of sequential requests to perform.",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)

        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError("URL scheme must be 'http' or 'https'.")

        if not parsed.netloc:
            raise ValueError("URL must include a valid host.")

        return value


class RequestResult(BaseModel):
    """Result of a single HTTP request performed during the test."""

    request_number: int = Field(..., description="1-indexed position of this request.")
    success: bool = Field(..., description="Whether the request completed successfully.")
    status_code: int | None = Field(
        default=None, description="HTTP status code, if a response was received."
    )
    duration_seconds: float = Field(
        ..., ge=0.0, description="Full time to download the response body, in seconds."
    )
    bytes_downloaded: int = Field(
        ..., ge=0, description="Number of bytes actually read from the response body."
    )
    size_mib: float = Field(..., ge=0.0, description="bytes_downloaded expressed in MiB.")
    speed_mib_s: float = Field(
        ...,
        ge=0.0,
        description="Per-request speed in MiB/s. 0.0 if duration was 0 or the request failed.",
    )
    error: str | None = Field(
        default=None, description="Human-readable error message if success is false."
    )


class SpeedTestResponse(BaseModel):
    """Aggregated result of a full speed test run."""

    url: str
    total_requests: int = Field(..., description="Number of requests that were attempted.")
    successful_requests: int
    failed_requests: int

    total_bytes: int = Field(..., description="Sum of bytes_downloaded across successful requests.")
    total_size_mib: float = Field(..., description="total_bytes expressed in MiB.")

    average_time_seconds: float = Field(
        ...,
        description="Mean duration_seconds across successful requests only. 0.0 if none succeeded.",
    )
    total_time_seconds: float = Field(
        ..., description="Sum of duration_seconds across successful requests."
    )

    average_speed_mib_s: float = Field(
        ...,
        description=(
            "Mean of each successful request's own speed_mib_s " "(average of per-request rates)."
        ),
    )
    overall_speed_mib_s: float = Field(
        ...,
        description=(
            "total_size_mib / total_time_seconds - overall throughput across "
            "all successful requests treated as one continuous transfer."
        ),
    )

    results: list[RequestResult]
