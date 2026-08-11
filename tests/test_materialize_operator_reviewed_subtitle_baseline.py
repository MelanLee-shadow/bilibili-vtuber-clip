import hashlib
from pathlib import Path

import pytest

from scripts.materialize_operator_reviewed_subtitle_baseline import (
    OperatorBaselineCompileError,
    compile_operator_baseline,
)
from src.autoslice.delivery_fast_path import (
    OPERATOR_TEXT_FULL_OWNERSHIP_SCHEMA,
    resolve_operator_text_full_ownership,
)
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)


CID = "auto_1_2_3"


def _srt(second_text: str, *, second_start: str = "00:00:01,000") -> str:
    return f"""1
00:00:00,000 --> 00:00:01,000
原一

2
{second_start} --> 00:00:02,000
{second_text}
"""


def _fixture(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.srt"
    source.write_text(_srt("星汐说"), encoding="utf-8")
    reviewed = tmp_path / "reviewed.srt"
    reviewed.write_text(_srt("xxsk说"), encoding="utf-8")
    return {
        "source_srt": source,
        "reviewed_srt": reviewed,
        "candidate_id": CID,
        "authority": "Ivan exhaustive reviewed subtitle truth",
        "source_recording_basename": "recording.mp4",
        "source_recording_sha256": "ab" * 32,
        "absolute_source_start_ms": 100_000,
        "absolute_source_end_ms": 102_500,
    }


def test_compiles_text_only_exact_replay_baseline_and_receipt(tmp_path: Path) -> None:
    result = compile_operator_baseline(**_fixture(tmp_path))

    assert "xxsk说" in result["baseline_srt"]
    manifest = result["baseline_manifest"]
    receipt = result["receipt"]
    assert manifest["exact_interval_replay"] is True
    assert "truth_full_ownership" not in manifest
    pin = manifest["operator_text_full_ownership"]
    assert pin == {
        "schema_version": "operator-reviewed-text-full-ownership-pin.v1",
        "authority": "Ivan exhaustive reviewed subtitle truth",
        "baseline_sha256": manifest["sha256"],
        "source_srt_sha256": receipt["source_srt"]["sha256"],
        "cue_count": 2,
        "changed_cue_count": 1,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }
    assert receipt["speaker_authority"] == "NOT_CLAIMED_TEXT_ONLY"
    assert receipt["changed_cue_count"] == 1
    assert receipt["changed_cues"][0]["absolute_source_start_ms"] == 101_000

    root = tmp_path / "assets"
    root.mkdir()
    baseline = root / f"{CID}.reviewed.srt"
    baseline.write_text(str(result["baseline_srt"]), encoding="utf-8")
    manifest["path"] = baseline.name
    manifest_path = root / f"{CID}.subtitle-baseline.v1.json"
    import json

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    loaded = load_candidate_reviewed_subtitle_baseline(root, CID)
    assert loaded is not None
    assert loaded.config["sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    ownership = resolve_operator_text_full_ownership(
        {"subtitle_redelivery_baseline": loaded.config}
    )
    assert ownership is not None
    assert ownership["schema_version"] == OPERATOR_TEXT_FULL_OWNERSHIP_SCHEMA
    assert ownership["coverage"]["speaker_ownership"] == "NOT_CLAIMED_TEXT_ONLY"


def test_rejects_timing_drift_and_speaker_annotation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["reviewed_srt"].write_text(
        _srt("xxsk说", second_start="00:00:01,010"), encoding="utf-8"
    )
    with pytest.raises(OperatorBaselineCompileError, match="timing drift"):
        compile_operator_baseline(**fixture)

    fixture = _fixture(tmp_path / "speaker")
    fixture["reviewed_srt"].write_text(_srt("[李豆沙] xxsk说"), encoding="utf-8")
    with pytest.raises(OperatorBaselineCompileError, match="speaker annotation"):
        compile_operator_baseline(**fixture)


def test_rejects_no_change_symlink_and_out_of_bounds(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["reviewed_srt"].write_text(_srt("星汐说"), encoding="utf-8")
    with pytest.raises(OperatorBaselineCompileError, match="no operator changes"):
        compile_operator_baseline(**fixture)

    fixture = _fixture(tmp_path / "symlink")
    target = fixture["reviewed_srt"]
    link = target.with_name("link.srt")
    link.symlink_to(target)
    fixture["reviewed_srt"] = link
    with pytest.raises(OperatorBaselineCompileError, match="non-symlink"):
        compile_operator_baseline(**fixture)

    fixture = _fixture(tmp_path / "bounds")
    fixture["absolute_source_end_ms"] = 101_500
    with pytest.raises(OperatorBaselineCompileError, match="beyond"):
        compile_operator_baseline(**fixture)
