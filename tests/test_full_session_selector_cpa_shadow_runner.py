import json
import subprocess
import sys
from pathlib import Path


def test_full_session_selector_cpa_shadow_runner_uses_cpa_response_path(tmp_path):
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n有个事我真的绷不住\n\n"
        "2\n00:00:10,000 --> 00:00:12,000\n她说先别急\n\n"
        "3\n00:00:20,000 --> 00:00:23,000\n结果下一秒就翻车了\n\n"
        "4\n00:00:30,000 --> 00:00:34,000\n最后大家都笑了\n",
        encoding="utf-8",
    )
    responder = tmp_path / "fake_cpa.py"
    responder.write_text(
        "import json, sys\n"
        "req=json.load(open(sys.argv[1], encoding='utf-8'))\n"
        "json.dump({\n"
        " 'schema_version':'cpa-semantic-review-response.v1',\n"
        " 'candidate_id': req['candidate_id'],\n"
        " 'request_sha256': req['request_sha256'],\n"
        " 'release_ready': True, 'semantic_complete': True, 'terminology_ok': True,\n"
        " 'title_hook_score': 0.9, 'context_dependency_score': 0.1, 'unsafe_upload_risk_score': 0.1,\n"
        " 'reason_codes': [], 'required_fixes': [],\n"
        " 'provider': {'name': 'fake-cpa', 'request_id': 'test'},\n"
        " 'artifact_paths': {'request_path': req['artifact_paths']['request_path'], 'response_path': sys.argv[2]},\n"
        " 'evidence': {'summary':'ok', 'terminology_findings': [], 'semantic_findings': []}\n"
        "}, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_full_session_selector_cpa_shadow.py",
            "--source-video",
            str(source_video),
            "--source-srt",
            str(source_srt),
            "--output-dir",
            str(output_dir),
            "--room-id",
            "22966160",
            "--cpa-command",
            f"{sys.executable} {responder} {{request_json}} {{response_json}}",
            "--copy-draft-context",
            "--no-ffmpeg",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    record = summary["records"][0]
    evidence = json.loads(Path(record["evidence_path"]).read_text(encoding="utf-8"))
    assert record["source_context_job"]["candidate_id"].startswith("fullctx_")
    assert record["source_context_job"]["cpa_semantic_request_path"].endswith(".cpa.request.json")
    assert record["source_context_job"]["cpa_semantic_response_path"].endswith(".cpa.response.json")
    assert evidence["metadata"]["cpa_semantic_qa"]["response_path"].endswith(".cpa.response.json")
    assert Path(record["cpa_request_json"]).is_file()
    assert Path(record["cpa_response_json"]).is_file()
    assert Path(record["cpa_response_json"]).parent.name == "cpa"
