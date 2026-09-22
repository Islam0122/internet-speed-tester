"""FastAPI application entrypoint for the Internet Speed Tester."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes.speed_test import router as speed_test_router

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Internet Speed Tester",
    description=(
        "Measures backend-side download speed by performing sequential HTTP "
        "GET requests against a user-provided URL."
    ),
    version="1.0.0",
)

app.include_router(speed_test_router)

# Serve the static frontend assets (CSS/JS) and the dashboard itself.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the frontend dashboard."""
    return FileResponse(STATIC_DIR / "index.html")
