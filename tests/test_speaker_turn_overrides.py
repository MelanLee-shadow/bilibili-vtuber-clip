import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_speaker_turn_overrides import (
    apply_overrides,
    parse_labelled_srt,
    write_ass,
    write_srt,
)


SOURCE_TEXT = """1
00:00:00,000 --> 00:00:01,000
[连线 -0.08] 第一条

2
00:00:01,000 --> 00:00:04,000
[连线 +0.06] 因为李豆沙会一直说，啊？啥意思？啊？
"""


def _document() -> dict:
    return {
        "schema_version": 1,
        "overrides": [
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:01,000",
                    "end": "00:00:04,000",
                    "text": "因为李豆沙会一直说，啊？啥意思？啊？",
                },
                "authority": "Ivan direct correction",
                "segments": [
                    {
                        "start": "00:00:01,000",
                        "end": "00:00:03,000",
                        "speaker": "连线",
                        "text": "因为李豆沙会一直说话",
                    },
                    {
                        "start": "00:00:03,000",
                        "end": "00:00:04,000",
                        "speaker": "李豆沙",
                        "text": "啥意思啊",
                    },
                ],
            }
        ],
    }


def test_override_can_split_one_asr_cue_into_two_speaker_turns(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    cues = apply_overrides(parse_labelled_srt(source), _document())

    assert [(cue.speaker, cue.text) for cue in cues] == [
        ("连线", "第一条"),
        ("连线", "因为李豆沙会一直说话"),
        ("李豆沙", "啥意思啊"),
    ]
    assert cues[1].end == cues[2].start == "00:00:03,000"
    assert cues[1].decision_source == cues[2].decision_source == "reviewed_override"

    output_srt = tmp_path / "output.srt"
    output_ass = tmp_path / "output.ass"
    write_srt(cues, output_srt)
    write_ass(cues, output_ass)
    assert "3\n00:00:03,000 --> 00:00:04,000\n[李豆沙] 啥意思啊" in output_srt.read_text(
        encoding="utf-8"
    )
    assert "Style: LDS" in output_ass.read_text(encoding="utf-8")
    assert "Style: GUEST" in output_ass.read_text(encoding="utf-8")


def test_override_can_drop_non_content_source_cue(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    document["overrides"].insert(
        0,
        {
            "source_cue": 1,
            "expect": {
                "start": "00:00:00,000",
                "end": "00:00:01,000",
                "text": "第一条",
            },
            "authority": "Ivan direct correction",
            "action": "drop",
            "reason": "simultaneous non-content vocalization",
        },
    )

    cues = apply_overrides(parse_labelled_srt(source), document)
    assert [(cue.speaker, cue.text) for cue in cues] == [
        ("连线", "因为李豆沙会一直说话"),
        ("李豆沙", "啥意思啊"),
    ]


def test_override_can_render_reliable_overlap_on_second_ass_layer(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    document["overrides"][0]["overlays"] = [
        {
            "start": "00:00:01,500",
            "end": "00:00:02,100",
            "speaker": "连线",
            "speaker_detail": "安晚/Awa",
            "text": "暂时",
        }
    ]

    cues = apply_overrides(parse_labelled_srt(source), document)
    overlap = next(cue for cue in cues if cue.decision_source == "reviewed_overlay")
    assert overlap.layer == 1
    assert overlap.placement == "above"
    assert overlap.speaker_detail == "安晚/Awa"

    output_srt = tmp_path / "output.srt"
    output_ass = tmp_path / "output.ass"
    write_srt(cues, output_srt)
    write_ass(cues, output_ass)
    assert "[连线] 暂时" in output_srt.read_text(encoding="utf-8")
    ass_text = output_ass.read_text(encoding="utf-8")
    assert "Style: GUEST_OVERLAP" in ass_text
    assert "Dialogue: 1,0:00:01.50,0:00:02.10,GUEST_OVERLAP" in ass_text
    assert "[连线]" not in ass_text  # production ASS uses colour, not debug prefixes


def test_ass_layout_preserves_libass_line_break_marker(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n"
        "[李豆沙] 这是一个需要在标点附近换行，才能保持两行以内的很长字幕文本\n",
        encoding="utf-8",
    )
    output_ass = tmp_path / "output.ass"
    write_ass(parse_labelled_srt(source), output_ass)
    ass_text = output_ass.read_text(encoding="utf-8")
    assert r"\N" in ass_text
    assert r"\\N" not in ass_text


def test_override_rejects_overlay_outside_source_interval(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    document["overrides"][0]["overlays"] = [
        {
            "start": "00:00:00,900",
            "end": "00:00:01,500",
            "speaker": "连线",
            "text": "暂时",
        }
    ]
    with pytest.raises(ValueError, match="escapes source interval"):
        apply_overrides(parse_labelled_srt(source), document)


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda doc: doc["overrides"][0]["segments"][1].update(start="00:00:03,100"), "gap or overlap"),
        (lambda doc: doc["overrides"][0]["expect"].update(text="漂移后的文本"), "text drift"),
        (lambda doc: doc["overrides"][0].update(authority=""), "no authority"),
    ],
)
def test_override_fails_closed_on_invalid_evidence(
    tmp_path: Path, mutator, message: str
) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    mutator(document)
    with pytest.raises(ValueError, match=message):
        apply_overrides(parse_labelled_srt(source), document)


def test_cli_rejects_source_hash_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import apply_speaker_turn_overrides as module

    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    document["source_srt_sha256"] = hashlib.sha256(b"different").hexdigest()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_speaker_turn_overrides.py",
            "--source",
            str(source),
            "--overrides",
            str(overrides),
            "--output-srt",
            str(tmp_path / "output.srt"),
            "--output-ass",
            str(tmp_path / "output.ass"),
            "--manifest",
            str(tmp_path / "manifest.json"),
        ],
    )
    with pytest.raises(ValueError, match="source SRT hash mismatch"):
        module.main()


def test_successful_cli_writes_self_consistent_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import apply_speaker_turn_overrides as module

    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = _document()
    document["source_srt_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output_srt = tmp_path / "output.srt"
    output_ass = tmp_path / "output.ass"
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_speaker_turn_overrides.py",
            "--source",
            str(source),
            "--overrides",
            str(overrides),
            "--output-srt",
            str(output_srt),
            "--output-ass",
            str(output_ass),
            "--manifest",
            str(manifest_path),
        ],
    )

    assert module.main() == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    assert manifest["source_srt_sha256"] == digest(source)
    assert manifest["overrides_sha256"] == digest(overrides)
    assert manifest["output_srt_sha256"] == digest(output_srt)
    assert manifest["output_ass_sha256"] == digest(output_ass)
    assert manifest["output_cue_count"] == 3
    assert manifest["source_cue_count"] == 2
    assert manifest["reviewed_output_cue_count"] == 2
    assert manifest["inherited_output_cue_count"] == 1
    assert manifest["overlap_output_cue_count"] == 0
    assert manifest["dropped_source_cues"] == []
    assert manifest["fully_reviewed"] is False
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_manifest_records_drop_and_reliable_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import apply_speaker_turn_overrides as module

    source = tmp_path / "source.srt"
    source.write_text(SOURCE_TEXT, encoding="utf-8")
    document = {
        "schema_version": 1,
        "status": "targeted_user_review_complete",
        "source_srt_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "overrides": [
            {
                "source_cue": 1,
                "expect": {
                    "start": "00:00:00,000",
                    "end": "00:00:01,000",
                    "text": "第一条",
                },
                "authority": "Ivan direct correction",
                "action": "drop",
                "reason": "simultaneous non-content vocalization",
            },
            {
                "source_cue": 2,
                "expect": {
                    "start": "00:00:01,000",
                    "end": "00:00:04,000",
                    "text": "因为李豆沙会一直说，啊？啥意思？啊？",
                },
                "authority": "Ivan direct correction",
                "segments": [
                    {
                        "start": "00:00:01,000",
                        "end": "00:00:04,000",
                        "speaker": "李豆沙",
                        "text": "但是对我来说",
                    }
                ],
                "overlays": [
                    {
                        "start": "00:00:01,500",
                        "end": "00:00:02,100",
                        "speaker": "连线",
                        "speaker_detail": "安晚/Awa",
                        "text": "暂时",
                    }
                ],
            },
        ],
    }
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    output_srt = tmp_path / "output.srt"
    output_ass = tmp_path / "output.ass"
    manifest_path = tmp_path / "manifest.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_speaker_turn_overrides.py",
            "--source",
            str(source),
            "--overrides",
            str(overrides),
            "--output-srt",
            str(output_srt),
            "--output-ass",
            str(output_ass),
            "--manifest",
            str(manifest_path),
        ],
    )

    assert module.main() == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_cue_count"] == 2
    assert manifest["output_cue_count"] == 2
    assert manifest["reviewed_output_cue_count"] == 2
    assert manifest["inherited_output_cue_count"] == 0
    assert manifest["overlap_output_cue_count"] == 1
    assert manifest["dropped_source_cues"] == [
        {
            "source_cue": 1,
            "reason": "simultaneous non-content vocalization",
            "authority": "Ivan direct correction",
        }
    ]
    assert manifest["review_status"] == "targeted_user_review_complete"
    assert manifest["fully_reviewed"] is True
