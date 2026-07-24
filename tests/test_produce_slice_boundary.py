"""Topic-closure boundary rules for finished clips (Ivan 2026-07-04): both
cuts must land on complete-sentence boundaries near the semantic targets, and
run-on cues near the closure trigger a fine micro-pass instead of a bad cut."""

import json

import pytest

from scripts.produce_slice_package import (
    ISLAND_CONTINUES_FLAG_MS,
    _load_superchats,
    _rebase_remote_speaker_manifest,
    _validated_burned_artifact,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
    syntactic_tail_audit,
    tail_requires_forward_extension,
)
from src.autoslice import producer_boundary_resolution as boundary_resolution
from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    _select_initial_boundary,
)
from src.autoslice.recovery_title_authority import (
    ROOT,
    build_recovery_publication_authorities,
)
from src.autoslice.subtitle_timing_qa import SpeechSpan


def test_load_superchats_video_relative_and_deduped(tmp_path):
    """SC (on-screen 醒目留言) come from the blrec .jsonl, mapped to video-relative
    time using the earliest event as t=0, and the CN/JPN twin events dedup by
    message (Ivan 2026-07-07: 结合画面SC)."""
    jsonl = tmp_path / "seg.jsonl"
    rows = [
        {"cmd": "DANMU_MSG", "send_time": 1000_000},  # earliest event → recording start
        {"cmd": "SUPER_CHAT_MESSAGE", "send_time": 1030_000,
         "data": {"message": "想看她唱地球大爆炸", "user_info": {"uname": "小凑るう子"}}},
        {"cmd": "SUPER_CHAT_MESSAGE_JPN", "send_time": 1030_050,
         "data": {"message": "想看她唱地球大爆炸", "message_jpn": "..."}},
        {"cmd": "SUPER_CHAT_MESSAGE",
         "data": {"send_time": 1090_000, "message": "可以跟lmsm学谢礼物", "user_info": {"uname": "十麻乃orient"}}},
    ]
    jsonl.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    scs = _load_superchats(jsonl)
    # video-relative ms, full sender uname carried, JPN twin deduped by message
    assert scs == [(30_000, "小凑るう子", "想看她唱地球大爆炸"), (90_000, "十麻乃orient", "可以跟lmsm学谢礼物")]
    assert _load_superchats(tmp_path / "missing.jsonl") == []


def test_delivery_burn_binding_ignores_coexisting_old_render(tmp_path):
    import hashlib

    old = tmp_path / "clip.recut.burned-final-sapphire72.mp4"
    old.write_bytes(b"old single-colour render")
    speaker = tmp_path / "clip.recut.burned-final-speaker.mp4"
    speaker.write_bytes(b"new speaker-colour render")
    digest = "sha256:" + hashlib.sha256(speaker.read_bytes()).hexdigest()
    record = {
        "burned_preview": {"status": "BURNED", "path": str(speaker), "burned_sha256": digest},
        "artifact_hashes": {"burned_video_sha256": digest},
    }
    assert _validated_burned_artifact(record) == speaker


def test_delivery_burn_binding_rejects_hash_drift(tmp_path):
    burned = tmp_path / "clip.recut.burned-final-speaker.mp4"
    burned.write_bytes(b"current")
    record = {
        "burned_preview": {"status": "BURNED", "path": str(burned), "burned_sha256": "sha256:stale"},
        "artifact_hashes": {"burned_video_sha256": "sha256:stale"},
    }
    with pytest.raises(RuntimeError, match="BURN_HASH_MISMATCH"):
        _validated_burned_artifact(record)


def test_remote_speaker_manifest_keeps_provenance_but_rebases_deleted_tmp_paths(tmp_path):
    media = tmp_path / "clip.mp4"
    text_srt = tmp_path / "text.srt"
    override = tmp_path / "override.json"
    output_srt = tmp_path / "speaker.srt"
    output_ass = tmp_path / "speaker.ass"
    manifest = {
        "source_media": "/tmp/run/media.mp4",
        "text_final_srt": "/tmp/run/text.srt",
        "speaker_override": "/tmp/run/overrides.json",
        "output_review_srt": "/tmp/run/speaker.srt",
        "output_ass": "/tmp/run/speaker.ass",
        "output_ass_sha256": "unchanged",
    }
    rebased = _rebase_remote_speaker_manifest(
        manifest,
        host="free",
        media_path=media,
        text_srt_path=text_srt,
        override_path=override,
        output_srt_path=output_srt,
        output_ass_path=output_ass,
    )
    assert rebased["runtime_host"] == "free"
    assert rebased["ephemeral_runtime_paths"]["source_media"] == "/tmp/run/media.mp4"
    assert rebased["source_media"] == str(media.resolve())
    assert rebased["output_ass"] == str(output_ass.resolve())
    assert rebased["output_ass_sha256"] == "unchanged"


def test_snap_end_picks_nearest_sentence_end():
    ends = [70_000, 79_900, 83_500, 96_000]
    assert snap_end_to_sentence(ends, 80_000) == 79_900
    assert snap_end_to_sentence(ends, 82_000) == 83_500


def test_snap_end_fails_closed_when_no_boundary_near_target():
    assert snap_end_to_sentence([10_000, 40_000], 25_000) is None


def test_snap_start_opens_on_a_sentence():
    starts = [2_600, 3_100, 9_000]
    assert snap_start_to_sentence(starts, 3_000) == 3_100
    assert snap_start_to_sentence([9_000], 3_000) is None


def _cue(start_ms, end_ms, text="x"):
    return SrtCue(index="1", start_ms=start_ms, end_ms=end_ms, text=text)


def _hotpot_exact_source_pin_authority():
    candidate_id = "auto_193450_1863_2056"
    return build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(
            ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ),
        expected_registry_sha256=(
            "sha256:"
            "be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757"
        ),
    )[candidate_id]


def test_runon_cue_straddling_target_triggers_refinement():
    # The real houqun failure: a 17s run-on cue welded the closure sentence to
    # the next topic, so the coarse grid could not place the cut.
    cues = [_cue(300_000, 319_000), _cue(319_000, 336_000)]
    assert needs_tail_refinement(cues, snapped_end=319_000, target_ms=324_700) is True


def test_clean_snap_near_target_needs_no_refinement():
    cues = [_cue(70_000, 79_900), _cue(80_200, 84_000)]
    assert needs_tail_refinement(cues, snapped_end=79_900, target_ms=80_000) is False


def test_structured_read_payoff_extends_semantic_tail_before_sentence_snap(tmp_path):
    cues = [
        _cue(0, 3_000, "开场。"),
        _cue(3_200, 9_000, "本来准备收尾。"),
        _cue(9_100, 12_000, "念完弹幕中的完整名字。"),
    ]
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 20_000}],
            "semantic_start_ms": 0,
            "semantic_end_ms": 9_000,
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 12_000,
            },
        },
        durations=[20_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=20_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=12_000,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert initial.target_rel == 12_000
    assert initial.snapped_end == 12_000
    assert initial.closure_cue.text == "念完弹幕中的完整名字。"


def test_human_reviewed_end_is_lower_bound_and_semantic_review_still_required(tmp_path):
    cues = [
        _cue(0, 3_000, "开场。"),
        _cue(3_200, 9_000, "故事在这里完整收束。"),
        _cue(9_100, 15_000, "紧接着开始下一条SC。"),
    ]
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 100_000, "end_ms": 120_000}],
            "semantic_start_ms": 100_000,
            "semantic_end_ms": 109_000,
            "given_end_ms": 109_000,
            "given_end_authority": "Ivan-reviewed source closure",
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 9_000,
            },
        },
        durations=[20_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=20_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert initial.target_rel == 9_000
    assert initial.snapped_end == 9_000
    assert initial.closure_cue.text == "故事在这里完整收束。"
    assert initial.manual_end_authority == "Ivan-reviewed source closure"


def test_boundary_semantic_context_exhaustion_emits_retry_scope(tmp_path):
    with pytest.raises(
        SystemExit,
        match=(
            "BOUNDARY_CONTEXT_EXHAUSTED:.*"
            "retry_scope=same_topic_continues"
        ),
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 90_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 9_000,
                "boundary_semantic_review": {
                    "status": "BLOCK",
                    "reason_codes": [
                        "SAME_TOPIC_FOLLOWUP",
                        "BOUNDARY_CONTEXT_EXHAUSTED",
                    ],
                    "needs_more_context": True,
                    "retry_scope": "same_topic_continues",
                    "max_forward_ms": 30_000,
                },
            },
            durations=[90_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=90_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[
                _cue(0, 9_000, "目标仍未回答完"),
                _cue(9_100, 35_000, "同一回答继续"),
            ],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_boundary_source_witness_reserve_emits_typed_retry_scope(tmp_path):
    with pytest.raises(
        SystemExit,
        match=(
            "BOUNDARY_CONTEXT_EXHAUSTED:.*"
            "retry_scope=source_witness_reserve"
        ),
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 141_820}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 109_820,
                "boundary_semantic_review": {
                    "status": "BLOCK",
                    "reason_codes": [
                        "BOUNDARY_SOURCE_WITNESS_RESERVE_INCOMPLETE"
                    ],
                    "needs_more_context": True,
                    "retry_scope": "source_witness_reserve",
                    "max_forward_ms": 30_000,
                },
            },
            durations=[141_820],
            padded=tmp_path / "unused.mp4",
            padded_dur=141_820,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[
                _cue(0, 109_820, "目标"),
                _cue(139_000, 141_410, "故事闭合"),
            ],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_boundary_semantic_recommendation_uses_retry_forward_cap(tmp_path):
    cues = [
        _cue(0, 9_000, "目标"),
        _cue(57_000, 58_300, "四十九秒后的完整闭环"),
    ]
    spec = {
        "pieces": [{"start_ms": 0, "end_ms": 90_000}],
        "semantic_start_ms": 0,
        "semantic_end_ms": 9_000,
        "boundary_semantic_review": {
            "status": "PASS",
            "recommended_end_ms": 58_300,
        },
    }
    with pytest.raises(
        SystemExit,
        match="BOUNDARY_SEMANTIC_RECOMMENDATION_INVALID",
    ):
        _select_initial_boundary(
            spec=spec,
            durations=[90_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=90_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=cues,
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
            boundary_repair_extend_cap_ms=30_000,
        )

    initial = _select_initial_boundary(
        spec=spec,
        durations=[90_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=90_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
        boundary_repair_extend_cap_ms=60_000,
    )

    assert initial.target_rel == 58_300
    assert initial.snapped_end == 58_300


def test_human_reviewed_end_preserves_later_structured_chat_payoff(tmp_path):
    cues = [
        _cue(0, 9_000, "语义候选先收束。"),
        _cue(9_100, 12_000, "人工下限到这里。"),
        _cue(12_100, 15_000, "弹幕原文的回收在这里完成。"),
    ]
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 100_000, "end_ms": 120_000}],
            "semantic_start_ms": 100_000,
            "semantic_end_ms": 109_000,
            "given_end_ms": 112_000,
            "given_end_authority": "Ivan-reviewed source closure",
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 9_000,
            },
        },
        durations=[20_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=20_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=15_000,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert initial.target_rel == 15_000
    assert initial.snapped_end == 15_000
    assert initial.closure_cue.text == "弹幕原文的回收在这里完成。"


def test_required_truth_owner_extends_boundary_and_cannot_become_not_required(
    tmp_path,
):
    cues = [
        _cue(0, 9_000, "原本准备收束。"),
        _cue(9_100, 14_000, "必需真值在更晚的位置。"),
        _cue(14_100, 18_000, "真值所属故事在这里完整闭环。"),
    ]
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 100_000, "end_ms": 120_000}],
            "semantic_start_ms": 100_000,
            "semantic_end_ms": 109_000,
            "given_end_ms": 109_000,
            "given_end_authority": "Ivan-reviewed lower bound",
            "required_boundary_owners": [
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "late-reviewed-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 12_000, "end_ms": 14_000}
                    ],
                }
            ],
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 18_000,
            },
        },
        durations=[20_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=20_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert initial.target_rel == 18_000
    assert initial.snapped_end == 18_000
    assert initial.required_boundary_owners[0]["owner_id"] == (
        "late-reviewed-truth"
    )


def test_required_owner_snap_never_selects_closer_sentence_before_truth(
    tmp_path,
):
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 20_000}],
            "semantic_start_ms": 0,
            "semantic_end_ms": 10_000,
            "required_boundary_owners": [
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "late-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 12_000, "end_ms": 14_000}
                    ],
                }
            ],
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 14_000,
            },
        },
        durations=[20_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=20_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=[
            _cue(0, 13_900, "更近但仍在真值结束之前。"),
            _cue(14_000, 15_000, "真值之后的完整句尾。"),
        ],
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )
    assert initial.snapped_end == 15_000


def test_required_owner_moves_lower_bound_without_moving_repair_cap(
    tmp_path,
):
    """A story truth may raise the closure floor while the original semantic
    target still owns the absolute repair cap."""

    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 250_000}],
            "semantic_start_ms": 0,
            "semantic_end_ms": 202_720,
            "required_boundary_owners": [
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "late-reviewed-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 229_000, "end_ms": 230_760}
                    ],
                }
            ],
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 202_720,
            },
        },
        durations=[250_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=250_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=[
            _cue(200_000, 230_700, "更近，但仍在必需真值结束之前。"),
            _cue(230_700, 231_000, "真值之后的合法完整收束。"),
            _cue(231_000, 233_000, "超过原始语义目标三十秒上限。"),
        ],
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
        boundary_repair_extend_cap_ms=30_000,
    )

    assert initial.repair_search_origin_ms == 202_720
    assert initial.target_rel == 230_760
    assert initial.repair_max_end_ms == 232_720
    assert initial.snapped_end == 231_000


def test_bound_manual_scope_resolves_generic_late_closure_without_cap_ratchet(
    tmp_path,
):
    scope = build_boundary_search_scope(
        semantic_target_ms=202_720,
        manual_lower_bound_ms=230_760,
        required_owner_end_ms=230_760,
        repair_cap_ms=60_000,
    )
    cues = [
        _cue(227_640, 230_760, "人工下界处的句子。"),
        _cue(261_850, 263_330, "同一故事终于完整收束。"),
        _cue(263_330, 268_480, "明确开始下一话题。"),
    ]
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 320_000}],
            "semantic_start_ms": 0,
            "semantic_end_ms": 202_720,
            "given_end_ms": 230_760,
            "given_end_authority": "Ivan-reviewed source closure",
            "required_boundary_owners": [
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "late-reviewed-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 229_000, "end_ms": 230_760}
                    ],
                }
            ],
            "boundary_search_scope": scope,
            "boundary_semantic_review": {
                "status": "PASS",
                "cue_grid_sha256": (
                    boundary_resolution.cue_grid_sha256(cues)
                ),
                "recommended_end_cue_index": 2,
                "recommended_end_ms": 263_330,
                "boundary_search_scope": scope,
            },
        },
        durations=[320_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=320_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
        boundary_repair_extend_cap_ms=60_000,
    )

    assert initial.repair_search_origin_ms == 230_760
    assert initial.repair_max_end_ms == 290_760
    assert initial.target_rel == 263_330
    assert initial.snapped_end == 263_330


def test_hotpot_exact_source_pin_emits_official_source_endpoint(tmp_path):
    candidate_id = "auto_193450_1863_2056"
    source_start_ms = 1_853_760
    official_source_end_ms = 2_056_480
    official_local_end_ms = official_source_end_ms - source_start_ms
    # Live V12 fresh ASR ends the semantic closure 400ms before official
    # BCUT cue 911, then starts a timing-drifted cue at that same instant.
    # The official source pin—not that derived ASR tail—owns the media cut.
    fresh_asr_closure_end_ms = official_local_end_ms - 400
    scope = build_boundary_search_scope(
        semantic_target_ms=official_local_end_ms,
        manual_lower_bound_ms=official_local_end_ms,
        repair_cap_ms=30_000,
        last_piece_start_ms=source_start_ms,
        boundary_end_mode="exact_source_pin",
    )
    cues = [
        _cue(0, 1_000, "开场完整。"),
        _cue(
            199_400,
            fresh_asr_closure_end_ms,
            "就是刚认识，暂时不太熟啊。",
        ),
        _cue(
            fresh_asr_closure_end_ms,
            official_local_end_ms + 1_400,
            "我行啊。",
        ),
        _cue(
            official_local_end_ms + 1_460,
            official_local_end_ms + 3_700,
            "谢谢恩恩的SC。",
        ),
    ]

    resolution = boundary_resolution.resolve_producer_boundary(
        spec={
            "candidate_id": candidate_id,
            "pieces": [
                {"start_ms": source_start_ms, "end_ms": 2_088_480}
            ],
            "semantic_start_ms": source_start_ms,
            "semantic_end_ms": official_source_end_ms,
            "given_end_ms": official_source_end_ms,
            "given_end_mode": "exact_source_pin",
            "given_end_authority": (
                "Pro source cue 911 exact endpoint"
            ),
            "recovery_publication_authority": (
                _hotpot_exact_source_pin_authority()
            ),
            "boundary_search_scope": scope,
            "boundary_semantic_review": {
                "status": "PASS",
                "cue_grid_sha256": (
                    boundary_resolution.cue_grid_sha256(cues)
                ),
                "recommended_end_cue_index": 2,
                "recommended_end_ms": fresh_asr_closure_end_ms,
                "boundary_search_scope": scope,
            },
        },
        durations=[234_720],
        padded=tmp_path / "unused.mp4",
        padded_dur=234_720,
        cid=candidate_id,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        spans=[
            SpeechSpan(0, 900),
            SpeechSpan(199_400, fresh_asr_closure_end_ms - 20),
            SpeechSpan(
                fresh_asr_closure_end_ms,
                official_local_end_ms + 1_380,
            ),
            SpeechSpan(
                official_local_end_ms + 1_460,
                official_local_end_ms + 3_680,
            ),
        ],
        boundary_repair_extend_cap_ms=30_000,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
        required_tail_end_ms=None,
    )

    assert resolution.audit["manual_end_mode"] == "exact_source_pin"
    assert (
        resolution.audit["snapped_sentence_end_ms"]
        == fresh_asr_closure_end_ms
    )
    assert resolution.final_end == official_local_end_ms
    assert source_start_ms + resolution.final_end == official_source_end_ms


def test_hotpot_exact_source_pin_rejects_later_otherwise_valid_cue(
    tmp_path,
):
    candidate_id = "auto_193450_1863_2056"
    source_start_ms = 1_853_760
    official_source_end_ms = 2_056_480
    official_local_end_ms = official_source_end_ms - source_start_ms
    later_local_end_ms = 2_084_520 - source_start_ms
    scope = build_boundary_search_scope(
        semantic_target_ms=official_local_end_ms,
        manual_lower_bound_ms=official_local_end_ms,
        repair_cap_ms=30_000,
        last_piece_start_ms=source_start_ms,
        boundary_end_mode="exact_source_pin",
    )
    cues = [
        _cue(
            199_600,
            official_local_end_ms,
            "就是刚认识，暂时不太熟啊。",
        ),
        _cue(
            227_640,
            later_local_end_ms,
            "邪恶守宫的下一条SC。",
        ),
    ]
    generic_scope = build_boundary_search_scope(
        semantic_target_ms=official_local_end_ms,
        manual_lower_bound_ms=official_local_end_ms,
        repair_cap_ms=30_000,
        last_piece_start_ms=source_start_ms,
    )
    assert (
        int(generic_scope["minimum_recommended_end_ms"])
        <= later_local_end_ms
        <= int(generic_scope["max_recommended_end_ms"])
    )
    assert later_local_end_ms > int(scope["max_recommended_end_ms"])

    with pytest.raises(
        SystemExit,
        match="BOUNDARY_SEMANTIC_RECOMMENDATION_INVALID",
    ):
        _select_initial_boundary(
            spec={
                "candidate_id": candidate_id,
                "pieces": [
                    {
                        "start_ms": source_start_ms,
                        "end_ms": 2_088_480,
                    }
                ],
                "semantic_start_ms": 1_863_450,
                "semantic_end_ms": official_source_end_ms,
                "given_end_ms": official_source_end_ms,
                "given_end_mode": "exact_source_pin",
                "given_end_authority": (
                    "Pro source cue 911 exact endpoint"
                ),
                "recovery_publication_authority": (
                    _hotpot_exact_source_pin_authority()
                ),
                "boundary_search_scope": scope,
                "boundary_semantic_review": {
                    "status": "PASS",
                    "cue_grid_sha256": (
                        boundary_resolution.cue_grid_sha256(cues)
                    ),
                    "recommended_end_cue_index": 2,
                    "recommended_end_ms": later_local_end_ms,
                    "boundary_search_scope": scope,
                },
            },
            durations=[234_720],
            padded=tmp_path / "unused.mp4",
            padded_dur=234_720,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=cues,
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
            boundary_repair_extend_cap_ms=30_000,
        )


def test_resolver_blocks_valid_but_different_search_scope(tmp_path):
    expected_scope = build_boundary_search_scope(
        semantic_target_ms=20_000,
        manual_lower_bound_ms=25_000,
        repair_cap_ms=60_000,
    )
    stale_scope = build_boundary_search_scope(
        semantic_target_ms=20_000,
        manual_lower_bound_ms=25_000,
        repair_cap_ms=30_000,
    )

    with pytest.raises(
        SystemExit,
        match="BOUNDARY_SEMANTIC_SEARCH_SCOPE_MISMATCH",
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 100_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 20_000,
                "given_end_ms": 25_000,
                "given_end_authority": "Ivan-reviewed source closure",
                "boundary_search_scope": stale_scope,
                "boundary_semantic_review": {
                    "status": "PASS",
                    "recommended_end_ms": 30_000,
                    "boundary_search_scope": stale_scope,
                },
            },
            durations=[100_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=100_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[_cue(25_000, 30_000, "完整收束。")],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
            boundary_repair_extend_cap_ms=int(
                expected_scope["repair_cap_ms"]
            ),
        )


def test_source_full_window_receipt_cannot_drop_bound_scope(tmp_path):
    with pytest.raises(
        SystemExit,
        match="BOUNDARY_SEMANTIC_SEARCH_SCOPE_INVALID",
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 100_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 20_000,
                "boundary_semantic_review": {
                    "schema_version": (
                        "talk-boundary-semantic-review.v1"
                    ),
                    "review_scope": "source_full_window",
                    "status": "PASS",
                    "recommended_end_ms": 20_000,
                },
            },
            durations=[100_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=100_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[_cue(0, 20_000, "完整收束。")],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
            boundary_repair_extend_cap_ms=30_000,
        )


def test_required_owner_fails_when_only_closure_is_beyond_original_cap(
    tmp_path,
):
    with pytest.raises(
        SystemExit,
        match="BOUNDARY_REQUIRED_OWNER_EXCLUDED.*\\[230760,232720\\]",
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 250_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 202_720,
                "required_boundary_owners": [
                    {
                        "owner_kind": "source_subtitle_truth",
                        "owner_id": "late-reviewed-truth",
                        "required": True,
                        "local_windows": [
                            {"start_ms": 229_000, "end_ms": 230_760}
                        ],
                    }
                ],
                "boundary_semantic_review": {
                    "status": "PASS",
                    "recommended_end_ms": 202_720,
                },
            },
            durations=[250_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=250_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[
                _cue(200_000, 230_700, "真值结束之前的句尾。"),
                _cue(230_700, 233_000, "唯一完整收束已经超过上限。"),
            ],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
            boundary_repair_extend_cap_ms=30_000,
        )


def test_required_owner_before_semantic_start_expands_clip_start(tmp_path):
    initial = _select_initial_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 30_000}],
            "semantic_start_ms": 10_000,
            "semantic_end_ms": 20_000,
            "required_boundary_owners": [
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "opening-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 2_000, "end_ms": 4_000}
                    ],
                }
            ],
            "boundary_semantic_review": {
                "status": "PASS",
                "recommended_end_ms": 20_000,
            },
        },
        durations=[30_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=30_000,
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=[
            _cue(10_000, 20_000, "语义主体完整收束。"),
        ],
        required_tail_end_ms=None,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert initial.final_start == 2_000
    assert initial.required_owner_start_ms == 2_000


def test_required_owner_repair_passes_original_origin_to_clean_closure_search(
    tmp_path,
    monkeypatch,
):
    calls = []

    def _no_clean_closure(*_args, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        boundary_resolution,
        "next_clean_closure",
        _no_clean_closure,
    )
    monkeypatch.setattr(
        boundary_resolution,
        "sanitize_cue_timing",
        lambda *_args, **_kwargs: ([_Txt("初次句尾。")], {}),
    )
    monkeypatch.setattr(
        boundary_resolution,
        "boundary_red_flags",
        lambda **_kwargs: ["closure_not_final_subtitle"],
    )
    monkeypatch.setattr(
        boundary_resolution,
        "tail_requires_forward_extension",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(
        SystemExit,
        match="BOUNDARY_REQUIRED_OWNER_EXCLUDED.*\\[230760,232720\\]",
    ):
        boundary_resolution._repair_boundary(
            cid="generic-late-owner",
            out_root=tmp_path,
            padded_dur=250_000,
            spans=[],
            cues=[
                _cue(200_000, 231_000, "初次句尾。"),
                _cue(231_000, 233_000, "上限以后的收束。"),
            ],
            target_start_rel=0,
            snapped_start=0,
            final_start=0,
            target_rel=230_760,
            closure_selection_lower_bound_ms=230_760,
            repair_search_origin_ms=202_720,
            repair_max_end_ms=232_720,
            snapped=231_000,
            closure_cue=_cue(200_000, 231_000, "初次句尾。"),
            refinement_used=False,
            manual_end_authority=None,
            manual_end_mode="semantic_lower_bound",
            semantic_review={
                "status": "PASS",
                "recommended_end_ms": 202_720,
            },
            required_boundary_owners=[
                {
                    "owner_kind": "source_subtitle_truth",
                    "owner_id": "late-reviewed-truth",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 229_000, "end_ms": 230_760}
                    ],
                }
            ],
            required_owner_start_ms=229_000,
            required_owner_end_ms=230_760,
            boundary_repair_extend_cap_ms=30_000,
        )

    assert calls == [
        {
            "after_ms": 231_000,
            "padded_dur_ms": 250_000,
            "cap_ms": 30_000,
            "search_origin_ms": 202_720,
        }
    ]
    audit = json.loads(
        (tmp_path / "generic-late-owner.boundary_audit.json").read_text()
    )
    assert audit["boundary_repair_search_origin_ms"] == 202_720
    assert audit["boundary_repair_max_end_ms"] == 232_720
    assert audit["required_boundary_owner_verification"]["status"] == "FAIL"


def test_reviewed_closure_tail_can_cover_exact_delivery_interval(tmp_path):
    """A reviewed cue end plus the normal 400ms tail may satisfy a media
    interval owner; the owner must not force selection of the next sentence."""

    cues = [
        _cue(0, 102_610, "其实是最包容异性恋的直播间"),
        _cue(103_120, 105_280, "总之先送妹妹礼物吧"),
    ]
    resolution = boundary_resolution.resolve_producer_boundary(
        spec={
            "pieces": [{"start_ms": 0, "end_ms": 120_000}],
            "semantic_start_ms": 0,
            "semantic_end_ms": 102_510,
            "given_end_ms": 102_510,
            "given_end_authority": "Ivan-reviewed source closure",
            "required_boundary_owners": [
                {
                    "owner_kind": "reviewed_redelivery_baseline",
                    "owner_id": "exact-reviewed-interval",
                    "required": True,
                    "local_windows": [
                        {"start_ms": 0, "end_ms": 103_010}
                    ],
                }
            ],
            "boundary_semantic_review": {
                "status": "PASS",
                "request_sha256": "sha256:" + "a" * 64,
                "cue_grid_sha256": (
                    boundary_resolution.cue_grid_sha256(cues)
                ),
                "recommended_end_cue_index": 1,
                "recommended_end_ms": 102_610,
            },
        },
        durations=[120_000],
        padded=tmp_path / "unused.mp4",
        padded_dur=120_000,
        cid="tail-covered-owner",
        out_root=tmp_path,
        transcriber=lambda *_args: "",
        cues=cues,
        spans=[SpeechSpan(0, 102_580)],
        boundary_repair_extend_cap_ms=30_000,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=lambda **_kwargs: [],
            run_command=lambda *_args, **_kwargs: None,
        ),
    )

    assert resolution.final_end == 103_010
    assert resolution.audit["snapped_sentence_end_ms"] == 102_610
    assert (
        resolution.audit["boundary_selection_lower_bound_ms"] == 102_610
    )
    assert resolution.audit["delivery_coverage_lower_bound_ms"] == 103_010
    assert resolution.audit["tail_pad_coverage_bridge"]["status"] == "USED"
    assert (
        resolution.audit["required_boundary_owner_verification"]["status"]
        == "PASS"
    )
    assert (
        resolution.audit["boundary_semantic_review"][
            "final_endpoint_binding"
        ]["status"]
        == "PASS"
    )


def test_tail_bridge_still_blocks_when_next_cue_clamps_before_owner(tmp_path):
    """Tail coverage is not a waiver: an intervening next cue that clamps the
    actual media end before the owner remains a hard failure."""

    with pytest.raises(
        SystemExit,
        match="BOUNDARY_REQUIRED_OWNER_EXCLUDED",
    ):
        clamp_cues = [
            _cue(0, 102_610, "包袱闭环"),
            _cue(102_800, 105_000, "下一话题"),
        ]
        boundary_resolution.resolve_producer_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 120_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 102_510,
                "given_end_ms": 102_510,
                "given_end_authority": "Ivan-reviewed source closure",
                "required_boundary_owners": [
                    {
                        "owner_kind": "reviewed_redelivery_baseline",
                        "owner_id": "exact-reviewed-interval",
                        "required": True,
                        "local_windows": [
                            {"start_ms": 0, "end_ms": 103_010}
                        ],
                    }
                ],
                "boundary_semantic_review": {
                    "status": "PASS",
                    "request_sha256": "sha256:" + "b" * 64,
                    "cue_grid_sha256": (
                        boundary_resolution.cue_grid_sha256(clamp_cues)
                    ),
                    "recommended_end_cue_index": 1,
                    "recommended_end_ms": 102_610,
                },
            },
            durations=[120_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=120_000,
            cid="clamped-tail-owner",
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=clamp_cues,
            spans=[
                SpeechSpan(0, 102_580),
                SpeechSpan(102_850, 104_900),
            ],
            boundary_repair_extend_cap_ms=30_000,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_manual_end_cannot_replace_semantic_review(tmp_path):
    with pytest.raises(SystemExit, match="BOUNDARY_SEMANTIC_REVIEW_REQUIRED"):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 100_000, "end_ms": 120_000}],
                "semantic_start_ms": 100_000,
                "semantic_end_ms": 109_000,
                "given_end_ms": 109_000,
                "given_end_authority": "generic prose is not a second witness",
            },
            durations=[20_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=20_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[_cue(0, 9_000, "完整收束。")],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_manual_end_cannot_truncate_semantic_target_with_authority(tmp_path):
    with pytest.raises(
        SystemExit,
        match="MANUAL_END_CANNOT_TRUNCATE_SEMANTIC_TARGET",
    ):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 20_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 15_000,
                "given_end_ms": 9_000,
                "given_end_authority": "reviewed but invalid lower bound",
                "boundary_semantic_review": {
                    "status": "PASS",
                    "recommended_end_ms": 15_000,
                },
            },
            durations=[20_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=20_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[_cue(0, 15_000, "完整收束。")],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_manual_end_without_authority_fails_closed(tmp_path):
    with pytest.raises(SystemExit, match="MANUAL_END_AUTHORITY_INVALID"):
        _select_initial_boundary(
            spec={
                "pieces": [{"start_ms": 0, "end_ms": 20_000}],
                "semantic_start_ms": 0,
                "semantic_end_ms": 15_000,
                "given_end_ms": 9_000,
            },
            durations=[20_000],
            padded=tmp_path / "unused.mp4",
            padded_dur=20_000,
            out_root=tmp_path,
            transcriber=lambda *_args: "",
            cues=[_cue(0, 9_000, "完整收束。")],
            required_tail_end_ms=None,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=lambda **_kwargs: [],
                run_command=lambda *_args, **_kwargs: None,
            ),
        )


def test_boundary_audit_requires_both_snapped_boundaries():
    spans = [SpeechSpan(85_000, 95_000)]
    ok = boundary_audit(spans, start_ms=250, cut_ms=90_000, start_snapped=True, end_snapped=True)
    assert ok["verdict"] == "ok_sentence_boundary_cut"
    assert ok["end_cut_inside_speech_island"] is True
    assert ok["end_island_continues_ms"] == 5_000

    bad_start = boundary_audit(spans, start_ms=0, cut_ms=90_000, start_snapped=False, end_snapped=True)
    assert bad_start["verdict"] == "start_not_on_sentence_boundary"


def test_syntactic_tail_guard_blocks_only_obvious_half_sentences():
    assert syntactic_tail_audit("跟他们说要跟第")["status"] == "INCOMPLETE"
    assert syntactic_tail_audit("因为")["status"] == "INCOMPLETE"
    assert syntactic_tail_audit("就是刚认识暂时不太熟啊")["status"] == "UNKNOWN"
    assert syntactic_tail_audit("故事到这里结束。")["status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# Deterministic red flags (2026-07-09 external audit): a snapped verdict alone
# let 6/10 real deliveries cut inside a still-running speech island and 4/10
# end on a different sentence than the claimed closure — all reported green.
# Policy since 2026-07-10 (Ivan): flags drive the SELF-REPAIR loop; a clip is
# delivered clean or fails closed — never delivered-with-flags (quarantine).
# ---------------------------------------------------------------------------


class _Txt:
    def __init__(self, text):
        self.text = text


def _flags(**overrides):
    base = dict(
        audit={"end_island_continues_ms": 0},
        cues=[],
        sanitized=[_Txt("收束句。")],
        final_start_ms=1_000,
        final_end_ms=90_400,
        snapped_end_ms=90_000,
        closure_text="收束句。",
    )
    base.update(overrides)
    return boundary_red_flags(**base)


def test_no_flags_on_a_clean_cut():
    assert _flags() == []


def test_flag_when_speech_continues_after_cut():
    """The old test explicitly ALLOWED a 5s continuing island — that green is
    exactly what the audit demolished.  It still cuts, but flags quarantine."""
    flags = _flags(audit={"end_island_continues_ms": 5_000})
    assert "speech_continues_5000ms_after_cut" in flags
    assert _flags(audit={"end_island_continues_ms": ISLAND_CONTINUES_FLAG_MS - 1}) == []


def test_flag_when_clip_opens_mid_sentence():
    # a cue that began 2s before the clip and is still running at clip start
    flags = _flags(cues=[_cue(-1_000, 2_000)], final_start_ms=1_000)
    assert "opens_mid_sentence" in flags
    # the previous sentence merely ENDING inside the 250ms lead air is fine
    assert _flags(cues=[_cue(-1_000, 1_100)], final_start_ms=1_000) == []


def test_flag_when_next_sentence_enters_tail_pad():
    # 收束句 snap 在 90s，pad 到 90.4s；下一句 90.2s 开始 → 全文闪现在片尾
    flags = _flags(cues=[_cue(90_200, 93_000)])
    assert "next_sentence_enters_tail_pad" in flags
    assert _flags(cues=[_cue(90_400, 93_000)]) == []  # 恰在 final_end 之后 → 无害


def test_flag_when_closure_is_not_the_final_subtitle():
    """4/10 real deliveries: the report's closure sentence was not the burned
    SRT's last line.  Deterministic text equality, no LLM."""
    flags = _flags(sanitized=[_Txt("收束句。"), _Txt("其实还有下一句")])
    assert "closure_not_final_subtitle" in flags
    assert _flags(sanitized=[_Txt(" 收束句。 ")]) == []  # whitespace-insensitive


# ---------------------------------------------------------------------------
# Boundary self-repair primitives (Ivan 2026-07-10: an unattended pipeline
# fixes what its auditors detect).  The end repair extends FORWARD to the next
# sentence end whose tail pad is verifiably quiet; the start repair opens on
# the straddled sentence's own start.  No candidate → the produce fails closed
# (BOUNDARY_UNREPAIRABLE), never a delivered-with-flags state.
# ---------------------------------------------------------------------------


def test_repair_extends_past_continuing_speech_to_clean_pause():
    # Closure snapped at 90s but the talk runs on (90.2s→93s enters the tail
    # pad, the island continues).  The next verifiably clean sentence end is
    # 93s: nothing starts in its pad and the island stops by 93.1s.
    cues = [_cue(80_000, 90_000), _cue(90_200, 93_000), _cue(96_000, 99_000)]
    spans = [SpeechSpan(80_000, 93_100)]
    assert next_clean_closure(cues, spans, after_ms=90_000, padded_dur_ms=120_000) == 93_000


def test_tail_pad_clamps_before_distinct_next_speech_island():
    # 7/11 auto_170019_305_355: closure 60.270s; the fixed 400ms tail entered
    # a new 60.520-80.432s VAD island and falsely reported ~19.7s continuing
    # speech.  Keep 150ms of closure air and stop 100ms before the next island.
    decision = adaptive_tail_cut(
        [SpeechSpan(58_000, 60_270), SpeechSpan(60_520, 80_432)],
        snapped_end_ms=60_270,
        padded_dur_ms=100_000,
    )
    assert decision["nominal_end_ms"] == 60_670
    assert decision["final_end_ms"] == 60_420
    assert decision["reason"] == "tail_clamped_before_next_speech_island"
    audit = boundary_audit(
        [SpeechSpan(58_000, 60_270), SpeechSpan(60_520, 80_432)],
        start_ms=0,
        cut_ms=decision["final_end_ms"],
        start_snapped=True,
        end_snapped=True,
    )
    assert audit["end_island_continues_ms"] == 0


def test_tail_pad_clamps_before_next_subtitle_even_when_vad_starts_later():
    """7/12 auto_154845_1140_1254: the next cue began 22ms before the
    VAD-derived cut.  The cue timeline is the stronger subtitle-flash
    authority, so keep the closure and trim only its disposable tail air."""
    cues = [_cue(120_630, 123_310, "收束句"), _cue(123_470, 124_750, "我要找一下")]
    decision = adaptive_tail_cut(
        [SpeechSpan(123_592, 124_800)],
        cues=cues,
        snapped_end_ms=123_310,
        padded_dur_ms=160_000,
    )

    assert decision["nominal_end_ms"] == 123_710
    assert decision["final_end_ms"] == 123_370
    assert decision["next_subtitle_start_ms"] == 123_470
    assert decision["reason"] == "tail_clamped_before_next_subtitle"
    assert boundary_red_flags(
        audit={"end_island_continues_ms": 0},
        cues=cues,
        sanitized=[_Txt("收束句")],
        final_start_ms=100_000,
        final_end_ms=decision["final_end_ms"],
        snapped_end_ms=123_310,
        closure_text="收束句",
    ) == []


def test_tail_pad_does_not_clamp_ambiguous_narrow_gap_or_distant_island():
    narrow = adaptive_tail_cut(
        [SpeechSpan(60_420, 80_000)],
        snapped_end_ms=60_270,
        padded_dur_ms=100_000,
    )
    assert narrow["final_end_ms"] == 60_670
    assert narrow["reason"] is None

    distant = adaptive_tail_cut(
        [SpeechSpan(60_800, 80_000)],
        snapped_end_ms=60_270,
        padded_dur_ms=100_000,
    )
    assert distant["final_end_ms"] == 60_670
    assert distant["reason"] is None


def test_only_existing_continuation_or_crossing_cue_can_extend_forward():
    assert tail_requires_forward_extension(
        [], [SpeechSpan(59_000, 80_000)], snapped_end_ms=60_270, cut_ms=60_670
    )
    assert tail_requires_forward_extension(
        [_cue(59_500, 80_000)], [], snapped_end_ms=60_270, cut_ms=60_670
    )
    # A cue/island that starts only AFTER the snapped closure is a new turn or
    # topic signal, even if the fixed tail cut would enter it.  It must not
    # grant forward-repair or source-context retry authority.
    assert not tail_requires_forward_extension(
        [_cue(60_300, 80_000)], [], snapped_end_ms=60_270, cut_ms=60_670
    )
    assert not tail_requires_forward_extension(
        [], [SpeechSpan(60_520, 80_000)], snapped_end_ms=60_270, cut_ms=60_670
    )


def test_repair_skips_candidates_whose_island_keeps_running():
    # 93s ends a cue but the VAD island runs to 96.2s (≥1.5s past its pad) →
    # skip to 96s, where the island has genuinely stopped.
    cues = [_cue(80_000, 90_000), _cue(90_200, 93_000), _cue(93_400, 96_000)]
    spans = [SpeechSpan(80_000, 96_200)]
    assert next_clean_closure(cues, spans, after_ms=90_000, padded_dur_ms=120_000) == 96_000


def test_repair_fails_closed_beyond_extend_cap():
    # Continuous back-to-back speech (every pause < tail pad) past the 25s cap:
    # no clean closure exists → None → produce exits BOUNDARY_UNREPAIRABLE.
    cues = [_cue(80_000, 90_000)] + [
        _cue(90_000 + i * 1_000, 90_900 + i * 1_000) for i in range(40)
    ]
    spans = [SpeechSpan(80_000, 140_000)]
    assert next_clean_closure(cues, spans, after_ms=90_000, padded_dur_ms=200_000) is None


def test_retry_cap_expands_search_beyond_30s_but_stays_absolute_from_target():
    cues = [_cue(80_000, 90_000), _cue(90_200, 130_000), _cue(130_200, 151_000)]
    spans = [SpeechSpan(80_000, 130_100), SpeechSpan(130_200, 151_100)]
    assert next_clean_closure(
        cues,
        spans,
        after_ms=90_000,
        padded_dur_ms=180_000,
        cap_ms=30_000,
        search_origin_ms=90_000,
    ) is None
    assert next_clean_closure(
        cues,
        spans,
        after_ms=90_000,
        padded_dur_ms=180_000,
        cap_ms=60_000,
        search_origin_ms=90_000,
    ) == 130_000
    # A later repair cannot ratchet another +60s from the previous closure:
    # 151s is outside the absolute 90s+60s ceiling.
    assert next_clean_closure(
        cues,
        spans,
        after_ms=130_000,
        padded_dur_ms=180_000,
        cap_ms=60_000,
        search_origin_ms=90_000,
    ) is None


def test_repair_start_opens_on_straddled_sentence_start():
    # A sentence running through the opening → open on ITS start instead.
    assert repair_start_for_straddler([_cue(400, 2_000)], final_start_ms=1_000) == 400
    # No straddler (previous sentence ends inside the lead air) → nothing to fix.
    assert repair_start_for_straddler([_cue(980, 2_000)], final_start_ms=1_000) is None


def test_final_semantic_endpoint_binding_matches_exact_closure():
    cues = [
        _cue(0, 5_000, "故事铺垫。"),
        _cue(5_100, 9_000, "包袱落地。"),
        _cue(9_100, 11_000, "下一话题。"),
    ]

    review, reasons = boundary_resolution._bind_final_semantic_endpoint(
        semantic_review={
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "request_sha256": "sha256:" + "a" * 64,
            "cue_grid_sha256": boundary_resolution.cue_grid_sha256(cues),
            "recommended_end_cue_index": 2,
            "recommended_end_ms": 9_000,
        },
        cues=cues,
        closure_cue=cues[1],
        snapped_end_ms=9_000,
        final_start_ms=0,
        final_end_ms=9_400,
    )

    assert reasons == []
    binding = review["final_endpoint_binding"]
    assert binding["status"] == "PASS"
    assert binding["final_closure_cue_index"] == 2
    assert binding["final_snapped_end_ms"] == 9_000
    assert binding["final_end_ms"] == 9_400
    assert binding["semantic_cue_grid_sha256"] == (
        binding["final_cue_grid_sha256"]
    )


def test_final_semantic_endpoint_binding_blocks_later_repair_endpoint():
    cues = [
        _cue(0, 5_000, "原推荐终点。"),
        _cue(5_100, 9_000, "修复后才切到这里。"),
    ]

    review, reasons = boundary_resolution._bind_final_semantic_endpoint(
        semantic_review={
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "recommended_end_cue_index": 1,
            "recommended_end_ms": 5_000,
        },
        cues=cues,
        closure_cue=cues[1],
        snapped_end_ms=9_000,
        final_start_ms=0,
        final_end_ms=9_400,
    )

    assert review["final_endpoint_binding"]["status"] == "BLOCK"
    assert "BOUNDARY_SEMANTIC_ENDPOINT_MS_MISMATCH" in reasons
    assert "BOUNDARY_SEMANTIC_ENDPOINT_CUE_MISMATCH" in reasons


def test_final_semantic_endpoint_binding_blocks_stale_cue_grid():
    reviewed_cues = [
        _cue(0, 5_000, "保留句。"),
        _cue(5_100, 7_000, "后来删除的幻听。"),
        _cue(7_100, 9_000, "包袱落地。"),
    ]
    final_cues = [reviewed_cues[0], reviewed_cues[2]]

    review, reasons = boundary_resolution._bind_final_semantic_endpoint(
        semantic_review={
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "request_sha256": "sha256:" + "c" * 64,
            "cue_grid_sha256": (
                boundary_resolution.cue_grid_sha256(reviewed_cues)
            ),
            "recommended_end_cue_index": 2,
            "recommended_end_ms": 9_000,
        },
        cues=final_cues,
        closure_cue=final_cues[1],
        snapped_end_ms=9_000,
        final_start_ms=0,
        final_end_ms=9_400,
    )

    assert review["final_endpoint_binding"]["status"] == "BLOCK"
    assert "BOUNDARY_SEMANTIC_CUE_GRID_MISMATCH" in reasons
