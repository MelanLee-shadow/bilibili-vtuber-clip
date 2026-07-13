import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_subtitle_text_overrides import (
    apply_document,
    parse_srt,
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
