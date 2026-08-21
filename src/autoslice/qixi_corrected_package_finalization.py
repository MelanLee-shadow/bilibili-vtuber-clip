"""Create-only finalization for Qixi's approved Z2 correction package.

This is deliberately not another production run.  Its input is a pair of
already-materialized, hash-bound sources:

* the operator-approved Z2 burn (and its correction/branding receipt); and
* a separately run, text-identical recovery package which supplies the fresh
  semantic, boundary, clip-context and publish evidence.

The module only projects those bytes into a new portable package.  It never
invokes ffmpeg, a title provider, a cover provider, or the selection pipeline.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.qixi_terminal_subtitle_projection import (
    TERMINAL_PROJECTION_RELATIVE_PATH,
    TerminalProjectionError,
    build_terminal_chat,
    validate_projection_assets,
)
from src.autoslice.qixi_source_fact_terminal_preservation import (
    QixiSourceFactTerminalPreservationError,
    build_terminal_preservation_review,
    validate_terminal_preservation_finalization_authority,
    validate_terminal_preservation_finalization_binding,
)
from src.autoslice.qixi_current_terminal_audit_closure import (
    AUTHORITY_RELATIVE_PATH as TERMINAL_AUDIT_CLOSURE_RELATIVE_PATH,
    QixiCurrentTerminalAuditClosureError,
    build_terminal_audit_closure,
    load_terminal_audit_closure,
    rebind_terminal_story_transcript,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
from src.autoslice.package_relocation_contract import (
    CHAT_PATH_POINTERS,
    PUBLISH_PATH_POINTERS,
    RECORD_PATH_POINTERS,
    PackageRelocationError,
    get_value,
    project_uniform_host_locators,
)
from src.autoslice.recovery_title_authority import validate_recovery_publication_authority
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    require_repository_asset_authority,
)


SCHEMA = "qixi-corrected-package-finalization-authority.v1"
RECEIPT_SCHEMA = "qixi-corrected-package-finalization-receipt.v1"
RECEIPT_FILENAME = "qixi-corrected-package-finalization.json"
AUTHORITY_RELATIVE_PATH = Path("assets/lidousha/qixi_corrected_package_finalization_authority.v1.json")
_SHA_LEN = 64
# ``record_mirror`` is a sealed read-only consistency input in the current
# terminal-projection lane.  It is deliberately not a second package artifact.
_NON_MATERIALIZED = frozenset({"source_record", "source_publish", "record_mirror"})
_BRANDING_SUMMARY_KEYS = frozenset(
    {"intro_id", "intro_media_sha256", "intro_offset_ms", "status"}
)


class QixiCorrectedPackageError(ValueError):
    """A Z2/manual-evidence finalization cannot be proved safely."""


@dataclass(frozen=True)
class _ExecutionContext:
    plan: dict[str, Any]
    repo_root: Path
    authority_path: Path
    authority_bytes: bytes
    repository_authority: RepositoryAssetAuthority
    authority: dict[str, Any]
    artifacts: dict[str, tuple[Path, str, int, str, str]]
    release_checked: Path
    evidence_checked: Path
    release_mappings: tuple[tuple[str, str], ...]
    release_workspace_root: str
    evidence_mappings: tuple[tuple[str, str], ...]
    evidence_workspace_root: str


def validate_applied_receipt(
    receipt: object, *, package_root: Path, repo_root: Path | None = None
) -> dict[str, Any]:
    """Replay a materialized Qixi receipt against its portable package bytes."""

    if not isinstance(receipt, Mapping):
        raise QixiCorrectedPackageError("finalization receipt is not an object")
    value = dict(receipt)
    required = {
        "schema_version", "mode", "candidate_id", "target_candidate_root", "package_root",
        "authority_sha256", "artifacts", "manual_corrected_same_bv", "after_image_sha256",
        "generated_record_sha256", "generated_publish_sha256", "generated_chat_authority_sha256",
    }
    if value.get("schema_version") != RECEIPT_SCHEMA or value.get("mode") != "APPLIED":
        raise QixiCorrectedPackageError("finalization receipt schema/mode is invalid")
    if package_root.is_symlink() or not package_root.is_dir():
        raise QixiCorrectedPackageError("finalization receipt package root is unsafe")
    cursor = package_root
    while cursor != cursor.parent:
        if stat.S_ISLNK(os.lstat(cursor).st_mode):
            raise QixiCorrectedPackageError("finalization receipt package ancestor is a symlink")
        cursor = cursor.parent
    root = package_root.resolve(strict=True)
    if (
        str(root) != str(value.get("package_root") or "")
        or str(root.parent) != str(value.get("target_candidate_root") or "")
    ):
        raise QixiCorrectedPackageError("finalization receipt package root differs")
    artifacts = value.get("artifacts")
    after = value.get("after_image_sha256")
    inner = value.get("manual_corrected_same_bv")
    if not isinstance(artifacts, Mapping) or not isinstance(after, Mapping) or not isinstance(inner, Mapping):
        raise QixiCorrectedPackageError("finalization receipt bindings are invalid")
    repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    sealed = _authority(
        _load_object(repo_root / AUTHORITY_RELATIVE_PATH, label="sealed finalization authority"),
        repo_root=repo_root,
    )
    generated_names = {"record", "publish", "chat_authority"}
    if sealed.get("finalization_mode") == "current_terminal_projection":
        generated_names.add("redelivery_baseline")
        required.add("generated_redelivery_baseline_sha256")
    if set(value) != required:
        raise QixiCorrectedPackageError("finalization receipt schema/mode is invalid")
    authority_path = repo_root / AUTHORITY_RELATIVE_PATH
    try:
        require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=AUTHORITY_RELATIVE_PATH,
            observed_bytes=authority_path.read_bytes(),
        )
    except ValueError as exc:
        raise QixiCorrectedPackageError("finalization receipt authority is not sealed") from exc
    if _normal_sha(value.get("authority_sha256"), label="receipt authority") != sealed["authority_sha256"]:
        raise QixiCorrectedPackageError("finalization receipt authority hash drifts")
    if value.get("candidate_id") != sealed["candidate_id"]:
        raise QixiCorrectedPackageError("finalization receipt candidate drifts")
    expected_descriptors: dict[str, Mapping[str, Any]] = {}
    for group in ("release", "evidence"):
        for name, descriptor in sealed[group].items():
            if name not in expected_descriptors:
                expected_descriptors[name] = descriptor
    if set(artifacts) != set(expected_descriptors) - _NON_MATERIALIZED:
        raise QixiCorrectedPackageError("finalization receipt artifact set drifts")
    for name, descriptor in expected_descriptors.items():
        if name in _NON_MATERIALIZED:
            continue
        actual = artifacts.get(name)
        if not isinstance(actual, Mapping) or set(actual) != {"target_role", "target", "sha256", "bytes"} or dict(actual) != {
            "target_role": descriptor["target_role"],
            "target": descriptor["target"],
            "sha256": _normal_sha(descriptor["sha256"], label=f"sealed {name}"),
            "bytes": descriptor["bytes"],
        }:
            raise QixiCorrectedPackageError(f"finalization receipt artifact descriptor drifts: {name}")
    if (
        set(inner) != {
            "schema_version", "candidate_id", "recovery_publication_authority",
            "approved_burned_video_sha256", "approved_subtitle_sha256", "approved_cover_sha256",
        }
        or inner.get("schema_version") != "manual-corrected-same-bv.v1"
        or inner.get("candidate_id") != sealed["candidate_id"]
        or inner.get("recovery_publication_authority") != sealed["recovery_publication_authority"]
    ):
        raise QixiCorrectedPackageError("finalization receipt same-BV binding is invalid")
    if set(after) != generated_names:
        raise QixiCorrectedPackageError("finalization receipt after-image keys drift")
    expected_files = {
        "record": "generated_record_sha256",
        "publish": "generated_publish_sha256",
        "chat_authority": "generated_chat_authority_sha256",
    }
    if "redelivery_baseline" in generated_names:
        expected_files["redelivery_baseline"] = "generated_redelivery_baseline_sha256"
    for name, receipt_key in expected_files.items():
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, Mapping):
            raise QixiCorrectedPackageError(f"receipt lacks {name} artifact")
        role, target = descriptor.get("target_role"), descriptor.get("target")
        if role not in {"package", "candidate"} or not isinstance(target, str):
            raise QixiCorrectedPackageError(f"receipt {name} artifact is invalid")
        base = root if role == "package" else root.parent
        path = _contained_regular(base, target, label=f"receipt {name}")
        after_entry = after.get(name)
        if not isinstance(after_entry, Mapping) or set(after_entry) != {"sha256", "bytes"}:
            raise QixiCorrectedPackageError(f"receipt {name} after-image is invalid")
        actual = _sha256(path)
        if (
            actual != _normal_sha(value.get(receipt_key), label=f"receipt {name}")
            or actual != _normal_sha(after_entry.get("sha256"), label=f"after {name}")
            or path.stat().st_size != after_entry.get("bytes")
        ):
            raise QixiCorrectedPackageError(f"receipt {name} hash drifts")
    for name, field in (("video", "approved_burned_video_sha256"), ("subtitle", "approved_subtitle_sha256"), ("cover", "approved_cover_sha256")):
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, Mapping):
            raise QixiCorrectedPackageError(f"receipt lacks {name} artifact")
        base = root if descriptor.get("target_role") == "package" else root.parent
        actual = _sha256(_contained_regular(base, descriptor.get("target"), label=f"receipt {name}"))
        if actual != _normal_sha(inner.get(field), label=f"inner {field}"):
            raise QixiCorrectedPackageError(f"receipt {name} truth hash drifts")
        if actual != _normal_sha(sealed["release"][name]["sha256"], label=f"sealed {name}"):
            raise QixiCorrectedPackageError(f"receipt {name} authority hash drifts")
    for name, descriptor in artifacts.items():
        assert isinstance(descriptor, Mapping)
        base = root if descriptor["target_role"] == "package" else root.parent
        path = _contained_regular(base, descriptor["target"], label=f"receipt {name}")
        expected_sha = (
            _normal_sha(after[name]["sha256"], label=f"after {name}")
            if name in generated_names
            else _normal_sha(descriptor["sha256"], label=f"artifact {name}")
        )
        expected_bytes = after[name]["bytes"] if name in generated_names else descriptor["bytes"]
        if path.stat().st_size != expected_bytes or _sha256(path) != expected_sha:
            raise QixiCorrectedPackageError(f"receipt materialized artifact drifts: {name}")
    if sealed.get("finalization_mode") == "current_terminal_projection":
        record_path = _contained_regular(root, artifacts["record"]["target"], label="receipt record")
        publish_path = _contained_regular(root, artifacts["publish"]["target"], label="receipt publish")
        subtitle_path = _contained_regular(root, artifacts["subtitle"]["target"], label="receipt subtitle")
        record = _load_object(record_path, label="receipt record")
        publish = _load_object(publish_path, label="receipt publish")
        story = record.get("story_contract")
        staging = record.get("publish_staging")
        review = story.get("source_fact_review") if isinstance(story, Mapping) else None
        if not (
            isinstance(story, Mapping)
            and isinstance(staging, Mapping)
            and review == staging.get("source_fact_review") == publish.get("source_fact_review")
        ):
            raise QixiCorrectedPackageError("current terminal source-fact surfaces drift")
        transcript = "\n".join(
            cue.text.strip()
            for cue in parse_srt_cues(subtitle_path.read_text(encoding="utf-8"))
            if cue.text.strip()
        )
        speaker_evidence = {
            "policy_ids": {
                "absence": "speaker_mode_uniform_host/v1",
                "alignment": "speaker_cue_subsegment_alignment/v1",
                "text": "compact_ws/v1",
                "timing": "half_open_integer_ms_exact/v1",
            },
            "reason": "speaker_mode_uniform_host",
            "state": "AbsentAuthorized",
        }
        try:
            source_fact_ok = validate_terminal_preservation_finalization_binding(
                review=review,
                finalization_receipt=value,
                repo_root=repo_root,
                subtitle_text=subtitle_path.read_text(encoding="utf-8"),
                final_transcript=transcript,
                title=str(publish.get("title") or ""),
                selection_hook=str(story.get("selection_hook") or ""),
                clip_context_prompt=str(story.get("clip_context_prompt") or ""),
                selection_scorecard=story.get("selection_scorecard"),
                speaker_evidence=speaker_evidence,
                correction_sha256=sealed["release"]["correction"]["sha256"],
                ass_repair_receipt_sha256=sealed["release"]["ass_repair_receipt"]["sha256"],
                recovery_publication_authority=sealed["recovery_publication_authority"],
                finalization_authority_sha256=sealed["authority_sha256"],
            )
        except (OSError, ValueError) as exc:
            raise QixiCorrectedPackageError("current terminal source-fact preservation is unreadable") from exc
        if not source_fact_ok:
            raise QixiCorrectedPackageError("current terminal source-fact preservation drifts")
    return value


def validate_manifest_bound_applied_receipt(
    item: Mapping[str, object],
    *,
    candidate_id: str,
    package_root: Path,
    repo_root: Path | None = None,
) -> bool:
    """Replay a typed manifest receipt and bind its file bytes to the item.

    ``False`` means this is an ordinary item with no typed Qixi fields.  A
    partial or drifted typed binding always raises.
    """

    fields = {
        "manual_corrected_same_bv",
        "manual_corrected_same_bv_receipt",
        "manual_corrected_same_bv_receipt_sha256",
    }
    present = fields & set(item)
    if not present:
        return False
    if present != fields or not isinstance(item.get("manual_corrected_same_bv"), Mapping):
        raise QixiCorrectedPackageError("manifest typed receipt fields are invalid")
    if item.get("candidate_id") != candidate_id:
        raise QixiCorrectedPackageError("manifest typed receipt candidate drifts")
    embedded = item["manual_corrected_same_bv"]
    receipt_name = item.get("manual_corrected_same_bv_receipt")
    if receipt_name != RECEIPT_FILENAME:
        raise QixiCorrectedPackageError("manifest typed receipt filename is invalid")
    receipt = _contained_regular(
        package_root,
        receipt_name,
        label="manual corrected same-BV receipt",
    )
    try:
        receipt_bytes = receipt.read_bytes()
        receipt_object = json.loads(receipt_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCorrectedPackageError("manifest typed receipt is unreadable") from exc
    if "sha256:" + hashlib.sha256(receipt_bytes).hexdigest() != _normal_sha(
        item.get("manual_corrected_same_bv_receipt_sha256"),
        label="manifest receipt",
    ):
        raise QixiCorrectedPackageError("manifest receipt hash drifts")
    replayed = validate_applied_receipt(
        receipt_object,
        package_root=package_root,
        repo_root=repo_root,
    )
    if (
        replayed.get("manual_corrected_same_bv") != dict(embedded)
        or embedded.get("candidate_id") != candidate_id
        or embedded.get("recovery_publication_authority")
        != item.get("recovery_publication_authority")
    ):
        raise QixiCorrectedPackageError("manifest same-BV receipt binding drifts")
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normal_sha(value: object, *, label: str) -> str:
    result = str(value or "").removeprefix("sha256:")
    if len(result) != _SHA_LEN or any(char not in "0123456789abcdef" for char in result):
        raise QixiCorrectedPackageError(f"{label} must be a sha256")
    return "sha256:" + result


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise QixiCorrectedPackageError(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCorrectedPackageError(f"{label} is unreadable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise QixiCorrectedPackageError(f"{label} must be a JSON object")
    return value


def _load_sealed_authority(
    repo_root: Path,
) -> tuple[Path, bytes, dict[str, Any], RepositoryAssetAuthority]:
    authority_path = repo_root / AUTHORITY_RELATIVE_PATH
    if authority_path.is_symlink() or not authority_path.is_file():
        raise QixiCorrectedPackageError("finalization authority is not a regular file")
    try:
        authority_bytes = authority_path.read_bytes()
        authority_raw = json.loads(authority_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCorrectedPackageError("finalization authority is unreadable") from exc
    try:
        repository_authority = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=AUTHORITY_RELATIVE_PATH,
            observed_bytes=authority_bytes,
        )
    except ValueError as exc:
        raise QixiCorrectedPackageError(f"finalization authority is not repository sealed: {exc}") from exc
    return (
        authority_path,
        authority_bytes,
        _authority(authority_raw, repo_root=repo_root),
        repository_authority,
    )


def _assert_context_authority_unchanged(context: _ExecutionContext) -> None:
    try:
        current = context.authority_path.read_bytes()
    except OSError as exc:
        raise QixiCorrectedPackageError("finalization authority disappeared during apply") from exc
    if current != context.authority_bytes:
        raise QixiCorrectedPackageError("finalization authority changed after planning")
    try:
        current_repository_authority = require_repository_asset_authority(
            repo_root=context.repo_root,
            relative_path=AUTHORITY_RELATIVE_PATH,
            observed_bytes=context.authority_bytes,
        )
    except ValueError as exc:
        raise QixiCorrectedPackageError(
            "finalization authority epoch changed after planning"
        ) from exc
    if current_repository_authority != context.repository_authority:
        raise QixiCorrectedPackageError("finalization authority epoch changed after planning")
    if context.authority.get("finalization_mode") == "current_terminal_projection":
        try:
            projection = validate_projection_assets(context.repo_root)
        except TerminalProjectionError as exc:
            raise QixiCorrectedPackageError(
                "terminal projection assets changed after planning"
            ) from exc
        terminal = context.authority.get("terminal_projection")
        if not isinstance(terminal, Mapping) or _normal_sha(
            terminal.get("authority_sha256"), label="terminal projection"
        ) != projection["authority_sha256"]:
            raise QixiCorrectedPackageError(
                "terminal projection authority changed after planning"
            )


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise QixiCorrectedPackageError(f"{label} path is missing")
    path = Path(value)
    if path.is_absolute() or not path.parts or "." in path.parts or ".." in path.parts:
        raise QixiCorrectedPackageError(f"{label} path is unsafe")
    return path


def _contained_regular(root: Path, relative: object, *, label: str) -> Path:
    rel = _safe_relative(relative, label=label)
    cursor = root
    for part in rel.parts:
        cursor /= part
        try:
            metadata = os.lstat(cursor)
        except OSError as exc:
            raise QixiCorrectedPackageError(f"{label} is unavailable: {rel}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise QixiCorrectedPackageError(f"{label} contains a symlink: {rel}")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise QixiCorrectedPackageError(f"{label} escapes its source root: {rel}") from exc
    if not resolved.is_file():
        raise QixiCorrectedPackageError(f"{label} is not a regular file: {rel}")
    return resolved


def _source_root(root: Path, *, label: str) -> Path:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise QixiCorrectedPackageError(f"{label} root must be an absolute non-symlink directory")
    return root.resolve(strict=True)


def _build_terminal_projection_chat(
    *,
    repo_root: Path,
    correction: Mapping[str, object],
    correction_sha256: str,
    record: Mapping[str, object],
    record_sha256: str,
    subtitle_text: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        return build_terminal_chat(
            repo_root=repo_root,
            correction=correction,
            correction_sha256=correction_sha256,
            record=record,
            record_sha256=record_sha256,
            subtitle_text=subtitle_text,
        )
    except TerminalProjectionError as exc:
        raise QixiCorrectedPackageError(str(exc)) from exc


def _artifact(
    root: Path, value: object, *, label: str
) -> tuple[Path, str, int, str, str]:
    if not isinstance(value, Mapping):
        raise QixiCorrectedPackageError(f"{label} descriptor is missing")
    expected_keys = {"source", "sha256", "bytes", "target", "target_role"}
    if set(value) != expected_keys:
        raise QixiCorrectedPackageError(f"{label} descriptor schema is invalid")
    source = _contained_regular(root, value.get("source"), label=label)
    expected_sha = _normal_sha(value.get("sha256"), label=label)
    expected_bytes = value.get("bytes")
    if isinstance(expected_bytes, bool) or not isinstance(expected_bytes, int) or expected_bytes < 0:
        raise QixiCorrectedPackageError(f"{label} bytes are invalid")
    if source.stat().st_size != expected_bytes or _sha256(source) != expected_sha:
        raise QixiCorrectedPackageError(f"{label} source bytes drifted")
    target = _safe_relative(value.get("target"), label=label).as_posix()
    target_role = value.get("target_role")
    if target_role not in {"package", "candidate"}:
        raise QixiCorrectedPackageError(f"{label} target role is invalid")
    return source, expected_sha, expected_bytes, target, target_role


def _authority(value: object, *, repo_root: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA:
        raise QixiCorrectedPackageError("finalization authority schema is invalid")
    required = {
        "schema_version", "candidate_id", "title", "recovery_publication_authority",
        "release", "evidence", "source_drift", "authority_sha256",
        "roots",
    }
    current_mode = value.get("finalization_mode") == "current_terminal_projection"
    if current_mode:
        required |= {
            "finalization_mode",
            "terminal_projection",
            "source_fact_terminal_preservation",
            "terminal_audit_closure",
        }
    if set(value) != required:
        raise QixiCorrectedPackageError("finalization authority keys are invalid")
    authority = dict(value)
    claimed = _normal_sha(authority.pop("authority_sha256"), label="authority")
    if claimed != _canonical_sha(authority):
        raise QixiCorrectedPackageError("finalization authority hash is invalid")
    authority["authority_sha256"] = claimed
    candidate_id = authority.get("candidate_id")
    title = authority.get("title")
    if not isinstance(candidate_id, str) or not candidate_id or not isinstance(title, str) or not title:
        raise QixiCorrectedPackageError("finalization candidate/title is invalid")
    try:
        publication = validate_recovery_publication_authority(
            authority["recovery_publication_authority"],
            candidate_id=candidate_id,
            expected_final_title=title,
            repo_root=repo_root,
        )
    except ValueError as exc:
        raise QixiCorrectedPackageError(f"recovery publication authority invalid: {exc}") from exc
    authority["recovery_publication_authority"] = publication
    for group in ("release", "evidence"):
        items = authority.get(group)
        if not isinstance(items, Mapping) or not items:
            raise QixiCorrectedPackageError(f"{group} artifacts are missing")
    drift = authority.get("source_drift")
    legacy_drift = {
        "source_publish_burned_video_sha256",
        "source_publish_subtitle_sha256",
        "source_record_ass_sha256",
        "source_burned_preview_sha256",
        "source_cover_generation_sha256",
        "r2_publish_burned_video_sha256",
        "r2_publish_subtitle_sha256",
    }
    current_drift = {
        "source_publish_burned_video_sha256",
        "source_publish_subtitle_sha256",
        "source_record_ass_sha256",
        "source_burned_preview_sha256",
        "source_cover_generation_sha256",
        "source_record_sha256",
        "source_record_mirror_sha256",
        "ass_repair_receipt_sha256",
    }
    if not isinstance(drift, Mapping) or set(drift) != (current_drift if current_mode else legacy_drift):
        raise QixiCorrectedPackageError("source_drift schema is invalid")
    for key, raw in drift.items():
        drift[key] = _normal_sha(raw, label=key)
    roots = authority.get("roots")
    if not isinstance(roots, Mapping) or set(roots) != {
        "source_package_relative",
        "source_candidate_relative",
    }:
        raise QixiCorrectedPackageError("finalization root layout is invalid")
    normalized_roots: dict[str, str] = {}
    for key, raw in roots.items():
        relative = _safe_relative(raw, label=key)
        normalized_roots[key] = relative.as_posix()
    authority["roots"] = normalized_roots
    if current_mode:
        if authority.get("finalization_mode") != "current_terminal_projection":
            raise QixiCorrectedPackageError("finalization mode is invalid")
        terminal_projection = authority.get("terminal_projection")
        if not isinstance(terminal_projection, Mapping) or set(terminal_projection) != {
            "relative_path", "authority_sha256"
        } or _safe_relative(
            terminal_projection.get("relative_path"), label="terminal projection"
        ) != TERMINAL_PROJECTION_RELATIVE_PATH:
            raise QixiCorrectedPackageError("terminal projection authority is invalid")
        try:
            projection = validate_projection_assets(repo_root)
        except TerminalProjectionError as exc:
            raise QixiCorrectedPackageError(str(exc)) from exc
        if _normal_sha(
            terminal_projection.get("authority_sha256"), label="terminal projection"
        ) != projection["authority_sha256"]:
            raise QixiCorrectedPackageError("terminal projection authority is invalid")
        try:
            validate_terminal_preservation_finalization_authority(
                repo_root=repo_root,
                authority_sha256=authority["authority_sha256"],
            )
        except QixiSourceFactTerminalPreservationError as exc:
            raise QixiCorrectedPackageError(str(exc)) from exc
        closure = authority.get("terminal_audit_closure")
        if not isinstance(closure, Mapping) or set(closure) != {
            "relative_path", "authority_sha256"
        } or _safe_relative(
            closure.get("relative_path"), label="terminal audit closure"
        ) != TERMINAL_AUDIT_CLOSURE_RELATIVE_PATH:
            raise QixiCorrectedPackageError("terminal audit closure authority is invalid")
        try:
            replayed_closure = load_terminal_audit_closure(repo_root)
        except QixiCurrentTerminalAuditClosureError as exc:
            raise QixiCorrectedPackageError(str(exc)) from exc
        if _normal_sha(
            closure.get("authority_sha256"), label="terminal audit closure"
        ) != replayed_closure["authority_sha256"]:
            raise QixiCorrectedPackageError("terminal audit closure authority is invalid")
    package_relative = Path(normalized_roots["source_package_relative"])
    candidate_relative = Path(normalized_roots["source_candidate_relative"])
    try:
        package_relative.relative_to(candidate_relative)
    except ValueError as exc:
        raise QixiCorrectedPackageError(
            "source package root must be inside the candidate root"
        ) from exc
    return authority


def _copy_checked(
    source: Path,
    destination: Path,
    expected_sha: str,
    *,
    owned: dict[Path, tuple[int, int, int]],
) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        _own_created(destination, owned, handle=writer)
        shutil.copyfileobj(reader, writer, length=1 << 20)
        writer.flush()
        os.fsync(writer.fileno())
    if _sha256(destination) != expected_sha:
        raise QixiCorrectedPackageError(f"copied artifact hash drifted: {destination.name}")


def _own_created(
    path: Path,
    owned: dict[Path, tuple[int, int, int]],
    *,
    handle: Any | None = None,
) -> None:
    if path in owned:
        raise QixiCorrectedPackageError("staging path was already owned")
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise QixiCorrectedPackageError("staging ownership encountered a symlink")
    identity = (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))
    if handle is not None:
        descriptor = os.fstat(handle.fileno())
        descriptor_identity = (
            descriptor.st_dev,
            descriptor.st_ino,
            stat.S_IFMT(descriptor.st_mode),
        )
        if not stat.S_ISREG(descriptor.st_mode) or descriptor_identity != identity:
            raise QixiCorrectedPackageError("staging writer ownership identity changed")
    owned[path] = identity


def _assert_owned_tree(root: Path, owned: Mapping[Path, tuple[int, int, int]]) -> None:
    current = {root, *root.rglob("*")}
    if current != set(owned):
        raise QixiCorrectedPackageError("staging ownership path set changed")
    for path, expected in owned.items():
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or (
            metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)
        ) != expected:
            raise QixiCorrectedPackageError("staging ownership inode changed")


def _mkdir_owned(
    root: Path,
    directory: Path,
    owned: dict[Path, tuple[int, int, int]],
) -> None:
    try:
        parts = directory.relative_to(root).parts
    except ValueError as exc:
        raise QixiCorrectedPackageError("staging directory escapes root") from exc
    cursor = root
    for part in parts:
        cursor /= part
        try:
            cursor.mkdir()
        except FileExistsError:
            if cursor not in owned:
                raise QixiCorrectedPackageError("staging directory was not created by this transaction")
        else:
            _own_created(cursor, owned)


def _unlink_owned(path: Path, owned: dict[Path, tuple[int, int, int]]) -> None:
    if path not in owned:
        raise QixiCorrectedPackageError("staging replacement path is not owned")
    path.unlink()
    owned.pop(path)


def _rollback_owned_tree(
    root: Path, owned: Mapping[Path, tuple[int, int, int]]
) -> None:
    _assert_owned_tree(root, owned)
    for path in sorted(owned, key=lambda value: len(value.parts), reverse=True):
        if path == root:
            continue
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _write_json_new(
    path: Path,
    value: Mapping[str, Any],
    *,
    owned: dict[Path, tuple[int, int, int]],
) -> None:
    payload = _json_bytes(value)
    with path.open("xb") as handle:
        _own_created(path, owned, handle=handle)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _target_path(
    stage: Path, target: str, *, owned: dict[Path, tuple[int, int, int]]
) -> Path:
    path = stage / _safe_relative(target, label="staging artifact")
    _mkdir_owned(stage, path.parent, owned)
    return path


def _final_path(root: Path, target: str) -> Path:
    """A locator persisted in JSON must survive stage -> final rename."""

    return root / _safe_relative(target, label="package artifact")


def _artifact_final_path(
    candidate_root: Path, artifact: tuple[Path, str, int, str, str]
) -> Path:
    _source, _sha, _bytes, target, role = artifact
    root = candidate_root / "replacement_recuts" if role == "package" else candidate_root
    return _final_path(root, target)


def _candidate_package_root(candidate_root: Path) -> Path:
    return candidate_root / "replacement_recuts"


def _validate_create_only_target(target: Path) -> Path:
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        raise QixiCorrectedPackageError("target package path must be an absolute directory path")
    if target.exists() or target.is_symlink():
        raise QixiCorrectedPackageError("target package must be absent and create-only")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise QixiCorrectedPackageError("target parent must be an existing non-symlink directory")
    cursor = parent
    while cursor != cursor.parent:
        if cursor.is_symlink():
            raise QixiCorrectedPackageError("target ancestor may not be a symlink")
        cursor = cursor.parent
    return target


def _verify_cross_surface(
    *,
    authority: Mapping[str, Any],
    release_root: Path,
    evidence_root: Path,
) -> dict[str, tuple[Path, str, int, str, str]]:
    release_root = _source_root(release_root, label="release")
    evidence_root = _source_root(evidence_root, label="evidence")
    selected: dict[str, tuple[Path, str, int, str, str]] = {}
    for source_name, root in (("release", release_root), ("evidence", evidence_root)):
        values = authority[source_name]
        assert isinstance(values, Mapping)
        for name, descriptor in values.items():
            if not isinstance(name, str):
                raise QixiCorrectedPackageError(f"invalid artifact key: {name}")
            artifact = _artifact(root, descriptor, label=f"{source_name}.{name}")
            if name in selected:
                # Identically hashed duplicate descriptors are an explicit
                # cross-source equality proof, not two files to copy.
                if selected[name][1:3] != artifact[1:3]:
                    raise QixiCorrectedPackageError(
                        f"fresh evidence {name} differs from release truth"
                    )
                continue
            selected[name] = artifact
    mandatory = {
        "video", "main", "subtitle", "ass", "correction", "record", "publish", "chat_authority",
        "clip_context", "redelivery_baseline", "cover", "cover_pre_overlay",
        "cover_title_mask", "cover_route_background", "cover_reference",
        "cover_host_witness", "cover_source_composition", "boundary_audit",
        "review_flags", "source_record", "source_publish",
    }
    if authority.get("finalization_mode") == "current_terminal_projection":
        mandatory = {
            "video", "main", "subtitle", "ass", "correction", "record", "record_mirror",
            "publish", "chat_authority", "clip_context", "redelivery_baseline", "ass_repair_receipt",
            "cover", "cover_pre_overlay", "cover_title_mask", "cover_route_background",
            "cover_reference", "cover_host_witness", "cover_source_composition", "boundary_audit",
            "review_flags",
        }
    if not mandatory.issubset(selected):
        raise QixiCorrectedPackageError("finalization authority lacks a mandatory artifact")
    # The semantic evidence may come from a different path, but must describe
    # the exact release subtitle/ASS bytes before it can be projected.
    return selected


def _target_overlaps_source(target: Path, source_root: Path) -> bool:
    """A package must never be staged inside either immutable source tree."""

    try:
        target.relative_to(source_root)
        return True
    except ValueError:
        return False


def _relocation_mappings(
    *, authority: Mapping[str, Any], evidence_root: Path, candidate_root: Path, repo_root: Path
) -> tuple[tuple[tuple[str, str], ...], str]:
    """Build the sole package/candidate/repo mapping from sealed layout data."""

    roots = authority["roots"]
    assert isinstance(roots, Mapping)
    source_candidate = (evidence_root / str(roots["source_candidate_relative"])).resolve()
    source_package = (evidence_root / str(roots["source_package_relative"])).resolve()
    if source_candidate.is_symlink() or source_package.is_symlink():
        raise QixiCorrectedPackageError("sealed source root contains a symlink")
    if not source_candidate.is_dir() or not source_package.is_dir():
        raise QixiCorrectedPackageError("sealed source root is unavailable")
    try:
        source_package.relative_to(source_candidate)
    except ValueError as exc:
        raise QixiCorrectedPackageError("sealed package root escapes candidate root") from exc
    return (
        (
            (str(source_package), str(_candidate_package_root(candidate_root))),
            (str(source_candidate), str(candidate_root)),
            (str(repo_root), str(repo_root)),
        ),
        str(evidence_root),
    )


def _release_relocation_mappings(
    *, release_root: Path, candidate_root: Path, repo_root: Path
) -> tuple[tuple[tuple[str, str], ...], str]:
    """Project the Z2 output base, whose package root is ``release_root``."""

    source_package = _source_root(release_root, label="release")
    source_candidate = source_package.parent
    if source_candidate.is_symlink() or not source_candidate.is_dir():
        raise QixiCorrectedPackageError("release candidate root is unsafe")
    return (
        (
            (str(source_package), str(_candidate_package_root(candidate_root))),
            (str(source_candidate), str(candidate_root)),
            (str(repo_root), str(repo_root)),
        ),
        str(source_candidate),
    )


def _bind_chat_release_locators(
    *,
    chat: dict[str, Any],
    record: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    candidate_root: Path,
) -> None:
    """Bind regenerated chat leaves to the exact Z2 files this plan materializes."""

    subtitle_path = str(_artifact_final_path(candidate_root, artifacts["subtitle"]))
    ass_path = str(_artifact_final_path(candidate_root, artifacts["ass"]))
    if not isinstance(chat.get("final_text_srt_path"), str):
        raise QixiCorrectedPackageError("fresh chat lacks final text SRT locator")
    chat["final_text_srt_path"] = subtitle_path
    if chat.get("final_speaker_srt_path") is not None:
        if (
            record.get("speaker_mode") != "uniform_host"
            or _normal_sha(
                chat.get("final_speaker_srt_sha256"),
                label="fresh chat speaker SRT",
            )
            != artifacts["subtitle"][1]
        ):
            raise QixiCorrectedPackageError("fresh chat speaker SRT cannot bind the uniform release subtitle")
        chat["final_speaker_srt_path"] = subtitle_path
    if chat.get("speaker_ass_path") is not None:
        if _normal_sha(chat.get("speaker_ass_sha256"), label="fresh chat speaker ASS") != artifacts["ass"][1]:
            raise QixiCorrectedPackageError("fresh chat speaker ASS cannot bind the release ASS")
        chat["speaker_ass_path"] = ass_path


def _assert_planned_locator_closure(
    *,
    record: Mapping[str, Any],
    publish: Mapping[str, Any],
    chat: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    candidate_root: Path,
) -> None:
    """Every mutable persisted locator must name one exact planned artifact."""

    planned = {
        str(_artifact_final_path(candidate_root, artifact))
        for name, artifact in artifacts.items()
        if name not in _NON_MATERIALIZED
    }
    for kind, document, pointers in (
        ("record", record, RECORD_PATH_POINTERS),
        ("publish", publish, PUBLISH_PATH_POINTERS),
        ("chat", chat, CHAT_PATH_POINTERS),
    ):
        for pointer in pointers:
            value = get_value(document, pointer)
            if value is None:
                continue
            if not isinstance(value, str) or value not in planned:
                raise QixiCorrectedPackageError(
                    f"{kind} mutable locator is outside the materialized plan: {'/'.join(pointer)}"
                )


def _without_superseded_r2_burned_preview(
    *,
    record: Mapping[str, Any],
    publish: Mapping[str, Any],
    authority: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove only the sealed r2 Z1 carrier before locator projection.

    The r2 record is evidence for text/semantic/boundary surfaces, while its
    entire Z1 ``burned_preview`` is intentionally superseded by the sealed Z2
    source subtree below.  Validate the outgoing carrier against r2's sealed
    publish identity first; no other frozen evidence is exempt from locator
    validation.
    """

    r2_burned = record.get("burned_preview")
    publish_hashes = publish.get("artifact_hashes")
    drift = authority.get("source_drift")
    if (
        not isinstance(r2_burned, Mapping)
        or not isinstance(publish_hashes, Mapping)
        or not isinstance(drift, Mapping)
    ):
        raise QixiCorrectedPackageError("r2 burned-preview supersession evidence is missing")
    r2_publish_burned = _normal_sha(
        publish_hashes.get("burned_video_sha256"),
        label="r2 publish burned hash",
    )
    r2_record_burned = _normal_sha(
        r2_burned.get("burned_sha256"),
        label="r2 burned-preview hash",
    )
    sealed_r2_burned = _normal_sha(
        drift.get("r2_publish_burned_video_sha256"),
        label="sealed r2 publish burned hash",
    )
    if r2_record_burned != r2_publish_burned or r2_publish_burned != sealed_r2_burned:
        raise QixiCorrectedPackageError("r2 burned-preview supersession hash differs")
    projected = dict(record)
    projected.pop("burned_preview")
    return projected


def _validate_source_burned_branding_summary(
    *,
    source_burned: Mapping[str, Any],
    correction_branding: object,
) -> None:
    """Match correction's strict four-field summary to source's full proof."""

    source_branding = source_burned.get("branding_intro")
    if not isinstance(source_branding, Mapping) or not isinstance(correction_branding, Mapping):
        raise QixiCorrectedPackageError("source Z2 burned branding summary is missing")
    if set(correction_branding) != _BRANDING_SUMMARY_KEYS or not _BRANDING_SUMMARY_KEYS.issubset(
        source_branding
    ):
        raise QixiCorrectedPackageError("source Z2 burned branding summary schema differs")
    for key in ("intro_id", "status"):
        source_value = source_branding.get(key)
        correction_value = correction_branding.get(key)
        if (
            not isinstance(source_value, str)
            or not isinstance(correction_value, str)
            or source_value != correction_value
        ):
            raise QixiCorrectedPackageError(
                f"source Z2 burned branding summary differs: {key}"
            )
    source_offset = source_branding.get("intro_offset_ms")
    correction_offset = correction_branding.get("intro_offset_ms")
    if (
        isinstance(source_offset, bool)
        or not isinstance(source_offset, int)
        or isinstance(correction_offset, bool)
        or not isinstance(correction_offset, int)
        or source_offset != correction_offset
    ):
        raise QixiCorrectedPackageError(
            "source Z2 burned branding summary differs: intro_offset_ms"
        )
    if _normal_sha(
        source_branding.get("intro_media_sha256"), label="source Z2 branding intro media"
    ) != _normal_sha(
        correction_branding.get("intro_media_sha256"),
        label="correction branding intro media",
    ):
        raise QixiCorrectedPackageError(
            "source Z2 burned branding summary differs: intro_media_sha256"
        )


def _project_documents(
    *,
    authority: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    candidate_root: Path,
    release_mappings: tuple[tuple[str, str], ...],
    release_workspace_root: str,
    evidence_mappings: tuple[tuple[str, str], ...],
    evidence_workspace_root: str,
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    if authority.get("finalization_mode") == "current_terminal_projection":
        return _project_current_terminal_documents(
            authority=authority,
            artifacts=artifacts,
            candidate_root=candidate_root,
            release_mappings=release_mappings,
            release_workspace_root=release_workspace_root,
            evidence_mappings=evidence_mappings,
            evidence_workspace_root=evidence_workspace_root,
            repo_root=repo_root,
        )
    candidate_id = str(authority["candidate_id"])
    title = str(authority["title"])
    publication = authority["recovery_publication_authority"]
    assert isinstance(publication, dict)
    record = _load_object(artifacts["record"][0], label="r2 fresh record")
    publish = _load_object(artifacts["publish"][0], label="r2 fresh publish")
    source_record = _load_object(artifacts["source_record"][0], label="Z2 source record")
    source_publish = _load_object(artifacts["source_publish"][0], label="Z2 source publish")
    chat = _load_object(artifacts["chat_authority"][0], label="fresh chat authority")
    # The sealed r2 burn is Z1 and its frozen branding paths need not exist in
    # the current evidence workspace.  Its complete carrier is replaced below
    # by the sealed Z2 source subtree, but only after the exact r2 hash check.
    record = _without_superseded_r2_burned_preview(
        record=record,
        publish=publish,
        authority=authority,
    )
    try:
        record = project_uniform_host_locators(
            record,
            kind="record",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
        )
        publish = project_uniform_host_locators(
            publish,
            kind="publish",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
        )
        chat = project_uniform_host_locators(
            chat,
            kind="chat",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
        )
    except PackageRelocationError as exc:
        raise QixiCorrectedPackageError(f"fresh evidence locator contract failed: {exc}") from exc
    correction = _load_object(artifacts["correction"][0], label="Z2 correction receipt")
    if publish.get("candidate_id") != candidate_id or source_publish.get("candidate_id") != candidate_id:
        raise QixiCorrectedPackageError("source/r2 publish candidate differs")
    if correction.get("candidate_id") != candidate_id or correction.get("upload_enabled") is not False:
        raise QixiCorrectedPackageError("Z2 correction receipt is not a no-upload candidate receipt")
    if _normal_sha(correction.get("after_srt_sha256"), label="correction after subtitle") != artifacts["subtitle"][1]:
        raise QixiCorrectedPackageError("Z2 correction receipt subtitle differs")
    if _normal_sha(correction.get("burned_media_sha256"), label="correction burned video") != artifacts["video"][1]:
        raise QixiCorrectedPackageError("Z2 correction receipt video differs")
    original_publish_hashes = publish.get("artifact_hashes")
    source_publish_hashes = source_publish.get("artifact_hashes")
    if not isinstance(original_publish_hashes, Mapping) or not isinstance(source_publish_hashes, Mapping):
        raise QixiCorrectedPackageError("source/r2 publish artifact hashes are missing")
    drift = authority["source_drift"]
    if (
        _normal_sha(source_publish_hashes.get("burned_video_sha256"), label="source publish burned hash") != drift["source_publish_burned_video_sha256"]
        or _normal_sha(source_publish_hashes.get("subtitle_sha256"), label="source publish subtitle hash") != drift["source_publish_subtitle_sha256"]
        or _normal_sha(original_publish_hashes.get("burned_video_sha256"), label="r2 publish burned hash") != drift["r2_publish_burned_video_sha256"]
        or _normal_sha(original_publish_hashes.get("subtitle_sha256"), label="r2 publish subtitle hash") != drift["r2_publish_subtitle_sha256"]
    ):
        raise QixiCorrectedPackageError("source/r2 stale diagnostic hashes drifted")
    source_hashes = source_record.get("artifact_hashes")
    if not isinstance(source_hashes, Mapping) or (
        _normal_sha(source_hashes.get("ass_sha256"), label="source record ASS hash")
        != drift["source_record_ass_sha256"]
    ):
        raise QixiCorrectedPackageError("source Z2 record stale ASS hash drifted")
    source_staging_raw = source_record.get("publish_staging")
    source_generation = (
        source_staging_raw.get("cover_generation")
        if isinstance(source_staging_raw, Mapping)
        else None
    )
    publish_generation = source_publish.get("cover_generation")
    if (
        source_record.get("cover_status") not in {None, "AI_COVER_READY"}
        or source_publish.get("cover_status") != "AI_COVER_READY"
        or not isinstance(source_generation, Mapping)
        or source_generation != publish_generation
        or _canonical_sha(source_generation) != drift["source_cover_generation_sha256"]
        or _normal_sha(source_generation.get("final_cover_sha256"), label="source cover")
        != artifacts["cover"][1]
    ):
        raise QixiCorrectedPackageError("source AI cover generation is not the sealed public cover")
    if source_record.get("duration_ms") != record.get("duration_ms"):
        raise QixiCorrectedPackageError("source/r2 duration differs")
    source_boundary = source_record.get("boundary_audit")
    r2_boundary = record.get("boundary_audit")
    if not isinstance(source_boundary, Mapping) or not isinstance(r2_boundary, Mapping):
        raise QixiCorrectedPackageError("source/r2 boundary evidence is missing")
    for key in ("final_start_ms", "final_end_ms"):
        if source_boundary.get(key) != r2_boundary.get(key):
            raise QixiCorrectedPackageError(f"source/r2 invariant boundary {key} differs")
    # Verify semantic evidence before changing any derived locator/hash.  This
    # is intentionally a hard gate: a Z2 burn must never launder an unproved
    # chat/baseline surface merely because its SRT bytes happen to match.
    subtitle_text = artifacts["subtitle"][0].read_text(encoding="utf-8")
    duration = int(record.get("duration_ms") or 0)
    boundary_audit = record.get("boundary_audit")
    delivery_start = (
        int(boundary_audit.get("final_start_ms"))
        if isinstance(boundary_audit, Mapping)
        and isinstance(boundary_audit.get("final_start_ms"), int)
        else None
    )
    if duration <= 0 or delivery_start is None or not verify_chat_authority_final_surfaces(
        chat,
        final_text_srt=subtitle_text,
        final_speaker_srt=subtitle_text,
        delivery_start_ms=delivery_start,
        delivery_end_ms=delivery_start + duration,
    ):
        raise QixiCorrectedPackageError("fresh chat authority does not verify the release subtitle")
    r2_record_hashes = record.get("artifact_hashes")
    if not isinstance(r2_record_hashes, Mapping):
        raise QixiCorrectedPackageError("r2 record artifact hashes are missing")
    record_only = {"publish_draft_sha256"}
    if (
        set(r2_record_hashes) != set(original_publish_hashes) | record_only
        or any(r2_record_hashes[key] != original_publish_hashes[key] for key in original_publish_hashes)
    ):
        raise QixiCorrectedPackageError("r2 record/publish artifact hash closure is not canonical")
    hashes = dict(original_publish_hashes)
    hashes.update(
        {
            "burned_video_sha256": artifacts["video"][1],
            "video_sha256": artifacts["main"][1],
            "subtitle_sha256": artifacts["subtitle"][1],
            "ass_sha256": artifacts["ass"][1],
            "chat_authority_audit_sha256": artifacts["chat_authority"][1],
            "clip_context_file_sha256": artifacts["clip_context"][1],
            "redelivery_baseline_audit_sha256": artifacts["redelivery_baseline"][1],
            "cover_sha256": artifacts["cover"][1],
        }
    )
    record = dict(record)
    record.update(
        {
            "candidate_id": candidate_id,
            "media_path": str(_artifact_final_path(candidate_root, artifacts["main"])),
            "subtitle_path": str(_artifact_final_path(candidate_root, artifacts["subtitle"])),
            "subtitle_ass_path": str(_artifact_final_path(candidate_root, artifacts["ass"])),
            "chat_authority_audit_path": str(_artifact_final_path(candidate_root, artifacts["chat_authority"])),
            "clip_context_path": str(_artifact_final_path(candidate_root, artifacts["clip_context"])),
            "redelivery_baseline_audit_path": str(_artifact_final_path(candidate_root, artifacts["redelivery_baseline"])),
            "artifact_hashes": hashes,
            "recovery_publication_authority": publication,
        }
    )
    source_burned = source_record.get("burned_preview")
    if not isinstance(source_burned, Mapping):
        raise QixiCorrectedPackageError("source record lacks a burned-preview contract")
    correction_branding = correction.get("delivery_branding_authority")
    if (
        not isinstance(correction_branding, Mapping)
        or _canonical_sha(source_burned) != drift["source_burned_preview_sha256"]
    ):
        raise QixiCorrectedPackageError("source Z2 burned branding differs from correction receipt")
    _validate_source_burned_branding_summary(
        source_burned=source_burned,
        correction_branding=correction_branding.get("branding_intro"),
    )
    try:
        burned_carrier = project_uniform_host_locators(
            {"burned_preview": dict(source_burned)},
            kind="record",
            mappings=release_mappings,
            source_workspace_root=release_workspace_root,
        )
    except PackageRelocationError as exc:
        raise QixiCorrectedPackageError("source Z2 burned locator contract failed") from exc
    transplanted_burned = burned_carrier["burned_preview"]
    assert isinstance(transplanted_burned, Mapping)
    if (
        transplanted_burned.get("burned_sha256") != artifacts["video"][1]
        and str(transplanted_burned.get("burned_sha256") or "").removeprefix("sha256:")
        != artifacts["video"][1].removeprefix("sha256:")
    ):
        raise QixiCorrectedPackageError("source burned-preview does not bind the Z2 video")
    # These two listed locators are the only mutable leaves in the transplanted
    # source subtree.  Prefix projection would retain the source basenames,
    # whereas the sealed package deliberately materializes flat final names.
    transplanted_burned = dict(transplanted_burned)
    transplanted_burned["path"] = str(_artifact_final_path(candidate_root, artifacts["video"]))
    transplanted_burned["ass_path"] = str(_artifact_final_path(candidate_root, artifacts["ass"]))
    record["burned_preview"] = transplanted_burned
    staging = dict(record.get("publish_staging") or {})
    source_staging = source_staging_raw
    if (
        staging.get("title") != title
        or publish.get("title") != title
        or source_publish.get("title") != title
        or not isinstance(source_staging, Mapping)
        or source_staging.get("title") != title
    ):
        raise QixiCorrectedPackageError("source/r2 title differs from publication authority")
    staging["recovery_publication_authority"] = publication
    staging["publish_json_path"] = str(_artifact_final_path(candidate_root, artifacts["publish"]))
    portable_cover = copy.deepcopy(dict(source_generation))
    locator_updates = {
        "final_cover": artifacts["cover"],
        "pre_overlay_path": artifacts["cover_pre_overlay"],
        "ai_background": artifacts["cover_route_background"],
        "reference_image": artifacts["cover_reference"],
    }
    for key, artifact in locator_updates.items():
        if key in portable_cover:
            portable_cover[key] = str(_artifact_final_path(candidate_root, artifact))
    pixels = portable_cover.get("rendered_text_pixels")
    if isinstance(pixels, Mapping):
        pixels = dict(pixels)
        if "mask_path" in pixels:
            pixels["mask_path"] = str(_artifact_final_path(candidate_root, artifacts["cover_title_mask"]))
        if "pre_overlay_path" in pixels:
            pixels["pre_overlay_path"] = str(_artifact_final_path(candidate_root, artifacts["cover_pre_overlay"]))
        portable_cover["rendered_text_pixels"] = pixels
    staging["cover_generation"] = portable_cover
    staging["cover_path"] = str(_artifact_final_path(candidate_root, artifacts["cover"]))
    staging["cover_status"] = "AI_COVER_READY"
    record["publish_staging"] = staging
    if "cover_status" in record:
        record["cover_status"] = "AI_COVER_READY"
    publish = dict(publish)
    publish_hashes = dict(hashes)
    publish.update(
        {
            "video_path": str(_artifact_final_path(candidate_root, artifacts["main"])),
            "artifact_hashes": publish_hashes,
            "recovery_publication_authority": publication,
            "cover_status": "AI_COVER_READY",
            "cover_generation": portable_cover,
            "cover_path": str(_artifact_final_path(candidate_root, artifacts["cover"])),
        }
    )
    chat["burn_binding"] = {
        "burned_media_path": str(_artifact_final_path(candidate_root, artifacts["video"])),
        "burned_media_sha256": artifacts["video"][1].removeprefix("sha256:"),
        "ass_path": str(_artifact_final_path(candidate_root, artifacts["ass"])),
        "ass_sha256": artifacts["ass"][1].removeprefix("sha256:"),
    }
    _bind_chat_release_locators(
        chat=chat,
        record=record,
        artifacts=artifacts,
        candidate_root=candidate_root,
    )
    try:
        record = project_uniform_host_locators(
            record,
            kind="record",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
            frozen_source_roots=(release_workspace_root,),
        )
        publish = project_uniform_host_locators(
            publish,
            kind="publish",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
            frozen_source_roots=(release_workspace_root,),
        )
        chat = project_uniform_host_locators(
            chat,
            kind="chat",
            mappings=evidence_mappings,
            source_workspace_root=evidence_workspace_root,
        )
    except PackageRelocationError as exc:
        raise QixiCorrectedPackageError(f"final locator contract failed: {exc}") from exc
    return record, publish, chat


def _validate_current_ass_repair(
    *, authority: Mapping[str, Any], artifacts: Mapping[str, tuple[Path, str, int, str, str]], record: Mapping[str, object]
) -> None:
    """Bind the repaired current record to its applied, sealed-runtime receipt."""
    receipt = _load_object(artifacts["ass_repair_receipt"][0], label="ASS repair receipt")
    drift = authority["source_drift"]
    if (
        _sha256(artifacts["ass_repair_receipt"][0]) != drift["ass_repair_receipt_sha256"]
        or receipt.get("schema_version") != "qixi-delivery-record-ass-binding-recovery.v1"
        or receipt.get("mode") != "APPLIED"
        or receipt.get("candidate_id") != authority["candidate_id"]
        or receipt.get("allowed_json_pointers") != ["/artifact_hashes/ass_sha256", "/subtitle_ass_path"]
    ):
        raise QixiCorrectedPackageError("current ASS repair receipt is invalid")
    postimage = receipt.get("postimage")
    evidence = receipt.get("evidence_descriptors")
    evidence_rows = {
        name: evidence.get(name) if isinstance(evidence, Mapping) else None
        for name in ("ass", "subtitle", "burned", "correction")
    }
    if (
        not isinstance(postimage, Mapping)
        or not isinstance(evidence, Mapping)
        or not all(isinstance(row, Mapping) for row in evidence_rows.values())
        or (
            _normal_sha(postimage.get("sha256"), label="ASS repair postimage")
            != artifacts["record"][1]
            or postimage.get("bytes") != artifacts["record"][2]
            or _normal_sha(evidence_rows["ass"].get("sha256"), label="ASS repair evidence")
            != artifacts["ass"][1]
            or _normal_sha(evidence_rows["subtitle"].get("sha256"), label="ASS repair evidence")
            != artifacts["subtitle"][1]
            or _normal_sha(evidence_rows["burned"].get("sha256"), label="ASS repair evidence")
            != artifacts["video"][1]
            or _normal_sha(evidence_rows["correction"].get("sha256"), label="ASS repair evidence")
            != artifacts["correction"][1]
        )
    ):
        raise QixiCorrectedPackageError("current ASS repair receipt evidence drifts")
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or _normal_sha(hashes.get("ass_sha256"), label="current record ASS") != artifacts["ass"][1]:
        raise QixiCorrectedPackageError("current ASS repair record binding drifts")


def _project_current_terminal_documents(
    *,
    authority: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    candidate_root: Path,
    release_mappings: tuple[tuple[str, str], ...],
    release_workspace_root: str,
    evidence_mappings: tuple[tuple[str, str], ...],
    evidence_workspace_root: str,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Materialize current Z2 sources plus a real terminal text/chat closure."""
    candidate_id = str(authority["candidate_id"])
    title = str(authority["title"])
    publication = authority["recovery_publication_authority"]
    assert isinstance(publication, Mapping)
    record = _load_object(artifacts["record"][0], label="current record")
    mirror = _load_object(artifacts["record_mirror"][0], label="current record mirror")
    publish = _load_object(artifacts["publish"][0], label="stale current publish")
    correction = _load_object(artifacts["correction"][0], label="current correction")
    drift = authority["source_drift"]
    # The deployed current-terminal record is a legacy delivery-record.v1
    # shape: its sealed primary/mirror bytes predate a top-level candidate_id.
    # Do not infer that identity from a filename or other mutable field.  The
    # exception is confined to this lane and is only useful alongside the
    # existing sealed primary/mirror descriptors and the independently-bound
    # publish/correction identities checked below.
    legacy_record_identity = (
        authority.get("finalization_mode") == "current_terminal_projection"
        and "candidate_id" not in record
    )
    if (
        artifacts["record"][1] != drift["source_record_sha256"]
        or artifacts["record_mirror"][1] != drift["source_record_mirror_sha256"]
        or artifacts["record"][1] != artifacts["record_mirror"][1]
        or record != mirror
        or (record.get("candidate_id") != candidate_id and not legacy_record_identity)
        or publish.get("candidate_id") != candidate_id
        or publish.get("title") != title
    ):
        raise QixiCorrectedPackageError("current record/publish sources drift")
    _validate_current_ass_repair(authority=authority, artifacts=artifacts, record=record)
    subtitle_text = artifacts["subtitle"][0].read_text(encoding="utf-8")
    if (
        correction.get("candidate_id") != candidate_id
        or correction.get("upload_enabled") is not False
        or _normal_sha(correction.get("after_srt_sha256"), label="current correction subtitle")
        != artifacts["subtitle"][1]
        or _normal_sha(correction.get("burned_media_sha256"), label="current correction burn")
        != artifacts["video"][1]
    ):
        raise QixiCorrectedPackageError("current correction release bindings drift")
    stale_publish_hashes = publish.get("artifact_hashes")
    if not isinstance(stale_publish_hashes, Mapping) or (
        _normal_sha(stale_publish_hashes.get("burned_video_sha256"), label="current stale publish burn")
        != drift["source_publish_burned_video_sha256"]
        or _normal_sha(stale_publish_hashes.get("subtitle_sha256"), label="current stale publish subtitle")
        != drift["source_publish_subtitle_sha256"]
    ):
        raise QixiCorrectedPackageError("current stale publish binding drifts")
    chat, redelivery_audit = _build_terminal_projection_chat(
        repo_root=repo_root,
        correction=correction,
        correction_sha256=artifacts["correction"][1],
        record=record,
        record_sha256=artifacts["record"][1],
        subtitle_text=subtitle_text,
    )
    source_chat = _load_object(artifacts["chat_authority"][0], label="source chat authority")
    source_boundary = _load_object(artifacts["boundary_audit"][0], label="source boundary audit")
    source_flags = _load_object(artifacts["review_flags"][0], label="source review flags")
    try:
        replayed_chat, current_boundary, frozen_chat_allowlist = build_terminal_audit_closure(
            repo_root=repo_root,
            source_chat=source_chat,
            source_chat_sha256=artifacts["chat_authority"][1],
            source_boundary=source_boundary,
            source_boundary_sha256=artifacts["boundary_audit"][1],
            source_review_flags=source_flags,
            source_flags_sha256=artifacts["review_flags"][1],
            record_boundary=record.get("boundary_audit")
            if isinstance(record.get("boundary_audit"), Mapping)
            else {},
            subtitle_text=subtitle_text,
        )
    except QixiCurrentTerminalAuditClosureError as exc:
        raise QixiCorrectedPackageError("current terminal audit closure failed") from exc
    # The source chat provides only already-reviewed decision and boundary
    # ownership evidence.  The terminal projection owns the fresh text/burn
    # surfaces and its deterministic baseline audit; preserve neither old
    # delivery locators nor old baseline receipts.
    replayed_chat["redelivery_subtitle_baseline_audit"] = redelivery_audit
    chat = replayed_chat
    record_hashes = record.get("artifact_hashes")
    publish_hashes = publish.get("artifact_hashes")
    if not isinstance(record_hashes, Mapping) or not isinstance(publish_hashes, Mapping) or (
        set(record_hashes) != set(publish_hashes) | {"publish_draft_sha256"}
    ):
        raise QixiCorrectedPackageError("current record/publish hash closure is invalid")
    generated_redelivery_sha = "sha256:" + hashlib.sha256(_json_bytes(redelivery_audit)).hexdigest()
    hashes = dict(publish_hashes)
    hashes.update(
        {
            "burned_video_sha256": artifacts["video"][1],
            "video_sha256": artifacts["main"][1],
            "subtitle_sha256": artifacts["subtitle"][1],
            "ass_sha256": artifacts["ass"][1],
            "clip_context_file_sha256": artifacts["clip_context"][1],
            "redelivery_baseline_audit_sha256": generated_redelivery_sha,
            "cover_sha256": artifacts["cover"][1],
            "cover_reference_sha256": artifacts["cover_reference"][1],
            "ai_background_sha256": artifacts["cover_route_background"][1],
        }
    )
    source_burned = record.get("burned_preview")
    if not isinstance(source_burned, Mapping) or _canonical_sha(source_burned) != drift["source_burned_preview_sha256"]:
        raise QixiCorrectedPackageError("current Z2 burned-preview drifts")
    branding = correction.get("delivery_branding_authority")
    if not isinstance(branding, Mapping):
        raise QixiCorrectedPackageError("current correction branding is invalid")
    _validate_source_burned_branding_summary(
        source_burned=source_burned, correction_branding=branding.get("branding_intro")
    )
    burned_preview = dict(source_burned)
    burned_preview["path"] = str(_artifact_final_path(candidate_root, artifacts["video"]))
    burned_preview["ass_path"] = str(_artifact_final_path(candidate_root, artifacts["ass"]))
    record = dict(record)
    record.update(
        {
            "media_path": str(_artifact_final_path(candidate_root, artifacts["main"])),
            "subtitle_path": str(_artifact_final_path(candidate_root, artifacts["subtitle"])),
            "subtitle_ass_path": str(_artifact_final_path(candidate_root, artifacts["ass"])),
            "chat_authority_audit_path": str(_artifact_final_path(candidate_root, artifacts["chat_authority"])),
            "clip_context_path": str(_artifact_final_path(candidate_root, artifacts["clip_context"])),
            "redelivery_baseline_audit_path": str(_artifact_final_path(candidate_root, artifacts["redelivery_baseline"])),
            "redelivery_baseline": redelivery_audit,
            "artifact_hashes": hashes,
            "burned_preview": burned_preview,
            "recovery_publication_authority": dict(publication),
            "boundary_audit": current_boundary,
        }
    )
    story_contract = dict(record.get("story_contract") or {})
    story_contract["boundary_semantic_review"] = copy.deepcopy(
        current_boundary["final_delivery_boundary_semantic_review"]
    )
    selection_hook = str(story_contract.get("selection_hook") or "")
    clip_context_prompt = str(story_contract.get("clip_context_prompt") or "")
    selection_scorecard = story_contract.get("selection_scorecard")
    final_transcript = "\n".join(
        cue.text.strip() for cue in parse_srt_cues(subtitle_text) if cue.text.strip()
    )
    try:
        story_contract = rebind_terminal_story_transcript(
            story_contract, final_transcript=final_transcript
        )
    except QixiCurrentTerminalAuditClosureError as exc:
        raise QixiCorrectedPackageError("current terminal story transcript closure failed") from exc
    uniform_speaker_evidence = {
        "policy_ids": {
            "absence": "speaker_mode_uniform_host/v1",
            "alignment": "speaker_cue_subsegment_alignment/v1",
            "text": "compact_ws/v1",
            "timing": "half_open_integer_ms_exact/v1",
        },
        "reason": "speaker_mode_uniform_host",
        "state": "AbsentAuthorized",
    }
    try:
        source_fact_review = build_terminal_preservation_review(
            repo_root=repo_root,
            subtitle_text=subtitle_text,
            final_transcript=final_transcript,
            title=title,
            selection_hook=selection_hook,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            speaker_evidence=uniform_speaker_evidence,
            correction_sha256=artifacts["correction"][1],
            ass_repair_receipt_sha256=artifacts["ass_repair_receipt"][1],
            recovery_publication_authority=publication,
            finalization_authority_sha256=str(authority["authority_sha256"]),
        )
    except (QixiSourceFactTerminalPreservationError, ValueError) as exc:
        raise QixiCorrectedPackageError("current terminal source-fact preservation failed") from exc
    story_contract["source_fact_review"] = source_fact_review
    record["story_contract"] = story_contract
    staging = dict(record.get("publish_staging") or {})
    generation = staging.get("cover_generation")
    if (
        not isinstance(generation, Mapping)
        or staging.get("title") != title
        or staging.get("cover_status") != "AI_COVER_READY"
        or _canonical_sha(generation) != drift["source_cover_generation_sha256"]
        or _normal_sha(generation.get("final_cover_sha256"), label="current source cover")
        != artifacts["cover"][1]
    ):
        raise QixiCorrectedPackageError("current record cover/title staging is invalid")
    portable_cover = copy.deepcopy(dict(generation))
    for key, name in {
        "final_cover": "cover", "pre_overlay_path": "cover_pre_overlay",
        "ai_background": "cover_route_background", "reference_image": "cover_reference",
    }.items():
        if key in portable_cover:
            portable_cover[key] = str(_artifact_final_path(candidate_root, artifacts[name]))
    pixels = portable_cover.get("rendered_text_pixels")
    if isinstance(pixels, Mapping):
        pixels = dict(pixels)
        if "mask_path" in pixels:
            pixels["mask_path"] = str(_artifact_final_path(candidate_root, artifacts["cover_title_mask"]))
        if "pre_overlay_path" in pixels:
            pixels["pre_overlay_path"] = str(_artifact_final_path(candidate_root, artifacts["cover_pre_overlay"]))
        portable_cover["rendered_text_pixels"] = pixels
    staging.update({
        "publish_json_path": str(_artifact_final_path(candidate_root, artifacts["publish"])),
        "cover_generation": portable_cover,
        "cover_path": str(_artifact_final_path(candidate_root, artifacts["cover"])),
        "cover_status": "AI_COVER_READY",
        "recovery_publication_authority": dict(publication),
        "source_fact_review": source_fact_review,
    })
    record["publish_staging"] = staging
    if "cover_status" in record:
        record["cover_status"] = "AI_COVER_READY"
    publish = dict(publish)
    publish.update({
        "video_path": str(_artifact_final_path(candidate_root, artifacts["main"])),
        "artifact_hashes": dict(hashes),
        "cover_generation": portable_cover,
        "cover_path": str(_artifact_final_path(candidate_root, artifacts["cover"])),
        "cover_status": "AI_COVER_READY",
        "recovery_publication_authority": dict(publication),
        "source_fact_review": source_fact_review,
    })
    chat.update({
        "final_status": "FINAL_ARTIFACTS_VERIFIED",
        "final_text_srt_path": str(_artifact_final_path(candidate_root, artifacts["subtitle"])),
        "final_text_srt_sha256": artifacts["subtitle"][1].removeprefix("sha256:"),
        "final_speaker_srt_path": str(_artifact_final_path(candidate_root, artifacts["subtitle"])),
        "final_speaker_srt_sha256": artifacts["subtitle"][1].removeprefix("sha256:"),
        # The current delivery is a uniform-host lane: text and speaker SRT
        # are the same sealed terminal projection, with no separate speaker
        # ASS surface to counterfeit or carry forward.
        "speaker_ass_path": None,
        "speaker_ass_sha256": None,
        "speaker_manifest_sha256": None,
        "burn_binding": {
            "burned_media_path": str(_artifact_final_path(candidate_root, artifacts["video"])),
            "burned_media_sha256": artifacts["video"][1].removeprefix("sha256:"),
            "ass_path": str(_artifact_final_path(candidate_root, artifacts["ass"])),
            "ass_sha256": artifacts["ass"][1].removeprefix("sha256:"),
        },
    })
    try:
        record = project_uniform_host_locators(record, kind="record", mappings=evidence_mappings, source_workspace_root=evidence_workspace_root, frozen_source_roots=(release_workspace_root,))
        publish = project_uniform_host_locators(publish, kind="publish", mappings=evidence_mappings, source_workspace_root=evidence_workspace_root, frozen_source_roots=(release_workspace_root,))
        chat = project_uniform_host_locators(chat, kind="chat", mappings=evidence_mappings, source_workspace_root=evidence_workspace_root, frozen_absolute_allowlist=frozen_chat_allowlist)
    except PackageRelocationError as exc:
        raise QixiCorrectedPackageError(f"current terminal locator contract failed: {exc}") from exc
    if (
        record.get("human_text_correction_manifest_path")
        != str(_artifact_final_path(candidate_root, artifacts["correction"]))
        or _normal_sha(
            record.get("human_text_correction_manifest_sha256"),
            label="current correction record locator",
        )
        != artifacts["correction"][1]
    ):
        raise QixiCorrectedPackageError("current correction record locator drifts")
    if not verify_chat_authority_final_surfaces(
        chat,
        final_text_srt=subtitle_text,
        final_speaker_srt=subtitle_text,
        delivery_start_ms=0,
        delivery_end_ms=int(record["duration_ms"]),
    ):
        raise QixiCorrectedPackageError("current terminal chat does not verify final release surfaces")
    return record, publish, chat


def _after_image(
    *,
    authority: Mapping[str, Any],
    artifacts: Mapping[str, tuple[Path, str, int, str, str]],
    candidate_root: Path,
    release_mappings: tuple[tuple[str, str], ...],
    release_workspace_root: str,
    evidence_mappings: tuple[tuple[str, str], ...],
    evidence_workspace_root: str,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Construct the complete derived closure without touching a target."""

    record, publish, chat = _project_documents(
        authority=authority,
        artifacts=artifacts,
        candidate_root=candidate_root,
        release_mappings=release_mappings,
        release_workspace_root=release_workspace_root,
        evidence_mappings=evidence_mappings,
        evidence_workspace_root=evidence_workspace_root,
        repo_root=repo_root,
    )
    _assert_planned_locator_closure(
        record=record,
        publish=publish,
        chat=chat,
        artifacts=artifacts,
        candidate_root=candidate_root,
    )
    after: dict[str, dict[str, Any]] = {}
    if authority.get("finalization_mode") == "current_terminal_projection":
        redelivery = chat.get("redelivery_subtitle_baseline_audit")
        if not isinstance(redelivery, Mapping):
            raise QixiCorrectedPackageError("current terminal chat lacks generated redelivery audit")
        redelivery_bytes = _json_bytes(dict(redelivery))
        redelivery_sha = "sha256:" + hashlib.sha256(redelivery_bytes).hexdigest()
        for document in (record, publish):
            hashes = dict(document["artifact_hashes"])
            if hashes.get("redelivery_baseline_audit_sha256") != redelivery_sha:
                raise QixiCorrectedPackageError("current terminal redelivery hash closure drifts")
            document["artifact_hashes"] = hashes
        after["redelivery_baseline"] = {
            "sha256": redelivery_sha,
            "bytes": len(redelivery_bytes),
        }
    chat_sha = "sha256:" + hashlib.sha256(_json_bytes(chat)).hexdigest()
    for document in (record, publish):
        hashes = dict(document["artifact_hashes"])
        hashes["chat_authority_audit_sha256"] = chat_sha
        document["artifact_hashes"] = hashes
    publish_sha = "sha256:" + hashlib.sha256(_json_bytes(publish)).hexdigest()
    record_hashes = dict(record["artifact_hashes"])
    record_hashes["publish_draft_sha256"] = publish_sha
    record["artifact_hashes"] = record_hashes
    after.update({
        "record": {
            "sha256": "sha256:" + hashlib.sha256(_json_bytes(record)).hexdigest(),
            "bytes": len(_json_bytes(record)),
        },
        "publish": {"sha256": publish_sha, "bytes": len(_json_bytes(publish))},
        "chat_authority": {"sha256": chat_sha, "bytes": len(_json_bytes(chat))},
    })
    return record, publish, chat, after


def _prepare_execution_context(
    *,
    repo_root: Path,
    release_root: Path,
    evidence_root: Path,
    target: Path,
) -> _ExecutionContext:
    repo_root = repo_root.resolve(strict=True)
    authority_path, authority_bytes, authority, repository_authority = _load_sealed_authority(
        repo_root
    )
    target = _validate_create_only_target(target)
    package_root = _candidate_package_root(target)
    release_checked = _source_root(release_root, label="release")
    evidence_checked = _source_root(evidence_root, label="evidence")
    if _target_overlaps_source(target, release_checked) or _target_overlaps_source(
        target, evidence_checked
    ):
        raise QixiCorrectedPackageError("target package may not overlap a source/evidence tree")
    artifacts = _verify_cross_surface(
        authority=authority, release_root=release_checked, evidence_root=evidence_checked
    )
    names = [
        (item[4], item[3]) for name, item in artifacts.items() if name not in _NON_MATERIALIZED
    ]
    if len(names) != len(set(names)):
        raise QixiCorrectedPackageError("artifact targets collide")
    _evidence_mappings, _evidence_workspace = _relocation_mappings(
        authority=authority,
        evidence_root=evidence_checked,
        candidate_root=target,
        repo_root=repo_root.resolve(strict=True),
    )
    _release_mappings, _release_workspace = _release_relocation_mappings(
        release_root=release_checked,
        candidate_root=target,
        repo_root=repo_root.resolve(strict=True),
    )
    _record, _publish, _chat, after_hashes = _after_image(
        authority=authority,
        artifacts=artifacts,
        candidate_root=target,
        release_mappings=_release_mappings,
        release_workspace_root=_release_workspace,
        evidence_mappings=_evidence_mappings,
        evidence_workspace_root=_evidence_workspace,
        repo_root=repo_root,
    )
    plan = {
        "schema_version": RECEIPT_SCHEMA,
        "mode": "DRY_RUN",
        "candidate_id": authority["candidate_id"],
        "target_candidate_root": str(target),
        "package_root": str(package_root),
        "authority_sha256": authority["authority_sha256"],
        "manual_corrected_same_bv": {
            "schema_version": "manual-corrected-same-bv.v1",
            "candidate_id": authority["candidate_id"],
            "recovery_publication_authority": authority["recovery_publication_authority"],
            "approved_burned_video_sha256": artifacts["video"][1],
            "approved_subtitle_sha256": artifacts["subtitle"][1],
            "approved_cover_sha256": artifacts["cover"][1],
        },
        "after_image_sha256": after_hashes,
        "artifacts": {
            name: {
                "target_role": item[4],
                "target": item[3],
                "sha256": item[1],
                "bytes": item[2],
            }
            for name, item in sorted(artifacts.items()) if name not in _NON_MATERIALIZED
        },
    }
    return _ExecutionContext(
        plan=plan,
        repo_root=repo_root,
        authority_path=authority_path,
        authority_bytes=authority_bytes,
        repository_authority=repository_authority,
        authority=authority,
        artifacts=artifacts,
        release_checked=release_checked,
        evidence_checked=evidence_checked,
        release_mappings=_release_mappings,
        release_workspace_root=_release_workspace,
        evidence_mappings=_evidence_mappings,
        evidence_workspace_root=_evidence_workspace,
    )


def plan_finalization(
    *,
    repo_root: Path,
    release_root: Path,
    evidence_root: Path,
    target: Path,
) -> dict[str, Any]:
    return _prepare_execution_context(
        repo_root=repo_root,
        release_root=release_root,
        evidence_root=evidence_root,
        target=target,
    ).plan


def finalize(
    *,
    repo_root: Path,
    release_root: Path,
    evidence_root: Path,
    target: Path,
    apply: bool = False,
) -> dict[str, Any]:
    context = _prepare_execution_context(
        repo_root=repo_root,
        release_root=release_root,
        evidence_root=evidence_root,
        target=target,
    )
    plan = context.plan
    if not apply:
        return plan
    _assert_context_authority_unchanged(context)
    authority = context.authority
    artifacts = context.artifacts
    candidate_root = target
    evidence_mappings = context.evidence_mappings
    evidence_workspace_root = context.evidence_workspace_root
    release_mappings = context.release_mappings
    release_workspace_root = context.release_workspace_root
    parent = target.parent
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.qixi-finalize-", dir=parent))
    owned: dict[Path, tuple[int, int, int]] = {}
    _own_created(stage, owned)
    committed = False
    try:
        for _name, (source, sha, _bytes, relative, role) in artifacts.items():
            if _name in _NON_MATERIALIZED:
                continue
            stage_relative = (
                str(Path("replacement_recuts") / relative)
                if role == "package"
                else relative
            )
            _copy_checked(
                source,
                _target_path(stage, stage_relative, owned=owned),
                sha,
                owned=owned,
            )
            _assert_owned_tree(stage, owned)
        record, publish, chat, after_hashes = _after_image(
            authority=authority,
            artifacts=artifacts,
            candidate_root=candidate_root,
            release_mappings=release_mappings,
            release_workspace_root=release_workspace_root,
            evidence_mappings=evidence_mappings,
            evidence_workspace_root=evidence_workspace_root,
            repo_root=context.repo_root,
        )
        if after_hashes != plan.get("after_image_sha256"):
            raise QixiCorrectedPackageError("source bytes changed after dry-run planning")
        # Generated primary documents replace copied stale r2 documents only
        # inside our fresh staging directory.
        # Persist in dependency order: chat is complete before its hash enters
        # record; publish is complete before record binds its final hash.
        if authority.get("finalization_mode") == "current_terminal_projection":
            redelivery = chat.get("redelivery_subtitle_baseline_audit")
            if not isinstance(redelivery, Mapping):
                raise QixiCorrectedPackageError(
                    "current terminal chat lacks generated redelivery audit"
                )
            redelivery_path = _target_path(
                stage,
                str(Path("replacement_recuts") / artifacts["redelivery_baseline"][3]),
                owned=owned,
            )
            _assert_owned_tree(stage, owned)
            _unlink_owned(redelivery_path, owned)
            _write_json_new(redelivery_path, dict(redelivery), owned=owned)
            _assert_owned_tree(stage, owned)
            if _sha256(redelivery_path) != after_hashes["redelivery_baseline"]["sha256"]:
                raise QixiCorrectedPackageError(
                    "materialized redelivery after-image drifted"
                )
        chat_path = _target_path(stage, artifacts["chat_authority"][3], owned=owned)
        _assert_owned_tree(stage, owned)
        _unlink_owned(chat_path, owned)
        _write_json_new(chat_path, chat, owned=owned)
        _assert_owned_tree(stage, owned)
        if _sha256(chat_path) != after_hashes["chat_authority"]["sha256"]:
            raise QixiCorrectedPackageError("materialized chat after-image drifted")
        publish_path = _target_path(
            stage,
            str(Path("replacement_recuts") / artifacts["publish"][3]),
            owned=owned,
        )
        _assert_owned_tree(stage, owned)
        _unlink_owned(publish_path, owned)
        _write_json_new(publish_path, publish, owned=owned)
        _assert_owned_tree(stage, owned)
        if _sha256(publish_path) != after_hashes["publish"]["sha256"]:
            raise QixiCorrectedPackageError("materialized publish after-image drifted")
        record_path = _target_path(
            stage,
            str(Path("replacement_recuts") / artifacts["record"][3]),
            owned=owned,
        )
        _assert_owned_tree(stage, owned)
        _unlink_owned(record_path, owned)
        _write_json_new(record_path, record, owned=owned)
        _assert_owned_tree(stage, owned)
        if _sha256(record_path) != after_hashes["record"]["sha256"]:
            raise QixiCorrectedPackageError("materialized record after-image drifted")
        receipt = dict(plan)
        receipt["mode"] = "APPLIED"
        receipt["generated_record_sha256"] = _sha256(record_path)
        receipt["generated_publish_sha256"] = _sha256(
            publish_path
        )
        receipt["generated_chat_authority_sha256"] = _sha256(
            chat_path
        )
        if authority.get("finalization_mode") == "current_terminal_projection":
            receipt["generated_redelivery_baseline_sha256"] = _sha256(redelivery_path)
        receipt_path = _target_path(
            stage,
            "replacement_recuts/qixi-corrected-package-finalization.json",
            owned=owned,
        )
        _write_json_new(
            receipt_path,
            receipt,
            owned=owned,
        )
        _assert_owned_tree(stage, owned)
        _assert_context_authority_unchanged(context)
        # A plan never authorizes replacement: re-check at the last possible
        # instant, including a symlink planted after initial validation.
        if target.exists() or target.is_symlink():
            raise QixiCorrectedPackageError("target appeared during finalization")
        os.replace(stage, target)
        committed = True
        return receipt
    finally:
        if not committed:
            # ``stage`` is the exact mkdtemp result, rooted immediately below
            # the validated target parent.  Do not broaden a failed rollback.
            if not stage.exists() or (
                stage.parent != target.parent
                or not stage.name.startswith(f".{target.name}.qixi-finalize-")
                or stage.is_symlink()
            ):
                raise QixiCorrectedPackageError("staging rollback ownership was lost")
            try:
                _rollback_owned_tree(stage, owned)
            except OSError as exc:
                raise QixiCorrectedPackageError("staging rollback failed") from exc
