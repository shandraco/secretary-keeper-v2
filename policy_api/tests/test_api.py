import time

import pytest
from fastapi.testclient import TestClient

KEY = {"Authorization": "Bearer demo"}


@pytest.fixture
def client(samples, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SAMPLES_DIR", str(samples))
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    from app.main import app

    with TestClient(app) as c:
        yield c


def _wait(client, run_id):
    for _ in range(100):
        body = client.get(f"/runs/{run_id}", headers=KEY).json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_needs_api_key(client):
    assert client.get("/subsets").status_code == 401
    assert client.get("/subsets", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/subsets", headers=KEY).status_code == 200


def test_rejects_unknown_host_header(client):
    # DNS-rebinding guard: requests must name one of our hostnames.
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/health").status_code == 200


def test_lookup_subsets_and_policies(client):
    assert client.get("/subsets", params={"q": "safety"}, headers=KEY).json()["subsets"][0]["name"] == "Safety"
    assert client.get("/policies", params={"q": "lockout"}, headers=KEY).json()["policies"][0]["id"] == "POL-007"


def test_upload_run_and_download(client, samples):
    with open(samples / "changes" / "POL-007-v4.md", "rb") as f:
        up = client.post("/uploads", files={"file": ("POL-007-v4.md", f, "text/markdown")}, headers=KEY).json()
    assert up["matches"][0] == {"id": "POL-007", "title": "Lockout/Tagout", "score": 1.0}

    run = client.post("/runs", json={"head_policy": "POL-007", "subset": "safety", "upload_id": up["upload_id"]}, headers=KEY)
    assert run.status_code == 202
    done = _wait(client, run.json()["run_id"])
    assert done["status"] == "done"
    assert done["counts"]["impacted"] == 2
    assert "Download the full comparison" in done["answer_markdown"]

    url = done["excel_url"].removeprefix("http://testserver")
    xlsx = client.get(url)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    assert client.get(url.replace("sig=", "sig=0")).status_code == 403


def test_without_upload_needs_two_versions(client):
    run = client.post("/runs", json={"head_policy": "POL-007", "subset": "Safety"}, headers=KEY).json()
    done = _wait(client, run["run_id"])
    assert done["status"] == "failed"
    assert "Only one version" in done["error"]


def test_upload_rejects_unnumbered_text(client):
    r = client.post("/uploads", files={"file": ("note.txt", b"just some words", "text/plain")}, headers=KEY)
    assert r.status_code == 400


def _finished_run(client, samples):
    with open(samples / "changes" / "POL-007-v4.md", "rb") as f:
        up = client.post("/uploads", files={"file": ("POL-007-v4.md", f, "text/markdown")}, headers=KEY).json()
    run = client.post("/runs", json={"head_policy": "POL-007", "subset": "Safety", "upload_id": up["upload_id"]}, headers=KEY)
    return _wait(client, run.json()["run_id"])["run_id"], up["upload_id"]


def _get(client, url):
    return client.get(url.removeprefix("http://testserver"))


def test_run_files_reports_and_charts(client, samples):
    run_id, _ = _finished_run(client, samples)
    r = client.post(f"/runs/{run_id}/files", json={"formats": ["pdf", "docx", "charts"]}, headers=KEY)
    assert r.status_code == 200
    files = r.json()["files"]
    assert [f["file"][36:] for f in files] == [".pdf", ".docx", "-status.png", "-matrix.png", "-types.png"]
    pdf, docx, png = (_get(client, files[i]["url"]) for i in (0, 1, 2))
    assert pdf.content[:4] == b"%PDF" and "inline" in pdf.headers["content-disposition"]
    assert docx.content[:2] == b"PK" and "attachment" in docx.headers["content-disposition"]
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n" and png.headers["content-type"] == "image/png"
    assert "policy-impact-POL-007-" in pdf.headers["content-disposition"]
    assert client.post(f"/runs/{run_id}/files", json={"formats": ["exe"]}, headers=KEY).status_code == 422
    assert client.post(f"/runs/{run_id}/files", json={"formats": ["pdf"]}).status_code == 401


def test_compare_versions_and_upload(client, samples):
    _, upload_id = _finished_run(client, samples)
    r = client.post("/compare", json={"left": {"policy_id": "POL-007"}, "right": {"upload_id": upload_id}, "format": "pdf"}, headers=KEY)
    assert r.status_code == 200, r.text
    f = r.json()["files"][0]
    assert "POL-007" in f["label"] and _get(client, f["url"]).content[:4] == b"%PDF"
    missing = client.post("/compare", json={"left": {"policy_id": "POL-007", "version": 9}, "right": {"policy_id": "SAF-112"}}, headers=KEY)
    assert missing.status_code == 404 and "Stored versions" in missing.json()["detail"]


def test_assistant_documents(client):
    spec = {"title": "Lockout summary", "sections": [
        {"heading": "Numbers", "table": {"columns": ["Policy", "Sections"], "rows": [["SAF-112", 2], ["=HYPERLINK(\"http://x\")", 1]]},
         "chart": {"type": "bar", "title": "Sections", "labels": ["SAF-112", "SAF-130"], "series": [{"name": "n", "values": [2, 2]}]}}]}
    for fmt, magic in (("docx", b"PK"), ("pdf", b"%PDF"), ("xlsx", b"PK"), ("png", b"\x89PNG")):
        r = client.post("/documents", json={"spec": spec, "format": fmt}, headers=KEY)
        assert r.status_code == 200, r.text
        assert _get(client, r.json()["files"][0]["url"]).content.startswith(magic)
    bad = client.post("/documents", json={"spec": {"title": "x", "sections": [{"chart": {"type": "radar"}}]}}, headers=KEY)
    assert bad.status_code == 422


def test_download_names_are_strict(client, samples):
    run_id, _ = _finished_run(client, samples)
    url = client.post(f"/runs/{run_id}/files", json={"formats": ["pdf"]}, headers=KEY).json()["files"][0]["url"]
    path = url.removeprefix("http://testserver")
    query = path.split("?", 1)[1]
    assert client.get(f"/exports/{run_id}.docx?{query}").status_code == 403      # signature is per file
    assert client.get(f"/exports/..%2F..%2Fetc%2Fpasswd?{query}").status_code == 404
    assert client.get(f"/exports/{run_id}.exe?{query}").status_code == 404
