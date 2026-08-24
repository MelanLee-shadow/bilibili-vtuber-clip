"""Hash-bound, fail-closed receipt for the final perceptual media review.

The automated package audit proves structural and deterministic contracts.  It
cannot prove that the named human or explicitly delegated root reviewer watched
the *final burned bytes* and checked the semantic claims visible in the cover.
This module validates that independent receipt without trusting filenames, a
nearby manifest, an arbitrary checklist, or a bare ``PASS``.  It still cannot
manufacture the real viewing action: callers must not issue a receipt until the
declared reviewer has actually completed the bound checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from . import final_human_review_evidence as _review_evidence
from .final_human_review_evidence import (
    FinalHumanReviewEvidenceError,
)
from .final_human_review_evidence import validate_bound_review_evidence
from .cover_route_evidence import validate_cover_route_decision
from .channel_profile import load_channel_profile as _load_channel_profile
from .story_contract import cover_story_contract_binding_matches
from .qixi_corrected_package_finalization import (
    QixiCorrectedPackageError,
    validate_manifest_bound_applied_receipt,
)


# 终审回执 schema 是包内持久证据词汇（旧包哈希兼容），保留 lidousha- 拼写。
SCHEMA_VERSION = "lidousha-final-human-review.v2"
REVIEW_EVIDENCE_SCHEMA_VERSION = _review_evidence.SCHEMA_VERSION
_CHECK_ANCHORS = _review_evidence.CHECK_ANCHORS
REVIEW_SCOPE = "same_bv_repair"
ACCEPTED_STATUS = "ACCEPTED_FOR_SAME_BV"
ROOT = Path(__file__).resolve().parents[2]

_CHANNEL_PROFILE = _load_channel_profile(ROOT)
# 契约文件是部署本地配置（非包内持久证据）：schema 与路径按 profile 派生，
# 默认 profile 字节等价。
REVIEW_CONTRACT_SCHEMA = (
    f"{_CHANNEL_PROFILE.profile_id}-final-media-review-contracts.v1"
)
FINAL_MEDIA_REVIEW_CONTRACT_PATH = _CHANNEL_PROFILE.asset_file(
    "final_media_review_contracts"
)
_SHA256_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANDIDATE_RX = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "status",
        "reviewer_kind",
        "reviewed_by",
        "reviewed_at",
        "approval_quote",
        "review_contract_sha256",
        "package_evidence",
        "items",
    }
)
_ITEM_FIELDS = frozenset(
    {
        "candidate_id",
        "reviewed_title",
        "artifacts",
        "record",
        "publication_target",
        "checks",
        "subtitle_review_points",
        "cover_story_claims",
    }
)
_ARTIFACT_FIELDS = ("video", "subtitle", "cover")
_ARTIFACT_BINDING_FIELDS = frozenset({"path", "sha256"})
_PACKAGE_EVIDENCE_FIELDS = frozenset(
    {"review_manifest", "package_audit", "review_evidence"}
)
_PACKAGE_ATTESTED_EVIDENCE_FIELDS = frozenset(
    {"review_manifest", "package_audit"}
)
_REVIEW_EVIDENCE_FILE_BINDING_FIELDS = frozenset(
    {"path", "sha256", "bytes"}
)
_PUBLICATION_TARGET_FIELDS = frozenset(
    {
        "candidate_id",
        "bvid",
        "aid",
        "cid",
        "final_title",
        "authority_sha256",
    }
)
_REVIEWER_KINDS = frozenset(
    {"human_owner", "human_delegate", "delegated_root_agent"}
)
# delegated_root_agent 的保留身份集：曾经只有 Codex root；2026-08-01 起
# Claude root 同为受 Ivan 委托的根代理（1013 same-BV 修复首次由其执行）。
_RESERVED_REVIEWER_IDENTITIES = {
    "human_owner": ("Ivan",),
    "delegated_root_agent": ("Codex root", "Claude root"),
}
_CHECK_FIELDS = (
    "final_burned_full_playback",
    "subtitle_audio",
    "silence_hallucination",
    "boundary_closure",
    "title_story",
    "cover_identity",
    "cover_story",
    "intro_timing",
)
_CHECK_RESULT_FIELDS = frozenset({"status", "evidence"})
_SUBTITLE_REVIEW_POINT_FIELDS = frozenset(
    {
        "point_id",
        "final_video_start_ms",
        "final_video_end_ms",
        "expectation",
        "status",
        "evidence",
    }
)
_SUBTITLE_CONTRACT_POINT_FIELDS = frozenset(
    {
        "point_id",
        "final_video_start_ms",
        "final_video_end_ms",
        "expectation",
    }
)
_CLAIM_FIELDS = frozenset(
    {"claim", "presentation", "status", "evidence"}
)
_CLAIM_PRESENTATIONS = frozenset({"SOURCE_FRAME", "COVER_TEXT"})
_MANIFEST_ARTIFACT_FIELDS = {
    "video": "video",
    "subtitle": "subtitle_srt",
    "cover": "cover",
}
_BVID_RX = re.compile(r"BV[0-9A-Za-z]{10}\Z")


class FinalHumanReviewError(ValueError):
    """The human-review receipt is incomplete, stale, or ambiguous."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        message = reason_code if not detail else f"{reason_code}: {detail}"
        super().__init__(message)


def _reserved_reviewer_kind(value: str) -> str | None:
    key = " ".join(
        unicodedata.normalize("NFKC", value).split()
    ).casefold()
    if (
        key == "ivan"
        or key.startswith("ivan ")
        or key.startswith("ivan本人")
    ):
        return "human_owner"
    if key == "codex root" or key.startswith("codex root "):
        return "delegated_root_agent"
    if key == "claude root" or key.startswith("claude root "):
        return "delegated_root_agent"
    return None


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    reason_code: str,
) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual, key=repr)
        extra = sorted(actual - set(expected), key=repr)
        raise FinalHumanReviewError(
            reason_code,
            f"missing={missing!r} extra={extra!r}",
        )


def _candidate_id(value: object, *, source: str) -> str:
    candidate_id = str(value or "")
    if (
        not isinstance(value, str)
        or _CANDIDATE_RX.fullmatch(candidate_id) is None
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CANDIDATE_ID_INVALID", source
        )
    return candidate_id


def _nonempty_string(
    value: object, *, reason_code: str, source: str
) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise FinalHumanReviewError(reason_code, source)
    return normalized


def _portable_path(value: object, *, source: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_PATH_INVALID", source
        )
    if "\x00" in value or "\\" in value:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_PATH_INVALID", source
        )
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or value != pure.as_posix()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_PATH_INVALID", source
        )
    return pure.as_posix()


def _package_directory(package_root: Path) -> Path:
    try:
        metadata = package_root.lstat()
        resolved = package_root.resolve(strict=True)
    except OSError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ROOT_INVALID"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not resolved.is_dir()
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ROOT_INVALID"
        )
    return resolved


def _regular_package_file(root: Path, relative_path: str) -> Path:
    cursor = root
    parts = PurePosixPath(relative_path).parts
    try:
        for index, part in enumerate(parts):
            cursor /= part
            metadata = cursor.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
                    relative_path,
                )
            if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
                    relative_path,
                )
        if not stat.S_ISREG(metadata.st_mode):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
                relative_path,
            )
        resolved = cursor.resolve(strict=True)
    except FinalHumanReviewError:
        raise
    except OSError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
            relative_path,
        ) from exc
    if not resolved.is_relative_to(root):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
            relative_path,
        )
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID", str(path)
        ) from exc
    return "sha256:" + digest.hexdigest()


def _json_object(path: Path, *, source: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_EVIDENCE_JSON_INVALID", source
        ) from exc
    if not isinstance(value, dict):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_EVIDENCE_JSON_INVALID", source
        )
    return value


def _review_contracts() -> tuple[
    str, dict[str, list[dict[str, object]]]
]:
    path = FINAL_MEDIA_REVIEW_CONTRACT_PATH
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CONTRACT_UNREADABLE", str(path)
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {
            "schema_version",
            "authority",
            "contracts",
        }
        or payload.get("schema_version") != REVIEW_CONTRACT_SCHEMA
        or not isinstance(payload.get("authority"), str)
        or not str(payload.get("authority") or "").strip()
        or not isinstance(payload.get("contracts"), list)
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CONTRACT_INVALID", str(path)
        )
    contracts: dict[str, list[dict[str, object]]] = {}
    for index, raw_contract in enumerate(payload["contracts"]):
        if (
            not isinstance(raw_contract, Mapping)
            or set(raw_contract)
            != {"candidate_id", "subtitle_review_points"}
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CONTRACT_INVALID",
                f"contracts[{index}]",
            )
        candidate_id = _candidate_id(
            raw_contract.get("candidate_id"),
            source=f"contracts[{index}]",
        )
        raw_points = raw_contract.get("subtitle_review_points")
        if (
            candidate_id in contracts
            or not isinstance(raw_points, list)
            or not raw_points
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CONTRACT_INVALID",
                candidate_id,
            )
        points: list[dict[str, object]] = []
        point_ids: set[str] = set()
        for point_index, raw_point in enumerate(raw_points):
            if not isinstance(raw_point, Mapping):
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_CONTRACT_INVALID",
                    f"{candidate_id}[{point_index}]",
                )
            _require_exact_fields(
                raw_point,
                _SUBTITLE_CONTRACT_POINT_FIELDS,
                reason_code=(
                    "FINAL_HUMAN_REVIEW_CONTRACT_INVALID"
                ),
            )
            point_id = _candidate_id(
                raw_point.get("point_id"),
                source=f"{candidate_id}[{point_index}].point_id",
            )
            start_ms = raw_point.get("final_video_start_ms")
            end_ms = raw_point.get("final_video_end_ms")
            expectation = _nonempty_string(
                raw_point.get("expectation"),
                reason_code="FINAL_HUMAN_REVIEW_CONTRACT_INVALID",
                source=f"{candidate_id}[{point_index}].expectation",
            )
            if (
                point_id in point_ids
                or not isinstance(start_ms, int)
                or isinstance(start_ms, bool)
                or not isinstance(end_ms, int)
                or isinstance(end_ms, bool)
                or start_ms < 0
                or end_ms <= start_ms
            ):
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_CONTRACT_INVALID",
                    f"{candidate_id}[{point_index}]",
                )
            point_ids.add(point_id)
            points.append(
                {
                    "point_id": point_id,
                    "final_video_start_ms": start_ms,
                    "final_video_end_ms": end_ms,
                    "expectation": expectation,
                }
            )
        contracts[candidate_id] = points
    return "sha256:" + hashlib.sha256(raw).hexdigest(), contracts


def _normalized_file_binding(
    raw_binding: object,
    *,
    package_root: Path,
    source: str,
) -> tuple[dict[str, str], Path]:
    if not isinstance(raw_binding, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_FILE_BINDING_INVALID", source
        )
    _require_exact_fields(
        raw_binding,
        _ARTIFACT_BINDING_FIELDS,
        reason_code="FINAL_HUMAN_REVIEW_FILE_BINDING_FIELDS_INVALID",
    )
    relative_path = _portable_path(
        raw_binding.get("path"), source=f"{source}.path"
    )
    expected_sha256 = raw_binding.get("sha256")
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_RX.fullmatch(expected_sha256) is None
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_FILE_HASH_INVALID", source
        )
    path = _regular_package_file(package_root, relative_path)
    if _sha256(path) != expected_sha256:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_FILE_HASH_MISMATCH", source
        )
    return {
        "path": relative_path,
        "sha256": expected_sha256,
    }, path


def _normalized_package_evidence(
    raw_evidence: object,
    *,
    package_root: Path,
    package_attestation: object,
    review_manifest: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    if not isinstance(raw_evidence, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_EVIDENCE_INVALID"
        )
    _require_exact_fields(
        raw_evidence,
        _PACKAGE_EVIDENCE_FIELDS,
        reason_code=(
            "FINAL_HUMAN_REVIEW_PACKAGE_EVIDENCE_FIELDS_INVALID"
        ),
    )
    if not isinstance(package_attestation, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_INVALID"
        )
    package_root_text = package_attestation.get("package_root")
    if (
        not isinstance(package_root_text, str)
        or Path(package_root_text).resolve() != package_root
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_INVALID",
            "package_root",
        )

    normalized: dict[str, dict[str, object]] = {}
    for key in sorted(_PACKAGE_ATTESTED_EVIDENCE_FIELDS):
        binding, path = _normalized_file_binding(
            raw_evidence.get(key),
            package_root=package_root,
            source=f"package_evidence.{key}",
        )
        attested = package_attestation.get(key)
        if (
            not isinstance(attested, Mapping)
            or set(attested) != {"path", "sha256", "bytes"}
            or Path(str(attested.get("path") or "")).resolve() != path
            or attested.get("sha256") != binding["sha256"][7:]
            or attested.get("bytes") != path.stat().st_size
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_MISMATCH",
                key,
            )
        normalized[key] = binding

    raw_review_evidence = raw_evidence.get("review_evidence")
    if not isinstance(raw_review_evidence, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_INVALID"
        )
    _require_exact_fields(
        raw_review_evidence,
        _REVIEW_EVIDENCE_FILE_BINDING_FIELDS,
        reason_code=(
            "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_INVALID"
        ),
    )
    review_evidence_path = _portable_path(
        raw_review_evidence.get("path"),
        source="package_evidence.review_evidence.path",
    )
    review_evidence_sha256 = raw_review_evidence.get("sha256")
    review_evidence_bytes = raw_review_evidence.get("bytes")
    if (
        not isinstance(review_evidence_sha256, str)
        or _SHA256_RX.fullmatch(review_evidence_sha256) is None
        or not isinstance(review_evidence_bytes, int)
        or isinstance(review_evidence_bytes, bool)
        or review_evidence_bytes < 0
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_INVALID"
        )
    review_evidence_file = _regular_package_file(
        package_root, review_evidence_path
    )
    if (
        _sha256(review_evidence_file) != review_evidence_sha256
        or review_evidence_file.stat().st_size != review_evidence_bytes
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_MISMATCH"
        )
    normalized["review_evidence"] = {
        "path": review_evidence_path,
        "sha256": review_evidence_sha256,
        "bytes": review_evidence_bytes,
    }

    loaded_review = _json_object(
        _regular_package_file(
            package_root,
            normalized["review_manifest"]["path"],
        ),
        source="package_evidence.review_manifest",
    )
    loaded_audit = _json_object(
        _regular_package_file(
            package_root,
            normalized["package_audit"]["path"],
        ),
        source="package_evidence.package_audit",
    )
    if loaded_review != dict(review_manifest):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_OBJECT_MISMATCH"
        )
    if (
        loaded_audit.get("passed") is not True
        or loaded_audit.get("blocking_issue_count") != 0
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_AUDIT_NOT_PASS"
        )
    return normalized


def _absolute_attested_file(
    value: object,
    *,
    label: str,
) -> tuple[dict[str, object], Path]:
    if not isinstance(value, Mapping) or set(value) != {
        "path",
        "sha256",
        "bytes",
    }:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ATTESTED_FILE_INVALID", label
        )
    path_text = value.get("path")
    expected_sha = value.get("sha256")
    expected_bytes = value.get("bytes")
    path = Path(str(path_text or ""))
    if (
        not isinstance(path_text, str)
        or not path.is_absolute()
        or not path.is_file()
        or str(path.resolve()) != path_text
        or not isinstance(expected_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
        or not isinstance(expected_bytes, int)
        or isinstance(expected_bytes, bool)
        or expected_bytes < 0
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ATTESTED_FILE_INVALID", label
        )
    actual_sha = _sha256(path)[7:]
    if actual_sha != expected_sha:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ATTESTED_FILE_HASH_DRIFT", label
        )
    if path.stat().st_size != expected_bytes:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ATTESTED_FILE_SIZE_DRIFT", label
        )
    return {
        "path": path_text,
        "sha256": expected_sha,
        "bytes": expected_bytes,
    }, path


def replay_final_human_review_attestation(
    manifest: Mapping[str, object],
) -> dict[str, object]:
    """Replay the exact package/review/receipt closure for same-BV repair."""

    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_INVALID"
        )
    package_root_text = attestation.get("package_root")
    package_root = Path(str(package_root_text or ""))
    if (
        not isinstance(package_root_text, str)
        or not package_root.is_absolute()
        or not package_root.is_dir()
        or str(package_root.resolve()) != package_root_text
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ATTESTATION_INVALID",
            "package_root",
        )
    # C1's line947-authorized fastlane is deliberately not a generic
    # perceptual receipt.  This exact schema pair is the only alternate lane.
    if "c1_technical_receipt" in attestation:
        entries: dict[str, dict[str, object]] = {}
        paths: dict[str, Path] = {}
        for key in ("review_manifest", "package_audit", "c1_technical_receipt"):
            entry, path = _absolute_attested_file(attestation.get(key), label=key)
            if not path.is_relative_to(package_root):
                raise FinalHumanReviewError("FINAL_HUMAN_REVIEW_ATTESTED_FILE_INVALID", key)
            entries[key], paths[key] = entry, path
        review = _json_object(paths["review_manifest"], source="review_manifest")
        if review.get("schema_version") != "fastlane-c1-formal-private-review-manifest.v1":
            raise FinalHumanReviewError("FINAL_HUMAN_REVIEW_C1_FORMAL_MANIFEST_REQUIRED")
        from src.autoslice.fastlane_c1_technical_receipt import C1TechnicalReceiptError, validate_completed
        try:
            validate_completed(_json_object(paths["c1_technical_receipt"], source="c1_technical_receipt"), package_root, paths["package_audit"])
        except C1TechnicalReceiptError as exc:
            raise FinalHumanReviewError("FINAL_HUMAN_REVIEW_C1_TECHNICAL_RECEIPT_INVALID", str(exc)) from exc
        return {"package_root": package_root_text, **entries}
    entries: dict[str, dict[str, object]] = {}
    paths: dict[str, Path] = {}
    for key in (
        "review_manifest",
        "package_audit",
        "final_human_review",
    ):
        entry, path = _absolute_attested_file(
            attestation.get(key), label=key
        )
        if not path.is_relative_to(package_root):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_ATTESTED_FILE_INVALID", key
            )
        entries[key] = entry
        paths[key] = path
    review_manifest = _json_object(
        paths["review_manifest"], source="review_manifest"
    )
    receipt = _json_object(
        paths["final_human_review"], source="final_human_review"
    )
    validate_final_human_review(
        receipt,
        package_root,
        review_manifest,
        attestation,
    )
    return {
        "package_root": package_root_text,
        **entries,
    }


def final_human_review_attestation_problems(
    manifest: Mapping[str, object],
) -> list[str]:
    """Return upload-manifest validation problems without weakening errors."""

    required = isinstance(
        manifest.get("recovery_publication_authority"), Mapping
    )
    attestation = manifest.get("package_attestation")
    receipt_present = isinstance(attestation, Mapping) and (
        "final_human_review" in attestation
    )
    if not required and not receipt_present:
        return []
    try:
        replay_final_human_review_attestation(manifest)
    except FinalHumanReviewError as exc:
        detail = f" ({exc.detail})" if exc.detail else ""
        return [
            "final perceptual review receipt rejected: "
            f"{exc.reason_code}{detail}"
        ]
    return []


def ordinary_upload_problems(
    manifest: Mapping[str, object],
) -> list[str]:
    if not isinstance(
        manifest.get("recovery_publication_authority"), Mapping
    ):
        return []
    return [
        "same-BV recovery manifests cannot use upload; use "
        "repair-plan/repair-run so no new BV can be created"
    ]


def attach_final_human_review(
    manifest: dict[str, object],
    receipt_argument: object,
    *,
    season_ids: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """Attach the receipt and exact section only for a recovery manifest."""

    argument = str(receipt_argument or "").strip()
    recovery = isinstance(
        manifest.get("recovery_publication_authority"), Mapping
    )
    if not recovery:
        return (
            [
                "--final-human-review is only valid for a same-BV "
                "recovery manifest"
            ]
            if argument
            else []
        )
    if not argument:
        return [
            "same-BV recovery manifest requires --final-human-review"
        ]
    receipt_path = Path(argument)
    if not receipt_path.is_file():
        return [f"final perceptual review receipt missing: {receipt_path}"]
    attestation = manifest.get("package_attestation")
    season = manifest.get("season")
    if not isinstance(attestation, dict):
        return ["same-BV recovery manifest has no package_attestation"]
    if not isinstance(season, dict):
        return ["same-BV recovery manifest requires a season binding"]
    lane = season.get("lane")
    exact_ids = season_ids.get(str(lane or ""))
    if not isinstance(exact_ids, Mapping):
        return ["same-BV recovery manifest has no exact season IDs"]
    season.update(exact_ids)
    resolved = receipt_path.resolve()
    attestation["final_human_review"] = {
        "path": str(resolved),
        "sha256": _sha256(resolved)[7:],
        "bytes": resolved.stat().st_size,
    }
    return []


def _declared_candidate_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for candidate in value:
        if (
            not isinstance(candidate, str)
            or _CANDIDATE_RX.fullmatch(candidate) is None
            or candidate in normalized
        ):
            return []
        normalized.append(candidate)
    return normalized


def _publication_target(
    authority: object,
    *,
    candidate_id: str,
    title: str,
    source: str,
) -> dict[str, object]:
    if not isinstance(authority, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PUBLICATION_AUTHORITY_INVALID",
            source,
        )
    def _authority_title_binding_ok(
        authority: Mapping[str, object], title: str
    ) -> bool:
        observed = authority.get("observed_public_title")
        if observed == title:
            return True
        # 3573/672 case (Ivan-planned prefix repair): the live title was a
        # manual override missing the 【李豆沙】 prefix; the repair publishes
        # the canonicalized form. Only that exact relationship may differ.
        if authority.get("title_mode") != "ivan_manual_override":
            return False
        if not isinstance(observed, str) or not observed:
            return False
        from src.autoslice.title_policy import canonicalize_publish_title

        return canonicalize_publish_title(observed) == title
    bvid = authority.get("bvid")
    aid = authority.get("aid")
    cid = authority.get("cid")
    authority_sha256 = authority.get("authority_sha256")
    if (
        authority.get("candidate_id") != candidate_id
        or not isinstance(bvid, str)
        or _BVID_RX.fullmatch(bvid) is None
        or not isinstance(aid, int)
        or isinstance(aid, bool)
        or aid <= 0
        or not isinstance(cid, int)
        or isinstance(cid, bool)
        or cid <= 0
        or not isinstance(authority_sha256, str)
        or _SHA256_RX.fullmatch(authority_sha256) is None
        or not _authority_title_binding_ok(authority, title)
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PUBLICATION_AUTHORITY_INVALID",
            source,
        )
    return {
        "candidate_id": candidate_id,
        "bvid": bvid,
        "aid": aid,
        "cid": cid,
        "final_title": title,
        "authority_sha256": authority_sha256,
    }


def _cover_story_claim_authority(
    record: Mapping[str, object],
    *,
    candidate_id: str,
) -> tuple[tuple[str, str], ...]:
    """Project only cover claims that the immutable record can authorize.

    A registered multi-person source reference carries explicit source-frame
    claims. Ordinary host-only screenshot covers intentionally have no such
    identity authority; for those, bind review only to the exact rendered text
    already hash-closed by the record.
    """

    story_contract = record.get("story_contract")
    if not isinstance(story_contract, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_COVER_AUTHORITY_INVALID",
            candidate_id,
        )
    cover_reference = story_contract.get("cover_reference_authority")
    if isinstance(cover_reference, Mapping):
        raw_source_claims = cover_reference.get("source_visible_claims")
        narrative = cover_reference.get("narrative_presentation")
        if (
            not isinstance(raw_source_claims, list)
            or not raw_source_claims
            or len(raw_source_claims) != len(set(raw_source_claims))
            or not all(
                isinstance(claim, str)
                and claim == claim.strip()
                and bool(claim)
                for claim in raw_source_claims
            )
            or not isinstance(narrative, str)
            or not narrative.strip()
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_AUTHORITY_INVALID",
                candidate_id,
            )
        publish_staging = record.get("publish_staging")
        generation = (
            publish_staging.get("cover_generation")
            if isinstance(publish_staging, Mapping)
            else None
        )
        # Historical receipts predate the hash-bound rendered-text surface.
        # Keep them readable under their original narrative claim; every
        # current package carries ``cover_generation`` and must bind the actual
        # rendered lines below instead of upgrading art direction into pixels.
        if generation is None:
            return (
                *((claim, "SOURCE_FRAME") for claim in raw_source_claims),
                (narrative.strip(), "COVER_TEXT"),
            )
        pixels = (
            generation.get("rendered_text_pixels")
            if isinstance(generation, Mapping)
            else None
        )
        rendered_lines = (
            generation.get("rendered_lines")
            if isinstance(generation, Mapping)
            else None
        )
        final_cover_sha256 = (
            generation.get("final_cover_sha256")
            if isinstance(generation, Mapping)
            else None
        )
        artifact_hashes = record.get("artifact_hashes")
        if (
            not isinstance(rendered_lines, list)
            or not rendered_lines
            or len(rendered_lines) != len(set(rendered_lines))
            or not all(
                isinstance(line, str) and line == line.strip() and bool(line)
                for line in rendered_lines
            )
            or not isinstance(pixels, Mapping)
            or pixels.get("status") != "PASS"
            or pixels.get("rendered_text") != "".join(rendered_lines)
            or not isinstance(artifact_hashes, Mapping)
            or not isinstance(final_cover_sha256, str)
            or _SHA256_RX.fullmatch(final_cover_sha256) is None
            or pixels.get("final_cover_sha256") != final_cover_sha256
            or artifact_hashes.get("cover_sha256") != final_cover_sha256
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_AUTHORITY_INVALID",
                candidate_id,
            )
        rendered_text_claim = (
            "封面文字呈现“" + " / ".join(rendered_lines) + "”"
        )
        return (
            *((claim, "SOURCE_FRAME") for claim in raw_source_claims),
            (rendered_text_claim, "COVER_TEXT"),
        )
    if cover_reference is not None:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_COVER_AUTHORITY_INVALID",
            candidate_id,
        )

    publish_staging = record.get("publish_staging")
    generation = (
        publish_staging.get("cover_generation")
        if isinstance(publish_staging, Mapping)
        else None
    )
    route = (
        generation.get("route_decision")
        if isinstance(generation, Mapping)
        else None
    )
    pixels = (
        generation.get("rendered_text_pixels")
        if isinstance(generation, Mapping)
        else None
    )
    artifact_hashes = record.get("artifact_hashes")
    rendered_lines = (
        generation.get("rendered_lines")
        if isinstance(generation, Mapping)
        else None
    )
    final_cover_sha256 = (
        generation.get("final_cover_sha256")
        if isinstance(generation, Mapping)
        else None
    )
    actual_treatment = (
        route.get("actual_treatment")
        if isinstance(route, Mapping)
        else None
    )
    host_only_render_ok = bool(
        isinstance(generation, Mapping)
        and isinstance(route, Mapping)
        and (
            (
                actual_treatment == "screenshot_direct"
                and generation.get("cover_origin") == "SOURCE_SCREENSHOT"
                and generation.get("image_generation_used") is False
                and route.get("image_generation_used") is False
            )
            or (
                actual_treatment == "screenshot_polish"
                and generation.get("cover_origin")
                == "SOURCE_SCREENSHOT_AI_POLISH"
                and generation.get("image_generation_used") is True
                and route.get("image_generation_used") is True
            )
            or (
                actual_treatment == "cpa_redraw"
                and generation.get("cover_origin") == "AI_REDRAW"
                and generation.get("image_generation_used") is True
                and route.get("image_generation_used") is True
                and generation.get("method") == "images.edit"
                and generation.get("model") == "gpt-image-2"
                and generation.get("image_gen_model") == "cpa"
            )
        )
    )
    route_execution_ok = bool(
        isinstance(generation, Mapping)
        and isinstance(route, Mapping)
        and (
            (
                route.get("execution_status") == "READY"
                and route.get("selected_treatment")
                == route.get("actual_treatment")
            )
            or (
                # A subtitle-only same-BV recovery may carry the already
                # published cover byte-for-byte.  Historical v2 routes can
                # legitimately record a screenshot materialization failure
                # followed by an explicit READY_DEGRADED redraw.  The shared
                # route validator verifies that the BLOCKED receipt, execution
                # detail, actual treatment, generation provenance, and (when
                # required) final host-identity witness all agree.  Keep this
                # exception narrow: a fresh/non-carried cover still needs the
                # ordinary READY + selected==actual closure above.
                generation.get("carried_forward_from_published_record") is True
                and generation.get("status") == "REUSED"
                and type(generation.get("reused_cover_candidates")) is int
                and generation.get("reused_cover_candidates") == 1
                and route.get("execution_status") == "READY_DEGRADED"
                and route.get("selected_treatment") == "screenshot_polish"
                and route.get("actual_treatment") == "cpa_redraw"
                and route.get("host_identity_required") is True
                and isinstance(generation.get("story_contract"), Mapping)
                and cover_story_contract_binding_matches(
                    story_contract, generation["story_contract"]
                )
                and (
                    record.get("cover_generation") is None
                    or record.get("cover_generation") == generation
                )
                and validate_cover_route_decision(
                    generation, allow_legacy_v1=False
                )
            )
        )
    )
    if (
        story_contract.get("cover_counterpart_reference_available") is not False
        or story_contract.get("relation_claim_allowed") is not False
        or story_contract.get("cover_fallback_mode")
        not in {"HOST_ONLY_GENERIC", "HOST_ONLY_RELATION_EXPLICIT"}
        or not isinstance(generation, Mapping)
        or not isinstance(route, Mapping)
        or not route_execution_ok
        or not host_only_render_ok
        or route.get("relationship_visual_required") is not False
        or route.get("required_participant_ids") != []
        or route.get("source_visible_participant_ids") != []
        or route.get("source_visibility_authority") != "NO_IDENTITY_AUTHORITY"
        or route.get("final_visibility_authority") != "NOT_REQUIRED"
        or not isinstance(rendered_lines, list)
        or not rendered_lines
        or len(rendered_lines) != len(set(rendered_lines))
        or not all(
            isinstance(line, str) and line == line.strip() and bool(line)
            for line in rendered_lines
        )
        or not isinstance(pixels, Mapping)
        or pixels.get("status") != "PASS"
        or pixels.get("rendered_text") != "".join(rendered_lines)
        or not isinstance(artifact_hashes, Mapping)
        or not isinstance(final_cover_sha256, str)
        or _SHA256_RX.fullmatch(final_cover_sha256) is None
        or pixels.get("final_cover_sha256") != final_cover_sha256
        or artifact_hashes.get("cover_sha256") != final_cover_sha256
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_COVER_AUTHORITY_INVALID",
            candidate_id,
        )
    narrative = "封面文字呈现“" + " / ".join(rendered_lines) + "”"
    return ((narrative, "COVER_TEXT"),)


def _validate_manual_corrected_same_bv_item(
    *,
    item: Mapping[str, object],
    candidate_id: str,
    package_root: Path,
    qixi_repo_root: Path | None,
) -> None:
    """Replay the typed Qixi receipt embedded in one manifest item.

    This narrow gate leaves untyped and legacy manual items on their existing
    final-human path.  A typed item must bind its regular receipt file, bytes,
    and replayed inner object to its candidate.
    """

    try:
        validate_manifest_bound_applied_receipt(
            item,
            candidate_id=candidate_id,
            package_root=package_root,
            repo_root=qixi_repo_root,
        )
    except (
        OSError,
        ValueError,
        QixiCorrectedPackageError,
    ) as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANUAL_CORRECTED_RECEIPT_INVALID",
            candidate_id,
        ) from exc


def _manifest_items(
    review_manifest: object,
    *,
    package_root: Path,
    qixi_repo_root: Path | None = None,
) -> tuple[list[str], dict[str, dict[str, object]]]:
    if not isinstance(review_manifest, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_INVALID"
        )
    if (
        review_manifest.get("status")
        != "finished_review_package_no_upload_pending_human_review"
        or review_manifest.get("upload_allowed") is not False
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_STATE_INVALID"
        )
    raw_items = review_manifest.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_INVALID",
            "items",
        )

    order: list[str] = []
    closure_by_candidate: dict[str, dict[str, object]] = {}
    all_paths: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_MANIFEST_INVALID",
                f"items[{index}]",
            )
        candidate_id = _candidate_id(
            raw_item.get("candidate_id"),
            source=f"review_manifest.items[{index}]",
        )
        if candidate_id in closure_by_candidate:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_MANIFEST_DUPLICATE_CANDIDATE",
                candidate_id,
            )
        title = _nonempty_string(
            raw_item.get("title"),
            reason_code="FINAL_HUMAN_REVIEW_MANIFEST_TITLE_INVALID",
            source=f"review_manifest.items[{index}].title",
        )
        manifest_artifacts: dict[str, str] = {}
        for artifact, manifest_field in _MANIFEST_ARTIFACT_FIELDS.items():
            path = _portable_path(
                raw_item.get(manifest_field),
                source=(
                    f"review_manifest.items[{index}].{manifest_field}"
                ),
            )
            if path in all_paths:
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_MANIFEST_DUPLICATE_ARTIFACT",
                    path,
                )
            all_paths.add(path)
            manifest_artifacts[artifact] = path
        if "mp4" in raw_item:
            alias = _portable_path(
                raw_item.get("mp4"),
                source=f"review_manifest.items[{index}].mp4",
            )
            if alias != manifest_artifacts["video"]:
                raise FinalHumanReviewError(
                    "FINAL_HUMAN_REVIEW_MANIFEST_VIDEO_ALIAS_MISMATCH",
                    candidate_id,
                )
        record_path = _portable_path(
            raw_item.get("record") or raw_item.get("record_json"),
            source=f"review_manifest.items[{index}].record",
        )
        if record_path in all_paths:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_MANIFEST_DUPLICATE_ARTIFACT",
                record_path,
            )
        all_paths.add(record_path)
        record = _json_object(
            _regular_package_file(package_root, record_path),
            source=f"review_manifest.items[{index}].record",
        )
        _validate_manual_corrected_same_bv_item(
            item=raw_item,
            candidate_id=candidate_id,
            package_root=package_root,
            qixi_repo_root=qixi_repo_root,
        )
        story_contract = record.get("story_contract")
        publish_staging = record.get("publish_staging")
        if (
            not isinstance(story_contract, Mapping)
            or story_contract.get("candidate_id") != candidate_id
            or not isinstance(publish_staging, Mapping)
            or publish_staging.get("title") != title
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_RECORD_IDENTITY_INVALID",
                candidate_id,
            )
        record_authority = record.get("recovery_publication_authority")
        review_authority = raw_item.get(
            "recovery_publication_authority"
        )
        if record_authority != review_authority:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_PUBLICATION_AUTHORITY_MISMATCH",
                candidate_id,
            )
        publication_target = _publication_target(
            record_authority,
            candidate_id=candidate_id,
            title=title,
            source=f"review_manifest.items[{index}]",
        )
        expected_cover_claims = set(
            _cover_story_claim_authority(
                record,
                candidate_id=candidate_id,
            )
        )
        burned_preview = record.get("burned_preview")
        branding_intro = (
            burned_preview.get("branding_intro")
            if isinstance(burned_preview, Mapping)
            else None
        )
        verification = (
            branding_intro.get("verification")
            if isinstance(branding_intro, Mapping)
            else None
        )
        final_duration_ms = (
            verification.get("duration_ms")
            if isinstance(verification, Mapping)
            else None
        )
        if (
            not isinstance(final_duration_ms, int)
            or isinstance(final_duration_ms, bool)
            or final_duration_ms <= 0
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_FINAL_DURATION_INVALID",
                candidate_id,
            )
        order.append(candidate_id)
        closure_by_candidate[candidate_id] = {
            "title": title,
            "artifacts": manifest_artifacts,
            "record_path": record_path,
            "publication_target": publication_target,
            "expected_cover_claims": expected_cover_claims,
            "final_duration_ms": final_duration_ms,
        }

    exact_ids = _declared_candidate_list(
        review_manifest.get("exact_candidate_ids")
    )
    # Ivan 2026-07-26 per-BV ruling: a release-scoped manifest carries only
    # the scoped items; the exact contract identity stays intact and the
    # scope must be its subset.
    scope = review_manifest.get("partial_release_scope")
    scoped_ids = (
        _declared_candidate_list(scope.get("candidates"))
        if isinstance(scope, Mapping)
        else []
    )
    expected_order = scoped_ids if scoped_ids else exact_ids
    if expected_order != order or (
        scoped_ids and not set(scoped_ids) <= set(exact_ids)
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_CANDIDATE_SET_INVALID"
        )
    selection_contract = review_manifest.get("selection_contract")
    if (
        not isinstance(selection_contract, Mapping)
        or _declared_candidate_list(
            selection_contract.get("candidate_ids")
        )
        != exact_ids
        or selection_contract.get("mode")
        != "EXACT_CANDIDATE_SET_NO_BACKFILL"
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_CANDIDATE_SET_INVALID"
        )
    return order, closure_by_candidate


def _normalized_claims(
    raw_claims: object,
    *,
    candidate_id: str,
    expected_claims: set[tuple[str, str]],
) -> list[dict[str, str]]:
    if not isinstance(raw_claims, list) or not raw_claims:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIMS_INVALID",
            candidate_id,
        )
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_claim in enumerate(raw_claims):
        if not isinstance(raw_claim, Mapping):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIMS_INVALID",
                f"{candidate_id}[{index}]",
            )
        _require_exact_fields(
            raw_claim,
            _CLAIM_FIELDS,
            reason_code=(
                "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_FIELDS_INVALID"
            ),
        )
        claim = str(raw_claim.get("claim") or "").strip()
        evidence = str(raw_claim.get("evidence") or "").strip()
        presentation = str(raw_claim.get("presentation") or "")
        if (
            not isinstance(raw_claim.get("claim"), str)
            or not claim
            or not isinstance(raw_claim.get("evidence"), str)
            or not evidence
            or presentation not in _CLAIM_PRESENTATIONS
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_INVALID",
                f"{candidate_id}[{index}]",
            )
        if raw_claim.get("status") != "PASS":
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_NOT_PASS",
                f"{candidate_id}[{index}]",
            )
        identity = (claim, presentation)
        if identity in seen:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_DUPLICATE",
                f"{candidate_id}[{index}]",
            )
        seen.add(identity)
        normalized.append(
            {
                "claim": claim,
                "presentation": presentation,
                "status": "PASS",
                "evidence": evidence,
            }
        )
    if seen != expected_claims:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_COVER_STORY_CLAIM_SET_MISMATCH",
            candidate_id,
        )
    return normalized


def _normalized_subtitle_review_points(
    raw_points: object,
    *,
    candidate_id: str,
    final_duration_ms: object,
    expected_points: object,
) -> list[dict[str, object]]:
    if (
        not isinstance(raw_points, list)
        or not isinstance(expected_points, list)
        or not expected_points
        or len(raw_points) != len(expected_points)
        or not isinstance(final_duration_ms, int)
        or isinstance(final_duration_ms, bool)
        or final_duration_ms <= 0
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_SUBTITLE_POINTS_INVALID",
            candidate_id,
        )
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, (raw_point, expected_point) in enumerate(
        zip(raw_points, expected_points, strict=True)
    ):
        if not isinstance(raw_point, Mapping):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID",
                f"{candidate_id}[{index}]",
            )
        _require_exact_fields(
            raw_point,
            _SUBTITLE_REVIEW_POINT_FIELDS,
            reason_code=(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_FIELDS_INVALID"
            ),
        )
        point_id = _candidate_id(
            raw_point.get("point_id"),
            source=f"{candidate_id}[{index}].point_id",
        )
        start_ms = raw_point.get("final_video_start_ms")
        end_ms = raw_point.get("final_video_end_ms")
        expectation = _nonempty_string(
            raw_point.get("expectation"),
            reason_code=(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID"
            ),
            source=f"{candidate_id}[{index}].expectation",
        )
        evidence = _nonempty_string(
            raw_point.get("evidence"),
            reason_code=(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID"
            ),
            source=f"{candidate_id}[{index}].evidence",
        )
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or start_ms < 0
            or end_ms <= start_ms
            # A tail point may round up to "the end": tolerate <=500ms past
            # EOS (2026-07-26 1475 final-laughter, 74.000s vs 73.822s media).
            or end_ms > final_duration_ms + 500
            or raw_point.get("status") != "PASS"
            or not isinstance(expected_point, Mapping)
            or {
                "point_id": point_id,
                "final_video_start_ms": start_ms,
                "final_video_end_ms": end_ms,
                "expectation": expectation,
            }
            != dict(expected_point)
        ):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_INVALID",
                f"{candidate_id}[{index}]",
            )
        if point_id in seen:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_SUBTITLE_POINT_DUPLICATE",
                f"{candidate_id}[{index}]",
            )
        seen.add(point_id)
        normalized.append(
            {
                "point_id": point_id,
                "final_video_start_ms": start_ms,
                "final_video_end_ms": end_ms,
                "expectation": expectation,
                "status": "PASS",
                "evidence": evidence,
            }
        )
    return normalized


def _normalized_item(
    raw_item: object,
    *,
    index: int,
    package_root: Path,
    manifest_closure: Mapping[str, object],
    expected_subtitle_points: list[dict[str, object]],
) -> dict[str, Any]:
    if not isinstance(raw_item, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ITEM_INVALID", f"items[{index}]"
        )
    _require_exact_fields(
        raw_item,
        _ITEM_FIELDS,
        reason_code="FINAL_HUMAN_REVIEW_ITEM_FIELDS_INVALID",
    )
    candidate_id = _candidate_id(
        raw_item.get("candidate_id"), source=f"receipt.items[{index}]"
    )
    reviewed_title = _nonempty_string(
        raw_item.get("reviewed_title"),
        reason_code="FINAL_HUMAN_REVIEW_TITLE_INVALID",
        source=f"{candidate_id}.reviewed_title",
    )
    if reviewed_title != manifest_closure.get("title"):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_TITLE_MISMATCH", candidate_id
        )

    raw_artifacts = raw_item.get("artifacts")
    if not isinstance(raw_artifacts, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACTS_INVALID", candidate_id
        )
    _require_exact_fields(
        raw_artifacts,
        frozenset(_ARTIFACT_FIELDS),
        reason_code="FINAL_HUMAN_REVIEW_ARTIFACTS_FIELDS_INVALID",
    )
    normalized_artifacts: dict[str, dict[str, str]] = {}
    manifest_artifacts = manifest_closure.get("artifacts")
    if not isinstance(manifest_artifacts, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_INVALID",
            f"{candidate_id}.artifacts",
        )
    for artifact in _ARTIFACT_FIELDS:
        binding, _path = _normalized_file_binding(
            raw_artifacts.get(artifact),
            package_root=package_root,
            source=f"{candidate_id}.{artifact}",
        )
        if binding["path"] != manifest_artifacts.get(artifact):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_ARTIFACT_PATH_MISMATCH",
                f"{candidate_id}.{artifact}",
            )
        normalized_artifacts[artifact] = binding

    record_binding, _record_path = _normalized_file_binding(
        raw_item.get("record"),
        package_root=package_root,
        source=f"{candidate_id}.record",
    )
    if record_binding["path"] != manifest_closure.get("record_path"):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_RECORD_PATH_MISMATCH", candidate_id
        )

    raw_publication_target = raw_item.get("publication_target")
    if not isinstance(raw_publication_target, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PUBLICATION_TARGET_INVALID",
            candidate_id,
        )
    _require_exact_fields(
        raw_publication_target,
        _PUBLICATION_TARGET_FIELDS,
        reason_code=(
            "FINAL_HUMAN_REVIEW_PUBLICATION_TARGET_FIELDS_INVALID"
        ),
    )
    publication_target = dict(raw_publication_target)
    if publication_target != manifest_closure.get(
        "publication_target"
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PUBLICATION_TARGET_MISMATCH",
            candidate_id,
        )

    raw_checks = raw_item.get("checks")
    if not isinstance(raw_checks, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CHECKS_INVALID", candidate_id
        )
    _require_exact_fields(
        raw_checks,
        frozenset(_CHECK_FIELDS),
        reason_code="FINAL_HUMAN_REVIEW_CHECKS_FIELDS_INVALID",
    )
    normalized_checks: dict[str, dict[str, str]] = {}
    for check in _CHECK_FIELDS:
        raw_result = raw_checks.get(check)
        if not isinstance(raw_result, Mapping):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CHECK_INVALID",
                f"{candidate_id}.{check}",
            )
        _require_exact_fields(
            raw_result,
            _CHECK_RESULT_FIELDS,
            reason_code="FINAL_HUMAN_REVIEW_CHECK_FIELDS_INVALID",
        )
        evidence = _nonempty_string(
            raw_result.get("evidence"),
            reason_code="FINAL_HUMAN_REVIEW_CHECK_EVIDENCE_INVALID",
            source=f"{candidate_id}.{check}",
        )
        if raw_result.get("status") != "PASS":
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CHECK_NOT_PASS",
                f"{candidate_id}.{check}",
            )
        normalized_checks[check] = {
            "status": "PASS",
            "evidence": evidence,
        }

    return {
        "candidate_id": candidate_id,
        "reviewed_title": reviewed_title,
        "artifacts": normalized_artifacts,
        "record": record_binding,
        "publication_target": publication_target,
        "checks": normalized_checks,
        "subtitle_review_points": (
            _normalized_subtitle_review_points(
                raw_item.get("subtitle_review_points"),
                candidate_id=candidate_id,
                final_duration_ms=manifest_closure.get(
                    "final_duration_ms"
                ),
                expected_points=expected_subtitle_points,
            )
        ),
        "cover_story_claims": _normalized_claims(
            raw_item.get("cover_story_claims"),
            candidate_id=candidate_id,
            expected_claims=set(
                manifest_closure.get("expected_cover_claims") or set()
            ),
        ),
    }


def validate_final_human_review(
    receipt: object,
    package_root: Path,
    review_manifest: object,
    package_attestation: object,
    *,
    qixi_repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and normalize a complete final perceptual-review receipt.

    The returned item order follows ``review_manifest.items``.  The function is
    pure validation: it performs no writes and never changes the supplied
    mappings.
    """

    if not isinstance(receipt, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MISSING_OR_INVALID"
        )
    _require_exact_fields(
        receipt,
        _TOP_LEVEL_FIELDS,
        reason_code="FINAL_HUMAN_REVIEW_FIELDS_INVALID",
    )
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_SCHEMA_INVALID"
        )
    if receipt.get("scope") != REVIEW_SCOPE:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_SCOPE_INVALID"
        )
    if receipt.get("status") != ACCEPTED_STATUS:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_STATUS_NOT_ACCEPTED"
        )
    reviewer_kind = receipt.get("reviewer_kind")
    if (
        not isinstance(reviewer_kind, str)
        or reviewer_kind not in _REVIEWER_KINDS
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEWER_KIND_INVALID"
        )
    reviewed_by = _nonempty_string(
        receipt.get("reviewed_by"),
        reason_code="FINAL_HUMAN_REVIEW_REVIEWED_BY_INVALID",
        source="reviewed_by",
    )
    reserved_identity = _RESERVED_REVIEWER_IDENTITIES.get(
        str(reviewer_kind)
    )
    claimed_reserved_kind = _reserved_reviewer_kind(reviewed_by)
    if (
        reserved_identity is not None
        and reviewed_by not in reserved_identity
    ) or (
        reviewer_kind == "human_delegate"
        and claimed_reserved_kind is not None
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEWER_IDENTITY_MISMATCH"
        )
    reviewed_at = _nonempty_string(
        receipt.get("reviewed_at"),
        reason_code="FINAL_HUMAN_REVIEW_REVIEWED_AT_INVALID",
        source="reviewed_at",
    )
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at)
    except ValueError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEWED_AT_INVALID"
        ) from exc
    if parsed_reviewed_at.tzinfo is None:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEWED_AT_INVALID"
        )
    approval_quote = _nonempty_string(
        receipt.get("approval_quote"),
        reason_code="FINAL_HUMAN_REVIEW_APPROVAL_QUOTE_INVALID",
        source="approval_quote",
    )
    review_contract_sha256, review_contracts = _review_contracts()
    if receipt.get("review_contract_sha256") != review_contract_sha256:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CONTRACT_HASH_MISMATCH"
        )
    try:
        root_argument = Path(package_root)
    except TypeError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_PACKAGE_ROOT_INVALID"
        ) from exc
    root = _package_directory(root_argument)
    if not isinstance(review_manifest, Mapping):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_MANIFEST_INVALID"
        )
    package_evidence = _normalized_package_evidence(
        receipt.get("package_evidence"),
        package_root=root,
        package_attestation=package_attestation,
        review_manifest=review_manifest,
    )
    manifest_order, manifest_closure = _manifest_items(
        review_manifest,
        package_root=root,
        qixi_repo_root=qixi_repo_root,
    )

    raw_items = receipt.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ITEMS_INVALID"
        )
    receipt_by_candidate: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_ITEM_INVALID", f"items[{index}]"
            )
        candidate_id = _candidate_id(
            raw_item.get("candidate_id"),
            source=f"receipt.items[{index}]",
        )
        if candidate_id in receipt_by_candidate:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_DUPLICATE_CANDIDATE",
                candidate_id,
            )
        if candidate_id not in manifest_closure:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CANDIDATE_SET_MISMATCH",
                candidate_id,
            )
        expected_subtitle_points = review_contracts.get(candidate_id)
        if expected_subtitle_points is None:
            raise FinalHumanReviewError(
                "FINAL_HUMAN_REVIEW_CONTRACT_CANDIDATE_MISSING",
                candidate_id,
            )
        receipt_by_candidate[candidate_id] = _normalized_item(
            raw_item,
            index=index,
            package_root=root,
            manifest_closure=manifest_closure[candidate_id],
            expected_subtitle_points=expected_subtitle_points,
        )
    if set(receipt_by_candidate) != set(manifest_order):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_CANDIDATE_SET_MISMATCH"
        )
    normalized_items = [
        receipt_by_candidate[candidate_id]
        for candidate_id in manifest_order
    ]
    review_evidence_binding = package_evidence["review_evidence"]
    review_evidence_path = _regular_package_file(
        root, str(review_evidence_binding["path"])
    )
    try:
        bound_review_evidence_bytes = review_evidence_path.read_bytes()
    except OSError as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_ARTIFACT_FILE_INVALID",
            str(review_evidence_path),
        ) from exc
    if (
        "sha256:"
        + hashlib.sha256(bound_review_evidence_bytes).hexdigest()
        != review_evidence_binding["sha256"]
        or len(bound_review_evidence_bytes)
        != review_evidence_binding["bytes"]
    ):
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_REVIEW_EVIDENCE_BINDING_MISMATCH"
        )
    try:
        bound_review_evidence = json.loads(
            bound_review_evidence_bytes.decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FinalHumanReviewError(
            "FINAL_HUMAN_REVIEW_EVIDENCE_JSON_INVALID",
            "package_evidence.review_evidence",
        ) from exc
    try:
        validate_bound_review_evidence(
            bound_review_evidence,
            review_contract_sha256=review_contract_sha256,
            package_evidence=package_evidence,
            reviewer_metadata={
                "reviewer_kind": reviewer_kind,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
                "approval_quote": approval_quote,
            },
            receipt_items=normalized_items,
        )
    except FinalHumanReviewEvidenceError as exc:
        raise FinalHumanReviewError(
            exc.reason_code, exc.detail
        ) from exc

    return {
        "schema_version": SCHEMA_VERSION,
        "scope": REVIEW_SCOPE,
        "status": ACCEPTED_STATUS,
        "reviewer_kind": reviewer_kind,
        "reviewed_by": reviewed_by,
        "reviewed_at": reviewed_at,
        "approval_quote": approval_quote,
        "review_contract_sha256": review_contract_sha256,
        "package_evidence": package_evidence,
        "items": normalized_items,
    }
