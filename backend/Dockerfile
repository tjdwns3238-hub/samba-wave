# Base Image
FROM python:3.12.3-slim

# Set environment variables for non-interactive installs and unbuffered python output.
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install system dependencies required by Python libraries like OpenCV.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget curl git ca-certificates \
    libgl1 libglib2.0-0 libsm6 libxext6 ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Remove the externally managed marker to allow system installs
RUN rm -f /usr/lib/python*/EXTERNALLY-MANAGED

# Download and run the uv installer (버전 고정 — 최신 uv의 런타임 변경 방지)
# /usr/local/bin으로 복사 — 비루트 사용자도 접근 가능
RUN wget -q https://astral.sh/uv/0.10.9/install.sh -O /uv-installer.sh
RUN sh /uv-installer.sh && rm /uv-installer.sh \
    && cp /root/.local/bin/uv /usr/local/bin/uv

# Tell uv to use the system Python by default
ENV UV_SYSTEM_PYTHON=1
ENV UV_COMPILE_BYTECODE=1

# Set the application's working directory.
WORKDIR /app/backend
COPY backend/__init__.py ./backend/__init__.py
COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md
COPY uv.lock ./uv.lock
RUN uv sync --compile-bytecode --no-dev


COPY backend ./backend
COPY scripts ./scripts
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

# 배포 메타데이터 (헬스체크에 노출) — deploy.sh가 --build-arg 로 주입
ARG COMMIT_SHA=unknown
ARG DEPLOYED_AT=
ENV COMMIT_SHA=$COMMIT_SHA
ENV DEPLOYED_AT=$DEPLOYED_AT

# 비루트 사용자로 실행 (보안 강화)
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["./scripts/entrypoint.sh"]
