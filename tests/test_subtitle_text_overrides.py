import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import (
    apply_document,
    decision_output_witness_sha256,
    parse_srt,
    source_cue_witness_sha256,
    validate_bound_override_document,
)


SOURCE = """1
00:00:00,000 --> 00:00:02,000
TA想问是三个位置哦

2
00:00:02,000 --> 00:00:04,000
因为李豆沙会一直说我帅

3
00:00:04,000 --> 00:00:05,000
哼，啊，不对
"""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_hash_bound_text_overrides_run_before_speaker_and_preserve_timing(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = {
        "schema_version": 1,
        "source_srt_sha256": _sha(source.read_bytes()),
        "overrides": [
            {
                "source_cue": 1,
                "expect": {"start": "00:00:00,000", "end": "00:00:02,000", "text": "TA想问是三个位置哦"},
                "authority": "Ivan direct correction",
                "text": "她想问是三个位置哦",
            },
            {
                "source_cue": 2,
                "expect": {"start": "00:00:02,000", "end": "00:00:04,000", "text": "因为李豆沙会一直说我帅"},
                "authority": "Ivan direct correction",
                "text": "因为李豆沙会一直说话",
            },
            {
                "source_cue": 3,
                "expect": {"start": "00:00:04,000", "end": "00:00:05,000", "text": "哼，啊，不对"},
                "authority": "Ivan direct correction",
                "action": "drop",
                "reason": "simultaneous non-semantic vocalization",
            },
        ],
    }
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "text-final.srt"
    manifest_path = tmp_path / "text-final.manifest.json"

    manifest = apply_document(source, overrides, output, manifest_path)

    cues = parse_srt(output)
    assert [cue.text for cue in cues] == ["她想问是三个位置哦", "因为李豆沙会一直说话"]
    assert [(cue.start, cue.end) for cue in cues] == [
        ("00:00:00,000", "00:00:02,000"),
        ("00:00:02,000", "00:00:04,000"),
    ]
    assert manifest["stage_order"].endswith("human_text_then_speaker_then_burn")
    assert manifest["output_srt_sha256"] == _sha(output.read_bytes())


def test_text_override_rejects_source_or_expected_cue_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = {
        "schema_version": 1,
        "source_srt_sha256": "0" * 64,
        "overrides": [],
    }
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        apply_document(source, overrides, tmp_path / "out.srt", tmp_path / "manifest.json")

    document["source_srt_sha256"] = _sha(source.read_bytes())
    document["overrides"] = [
        {
            "source_cue": 1,
            "expect": {"start": "00:00:00,000", "end": "00:00:02,000", "text": "stale"},
            "authority": "Ivan direct correction",
            "text": "她想问是三个位置哦",
        }
    ]
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="text drift"):
        apply_document(source, overrides, tmp_path / "out.srt", tmp_path / "manifest.json")


def test_bound_human_decision_proves_candidate_source_and_derived_final(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    final_payload = (
        "1\n00:00:00,000 --> 00:00:02,000\n她想问是三个位置哦\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n因为李豆沙会一直说我帅\n\n"
        "3\n00:00:04,000 --> 00:00:05,000\n哼，啊，不对\n"
    )
    final_sha = _sha(final_payload.encode("utf-8"))
    document = {
        "schema_version": 1,
        "candidate_id": "promo_test",
        "source_srt_sha256": _sha(source.read_bytes()),
        "text_final_srt_sha256": final_sha,
        "overrides": [
            {
                "source_cue": 1,
                "expect": {
                    "start": "00:00:00,000",
                    "end": "00:00:02,000",
                    "text": "TA想问是三个位置哦",
                },
                "authority": "Ivan direct correction",
                "text": "她想问是三个位置哦",
            }
        ],
    }
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    validate_bound_override_document(
        source,
        decision,
        candidate_id="promo_test",
        expected_source_srt_sha256=document["source_srt_sha256"],
        expected_final_srt_sha256=final_sha,
    )

    document["candidate_id"] = "wrong_candidate"
    decision.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="candidate_id mismatch"):
        validate_bound_override_document(
            source,
            decision,
            candidate_id="promo_test",
            expected_source_srt_sha256=document["source_srt_sha256"],
            expected_final_srt_sha256=final_sha,
        )

    document["candidate_id"] = "promo_test"
    document["text_final_srt_sha256"] = "0" * 64
    decision.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match the batch plan"):
        validate_bound_override_document(
            source,
            decision,
            candidate_id="promo_test",
            expected_source_srt_sha256=document["source_srt_sha256"],
            expected_final_srt_sha256=final_sha,
        )


def _cue_bound_document(source: Path) -> dict:
    document = {
        "schema_version": 2,
        "candidate_id": "cue_bound_test",
        "source_cue_count": 3,
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:02,000",
                    "end": "00:00:04,000",
                    "text": "因为李豆沙会一直说我帅",
                },
                "authority": "Ivan direct correction",
                "text": "因为李豆沙会一直说话",
            }
        ],
    }
    cues = parse_srt(source)
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(cues, document)
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(cues, document)
    return document


def test_cue_bound_override_ignores_unreviewed_punctuation_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = _cue_bound_document(source)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(SOURCE.replace("哼，啊，不对", "哼啊，不对。"), encoding="utf-8")
    output = tmp_path / "out.srt"
    manifest = apply_document(source, overrides, output, tmp_path / "manifest.json")

    assert [cue.text for cue in parse_srt(output)] == [
        "TA想问是三个位置哦",
        "因为李豆沙会一直说话",
        "哼啊，不对。",
    ]
    assert manifest["override_schema_version"] == 2
    assert manifest["candidate_id"] == "cue_bound_test"


def test_cue_bound_override_rejects_reviewed_cue_or_count_drift(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = _cue_bound_document(source)
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(SOURCE.replace("因为李豆沙会一直说我帅", "因为李豆沙会说我帅"), encoding="utf-8")
    with pytest.raises(ValueError, match="witness mismatch"):
        apply_document(source, overrides, tmp_path / "out.srt", tmp_path / "manifest.json")

    source.write_text(SOURCE.rsplit("\n\n", 1)[0] + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cue count drift"):
        apply_document(source, overrides, tmp_path / "out.srt", tmp_path / "manifest.json")


def test_cue_bound_override_accepts_only_reviewed_source_text_alternatives(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = _cue_bound_document(source)
    document["overrides"][0]["expect"]["text_alternatives"] = [
        "因为李豆沙会一直说我帅。",
    ]
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(
        parse_srt(source), document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(
        SOURCE.replace("因为李豆沙会一直说我帅", "因为李豆沙会一直说我帅。"),
        encoding="utf-8",
    )
    output = tmp_path / "out.srt"
    apply_document(source, overrides, output, tmp_path / "manifest.json")
    assert parse_srt(output)[1].text == "因为李豆沙会一直说话"

    source.write_text(
        SOURCE.replace("因为李豆沙会一直说我帅", "因为李豆沙会说我帅"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="witness mismatch"):
        apply_document(source, overrides, output, tmp_path / "manifest.json")


def test_timeline_bound_override_survives_unrelated_cue_resegmentation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE, encoding="utf-8")
    document = _cue_bound_document(source)
    document["schema_version"] = 3
    document.pop("source_cue_count")
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(
        parse_srt(source), document
    )
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(
        parse_srt(source), document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    resegmented = """1
00:00:02,000 --> 00:00:04,000
因为李豆沙会一直说我帅

2
00:00:04,000 --> 00:00:05,000
哼，啊，不对
"""
    source.write_text(resegmented, encoding="utf-8")
    output = tmp_path / "out.srt"
    manifest = apply_document(source, overrides, output, tmp_path / "manifest.json")

    assert [cue.text for cue in parse_srt(output)] == [
        "因为李豆沙会一直说话",
        "哼，啊，不对",
    ]
    assert manifest["override_schema_version"] == 3

    source.write_text(resegmented.replace("00:00:02,000", "00:00:02,100"), encoding="utf-8")
    with pytest.raises(ValueError, match="matched 0 cues"):
        apply_document(source, overrides, output, tmp_path / "manifest.json")


def test_timeline_substring_override_survives_sentence_resegmentation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        """1
00:01:30,930 --> 00:01:34,510
应该不是不可以把杖剑传说换成Galgame 的意思吧
""",
        encoding="utf-8",
    )
    document = {
        "schema_version": 3,
        "candidate_id": "substring_test",
        "source_cue_witness_sha256": "",
        "decision_output_witness_sha256": "",
        "overrides": [
            {
                "source_cue": 1,
                "action": "replace_substring",
                "locator": {
                    "start": "00:01:29,000",
                    "end": "00:01:35,500",
                },
                "old_text": "换成",
                "text": "玩成",
                "authority": "reviewed acoustic decision",
            }
        ],
    }
    cues = parse_srt(source)
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(cues, document)
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(
        cues, document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(
        """1
00:01:29,950 --> 00:01:32,510
私人飞机应该不是不可以

2
00:01:32,510 --> 00:01:34,510
把杖剑传说换成Galgame 的意思吧
""",
        encoding="utf-8",
    )
    output = tmp_path / "out.srt"
    apply_document(source, overrides, output, tmp_path / "manifest.json")

    assert "把杖剑传说玩成Galgame" in output.read_text(encoding="utf-8")
    assert "换成Galgame" not in output.read_text(encoding="utf-8")

    source.write_text(
        source.read_text(encoding="utf-8").replace("换成", "变成"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="matched 0 cues"):
        apply_document(source, overrides, output, tmp_path / "manifest.json")


def test_optional_timeline_substring_override_skips_when_source_is_already_clean(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        """1
00:00:55,730 --> 00:00:58,650
don't know那么多，所有的
""",
        encoding="utf-8",
    )
    document = {
        "schema_version": 3,
        "candidate_id": "optional_substring_test",
        "source_cue_witness_sha256": "",
        "decision_output_witness_sha256": "",
        "overrides": [
            {
                "source_cue": 1,
                "action": "replace_substring",
                "locator": {
                    "start": "00:00:55,000",
                    "end": "00:00:59,000",
                },
                "old_text": "don't know",
                "text": "都问",
                "required": False,
                "authority": "reviewed conditional repair",
            }
        ],
    }
    cues = parse_srt(source)
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(cues, document)
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(
        cues, document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(
        """1
00:00:55,730 --> 00:00:57,410
懂得那么多。
""",
        encoding="utf-8",
    )
    output = tmp_path / "out.srt"
    manifest = apply_document(source, overrides, output, tmp_path / "manifest.json")

    assert output.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert manifest["decisions"] == []


def test_timeline_override_can_ignore_punctuation_but_not_word_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        """1
00:00:51,570 --> 00:00:55,370
呵，“侄女，直系女同喜欢吗？”
""",
        encoding="utf-8",
    )
    document = {
        "schema_version": 3,
        "candidate_id": "punctuation_test",
        "source_cue_witness_sha256": "",
        "decision_output_witness_sha256": "",
        "overrides": [
            {
                "source_cue": 1,
                "action": "replace",
                "expect": {
                    "start": "00:00:51,570",
                    "end": "00:00:55,370",
                    "text": "呵，“侄女，直系女同喜欢吗？”",
                    "text_match_mode": "punctuation_insensitive",
                },
                "text": "呵，直女，直系女同喜欢吗？",
                "authority": "reviewed homophone correction",
            }
        ],
    }
    cues = parse_srt(source)
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(cues, document)
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(
        cues, document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    source.write_text(
        """1
00:00:51,570 --> 00:00:55,370
呵，“侄女直系女同喜欢吗？”
""",
        encoding="utf-8",
    )
    output = tmp_path / "out.srt"
    apply_document(source, overrides, output, tmp_path / "manifest.json")
    assert "呵，侄女，直系女同喜欢吗？" in output.read_text(encoding="utf-8")
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["decisions"][0]["requested_output_text"] == (
        "呵，直女，直系女同喜欢吗？"
    )
    assert manifest["decisions"][0]["output_text"] == (
        "呵，侄女，直系女同喜欢吗？"
    )
    assert manifest["decisions"][0]["final_surface_policy"]["authority"] == (
        "lidousha-hard-meme-canon.v1"
    )

    source.write_text(
        source.read_text(encoding="utf-8").replace("直系女同", "女同"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="text drift"):
        apply_document(source, overrides, output, tmp_path / "manifest.json")


def test_timeline_pattern_override_preserves_prefix_and_cleans_optional_punctuation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        """1
00:00:47,790 --> 00:00:49,590
有没有侄女女友喜欢吗？”
""",
        encoding="utf-8",
    )
    document = {
        "schema_version": 3,
        "candidate_id": "pattern_test",
        "source_cue_witness_sha256": "",
        "decision_output_witness_sha256": "",
        "overrides": [
            {
                "source_cue": 1,
                "action": "replace_pattern",
                "locator": {
                    "start": "00:00:47,000",
                    "end": "00:00:50,000",
                },
                "pattern": "侄女女友喜欢吗[？?]?[”\\\"]?",
                "text": "直女女友喜欢吗？",
                "authority": "reviewed flexible phrase repair",
            }
        ],
    }
    cues = parse_srt(source)
    document["source_cue_witness_sha256"] = source_cue_witness_sha256(cues, document)
    document["decision_output_witness_sha256"] = decision_output_witness_sha256(
        cues, document
    )
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out.srt"

    apply_document(source, overrides, output, tmp_path / "manifest.json")
    assert "有没有侄女女友喜欢吗？" in output.read_text(encoding="utf-8")
    assert "”" not in output.read_text(encoding="utf-8")

    source.write_text(
        """1
00:00:47,790 --> 00:00:49,590
侄女女友喜欢吗
""",
        encoding="utf-8",
    )
    apply_document(source, overrides, output, tmp_path / "manifest.json")
    assert "侄女女友喜欢吗？" in output.read_text(encoding="utf-8")


def test_committed_dog_clip_override_rejects_collateral_word_salad(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    reviewed_cues = [
        ("00:00:23,500", "00:00:25,100", "如果你是狗"),
        ("00:00:31,800", "00:00:33,760", "抱紧了，抱紧了"),
        ("00:00:33,760", "00:00:36,320", "我可怎么就不……"),
        ("00:00:45,158", "00:00:47,170", "的心动人你太坏了"),
        ("00:00:48,806", "00:00:49,806", "如果你是狗"),
        ("00:00:52,780", "00:00:54,700", "如果你是狗"),
        ("00:01:02,500", "00:01:04,740", "如果你是狗"),
        ("00:01:31,830", "00:01:35,670", "如果你是狗，太可怜了，抱紧了我"),
        ("00:01:35,710", "00:01:39,470", "可怎么就不给我一点机会"),
        ("00:01:39,700", "00:01:41,580", "别再离开我了"),
    ]
    source.write_text(
        "\n\n".join(
            f"{index}\n{start} --> {end}\n{text}"
            for index, (start, end, text) in enumerate(reviewed_cues, start=1)
        )
        + "\n",
        encoding="utf-8",
    )
    override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/auto_162645_394_507.text.v1.json"
    )
    output = tmp_path / "out.srt"

    manifest = apply_document(
        source,
        override,
        output,
        tmp_path / "manifest.json",
    )

    assert [cue.text for cue in parse_srt(output)] == ["人类你太坏了！"]
    assert len(manifest["decisions"]) == 10
    assert sum(row["action"] == "drop" for row in manifest["decisions"]) == 9
    assert any(
        row["authority"].startswith("The immediately repeated complaint")
        for row in manifest["decisions"]
    )


@pytest.mark.parametrize(
    "source_text",
    [
        "让礼墨线下叫kmx",
        "让刘莎线下叫停了时",
        "让李豆沙线下叫停了时",
        "让李豆沙线下叫kmx",
    ],
)
def test_committed_kmx_override_accepts_known_surfaces_and_canonicalizes(
    tmp_path: Path,
    source_text: str,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:14,540 --> 00:00:17,740\n" + source_text + "\n",
        encoding="utf-8",
    )
    override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/auto_225942_962_980.text.v1.json"
    )
    output = tmp_path / "out.srt"

    apply_document(
        source,
        override,
        output,
        tmp_path / "manifest.json",
    )

    assert "让李豆沙线下叫kmx" in output.read_text(encoding="utf-8")


def test_committed_kmx_override_rebases_after_boundary_recut(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:04,770 --> 00:00:07,970\n让李豆沙线下叫停了时\n",
        encoding="utf-8",
    )
    override = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/subtitle_text_overrides/auto_225942_962_980.text.v1.json"
    )
    output = tmp_path / "out.srt"

    manifest = apply_document(
        source,
        override,
        output,
        tmp_path / "manifest.json",
        timeline_offset_ms=9_770,
    )

    assert "00:00:04,770 --> 00:00:07,970" in output.read_text(encoding="utf-8")
    assert "让李豆沙线下叫kmx" in output.read_text(encoding="utf-8")
    assert manifest["source_timeline_offset_ms"] == 9_770
    assert manifest["source_cue_witness_sha256"] == json.loads(
        override.read_text(encoding="utf-8")
    )["source_cue_witness_sha256"]
