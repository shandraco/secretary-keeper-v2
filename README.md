# secretary-keeper

> **This branch (`main`) is the v0.1 baseline.** Three newer versions live on their own branches. All three
> use IBM Granite 4.2 30B on your own vLLM server and a trimmed Open WebUI:
>
> | Branch | What answers the chat | How each check runs |
> |---|---|---|
> | [`langgraph`](../../tree/langgraph) | Open WebUI with the Policy Impact tool | A LangGraph graph with saved steps, crash-resume and an optional review pause |
> | [`hermes`](../../tree/hermes) | Hermes Agent with a policy-tools plugin, answering only from the policy database | The fixed check pipeline |
> | [`hermes-langgraph`](../../tree/hermes-langgraph) | Hermes Agent with the plugin | The LangGraph graph, with the review pause |
>
> Each branch's README and `docs/architecture.md` describe its architecture; PDFs are in `docs/pdf/`.

Ask **"Which of our Safety policies are affected by the change to POL-007?"** and get back a
plain-language answer, a comparison table, and an Excel workbook with the evidence for every policy.

It runs entirely on your own hardware with locally hosted, US-developed models. Policy text never
leaves the contained environment.

```
Staff ─▶ Open WebUI (chat) ─▶ Policy Impact tool ─▶ policy-api ─▶ diff → find related sections → judge each pair
                                                                    │              │                  │
                                                              Postgres +      embeddings +       gpt-oss-120b
                                                               pgvector         reranker      (+ Inkling-Small for
                                                                                                "unsure" cases)
```

## Try it in five minutes (no GPUs)

Demo mode uses the sample policies in `samples/` and simple stand-in models. It shows the flow
and the outputs. **It is not for real decisions.**

```bash
cd policy_api
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m app.demo ../samples --out demo.xlsx    # prints the chat answer, writes the Excel file
.venv/bin/python -m pytest -q                               # runs the tests
```

## Documentation

| If you want to… | Read |
|---|---|
| Read everything as one PDF, with diagrams | [docs/pdf/handbook.pdf](docs/pdf/handbook.pdf) |
| Understand how it works and why it's built this way | [docs/architecture.md](docs/architecture.md) |
| Install it on the GPU server | [docs/setup.md](docs/setup.md) |
| Choose which GPUs each model uses | `python3 scripts/setup_gpus.py` ([setup.md step 3](docs/setup.md)) |
| Ask questions and read the results (staff) | [docs/how-to.md](docs/how-to.md) |
| Add policies, groups or models (admins) | [docs/how-to.md](docs/how-to.md#for-admins) |
| Run it day to day, upgrade it, fix problems | [docs/operations.md](docs/operations.md) |
| Run and understand the tests | [docs/testing.md](docs/testing.md) |
| Measure whether it finds the right policies | [docs/evaluation.md](docs/evaluation.md) |
| Keep data contained and the system safe | [docs/security.md](docs/security.md) |

## What's in the repo

| Path | What it is |
|---|---|
| `compose.yaml`, `.env.example` | The whole stack: models (vLLM, TEI), Postgres, policy-api, Open WebUI, proxy |
| `policy_api/` | The analysis pipeline and HTTP API (Python, FastAPI) |
| `openwebui/` | The Open WebUI tool and the system prompt for the chat model |
| `proxy/nginx.conf` | The only service users reach |
| `db/init.sql` | Database schema, including the audit trail |
| `scripts/` | Network-containment check and model hash check |
| `samples/` | Sample policies and a sample changed policy |
| `eval/` | Evaluation cases (real ones stay out of git) |
| `models/` | Model weights go here (not in git); see `models/README.md` |

## Status

Version 0.1. Built and tested: the pipeline, the API, the Excel output, the Postgres store, the Open
WebUI tool's logic, the Docker network containment, and the proxy. These were tested with stand-in
models only. Before real use, run the full stack on the GPU server with the real models and complete
an evaluation against past policy changes ([docs/evaluation.md](docs/evaluation.md)).

Also built: a job queue that keeps checks alive across restarts, PDF, Word, Excel and chart output
from the chat, a review queue (Argilla) where staff confirm or correct verdicts, and LoRA fine-tuning
from those reviews. See [docs/review-and-training.md](docs/review-and-training.md) and
[docs/architecture.md](docs/architecture.md#roadmap).

## License

MIT. See [LICENSE](LICENSE).
