import json
from pathlib import Path

import pytest

from src.autoslice.subtitle_regression import (
    SubtitleRegressionError,
    load_subtitle_regression_document,
    verify_subtitle_regression_surfaces,
)


def _srt(*texts: str, speaker: bool = False) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        label = "[李豆沙] " if speaker else ""
        blocks.append(
            f"{index}\n00:00:{index:02d},000 --> 00:00:{index + 1:02d},000\n{label}{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _asset(tmp_path, **updates):
    document = {
        "schema_version": "lidousha-subtitle-regression.v1",
        "candidate_id": "auto_truth",
        "required_payload_substrings": ["但是我确实很想跟大家看梦限大", "恋死看吗"],
        "forbidden_payload_substrings": ["母鸡卡", "哇哭哇哭"],
        "forbidden_exact_cues": ["恋死看"],
    }
    document.update(updates)
    path = tmp_path / "auto_truth.subtitle-regression.v1.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_candidate_truth_gate_passes_both_final_surfaces_and_strips_speaker_labels(tmp_path):
    path = _asset(tmp_path)
    text = _srt("但是我确实很想跟大家看梦限大", "恋死看吗")
    speaker = _srt("但是我确实很想跟大家看梦限大", "恋死看吗", speaker=True)

    audit = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=text,
        final_speaker_srt=speaker,
    )

    assert audit["status"] == "PASS"
    assert audit["surfaces"]["final_text_srt"]["status"] == "PASS"
    assert audit["surfaces"]["final_speaker_srt"]["status"] == "PASS"


def test_candidate_truth_gate_fails_when_required_truth_disappears(tmp_path):
    path = _asset(tmp_path)
    wrong = _srt("但是我确实很想跟大家看梦限大")

    audit = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=wrong,
        final_speaker_srt=wrong,
    )

    assert audit["status"] == "FAIL"
    assert audit["surfaces"]["final_text_srt"]["missing_required"] == ["恋死看吗"]


def test_candidate_truth_gate_rejects_forbidden_substring_on_either_surface(tmp_path):
    path = _asset(tmp_path)
    text = _srt("但是我确实很想跟大家看梦限大", "恋死看吗")
    wrong_speaker = _srt(
        "但是我确实很想跟大家看梦限大，可是弹幕写母鸡卡",
        "恋死看吗",
        speaker=True,
    )

    audit = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=text,
        final_speaker_srt=wrong_speaker,
    )

    assert audit["status"] == "FAIL"
    assert audit["surfaces"]["final_speaker_srt"]["found_forbidden"] == ["母鸡卡"]


def test_forbidden_exact_cue_does_not_reject_longer_correct_question(tmp_path):
    path = _asset(tmp_path)
    correct = _srt("但是我确实很想跟大家看梦限大", "恋死看吗")
    wrong = _srt("但是我确实很想跟大家看梦限大", "恋死看")

    passing = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=correct,
        final_speaker_srt=correct,
    )
    failing = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=wrong,
        final_speaker_srt=wrong,
    )

    assert passing["status"] == "PASS"
    assert failing["surfaces"]["final_text_srt"]["found_forbidden_exact_cues"] == [
        "恋死看"
    ]


def test_truth_asset_is_bound_to_candidate_and_rejects_symlink(tmp_path):
    path = _asset(tmp_path)
    with pytest.raises(SubtitleRegressionError, match="candidate_id mismatch"):
        verify_subtitle_regression_surfaces(
            path,
            candidate_id="another_candidate",
            final_text_srt=_srt("x"),
            final_speaker_srt=_srt("x"),
        )

    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(SubtitleRegressionError, match="non-symlink"):
        verify_subtitle_regression_surfaces(
            link,
            candidate_id="auto_truth",
            final_text_srt=_srt("x"),
            final_speaker_srt=_srt("x"),
        )


def test_all_committed_subtitle_regression_assets_are_loadable():
    root = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_regressions"
    )
    paths = sorted(root.glob("*.subtitle-regression.v1.json"))
    assert paths

    for path in paths:
        candidate_id = json.loads(path.read_text(encoding="utf-8"))["candidate_id"]
        document, digest = load_subtitle_regression_document(
            path,
            candidate_id=candidate_id,
        )
        assert document["candidate_id"] == candidate_id
        assert len(digest) == 64
