FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies first for better layer caching.
# Dev tools (pytest, ruff) are in requirements-dev.txt and are not installed here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code only (tests and docs are excluded via .dockerignore).
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# APP_HOST and APP_PORT are read at container start (defaults: 0.0.0.0 and 8000).
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"${APP_HOST:-0.0.0.0}\" --port \"${APP_PORT:-8000}\""]
