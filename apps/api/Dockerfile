# =============================================================================
# Atcraft ERP - FastAPI backend image (master rules section 48)
#   - multi-stage build, small production image
#   - non-root runtime user
#   - pinned base image
#   - development target keeps dev tooling and hot reload
# =============================================================================

# ------------------------------------------------------------------ base ----
FROM python:3.11.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# libpq is required by psycopg (Alembic sync path); curl backs container probes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. /storage is a mounted volume, owned by this user.
RUN groupadd --gid 10001 atcraft \
 && useradd --uid 10001 --gid atcraft --create-home --shell /usr/sbin/nologin atcraft \
 && mkdir -p /storage \
 && chown -R atcraft:atcraft /storage /app

# --------------------------------------------------------------- builder ----
FROM base AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Dependency layer is cached on pyproject.toml alone, so source edits do not
# trigger a full reinstall.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && mkdir -p src && touch src/__init__.py \
 && /opt/venv/bin/pip install ".[dev]"

# ----------------------------------------------------------- development ----
FROM base AS development

ENV PATH="/opt/venv/bin:$PATH" \
    APP_ENV=development

COPY --from=builder /opt/venv /opt/venv
COPY --chown=atcraft:atcraft . /app

USER atcraft
EXPOSE 8000

# Overridden by docker-compose.dev.yml; kept so the target runs standalone.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ------------------------------------------------------------ production ----
FROM base AS production

ENV PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production

# Runtime dependencies only - no dev extras in the production image.
COPY --from=builder /opt/venv /opt/venv
RUN /opt/venv/bin/pip uninstall -y pytest pytest-asyncio pytest-cov ruff mypy faker watchfiles 2>/dev/null || true

COPY --chown=atcraft:atcraft src/ /app/src/
COPY --chown=atcraft:atcraft migrations/ /app/migrations/
COPY --chown=atcraft:atcraft alembic.ini pyproject.toml /app/

USER atcraft
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health/live || exit 1

CMD ["gunicorn", "src.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--access-logfile", "-"]
