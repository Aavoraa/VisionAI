# syntax=docker/dockerfile:1
#
# Multi-stage build: dependencies are compiled/installed in a throwaway builder stage and
# only the resulting virtualenv plus application code reach the runtime image.
#
#   docker build -t vision-api .
#   docker build -t vision-api:local-model --build-arg INSTALL_LOCAL_MODEL=true .   # + CPU torch/transformers
#   docker build -t vision-api:gpu --build-arg INSTALL_LOCAL_MODEL=true \
#       --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 .      # + CUDA torch
#
# For fully reproducible builds, pin the base image by digest as well:
#   ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm@sha256:<digest>

ARG PYTHON_IMAGE=python:3.11.13-slim-bookworm

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Toolchain for any dependency that must be compiled from source. Never reaches the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt

# Optional in-process inference backend (VISION_USE_MOCK_MODEL=false).
# TORCH_INDEX_URL selects the torch build: CPU by default; for NVIDIA GPUs use a CUDA index,
# e.g. https://download.pytorch.org/whl/cu128 (CUDA wheels bundle the CUDA runtime, so the
# slim base works; the host only needs the NVIDIA driver + NVIDIA Container Toolkit).
ARG INSTALL_LOCAL_MODEL=false
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN if [ "${INSTALL_LOCAL_MODEL}" = "true" ]; then \
        pip install --index-url "${TORCH_INDEX_URL}" torch torchvision \
        && pip install transformers accelerate; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    # Uvicorn reads its worker count from WEB_CONCURRENCY; override at run time
    # (use 1 with the in-process transformers backend: each worker loads its own model copy).
    WEB_CONCURRENCY=2 \
    # Hugging Face cache for downloaded weights (mount a volume here to persist them).
    HF_HOME=/models

# Unprivileged system user with no login shell and no home directory.
RUN groupadd --system --gid 10001 appuser \
    && useradd --system --uid 10001 --gid appuser --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin appuser \
    && mkdir -p /models /data/invoices \
    && chown appuser:appuser /models /data/invoices

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
# Code stays root-owned and read-only for the service user.
COPY app ./app
COPY scripts ./scripts

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header", \
     "--timeout-graceful-shutdown", "30"]
