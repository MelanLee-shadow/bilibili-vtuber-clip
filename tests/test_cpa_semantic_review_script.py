import json
import subprocess
import sys
from pathlib import Path


def test_cpa_semantic_review_script_writes_request_and_accepts_json_response(tmp_path):
    responder = tmp_path / "fake_cpa.py"
    responder.write_text(
        "import json, sys\n"
        "request=json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "json.dump({\n"
        " 'schema_version':'cpa-semantic-review-response.v1',\n"
        " 'candidate_id': request['candidate_id'],\n"
        " 'request_sha256': request['request_sha256'],\n"
        " 'release_ready': True,\n"
        " 'semantic_complete': True,\n"
        " 'terminology_ok': True,\n"
        " 'title_hook_score': 0.9,\n"
        " 'context_dependency_score': 0.1,\n"
        " 'unsafe_upload_risk_score': 0.1,\n"
        " 'reason_codes': [],\n"
        " 'required_fixes': [],\n"
        " 'provider': {'name': 'fake-cpa', 'request_id': 'test'},\n"
        " 'artifact_paths': {'request_path': request['artifact_paths']['request_path'], 'response_path': sys.argv[2]},\n"
        " 'evidence': {'summary': 'ok', 'terminology_findings': [], 'semantic_findings': []}\n"
        "}, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)\n",
        encoding="utf-8",
    )
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/cpa_semantic_review.py",
            "--candidate-id",
            "candidate-1",
            "--candidate-text",
            "kmx被骗到了，最后展示环节结束",
            "--normalized-text",
            "kmx被骗到了，最后展示环节结束",
            "--request-json",
            str(request),
            "--response-json",
            str(response),
            "--cpa-command",
            f"{sys.executable} {responder} {{request_json}} {{response_json}}",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    response_payload = json.loads(response.read_text(encoding="utf-8"))
    assert request_payload["schema_version"] == "cpa-semantic-review-request.v1"
    assert request_payload["candidate_id"] == "candidate-1"
    assert request_payload["request_sha256"].startswith("sha256:")
    # applied_terms is now the full glossary canon (dynamically parsed), not the
    # historic single ["kmx"]; kmx must still be present.
    applied_terms = request_payload["terminology"]["applied_terms"]
    assert "kmx" in applied_terms
    assert len(applied_terms) > 1
    for canon in ("142", "小室", "Ado", "沙豆李", "奶油苏打"):
        assert canon in applied_terms, canon
    # the ASR mishearing blacklist rides along in metadata so the CPA judge's
    # terminology_ok gate can check the normalized text no longer contains them.
    blacklist = request_payload["metadata"]["terminology_blacklist"]
    for variant in ("停放熊", "沙特琳", "一四二", "苏丹"):
        assert variant in blacklist, variant
    assert response_payload["candidate_id"] == "candidate-1"
    assert response_payload["request_sha256"] == request_payload["request_sha256"]
    assert response_payload["terminology_ok"] is True


def test_cpa_semantic_review_script_fails_closed_on_invalid_response(tmp_path):
    responder = tmp_path / "bad_cpa.py"
    responder.write_text("open(__import__('sys').argv[2], 'w').write('not json')\n", encoding="utf-8")
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/cpa_semantic_review.py",
            "--candidate-id",
            "candidate-1",
            "--candidate-text",
            "天不熊被骗到了",
            "--normalized-text",
            "kmx被骗到了",
            "--request-json",
            str(request),
            "--response-json",
            str(response),
            "--cpa-command",
            f"{sys.executable} {responder} {{request_json}} {{response_json}}",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "CPA_SEMANTIC_QA_INVALID_JSON" in completed.stderr
