# Model weights

Download the weights **outside** the contained environment, record their hashes, then carry the
folder in. Nothing in the stack downloads anything at runtime (`HF_HUB_OFFLINE=1` everywhere).

Expected layout (folder names are what `compose.yaml` mounts):

| Folder | Model (check exact repo name and license before downloading) | Used by | Required |
|---|---|---|---|
| `gpt-oss-120b/` | `openai/gpt-oss-120b` (Apache 2.0) | `judge` | Yes |
| `embedding/` | `ibm-granite/granite-embedding-english-r2` (Apache 2.0), or `Snowflake/snowflake-arctic-embed-m-v2.0` | `embed` | Yes |
| `reranker/` | `ibm-granite/granite-embedding-reranker-english-r2`, or an NVIDIA NeMo Retriever reranker | `rerank` | Yes |
| `gpt-oss-20b/` | `openai/gpt-oss-20b` (Apache 2.0) | `extract` | Only with `--llm-refs` |
| `inkling-small/` | Thinking Machines Inkling-Small (Apache 2.0); check the repo name on the model card | `escalate` | Optional |

All are US-developed models. Confirm each one against your organisation's approved-model list.

```bash
# On a machine with internet access:
pip install -U "huggingface_hub[cli]"
hf download openai/gpt-oss-120b --local-dir models/gpt-oss-120b
hf download ibm-granite/granite-embedding-english-r2 --local-dir models/embedding
hf download ibm-granite/granite-embedding-reranker-english-r2 --local-dir models/reranker
scripts/verify_models.sh record

# Inside the contained environment, after copying the folder in:
scripts/verify_models.sh check
```

If the embedding model's output size is not 768, change `vector(768)` in `db/init.sql`
**before** the database is first created. See `docs/operations.md`.
