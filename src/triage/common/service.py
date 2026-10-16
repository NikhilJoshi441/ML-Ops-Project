"""Shared FastAPI scaffolding: correlation-id middleware, RED metrics, /metrics,
liveness, tracing. Every HTTP service is built with `create_app`."""
from __future__ import annotations

import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from triage.common.logging import correlation_id, setup_logging
from triage.common.telemetry import instrument_fastapi, instrument_httpx, setup_tracing
from triage.config import get_settings

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["service", "route", "method", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
SERVICE_START = Gauge("service_start_time_seconds", "Unix time the service process started", ["service"])
SERVICE_INFO = Gauge("service_info", "Static service info", ["service", "environment"])


def create_app(service: str, title: str, version: str = "1.0.0", **kwargs) -> FastAPI:
    s = get_settings()
    setup_logging(service, s.log_level)
    setup_tracing(service, s.otel_exporter_otlp_endpoint)
    instrument_httpx()
    app = FastAPI(title=title, version=version, **kwargs)
    SERVICE_START.labels(service).set(time.time())
    SERVICE_INFO.labels(service, s.environment).set(1)

    @app.middleware("http")
    async def _mw(request: Request, call_next: Callable):
        cid = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex[:16]
        token = correlation_id.set(cid)
        t0 = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = cid
            return response
        finally:
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            if path not in ("/metrics",):
                HTTP_LATENCY.labels(service, path, request.method, str(status)).observe(time.perf_counter() - t0)
                HTTP_REQUESTS.labels(service, path, request.method, str(status)).inc()
            correlation_id.reset(token)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict:
        return {"status": "ok", "service": service}

    instrument_fastapi(app)
    return app
