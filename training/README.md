# Training the judge

Fine-tunes the judge on answers people corrected in the review queue. Runs offline in Docker;
the base model is mounted from `models/`, nothing is downloaded. Start with `docs/review-and-training.md`.

## Files

| File | Does |
|---|---|
| `export_dataset.py` | Reads reviewed judgements from Postgres (`DATABASE_URL`) and writes `train.jsonl`, `holdout.jsonl`, `manifest.json`. Refuses below `--min-examples` (default 200). |
| `train_lora.py` | LoRA fine-tune (TRL `SFTTrainer` + PEFT), bf16, gradient checkpointing. Writes the adapter and `metadata.json`. |
| `merge_adapter.py` | Merges the adapter into the base model and saves a normal model folder for vLLM. |
| `compare_eval.py` | The promotion rule: recall not lower, precision higher. |
| `Dockerfile`, `requirements.txt` | `pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime` plus pinned transformers 4.56.2, trl 0.21.0, peft 0.17.1, accelerate 1.10.1, datasets 4.0.0, psycopg 3.2.13. |

## The data

Each line of `train.jsonl` is `{"id", "head_policy", "messages": [system, user, assistant]}`.

- system: `JUDGE_SYSTEM` from `policy_api/app/prompts.py` (copied into the image, so it is the exact same text)
- user: the stored `judgements.prompt`, the exact text the judge saw
- assistant: the corrected verdict as JSON with the keys of `IMPACT_SCHEMA`
  - reviewer agreed: the model's `why` and evidence are kept (a reviewer note, if any, replaces `why`)
  - reviewer disagreed: the note becomes `why`, evidence and `suggested_edit` are blank
  - "no" always has impact type `none` and blank evidence
  - a "yes" with no kind of impact (reviewer's or model's) is left out

If one prompt was reviewed twice, the latest review is used. The split into train and holdout is by
changed policy (a policy is never in both), deterministic (hash of the policy id), default 20% holdout.
Training computes loss on the answer only (prompt-completion format).

## Run

See the commands in `docs/review-and-training.md`. Without Docker:

```
python train_lora.py --train out/data/train.jsonl --dry-run     # checks the data and settings, loads no model
```

Settings (`python train_lora.py --help`): `--r 16 --alpha 32 --dropout 0.05 --lr 1e-4 --epochs 2
--max-len 4096 --batch-size 1 --grad-accum 16`, `--target-modules q_proj,k_proj,v_proj,o_proj`.

## Notes

- Base model loading follows the gpt-oss recipe: `Mxfp4Config(dequantize=True)` into bf16, eager attention.
  The dequantized 20b model needs about 42 GB of GPU memory for the weights alone, plus activations
  and LoRA state. Adjust `--max-len`, `--batch-size`, `--grad-accum` to fit.
- Attention-only LoRA is the default because vLLM does not support LoRA on the expert layers. The merged
  model is bf16 and needs more GPU memory than the shipped 4-bit one; re-quantizing is optional.
- `merge_adapter.py` removes `quantization_config` from the merged `config.json` (the weights are bf16),
  so vLLM does not try to read them as 4-bit.
- Tests (no GPU, no database): `pytest training/tests -q` from the repo root.
- `metadata.json` next to the adapter records the base model, a SHA-256 of the data, counts, settings,
  library versions and the final losses. Keep it with the model you promote.
