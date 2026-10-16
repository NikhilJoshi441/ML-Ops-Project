"""Runtime configuration.

Every environment-specific value (endpoints, credentials, model identifiers,
prices) is injected through environment variables at runtime (D4). Nothing in
this file names a provider, a model or a credential; defaults are either
neutral (timeouts, limits) or point at the service names declared in
docker-compose.yml.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- identity -----------------------------------------------------------
    service_name: str = "triage"
    environment: str = "local"
    log_level: str = "INFO"

    # --- infrastructure endpoints -----------------------------------------
    redis_url: str = "redis://redis:6379/0"
    retriever_url: str = "http://retriever:8081"
    llm_gateway_url: str = "http://llm-gateway:8082"
    otel_exporter_otlp_endpoint: str = ""  # e.g. http://jaeger:4318 ; empty disables export

    # --- LLM provider (gateway only) ------------------------------------
    llm_base_url: str = ""          # OpenAI-compatible base URL of the primary provider
    llm_api_key: str = ""
    llm_model: str = ""             # provider model identifier, injected at runtime only
    llm_fallback_base_url: str = "" # optional secondary provider (degradation path step 2)
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""
    llm_price_prompt_per_1m: float = 0.0      # USD per 1M prompt tokens
    llm_price_completion_per_1m: float = 0.0  # USD per 1M completion tokens
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2                 # retries after the first attempt
    llm_backoff_base_s: float = 0.5
    llm_backoff_max_s: float = 4.0
    llm_cache_ttl_s: int = 3600
    llm_cache_enabled: bool = True
    breaker_failure_threshold: int = 5
    breaker_window_s: int = 60
    breaker_open_s: int = 30

    # --- embeddings / retrieval -------------------------------------------
    embedding_model: str = ""       # sentence-transformers model id, injected at runtime
    index_dir: str = "/data/index"
    corpus_dir: str = "/app/corpus"
    default_retrieval_profile: str = "clause"
    retrieval_cache_ttl_s: int = 3600
    retrieval_cache_enabled: bool = True
    out_of_corpus_threshold: float = 0.30   # max dense similarity below this => outside corpus

    # --- agent loop bounds (A1) -------------------------------------------
    agent_max_steps: int = 8
    agent_max_tool_calls: int = 12
    agent_wall_clock_s: float = 120.0
    agent_max_verification_attempts: int = 2
    tool_timeout_s: float = 10.0
    gateway_call_timeout_s: float = 75.0
    gateway_call_retries: int = 1
    support_threshold: float = 0.55

    # --- prompts ----------------------------------------------------------
    prompt_variant: str = "v2"
    allow_experiment_overrides: bool = False

    # --- queue / workers ------------------------------------------------
    worker_metrics_port: int = 9102
    job_lease_s: int = 60
    job_max_attempts: int = 2
    run_ttl_s: int = 7 * 24 * 3600
    online_eval_sample_rate: float = 0.2

    # --- api --------------------------------------------------------------
    rate_limit_per_minute: int = 30
    rate_limit_burst: int = 10
    max_queue_depth: int = 500

    # --- guardrails -------------------------------------------------------
    injection_threshold: float = 0.5
    toxicity_threshold: float = 0.5

    # --- llm stub ---------------------------------------------------------
    stub_delay_mean_ms: float = 1200.0
    stub_delay_sigma: float = 0.35

    extra_holidays: str = Field(default="", description="comma separated YYYY-MM-DD bank holidays")


@lru_cache
def get_settings() -> Settings:
    return Settings()
