"""OpenTelemetry tracing setup (OTLP/HTTP -> Jaeger) and context propagation
helpers for hand-offs that cross the Redis queue."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)
_configured = False


def setup_tracing(service: str, endpoint: str) -> None:
    global _configured
    if _configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
        except Exception:  # pragma: no cover
            logger.exception("otlp exporter unavailable; tracing kept in-process only")
    trace.set_tracer_provider(provider)
    _configured = True


def instrument_fastapi(app: Any) -> None:
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,healthz,readyz")
    except Exception:  # pragma: no cover
        logger.warning("fastapi instrumentation unavailable")


def instrument_httpx() -> None:
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:  # pragma: no cover
        logger.info("httpx instrumentation unavailable; propagating context manually")


def tracer(name: str = "triage") -> trace.Tracer:
    return trace.get_tracer(name)


def inject_carrier() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def attached(carrier: dict[str, str] | None) -> Iterator[None]:
    token = otel_context.attach(propagate.extract(carrier or {}))
    try:
        yield
    finally:
        otel_context.detach(token)
