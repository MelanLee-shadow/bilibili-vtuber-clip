import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import huozi_luanshua as huozi_cli
from src.autoslice.huozi_luanshua import (
    SOURCE_MANIFEST_SCHEMA,
    VERIFICATION_SCHEMA,
    CorpusValidationError,
    UncoveredTextError,
    VerificationError,
    apply_verification,
    build_corpus,
    build_verification,
    normalize_text,
    plan_text,
    rank_suggestions,
    validate_renderable_plan,
)


def _utterance(text: str, start_ms: int = 0, *, grouped: bool = False) -> dict:
    if grouped:
        words = [{"label": text, "start_time": start_ms, "end_time": start_ms + 600}]
    else:
        words = [
            {
                "label": character,
                "start_time": start_ms + index * 120,
                "end_time": start_ms + (index + 1) * 120,
            }
            for index, character in enumerate(text)
        ]
    return {
        "start_time": start_ms,
        "end_time": words[-1]["end_time"],
        "transcript": text,
        "words": words,
    }


def _source_manifest(tmp_path, utterances, **overrides):
    asr = tmp_path / "source.json"
    asr.write_text(json.dumps({"utterances": utterances}, ensure_ascii=False), encoding="utf-8")
    second_asr = tmp_path / "source-second.json"
    second_asr.write_text(
        json.dumps({"utterances": utterances}, ensure_ascii=False), encoding="utf-8"
    )
    media = tmp_path / "solo.mp4"
    media.write_bytes(b"test-media")
    speaker_one = tmp_path / "speaker-human.json"
    speaker_one.write_text('{"authority":"human"}\n', encoding="utf-8")
    speaker_two = tmp_path / "speaker-acoustic.json"
    speaker_two.write_text('{"authority":"acoustic"}\n', encoding="utf-8")
    source = {
        "source_id": "solo-20260710",
        "source_date": "2026-07-10",
        "media_path": str(media),
        "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
        "asr_json_path": str(asr),
        "speaker": "lidousha",
        "speaker_confidence": 1.0,
        "speaker_authority": "ivan_confirmed_solo_session",
        "transcript_confidence": 0.95,
        "transcript_authorities": ["bcut_word_timestamps", "second_asr"],
        "transcript_evidence": [
            {
                "authority": "bcut_word_timestamps",
                "path": str(asr),
                "confidence": 0.99,
            },
            {"authority": "second_asr", "path": str(second_asr), "confidence": 0.99},
        ],
        "speaker_evidence": [
            {"authority": "human", "path": str(speaker_one)},
            {"authority": "acoustic", "path": str(speaker_two)},
        ],
        "content_kind": "talk",
    }
    source.update(overrides)
    return {"schema_version": SOURCE_MANIFEST_SCHEMA, "sources": [source]}


def _corpus(tmp_path, texts):
    utterances = [_utterance(text, index * 2_000) for index, text in enumerate(texts)]
    return build_corpus(_source_manifest(tmp_path, utterances), manifest_dir=tmp_path)


def _verification_for(plan, *, observed_override=None, authorities=None, confidence=0.99):
    pieces = {}
    for piece in plan["pieces"]:
        source_evidence = piece["transcript_evidence"]
        selected_authorities = authorities or [
            str(source_evidence[0]["authority"]),
            str(source_evidence[1]["authority"]),
        ]
        pieces[piece["piece_id"]] = {
            "status": "VERIFIED",
            "observed_text": (
                observed_override
                if observed_override is not None
                else f"前文{piece['text']}后文"
            ),
            "transcript_authorities": selected_authorities,
            "transcript_confidence": confidence,
            "media_sha256": piece["media_sha256"],
            "evidence": [
                {
                    "authority": authority,
                    "path": source_evidence[index % len(source_evidence)]["path"],
                    "sha256": source_evidence[index % len(source_evidence)]["sha256"],
                    "observed_text": (
                        observed_override
                        if observed_override is not None
                        else f"前文{piece['text']}后文"
                    ),
                }
                for index, authority in enumerate(selected_authorities)
            ],
        }
    return {
        "schema_version": VERIFICATION_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "pieces": pieces,
    }


def test_normalize_text_keeps_spoken_characters_and_drops_punctuation():
    assert normalize_text("我本来就是零，不对！") == "我本来就是零不对"
    assert normalize_text("ＡI 0.5") == "ai05"


def test_corpus_requires_explicit_high_confidence_lidousha_authority(tmp_path):
    manifest = _source_manifest(
        tmp_path,
        [_utterance("我本来")],
        speaker_authority="automatic_text_context",
    )
    with pytest.raises(CorpusValidationError, match="untrusted speaker authority"):
        build_corpus(manifest, manifest_dir=tmp_path)


def test_corpus_excludes_non_talk_ranges(tmp_path):
    manifest = _source_manifest(
        tmp_path,
        [_utterance("我本来", 0), _utterance("为爱做零", 3_000)],
        excluded_ranges_ms=[[2_500, 5_000, "singing"]],
    )
    corpus = build_corpus(manifest, manifest_dir=tmp_path)
    assert [row["text"] for row in corpus["utterances"]] == ["我本来"]


def test_collab_source_only_promotes_fully_contained_trusted_ranges(tmp_path):
    manifest = _source_manifest(
        tmp_path,
        [
            _utterance("礼墨说的", 0),
            _utterance("我要为爱做零", 3_000),
            _utterance("边界外", 6_000),
        ],
        source_id="collab-20260712",
        speaker="mixed",
        speaker_confidence=0.0,
        speaker_authority="unreviewed_collab_session",
        trusted_ranges_ms=[
            {
                "start_ms": 2_900,
                "end_ms": 3_800,
                "speaker_authority": "verified_lidousha_voiceprint",
                "speaker_confidence": 0.995,
            }
        ],
    )

    corpus = build_corpus(manifest, manifest_dir=tmp_path)

    assert [row["text"] for row in corpus["utterances"]] == ["我要为爱做零"]
    assert corpus["utterances"][0]["speaker_authority"] == "verified_lidousha_voiceprint"


def test_planner_prefers_few_long_natural_segments_and_reuses_verified_zero(tmp_path):
    corpus = _corpus(
        tmp_path,
        [
            "我本来就是",
            "零",
            "不对",
            "我要为爱做",
            *list("我本来就是不对我要为爱做零"),
        ],
    )
    plan = plan_text("我本来就是零，不对，我要为爱做零", corpus)

    assert [piece["text"] for piece in plan["pieces"]] == [
        "我本来就是",
        "零",
        "不对",
        "我要为爱做",
        "零",
    ]
    assert plan["quality"] == {
        "character_count": 14,
        "piece_count": 5,
        "single_character_piece_count": 2,
        "longest_piece_characters": 5,
        "average_piece_characters": 2.8,
        "multi_character_coverage_ratio": 0.8571,
        "fragmented": True,
    }


def test_planner_prefers_fuller_single_syllable_at_clause_end(tmp_path):
    short_dir = tmp_path / "short"
    full_dir = tmp_path / "full"
    short_dir.mkdir()
    full_dir.mkdir()
    short_zero = {
        "start_time": 0,
        "end_time": 480,
        "transcript": "零食不对",
        "words": [
            {"label": "零", "start_time": 0, "end_time": 120},
            {"label": "食", "start_time": 120, "end_time": 240},
            {"label": "不", "start_time": 240, "end_time": 360},
            {"label": "对", "start_time": 360, "end_time": 480},
        ],
    }
    full_zero = {
        "start_time": 1_000,
        "end_time": 1_480,
        "transcript": "做零了",
        "words": [
            {"label": "做", "start_time": 1_000, "end_time": 1_120},
            {"label": "零", "start_time": 1_120, "end_time": 1_360},
            {"label": "了", "start_time": 1_360, "end_time": 1_480},
        ],
    }
    short_manifest = _source_manifest(
        short_dir,
        [short_zero],
        source_id="short-high-confidence",
        speaker_confidence=1.0,
    )
    full_manifest = _source_manifest(
        full_dir,
        [full_zero],
        source_id="full-terminal-syllable",
        speaker_confidence=0.99,
    )
    corpus = build_corpus(
        {
            "schema_version": SOURCE_MANIFEST_SCHEMA,
            "sources": [
                short_manifest["sources"][0],
                full_manifest["sources"][0],
            ],
        },
        manifest_dir=tmp_path,
    )

    terminal_plan = plan_text("零", corpus)
    assert terminal_plan["pieces"][0]["source_id"] == "full-terminal-syllable"
    assert terminal_plan["pieces"][0]["core_end_ms"] - terminal_plan["pieces"][0]["core_start_ms"] == 240

    nonterminal_plan = plan_text("零不对", corpus)
    assert nonterminal_plan["pieces"][0]["source_id"] == "short-high-confidence"


def test_match_must_align_to_asr_token_boundaries(tmp_path):
    # A provider may occasionally return a multi-character token.  Cutting
    # "本来" out of that token would invent timings, so it must not match.
    corpus = build_corpus(
        _source_manifest(tmp_path, [_utterance("我本来", grouped=True)]),
        manifest_dir=tmp_path,
    )
    with pytest.raises(UncoveredTextError):
        plan_text("本来", corpus)
    assert plan_text("我本来", corpus)["quality"]["piece_count"] == 1


def test_suggestion_ranking_only_keeps_small_edits_that_reduce_fragmentation(tmp_path):
    corpus = _corpus(
        tmp_path,
        ["我本来就是", "零", "不对", "我要", "为", "爱", "做", "我要为了爱做零"],
    )
    ranked = rank_suggestions(
        "我本来就是零，不对，我要为爱做零",
        corpus,
        [
            {
                "text": "我本来就是零，不对，我要为了爱做零",
                "rationale": "加入了可连续命中的了",
                "meaning_preservation": "含义不变",
            },
            {"text": "我完全不是零", "rationale": "改得太多"},
        ],
        max_edits=4,
    )
    assert [row["text"] for row in ranked] == ["我本来就是零，不对，我要为了爱做零"]
    assert ranked[0]["plan"]["quality"]["piece_count"] == 4


def test_build_verification_binds_two_timed_asr_files_and_zero_orthography(tmp_path):
    manifest = _source_manifest(tmp_path, [_utterance("为爱做零", 1_000)])
    source = manifest["sources"][0]
    jianying = tmp_path / "jianying.json"
    jianying.write_text(
        json.dumps(
            {
                "utterances": [
                    {
                        "start_time": 1_000,
                        "end_time": 1_480,
                        "transcript": "为爱做0",
                        "words": [
                            {
                                "label": character,
                                "start_time": 1_000 + index * 120,
                                "end_time": 1_000 + (index + 1) * 120,
                            }
                            for index, character in enumerate("为爱做0")
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source["transcript_authorities"] = ["bcut", "jianying"]
    source["transcript_evidence"] = [
        {"authority": "bcut", "path": source["asr_json_path"], "confidence": 0.99},
        {"authority": "jianying", "path": str(jianying), "confidence": 0.99},
    ]
    plan = plan_text("为爱做零", build_corpus(manifest, manifest_dir=tmp_path))

    verification = build_verification(plan, timing_tolerance_ms=200)
    verified = apply_verification(plan, verification)

    assert verified["status"] == "READY_TO_RENDER"
    assert verified["pieces"][0]["transcript_authorities"] == ["bcut", "jianying"]
    validate_renderable_plan(verified)


def test_planner_rejects_text_found_by_second_asr_at_the_wrong_time(tmp_path):
    manifest = _source_manifest(tmp_path, [_utterance("不对", 1_000)])
    source = manifest["sources"][0]
    late = tmp_path / "late.json"
    late.write_text(
        json.dumps({"utterances": [_utterance("不对", 9_000)]}, ensure_ascii=False),
        encoding="utf-8",
    )
    source["transcript_authorities"] = ["bcut", "late"]
    source["transcript_evidence"] = [
        {"authority": "bcut", "path": source["asr_json_path"]},
        {"authority": "late", "path": str(late)},
    ]
    corpus = build_corpus(manifest, manifest_dir=tmp_path)

    with pytest.raises(UncoveredTextError):
        plan_text("不对", corpus)


def test_render_gate_requires_two_independent_text_authorities_and_media_hash(tmp_path):
    plan = plan_text("我本来", _corpus(tmp_path, ["我本来"]))
    with pytest.raises(VerificationError, match="only a verified plan"):
        validate_renderable_plan(plan)

    one_authority = _verification_for(plan, authorities=["bcut"])
    with pytest.raises(VerificationError, match="two transcript authorities"):
        apply_verification(plan, one_authority)

    verified = apply_verification(plan, _verification_for(plan))
    validate_renderable_plan(verified)
    assert verified["status"] == "READY_TO_RENDER"
    assert verified["pieces"][0]["verification_status"] == "VERIFIED"


def test_render_gate_rechecks_verified_plan_and_transcript_evidence_hashes(tmp_path):
    plan = plan_text("不对", _corpus(tmp_path, ["不对"]))
    verified = apply_verification(plan, _verification_for(plan))

    edited = json.loads(json.dumps(verified))
    edited["pieces"][0]["text"] = "没错"
    with pytest.raises(VerificationError, match="verified plan hash drift"):
        validate_renderable_plan(edited)

    evidence_path = verified["pieces"][0]["verification_evidence"]["evidence"][0]["path"]
    with open(evidence_path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(VerificationError, match="transcript evidence hash drift"):
        validate_renderable_plan(verified)


def test_verification_is_hash_bound_and_must_observe_exact_piece_text(tmp_path):
    plan = plan_text("不对", _corpus(tmp_path, ["不对"]))
    wrong_plan = _verification_for(plan)
    wrong_plan["plan_sha256"] = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(VerificationError, match="different plan"):
        apply_verification(plan, wrong_plan)

    wrong_text = _verification_for(plan, observed_override="没错")
    with pytest.raises(VerificationError, match="not independently observed"):
        apply_verification(plan, wrong_text)


def test_verification_rejects_low_confidence_even_with_two_providers(tmp_path):
    plan = plan_text("我要", _corpus(tmp_path, ["我要"]))
    with pytest.raises(VerificationError, match="below render gate"):
        apply_verification(plan, _verification_for(plan, confidence=0.97))


def test_render_piece_uses_one_accurate_input_seek_for_subsecond_audio(tmp_path, monkeypatch):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source")
    destination = tmp_path / "piece.mp4"
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        destination.write_bytes(b"x" * 4_096)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(huozi_cli, "_run", fake_run)
    huozi_cli._render_piece(
        {
            "piece_id": "p001",
            "media_path": str(media),
            "media_sha256": "already-verified",
            "cut_start_ms": 12_345,
            "cut_end_ms": 12_545,
        },
        destination,
        actual_media_sha256="already-verified",
        lead_pause_ms=120,
    )

    command = commands[0]
    assert "-nostdin" in command
    assert command.count("-ss") == 1
    assert command[command.index("-ss") + 1] == "12.345"
    assert "tpad=start_mode=clone:start_duration=0.120" in command[command.index("-vf") + 1]
    assert "loudnorm=I=-18:LRA=7:TP=-2" in command[command.index("-af") + 1]
    assert "adelay=120|120" in command[command.index("-af") + 1]


def test_ffprobe_duration_falls_back_from_na_format_to_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(
        huozi_cli,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout='{"format":{"duration":"N/A"},"streams":[{"duration":"1.234"}]}'
        ),
    )
    assert huozi_cli._ffprobe_duration_ms(tmp_path / "piece.mp4") == 1_234


def test_final_encode_forces_cfr_and_retimestamps_audio(tmp_path):
    command = huozi_cli._final_encode_command(
        tmp_path / "clean.mp4", tmp_path / "subtitle.ass", tmp_path / "final.mp4"
    )

    assert "-nostdin" in command
    assert "fps=30" in command[command.index("-vf") + 1]
    assert command[command.index("-fps_mode") + 1] == "cfr"
    assert command[command.index("-af") + 1] == "aresample=48000:async=1:first_pts=0"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-avoid_negative_ts") + 1] == "make_zero"


def test_render_subtitles_are_clause_level_instead_of_flashing_each_fragment(tmp_path):
    plan = plan_text(
        "我本来就是零，不对，我要为爱做零",
        _corpus(tmp_path, ["我", "本来就是", "零", "不对", "我要", "为爱做零"]),
    )
    subtitle = tmp_path / "intro.srt"

    huozi_cli._write_plan_srt(plan, [200, 800, 200, 400, 400, 800], subtitle)

    rendered = subtitle.read_text(encoding="utf-8")
    assert rendered.count(" --> ") == 3
    assert "我本来就是零，" in rendered
    assert "我要为爱做零" in rendered
    assert "\n本来就是\n" not in rendered


def test_history_scan_finds_cross_cue_long_fragment_but_not_across_long_silence(tmp_path):
    transcript = tmp_path / "2026-07-12-source.srt"
    transcript.write_text(
        """1
00:00:00,000 --> 00:00:00,600
我本来

2
00:00:00,700 --> 00:00:01,200
就是

3
00:00:04,000 --> 00:00:04,300
零

4
00:00:05,000 --> 00:00:05,400
不对

5
00:00:08,000 --> 00:00:08,500
我要

6
00:00:10,500 --> 00:00:11,300
为爱做零
""",
        encoding="utf-8",
    )

    report = huozi_cli.scan_history_transcripts(
        "我本来就是零，不对，我要为爱做零",
        [transcript],
        max_gap_ms=1_500,
    )

    fragments = [row["fragment"] for row in report["matches"]]
    assert report["status"] == "DISCOVERY_ONLY_NOT_RENDERABLE"
    assert report["transcripts"][0]["sha256"] == hashlib.sha256(
        transcript.read_bytes()
    ).hexdigest()
    assert "我本来就是" in fragments
    assert "我要为爱做零" not in fragments
    assert all(row["promotion_status"] == "DISCOVERY_ONLY" for row in report["matches"])


def test_history_scan_keeps_distinct_partial_hit_even_when_full_hit_exists_in_same_group(tmp_path):
    transcript = tmp_path / "2026-07-12-mixed.srt"
    transcript.write_text(
        """1
00:00:00,000 --> 00:00:00,900
我本来就是零

2
00:00:01,000 --> 00:00:01,800
我本来就是
""",
        encoding="utf-8",
    )

    report = huozi_cli.scan_history_transcripts("我本来就是零", [transcript])
    hits = [(row["fragment"], row["source_interval_ms"]) for row in report["matches"]]

    assert ("我本来就是零", [0, 900]) in hits
    assert ("我本来就是", [1_000, 1_800]) in hits


def test_comparison_bundle_hash_binds_both_rendered_choices(tmp_path):
    def make_render(name, target):
        artifacts = {}
        for artifact_name, suffix in (("video", ".mp4"), ("subtitle_srt", ".srt"), ("subtitle_ass", ".ass")):
            path = tmp_path / f"{name}{suffix}"
            path.write_bytes(f"{name}-{artifact_name}".encode())
            artifacts[artifact_name] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        manifest = {
            "schema_version": huozi_cli.RENDER_MANIFEST_SCHEMA,
            "status": "REVIEW_READY_NO_UPLOAD",
            "upload_enabled": False,
            "target": target,
            "quality": {"piece_count": 2},
            "artifacts": artifacts,
        }
        manifest["manifest_sha256"] = hashlib.sha256(
            huozi_cli.canonical_json_bytes(manifest)
        ).hexdigest()
        path = tmp_path / f"{name}.manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path, artifacts["video"]["path"]

    original_target = "我本来就是零，不对，我要为爱做零"
    suggested_target = "我本来就是零，不对，我就为爱做零"
    original_manifest, _ = make_render("original", original_target)
    suggested_manifest, suggested_video = make_render("suggested", suggested_target)
    suggestion_report = tmp_path / "suggestion-report.json"
    suggestion_report.write_text(
        json.dumps(
            {
                "schema_version": huozi_cli.SUGGESTION_REPORT_SCHEMA,
                "target": original_target,
                "recommendation": {
                    "text": suggested_target,
                    "edit_distance": 1,
                    "rationale": "减少切点",
                    "meaning_preservation": "保留原意",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    bundle = huozi_cli.build_comparison_manifest(
        original_manifest, suggested_manifest, suggestion_report
    )

    assert bundle["status"] == "USER_CHOICE_REQUIRED_NO_UPLOAD"
    assert [choice["target"] for choice in bundle["choices"]] == [
        original_target,
        suggested_target,
    ]
    selected = huozi_cli.build_comparison_manifest(
        original_manifest,
        suggested_manifest,
        suggestion_report,
        selection="original",
    )
    assert selected["status"] == "USER_SELECTED_NO_UPLOAD"
    assert selected["selection"] == "original"
    assert selected["upload_enabled"] is False

    Path(suggested_video).write_bytes(b"drift")
    with pytest.raises(RuntimeError, match="artifact hash drift"):
        huozi_cli.build_comparison_manifest(
            original_manifest, suggested_manifest, suggestion_report
        )
