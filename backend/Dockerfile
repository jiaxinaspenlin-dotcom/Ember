# Ember — single Python service serving both the UI and the JSON API.
#
# The Tailwind stylesheet is built ahead of time and committed to
# app/static/css/app.css, so this image needs no Node toolchain.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build dependencies for psycopg's C extensions are already covered by the
# binary wheel; libpq is still required at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 ember && chown -R ember:ember /app
USER ember

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

# Migrations run as a release/pre-deploy step, never at import time.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
