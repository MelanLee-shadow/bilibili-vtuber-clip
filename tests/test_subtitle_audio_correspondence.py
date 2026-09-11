from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.subtitle_audio_correspondence import (
    CorrespondenceInputError,
    check_subtitle_audio_correspondence,
)


def _time(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _srt(rows: list[tuple[int, int, str]]) -> str:
    return "\n\n".join(
        f"{index}\n{_time(start)} --> {_time(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(
    tmp_path: Path,
    *,
    final_rows: list[tuple[int, int, str]],
    witness_rows: list[tuple[int, int, str]],
    intro_offset_ms: int = 0,
    media_bytes: bytes = b"actual media bytes",
    declared_intro_offset_ms: int | None = None,
) -> tuple[Path, Path, Path, Path, int]:
    final_path = tmp_path / "final.srt"
    witness_path = tmp_path / "witness.srt"
    media_path = tmp_path / "actual.mp4"
    provenance_path = tmp_path / "provenance.json"
    final_path.write_text(_srt(final_rows), encoding="utf-8")
    witness_path.write_text(_srt(witness_rows), encoding="utf-8")
    media_path.write_bytes(media_bytes)
    timebase = {
        "unit": "ms",
        "final_srt": "delivery_local_ms",
        "witness_srt": "actual_media_local_ms",
        "intro_offset_application": "add_once_to_final_srt",
    }
    if declared_intro_offset_ms is not None:
        timebase["declared_intro_offset_ms"] = declared_intro_offset_ms
    provenance_path.write_text(
        json.dumps(
            {
                "schema_version": "subtitle-audio-correspondence-provenance.v1",
                "actual_media_sha256": _sha(media_path),
                "witness_srt_sha256": _sha(witness_path),
                "timebase": timebase,
                # This value is intentionally not used as evidence by the checker.
                "status": "PASS",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return final_path, media_path, witness_path, provenance_path, intro_offset_ms


_TEXTS = [
    "开场这句内容很特别",
    "中段这里继续说明问题",
    "最后我们明确完成收束",
]


def test_c7b_like_9750ms_global_shift_blocks(tmp_path: Path):
    final_rows = [(1000, 5000, _TEXTS[0]), (23000, 27000, _TEXTS[1]), (42000, 46000, _TEXTS[2])]
    witness_rows = [(start + 9750, end + 9750, text) for start, end, text in final_rows]
    final, media, witness, provenance, offset = _inputs(
        tmp_path, final_rows=final_rows, witness_rows=witness_rows
    )

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "BLOCK"
    assert "GLOBAL_TIMING_SHIFT" in receipt["reason_codes"]
    assert receipt["timing_summary"]["median_start_delta_ms"] == 9750
    assert receipt["anchor_coverage"]["status"] == "PASS"


def test_corrected_timing_passes_and_intro_is_added_once(tmp_path: Path):
    intro_offset_ms = 6200
    final_rows = [(1000, 5000, _TEXTS[0]), (23000, 27000, _TEXTS[1]), (42000, 46000, _TEXTS[2])]
    witness_rows = [(start + intro_offset_ms, end + intro_offset_ms, text) for start, end, text in final_rows]
    final, media, witness, provenance, offset = _inputs(
        tmp_path,
        final_rows=final_rows,
        witness_rows=witness_rows,
        intro_offset_ms=intro_offset_ms,
        declared_intro_offset_ms=intro_offset_ms,
    )
    before = final.read_text(encoding="utf-8")

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "PASS"
    assert receipt["timing_status"] == "PASS"
    assert receipt["text_correctness_status"] == "UNASSESSED"
    assert receipt["timebase"]["intro_offset_application_count"] == 1
    assert receipt["timebase"]["mapping"] == "witness_ms = final_delivery_ms + intro_offset_ms"
    assert final.read_text(encoding="utf-8") == before


def test_fuzzy_anchor_requires_shared_text_start_and_rejects_b_false_match(tmp_path: Path):
    # These rows preserve the relevant B timing/text shape: the cue-47-like
    # overlap shares only a later phrase, while the PSP/LIVE pair is a valid
    # ASR tail variation with the same sentence start.
    intro_offset_ms = 5750
    final_rows = [
        (250, 1800, "主播什么时候进的psp"),
        (48720, 51000, "主播现在确实是 PSP LIVE 了"),
        (108240, 109560, "channel家族"),
        (112500, 113660, "本人就是关系户"),
        (204960, 208560, "因为暗影大熊猫，我是隐藏姿态"),
    ]
    witness_rows = [
        (6060, 8000, "主播什么时候进的 PSP"),
        (54500, 57000, "主播现在确实是 PSP 来舞了"),
        (109320, 113220, "谁哈 CHANEL 家族吗"),
        (118250, 119450, "本人就是关系户"),
        (210740, 214140, "因为暗影大熊猫，我是隐藏姿态"),
    ]
    final, media, witness, provenance, offset = _inputs(
        tmp_path,
        final_rows=final_rows,
        witness_rows=witness_rows,
        intro_offset_ms=intro_offset_ms,
    )

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "PASS"
    assert receipt["timing_status"] == "PASS"
    assert receipt["timing_summary"]["anchor_count"] == 4
    assert receipt["anchor_policy"]["fuzzy_start_anchor_rule"] == (
        "first_nonempty_matching_block_starts_at_zero_on_both_sides"
    )
    anchor_texts = {row["final_text"] for row in receipt["anchors"]}
    assert "channel家族" not in anchor_texts
    assert "主播现在确实是 PSP LIVE 了" in anchor_texts


def test_fuzzy_start_anchor_still_blocks_global_shift(tmp_path: Path):
    final_rows = [
        (1000, 5000, "主播现在确实是 PSP LIVE 了"),
        (23000, 27000, _TEXTS[1]),
        (42000, 46000, _TEXTS[2]),
    ]
    witness_rows = [
        (10750, 14750, "主播现在确实是 PSP 来舞了"),
        (32750, 36750, _TEXTS[1]),
        (51750, 55750, _TEXTS[2]),
    ]
    final, media, witness, provenance, offset = _inputs(
        tmp_path, final_rows=final_rows, witness_rows=witness_rows
    )

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "BLOCK"
    assert "GLOBAL_TIMING_SHIFT" in receipt["reason_codes"]
    assert receipt["timing_summary"]["median_start_delta_ms"] == 9750
    assert any(row["similarity"] < 1.0 for row in receipt["anchors"])


def test_fuzzy_start_anchor_still_blocks_local_drift(tmp_path: Path):
    final_rows = [
        (1000, 5000, _TEXTS[0]),
        (23000, 27000, _TEXTS[1]),
        (42000, 46000, "主播现在确实是 PSP LIVE 了"),
    ]
    witness_rows = [
        (1000, 5000, _TEXTS[0]),
        (23000, 27000, _TEXTS[1]),
        (46000, 50000, "主播现在确实是 PSP 来舞了"),
    ]
    final, media, witness, provenance, offset = _inputs(
        tmp_path, final_rows=final_rows, witness_rows=witness_rows
    )

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "BLOCK"
    assert "INCONSISTENT_TIMING_DRIFT" in receipt["reason_codes"]
    assert receipt["timing_summary"]["start_delta_spread_ms"] == 4000
    assert any(row["similarity"] < 1.0 for row in receipt["anchors"])


def test_short_common_words_do_not_count_as_anchors(tmp_path: Path):
    rows = [(1000, 2000, "然后"), (20000, 21000, "我们"), (40000, 41000, "看看")]
    final, media, witness, provenance, offset = _inputs(tmp_path, final_rows=rows, witness_rows=rows)

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "INSUFFICIENT"
    assert receipt["reason_codes"] == ["INSUFFICIENT_UNIQUE_ANCHORS_START_MIDDLE_END"]
    assert receipt["anchors"] == []


def test_short_main_clip_coverage_excludes_long_branding_intro(tmp_path: Path):
    rows = [(100, 2000, _TEXTS[0]), (3600, 6000, _TEXTS[1]), (7800, 9500, _TEXTS[2])]
    intro = 6184
    witness = [(start + intro, end + intro, text) for start, end, text in rows]
    inputs = _inputs(tmp_path, final_rows=rows, witness_rows=witness, intro_offset_ms=intro)
    receipt = check_subtitle_audio_correspondence(*inputs)
    assert receipt["status"] == "PASS"
    assert receipt["anchor_coverage"]["start"] == ["1"]
    assert receipt["anchor_coverage"]["middle"] == ["2"]
    assert receipt["anchor_coverage"]["end"] == ["3"]
    assert receipt["timing_summary"]["median_start_delta_ms"] == 0


def test_repeated_or_missing_coverage_is_insufficient(tmp_path: Path):
    rows = [
        (1000, 5000, "唯一的开场锚点内容"),
        (20000, 24000, "唯一的中间锚点内容"),
        (40000, 41000, "然后"),
    ]
    final, media, witness, provenance, offset = _inputs(tmp_path, final_rows=rows, witness_rows=rows)

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "INSUFFICIENT"
    assert receipt["anchor_coverage"]["start"]
    assert receipt["anchor_coverage"]["middle"]
    assert receipt["anchor_coverage"]["end"] == []


def test_front_middle_end_drift_blocks(tmp_path: Path):
    final_rows = [(1000, 5000, _TEXTS[0]), (23000, 27000, _TEXTS[1]), (42000, 46000, _TEXTS[2])]
    witness_rows = [
        (1000, 5000, _TEXTS[0]),
        (23000, 27000, _TEXTS[1]),
        (46000, 50000, _TEXTS[2]),
    ]
    final, media, witness, provenance, offset = _inputs(
        tmp_path, final_rows=final_rows, witness_rows=witness_rows
    )

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "BLOCK"
    assert receipt["timing_status"] == "BLOCK"
    assert "INCONSISTENT_TIMING_DRIFT" in receipt["reason_codes"]
    assert receipt["timing_summary"]["start_delta_spread_ms"] == 4000


def test_media_hash_drift_is_rejected(tmp_path: Path):
    final, media, witness, provenance, offset = _inputs(
        tmp_path,
        final_rows=[(1000, 5000, _TEXTS[0])],
        witness_rows=[(1000, 5000, _TEXTS[0])],
    )
    media.write_bytes(b"a different actual media file")

    with pytest.raises(CorrespondenceInputError, match="PROVENANCE_MEDIA_HASH_MISMATCH"):
        check_subtitle_audio_correspondence(final, media, witness, provenance, offset)


def test_existing_timed_witness_provenance_shape_is_supported(tmp_path: Path):
    rows = [(1000, 5000, _TEXTS[0]), (23000, 27000, _TEXTS[1]), (42000, 46000, _TEXTS[2])]
    final, media, witness, provenance, offset = _inputs(
        tmp_path, final_rows=rows, witness_rows=rows
    )
    value = json.loads(provenance.read_text(encoding="utf-8"))
    value["schema_version"] = "subtitle-audio-witness-provenance.v1"
    value["media_sha256"] = value.pop("actual_media_sha256")
    value["time_domain"] = "FINAL_MEDIA_LOCAL"
    value["time_origin_ms"] = 0
    value.pop("timebase")
    provenance.write_text(json.dumps(value), encoding="utf-8")

    receipt = check_subtitle_audio_correspondence(final, media, witness, provenance, offset)

    assert receipt["status"] == "PASS"
    assert receipt["provenance_validation"]["timebase"]["provenance_time_domain"] == "FINAL_MEDIA_LOCAL"


def test_non_integer_intro_offset_is_rejected(tmp_path: Path):
    final, media, witness, provenance, _ = _inputs(
        tmp_path,
        final_rows=[(1000, 5000, _TEXTS[0])],
        witness_rows=[(1000, 5000, _TEXTS[0])],
    )

    with pytest.raises(CorrespondenceInputError, match="INTRO_OFFSET_NOT_INTEGER_MS"):
        check_subtitle_audio_correspondence(final, media, witness, provenance, 6200.0)  # type: ignore[arg-type]
