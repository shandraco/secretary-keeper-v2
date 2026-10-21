"""See the whole flow on sample policies with stand-in models. No GPU, database or network needed.

    python -m app.demo ../samples --out demo.xlsx
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path

from .answer import render_answer
from .config import load_settings
from .documents import parse_policy
from .excel import build_workbook
from .pipeline import execute_run
from .services import build_services


async def run_demo(samples: Path, out: Path) -> tuple[dict, str]:
    settings = replace(load_settings(), demo_mode=True, samples_dir=str(samples))
    svc = await build_services(settings)
    upload = samples / "changes" / "POL-007-v4.md"
    doc = parse_policy(upload.name, upload.read_bytes(), settings.policy_id_pattern)
    upload_id = await svc.store.save_upload(upload.name, doc.body)
    run_id = await svc.store.create_run("POL-007", "Safety", upload_id, "demo")
    await execute_run(svc, run_id)
    run = await svc.store.get_run(run_id)
    if run["status"] != "done":
        raise SystemExit(f"Demo run failed: {run['error']}")
    out.write_bytes(build_workbook(run["result"]))
    return run["result"], render_answer(run["result"], out.resolve().as_uri())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path, nargs="?", default=Path("../samples"))
    parser.add_argument("--out", type=Path, default=Path("demo.xlsx"))
    args = parser.parse_args()
    _, answer = asyncio.run(run_demo(args.samples, args.out))
    print(answer)
    print(f"\nExcel written to {args.out.resolve()}")


if __name__ == "__main__":
    main()
