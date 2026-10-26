# =============================================================================
# Atcraft ERP - FastAPI backend image (master rules section 48)
#   - multi-stage build, small production image
#   - non-root runtime user
#   - pinned base image
#   - development target keeps dev tooling and hot reload
# =============================================================================

# ------------------------------------------------------------------ base ----
# Patch releases and their multi-platform index digests are deliberate release
# inputs. Dependabot proposes reviewed digest updates, so the same source
# revision cannot silently rebuild from different base bytes.
FROM python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# The pinned Python base predates Debian DSA-6572-1. Update the system trust
# store as well as certifi: stdlib TLS clients do not use certifi's CA bundle.
RUN apt-get update \
 && apt-get install --yes --no-install-recommends ca-certificates=20250419+deb13u1 \
 && rm -rf /var/lib/apt/lists/*

# Non-root runtime user. /storage is a mounted volume, owned by this user.
RUN groupadd --gid 10001 atcraft \
 && useradd --uid 10001 --gid atcraft --create-home --shell /usr/sbin/nologin atcraft \
 && mkdir -p /storage \
 && chown -R atcraft:atcraft /storage /app

# ------------------------------------------------------- runtime builder ----
FROM base AS runtime-builder

# Exact versions and artifact hashes are reviewed in source control. Binary
# wheels keep compilers and development headers out of both the build and final
# images; CI verifies this lock on every change.
COPY requirements.lock ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.lock

# ----------------------------------------------------------- dev builder ----
FROM base AS development-builder

COPY requirements-dev.lock ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r requirements-dev.lock

# ----------------------------------------------------------- development ----
FROM base AS development

ENV PATH="/opt/venv/bin:$PATH" \
    APP_ENV=development

COPY --from=development-builder /opt/venv /opt/venv
COPY --chown=atcraft:atcraft . /app

USER atcraft
EXPOSE 8000

# Overridden by docker-compose.dev.yml; kept so the target runs standalone.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ------------------------------------------------------------ production ----
FROM base AS production

ENV PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production

# Runtime-only environment is built independently; test and lint packages were
# never present in a lower layer and therefore cannot inflate the final image.
COPY --from=runtime-builder /opt/venv /opt/venv

# The runtime does not need setuptools; its vendored archive handlers include
# vulnerable code that is otherwise shipped in the image.
RUN /usr/local/bin/python -m pip uninstall --yes setuptools \
 && /opt/venv/bin/python -m pip uninstall --yes setuptools

COPY --chown=atcraft:atcraft src/ /app/src/
COPY --chown=atcraft:atcraft migrations/ /app/migrations/
COPY --chown=atcraft:atcraft alembic.ini pyproject.toml /app/

USER atcraft
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).status == 200 else 1)" || exit 1

CMD ["gunicorn", "src.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "60", \
     "--graceful-timeout", "30", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--access-logfile", "-"]
