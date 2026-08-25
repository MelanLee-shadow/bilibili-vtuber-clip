from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autoslice import c5_start_clamp as c5
from src.autoslice import reviewed_text_only_speaker_successor as successor_module
from scripts.apply_speaker_turn_overrides import Cue, write_srt
from scripts.apply_subtitle_text_overrides import parse_srt
from src.autoslice.addressee_attribution import SpeakerEvidenceState, build_addressee_evidence
from src.autoslice.review_evidence import SourceCue
from src.autoslice.reviewed_text_only_speaker_successor import (
    ReviewedTextOnlySpeakerSuccessorError,
    build_text_only_speaker_successor_fields,
    materialize_text_only_speaker_successor,
)
from src.autoslice.speaker_common import SPEAKER_FINALIZATION_SCHEMA


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _plain_srt(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text("\n\n".join(
        f"{index}\n{start} --> {end}\n{text}"
        for index, (start, end, text) in enumerate(rows, 1)
    ) + "\n", encoding="utf-8")


def _bundle(tmp_path: Path) -> dict[str, object]:
    media = tmp_path / "recut.mp4"; media.write_bytes(b"same-media")
    old_plain, new_plain, old_speaker = (tmp_path / "old.srt", tmp_path / "new.srt", tmp_path / "old.speaker.srt")
    plain = [Cue(1, "00:00:00,000", "00:00:01,000", "李豆沙", "旧文本", "old"), Cue(2, "00:00:01,000", "00:00:02,000", "李豆沙", "不变", "old")]
    _plain_srt(old_plain, [(cue.start, cue.end, cue.text) for cue in plain])
    _plain_srt(new_plain, [(plain[0].start, plain[0].end, plain[0].text), (plain[1].start, plain[1].end, "新文本")])
    write_srt(plain, old_speaker)
    diagnostic = tmp_path / "diagnostic.srt"; diagnostic.write_bytes(old_plain.read_bytes())
    ledger = tmp_path / "ledger.json"
    authority = {"kind": "IVAN_OPERATOR", "evidence_ref": "test sealed operator ruling"}
    ledger_document = {"schema_version": "operator-reviewed-subtitle-decisions.v3", "candidate_id":"cid", "report_scope":"EXHAUSTIVE", "pipeline_srt_sha256": _sha(old_plain), "operator_authority": authority, "cue_decisions":[
        {"cue":1,"disposition":"OPERATOR_UNCHANGED_FREEZE"},
        {"cue":2,"disposition":"OPERATOR_EXACT_TEXT","decision_authority":"LEDGER_OPERATOR_AUTHORITY","release_text":"新文本"},
    ]}
    _write(ledger, ledger_document)
    truth_diff = tmp_path / "truth-diff.json"
    _write(truth_diff, {"schema_version": "operator-reviewed-subtitle-truth-diff.v2", "candidate_id": "cid", "pipeline_srt_sha256": _sha(old_plain), "release_truth_srt_sha256": _sha(new_plain), "decision_ledger_sha256": _sha(ledger), "rows": [
        {"cue": 1, "source_index": "1", "start_ms": 0, "end_ms": 1000, "pipeline_text": "旧文本", "disposition": "OPERATOR_UNCHANGED_FREEZE", "release_cue_index": 1, "release_truth_text": "旧文本"},
        {"cue": 2, "source_index": "2", "start_ms": 1000, "end_ms": 2000, "pipeline_text": "不变", "disposition": "OPERATOR_EXACT_TEXT", "release_cue_index": 2, "release_truth_text": "新文本", "decision_authority": authority},
    ]})
    manifest = tmp_path / "old.speaker.json"
    decisions = [{"source_index":i, "start":cue.start, "end":cue.end, "speaker":"李豆沙", "text":cue.text, "decision_source":"old", "authority":None, "note":None, "speaker_detail":None, "layer":0, "placement":"main"} for i,cue in enumerate(plain,1)]
    _write(manifest, {"schema_version":SPEAKER_FINALIZATION_SCHEMA,"status":"READY","production_ready":True,"text_final_srt":str(old_plain),"text_final_srt_sha256":_sha(old_plain).removeprefix("sha256:"),"output_review_srt":str(old_speaker),"output_review_srt_sha256":_sha(old_speaker).removeprefix("sha256:"),"source_media_sha256":_sha(media),"source_cue_count":2,"output_cue_count":2,"final_decisions":decisions})
    record = {"speaker_mode":"auto","speaker_finalization":json.loads(manifest.read_text()),"speaker_finalization_manifest_sha256":_sha(manifest)}
    return locals()


def _call(b: dict[str, object], tmp_path: Path) -> dict[str, object]:
    return materialize_text_only_speaker_successor(candidate_id="cid", old_record=b["record"], old_record_sha256="sha256:"+"1"*64, old_manifest_path=b["manifest"], old_manifest_sha256=_sha(b["manifest"]), old_diagnostic_path=b["diagnostic"], old_diagnostic_sha256=_sha(b["diagnostic"]), reviewed_baseline_path=b["new_plain"], reviewed_baseline_sha256=_sha(b["new_plain"]), ledger_path=b["ledger"], ledger_sha256=_sha(b["ledger"]), truth_diff_path=b["truth_diff"], truth_diff_sha256=_sha(b["truth_diff"]), new_plain_srt=b["new_plain"], new_media=b["media"], expected_media_sha256=_sha(b["media"]), output_srt=tmp_path/"new.speaker.srt", output_ass=tmp_path/"new.speaker.ass", output_manifest=tmp_path/"new.speaker.json")


def _stamp(ms: int) -> str:
    seconds, millis = divmod(ms, 1_000)
    minutes, seconds = divmod(seconds, 60)
    return f"00:{minutes:02d}:{seconds:02d},{millis:03d}"


def _canonical_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _c4_delivery_receipt(b: dict[str, object], tmp_path: Path) -> tuple[Path, Path]:
    """Build the C4-shaped sealed 24 -> 20 -> 17 delivery fixture."""

    release = parse_srt(b["new_plain"])
    retained = [(index, cue) for index, cue in enumerate(release, 1) if _srt(cue.start) >= 3_000]
    delivery = tmp_path / "delivery.srt"
    _plain_srt(delivery, [
        (_stamp(_srt(cue.start) - 3_000), _stamp(_srt(cue.end) - 3_000), cue.text)
        for _release_index, cue in retained
    ])
    b["record"]["boundary_audit"] = {"final_start_ms": 3_000, "final_end_ms": 24_000}
    dropped = {8, 12, 13, 14}
    rows, release_index, delivery_index = [], 0, 0
    for old_index in range(1, 25):
        if old_index in dropped:
            rows.append({"old_source_index": old_index, "release_cue_index": None, "delivery_cue_index": None, "disposition": "OPERATOR_DROP"})
            continue
        release_index += 1
        if old_index <= 3:
            rows.append({"old_source_index": old_index, "release_cue_index": release_index, "delivery_cue_index": None, "disposition": "OUTSIDE_FINAL_DELIVERY"})
            continue
        delivery_index += 1
        cue = release[release_index - 1]
        start, end = _srt(cue.start), _srt(cue.end)
        rows.append({"old_source_index": old_index, "release_cue_index": release_index, "delivery_cue_index": delivery_index, "disposition": "RETAINED_FINAL_DELIVERY", "release_start_ms": start, "release_end_ms": end, "delivery_start_ms": start - 3_000, "delivery_end_ms": end - 3_000})
    receipt = tmp_path / "projection.json"
    _write(receipt, {
        "schema_version": "reviewed-baseline-full-release-delivery-projection.v1",
        "candidate_id": "cid", "record_sha256": "sha256:" + "1" * 64,
        "record_boundary_sha256": _canonical_sha(b["record"]["boundary_audit"]),
        "padded_source_interval": {"start_ms": 0, "end_ms": 24_000},
        "final_delivery_boundary": {"start_ms": 3_000, "end_ms": 24_000},
        "pipeline_diagnostic_sha256": _sha(b["diagnostic"]),
        "full_release_srt_sha256": _sha(b["new_plain"]),
        "operator_ledger_sha256": _sha(b["ledger"]),
        "operator_truth_diff_sha256": _sha(b["truth_diff"]),
        "staged_srt_sha256": _sha(delivery), "staged_media_sha256": _sha(b["media"]),
        "old_diagnostic_cue_count": 24, "full_release_cue_count": 20,
        "final_delivery_cue_count": 17, "rows": rows,
    })
    return delivery, receipt


def _srt(value: str) -> int:
    hh, mm, rest = value.split(":")
    ss, msec = rest.split(",")
    return ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(msec)


def _call_delivery(b: dict[str, object], tmp_path: Path, delivery: Path, receipt: Path, *, receipt_sha: str | None = None) -> dict[str, object]:
    return materialize_text_only_speaker_successor(
        candidate_id="cid", old_record=b["record"], old_record_sha256="sha256:" + "1" * 64,
        old_manifest_path=b["manifest"], old_manifest_sha256=_sha(b["manifest"]),
        old_diagnostic_path=b["diagnostic"], old_diagnostic_sha256=_sha(b["diagnostic"]),
        reviewed_baseline_path=b["new_plain"], reviewed_baseline_sha256=_sha(b["new_plain"]),
        ledger_path=b["ledger"], ledger_sha256=_sha(b["ledger"]),
        truth_diff_path=b["truth_diff"], truth_diff_sha256=_sha(b["truth_diff"]),
        delivery_projection_receipt_path=receipt, delivery_projection_receipt_sha256=receipt_sha or _sha(receipt),
        new_plain_srt=delivery, new_media=b["media"], expected_media_sha256=_sha(b["media"]),
        output_srt=tmp_path / "delivery.speaker.srt", output_ass=tmp_path / "delivery.speaker.ass",
        output_manifest=tmp_path / "delivery.speaker.json",
    )


def _production_delivery_wrapper(b: dict[str, object], receipt: Path):
    """Exercise the canonical replay wrapper's production kwargs shape."""

    b["record"]["speaker_finalization_manifest_path"] = str(b["manifest"])
    config = {
        "operator_truth_lanes": {
            "pipeline_diagnostic": {"path": str(b["diagnostic"]), "sha256": _sha(b["diagnostic"])},
            "decision_ledger": {"path": str(b["ledger"]), "sha256": _sha(b["ledger"])},
            "diff_receipt": {"path": str(b["truth_diff"]), "sha256": _sha(b["truth_diff"])},
            "release_truth": {"srt_sha256": _sha(b["new_plain"])},
        },
        "operator_text_full_ownership": {
            "speaker_authority": "NOT_CLAIMED_TEXT_ONLY",
            "decision_ledger_sha256": _sha(b["ledger"]),
            "pipeline_srt_sha256": _sha(b["diagnostic"]),
            "diagnostic_diff_sha256": _sha(b["truth_diff"]),
            "baseline_sha256": _sha(b["new_plain"]),
        },
    }

    def binding(path: Path, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(path=path, sha256=_sha(path))

    def generated_guess(**_kwargs: object) -> dict[str, object]:
        from src.autoslice.speaker_guess import SPEAKER_GUESS_STATUS
        return {"status": SPEAKER_GUESS_STATUS}

    fields = build_text_only_speaker_successor_fields(
        finalizer=None, original_speaker_finalizer=generated_guess,
        candidate_id="cid", record=b["record"], old_record_sha256="sha256:" + "1" * 64,
        baseline_config=config, baseline_manifest_parent=Path(b["tmp_path"]),
        reviewed_baseline_path=b["new_plain"], reviewed_baseline_sha256=_sha(b["new_plain"]),
        expected_media_sha256=_sha(b["media"]),
        delivery_projection_receipt_path=receipt,
        delivery_projection_receipt_sha256=_sha(receipt),
        regular_binding=binding, replay_error=RuntimeError, error_factory=RuntimeError,
    )
    return fields["run_speaker_finalization"]


def _refresh_record_manifest(b: dict[str, object]) -> None:
    manifest = json.loads(b["manifest"].read_text())
    b["record"]["speaker_finalization"] = manifest
    b["record"]["speaker_finalization_manifest_sha256"] = _sha(b["manifest"])


def _drop_bundle(tmp_path: Path, *, drops: set[int] | None = None) -> dict[str, object]:
    b = _bundle(tmp_path)
    old_rows = [(f"00:00:{index:02d},000", f"00:00:{index:02d},900", f"旧{index}") for index in range(24)]
    old_rows = [(start.replace(":24,", ":23,"), end.replace(":24,", ":23,"), text) for start, end, text in old_rows]
    _plain_srt(b["old_plain"], old_rows)
    b["diagnostic"].write_bytes(b["old_plain"].read_bytes())
    drops = drops or {8, 12, 13, 14}
    release_rows = [(start, end, "精确三" if index == 3 else text) for index, (start, end, text) in enumerate(old_rows, 1) if index not in drops]
    _plain_srt(b["new_plain"], release_rows)
    old_cues = [Cue(index, start, end, "李豆沙", text, "old") for index, (start, end, text) in enumerate(old_rows, 1)]
    write_srt(old_cues, b["old_speaker"])
    decisions = [{"source_index": index, "start": cue.start, "end": cue.end, "speaker": "李豆沙", "text": cue.text, "decision_source": "old", "authority": None, "note": None, "speaker_detail": None, "layer": 0, "placement": "main"} for index, cue in enumerate(old_cues, 1)]
    manifest = json.loads(b["manifest"].read_text())
    manifest.update({"text_final_srt": str(b["old_plain"]), "text_final_srt_sha256": _sha(b["old_plain"]).removeprefix("sha256:"), "output_review_srt": str(b["old_speaker"]), "output_review_srt_sha256": _sha(b["old_speaker"]).removeprefix("sha256:"), "source_cue_count": 24, "output_cue_count": 24, "final_decisions": decisions})
    _write(b["manifest"], manifest); _refresh_record_manifest(b)
    authority = {"kind": "IVAN_OPERATOR", "evidence_ref": "test sealed drop ruling"}
    ledger_rows, diff_rows, release_index = [], [], 0
    for index, (start, end, text) in enumerate(old_rows, 1):
        base = {"cue": index, "source_index": str(index), "start_ms": (index - 1) * 1000, "end_ms": (index - 1) * 1000 + 900, "pipeline_text": text}
        if index in drops:
            ledger_rows.append({"cue": index, "disposition": "OPERATOR_DROP", "decision_authority": "LEDGER_OPERATOR_AUTHORITY", "drop_reason": "operator drop"})
            diff_rows.append({**base, "disposition": "OPERATOR_DROP", "release_cue_index": None, "release_truth_text": None, "decision_authority": authority, "drop_reason": "operator drop"})
        else:
            release_index += 1
            disposition = "OPERATOR_EXACT_TEXT" if index == 3 else "OPERATOR_UNCHANGED_FREEZE"
            ledger_row = {"cue": index, "disposition": disposition}
            if index == 3:
                ledger_row.update({"release_text": "精确三", "decision_authority": "LEDGER_OPERATOR_AUTHORITY"})
            ledger_rows.append(ledger_row)
            diff_row = {**base, "disposition": disposition, "release_cue_index": release_index, "release_truth_text": "精确三" if index == 3 else text}
            if index == 3:
                diff_row["decision_authority"] = authority
            diff_rows.append(diff_row)
    _write(b["ledger"], {"schema_version": "operator-reviewed-subtitle-decisions.v3", "candidate_id": "cid", "report_scope": "EXHAUSTIVE", "pipeline_srt_sha256": _sha(b["old_plain"]), "operator_authority": authority, "cue_decisions": ledger_rows})
    _write(b["truth_diff"], {"schema_version": "operator-reviewed-subtitle-truth-diff.v2", "candidate_id": "cid", "pipeline_srt_sha256": _sha(b["old_plain"]), "release_truth_srt_sha256": _sha(b["new_plain"]), "decision_ledger_sha256": _sha(b["ledger"]), "rows": diff_rows})
    return b


def test_successor_only_rebinds_ledger_named_text_and_keeps_labels(tmp_path: Path) -> None:
    b = _bundle(tmp_path)
    result = _call(b, tmp_path)
    assert result["status"] == "READY"
    assert result["reviewed_baseline_text_only_successor"]["speaker_label_mutation_authorized"] is False
    assert result["final_decisions"][0]["text"] == "旧文本"
    assert result["final_decisions"][1]["text"] == "新文本"
    assert "[李豆沙] 新文本" in (tmp_path/"new.speaker.srt").read_text()
    assert result["source_media_sha256"] == _sha(b["media"]).removeprefix("sha256:")
    successor_record = {"speaker_mode":"auto", "speaker_review_srt_path":str(tmp_path/"new.speaker.srt"), "speaker_finalization_manifest_path":str(tmp_path/"new.speaker.json"), "speaker_finalization_manifest_sha256":_sha(tmp_path/"new.speaker.json"), "speaker_finalization":json.loads((tmp_path/"new.speaker.json").read_text()), "artifact_hashes":{"speaker_review_srt_sha256":_sha(tmp_path/"new.speaker.srt"), "subtitle_sha256":_sha(b["new_plain"])}}
    new_cues = [
        SourceCue("1", 0, 1_000, "旧文本"),
        SourceCue("2", 1_000, 2_000, "新文本"),
    ]
    _plain, evidence = build_addressee_evidence(successor_record, new_cues)
    assert evidence.state is SpeakerEvidenceState.PRESENT_VALID
    assert "2 [李豆沙] 新文本" in evidence.transcript


def test_successor_uses_sealed_diagnostic_when_historical_plain_path_has_drifted(tmp_path: Path) -> None:
    b = _bundle(tmp_path)
    # The manifest keeps this absolute historical path and its original hash,
    # but it is no longer eligible as evidence.
    b["old_plain"].write_text("1\n00:00:00,000 --> 00:00:01,000\n漂移", encoding="utf-8")
    result = _call(b, tmp_path)
    assert result["final_decisions"][1]["text"] == "新文本"
    assert result["reviewed_baseline_text_only_successor"]["old_diagnostic_sha256"] == _sha(b["diagnostic"])


def test_successor_materializes_sealed_24_to_20_drop_grid(tmp_path: Path) -> None:
    b = _drop_bundle(tmp_path)
    # C4's old manifest can name a stale private plain-SRT path. The 24-cue
    # sealed diagnostic remains the only permitted old grid.
    b["old_plain"].write_text("stale private artifact", encoding="utf-8")
    result = _call(b, tmp_path)
    sealed = result["reviewed_baseline_text_only_successor"]
    assert len(result["final_decisions"]) == 20
    assert sealed["dropped_old_source_indices"] == [8, 12, 13, 14]
    assert sealed["old_to_release_index_map"][7] == {"old_source_index": 9, "release_source_index": 8}
    assert result["final_decisions"][2]["text"] == "精确三"
    assert [row["source_index"] for row in result["final_decisions"]] == list(range(1, 21))
    record = {"speaker_mode": "auto", "speaker_review_srt_path": str(tmp_path / "new.speaker.srt"), "speaker_finalization_manifest_path": str(tmp_path / "new.speaker.json"), "speaker_finalization_manifest_sha256": _sha(tmp_path / "new.speaker.json"), "speaker_finalization": result, "artifact_hashes": {"speaker_review_srt_sha256": _sha(tmp_path / "new.speaker.srt"), "subtitle_sha256": _sha(b["new_plain"])}}
    # The release grid, not the diagnostic grid, is the addressee input.
    retained = [old_index for old_index in range(1, 25) if old_index not in {8, 12, 13, 14}]
    cues = [
        SourceCue(str(release_index), (old_index - 1) * 1000, (old_index - 1) * 1000 + 900,
                  "精确三" if old_index == 3 else f"旧{old_index - 1}")
        for release_index, old_index in enumerate(retained, 1)
    ]
    _plain, evidence = build_addressee_evidence(record, cues)
    assert evidence.state is SpeakerEvidenceState.PRESENT_VALID


def test_successor_materializes_c4_full_release_to_final_delivery_projection(tmp_path: Path) -> None:
    b = _drop_bundle(tmp_path)
    delivery, receipt = _c4_delivery_receipt(b, tmp_path)
    result = _call_delivery(b, tmp_path, delivery, receipt)
    sealed = result["reviewed_baseline_text_only_successor"]
    assert sealed["input_grid_mode"] == "FINAL_DELIVERY_PROJECTION"
    assert len(result["final_decisions"]) == 17
    assert sealed["old_to_release_index_map"][0] == {"old_source_index": 1, "release_source_index": 1}
    assert sealed["release_to_delivery_index_map"][0] == {"release_source_index": 4, "delivery_source_index": 1}
    assert sealed["speaker_label_mutation_authorized"] is False
    assert result["final_decisions"][0]["source_index"] == 1
    assert result["final_decisions"][0]["text"] == "旧3"
    record = {
        "speaker_mode": "auto", "speaker_review_srt_path": str(tmp_path / "delivery.speaker.srt"),
        "speaker_finalization_manifest_path": str(tmp_path / "delivery.speaker.json"),
        "speaker_finalization_manifest_sha256": _sha(tmp_path / "delivery.speaker.json"),
        "speaker_finalization": result,
        "artifact_hashes": {"speaker_review_srt_sha256": _sha(tmp_path / "delivery.speaker.srt"), "subtitle_sha256": _sha(delivery)},
    }
    delivery_cues = [
        SourceCue(str(index), _srt(cue.start), _srt(cue.end), cue.text)
        for index, cue in enumerate(parse_srt(delivery), 1)
    ]
    _plain, evidence = build_addressee_evidence(record, delivery_cues)
    assert evidence.state is SpeakerEvidenceState.PRESENT_VALID


def test_production_wrapper_uses_17_cue_delivery_kwargs_not_full_release(
    tmp_path: Path,
) -> None:
    b = _drop_bundle(tmp_path)
    delivery, receipt = _c4_delivery_receipt(b, tmp_path)
    runner = _production_delivery_wrapper(b, receipt)
    result = runner(
        text_srt_path=str(delivery), media_path=str(b["media"]),
        output_srt_path=str(tmp_path / "wrapper.speaker.srt"),
        output_ass_path=str(tmp_path / "wrapper.speaker.ass"),
        output_manifest_path=str(tmp_path / "wrapper.speaker.json"),
    )
    assert result["output_cue_count"] == 17
    assert len(parse_srt(tmp_path / "wrapper.speaker.srt")) == 17
    final_plain = [
        SourceCue(str(index), _srt(cue.start), _srt(cue.end), cue.text)
        for index, cue in enumerate(parse_srt(delivery), 1)
    ]
    record = {
        "speaker_mode": "auto", "speaker_review_srt_path": str(tmp_path / "wrapper.speaker.srt"),
        "speaker_finalization_manifest_path": str(tmp_path / "wrapper.speaker.json"),
        "speaker_finalization_manifest_sha256": _sha(tmp_path / "wrapper.speaker.json"),
        "speaker_finalization": result,
        "artifact_hashes": {"speaker_review_srt_sha256": _sha(tmp_path / "wrapper.speaker.srt"), "subtitle_sha256": _sha(delivery)},
    }
    _plain, evidence = build_addressee_evidence(record, final_plain)
    assert evidence.state is SpeakerEvidenceState.PRESENT_VALID
    with pytest.raises(RuntimeError, match="SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"):
        runner(
            text_srt_path=str(b["new_plain"]), media_path=str(b["media"]),
            output_srt_path=str(tmp_path / "wrong.speaker.srt"),
            output_ass_path=str(tmp_path / "wrong.speaker.ass"),
            output_manifest_path=str(tmp_path / "wrong.speaker.json"),
        )


@pytest.mark.parametrize("mutation", ["receipt_row", "receipt_hash", "full_as_delivery"])
def test_successor_refuses_c4_projection_receipt_or_grid_drift(
    tmp_path: Path, mutation: str,
) -> None:
    b = _drop_bundle(tmp_path)
    delivery, receipt = _c4_delivery_receipt(b, tmp_path)
    if mutation == "receipt_row":
        document = json.loads(receipt.read_text())
        document["rows"][3]["delivery_start_ms"] += 1
        _write(receipt, document)
        with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="DELIVERY_PROJECTION_MAP_INVALID"):
            _call_delivery(b, tmp_path, delivery, receipt)
    elif mutation == "receipt_hash":
        stale_sha = _sha(receipt)
        receipt.write_text(receipt.read_text() + " ", encoding="utf-8")
        with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="DELIVERY_PROJECTION_RECEIPT_BINDING"):
            _call_delivery(b, tmp_path, delivery, receipt, receipt_sha=stale_sha)
    else:
        # A 20-cue full release must never carry a 17-cue projection receipt.
        with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="DELIVERY_PROJECTION_UNEXPECTED"):
            _call_delivery(b, tmp_path, b["new_plain"], receipt)


@pytest.mark.parametrize(("mutation", "reason"), [
    ("record_boundary", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("delivery_text", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("delivery_timing", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("declared_baseline", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("declared_diff", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("staged_media", "DELIVERY_PROJECTION_RECEIPT_INVALID"),
    ("padded_bool", "DELIVERY_PROJECTION_RECORD_BOUNDARY"),
    ("straddler", "DELIVERY_PROJECTION_STRADDLER"),
])
def test_successor_refuses_projection_binding_and_grid_negatives(
    tmp_path: Path, mutation: str, reason: str,
) -> None:
    b = _drop_bundle(tmp_path)
    delivery, receipt = _c4_delivery_receipt(b, tmp_path)
    document = json.loads(receipt.read_text())
    if mutation == "record_boundary":
        b["record"]["boundary_audit"]["final_end_ms"] -= 1
    elif mutation == "delivery_text":
        delivery.write_text(delivery.read_text(encoding="utf-8").replace("旧3", "漂移", 1), encoding="utf-8")
    elif mutation == "delivery_timing":
        delivery.write_text(delivery.read_text(encoding="utf-8").replace("00:00:00,000", "00:00:00,001", 1), encoding="utf-8")
    elif mutation == "declared_baseline":
        document["full_release_srt_sha256"] = "sha256:" + "f" * 64
        _write(receipt, document)
    elif mutation == "declared_diff":
        document["operator_truth_diff_sha256"] = "sha256:" + "f" * 64
        _write(receipt, document)
    elif mutation == "staged_media":
        document["staged_media_sha256"] = "sha256:" + "f" * 64
        _write(receipt, document)
    elif mutation == "padded_bool":
        document["padded_source_interval"]["start_ms"] = False
        _write(receipt, document)
    else:
        b["record"]["boundary_audit"] = {"final_start_ms": 3_500, "final_end_ms": 24_000}
        document["record_boundary_sha256"] = _canonical_sha(b["record"]["boundary_audit"])
        document["final_delivery_boundary"] = {"start_ms": 3_500, "end_ms": 24_000}
        _write(receipt, document)
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match=reason):
        _call_delivery(b, tmp_path, delivery, receipt)


def test_successor_rejects_text_change_not_named_by_ledger(tmp_path: Path) -> None:
    b = _bundle(tmp_path)
    b["new_plain"].write_text(b["new_plain"].read_text().replace("旧文本", "越权"), encoding="utf-8")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="SEALED_TRUTH_CONTRACT_INVALID"):
        _call(b, tmp_path)


@pytest.mark.parametrize(("mutate", "reason"), [
    ("diagnostic", "OLD_DIAGNOSTIC_MANIFEST_BINDING"),
    ("missing_diff", "TRUTH_DIFF_INVALID"),
    ("unlisted_drop", "SEALED_TRUTH_CONTRACT_INVALID"),
    ("reorder", "SEALED_TRUTH_CONTRACT_INVALID"),
])
def test_successor_rejects_unsealed_drop_mapping_drift(
    tmp_path: Path, mutate: str, reason: str,
) -> None:
    b = _drop_bundle(tmp_path)
    if mutate == "diagnostic":
        b["diagnostic"].write_text("not the old grid", encoding="utf-8")
    elif mutate == "missing_diff":
        b["truth_diff"].write_text("not json", encoding="utf-8")
    elif mutate == "unlisted_drop":
        ledger = json.loads(b["ledger"].read_text())
        ledger["cue_decisions"][0] = {"cue": 1, "disposition": "OPERATOR_DROP", "decision_authority": "LEDGER_OPERATOR_AUTHORITY", "drop_reason": "forged"}
        _write(b["ledger"], ledger)
    else:
        release = b["new_plain"].read_text(encoding="utf-8")
        b["new_plain"].write_text(release.replace("旧0", "__swap__", 1).replace("旧1", "旧0", 1).replace("__swap__", "旧1", 1), encoding="utf-8")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match=reason):
        _call(b, tmp_path)


def test_c5_runtime_authority_cannot_materialize_revoked_clamp(tmp_path: Path, monkeypatch) -> None:
    """The historical 24->21->17 projection is retained only as bad evidence."""
    root = Path(__file__).parents[1]
    assets = root / "assets" / "lidousha" / "reviewed_subtitle_baselines"
    diagnostic, reviewed = assets / "auto_113028_1271_1328.pipeline-diagnostic.srt", assets / "auto_113028_1271_1328.reviewed.srt"
    ledger, truth = assets / "auto_113028_1271_1328.operator-decisions.v3.json", assets / "auto_113028_1271_1328.operator-truth-diff.v2.json"
    old_speaker, manifest, media = tmp_path / "old.speaker.srt", tmp_path / "old.json", tmp_path / "recut.mp4"
    old_cues = parse_srt(diagnostic)
    write_srt([Cue(i, cue.start, cue.end, "李豆沙", cue.text, "historical") for i, cue in enumerate(old_cues, 1)], old_speaker)
    media.write_bytes(b"fixture media")
    decisions = [{"source_index": i, "start": cue.start, "end": cue.end, "speaker": "李豆沙", "text": cue.text, "decision_source": "historical", "authority": None, "note": None, "speaker_detail": None, "layer": 0, "placement": "main"} for i, cue in enumerate(old_cues, 1)]
    _write(manifest, {"schema_version": SPEAKER_FINALIZATION_SCHEMA, "status": "READY", "production_ready": True, "text_final_srt": str(diagnostic), "text_final_srt_sha256": _sha(diagnostic).removeprefix("sha256:"), "output_review_srt": str(old_speaker), "output_review_srt_sha256": _sha(old_speaker).removeprefix("sha256:"), "source_media_sha256": str(c5.BOUNDARY["media_sha256"]), "source_cue_count": 24, "output_cue_count": 24, "final_decisions": decisions})
    record = {"speaker_mode": "auto", "speaker_finalization": json.loads(manifest.read_text()), "speaker_finalization_manifest_sha256": _sha(manifest), "boundary_audit": {"final_start_ms": 9750, "final_end_ms": 67524}}
    release = parse_srt(reviewed); delivery = tmp_path / "delivery.srt"
    retained = [(index, cue) for index, cue in enumerate(release, 1) if _srt(cue.end) > 9750]
    _plain_srt(delivery, [("00:00:00,000", "00:00:00,330", retained[0][1].text)] + [(_stamp(_srt(cue.start) - 9750), _stamp(_srt(cue.end) - 9750), cue.text) for _index, cue in retained[1:]])
    rows=[]; release_index=delivery_index=0
    for old_index, old in enumerate(old_cues, 1):
        if old_index in {13,14,15}:
            rows.append({"old_source_index":old_index,"release_cue_index":None,"delivery_cue_index":None,"disposition":"OPERATOR_DROP"}); continue
        release_index += 1; start,end=_srt(old.start),_srt(old.end)
        if end <= 9750:
            rows.append({"old_source_index":old_index,"release_cue_index":release_index,"delivery_cue_index":None,"disposition":"OUTSIDE_FINAL_DELIVERY"}); continue
        delivery_index += 1; ds,de=(0,330) if old_index == 5 else (start-9750,end-9750)
        rows.append({"old_source_index":old_index,"release_cue_index":release_index,"delivery_cue_index":delivery_index,"disposition":"RETAINED_FINAL_DELIVERY_START_CLAMP" if old_index == 5 else "RETAINED_FINAL_DELIVERY","release_start_ms":start,"release_end_ms":end,"delivery_start_ms":ds,"delivery_end_ms":de})
    receipt=tmp_path/"receipt.json"; old_record_sha="sha256:"+"1"*64
    _write(receipt,{"schema_version":"reviewed-baseline-full-release-delivery-projection.v1","candidate_id":c5.CANDIDATE_ID,"record_sha256":old_record_sha,"record_boundary_sha256":_canonical_sha(record["boundary_audit"]),"padded_source_interval":{"start_ms":0,"end_ms":67524},"final_delivery_boundary":{"start_ms":9750,"end_ms":67524},"pipeline_diagnostic_sha256":_sha(diagnostic),"full_release_srt_sha256":_sha(reviewed),"operator_ledger_sha256":_sha(ledger),"operator_truth_diff_sha256":_sha(truth),"staged_srt_sha256":_sha(delivery),"staged_media_sha256":str(c5.BOUNDARY["media_sha256"]),"old_diagnostic_cue_count":24,"full_release_cue_count":21,"final_delivery_cue_count":17,"rows":rows})
    runtime = tmp_path / "runtime"; authority = runtime / ".private-c5-start-clamp-authority"; authority.mkdir(parents=True, mode=0o700)
    proposal_path, acceptance_path = c5.runtime_authority_paths(runtime)
    proposal_path.write_bytes((root / "docs/reviews/auto_113028_1271_1328-c5-start-clamp-proposal.v1.json").read_bytes()); proposal, proposal_sha = c5.load_proposal(proposal_path)
    acceptance = c5.build_accepted_authority(proposal_path=proposal_path, proposal_file_sha256=proposal_sha, proposal_self_sha256=str(proposal["self_sha256"]), expectations=c5.C5_ACCEPTANCE_EXPECTATIONS)
    c5.materialize_accepted_authority(acceptance_path, proposal_path=proposal_path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=c5.C5_ACCEPTANCE_EXPECTATIONS)
    original_sha = successor_module.sha256_file
    monkeypatch.setattr(successor_module, "sha256_file", lambda path: str(c5.BOUNDARY["media_sha256"]).removeprefix("sha256:") if Path(path) == media else original_sha(path))
    kwargs = dict(candidate_id=c5.CANDIDATE_ID, old_record=record, old_record_sha256=old_record_sha, old_manifest_path=manifest, old_manifest_sha256=_sha(manifest), old_diagnostic_path=diagnostic, old_diagnostic_sha256=_sha(diagnostic), reviewed_baseline_path=reviewed, reviewed_baseline_sha256=_sha(reviewed), ledger_path=ledger, ledger_sha256=_sha(ledger), truth_diff_path=truth, truth_diff_sha256=_sha(truth), delivery_projection_receipt_path=receipt, delivery_projection_receipt_sha256=_sha(receipt), c5_start_clamp_proposal_path=proposal_path, c5_start_clamp_acceptance_path=acceptance_path, recording_date="2026-08-14", new_plain_srt=delivery, new_media=media, expected_media_sha256=str(c5.BOUNDARY["media_sha256"]), output_srt=tmp_path/"out.srt", output_ass=tmp_path/"out.ass", output_manifest=tmp_path/"out.json")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match="DELIVERY_PROJECTION_STRADDLER"):
        materialize_text_only_speaker_successor(**kwargs)
    assert not (tmp_path / "out.srt").exists()
    assert not (tmp_path / "out.ass").exists()
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(("mutate", "reason"), [
    ("label", "SPEAKER_LABEL_OR_DECISION_DRIFT"),
    ("text", "SPEAKER_LABEL_OR_DECISION_DRIFT"),
    ("timing", "CUE_INDEX_OR_TIMING_DRIFT"),
    ("metadata", "SPEAKER_LABEL_OR_DECISION_DRIFT"),
])
def test_successor_rejects_dropped_old_speaker_or_decision_drift(
    tmp_path: Path, mutate: str, reason: str,
) -> None:
    b = _drop_bundle(tmp_path)
    if mutate in {"label", "text", "timing"}:
        speaker = b["old_speaker"].read_text(encoding="utf-8")
        if mutate == "label":
            speaker = speaker.replace("[李豆沙] 旧7", "[连线] 旧7")
        elif mutate == "text":
            speaker = speaker.replace("[李豆沙] 旧7", "[李豆沙] 伪造")
        else:
            speaker = speaker.replace("00:00:07,000 --> 00:00:07,900", "00:00:07,001 --> 00:00:07,900")
        b["old_speaker"].write_text(speaker, encoding="utf-8")
        manifest = json.loads(b["manifest"].read_text())
        manifest["output_review_srt_sha256"] = _sha(b["old_speaker"]).removeprefix("sha256:")
        _write(b["manifest"], manifest)
    else:
        manifest = json.loads(b["manifest"].read_text())
        manifest["final_decisions"][7]["layer"] = 1
        _write(b["manifest"], manifest)
    _refresh_record_manifest(b)
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match=reason):
        _call(b, tmp_path)


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("text_final_srt_sha256", "f" * 64, "OLD_DIAGNOSTIC_MANIFEST_BINDING"),
    ("source_media_sha256", "e" * 64, "MEDIA_BINDING"),
])
def test_c5_shaped_24_to_21_unsealed_manifest_binding_stays_closed(
    tmp_path: Path, field: str, value: str, reason: str,
) -> None:
    b = _drop_bundle(tmp_path, drops={8, 12, 13})
    manifest = json.loads(b["manifest"].read_text())
    manifest[field] = value
    _write(b["manifest"], manifest)
    _refresh_record_manifest(b)
    assert len(b["ledger"].read_text(encoding="utf-8").split('"disposition": "OPERATOR_DROP"')) - 1 == 3
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match=reason):
        _call(b, tmp_path)


@pytest.mark.parametrize(("mutate", "reason"), [
    ("label", "SPEAKER_LABEL_OR_DECISION_DRIFT"), ("labelled_text", "SPEAKER_LABEL_OR_DECISION_DRIFT"),
    ("decision_timing", "SPEAKER_LABEL_OR_DECISION_DRIFT"), ("media", "MEDIA_BINDING"),
])
def test_successor_rejects_speaker_or_media_drift(tmp_path: Path, mutate: str, reason: str) -> None:
    b = _bundle(tmp_path)
    if mutate in {"label", "labelled_text"}:
        text = b["old_speaker"].read_text()
        b["old_speaker"].write_text(text.replace("[李豆沙] 旧文本", "[连线] 旧文本" if mutate == "label" else "[李豆沙] 伪造"), encoding="utf-8")
        manifest = json.loads(b["manifest"].read_text()); manifest["output_review_srt_sha256"] = _sha(b["old_speaker"]).removeprefix("sha256:"); _write(b["manifest"], manifest); _refresh_record_manifest(b)
    elif mutate == "decision_timing":
        manifest = json.loads(b["manifest"].read_text()); manifest["final_decisions"][0]["end"] = "00:00:01,001"; _write(b["manifest"], manifest); _refresh_record_manifest(b)
    else:
        b["media"].write_bytes(b"drift")
    with pytest.raises(ReviewedTextOnlySpeakerSuccessorError, match=reason):
        _call(b, tmp_path)
