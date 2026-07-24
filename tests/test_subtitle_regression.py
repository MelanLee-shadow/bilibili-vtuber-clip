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


def test_candidate_truth_gate_accepts_reviewed_required_alternatives(tmp_path):
    path = _asset(
        tmp_path,
        required_any_substring_groups=[
            ["萱卡一点都不妈", "萱萱卡娅一点都不妈"],
        ],
    )
    short_name = _srt(
        "但是我确实很想跟大家看梦限大",
        "恋死看吗",
        "萱卡一点都不妈",
    )
    full_name = short_name.replace("萱卡一点都不妈", "萱萱卡娅一点都不妈")
    missing = short_name.replace("萱卡一点都不妈", "一点都不妈")

    for payload in (short_name, full_name):
        audit = verify_subtitle_regression_surfaces(
            path,
            candidate_id="auto_truth",
            final_text_srt=payload,
            final_speaker_srt=payload,
        )
        assert audit["status"] == "PASS"

    failed = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=missing,
        final_speaker_srt=missing,
    )
    assert failed["surfaces"]["final_text_srt"]["missing_required_any_groups"] == [
        ["萱卡一点都不妈", "萱萱卡娅一点都不妈"]
    ]


def test_candidate_truth_gate_requires_every_reviewed_occurrence(tmp_path):
    path = _asset(
        tmp_path,
        required_payload_min_occurrences={"姐感妹": 2},
    )
    complete = _srt(
        "但是我确实很想跟大家看梦限大",
        "恋死看吗",
        "她是一个姐感妹",
        "姐感妹",
    )
    one_missing = complete.replace("她是一个姐感妹", "她是一个桔梗妹")

    passing = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=complete,
        final_speaker_srt=complete,
    )
    failing = verify_subtitle_regression_surfaces(
        path,
        candidate_id="auto_truth",
        final_text_srt=one_missing,
        final_speaker_srt=one_missing,
    )

    assert passing["status"] == "PASS"
    assert failing["surfaces"]["final_text_srt"][
        "missing_required_min_occurrences"
    ] == [{"text": "姐感妹", "required_count": 2, "found_count": 1}]


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


@pytest.mark.parametrize(
    ("candidate_id", "poison"),
    [
        ("auto_193450_3573_3665", "她就叫晴"),
        ("auto_193450_672_945", "分牙三四关"),
        ("auto_193450_672_945", "确实要住她家了"),
        ("auto_193450_672_945", "NNL一般"),
        ("auto_193450_1863_2056", "谢谢你的素材"),
    ],
)
def test_20260722_candidate_regressions_gate_both_surfaces(candidate_id, poison):
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_regressions"
        / f"{candidate_id}.subtitle-regression.v1.json"
    )
    document, _ = load_subtitle_regression_document(path, candidate_id=candidate_id)
    truth_texts = list(document["required_payload_substrings"])
    for text, count in document["required_payload_min_occurrences"].items():
        truth_texts.extend([text] * count)
    correct = _srt(*truth_texts)
    correct_speaker = _srt(*truth_texts, speaker=True)
    passing = verify_subtitle_regression_surfaces(
        path,
        candidate_id=candidate_id,
        final_text_srt=correct,
        final_speaker_srt=correct_speaker,
    )
    assert passing["status"] == "PASS"

    poisoned = _srt(*truth_texts, poison)
    failing = verify_subtitle_regression_surfaces(
        path,
        candidate_id=candidate_id,
        final_text_srt=poisoned,
        final_speaker_srt=poisoned,
    )
    assert poison in failing["surfaces"]["final_text_srt"]["found_forbidden"]


def test_sumi_regression_requires_the_third_qin_surface():
    candidate_id = "auto_193450_3573_3665"
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_regressions"
        / f"{candidate_id}.subtitle-regression.v1.json"
    )
    document, _ = load_subtitle_regression_document(path, candidate_id=candidate_id)
    wrong_required = [
        value.replace("姐感妹秦秦秦她", "姐感妹秦秦她")
        for value in document["required_payload_substrings"]
    ]
    wrong = _srt(*wrong_required)
    audit = verify_subtitle_regression_surfaces(
        path,
        candidate_id=candidate_id,
        final_text_srt=wrong,
        final_speaker_srt=wrong,
    )
    assert "姐感妹秦秦秦她是一个非常闹腾的小朋友" in audit[
        "surfaces"
    ]["final_text_srt"]["missing_required"]


def test_nancho_regression_rejects_old_exact_single_character_xing_cue():
    candidate_id = "auto_193450_672_945"
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_regressions"
        / f"{candidate_id}.subtitle-regression.v1.json"
    )
    document, _ = load_subtitle_regression_document(path, candidate_id=candidate_id)
    truth_texts = list(document["required_payload_substrings"])
    correct = _srt(*truth_texts)
    poisoned = _srt(*truth_texts, "行")

    passing = verify_subtitle_regression_surfaces(
        path,
        candidate_id=candidate_id,
        final_text_srt=correct,
        final_speaker_srt=correct,
    )
    failing = verify_subtitle_regression_surfaces(
        path,
        candidate_id=candidate_id,
        final_text_srt=poisoned,
        final_speaker_srt=poisoned,
    )

    assert passing["status"] == "PASS"
    assert failing["surfaces"]["final_text_srt"][
        "found_forbidden_exact_cues"
    ] == ["行"]
