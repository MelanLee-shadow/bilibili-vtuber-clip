"""Candidate-scoped, hash-bound reviewed subtitle baseline discovery.

Reviewed subtitle text is human truth, independent of whether a cover is kept
or regenerated.  The runner discovers one canonical manifest per candidate
and injects its validated producer baseline config.  Both the manifest and the
SRT bytes are returned as fingerprint inputs so an edit wakes only that
candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from src.autoslice.redelivery_boundary_projection import (
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.qixi_cue21_diagnostic_evidence import (
    QixiCue21DiagnosticEvidenceError,
    validate_qixi_cue21_diagnostic_evidence,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)
from src.autoslice.reviewed_exact_source_interval import (
    REFERENCE_CONFIG_KEY as EXACT_INTERVAL_REFERENCE_CONFIG_KEY,
    REFERENCE_SCHEMA_VERSION as EXACT_INTERVAL_REFERENCE_SCHEMA_VERSION,
    ReviewedExactSourceIntervalError,
    validate_runtime_authority,
)
from src.autoslice.redelivery_time_domain import (
    RedeliveryTimeDomainError,
    operator_v3_time_domain,
)


REGISTRY_SCHEMA_VERSION = "candidate-reviewed-subtitle-baseline.v1"
BASELINE_SCHEMA_VERSIONS = frozenset(
    {"subtitle-redelivery-baseline.v1", "subtitle-redelivery-baseline.v2"}
)
BASELINE_MODE = "preserve_text_outside_source_truth"
OPERATOR_TEXT_PIN_V2 = "operator-reviewed-text-full-ownership-pin.v2"
OPERATOR_TEXT_PIN_V3 = "operator-reviewed-text-full-ownership-pin.v3"
OPERATOR_TRUTH_LANES_SCHEMA = "operator-reviewed-subtitle-truth-lanes.v1"
OPERATOR_TRUTH_LANES_V2_SCHEMA = "operator-reviewed-subtitle-truth-lanes.v2"
OPERATOR_DECISION_LEDGER_SCHEMAS = frozenset({
    "operator-reviewed-subtitle-decisions.v1",
    "operator-reviewed-subtitle-decisions.v2",
    "operator-reviewed-subtitle-decisions.v3",
})
OPERATOR_DIAGNOSTIC_DIFF_SCHEMA = "operator-reviewed-subtitle-truth-diff.v1"
OPERATOR_DIAGNOSTIC_DIFF_V2_SCHEMA = "operator-reviewed-subtitle-truth-diff.v2"
_CANDIDATE_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA256_RX = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_REPO_DIRECTORY = Path("assets/lidousha/reviewed_subtitle_baselines")
_EXACT_INTERVAL_REPO_DIRECTORY = Path("assets/lidousha/reviewed_exact_source_intervals")


def _valid_speaker_authority(*, candidate_id: str, value: object) -> bool:
    """Allow the exceptional C3 speaker authority without opening other lanes."""

    return value == "NOT_CLAIMED_TEXT_ONLY" or (
        candidate_id == "auto_220021_561_670"
        and value == "IVAN_LINE947_EXHAUSTIVE"
    )


class ReviewedSubtitleBaselineRegistryError(ValueError):
    """The canonical baseline asset is present but cannot be trusted."""


def _validate_diagnostic_evidence(
    value: object, *, root: Path, candidate_id: str, document: Mapping[str, object], cue_start_ms: int, cue_end_ms: int
) -> None:
    if value is None:
        return
    try:
        validate_qixi_cue21_diagnostic_evidence(
            value,
            evidence_root=root,
            candidate_id=candidate_id,
            source_basename=str(document["source_recording_basename"]),
            source_sha256=str(document["source_sha256"]),
            cue_start_ms=cue_start_ms,
            cue_end_ms=cue_end_ms,
            absolute_source_start_ms=int(document["absolute_source_start_ms"]),
        )
    except (KeyError, TypeError, ValueError, QixiCue21DiagnosticEvidenceError) as exc:
        raise ReviewedSubtitleBaselineRegistryError("operator diagnostic evidence is invalid") from exc


@dataclass(frozen=True)
class ReviewedSubtitleBaseline:
    config: dict[str, Any]
    manifest_path: Path
    baseline_path: Path
    operator_truth_lane_paths: tuple[Path, ...] = ()
    exact_interval_authority_path: Path | None = None
    exact_interval_authority: dict[str, object] | None = None

    @property
    def fingerprint_paths(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in (
                self.manifest_path,
                self.baseline_path,
                *self.operator_truth_lane_paths,
                self.exact_interval_authority_path,
            )
            if path is not None
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_sha256(value: object) -> str:
    match = _SHA256_RX.fullmatch(str(value or "").strip())
    return match.group(1) if match is not None else ""


def _regular_canonical_file(path: Path, *, root: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ReviewedSubtitleBaselineRegistryError(f"{label} must be a regular non-symlink file")
    resolved = path.resolve()
    if resolved.parent != root.resolve():
        raise ReviewedSubtitleBaselineRegistryError(f"{label} escapes its canonical asset root")
    return resolved


def _lexical_absolute(path: Path) -> Path:
    """Make a path absolute without resolving any symlink component."""

    return Path(os.path.abspath(os.fspath(path)))


def _require_trusted_exact_asset_roots(
    root: Path,
    *,
    repo_root: Path,
) -> tuple[Path, Path]:
    """Bind exact replay assets to this process's explicit repository root.

    Resolving ``root`` before establishing this relationship would let a
    canonical-looking profile directory symlink to a second Git checkout (or a
    forged deployed tree) and borrow that tree's authority.  Compare lexical
    paths first, then reject every symlink below the trusted repository root.
    The repository authority loader repeats the component check while binding
    the exact bytes, closing a subsequent path-swap race.
    """
    declared_repo_root = _lexical_absolute(repo_root)
    declared_baseline_root = _lexical_absolute(root)
    expected_baseline_root = declared_repo_root / _BASELINE_REPO_DIRECTORY
    expected_authority_root = declared_repo_root / _EXACT_INTERVAL_REPO_DIRECTORY
    if declared_baseline_root != expected_baseline_root:
        raise ReviewedSubtitleBaselineRegistryError(
            "reviewed exact interval baseline root is not canonical for the trusted repository"
        )
    try:
        if declared_repo_root.is_symlink():
            raise ReviewedSubtitleBaselineRegistryError(
                "trusted repository root must not be a symlink"
            )
        resolved_repo_root = declared_repo_root.resolve(strict=True)
        if resolved_repo_root != declared_repo_root:
            raise ReviewedSubtitleBaselineRegistryError(
                "trusted repository root contains a symlink"
            )
        for relative in (
            _BASELINE_REPO_DIRECTORY,
            _EXACT_INTERVAL_REPO_DIRECTORY,
        ):
            cursor = declared_repo_root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ReviewedSubtitleBaselineRegistryError(
                        "reviewed exact interval asset path contains a parent symlink"
                    )
            resolved = cursor.resolve(strict=True)
            if resolved != resolved_repo_root / relative:
                raise ReviewedSubtitleBaselineRegistryError(
                    "reviewed exact interval asset path escapes the trusted repository"
                )
    except ReviewedSubtitleBaselineRegistryError:
        raise
    except (OSError, ValueError) as exc:
        raise ReviewedSubtitleBaselineRegistryError(
            "reviewed exact interval trusted repository path is unavailable"
        ) from exc
    return declared_repo_root, expected_authority_root


def _required_text(value: object, *, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} is required")
    return text


def _read_hash_bound_sibling(
    value: object,
    *,
    root: Path,
    label: str,
) -> tuple[Path, str, bytes]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} binding is invalid")
    raw_path = _required_text(value.get("path"), label=f"{label} path")
    relative = Path(raw_path)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != raw_path:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} path must be a sibling filename")
    path = _regular_canonical_file(root / relative, root=root, label=label)
    expected = _SHA256_RX.fullmatch(str(value.get("sha256") or ""))
    if expected is None or _sha256(path) != expected.group(1):
        raise ReviewedSubtitleBaselineRegistryError(f"{label} sha256 does not match")
    try:
        return path, expected.group(1), path.read_bytes()
    except OSError as exc:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} cannot be read") from exc


def _require_operator_authority(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "evidence_ref"}:
        raise ReviewedSubtitleBaselineRegistryError(f"{label} must be typed Ivan operator authority")
    kind = value.get("kind")
    evidence_ref = value.get("evidence_ref")
    if kind != "IVAN_OPERATOR" or not isinstance(evidence_ref, str) or not evidence_ref.strip():
        raise ReviewedSubtitleBaselineRegistryError(f"{label} must be typed Ivan operator authority")
    return {"kind": "IVAN_OPERATOR", "evidence_ref": evidence_ref.strip()}


def _validate_operator_truth_lanes(
    document: Mapping[str, object],
    *,
    root: Path,
    candidate_id: str,
    baseline_sha256: str,
) -> tuple[dict[str, object], dict[str, Path]]:
    """Validate v2 lanes; legacy v1 pins never obtain the all-text fast path."""

    pin = document.get("operator_text_full_ownership")
    if not isinstance(pin, Mapping) or pin.get("schema_version") not in {OPERATOR_TEXT_PIN_V2, OPERATOR_TEXT_PIN_V3}:
        return {}, {}
    if pin.get("schema_version") == OPERATOR_TEXT_PIN_V3:
        return _validate_operator_truth_lanes_v3(
            document, root=root, candidate_id=candidate_id, baseline_sha256=baseline_sha256
        )
    lanes = document.get("operator_truth_lanes")
    if not isinstance(lanes, Mapping) or set(lanes) != {
        "schema_version", "release_truth", "pipeline_diagnostic", "decision_ledger", "diff_receipt"
    } or lanes.get("schema_version") != OPERATOR_TRUTH_LANES_SCHEMA:
        raise ReviewedSubtitleBaselineRegistryError("operator truth lanes are invalid")
    release = lanes.get("release_truth")
    if not isinstance(release, Mapping) or set(release) != {"srt_sha256"}:
        raise ReviewedSubtitleBaselineRegistryError("operator release truth lane is invalid")
    release_sha = _SHA256_RX.fullmatch(str(release.get("srt_sha256") or ""))
    if release_sha is None or release_sha.group(1) != baseline_sha256:
        raise ReviewedSubtitleBaselineRegistryError("operator release truth lane sha256 drift")
    pipeline_path, pipeline_sha, pipeline_bytes = _read_hash_bound_sibling(
        lanes.get("pipeline_diagnostic"), root=root, label="operator pipeline diagnostic SRT"
    )
    ledger_path, ledger_sha, ledger_bytes = _read_hash_bound_sibling(
        lanes.get("decision_ledger"), root=root, label="operator decision ledger"
    )
    diff_path, diff_sha, diff_bytes = _read_hash_bound_sibling(
        lanes.get("diff_receipt"), root=root, label="operator diagnostic diff"
    )
    try:
        ledger = json.loads(ledger_bytes.decode("utf-8"))
        diff = json.loads(diff_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedSubtitleBaselineRegistryError("operator truth-lane JSON is invalid") from exc
    if not isinstance(ledger, Mapping) or not isinstance(diff, Mapping):
        raise ReviewedSubtitleBaselineRegistryError("operator truth-lane JSON must be an object")
    expected_ledger_keys = {
        "schema_version",
        "candidate_id",
        "report_scope",
        "pipeline_srt_sha256",
        "operator_authority",
        "cue_decisions",
    }
    if (
        set(ledger) != expected_ledger_keys
        or ledger.get("schema_version") not in OPERATOR_DECISION_LEDGER_SCHEMAS
        or ledger.get("candidate_id") != candidate_id
        or ledger.get("report_scope") != "EXHAUSTIVE"
        or not isinstance(ledger.get("cue_decisions"), list)
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator decision ledger contract is invalid")
    authority = _require_operator_authority(
        ledger.get("operator_authority"), label="operator decision ledger authority"
    )
    expected_diff_keys = {
        "schema_version",
        "candidate_id",
        "pipeline_srt_sha256",
        "release_truth_srt_sha256",
        "decision_ledger_sha256",
        "cue_count",
        "changed_cue_count",
        "rows",
    }
    if (
        set(diff) != expected_diff_keys
        or diff.get("schema_version") != OPERATOR_DIAGNOSTIC_DIFF_SCHEMA
        or diff.get("candidate_id") != candidate_id
        or diff.get("pipeline_srt_sha256") != pipeline_sha
        or diff.get("release_truth_srt_sha256") != baseline_sha256
        or diff.get("decision_ledger_sha256") != ledger_sha
        or isinstance(diff.get("cue_count"), bool)
        or not isinstance(diff.get("cue_count"), int)
        or isinstance(diff.get("changed_cue_count"), bool)
        or not isinstance(diff.get("changed_cue_count"), int)
        or not isinstance(diff.get("rows"), list)
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator diagnostic diff contract is invalid")
    try:
        pipeline_cues = parse_srt_cues(pipeline_bytes.decode("utf-8"))
        release_cues = parse_srt_cues((root / str(document["path"])).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, OSError, ValueError) as exc:
        raise ReviewedSubtitleBaselineRegistryError("operator truth-lane SRT is invalid") from exc
    decisions = ledger["cue_decisions"]
    diff_rows = diff["rows"]
    if (
        len(pipeline_cues) != len(release_cues)
        or len(decisions) != len(pipeline_cues)
        or len(diff_rows) != len(pipeline_cues)
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator decision ledger cue grid drift")
    ledger_exact_count = 0
    ledger_freeze_count = 0
    actual_changed_count = 0
    for ordinal, (pipeline_cue, release_cue, decision, diff_row) in enumerate(
        zip(pipeline_cues, release_cues, decisions, diff_rows, strict=True), start=1
    ):
        if (
            str(pipeline_cue.index) != str(release_cue.index)
            or str(release_cue.index) != str(ordinal)
            or (pipeline_cue.start_ms, pipeline_cue.end_ms)
            != (release_cue.start_ms, release_cue.end_ms)
            or not isinstance(decision, Mapping)
        ):
            raise ReviewedSubtitleBaselineRegistryError("operator decision ledger cue grid drift")
        required_decision = {
            "cue", "source_index", "start_ms", "end_ms", "source_text_sha256", "disposition"
        }
        if not required_decision.issubset(decision) or set(decision) - (
            required_decision
            | {"release_text", "decision_authority", "proposal", "rejected_machine_proposal", "diagnostic_evidence"}
        ):
            raise ReviewedSubtitleBaselineRegistryError("operator decision ledger cue row is invalid")
        source_text_sha = hashlib.sha256(
            str(pipeline_cue.text).strip().encode("utf-8")
        ).hexdigest()
        if (
            decision.get("cue") != ordinal
            or decision.get("source_index") != str(pipeline_cue.index)
            or (decision.get("start_ms"), decision.get("end_ms"))
            != (pipeline_cue.start_ms, pipeline_cue.end_ms)
            or str(decision.get("source_text_sha256") or "").removeprefix("sha256:")
            != source_text_sha
        ):
            raise ReviewedSubtitleBaselineRegistryError("operator decision ledger source binding drift")
        before = str(pipeline_cue.text).strip()
        after = str(release_cue.text).strip()
        if before != after:
            actual_changed_count += 1
        disposition = decision.get("disposition")
        expected_diff = {
            "cue": ordinal,
            "source_index": str(pipeline_cue.index),
            "start_ms": pipeline_cue.start_ms,
            "end_ms": pipeline_cue.end_ms,
            "pipeline_text": before,
            "release_truth_text": after,
            "disposition": disposition,
        }
        if (
            not isinstance(diff_row, Mapping)
            or not set(expected_diff).issubset(diff_row)
            or set(diff_row)
            - (set(expected_diff) | {"decision_authority", "proposal", "rejected_machine_proposal", "diagnostic_evidence"})
            or any(diff_row.get(key) != value for key, value in expected_diff.items())
        ):
            raise ReviewedSubtitleBaselineRegistryError("operator diagnostic diff cue row is invalid")
        if disposition == "OPERATOR_EXACT_TEXT":
            decision_authority = _require_operator_authority(
                decision.get("decision_authority"), label="operator exact text decision"
            )
            if (
                decision.get("release_text") != after
                or "proposal" in decision
            ):
                raise ReviewedSubtitleBaselineRegistryError("operator exact text decision is invalid")
            rejected = decision.get("rejected_machine_proposal")
            if rejected is not None:
                provenance = rejected.get("provenance") if isinstance(rejected, Mapping) else None
                if (
                    not isinstance(rejected, Mapping)
                    or set(rejected) != {"text", "provenance"}
                    or not isinstance(rejected.get("text"), str)
                    or not rejected["text"].strip()
                    or not isinstance(provenance, Mapping)
                    or set(provenance) != {"provider", "artifact_sha256"}
                    or not isinstance(provenance.get("provider"), str)
                    or not provenance["provider"].strip()
                    or _SHA256_RX.fullmatch(str(provenance.get("artifact_sha256") or "")) is None
                ):
                    raise ReviewedSubtitleBaselineRegistryError("rejected machine proposal is invalid")
                expected_rejected = {
                    "text": rejected["text"].strip(),
                    "provenance": dict(provenance),
                    "disposition": "REJECTED_BY_OPERATOR_EXACT_TEXT",
                }
                if diff_row.get("rejected_machine_proposal") != expected_rejected:
                    raise ReviewedSubtitleBaselineRegistryError("operator diagnostic rejection drift")
            elif "rejected_machine_proposal" in diff_row:
                raise ReviewedSubtitleBaselineRegistryError("operator diagnostic rejection is unbound")
            if diff_row.get("decision_authority") != decision_authority:
                raise ReviewedSubtitleBaselineRegistryError("operator diagnostic authority drift")
            if diff_row.get("diagnostic_evidence") != decision.get("diagnostic_evidence"):
                raise ReviewedSubtitleBaselineRegistryError("operator diagnostic evidence drift")
            _validate_diagnostic_evidence(
                decision.get("diagnostic_evidence"), root=root, candidate_id=candidate_id,
                document=document, cue_start_ms=pipeline_cue.start_ms, cue_end_ms=pipeline_cue.end_ms,
            )
            ledger_exact_count += 1
        elif disposition == "OPERATOR_UNCHANGED_FREEZE":
            if (
                before != after
                or "release_text" in decision
                or "decision_authority" in decision
                or "rejected_machine_proposal" in decision
            ):
                raise ReviewedSubtitleBaselineRegistryError("operator unchanged freeze is invalid")
            proposal = decision.get("proposal")
            if proposal is not None:
                provenance = proposal.get("provenance") if isinstance(proposal, Mapping) else None
                if (
                    not isinstance(proposal, Mapping)
                    or set(proposal) != {"text", "provenance"}
                    or not isinstance(proposal.get("text"), str)
                    or not proposal["text"].strip()
                    or not isinstance(provenance, Mapping)
                    or set(provenance) != {"provider", "artifact_sha256"}
                    or not isinstance(provenance.get("provider"), str)
                    or not provenance["provider"].strip()
                    or _SHA256_RX.fullmatch(str(provenance.get("artifact_sha256") or "")) is None
                    or diff_row.get("proposal")
                    != {"text": proposal["text"].strip(), "provenance": dict(provenance)}
                ):
                    raise ReviewedSubtitleBaselineRegistryError("operator freeze proposal is invalid")
            elif "proposal" in diff_row:
                raise ReviewedSubtitleBaselineRegistryError("operator freeze proposal is unbound")
            if "decision_authority" in diff_row or "rejected_machine_proposal" in diff_row:
                raise ReviewedSubtitleBaselineRegistryError("operator unchanged diagnostic drift")
            ledger_freeze_count += 1
        elif disposition == "MACHINE_PROPOSAL":
            raise ReviewedSubtitleBaselineRegistryError(
                "standalone machine proposal cannot satisfy operator coverage"
            )
        else:
            raise ReviewedSubtitleBaselineRegistryError("operator decision disposition is invalid")
    expected_pin = {
        "schema_version",
        "baseline_sha256",
        "pipeline_srt_sha256",
        "decision_ledger_sha256",
        "diagnostic_diff_sha256",
        "operator_authority",
        "cue_count",
        "changed_cue_count",
        "operator_exact_text_cue_count",
        "operator_unchanged_freeze_cue_count",
        "speaker_authority",
    }
    if set(pin) != expected_pin:
        raise ReviewedSubtitleBaselineRegistryError("operator text ownership pin has unsupported fields")
    if (
        pin.get("baseline_sha256") != baseline_sha256
        or pin.get("pipeline_srt_sha256") != pipeline_sha
        or pin.get("decision_ledger_sha256") != ledger_sha
        or pin.get("diagnostic_diff_sha256") != diff_sha
        or _require_operator_authority(pin.get("operator_authority"), label="operator text pin authority") != authority
        or not _valid_speaker_authority(candidate_id=candidate_id, value=pin.get("speaker_authority"))
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator text ownership pin is not bound to truth lanes")
    counts = (
        pin.get("cue_count"),
        pin.get("changed_cue_count"),
        pin.get("operator_exact_text_cue_count"),
        pin.get("operator_unchanged_freeze_cue_count"),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
        raise ReviewedSubtitleBaselineRegistryError("operator text ownership pin counts are invalid")
    cue_count, changed_count, exact_count, freeze_count = counts
    if (
        cue_count <= 0
        or changed_count < 0
        or changed_count > exact_count
        or changed_count != actual_changed_count
        or exact_count + freeze_count != cue_count
        or ledger_exact_count != exact_count
        or ledger_freeze_count != freeze_count
        or len(ledger["cue_decisions"]) != cue_count
        or diff.get("cue_count") != cue_count
        or diff.get("changed_cue_count") != actual_changed_count
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator text ownership pin coverage is invalid")
    if str(ledger.get("pipeline_srt_sha256") or "").removeprefix("sha256:") != pipeline_sha:
        raise ReviewedSubtitleBaselineRegistryError("operator decision ledger pipeline binding drift")
    # The diagnostic SRT must be the ledger-bound pipeline output.  Its bytes
    # are intentionally not compared to release truth: their difference is the
    # artifact we need to diagnose the pipeline without contaminating it.
    if hashlib.sha256(pipeline_bytes).hexdigest() != pipeline_sha:
        raise ReviewedSubtitleBaselineRegistryError("operator pipeline diagnostic bytes drift")
    return (
        {
            "schema_version": OPERATOR_TRUTH_LANES_SCHEMA,
            "release_truth": {"srt_sha256": baseline_sha256},
            "pipeline_diagnostic": {"path": str(pipeline_path), "sha256": pipeline_sha},
            "decision_ledger": {"path": str(ledger_path), "sha256": ledger_sha},
            "diff_receipt": {"path": str(diff_path), "sha256": diff_sha},
        },
        {
            "pipeline_diagnostic": pipeline_path,
            "decision_ledger": ledger_path,
            "diff_receipt": diff_path,
        },
    )


def _validate_operator_truth_lanes_v3(
    document: Mapping[str, object], *, root: Path, candidate_id: str, baseline_sha256: str
) -> tuple[dict[str, object], dict[str, Path]]:
    """Fail closed on the v3 source-cue KEEP/REPLACE/DROP mapping."""

    pin = document["operator_text_full_ownership"]
    assert isinstance(pin, Mapping)
    lanes = document.get("operator_truth_lanes")
    lane_keys = {"schema_version", "release_truth", "pipeline_diagnostic", "decision_ledger", "diff_receipt"}
    if not isinstance(lanes, Mapping) or set(lanes) != lane_keys or lanes.get("schema_version") != OPERATOR_TRUTH_LANES_V2_SCHEMA:
        raise ReviewedSubtitleBaselineRegistryError("operator v3 truth lanes are invalid")
    release = lanes.get("release_truth")
    if not isinstance(release, Mapping) or set(release) != {"srt_sha256"} or _clean_sha256(release.get("srt_sha256")) != baseline_sha256:
        raise ReviewedSubtitleBaselineRegistryError("operator v3 release truth lane sha256 drift")
    pipeline_path, pipeline_sha, pipeline_bytes = _read_hash_bound_sibling(lanes.get("pipeline_diagnostic"), root=root, label="operator v3 pipeline diagnostic SRT")
    ledger_path, ledger_sha, ledger_bytes = _read_hash_bound_sibling(lanes.get("decision_ledger"), root=root, label="operator v3 decision ledger")
    diff_path, diff_sha, diff_bytes = _read_hash_bound_sibling(lanes.get("diff_receipt"), root=root, label="operator v3 diagnostic diff")
    try:
        pipeline = parse_srt_cues(pipeline_bytes.decode("utf-8"))
        release_cues = parse_srt_cues((root / str(document["path"])).read_text(encoding="utf-8"))
        ledger = json.loads(ledger_bytes.decode("utf-8"))
        diff = json.loads(diff_bytes.decode("utf-8"))
    except (UnicodeDecodeError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewedSubtitleBaselineRegistryError("operator v3 truth-lane input is invalid") from exc
    ledger_keys = {"schema_version", "candidate_id", "report_scope", "pipeline_srt_sha256", "operator_authority", "cue_decisions"}
    diff_keys = {"schema_version", "candidate_id", "pipeline_srt_sha256", "release_truth_srt_sha256", "decision_ledger_sha256", "cue_count", "changed_cue_count", "source_cue_count", "release_cue_count", "operator_drop_cue_count", "rows"}
    if (
        not isinstance(ledger, Mapping) or set(ledger) != ledger_keys
        or ledger.get("schema_version") != "operator-reviewed-subtitle-decisions.v3"
        or ledger.get("candidate_id") != candidate_id or ledger.get("report_scope") != "EXHAUSTIVE"
        or str(ledger.get("pipeline_srt_sha256") or "").removeprefix("sha256:") != pipeline_sha
        or not isinstance(ledger.get("cue_decisions"), list)
        or not isinstance(diff, Mapping) or set(diff) != diff_keys
        or diff.get("schema_version") != OPERATOR_DIAGNOSTIC_DIFF_V2_SCHEMA
        or diff.get("candidate_id") != candidate_id or diff.get("pipeline_srt_sha256") != pipeline_sha
        or diff.get("release_truth_srt_sha256") != baseline_sha256 or diff.get("decision_ledger_sha256") != ledger_sha
        or not isinstance(diff.get("rows"), list)
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator v3 truth-lane contract is invalid")
    authority = _require_operator_authority(ledger.get("operator_authority"), label="operator v3 decision ledger authority")
    decisions = ledger["cue_decisions"]
    rows = diff["rows"]
    if len(decisions) != len(pipeline) or len(rows) != len(pipeline) or not pipeline or not release_cues:
        raise ReviewedSubtitleBaselineRegistryError("operator v3 cue coverage is invalid")
    release_cursor = exact_count = freeze_count = drop_count = changed_count = 0
    def row_authority(value: object) -> dict[str, str]:
        return authority if value == "LEDGER_OPERATOR_AUTHORITY" else _require_operator_authority(value, label="operator v3 cue decision")
    for ordinal, (source, decision, row) in enumerate(zip(pipeline, decisions, rows, strict=True), start=1):
        common = {"cue", "disposition"}
        if not isinstance(decision, Mapping) or not isinstance(row, Mapping) or not common.issubset(decision):
            raise ReviewedSubtitleBaselineRegistryError("operator v3 cue row is invalid")
        before = str(source.text).strip()
        if (
            decision.get("cue") != ordinal
        ):
            raise ReviewedSubtitleBaselineRegistryError("operator v3 source binding drift")
        disposition = decision.get("disposition")
        expected = {"cue": ordinal, "source_index": str(source.index), "start_ms": source.start_ms, "end_ms": source.end_ms, "pipeline_text": before, "disposition": disposition}
        if disposition == "OPERATOR_DROP":
            if set(decision) != common | {"decision_authority", "drop_reason"} or not isinstance(decision.get("drop_reason"), str) or not decision["drop_reason"].strip():
                raise ReviewedSubtitleBaselineRegistryError("operator drop row is invalid")
            expected.update({"release_cue_index": None, "release_truth_text": None, "decision_authority": row_authority(decision.get("decision_authority")), "drop_reason": decision["drop_reason"].strip()})
            drop_count += 1
        else:
            if release_cursor >= len(release_cues):
                raise ReviewedSubtitleBaselineRegistryError("operator v3 release grid is incomplete")
            target = release_cues[release_cursor]
            after = str(target.text).strip()
            if str(target.index) != str(release_cursor + 1) or (target.start_ms, target.end_ms) != (source.start_ms, source.end_ms) or not after:
                raise ReviewedSubtitleBaselineRegistryError("operator v3 release grid drift")
            expected.update({"release_cue_index": release_cursor + 1, "release_truth_text": after})
            if disposition == "OPERATOR_UNCHANGED_FREEZE":
                if set(decision) != common or before != after:
                    raise ReviewedSubtitleBaselineRegistryError("operator v3 unchanged freeze is invalid")
                freeze_count += 1
            elif disposition == "OPERATOR_EXACT_TEXT":
                if set(decision) != common | {"release_text", "decision_authority"} or decision.get("release_text") != after:
                    raise ReviewedSubtitleBaselineRegistryError("operator v3 exact text is invalid")
                expected["decision_authority"] = row_authority(decision.get("decision_authority"))
                exact_count += 1
            else:
                raise ReviewedSubtitleBaselineRegistryError("operator v3 disposition is invalid")
            if before != after:
                changed_count += 1
            release_cursor += 1
        if dict(row) != expected:
            raise ReviewedSubtitleBaselineRegistryError("operator v3 diagnostic diff drift")
    expected_pin = {"schema_version", "baseline_sha256", "pipeline_srt_sha256", "decision_ledger_sha256", "diagnostic_diff_sha256", "operator_authority", "source_cue_count", "release_cue_count", "changed_cue_count", "operator_exact_text_cue_count", "operator_unchanged_freeze_cue_count", "operator_drop_cue_count", "speaker_authority"}
    if (
        set(pin) != expected_pin or pin.get("schema_version") != OPERATOR_TEXT_PIN_V3
        or _clean_sha256(pin.get("baseline_sha256")) != baseline_sha256
        or _clean_sha256(pin.get("pipeline_srt_sha256")) != pipeline_sha
        or _clean_sha256(pin.get("decision_ledger_sha256")) != ledger_sha
        or _clean_sha256(pin.get("diagnostic_diff_sha256")) != diff_sha
        or _require_operator_authority(pin.get("operator_authority"), label="operator v3 pin authority") != authority
        or not _valid_speaker_authority(
            candidate_id=candidate_id, value=pin.get("speaker_authority")
        )
        or (pin.get("source_cue_count"), pin.get("release_cue_count"), pin.get("changed_cue_count"), pin.get("operator_exact_text_cue_count"), pin.get("operator_unchanged_freeze_cue_count"), pin.get("operator_drop_cue_count")) != (len(pipeline), len(release_cues), changed_count + drop_count, exact_count, freeze_count, drop_count)
        or (diff.get("source_cue_count"), diff.get("release_cue_count"), diff.get("cue_count"), diff.get("changed_cue_count"), diff.get("operator_drop_cue_count")) != (len(pipeline), len(release_cues), len(release_cues), changed_count + drop_count, drop_count)
        or not (exact_count + drop_count) or release_cursor != len(release_cues)
    ):
        raise ReviewedSubtitleBaselineRegistryError("operator v3 pin coverage is invalid")
    return (
        {"schema_version": OPERATOR_TRUTH_LANES_V2_SCHEMA, "release_truth": {"srt_sha256": baseline_sha256},
         "pipeline_diagnostic": {"path": str(pipeline_path), "sha256": pipeline_sha},
         "decision_ledger": {"path": str(ledger_path), "sha256": ledger_sha},
         "diff_receipt": {"path": str(diff_path), "sha256": diff_sha}},
        {"pipeline_diagnostic": pipeline_path, "decision_ledger": ledger_path, "diff_receipt": diff_path},
    )


def load_candidate_reviewed_subtitle_baseline(
    root: Path,
    candidate_id: str,
    *,
    repo_root: Path = _REPO_ROOT,
) -> ReviewedSubtitleBaseline | None:
    """Load one candidate baseline, returning ``None`` only when absent."""

    if not _CANDIDATE_ID_RX.fullmatch(str(candidate_id or "")):
        raise ReviewedSubtitleBaselineRegistryError(
            "unsafe candidate id for reviewed subtitle baseline"
        )
    root = _lexical_absolute(root)
    manifest = root / f"{candidate_id}.subtitle-baseline.v1.json"
    if not manifest.exists():
        return None
    manifest = _regular_canonical_file(
        manifest, root=root, label="candidate subtitle baseline manifest"
    )
    try:
        manifest_bytes = manifest.read_bytes()
        document = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline manifest is invalid JSON"
        ) from exc
    if not isinstance(document, Mapping):
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline manifest must be an object"
        )
    if document.get("registry_schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline registry schema is unsupported"
        )
    if document.get("candidate_id") != candidate_id:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline id does not match its canonical filename"
        )
    schema_version = document.get("schema_version")
    if schema_version not in BASELINE_SCHEMA_VERSIONS:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline producer schema is unsupported"
        )
    if document.get("mode") != BASELINE_MODE:
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline mode is unsupported"
        )
    raw_path = _required_text(document.get("path"), label="baseline path")
    relative = Path(raw_path)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != raw_path:
        raise ReviewedSubtitleBaselineRegistryError("baseline path must be a sibling filename")
    baseline = _regular_canonical_file(
        root / relative,
        root=root,
        label="candidate reviewed subtitle baseline",
    )
    expected_match = _SHA256_RX.fullmatch(str(document.get("sha256") or ""))
    if expected_match is None:
        raise ReviewedSubtitleBaselineRegistryError("candidate subtitle baseline sha256 is invalid")
    actual_sha256 = _sha256(baseline)
    if actual_sha256 != expected_match.group(1):
        raise ReviewedSubtitleBaselineRegistryError(
            "candidate subtitle baseline sha256 does not match"
        )
    _required_text(document.get("authority"), label="baseline authority")
    operator_truth_lanes, _operator_truth_lane_paths = _validate_operator_truth_lanes(
        document,
        root=root,
        candidate_id=candidate_id,
        baseline_sha256=actual_sha256,
    )
    # A full-ownership lane changes what provider/reviewer work may be skipped.
    # On its canonical repository path every byte in that lane must therefore
    # come from HEAD (or the deployed authority manifest), never an untracked
    # local reviewed-SRT/ledger injection.
    canonical_root = _lexical_absolute(repo_root) / _BASELINE_REPO_DIRECTORY
    if operator_truth_lanes and root == canonical_root:
        try:
            for path in (manifest, baseline, *_operator_truth_lane_paths.values()):
                require_repository_asset_authority(
                    repo_root=repo_root,
                    relative_path=path.relative_to(repo_root),
                    observed_bytes=path.read_bytes(),
                )
        except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "operator truth lanes are not sealed by the active repository"
            ) from exc

    if schema_version == "subtitle-redelivery-baseline.v2":
        if not isinstance(document.get("exact_interval_replay", False), bool):
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline exact interval replay flag must be boolean"
            )
        basename = _required_text(
            document.get("source_recording_basename"),
            label="baseline source recording basename",
        )
        if Path(basename).name != basename:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline source recording basename must not contain a path"
            )
        if _SHA256_RX.fullmatch(str(document.get("source_sha256") or "")) is None:
            raise ReviewedSubtitleBaselineRegistryError("baseline source sha256 is invalid")
        start = document.get("absolute_source_start_ms")
        end = document.get("absolute_source_end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline absolute source interval is invalid"
            )
        try:
            time_domain = operator_v3_time_domain(document)
        except RedeliveryTimeDomainError as exc:
            raise ReviewedSubtitleBaselineRegistryError(str(exc)) from exc
        if time_domain is not None:
            try:
                baseline_cues = parse_srt_cues(baseline.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise ReviewedSubtitleBaselineRegistryError(
                    "baseline time-domain SRT is invalid"
                ) from exc
            interval_duration_ms = end - start
            if not baseline_cues or any(
                cue.start_ms < 0
                or cue.end_ms <= cue.start_ms
                or cue.end_ms > interval_duration_ms
                for cue in baseline_cues
            ):
                raise ReviewedSubtitleBaselineRegistryError(
                    "REDELIVERY_BASELINE_TIME_DOMAIN_GEOMETRY_INVALID"
                )
        projection_mode = document.get(PROJECTION_MODE_CONFIG_KEY)
        if projection_mode is not None and projection_mode != PROJECTION_MODE:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline terminal projection mode is unsupported"
            )
        if projection_mode is not None and document.get("exact_interval_replay") is not True:
            raise ReviewedSubtitleBaselineRegistryError(
                "baseline terminal projection requires exact interval replay"
            )

    exact_interval_authority_path: Path | None = None
    exact_interval_authority: dict[str, object] | None = None
    exact_interval_reference = document.get(EXACT_INTERVAL_REFERENCE_CONFIG_KEY)
    if exact_interval_reference is not None:
        if schema_version != "subtitle-redelivery-baseline.v2":
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority requires baseline v2"
            )
        if not isinstance(exact_interval_reference, Mapping):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority reference is invalid"
            )
        if (
            exact_interval_reference.get("schema_version")
            != EXACT_INTERVAL_REFERENCE_SCHEMA_VERSION
        ):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority reference schema is unsupported"
            )
        repo_relative = _required_text(
            exact_interval_reference.get("path"),
            label="reviewed exact interval authority path",
        )
        expected_repo_relative = (
            "assets/lidousha/reviewed_exact_source_intervals/"
            f"{candidate_id}.reviewed-exact-source-interval.v1.json"
        )
        if repo_relative != expected_repo_relative:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority path is not canonical"
            )
        trusted_repo_root, authority_root = _require_trusted_exact_asset_roots(
            root,
            repo_root=repo_root,
        )
        try:
            manifest_relative = manifest.relative_to(trusted_repo_root)
        except ValueError as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval manifest escapes the trusted repository"
            ) from exc
        exact_interval_authority_path = _regular_canonical_file(
            trusted_repo_root / repo_relative,
            root=authority_root,
            label="reviewed exact interval authority",
        )
        expected_authority_match = _SHA256_RX.fullmatch(
            str(exact_interval_reference.get("sha256") or "")
        )
        if expected_authority_match is None:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority sha256 is invalid"
            )
        try:
            require_repository_asset_authority(
                repo_root=trusted_repo_root,
                relative_path=manifest_relative,
                observed_bytes=manifest_bytes,
            )
            authority_bytes = exact_interval_authority_path.read_bytes()
            require_repository_asset_authority(
                repo_root=trusted_repo_root,
                relative_path=Path(repo_relative),
                observed_bytes=authority_bytes,
            )
        except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority is not sealed by the active repository"
            ) from exc
        if hashlib.sha256(authority_bytes).hexdigest() != expected_authority_match.group(1):
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority sha256 does not match"
            )
        try:
            authority_value = json.loads(authority_bytes.decode("utf-8"))
            exact_interval_authority = validate_runtime_authority(authority_value)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ReviewedExactSourceIntervalError,
        ) as exc:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority document is invalid"
            ) from exc
        if exact_interval_authority.get("candidate_id") != candidate_id:
            raise ReviewedSubtitleBaselineRegistryError(
                "reviewed exact interval authority candidate does not match"
            )

    config = dict(document)
    config.pop("registry_schema_version", None)
    config.pop("candidate_id", None)
    config["path"] = str(baseline)
    config["sha256"] = actual_sha256
    if operator_truth_lanes:
        config["operator_truth_lanes"] = operator_truth_lanes
    if exact_interval_authority_path is not None:
        config[EXACT_INTERVAL_REFERENCE_CONFIG_KEY] = {
            **dict(config[EXACT_INTERVAL_REFERENCE_CONFIG_KEY]),
            "path": str(exact_interval_authority_path),
        }
    return ReviewedSubtitleBaseline(
        config=config,
        manifest_path=manifest,
        baseline_path=baseline,
        operator_truth_lane_paths=tuple(_operator_truth_lane_paths.values()),
        exact_interval_authority_path=exact_interval_authority_path,
        exact_interval_authority=exact_interval_authority,
    )
