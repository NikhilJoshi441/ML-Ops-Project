"""Prompt registry: prompts are versioned YAML artifacts (L2). Every LLM call
records `<id>@<version>#<sha8>` so a response can be traced to the exact text."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    user: str
    max_tokens: int
    sha: str

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}#{self.sha}"

    def render(self, **kw: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": self.system}, {"role": "user", "content": self.user.format(**kw)}]


@lru_cache
def _all() -> dict[tuple[str, str], Prompt]:
    out = {}
    for f in sorted(PROMPT_DIR.glob("*.yaml")):
        d = yaml.safe_load(f.read_text())
        sha = hashlib.sha256((d["system"] + "\x00" + d["user"]).encode()).hexdigest()[:8]
        out[(d["id"], d["version"])] = Prompt(d["id"], d["version"], d["system"], d["user"], int(d.get("max_tokens", 700)), sha)
    return out


def get(prompt_id: str, version: str | None = None) -> Prompt:
    prompts = _all()
    if version and (prompt_id, version) in prompts:
        return prompts[(prompt_id, version)]
    versions = sorted(v for (i, v) in prompts if i == prompt_id)
    if not versions:
        raise KeyError(prompt_id)
    return prompts[(prompt_id, versions[-1])]


def catalogue() -> list[dict[str, str]]:
    return [{"id": p.id, "version": p.version, "sha": p.sha} for p in _all().values()]
