#!/usr/bin/env python3
"""Assign this server's GPUs to the model services this stack runs itself.

Granite 4.2 30B is not one of them: it already runs on your own vLLM server and is reached through
granite-gateway (docs/setup.md). This script places the embedding and reranker models, and the
optional extract, escalate, judge-tuned, judge-candidate and lora-trainer services.

compose.yaml deliberately names no GPUs, because every cluster is different. Run this once on the
GPU server, after .env exists (docs/setup.md step 3). It:

  1. lists the GPUs that nvidia-smi sees,
  2. asks which GPU numbers each model service should use (or takes them as flags),
  3. checks the plan (GPUs exist, big models don't share a GPU, sizes vLLM accepts),
  4. writes compose.gpu.yaml and sets COMPOSE_FILE, ESCALATE_TP, TUNED_TP and CANDIDATE_TP in .env.

After that, plain `docker compose ...` commands use the GPUs you chose. Run it again any time the
hardware changes. To do the same by hand instead, copy compose.gpu.example.yaml.

Examples:
  python3 scripts/setup_gpus.py                                  # asks questions
  python3 scripts/setup_gpus.py --embed 0 --rerank 0             # no questions
  python3 scripts/setup_gpus.py --embed 0 --rerank 0 --extract 1 --escalate 4,5,6,7
  python3 scripts/setup_gpus.py --embed 0 --rerank 0 --dry-run   # show the result, write nothing
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERRIDE = ROOT / "compose.gpu.yaml"
ENV = ROOT / ".env"

# name: (what it is, served by vLLM?, required?)
SERVICES = {
    "embed": ("embedding model, small. Can share a GPU with rerank.", False, True),
    "rerank": ("reranker model, small. Can share a GPU with embed.", False, True),
    "extract": ("gpt-oss-20b, optional, finds references during ingest. About 24 GB.", True, False),
    "escalate": ("Inkling-Small, optional second opinion. See its model card for size.", True, False),
    "judge-tuned": ("fine-tuned judge, optional, after training passes evaluation. Size of its base model.", True, False),
    "judge-candidate": ("model being tested after training, optional, only while evaluating.", True, False),
    "lora-trainer": ("training, optional, only while training. 20b needs about 80 GB.", False, False),
}
VLLM_TP_SIZES = {1, 2, 4, 8}


def detect_gpus() -> dict[int, tuple[str, int]]:
    """Return {index: (name, memory MiB)} from nvidia-smi, or {} if it isn't available."""
    if not shutil.which("nvidia-smi"):
        return {}
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=False,
    )
    gpus = {}
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].isdigit():
            gpus[int(parts[0])] = (parts[1], int(float(parts[2])))
    return gpus


def parse_ids(text: str) -> list[int]:
    text = (text or "").strip()
    if not text:
        return []
    ids = []
    for part in text.replace(" ", ",").split(","):
        if part:
            if not part.isdigit():
                raise ValueError(f"'{part}' is not a GPU number")
            ids.append(int(part))
    if len(set(ids)) != len(ids):
        raise ValueError("the same GPU is listed twice")
    return ids


def ask(name: str, about: str, required: bool) -> list[int]:
    hint = "e.g. 0 or 0,1" if required else "leave blank to skip"
    while True:
        reply = input(f"  {name:8} {about}\n           GPU numbers ({hint}): ")
        try:
            ids = parse_ids(reply)
        except ValueError as exc:
            print(f"           {exc}. Try again.")
            continue
        if required and not ids:
            print("           This one is required. Try again.")
            continue
        return ids


def check(plan: dict[str, list[int]], gpus: dict[int, tuple[str, int]]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    for name, (_, _, required) in SERVICES.items():
        if required and not plan.get(name):
            errors.append(f"{name}: no GPU given (required)")
    for name, ids in plan.items():
        if gpus:
            missing = [i for i in ids if i not in gpus]
            if missing:
                errors.append(f"{name}: GPU {', '.join(map(str, missing))} not found by nvidia-smi")
        if SERVICES[name][1] and ids and len(ids) not in VLLM_TP_SIZES:
            errors.append(f"{name}: uses {len(ids)} GPUs; vLLM needs 1, 2, 4 or 8")

    vllm = [n for n in plan if SERVICES[n][1] and plan[n]]
    for i, a in enumerate(vllm):
        for b in vllm[i + 1:]:
            shared = sorted(set(plan[a]) & set(plan[b]))
            if shared:
                errors.append(f"{a} and {b} both use GPU {shared}; each vLLM model claims most of its GPU's memory")
    trainer = set(plan.get("lora-trainer") or [])
    for big in vllm:
        if trainer & set(plan[big]):
            warnings.append(f"lora-trainer shares GPUs with {big}; stop {big} while training.")
    for small in ("embed", "rerank"):
        for big in vllm:
            shared = sorted(set(plan.get(small, [])) & set(plan[big]))
            if shared:
                warnings.append(f"{small} shares GPU {shared} with {big}; it may not fit. "
                                f"If it fails to start, give {small} its own GPU.")
    for small in ("embed", "rerank"):
        if len(plan.get(small, [])) > 1:
            warnings.append(f"{small} only uses one GPU; extra GPUs listed for it are wasted")

    if plan.get("escalate"):
        warnings.append("escalate: check Inkling-Small's model card and the vLLM Inkling recipe for how many "
                        "GPUs it needs on your hardware, and its exact folder name (ESCALATE_MODEL_DIR).")
    return errors, warnings


def render_override(plan: dict[str, list[int]]) -> str:
    lines = [
        "# Written by scripts/setup_gpus.py for this server. Not committed; rerun the script to change it.",
        "services:",
    ]
    for name, ids in plan.items():
        if not ids:
            continue
        quoted = ", ".join(f'"{i}"' for i in ids)
        lines += [
            f"  {name}:",
            "    deploy:",
            "      resources:",
            "        reservations:",
            f"          devices: [{{driver: nvidia, device_ids: [{quoted}], capabilities: [gpu]}}]",
        ]
    return "\n".join(lines) + "\n"


def update_env(values: dict[str, str]) -> None:
    lines = ENV.read_text().splitlines()
    seen = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in values and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    missing = [k for k in values if k not in seen]
    if missing:
        lines += ["", "# --- GPUs (written by scripts/setup_gpus.py) ---"]
        lines += [f"{k}={values[k]}" for k in missing]
    ENV.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, (about, _, _) in SERVICES.items():
        parser.add_argument(f"--{name}", dest=name.replace("-", "_"), metavar="IDS", help=about)
    parser.add_argument("--dry-run", action="store_true", help="show what would be written, change nothing")
    parser.add_argument("--judge", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.judge is not None:
        print("Error: there is no 'judge' service to place. Granite runs on your own vLLM server "
              "(docs/setup.md, Granite server requirements).", file=sys.stderr)
        return 1

    if not ENV.exists():
        print("No .env yet. Do docs/setup.md step 3 first (cp .env.example .env).", file=sys.stderr)
        return 1

    gpus = detect_gpus()
    if gpus:
        print("GPUs on this server:")
        for i, (name, mib) in sorted(gpus.items()):
            print(f"  {i}: {name}, {mib // 1024} GB")
    else:
        print("nvidia-smi not found or listed no GPUs; GPU numbers can't be checked on this machine.")

    given = {n: getattr(args, n.replace("-", "_")) for n in SERVICES if getattr(args, n.replace("-", "_")) is not None}
    plan: dict[str, list[int]] = {}
    if given:
        try:
            plan = {n: parse_ids(v) for n, v in given.items()}
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    else:
        print("\nWhich GPUs should each service use?")
        for name, (about, _, required) in SERVICES.items():
            plan[name] = ask(name, about, required)

    errors, warnings = check(plan, gpus)
    for w in warnings:
        print(f"Note: {w}")
    if errors:
        print("\nNot written, because:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    override = render_override(plan)
    env_values = {
        "COMPOSE_FILE": "compose.yaml:compose.gpu.yaml",
        "ESCALATE_TP": str(len(plan.get("escalate") or [1])),
        "TUNED_TP": str(len(plan.get("judge-tuned") or [1])),
        "CANDIDATE_TP": str(len(plan.get("judge-candidate") or [1])),
    }
    if args.dry_run:
        print(f"\n--- would write {OVERRIDE.name} ---\n{override}--- would set in .env ---")
        for k, v in env_values.items():
            print(f"{k}={v}")
        return 0

    OVERRIDE.write_text(override)
    update_env(env_values)
    print(f"\nWrote {OVERRIDE.name} and set {', '.join(env_values)} in .env.")
    print("Check it with:  docker compose config --quiet   (prints nothing when all is well)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
