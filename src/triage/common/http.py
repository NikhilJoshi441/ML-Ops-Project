"""HTTP calls between services: per-call timeout, bounded retry with
exponential backoff and jitter, correlation-id and trace propagation (A5)."""
from __future__ import annotations

import random
import time
from typing import Any

import httpx

from triage.common.logging import correlation_id
from triage.common.telemetry import inject_carrier

RETRYABLE_STATUS = {429, 500, 502, 504}


class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _headers() -> dict[str, str]:
    h = {"X-Correlation-ID": correlation_id.get()}
    h.update(inject_carrier())
    return h


def post_json(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    retries: int = 2,
    backoff_base_s: float = 0.3,
    backoff_max_s: float = 3.0,
    on_retry: Any = None,
) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            r = client.post(url, json=payload, timeout=timeout_s, headers=_headers())
            if r.status_code in RETRYABLE_STATUS and attempt < retries:
                raise UpstreamError(f"retryable status {r.status_code}", r.status_code, r.text)
            if r.status_code >= 400:
                body: Any
                try:
                    body = r.json()
                except Exception:
                    body = r.text
                raise UpstreamError(f"{url} -> {r.status_code}", r.status_code, body)
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError, UpstreamError) as exc:
            status = getattr(exc, "status", None)
            retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError)) or status in RETRYABLE_STATUS
            if not retryable or attempt >= retries:
                if isinstance(exc, UpstreamError):
                    raise
                raise UpstreamError(f"{url} failed: {type(exc).__name__}", None, str(exc)) from exc
            attempt += 1
            if on_retry:
                on_retry(attempt, exc)
            delay = min(backoff_max_s, backoff_base_s * (2 ** (attempt - 1)))
            time.sleep(delay * (0.5 + random.random() / 2))


def get_json(client: httpx.Client, url: str, *, timeout_s: float, retries: int = 1) -> dict[str, Any]:
    attempt = 0
    while True:
        try:
            r = client.get(url, timeout=timeout_s, headers=_headers())
            if r.status_code >= 400:
                raise UpstreamError(f"{url} -> {r.status_code}", r.status_code, r.text)
            return r.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= retries:
                raise UpstreamError(f"{url} failed: {type(exc).__name__}") from exc
            attempt += 1
            time.sleep(0.2 * 2**attempt)
