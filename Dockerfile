# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# curl is used by the compose healthcheck; build-essential is not needed because
# every dependency ships manylinux wheels for CPython 3.12.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Dependency layer is cached independently of source so code edits don't reinstall.
# The placeholder package is needed because an editable install resolves the
# package directory at install time, and the real source is copied in below.
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
 && pip install --upgrade pip setuptools wheel \
 && pip install -e ".[dev]"

COPY . .

# Run as a non-root user: the container never needs write access to its own code.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /srv
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
