"""Decide whether the fine-tuned judge may replace the current one.

    python compare_eval.py base.json candidate.json

Both files are the output of `python -m app.evaluate` (one run per model, same cases).
Rule: the candidate's mean recall must not be lower AND its mean precision must be higher.
Exit code 0 = switch, 1 = do not switch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("errors"):
        raise SystemExit(f"{path}: {data['errors']} case(s) failed to run. Fix that and run the evaluation again.")
    return data


def decide(base: dict, cand: dict) -> tuple[bool, str]:
    recall_ok = cand["mean_recall"] >= base["mean_recall"]
    precision_ok = cand["mean_precision"] > base["mean_precision"]
    lines = [
        f"                 current    new",
        f"  recall         {base['mean_recall']:.3f}      {cand['mean_recall']:.3f}   {'ok' if recall_ok else 'WORSE'}",
        f"  precision      {base['mean_precision']:.3f}      {cand['mean_precision']:.3f}   {'ok' if precision_ok else 'NOT BETTER'}",
    ]
    ok = recall_ok and precision_ok
    lines.append("RESULT: the new model is better. You may switch." if ok else "RESULT: keep the current model.")
    return ok, "\n".join(lines)


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    ok, text = decide(load(sys.argv[1]), load(sys.argv[2]))
    print(text)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
