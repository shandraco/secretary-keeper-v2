# LoRA trainer for the judge. Offline at run time: models are mounted, nothing is downloaded.
# Build context is the repo root (compose.yaml does this), so the prompts can be copied in.
# Base image: official PyTorch 2.8.0 + CUDA 12.9 (tag verified on Docker Hub). Build once on a machine
# with internet access, then move the image to the server if the server has none.
FROM pytorch/pytorch:2.8.0-cuda12.9-cudnn9-runtime

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1

COPY training/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

WORKDIR /opt/training
COPY training/export_dataset.py training/train_lora.py training/merge_adapter.py training/compare_eval.py ./
# The judge's instructions live in policy_api/app/prompts.py (the training data must use the exact same text).
COPY policy_api/app ./app

ENTRYPOINT ["python"]
CMD ["train_lora.py", "--help"]
