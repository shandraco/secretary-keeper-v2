"""Run from the repo root:  pytest training/tests -q   (needs pyyaml; no GPU, no database)."""
import json
import sys
from pathlib import Path

import pytest

TRAINING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TRAINING))
sys.path.insert(0, str(TRAINING.parent / "policy_api"))

import export_dataset as ex  # noqa: E402
import train_lora as tl  # noqa: E402
from app.prompts import IMPACT_SCHEMA, JUDGE_SYSTEM  # noqa: E402

TYPES = ex.impact_types()


def row(i, policy, decision, model=None, rtype="", note=""):
    model = model or {"impacted": "yes", "impact_type": "needs_update", "why": "old value",
                      "evidence_head": "7 years", "evidence_candidate": "3 years", "suggested_edit": "use 7"}
    return {"id": i, "prompt": f"prompt {i}", "result": model, "reviewer_decision": decision,
            "reviewer_impact_type": rtype, "reviewer_note": note, "reviewed_at": None, "head_policy": policy}


def verdict(r):
    v, _ = ex.build_verdict(r, TYPES)
    return v


def test_verdict_matches_schema_keys_and_enums():
    for r in (row(1, "A", "yes"), row(2, "A", "no", {"impacted": "no", "impact_type": "none"}),
              row(3, "A", "unsure", rtype="none")):
        v = verdict(r)
        assert list(v) == list(IMPACT_SCHEMA["required"])
        assert v["impacted"] in IMPACT_SCHEMA["properties"]["impacted"]["enum"]
        assert v["impact_type"] in IMPACT_SCHEMA["properties"]["impact_type"]["enum"]


def test_agreement_keeps_model_reasons():
    v = verdict(row(1, "A", "yes", rtype="needs_update"))
    assert v["why"] == "old value" and v["evidence_head"] == "7 years"


def test_overrule_uses_note_and_blanks_evidence():
    v = verdict(row(1, "A", "no", note="This clause is about leave, not records."))
    assert v == {"impacted": "no", "impact_type": "none", "why": "This clause is about leave, not records.",
                 "evidence_head": "", "evidence_candidate": "", "suggested_edit": ""}


def test_overrule_to_yes_uses_reviewer_type_and_has_no_evidence():
    m = {"impacted": "no", "impact_type": "none", "why": "fine", "evidence_head": "", "evidence_candidate": "",
         "suggested_edit": ""}
    v = verdict(row(1, "A", "yes", m, rtype="conflict", note="Limit is now lower."))
    assert v["impact_type"] == "conflict" and v["why"] == "Limit is now lower." and v["evidence_head"] == ""


def test_yes_without_any_kind_is_skipped():
    m = {"impacted": "no", "impact_type": "none", "why": "", "evidence_head": "", "evidence_candidate": "",
         "suggested_edit": ""}
    assert ex.build_verdict(row(1, "A", "yes", m), TYPES)[0] is None


def test_split_is_by_policy_deterministic_and_leak_free():
    examples, _ = ex.build_examples([row(i, f"P{i % 10}", "yes", rtype="conflict") for i in range(100)],
                                    JUDGE_SYSTEM, TYPES)
    train, held = ex.split_by_policy(examples, 0.2)
    assert train and held
    assert {e["head_policy"] for e in train}.isdisjoint({e["head_policy"] for e in held})
    assert 10 <= len(held) <= 40
    assert ex.split_by_policy(examples, 0.2) == (train, held)


def test_refuses_to_write_below_minimum(tmp_path, capsys):
    rows = [row(i, f"P{i % 5}", "yes", rtype="conflict") for i in range(30)]
    assert ex.run(rows, tmp_path / "d", 0.2, 200) == 2
    assert not (tmp_path / "d").exists()
    assert "Not enough yet" in capsys.readouterr().out


def test_writes_files_and_dry_run_reads_them(tmp_path, capsys):
    rows = [row(i, f"P{i % 5}", "yes" if i % 3 else "no", rtype="conflict", note="n") for i in range(60)]
    assert ex.run(rows, tmp_path, 0.2, 50) == 0
    first = json.loads((tmp_path / "train.jsonl").read_text().splitlines()[0])
    assert [m["role"] for m in first["messages"]] == ["system", "user", "assistant"]
    assert first["messages"][0]["content"] == JUDGE_SYSTEM
    json.loads(first["messages"][2]["content"])
    assert json.loads((tmp_path / "manifest.json").read_text())["train"] > 0
    rc = tl.main(["--train", str(tmp_path / "train.jsonl"), "--eval", str(tmp_path / "holdout.jsonl"), "--dry-run"])
    assert rc == 0 and "Dry run" in capsys.readouterr().out


def test_train_rejects_bad_rows(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}) + "\n")
    with pytest.raises(ValueError):
        tl.load_jsonl(bad)


def test_defaults_are_attention_only():
    a = tl.parse_args(["--train", "x.jsonl"])
    assert tl.hyperparams(a)["target_modules"] == ["q_proj", "k_proj", "v_proj", "o_proj"]


def test_promotion_gate():
    import compare_eval as ce
    base = {"mean_recall": 0.80, "mean_precision": 0.70}
    assert ce.decide(base, {"mean_recall": 0.80, "mean_precision": 0.75})[0]
    assert not ce.decide(base, {"mean_recall": 0.79, "mean_precision": 0.90})[0]   # recall dropped
    assert not ce.decide(base, {"mean_recall": 0.90, "mean_precision": 0.70})[0]   # precision not higher
