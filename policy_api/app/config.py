"""Settings, read from environment variables when the service starts."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str = "") -> str:
    # An empty variable means "use the default": Compose passes unset optional settings as "".
    return (os.environ.get(name) or "").strip() or default


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str
    link_signing_key: str
    public_base_url: str
    allowed_hosts: tuple[str, ...]
    export_dir: str
    llm_api_key: str
    judge_url: str
    judge_model: str
    judge_api_key: str
    judge_thinking: bool
    escalate_url: str
    escalate_model: str
    extract_url: str
    extract_model: str
    embed_url: str
    rerank_url: str
    semantic_k: int
    rerank_keep: int
    judge_concurrency: int
    link_ttl_hours: int
    max_upload_mb: int
    policy_id_pattern: str
    demo_mode: bool
    samples_dir: str
    worker_enabled: bool = True
    worker_concurrency: int = 2
    job_lease_seconds: int = 60
    job_max_attempts: int = 3
    export_retention_days: int = 30
    argilla_url: str = ""
    argilla_api_key: str = ""
    argilla_workspace: str = "policy-review"
    review_sync_seconds: int = 300
    review_sample_no: float = 0.1


def load_settings() -> Settings:
    return Settings(
        database_url=_get("DATABASE_URL"),
        api_key=_get("POLICY_API_KEY"),
        link_signing_key=_get("LINK_SIGNING_KEY"),
        public_base_url=_get("PUBLIC_BASE_URL", "http://localhost:8100").rstrip("/"),
        allowed_hosts=tuple(h.strip() for h in _get("ALLOWED_HOSTS", "localhost,127.0.0.1,policy-api,testserver").split(",") if h.strip()),
        export_dir=_get("EXPORT_DIR", "./exports"),
        llm_api_key=_get("LLM_API_KEY"),
        judge_url=_get("JUDGE_URL", "http://granite-gateway:8000/v1"),
        judge_model=_get("JUDGE_MODEL", "granite-4.2-30b"),
        judge_api_key=_get("JUDGE_API_KEY"),  # empty for Granite: granite-gateway adds its key
        judge_thinking=_get("JUDGE_THINKING", "0").lower() in ("1", "true", "yes"),
        escalate_url=_get("ESCALATE_URL"),
        escalate_model=_get("ESCALATE_MODEL", "escalate"),
        extract_url=_get("EXTRACT_URL"),
        extract_model=_get("EXTRACT_MODEL", "extract"),
        embed_url=_get("EMBED_URL", "http://embed").rstrip("/"),
        rerank_url=_get("RERANK_URL", "http://rerank").rstrip("/"),
        semantic_k=int(_get("SEMANTIC_K", "30")),
        rerank_keep=int(_get("RERANK_KEEP", "20")),
        judge_concurrency=int(_get("JUDGE_CONCURRENCY", "16")),
        link_ttl_hours=int(_get("LINK_TTL_HOURS", "72")),
        max_upload_mb=int(_get("MAX_UPLOAD_MB", "20")),
        policy_id_pattern=_get("POLICY_ID_PATTERN", r"[A-Z]{2,6}-\d{1,5}"),
        demo_mode=_get("DEMO_MODE", "0").lower() in ("1", "true", "yes"),
        samples_dir=_get("SAMPLES_DIR", "../samples"),
        worker_enabled=_get("WORKER_ENABLED", "1").lower() in ("1", "true", "yes"),
        worker_concurrency=int(_get("WORKER_CONCURRENCY", "2")),
        job_lease_seconds=int(_get("JOB_LEASE_SECONDS", "60")),
        job_max_attempts=int(_get("JOB_MAX_ATTEMPTS", "3")),
        export_retention_days=int(_get("EXPORT_RETENTION_DAYS", "30")),
        argilla_url=_get("ARGILLA_URL").rstrip("/"),
        argilla_api_key=_get("ARGILLA_API_KEY"),
        argilla_workspace=_get("ARGILLA_WORKSPACE", "policy-review"),
        review_sync_seconds=int(_get("REVIEW_SYNC_SECONDS", "300")),
        review_sample_no=float(_get("REVIEW_SAMPLE_NO", "0.1")),
    )
