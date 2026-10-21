"""Turn reviewed judgements into training data (chat-format JSONL), split by changed policy.

    python export_dataset.py --out /out/data
    python export_dataset.py --out /out/data --min-examples 200 --holdout 0.2

Reads DATABASE_URL. Writes train.jsonl, holdout.jsonl and manifest.json into --out.
Refuses to write anything until there are enough reviewed examples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / "policy_api"):      # in the image: ./app ; in the repo: ../policy_api/app
    if (candidate / "app" / "prompts.py").exists():
        sys.path.insert(0, str(candidate))
        break

DECISIONS = ("yes", "no", "unsure")
SPLIT_SEED = "secretary-keeper-split-v1"
FIELDS = ["impacted", "impact_type", "why", "evidence_head", "evidence_candidate", "suggested_edit"]

QUERY = """
SELECT j.id, j.prompt, j.result, j.reviewer_decision, j.reviewer_impact_type, j.reviewer_note,
       j.reviewed_at, r.head_policy
FROM judgements j JOIN runs r ON r.id = j.run_id
WHERE j.reviewer_decision IN ('yes', 'no', 'unsure')
  AND j.prompt IS NOT NULL AND j.prompt <> ''
ORDER BY j.id
"""


def judge_system() -> str:
    from app.prompts import JUDGE_SYSTEM, IMPACT_TYPES  # noqa: F401
    return JUDGE_SYSTEM


def impact_types() -> list[str]:
    from app.prompts import IMPACT_TYPES
    return list(IMPACT_TYPES)


def build_verdict(row: dict, types: list[str]) -> tuple[dict | None, str]:
    """The corrected answer for one reviewed judgement, or (None, reason) if it cannot be used."""
    model = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
    decision = row["reviewer_decision"]
    agreed = model.get("impacted") == decision
    note = (row.get("reviewer_note") or "").strip()
    model_type = model.get("impact_type") if model.get("impact_type") in types else "none"
    picked = row.get("reviewer_impact_type") if row.get("reviewer_impact_type") in types else ""

    if decision == "no":
        impact = "none"
    elif picked and picked != "none":
        impact = picked
    elif model_type != "none":
        impact = model_type
    elif decision == "unsure":
        impact = "none"
    else:
        return None, "said yes without a kind of impact"

    if agreed:
        verdict = {k: str(model.get(k) or "") for k in FIELDS}
        verdict["impacted"], verdict["impact_type"] = decision, impact
        if note:
            verdict["why"] = note
    else:
        fallback = {"yes": "A reviewer confirmed this section is affected by the change.",
                    "no": "A reviewer confirmed this section is not affected by the change.",
                    "unsure": "A reviewer found these two texts are not enough to decide."}[decision]
        verdict = {"impacted": decision, "impact_type": impact, "why": note or fallback,
                   "evidence_head": "", "evidence_candidate": "", "suggested_edit": ""}
    if decision == "no":
        verdict["evidence_head"] = verdict["evidence_candidate"] = ""
    return {k: verdict[k] for k in FIELDS}, "agreed" if agreed else "overruled"


def build_examples(rows: list[dict], system: str, types: list[str]) -> tuple[list[dict], dict]:
    """One example per distinct prompt (the latest review wins)."""
    stats = {"reviewed": len(rows), "agreed": 0, "overruled": 0, "skipped": 0, "duplicate_prompts": 0}
    by_prompt: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (str(r.get("reviewed_at") or ""), r["id"])):
        verdict, why = build_verdict(row, types)
        if verdict is None:
            stats["skipped"] += 1
            continue
        if row["prompt"] in by_prompt:
            stats["duplicate_prompts"] += 1
        by_prompt[row["prompt"]] = {"id": row["id"], "head_policy": row["head_policy"], "kind": why,
                                    "messages": [{"role": "system", "content": system},
                                                 {"role": "user", "content": row["prompt"]},
                                                 {"role": "assistant",
                                                  "content": json.dumps(verdict, ensure_ascii=False)}]}
    examples = sorted(by_prompt.values(), key=lambda e: e["id"])
    stats["agreed"] = sum(e["kind"] == "agreed" for e in examples)
    stats["overruled"] = sum(e["kind"] == "overruled" for e in examples)
    stats["usable"] = len(examples)
    return examples, stats


def split_by_policy(examples: list[dict], holdout: float, seed: str = SPLIT_SEED) -> tuple[list[dict], list[dict]]:
    """Whole changed policies go to the holdout, so the model never sees a policy it is tested on.
    Policies are ordered by a hash (stable across runs), then added until the holdout is big enough."""
    policies = sorted({e["head_policy"] for e in examples},
                      key=lambda p: hashlib.sha256(f"{seed}:{p}".encode()).hexdigest())
    target = holdout * len(examples)
    held: set[str] = set()
    count = 0
    for p in policies:
        if count >= target or len(held) >= len(policies) - 1:
            break
        held.add(p)
        count += sum(e["head_policy"] == p for e in examples)
    return ([e for e in examples if e["head_policy"] not in held],
            [e for e in examples if e["head_policy"] in held])


def write_jsonl(path: Path, examples: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in examples:
            f.write(json.dumps({"id": e["id"], "head_policy": e["head_policy"], "messages": e["messages"]},
                               ensure_ascii=False) + "\n")


def fetch_rows(database_url: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        return conn.execute(QUERY).fetchall()


def run(rows: list[dict], out: Path, holdout: float, min_examples: int) -> int:
    system, types = judge_system(), impact_types()
    examples, stats = build_examples(rows, system, types)
    print(f"Reviewed answers found:      {stats['reviewed']}")
    print(f"  usable for training:       {stats['usable']}  (reviewer agreed: {stats['agreed']}, reviewer corrected: {stats['overruled']})")
    print(f"  left out:                  {stats['skipped']} unclear, {stats['duplicate_prompts']} repeated")
    if stats["usable"] < min_examples:
        print(f"\nNot enough yet: {stats['usable']} usable answers, and {min_examples} are needed before training.\n"
              f"Ask reviewers for {min_examples - stats['usable']} more, then run this again. Nothing was written.")
        return 2
    train, held = split_by_policy(examples, holdout)
    if not train or not held:
        print("\nAll the reviewed answers are about one changed policy, so a fair test set cannot be made.\n"
              "Review answers for at least two different changed policies. Nothing was written.")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(out / "train.jsonl", train)
    write_jsonl(out / "holdout.jsonl", held)
    digest = hashlib.sha256()
    for name in ("train.jsonl", "holdout.jsonl"):
        digest.update((out / name).read_bytes())
    manifest = {"train": len(train), "holdout": len(held), "stats": stats, "data_sha256": digest.hexdigest(),
                "holdout_policies": sorted({e["head_policy"] for e in held}),
                "train_policies": sorted({e["head_policy"] for e in train}),
                "holdout_fraction_requested": holdout, "split_seed": SPLIT_SEED}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nWrote {len(train)} training and {len(held)} test examples to {out}")
    print(f"  test set = {len(manifest['holdout_policies'])} changed policies the model never trains on")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--out", type=Path, default=Path("/out/data"))
    ap.add_argument("--holdout", type=float, default=0.2, help="share of examples kept for testing (default 0.2)")
    ap.add_argument("--min-examples", type=int, default=200, help="refuse to write below this many (default 200)")
    a = ap.parse_args()
    if not a.database_url:
        sys.exit("DATABASE_URL is not set.")
    if not 0 < a.holdout < 1:
        sys.exit("--holdout must be between 0 and 1.")
    sys.exit(run(fetch_rows(a.database_url), a.out, a.holdout, a.min_examples))


if __name__ == "__main__":
    main()
