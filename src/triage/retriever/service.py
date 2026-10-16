"""Retriever service: the only process that loads the embedding model and the
vector index. Agents reach the corpus exclusively through this API, which is
what lets them treat retrieval as a tool.

Deployment boundary rationale: memory-heavy (model + index), CPU-bound,
read-mostly, and released on a different cadence (re-indexing) from the agent
code. It scales independently of the agent workers."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Optional

import redis
from fastapi import HTTPException
from prometheus_client import Counter, Gauge, Histogram
from pydantic import BaseModel, Field

from triage.common.logging import log
from triage.common.service import create_app
from triage.common.telemetry import tracer
from triage.config import get_settings
from triage.retriever.index import CorpusIndex

logger = logging.getLogger("retriever")
s = get_settings()
app = create_app("retriever", "Corpus retriever", description="Hybrid clause-aware retrieval over the RBI/bank corpus")

INDEX_BUILT = Gauge("index_built_timestamp_seconds", "When the index profile was built", ["profile"])
INDEX_CHUNKS = Gauge("index_chunks", "Chunks in the index profile", ["profile"])
INDEX_DOC = Gauge("index_document_info", "Corpus document versions in the index", ["profile", "doc_id", "version", "sha256"])
INGEST_TPUT = Gauge("ingest_throughput_chunks_per_second", "Pipeline throughput at last build", ["profile", "stage"])
INDEX_AGE = Gauge("index_age_seconds", "Seconds since the index profile was built", ["profile"])
SEARCH_LAT = Histogram("retrieval_latency_seconds", "Retriever operation latency", ["op"],
                       buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5))
CACHE = Counter("retrieval_cache_total", "Retrieval cache lookups", ["result"])

_index: Optional[CorpusIndex] = None
_lock = threading.Lock()
_redis = redis.Redis.from_url(s.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)


def index() -> CorpusIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = CorpusIndex(s.index_dir, s.embedding_model)
                _publish_meta()
    return _index


def _publish_meta() -> None:
    if _index is None:
        return
    for name, p in _index.profiles.items():
        INDEX_BUILT.labels(name).set(p.meta.get("built_at", 0))
        INDEX_CHUNKS.labels(name).set(p.meta.get("chunks_total", 0))
        INGEST_TPUT.labels(name, "total").set(p.meta.get("throughput_chunks_per_s") or 0)
        INGEST_TPUT.labels(name, "embed").set(p.meta.get("embed_throughput_chunks_per_s") or 0)
        for d in p.meta.get("documents", []):
            INDEX_DOC.labels(name, d["doc_id"], d["version"], d["sha256"][:12]).set(1)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=index, daemon=True).start()


@app.get("/readyz", tags=["ops"])
def readyz():
    if _index is None or not _index.ready():
        raise HTTPException(503, "index not loaded")
    for name, p in _index.profiles.items():
        INDEX_AGE.labels(name).set(time.time() - p.meta.get("built_at", time.time()))
    return {"status": "ready", "profiles": list(_index.profiles)}


class SearchReq(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    k: int = Field(default=5, ge=1, le=12)
    profile: Optional[str] = None
    doc_ids: Optional[list[str]] = None
    mode: str = Field(default="hybrid", pattern="^(hybrid|dense|bm25)$")


@app.post("/v1/search")
def search(req: SearchReq):
    idx = index()
    prof = idx.profile(req.profile)
    key = "rcache:" + hashlib.sha256(
        json.dumps([req.model_dump(), prof.name, prof.meta.get("built_at")], sort_keys=True).encode()
    ).hexdigest()
    if s.retrieval_cache_enabled:
        try:
            cached = _redis.get(key)
            if cached:
                CACHE.labels("hit").inc()
                out = json.loads(cached)
                out["cached"] = True
                return out
        except redis.RedisError:
            pass
        CACHE.labels("miss").inc()
    with SEARCH_LAT.labels("search").time(), tracer().start_as_current_span("retriever.search") as span:
        span.set_attribute("retrieval.query", req.query[:200])
        span.set_attribute("retrieval.profile", prof.name)
        out = idx.search(req.query, req.k, req.profile, req.doc_ids, req.mode)
        span.set_attribute("retrieval.hits", ",".join(h["chunk_id"] for h in out["hits"]))
    out["cached"] = False
    if s.retrieval_cache_enabled:
        try:
            _redis.setex(key, s.retrieval_cache_ttl_s, json.dumps(out))
        except redis.RedisError:
            pass
    return out


@app.get("/v1/clause/{ident:path}")
def clause(ident: str, profile: Optional[str] = None):
    with SEARCH_LAT.labels("clause").time():
        recs = index().get_clause(ident, profile)
    if not recs:
        raise HTTPException(404, f"clause {ident!r} not found")
    return {"clause": ident, "chunks": recs}


class RelatedReq(BaseModel):
    clause_id: str
    profile: Optional[str] = None


@app.post("/v1/related")
def related(req: RelatedReq):
    with SEARCH_LAT.labels("related").time():
        return {"clause_id": req.clause_id, "related": index().related(req.clause_id, req.profile)}


class SupportPair(BaseModel):
    claim: str = Field(max_length=2000)
    passage: str = Field(max_length=8000)
    allowed_numbers: list[str] = Field(default_factory=list)


class SupportReq(BaseModel):
    pairs: list[SupportPair] = Field(max_length=64)


@app.post("/v1/support")
def support(req: SupportReq):
    with SEARCH_LAT.labels("support").time():
        return {"scores": index().support([p.model_dump() for p in req.pairs])}


class SimReq(BaseModel):
    query: str
    texts: list[str] = Field(max_length=64)


@app.post("/v1/similarity")
def similarity(req: SimReq):
    with SEARCH_LAT.labels("similarity").time():
        return {"scores": index().similarity(req.query, req.texts)}


@app.get("/v1/catalogue")
def catalogue(profile: Optional[str] = None, doc_ids: str = "RBI-CIRC-2017,FED-CPP-2025"):
    """Compact table of contents (clause id + opening words) the planner may use with get_clause."""
    prof = index().profile(profile)
    wanted = set(doc_ids.split(","))
    out = []
    for cid, ids in prof.by_clause.items():
        c = prof.chunks[ids[0]]
        if c["doc_id"] in wanted and c["kind"] != "preamble":
            words = " ".join(c["text"].split()[:9])
            out.append({"clause_id": cid, "hint": words})
    return {"profile": prof.name, "clauses": out}


@app.get("/v1/index/info")
def info():
    idx = index()
    return {name: {k: v for k, v in p.meta.items()} for name, p in idx.profiles.items()} | {
        "embedding_cache": {"hits": idx.query_cache.hits, "misses": idx.query_cache.misses}
    }


@app.post("/admin/reload", tags=["ops"])
def reload():
    idx = index()
    with _lock:
        idx.load()
        _publish_meta()
    log(logger, logging.INFO, "index reloaded", profiles=list(idx.profiles))
    return {"reloaded": list(idx.profiles)}
