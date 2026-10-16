# Light image: api, triage-agent, agent-worker, llm-gateway, llm-stub
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY corpus/manifest.yaml ./corpus/manifest.yaml
RUN useradd -u 10001 -m app
USER app
