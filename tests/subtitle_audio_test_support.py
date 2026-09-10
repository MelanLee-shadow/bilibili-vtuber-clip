"""Synthetic timing evidence for uploader integration tests, never real media proof.

Only extraction and the BCUT response are substituted. The production capture,
SRT renderer, correspondence checker and uploader validation all run unchanged.
All inputs and outputs belong to a pytest temporary package.
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.free_asr_client import to_srt
from src.autoslice.final_subtitle_audio_gate import (
    FinalSubtitleAudioGateAdapters,
    capture_final_subtitle_audio_check,
    subtitle_audio_artifact_paths,
    validate_final_subtitle_audio_check,
)

SYNTHETIC_ROWS = (
    (1_000, 5_000, "开场这句内容很特别"),
    (23_000, 27_000, "中段这里继续说明问题"),
    (42_000, 46_000, "最后我们明确完成收束"),
)


def _raw_result(offset_ms: int = 0) -> dict:
    return {
        "fixture_scope": "SYNTHETIC_TIMING_TEST_NOT_REAL_ASR",
        "utterances": [
            {"start_time": start + offset_ms, "end_time": end + offset_ms,
             "transcript": text, "words": []}
            for start, end, text in SYNTHETIC_ROWS
        ],
    }


SYNTHETIC_SRT = to_srt(_raw_result())
SYNTHETIC_TRANSCRIPT = "\n".join(text for _, _, text in SYNTHETIC_ROWS)


def bind_synthetic_audio_evidence(
    record: dict, *, video: Path, subtitle: Path, intro_offset_ms: int = 6_200,
) -> dict:
    """Complete an existing synthetic record through the real timing gate.

    Evidence is first captured under its candidate ID, then copied to the
    delivered subtitle stem using the same relocatable-path contract as a real
    package. Nothing here bypasses the production validator or contacts BCUT.
    """
    assert video.parent == subtitle.parent
    assert subtitle.read_text(encoding="utf-8") == SYNTHETIC_SRT
    candidate_id = str(record["story_contract"]["candidate_id"])
    media_sha = "sha256:" + hashlib.sha256(video.read_bytes()).hexdigest()
    record["burned_preview"] = {
        "status": "BURNED", "path": str(video), "burned_sha256": media_sha,
        "branding_intro": {"intro_offset_ms": intro_offset_ms},
    }
    record["story_contract"]["transcript_sha256"] = (
        "sha256:" + hashlib.sha256(SYNTHETIC_TRANSCRIPT.encode()).hexdigest()
    )
    calls: list[str] = []
    synthetic_audio = b"SYNTHETIC_AUDIO_TEST_ADAPTER_NOT_A_RECORDING"

    def extract(media: Path, target: Path) -> None:
        assert media.resolve() == video.resolve()
        calls.append("extract")
        target.write_bytes(synthetic_audio)

    def transcribe(payload: bytes) -> dict:
        assert payload == synthetic_audio
        calls.append("transcribe")
        return _raw_result(intro_offset_ms)

    adapters = FinalSubtitleAudioGateAdapters(extract, transcribe, to_srt)
    with TemporaryDirectory(prefix="synthetic-audio-", dir=video.parent) as folder:
        record = capture_final_subtitle_audio_check(
            record, subtitle, Path(folder), candidate_id, adapters=adapters,
        )
        assert calls == ["extract", "transcribe"]
        evidence = record["subtitle_audio_correspondence"]
        assert evidence["status"] == "PASS"
        for source in subtitle_audio_artifact_paths(record):
            suffix = source.name.removeprefix(candidate_id)
            target = video.parent / (subtitle.stem + suffix)
            shutil.copyfile(source, target)
            for key in ("witness_srt_path", "provenance_path", "correspondence_path", "raw_result_path"):
                if evidence.get(key) == str(source):
                    evidence[key] = str(target)
        evidence["output_dir"] = str(video.parent)
    # Validate after the capture directory is gone: only delivered artifacts count.
    validate_final_subtitle_audio_check(
        record, final_srt=subtitle, actual_media=video, package_root=video.parent,
    )
    return record
