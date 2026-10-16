"""RAG-triad style quality metrics, computed the same way offline (eval CLI)
and online (sampled live runs). TruLens-equivalent, implemented locally with
the retriever's embedding model so no paid judge model is needed.

Definitions (per run):
* groundedness      share of answer claims supported by at least one passage
                    retrieved during the run (support = max sentence cosine
                    >= tau, or >= tau-0.1 with content-token recall >= 0.5,
                    and every number in the claim present in the passage)
* faithfulness      share of claims supported by the passages they cite, with
                    every citation resolving to retrieved text (stricter than
                    groundedness: right source, not just some source)
* context_relevance mean cosine similarity between the case description and
                    each distinct passage retrieved (how on-topic retrieval was)
* hallucinated      1 if any claim is unsupported by all retrieved passages or
                    any citation does not resolve; hallucination rate = mean
Runs with no claims score None for the per-claim metrics.
"""
from __future__ import annotations

from typing import Any

import httpx

from triage.common.http import post_json

TAU = 0.55


def _supported(sc: dict[str, Any], tau: float = TAU) -> bool:
    return (sc["cosine"] >= tau or (sc["cosine"] >= tau - 0.1 and sc["token_recall"] >= 0.5)) and sc["numbers_consistent"]


def score_run(http: httpx.Client, retriever_url: str, case: str, claims: list[dict[str, Any]],
              passages: dict[str, str], allowed_numbers: list[str] | None = None) -> dict[str, Any]:
    allowed = allowed_numbers or []
    texts = list(passages.values())
    out: dict[str, Any] = {"n_claims": len(claims), "n_passages": len(texts)}
    if texts:
        sims = post_json(http, f"{retriever_url}/v1/similarity", {"query": case[:2000], "texts": [t[:2000] for t in texts[:64]]},
                         timeout_s=20)["scores"]
        out["context_relevance"] = round(sum(sims) / len(sims), 4)
    else:
        out["context_relevance"] = 0.0
    if not claims:
        out.update({"groundedness": None, "faithfulness": None, "hallucinated": 0})
        return out
    grounded = faithful = 0
    unresolved = False
    for cl in claims:
        cites = [c for c in cl.get("citations", [])]
        if any(c not in passages for c in cites):
            unresolved = True
        cited_text = " ".join(passages[c] for c in cites if c in passages)
        pairs = [{"claim": cl["text"][:1500], "passage": p[:7900], "allowed_numbers": allowed} for p in texts[:40]]
        if cited_text:
            pairs.append({"claim": cl["text"][:1500], "passage": cited_text[:7900], "allowed_numbers": allowed})
        scores = post_json(http, f"{retriever_url}/v1/support", {"pairs": pairs}, timeout_s=30)["scores"] if pairs else []
        any_ok = any(_supported(s) for s in scores[: len(texts[:40])])
        cited_ok = bool(cited_text) and _supported(scores[-1]) and all(c in passages for c in cites)
        grounded += any_ok or cited_ok
        faithful += cited_ok
    n = len(claims)
    out["groundedness"] = round(grounded / n, 4)
    out["faithfulness"] = round(faithful / n, 4)
    out["hallucinated"] = int(unresolved or grounded < n)
    return out
