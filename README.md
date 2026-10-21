# secretary-keeper

Ask **"Which of our Safety policies are affected by the change to POL-007?"** and get back a
plain-language answer, a comparison table, and an Excel workbook with the evidence for every policy.

> **You are on the `hermes` branch.** In this version, people chat with an assistant (Hermes Agent).
> Hermes decides which tool to call, and the tools use a fixed check pipeline. The other versions are
> described under [Three versions](#three-versions). The full design is in
> [docs/architecture.md](docs/architecture.md).

It runs on your own hardware with US-developed models. Policy text never leaves your network: the
backend is sealed, and the only way out is one gateway to your own Granite server.

```mermaid
flowchart LR
  U[Staff] --> P[nginx proxy]
  P --> W[Open WebUI + upload filter]
  W -->|chat| H[Hermes Agent API server<br/>secretary plugin]
  W -->|attached files| A
  H -->|policy tools| A[policy-api<br/>fixed pipeline + job queue]
  H -->|model calls| G[granite-gateway]
  A --> D[(Postgres + pgvector)]
  A --> T[TEI embed + rerank]
  A -->|verdicts| G
  G --> V[Granite 4.2 30B on your vLLM server]
```

How it works in one paragraph: Open WebUI passes each chat to Hermes. Hermes asks Granite what to do.
Granite picks a tool from the `secretary` plugin (search policies, read a policy, run a check, make
files). The plugin calls policy-api. A check runs as a background job, and the plugin waits for it.
Code makes every impact decision and builds every number and file. Hermes has no web, files, terminal,
memory or MCP, so it answers only from the policy database.

The main model, IBM Granite 4.2 30B, already runs on your own vLLM server. The stack does not start
it. Everything reaches it through `granite-gateway`, the one way out of the sealed backend network.
The gateway passes on chat and model-list calls only, adds the API key itself, and checks the
server's certificate. Inkling-Small ("unsure" cases) and the other extra models are optional and run
locally. Granite needs a context of 65536 tokens or more here (Hermes refuses less). See
[docs/setup.md](docs/setup.md#granite-server-requirements).

## Three versions

This repo has four branches; you are on `hermes`. `main` is the v0.1 baseline. The other three build on one shared base
(the trimmed chat screen and the Granite gateway) and differ in who drives a check.

| Branch | In one line |
|---|---|
| `main` | v0.1 baseline. An Open WebUI tool calls policy-api, which runs a fixed pipeline. |
| `langgraph` | No Hermes. Open WebUI plus the policy tool. Each check runs as a LangGraph graph with Postgres checkpoints, so a crash resumes where it stopped. With `REVIEW_GATE` on, the check pauses and the person decides the "maybe" sections in chat. |
| `hermes` | Open WebUI talks to the Hermes Agent API server, which uses Granite. Hermes has a `secretary` plugin with policy tools, including database search (`GET /search`) and policy text (`GET /policies/{id}/text`). It has no web, files, terminal or memory, and no MCP. |
| `hermes-langgraph` | Hermes in front, LangGraph running each check. The person records review decisions by typing a message that starts with `decide <check> <section> <yes|no>, ...`. The Open WebUI upload filter parses it and records it under the user's real name. The model has no decide tool. |

### `langgraph`

```mermaid
flowchart LR
  U[Staff] --> P[nginx proxy]
  P --> W[Open WebUI + upload filter]
  W -->|policy tool| A[policy-api]
  A --> L[LangGraph graph<br/>checkpoints in Postgres]
  L --> D[(Postgres + pgvector)]
  L --> T[TEI embed + rerank]
  L --> G[granite-gateway]
  W -->|chat| G
  G --> V[Granite vLLM server]
```

### `hermes`

```mermaid
flowchart LR
  U[Staff] --> P[nginx proxy]
  P --> W[Open WebUI + upload filter]
  W --> H[Hermes Agent API server<br/>secretary plugin]
  H -->|policy tools| A[policy-api<br/>fixed pipeline]
  H -->|model calls| G[granite-gateway]
  A --> D[(Postgres + pgvector)]
  A --> T[TEI embed + rerank]
  A --> G
  G --> V[Granite vLLM server]
```

### `hermes-langgraph`

```mermaid
flowchart LR
  U[Staff] --> P[nginx proxy]
  P --> W[Open WebUI + upload filter]
  W --> H[Hermes Agent API server<br/>secretary plugin]
  H -->|policy tools| A[policy-api]
  W -->|"decide ..." messages| A
  A --> L[LangGraph graph<br/>checkpoints in Postgres]
  L --> D[(Postgres + pgvector)]
  L --> T[TEI embed + rerank]
  L --> G[granite-gateway]
  H -->|model calls| G
  G --> V[Granite vLLM server]
```

### Comparing them

| | `main` | `langgraph` | `hermes` | `hermes-langgraph` |
|---|---|---|---|---|
| Who decides which tool to call | Granite, through Open WebUI | Granite, through Open WebUI | Hermes (with Granite) | Hermes (with Granite) |
| How a check runs | Fixed pipeline in policy-api | LangGraph graph | Fixed pipeline in policy-api | LangGraph graph |
| Crash recovery | Job queue restarts the job | Resumes from the last checkpoint | Job queue restarts the job | Resumes from the last checkpoint |
| Review pause | No (review happens afterwards) | Optional `REVIEW_GATE`: person decides "maybe" sections in chat | No | Optional pause; the person types `decide <check> <section> <yes\|no>, ...` and the upload filter records it |
| Extra containers | None | None (graph runs inside policy-api) | Hermes | Hermes |
| Granite context needed | 32K or more | 32K or more | 64K or more (Hermes refuses less); 128K recommended | 64K or more; 128K recommended |

In every version, code makes the impact decisions and builds the numbers and the Excel file. The
chat model only passes the question in and the answer back. Required Granite server settings are in
[docs/setup.md](docs/setup.md#granite-server-requirements).

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
| Install it (and what your Granite server needs) | [docs/setup.md](docs/setup.md) |
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
| `compose.yaml`, `.env.example` | The whole stack: Granite gateway, optional local models (vLLM), TEI, Postgres, policy-api, Hermes, Open WebUI, proxy |
| `policy_api/` | The analysis pipeline and HTTP API (Python, FastAPI) |
| `hermes/` | Hermes settings (`config.yaml`), its rules (`SOUL.md`) and the `secretary` plugin with the policy tools |
| `openwebui/` | The upload filter and custom CSS. `policy_impact_tool.py` and `system_prompt.md` are for the other versions and are not used here |
| `gateway/` | nginx config for `granite-gateway`, the only way out of the backend network |
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
