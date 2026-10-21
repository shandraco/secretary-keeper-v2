# Architecture

This is the **hermes** version. People chat with an assistant (Hermes Agent). The assistant decides
which tool to call. The tools talk to a fixed check pipeline. Other versions of the product are
described in the [README](../README.md#three-versions).

## The job

When a head policy changes, find every policy in a chosen group that now conflicts with it, repeats
its old wording, points to a section that moved, or uses a defined term whose meaning changed. Missing
an affected policy is the expensive mistake, so the design favours **recall** (finding everything)
over precision (flagging nothing extra), and it puts a person in the loop for anything uncertain.

## The core rule

**The chat model never decides impact itself.** Hermes works out what the person is asking, calls a
tool, and passes on the result. The pipeline inside policy-api makes every decision, and code builds
every number, table and spreadsheet. The chat answer and the Excel file therefore always agree, and
every decision can be traced in the audit trail.

Hermes also has no other source of facts. It has no web search, no files, no terminal, no memory and
no MCP servers. If the tools do not return it, the assistant says "Our policies don't cover that".

## The parts in one picture

Read the numbers 1 to 7 in the legend. Solid lines are requests; dashed lines are attached files and
the single way out to the Granite server.

## Request flow, step by step

### A. A question about what a policy says

Example: "What do we require for lockout devices?"

1. The person types in Open WebUI, reached through the `proxy`.
2. Open WebUI sends the chat to Hermes as an OpenAI-style request for the model `policy-assistant`,
   signed with `HERMES_API_KEY`.
3. Hermes builds a prompt from its rules (`hermes/SOUL.md`), the list of tools it may use, and the chat
   so far. It sends the prompt to Granite through `granite-gateway`.
4. Granite replies with a tool call, here `search_policies(question=...)`.
5. The plugin calls `GET /search` on policy-api. policy-api embeds the question (TEI `embed`), finds
   the nearest sections in Postgres (pgvector), and lets the reranker put them in order. The best 8
   (up to 20) come back, each cut to 1,500 characters.
6. The plugin wraps each section in `<<<` and `>>>` so the model treats it as wording, never as
   instructions, and hands the text to Hermes. Hermes sends it to Granite.
7. Granite writes the answer and cites each fact as `[policy §section]`. Hermes returns it to Open
   WebUI.

If nothing matches, the tool says so and the assistant answers that the policies do not cover it. To
read a whole policy or one section, Granite calls `read_policy` (`GET /policies/{id}/text`).

### B. A question with an attached document

1. The person attaches a file. Before the message leaves Open WebUI, the **upload filter**
   (`openwebui/upload_filter.py`, a global function) sends the file to `POST /uploads` on policy-api
   with `POLICY_API_KEY`.
2. policy-api reads the file in its own limited process, splits it into numbered sections, stores it,
   and guesses which stored policy it is a new version of.
3. The filter adds a note to the end of the person's message: `[Attached file ...: upload id ...]`. It
   also stops Open WebUI from cutting the file into its own search index.
4. Hermes sees the note. Its rules tell it to pass the upload id to the tools.

### C. A policy change check, end to end

Example: "Which Safety policies are affected by the change to POL-007?" with the new version attached.

1. Granite calls `check_policy_change(policy_group, head_policy, upload_id)`.
2. The plugin finds the group (`GET /subsets`) and the head policy (`GET /policies`). If a name is not a
   clear match, the tool returns a question for the person instead of guessing.
3. The plugin sends `POST /runs`, recording the request as made by `assistant`. policy-api stores a
   `runs` row and a row in the `jobs` table, and answers **202 Accepted** with a run id.
4. A worker inside policy-api claims the job (`FOR UPDATE SKIP LOCKED`) with a 60-second lease and
   renews the lease while it works. It runs the pipeline below. Many Granite calls run at once
   (`JUDGE_CONCURRENCY`, default 16).
5. While the job runs, the plugin asks `GET /runs/{id}` every 3 seconds. The Hermes turn waits, so the
   person's chat shows a running answer.
6. When the run is `done`, policy-api returns the chat answer (built in code) and a signed Excel link
   that works for 72 hours. The plugin passes both to Hermes with an instruction to show them exactly.
7. Granite relays the answer. If the person then asks for a PDF, Word file or charts, Granite calls
   `make_check_files`, which uses `POST /runs/{id}/files`.

The pipeline in step 4:

```
1. Resolve        "Safety" → group; "POL-007" or an uploaded file → head policy
2. Diff           clause-level comparison of old vs new head policy
                  → modified / added / removed / renumbered sections, and defined terms touched
3. Find related   for each changed section, collect clauses in the group that:
   sections         a. cite the head policy (whole policy, the section, or its parent section)   ← always kept
                    b. use a defined term from the changed section                             ← always kept
                    c. have similar meaning (embedding search, then reranker keeps the top N)
4. Judge          Granite 4.2 30B (on your own vLLM server, reached through granite-gateway) reads each (changed section, related clause) pair and returns
                  yes / no / unsure, a reason, evidence quotes and a suggested edit, forced into a JSON schema
5. Escalate       "unsure" pairs go to Inkling-Small for a second opinion (optional)
6. Summarise      per policy: Yes if any section is "yes", Maybe if any is "unsure", else not affected
7. Report         chat answer and Excel workbook, both built in code from the same result
```

Steps 3a and 3b are never cut by the reranker, because explicit links are where most real impact is.
Step 3c catches policies that overlap in meaning without citing the head policy.

## The agent loop

Hermes repeats the same small loop for every question: ask Granite, run the tool Granite picks, give
Granite the result, and ask again. The loop ends when Granite answers in words instead of asking for a
tool. Hermes stops it after `max_turns: 30` rounds.

The plugin returns plain text, not data. Each result starts with an instruction to Granite ("show
this answer exactly as written", "answer only from these sections"). That is how numbers, policy IDs
and links reach the person unchanged.

## Components

| Component | Image and version | Role | Networks |
|---|---|---|---|
| `proxy` | `nginx:1.27-alpine` (`NGINX_TAG`) | The only thing people reach: Open WebUI, plus report downloads and `/health`. Holds no data | backend + frontend |
| `open-webui` | `ghcr.io/open-webui/open-webui` (`OPENWEBUI_TAG`, pinned in `.env`) | Chat screen. Sends chats to Hermes. Runs the upload filter. Trimmed: no web search, tools, memories or code running | backend |
| `hermes` | `nousresearch/hermes-agent` (`HERMES_TAG`; tested with v0.22.3) | The assistant. OpenAI-compatible API on port 8642, model name `policy-assistant`. Runs the agent loop and the `secretary` plugin | backend |
| `policy-api` | Built from `policy_api/` (Python 3.12, FastAPI 0.115 or later; `APP_VERSION`) | The check pipeline, the job queue and worker, search, uploads, reports and downloads | backend |
| `db` | `pgvector/pgvector:pg17` (`PGVECTOR_TAG`) | Postgres with pgvector: policies, clauses, graph, runs, jobs, audit trail | backend |
| `embed`, `rerank` | `text-embeddings-inference` (`TEI_TAG`) with Granite embedding and reranker models | Embeddings for similarity search; reranker to order and cut candidates | backend |
| `granite-gateway` | `nginx:1.27-alpine` | The backend's only way out. Passes chat and model-list calls to `GRANITE_URL`, adds the Granite key, checks TLS | backend + llm-egress |
| Granite 4.2 30B | Your own vLLM server, outside this stack (vLLM 0.20 or later) | The model behind Hermes' reasoning and policy-api's verdicts | Reached only through the gateway |
| `escalate` (optional) | `vllm/vllm-openai` (`VLLM_TAG`), Inkling-Small | Second opinion on "unsure" pairs | backend |
| `extract` (optional) | `vllm/vllm-openai` (`VLLM_TAG`), gpt-oss-20b | Finds references the regex misses during ingest | backend |
| Review and training services (optional) | Argilla v2.8.0 and its helpers; `lora-trainer`; `judge-candidate`; `judge-tuned` | Review queue and fine-tuning ([review-and-training.md](review-and-training.md)) | backend |

The Open WebUI tool file `openwebui/policy_impact_tool.py` is **not used** in this version. Hermes and
its plugin do that job. The file stays in the repo for the `main` and `langgraph` versions.

## Hermes: settings and why

The settings are in `hermes/config.yaml`, mounted read-only, so nobody can change them from the chat.

| Setting | Value | Why |
|---|---|---|
| Model | `granite-4.2-30b` through `http://granite-gateway:8000/v1` | One model, one way out. The gateway adds the key, so Hermes holds none |
| `context_length` | 131072 | What the Granite server serves. Hermes refuses a model with less than 64,000 tokens, so the server needs `--max-model-len` of **65536 or more** (131072 recommended) |
| Tools allowed (`api_server`) | `secretary`, `todo`, `delegation` | The policy tools, a to-do list for multi-part requests, and subagents for side tasks |
| Tools switched off | web, search, browser, vision, image and video, speech, terminal, file, code execution, skills, memory, session search, scheduled jobs, kanban, connections | Answers come only from the policy database. Nothing can reach outside or change a file |
| `tool_search` | off | Hermes would otherwise hide some tools behind a search step. A mid-size model handles a short, plain list of tools more reliably |
| `max_turns` | 30 | Hermes' default is no limit. A loop that never ends would never free the chat |
| Subagents (`delegation`) | Same model, 20 steps each, 3 at a time, no subagents of their own | Keeps side tasks small |
| Helper calls (titles, compression, approvals) | Sent to the main model | "Auto" would try outside providers first. The sealed network would block them, but slowly |
| Memory, user profile, curator, background review | Off | Nothing is remembered between chats, and the assistant writes no skills of its own |
| Update checks, model catalog, lazy installs | Off | No calls home, nothing installed at run time |
| Plugin | `secretary` only | The tools below |

Open WebUI also has its own title, tag and follow-up generation switched off. Each one would be a
full Hermes run.

## The plugin's tools

The plugin is `hermes/plugins/secretary/`. Every tool calls policy-api with `POLICY_API_KEY`. Each
tool returns text with instructions for the model, and turns network errors into a plain message.

| Tool | What it does | Calls |
|---|---|---|
| `search_policies` | Finds sections that answer a question, optionally within one group or one policy | `GET /search` |
| `read_policy` | Reads a whole policy, or one section with its subsections, at a chosen version | `GET /policies` (to resolve the name), `GET /policies/{id}/text` |
| `list_policy_groups` | Lists groups such as Safety or HR | `GET /subsets` |
| `find_policy` | Finds a policy by ID or title words | `GET /policies` |
| `check_policy_change` | Starts a check and waits for the result | `GET /subsets`, `GET /policies`, `POST /runs`, then `GET /runs/{id}` |
| `get_check_result` | Gets the result of a check that was still running | `GET /runs/{id}` |
| `make_check_files` | PDF, Word, Excel or charts of a finished check | `POST /runs/{id}/files` |
| `compare_policies` | Compares two policies, two versions, or a policy and an attached document | `GET /policies`, `POST /compare` |
| `make_document` | Word, PDF, Excel or chart from content the model writes, for requests that are not a check | `POST /documents` |

There is no tool for deciding "maybe" sections, and no review pause in this version. "Maybe" results
go to the review queue and the Excel Summary sheet.

## Data stores

| Store | Holds | Notes |
|---|---|---|
| Postgres (`pgdata` volume) | Policies and every version, clauses with embeddings, the reference graph, groups, uploads, runs, the job queue, and every model verdict | Back up daily ([operations.md](operations.md#backups)) |
| `exports` volume | Excel, PDF, Word and chart files | Removed after `EXPORT_RETENTION_DAYS` (default 30) |
| `hermes-data` volume | Hermes' own working folder | Config, rules and plugin are mounted read-only on top. Memory is off |
| `openwebui` volume | Users, chats, the upload filter and its key | Back up with `.env` |
| Argilla databases (optional) | Review queue | Separate from the policy database |

## Network and security boundaries

```
frontend network:   people ─▶ proxy
backend network:    proxy ─▶ open-webui ─▶ hermes ─▶ policy-api ─▶ db, embed, rerank
backend + egress:   hermes and policy-api ─▶ granite-gateway ─▶ Granite server
```

- `backend` is `internal: true`: no route out. `proxy` is the only container on `frontend`. The
  gateway is the only container on `llm-egress`.
- The gateway passes `/v1/chat/completions` and `/v1/models` and nothing else, and checks the Granite
  server's certificate.
- Hermes has no route to the internet and no tool that would use one. It reaches policy-api and the
  gateway, and nothing else.
- policy-api answers only requests with the right key and a known `Host` name. The proxy shows
  people only Open WebUI, report downloads and `/health`.

Three keys keep the parts apart. Each is held by as few parts as possible.

| Key | Where it lives | Who uses it | What it protects |
|---|---|---|---|
| `POLICY_API_KEY` | `.env` | policy-api (to check it); Hermes (as `SECRETARY_API_KEY`, for the plugin); the upload filter (a setting in Open WebUI) | Every policy-api route except signed downloads |
| `HERMES_API_KEY` (16+ characters) | `.env` | Hermes (as `API_SERVER_KEY`); Open WebUI (as `OPENAI_API_KEY`) | Hermes' chat API |
| `GRANITE_API_KEY` | `.env` | `granite-gateway` only | The Granite server. Hermes and policy-api never see it |

`LLM_API_KEY` guards the optional local models. `LINK_SIGNING_KEY` signs download links. See
[security.md](security.md#secrets).

## When things fail

| What fails | What happens |
|---|---|
| Granite server down or slow | Chat: Hermes gets no reply from Granite, so the chat shows an error. Checks: each judged pair that fails is marked "could not be checked automatically" and shows as Maybe, for a person to check. If **every** pair fails, the check fails at once with "The checking model is not responding" and is not retried |
| policy-api down | Each tool returns "The policy service did not respond"; Hermes tells the person to try again later. A check already queued waits in the database |
| policy-api restarts during a check | The worker takes its own jobs back at once. After a crash, the 60-second lease runs out and any worker picks the job up. The check starts again from the beginning |
| A temporary error inside a check (database blip, model restart) | Retried up to `JOB_MAX_ATTEMPTS` (default 3) with growing waits (up to 5 minutes, random). After that the job is `dead` and the person sees "failed several times in a row" |
| A problem with the question (no such policy, nothing changed) | Not retried. The person gets a plain reason |
| A check takes longer than 10 minutes (`SECRETARY_MAX_WAIT_MINUTES`) | The plugin stops waiting and tells Hermes the check is still running. The check itself continues. The person can ask again and Hermes calls `get_check_result`. It must not start a second check |
| Hermes loops without answering | Hermes stops after 30 rounds (`max_turns`) |
| A file cannot be read | The upload filter puts a plain note in the message; Hermes tells the person |

## Limits

- **No per-person identity reaches policy-api.** Open WebUI knows who is signed in, but Hermes does
  not pass that on. Every check is stored as requested by `assistant`. Use Open WebUI's own account
  records to see who asked, and tell staff their questions are logged.
- **Everyone with an account can ask about every group.** There is no per-user group filter.
- **Training still targets gpt-oss-20b.** The fine-tuning scripts and the `judge-candidate` and
  `judge-tuned` services are set up for that model, not for Granite. Verdicts can be sent to a tuned
  judge; the chat stays on Granite ([review-and-training.md](review-and-training.md)).
- **The Granite server must be set up for this.** Tool calling, structured output and a context of
  at least 65536 tokens ([setup.md](setup.md#granite-server-requirements)).
- **A very long check holds the chat open.** The plugin waits up to 10 minutes
  (`SECRETARY_MAX_WAIT_MINUTES`); Open WebUI waits 15 (`OPENWEBUI_TIMEOUT_SECONDS`, 900). Keep the
  first below the second, and set any proxy in front of the stack to at least the same. If a check
  runs longer, Hermes says so and the result is fetched later by check number.
- **Hermes follows its rules by instruction, not by force.** It cannot reach the internet or change
  a file, because those tools are switched off. It can still word an answer badly. That is why numbers,
  links and verdicts are produced by code and passed through unchanged.

## Why these choices

| Choice | Why | Instead of |
|---|---|---|
| Hermes picks the tool; a fixed pipeline decides impact | The assistant handles the conversation (which group, which policy, which file, follow-up questions). The pipeline is deterministic, testable and auditable | An agent that plans its own steps and judges impact itself (more tokens, results vary between runs, harder to audit) |
| A plugin that returns text with instructions, and nothing else | Hermes has no web, files, terminal or memory, so a wrong answer can be badly worded but cannot leak or change anything | Giving the agent general tools and trusting it to behave |
| Granite 4.2 30B as judge and chat model | US-developed (IBM), Apache 2.0, dense 30B that fits one 80 GB GPU in bf16, 128K context, tool calling, thinking can be switched off per request for fast, repeatable verdicts | Inkling for everything (needs 4–8 high-end GPUs per replica) |
| Use the Granite server you already run, behind one gateway | No second copy of a 58 GB model to host and update. One narrow, checked way out (two routes, one address, key held in one place) instead of a model container inside the stack | Starting a local judge container, or letting each service call the server on its own |
| Inkling-Small only for "unsure" | Larger-model judgement where it matters, at a fraction of the cost | Running every pair through the large model |
| Reranker before judging | Cuts hundreds of similar clauses to about 20 per change; the biggest compute saving | Judging every similar clause |
| JSON-schema guided decoding | Every verdict is valid and machine-readable | Asking for JSON in the prompt and hoping |
| Postgres for the graph | Recursive queries cover policy hierarchies; one fewer system to run | A separate graph database |
| Excel built with openpyxl | Exact, repeatable, safe from formula injection | Letting the model write the spreadsheet |
| Job queue in Postgres (`FOR UPDATE SKIP LOCKED`, leases) | Runs survive restarts with no new service to run or secure; Postgres is already here | Temporal or Dagster: another stateful system to operate inside the contained network, for a queue of a few hundred jobs a day |

## Data model (db/init.sql)

- `policies`, `policy_versions`, `clauses`: every version is kept. `current_clauses` is a view of each policy's latest version.
- `refs`: the reference graph (which clause cites which policy and section), for latest versions.
- `subsets`: which policies belong to which group. A policy can be in several groups.
- `uploads`, `runs`, `changes`: each question and what changed.
- `judgements`: one row per model decision, with model name, the exact prompt and its hash, verdict, and the reviewer's decision. This is the audit trail and the training data.
- `jobs`: the work queue (see [operations.md](operations.md#the-job-queue)). `schema_migrations` records which files in `policy_api/app/migrations/` have been applied; `db/init.sql` is the starting point for a new database.

## Clause parsing rules

A new clause starts at a line that begins with a section number:
- a Markdown heading with a number (`## 4 Lockout devices`)
- any multi-level number (`4.2 Devices are inspected…`, `4.2.1) …`)
- a single number followed by a short title (`4 Lockout devices`)

`1. Do this.` inside a clause stays part of that clause. Text before the first number is clause `0`.
Policy IDs are found with `POLICY_ID_PATTERN` (default `[A-Z]{2,6}-\d{1,5}`), in forms like
`POL-007 §4.2`, `§4.2 of POL-007`, `POL-007 section 4` and `POL-007` alone (whole policy).

## Roadmap

Built since v0.1: the job queue ([operations.md](operations.md#the-job-queue)), the review queue and
LoRA fine-tuning ([review-and-training.md](review-and-training.md)), and reports, comparisons and
charts ([how-to.md](how-to.md)).

1. **OCR** for scanned PDFs, and reading Word's automatic numbering (see known limits in [how-to.md](how-to.md#known-limits)).
2. **Kubernetes** manifests when the models need more than one node.
