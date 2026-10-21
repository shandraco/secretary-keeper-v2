"""Wire up the store and model clients from settings."""
from __future__ import annotations

import logging
import secrets
from dataclasses import replace
from pathlib import Path

from .config import Settings
from .graph import attach_graph
from .pipeline import Services

log = logging.getLogger(__name__)


async def build_services(settings: Settings) -> Services:
    if settings.demo_mode:
        from .fakes import FakeEmbedder, FakeJudge, FakeReranker
        from .ingest import ingest_dir
        from .store import MemoryStore

        settings = replace(
            settings,
            api_key=settings.api_key or "demo",
            link_signing_key=settings.link_signing_key or secrets.token_hex(32),
        )
        svc = Services(settings, MemoryStore(), FakeEmbedder(), FakeReranker(), FakeJudge())
        policies = Path(settings.samples_dir) / "policies"
        if policies.is_dir():
            await ingest_dir(svc, policies)
        log.warning("DEMO MODE: stand-in models and sample policies. Never use for real decisions.")
        return await attach_graph(svc)

    missing = [name for name, value in (
        ("DATABASE_URL", settings.database_url),
        ("POLICY_API_KEY", settings.api_key),
        ("LINK_SIGNING_KEY", settings.link_signing_key),
    ) if not value]
    if missing:
        raise RuntimeError(f"Missing settings: {', '.join(missing)}. See docs/setup.md.")
    if len(settings.api_key) < 32 or len(settings.link_signing_key) < 32:
        raise RuntimeError("POLICY_API_KEY and LINK_SIGNING_KEY must each be at least 32 characters.")

    from .llm import LLMExtractor, LLMJudge, TEIEmbedder, TEIReranker
    from .pgstore import PgStore

    store = PgStore(settings.database_url)
    await store.open()
    key = settings.llm_api_key
    review = None
    if settings.argilla_url:
        if not settings.argilla_api_key:
            raise RuntimeError("ARGILLA_URL is set but ARGILLA_API_KEY is empty. See docs/review-and-training.md.")
        from .review import ArgillaReview

        review = ArgillaReview(settings.argilla_url, settings.argilla_api_key, settings.argilla_workspace)
    return await attach_graph(Services(
        settings,
        store,
        TEIEmbedder(settings.embed_url),
        TEIReranker(settings.rerank_url),
        LLMJudge(settings.judge_url, settings.judge_api_key, settings.judge_model, {"enable_thinking": settings.judge_thinking}),
        LLMJudge(settings.escalate_url, key, settings.escalate_model) if settings.escalate_url else None,
        LLMExtractor(settings.extract_url, key, settings.extract_model, settings.policy_id_pattern) if settings.extract_url else None,
        review,
    ))
