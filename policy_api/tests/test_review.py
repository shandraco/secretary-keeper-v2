"""Review queue. The FakeReview tests always run. The Argilla test needs a real server:

    ARGILLA_TEST_URL=http://localhost:6900 ARGILLA_TEST_KEY=<owner api key> pytest tests/test_review.py
"""
import os
import uuid

import pytest

from app.prompts import IMPACT_TYPES
from app.review import (DECISIONS, ArgillaReview, FakeReview, ReviewDecision, ReviewItem,
                        record_fields, record_metadata)


def item(i: int, verdict: str = "yes", kind: str = "needs_update") -> ReviewItem:
    return ReviewItem(
        judgement_id=i, run_id="run-1", head_policy="POL-001", head_change_label="section 3 changed",
        old_text="Keep records 3 years.", new_text="Keep records 7 years.",
        candidate_policy="POL-002", candidate_title="Records", candidate_number=f"4.{i}",
        candidate_text="Records are kept for 3 years.", model_verdict=verdict, impact_type=kind,
        why="It repeats the old 3 year period.", evidence_head="3 years", evidence_candidate="3 years")


def test_fields_are_plain_text_with_the_model_answer():
    f = record_fields(item(1))
    assert set(f) == {"what_changed", "old_wording", "new_wording", "section", "model_answer"}
    assert "POL-001" in f["what_changed"] and "Records are kept" in f["section"]
    assert "YES" in f["model_answer"] and "old 3 year period" in f["model_answer"]
    assert record_metadata(item(1)) == {"run_id": "run-1", "head_policy": "POL-001",
                                        "candidate_policy": "POL-002", "model_verdict": "yes"}


def test_empty_text_is_shown_as_none_not_blank():
    it = item(1)
    blank = ReviewItem(**{**it.__dict__, "old_text": "", "evidence_head": ""})
    f = record_fields(blank)
    assert f["old_wording"] == "(none)" and "Quoted from the changed policy" not in f["model_answer"]


def test_fake_push_is_idempotent():
    r = FakeReview()
    assert r.push([item(1), item(2)]) == 2
    assert r.push([item(1), item(2), item(3)]) == 1
    assert sorted(r.items) == [1, 2, 3]


def test_fake_pull_only_returns_submitted_answers():
    r = FakeReview()
    r.ensure_dataset()
    r.push([item(1), item(2), item(3)])
    assert r.pull_submitted() == []
    r.submit(1, "no", note="clause is about something else", reviewer="ana")
    r.submit(2, "yes", "conflict", reviewer="bo")
    got = r.pull_submitted()
    assert [d.judgement_id for d in got] == [1, 2]
    assert got[0] == ReviewDecision(1, "no", "none", "clause is about something else", "ana", got[0].reviewed_at)
    assert got[1].impact_type == "conflict"


def test_fake_pull_since_filters_older_answers():
    r = FakeReview()
    r.push([item(1), item(2)])
    r.submit(1, "yes", "conflict", reviewed_at="2026-01-01T00:00:00+00:00")
    r.submit(2, "unsure", reviewed_at="2026-03-01T00:00:00+00:00")
    assert [d.judgement_id for d in r.pull_submitted(since="2026-02-01T00:00:00Z")] == [2]


def test_fake_rejects_unknown_input():
    r = FakeReview()
    r.push([item(1)])
    with pytest.raises(KeyError):
        r.submit(9, "yes")
    with pytest.raises(ValueError):
        r.submit(1, "maybe")


def test_argilla_pull_reads_status_and_picks_latest(monkeypatch):
    """Parsing of the server's answer, without a server."""
    rows = [
        {"external_id": "7", "responses": [
            {"status": "draft", "user_id": "u1", "updated_at": "2026-05-01T10:00:00.1",
             "values": {"affected": {"value": "yes"}}},
            {"status": "submitted", "user_id": "u1", "updated_at": "2026-05-01T11:00:00.1",
             "values": {"affected": {"value": "yes"}, "impact_type": {"value": "conflict"},
                        "note": {"value": " check this "}}},
            {"status": "submitted", "user_id": "u2", "updated_at": "2026-05-01T12:00:00.1",
             "values": {"affected": {"value": "no"}, "impact_type": {"value": "conflict"}}}]},
        {"external_id": "8", "responses": [{"status": "discarded", "user_id": "u1",
                                            "updated_at": "2026-05-01T11:00:00", "values": None}]},
        {"external_id": "not-ours", "responses": []},
    ]
    r = ArgillaReview("http://x", "k", "ws")
    monkeypatch.setattr(r, "_dataset", lambda: object())
    monkeypatch.setattr(r, "_records_with_responses", lambda ds: iter(rows))
    monkeypatch.setattr(r, "_username", lambda uid: {"u1": "ana", "u2": "bo"}[uid])
    got = r.pull_submitted()
    assert len(got) == 1
    assert got[0].decision == "no" and got[0].impact_type == "none" and got[0].reviewer == "bo"
    assert got[0].reviewed_at.startswith("2026-05-01T12:00:00")
    assert r.pull_submitted(since="2026-05-01T13:00:00Z") == []


def test_no_network_at_import_or_construction():
    ArgillaReview("http://does-not-exist.invalid", "k", "ws")  # must not connect


# --- real server --------------------------------------------------------------------------------
URL, KEY = os.environ.get("ARGILLA_TEST_URL"), os.environ.get("ARGILLA_TEST_KEY")


@pytest.mark.skipif(not (URL and KEY), reason="set ARGILLA_TEST_URL and ARGILLA_TEST_KEY to run against Argilla")
def test_real_argilla_round_trip():
    import argilla as rg

    name = f"test-{uuid.uuid4().hex[:8]}"
    review = ArgillaReview(URL, KEY, os.environ.get("ARGILLA_TEST_WORKSPACE", "policy-review"), dataset_name=name)
    try:
        review.ensure_dataset()
        review.ensure_dataset()                                   # idempotent
        assert review.push([item(1), item(2, "no", "none"), item(3, "unsure", "none")]) == 3
        assert review.push([item(1), item(2, "no", "none"), item(3, "unsure", "none")]) == 0   # no duplicates
        ds = review._dataset()
        assert len(list(ds.records())) == 3
        assert review.pull_submitted() == []                      # nothing answered yet

        # Pre-filled model suggestions are present.
        first = next(iter(ds.records(with_suggestions=True)))
        assert {s.question_name for s in first.suggestions} >= {"affected"}

        # A reviewer starts card 2 (a draft: must be ignored) and submits card 1.
        me = review.client.me
        recs = {str(r.id): r for r in ds.records(with_responses=True)}
        recs["2"].responses.add(rg.Response("affected", "yes", user_id=me.id, status="draft"))
        rec = recs["1"]
        rec.responses.add(rg.Response("affected", "yes", user_id=me.id, status="submitted"))
        rec.responses.add(rg.Response("impact_type", "conflict", user_id=me.id, status="submitted"))
        rec.responses.add(rg.Response("note", "model missed that this is a hard limit", user_id=me.id, status="submitted"))
        ds.records.log([rec, recs["2"]])

        got = review.pull_submitted()
        assert len(got) == 1
        d = got[0]
        assert (d.judgement_id, d.decision, d.impact_type) == (1, "yes", "conflict")
        assert d.note == "model missed that this is a hard limit"
        assert d.reviewer == me.username and d.reviewed_at.endswith("+00:00")
        assert review.pull_submitted(since="2999-01-01T00:00:00Z") == []
        assert review.pull_submitted(since="2000-01-01T00:00:00Z") == got

        # Pushing again must not touch the answered card.
        assert review.push([item(1), item(4)]) == 1
        assert review.pull_submitted() == got
    finally:
        ds = review.client.datasets(name, workspace=review.workspace)
        if ds is not None:
            ds.delete()
