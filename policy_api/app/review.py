"""Human review queue: push the judge's answers to Argilla, pull back what reviewers decided.

policy-api calls this through asyncio.to_thread (the calls are synchronous). Nothing here touches
the network until a method is called. See docs/review-and-training.md.

    review = ArgillaReview(api_url, api_key, workspace)
    review.ensure_dataset()
    review.push(items)                       # safe to repeat: a judgement is never added twice
    decisions = review.pull_submitted()      # only answers a reviewer has submitted
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol

from .prompts import IMPACT_TYPES

DEFAULT_DATASET = "policy-impact-review"
DECISIONS = ("yes", "no", "unsure")

# Q_AFFECTED, Q_KIND and Q_NOTE are the question names inside Argilla.
Q_AFFECTED = "affected"
Q_KIND = "impact_type"
Q_NOTE = "note"

DECISION_LABELS = {
    "yes": "Yes, this section is affected",
    "no": "No, this section is not affected",
    "unsure": "I can't tell",
}
IMPACT_LABELS = {
    "conflict": "It now contradicts the new wording",
    "outdated_reference": "It points to a section that moved or was removed",
    "changed_term": "It uses a word whose meaning changed",
    "needs_update": "It repeats old wording or an old number",
    "redundant": "It now just repeats the other policy",
    "none": "No impact",
}

GUIDELINES = """\
WHAT YOU ARE DOING
A computer program read a change to one policy and thinks it may affect a section of another policy.
Your job is to say whether it is right. You are checking the program's work, not doing new research.

FOR EACH CARD
1. Read "What changed" and the old and new wording.
2. Read the "Section that may be affected".
3. Read "What the program said". Its answer is already filled in for you.
4. Answer the first question: is this section affected by the change?
     Yes     - the section now disagrees with the new wording, repeats the old wording,
               points to something that moved or was removed, or uses a word whose meaning changed.
     No      - the section is fine as it is.
     Can't tell - you would need more than these two pieces of text to decide.
5. If you answered Yes, pick the kind of impact. If you answered No, leave it as it is.
6. Write a short note only if you disagree with the program, or something looks odd.
7. Press Submit. Press Discard if you do not want to answer this one. Nothing is lost either way.

GOOD TO KNOW
- If the program is right, just press Submit. You do not have to change anything.
- Use only the text on the card. Do not guess at facts that are not there.
- It is fine to answer "Can't tell". It is better than a wrong answer.
- Your answers are used to check and improve the program. They do not change any policy.
"""


@dataclass(frozen=True)
class ReviewItem:
    judgement_id: int
    run_id: str
    head_policy: str
    head_change_label: str
    old_text: str
    new_text: str
    candidate_policy: str
    candidate_title: str
    candidate_number: str
    candidate_text: str
    model_verdict: str
    impact_type: str
    why: str
    evidence_head: str
    evidence_candidate: str


@dataclass(frozen=True)
class ReviewDecision:
    judgement_id: int
    decision: str          # 'yes' | 'no' | 'unsure'
    impact_type: str       # one of IMPACT_TYPES, or "" when the reviewer left it empty
    note: str
    reviewer: str
    reviewed_at: str       # ISO 8601, UTC


class Review(Protocol):
    def ensure_dataset(self) -> None: ...
    def push(self, items: Iterable[ReviewItem]) -> int: ...
    def pull_submitted(self, since: str | None = None) -> list[ReviewDecision]: ...


def _text(value: str | None) -> str:
    return (value or "").strip() or "(none)"


def record_fields(item: ReviewItem) -> dict[str, str]:
    """What a reviewer sees. Plain text on purpose: policy text is never rendered as markup."""
    answer = {"yes": "YES, affected", "no": "NO, not affected", "unsure": "UNSURE"}.get(item.model_verdict, item.model_verdict)
    lines = [f"Answer: {answer}"]
    if item.impact_type and item.impact_type != "none":
        lines.append(f"Kind of impact: {IMPACT_LABELS.get(item.impact_type, item.impact_type)}")
    lines.append(f"Reason: {_text(item.why)}")
    if item.evidence_head.strip():
        lines.append(f"Quoted from the changed policy: {item.evidence_head.strip()}")
    if item.evidence_candidate.strip():
        lines.append(f"Quoted from the section: {item.evidence_candidate.strip()}")
    return {
        "what_changed": f"Policy {item.head_policy}, {item.head_change_label}",
        "old_wording": _text(item.old_text),
        "new_wording": _text(item.new_text),
        "section": (f"Policy {item.candidate_policy} - {item.candidate_title}\n"
                    f"Section {item.candidate_number}\n\n{_text(item.candidate_text)}"),
        "model_answer": "\n".join(lines),
    }


def record_metadata(item: ReviewItem) -> dict[str, str]:
    return {
        "run_id": item.run_id,
        "head_policy": item.head_policy,
        "candidate_policy": item.candidate_policy,
        "model_verdict": item.model_verdict,
    }


def _iso(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ArgillaReview:
    """Talks to an Argilla 2.x server. Pinned and tested against server v2.8.0 and SDK argilla==2.8.0."""

    def __init__(self, api_url: str, api_key: str, workspace: str, dataset_name: str = DEFAULT_DATASET):
        self.api_url = api_url
        self.api_key = api_key
        self.workspace = workspace
        self.dataset_name = dataset_name
        self._client = None
        self._users: dict[str, str] = {}

    # --- connection -------------------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            import argilla as rg
            self._client = rg.Argilla(api_url=self.api_url, api_key=self.api_key)
        return self._client

    def _settings(self):
        import argilla as rg
        return rg.Settings(
            guidelines=GUIDELINES,
            fields=[
                rg.TextField(name="what_changed", title="What changed", use_markdown=False),
                rg.TextField(name="old_wording", title="Old wording", use_markdown=False),
                rg.TextField(name="new_wording", title="New wording", use_markdown=False),
                rg.TextField(name="section", title="Section that may be affected", use_markdown=False),
                rg.TextField(name="model_answer", title="What the program said", use_markdown=False),
            ],
            questions=[
                rg.LabelQuestion(
                    name=Q_AFFECTED,
                    title="Is this policy section affected by the change?",
                    labels=dict(DECISION_LABELS),
                    required=True,
                ),
                rg.LabelQuestion(
                    name=Q_KIND,
                    title="What kind of impact?",
                    description="Only needed if you answered Yes.",
                    labels={k: IMPACT_LABELS[k] for k in IMPACT_TYPES},
                    required=False,
                ),
                rg.TextQuestion(
                    name=Q_NOTE,
                    title="Anything we should know? (optional)",
                    description="For example: why you disagree with the program.",
                    required=False,
                    use_markdown=False,
                ),
            ],
            metadata=[
                rg.TermsMetadataProperty(name="run_id", title="Run"),
                rg.TermsMetadataProperty(name="head_policy", title="Changed policy"),
                rg.TermsMetadataProperty(name="candidate_policy", title="Other policy"),
                rg.TermsMetadataProperty(name="model_verdict", title="Program's answer", options=list(DECISIONS)),
            ],
            allow_extra_metadata=True,
        )

    # --- dataset ----------------------------------------------------------------------------
    def ensure_dataset(self) -> None:
        """Create the workspace and dataset if they are missing. Safe to call every time."""
        import argilla as rg
        if self.client.workspaces(self.workspace) is None:
            rg.Workspace(name=self.workspace, client=self.client).create()
        if self.client.datasets(self.dataset_name, workspace=self.workspace) is None:
            rg.Dataset(name=self.dataset_name, workspace=self.workspace,
                       settings=self._settings(), client=self.client).create()

    def _dataset(self):
        ds = self.client.datasets(self.dataset_name, workspace=self.workspace)
        if ds is None:
            self.ensure_dataset()
            ds = self.client.datasets(self.dataset_name, workspace=self.workspace)
        return ds

    # --- push -------------------------------------------------------------------------------
    def push(self, items: Iterable[ReviewItem]) -> int:
        """Add judgements to the queue. A judgement already there (by id) is skipped, so reviewer
        work is never overwritten. Returns how many were added."""
        import argilla as rg
        items = list(items)
        if not items:
            return 0
        ds = self._dataset()
        have = {str(r.id) for r in ds.records(with_suggestions=False, with_responses=False)}
        records, seen = [], set()
        for it in items:
            rid = str(it.judgement_id)
            if rid in have or rid in seen:
                continue
            seen.add(rid)
            suggestions = []
            if it.model_verdict in DECISIONS:
                suggestions.append(rg.Suggestion(Q_AFFECTED, it.model_verdict, agent="judge", type="model"))
            if it.impact_type in IMPACT_TYPES:
                suggestions.append(rg.Suggestion(Q_KIND, it.impact_type, agent="judge", type="model"))
            records.append(rg.Record(id=rid, fields=record_fields(it), metadata=record_metadata(it),
                                     suggestions=suggestions))
        if records:
            ds.records.log(records)
        return len(records)

    # --- pull -------------------------------------------------------------------------------
    def _username(self, user_id) -> str:
        key = str(user_id)
        if key not in self._users:
            try:
                for u in self.client.users:
                    self._users[str(u.id)] = u.username
            except Exception:  # listing users needs an owner key; fall back to the id
                pass
            self._users.setdefault(key, key)
        return self._users[key]

    def _records_with_responses(self, ds, page: int = 500):
        """All records with their responses, from the REST API. The Python SDK drops each answer's
        status and time, and we need both."""
        offset = 0
        while True:
            r = self.client.http_client.get(
                f"/api/v1/datasets/{ds.id}/records",
                params={"include": "responses", "offset": offset, "limit": page})
            r.raise_for_status()
            rows = r.json().get("items", [])
            yield from rows
            if len(rows) < page:
                return
            offset += page

    def pull_submitted(self, since: str | None = None) -> list[ReviewDecision]:
        """Answers a reviewer has submitted (not drafts, not discarded). If several people answered
        the same card, the latest answer wins. `since` is an ISO time: skip answers saved before it."""
        floor = _parse_iso(since) if since else None
        ds = self._dataset()
        out: list[ReviewDecision] = []
        for rec in self._records_with_responses(ds):
            try:
                jid = int(str(rec.get("external_id")))
            except ValueError:
                continue
            best = None
            for resp in rec.get("responses", []):
                if resp.get("status") != "submitted":
                    continue
                value = ((resp.get("values") or {}).get(Q_AFFECTED) or {}).get("value")
                if value not in DECISIONS:
                    continue
                when = _parse_iso(resp["updated_at"])
                if best is None or when > best[0]:
                    best = (when, resp, value)
            if best is None:
                continue
            when, resp, value = best
            if floor and when < floor:
                continue
            values = resp.get("values") or {}
            kind = (values.get(Q_KIND) or {}).get("value") or ""
            if kind not in IMPACT_TYPES:
                kind = ""
            if value == "no":
                kind = "none"
            note = str((values.get(Q_NOTE) or {}).get("value") or "").strip()
            out.append(ReviewDecision(jid, value, kind, note, self._username(resp.get("user_id")), _iso(when)))
        return sorted(out, key=lambda d: (d.reviewed_at, d.judgement_id))


class FakeReview:
    """In-memory stand-in with the same methods. For tests and demo mode."""

    def __init__(self, dataset_name: str = DEFAULT_DATASET):
        self.dataset_name = dataset_name
        self.ready = False
        self.items: dict[int, ReviewItem] = {}
        self.answers: dict[int, ReviewDecision] = {}

    def ensure_dataset(self) -> None:
        self.ready = True

    def push(self, items: Iterable[ReviewItem]) -> int:
        self.ensure_dataset()
        added = 0
        for it in items:
            if it.judgement_id not in self.items:
                self.items[it.judgement_id] = it
                added += 1
        return added

    def pull_submitted(self, since: str | None = None) -> list[ReviewDecision]:
        floor = _parse_iso(since) if since else None
        rows = [d for d in self.answers.values() if not floor or _parse_iso(d.reviewed_at) >= floor]
        return sorted(rows, key=lambda d: (d.reviewed_at, d.judgement_id))

    # test/demo helper: act as a reviewer
    def submit(self, judgement_id: int, decision: str, impact_type: str = "", note: str = "",
               reviewer: str = "demo-reviewer", reviewed_at: str | None = None) -> None:
        if judgement_id not in self.items:
            raise KeyError(judgement_id)
        if decision not in DECISIONS:
            raise ValueError(decision)
        if decision == "no":
            impact_type = "none"
        self.answers[judgement_id] = ReviewDecision(
            judgement_id, decision, impact_type, note, reviewer,
            reviewed_at or datetime.now(timezone.utc).isoformat())
