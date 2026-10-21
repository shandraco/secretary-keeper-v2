"""Fold a trained LoRA adapter into the base model and save a normal model folder vLLM can serve.

    python merge_adapter.py --base /models/gpt-oss-20b --adapter /out/adapter --out /out/merged

The result is bf16 (about 4x larger than the shipped 4-bit model). Re-quantizing is optional and not done here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", type=Path, required=True, help="the same base model folder used for training")
    ap.add_argument("--adapter", type=Path, required=True, help="folder written by train_lora.py")
    ap.add_argument("--out", type=Path, required=True, help="folder for the merged model")
    ap.add_argument("--device-map", default="auto", help='"auto" uses the GPUs and spills to RAM; "cpu" for RAM only')
    return ap.parse_args(argv)


def strip_quantization_config(model_dir: Path) -> bool:
    """The merged weights are bf16. If config.json still says 4-bit, vLLM would load them wrongly."""
    cfg_path = model_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    if "quantization_config" not in cfg:
        return False
    del cfg["quantization_config"]
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    for p in (a.base, a.adapter):
        if not p.is_dir():
            sys.exit(f"Folder not found: {p}")

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, Mxfp4Config

    model = AutoModelForCausalLM.from_pretrained(
        a.base, quantization_config=Mxfp4Config(dequantize=True), torch_dtype=torch.bfloat16,
        attn_implementation="eager", device_map=a.device_map)
    merged = PeftModel.from_pretrained(model, a.adapter).merge_and_unload()
    a.out.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(a.out, safe_serialization=True)
    AutoTokenizer.from_pretrained(a.base).save_pretrained(a.out)
    if strip_quantization_config(a.out):
        print("Removed the 4-bit setting from config.json (the saved weights are bf16).")
    meta = a.adapter / "metadata.json"
    if meta.exists():
        (a.out / "training_metadata.json").write_text(meta.read_text())
    print(f"Merged model saved to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
