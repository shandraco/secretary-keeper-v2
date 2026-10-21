# How to use it

## For staff

### Ask a question

Open the Policy Impact Assistant and type your question in your own words. Name two things:

1. **The group of policies to check**, such as Safety or HR
2. **The policy that changed**, either by its ID or name, or by attaching the new version

Examples:

> Which Safety policies are affected by the change to POL-007?

> Here's the new lockout policy. What in Safety needs updating? *(attach the file)*

> Check the HR group against the latest version of the leave policy.

If something is unclear, the assistant asks you one short question. A check usually takes one to a
few minutes. You'll see progress messages while it works.

**Attaching a file:** use `.docx`, `.pdf`, `.md` or `.txt`. The sections must be numbered (1, 2.1,
4.3…), because the system compares the policy section by section.

**Without a file**, the assistant compares the two newest stored versions of the policy. If only one
version is stored, it asks you to attach the new one.

### Read the answer

> **2 of the 5 Safety policies need updating because of the change to POL-007 (Lockout/Tagout).**

Then you'll see:
- **What changed** in the head policy, section by section
- **A table** of the sections that need updating, and why
- **"Might be affected"** policies. A person must check these: the system wasn't sure.
- How many were checked and are **not affected**
- **A link to the Excel file** with all the detail

### Use the Excel file

| Sheet | What's on it |
|---|---|
| **Summary** | The question, the answer and the counts |
| **Impacted** | One row per affected section. **Red = needs updating. Yellow = a person must decide.** Each row shows the old and new wording, what the section says now, why it's affected, quotes as evidence, and a suggested edit |
| **Head policy changes** | Every changed section, old wording beside new |
| **Checked, not impacted** | Every other policy in the group, and why it was ruled out |
| **Method** | Which models and settings were used (for audits) |
| **Charts** | A bar chart of affected sections by policy |
| **Change matrix** | Which change in the head policy affects which policy |

Fill in **Review decision** (Agree, Disagree, Not sure) and **Reviewer** as you go. The file is yours
to keep and share inside the organisation.

The download link stops working after 72 hours. Ask the assistant again for a new one.

### Get a report, chart or comparison

Just ask, in your own words, after a check or on its own:

| Say something like | You get |
|---|---|
| "Can I have that as a PDF?" / "…as a Word report?" | A report of the last check: the answer, charts, the table of affected sections, and the old and new wording with changes marked |
| "Show me a chart of that" | Charts in the chat: affected sections by policy, which change affects which policy, kinds of impact |
| "Compare POL-007 version 3 with version 4" | A side-by-side comparison, section by section. Removed words are red and crossed out; new words are blue and underlined |
| "Compare this file with POL-007" (with a file attached) | The same comparison, against your file |
| "Compare SAF-112 and SAF-130 in Excel" | One row per section, in a workbook |
| "Make a one-page Word summary of this for the safety committee" | A document the assistant writes from what's in the chat. It says so at the top: check it before relying on it |

Files can be Word (.docx), PDF, or Excel (.xlsx). Links last 72 hours; one-off documents are kept for
30 days, and reports of a check can always be made again.

### What the system can and can't do

- It **finds and explains**. People still decide and approve every policy change.
- **"Maybe" never means "fine".** Always check these.
- It only knows the policies that have been loaded. If one seems missing, tell the system owner.
- It can be wrong in both directions. If it flags something that isn't affected, or misses something
  that is, tell the system owner. Mistakes are how it gets better.

### Known limits

- **Scanned PDFs** (pictures of pages) can't be read yet. Use the Word or text version.
- **Word's automatic numbering** isn't stored as text, so a Word file whose numbers are automatic may
  appear to have no sections. Save it with the numbers typed in, or as PDF.
- **Policies outside the group** you named are not checked. Ask about each group separately.

## For admins

### Add or update policies

Put the files in `POLICY_SOURCE_DIR` and run:

```bash
docker compose run --rm policy-api python -m app.ingest /policies
```

Ingest is safe to repeat. A file whose header has the same `version` replaces that version; a higher
`version`, or no version, adds a new one. Earlier versions are kept, so "compare the two newest
versions" works without uploading anything.

### Add a group, or move a policy between groups

Edit `subsets:` in the policy's header, then ingest that file again. A policy can be in several
groups. A policy's groups are replaced each time it's ingested.

### Turn on the second-opinion model (Inkling-Small)

Start it with `docker compose --profile escalate up -d escalate`, set
`ESCALATE_URL=http://escalate:8000/v1` in `.env`, then run `docker compose up -d policy-api`. "Unsure"
pairs then go to Inkling-Small. The **Checked by larger model** column in Excel shows which.

### Find more references when ingesting

`--llm-refs` asks the `extract` model to find references that the ID pattern misses, such as "the
lockout policy". Start the model first (`--profile extract`) and set `EXTRACT_URL`.

### Change how wide the search is

In `.env`:
- `SEMANTIC_K` (default 30): how many similar clauses to consider for each change
- `RERANK_KEEP` (default 20): how many of those to judge

Raise them if evaluation shows misses on policies that don't cite the head policy. Lower them if
runs are too slow. Clauses that cite the head policy, or use a changed term, are always judged
regardless of these settings.

### Look up what happened in a run

Every decision is in the `judgements` table:

```bash
docker compose exec db psql -U policy -c \
  "SELECT candidate_policy, candidate_number, stage, model, result->>'impacted', result->>'why'
   FROM judgements WHERE run_id = '<run id from the Excel Summary sheet>' ORDER BY candidate_policy"
```
