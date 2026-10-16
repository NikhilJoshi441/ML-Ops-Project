"""Idempotent corpus pipeline: read -> parse -> chunk -> link -> embed -> index (D2, L1).

Usage (inside the `ingest` container, or locally):

    python -m triage.ingest.pipeline build            # all profiles, incremental
    python -m triage.ingest.pipeline build --profile clause
    python -m triage.ingest.pipeline build --reembed --doc FED-CPP-2025
    python -m triage.ingest.pipeline status

Idempotency: every chunk has a deterministic id (its clause id) and a content
hash (sha256 of the embedded text + embedding model id). A build only embeds
chunks whose hash changed, upserts them, and deletes chunks that disappeared.
Running `build` twice in a row embeds nothing the second time.

Re-embedding without a rebuild: `--reembed --doc X` recomputes embeddings for
one document only; changing EMBEDDING_MODEL writes a new collection (the model
slug is part of the collection name) so the old index keeps serving until the
retriever is pointed at the new one.

Freshness: index_meta.<profile>.json records build time, corpus versions and
file hashes, chunk counts and throughput; the retriever exposes them as
Prometheus metrics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from triage.common.logging import log, setup_logging
from triage.config import get_settings
from triage.ingest.parsers import read_document
from triage.ingest.segment import Chunk, Segmenter, naive_chunks

logger = logging.getLogger("ingest")
PROFILES = ("clause", "naive")


def model_slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[-40:] or "default"


def collection_name(profile: str, model: str) -> str:
    return f"disputes-{profile}-{model_slug(model)}"[:63]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def load_manifest(corpus_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((corpus_dir / "manifest.yaml").read_text())


# --------------------------------------------------------------------------
# cross references and defined terms
# --------------------------------------------------------------------------
REF_PATTERNS = [
    (re.compile(r"paragraphs?\s+(\d{1,2})\s*\(\s*([ivx]+)\s*\)", re.I), lambda m: [f"{m.group(1)}({m.group(2).lower()})"]),
    (re.compile(r"paragraphs\s+(\d{1,2})\s+to\s+(\d{1,2})", re.I), lambda m: [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]),
    (re.compile(r"paragraph\s+(\d{1,2})\b(?!\s*\()", re.I), lambda m: [m.group(1)]),
    (re.compile(r"\bTable\s+(\d)\b"), lambda m: [f"Table-{m.group(1)}"]),
    (re.compile(r"clauses?\s+(\d{1,2})((?:\s*\(\w{1,4}\))*)", re.I), lambda m: [m.group(1) + re.sub(r"\s+", "", m.group(2) or "")]),
]


def extract_refs(text: str) -> list[str]:
    refs: list[str] = []
    for rx, fn in REF_PATTERNS:
        for m in rx.finditer(text):
            refs.extend(fn(m))
    return list(dict.fromkeys(refs))


def resolve_refs(doc_id: str, refs: list[str], clause_ids: set[str], self_id: str) -> list[str]:
    out: list[str] = []
    for r in refs:
        cand = f"{doc_id}:{r}"
        hits = [c for c in clause_ids if c == cand] or [c for c in clause_ids if c.startswith(cand + "(") or c.startswith(cand + ".")]
        if not hits and "(" in r:  # e.g. clause 14(4)(a) -> 14(4)
            base = r
            while "(" in base and not hits:
                base = base[: base.rfind("(")]
                hits = [c for c in clause_ids if c == f"{doc_id}:{base}"]
        out.extend(h for h in hits if h != self_id)
    return list(dict.fromkeys(out))


def build_relations(manifest: dict[str, Any], chunks: list[Chunk]) -> dict[str, Any]:
    """Clause graph: explicit refs, defined-term sources, curated cross-document relations."""
    clause_ids = {c.clause_id for c in chunks}
    terms_cfg = manifest.get("terms", {})
    graph: dict[str, Any] = {"refs": {}, "terms": {}, "term_sources": {}, "relations": []}
    for t, cfg in terms_cfg.items():
        graph["term_sources"][t] = []
    for c in chunks:
        refs = resolve_refs(c.doc_id, extract_refs(c.text), clause_ids, c.clause_id)
        if refs:
            graph["refs"].setdefault(c.clause_id, [])
            graph["refs"][c.clause_id] = list(dict.fromkeys(graph["refs"][c.clause_id] + refs))
        used = [t for t, cfg in terms_cfg.items() if re.search(cfg["match"], c.text, re.I)]
        if used:
            graph["terms"].setdefault(c.clause_id, [])
            graph["terms"][c.clause_id] = list(dict.fromkeys(graph["terms"][c.clause_id] + used))
        for t, cfg in terms_cfg.items():
            if re.search(cfg["defined_by"], c.text, re.I) and c.clause_id not in graph["term_sources"][t]:
                graph["term_sources"][t].append(c.clause_id)
    for rel in manifest.get("relations", []):
        if rel["source"] in clause_ids and rel["target"] in clause_ids:
            graph["relations"].append(rel)
        else:
            log(logger, logging.WARNING, "relation endpoint not found in corpus", relation=rel)
    return graph


# --------------------------------------------------------------------------
# embeddings and vector store (LangChain + Chroma)
# --------------------------------------------------------------------------
def make_embeddings(model: str):
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model_name=model, encode_kwargs={"normalize_embeddings": True})


def open_store(index_dir: Path, profile: str, model: str, embeddings):
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    from langchain_chroma import Chroma

    client = chromadb.PersistentClient(path=str(index_dir / "chroma"), settings=ChromaSettings(anonymized_telemetry=False))
    return Chroma(
        client=client,
        collection_name=collection_name(profile, model),
        embedding_function=embeddings,
        collection_metadata={"hnsw:space": "cosine"},
    )


def chunk_corpus(corpus_dir: Path, manifest: dict[str, Any], profile: str) -> tuple[list[Chunk], list[dict[str, Any]]]:
    chunks: list[Chunk] = []
    versions: list[dict[str, Any]] = []
    for d in manifest["documents"]:
        path = corpus_dir / d["file"]
        lines = read_document(path, d["format"], d.get("start_marker"), d.get("end_marker"))
        if profile == "clause":
            style = "dotted" if d["chunk_depth"] >= 3 else "paren"
            doc_chunks = Segmenter(d["doc_id"], d["chunk_depth"], d["label_prefix"], d["short"], style).chunks(lines)
        else:
            doc_chunks = naive_chunks(d["doc_id"], d["short"], lines)
        chunks.extend(doc_chunks)
        versions.append(
            {"doc_id": d["doc_id"], "version": d["version"], "sha256": sha256_file(path), "source_url": d["source_url"],
             "title": d["title"], "chunks": len(doc_chunks)}
        )
    return chunks, versions


def build(profile: str, reembed_docs: set[str] | None = None, force: bool = False) -> dict[str, Any]:
    s = get_settings()
    if not s.embedding_model:
        raise SystemExit("EMBEDDING_MODEL is not set (see .env.example)")
    corpus_dir, index_dir = Path(s.corpus_dir), Path(s.index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(corpus_dir)
    t0 = time.perf_counter()

    chunks, versions = chunk_corpus(corpus_dir, manifest, profile)
    t_parse = time.perf_counter() - t0
    embeddings = make_embeddings(s.embedding_model)
    store = open_store(index_dir, profile, s.embedding_model, embeddings)

    existing = store.get(include=["metadatas"])
    existing_hash = {i: (m or {}).get("content_hash") for i, m in zip(existing["ids"], existing["metadatas"])}
    wanted: dict[str, Chunk] = {}
    for c in chunks:
        wanted[c.chunk_id] = c

    def chash(c: Chunk) -> str:
        return hashlib.sha256((s.embedding_model + "\x00" + c.context_text).encode()).hexdigest()[:32]

    to_upsert = [
        c for c in chunks
        if force or existing_hash.get(c.chunk_id) != chash(c) or (reembed_docs and c.doc_id in reembed_docs)
    ]
    to_delete = [i for i in existing_hash if i not in wanted]
    unchanged = len(chunks) - len(to_upsert)

    t1 = time.perf_counter()
    if to_delete:
        store.delete(ids=to_delete)
    if to_upsert:
        # embed the contextual text; keep the verbatim clause text in metadata for citation
        metadatas = [
            {
                "doc_id": c.doc_id, "clause_id": c.clause_id, "label": c.label, "heading": c.heading,
                "kind": c.kind, "part": c.part, "order": c.order, "content_hash": chash(c), "text": c.text,
            }
            for c in to_upsert
        ]
        store.add_texts(texts=[c.context_text for c in to_upsert], metadatas=metadatas, ids=[c.chunk_id for c in to_upsert])
    t_embed = time.perf_counter() - t1

    relations = build_relations(manifest, chunks) if profile == "clause" else {"refs": {}, "terms": {}, "term_sources": {}, "relations": []}
    (index_dir / f"relations.{profile}.json").write_text(json.dumps(relations, indent=1))
    (index_dir / f"chunks.{profile}.jsonl").write_text("\n".join(json.dumps(asdict(c)) for c in chunks))

    total = time.perf_counter() - t0
    meta = {
        "profile": profile,
        "collection": collection_name(profile, s.embedding_model),
        "embedding_model": s.embedding_model,
        "built_at": time.time(),
        "built_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "documents": versions,
        "chunks_total": len(chunks),
        "chunks_embedded": len(to_upsert),
        "chunks_deleted": len(to_delete),
        "chunks_unchanged": unchanged,
        "duration_s": round(total, 3),
        "parse_s": round(t_parse, 3),
        "embed_s": round(t_embed, 3),
        "throughput_chunks_per_s": round(len(chunks) / total, 2) if total else None,
        "embed_throughput_chunks_per_s": round(len(to_upsert) / t_embed, 2) if to_upsert and t_embed else None,
    }
    (index_dir / f"index_meta.{profile}.json").write_text(json.dumps(meta, indent=1))
    log(logger, logging.INFO, "index build complete", **{k: v for k, v in meta.items() if k != "documents"})
    return meta


def status() -> None:
    s = get_settings()
    for p in PROFILES:
        f = Path(s.index_dir) / f"index_meta.{p}.json"
        print(f.read_text() if f.exists() else f"{p}: not built")


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    setup_logging("ingest", s.log_level)
    ap = argparse.ArgumentParser(prog="triage-ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--profile", choices=[*PROFILES, "all"], default="all")
    b.add_argument("--reembed", action="store_true", help="recompute embeddings for --doc documents")
    b.add_argument("--doc", action="append", default=[])
    b.add_argument("--force", action="store_true", help="re-embed everything")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        status()
        return 0
    profiles = PROFILES if a.profile == "all" else (a.profile,)
    for p in profiles:
        meta = build(p, set(a.doc) if a.reembed else None, a.force)
        print(json.dumps({k: v for k, v in meta.items() if k != "documents"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
