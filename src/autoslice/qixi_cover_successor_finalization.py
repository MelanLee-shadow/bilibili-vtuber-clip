"""Create and replay the one sealed Qixi CPA-cover successor package.

This is deliberately not a reusable cover-repair lane. The only accepted
candidate is the Qixi record named below, and sealed repository authority is
replayed both when creating and when consuming a package.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.cover_font_paths import resolve_trusted_cover_font
from src.autoslice.cover_host_identity_gate import validate_final_host_identity_verification
from src.autoslice.cover_punch_semantics import (
    review_cover_punch_semantics,
    validate_cover_punch_semantic_review,
)
from src.autoslice.cover_route_evidence import validate_cover_route_decision
from src.autoslice.cover_text_pixel_evidence import (
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.qixi_cover_route_displacement import (
    CANDIDATE_ID,
    QixiCoverRouteDisplacementError,
    load_authority,
    validate_provider_evidence,
)
from src.autoslice.qixi_transaction_core import (
    QixiTransactionCoreError,
    safe_parent,
    stable_regular_snapshot,
)
from src.autoslice.story_contract import cover_story_contract_binding


RECEIPT = "qixi-cover-successor-finalization.json"
SCHEMA = "qixi-cover-successor-finalization-receipt.v2"
PORTABLE_PROVENANCE_SCHEMA = "qixi-cover-successor-portable-provenance.v1"
PACKAGE_AUDIT = f"{CANDIDATE_ID}.package-audit.json"

_TRIAL_NAMES = {
    "final": "qixi-cpa-redraw.png",
    "background": "qixi-cpa-redraw.ai-bg.png",
    "pre": "qixi-cpa-redraw.pre-overlay.png",
    "mask": "qixi-cpa-redraw.title-mask.png",
    "generation": "qixi-cpa-redraw.cover_generation.json",
    "identity": "qixi-cpa-redraw.host-identity-witness.png",
    "no_text": "qixi-cpa-redraw.no-model-text-witness.json",
    "joint": "qixi-cpa-redraw.title-cover-joint-qc.json",
    "request": "qixi-cpa-redraw.cpa-request.redacted.json",
    "response": "qixi-cpa-redraw.cpa-response.redacted.json",
}

_PORTABLE_PATHS = {
    "final": f"{CANDIDATE_ID}.cover.png",
    "background": f"{CANDIDATE_ID}.cover.ai-bg.png",
    "route_background": f"{CANDIDATE_ID}.cover.route-background.png",
    "pre": f"{CANDIDATE_ID}.cover.pre-overlay.png",
    "mask": f"{CANDIDATE_ID}.cover.title-mask.png",
    "reference": "evidence/cover-reference.png",
    "identity": "evidence/qixi-cover-successor/qixi-cpa-redraw.host-identity-witness.png",
}

_PROVENANCE_NAMES = {
    **{key: value for key, value in _TRIAL_NAMES.items() if key != "identity"},
    "identity": "qixi-cpa-redraw.host-identity-witness.provenance.json",
}


class QixiCoverSuccessorError(ValueError):
    """The one Qixi cover successor does not replay safely."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical_sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _safe_absolute_dir(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise QixiCoverSuccessorError(f"{label} must be absolute")
    try:
        safe_parent(path / ".qixi-successor-probe")
        info = os.lstat(path)
    except (OSError, QixiTransactionCoreError) as exc:
        raise QixiCoverSuccessorError(f"{label} is unavailable or unsafe") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise QixiCoverSuccessorError(f"{label} is not a non-symlink directory")
    return path.resolve(strict=True)


def _private_output_parent(parent: Path) -> Path:
    """Require a pre-created, owner-private target parent before staging."""

    parent = _safe_absolute_dir(parent, label="target parent")
    info = os.lstat(parent)
    if (
        stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.geteuid()
    ):
        raise QixiCoverSuccessorError("target parent must be owner-private mode 0700")
    return parent


def _regular(root: Path, relative: str, *, label: str, nonempty: bool = True) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise QixiCoverSuccessorError(f"{label} locator is unsafe")
    path = root.joinpath(*relative_path.parts)
    try:
        snapshot = stable_regular_snapshot(path, label=label)
    except QixiTransactionCoreError as exc:
        raise QixiCoverSuccessorError(f"{label} is unsafe") from exc
    if snapshot is None or (nonempty and not snapshot.payload):
        raise QixiCoverSuccessorError(f"{label} is absent or empty")
    return path


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QixiCoverSuccessorError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise QixiCoverSuccessorError(f"{label} is not an object")
    return value


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    payload = _json_bytes(value)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short JSON write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_json(path: Path, value: Mapping[str, object]) -> None:
    """Replace an already-copied staged regular file after a no-follow read."""

    _regular(path.parent, path.name, label=f"staged {path.name}")
    temporary = path.with_name(f".{path.name}.qixi-successor.tmp")
    if os.path.lexists(temporary):
        raise QixiCoverSuccessorError("staged JSON temporary already exists")
    try:
        _write_json(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _assert_tree_regular(root: Path, *, label: str) -> None:
    for item in [root, *root.rglob("*")]:
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode):
            raise QixiCoverSuccessorError(f"{label} contains a symlink")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise QixiCoverSuccessorError(f"{label} contains a non-regular entry")


def _seal_private_tree(root: Path) -> None:
    """Make the newly copied runtime bundle owner-private before mutation."""

    _assert_tree_regular(root, label="staged package")
    for item in [root, *sorted(root.rglob("*"))]:
        info = os.lstat(item)
        if info.st_uid != os.geteuid():
            raise QixiCoverSuccessorError("staged package owner drifts")
        expected_mode = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
        os.chmod(item, expected_mode)
        observed = os.lstat(item)
        if stat.S_IMODE(observed.st_mode) != expected_mode or observed.st_uid != os.geteuid():
            raise QixiCoverSuccessorError("staged package private mode drifts")


def seal_private_successor_tree(root: Path) -> None:
    """Seal the sole successor package after a later sealed sidecar write.

    The manual-review manifest and its canonical audit are created only after
    the finalizer has installed the successor receipt.  Their candidate-only
    orchestrator calls this exported narrow helper before considering the
    package complete; it is deliberately not a generic package-permission API.
    """

    _seal_private_tree(root)


def _snapshot(root: Path) -> dict[str, str]:
    """Freeze every preimage byte except the expressly replaced cover surface."""

    ignored = {
        f"replacement_recuts/{CANDIDATE_ID}.cover.png",
        f"replacement_recuts/{CANDIDATE_ID}.cover.ai-bg.png",
        f"replacement_recuts/{CANDIDATE_ID}.cover.route-background.png",
        f"replacement_recuts/{CANDIDATE_ID}.cover.pre-overlay.png",
        f"replacement_recuts/{CANDIDATE_ID}.cover.title-mask.png",
        f"replacement_recuts/{CANDIDATE_ID}.record.json",
        f"replacement_recuts/{CANDIDATE_ID}.publish.json",
        f"replacement_recuts/{RECEIPT}",
        "replacement_recuts/review_manifest.json",
        f"replacement_recuts/{CANDIDATE_ID}.package-audit.json",
    }
    hashes: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode):
            raise QixiCoverSuccessorError("preimage/package contains a symlink")
        if stat.S_ISREG(info.st_mode) and (
            relative not in ignored
            and not relative.startswith("replacement_recuts/evidence/qixi-cover-successor/")
        ):
            hashes[relative] = _sha(item)
    return hashes


def _portable_locator(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QixiCoverSuccessorError(f"{label} is absent")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise QixiCoverSuccessorError(f"{label} is not package-relative")
    return path.as_posix()


def _assert_no_absolute_locator(value: object, *, label: str) -> None:
    """Reject absolute host locators in portable package evidence recursively."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise QixiCoverSuccessorError(f"{label} has a non-string key")
            _assert_no_absolute_locator(child, label=f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_absolute_locator(child, label=f"{label}[{index}]")
    elif isinstance(value, str) and value.startswith("/"):
        raise QixiCoverSuccessorError(f"{label} contains an absolute locator")


def validate_portable_cover_locators(generation: object) -> dict[str, Any]:
    """Validate the concrete package locators consumed by this successor."""

    if not isinstance(generation, Mapping):
        raise QixiCoverSuccessorError("cover generation is not an object")
    normalized = dict(generation)
    for key in (
        "final_cover",
        "ai_background",
        "pre_overlay_path",
        "reference_image",
        "request_path",
        "response_path",
    ):
        _portable_locator(normalized.get(key), label=f"cover generation {key}")
    pixels = normalized.get("rendered_text_pixels")
    identity = normalized.get("final_host_identity_verification")
    if not isinstance(pixels, Mapping) or not isinstance(identity, Mapping):
        raise QixiCoverSuccessorError("cover pixels/identity evidence missing")
    for key in ("mask_path", "pre_overlay_path"):
        _portable_locator(pixels.get(key), label=f"rendered pixels {key}")
    for key in ("final_cover_path", "comparison_path", "reference_path"):
        _portable_locator(identity.get(key), label=f"identity {key}")
    witness = identity.get("witness")
    if not isinstance(witness, Mapping):
        raise QixiCoverSuccessorError("identity witness missing")
    _portable_locator(witness.get("image_path"), label="identity witness image_path")
    _assert_no_absolute_locator(normalized, label="cover generation")
    return normalized


def validate_current_successor_audit(*, package_root: Path) -> dict[str, Any]:
    """Reject a copied preimage audit in the sole successor review package."""

    package = _safe_absolute_dir(package_root, label="successor package")
    audit = _load_json(
        _regular(package, PACKAGE_AUDIT, label="successor package audit"),
        label="successor package audit",
    )
    if not (
        audit.get("schema_version") == "lidousha-review-package-audit.v2"
        and audit.get("passed") is True
        and audit.get("issues") == []
        and audit.get("issue_count") == 0
        and audit.get("blocking_issue_count") == 0
        and audit.get("root") == str(package)
    ):
        raise QixiCoverSuccessorError("successor package audit is stale or failed")
    rows = audit.get("audited_inputs")
    if not isinstance(rows, list):
        raise QixiCoverSuccessorError("successor package audit inputs are absent")
    indexed = {
        row.get("path"): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    for relative in (
        _PORTABLE_PATHS["final"],
        f"{CANDIDATE_ID}.record.json",
        f"{CANDIDATE_ID}.publish.json",
        "review_manifest.json",
        RECEIPT,
    ):
        row = indexed.get(relative)
        if not isinstance(row, Mapping):
            raise QixiCoverSuccessorError(f"successor package audit omits {relative}")
        expected = _sha(_regular(package, relative, label=f"audited {relative}"))[7:]
        if row.get("sha256") != expected:
            raise QixiCoverSuccessorError(f"successor package audit input hash drifts: {relative}")
    return audit


def _trial_files(root: Path, authority: Mapping[str, object]) -> tuple[dict[str, Path], dict[str, Any]]:
    files = {
        key: _regular(root, name, label=f"trial {key}")
        for key, name in _TRIAL_NAMES.items()
    }
    replacement = authority["replacement"]
    assert isinstance(replacement, Mapping)
    expected = {
        "final": replacement["final_cover_sha256"],
        "background": replacement["route_background_sha256"],
        "pre": replacement["pre_overlay_sha256"],
        "mask": replacement["title_mask_sha256"],
    }
    if any(_sha(files[key]) != expected_hash for key, expected_hash in expected.items()):
        raise QixiCoverSuccessorError("trial cover bytes drift from sealed authority")
    generation = _load_json(files["generation"], label="trial generation")
    no_text = _load_json(files["no_text"], label="trial no-model-text witness")
    joint = _load_json(files["joint"], label="trial joint QC")
    identity = generation.get("final_host_identity_verification")
    pixels = generation.get("rendered_text_pixels")
    if not (
        generation.get("candidate_id") == CANDIDATE_ID
        and generation.get("final_cover_sha256") == expected["final"]
        and generation.get("ai_background_sha256") == expected["background"]
        and generation.get("pre_overlay_sha256") == expected["pre"]
        and generation.get("rendered_lines") == replacement["required_title_lines"]
        and isinstance(identity, Mapping)
        and identity.get("status") == "PASS"
        and identity.get("final_cover_sha256") == expected["final"]
        and isinstance(pixels, Mapping)
        and pixels.get("status") == "PASS"
        and pixels.get("mask_sha256") == expected["mask"]
        and no_text.get("schema_version") == "cpa-frame-witness.v1"
        and no_text.get("status") == "OBSERVED"
        and no_text.get("image_sha256") == expected["pre"][7:]
        and '"has_readable_text":false' in str(no_text.get("answer"))
        and '"text_fragments":[]' in str(no_text.get("answer"))
        and joint.get("status") == "PASS"
        and joint.get("pass") is True
        and joint.get("cover_sha256") == expected["final"]
        and isinstance(joint.get("verdict"), Mapping)
        and joint["verdict"].get("unrelated_or_misleading_elements") == []
    ):
        raise QixiCoverSuccessorError("trial identity/no-text/joint-QC evidence invalid")
    return files, generation


def _portableize(value: object, *, paths: Mapping[str, str]) -> object:
    """Copy provenance semantics while removing only concrete host locators."""

    if isinstance(value, Mapping):
        return {str(key): _portableize(child, paths=paths) for key, child in value.items()}
    if isinstance(value, list):
        return [_portableize(child, paths=paths) for child in value]
    if isinstance(value, str) and value.startswith("/"):
        replacement = paths.get(Path(value).name)
        if replacement is None:
            raise QixiCoverSuccessorError("provider provenance has an unknown absolute locator")
        return replacement
    return value


def _expected_provenance_source_hashes(authority: Mapping[str, object]) -> dict[str, str]:
    """Return all six source hashes from the sealed Qixi authority."""

    replacement = authority["replacement"]
    joint = authority["joint_qc"]
    if not isinstance(replacement, Mapping) or not isinstance(joint, Mapping):
        raise QixiCoverSuccessorError("sealed provenance authority is malformed")
    values = {
        "generation": replacement.get("provider_generation_sha256"),
        "request": replacement.get("provider_request_sha256"),
        "response": replacement.get("provider_response_sha256"),
        "identity": replacement.get("identity_witness_sha256"),
        "no_text": replacement.get("no_model_text_witness_sha256"),
        "joint": joint.get("sha256"),
    }
    if any(not isinstance(value, str) or not value.startswith("sha256:") for value in values.values()):
        raise QixiCoverSuccessorError("sealed provenance hashes are malformed")
    return {key: str(value) for key, value in values.items()}


def _portable_path_map() -> dict[str, str]:
    return {
        _TRIAL_NAMES["final"]: _PORTABLE_PATHS["final"],
        _TRIAL_NAMES["background"]: _PORTABLE_PATHS["background"],
        _TRIAL_NAMES["pre"]: _PORTABLE_PATHS["pre"],
        _TRIAL_NAMES["mask"]: _PORTABLE_PATHS["mask"],
        _TRIAL_NAMES["identity"]: _PORTABLE_PATHS["identity"],
        _TRIAL_NAMES["request"]: "evidence/qixi-cover-successor/" + _PROVENANCE_NAMES["request"],
        _TRIAL_NAMES["response"]: "evidence/qixi-cover-successor/" + _PROVENANCE_NAMES["response"],
        "cover-reference.png": _PORTABLE_PATHS["reference"],
    }


def _write_portable_provenance(
    *, evidence: Path, files: Mapping[str, Path], authority: Mapping[str, object]
) -> dict[str, str]:
    replacement = authority["replacement"]
    assert isinstance(replacement, Mapping)
    source_hashes = _expected_provenance_source_hashes(authority)
    path_map = _portable_path_map()
    written: dict[str, str] = {}
    identity_destination = evidence / _TRIAL_NAMES["identity"]
    shutil.copy2(files["identity"], identity_destination)
    written["identity"] = _sha(identity_destination)
    identity_document = {
        "image_path": _PORTABLE_PATHS["identity"],
        "image_sha256": _sha(identity_destination),
    }
    identity_payload = {
        "schema_version": PORTABLE_PROVENANCE_SCHEMA,
        "kind": "identity",
        "source_sha256": source_hashes["identity"],
        "document": identity_document,
    }
    _write_json(evidence / _PROVENANCE_NAMES["identity"], identity_payload)
    for key in ("generation", "no_text", "joint", "request", "response"):
        source = _load_json(files[key], label=f"trial {key}")
        document = _portableize(source, paths=path_map)
        _assert_no_absolute_locator(document, label=f"portable provenance {key}")
        payload = {
            "schema_version": PORTABLE_PROVENANCE_SCHEMA,
            "kind": key,
            "source_sha256": source_hashes[key],
            "document": document,
        }
        destination = evidence / _PROVENANCE_NAMES[key]
        _write_json(destination, payload)
        written[key] = _sha(destination)
    return written


def _route_decision(
    *, original: Mapping[str, object], story: Mapping[str, object], generation: Mapping[str, object]
) -> dict[str, Any]:
    route = copy.deepcopy(original.get("route_decision"))
    if not isinstance(route, dict):
        raise QixiCoverSuccessorError("predecessor route decision missing")
    reason = (
        "sealed Qixi displacement: CPA title-cover QC failed the classroom/uniform "
        "narrative; canonical e3fa route-preserving repair still retained it; CPA redraw "
        "passed identity, no-model-text, and Qixi joint-QC"
    )
    rejected = {
        "screenshot_direct": reason + "; direct screenshot cannot displace the failed narrative",
        "screenshot_polish": reason + "; e3fa route-preserving polish retained classroom narrative",
    }
    route.update(
        {
            "selected_treatment": "cpa_redraw",
            "actual_treatment": "cpa_redraw",
            "reason": reason,
            "selected_rationale": reason,
            "execution_status": "READY",
            "image_generation_planned": True,
            "image_generation_attempted": True,
            "image_generation_used": True,
            "alternatives": [
                {
                    "treatment": "screenshot_direct",
                    "status": "REJECTED",
                    "rationale": reason,
                    "per_frame_evidence": reason,
                    "rejected_reason": rejected["screenshot_direct"],
                },
                {
                    "treatment": "screenshot_polish",
                    "status": "REJECTED",
                    "rationale": reason,
                    "per_frame_evidence": reason,
                    "rejected_reason": rejected["screenshot_polish"],
                },
                {
                    "treatment": "cpa_redraw",
                    "status": "SELECTED",
                    "rationale": reason,
                    "per_frame_evidence": reason,
                    "rejected_reason": None,
                },
            ],
            "rejected_alternatives": [
                {"treatment": treatment, "rejected_reason": rejection}
                for treatment, rejection in rejected.items()
            ],
            "qixi_route_displacement": {
                "schema_version": "qixi-cover-route-displacement.v1",
                "predecessor_selected_treatment": "screenshot_polish",
            },
        }
    )
    candidate = dict(generation)
    candidate["route_decision"] = route
    candidate["story_contract"] = cover_story_contract_binding(story)
    if not validate_cover_route_decision(candidate, allow_legacy_v1=False):
        raise QixiCoverSuccessorError("canonical route decision v2 rejects successor")
    return route


def _generation(
    *,
    original: Mapping[str, object],
    trial: Mapping[str, object],
    package: Path,
    repo: Path,
    story: Mapping[str, object],
    punch_response: str,
) -> dict[str, Any]:
    generation = copy.deepcopy(dict(trial))
    final = package / _PORTABLE_PATHS["final"]
    background = package / _PORTABLE_PATHS["background"]
    pre_overlay = package / _PORTABLE_PATHS["pre"]
    mask = package / _PORTABLE_PATHS["mask"]
    generation.update(
        {
            "cover_origin": "AI_REDRAW",
            "cover_status": "AI_COVER_READY",
            "cover_treatment": {
                "treatment": "cpa_redraw",
                "reason": "sealed Qixi displacement authority",
            },
            "method": "images.edit",
            "image_gen_model": "cpa",
            "image_generation_planned": True,
            "image_generation_attempted": True,
            "image_generation_used": True,
            "final_cover": _PORTABLE_PATHS["final"],
            "final_cover_sha256": _sha(final),
            "ai_background": _PORTABLE_PATHS["background"],
            "ai_background_sha256": _sha(background),
            "pre_overlay_path": _PORTABLE_PATHS["pre"],
            "pre_overlay_sha256": _sha(pre_overlay),
            "reference_image": _PORTABLE_PATHS["reference"],
            "reference_sha256": original.get("reference_sha256"),
            "request_path": "evidence/qixi-cover-successor/" + _TRIAL_NAMES["request"],
            "response_path": "evidence/qixi-cover-successor/" + _TRIAL_NAMES["response"],
            "story_contract": cover_story_contract_binding(story),
            "title": original.get("title"),
        }
    )
    lines = generation.get("rendered_lines")
    if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
        raise QixiCoverSuccessorError("rendered title lines missing")
    cover_text = "\n".join(lines)
    reviewed_lines, punch_receipt = review_cover_punch_semantics(
        title=str(original.get("title") or ""),
        cover_text=cover_text,
        story_hook=str(story.get("selection_hook") or ""),
        punch=tuple(lines),
        llm_call=lambda _prompt: punch_response,
    )
    if tuple(lines) != reviewed_lines or not validate_cover_punch_semantic_review(
        punch_receipt,
        rendered_lines=lines,
        cover_text=cover_text,
        story_hook=str(story.get("selection_hook") or ""),
    ):
        raise QixiCoverSuccessorError("canonical punch semantic review fails")
    art_direction = copy.deepcopy(original.get("art_direction"))
    if not isinstance(art_direction, dict):
        raise QixiCoverSuccessorError("predecessor art direction missing")
    art_direction["cover_punch"] = list(lines)
    art_direction["cover_punch_semantic_review"] = punch_receipt
    generation.update(
        {
            "cover_text": cover_text,
            "cover_text_mode": "punch",
            "cover_punch": list(lines),
            "cover_punch_allowed": True,
            "art_direction": art_direction,
        }
    )
    pixels = generation.get("rendered_text_pixels")
    if not isinstance(pixels, dict):
        raise QixiCoverSuccessorError("rendered text evidence missing")
    pixels["mask_path"] = _PORTABLE_PATHS["mask"]
    pixels["pre_overlay_path"] = _PORTABLE_PATHS["pre"]
    identity = generation.get("final_host_identity_verification")
    if not isinstance(identity, dict) or not isinstance(identity.get("witness"), dict):
        raise QixiCoverSuccessorError("identity evidence missing")
    identity.update(
        {
            "final_cover_path": _PORTABLE_PATHS["final"],
            "comparison_path": _PORTABLE_PATHS["identity"],
            "reference_path": _PORTABLE_PATHS["reference"],
        }
    )
    identity["witness"]["image_path"] = _PORTABLE_PATHS["identity"]
    generation["route_decision"] = _route_decision(
        original=original, story=story, generation=generation
    )
    font = resolve_trusted_cover_font(
        file_name=str(pixels.get("font_file_name") or ""),
        expected_sha256=str(pixels.get("font_file_sha256") or ""),
        channel_profile=load_channel_profile(repo),
        root=repo,
    )
    if not verify_rendered_text_pixel_artifacts(
        pixels,
        final_cover_path=final,
        pre_overlay_path=pre_overlay,
        mask_path=mask,
        font_path=font,
        expected_pre_overlay_sha256=_sha(pre_overlay),
    ):
        raise QixiCoverSuccessorError("title-mask recomposition fails")
    if not verify_pre_overlay_route_background(
        route_background_path=background,
        pre_overlay_path=pre_overlay,
        expected_route_background_sha256=_sha(background),
        text_backing=generation.get("text_backing"),
        scrim=generation.get("scrim"),
    ):
        raise QixiCoverSuccessorError("background recomposition fails")
    bbox = pixels.get("text_pixel_bbox")
    if not (isinstance(bbox, list) and len(bbox) == 4 and 260 <= bbox[0] <= bbox[2] <= 1660):
        raise QixiCoverSuccessorError("feed-safe bbox fails")
    if not validate_final_host_identity_verification(generation):
        raise QixiCoverSuccessorError("identity v3 evidence fails")
    validate_portable_cover_locators(generation)
    return generation


def _provenance_document(
    package: Path, key: str, *, authority: Mapping[str, object], repo: Path
) -> dict[str, Any]:
    path = _regular(
        package,
        "evidence/qixi-cover-successor/" + _PROVENANCE_NAMES[key],
        label=f"portable provenance {key}",
    )
    value = _load_json(path, label=f"portable provenance {key}")
    if value.get("schema_version") != PORTABLE_PROVENANCE_SCHEMA or value.get("kind") != key:
        raise QixiCoverSuccessorError(f"portable provenance {key} schema drifts")
    document = value.get("document")
    if not isinstance(document, dict):
        raise QixiCoverSuccessorError(f"portable provenance {key} document missing")
    _assert_no_absolute_locator(value, label=f"portable provenance {key}")
    expected_source = _expected_provenance_source_hashes(authority)[key]
    if value.get("source_sha256") != expected_source:
        raise QixiCoverSuccessorError(f"portable provenance {key} source hash drifts")
    replacement = authority["replacement"]
    joint = authority["joint_qc"]
    assert isinstance(replacement, Mapping) and isinstance(joint, Mapping)
    sealed_paths = {
        "generation": replacement.get("provider_generation_relative_path"),
        "request": replacement.get("provider_request_relative_path"),
        "response": replacement.get("provider_response_relative_path"),
        "joint": joint.get("relative_path"),
    }
    sealed_path = sealed_paths.get(key)
    if sealed_path is not None:
        if not isinstance(sealed_path, str):
            raise QixiCoverSuccessorError(f"portable provenance {key} sealed path missing")
        source = _load_json(_regular(repo, sealed_path, label=f"sealed {key}"), label=f"sealed {key}")
        expected_document = _portableize(source, paths=_portable_path_map())
        if document != expected_document:
            raise QixiCoverSuccessorError(f"portable provenance {key} document drifts")
    elif key == "identity":
        expected_document = {
            "image_path": _PORTABLE_PATHS["identity"],
            "image_sha256": expected_source,
        }
        if (
            document != expected_document
            or _canonical_sha(document)
            != replacement.get("identity_portable_document_sha256")
        ):
            raise QixiCoverSuccessorError("portable provenance identity document drifts")
    elif key == "no_text":
        if _canonical_sha(document) != replacement.get("no_model_text_portable_document_sha256"):
            raise QixiCoverSuccessorError("portable provenance no_text document drifts")
    else:
        raise QixiCoverSuccessorError(f"portable provenance {key} is unsupported")
    return document


def _validate_deep_package(
    *, package: Path, receipt: Mapping[str, object], repo: Path
) -> None:
    authority = load_authority(repo)
    validate_provider_evidence(authority=authority, bundle_root=repo)
    replacement = authority["replacement"]
    assert isinstance(replacement, Mapping)
    record_path = _regular(package, f"{CANDIDATE_ID}.record.json", label="record")
    publish_path = _regular(package, f"{CANDIDATE_ID}.publish.json", label="publish")
    record = _load_json(record_path, label="record")
    publish = _load_json(publish_path, label="publish")
    staging = record.get("publish_staging")
    generation = staging.get("cover_generation") if isinstance(staging, Mapping) else None
    if not isinstance(generation, dict) or publish.get("cover_generation") != generation:
        raise QixiCoverSuccessorError("record/publish cover generation drifts")
    if record.get("cover_generation") != generation:
        raise QixiCoverSuccessorError("record cover generation drifts")
    if record.get("story_contract", {}).get("candidate_id") != CANDIDATE_ID:
        raise QixiCoverSuccessorError("record candidate/story contract drifts")
    if generation.get("rendered_lines") != replacement["required_title_lines"]:
        raise QixiCoverSuccessorError("cover text lines drift")
    validate_portable_cover_locators(generation)
    if not validate_cover_route_decision(generation, allow_legacy_v1=False):
        raise QixiCoverSuccessorError("canonical route decision v2 fails")
    if not validate_final_host_identity_verification(generation):
        raise QixiCoverSuccessorError("identity v3 fails")
    pixels = generation.get("rendered_text_pixels")
    if not isinstance(pixels, Mapping):
        raise QixiCoverSuccessorError("pixel evidence missing")
    font = resolve_trusted_cover_font(
        file_name=str(pixels.get("font_file_name") or ""),
        expected_sha256=str(pixels.get("font_file_sha256") or ""),
        channel_profile=load_channel_profile(repo),
        root=repo,
    )
    final = _regular(package, _PORTABLE_PATHS["final"], label="final cover")
    background = _regular(package, _PORTABLE_PATHS["background"], label="route background")
    pre_overlay = _regular(package, _PORTABLE_PATHS["pre"], label="pre-overlay")
    mask = _regular(package, _PORTABLE_PATHS["mask"], label="title mask")
    if not verify_rendered_text_pixel_artifacts(
        pixels,
        final_cover_path=final,
        pre_overlay_path=pre_overlay,
        mask_path=mask,
        font_path=font,
        expected_pre_overlay_sha256=_sha(pre_overlay),
    ):
        raise QixiCoverSuccessorError("receipt pixel recomposition fails")
    if not verify_pre_overlay_route_background(
        route_background_path=background,
        pre_overlay_path=pre_overlay,
        expected_route_background_sha256=_sha(background),
        text_backing=generation.get("text_backing"),
        scrim=generation.get("scrim"),
    ):
        raise QixiCoverSuccessorError("receipt background recomposition fails")
    no_text = _provenance_document(package, "no_text", authority=authority, repo=repo)
    joint = _provenance_document(package, "joint", authority=authority, repo=repo)
    provider_generation = _provenance_document(package, "generation", authority=authority, repo=repo)
    provider_request = _provenance_document(package, "request", authority=authority, repo=repo)
    provider_response = _provenance_document(package, "response", authority=authority, repo=repo)
    identity_provenance = _provenance_document(package, "identity", authority=authority, repo=repo)
    identity_path = _regular(package, _PORTABLE_PATHS["identity"], label="identity comparison")
    identity = generation.get("final_host_identity_verification")
    if (
        not isinstance(identity, Mapping)
        or identity.get("comparison_sha256") != _sha(identity_path)
        or identity_provenance.get("image_sha256") != _sha(identity_path)
    ):
        raise QixiCoverSuccessorError("identity comparison bytes drift")
    if not (
        provider_generation.get("final_cover_sha256") == replacement["final_cover_sha256"]
        and provider_generation.get("ai_background_sha256") == replacement["route_background_sha256"]
        and provider_generation.get("pre_overlay_sha256") == replacement["pre_overlay_sha256"]
        and provider_generation.get("rendered_lines") == replacement["required_title_lines"]
        and provider_request.get("method") == "images.edit"
        and provider_request.get("image_gen_model") == "cpa"
        and isinstance(provider_response.get("attempts"), list)
        and provider_response["attempts"]
        and provider_response["attempts"][0].get("output_sha256")
        == replacement["route_background_sha256"]
        and no_text.get("status") == "OBSERVED"
        and no_text.get("image_sha256") == replacement["pre_overlay_sha256"][7:]
        and '"has_readable_text":false' in str(no_text.get("answer"))
        and '"text_fragments":[]' in str(no_text.get("answer"))
        and joint.get("status") == "PASS"
        and joint.get("pass") is True
        and joint.get("cover_sha256") == replacement["final_cover_sha256"]
        and isinstance(joint.get("verdict"), Mapping)
        and joint["verdict"].get("unrelated_or_misleading_elements") == []
    ):
        raise QixiCoverSuccessorError("receipt no-text/joint-QC evidence fails")
    story = record.get("story_contract")
    art = generation.get("art_direction")
    if not isinstance(story, Mapping) or not isinstance(art, Mapping):
        raise QixiCoverSuccessorError("story/punch authority missing")
    if not validate_cover_punch_semantic_review(
        art.get("cover_punch_semantic_review"),
        rendered_lines=generation.get("rendered_lines"),
        cover_text=str(generation.get("cover_text") or ""),
        story_hook=str(story.get("selection_hook") or ""),
    ):
        raise QixiCoverSuccessorError("punch semantic review fails")
    successor = record.get("qixi_cover_successor")
    if not isinstance(successor, Mapping) or successor.get("authority_sha256") != authority["authority_sha256"]:
        raise QixiCoverSuccessorError("successor authority binding drifts")
    if _canonical_sha(_snapshot(package.parent)) != receipt.get("preimage_noncover_sha256"):
        raise QixiCoverSuccessorError("noncover preimage snapshot drifts")


def validate_applied_receipt(
    receipt: object, *, package_root: Path, repo_root: Path | None = None
) -> dict[str, Any]:
    """Replay all authority, byte, route, provenance and pixel gates."""

    if not isinstance(receipt, Mapping):
        raise QixiCoverSuccessorError("successor receipt is not an object")
    value = dict(receipt)
    required = {
        "schema_version", "mode", "candidate_id", "status", "upload_allowed",
        "ready_for_serial_upload", "authority_sha256", "preimage_noncover_sha256",
        "final_cover_sha256", "route_background_sha256", "pre_overlay_sha256",
        "title_mask_sha256", "record_sha256", "publish_sha256",
    }
    if (
        set(value) != required
        or value.get("schema_version") != SCHEMA
        or value.get("mode") != "APPLIED"
        or value.get("candidate_id") != CANDIDATE_ID
        or value.get("status") != "finished_review_package_no_upload_pending_human_review"
        or value.get("upload_allowed") is not False
        or value.get("ready_for_serial_upload") != "NO_WAITING_FINAL_HUMAN_REVIEW"
    ):
        raise QixiCoverSuccessorError("successor receipt schema/status invalid")
    package = _safe_absolute_dir(package_root, label="package")
    for relative, key in (
        (_PORTABLE_PATHS["final"], "final_cover_sha256"),
        (_PORTABLE_PATHS["background"], "route_background_sha256"),
        (_PORTABLE_PATHS["pre"], "pre_overlay_sha256"),
        (_PORTABLE_PATHS["mask"], "title_mask_sha256"),
        (f"{CANDIDATE_ID}.record.json", "record_sha256"),
        (f"{CANDIDATE_ID}.publish.json", "publish_sha256"),
    ):
        if _sha(_regular(package, relative, label=relative)) != value.get(key):
            raise QixiCoverSuccessorError(f"successor receipt {relative} drifts")
    repo = _safe_absolute_dir(
        repo_root or Path(__file__).resolve().parents[2], label="repository"
    )
    _validate_deep_package(package=package, receipt=value, repo=repo)
    return value


def finalize(
    *,
    repo_root: Path,
    preimage: Path,
    trial_root: Path,
    target: Path,
    apply: bool,
    punch_response: str | None = None,
) -> dict[str, Any]:
    """Build a new private target once; never overwrite a prior package."""

    repo = _safe_absolute_dir(repo_root, label="repository")
    source = _safe_absolute_dir(preimage, label="preimage")
    trial_root = _safe_absolute_dir(trial_root, label="trial root")
    if not target.is_absolute() or os.path.lexists(target):
        raise QixiCoverSuccessorError("target is not create-only")
    parent = _private_output_parent(target.parent)
    if target.parent != parent or target.name != CANDIDATE_ID:
        raise QixiCoverSuccessorError("target must be the candidate under its private parent")
    try:
        authority = load_authority(repo)
        validate_provider_evidence(authority=authority, bundle_root=repo)
    except QixiCoverRouteDisplacementError as exc:
        raise QixiCoverSuccessorError(str(exc)) from exc
    files, trial = _trial_files(trial_root, authority)
    before = _snapshot(source)
    preimage_package = source / "replacement_recuts"
    record = _load_json(
        _regular(preimage_package, f"{CANDIDATE_ID}.record.json", label="predecessor record"),
        label="predecessor record",
    )
    staging = record.get("publish_staging")
    original = staging.get("cover_generation") if isinstance(staging, Mapping) else None
    if not isinstance(original, Mapping) or _sha(
        _regular(preimage_package, _PORTABLE_PATHS["final"], label="predecessor cover")
    ) != authority["predecessor"]["previous_cover_sha256"]:
        raise QixiCoverSuccessorError("predecessor record/source/cover drifts")
    dry = {
        "schema_version": SCHEMA,
        "mode": "DRY_RUN",
        "candidate_id": CANDIDATE_ID,
        "upload_allowed": False,
        "preimage_noncover_sha256": _canonical_sha(before),
        "replacement_final_cover_sha256": authority["replacement"]["final_cover_sha256"],
    }
    if not apply:
        return dry
    if not isinstance(punch_response, str) or not punch_response.strip():
        raise QixiCoverSuccessorError("canonical punch-review response is required for apply")
    stage = Path(tempfile.mkdtemp(prefix=".qixi-cover-successor-", dir=parent))
    try:
        candidate = stage / CANDIDATE_ID
        shutil.copytree(source, candidate, symlinks=True, copy_function=shutil.copy2)
        _seal_private_tree(candidate)
        package = candidate / "replacement_recuts"
        evidence = package / "evidence" / "qixi-cover-successor"
        evidence.mkdir(parents=True, mode=0o700)
        copy_map = {
            "final": _PORTABLE_PATHS["final"],
            "background": _PORTABLE_PATHS["background"],
            "pre": _PORTABLE_PATHS["pre"],
            "mask": _PORTABLE_PATHS["mask"],
        }
        for key, relative in copy_map.items():
            shutil.copy2(files[key], package / relative)
        shutil.copy2(files["background"], package / _PORTABLE_PATHS["route_background"])
        _write_portable_provenance(evidence=evidence, files=files, authority=authority)
        record_path = package / f"{CANDIDATE_ID}.record.json"
        publish_path = package / f"{CANDIDATE_ID}.publish.json"
        current = _load_json(record_path, label="staged record")
        story = current.get("story_contract")
        if not isinstance(story, dict):
            raise QixiCoverSuccessorError("story contract missing")
        generation = _generation(
            original=original,
            trial=trial,
            package=package,
            repo=repo,
            story=story,
            punch_response=punch_response,
        )
        current_staging = current.get("publish_staging")
        if not isinstance(current_staging, dict):
            raise QixiCoverSuccessorError("publish staging missing")
        current_staging.update(
            {
                "cover_generation": generation,
                "cover_text": generation["cover_text"],
                "cover_path": _PORTABLE_PATHS["final"],
                "cover_status": "AI_COVER_READY",
            }
        )
        current.update(
            {
                "cover_generation": generation,
                "cover_path": _PORTABLE_PATHS["final"],
                "cover_status": "AI_COVER_READY",
            }
        )
        hashes = current.get("artifact_hashes")
        if not isinstance(hashes, dict):
            raise QixiCoverSuccessorError("artifact hashes missing")
        hashes["cover_sha256"] = _sha(package / _PORTABLE_PATHS["final"])
        hashes["ai_background_sha256"] = _sha(package / _PORTABLE_PATHS["background"])
        current["qixi_cover_successor"] = {
            "schema_version": "qixi-cover-successor.v2",
            "authority_sha256": authority["authority_sha256"],
            "predecessor_cover_sha256": authority["predecessor"]["previous_cover_sha256"],
            "failed_joint_qc_sha256": authority["predecessor"]["failed_joint_qc_sha256"],
            "route_preserving_attempt": authority["route_preserving_attempt"],
            "portable_provenance": {
                key: "evidence/qixi-cover-successor/" + _PROVENANCE_NAMES[key]
                for key in ("generation", "identity", "no_text", "joint", "request", "response")
            },
            "upload_allowed": False,
        }
        _replace_json(record_path, current)
        publish = _load_json(publish_path, label="staged publish")
        publish.update(
            {
                "cover_generation": generation,
                "cover_text": generation["cover_text"],
                "cover_path": _PORTABLE_PATHS["final"],
                "cover_status": "AI_COVER_READY",
            }
        )
        if isinstance(publish.get("artifact_hashes"), dict):
            publish["artifact_hashes"]["cover_sha256"] = hashes["cover_sha256"]
            publish["artifact_hashes"]["ai_background_sha256"] = hashes["ai_background_sha256"]
        _replace_json(publish_path, publish)
        hashes["publish_draft_sha256"] = _sha(publish_path)
        _replace_json(record_path, current)
        if before != _snapshot(candidate):
            raise QixiCoverSuccessorError("noncover preimage bytes drift")
        receipt = {
            "schema_version": SCHEMA,
            "mode": "APPLIED",
            "candidate_id": CANDIDATE_ID,
            "status": "finished_review_package_no_upload_pending_human_review",
            "upload_allowed": False,
            "ready_for_serial_upload": "NO_WAITING_FINAL_HUMAN_REVIEW",
            "authority_sha256": authority["authority_sha256"],
            "preimage_noncover_sha256": _canonical_sha(before),
            "final_cover_sha256": hashes["cover_sha256"],
            "route_background_sha256": hashes["ai_background_sha256"],
            "pre_overlay_sha256": generation["pre_overlay_sha256"],
            "title_mask_sha256": generation["rendered_text_pixels"]["mask_sha256"],
            "record_sha256": _sha(record_path),
            "publish_sha256": _sha(publish_path),
        }
        _write_json(package / RECEIPT, receipt)
        # Trial copy2 preserves source modes.  Re-seal after every successor
        # mutation, including provenance, rewritten record/publish and the
        # receipt itself, before the create-only rename is possible.
        _seal_private_tree(candidate)
        _fsync_directory(evidence)
        _fsync_directory(package)
        if os.path.lexists(target):
            raise QixiCoverSuccessorError("target ceased being create-only")
        os.rename(candidate, target)
        _fsync_directory(parent)
        return receipt | {"target": str(target), "package_root": str(target / "replacement_recuts")}
    finally:
        shutil.rmtree(stage, ignore_errors=True)
