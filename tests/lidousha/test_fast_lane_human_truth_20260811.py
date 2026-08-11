from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.reviewed_speaker_baseline import load_reviewed_speaker_baseline
from src.autoslice.title_policy import (
    TitlePolicyError,
    manual_title_override,
    validate_candidate_title_surface,
)


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/reviews/evidence/2026-08-11-fast-lane-human-truth"
BASELINES = ROOT / "assets/lidousha/reviewed_subtitle_baselines"
SPEAKER_TRUTH = ROOT / "assets/lidousha/reviewed_speaker_truth"

SOLO = "auto_230125_1157_1229"
STORY = "auto_223750_578_734"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _cue_texts(path: Path) -> list[str]:
    return [
        str(cue.text).strip()
        for cue in parse_srt_cues(path.read_text(encoding="utf-8"))
    ]


def test_solo_truth_is_exactly_the_five_operator_changes_and_text_only() -> None:
    pristine = EVIDENCE / f"{SOLO}.pristine.srt"
    reviewed = EVIDENCE / f"{SOLO}.ivan-reviewed.srt"
    baseline = BASELINES / f"{SOLO}.reviewed.srt"
    before = _cue_texts(pristine)
    after = _cue_texts(reviewed)

    changed = [
        index
        for index, pair in enumerate(zip(before, after, strict=True), start=1)
        if pair[0] != pair[1]
    ]
    assert changed == [4, 8, 15, 19, 22]
    assert {index: after[index - 1] for index in changed} == {
        4: "我对xxsk说",
        8: "然后xxsk说",
        15: "然后xxsk就说：啊",
        19: "然后xxsk又说",
        22: "她说，xxsk说，好的",
    }
    assert baseline.read_bytes() == reviewed.read_bytes()
    assert "星汐" not in baseline.read_text(encoding="utf-8")
    assert _sha256(baseline) == (
        "c29cc5aed6b52cca7618a88b083b9b46e86320db5595bcaa5a4ef4c57499ca7b"
    )
    assert not (
        ROOT / f"assets/lidousha/speaker_overrides/{SOLO}.speaker.v1.json"
    ).exists()

    manifest = _json(BASELINES / f"{SOLO}.subtitle-baseline.v1.json")
    assert (
        manifest["source_recording_basename"]
        == "22966160_20260808-23-01-25.mp4"
    )
    assert manifest["source_sha256"] == (
        "6cf042681885d18ac4e3abb9548fe8ac3033f8b2e3365c44b6748fd3e4a324a3"
    )
    assert (
        manifest["absolute_source_start_ms"],
        manifest["absolute_source_end_ms"],
    ) == (1157280, 1229510)


def test_story_truth_is_full_labelled_and_hash_bound_without_machine_cues() -> None:
    truth = SPEAKER_TRUTH / f"{STORY}.truth-diff.v2.json"
    override = _json(
        ROOT / f"assets/lidousha/speaker_overrides/{STORY}.speaker.v1.json"
    )
    automatic = (
        ROOT
        / f"assets/lidousha/speaker_automatic_baselines/{STORY}.automatic-labelled.srt"
    )
    baseline = BASELINES / f"{STORY}.reviewed.srt"
    manifest = _json(BASELINES / f"{STORY}.subtitle-baseline.v1.json")

    truth_payload = _json(truth)
    assert truth_payload["summary"] == {
        "cues": 52,
        "text_changed": 10,
        "label_changed": 6,
        "mixed": 3,
        "marked": 6,
        "merged": 0,
        "timing_tweaked": 0,
    }
    assert len(override["overrides"]) == 52
    assert override["status"] == "ivan_reviewed_speaker_complete"
    assert override["reviewed_speaker_baseline"]["machine_cues"] == []
    ownership = manifest["truth_full_ownership"]
    assert ownership["machine_cues"] == []
    assert ownership["reviewed_override_count"] == 52
    assert ownership["truth_input"] == {
        "path": f"assets/lidousha/reviewed_speaker_truth/{STORY}.truth-diff.v2.json",
        "sha256": _sha256(truth),
    }
    assert manifest["sha256"] == _sha256(baseline)
    disposition = ownership["arbitration_disposition"]
    assert disposition["status"] == "NOT_REQUIRED_FULLY_LABELLED"
    assert disposition["truth_input_sha256"] == _sha256(truth)
    assert disposition["automatic_labelled_srt_sha256"] == _sha256(automatic)

    loaded = load_reviewed_speaker_baseline(
        override,
        candidate_id=STORY,
        cues=parse_srt(baseline),
        repo_root=ROOT,
    )
    assert loaded is not None
    assert loaded.machine_cues == ()
    assert loaded.evidence["reviewed_cue_count"] == 52

    texts = _cue_texts(baseline)
    assert texts[3] == "莉亚 活着"
    assert texts[33] == "呃，我这轮是跟南町nightin"
    assert texts[35] == "然后南町没跟我们一起"
    assert texts[36] == "南町nightin往右走了"


def test_fast_lane_titles_use_candidate_scoped_reviewed_surfaces() -> None:
    assert manual_title_override(SOLO) == (
        "弹幕问有没有跳《夜蝶》的对象，小李公主抱xxsk后反问“那你要负责吗”，被奶P赖上了"
    )
    story_title = manual_title_override(STORY)
    assert story_title == "莉娅求小李“就算你是狼也放过我”，结伴后小李突然连声道歉"
    assert "莉亚" not in story_title

    solo_audit = validate_candidate_title_surface(SOLO, manual_title_override(SOLO))
    story_audit = validate_candidate_title_surface(STORY, story_title)
    assert solo_audit is not None and solo_audit["status"] == "PASS"
    assert story_audit is not None and story_audit["status"] == "PASS"
    with pytest.raises(TitlePolicyError, match="PROJECTED_TEXT_SURFACE_CONFLICT"):
        validate_candidate_title_surface(
            STORY,
            "莉亚求小李就算你是狼也放过我，结伴后小李突然连声道歉",
        )


def test_explicit_operator_changes_have_required_source_interval_ledger_owners() -> None:
    ledger = _json(ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json")
    rows = [
        row
        for row in ledger["entries"]
        if str(row.get("truth_id") or "").startswith(
            ("20260808-solo-230125", "20260807-story-578734")
        )
    ]
    assert len(rows) == 15
    assert (
        sum(
            str(row["truth_id"]).startswith("20260808-solo-230125")
            for row in rows
        )
        == 5
    )
    assert (
        sum(
            str(row["truth_id"]).startswith("20260807-story-578734")
            for row in rows
        )
        == 10
    )
    assert all(row["required"] is True for row in rows)
    assert all(row["decision_authority"] == "IVAN_OPERATOR_TRUTH" for row in rows)
    assert all(row["assertion_state"] == "VERIFIED_ACTIVE" for row in rows)
    assert all(row["knowledge_type"] == "SOURCE_INTERVAL_TRUTH" for row in rows)
    assert all(int(row["source_end_ms"]) > int(row["source_start_ms"]) for row in rows)
