"""Shared test helpers."""

from __future__ import annotations

import httpx


def streamed_response(
    status_code: int,
    *,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
    request: httpx.Request | None = None,
) -> httpx.Response:
    """Build a mock response whose body is still unread, as on a real network.

    ``httpx.Response(content=...)`` reads the body eagerly, after which
    ``aiter_raw`` refuses to stream it. A real socket response is always a
    stream, so mock responses must be streams too.
    """
    return httpx.Response(
        status_code,
        headers=headers,
        stream=httpx.ByteStream(content),
        request=request,
    )
