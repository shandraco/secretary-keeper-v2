"""Score the pipeline against past head-policy changes with known answers. See docs/evaluation.md.

    python -m app.evaluate /eval/cases.json
    python -m app.evaluate /eval/cases.json --demo   # stand-in models, to check the harness itself
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import load_settings
from .documents import parse_policy
from .pipeline import execute_run
from .services import build_services


def score(found: set[str], expected: set[str]) -> dict:
    hit = found & expected
    return {
        "recall": len(hit) / len(expected) if expected else 1.0,
        "precision": len(hit) / len(found) if found else (1.0 if not expected else 0.0),
        "missed": sorted(expected - found),
        "extra": sorted(found - expected),
    }


async def evaluate(cases_path: Path, demo: bool, include_maybe: bool) -> dict:
    settings = load_settings()
    if demo:
        settings = replace(settings, demo_mode=True)
    svc = await build_services(settings)
    base = cases_path.parent
    report = []
    try:
        for case in json.loads(cases_path.read_text()):
            upload_id = None
            if case.get("new_version_file"):
                path = base / case["new_version_file"]
                doc = parse_policy(path.name, path.read_bytes(), settings.policy_id_pattern)
                upload_id = await svc.store.save_upload(path.name, doc.body)
            run_id = await svc.store.create_run(case["head_policy"], case["subset"], upload_id, "evaluate")
            await execute_run(svc, run_id)
            run = await svc.store.get_run(run_id)
            if run["status"] != "done":
                report.append({"name": case["name"], "error": run["error"]})
                continue
            wanted = {"Yes", "Maybe"} if include_maybe else {"Yes"}
            found = {r["policy_id"] for r in run["result"]["rows"] if r["status"] in wanted}
            report.append({"name": case["name"], **score(found, set(case["expected_impacted"])),
                           "counts": run["result"]["counts"]})
    finally:
        await svc.close()
    ok = [r for r in report if "error" not in r]
    return {
        "cases": report,
        "mean_recall": sum(r["recall"] for r in ok) / len(ok) if ok else 0.0,
        "mean_precision": sum(r["precision"] for r in ok) / len(ok) if ok else 0.0,
        "errors": len(report) - len(ok),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    parser.add_argument("--demo", action="store_true", help="use stand-in models and the sample policies")
    parser.add_argument("--yes-only", action="store_true", help="count only 'Yes' rows as found (default counts Yes and Maybe)")
    parser.add_argument("--min-recall", type=float, default=0.0, help="exit non-zero if mean recall is below this")
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.cases, args.demo, include_maybe=not args.yes_only))
    print(json.dumps(result, indent=2))
    if result["errors"] or result["mean_recall"] < args.min_recall:
        sys.exit(1)


if __name__ == "__main__":
    main()
