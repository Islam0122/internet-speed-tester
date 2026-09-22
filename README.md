# Internet Speed Tester

A small full-stack application that measures **backend-side download
performance** by performing a configurable number of sequential HTTP GET
requests against a URL you provide, and reports timing and throughput
statistics.

> **Important:** This measures how fast the *server running this
> application* can download the target file — not your browser's or your
> own machine's internet connection. See [Limitations](#limitations).

---

## Features

- Runs **N sequential HTTP GET requests** (default 10, configurable 1-50) against any HTTP/HTTPS URL.
- Streams each response and counts bytes actually read (never trusts `Content-Length`).
- Measures full download duration per request with `time.perf_counter()`.
- Computes average request time, average per-request speed, and overall throughput.
- Distinguishes successful vs. failed requests; a single failure never aborts the run.
- Basic SSRF protection: rejects targets that resolve to private/loopback/link-local/metadata addresses.
- Enforces a maximum response size and bounded redirects.
- Clean, responsive dashboard (vanilla HTML/CSS/JS, no frontend framework).
- Fully tested with pytest against a mocked HTTP transport (no external network dependency in tests).

---

## Tech stack

**Backend:** Python 3.11+, FastAPI, Uvicorn, HTTPX (async, streaming), Pydantic v2, Pytest, pytest-asyncio, Ruff.

**Frontend:** HTML5, CSS3, vanilla JavaScript (Fetch API). No build step, no framework.

**DevOps:** Docker, Docker Compose, `.env`-based configuration.

---

## Architecture

```
internet-speed-tester/
│
├── app/
│   ├── main.py                    # FastAPI app, static file mounting, index route
│   ├── config.py                  # Environment-driven settings (timeouts, limits, etc.)
│   ├── schemas/
│   │   └── speed_test.py          # Pydantic request/response models + URL validation
│   ├── api/routes/
│   │   └── speed_test.py          # /health and /api/speed-test routes (thin controllers)
│   ├── services/
│   │   └── speed_tester.py        # Core logic: sequential downloads, SSRF guard, metrics
│   └── static/                    # index.html, style.css, app.js (the dashboard)
│
├── tests/
│   ├── test_speed_tester.py       # Unit tests for the service layer (mocked transport)
│   └── test_api.py                # API integration tests (FastAPI TestClient)
│
├── requirements.txt
├── pyproject.toml                 # Ruff + pytest configuration
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── .gitignore
```

**Separation of concerns:**
- **Routes** validate input (via Pydantic), call the service, and shape the HTTP response. No business logic lives here.
- **Schemas** define and validate the API's public contract.
- **Service** (`SpeedTester`) owns all download/measurement logic and is fully unit-testable in isolation from FastAPI, using an injectable HTTPX transport.

---

## Installation

```bash
git clone <this-repository-url>
cd internet-speed-tester

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Local launch

```bash
uvicorn app.main:app --reload
```

Then open:
- Dashboard: http://localhost:8000
- Swagger / API docs: http://localhost:8000/docs

Optionally copy `.env.example` to `.env` and adjust values; the defaults work out of the box.

## Docker launch

```bash
docker build -t internet-speed-tester .
docker run --rm -p 8000:8000 internet-speed-tester
```

Or with Docker Compose:

```bash
docker compose up --build
```

The app will be available at http://localhost:8000.

---

## API usage

### `GET /health`

```json
{ "status": "ok" }
```

### `POST /api/speed-test`

Request:

```json
{
  "url": "https://example.com/sample-file.jpg",
  "requests_count": 10
}
```

`requests_count` is optional (defaults to 10, max 50).

Response:

```json
{
  "url": "https://example.com/sample-file.jpg",
  "total_requests": 10,
  "successful_requests": 10,
  "failed_requests": 0,
  "total_bytes": 52428800,
  "total_size_mib": 50.0,
  "average_time_seconds": 0.412,
  "total_time_seconds": 4.12,
  "average_speed_mib_s": 12.14,
  "overall_speed_mib_s": 12.14,
  "results": [
    {
      "request_number": 1,
      "success": true,
      "status_code": 200,
      "duration_seconds": 0.41,
      "bytes_downloaded": 5242880,
      "size_mib": 5.0,
      "speed_mib_s": 12.19,
      "error": null
    }
  ]
}
```

On a rejected URL (invalid scheme, unresolvable host, or an SSRF-blocked
target), the API returns `400` or `422` with a `detail` message — never a raw
stack trace.

---

## Calculation methodology

- **What is measured:** for each request, a high-resolution timer
  (`time.perf_counter()`) starts right before the GET request is sent and
  stops only after the *entire response body* has been streamed and read.
  This is full download time, not just time-to-first-byte/latency.
- **How bytes are counted:** bytes are summed from the chunks actually read
  off the response stream. The `Content-Length` header is never trusted —
  a request that lies about its length, or is truncated mid-transfer, is
  measured by what was actually received.
- **Average request time:** `sum(duration_seconds of successful requests) / successful_count`. Failed requests are excluded, since they don't represent a completed download.
- **Per-request speed:** `size_mib / duration_seconds` for that single request. `0.0` when duration is `0` (never divides by zero).
- **Average speed (`average_speed_mib_s`):** the mean of each successful request's own per-request speed. Every request counts equally regardless of its size.
- **Overall throughput (`overall_speed_mib_s`):** `total_size_mib / total_time_seconds` across all successful requests, treated as one continuous transfer. This is generally the more representative "real world" number when request sizes or durations vary.
- **MiB vs MB:** this project uses **binary megabytes (MiB = 1024×1024 bytes)** throughout, not decimal MB (1,000,000 bytes). All fields are named `*_mib*` to make this explicit.

---

## Security

The target URL is treated as **untrusted input**, since this endpoint makes
the server perform outbound requests on the caller's behalf (a classic SSRF
vector). Mitigations implemented:

- Only `http://` and `https://` URLs are accepted (validated by Pydantic).
- Connection and read timeouts are enforced (configurable via env vars).
- The response body is capped at `MAX_RESPONSE_BYTES` (default 200 MiB); downloads exceeding this are aborted and marked failed.
- Redirects are followed but bounded by `MAX_REDIRECTS` (default 5).
- **DNS-resolution SSRF guard:** before connecting, the target hostname is resolved and every returned IP is checked against private, loopback, link-local, multicast, reserved, and unspecified ranges (this also blocks the common `169.254.169.254` cloud metadata address, which falls under link-local). Requests to any matching address are rejected with `400`.
- Errors returned to clients are generic; stack traces are logged server-side only, never exposed in responses.

### Documented limitations

This is a **best-effort, defense-in-depth** guard suitable for a local
technical assignment — it is **not** a complete SSRF-proofing:

- **DNS rebinding:** the guard resolves the hostname once before connecting; HTTPX performs its own resolution when it actually opens the connection. A malicious DNS server could theoretically return a public IP for the check and a private IP moments later. Closing this gap fully requires pinning the resolved IP and connecting to it directly, which adds meaningful complexity and isn't done here.
- It does not inspect redirect targets individually for SSRF — a redirect to a private address after the initial check could bypass the guard. If this needs to be production-hardened, add a per-hop resolution check or disable redirects entirely.
- It can be disabled via `ENABLE_SSRF_PROTECTION=false` for local testing against `localhost` resources; do not disable it in any environment exposed to untrusted users.

**This application must not be considered production-hardened as-is.** Treat
it as a solid starting point for a real SSRF defense, not a finished one.

---

## Testing

```bash
pytest
```

Tests use `httpx.MockTransport` (unit tests) and FastAPI's dependency
override system with a mocked transport (API tests) — no real network access
is required or used, so results are deterministic and reproducible offline.

Covered scenarios include: a single successful download, ten sequential
requests, byte counting independent of `Content-Length`, average time and
speed calculations, failed HTTP responses, timeouts, connection errors,
empty response bodies, zero-duration protection, invalid URLs and schemes,
out-of-range `requests_count`, API response schema, and an explicit
assertion that requests are never executed concurrently.

Linting:

```bash
ruff check .
ruff format --check .
```

---

## Limitations

- Measures the **backend server's** download performance, not the end
  user's browser/internet connection.
- Results depend on server-side network conditions, the target host's own
  load/throttling, and general variability of HTTP transfers — they are not
  a guaranteed, repeatable benchmark.
- Requests run strictly **sequentially** by design (per the assignment
  spec); this is intentionally not the fastest way to saturate a link, and
  will under-report compared to a multi-connection speed test.
- URL safety restrictions (SSRF guard, size cap, redirect cap) apply — some
  legitimate URLs behind private networks, or unusually large files, will be
  rejected or truncated. See [Security](#security) for exact limits and caveats.
- The dashboard's progress indicator is intentionally indeterminate: the
  current API executes the whole test as a single request/response, so no
  real per-request progress is available yet, and the UI does not fabricate
  numbers it can't back up.

---

## Future improvements

- Background job execution with a polling or WebSocket-based progress API, so the frontend can show real per-request progress instead of an indeterminate bar.
- Persistent history of past test runs (would require a database).
- Authentication / rate limiting for multi-tenant deployments.
- Support for testing from multiple geographic locations.
- Per-hop SSRF validation on redirects, and IP-pinned connections to fully close the DNS-rebinding gap described above.

None of the above are implemented today — they are listed here only as
possible next steps, not existing functionality.
