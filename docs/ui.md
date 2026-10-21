# The chat screen

People get one screen: type a question, attach a document if they have one, read the answer, open the
files it links to. Everything else Open WebUI can do is switched off.

## What is switched off, and where

| Where | What |
|---|---|
| `compose.yaml`, `open-webui` service | Sign-up, Ollama, web search, image generation, code running, direct connections, tool servers, sharing, ratings, arena models, memories, notes, channels, folders, API keys, follow-up and tag suggestions. `ENABLE_PERSISTENT_CONFIG=false` makes these settings win over the admin panel, so nobody can turn them back on by accident. |
| `openwebui/custom.css` | Leftover buttons that have no setting (workspace link, rate and read-aloud buttons, "add model"). The proxy serves this file at `/static/custom.css`, so it applies without rebuilding Open WebUI. |
| `openwebui/upload_filter.py` | Attached documents. It sends them to policy-api and tells the assistant the upload id, instead of letting Open WebUI cut them into its own search index. It also records review decisions (below). |

Open WebUI's name and logo stay. Its licence allows removing them only for deployments of 50 users or
fewer in any 30 days, or with a commercial licence.

## Setting it up (once, as admin)

1. Set `OPENWEBUI_ENABLE_SIGNUP=true` in `.env`, start the stack, create the admin account at
   `http://localhost:3000`, then set it back to `false` and run `docker compose up -d open-webui`.
2. **Admin Panel → Functions → +**: paste `openwebui/upload_filter.py`. Open its valves and set
   `API_KEY` to `POLICY_API_KEY` from `.env` (leave `API_URL` as `http://policy-api:8000`). Switch it
   on and make it **Global**.
3. Add the policy tool, the assistant model and its system prompt
   ([setup.md, step 7](setup.md#7-set-up-open-webui)). Tick the tool on the model, and set Function
   Calling to **Native**.
4. **Admin Panel → Users**: add people. They sign in and see only the chat.

## Review decisions (`REVIEW_GATE=1`)

When a check has "maybe" sections, the assistant lists them and tells the person to type a message that starts with `decide`:

    decide <check number> SAF-150 3.1 yes, SAF-151 2.4 no   (their own answers; the assistant never fills these in)

`yes` means the section is affected, `no` means it is not. `decide <check number> all no` (or `all yes`)
answers every listed section at once. Sections left out stay "maybe" in the final answer; the check does
not pause a second time. The message must start with the word `decide`; a quoted or pasted line further down is ignored. The **upload filter** reads this from the person's own message,
checks it, and sends it to policy-api under their Open WebUI name. The assistant has no tool for deciding
and cannot approve or reject on anyone's behalf; anything in an assistant or tool message is ignored. So the
filter must be switched on and **Global** (or enabled for the policy-assistant model); without it, a paused
check can never be decided from the chat.

The **check number** people see and type is the first 8 characters of the check's id (for example
`3f9a1c2e`). policy-api accepts exactly those 8 characters, or the full id, and only on the status and
review routes. It answers 409 if the 8 characters match more than one paused check. A check number is not tied to a chat or a person: anyone who can see it
can decide that check, and the name recorded is the Open WebUI name of whoever typed the command. Do not
paste check numbers where people outside the team can read them.

## After an Open WebUI upgrade

Open the chat as an ordinary user and check, in this order:

1. You can type a question and get an answer.
2. You can attach a `.docx` and the answer mentions its policy.
3. Links in the answer download, and charts show.
   With `REVIEW_GATE=1`, typing `decide <check number> ... yes` on a paused check gets a "recorded" reply.
4. Nothing you need is hidden. If a button you need is gone, remove its line from `custom.css`.
5. Nothing that should be gone is back. Button labels change between releases; update the selectors
   in `custom.css`.
