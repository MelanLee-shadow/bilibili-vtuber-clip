from pathlib import Path

import pytest

from scripts.rebuild_fastlane_c2_full_window_baseline import (
    CHANGES, FINAL_END_MS, FINAL_START_MS, PADDED_SHA256, build, project_delivery,
)
from src.autoslice.jingting_chunker import parse_srt_cues


SOURCE = Path("assets/lidousha/fastlane_c2_private/auto_203011_328_389.padded.fresh.srt")
DELIVERY_ONLY = Path("assets/lidousha/fastlane_c2_private/auto_203011_328_389.pipeline-diagnostic.srt")


def test_c2_full_source_is_exact_padded_44_cue_authority():
    diagnostic, reviewed, ledger, projection = build(SOURCE)
    assert __import__("hashlib").sha256(diagnostic.encode()).hexdigest() == PADDED_SHA256
    assert len(parse_srt_cues(diagnostic)) == 44
    assert len(parse_srt_cues(reviewed)) == 44
    assert len(ledger["cue_decisions"]) == 44
    assert len(projection) == 22
    assert projection[0]["full_cue"] == 3
    assert projection[-1]["full_cue"] == 24


def test_c2_only_named_full_window_text_changes_are_exact():
    diagnostic, reviewed, _ledger, _projection = build(SOURCE)
    before, after = parse_srt_cues(diagnostic), parse_srt_cues(reviewed)
    changed = {n: cue.text for n, (old, cue) in enumerate(zip(before, after, strict=True), start=1) if old.text != cue.text}
    assert changed == CHANGES
    assert after[6].text == "小豆老公；； 不是你老公"
    assert after[22].text == "小豆哪有好吵"


def test_c2_rejects_delivery_only_grid_as_full_window_projection():
    with pytest.raises(ValueError, match="C2_DELIVERY_PROJECTION_STRADDLER"):
        project_delivery(parse_srt_cues(DELIVERY_ONLY.read_text()), final_start_ms=FINAL_START_MS, final_end_ms=FINAL_END_MS)


def test_c2_rejects_wrong_offset_and_straddling_crop():
    cues = parse_srt_cues(SOURCE.read_text())
    with pytest.raises(ValueError, match="C2_DELIVERY_PROJECTION_STRADDLER"):
        project_delivery(cues, final_start_ms=10_001, final_end_ms=FINAL_END_MS)


def test_c2_rejects_source_hash_drift(tmp_path):
    bad = tmp_path / "padded.srt"
    bad.write_text(SOURCE.read_text().replace("官网", "官 网", 1))
    with pytest.raises(ValueError, match="C2_PADDED_FRESH_SHA256_DRIFT"):
        build(bad)
