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
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
)
from src.autoslice.jingting_chunker import SrtCue
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


def _cue(start_ms, end_ms):
    return SrtCue(index="1", start_ms=start_ms, end_ms=end_ms, text="x")


def test_runon_cue_straddling_target_triggers_refinement():
    # The real houqun failure: a 17s run-on cue welded the closure sentence to
    # the next topic, so the coarse grid could not place the cut.
    cues = [_cue(300_000, 319_000), _cue(319_000, 336_000)]
    assert needs_tail_refinement(cues, snapped_end=319_000, target_ms=324_700) is True


def test_clean_snap_near_target_needs_no_refinement():
    cues = [_cue(70_000, 79_900), _cue(80_200, 84_000)]
    assert needs_tail_refinement(cues, snapped_end=79_900, target_ms=80_000) is False


def test_boundary_audit_requires_both_snapped_boundaries():
    spans = [SpeechSpan(85_000, 95_000)]
    ok = boundary_audit(spans, start_ms=250, cut_ms=90_000, start_snapped=True, end_snapped=True)
    assert ok["verdict"] == "ok_sentence_boundary_cut"
    assert ok["end_cut_inside_speech_island"] is True
    assert ok["end_island_continues_ms"] == 5_000

    bad_start = boundary_audit(spans, start_ms=0, cut_ms=90_000, start_snapped=False, end_snapped=True)
    assert bad_start["verdict"] == "start_not_on_sentence_boundary"


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


def test_repair_start_opens_on_straddled_sentence_start():
    # A sentence running through the opening → open on ITS start instead.
    assert repair_start_for_straddler([_cue(400, 2_000)], final_start_ms=1_000) == 400
    # No straddler (previous sentence ends inside the lead air) → nothing to fix.
    assert repair_start_for_straddler([_cue(980, 2_000)], final_start_ms=1_000) is None
