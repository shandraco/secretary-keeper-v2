# The chat screen

People get one screen: type a question, attach a document if they have one, read the answer, open the
files it links to. Everything else Open WebUI can do is switched off.

## What is switched off, and where

| Where | What |
|---|---|
| `compose.yaml`, `open-webui` service | Sign-up, Ollama, web search, image generation, code running, direct connections, tool servers, sharing, ratings, arena models, memories, notes, channels, folders, API keys, follow-up and tag suggestions. `ENABLE_PERSISTENT_CONFIG=false` makes these settings win over the admin panel, so nobody can turn them back on by accident. |
| `openwebui/custom.css` | Leftover buttons that have no setting (workspace link, rate and read-aloud buttons, "add model"). The proxy serves this file at `/static/custom.css`, so it applies without rebuilding Open WebUI. |
| `openwebui/upload_filter.py` | Attached documents. It sends them to policy-api and tells the assistant the upload id, instead of letting Open WebUI cut them into its own search index. |

Open WebUI's name and logo stay. Its licence allows removing them only for deployments of 50 users or
fewer in any 30 days, or with a commercial licence.

## Setting it up (once, as admin)

1. Set `OPENWEBUI_ENABLE_SIGNUP=true` in `.env`, start the stack, create the admin account at
   `http://localhost:3000`, then set it back to `false` and run `docker compose up -d open-webui`.
2. **Admin Panel → Functions → +**: paste `openwebui/upload_filter.py`. Open its valves and set
   `API_KEY` to `POLICY_API_KEY` from `.env`. Switch it on and make it **Global**.
3. Nothing else to install on this branch. Open WebUI talks to Hermes, which has the policy tools ([setup.md](setup.md#7-set-up-hermes-and-open-webui)). The Open WebUI tool and system prompt in `openwebui/` are not used here.
4. **Admin Panel → Users**: add people. They sign in and see only the chat.

## After an Open WebUI upgrade

Open the chat as an ordinary user and check, in this order:

1. You can type a question and get an answer.
2. You can attach a `.docx` and the answer mentions its policy.
3. Links in the answer download, and charts show.
4. Nothing you need is hidden. If a button you need is gone, remove its line from `custom.css`.
5. Nothing that should be gone is back. Button labels change between releases; update the selectors
   in `custom.css`.
