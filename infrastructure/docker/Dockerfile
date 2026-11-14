# Shared build cache; each named target is published as a separate service image.
# Layer order: dependencies first, source code last, so a code change never reinstalls pip packages.
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY infrastructure/requirements/base.txt /tmp/base.txt
RUN python -m pip install -r /tmp/base.txt \
    && useradd --create-home --uid 10001 app \
    && mkdir /data && chown app:app /data
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD ["python", "-m", "common.service_health"]

# Code every service imports (HTTP boundary, config bootstrap, telemetry client).
FROM base AS shared
COPY common/ /app/common/
COPY config/ /app/config/
COPY monitoring/ /app/monitoring/

FROM base AS config
COPY --from=shared /app/ /app/
USER app
CMD ["python", "-m", "config.service"]

FROM base AS monitoring
COPY --from=shared /app/ /app/
USER app
CMD ["python", "-m", "monitoring.service"]

FROM base AS worker
COPY --from=shared /app/ /app/
COPY workers/ workers/
USER app
CMD ["python", "-m", "workers"]

FROM base AS input-processing
COPY infrastructure/requirements/processing.txt /tmp/processing.txt
RUN python -m pip install -r /tmp/processing.txt \
    && apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-vie \
    && rm -rf /var/lib/apt/lists/*
COPY --from=shared /app/ /app/
COPY input_processing/ input_processing/
USER app
CMD ["python", "-m", "input_processing.service"]

FROM base AS mcp
COPY infrastructure/requirements/mcp.txt /tmp/mcp.txt
# /models: mount point for the repo's ./models (read-only, pre-downloaded by scripts/download_models.py)
RUN python -m pip install -r /tmp/mcp.txt \
    && mkdir /models && chown app:app /models
COPY --from=shared /app/ /app/
COPY integrations/ integrations/
COPY rag/ rag/
COPY mcp_server/ mcp_server/
USER app
CMD ["python", "-m", "mcp_server.service"]

FROM base AS hermes-deps
COPY infrastructure/requirements/agents.txt /tmp/agents.txt
RUN python -m pip install -r /tmp/agents.txt

FROM hermes-deps AS hermes-base
COPY --from=shared /app/ /app/
COPY integrations/ integrations/
# agents/ carries the three agents plus Hermes (agents/hermes: integration code + hermes_agent upstream source).
COPY agents/ agents/
COPY configs/ configs/
USER app

FROM hermes-base AS agent-1
ENV AGENT_ID=agent_1_classification
CMD ["python", "-m", "agents.service"]

FROM hermes-base AS agent-2
ENV AGENT_ID=agent_2_risk_scoring
CMD ["python", "-m", "agents.service"]

FROM hermes-base AS agent-3
ENV AGENT_ID=agent_3_recommendation
CMD ["python", "-m", "agents.service"]

FROM hermes-base AS langgraph
COPY orchestration/ orchestration/
CMD ["python", "-m", "orchestration.service"]

FROM hermes-deps AS test
COPY infrastructure/requirements/test.txt /tmp/test.txt
RUN python -m pip install -r /tmp/test.txt
COPY --from=shared /app/ /app/
COPY integrations/ integrations/
COPY agents/ agents/
COPY configs/ configs/
COPY orchestration/ orchestration/
COPY workers/ workers/
COPY input_processing/ input_processing/
COPY tests/ tests/
USER app
CMD ["python", "tests/integration/container_smoke.py"]
