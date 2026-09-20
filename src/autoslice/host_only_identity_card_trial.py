"""Build deterministic HOST_ONLY identity-card cover pixels in a private trial.

This module closes the pixel-generation gap that the byte-preserving HOST_ONLY
v4 package successor intentionally does not cover.  It consumes one immutable
review package whose legacy screenshot crop falsely preserved the full source
frame, replays the hash-bound source-composition authority through the current
identity-card compositor, remaps the existing landmark/title exclusion through
that deterministic geometry, and renders a new cover without provider calls.

The output is a *trial*, not a review package: no package surface, state file,
upload authority, or final HOST_ONLY witness is written.  A later step must bind
a current v4 final-pixel witness and run the canonical package successor/audit.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from src.autoslice.cover_generation import (
    LidoushaCoverArtDirection,
    _overlay_cover_title,
)
from src.autoslice.cover_identity_landmark import resolve_title_exclusion
from src.autoslice.cover_route_evidence import (
    host_only_visual_safety_evidence,
    record_cover_route_execution,
)
from src.autoslice.cover_screenshot_poster import (
    _compose_screenshot_poster_background,
)
from src.autoslice.cover_source_composition import (
    IDENTITY_CARD_CROP_STRATEGY,
    extract_authority_source_crop,
    source_composition_bbox,
    source_composition_scene_kind,
    validate_source_composition_verification,
)
from src.autoslice.host_only_v4_package_binding import (
    HostOnlyV4PackageBindingError,
    read_regular_file_once,
)
from src.autoslice.review_package_cover_diagnostics import (
    host_only_identity_route_blocker_detail,
)

SCHEMA_VERSION = "host-only-identity-card-cover-trial.v1"
GENERATION_BINDING_SCHEMA = "host-only-identity-card-pixel-successor.v1"
FAILURE_SCHEMA_VERSION = "host-only-identity-card-cover-trial-failure.v1"
GENERATION_BINDING_KEY = "identity_card_pixel_successor"
TITLE_EXCLUSION_AUTHORITY_SCHEMA = "host-only-title-exclusion-authority.v1"


class HostOnlyIdentityCardTrialError(ValueError):
    """The requested deterministic identity-card trial is unsafe or invalid."""


class HostOnlyTitleExclusionRequired(HostOnlyIdentityCardTrialError):
    """The safe background exists, but title placement lacks bound authority."""


def _sha_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _canonical_sha(value: object) -> str:
    return _sha_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _load_object(path: Path, *, label: str, expected_sha256: str | None = None) -> dict[str, Any]:
    try:
        # Reuse the package binder's no-follow reader; parse exactly the bytes
        # checked against the pre-read snapshot, not a second path opening.
        payload = read_regular_file_once(path, label=label)
        if expected_sha256 is not None and _sha_bytes(payload) != expected_sha256:
            raise HostOnlyIdentityCardTrialError(f"{label} changed before consumption")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, HostOnlyV4PackageBindingError) as exc:
        raise HostOnlyIdentityCardTrialError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise HostOnlyIdentityCardTrialError(f"{label} is not a JSON object")
    return value


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_root(path: Path, *, label: str) -> Path:
    raw = path.absolute()
    try:
        info = os.lstat(raw)
    except OSError as exc:
        raise HostOnlyIdentityCardTrialError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise HostOnlyIdentityCardTrialError(f"{label} is not a regular directory")
    return raw.resolve(strict=True)


def _prepare_destination(path: Path) -> Path:
    raw = path.absolute()
    if os.path.lexists(raw):
        raise HostOnlyIdentityCardTrialError("trial destination already exists")
    parent = _safe_root(raw.parent, label="trial destination parent")
    info = os.lstat(parent)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise HostOnlyIdentityCardTrialError(
            "trial destination parent must be owner-private mode 0700"
        )
    destination = parent / raw.name
    destination.mkdir(mode=0o700)
    return destination


def _contained_regular(root: Path, locator: object, *, label: str) -> Path:
    if not isinstance(locator, str) or not locator.strip():
        raise HostOnlyIdentityCardTrialError(f"{label} locator is absent")
    relative = Path(locator)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HostOnlyIdentityCardTrialError(f"{label} locator is unsafe")
    path = root.joinpath(*relative.parts)
    try:
        resolved = path.resolve(strict=True)
        info = os.lstat(path)
    except OSError as exc:
        raise HostOnlyIdentityCardTrialError(f"{label} is unavailable") from exc
    if not resolved.is_relative_to(root):
        raise HostOnlyIdentityCardTrialError(f"{label} escapes the package")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise HostOnlyIdentityCardTrialError(f"{label} is not a regular file")
    return path


def _bound_candidates(
    root: Path,
    *,
    expected_sha256: str,
    preferred_name: str,
    suffixes: set[str],
    label: str,
) -> Path:
    if not isinstance(expected_sha256, str) or not expected_sha256.startswith("sha256:"):
        raise HostOnlyIdentityCardTrialError(f"{label} hash is absent")
    preferred: list[Path] = []
    if preferred_name:
        for path in root.rglob(preferred_name):
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise HostOnlyIdentityCardTrialError(f"{label} search encountered a symlink")
            if stat.S_ISREG(info.st_mode) and _sha(path) == expected_sha256:
                preferred.append(path)
    if len(preferred) == 1:
        return preferred[0]
    if len(preferred) > 1:
        raise HostOnlyIdentityCardTrialError(f"package contains multiple preferred {label} files")
    matches: list[Path] = []
    for path in root.rglob("*"):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise HostOnlyIdentityCardTrialError(f"{label} search encountered a symlink")
        if not stat.S_ISREG(info.st_mode) or path.suffix.lower() not in suffixes:
            continue
        if _sha(path) == expected_sha256:
            matches.append(path)
    if len(matches) != 1:
        raise HostOnlyIdentityCardTrialError(f"package must contain exactly one hash-bound {label}")
    return matches[0]


def _generation(document: Mapping[str, object], *, publish: bool) -> dict[str, Any]:
    value: object
    if publish:
        value = document.get("cover_generation")
    else:
        staging = document.get("publish_staging")
        value = staging.get("cover_generation") if isinstance(staging, Mapping) else None
    if not isinstance(value, dict):
        raise HostOnlyIdentityCardTrialError("package surface lacks cover_generation")
    return value


def _json_difference_paths(left: object, right: object, path: str = "") -> list[str]:
    """Return exact JSON-pointer-like paths whose values differ."""

    if type(left) is not type(right):
        return [path or "/"]
    if isinstance(left, Mapping):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}/{key}"
            if key not in left or key not in right:
                differences.append(child)
            else:
                differences.extend(_json_difference_paths(left[key], right[key], child))
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            return [path or "/"]
        differences: list[str] = []
        for index, (left_item, right_item) in enumerate(zip(left, right, strict=True)):
            differences.extend(
                _json_difference_paths(
                    left_item,
                    right_item,
                    f"{path}/{index}",
                )
            )
        return differences
    return [] if left == right else [path or "/"]


def _bound_source_composition_authority(
    *,
    inline: object,
    receipt_path: Path,
    reference_sha256: str,
    reference_name: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Recover one relocated inline authority from its hash-bound receipt.

    A package relocation may rewrite only the concrete reference locators in
    the inline copy.  That changes the canonical witness digest even though the
    sealed receipt, verdict, routing, and image hash remain unchanged.  Consume
    the receipt only when it passes the current validator and every semantic
    field remains byte-for-byte equivalent.
    """

    if not isinstance(inline, Mapping):
        raise HostOnlyIdentityCardTrialError("source-composition inline authority is absent")
    bound = _load_object(receipt_path, label="source-composition receipt")
    if not validate_source_composition_verification(
        bound,
        reference_sha256=reference_sha256,
    ):
        raise HostOnlyIdentityCardTrialError(
            "hash-bound source-composition receipt fails the current validator"
        )
    inline_valid = validate_source_composition_verification(
        inline,
        reference_sha256=reference_sha256,
    )
    differences = _json_difference_paths(inline, bound)
    allowed = {"/reference_path", "/witness/image_path"}
    unexpected = sorted(set(differences) - allowed)
    if unexpected:
        raise HostOnlyIdentityCardTrialError(
            "inline source-composition authority drifts from its hash-bound receipt: "
            + ", ".join(unexpected)
        )
    witness = inline.get("witness")
    bound_witness = bound.get("witness")
    if not isinstance(witness, Mapping) or not isinstance(bound_witness, Mapping):
        raise HostOnlyIdentityCardTrialError("source-composition witness is absent")
    locators = (
        inline.get("reference_path"),
        bound.get("reference_path"),
        witness.get("image_path"),
        bound_witness.get("image_path"),
    )
    if any(Path(str(value or "")).name != reference_name for value in locators):
        raise HostOnlyIdentityCardTrialError(
            "source-composition locator relocation changes the reference basename"
        )
    declared = str(inline.get("witness_receipt_sha256") or "")
    if declared != str(bound.get("witness_receipt_sha256") or ""):
        raise HostOnlyIdentityCardTrialError(
            "inline and bound source-composition witness digests disagree"
        )
    evidence = {
        "schema_version": "source-composition-bound-receipt-recovery.v1",
        "status": "BOUND_RECEIPT_CURRENT",
        "inline_current_validator": inline_valid,
        "bound_receipt_current_validator": True,
        "differing_json_pointers": differences,
        "allowed_locator_differences": sorted(allowed),
        "reference_basename": reference_name,
        "receipt_sha256": _sha(receipt_path),
        "authority_source": (
            "INLINE_AND_BOUND_RECEIPT_EQUAL"
            if not differences
            else "HASH_BOUND_RECEIPT_WITH_RELOCATED_INLINE_LOCATORS"
        ),
    }
    return copy.deepcopy(bound), evidence


def _manifest_item(
    root: Path, *, candidate_id: str
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Path],
    dict[str, dict[str, object]],
]:
    manifest_path = _contained_regular(
        root,
        "review_manifest.json",
        label="review manifest",
    )
    inputs = {"review_manifest": manifest_path}
    before = _snapshot(inputs)
    manifest = _load_object(
        manifest_path,
        label="review manifest",
        expected_sha256=str(before["review_manifest"]["sha256"]),
    )
    items = manifest.get("items")
    if not isinstance(items, list):
        raise HostOnlyIdentityCardTrialError("review manifest items are absent")
    matching = [
        row for row in items if isinstance(row, dict) and row.get("candidate_id") == candidate_id
    ]
    if len(matching) != 1:
        raise HostOnlyIdentityCardTrialError("review manifest must contain one matching candidate")
    item = matching[0]
    evidence_path = _contained_regular(root, item.get("evidence_json"), label="evidence record")
    record_path = _contained_regular(root, item.get("record"), label="delivery record")
    publish_path = _contained_regular(root, item.get("publish_json"), label="publish draft")
    if len({evidence_path, record_path, publish_path}) != 3:
        raise HostOnlyIdentityCardTrialError(
            "evidence, delivery, and publish surfaces must be distinct"
        )
    document_inputs = {
        "evidence_record": evidence_path,
        "delivery_record": record_path,
        "publish_draft": publish_path,
    }
    # These records carry the title, story and exclusion authority. Freeze
    # them before parsing and retain their original snapshots through render.
    inputs.update(document_inputs)
    before.update(_snapshot(document_inputs))
    evidence = _load_object(
        evidence_path,
        label="evidence record",
        expected_sha256=str(before["evidence_record"]["sha256"]),
    )
    record = _load_object(
        record_path,
        label="delivery record",
        expected_sha256=str(before["delivery_record"]["sha256"]),
    )
    publish = _load_object(
        publish_path,
        label="publish draft",
        expected_sha256=str(before["publish_draft"]["sha256"]),
    )
    generations = (
        _generation(evidence, publish=False),
        _generation(record, publish=False),
        _generation(publish, publish=True),
    )
    if not generations[0] == generations[1] == generations[2]:
        raise HostOnlyIdentityCardTrialError("source cover_generation surfaces drift")
    return item, manifest, publish, generations[0], inputs, before


def _art_direction(payload: object) -> LidoushaCoverArtDirection:
    if not isinstance(payload, Mapping):
        raise HostOnlyIdentityCardTrialError("source art_direction is absent")
    fields = {field.name for field in dataclasses.fields(LidoushaCoverArtDirection)}
    kwargs = {key: value for key, value in payload.items() if key in fields}
    if isinstance(kwargs.get("cover_punch"), list):
        kwargs["cover_punch"] = tuple(kwargs["cover_punch"])
    try:
        return LidoushaCoverArtDirection(**kwargs)
    except (TypeError, ValueError) as exc:
        raise HostOnlyIdentityCardTrialError(
            "source art_direction cannot be reconstructed"
        ) from exc


def _bbox_int(values: Sequence[object], *, label: str) -> list[int]:
    if len(values) != 4:
        raise HostOnlyIdentityCardTrialError(f"{label} must contain four values")
    try:
        result = [int(round(float(value))) for value in values]
    except (TypeError, ValueError) as exc:
        raise HostOnlyIdentityCardTrialError(f"{label} is malformed") from exc
    if result[2] <= result[0] or result[3] <= result[1]:
        raise HostOnlyIdentityCardTrialError(f"{label} is empty")
    return result


def _bbox_float(values: Sequence[object], *, label: str) -> list[float]:
    if len(values) != 4:
        raise HostOnlyIdentityCardTrialError(f"{label} must contain four values")
    try:
        result = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise HostOnlyIdentityCardTrialError(f"{label} is malformed") from exc
    if result[2] <= result[0] or result[3] <= result[1]:
        raise HostOnlyIdentityCardTrialError(f"{label} is empty")
    return result


def _boxes_intersect(left: Sequence[int], right: Sequence[int]) -> bool:
    return not (
        left[2] <= right[0] or right[2] <= left[0] or left[3] <= right[1] or right[3] <= left[1]
    )


def _map_box_to_content(
    box: Sequence[float], *, source_size: Sequence[int], content_box: Sequence[int]
) -> list[float]:
    source_width, source_height = (float(source_size[0]), float(source_size[1]))
    x0, y0, x1, y1 = (float(value) for value in content_box)
    scale_x = (x1 - x0) / source_width
    scale_y = (y1 - y0) / source_height
    if abs(scale_x - scale_y) > 1e-6:
        raise HostOnlyIdentityCardTrialError(
            "poster content transform is not an aspect-preserving contain"
        )
    return [
        x0 + float(box[0]) * scale_x,
        y0 + float(box[1]) * scale_y,
        x0 + float(box[2]) * scale_x,
        y0 + float(box[3]) * scale_y,
    ]


def _source_bbox_pixels(bbox_frac: Sequence[float], *, source_size: Sequence[int]) -> list[float]:
    width, height = (float(source_size[0]), float(source_size[1]))
    return [
        bbox_frac[0] * width,
        bbox_frac[1] * height,
        bbox_frac[2] * width,
        bbox_frac[3] * height,
    ]


def _new_base_host_box(
    *,
    source_bbox: Sequence[float],
    crop_box: Sequence[int],
    foreground_box: Sequence[int],
) -> list[float]:
    crop_width = float(crop_box[2] - crop_box[0])
    crop_height = float(crop_box[3] - crop_box[1])
    foreground_width = float(foreground_box[2] - foreground_box[0])
    foreground_height = float(foreground_box[3] - foreground_box[1])
    scale_x = foreground_width / crop_width
    scale_y = foreground_height / crop_height
    if abs(scale_x - scale_y) > 1e-6:
        raise HostOnlyIdentityCardTrialError(
            "identity-card foreground transform is not aspect preserving"
        )
    return [
        foreground_box[0] + (source_bbox[0] - crop_box[0]) * scale_x,
        foreground_box[1] + (source_bbox[1] - crop_box[1]) * scale_y,
        foreground_box[0] + (source_bbox[2] - crop_box[0]) * scale_x,
        foreground_box[1] + (source_bbox[3] - crop_box[1]) * scale_y,
    ]


def _map_landmark_box(
    protected: Sequence[int],
    *,
    old_host_box: Sequence[float],
    new_host_box: Sequence[float],
) -> list[int]:
    old_width = old_host_box[2] - old_host_box[0]
    old_height = old_host_box[3] - old_host_box[1]
    new_width = new_host_box[2] - new_host_box[0]
    new_height = new_host_box[3] - new_host_box[1]
    if min(old_width, old_height, new_width, new_height) <= 0:
        raise HostOnlyIdentityCardTrialError("host geometry is empty")
    scale_x = new_width / old_width
    scale_y = new_height / old_height
    mapped = [
        new_host_box[0] + (protected[0] - old_host_box[0]) * scale_x,
        new_host_box[1] + (protected[1] - old_host_box[1]) * scale_y,
        new_host_box[0] + (protected[2] - old_host_box[0]) * scale_x,
        new_host_box[1] + (protected[3] - old_host_box[1]) * scale_y,
    ]
    # The old exclusion is a manually reviewed bounding region.  A small
    # outward pad keeps the successor conservative after the deterministic
    # affine remap and integer rounding.
    pad = 12
    return [
        max(0, int(mapped[0]) - pad),
        max(0, int(mapped[1]) - pad),
        min(1920, int(mapped[2] + 0.999999) + pad),
        min(1080, int(mapped[3] + 0.999999) + pad),
    ]


def _rebound_exclusion(
    *,
    predecessor: Mapping[str, object],
    old_background_sha256: str,
    new_background_sha256: str,
    source_bbox_frac: Sequence[float],
    source_size: Sequence[int],
    crop_evidence: Mapping[str, object],
    old_transform: Mapping[str, object],
    new_transform: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    raw = predecessor.get("identity_landmark_title_exclusion")
    if not isinstance(raw, Mapping):
        raise HostOnlyTitleExclusionRequired("predecessor identity landmark exclusion is absent")
    allowed = {
        key: copy.deepcopy(raw.get(key))
        for key in (
            "schema_version",
            "landmark",
            "background_sha256",
            "protected_bbox",
            "title_zone",
        )
    }
    try:
        old_exclusion = resolve_title_exclusion(
            allowed,
            background_sha256=old_background_sha256,
        )
    except ValueError as exc:
        raise HostOnlyIdentityCardTrialError(
            "predecessor identity landmark exclusion does not replay"
        ) from exc
    if old_exclusion is None:
        raise HostOnlyTitleExclusionRequired("predecessor identity landmark exclusion is absent")

    old_content = _bbox_int(
        old_transform.get("rendered_content_box") or (),
        label="predecessor rendered_content_box",
    )
    new_content = _bbox_int(
        new_transform.get("rendered_content_box") or (),
        label="successor rendered_content_box",
    )
    old_source_size = _bbox_int(
        [0, 0, *(old_transform.get("source_size") or ())],
        label="predecessor poster source size",
    )[2:]
    new_source_size = _bbox_int(
        [0, 0, *(new_transform.get("source_size") or ())],
        label="successor poster source size",
    )[2:]
    if old_source_size != [int(source_size[0]), int(source_size[1])]:
        raise HostOnlyIdentityCardTrialError(
            "predecessor poster does not contain the full reference canvas"
        )
    if new_source_size != [1920, 1080]:
        raise HostOnlyIdentityCardTrialError("successor identity-card canvas is not 1920x1080")

    source_bbox = _source_bbox_pixels(
        source_bbox_frac,
        source_size=source_size,
    )
    old_host = _map_box_to_content(
        source_bbox,
        source_size=old_source_size,
        content_box=old_content,
    )
    crop_box = _bbox_int(crop_evidence.get("crop_box") or (), label="identity-card crop_box")
    foreground = _bbox_int(
        crop_evidence.get("identity_card_foreground_box") or (),
        label="identity-card foreground box",
    )
    successor_base_host = _new_base_host_box(
        source_bbox=source_bbox,
        crop_box=crop_box,
        foreground_box=foreground,
    )
    new_host = _map_box_to_content(
        successor_base_host,
        source_size=new_source_size,
        content_box=new_content,
    )
    protected = _map_landmark_box(
        old_exclusion["protected_bbox"],
        old_host_box=old_host,
        new_host_box=new_host,
    )
    title_zone = list(old_exclusion["title_zone"])
    adjusted = False
    if _boxes_intersect(protected, title_zone):
        title_zone[1] = max(title_zone[1], protected[3] + 16)
        adjusted = True
    if title_zone[3] - title_zone[1] < 160:
        raise HostOnlyIdentityCardTrialError("mapped identity landmark leaves no safe title zone")
    rebound = {
        "schema_version": old_exclusion["schema_version"],
        "landmark": old_exclusion["landmark"],
        "background_sha256": new_background_sha256,
        "protected_bbox": protected,
        "title_zone": title_zone,
    }
    try:
        resolved = resolve_title_exclusion(
            rebound,
            background_sha256=new_background_sha256,
        )
    except ValueError as exc:
        raise HostOnlyIdentityCardTrialError(
            "successor identity landmark exclusion is invalid"
        ) from exc
    if resolved is None:
        raise HostOnlyIdentityCardTrialError("successor identity landmark exclusion is absent")
    evidence = {
        "schema_version": "identity-landmark-affine-rebind.v1",
        "old_host_bbox": [round(value, 4) for value in old_host],
        "new_host_bbox": [round(value, 4) for value in new_host],
        "old_protected_bbox": list(old_exclusion["protected_bbox"]),
        "new_protected_bbox": protected,
        "old_title_zone": list(old_exclusion["title_zone"]),
        "new_title_zone": title_zone,
        "title_zone_adjusted": adjusted,
        "mapping_basis": ("SOURCE_COMPOSITION_BBOX_THROUGH_OLD_AND_IDENTITY_CARD_TRANSFORMS"),
    }
    return resolved, evidence


def _snapshot(paths: Mapping[str, Path]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for label, path in paths.items():
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise HostOnlyIdentityCardTrialError(f"{label} is not a regular file")
        result[label] = {
            "path": str(path),
            "bytes": info.st_size,
            "sha256": _sha(path),
            "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
    return result


@dataclasses.dataclass(frozen=True)
class _TrialPreimage:
    source: Path
    item: dict[str, Any]
    predecessor: dict[str, Any]
    blocker: str
    safety: dict[str, object]
    reference: Path
    receipt_path: Path
    receipt_sha256: str
    verification: dict[str, object]
    verification_recovery: dict[str, object]
    bbox_frac: tuple[float, float, float, float]
    source_size: tuple[int, int]
    frame: dict[str, object]
    old_background_sha256: str
    old_poster_evidence: dict[str, object]
    old_transform: dict[str, object]
    cover: Path
    inputs: dict[str, Path]
    before: dict[str, dict[str, object]]
    direction: LidoushaCoverArtDirection


@dataclasses.dataclass(frozen=True)
class _RenderedBackground:
    base_path: Path
    crop_evidence: dict[str, object]
    poster_path: Path
    poster_evidence: dict[str, object]
    new_transform: dict[str, object]


@dataclasses.dataclass(frozen=True)
class _RenderedTrial:
    base_path: Path
    crop_evidence: dict[str, object]
    poster_path: Path
    poster_evidence: dict[str, object]
    exclusion_evidence: dict[str, object]
    final_cover: Path
    overlay: dict[str, object]


def _prepare_trial_preimage(
    *,
    source: Path,
    candidate_id: str,
) -> _TrialPreimage:
    item, _manifest, _publish, predecessor, document_inputs, document_before = _manifest_item(
        source,
        candidate_id=candidate_id,
    )
    if predecessor.get("candidate_id") not in {None, candidate_id}:
        raise HostOnlyIdentityCardTrialError(
            "source generation candidate_id disagrees with request"
        )
    if predecessor.get("method") != "screenshot_direct":
        raise HostOnlyIdentityCardTrialError(
            "identity-card trial requires a direct screenshot predecessor"
        )
    blocker = host_only_identity_route_blocker_detail(predecessor)
    if not blocker:
        raise HostOnlyIdentityCardTrialError(
            "predecessor is not blocked by current HOST_ONLY identity evidence"
        )

    reference_sha = str(predecessor.get("reference_sha256") or "")
    reference_name = Path(str(predecessor.get("reference_image") or "")).name
    reference = _bound_candidates(
        source,
        expected_sha256=reference_sha,
        preferred_name=reference_name,
        suffixes={".png", ".jpg", ".jpeg", ".webp"},
        label="cover reference",
    )
    receipt = predecessor.get("source_composition_receipt")
    if not isinstance(receipt, Mapping):
        raise HostOnlyIdentityCardTrialError("source-composition receipt binding is absent")
    receipt_sha = str(receipt.get("sha256") or "")
    receipt_name = Path(str(receipt.get("path") or "")).name
    receipt_path = _bound_candidates(
        source,
        expected_sha256=receipt_sha,
        preferred_name=receipt_name,
        suffixes={".json"},
        label="source-composition receipt",
    )
    verification, verification_recovery = _bound_source_composition_authority(
        inline=predecessor.get("source_composition_verification"),
        receipt_path=receipt_path,
        reference_sha256=reference_sha,
        reference_name=reference.name,
    )
    bbox = source_composition_bbox(verification)
    if bbox is None:
        raise HostOnlyIdentityCardTrialError("source-composition authority has no host bbox")
    bbox_frac = tuple(_bbox_float(bbox, label="source-composition host bbox"))
    scene_kind = source_composition_scene_kind(verification)
    safety = host_only_visual_safety_evidence(
        predecessor.get("story_contract"),
        scene_kind=scene_kind,
    )
    if safety.get("status") != "REQUIRED":
        raise HostOnlyIdentityCardTrialError("story no longer requires HOST_ONLY output")

    frame = predecessor.get("screenshot_frame")
    if not isinstance(frame, Mapping):
        raise HostOnlyIdentityCardTrialError("predecessor screenshot_frame is absent")
    with Image.open(reference) as image:
        source_size = (image.width, image.height)
        reference_pixels = image.convert("RGB")
    old_crop_box = _bbox_int(
        frame.get("crop_box") or (),
        label="predecessor crop_box",
    )
    if old_crop_box != [0, 0, *source_size]:
        raise HostOnlyIdentityCardTrialError("predecessor is not the full-frame crop degeneracy")
    old_base_sha = str(frame.get("crop_output_sha256") or "")
    old_base = _bound_candidates(
        source,
        expected_sha256=old_base_sha,
        preferred_name=f"{candidate_id}.screenshot-base.png",
        suffixes={".png", ".jpg", ".jpeg", ".webp"},
        label="predecessor screenshot base",
    )
    with Image.open(old_base) as image:
        if (image.width, image.height) != source_size:
            raise HostOnlyIdentityCardTrialError(
                "predecessor screenshot base size drifts from reference"
            )
        old_base_pixels = image.convert("RGB")
    if ImageChops.difference(reference_pixels, old_base_pixels).getbbox() is not None:
        raise HostOnlyIdentityCardTrialError(
            "predecessor full-frame screenshot base pixels drift from reference"
        )

    old_background_sha = str(predecessor.get("ai_background_sha256") or "")
    old_background = _bound_candidates(
        source,
        expected_sha256=old_background_sha,
        preferred_name=Path(str(predecessor.get("ai_background") or "")).name,
        suffixes={".png", ".jpg", ".jpeg", ".webp"},
        label="predecessor poster",
    )
    old_poster_raw = predecessor.get("screenshot_graphic_poster")
    if not isinstance(old_poster_raw, Mapping):
        raise HostOnlyIdentityCardTrialError("predecessor poster evidence is absent")
    old_poster_evidence = copy.deepcopy(dict(old_poster_raw))
    old_transform_raw = old_poster_evidence.get("source_frame_transform")
    if not isinstance(old_transform_raw, Mapping):
        raise HostOnlyIdentityCardTrialError("predecessor poster transform is absent")
    old_transform = copy.deepcopy(dict(old_transform_raw))
    if old_transform.get("input_sha256") != old_base_sha:
        raise HostOnlyIdentityCardTrialError(
            "predecessor poster is not bound to the full-frame base"
        )

    cover = _contained_regular(
        source,
        item.get("cover"),
        label="source cover",
    )
    inputs = {
        "reference": reference,
        "source_composition_receipt": receipt_path,
        "predecessor_screenshot_base": old_base,
        "predecessor_poster": old_background,
        "predecessor_cover": cover,
        **document_inputs,
    }
    before = _snapshot(inputs)
    if any(before[label] != snapshot for label, snapshot in document_before.items()):
        raise HostOnlyIdentityCardTrialError(
            "source package records changed during preimage preparation"
        )
    return _TrialPreimage(
        source=source,
        item=item,
        predecessor=predecessor,
        blocker=blocker,
        safety=copy.deepcopy(dict(safety)),
        reference=reference,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha,
        verification=verification,
        verification_recovery=verification_recovery,
        bbox_frac=bbox_frac,
        source_size=source_size,
        frame=copy.deepcopy(dict(frame)),
        old_background_sha256=old_background_sha,
        old_poster_evidence=old_poster_evidence,
        old_transform=old_transform,
        cover=cover,
        inputs=inputs,
        before=before,
        direction=_art_direction(predecessor.get("art_direction")),
    )


def _render_trial_background(
    *,
    trial: Path,
    preimage: _TrialPreimage,
) -> _RenderedBackground:
    base_path = trial / "identity-card.base.png"
    crop_evidence = extract_authority_source_crop(
        reference_path=preimage.reference,
        output_path=base_path,
        frame_ms=int(preimage.frame.get("frame_ms") or 0),
        verification=preimage.verification,
        verification_receipt_path=preimage.receipt_path,
        verification_receipt_sha256=preimage.receipt_sha256,
    )
    if not (
        crop_evidence.get("crop_strategy") == IDENTITY_CARD_CROP_STRATEGY
        and crop_evidence.get("full_frame_degeneracy_avoided") is True
        and crop_evidence.get("crop_box") != [0, 0, *preimage.source_size]
        and crop_evidence.get("crop_output_sha256") == _sha(base_path)
    ):
        raise HostOnlyIdentityCardTrialError(
            "current compositor did not materialize the identity-card strategy"
        )

    old_transform = preimage.old_transform
    old_poster = preimage.old_poster_evidence
    source_led = (
        old_poster.get("background_style") == "source-led"
        and old_poster.get("composition") == "source_frame_with_title_reservation"
    )
    # Source-led production measured this caption band before composing its
    # poster. Omitting it here selects a different default and shrinks the
    # picture, even though neither the wording nor layout was changed.
    source_title_zone = (
        tuple(_bbox_int(old_poster.get("title_zone") or (), label="predecessor title_zone"))
        if source_led
        else None
    )
    poster_path = trial / "identity-card.poster.png"
    poster_evidence = _compose_screenshot_poster_background(
        base_path,
        poster_path,
        art_direction=preimage.direction,
        source_ai_modified=False,
        face_safe_contain=True,
        **({"source_title_zone": source_title_zone} if source_led else {}),
    )
    new_transform = poster_evidence.get("source_frame_transform")
    if not isinstance(new_transform, Mapping):
        raise HostOnlyIdentityCardTrialError("successor poster transform is absent")
    legacy_contain = (
        old_transform.get("card_fit") == "contain_face_safe"
        and new_transform.get("card_fit") == "contain_face_safe"
    )
    source_led_contain = (
        source_led
        and poster_evidence.get("background_style") == "source-led"
        and poster_evidence.get("composition") == "source_frame_with_title_reservation"
        and old_poster.get("title_zone") == poster_evidence.get("title_zone")
        and all(
            transform.get("card_fit") == "full_frame"
            and transform.get("crop_applied") is False
            and transform.get("full_frame_preserved") is True
            and transform.get("face_safe_contain") is True
            and transform.get("center_4_3_safe") is True
            and transform.get("ai_modified") is False
            for transform in (old_transform, new_transform)
        )
    )
    if not (
        (legacy_contain or source_led_contain)
        and old_transform.get("rendered_content_box") == new_transform.get("rendered_content_box")
        and old_poster.get("screenshot_card") == poster_evidence.get("screenshot_card")
    ):
        raise HostOnlyIdentityCardTrialError(
            "successor poster does not preserve the reviewed screen-space geometry"
        )
    return _RenderedBackground(
        base_path=base_path,
        crop_evidence=copy.deepcopy(dict(crop_evidence)),
        poster_path=poster_path,
        poster_evidence=copy.deepcopy(dict(poster_evidence)),
        new_transform=copy.deepcopy(dict(new_transform)),
    )


def _prepare_current_title_exclusion(
    path: Path, *, candidate_id: str, preimage: _TrialPreimage
) -> tuple[dict[str, object], _TrialPreimage]:
    """Consume a real current-poster placement review, never identity approval."""

    if preimage.predecessor.get("identity_landmark_title_exclusion") is not None:
        raise HostOnlyIdentityCardTrialError(
            "current title review cannot override predecessor exclusion"
        )
    paths = {"title_exclusion_authority": path.absolute()}
    before = _snapshot(paths)
    document = _load_object(
        paths["title_exclusion_authority"],
        label="title exclusion authority",
        expected_sha256=str(before["title_exclusion_authority"]["sha256"]),
    )
    expected_keys = {
        "schema_version",
        "scope",
        "authority",
        "candidate_id",
        "predecessor_generation_sha256",
        "background_sha256",
        "exclusion",
        "reviewed_by",
        "reviewed_at",
        "observations",
        "inspection",
    }
    if (
        set(document) != expected_keys
        or document.get("schema_version") != TITLE_EXCLUSION_AUTHORITY_SCHEMA
        or document.get("scope") != "TITLE_PLACEMENT_ONLY"
        or document.get("authority") != "ROOT_AGENT_VISUAL_REVIEW"
        or document.get("candidate_id") != candidate_id
        or document.get("predecessor_generation_sha256") != _canonical_sha(preimage.predecessor)
    ):
        raise HostOnlyIdentityCardTrialError("current title exclusion authority binding is invalid")
    try:
        reviewed_at = dt.datetime.fromisoformat(str(document.get("reviewed_at") or ""))
    except ValueError as exc:
        raise HostOnlyIdentityCardTrialError("title exclusion review time is invalid") from exc
    observations = document.get("observations")
    if (
        reviewed_at.tzinfo is None
        or not isinstance(document.get("reviewed_by"), str)
        or not document["reviewed_by"].strip()
        or not isinstance(observations, list)
        or not observations
        or any(not isinstance(row, str) or not row.strip() for row in observations)
    ):
        raise HostOnlyIdentityCardTrialError("title exclusion review provenance is incomplete")
    inspection = document.get("inspection")
    if (
        not isinstance(inspection, dict)
        or inspection.get("source_sha256") != document.get("background_sha256")
        or inspection.get("source_size") != [1920, 1080]
        or inspection.get("transform") != "FULL_FRAME_RESIZE"
        or not isinstance(inspection.get("image_sha256"), str)
        or len(inspection["image_sha256"]) != 71
        or not inspection["image_sha256"].startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in inspection["image_sha256"][7:])
    ):
        raise HostOnlyIdentityCardTrialError("title exclusion inspection binding is invalid")
    try:
        exclusion = resolve_title_exclusion(
            document.get("exclusion"),
            background_sha256=str(document.get("background_sha256") or ""),
        )
    except ValueError as exc:
        raise HostOnlyIdentityCardTrialError("current title exclusion geometry is invalid") from exc
    if exclusion is None:
        raise HostOnlyIdentityCardTrialError("current title exclusion geometry is absent")
    frozen = {"document": document, "input": before["title_exclusion_authority"]}
    return frozen, dataclasses.replace(
        preimage, inputs={**preimage.inputs, **paths}, before={**preimage.before, **before}
    )


def _render_trial_title(
    *,
    preimage: _TrialPreimage,
    background: _RenderedBackground,
    title_exclusion_authority: Mapping[str, object] | None = None,
) -> _RenderedTrial:
    if title_exclusion_authority is None:
        exclusion, exclusion_evidence = _rebound_exclusion(
            predecessor=preimage.predecessor,
            old_background_sha256=preimage.old_background_sha256,
            new_background_sha256=_sha(background.poster_path),
            source_bbox_frac=preimage.bbox_frac,
            source_size=preimage.source_size,
            crop_evidence=background.crop_evidence,
            old_transform=preimage.old_transform,
            new_transform=background.new_transform,
        )
    else:
        document = title_exclusion_authority["document"]
        if document["background_sha256"] != _sha(background.poster_path):
            raise HostOnlyIdentityCardTrialError("current title review poster bytes drift")
        exclusion = resolve_title_exclusion(
            document["exclusion"], background_sha256=_sha(background.poster_path)
        )
        exclusion_evidence = {
            "schema_version": "current-poster-title-exclusion-consumption.v1",
            "mapping_basis": "REVIEWED_CURRENT_POSTER_NO_AFFINE_REMAP",
            "authority_input": copy.deepcopy(title_exclusion_authority["input"]),
            "authority": copy.deepcopy(document),
            "final_host_identity_verified": False,
            "upload_authorized": False,
        }
    final_cover = background.poster_path.parent / "identity-card.cover.png"
    overlay = _overlay_cover_title(
        background.poster_path,
        final_cover,
        cover_text=str(preimage.predecessor.get("cover_text") or ""),
        art_direction=preimage.direction,
        full_text_cover_contract=preimage.predecessor.get("full_text_cover_contract"),
        identity_landmark_title_exclusion=exclusion,
    )
    if overlay.get("rendered_lines") != preimage.predecessor.get("rendered_lines"):
        raise HostOnlyIdentityCardTrialError(
            "successor title lines drift from predecessor authority"
        )
    with Image.open(final_cover) as image:
        if image.size != (1920, 1080):
            raise HostOnlyIdentityCardTrialError("successor cover canvas is not 1920x1080")
    return _RenderedTrial(
        base_path=background.base_path,
        crop_evidence=background.crop_evidence,
        poster_path=background.poster_path,
        poster_evidence=background.poster_evidence,
        exclusion_evidence=exclusion_evidence,
        final_cover=final_cover,
        overlay=copy.deepcopy(dict(overlay)),
    )


def _build_successor_generation(
    *,
    candidate_id: str,
    preimage: _TrialPreimage,
    rendered: _RenderedTrial,
) -> dict[str, object]:
    successor = copy.deepcopy(preimage.predecessor)
    predecessor_host = successor.pop("final_host_identity_verification", None)
    predecessor_face = successor.pop("polish_face_verification", None)
    successor.update(
        {
            "source_composition_verification": preimage.verification,
            "source_composition_receipt": {
                "path": str(preimage.receipt_path),
                "sha256": preimage.receipt_sha256,
            },
            "status": "BLOCKED_HOST_ONLY_V4_WITNESS_REQUIRED",
            "method": "screenshot_direct",
            "model": "none",
            "image_gen_model": "none",
            "cover_origin": "SOURCE_SCREENSHOT",
            "image_generation_used": False,
            "screenshot_frame": rendered.crop_evidence,
            "screenshot_graphic_poster": rendered.poster_evidence,
            "ai_background": str(rendered.poster_path),
            "ai_background_sha256": _sha(rendered.poster_path),
            "final_cover": str(rendered.final_cover),
            "final_cover_sha256": _sha(rendered.final_cover),
            "attempted_models": [],
            "model_fallback_used": False,
            "fallback_used": False,
            **rendered.overlay,
        }
    )
    route = successor.get("route_decision")
    if isinstance(route, dict):
        route["host_identity_required"] = True
        route["host_only_visual_required"] = True
        route["host_only_visual_safety_evidence"] = copy.deepcopy(preimage.safety)
    record_cover_route_execution(
        successor,
        actual_treatment=None,
        execution_status="BLOCKED",
        image_generation_attempted=False,
        image_generation_used=False,
        detail=(
            "deterministic identity-card pixels require a current HOST_ONLY "
            "v4 final-pixel witness before package binding"
        ),
    )
    successor[GENERATION_BINDING_KEY] = {
        "schema_version": GENERATION_BINDING_SCHEMA,
        "status": "PIXELS_READY_WITNESS_REQUIRED",
        "candidate_id": candidate_id,
        "predecessor_generation_sha256": _canonical_sha(preimage.predecessor),
        "predecessor_final_cover_sha256": preimage.predecessor.get("final_cover_sha256"),
        "predecessor_host_identity_sha256": (
            _canonical_sha(predecessor_host) if isinstance(predecessor_host, Mapping) else None
        ),
        "predecessor_face_verification_sha256": (
            _canonical_sha(predecessor_face) if isinstance(predecessor_face, Mapping) else None
        ),
        "crop_strategy": rendered.crop_evidence.get("crop_strategy"),
        "full_frame_degeneracy_avoided": True,
        "source_composition_receipt_sha256": preimage.receipt_sha256,
        "source_composition_recovery": preimage.verification_recovery,
        "landmark_rebind": rendered.exclusion_evidence,
        "provider_calls": 0,
        "image_generation_calls": 0,
        "package_writes": 0,
        "state_writes": 0,
        "upload_calls": 0,
        "upload_allowed": False,
    }
    return successor


def _finalize_background_blocker(
    *,
    candidate_id: str,
    trial: Path,
    preimage: _TrialPreimage,
    background: _RenderedBackground,
) -> dict[str, object]:
    binding = {
        "schema_version": "host-only-identity-card-background-trial.v1",
        "status": "BACKGROUND_READY_TITLE_EXCLUSION_AUTHORITY_REQUIRED",
        "candidate_id": candidate_id,
        "predecessor_generation_sha256": _canonical_sha(preimage.predecessor),
        "predecessor_final_cover_sha256": preimage.predecessor.get("final_cover_sha256"),
        "crop_strategy": background.crop_evidence.get("crop_strategy"),
        "full_frame_degeneracy_avoided": True,
        "source_composition_receipt_sha256": preimage.receipt_sha256,
        "source_composition_recovery": preimage.verification_recovery,
        "title_exclusion_authority": "REQUIRED_NOT_PRESENT",
        "provider_calls": 0,
        "image_generation_calls": 0,
        "package_writes": 0,
        "state_writes": 0,
        "upload_calls": 0,
        "upload_allowed": False,
    }
    binding_path = trial / "identity-card.background_trial.json"
    _write_new(binding_path, _json_bytes(binding))
    if preimage.before != _snapshot(preimage.inputs):
        raise HostOnlyIdentityCardTrialError(
            "source package inputs changed during background trial construction"
        )
    outputs = _snapshot(
        {
            "base": background.base_path,
            "poster": background.poster_path,
            "background_trial": binding_path,
        }
    )
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS_BACKGROUND_READY_TITLE_EXCLUSION_AUTHORITY_REQUIRED",
        "candidate_id": candidate_id,
        "source_package": str(preimage.source),
        "destination": str(trial),
        "source_inputs": preimage.before,
        "outputs": outputs,
        "crop_evidence": background.crop_evidence,
        "source_composition_recovery": preimage.verification_recovery,
        "host_only_visual_safety_evidence": preimage.safety,
        "predecessor_blocker": preimage.blocker,
        "source_preimage_unchanged": True,
        "background_pixels_changed": (
            _sha(background.poster_path) != preimage.old_background_sha256
        ),
        "final_cover_generated": False,
        "cover_pixels_changed": None,
        "video_pixels_changed": False,
        "provider_calls": 0,
        "image_generation_calls": 0,
        "package_writes": 0,
        "production_state_writes": 0,
        "upload_calls": 0,
        "upload_allowed": False,
        "title_exclusion_authority": "REQUIRED_NOT_PRESENT",
        "final_host_only_v4_witness": "NOT_APPLICABLE_UNTIL_FINAL_COVER_EXISTS",
        "next_required_gate": "CANDIDATE_BOUND_TITLE_EXCLUSION_AUTHORITY",
    }
    _write_new(trial / "TRIAL-RESULT.json", _json_bytes(result))
    return result


def _finalize_trial_result(
    *,
    candidate_id: str,
    trial: Path,
    preimage: _TrialPreimage,
    rendered: _RenderedTrial,
    successor: dict[str, object],
) -> dict[str, object]:
    generation_path = trial / "identity-card.cover_generation.json"
    _write_new(generation_path, _json_bytes(successor))
    if preimage.before != _snapshot(preimage.inputs):
        raise HostOnlyIdentityCardTrialError(
            "source package inputs changed during trial construction"
        )
    rendered_text = successor.get("rendered_text_pixels")
    title_mask = rendered_text.get("mask_path") if isinstance(rendered_text, Mapping) else ""
    output_snapshot = _snapshot(
        {
            "base": rendered.base_path,
            "poster": rendered.poster_path,
            "cover": rendered.final_cover,
            "pre_overlay": Path(str(successor.get("pre_overlay_path") or "")),
            "title_mask": Path(str(title_mask or "")),
            "generation": generation_path,
        }
    )
    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS_PIXELS_READY_WITNESS_REQUIRED",
        "candidate_id": candidate_id,
        "source_package": str(preimage.source),
        "destination": str(trial),
        "source_inputs": preimage.before,
        "outputs": output_snapshot,
        "crop_evidence": rendered.crop_evidence,
        "source_composition_recovery": preimage.verification_recovery,
        "landmark_rebind": rendered.exclusion_evidence,
        "host_only_visual_safety_evidence": preimage.safety,
        "predecessor_blocker": preimage.blocker,
        "source_preimage_unchanged": True,
        "cover_pixels_changed": (_sha(rendered.final_cover) != _sha(preimage.cover)),
        "video_pixels_changed": False,
        "provider_calls": 0,
        "image_generation_calls": 0,
        "package_writes": 0,
        "production_state_writes": 0,
        "upload_calls": 0,
        "upload_allowed": False,
        "final_host_only_v4_witness": "REQUIRED_NOT_RUN",
    }
    _write_new(trial / "TRIAL-RESULT.json", _json_bytes(result))
    return result


def _write_trial_failure(
    *,
    path: Path,
    candidate_id: str,
    error: Exception,
) -> None:
    failure = {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "status": "FAILED",
        "candidate_id": candidate_id,
        "error_type": type(error).__name__,
        "error": str(error),
        "provider_calls": 0,
        "image_generation_calls": 0,
        "package_writes": 0,
        "production_state_writes": 0,
        "upload_calls": 0,
    }
    if not os.path.lexists(path):
        _write_new(path, _json_bytes(failure))


def build_host_only_identity_card_trial(
    *,
    source_package: Path,
    destination: Path,
    candidate_id: str,
    title_exclusion_authority: Path | None = None,
) -> dict[str, object]:
    """Create deterministic cover pixels and an unbound successor generation."""

    if not candidate_id.strip():
        raise HostOnlyIdentityCardTrialError("candidate_id is required")
    source = _safe_root(source_package, label="source package")
    trial = _prepare_destination(destination)
    failure_path = trial / "FAILURE.json"
    try:
        preimage = _prepare_trial_preimage(
            source=source,
            candidate_id=candidate_id,
        )
        frozen_title_review = None
        if title_exclusion_authority is not None:
            frozen_title_review, preimage = _prepare_current_title_exclusion(
                title_exclusion_authority, candidate_id=candidate_id, preimage=preimage
            )
        background = _render_trial_background(
            trial=trial,
            preimage=preimage,
        )
        try:
            rendered = _render_trial_title(
                preimage=preimage,
                background=background,
                title_exclusion_authority=frozen_title_review,
            )
        except HostOnlyTitleExclusionRequired:
            return _finalize_background_blocker(
                candidate_id=candidate_id,
                trial=trial,
                preimage=preimage,
                background=background,
            )
        successor = _build_successor_generation(
            candidate_id=candidate_id,
            preimage=preimage,
            rendered=rendered,
        )
        return _finalize_trial_result(
            candidate_id=candidate_id,
            trial=trial,
            preimage=preimage,
            rendered=rendered,
            successor=successor,
        )
    except Exception as exc:
        _write_trial_failure(
            path=failure_path,
            candidate_id=candidate_id,
            error=exc,
        )
        if isinstance(exc, HostOnlyIdentityCardTrialError):
            raise
        raise HostOnlyIdentityCardTrialError(str(exc)) from exc
