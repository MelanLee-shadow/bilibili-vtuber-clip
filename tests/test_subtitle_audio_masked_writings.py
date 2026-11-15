"""A Latin-letter name in the final subtitles is a timing anchor though the witness ASR writes its sound (
semantictalk_1084420: the first third was all kmx, written QQ熊/GIO胸 by BCUT, so the clip stopped at step 8)."""
from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.free_asr_client import to_srt
from src.autoslice import subtitle_audio_correspondence as c, subtitle_audio_masked
from src.autoslice.final_subtitle_audio_gate import (
    FinalSubtitleAudioGateAdapters,
    capture_final_subtitle_audio_check,
    validate_final_subtitle_audio_check,
)

INTRO = 6_200
NAMED = "假如kmx变成薯条你也会喜欢kmx吗"
FINAL = ((0, 4_000, NAMED), (20_000, 24_000, "中段这里继续说明问题"), (40_000, 44_000, "最后我们明确完成收束"))
# BCUT splits the sentence in two and hears the name two ways (as it did on semantictalk_1084420)
HEARD = ((0, 1_900, "假如给我胸变成薯条"), (2_000, 4_000, "你也会喜欢GIO胸吗"), *FINAL[1:])


def _raw(rows, offset: int) -> dict:
    return {"utterances": [{"start_time": s + offset, "end_time": e + offset, "transcript": t, "words": []}
                           for s, e, t in rows]}


def _cues(rows, offset: int = 0):
    return c.parse_timed_srt(to_srt(_raw(rows, offset)))


def _anchors(final, witness, masked):
    return c._find_anchors(final, witness, intro_offset_ms=INTRO, masked_writings=subtitle_audio_masked.normalized(masked))


def test_a_latin_name_spoken_as_other_characters_anchors_the_first_third_only_with_the_masked_names():
    final, witness = _cues(FINAL), _cues(HEARD, INTRO)
    end = max(cue.end_ms for cue in final)
    assert c._coverage(_anchors(final, witness, ()), end)["status"] == "INSUFFICIENT"
    found = _anchors(final, witness, ["kmx"])
    assert c._coverage(found, end)["status"] == "PASS"
    assert [(a.final_index, a.witness_index, a.start_delta_ms) for a in found][0] == ("1", "1+2", 0)


def test_a_masked_sentence_heard_twice_in_the_witness_is_not_an_anchor():
    witness = _cues(HEARD + ((50_000, 54_000, "假如提莫熊变成薯条你也会喜欢请问熊吗"),), INTRO)
    found = _anchors(_cues(FINAL), witness, ["kmx"])
    assert "1" not in {a.final_index for a in found}


def test_the_other_characters_must_match_exactly():
    witness = _cues((HEARD[0], (2_000, 4_000, "你还会喜欢GIO胸吗"), *HEARD[2:]), INTRO)
    assert "1" not in {a.final_index for a in _anchors(_cues(FINAL), witness, ["kmx"])}


def test_a_capture_made_without_the_names_is_recomputed_from_the_same_witness_and_replays(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"SYNTHETIC_BURNED_VIDEO")
    subtitle = tmp_path / "clip.srt"
    subtitle.write_text(to_srt(_raw(FINAL, 0)), encoding="utf-8")
    sha = "sha256:" + hashlib.sha256(video.read_bytes()).hexdigest()
    record = {"burned_preview": {"status": "BURNED", "path": str(video), "burned_sha256": sha,
                                 "branding_intro": {"intro_offset_ms": INTRO}},
              "artifact_hashes": {"burned_video_sha256": sha}}
    calls: list[str] = []

    def extract(_media: Path, target: Path) -> None:
        calls.append("extract")
        target.write_bytes(b"SYNTHETIC_AUDIO")

    def transcribe(_audio: bytes) -> dict:
        calls.append("transcribe")
        return _raw(HEARD, INTRO)

    adapters = FinalSubtitleAudioGateAdapters(extract, transcribe, to_srt)
    out = tmp_path / "out"
    capture_final_subtitle_audio_check(record, subtitle, out, "clip", adapters=adapters)
    before = record["subtitle_audio_correspondence"]["receipt"]
    assert before["status"] == "BLOCK" and "masked_writings" not in before["anchor_policy"]
    capture_final_subtitle_audio_check(record, subtitle, out, "clip", adapters=adapters, masked_writings=["kmx"])
    after = record["subtitle_audio_correspondence"]["receipt"]
    assert calls == ["extract", "transcribe"]                      # the witness was not asked for again
    assert after["status"] == "PASS" and after["anchor_policy"]["masked_writings"] == ["kmx"]
    validate_final_subtitle_audio_check(record, subtitle, video, out, candidate_id="clip")
