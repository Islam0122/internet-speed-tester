# Internet Speed Tester

A small full-stack application that measures **backend-side download
performance** by performing a configurable number of sequential HTTP GET
requests against a URL you provide. It reports timing and throughput, both
through a web dashboard and a command-line interface.

> **Important:** This measures how fast the *server running this application*
> can download the target file, not your browser's or your own machine's
> connection. See [Limitations](#limitations).

---

## Features

- Runs **N sequential HTTP GET requests** (default 10, allowed 1–50) against an HTTP or HTTPS URL.
- Requests **identity encoding** (`Accept-Encoding: identity`) and counts the bytes actually received. `Content-Length` is never trusted.
- Measures the full download time of each response body with `time.perf_counter()`.
- Reports average request time, average per-request speed and overall throughput, all in **MiB/s**.
- A failed request never aborts the run. It is reported with its error.
- Redirects are **not followed**. A 3xx response is reported as a failed request.
- SSRF guard: rejects targets that resolve to private, loopback, link-local, metadata or other non-public addresses.
- Limits: response size cap, per-request timeouts and a **total deadline** for the whole run.
- Web dashboard (vanilla HTML/CSS/JS) and a console CLI that share one implementation.
- Tested with pytest. The tests use mocked transports and need no network access.

---

## Tech stack

**Backend:** Python 3.11+, FastAPI, Uvicorn, HTTPX (async, streaming), Pydantic v2.
**Tooling:** pytest, pytest-asyncio, Ruff.
**Frontend:** HTML5, CSS3, vanilla JavaScript (Fetch API). No build step.
**Deployment:** Docker, Docker Compose, `.env`-based configuration.

---

## Architecture

```
internet-speed-tester/
│
├── app/
│   ├── main.py                    # FastAPI app, static files, index route
│   ├── config.py                  # Environment-driven settings (timeouts, limits, SSRF switch)
│   ├── schemas/
│   │   └── speed_test.py          # Pydantic request/response models + URL validation
│   ├── api/routes/
│   │   └── speed_test.py          # /health and /api/speed-test (thin controllers)
│   ├── services/
│   │   └── speed_tester.py        # Core logic: sequential downloads, SSRF guard, metrics
│   ├── cli.py                     # Console entry point (argparse), reuses SpeedTester
│   └── static/                    # index.html, style.css, app.js (dashboard)
│
├── tests/
│   ├── helpers.py                 # Shared mock-response helper
│   ├── test_speed_tester.py       # Service tests (sequencing, gzip, redirects, SSRF, limits)
│   ├── test_api.py                # API tests (FastAPI TestClient)
│   └── test_cli.py                # CLI tests (exit codes, output, Ctrl+C)
│
├── requirements.txt               # Runtime dependencies (used by Docker)
├── requirements-dev.txt           # Adds pytest, pytest-asyncio, ruff
├── pyproject.toml                 # Ruff and pytest configuration
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
└── .env.example
```

**Separation of concerns:**
- **Routes** validate input (Pydantic), call the service and shape the HTTP response.
- **Schemas** define and validate the public API contract.
- **Service** (`SpeedTester`) owns download, timing, SSRF and metrics. It is testable without FastAPI, using an injectable HTTPX transport.
- **CLI** is a thin layer over the same service.

---

## Running it

### Requirements

Python 3.11 or newer.

### Local setup

```bash
git clone https://github.com/Islam0122/internet-speed-tester.git
cd internet-speed-tester

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements-dev.txt   # runtime + pytest + ruff
```

To install only the runtime dependencies, use `pip install -r requirements.txt`.

### Web server

```bash
uvicorn app.main:app --reload
```

- Dashboard: http://localhost:8000
- Swagger / API docs: http://localhost:8000/docs

Optionally copy `.env.example` to `.env` and adjust values. The defaults work without it.

### Command line

```bash
python -m app.cli https://example.com/large-image.jpg
```

See [CLI usage](#cli-usage) for options, output and exit codes.

### Docker

```bash
docker build -t internet-speed-tester .
docker run --rm -p 8000:8000 internet-speed-tester
```

Or with Docker Compose (reads `.env` if it exists):

```bash
docker compose up --build
```

The app is then available at http://localhost:8000. The image runs as a non-root
user and includes only runtime dependencies (no pytest or ruff).

---

## CLI usage

The CLI runs **10 sequential HTTP GET requests by default**, waits for each
response body to be fully downloaded, and prints timings and throughput.

```bash
python -m app.cli https://example.com/large-image.jpg          # 10 requests
python -m app.cli https://example.com/large-image.jpg -n 5     # 5 requests
```

| Option | Description |
|---|---|
| `url` (required) | HTTP or HTTPS URL of the file to download. |
| `-n N`, `--requests N` | Number of sequential requests. Default `10`, range `1`–`50`. |
| `-h`, `--help` | Show usage. |

Real output (a 5 MiB file on a local server, 10 requests):

```
INTERNET SPEED TEST
─────────────────────────────
[1/10]  Downloaded 5.00 MiB in 0.028 s
[2/10]  Downloaded 5.00 MiB in 0.009 s
[3/10]  Downloaded 5.00 MiB in 0.008 s
[4/10]  Downloaded 5.00 MiB in 0.008 s
[5/10]  Downloaded 5.00 MiB in 0.008 s
[6/10]  Downloaded 5.00 MiB in 0.009 s
[7/10]  Downloaded 5.00 MiB in 0.008 s
[8/10]  Downloaded 5.00 MiB in 0.008 s
[9/10]  Downloaded 5.00 MiB in 0.008 s
[10/10] Downloaded 5.00 MiB in 0.008 s

─────────────────────────────
RESULTS

Successful requests: 10/10
Average request time: 0.010 s
Total downloaded: 50.00 MiB
Total download time: 0.10 s
Download speed: 482.44 MiB/s

Status: SUCCESS
─────────────────────────────
```

A failed request is shown in place of its line, and the run continues:

```
[1/2]   FAILED (HTTP error: 404)
[2/2]   FAILED (HTTP error: 404)
...
Status: FAILED
```

**Units:** times in seconds (`s`), sizes in binary mebibytes (`MiB`, 1 MiB = 1024 × 1024 bytes),
speeds in `MiB/s`. `Download speed` is the overall throughput: total downloaded size divided by
total download time. The API also returns `average_speed_mib_s`, the mean of the per-request speeds.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | All requests succeeded. |
| `1` | At least one request failed, or the test could not run (SSRF block, total deadline exceeded, unexpected error). |
| `2` | Invalid arguments (malformed URL, or `-n` outside `1`–`50`). |
| `130` | Interrupted with Ctrl+C. No report is printed. |

When every request fails, averages show `n/a`. The CLI uses the same SSRF guard, size cap,
timeouts and settings as the API. To test a resource on `localhost`, set
`ENABLE_SSRF_PROTECTION=false` for that run only.

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

`requests_count` is optional (default 10, range 1–50).

Response (abbreviated):

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

**Status codes:**

| Code | When |
|---|---|
| `200` | The test ran. Individual requests may still have failed, see `results`. |
| `400` | The target was rejected by the SSRF guard. |
| `422` | Invalid input: malformed URL, unsupported scheme, `requests_count` out of range. |
| `429` | Another speed test is already running (see `MAX_CONCURRENT_TESTS`). The request is refused at once, not queued. |
| `504` | The whole test exceeded `TOTAL_TIMEOUT_SECONDS`, including the DNS check. |
| `500` | Unexpected server error. The details are logged on the server, not returned. |

---

## Calculation methodology

- **What is measured:** for each request, `time.perf_counter()` starts just before the GET is sent and stops after the *entire response body* has been received. This is full download time, not latency.
- **How bytes are counted:** the client requests `Accept-Encoding: identity`, and bytes are counted from the raw body as received. The count is the amount that crossed the network, never `Content-Length`. If a server compresses the body anyway, the count is the compressed size, not the decompressed size, so speed reflects the network and not the decompression.
- **Average request time (`average_time_seconds`):** `sum(duration of successful requests) / successful count`. Failed requests are excluded. Successful requests with an empty body are included.
- **Per-request speed (`speed_mib_s`):** `size_mib / duration_seconds`. `0.0` for failed requests, and `0.0` if the duration is zero.
- **Average speed (`average_speed_mib_s`):** mean of `speed_mib_s` over successful requests that downloaded **at least one byte**. A successful request with an empty body has no meaningful speed, so it is left out of this mean. `0.0` when no request downloaded any bytes.
- **Overall throughput (`overall_speed_mib_s`):** `total_size_mib / total_time_seconds` over successful requests, treated as one continuous transfer. Empty successful requests add to the time but not to the size, so they lower this figure. `0.0` when `total_time_seconds` is zero or no bytes were received. This is usually the more representative figure when request sizes or durations differ.

**Worked example.** One request downloads 1 MiB in 1 s, and a second successful request returns an empty body in 1 s:

| Field | Value | Why |
|---|---|---|
| `average_time_seconds` | `1.0` | (1 + 1) / 2 successful requests |
| `average_speed_mib_s` | `1.0` | Only the 1 MiB request counts: 1 / 1 |
| `overall_speed_mib_s` | `0.5` | 1 MiB / (1 s + 1 s) |

The two speed figures differ on purpose: one is the mean per-request speed, the other is throughput over the whole run.
- **MiB, not MB:** all sizes and speeds use binary mebibytes (MiB = 1024 × 1024 bytes). Field names carry `_mib` to make this explicit.
- **Failed transfers:** an aborted or truncated response is a failed request. Its partial bytes are shown for that request but are not included in totals.

---

## HTTP behaviour

| Situation | Result |
|---|---|
| `2xx` response, body read to the end | Successful request. |
| `3xx` response (redirect) | **Failed**, with the error `Redirect not followed: HTTP <code>`. The `Location` target is never requested. |
| `4xx` / `5xx` response | Failed, with `HTTP error: <code>`. |
| No response within `CONNECT_TIMEOUT_SECONDS`, or no data for `READ_TIMEOUT_SECONDS` | Failed, with a timeout error. |
| Connection lost during the body | Failed, with a connection error. The partial bytes are not counted in totals. |
| Body larger than `MAX_RESPONSE_BYTES` | Failed as soon as the cap is exceeded. The rest of the body is not read. |
| Empty body (`200` with 0 bytes) | Successful, with 0 bytes and speed 0. |
| Whole run exceeds `TOTAL_TIMEOUT_SECONDS` (including the DNS check) | The run is aborted. The API returns `504`, the CLI exits with `1`. |

**Redirects are not followed**, on purpose. Following them would let a public URL
redirect the server to an internal address after the SSRF check had already passed.
To download from a redirected URL, use the final address directly.

---

## Security

The target URL is **untrusted input**. The endpoint makes the server send requests
on the caller's behalf, which is a classic SSRF vector. The following measures are implemented.

- Only `http://` and `https://` URLs are accepted (validated by Pydantic).
- **SSRF guard:** before the first request, the hostname is resolved (without blocking the event loop) and every returned address is checked. Requests are refused if any address is:
  - private, loopback, link-local, multicast, reserved or unspecified (`ipaddress` checks, which include `169.254.169.254`, the AWS/GCP/Azure metadata address);
  - in one of these ranges that `ipaddress` does not always cover: `100.64.0.0/10` (carrier-grade NAT, which hosts Alibaba Cloud metadata at `100.100.100.200`), `198.18.0.0/15` (benchmarking), `192.0.0.0/24` (IETF assignments), `240.0.0.0/4` (reserved);
  - an IPv4-mapped IPv6 address whose IPv4 part falls in any of these ranges (for example `::ffff:127.0.0.1`).
- **Redirects are not followed**, so the check applies to the exact URL the client asked for. There is no redirect path to an internal address.
- **Size cap:** a response is aborted when it exceeds `MAX_RESPONSE_BYTES` (default 200 MiB).
- **Timeouts:** connect and read timeouts per request, and `TOTAL_TIMEOUT_SECONDS` for the whole run. The deadline also covers the DNS check, so a slow resolver cannot extend a run.
- **Concurrency limit:** the API runs at most `MAX_CONCURRENT_TESTS` tests at once (default 1). Further requests get `429` immediately and are not queued. This limits the amplification described below. The limit applies per process, and the Docker image runs a single worker.
- Errors returned to clients are generic. Stack traces are logged on the server only.

### Known limitations of the SSRF guard

- **DNS rebinding is not prevented.** The hostname is resolved once for the check, and the HTTP client resolves it again when it connects. A DNS server that answers with a public address for the check and a private address for the connection gets past the guard. Closing this needs the connection pinned to the checked IP address, which is not implemented.
- The guard is a **defence-in-depth measure**, not a complete control.
- **There is no authentication and no request-rate limit.** Concurrent tests are capped by `MAX_CONCURRENT_TESTS`, but one test can still make the server download up to `MAX_REQUESTS_COUNT × MAX_RESPONSE_BYTES` (50 × 200 MiB = 10 GiB), and clients can run tests back to back. Do not expose this service to untrusted users without adding authentication and rate limits in front of it.
- `ENABLE_SSRF_PROTECTION=false` disables the guard. Use it only for local testing.

---

## Configuration

Settings are read from environment variables (see `.env.example`).

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_REQUESTS_COUNT` | `10` | Requests performed when `requests_count` is not given. |
| `MAX_REQUESTS_COUNT` | `50` | Upper bound for `requests_count`. |
| `CONNECT_TIMEOUT_SECONDS` | `5.0` | Timeout for establishing a connection. |
| `READ_TIMEOUT_SECONDS` | `15.0` | Maximum wait for each chunk of the body. |
| `TOTAL_TIMEOUT_SECONDS` | `120.0` | Deadline for the whole run, including the DNS check. |
| `MAX_CONCURRENT_TESTS` | `1` | Speed tests the API runs at once. Extra requests get `429`. |
| `MAX_RESPONSE_BYTES` | `209715200` (200 MiB) | Size cap for one response. |
| `CHUNK_SIZE_BYTES` | `65536` (64 KiB) | Read size while streaming a body. |
| `ENABLE_SSRF_PROTECTION` | `true` | Enables the SSRF guard. |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | Bind address for the Docker container's server. |

---

## Testing

```bash
pytest                       # all tests
ruff check .                 # lint
ruff format --check .        # formatting
```

The tests use `httpx.MockTransport` and FastAPI's dependency overrides, so no real network access is needed.
Sequencing tests use an async transport that records how many requests are in flight at the same time.
A negative control confirms that this detector does catch overlapping requests.

Covered scenarios: a successful download, ten sequential requests, byte counting independent of
`Content-Length`, gzip responses, identity encoding in requests, redirects (not followed, and
not requested even with the SSRF guard on), HTTP errors, timeouts, connection errors, connection
dropped mid-body, the size cap, the total deadline (including a slow DNS check), empty bodies,
all requests failing, the speed formulas with empty bodies, SSRF address ranges, CLI exit codes,
output and Ctrl+C, API status codes, and the concurrency limit (`429` for a second simultaneous request).

---

## Limitations

- Measures the **backend server's** download performance, not the end user's connection.
- Results depend on server-side network conditions, the target's load and throttling, and the normal variability of HTTP transfers. They are not a guaranteed, repeatable benchmark.
- Requests run **strictly sequentially** by design. This under-reports compared with a multi-connection test that saturates a link.
- A CDN or proxy in front of the target may serve cached copies, which makes later requests faster than the first. The tester does not add cache-busting parameters.
- Redirects are not followed. Some legitimate URLs that redirect will therefore fail.
- Very large files: a response above `MAX_RESPONSE_BYTES` is always reported as failed, because it is cut off at the cap.
- The dashboard's progress indicator is indeterminate. The API runs the whole test as one request, so per-request progress is not available.
- The CLI prints its report when the run finishes. Per-request lines are not streamed during the run.

---

## Future improvements

- Stream per-request progress (through an optional callback in `SpeedTester.run`) to the CLI and dashboard.
- Pin the resolved IP address for the connection to close the DNS-rebinding gap.
- Rate limiting and authentication for multi-user deployments.
- Background jobs with polling or WebSocket progress.
- Persistent history of past runs (would need a database).

None of these are implemented today. They are listed as possible next steps only.
