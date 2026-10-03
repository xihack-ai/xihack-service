"""File imports use real AnyDoc; labelled evidence tests workflow isolation offline."""

from fastapi.testclient import TestClient
import io
import zipfile
import pytest

from app.main import app
from app import resume_parser
from app.workflow import graph, nodes


def rtf(text="Built and delivered Python backend services for order processing."):
    return ("{\\rtf1\\ansi " + text + "}").encode()


@pytest.fixture
def client():
    return TestClient(app)


def job(client, *, confirm=False):
    response = client.post("/jobs", json={"title": "Upload test", "requirement": "Python backend delivery",
                                          "require_spec_confirmation": True})
    assert response.status_code == 200
    value = response.json()
    if confirm:
        value = client.post(f"/calibrations/{value['job_id']}/spec/confirm", json={
            "criteria": [{"id": "c1", "type": "MUST_HAVE", "text": "Python backend delivery", "weight": 3}]
        }).json()
    return value


def upload(client, jid, files=None):
    return client.post(f"/calibrations/{jid}/candidates/upload", files=files or [
        ("files", ("candidate.rtf", rtf(), "application/rtf"))])


def enable_labelled_evaluator(monkeypatch, calls):
    monkeypatch.setenv("LLM_API_KEY", "test-not-a-provider-key")
    monkeypatch.setenv("REPLAY_ONLY", "0")
    def extract(candidate, criteria):
        calls.append(candidate["id"])
        return [{"candidate_id": candidate["id"], "criterion_id": c["id"],
                 "quote": "Python backend", "stance": "support", "valid": False} for c in criteria]
    monkeypatch.setattr(nodes, "_extract_for_candidate", extract)


def test_real_anydoc_parses_rtf_and_preserves_filename():
    candidate = resume_parser.parse_resume(rtf(), "../candidate.rtf", "application/rtf")
    assert candidate["name"] == "candidate"
    assert candidate["source_file"] == "candidate.rtf"
    assert "Python backend" in candidate["resume_text"]
    assert candidate["parser_version"].startswith("anydoc-")


def test_real_anydoc_parses_docx():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Built and delivered Python backend services for orders.</w:t></w:r></w:p></w:body></w:document>')
    candidate = resume_parser.parse_resume(buffer.getvalue(), "candidate.docx")
    assert "Python backend" in candidate["resume_text"]


@pytest.mark.parametrize("data,filename", [(b"", "empty.pdf"), (b"fake", "broken.pdf"),
                                             (rtf(), "unsupported.exe"), (b"a,b\n1,2", "renamed.pdf")])
def test_invalid_files_are_rejected(data, filename):
    with pytest.raises(resume_parser.ResumeParseError):
        resume_parser.parse_resume(data, filename)


def test_file_size_limit():
    with pytest.raises(resume_parser.ResumeParseError, match="10 MB"):
        resume_parser.parse_resume(b"x" * (resume_parser.MAX_FILE_BYTES + 1), "big.pdf")


def test_upload_before_confirmation_preserves_pause_and_defers_evaluation(client, monkeypatch):
    value = job(client)
    jid = value["job_id"]
    response = upload(client, jid)
    assert response.status_code == 200
    value = response.json()["calibration"]
    assert value["status"] == "AWAITING_SPEC_CONFIRMATION"
    assert len(value["candidates"]) == 1 and value["evaluations"] == []
    cid = value["candidates"][0]["candidate_id"]
    assert client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [cid]}).status_code == 409
    monkeypatch.setattr(nodes, "_extract_for_candidate", lambda *args: pytest.fail("confirmation must not evaluate file imports"))
    confirmed = client.post(f"/calibrations/{jid}/spec/confirm", json={"criteria": value["criteria"]})
    assert confirmed.status_code == 200
    assert confirmed.json()["workflow"]["waiting_for"] is None
    assert confirmed.json()["evaluations"] == []


def test_mixed_batch_duplicate_and_preview(client):
    jid = job(client, confirm=True)["job_id"]
    response = upload(client, jid, [("files", ("first.rtf", rtf(), "application/rtf")),
                                   ("files", ("same.rtf", rtf(), "application/rtf")),
                                   ("files", ("broken.pdf", b"not a pdf", "application/pdf"))])
    assert response.status_code == 200
    out = response.json()
    assert [r["status"] for r in out["results"]] == ["imported", "duplicate", "failed"]
    assert len(out["calibration"]["candidates"]) == 1
    assert out["calibration"]["candidates"][0]["name"] == "first"
    cid = out["results"][0]["candidate_id"]
    preview = client.get(f"/calibrations/{jid}/candidates/{cid}/resume")
    assert "Python backend" in preview.json()["resume_text"]
    repeated = upload(client, jid).json()
    assert repeated["results"][0]["status"] == "duplicate"
    assert repeated["results"][0]["candidate_id"] == cid
    other_job = job(client)["job_id"]
    assert client.get(f"/calibrations/{other_job}/candidates/{cid}/resume").status_code == 404


def test_only_new_candidates_evaluated_without_regenerating_spec(client, monkeypatch):
    value = job(client, confirm=True)
    jid = value["job_id"]
    value = upload(client, jid).json()["calibration"]
    first = value["candidates"][0]["candidate_id"]
    calls = []
    enable_labelled_evaluator(monkeypatch, calls)
    response = client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [first]})
    assert response.status_code == 200
    initial = response.json()
    assert initial["spec_version"] == 1 and initial["criteria"] == value["criteria"]
    assert initial["evaluations"][0]["verdict"] == "推荐"
    assert initial["candidates"][0]["evaluated"]
    assert client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [first]}).status_code == 200
    assert calls == [first]
    imported = upload(client, jid, [("files", ("second.rtf", rtf("Python backend developer responsible for payments and billing."), "application/rtf"))]).json()["calibration"]
    assert imported["evaluations"] == initial["evaluations"]
    second = next(c["candidate_id"] for c in imported["candidates"] if not c["evaluated"])
    final = client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [second]}).json()
    assert len(final["evaluations"]) == 2 and calls == [first, second]
    assert all(e["valid"] for e in final["evidence"])
    assert final["workflow"]["completed_nodes"].count("generate_spec") == 1


def test_offline_evaluation_is_explicit_error(client, monkeypatch):
    jid = job(client, confirm=True)["job_id"]          # 先用假模型建好职位
    monkeypatch.undo()                                  # 恢复真实离线环境后再评估
    cid = upload(client, jid).json()["results"][0]["candidate_id"]
    response = client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [cid]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "LIVE_MODEL_REQUIRED"


def test_failed_evaluation_can_retry_and_blocks_other_mutations(client, monkeypatch):
    from hirespec_loop.errors import ModelTaskError
    jid = job(client, confirm=True)["job_id"]
    cid = upload(client, jid).json()["results"][0]["candidate_id"]
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("REPLAY_ONLY", "0")
    def fail(*args):
        raise ModelTaskError("synthetic provider failure")
    monkeypatch.setattr(nodes, "_extract_for_candidate", fail)
    assert client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [cid]}).status_code == 503
    assert client.get(f"/calibrations/{jid}").json()["evaluations"] == []
    assert upload(client, jid).status_code == 409
    assert client.post(f"/calibrations/{jid}/feedback", json={"text": "change criteria"}).status_code == 409
    calls = []
    enable_labelled_evaluator(monkeypatch, calls)
    assert client.post(f"/calibrations/{jid}/candidates/evaluate", json={"candidate_ids": [cid]}).status_code == 200
    assert calls == [cid]


def test_upload_during_human_approval_is_rejected(client):
    jid = job(client, confirm=True)["job_id"]
    g = graph.get_graph()
    g.update_state({"configurable": {"thread_id": jid}},
                   {"change_proposal": {"reason": "test", "add": [{"text": "test"}], "remove": []}},
                   as_node="propose_change")
    assert "human_approval" in g.get_state({"configurable": {"thread_id": jid}}).next
    assert upload(client, jid).status_code == 409


def test_unknown_job_does_not_import(client):
    assert upload(client, "missing").status_code == 404


def test_max_batch_count(client):
    jid = job(client)["job_id"]
    assert upload(client, jid, [("files", (f"{i}.rtf", rtf(), "application/rtf")) for i in range(51)]).status_code == 422


def test_batch_size_rejection_does_not_import_partial_candidates(client, monkeypatch):
    jid = job(client)["job_id"]
    monkeypatch.setattr(resume_parser, "MAX_BATCH_BYTES", 1)
    assert upload(client, jid).status_code == 413
    assert client.get(f"/calibrations/{jid}").json()["candidates"] == []


def test_spec_upgrade_keeps_unevaluated_uploads_pending(client):
    jid = job(client, confirm=True)["job_id"]
    cid = upload(client, jid).json()["results"][0]["candidate_id"]
    proposed = client.post(f"/calibrations/{jid}/feedback", json={"text": "更看重分布式系统设计"})
    assert proposed.status_code == 200
    assert proposed.json()["status"] == "AWAITING_APPROVAL"
    accepted = client.post(f"/calibrations/{jid}/approval", json={"action": "accept"}).json()
    assert accepted["spec_version"] == 2
    assert accepted["evaluations"] == []
    assert accepted["candidates"][0]["candidate_id"] == cid
    assert not accepted["candidates"][0]["evaluated"]
