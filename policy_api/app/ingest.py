"""Load policy files into the store: clauses, embeddings and the reference graph.

    python -m app.ingest /policies            # regex references only
    python -m app.ingest /policies --llm-refs # also ask the extract model for references the regex misses
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .documents import SUPPORTED_SUFFIXES, PolicyDoc, find_refs, parse_policy
from .pipeline import Services

log = logging.getLogger(__name__)


async def ingest_doc(svc: Services, doc: PolicyDoc, use_llm: bool = False) -> int:
    pattern = svc.settings.policy_id_pattern
    vectors = await svc.embedder.embed([c.text for c in doc.clauses]) if doc.clauses else []
    refs, seen = [], set()
    for c in doc.clauses:
        found = [(p, n, "regex") for p, n in find_refs(c.text, pattern, doc.id)]
        if use_llm and svc.extractor is not None:
            found += [(p, n, "llm") for p, n in await svc.extractor.refs(c.text) if p != doc.id]
        for p, n, src in found:
            if (c.number, p, n) not in seen:
                seen.add((c.number, p, n))
                refs.append((c.number, p, n, src))
    return await svc.store.upsert_policy(doc, vectors, refs)


async def ingest_dir(svc: Services, folder: Path, use_llm: bool = False) -> list[tuple[str, int]]:
    done, skipped = [], []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
            continue
        try:
            doc = parse_policy(path.name, path.read_bytes(), svc.settings.policy_id_pattern)
        except ValueError as e:
            skipped.append(f"{path.name}: {e}")
            continue
        if not doc.id:
            skipped.append(f"{path.name}: no policy ID in the header, file name or first lines")
            continue
        if not doc.clauses:
            skipped.append(f"{path.name}: no numbered sections found")
            continue
        done.append((doc.id, await ingest_doc(svc, doc, use_llm)))
    for s in skipped:
        log.warning("skipped %s", s)
    return done


async def _main(folder: Path, use_llm: bool) -> int:
    from .config import load_settings
    from .services import build_services

    svc = await build_services(load_settings())
    try:
        done = await ingest_dir(svc, folder, use_llm)
    finally:
        await svc.close()
    for pid, version in done:
        print(f"ingested {pid} v{version}")
    print(f"{len(done)} policy file(s) ingested.")
    return 0 if done else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest policy files.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--llm-refs", action="store_true", help="also use the extract model to find references")
    args = parser.parse_args()
    if not args.folder.is_dir():
        sys.exit(f"Not a folder: {args.folder}")
    sys.exit(asyncio.run(_main(args.folder, args.llm_refs)))


if __name__ == "__main__":
    main()
