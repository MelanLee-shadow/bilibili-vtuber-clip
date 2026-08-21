"""Sealed terminal subtitle projection for the Qixi corrected-package lane."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
from src.autoslice.redelivery_subtitle_baseline import apply_redelivery_subtitle_baseline
from src.autoslice.repository_asset_authority import require_repository_asset_authority


TERMINAL_PROJECTION_RELATIVE_PATH = Path(
    "assets/lidousha/qixi_terminal_subtitle_projection/"
    "auto_113022_354_496.terminal-projection.v1.json"
)
SCHEMA = "qixi-reviewed-terminal-subtitle-projection.v1"
_SHA_LEN = 64


class TerminalProjectionError(ValueError):
    """The sealed terminal projection cannot be replayed safely."""


@dataclass(frozen=True)
class _ProjectionAssets:
    projection: dict[str, Any]
    current_cues: list[SrtCue]
    current_ledger: dict[str, Any]
    preimage_cues: list[SrtCue]
    terminal_cues: list[SrtCue]
    terminal_bytes: bytes


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _normal_sha(value: object, *, label: str) -> str:
    result = str(value or "").removeprefix("sha256:")
    if len(result) != _SHA_LEN or any(char not in "0123456789abcdef" for char in result):
        raise TerminalProjectionError(f"{label} must be a sha256")
    return "sha256:" + result


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise TerminalProjectionError(f"{label} path is missing")
    path = Path(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise TerminalProjectionError(f"{label} path is unsafe")
    return path


def _contained_regular(root: Path, relative: object, *, label: str) -> Path:
    rel = _safe_relative(relative, label=label)
    cursor = root
    for part in rel.parts:
        cursor /= part
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise TerminalProjectionError(f"{label} is unavailable: {rel}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise TerminalProjectionError(f"{label} contains a symlink: {rel}")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise TerminalProjectionError(f"{label} escapes its source root: {rel}") from exc
    if not resolved.is_file():
        raise TerminalProjectionError(f"{label} is not a regular file: {rel}")
    return resolved


def _sealed_repository_bytes(repo_root: Path, relative: Path, *, label: str) -> tuple[Path, bytes]:
    path = _contained_regular(repo_root, relative.as_posix(), label=label)
    try:
        payload = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=relative, observed_bytes=payload
        )
    except (OSError, ValueError) as exc:
        raise TerminalProjectionError(f"{label} is not repository sealed") from exc
    return path, payload


def _srt_bytes(cues: list[SrtCue]) -> bytes:
    def clock(value: int) -> str:
        hours, remainder = divmod(value, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

    return "\n".join(
        f"{position}\n{clock(cue.start_ms)} --> {clock(cue.end_ms)}\n{cue.text}\n"
        for position, cue in enumerate(cues, start=1)
    ).encode("utf-8")


def _projection_sha_entry(value: Mapping[str, object], key: str, *, label: str) -> str:
    return _normal_sha(value.get(key), label=f"{label} {key}")


def load_projection(repo_root: Path) -> tuple[dict[str, Any], bytes]:
    _path, payload = _sealed_repository_bytes(
        repo_root, TERMINAL_PROJECTION_RELATIVE_PATH, label="Qixi terminal projection"
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalProjectionError("Qixi terminal projection is unreadable") from exc
    if not isinstance(value, Mapping):
        raise TerminalProjectionError("Qixi terminal projection must be an object")
    projection = dict(value)
    claimed = _normal_sha(projection.pop("authority_sha256", None), label="terminal projection")
    if claimed != _canonical_sha(projection):
        raise TerminalProjectionError("Qixi terminal projection hash drifts")
    projection["authority_sha256"] = claimed
    return projection, payload


def _sealed_json(
    repo_root: Path, path_value: object, expected_sha: object, *, label: str
) -> tuple[Path, dict[str, Any]]:
    path, payload = _sealed_repository_bytes(
        repo_root, _safe_relative(path_value, label=label), label=label
    )
    if _sha256(path) != _normal_sha(expected_sha, label=label):
        raise TerminalProjectionError(f"{label} hash drifts")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TerminalProjectionError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise TerminalProjectionError(f"{label} must be an object")
    return path, dict(value)


def _truth_lane(
    *, repo_root: Path, lane: Mapping[str, object], label: str, require_current_extras: bool
) -> tuple[list[SrtCue], dict[str, Any], dict[str, Any], dict[str, Any]]:
    required = {
        "registry_path", "registry_sha256", "srt_path", "srt_sha256", "cue_count",
        "pipeline_diagnostic_path", "pipeline_diagnostic_sha256",
        "operator_decision_ledger_path", "operator_decision_ledger_sha256",
        "operator_truth_diff_path", "operator_truth_diff_sha256",
        "operator_delivery_receipt_path", "operator_delivery_receipt_sha256",
    }
    if require_current_extras:
        required |= {
            "cue21_evidence_path", "cue21_evidence_sha256", "final_contract_path",
            "final_contract_sha256",
        }
    if set(lane) != required:
        raise TerminalProjectionError(f"Qixi {label} truth schema is invalid")
    registry_path, registry = _sealed_json(
        repo_root, lane["registry_path"], lane["registry_sha256"], label=f"Qixi {label} registry"
    )
    srt_path, srt_bytes = _sealed_repository_bytes(
        repo_root, _safe_relative(lane["srt_path"], label=f"Qixi {label} SRT"), label=f"Qixi {label} SRT"
    )
    if _sha256(srt_path) != _normal_sha(lane["srt_sha256"], label=f"Qixi {label} SRT"):
        raise TerminalProjectionError(f"Qixi {label} SRT hash drifts")
    try:
        cues = parse_srt_cues(srt_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise TerminalProjectionError(f"Qixi {label} SRT is invalid") from exc
    if isinstance(lane["cue_count"], bool) or lane["cue_count"] != len(cues):
        raise TerminalProjectionError(f"Qixi {label} cue count drifts")
    ledger_path, ledger = _sealed_json(
        repo_root, lane["operator_decision_ledger_path"], lane["operator_decision_ledger_sha256"],
        label=f"Qixi {label} ledger",
    )
    diff_path, diff = _sealed_json(
        repo_root, lane["operator_truth_diff_path"], lane["operator_truth_diff_sha256"],
        label=f"Qixi {label} truth diff",
    )
    _receipt_path, receipt = _sealed_json(
        repo_root, lane["operator_delivery_receipt_path"], lane["operator_delivery_receipt_sha256"],
        label=f"Qixi {label} delivery receipt",
    )
    pipeline_path, pipeline_bytes = _sealed_repository_bytes(
        repo_root,
        _safe_relative(lane["pipeline_diagnostic_path"], label=f"Qixi {label} pipeline diagnostic"),
        label=f"Qixi {label} pipeline diagnostic",
    )
    if _sha256(pipeline_path) != _normal_sha(
        lane["pipeline_diagnostic_sha256"], label=f"Qixi {label} pipeline diagnostic"
    ):
        raise TerminalProjectionError(f"Qixi {label} pipeline diagnostic hash drifts")
    _validate_truth_sidecars(
        label=label, registry=registry, registry_path=registry_path, srt_path=srt_path,
        srt_sha=_sha256(srt_path), cues=cues, ledger=ledger, ledger_path=ledger_path,
        diff=diff, diff_path=diff_path, receipt=receipt, pipeline_bytes=pipeline_bytes,
    )
    return cues, registry, ledger, diff


def _validate_truth_sidecars(
    *, label: str, registry: Mapping[str, object], registry_path: Path, srt_path: Path,
    srt_sha: str, cues: list[SrtCue], ledger: Mapping[str, object], ledger_path: Path,
    diff: Mapping[str, object], diff_path: Path, receipt: Mapping[str, object],
    pipeline_bytes: bytes | None,
) -> None:
    candidate_id = "auto_113022_354_496"
    if (
        registry.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or registry.get("registry_schema_version") != "candidate-reviewed-subtitle-baseline.v1"
        or registry.get("candidate_id") != candidate_id
        or _normal_sha(registry.get("sha256"), label=f"Qixi {label} registry SRT") != srt_sha
        or registry.get("path") != srt_path.name
    ):
        raise TerminalProjectionError(f"Qixi {label} registry does not bind its SRT")
    ownership = registry.get("operator_text_full_ownership")
    lanes = registry.get("operator_truth_lanes")
    release_lane = lanes.get("release_truth") if isinstance(lanes, Mapping) else None
    ledger_lane = lanes.get("decision_ledger") if isinstance(lanes, Mapping) else None
    diff_lane = lanes.get("diff_receipt") if isinstance(lanes, Mapping) else None
    if not isinstance(ownership, Mapping) or not isinstance(lanes, Mapping) or (
        ownership.get("baseline_sha256") != srt_sha.removeprefix("sha256:")
        or ownership.get("cue_count") != len(cues)
        or ownership.get("decision_ledger_sha256") != _sha256(ledger_path).removeprefix("sha256:")
        or ownership.get("diagnostic_diff_sha256") != _sha256(diff_path).removeprefix("sha256:")
        or not isinstance(release_lane, Mapping)
        or release_lane.get("srt_sha256") != srt_sha.removeprefix("sha256:")
        or not isinstance(ledger_lane, Mapping)
        or ledger_lane.get("sha256") != _sha256(ledger_path).removeprefix("sha256:")
        or not isinstance(diff_lane, Mapping)
        or diff_lane.get("sha256") != _sha256(diff_path).removeprefix("sha256:")
    ):
        raise TerminalProjectionError(f"Qixi {label} registry ownership drifts")
    if (
        ledger.get("candidate_id") != candidate_id
        or ledger.get("schema_version") not in {
            "operator-reviewed-subtitle-decisions.v1", "operator-reviewed-subtitle-decisions.v2"
        }
        or diff.get("schema_version") != "operator-reviewed-subtitle-truth-diff.v1"
        or diff.get("candidate_id") != candidate_id
        or _normal_sha(diff.get("release_truth_srt_sha256"), label=f"Qixi {label} diff SRT") != srt_sha
        or _normal_sha(diff.get("decision_ledger_sha256"), label=f"Qixi {label} diff ledger")
        != _sha256(ledger_path)
    ):
        raise TerminalProjectionError(f"Qixi {label} ledger/diff binding drifts")
    pipeline_lane = lanes.get("pipeline_diagnostic")
    pipeline_sha = "sha256:" + hashlib.sha256(pipeline_bytes).hexdigest()
    if (
        not isinstance(pipeline_lane, Mapping)
        or _normal_sha(ledger.get("pipeline_srt_sha256"), label=f"Qixi {label} ledger pipeline")
        != pipeline_sha
        or _normal_sha(diff.get("pipeline_srt_sha256"), label=f"Qixi {label} diff pipeline")
        != pipeline_sha
        or ownership.get("pipeline_srt_sha256") != pipeline_sha.removeprefix("sha256:")
        or pipeline_lane.get("sha256") != pipeline_sha.removeprefix("sha256:")
    ):
        raise TerminalProjectionError(f"Qixi {label} truth pipeline binding drifts")
    try:
        pipeline_cues = parse_srt_cues(pipeline_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise TerminalProjectionError(f"Qixi {label} pipeline diagnostic is invalid") from exc
    decisions = ledger.get("cue_decisions")
    rows = diff.get("rows")
    if not isinstance(decisions, list) or not isinstance(rows, list) or len(decisions) != len(cues) or len(rows) != len(cues):
        raise TerminalProjectionError(f"Qixi {label} cue rows are incomplete")
    for position, (cue, decision, row) in enumerate(
        zip(cues, decisions, rows, strict=True), start=1
    ):
        if not isinstance(decision, Mapping) or not isinstance(row, Mapping) or (
            decision.get("cue") != position or row.get("cue") != position
            or decision.get("start_ms") != cue.start_ms or row.get("start_ms") != cue.start_ms
            or decision.get("end_ms") != cue.end_ms or row.get("end_ms") != cue.end_ms
            or row.get("release_truth_text") != cue.text
            or row.get("disposition") != decision.get("disposition")
        ):
            raise TerminalProjectionError(f"Qixi {label} cue row drifts")
        if decision.get("disposition") == "OPERATOR_EXACT_TEXT" and decision.get("release_text") != cue.text:
            raise TerminalProjectionError(f"Qixi {label} exact decision text drifts")
        if (
            pipeline_cues[position - 1].start_ms != cue.start_ms
            or pipeline_cues[position - 1].end_ms != cue.end_ms
            or row.get("pipeline_text") != pipeline_cues[position - 1].text
        ):
            raise TerminalProjectionError("Qixi current diagnostic cue binding drifts")
    receipt_lanes = receipt.get("truth_lanes")
    if (
        receipt.get("schema_version") != "operator-reviewed-subtitle-baseline-delivery.v1"
        or receipt.get("candidate_id") != candidate_id
        or _normal_sha(receipt.get("baseline_sha256"), label=f"Qixi {label} receipt SRT") != srt_sha
        or receipt.get("cue_count") != len(cues)
        or not isinstance(receipt.get("reviewed_srt"), Mapping)
        or _normal_sha(receipt["reviewed_srt"].get("sha256"), label=f"Qixi {label} receipt reviewed SRT") != srt_sha
        or not isinstance(receipt_lanes, Mapping)
        or receipt_lanes.get("schema_version") != "operator-reviewed-subtitle-truth-lanes.v1"
        or not isinstance(receipt_lanes.get("release_truth"), Mapping)
        or receipt_lanes["release_truth"].get("srt_sha256") != srt_sha.removeprefix("sha256:")
        or not isinstance(receipt_lanes.get("pipeline_diagnostic"), Mapping)
        or receipt_lanes["pipeline_diagnostic"].get("sha256") != pipeline_sha.removeprefix("sha256:")
        or not isinstance(receipt_lanes.get("decision_ledger"), Mapping)
        or receipt_lanes["decision_ledger"].get("sha256") != _sha256(ledger_path).removeprefix("sha256:")
        or not isinstance(receipt_lanes.get("diff_receipt"), Mapping)
        or receipt_lanes["diff_receipt"].get("sha256") != _sha256(diff_path).removeprefix("sha256:")
        or receipt.get("diagnostic_diff") != diff
    ):
        raise TerminalProjectionError(f"Qixi {label} delivery receipt drifts")


def _validate_current_extras(
    *, repo_root: Path, lane: Mapping[str, object], cues: list[SrtCue], source: Mapping[str, object]
) -> None:
    _evidence_path, evidence = _sealed_json(
        repo_root, lane["cue21_evidence_path"], lane["cue21_evidence_sha256"], label="Qixi cue21 evidence"
    )
    _contract_path, contract = _sealed_json(
        repo_root, lane["final_contract_path"], lane["final_contract_sha256"], label="Qixi final contract"
    )
    if (
        evidence.get("schema_version") != "qixi-cue21-canonical-provider-evidence.v1"
        or evidence.get("status") != "UNRESOLVED_NO_RELEASE_TEXT"
        or not isinstance(evidence.get("source_recording"), Mapping)
        or evidence["source_recording"].get("basename") != "22966160_20260817-11-30-22.mp4"
        or evidence["source_recording"].get("sha256") != str(source.get("sha256", "")).removeprefix("sha256:")
        or contract.get("schema_version") != "lidousha-final-media-review-contracts.v1"
    ):
        raise TerminalProjectionError("Qixi current diagnostic/final-contract schema drifts")
    contracts = contract.get("contracts")
    qixi = [item for item in contracts if isinstance(item, Mapping) and item.get("candidate_id") == "auto_113022_354_496"] if isinstance(contracts, list) else []
    points = qixi[0].get("subtitle_review_points", []) if len(qixi) == 1 else []
    point_map = {
        point.get("point_id"): point for point in points if isinstance(point, Mapping)
    } if isinstance(points, list) else {}
    if set(point_map) != {
        "qixi-tomorrow-night-lara-title", "qixi-balance-iiya",
        "qixi-sweet-or-bitter-ending", "qixi-full-release-text-stability",
    } or point_map["qixi-balance-iiya"].get("final_video_start_ms") != 71873 or point_map[
        "qixi-balance-iiya"
    ].get("final_video_end_ms") != 74273:
        raise TerminalProjectionError("Qixi current final contract does not bind its review points")
    expected = {5: "明天晚上和大家看《再见菈菈》", 21: "非常 balance いいや", 59: "这两天这两天温柔播", 60: "播的有点压抑了"}
    if any(cues[index - 1].text != text for index, text in expected.items()):
        raise TerminalProjectionError("Qixi current cue authority drifts")
    cue21 = cues[20]
    if (
        evidence["source_recording"].get("target_start_ms")
        != source["absolute_start_ms"] + cue21.start_ms
        or evidence["source_recording"].get("target_end_ms")
        != source["absolute_start_ms"] + cue21.end_ms
    ):
        raise TerminalProjectionError("Qixi cue21 evidence interval drifts")


def _require_current_cue_authorities(ledger: Mapping[str, object]) -> None:
    decisions = ledger.get("cue_decisions")
    if not isinstance(decisions, list) or len(decisions) < 60:
        raise TerminalProjectionError("Qixi current decision authority rows are incomplete")
    cue5, cue21, cue59, cue60 = (decisions[index - 1] for index in (5, 21, 59, 60))
    if not all(isinstance(item, Mapping) for item in (cue5, cue21, cue59, cue60)):
        raise TerminalProjectionError("Qixi current decision authority rows are invalid")
    if (
        cue5.get("disposition") != "OPERATOR_UNCHANGED_FREEZE"
        or "decision_authority" in cue5
        or cue21.get("disposition") != "OPERATOR_EXACT_TEXT"
        or cue21.get("release_text") != "非常 balance いいや"
        or cue59.get("disposition") != "OPERATOR_EXACT_TEXT"
        or cue59.get("release_text") != "这两天这两天温柔播"
        or cue60.get("disposition") != "OPERATOR_EXACT_TEXT"
        or cue60.get("release_text") != "播的有点压抑了"
    ):
        raise TerminalProjectionError("Qixi current decision text rows drift")
    cue21_authority = cue21.get("decision_authority")
    old_authorities = (cue59.get("decision_authority"), cue60.get("decision_authority"))
    if (
        not isinstance(cue21_authority, Mapping)
        or cue21_authority.get("kind") != "IVAN_OPERATOR"
        or "非常 balance iiya" not in str(cue21_authority.get("evidence_ref"))
        or "非常 balance いいや" not in str(cue21_authority.get("evidence_ref"))
        or any(
            not isinstance(authority, Mapping)
            or authority.get("kind") != "IVAN_OPERATOR"
            or "2:39说的是播的有点压抑了" not in str(authority.get("evidence_ref"))
            for authority in old_authorities
        )
    ):
        raise TerminalProjectionError("Qixi current decision provenance drifts")


def _validate_terminal_boundary(
    *, boundary: Mapping[str, object], record: Mapping[str, object], record_sha256: str,
    terminal_cues: list[SrtCue], preimage_cues: list[SrtCue], current_cues: list[SrtCue],
) -> None:
    required = {
        "record_sha256", "boundary_audit_canonical_sha256", "final_start_ms", "final_end_ms",
        "final_closure_cue_index", "final_closure_end_ms", "final_closure_text_sha256",
    }
    final_end = boundary.get("final_end_ms")
    record_boundary = record.get("boundary_audit")
    review = (
        record_boundary.get("final_delivery_boundary_semantic_review")
        if isinstance(record_boundary, Mapping)
        else None
    )
    endpoint = review.get("final_endpoint_binding") if isinstance(review, Mapping) else None
    if (
        set(boundary) != required
        or _projection_sha_entry(boundary, "record_sha256", label="terminal projection")
        != _normal_sha(record_sha256, label="terminal record")
        or _projection_sha_entry(boundary, "boundary_audit_canonical_sha256", label="terminal projection")
        != _canonical_sha(record_boundary)
        or not isinstance(final_end, int)
        or isinstance(final_end, bool)
        or boundary.get("final_start_ms") != 0
        or record.get("duration_ms") != final_end
        or boundary.get("final_closure_cue_index") != 54
        or terminal_cues[-1].end_ms != boundary.get("final_closure_end_ms")
        or _normal_sha(
            "sha256:" + hashlib.sha256(terminal_cues[-1].text.encode()).hexdigest(),
            label="terminal closure",
        ) != _projection_sha_entry(boundary, "final_closure_text_sha256", label="terminal projection")
        or not isinstance(endpoint, Mapping)
        or endpoint.get("final_start_ms") != boundary["final_start_ms"]
        or endpoint.get("final_end_ms") != final_end
        or endpoint.get("final_closure_cue_index") != boundary["final_closure_cue_index"]
        or endpoint.get("final_snapped_end_ms") != boundary["final_closure_end_ms"]
        or endpoint.get("closure_text_sha256") != boundary["final_closure_text_sha256"]
        or any(cue.start_ms <= final_end for cue in preimage_cues[54:])
        or any(cue.start_ms <= final_end for cue in current_cues[54:])
    ):
        raise TerminalProjectionError("Qixi terminal boundary/closure drifts")


def _sealed_srt(
    repo_root: Path, descriptor: Mapping[str, object], *, label: str
) -> tuple[list[SrtCue], bytes]:
    if set(descriptor) != {"path", "sha256", "bytes", "cue_count"}:
        raise TerminalProjectionError(f"{label} descriptor is invalid")
    path, payload = _sealed_repository_bytes(
        repo_root,
        _safe_relative(descriptor.get("path"), label=label),
        label=label,
    )
    if (
        _sha256(path) != _projection_sha_entry(descriptor, "sha256", label=label)
        or descriptor.get("bytes") != len(payload)
    ):
        raise TerminalProjectionError(f"{label} hash drifts")
    try:
        cues = parse_srt_cues(payload.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise TerminalProjectionError(f"{label} is invalid") from exc
    if descriptor.get("cue_count") != len(cues):
        raise TerminalProjectionError(f"{label} cue count drifts")
    return cues, payload


def _validate_preimage(
    *, repo_root: Path, descriptor: Mapping[str, object], current: Mapping[str, object],
    correction_pin: Mapping[str, object], terminal: Mapping[str, object],
) -> list[SrtCue]:
    if set(descriptor) != {"manifest_path", "manifest_sha256"}:
        raise TerminalProjectionError("Qixi superseded preimage descriptor is invalid")
    _path, manifest = _sealed_json(
        repo_root,
        descriptor["manifest_path"],
        descriptor["manifest_sha256"],
        label="Qixi superseded preimage manifest",
    )
    required = {
        "schema_version", "status", "not_release_authority", "candidate_id",
        "pre_correction_srt", "superseded_by",
    }
    if (
        set(manifest) != required
        or manifest.get("schema_version") != "qixi-superseded-terminal-preimage.v1"
        or manifest.get("status") != "SUPERSEDED_TEXT_PREIMAGE_ONLY"
        or manifest.get("not_release_authority") is not True
        or manifest.get("candidate_id") != "auto_113022_354_496"
        or not isinstance(manifest.get("pre_correction_srt"), Mapping)
        or not isinstance(manifest.get("superseded_by"), Mapping)
    ):
        raise TerminalProjectionError("Qixi superseded preimage manifest is invalid")
    preimage, _payload = _sealed_srt(
        repo_root, manifest["pre_correction_srt"], label="Qixi superseded preimage SRT"
    )
    superseded_by = manifest["superseded_by"]
    if (
        set(superseded_by) != {
            "current_truth_srt_sha256", "terminal_srt_sha256", "correction_sha256"
        }
        or superseded_by.get("current_truth_srt_sha256") != current.get("srt_sha256")
        or superseded_by.get("terminal_srt_sha256") != terminal.get("sha256")
        or superseded_by.get("correction_sha256") != correction_pin.get("sha256")
    ):
        raise TerminalProjectionError("Qixi superseded preimage successor binding drifts")
    return preimage


def _replay_projection_assets(repo_root: Path) -> _ProjectionAssets:
    projection, _payload = load_projection(repo_root)
    required = {
        "schema_version", "status", "candidate_id", "superseded_preimage", "current_truth",
        "terminal_srt", "source_recording", "terminal_boundary", "current_correction",
        "authority_sha256",
    }
    if (
        set(projection) != required
        or projection.get("schema_version") != SCHEMA
        or projection.get("status") != "READY"
        or projection.get("candidate_id") != "auto_113022_354_496"
    ):
        raise TerminalProjectionError("Qixi terminal projection schema is invalid")
    current = projection["current_truth"]
    terminal = projection["terminal_srt"]
    source = projection["source_recording"]
    correction_pin = projection["current_correction"]
    preimage_descriptor = projection["superseded_preimage"]
    if not all(
        isinstance(value, Mapping)
        for value in (current, terminal, source, correction_pin, preimage_descriptor)
    ):
        raise TerminalProjectionError("Qixi terminal projection bindings are invalid")
    current_cues, _registry, current_ledger, _diff = _truth_lane(
        repo_root=repo_root, lane=current, label="current", require_current_extras=True
    )
    terminal_cues, terminal_bytes = _sealed_srt(
        repo_root, terminal, label="Qixi terminal projection SRT"
    )
    if terminal.get("cue_count") != 54 or len(terminal_cues) != 54 or _srt_bytes(current_cues[:54]) != terminal_bytes:
        raise TerminalProjectionError("Qixi terminal is not the current operator-truth prefix")
    if (
        set(correction_pin) != {
            "sha256", "schema_version", "before_srt_sha256", "after_srt_sha256",
            "replace_operations", "set_line_operations",
        }
        or correction_pin.get("schema_version") != "human-subtitle-correction.v2"
        or correction_pin.get("replace_operations") != []
        or correction_pin.get("set_line_operations") != ["21=非常 balance いいや"]
    ):
        raise TerminalProjectionError("Qixi terminal projection correction schema drifts")
    preimage_cues = _validate_preimage(
        repo_root=repo_root,
        descriptor=preimage_descriptor,
        current=current,
        correction_pin=correction_pin,
        terminal=terminal,
    )
    if len(preimage_cues) != 54:
        raise TerminalProjectionError("Qixi superseded preimage cue count drifts")
    preimage = _srt_bytes(preimage_cues)
    corrected = list(preimage_cues)
    corrected[20] = SrtCue(corrected[20].index, corrected[20].start_ms, corrected[20].end_ms, "非常 balance いいや")
    if (
        "sha256:" + hashlib.sha256(preimage).hexdigest()
        != _projection_sha_entry(correction_pin, "before_srt_sha256", label="terminal projection")
        or _projection_sha_entry(correction_pin, "after_srt_sha256", label="terminal projection")
        != _projection_sha_entry(terminal, "sha256", label="terminal projection")
        or _srt_bytes(corrected) != terminal_bytes
    ):
        raise TerminalProjectionError("Qixi terminal historical correction replay drifts")
    source_keys = {
        "absolute_start_ms", "basename", "reviewed_absolute_end_ms", "sha256",
        "terminal_absolute_end_ms",
    }
    source_times = (
        source.get("absolute_start_ms"), source.get("terminal_absolute_end_ms"),
        source.get("reviewed_absolute_end_ms"),
    )
    if (
        set(source) != source_keys
        or any(isinstance(value, bool) or not isinstance(value, int) for value in source_times)
        or source.get("basename") != "22966160_20260817-11-30-22.mp4"
        or _normal_sha(source.get("sha256"), label="terminal source recording")
        != "sha256:212eb59bee50dda1601a3f30e3c7b9a307715058fb472646ddcfa200615c6a97"
        or source["terminal_absolute_end_ms"] >= source["reviewed_absolute_end_ms"]
        or source["reviewed_absolute_end_ms"]
        != source["absolute_start_ms"] + current_cues[-1].end_ms
    ):
        raise TerminalProjectionError("Qixi terminal source interval drifts")
    _validate_current_extras(repo_root=repo_root, lane=current, cues=current_cues, source=source)
    _require_current_cue_authorities(current_ledger)
    return _ProjectionAssets(
        projection=projection,
        current_cues=current_cues,
        current_ledger=current_ledger,
        preimage_cues=preimage_cues,
        terminal_cues=terminal_cues,
        terminal_bytes=terminal_bytes,
    )


def validate_projection_assets(repo_root: Path) -> dict[str, Any]:
    """Replay the complete sealed Qixi truth graph without mutable package inputs."""

    return _replay_projection_assets(repo_root).projection


def validate_projection(
    *, repo_root: Path, correction: Mapping[str, object], correction_sha256: str,
    record: Mapping[str, object], record_sha256: str, subtitle_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assets = _replay_projection_assets(repo_root)
    projection = assets.projection
    source = projection["source_recording"]
    boundary = projection["terminal_boundary"]
    correction_pin = projection["current_correction"]
    if assets.terminal_bytes != subtitle_text.encode("utf-8"):
        raise TerminalProjectionError("Qixi terminal projection SRT differs from release subtitle")
    if (
        _normal_sha(correction_sha256, label="current correction")
        != _projection_sha_entry(correction_pin, "sha256", label="terminal projection")
        or correction.get("schema_version") != correction_pin.get("schema_version")
        or correction.get("candidate_id") != projection["candidate_id"]
        or correction.get("replace_operations") != correction_pin.get("replace_operations")
        or correction.get("set_line_operations") != correction_pin.get("set_line_operations")
        or _normal_sha(correction.get("before_srt_sha256"), label="current correction before")
        != _projection_sha_entry(correction_pin, "before_srt_sha256", label="terminal projection")
        or _normal_sha(correction.get("after_srt_sha256"), label="current correction after")
        != _projection_sha_entry(correction_pin, "after_srt_sha256", label="terminal projection")
    ):
        raise TerminalProjectionError("Qixi terminal correction receipt drifts")
    if source["absolute_start_ms"] + int(record.get("duration_ms", -1)) != source[
        "terminal_absolute_end_ms"
    ]:
        raise TerminalProjectionError("Qixi terminal source duration drifts")
    _validate_terminal_boundary(
        boundary=boundary,
        record=record,
        record_sha256=record_sha256,
        terminal_cues=assets.terminal_cues,
        preimage_cues=assets.preimage_cues,
        current_cues=assets.current_cues,
    )
    config = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "mode": "preserve_text_outside_source_truth",
        "exact_interval_replay": True,
        "path": str(
            repo_root
            / _safe_relative(projection["terminal_srt"]["path"], label="terminal SRT")
        ),
        "sha256": projection["terminal_srt"]["sha256"].removeprefix("sha256:"),
        "authority": "sealed superseded preimage plus current exact correction",
        "source_recording_basename": source["basename"],
        "source_sha256": str(source["sha256"]).removeprefix("sha256:"),
        "absolute_source_start_ms": source["absolute_start_ms"],
        "absolute_source_end_ms": source["terminal_absolute_end_ms"],
    }
    output, audit = apply_redelivery_subtitle_baseline(
        subtitle_text,
        config=config,
        spec_parent=repo_root,
        current_source_start_ms=source["absolute_start_ms"],
        current_source_end_ms=source["terminal_absolute_end_ms"],
        current_source_recording_basename=source["basename"],
        current_source_sha256=str(source["sha256"]).removeprefix("sha256:"),
    )
    if output != subtitle_text or audit.get("status") not in {"ALREADY_SATISFIED", "APPLIED"}:
        raise TerminalProjectionError(
            "Qixi terminal projection baseline replay does not reproduce release subtitle: "
            f"{audit.get('status')} {audit.get('failures')} "
            f"{audit.get('baseline_sha256')} {audit.get('expected_baseline_sha256')}"
        )
    return projection, audit


def build_terminal_chat(**kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    projection, baseline_audit = validate_projection(**kwargs)
    subtitle_text = str(kwargs["subtitle_text"])
    record = kwargs["record"]
    if not isinstance(record, Mapping):
        raise TerminalProjectionError("Qixi terminal record is invalid")
    chat: dict[str, Any] = {
        "schema_version": "chat-authority-audit.v2", "status": "APPLIED_AND_VERIFIED",
        "input_srt_sha256": hashlib.sha256(subtitle_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(subtitle_text.encode()).hexdigest(),
        "source_subtitle_truth_audit": {"schema_version": "source-subtitle-truth-audit.v1", "status": "NO_RELEVANT_INTERVAL", "applied": [], "satisfied": [], "failures": []},
        "redelivery_subtitle_baseline_audit": baseline_audit,
        "terminal_projection_authority": {"schema_version": projection["schema_version"], "authority_sha256": projection["authority_sha256"]},
        "applied": [], "sender_repairs": [], "gift_repairs": [], "coreference_repairs": [], "entity_repairs": [], "pending_text_overrides": [],
    }
    if not verify_chat_authority_final_surfaces(chat, final_text_srt=subtitle_text, final_speaker_srt=subtitle_text, delivery_start_ms=0, delivery_end_ms=int(record["duration_ms"])):
        raise TerminalProjectionError("Qixi terminal projection chat does not verify final surfaces")
    return chat, baseline_audit
