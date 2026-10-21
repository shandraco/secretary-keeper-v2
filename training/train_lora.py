"""LoRA fine-tune of the judge on reviewed answers (TRL SFTTrainer + PEFT). Offline.

    python train_lora.py --train /out/data/train.jsonl --eval /out/data/holdout.jsonl \\
        --base /models/gpt-oss-20b --out /out/adapter
    python train_lora.py --train tiny.jsonl --dry-run     # check the data and settings, load no model

Writes the adapter and metadata.json into --out. Needs a GPU; see README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Attention only: vLLM cannot apply LoRA to the mixture-of-experts layers of gpt-oss.
ATTENTION_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", type=Path, required=True, help="train.jsonl from export_dataset.py")
    ap.add_argument("--eval", type=Path, default=None, help="holdout.jsonl (optional, reports loss on it)")
    ap.add_argument("--base", type=Path, default=Path("/models/gpt-oss-20b"), help="local base model folder")
    ap.add_argument("--out", type=Path, default=Path("/out/adapter"))
    ap.add_argument("--r", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--dropout", type=float, default=0.05)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--target-modules", default=",".join(ATTENTION_MODULES),
                    help="comma-separated layer names (default: attention only)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="check data and settings, then stop before loading the model")
    return ap.parse_args(argv)


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        msgs = row.get("messages")
        roles = [m.get("role") for m in msgs or []]
        if roles != ["system", "user", "assistant"] or not all(m.get("content") for m in msgs):
            raise ValueError(f"{path}:{n}: expected system, user, assistant messages with text")
        json.loads(msgs[2]["content"])                       # the answer must itself be valid JSON
        rows.append(row)
    if not rows:
        raise ValueError(f"{path} has no examples")
    return rows


def sha256_of(*paths: Path | None) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p:
            h.update(p.read_bytes())
    return h.hexdigest()


def hyperparams(a: argparse.Namespace) -> dict:
    return {"r": a.r, "alpha": a.alpha, "dropout": a.dropout, "lr": a.lr, "epochs": a.epochs,
            "max_len": a.max_len, "batch_size": a.batch_size, "grad_accum": a.grad_accum,
            "target_modules": [m for m in a.target_modules.split(",") if m], "seed": a.seed, "dtype": "bfloat16"}


def to_prompt_completion(rows: list[dict]) -> list[dict]:
    """TRL then computes the loss on the answer only, not on the long policy text."""
    return [{"prompt": r["messages"][:2], "completion": r["messages"][2:]} for r in rows]


def build_metadata(a: argparse.Namespace, n_train: int, n_eval: int) -> dict:
    return {"base_model": str(a.base), "base_model_name": a.base.name,
            "train_file": str(a.train), "eval_file": str(a.eval) if a.eval else None,
            "data_sha256": sha256_of(a.train, a.eval), "train_examples": n_train, "eval_examples": n_eval,
            "hyperparams": hyperparams(a), "created_at": datetime.now(timezone.utc).isoformat()}


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    train_rows = load_jsonl(a.train)
    eval_rows = load_jsonl(a.eval) if a.eval else []
    meta = build_metadata(a, len(train_rows), len(eval_rows))
    print(f"Training examples: {len(train_rows)}   test examples: {len(eval_rows)}")
    print(f"Base model:        {a.base}")
    print("Settings:         ", json.dumps(meta["hyperparams"]))
    if a.dry_run:
        print("Dry run: data and settings are fine. No model was loaded.")
        return 0

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if not a.base.is_dir():
        sys.exit(f"Base model folder not found: {a.base}")

    import torch
    import transformers
    import peft
    import trl
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, Mxfp4Config
    from trl import SFTConfig, SFTTrainer

    tokenizer = AutoTokenizer.from_pretrained(a.base)
    model = AutoModelForCausalLM.from_pretrained(
        a.base,
        quantization_config=Mxfp4Config(dequantize=True),   # gpt-oss ships 4-bit; train in bf16
        torch_dtype=torch.bfloat16,
        attn_implementation="eager",
        use_cache=False,
        device_map="auto",
    )
    lora = LoraConfig(r=a.r, lora_alpha=a.alpha, lora_dropout=a.dropout, bias="none", task_type="CAUSAL_LM",
                      target_modules=meta["hyperparams"]["target_modules"])
    cfg = SFTConfig(
        output_dir=str(a.out / "checkpoints"),
        num_train_epochs=a.epochs,
        learning_rate=a.lr,
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        per_device_train_batch_size=a.batch_size,
        per_device_eval_batch_size=a.batch_size,
        gradient_accumulation_steps=a.grad_accum,
        gradient_checkpointing=True,
        bf16=True,
        max_length=a.max_len,
        logging_steps=5,
        eval_strategy="epoch" if eval_rows else "no",
        save_strategy="no",
        seed=a.seed,
        report_to="none",
    )
    trainer = SFTTrainer(
        model=model, args=cfg, processing_class=tokenizer, peft_config=lora,
        train_dataset=Dataset.from_list(to_prompt_completion(train_rows)),
        eval_dataset=Dataset.from_list(to_prompt_completion(eval_rows)) if eval_rows else None,
    )
    result = trainer.train()
    a.out.mkdir(parents=True, exist_ok=True)
    trainer.model.save_pretrained(a.out)
    tokenizer.save_pretrained(a.out)
    meta["train_loss"] = result.metrics.get("train_loss")
    meta["eval_loss"] = trainer.evaluate().get("eval_loss") if eval_rows else None
    meta["versions"] = {"torch": torch.__version__, "transformers": transformers.__version__,
                        "peft": peft.__version__, "trl": trl.__version__}
    (a.out / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Saved adapter to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
