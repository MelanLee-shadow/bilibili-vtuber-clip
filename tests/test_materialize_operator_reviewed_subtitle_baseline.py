import hashlib
import json
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
    ReviewedSubtitleBaselineRegistryError,
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
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "source_srt": source,
        "reviewed_srt": reviewed,
        "candidate_id": CID,
        "authority": "Ivan exhaustive reviewed subtitle truth",
        "source_recording_basename": "recording.mp4",
        "source_recording_sha256": "ab" * 32,
        "absolute_source_start_ms": 100_000,
        "absolute_source_end_ms": 102_500,
        "decision_ledger": {
            "schema_version": "operator-reviewed-subtitle-decisions.v1",
            "candidate_id": CID,
            "report_scope": "EXHAUSTIVE",
            "pipeline_srt_sha256": source_sha,
            "operator_authority": {
                "kind": "IVAN_OPERATOR",
                "evidence_ref": "review: synthetic operator ruling",
            },
            "cue_decisions": [
                {
                    "cue": 1,
                    "source_index": "1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "source_text_sha256": hashlib.sha256("原一".encode()).hexdigest(),
                    "disposition": "OPERATOR_UNCHANGED_FREEZE",
                    "proposal": {
                        "text": "模型候选仅作诊断",
                        "provenance": {"provider": "GEMINI", "artifact_sha256": "ef" * 32},
                    },
                },
                {
                    "cue": 2,
                    "source_index": "2",
                    "start_ms": 1000,
                    "end_ms": 2000,
                    "source_text_sha256": hashlib.sha256("星汐说".encode()).hexdigest(),
                    "disposition": "OPERATOR_EXACT_TEXT",
                    "release_text": "xxsk说",
                    "decision_authority": {
                        "kind": "IVAN_OPERATOR",
                        "evidence_ref": "review: synthetic operator ruling",
                    },
                },
            ],
        },
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
        "schema_version": "operator-reviewed-text-full-ownership-pin.v2",
        "baseline_sha256": manifest["sha256"],
        "pipeline_srt_sha256": receipt["source_srt"]["sha256"],
        "decision_ledger_sha256": manifest["operator_truth_lanes"]["decision_ledger"]["sha256"],
        "diagnostic_diff_sha256": manifest["operator_truth_lanes"]["diff_receipt"]["sha256"],
        "operator_authority": {
            "kind": "IVAN_OPERATOR",
            "evidence_ref": "review: synthetic operator ruling",
        },
        "cue_count": 2,
        "changed_cue_count": 1,
        "operator_exact_text_cue_count": 1,
        "operator_unchanged_freeze_cue_count": 1,
        "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
    }
    assert receipt["speaker_authority"] == "NOT_CLAIMED_TEXT_ONLY"
    assert receipt["changed_cue_count"] == 1
    assert receipt["changed_cues"][0]["absolute_source_start_ms"] == 101_000

    root = tmp_path / "assets"
    root.mkdir()
    baseline = root / f"{CID}.reviewed.srt"
    baseline.write_text(str(result["baseline_srt"]), encoding="utf-8")
    pipeline_srt = root / f"{CID}.pipeline.srt"
    pipeline_srt.write_text(str(result["pipeline_diagnostic_srt"]), encoding="utf-8")
    decision_ledger = root / f"{CID}.decisions.json"
    decision_ledger.write_text(str(result["decision_ledger"]), encoding="utf-8")
    diagnostic_diff = root / f"{CID}.truth-diff.json"
    diagnostic_diff.write_text(str(result["diagnostic_diff"]), encoding="utf-8")
    manifest["path"] = baseline.name
    manifest["operator_truth_lanes"] = {
        **manifest["operator_truth_lanes"],
        "pipeline_diagnostic": {
            **manifest["operator_truth_lanes"]["pipeline_diagnostic"],
            "path": pipeline_srt.name,
        },
        "decision_ledger": {
            **manifest["operator_truth_lanes"]["decision_ledger"],
            "path": decision_ledger.name,
        },
        "diff_receipt": {
            **manifest["operator_truth_lanes"]["diff_receipt"],
            "path": diagnostic_diff.name,
        },
    }
    manifest_path = root / f"{CID}.subtitle-baseline.v1.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    loaded = load_candidate_reviewed_subtitle_baseline(root, CID)
    assert loaded is not None
    assert loaded.config["sha256"] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert loaded.fingerprint_paths == (
        manifest_path.resolve(),
        baseline.resolve(),
        pipeline_srt.resolve(),
        decision_ledger.resolve(),
        diagnostic_diff.resolve(),
    )
    ownership = resolve_operator_text_full_ownership(
        {"subtitle_redelivery_baseline": loaded.config}
    )
    assert ownership is not None
    assert ownership["schema_version"] == OPERATOR_TEXT_FULL_OWNERSHIP_SCHEMA
    assert ownership["coverage"]["speaker_ownership"] == "NOT_CLAIMED_TEXT_ONLY"
    assert ownership["coverage"]["unchanged_freeze_cue_count"] == 1
    assert "星汐说" in pipeline_srt.read_text(encoding="utf-8")
    assert json.loads(diagnostic_diff.read_text(encoding="utf-8"))["rows"][1]["release_truth_text"] == "xxsk说"
    assert json.loads(diagnostic_diff.read_text(encoding="utf-8"))["rows"][0]["proposal"]["text"] == "模型候选仅作诊断"

    pipeline_contents = pipeline_srt.read_text(encoding="utf-8")
    pipeline_srt.write_text("drift\n", encoding="utf-8")
    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="diagnostic SRT sha256"):
        load_candidate_reviewed_subtitle_baseline(root, CID)
    pipeline_srt.write_text(pipeline_contents, encoding="utf-8")

    def bind_tampered_diff(payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        diagnostic_diff.write_text(encoded, encoding="utf-8")
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        manifest["operator_truth_lanes"]["diff_receipt"]["sha256"] = digest
        manifest["operator_text_full_ownership"]["diagnostic_diff_sha256"] = digest
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    diff_payload = json.loads(str(result["diagnostic_diff"]))
    diff_payload["changed_cue_count"] = 0
    bind_tampered_diff(diff_payload)
    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="coverage is invalid"):
        load_candidate_reviewed_subtitle_baseline(root, CID)

    diff_payload = json.loads(str(result["diagnostic_diff"]))
    diff_payload["unexpected"] = "must fail closed"
    bind_tampered_diff(diff_payload)
    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="diff contract is invalid"):
        load_candidate_reviewed_subtitle_baseline(root, CID)

    ledger_payload = json.loads(str(result["decision_ledger"]))
    ledger_payload["unexpected"] = "must fail closed"
    encoded_ledger = json.dumps(ledger_payload, ensure_ascii=False, indent=2) + "\n"
    decision_ledger.write_text(encoded_ledger, encoding="utf-8")
    ledger_digest = hashlib.sha256(encoded_ledger.encode("utf-8")).hexdigest()
    manifest["operator_truth_lanes"]["decision_ledger"]["sha256"] = ledger_digest
    manifest["operator_text_full_ownership"]["decision_ledger_sha256"] = ledger_digest
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ReviewedSubtitleBaselineRegistryError, match="decision ledger contract is invalid"):
        load_candidate_reviewed_subtitle_baseline(root, CID)


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
    ledger = fixture["decision_ledger"]
    assert isinstance(ledger, dict)
    ledger["cue_decisions"][1] = {
        "cue": 2,
        "source_index": "2",
        "start_ms": 1000,
        "end_ms": 2000,
        "source_text_sha256": hashlib.sha256("星汐说".encode()).hexdigest(),
        "disposition": "OPERATOR_UNCHANGED_FREEZE",
    }
    with pytest.raises(OperatorBaselineCompileError, match="contains no operator exact text"):
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


def test_machine_proposal_cannot_be_laundered_into_operator_release_truth(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture["reviewed_srt"].write_text(_srt("星汐说"), encoding="utf-8")
    ledger = fixture["decision_ledger"]
    assert isinstance(ledger, dict)
    ledger["cue_decisions"][1] = {
        **ledger["cue_decisions"][1],
        "disposition": "MACHINE_PROPOSAL",
        "proposal": {
            "text": "ぶらどらぶ",
            "provenance": {"provider": "GEMINI", "artifact_sha256": "cd" * 32},
        },
    }
    ledger["cue_decisions"][1].pop("release_text")
    ledger["cue_decisions"][1].pop("decision_authority")

    with pytest.raises(OperatorBaselineCompileError, match="standalone machine proposal"):
        compile_operator_baseline(**fixture)


def test_machine_proposal_is_retained_for_diagnosis_but_cannot_replace_qixi_source_text(
    tmp_path: Path,
) -> None:
    source = tmp_path / "pipeline.srt"
    reviewed = tmp_path / "release-truth.srt"
    source_text = """1
00:00:00,000 --> 00:00:01,000
原句

2
00:00:01,000 --> 00:00:02,000
再见菈菈

3
00:00:02,000 --> 00:00:03,000
原错字
"""
    reviewed_text = source_text.replace("原错字", "人工精确改字")
    source.write_text(source_text, encoding="utf-8")
    reviewed.write_text(reviewed_text, encoding="utf-8")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    source_rows = [("原句", 0, 1000), ("再见菈菈", 1000, 2000), ("原错字", 2000, 3000)]
    rows = [
        {
            "cue": ordinal,
            "source_index": str(ordinal),
            "start_ms": start,
            "end_ms": end,
            "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "disposition": "OPERATOR_UNCHANGED_FREEZE",
        }
        for ordinal, (text, start, end) in enumerate(source_rows, start=1)
    ]
    rows[1] = {
        **rows[1],
        "disposition": "OPERATOR_EXACT_TEXT",
        "release_text": "再见菈菈",
        "decision_authority": {
            "kind": "IVAN_OPERATOR",
            "evidence_ref": "review: qixi synthetic exact correction",
        },
        "rejected_machine_proposal": {
            "text": "ぶらどらぶ",
            "provenance": {"provider": "GEMINI", "artifact_sha256": "ef" * 32},
        },
    }
    rows[2] = {
        **rows[2],
        "disposition": "OPERATOR_EXACT_TEXT",
        "release_text": "人工精确改字",
        "decision_authority": {
            "kind": "IVAN_OPERATOR",
            "evidence_ref": "review: qixi synthetic exact correction",
        },
    }
    result = compile_operator_baseline(
        source_srt=source,
        reviewed_srt=reviewed,
        candidate_id=CID,
        authority="display only; not decision authority",
        source_recording_basename="recording.mp4",
        source_recording_sha256="ab" * 32,
        absolute_source_start_ms=100_000,
        absolute_source_end_ms=103_000,
        decision_ledger={
            "schema_version": "operator-reviewed-subtitle-decisions.v1",
            "candidate_id": CID,
            "report_scope": "EXHAUSTIVE",
            "pipeline_srt_sha256": source_sha,
            "operator_authority": {
                "kind": "IVAN_OPERATOR",
                "evidence_ref": "review: qixi synthetic exact correction",
            },
            "cue_decisions": rows,
        },
    )

    assert "再见菈菈" in result["baseline_srt"]
    diff = json.loads(str(result["diagnostic_diff"]))
    assert diff["rows"][1]["release_truth_text"] == "再见菈菈"
    assert diff["rows"][1]["pipeline_text"] == "再见菈菈"
    assert diff["rows"][1]["rejected_machine_proposal"]["text"] == "ぶらどらぶ"
    assert diff["rows"][1]["rejected_machine_proposal"]["disposition"] == (
        "REJECTED_BY_OPERATOR_EXACT_TEXT"
    )
