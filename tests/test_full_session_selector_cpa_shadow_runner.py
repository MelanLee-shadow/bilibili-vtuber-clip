import json
import subprocess
import sys
from pathlib import Path


def _write_ok_cpa_responder(tmp_path: Path, *, viewer_context_branch: bool = False) -> Path:
    """Fake CPA judge: always passes; with viewer_context_branch it flags the
    first (non-expanded) candidate as viewer-context-incomplete with an
    expansion suggestion, and passes the _ctxexp retry."""

    responder = tmp_path / "fake_cpa.py"
    branch = (
        "incomplete = '_ctxexp' not in req['candidate_id']\n"
        if viewer_context_branch
        else "incomplete = False\n"
    )
    responder.write_text(
        "import json, sys\n"
        "req=json.load(open(sys.argv[1], encoding='utf-8'))\n"
        + branch
        + "json.dump({\n"
        " 'schema_version':'cpa-semantic-review-response.v1',\n"
        " 'candidate_id': req['candidate_id'],\n"
        " 'request_sha256': req['request_sha256'],\n"
        " 'release_ready': not incomplete, 'semantic_complete': True, 'terminology_ok': True,\n"
        " 'title_hook_score': 0.9, 'context_dependency_score': 0.1, 'unsafe_upload_risk_score': 0.1,\n"
        " 'reason_codes': ['VIEWER_CONTEXT_INCOMPLETE'] if incomplete else [],\n"
        " 'required_fixes': ['向前扩窗把触发点包进来'] if incomplete else [],\n"
        " 'provider': {'name': 'fake-cpa', 'request_id': 'test'},\n"
        " 'artifact_paths': {'request_path': req['artifact_paths']['request_path'], 'response_path': sys.argv[2]},\n"
        " 'evidence': {'summary':'ok', 'terminology_findings': [], 'semantic_findings': []},\n"
        " 'metadata': {'viewer_context': {'viewer_context_ok': not incomplete, 'expand_before_ms': 10000 if incomplete else 0, 'expand_after_ms': 0}}\n"
        "}, open(sys.argv[2], 'w', encoding='utf-8'), ensure_ascii=False)\n",
        encoding="utf-8",
    )
    return responder


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
    assert record["materialized_recut"] == summary["last_shadow_summary"]["records"][0]["materialized_recut"]
    assert record["title"] == summary["last_shadow_summary"]["records"][0]["title"]
    assert record["materialized_recut"]["status"] == "MATERIALIZED"
    assert evidence["metadata"]["cpa_semantic_qa"]["response_path"].endswith(".cpa.response.json")
    assert Path(record["cpa_request_json"]).is_file()
    assert Path(record["cpa_response_json"]).is_file()
    assert Path(record["cpa_response_json"]).parent.name == "cpa"
    request_payload = json.loads(Path(record["cpa_request_json"]).read_text(encoding="utf-8"))
    assert request_payload["metadata"]["content_type_hint"] == "talk"


def test_seeded_song_candidate_bypasses_wide_window_talk_recall():
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    from src.autoslice.review_evidence import SourceCue

    cues = [
        SourceCue("song-1", 45_000, 55_000, "最初に望んだ未来とは", kind="singing"),
        SourceCue("song-2", 190_000, 200_000, "最後は", kind="singing"),
        SourceCue("talk", 228_000, 244_000, "年度晚安，大家晚安", kind="speech"),
    ]
    candidate = shadow._seeded_song_candidate(
        cues,
        candidate_id="seededsong_45000_200540",
        anchor_start_ms=45_000,
        anchor_end_ms=200_540,
        source_duration_ms=245_566,
    )

    assert candidate.content_type_hint == "song"
    assert [cue.cue_id for cue in candidate.cues] == ["song-1", "song-2"]
    job = candidate.to_source_context_job(source_duration_ms=245_566)
    assert job["timeline"]["context_start_ms"] == 0
    assert job["timeline"]["context_end_ms"] == 245_566
    assert job["song_candidate"] is True
    assert job["requires_full_source_song_boundary_redo"] is True


def test_seeded_song_cli_bypasses_empty_semantic_recall(tmp_path):
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:05,000\n最初に望んだ未来とは\n\n"
        "2\n00:00:12,000 --> 00:00:19,000\n最後はなにもいらない\n\n"
        "3\n00:00:22,000 --> 00:00:25,000\n大家晚安\n",
        encoding="utf-8",
    )
    # If the seed is honored this command is never called.  Without the seed
    # the same zero/failed recall would lead to NO_FULL_SESSION_CANDIDATES for
    # sparse Japanese ASR.
    broken_recall = tmp_path / "broken_recall.py"
    broken_recall.write_text("raise SystemExit(99)\n", encoding="utf-8")
    responder = _write_ok_cpa_responder(tmp_path)
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
            "--source-duration-ms",
            "26000",
            "--seed-song-candidate-id",
            "seededsong_1000_19000",
            "--seed-song-anchor-start-ms",
            "1000",
            "--seed-song-anchor-end-ms",
            "19000",
            "--semantic-recall-llm-command",
            f"{sys.executable} {broken_recall} {{prompt_file}} {{completion_file}}",
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
    assert summary["selector_stage"] == "seeded_song_anchor"
    record = summary["records"][0]
    assert record["candidate_id"] == "seededsong_1000_19000"
    assert record["source_context_job"]["song_candidate"] is True
    assert not (output_dir / "semantic_recall.json").exists()


def test_semantic_recall_lane_runs_first_and_marks_semantic_authority(tmp_path):
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_srt = tmp_path / "source.srt"
    # Reactive banter with no storytelling keywords: both keyword lanes
    # produce nothing usable from this — only the semantic lane can.
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n我们来看看这张AI生成的图\n\n"
        "2\n00:00:05,000 --> 00:00:09,000\n好像阿朵\n\n"
        "3\n00:00:10,000 --> 00:00:14,000\n这真的不是融了阿朵吗\n\n"
        "4\n00:00:15,000 --> 00:00:19,000\n一眼AI 好吧\n",
        encoding="utf-8",
    )
    recall = tmp_path / "fake_recall.py"
    recall.write_text(
        "import json, sys\n"
        "completion = json.dumps({'candidates': [{'start_cue': 2, 'end_cue': 4, 'kind': 'talk',"
        " 'hook': '一眼AI连环吐槽', 'context_trigger_cue': 1, 'context_inferable': False, 'confidence': 0.9}]},"
        " ensure_ascii=False)\n"
        "open(sys.argv[2], 'w', encoding='utf-8').write(completion)\n",
        encoding="utf-8",
    )
    responder = _write_ok_cpa_responder(tmp_path)
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
            "--semantic-recall-llm-command",
            f"{sys.executable} {recall} {{prompt_file}} {{completion_file}}",
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
    assert summary["selector_stage"] == "semantic_recall"
    record = summary["records"][0]
    # The context trigger (cue 1, reading out the image) was pulled into the window.
    assert record["candidate_id"] == "semantictalk_1000_19000"
    assert record["source_context_job"]["boundary_authority"] == "semantic"
    diagnostics = json.loads((output_dir / "semantic_recall.json").read_text(encoding="utf-8"))
    assert diagnostics["hooks"]["semantictalk_1000_19000"] == "一眼AI连环吐槽"


def test_viewer_context_incomplete_expands_window_and_rereviews(tmp_path):
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n我们来看看这张AI生成的图\n\n"
        "2\n00:00:12,000 --> 00:00:15,000\n有个事我真的绷不住\n\n"
        "3\n00:00:20,000 --> 00:00:23,000\n结果下一秒就翻车了\n\n"
        "4\n00:00:30,000 --> 00:00:34,000\n最后大家都笑了\n",
        encoding="utf-8",
    )
    responder = _write_ok_cpa_responder(tmp_path, viewer_context_branch=True)
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
    expansion = record["viewer_context_expansion"]
    assert expansion is not None
    assert expansion["expanded_candidate_id"].endswith("_ctxexp")
    assert expansion["expanded_start_ms"] < expansion["original_start_ms"]
    assert record["candidate_id"] == expansion["expanded_candidate_id"]
    # The expanded window was re-reviewed: a second request/response pair exists.
    assert Path(record["cpa_request_json"]).name.startswith(expansion["expanded_candidate_id"])
    assert Path(record["cpa_response_json"]).is_file()
    # Expanded windows carry semantic authority in the job manifest.
    assert record["source_context_job"]["boundary_authority"] == "semantic"


def test_cpa_pronoun_ta_pass_converts_unknown_gender_he_to_ta():
    """Dedicated pronoun pass: the MODEL judges which cue numbers refer to an
    unknown-gender person, the CODE mechanically rewrites 他/她 → TA in exactly
    those cues.  Other cues, 其他/他们, and the timeline stay untouched; the LLM
    is skipped entirely when the clip has no personal pronoun."""
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    srt = (
        "1\n00:00:00,000 --> 00:00:02,000\n我想跟他说\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n他就看傻子一样看我\n\n"
        "3\n00:00:04,000 --> 00:00:06,000\n还有其他人在\n"
    )

    def fake_cpa(prompt):
        # numbers-only judgement prompt, sees the whole clip + candidate list
        assert "判断代词" in prompt and "候选编号" in prompt and "他就看傻子" in prompt
        return '{"ta_cues":[1,2]}'

    out = shadow._cpa_pronoun_ta_pass(srt, cpa_llm_call=fake_cpa)
    assert "我想跟TA说" in out and "TA就看傻子一样看我" in out  # cues 1,2 rewritten
    assert out.count("TA") == 2
    assert "还有其他人在" in out  # 其他 is not a pronoun → untouched (guards against 其他→其TA)

    # Cheap gate: only 其他/他们 (no bare pronoun) → never calls the LLM.
    only_qita = "1\n00:00:00,000 --> 00:00:02,000\n还有其他他们的东西\n"
    assert shadow._cpa_pronoun_ta_pass(only_qita, cpa_llm_call=lambda p: 1 / 0) == only_qita
    # Empty ta_cues (e.g. named figure 司马懿) → no rewrite.
    named = "1\n00:00:00,000 --> 00:00:02,000\n他后来就造反了\n"
    assert shadow._cpa_pronoun_ta_pass(named, cpa_llm_call=lambda p: '{"ta_cues":[]}') == named


def test_cpa_pronoun_ta_pass_fails_open_on_llm_error():
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    srt = "1\n00:00:00,000 --> 00:00:02,000\n我想跟他说\n"

    def boom(prompt):
        raise RuntimeError("cpa down")

    # fail-open: returns the pre-pass text unchanged
    assert shadow._cpa_pronoun_ta_pass(srt, cpa_llm_call=boom) == srt
