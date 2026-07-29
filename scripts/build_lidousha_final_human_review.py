#!/usr/bin/env python3
"""Build a hash-bound final perceptual-review receipt from completed evidence.

This command does not perform, simulate, or pre-fill the perceptual review.  It
only accepts a strict external evidence document written after the declared
reviewer completed the review.  Every reproducible field in the final receipt
is derived from the current review package, current canonical package audit,
committed exact-point contract, and package records.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lidousha_review_package import (  # noqa: E402
    AUDIT_POLICY_EPOCH,
    AUDIT_SCHEMA_VERSION,
    audit_package,
)
from src.autoslice import final_human_review as human_review  # noqa: E402


EVIDENCE_SCHEMA_VERSION = human_review.REVIEW_EVIDENCE_SCHEMA_VERSION
DEFAULT_RECEIPT_RELATIVE_PATH = "verification/final-human-review.json"
DEFAULT_EVIDENCE_TEMPLATE_RELATIVE_PATH = (
    "verification/final-human-review-evidence.v2.json"
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "bindings",
        "reviewer_kind",
        "reviewed_by",
        "reviewed_at",
        "approval_quote",
        "items",
    }
)
_ITEM_EVIDENCE_FIELDS = frozenset(
    {
        "candidate_id",
        "checks",
        "subtitle_review_points",
        "cover_story_claims",
    }
)
_EVIDENCE_BINDING_FIELDS = frozenset(
    {
        "review_contract_sha256",
        "review_manifest",
        "package_audit",
        "items",
    }
)
_EVIDENCE_ITEM_BINDING_FIELDS = frozenset(
    {"candidate_id", "record", "artifacts"}
)
_EVIDENCE_ARTIFACT_BINDING_FIELDS = frozenset({"video", "subtitle", "cover"})
_POINT_EVIDENCE_FIELDS = frozenset({"point_id", "observation"})
_CLAIM_EVIDENCE_FIELDS = frozenset(
    {"claim", "presentation", "observation"}
)
_OBSERVATION_FIELDS = frozenset({"anchor", "detail"})
_AUDIT_BINDING_FIELDS = (
    "schema_version",
    "policy_epoch",
    "policy_fingerprint",
    "auditor_source_sha256",
    "passed",
    "root",
    "audited_inputs",
    "issues",
    "issue_count",
    "blocking_issue_count",
)
_GENERIC_EVIDENCE = re.compile(
    r"^(?:"
    r"pass|ok|okay|done|checked|inspected|reviewed|watched|confirmed|"
    r"通过|已通过|检查|已检查|检查完成|审阅|已审阅|审阅完成|"
    r"观看|已观看|观看完成|确认|已确认|无误|没问题|正常|一致|"
    r"(?:已)?(?:检查|审阅|观看|核对|确认)"
    r"(?:最终)?(?:视频|字幕|封面|音频|内容|该点|此点|该项)?"
    r"(?:完成|通过|无误|正常|一致)?"
    r")$",
    re.IGNORECASE,
)
_PLACEHOLDER_EVIDENCE = re.compile(
    r"(?:\b(?:todo|tbd|n/?a|placeholder)\b|待填|占位|"
    r"<[^>]+>|\{\{[^}]+\}\})",
    re.IGNORECASE,
)
_GENERIC_NUMBERED_EVIDENCE = re.compile(
    r"^(?:(?:编号|第)?[0-9一二三四五六七八九十]+(?:号|项|点|条)?)*"
    r"(?:均|都|全部|全都)?"
    r"(?:无异常|没有异常|没异常|无问题|没有问题|没问题|正常|通过|pass|ok|okay)$",
    re.IGNORECASE,
)
_GENERIC_EVIDENCE_TERMS = (
    "completed",
    "confirmed",
    "inspected",
    "reviewed",
    "checked",
    "watched",
    "normal",
    "correct",
    "final",
    "audio",
    "video",
    "cover",
    "subtitle",
    "content",
    "pass",
    "done",
    "okay",
    "ok",
    "检查完成",
    "审阅完成",
    "观看完成",
    "核对完成",
    "确认完成",
    "已检查",
    "已审阅",
    "已观看",
    "已核对",
    "已确认",
    "最终",
    "视频",
    "字幕",
    "封面",
    "音频",
    "内容",
    "该点",
    "此点",
    "该项",
    "检查",
    "审阅",
    "观看",
    "核对",
    "确认",
    "完成",
    "通过",
    "无误",
    "无异常",
    "没有异常",
    "没异常",
    "无问题",
    "没有问题",
    "没问题",
    "编号",
    "均",
    "都",
    "全部",
    "全都",
    "正常",
    "一致",
    "已",
)

_CHECK_ANCHORS = {
    "final_burned_full_playback": "00:00.000-EOS",
    "subtitle_audio": "FULL_VIDEO_AUDIO_SUBTITLE",
    "silence_hallucination": "KNOWN_SILENCE_AND_FULL_VIDEO",
    "boundary_closure": "CLIP_START_AND_EOS",
    "title_story": "FINAL_TITLE_AND_FULL_STORY",
    "cover_identity": "FINAL_COVER",
    "cover_story": "FINAL_COVER_AND_STORY",
    "intro_timing": "INTRO_TO_MAIN_TRANSITION",
}
_CHECK_SPECIFIC_TERMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "final_burned_full_playback": (
        ("00:00", "开头", "start", "begin"),
        ("eos", "结尾", "结束", "end"),
    ),
    "subtitle_audio": (
        ("字幕", "subtitle", "字卡"),
        ("听到", "发声", "音频", "audio", "speech", "台词"),
    ),
    "silence_hallucination": (
        ("静音", "无声", "没说话", "silence", "silent"),
        ("字幕", "幻听", "hallucination", "cue"),
    ),
    "boundary_closure": (
        ("开头", "起点", "start", "结尾", "收尾", "end", "eos"),
        ("完整", "自然", "话题", "句子", "closure", "sentence", "topic"),
    ),
    "title_story": (
        ("标题", "title"),
        ("故事", "内容", "反转", "争论", "story", "topic"),
    ),
    "cover_identity": (
        ("封面", "cover"),
        ("人物", "身份", "立绘", "左侧", "右侧", "identity", "character"),
    ),
    "cover_story": (
        ("封面", "cover"),
        ("文字", "叙事", "故事", "关系", "story", "text"),
    ),
    "intro_timing": (
        ("片头", "intro"),
        ("正片", "切入", "衔接", "transition", "main"),
    ),
}


class FinalHumanReviewBuildError(ValueError):
    """The package or completed-review evidence cannot produce a receipt."""


class FinalHumanReviewCommittedDurabilityUnconfirmed(FinalHumanReviewBuildError):
    """The output name was committed, but cleanup/directory durability was not confirmed."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(
            f"COMMITTED_BUT_DURABILITY_UNCONFIRMED: {path}: {detail}"
        )


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise FinalHumanReviewBuildError(f"{label} must be a regular non-symlink file: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FinalHumanReviewBuildError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FinalHumanReviewBuildError(f"{label} is unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise FinalHumanReviewBuildError(f"{label} JSON must be an object: {path}")
    return value


def _sha256(path: Path, *, prefix: bool = True) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise FinalHumanReviewBuildError(f"cannot hash required file: {path}") from exc
    value = digest.hexdigest()
    return f"sha256:{value}" if prefix else value


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        raise FinalHumanReviewBuildError(
            f"{label} fields invalid: missing={missing!r} extra={extra!r}"
        )


def _package_root(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FinalHumanReviewBuildError(f"package root is missing: {path}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or not resolved.is_dir()
    ):
        raise FinalHumanReviewBuildError(f"package root must be a non-symlink directory: {path}")
    return resolved


def _package_relative_regular(root: Path, path: Path, *, label: str) -> tuple[str, Path]:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FinalHumanReviewBuildError(
            f"{label} must be a regular file inside package root: {path}"
        ) from exc
    relative_text = PurePosixPath(*relative.parts).as_posix()
    # Reuse the canonical walk so a symlink in any path component is rejected.
    try:
        canonical = human_review._regular_package_file(  # noqa: SLF001
            root, relative_text
        )
    except human_review.FinalHumanReviewError as exc:
        raise FinalHumanReviewBuildError(f"{label} is not a canonical package file: {exc}") from exc
    return relative_text, canonical


def _file_binding(root: Path, path: Path, *, label: str) -> dict[str, str]:
    relative, canonical = _package_relative_regular(root, path, label=label)
    return {"path": relative, "sha256": _sha256(canonical)}


def _file_binding_with_bytes(
    root: Path,
    path: Path,
    *,
    label: str,
) -> dict[str, object]:
    relative, canonical = _package_relative_regular(
        root, path, label=label
    )
    return {
        "path": relative,
        "sha256": _sha256(canonical),
        "bytes": canonical.stat().st_size,
    }


def _absolute_attestation(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "sha256": _sha256(resolved, prefix=False),
        "bytes": resolved.stat().st_size,
    }


def _audit_binding(audit: Mapping[str, object]) -> dict[str, object]:
    return {key: audit.get(key) for key in _AUDIT_BINDING_FIELDS}


def _current_audit(root: Path, audit_path: Path) -> tuple[dict[str, Any], Path]:
    _relative, canonical_path = _package_relative_regular(root, audit_path, label="package audit")
    supplied = _load_json_object(canonical_path, label="package audit")
    fresh = audit_package(root)
    if (
        fresh.get("schema_version") != AUDIT_SCHEMA_VERSION
        or fresh.get("policy_epoch") != AUDIT_POLICY_EPOCH
        or fresh.get("passed") is not True
        or fresh.get("blocking_issue_count") != 0
    ):
        raise FinalHumanReviewBuildError("current canonical package audit rejects the package")
    if _audit_binding(supplied) != _audit_binding(fresh):
        raise FinalHumanReviewBuildError(
            "supplied package audit is stale or does not bind the current "
            "canonical package input closure"
        )
    return supplied, canonical_path


def _evidence_bindings(
    *,
    root: Path,
    review_path: Path,
    audit_path: Path,
    review_contract_sha256: str,
    manifest_order: Sequence[str],
    manifest_closure: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    item_bindings: list[dict[str, object]] = []
    for candidate_id in manifest_order:
        closure = manifest_closure[candidate_id]
        artifacts = closure.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise FinalHumanReviewBuildError(
                f"{candidate_id} manifest artifacts are invalid"
            )
        item_bindings.append(
            {
                "candidate_id": candidate_id,
                "record": _file_binding(
                    root,
                    root / str(closure["record_path"]),
                    label=f"{candidate_id}.record",
                ),
                "artifacts": {
                    artifact: _file_binding(
                        root,
                        root / str(artifacts[artifact]),
                        label=f"{candidate_id}.{artifact}",
                    )
                    for artifact in human_review._ARTIFACT_FIELDS  # noqa: SLF001
                },
            }
        )
    return {
        "review_contract_sha256": review_contract_sha256,
        "review_manifest": _file_binding(
            root, review_path, label="review manifest"
        ),
        "package_audit": _file_binding(
            root, audit_path, label="package audit"
        ),
        "items": item_bindings,
    }


def _validate_evidence_bindings(
    value: object,
    *,
    expected: Mapping[str, object],
) -> None:
    if not isinstance(value, Mapping):
        raise FinalHumanReviewBuildError(
            "review evidence bindings must be an object"
        )
    _require_exact_fields(
        value, _EVIDENCE_BINDING_FIELDS, label="review evidence bindings"
    )
    raw_items = value.get("items")
    expected_items = expected.get("items")
    if not isinstance(raw_items, list) or not isinstance(expected_items, list):
        raise FinalHumanReviewBuildError(
            "review evidence binding items must be a list"
        )
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise FinalHumanReviewBuildError(
                f"review evidence bindings.items[{index}] must be an object"
            )
        _require_exact_fields(
            raw_item,
            _EVIDENCE_ITEM_BINDING_FIELDS,
            label=f"review evidence bindings.items[{index}]",
        )
        artifacts = raw_item.get("artifacts")
        if not isinstance(artifacts, Mapping):
            raise FinalHumanReviewBuildError(
                f"review evidence bindings.items[{index}].artifacts must be an object"
            )
        _require_exact_fields(
            artifacts,
            _EVIDENCE_ARTIFACT_BINDING_FIELDS,
            label=f"review evidence bindings.items[{index}].artifacts",
        )
    if dict(value) != dict(expected):
        raise FinalHumanReviewBuildError(
            "review evidence byte bindings are stale: review contract, "
            "manifest, current audit, record, video, subtitle, or cover drifted"
        )


def _review_authority(
    *,
    package_root: Path,
    package_audit_path: Path,
    review_manifest_path: Path | None,
) -> dict[str, object]:
    root = _package_root(package_root)
    review_path_argument = (
        review_manifest_path
        if review_manifest_path is not None
        else root / "review_manifest.json"
    )
    _review_relative, review_path = _package_relative_regular(
        root, review_path_argument, label="review manifest"
    )
    review_manifest = _load_json_object(
        review_path, label="review manifest"
    )
    _audit, audit_path = _current_audit(root, package_audit_path)
    try:
        manifest_order, manifest_closure = human_review._manifest_items(  # noqa: SLF001
            review_manifest, package_root=root
        )
        review_contract_sha256, review_contracts = (
            human_review._review_contracts()  # noqa: SLF001
        )
    except human_review.FinalHumanReviewError as exc:
        raise FinalHumanReviewBuildError(
            f"canonical final-review authority rejects the package: {exc}"
        ) from exc
    bindings = _evidence_bindings(
        root=root,
        review_path=review_path,
        audit_path=audit_path,
        review_contract_sha256=review_contract_sha256,
        manifest_order=manifest_order,
        manifest_closure=manifest_closure,
    )
    return {
        "root": root,
        "review_path": review_path,
        "review_manifest": review_manifest,
        "audit_path": audit_path,
        "manifest_order": manifest_order,
        "manifest_closure": manifest_closure,
        "review_contract_sha256": review_contract_sha256,
        "review_contracts": review_contracts,
        "bindings": bindings,
    }


def _format_review_ms(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FinalHumanReviewBuildError(
            f"review point timestamp is invalid: {value!r}"
        )
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _point_anchor(point: Mapping[str, object]) -> str:
    return (
        f"{_format_review_ms(point.get('final_video_start_ms'))}-"
        f"{_format_review_ms(point.get('final_video_end_ms'))}"
    )


def build_evidence_template(
    *,
    package_root: Path,
    package_audit_path: Path,
    review_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Build a byte-bound, deliberately incomplete evidence template."""

    authority = _review_authority(
        package_root=package_root,
        package_audit_path=package_audit_path,
        review_manifest_path=review_manifest_path,
    )
    root = authority["root"]
    assert isinstance(root, Path)
    manifest_order = authority["manifest_order"]
    manifest_closure = authority["manifest_closure"]
    review_contracts = authority["review_contracts"]
    assert isinstance(manifest_order, list)
    assert isinstance(manifest_closure, Mapping)
    assert isinstance(review_contracts, Mapping)
    items: list[dict[str, object]] = []
    for candidate_id in manifest_order:
        closure = manifest_closure[candidate_id]
        assert isinstance(closure, Mapping)
        record = _load_json_object(
            root / str(closure["record_path"]),
            label=f"{candidate_id} record",
        )
        expected_points = review_contracts.get(candidate_id)
        if not isinstance(expected_points, list) or not expected_points:
            raise FinalHumanReviewBuildError(
                f"committed review contract has no candidate: {candidate_id}"
            )
        final_duration_ms = closure.get("final_duration_ms")
        if not isinstance(final_duration_ms, int) or isinstance(
            final_duration_ms, bool
        ):
            raise FinalHumanReviewBuildError(
                f"final media duration is invalid: {candidate_id}"
            )
        for point in expected_points:
            point_id = point.get("point_id")
            point_end_ms = point.get("final_video_end_ms")
            # Keep this tolerance identical to canonical receipt validation:
            # a tail window may round at most 500 ms past the probed EOS.
            if (
                not isinstance(point_end_ms, int)
                or isinstance(point_end_ms, bool)
                or point_end_ms > final_duration_ms + 500
            ):
                raise FinalHumanReviewBuildError(
                    "committed review point exceeds final media duration: "
                    f"{candidate_id}.{point_id} end={point_end_ms} "
                    f"duration={final_duration_ms}"
                )
        expected_claims = _ordered_cover_claims(
            record, candidate_id=candidate_id
        )
        items.append(
            {
                "candidate_id": candidate_id,
                "checks": {
                    check: {
                        "anchor": _CHECK_ANCHORS[check],
                        "detail": (
                            f"<REQUIRED_POST_REVIEW_OBSERVATION_FOR_{check}>"
                        ),
                    }
                    for check in human_review._CHECK_FIELDS  # noqa: SLF001
                },
                "subtitle_review_points": [
                    {
                        "point_id": point["point_id"],
                        "observation": {
                            "anchor": _point_anchor(point),
                            "detail": (
                                "<REQUIRED_POST_REVIEW_HEARD_AND_SEEN_DETAIL>"
                            ),
                        },
                    }
                    for point in expected_points
                ],
                "cover_story_claims": [
                    {
                        "claim": claim,
                        "presentation": presentation,
                        "observation": {
                            "anchor": f"FINAL_COVER/{presentation}",
                            "detail": (
                                "<REQUIRED_POST_REVIEW_VISIBLE_COVER_DETAIL>"
                            ),
                        },
                    }
                    for claim, presentation in expected_claims
                ],
            }
        )
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "bindings": authority["bindings"],
        "reviewer_kind": "<REQUIRED_REVIEWER_KIND>",
        "reviewed_by": "<REQUIRED_ACTUAL_REVIEWER>",
        "reviewed_at": "<REQUIRED_ISO8601_TIMESTAMP_AFTER_REVIEW>",
        "approval_quote": "<REQUIRED_SCOPE_AUTHORIZATION_QUOTE>",
        "items": items,
    }


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _evidence_probe(evidence: str, *, derived_phrases: Sequence[str]) -> str:
    probe = _normalized_text(evidence)
    for phrase in sorted(
        {_normalized_text(value) for value in derived_phrases if value},
        key=len,
        reverse=True,
    ):
        probe = probe.replace(phrase, "")
    probe = re.sub(
        r"(?:(?:编号|第)?[0-9一二三四五六七八九十]+(?:号|项|点|条)?|"
        r"\b(?:no|number)\s*[0-9]+\b)",
        "",
        probe,
        flags=re.IGNORECASE,
    )
    return re.sub(r"[\W_]+", "", probe, flags=re.UNICODE)


def _concrete_evidence_detail(
    value: object,
    *,
    label: str,
    derived_phrases: Sequence[str] = (),
) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) < 12
        or any(ord(character) < 32 for character in value)
        or _PLACEHOLDER_EVIDENCE.search(value)
    ):
        raise FinalHumanReviewBuildError(f"{label} requires concrete post-review evidence")
    probe = _evidence_probe(value, derived_phrases=derived_phrases)
    generic_residue = probe
    for term in _GENERIC_EVIDENCE_TERMS:
        generic_residue = generic_residue.replace(_normalized_text(term), "")
    if (
        not probe
        or _GENERIC_EVIDENCE.fullmatch(probe)
        or _GENERIC_NUMBERED_EVIDENCE.fullmatch(probe)
        or not generic_residue
    ):
        raise FinalHumanReviewBuildError(f"{label} rejects generic or restated evidence")
    return value, probe


def _register_observation(
    *,
    probe: str,
    label: str,
    evidence_seen: list[tuple[str, str]],
) -> None:
    for previous_probe, previous_label in evidence_seen:
        if probe == previous_probe:
            raise FinalHumanReviewBuildError(
                "review evidence must be observation-specific, not reused: "
                f"{label} duplicates {previous_label}"
            )
        if (
            min(len(probe), len(previous_probe)) >= 12
            and difflib.SequenceMatcher(
                None, probe, previous_probe, autojunk=False
            ).ratio()
            >= 0.94
        ):
            raise FinalHumanReviewBuildError(
                "review evidence must not be a near-duplicate after "
                f"NFKC/number/punctuation normalization: {label} ~ {previous_label}"
            )
    evidence_seen.append((probe, label))


def _observation(
    value: object,
    *,
    expected_anchor: str,
    label: str,
    evidence_seen: list[tuple[str, str]],
    derived_phrases: Sequence[str] = (),
    specific_terms: Sequence[Sequence[str]] = (),
) -> str:
    if not isinstance(value, Mapping):
        raise FinalHumanReviewBuildError(
            f"{label} requires structured observation object"
        )
    _require_exact_fields(value, _OBSERVATION_FIELDS, label=f"{label}.observation")
    if value.get("anchor") != expected_anchor:
        raise FinalHumanReviewBuildError(
            f"{label}.observation.anchor must be {expected_anchor!r}"
        )
    detail, probe = _concrete_evidence_detail(
        value.get("detail"),
        label=f"{label}.observation.detail",
        derived_phrases=(*derived_phrases, expected_anchor),
    )
    normalized_detail = _normalized_text(detail)
    for alternatives in specific_terms:
        if not any(
            _normalized_text(term) in normalized_detail
            for term in alternatives
        ):
            raise FinalHumanReviewBuildError(
                f"{label} lacks check-specific observed detail "
                f"(one of {tuple(alternatives)!r})"
            )
    _register_observation(
        probe=probe,
        label=label,
        evidence_seen=evidence_seen,
    )
    return f"{expected_anchor} — {detail}"


def _review_evidence(
    path: Path,
    *,
    expected_bindings: Mapping[str, object],
) -> dict[str, Any]:
    evidence = _load_json_object(path, label="review evidence")
    _require_exact_fields(evidence, _EVIDENCE_FIELDS, label="review evidence")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise FinalHumanReviewBuildError("review evidence schema_version is invalid")
    _validate_evidence_bindings(
        evidence.get("bindings"),
        expected=expected_bindings,
    )
    items = evidence.get("items")
    if not isinstance(items, list) or not items:
        raise FinalHumanReviewBuildError("review evidence items must be a non-empty list")
    return evidence


def _evidence_items(
    evidence: Mapping[str, object], expected_order: Sequence[str]
) -> dict[str, Mapping[str, object]]:
    raw_items = evidence.get("items")
    assert isinstance(raw_items, list)
    normalized: dict[str, Mapping[str, object]] = {}
    order: list[str] = []
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise FinalHumanReviewBuildError(f"review evidence items[{index}] must be an object")
        _require_exact_fields(
            raw_item,
            _ITEM_EVIDENCE_FIELDS,
            label=f"review evidence items[{index}]",
        )
        candidate_id = raw_item.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise FinalHumanReviewBuildError(
                f"review evidence items[{index}] candidate_id is invalid"
            )
        if candidate_id in normalized:
            raise FinalHumanReviewBuildError(f"duplicate review evidence candidate: {candidate_id}")
        normalized[candidate_id] = raw_item
        order.append(candidate_id)
    if order != list(expected_order):
        raise FinalHumanReviewBuildError(
            "review evidence candidate order/set does not exactly match review_manifest.items"
        )
    return normalized


def _check_results(
    value: object,
    *,
    candidate_id: str,
    evidence_seen: list[tuple[str, str]],
) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise FinalHumanReviewBuildError(f"{candidate_id}.checks must be an object")
    _require_exact_fields(
        value,
        frozenset(human_review._CHECK_FIELDS),  # noqa: SLF001
        label=f"{candidate_id}.checks",
    )
    results: dict[str, dict[str, str]] = {}
    for check in human_review._CHECK_FIELDS:  # noqa: SLF001
        label = f"{candidate_id}.checks.{check}"
        evidence = _observation(
            value.get(check),
            expected_anchor=_CHECK_ANCHORS[check],
            label=label,
            evidence_seen=evidence_seen,
            derived_phrases=(candidate_id, check),
            specific_terms=_CHECK_SPECIFIC_TERMS[check],
        )
        results[check] = {"status": "PASS", "evidence": evidence}
    return results


def _subtitle_points(
    value: object,
    *,
    candidate_id: str,
    expected: Sequence[Mapping[str, object]],
    evidence_seen: list[tuple[str, str]],
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(expected):
        raise FinalHumanReviewBuildError(f"{candidate_id}.subtitle_review_points count mismatch")
    points: list[dict[str, object]] = []
    for index, (raw_point, expected_point) in enumerate(zip(value, expected, strict=True)):
        if not isinstance(raw_point, Mapping):
            raise FinalHumanReviewBuildError(
                f"{candidate_id}.subtitle_review_points[{index}] must be an object"
            )
        _require_exact_fields(
            raw_point,
            _POINT_EVIDENCE_FIELDS,
            label=f"{candidate_id}.subtitle_review_points[{index}]",
        )
        point_id = expected_point["point_id"]
        if raw_point.get("point_id") != point_id:
            raise FinalHumanReviewBuildError(
                f"{candidate_id}.subtitle_review_points[{index}] "
                "does not match committed point order"
            )
        expectation = str(expected_point["expectation"])
        label = f"{candidate_id}.subtitle_review_points[{point_id}]"
        evidence = _observation(
            raw_point.get("observation"),
            expected_anchor=_point_anchor(expected_point),
            label=label,
            evidence_seen=evidence_seen,
            derived_phrases=(candidate_id, str(point_id), expectation),
            specific_terms=(
                ("听到", "发声", "无声", "静音", "heard", "audio", "silent"),
                ("字幕", "烧录", "cue", "subtitle"),
            ),
        )
        points.append(
            {
                **dict(expected_point),
                "status": "PASS",
                "evidence": evidence,
            }
        )
    return points


def _ordered_cover_claims(
    record: Mapping[str, object], *, candidate_id: str
) -> list[tuple[str, str]]:
    try:
        claims = human_review._cover_story_claim_authority(  # noqa: SLF001
            record,
            candidate_id=candidate_id,
        )
    except human_review.FinalHumanReviewError as exc:
        raise FinalHumanReviewBuildError(
            f"{candidate_id} record has no complete cover claim authority"
        ) from exc
    return list(claims)


def _cover_claims(
    value: object,
    *,
    candidate_id: str,
    expected: Sequence[tuple[str, str]],
    evidence_seen: list[tuple[str, str]],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != len(expected):
        raise FinalHumanReviewBuildError(f"{candidate_id}.cover_story_claims count mismatch")
    claims: list[dict[str, str]] = []
    for index, (raw_claim, expected_claim) in enumerate(zip(value, expected, strict=True)):
        if not isinstance(raw_claim, Mapping):
            raise FinalHumanReviewBuildError(
                f"{candidate_id}.cover_story_claims[{index}] must be an object"
            )
        _require_exact_fields(
            raw_claim,
            _CLAIM_EVIDENCE_FIELDS,
            label=f"{candidate_id}.cover_story_claims[{index}]",
        )
        claim, presentation = expected_claim
        if raw_claim.get("claim") != claim or raw_claim.get("presentation") != presentation:
            raise FinalHumanReviewBuildError(
                f"{candidate_id}.cover_story_claims[{index}] does not match record authority order"
            )
        label = f"{candidate_id}.cover_story_claims[{index}]"
        claim_terms: tuple[tuple[str, ...], ...]
        if presentation == "SOURCE_FRAME":
            claim_terms = (
                ("封面", "画面", "源帧", "cover", "frame"),
                ("可见", "人物", "立绘", "左", "右", "visible", "character"),
            )
        else:
            claim_terms = (
                ("封面", "大字", "文字", "cover", "text"),
                ("叙事", "故事", "关系", "内容", "story", "topic"),
            )
        evidence = _observation(
            raw_claim.get("observation"),
            expected_anchor=f"FINAL_COVER/{presentation}",
            label=label,
            evidence_seen=evidence_seen,
            derived_phrases=(candidate_id, claim, presentation),
            specific_terms=claim_terms,
        )
        claims.append(
            {
                "claim": claim,
                "presentation": presentation,
                "status": "PASS",
                "evidence": evidence,
            }
        )
    return claims


def build_receipt(
    *,
    package_root: Path,
    package_audit_path: Path,
    evidence_path: Path,
    review_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Derive and canonically validate a receipt without writing it."""

    authority = _review_authority(
        package_root=package_root,
        package_audit_path=package_audit_path,
        review_manifest_path=review_manifest_path,
    )
    root = authority["root"]
    review_path = authority["review_path"]
    review_manifest = authority["review_manifest"]
    audit_path = authority["audit_path"]
    manifest_order = authority["manifest_order"]
    manifest_closure = authority["manifest_closure"]
    review_contract_sha256 = authority["review_contract_sha256"]
    review_contracts = authority["review_contracts"]
    assert isinstance(root, Path)
    assert isinstance(review_path, Path)
    assert isinstance(review_manifest, Mapping)
    assert isinstance(audit_path, Path)
    assert isinstance(manifest_order, list)
    assert isinstance(manifest_closure, Mapping)
    assert isinstance(review_contract_sha256, str)
    assert isinstance(review_contracts, Mapping)
    bindings = authority["bindings"]
    assert isinstance(bindings, Mapping)
    _evidence_relative, canonical_evidence_path = (
        _package_relative_regular(
            root,
            evidence_path,
            label="review evidence",
        )
    )
    evidence = _review_evidence(
        canonical_evidence_path,
        expected_bindings=bindings,
    )
    evidence_by_candidate = _evidence_items(evidence, manifest_order)
    evidence_seen: list[tuple[str, str]] = []

    receipt_items: list[dict[str, Any]] = []
    for candidate_id in manifest_order:
        closure = manifest_closure[candidate_id]
        raw_evidence = evidence_by_candidate[candidate_id]
        artifacts = closure["artifacts"]
        if not isinstance(artifacts, Mapping):
            raise FinalHumanReviewBuildError(f"{candidate_id} manifest artifacts are invalid")
        artifact_bindings = {
            artifact: _file_binding(
                root,
                root / str(artifacts[artifact]),
                label=f"{candidate_id}.{artifact}",
            )
            for artifact in human_review._ARTIFACT_FIELDS  # noqa: SLF001
        }
        record_relative = str(closure["record_path"])
        record_path = root / record_relative
        record = _load_json_object(record_path, label=f"{candidate_id} record")
        expected_points = review_contracts.get(candidate_id)
        if expected_points is None:
            raise FinalHumanReviewBuildError(
                f"committed review contract has no candidate: {candidate_id}"
            )
        expected_claims = _ordered_cover_claims(record, candidate_id=candidate_id)
        if set(expected_claims) != set(closure.get("expected_cover_claims") or set()):
            raise FinalHumanReviewBuildError(
                f"{candidate_id} cover claim projection drifted from canonical manifest closure"
            )
        receipt_items.append(
            {
                "candidate_id": candidate_id,
                "reviewed_title": closure["title"],
                "artifacts": artifact_bindings,
                "record": _file_binding(
                    root,
                    record_path,
                    label=f"{candidate_id}.record",
                ),
                "publication_target": dict(closure["publication_target"]),
                "checks": _check_results(
                    raw_evidence.get("checks"),
                    candidate_id=candidate_id,
                    evidence_seen=evidence_seen,
                ),
                "subtitle_review_points": _subtitle_points(
                    raw_evidence.get("subtitle_review_points"),
                    candidate_id=candidate_id,
                    expected=expected_points,
                    evidence_seen=evidence_seen,
                ),
                "cover_story_claims": _cover_claims(
                    raw_evidence.get("cover_story_claims"),
                    candidate_id=candidate_id,
                    expected=expected_claims,
                    evidence_seen=evidence_seen,
                ),
            }
        )

    receipt: dict[str, Any] = {
        "schema_version": human_review.SCHEMA_VERSION,
        "scope": human_review.REVIEW_SCOPE,
        "status": human_review.ACCEPTED_STATUS,
        "reviewer_kind": evidence.get("reviewer_kind"),
        "reviewed_by": evidence.get("reviewed_by"),
        "reviewed_at": evidence.get("reviewed_at"),
        "approval_quote": evidence.get("approval_quote"),
        "review_contract_sha256": review_contract_sha256,
        "package_evidence": {
            "review_manifest": _file_binding(root, review_path, label="review manifest"),
            "package_audit": _file_binding(root, audit_path, label="package audit"),
            "review_evidence": _file_binding_with_bytes(
                root,
                canonical_evidence_path,
                label="review evidence",
            ),
        },
        "items": receipt_items,
    }
    package_attestation = {
        "package_root": str(root),
        "review_manifest": _absolute_attestation(review_path),
        "package_audit": _absolute_attestation(audit_path),
    }
    try:
        # Close changes that occurred while the reviewer evidence was being
        # projected.  The validator below then re-hashes every receipt-bound
        # file, so neither a stale audit nor mid-build media drift can pass.
        _current_audit(root, audit_path)
        return human_review.validate_final_human_review(
            receipt,
            root,
            review_manifest,
            package_attestation,
        )
    except human_review.FinalHumanReviewError as exc:
        raise FinalHumanReviewBuildError(
            f"canonical final-human-review validation failed: {exc}"
        ) from exc


def _output_relative_path(root: Path, path: Path) -> tuple[Path, PurePosixPath]:
    candidate = path if path.is_absolute() else root / path
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise FinalHumanReviewBuildError(
            f"create-only output must be inside package root: {candidate}"
        ) from exc
    pure = PurePosixPath(*relative.parts)
    if (
        not pure.parts
        or pure.name in {"", ".", ".."}
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise FinalHumanReviewBuildError(
            f"create-only output path is invalid: {candidate}"
        )
    return candidate, pure


def _open_output_parent(
    root: Path,
    relative: PurePosixPath,
) -> tuple[int, list[int]]:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        expected_root = root.stat()
        root_fd = os.open(root, directory_flags)
        opened = [root_fd]
        actual_root = os.fstat(root_fd)
        if (
            actual_root.st_dev != expected_root.st_dev
            or actual_root.st_ino != expected_root.st_ino
        ):
            raise OSError("package root changed while opening output parent")
        parent_fd = root_fd
        for part in relative.parts[:-1]:
            child_fd = os.open(
                part,
                directory_flags,
                dir_fd=parent_fd,
            )
            opened.append(child_fd)
            parent_fd = child_fd
        return parent_fd, opened
    except OSError as exc:
        for descriptor in reversed(locals().get("opened", [])):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise FinalHumanReviewBuildError(
            "create-only output parent must be an existing non-symlink "
            f"directory under package root: {relative.parent}"
        ) from exc


def _secure_json_create_only(
    *,
    path: Path,
    value: Mapping[str, object],
    package_root: Path,
    before_link: Callable[[], None],
) -> Path:
    root = _package_root(package_root)
    output, relative = _output_relative_path(root, path)
    parent_fd, opened_fds = _open_output_parent(root, relative)
    output_name = relative.name
    temporary_name = (
        f".{output_name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    body = (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary_fd: int | None = None
    temporary_exists = False
    committed = False
    post_commit_errors: list[str] = []
    try:
        try:
            os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FinalHumanReviewBuildError(
                f"receipt output already exists (create-only): {output}"
            )
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_exists = True
        view = memoryview(body)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short write while building create-only JSON")
            view = view[written:]
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None

        # Re-hash/re-audit all evidence-bound inputs after initial canonical
        # validation and after the output bytes are ready, immediately before
        # the single namespace-commit operation.
        before_link()
        try:
            os.link(
                temporary_name,
                output_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise FinalHumanReviewBuildError(
                f"receipt output already exists (create-only): {output}"
            ) from exc
        committed = True

        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_exists = False
        except OSError as exc:
            post_commit_errors.append(f"temporary cleanup failed: {exc}")
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            post_commit_errors.append(f"parent fsync failed: {exc}")
        if post_commit_errors:
            raise FinalHumanReviewCommittedDurabilityUnconfirmed(
                output, "; ".join(post_commit_errors)
            )
        return output
    except FinalHumanReviewCommittedDurabilityUnconfirmed:
        raise
    except Exception as exc:
        if committed:
            post_commit_errors.append(f"post-commit operation failed: {exc}")
            raise FinalHumanReviewCommittedDurabilityUnconfirmed(
                output, "; ".join(post_commit_errors)
            ) from exc
        if isinstance(exc, FinalHumanReviewBuildError):
            raise
        raise FinalHumanReviewBuildError(
            f"failed before create-only output commit: {output}"
        ) from exc
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                # Before commit, this is a harmless private temp leak from a
                # failed operation.  After commit, the explicit status above
                # already reports cleanup/durability uncertainty.
                pass
        for descriptor in reversed(opened_fds):
            try:
                os.close(descriptor)
            except OSError:
                pass


def write_receipt_create_only(
    path: Path,
    receipt: Mapping[str, object],
    *,
    package_root: Path,
    package_audit_path: Path,
    evidence_path: Path,
    review_manifest_path: Path | None = None,
) -> Path:
    """Revalidate inputs, then atomically publish without replacing any path."""

    def revalidate() -> None:
        current = build_receipt(
            package_root=package_root,
            package_audit_path=package_audit_path,
            evidence_path=evidence_path,
            review_manifest_path=review_manifest_path,
        )
        if current != dict(receipt):
            raise FinalHumanReviewBuildError(
                "receipt/input projection drifted after initial validation"
            )

    return _secure_json_create_only(
        path=path,
        value=receipt,
        package_root=package_root,
        before_link=revalidate,
    )


def write_evidence_template_create_only(
    path: Path,
    template: Mapping[str, object],
    *,
    package_root: Path,
    package_audit_path: Path,
    review_manifest_path: Path | None = None,
) -> Path:
    """Publish a byte-bound evidence template after one last authority replay."""

    def revalidate() -> None:
        current = build_evidence_template(
            package_root=package_root,
            package_audit_path=package_audit_path,
            review_manifest_path=review_manifest_path,
        )
        if current != dict(template):
            raise FinalHumanReviewBuildError(
                "evidence-template bindings drifted before create-only commit"
            )

    return _secure_json_create_only(
        path=path,
        value=template,
        package_root=package_root,
        before_link=revalidate,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a create-only final perceptual-review receipt from completed external evidence"
        )
    )
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--package-audit", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--prepare-evidence-template",
        action="store_true",
        help=(
            "create a deliberately incomplete v2 evidence template with all "
            "current byte bindings; --out selects its create-only path"
        ),
    )
    parser.add_argument("--review-manifest", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    root = args.package_root.resolve()
    if args.prepare_evidence_template and args.evidence is not None:
        parser.error(
            "--prepare-evidence-template and --evidence are mutually exclusive"
        )
    if not args.prepare_evidence_template and args.evidence is None:
        parser.error("--evidence is required unless preparing a template")
    try:
        if args.prepare_evidence_template:
            output = (
                args.out
                or root / DEFAULT_EVIDENCE_TEMPLATE_RELATIVE_PATH
            )
            template = build_evidence_template(
                package_root=args.package_root,
                package_audit_path=args.package_audit,
                review_manifest_path=args.review_manifest,
            )
            written = write_evidence_template_create_only(
                output,
                template,
                package_root=args.package_root,
                package_audit_path=args.package_audit,
                review_manifest_path=args.review_manifest,
            )
            print(
                json.dumps(
                    {
                        "status": "EVIDENCE_TEMPLATE_CREATED",
                        "evidence_template": str(written),
                        "sha256": _sha256(written),
                        "items": len(template["items"]),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        output = args.out or root / DEFAULT_RECEIPT_RELATIVE_PATH
        assert args.evidence is not None
        receipt = build_receipt(
            package_root=args.package_root,
            package_audit_path=args.package_audit,
            evidence_path=args.evidence,
            review_manifest_path=args.review_manifest,
        )
        written = write_receipt_create_only(
            output,
            receipt,
            package_root=args.package_root,
            package_audit_path=args.package_audit,
            evidence_path=args.evidence,
            review_manifest_path=args.review_manifest,
        )
    except FinalHumanReviewCommittedDurabilityUnconfirmed as exc:
        print(
            json.dumps(
                {
                    "status": "COMMITTED_BUT_DURABILITY_UNCONFIRMED",
                    "path": str(exc.path),
                    "detail": exc.detail,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3
    except (FinalHumanReviewBuildError, OSError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "receipt": str(written),
                "sha256": _sha256(written),
                "items": len(receipt["items"]),
                "reviewed_by": receipt["reviewed_by"],
                "reviewed_at": receipt["reviewed_at"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
