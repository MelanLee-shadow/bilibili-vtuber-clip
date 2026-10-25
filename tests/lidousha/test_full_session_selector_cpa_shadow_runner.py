import json
import subprocess
import sys
from pathlib import Path

import pytest


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
        cwd=Path(__file__).resolve().parents[2],
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
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["selector_stage"] == "seeded_song_anchor"
    record = summary["records"][0]
    assert record["candidate_id"] == "seededsong_1000_19000"
    assert record["source_context_job"]["schema_version"] == (
        "source-context-job-from-full-session-candidate.v1"
    )
    assert summary["last_shadow_summary"]["records"][0]["source_context_job"][
        "schema_version"
    ] == record["source_context_job"]["schema_version"]
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
        "2\n00:00:05,000 --> 00:00:20,000\n好像阿朵\n\n"
        "3\n00:00:21,000 --> 00:00:38,000\n这真的不是融了阿朵吗\n\n"
        "4\n00:00:39,000 --> 00:01:10,000\n一眼AI 好吧\n",
        encoding="utf-8",
    )
    recall = tmp_path / "fake_recall.py"
    recall.write_text(
        "import json, sys\n"
        "completion = json.dumps({'candidates': [{'start_cue': 2, 'end_cue': 4, 'kind': 'talk',"
        " 'hook': '一眼AI连环吐槽', 'context_trigger_cue': 1, 'context_inferable': False, 'confidence': 0.9,"
        " 'selection_scorecard': {"
        "'tier': 2, 'tier_basis': 'personal_stance',"
        "'tier_reason': '片内连续反应形成完整吐槽链', 'tier_evidence_cues': [2, 3, 4],"
        "'dimensions': {'lidousha_centrality': 4, 'stance_intensity': 3,"
        "'audience_salience': 3, 'relationship_interaction': 2, 'persona_reversal': 3,"
        "'comedic_payoff': 3, 'self_contained': 4}, 'uncertainty_penalty': 0,"
        "'fatigue_penalty': 0, 'candidate_quality': {'watchability': 'STRONG',"
        "'differentiation': 'DISTINCTIVE', 'production_disposition': 'PRODUCTION',"
        "'reason': '片内完整的一眼AI连环吐槽', 'evidence_cues': [2, 3, 4]}"
        "}}]},"
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
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["selector_stage"] == "semantic_recall"
    record = summary["records"][0]
    # The context trigger (cue 1, reading out the image) was pulled into the window.
    assert record["candidate_id"] == "semantictalk_1000_70000"
    assert record["source_context_job"]["boundary_authority"] == "semantic"
    diagnostics = json.loads((output_dir / "semantic_recall.json").read_text(encoding="utf-8"))
    assert diagnostics["hooks"]["semantictalk_1000_70000"] == "一眼AI连环吐槽"


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
        cwd=Path(__file__).resolve().parents[2],
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


def test_cpa_requests_use_complete_ordered_cues_for_initial_and_expanded_windows(tmp_path, monkeypatch):
    """The CPA payload must include payoff text beyond the short preview.

    This exercises the actual request writer on both the initial candidate and
    its viewer-context expansion, rather than only checking the helper's
    returned string.
    """

    from types import SimpleNamespace

    import importlib

    from src.autoslice.auto_review import DecisionAction
    from src.autoslice.boundary_resolver import AnchorCandidate, BoundaryResolution
    from src.autoslice.full_session_candidate_selector import FullSessionCandidate
    from src.autoslice.review_evidence import SourceCue

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"fake video")
    source_srt = tmp_path / "source.srt"
    source_srt.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\n前置触发\n\n"
        "2\n00:00:10,000 --> 00:00:12,000\n核心起点\n\n"
        "3\n00:00:12,000 --> 00:00:15,000\n我在疯狂逼迫老头\n\n"
        "4\n00:00:15,000 --> 00:00:19,000\n尾部反向酒桌吐槽\n",
        encoding="utf-8",
    )
    responder = _write_ok_cpa_responder(tmp_path, viewer_context_branch=True)
    output_dir = tmp_path / "out"

    cues = (
        SourceCue("u_1", 1_000, 4_000, "前置触发", language="zh"),
        SourceCue("u_2", 10_000, 12_000, "核心起点", language="zh"),
        SourceCue("u_3", 12_000, 15_000, "我在疯狂逼迫老头", language="zh"),
        SourceCue("u_4", 15_000, 19_000, "尾部反向酒桌吐槽", language="zh"),
    )
    anchor = AnchorCandidate(
        candidate_id="semantictalk_10000_19000",
        anchor_start_ms=10_000,
        anchor_end_ms=19_000,
    )
    boundary = BoundaryResolution(
        candidate_id=anchor.candidate_id,
        action=DecisionAction.AUTO_RECUT,
        resolved_start_ms=10_000,
        resolved_end_ms=19_000,
        start_boundary_score=0.9,
        end_boundary_score=0.9,
        reason_codes=(),
        next_start_ms=10_000,
        next_end_ms=19_000,
    )
    candidate = FullSessionCandidate(
        anchor=anchor,
        boundary=boundary,
        cues=cues[1:],
        text_preview="截断预览",
        content_type_hint="talk",
    )
    args = SimpleNamespace(
        source_srt=source_srt,
        source_video=source_video,
        output_dir=output_dir,
        max_candidates=1,
        room_id="22966160",
        cpa_command=f"{sys.executable} {responder} {{request_json}} {{response_json}}",
        agy_ssh_host=None,
        copy_draft_context=False,
        agy_audio_lrc_align=False,
        song_lrc_query=(),
        canonical_lrc_file=None,
        canonical_lrc_title=None,
        canonical_lrc_artist=None,
        canonical_lrc_source_ref=None,
        lrc_provider="none",
        no_ffmpeg=True,
        song_hint_llm_command=None,
        title_llm_command=None,
        cover_art_direction_llm_command=None,
        publish_staging=False,
        burn_preview=False,
    )
    monkeypatch.setattr(
        shadow,
        "run_shadow_pipeline",
        lambda **_kwargs: {"records": [{"decision_action": "AUTO_RECUT"}]},
    )

    records, _summary = shadow._review_selected_candidates(
        args=args,
        candidates=[candidate],
        cues=cues,
        source_duration_ms=19_000,
        selector_stage="semantic_recall",
        danmaku_items=[],
        host_vocal_prover=None,
        branding_intro=None,
    )

    assert records[0]["viewer_context_expansion"]["expanded_candidate_id"].endswith("_ctxexp")
    request_paths = sorted((output_dir / candidate.anchor.candidate_id / "cpa").glob("*.cpa.request.json"))
    assert len(request_paths) == 2
    requests = {json.loads(path.read_text(encoding="utf-8"))["candidate_id"]: json.loads(path.read_text(encoding="utf-8")) for path in request_paths}
    original_text = requests["semantictalk_10000_19000"]["candidate_text"]
    expanded_text = requests["semantictalk_1000_19000_ctxexp"]["candidate_text"]
    assert original_text.splitlines() == ["核心起点", "我在疯狂逼迫老头", "尾部反向酒桌吐槽"]
    assert expanded_text.splitlines() == ["前置触发", "核心起点", "我在疯狂逼迫老头", "尾部反向酒桌吐槽"]
    assert "尾部反向酒桌吐槽" in original_text
    assert "尾部反向酒桌吐槽" in expanded_text
    assert original_text != candidate.text_preview


def test_cpa_pronoun_ta_pass_resolves_each_occurrence_bidirectionally():
    """The final pronoun pass sees existing TA as well as 他/她/它 and applies
    occurrence-scoped rewrites without letting the model re-emit cue text."""
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    srt = (
        "1\n00:00:00,000 --> 00:00:02,000\n我想跟他说\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\nTA想问的是三个位置哦\n\n"
        "3\n00:00:04,000 --> 00:00:06,000\n她说那只猫饿了就喂他，还有其他人在\n"
    )

    def fake_cpa(prompt):
        assert "最终定稿代词" in prompt and "TA想问的是三个位置哦" in prompt
        assert "李豆沙、礼墨Sumi、安晚Awa" in prompt
        return json.dumps(
            {
                "rewrites": [
                    {"n": 1, "occurrence": 1, "from": "他", "to": "TA"},
                    {"n": 2, "occurrence": 1, "from": "TA", "to": "她"},
                    {"n": 3, "occurrence": 2, "from": "他", "to": "它"},
                ]
            },
            ensure_ascii=False,
        )

    out = shadow._cpa_pronoun_ta_pass(srt, cpa_llm_call=fake_cpa)
    assert "我想跟TA说" in out
    assert "她想问的是三个位置哦" in out
    assert "她说那只猫饿了就喂它" in out
    assert "还有其他人在" in out  # 其他 is not a pronoun → untouched (guards against 其他→其TA)

    # Cheap gate: 其他/他们/TA们 and TA inside an ASCII token are not singular
    # pronoun candidates, so the LLM must not be called.
    only_qita = "1\n00:00:00,000 --> 00:00:02,000\n还有其他他们的东西，TA们来了，META也来了\n"
    assert shadow._cpa_pronoun_ta_pass(only_qita, cpa_llm_call=lambda p: 1 / 0) == only_qita
    # Empty rewrite list (e.g. named figure 司马懿 already correct) → no rewrite.
    named = "1\n00:00:00,000 --> 00:00:02,000\n他后来就造反了\n"
    assert shadow._cpa_pronoun_ta_pass(named, cpa_llm_call=lambda p: '{"rewrites":[]}') == named


def test_cpa_pronoun_ta_pass_rejects_stale_or_invalid_occurrence_rewrites():
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    srt = "1\n00:00:00,000 --> 00:00:02,000\nTA问她信不信\n"

    out = shadow._cpa_pronoun_ta_pass(
        srt,
        cpa_llm_call=lambda _prompt: json.dumps(
            {
                "rewrites": [
                    {"n": 1, "occurrence": 1, "from": "她", "to": "他"},
                    {"n": 1, "occurrence": 2, "from": "她", "to": "未知"},
                    {"n": 99, "occurrence": 1, "from": "TA", "to": "她"},
                ]
            },
            ensure_ascii=False,
        ),
    )
    assert out == srt


def test_cpa_pronoun_ta_pass_fails_open_on_llm_error():
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    srt = "1\n00:00:00,000 --> 00:00:02,000\n我想跟他说\n"

    def boom(prompt):
        raise RuntimeError("cpa down")

    # fail-open: returns the pre-pass text unchanged
    assert shadow._cpa_pronoun_ta_pass(srt, cpa_llm_call=boom) == srt


def test_canonical_lrc_cli_freezes_file_and_records_provenance(tmp_path):
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    lrc_path = tmp_path / "canonical.lrc"
    lrc_path.write_text(
        "[00:01.00]WORLD.execute(me);\n[00:03.50]EXECUTION L0-O\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    args = shadow._build_parser().parse_args(
        [
            "--source-video",
            str(tmp_path / "source.mp4"),
            "--source-srt",
            str(tmp_path / "source.srt"),
            "--output-dir",
            str(output_dir),
            "--cpa-command",
            "unused",
            "--canonical-lrc-file",
            str(lrc_path),
            "--canonical-lrc-title",
            "Canonical song",
            "--canonical-lrc-artist",
            "Artist",
            "--canonical-lrc-source-ref",
            "official://canonical-song",
        ]
    )

    result, provenance_path = shadow._prepare_canonical_lrc_input(args)
    assert result is not None
    assert provenance_path is not None
    assert result.song_title == "Canonical song"
    assert result.artist == "Artist"
    assert result.source_ref == "official://canonical-song"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["file_sha256"] == provenance["copied_file_sha256"]
    assert provenance["timed_line_count"] == 2
    assert provenance["source_ref"] == "official://canonical-song"

    # The provider uses the validated in-memory result, so changing the caller
    # file after the preflight cannot silently change the candidate.
    provider = shadow._build_lrc_provider(args, result)
    lrc_path.write_text("[00:01.00]TAMPERED\n", encoding="utf-8")
    found = provider("ignored query")
    assert len(found) == 1
    assert [line.text for line in found[0].lines] == ["WORLD.execute(me);", "EXECUTION L0-O"]


def test_canonical_lrc_cli_rejects_blank_file_and_keeps_default_provider_lane(tmp_path):
    import importlib

    shadow = importlib.import_module("scripts.run_full_session_selector_cpa_shadow")
    from src.autoslice.song_lrc_provider import load_canonical_lrc_result

    blank = tmp_path / "blank.lrc"
    blank.write_text("[00:00.00]作词 : unknown\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no timed lyric lines"):
        load_canonical_lrc_result(
            lrc_path=blank,
            song_title="Blank",
            artist=None,
            source_ref="official://blank",
        )

    args = shadow._build_parser().parse_args(
        [
            "--source-video",
            str(tmp_path / "source.mp4"),
            "--source-srt",
            str(tmp_path / "source.srt"),
            "--output-dir",
            str(tmp_path / "out"),
            "--cpa-command",
            "unused",
        ]
    )
    assert shadow._build_lrc_provider(args, None) is None
