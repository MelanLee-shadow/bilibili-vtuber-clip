"""Regression tests for C3 line947 canonical reclosure."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.fastlane_c3_canonical_reclosure import reclose_c3_canonical_package


ROOT = Path(__file__).resolve().parents[1]
V3_ROOT = Path("/private/tmp/c3-v3-reclosure-from-free-20260826")
OLD_PROMPT_SHA256 = "sha256:2e586da5aaf91a15a737eaa8ac1c088fb5e4fc678100f681ae7f8ce847ed0959"
CURRENT_PROMPT_SHA256 = "sha256:ea7c56f1b0941cb5f6ba82be25ed3bed9b2c9e3f94641ed9da1979505078e91a"
CURRENT_CONTEXT_SHA256 = "sha256:5cf64e8ab1b3fdd666df9c24b03a071d37834699d1ba9e232ea5df8d68486629"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not V3_ROOT.is_dir(), reason="sealed C3 v3 probe input is unavailable")
def test_c3_reclosure_projects_line947_prompt_as_single_current_authority(tmp_path: Path) -> None:
    result = reclose_c3_canonical_package(
        v3_root=V3_ROOT,
        destination=tmp_path / "package",
        repo_root=ROOT,
    )
    package = result.package_root
    record = _load(package / "auto_220021_561_670.record.json")
    publish = _load(package / "auto_220021_561_670.recut.publish.json")
    context = _load(package / "auto_220021_561_670.clip-context.json")
    story = record["story_contract"]
    source_fact = story["source_fact_review"]

    assert source_fact["decision"] == "FASTLANE_C3_TERMINAL_SOURCE_FACT_SUPERSESSION"
    assert record["publish_staging"]["source_fact_review"] == source_fact
    assert publish["source_fact_review"] == source_fact
    assert "sha256:" + hashlib.sha256(story["clip_context_prompt"].encode("utf-8")).hexdigest() == CURRENT_PROMPT_SHA256
    assert "sha256:" + hashlib.sha256(story["clip_context_prompt"].encode("utf-8")).hexdigest() != OLD_PROMPT_SHA256
    assert context["context_sha256"] == CURRENT_CONTEXT_SHA256
    assert story["clip_context_binding"]["context_sha256"] == CURRENT_CONTEXT_SHA256
    assert story["transcript_sha256"] == "sha256:0e1c094b719790beaec22f15204244c4810d8f4065708688998b7806bec9940b"
    assert result.receipt_path.is_file()


@pytest.mark.skipif(not V3_ROOT.is_dir(), reason="sealed C3 v3 probe input is unavailable")
def test_c3_reclosure_is_create_only_and_cleans_failed_destination(tmp_path: Path) -> None:
    destination = tmp_path / "package"
    destination.mkdir()
    with pytest.raises(ValueError, match="C3_DESTINATION_EXISTS"):
        reclose_c3_canonical_package(
            v3_root=V3_ROOT,
            destination=destination,
            repo_root=ROOT,
        )
    assert destination.is_dir()


@pytest.mark.skipif(not V3_ROOT.is_dir(), reason="sealed C3 v3 probe input is unavailable")
def test_c3_supersession_rejects_independent_prompt_hash_override() -> None:
    from src.autoslice.addressee_attribution import rebuild_speaker_evidence
    from src.autoslice.fastlane_c3_canonical_reclosure import _C3ReclosurePlan
    from src.autoslice.fastlane_c3_source_fact_supersession import (
        C3SourceFactSupersessionError,
        _rebuild_c3_source_fact_supersession,
    )
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.review_evidence import SourceCue

    record = _load(V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.record.json")
    publish = _load(V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.publish.json")
    source_fact = _load(
        V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.terminal-source-fact-preservation.json"
    )
    subtitle_path = V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.reviewed-baseline.srt"
    cues = parse_srt_cues(subtitle_path.read_text(encoding="utf-8"))
    source_cues = [
        SourceCue(cue_id=str(cue.index), source_start_ms=cue.start_ms, source_end_ms=cue.end_ms, text=cue.text)
        for cue in cues
    ]
    speaker_evidence = rebuild_speaker_evidence(
        record,
        source_cues,
        speaker_srt_bytes=(V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.speaker.srt").read_bytes(),
        speaker_manifest_bytes=(V3_ROOT / "auto_220021_561_670.recut.burned-successor-v2.speaker-finalization.json").read_bytes(),
    ).speaker_evidence
    transcript = "\n".join(cue.text.strip() for cue in cues if cue.text.strip())
    with pytest.raises(C3SourceFactSupersessionError, match="PROMPT_BINDING_DRIFT"):
        _rebuild_c3_source_fact_supersession(
            repo_root=ROOT,
            outer_receipt=source_fact,
            candidate_id=record["story_contract"]["candidate_id"],
            recording_date="2026-08-13",
            title=publish["title"],
            selection_hook=record["story_contract"]["selection_hook"],
            final_transcript=transcript,
            clip_context_prompt="current prompt bytes",
            clip_context_prompt_sha256=OLD_PROMPT_SHA256,
            selection_scorecard=record["story_contract"]["selection_scorecard"],
            speaker_evidence=speaker_evidence,
            speaker_finalization_sha256=source_fact["fastlane_terminal_source_fact_preservation"]["speaker_finalization_sha256"],
            final_reviewed_srt_path=subtitle_path,
            replay_plan=_C3ReclosurePlan(
                candidate_id=record["story_contract"]["candidate_id"],
                date="2026-08-13",
                input_manifest_raw_sha256="sha256:" + "0" * 64,
                input_manifest_self_seal="sha256:" + "0" * 64,
            ),
        )
