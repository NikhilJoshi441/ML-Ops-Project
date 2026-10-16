"""Read side of the index: hybrid retrieval (dense via LangChain/Chroma + BM25),
clause lookup, clause-graph expansion and sentence-level support scoring."""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from triage.ingest.pipeline import PROFILES, make_embeddings, open_store

TOKEN = re.compile(r"[a-z0-9]+")
STOP = set(
    "a an the of to in on for and or by with is are be was were as at from that this which it its their his her he she "
    "shall will may any such where when if not no than whichever under per".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN.findall(text.lower()) if t not in STOP]


@dataclass
class Profile:
    name: str
    store: Any
    chunks: dict[str, dict[str, Any]]      # chunk_id -> chunk record
    by_clause: dict[str, list[str]]        # clause_id -> chunk ids (ordered)
    bm25: Any
    bm25_ids: list[str]
    graph: dict[str, Any]
    meta: dict[str, Any]


class LRU:
    def __init__(self, size: int = 2048):
        self.size, self.d, self.lock = size, OrderedDict(), threading.Lock()
        self.hits = self.misses = 0

    def get(self, k):
        with self.lock:
            if k in self.d:
                self.d.move_to_end(k)
                self.hits += 1
                return self.d[k]
            self.misses += 1
            return None

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            if len(self.d) > self.size:
                self.d.popitem(last=False)


class CorpusIndex:
    def __init__(self, index_dir: str, embedding_model: str):
        self.index_dir = Path(index_dir)
        self.model = embedding_model
        self.embeddings = make_embeddings(embedding_model)
        self.profiles: dict[str, Profile] = {}
        self.query_cache = LRU(4096)   # embedding cache for queries and claims
        self.load()

    # ---------------------------------------------------------------- load
    def load(self) -> None:
        from rank_bm25 import BM25Okapi

        loaded: dict[str, Profile] = {}
        for p in PROFILES:
            meta_f = self.index_dir / f"index_meta.{p}.json"
            chunks_f = self.index_dir / f"chunks.{p}.jsonl"
            if not meta_f.exists() or not chunks_f.exists():
                continue
            meta = json.loads(meta_f.read_text())
            if meta.get("embedding_model") != self.model:
                continue
            chunks = {}
            by_clause: dict[str, list[str]] = {}
            for line in chunks_f.read_text().splitlines():
                c = json.loads(line)
                chunks[c["chunk_id"]] = c
                by_clause.setdefault(c["clause_id"], []).append(c["chunk_id"])
            ids = list(chunks)
            bm25 = BM25Okapi([tokenize(chunks[i]["context_text"]) for i in ids])
            graph_f = self.index_dir / f"relations.{p}.json"
            graph = json.loads(graph_f.read_text()) if graph_f.exists() else {}
            store = open_store(self.index_dir, p, self.model, self.embeddings)
            loaded[p] = Profile(p, store, chunks, by_clause, bm25, ids, graph, meta)
        self.profiles = loaded

    def ready(self) -> bool:
        return bool(self.profiles)

    def profile(self, name: str | None) -> Profile:
        if name and name in self.profiles:
            return self.profiles[name]
        if "clause" in self.profiles:
            return self.profiles["clause"]
        return next(iter(self.profiles.values()))

    # ----------------------------------------------------------- embeddings
    def embed(self, texts: list[str]) -> np.ndarray:
        out: list[Any] = [None] * len(texts)
        missing = []
        for i, t in enumerate(texts):
            v = self.query_cache.get(t)
            if v is None:
                missing.append(i)
            else:
                out[i] = v
        if missing:
            vecs = self.embeddings.embed_documents([texts[i] for i in missing])
            for i, v in zip(missing, vecs):
                arr = np.asarray(v, dtype=np.float32)
                self.query_cache.put(texts[i], arr)
                out[i] = arr
        return np.vstack(out) if out else np.zeros((0, 384), dtype=np.float32)

    # --------------------------------------------------------------- search
    def _record(self, prof: Profile, cid: str, **scores: Any) -> dict[str, Any]:
        c = prof.chunks[cid]
        doc = next((d for d in prof.meta.get("documents", []) if d["doc_id"] == c["doc_id"]), {})
        return {
            "chunk_id": cid, "clause_id": c["clause_id"], "doc_id": c["doc_id"], "label": c["label"],
            "heading": c["heading"], "kind": c["kind"], "text": c["text"], "source_url": doc.get("source_url", ""),
            "doc_version": doc.get("version", ""), **scores,
        }

    def search(self, query: str, k: int = 5, profile: str | None = None, doc_ids: list[str] | None = None,
               mode: str = "hybrid") -> dict[str, Any]:
        prof = self.profile(profile)
        k = max(1, min(k, 12))
        pool = 25
        dense: list[tuple[str, float]] = []
        if mode in ("hybrid", "dense"):
            qv = self.embed([query])[0].tolist()
            flt = {"doc_id": {"$in": doc_ids}} if doc_ids else None
            res = prof.store.similarity_search_by_vector_with_relevance_scores(qv, k=pool, filter=flt) \
                if hasattr(prof.store, "similarity_search_by_vector_with_relevance_scores") else \
                prof.store.similarity_search_with_relevance_scores(query, k=pool, filter=flt)
            for doc, score in res:
                cid = doc.id if getattr(doc, "id", None) else None
                if cid is None:
                    md = doc.metadata
                    cid = next((i for i in prof.by_clause.get(md["clause_id"], []) if prof.chunks[i]["part"] == md.get("part", 1)), None)
                if cid in prof.chunks:
                    dense.append((cid, float(score)))
        sparse: list[tuple[str, float]] = []
        if mode in ("hybrid", "bm25"):
            scores = prof.bm25.get_scores(tokenize(query))
            order = np.argsort(-scores)[:pool]
            for i in order:
                cid = prof.bm25_ids[int(i)]
                if doc_ids and prof.chunks[cid]["doc_id"] not in doc_ids:
                    continue
                if scores[i] > 0:
                    sparse.append((cid, float(scores[i])))
        # reciprocal rank fusion
        fused: dict[str, float] = {}
        for rank, (cid, _) in enumerate(dense):
            fused[cid] = fused.get(cid, 0) + 1 / (60 + rank)
        for rank, (cid, _) in enumerate(sparse):
            fused[cid] = fused.get(cid, 0) + 1 / (60 + rank)
        dmap, smap = dict(dense), dict(sparse)
        top = sorted(fused.items(), key=lambda kv: -kv[1])[:k]
        hits = [
            self._record(prof, cid, score=round(sc, 5), dense_score=round(dmap.get(cid, 0.0), 4),
                         bm25_score=round(smap.get(cid, 0.0), 3))
            for cid, sc in top
        ]
        return {
            "query": query, "profile": prof.name, "mode": mode, "hits": hits,
            "max_dense_score": round(max(dmap.values()), 4) if dmap else 0.0,
            "index_built_at": prof.meta.get("built_at"),
        }

    # --------------------------------------------------------------- lookup
    def get_clause(self, ident: str, profile: str | None = None) -> list[dict[str, Any]]:
        prof = self.profile(profile)
        if ident in prof.chunks:
            return [self._record(prof, ident)]
        ids = prof.by_clause.get(ident)
        if not ids:  # tolerate "7(i)" without doc prefix or minor spacing differences
            norm = re.sub(r"\s+", "", ident)
            ids = next((v for k2, v in prof.by_clause.items() if re.sub(r"\s+", "", k2) == norm or k2.endswith(":" + norm)), None)
        return [self._record(prof, i) for i in (ids or [])]

    def related(self, clause_id: str, profile: str | None = None) -> list[dict[str, Any]]:
        """Provisions that may modify, define, or be delegated to by `clause_id`."""
        prof = self.profile(profile)
        g = prof.graph or {}
        if not g:
            return []
        if clause_id in prof.chunks:
            clause_id = prof.chunks[clause_id]["clause_id"]
        out: list[tuple[str, str]] = []
        for r in g.get("refs", {}).get(clause_id, []):
            out.append((r, "cross_reference"))
        for src, refs in g.get("refs", {}).items():
            if clause_id in refs:
                out.append((src, "referenced_by"))
        for rel in g.get("relations", []):
            if rel["source"] == clause_id:
                out.append((rel["target"], rel["kind"]))
            if rel["target"] == clause_id:
                out.append((rel["source"], f"{rel['kind']}_from"))
        for term in g.get("terms", {}).get(clause_id, []):
            for src in g.get("term_sources", {}).get(term, []):
                if src != clause_id:
                    out.append((src, f"defines:{term}"))
        # lead-in / siblings: "(ii)" is only meaningful with "7." and "(i)"
        m = re.match(r"^(.*?)(\(([ivx]+)\))$", clause_id)
        if m:
            parent = m.group(1).rstrip(".")
            if parent in prof.by_clause:
                out.append((parent, "lead_in"))
            for cid in prof.by_clause:
                if cid != clause_id and re.match(re.escape(parent) + r"\.?\([ivx]+\)$", cid):
                    out.append((cid, "sibling"))
        merged: dict[str, list[str]] = {}
        for cid, rel in out:
            if cid not in prof.by_clause:
                continue
            for chunk_id in prof.by_clause[cid]:
                merged.setdefault(chunk_id, [])
                if rel not in merged[chunk_id]:
                    merged[chunk_id].append(rel)
        # strongest relations first: delegation/implementation, then structure, then definitions
        def rank(rels: list[str]) -> int:
            r = rels[0]
            return 0 if r.startswith(("delegates_to", "implements")) else 1 if r in ("sibling", "lead_in", "cross_reference") else 2
        ordered = sorted(merged.items(), key=lambda kv: rank(kv[1]))
        return [{**self._record(prof, cid), "relation": ",".join(rels)} for cid, rels in ordered[:14]]

    # ------------------------------------------------------------- support
    def support(self, pairs: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Sentence-level support of claim by passage: max cosine between the claim
        and any passage sentence, plus content-token recall and number agreement."""
        out = []
        for p in pairs:
            claim, passage = p["claim"], p["passage"]
            sents = [s for s in re.split(r"(?<=[.;:])\s+|\s\|\s|•", passage) if len(s.strip()) > 15] or [passage]
            vecs = self.embed([claim] + sents)
            sims = vecs[1:] @ vecs[0]
            best = float(sims.max()) if len(sims) else 0.0
            ct, pt = set(tokenize(claim)), set(tokenize(passage))
            recall = len(ct & pt) / len(ct) if ct else 0.0
            nums_claim = set(_numbers(claim)) - {str(x).lstrip("0") for x in p.get("allowed_numbers", [])}
            nums_ok = nums_claim.issubset(set(_numbers(passage))) if nums_claim else True
            out.append({"cosine": round(best, 4), "token_recall": round(recall, 4), "numbers_consistent": nums_ok})
        return out

    def similarity(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        v = self.embed([query] + texts)
        return [round(float(x), 4) for x in (v[1:] @ v[0])]


NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
            "nine": "9", "ten": "10", "thirty": "30", "ninety": "90"}


def _numbers(text: str) -> list[str]:
    t = text.lower().replace(",", "")
    for w, d in NUMWORDS.items():
        t = re.sub(rf"\b{w}\b", d, t)
    return [n.lstrip("0") or "0" for n in re.findall(r"\b\d+(?:\.\d+)?\b", t) if not re.fullmatch(r"20\d\d|19\d\d", n)]
