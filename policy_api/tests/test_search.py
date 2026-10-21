"""Answering from the database: section search and reading policy text."""
import pytest
from fastapi.testclient import TestClient

KEY = {"Authorization": "Bearer demo"}


@pytest.fixture
def client(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_search_finds_sections_and_respects_scope(client):
    r = client.get("/search", params={"q": "contractor access permit"}, headers=KEY).json()["results"]
    assert r and all({"policy_id", "policy_title", "clause", "text", "score"} <= set(x) for x in r)
    assert r == sorted(r, key=lambda x: -x["score"])
    only = client.get("/search", params={"q": "inspection", "policy": "SAF-112"}, headers=KEY).json()["results"]
    assert only and {x["policy_id"] for x in only} == {"SAF-112"}
    hr = client.get("/search", params={"q": "inspection", "group": "hr"}, headers=KEY).json()["results"]
    assert {x["policy_id"] for x in hr} <= {"HR-201"}
    assert client.get("/search", params={"q": "x y", "group": "nope"}, headers=KEY).status_code == 404
    assert client.get("/search", params={"q": "inspection"}).status_code == 401


def test_read_policy_whole_and_one_section(client):
    whole = client.get("/policies/POL-007/text", headers=KEY).json()
    assert whole["version"] == max(whole["versions"]) and "4.2" in whole["text"]
    one = client.get("/policies/POL-007/text", params={"clause": "§4"}, headers=KEY).json()
    assert one["text"].startswith("§4") and "§4.2" in one["text"] and "§3" not in one["text"]
    assert client.get("/policies/POL-007/text", params={"clause": "99"}, headers=KEY).status_code == 404
    assert client.get("/policies/POL-007/text", params={"version": 99}, headers=KEY).status_code == 404
