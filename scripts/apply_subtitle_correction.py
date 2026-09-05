#!/usr/bin/env python3
"""Apply a HUMAN text correction, rerun speaker finalization, then re-burn.

No full re-produce and no cover regeneration.  The ordering is mandatory:
human text is committed to the clean SRT first, then CAM++/context/turn
overrides regenerate the speaker SRT and colour ASS, and only that ASS is
burned.  Upload stays OFF.

Usage:
  apply_subtitle_correction.py --cid <cid> --date <YYYY-MM-DD> \
      --delivery '/…/lidousha/<date>/<name>.mp4' \
      --replace '旧文字=新文字' [--replace ... ]     # surgical text swaps
  # or replace whole cues by 1-based index:
      --set-line 3='李姐能帮忙跟李豆沙说句'
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.run_auto_review_shadow_pipeline import _burn_preview_subtitles  # noqa: E402
from scripts.run_auto_review_shadow_pipeline import _sha256  # noqa: E402
from scripts.produce_slice_package import run_speaker_finalizer  # noqa: E402
from scripts.apply_subtitle_text_overrides import (  # noqa: E402
    apply_document as apply_text_override_document,
)
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID  # noqa: E402
from scripts.suggest_upload_tags import generate_upload_tags  # noqa: E402
from src.autoslice.branding_intro import (  # noqa: E402
    BrandingIntroError,
    pin_existing_delivery_intro,
    require_branding_intro,
)
from src.autoslice.jingting_chunker import parse_srt_cues  # noqa: E402
from src.autoslice.repository_asset_authority import (  # noqa: E402
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)

BASE = Path("/opt/bilive/autoslice")


class DeliveryCopyError(ValueError):
    """A delivery target cannot be copied without unsafe aliasing."""


class DeliveryBrandingAuthorityError(ValueError):
    """The prior delivery cannot prove which branding intro must be reused."""


_RECOVERY_AUTHORITY_SCHEMAS = frozenset(
    {"delivery-branding-recovery-authority.v1", "delivery-branding-recovery-authority.v2"}
)
_RECOVERY_AUTHORITY_ROOT = Path("assets/lidousha/delivery_branding_recovery")
_RECOVERY_AUTHORITY_KEYS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "authority",
        "prior_burned_video_sha256",
        "prior_subtitle_sha256",
        "prior_record_sha256",
        "intro_id",
        "intro_media_sha256",
        "intro_offset_ms",
        "prior_package_authority_path",
        "prior_package_authority_sha256",
        "operator_freeze_authority_path",
        "operator_freeze_authority_sha256",
        "incident_correction_manifest_path",
        "incident_correction_manifest_sha256",
        "incident_before_srt_sha256",
        "incident_after_srt_sha256",
        "incident_new_burned_video_sha256",
    }
)
_RECOVERY_AUTHORITY_V2_KEYS = _RECOVERY_AUTHORITY_KEYS | frozenset(
    {
        "successor_correction_manifest_path",
        "successor_correction_manifest_sha256",
        "successor_correction_manifest_bytes",
        "successor_before_srt_sha256",
        "successor_after_srt_sha256",
        "successor_burned_video_sha256",
        "successor_record_path",
        "successor_record_sha256",
        "successor_record_bytes",
        "predecessor_recovery_authority_path",
        "predecessor_recovery_authority_sha256",
        "predecessor_recovery_authority_commit",
        "predecessor_operator_freeze_authority_path",
        "predecessor_operator_freeze_authority_sha256",
    }
)


def _regular_or_absent(path: Path, *, label: str) -> None:
    """Reject endpoint symlinks and special files without following them."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DeliveryCopyError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise DeliveryCopyError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise DeliveryCopyError(f"{label} must be a regular file: {path}")


def _same_regular_file(source: Path, destination: Path) -> bool:
    """Return whether two already-validated regular files share one identity."""

    _regular_or_absent(source, label="delivery source")
    _regular_or_absent(destination, label="delivery destination")
    try:
        source_stat = source.stat()
    except OSError as exc:
        raise DeliveryCopyError(f"cannot inspect delivery source: {source}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise DeliveryCopyError(f"delivery source must be a regular file: {source}")
    try:
        destination_stat = destination.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DeliveryCopyError(f"cannot inspect delivery destination: {destination}") from exc
    if not stat.S_ISREG(destination_stat.st_mode):
        raise DeliveryCopyError(f"delivery destination must be a regular file: {destination}")
    try:
        if source.resolve(strict=True) == destination.resolve(strict=True):
            return True
        return os.path.samefile(source, destination)
    except OSError as exc:
        raise DeliveryCopyError(
            f"cannot determine delivery file identity: {source} -> {destination}"
        ) from exc


def _preflight_delivery_targets(targets: list[Path]) -> None:
    """Fail before package mutation if any planned target is unsafe."""

    for target in targets:
        _regular_or_absent(target, label="delivery target")


def _preflight_existing_delivery_copies(pairs: list[tuple[Path, Path]]) -> None:
    """Validate any already-materialized source/target identity before mutation.

    Some outputs (the fresh burned video and correction manifest) do not exist
    at this point.  They still get the same strict check immediately before
    copying; this early pass catches the existing SRT/record/sidecar aliases
    before this script writes package state.
    """

    for source, destination in pairs:
        _regular_or_absent(source, label="planned delivery source")
        try:
            source.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeliveryCopyError(
                f"cannot inspect planned delivery source: {source}"
            ) from exc
        _same_regular_file(source, destination)


def _copy_delivery_file(source: Path, destination: Path) -> str:
    """Copy one artifact, preserving a same-file delivery as already current."""

    if _same_regular_file(source, destination):
        return "ALREADY_DELIVERED"
    try:
        shutil.copy2(source, destination)
    except OSError as exc:
        raise DeliveryCopyError(f"delivery copy failed: {source} -> {destination}") from exc
    return "COPIED"


def _preflight_correction_delivery(
    *,
    delivery: Path,
    srt_path: Path,
    record_path: Path,
    recut_dir: Path,
    candidate_id: str,
) -> tuple[Path, Path, Path, Path]:
    """Validate planned delivery aliases before the correction writes package state."""

    speaker_srt = srt_path.with_suffix(".speaker-final.srt")
    speaker_ass = srt_path.with_suffix(".speaker-final.ass")
    speaker_manifest_path = srt_path.with_suffix(".speaker-final.json")
    correction_manifest_path = recut_dir / f"{candidate_id}.human-text-correction.json"
    _preflight_delivery_targets(
        [
            delivery,
            delivery.with_suffix(".srt"),
            delivery.with_suffix(".human-text-correction.json"),
            delivery.with_suffix(".record.json"),
            # Uniform-host removes stale speaker copies, so those endpoints
            # must be proven safe even when it will not produce new ones.
            delivery.with_suffix(".speaker.srt"),
            delivery.with_suffix(".speaker.ass"),
            delivery.with_suffix(".speaker.json"),
        ]
    )
    _preflight_existing_delivery_copies(
        [
            (srt_path, delivery.with_suffix(".srt")),
            (record_path, delivery.with_suffix(".record.json")),
            (
                correction_manifest_path,
                delivery.with_suffix(".human-text-correction.json"),
            ),
            (speaker_srt, delivery.with_suffix(".speaker.srt")),
            (speaker_ass, delivery.with_suffix(".speaker.ass")),
            (speaker_manifest_path, delivery.with_suffix(".speaker.json")),
        ]
    )
    return speaker_srt, speaker_ass, speaker_manifest_path, correction_manifest_path


def _read_regular_json(path: Path, *, label: str) -> dict[str, object]:
    _regular_or_absent(path, label=label)
    if not path.is_file():
        raise DeliveryBrandingAuthorityError(f"{label} is missing: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeliveryBrandingAuthorityError(f"{label} is unreadable: {path}") from exc
    if not isinstance(document, dict):
        raise DeliveryBrandingAuthorityError(f"{label} is not an object: {path}")
    return document


def _required_prefixed_sha256(
    value: object, *, label: str
) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise DeliveryBrandingAuthorityError(f"{label} must be a sha256: prefixed digest")
    return value


def _expected_recovery_authority_path(candidate_id: str) -> Path:
    return ROOT / _RECOVERY_AUTHORITY_ROOT / f"{candidate_id}.v1.json"


def _require_canonical_recovery_authority(
    authority_path: Path, *, candidate_id: str
) -> tuple[dict[str, object], Mapping[str, object]]:
    """Read only a deployed-sealed, candidate-specific recovery decision.

    A CLI pathname is a locator, never an authority grant.  This prevents an
    arbitrary JSON outside the managed asset set from selecting a historic
    intro after an incident has overwritten the mutable package record.
    """

    expected = _expected_recovery_authority_path(candidate_id)
    try:
        if authority_path.resolve(strict=True) != expected.resolve(strict=True):
            raise DeliveryBrandingAuthorityError(
                "recovery branding authority must be the canonical candidate asset"
            )
        payload = authority_path.read_bytes()
        seal = require_repository_asset_authority(
            repo_root=ROOT,
            relative_path=authority_path.resolve(strict=True).relative_to(ROOT),
            observed_bytes=payload,
        )
    except (OSError, ValueError, RepositoryAssetAuthorityError) as exc:
        if isinstance(exc, DeliveryBrandingAuthorityError):
            raise
        raise DeliveryBrandingAuthorityError(
            "recovery branding authority is not repository/deployment bound"
        ) from exc
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DeliveryBrandingAuthorityError(
            "recovery branding authority is unreadable"
        ) from exc
    if not isinstance(document, dict):
        raise DeliveryBrandingAuthorityError("recovery branding authority is not an object")
    return document, {
        "mode": seal.mode,
        "deployed_commit": seal.commit,
        "relative_path": seal.relative_path,
        "sha256": seal.file_sha256,
    }


def _validate_recovery_authority_shape(
    authority: Mapping[str, object], *, candidate_id: str
) -> dict[str, object]:
    """Validate the static recovery decision before reading any runtime proof."""

    schema = authority.get("schema_version")
    expected_keys = (
        _RECOVERY_AUTHORITY_V2_KEYS
        if schema == "delivery-branding-recovery-authority.v2"
        else _RECOVERY_AUTHORITY_KEYS
    )
    if set(authority) != expected_keys:
        raise DeliveryBrandingAuthorityError("recovery branding authority has an unexpected key set")
    if schema not in _RECOVERY_AUTHORITY_SCHEMAS:
        raise DeliveryBrandingAuthorityError("unsupported recovery branding authority schema")
    if authority.get("candidate_id") != candidate_id:
        raise DeliveryBrandingAuthorityError(
            "recovery branding authority candidate_id does not match --cid"
        )
    if not isinstance(authority.get("authority"), str) or not authority["authority"].strip():
        raise DeliveryBrandingAuthorityError("recovery branding authority lacks operator decision text")
    for key in (
        "prior_burned_video_sha256", "prior_subtitle_sha256", "prior_record_sha256",
        "intro_media_sha256", "prior_package_authority_sha256",
        "operator_freeze_authority_sha256", "incident_correction_manifest_sha256",
        "incident_before_srt_sha256", "incident_after_srt_sha256",
        "incident_new_burned_video_sha256",
    ):
        _required_prefixed_sha256(authority.get(key), label=f"recovery {key}")
    if schema == "delivery-branding-recovery-authority.v2":
        for key in (
            "successor_correction_manifest_sha256",
            "successor_before_srt_sha256",
            "successor_after_srt_sha256",
            "successor_burned_video_sha256",
            "successor_record_sha256",
            "predecessor_recovery_authority_sha256",
            "predecessor_operator_freeze_authority_sha256",
        ):
            _required_prefixed_sha256(authority.get(key), label=f"recovery {key}")
        for key in ("successor_correction_manifest_bytes", "successor_record_bytes"):
            if isinstance(authority.get(key), bool) or not isinstance(authority.get(key), int) or authority[key] <= 0:
                raise DeliveryBrandingAuthorityError(f"recovery {key} is invalid")
        if not re.fullmatch(
            r"[0-9a-f]{40}", str(authority.get("predecessor_recovery_authority_commit") or "")
        ):
            raise DeliveryBrandingAuthorityError("recovery predecessor authority commit is invalid")
        if authority["predecessor_operator_freeze_authority_sha256"] == authority[
            "operator_freeze_authority_sha256"
        ]:
            raise DeliveryBrandingAuthorityError("recovery predecessor/current freeze authorities must differ")
    if not isinstance(authority.get("intro_id"), str) or not authority["intro_id"]:
        raise DeliveryBrandingAuthorityError("recovery intro_id is invalid")
    offset = authority.get("intro_offset_ms")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset <= 0:
        raise DeliveryBrandingAuthorityError("recovery intro_offset_ms must be positive")
    for key in (
        "prior_package_authority_path", "operator_freeze_authority_path",
        "incident_correction_manifest_path",
    ):
        value = authority.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise DeliveryBrandingAuthorityError(f"recovery {key} must be an absolute path")
    if schema == "delivery-branding-recovery-authority.v2":
        for key in (
            "successor_correction_manifest_path",
            "successor_record_path",
            "predecessor_recovery_authority_path",
            "predecessor_operator_freeze_authority_path",
        ):
            value = authority.get(key)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise DeliveryBrandingAuthorityError(f"recovery {key} must be an absolute path")
    return dict(authority)


def _extract_prior_review_manifest_hashes(
    document: Mapping[str, object], *, candidate_id: str
) -> tuple[str, str, str]:
    """Read exactly one candidate entry from a daily/recovery review manifest."""

    if document.get("schema_version") not in {
        "lidousha-daily-review-manifest.v1",
        "lidousha-recovery-review-manifest.v1",
    } or not isinstance(document.get("items"), list):
        raise DeliveryBrandingAuthorityError("prior package authority is not a supported review manifest")
    matches = [
        item for item in document["items"]
        if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise DeliveryBrandingAuthorityError("prior review manifest has no unique candidate entry")
    item = matches[0]
    hashes = item.get("sha256")
    if not isinstance(hashes, Mapping):
        raise DeliveryBrandingAuthorityError("prior review manifest candidate lacks sha256 map")
    for path_key, hash_key in (("video", "video"), ("subtitle_srt", "subtitle_srt"), ("record", "evidence_json")):
        if not isinstance(item.get(path_key), str) or not item[path_key]:
            raise DeliveryBrandingAuthorityError(
                f"prior review manifest candidate lacks {path_key} path"
            )
        raw = hashes.get(hash_key)
        if not isinstance(raw, str) or re.fullmatch(r"[0-9a-f]{64}", raw) is None:
            raise DeliveryBrandingAuthorityError(
                f"prior review manifest candidate lacks {hash_key} hash"
            )
    return (
        "sha256:" + str(hashes["video"]),
        "sha256:" + str(hashes["subtitle_srt"]),
        "sha256:" + str(hashes["evidence_json"]),
    )


def _validate_freeze_contract(
    document: Mapping[str, object], *, candidate_id: str
) -> None:
    """Require the exact final-review contract and its three Qixi invariants."""

    if document.get("schema_version") != "lidousha-final-media-review-contracts.v1":
        raise DeliveryBrandingAuthorityError("operator freeze authority schema drifted")
    contracts = document.get("contracts")
    if not isinstance(contracts, list):
        raise DeliveryBrandingAuthorityError("operator freeze authority lacks contracts")
    matches = [
        contract for contract in contracts
        if isinstance(contract, Mapping) and contract.get("candidate_id") == candidate_id
    ]
    if len(matches) != 1:
        raise DeliveryBrandingAuthorityError("operator freeze authority has no unique candidate contract")
    points = matches[0].get("subtitle_review_points")
    if not isinstance(points, list):
        raise DeliveryBrandingAuthorityError("operator freeze authority lacks review points")
    expected = {
        "qixi-tomorrow-night-lara-title",
        "qixi-balance-iiya",
        "qixi-sweet-or-bitter-ending",
        "qixi-full-release-text-stability",
    }
    actual = {
        point.get("point_id") for point in points
        if isinstance(point, Mapping) and isinstance(point.get("point_id"), str)
    }
    if actual != expected:
        raise DeliveryBrandingAuthorityError("operator freeze contract invariants drifted")


def _validate_v2_successor_chain(
    *,
    authority: Mapping[str, object],
    working_record_path: Path,
    candidate_id: str,
    incident_path: Path,
    incident_sha: str,
    after_srt: str,
    prior_binding: Mapping[str, object],
) -> tuple[Path, str]:
    """Bind the existing Z2 recovery receipt before it can seed another reburn."""

    successor_path = Path(str(authority["successor_correction_manifest_path"]))
    if successor_path != incident_path:
        raise DeliveryBrandingAuthorityError(
            "recovery v2 incident and successor paths must name the same overwritten receipt"
        )
    successor_sha = _required_prefixed_sha256(
        authority.get("successor_correction_manifest_sha256"),
        label="recovery successor_correction_manifest_sha256",
    )
    successor = _read_regular_json(successor_path, label="recovery successor correction manifest")
    if (
        "sha256:" + _sha256(successor_path) != successor_sha
        or successor_path.stat().st_size != authority["successor_correction_manifest_bytes"]
    ):
        raise DeliveryBrandingAuthorityError("recovery successor correction manifest hash drifted")
    successor_before = _required_prefixed_sha256(
        authority.get("successor_before_srt_sha256"), label="recovery successor_before_srt_sha256"
    )
    successor_after = _required_prefixed_sha256(
        authority.get("successor_after_srt_sha256"), label="recovery successor_after_srt_sha256"
    )
    successor_burned = _required_prefixed_sha256(
        authority.get("successor_burned_video_sha256"), label="recovery successor_burned_video_sha256"
    )
    if successor_before != after_srt or (
        successor.get("candidate_id") != candidate_id
        or "sha256:" + str(successor.get("before_srt_sha256") or "") != successor_before
        or "sha256:" + str(successor.get("after_srt_sha256") or "") != successor_after
        or "sha256:" + str(successor.get("burned_media_sha256") or "") != successor_burned
    ):
        raise DeliveryBrandingAuthorityError("recovery successor correction chain drifts")
    recovery = successor.get("delivery_branding_authority")
    predecessor_path = str(authority["predecessor_recovery_authority_path"])
    predecessor_sha = _required_prefixed_sha256(
        authority.get("predecessor_recovery_authority_sha256"),
        label="recovery predecessor_recovery_authority_sha256",
    )
    predecessor_commit = str(authority["predecessor_recovery_authority_commit"])
    expected_seal = {
        "mode": "DEPLOYED_MANIFEST",
        "deployed_commit": predecessor_commit,
        "relative_path": _expected_recovery_authority_path(candidate_id).relative_to(ROOT).as_posix(),
        "sha256": predecessor_sha,
    }
    expected_recovery_keys = {
        "schema_version", "authority_path", "authority_sha256", "authority_repository_seal",
        "prior_burned_video_sha256", "prior_subtitle_sha256", "prior_record_sha256",
        "prior_package_authority_path", "prior_package_authority_sha256",
        "operator_freeze_authority_path", "operator_freeze_authority_sha256",
        "incident_correction_manifest_path", "incident_correction_manifest_sha256", "branding_intro",
    }
    if (
        not isinstance(recovery, Mapping)
        or set(recovery) != expected_recovery_keys
        or recovery.get("schema_version") != "delivery-branding-recovery-authority.v1"
        or recovery.get("authority_path") != predecessor_path
        or recovery.get("authority_sha256") != predecessor_sha
        or not isinstance(recovery.get("authority_repository_seal"), Mapping)
        or set(recovery["authority_repository_seal"])
        != {"mode", "deployed_commit", "relative_path", "sha256"}
        or any(recovery["authority_repository_seal"].get(key) != value for key, value in expected_seal.items())
        or recovery.get("incident_correction_manifest_path") != str(incident_path)
        or recovery.get("incident_correction_manifest_sha256") != incident_sha
        or recovery.get("prior_burned_video_sha256") != authority["prior_burned_video_sha256"]
        or recovery.get("prior_subtitle_sha256") != authority["prior_subtitle_sha256"]
        or recovery.get("prior_record_sha256") != authority["prior_record_sha256"]
        or recovery.get("prior_package_authority_path") != authority["prior_package_authority_path"]
        or recovery.get("prior_package_authority_sha256") != authority["prior_package_authority_sha256"]
        or recovery.get("operator_freeze_authority_path")
        != authority["predecessor_operator_freeze_authority_path"]
        or recovery.get("operator_freeze_authority_sha256")
        != authority["predecessor_operator_freeze_authority_sha256"]
        or recovery.get("branding_intro") != dict(prior_binding)
    ):
        raise DeliveryBrandingAuthorityError("recovery successor does not bind deployed Z2 authority")
    record_path = Path(str(authority["successor_record_path"]))
    record_sha = _required_prefixed_sha256(
        authority.get("successor_record_sha256"), label="recovery successor_record_sha256"
    )
    current = _read_regular_json(record_path, label="recovery successor record")
    if (
        record_path != working_record_path
        or "sha256:" + _sha256(record_path) != record_sha
        or record_path.stat().st_size != authority["successor_record_bytes"]
        or current.get("human_text_correction_manifest_path") != str(successor_path)
        or current.get("human_text_correction_manifest_sha256") != successor_sha
    ):
        raise DeliveryBrandingAuthorityError("current record does not bind recovery successor")
    hashes = current.get("artifact_hashes")
    if not isinstance(hashes, Mapping) or (
        _required_prefixed_sha256(hashes.get("subtitle_sha256"), label="successor record subtitle")
        != successor_after
        or _required_prefixed_sha256(hashes.get("burned_video_sha256"), label="successor record burn")
        != successor_burned
    ):
        raise DeliveryBrandingAuthorityError("current record successor hashes drift")
    return successor_path, successor_sha


def _load_recovery_branding_authority(
    *,
    authority_path: Path,
    working_record_path: Path,
    working_record: Mapping[str, object],
    candidate_id: str,
    branding_intro: Mapping[str, object] | None,
) -> tuple[Mapping[str, object], dict[str, object]]:
    """Validate the one-off recovery bridge for a partially overwritten package.

    This is intentionally not a generic operator pick.  It is usable only
    after a documented correction chain has overwritten the normal record, and
    binds both the immutable pre-incident package authority and the current
    operator freeze before selecting a currently hash-verified intro member.
    """

    raw_authority, repository_seal = _require_canonical_recovery_authority(
        authority_path, candidate_id=candidate_id
    )
    authority = _validate_recovery_authority_shape(raw_authority, candidate_id=candidate_id)
    prior_burned = _required_prefixed_sha256(
        authority.get("prior_burned_video_sha256"), label="recovery prior_burned_video_sha256"
    )
    prior_subtitle = _required_prefixed_sha256(
        authority.get("prior_subtitle_sha256"), label="recovery prior_subtitle_sha256"
    )
    prior_record = _required_prefixed_sha256(
        authority.get("prior_record_sha256"), label="recovery prior_record_sha256"
    )
    before_srt = _required_prefixed_sha256(
        authority.get("incident_before_srt_sha256"), label="recovery incident_before_srt_sha256"
    )
    after_srt = _required_prefixed_sha256(
        authority.get("incident_after_srt_sha256"), label="recovery incident_after_srt_sha256"
    )
    new_burned = _required_prefixed_sha256(
        authority.get("incident_new_burned_video_sha256"), label="recovery incident_new_burned_video_sha256"
    )
    if prior_subtitle != before_srt:
        raise DeliveryBrandingAuthorityError(
            "recovery prior subtitle hash does not equal incident correction before hash"
        )

    prior_package_path = Path(str(authority["prior_package_authority_path"]))
    prior_package_sha = _required_prefixed_sha256(
        authority.get("prior_package_authority_sha256"),
        label="recovery prior_package_authority_sha256",
    )
    prior_package = _read_regular_json(
        prior_package_path, label="recovery prior package authority"
    )
    if "sha256:" + _sha256(prior_package_path) != prior_package_sha:
        raise DeliveryBrandingAuthorityError(
            "recovery prior package authority hash drifted"
        )
    if _extract_prior_review_manifest_hashes(
        prior_package, candidate_id=candidate_id
    ) != (prior_burned, prior_subtitle, prior_record):
        raise DeliveryBrandingAuthorityError(
            "recovery prior package authority does not bind this candidate's package hashes"
        )

    freeze_path = Path(str(authority["operator_freeze_authority_path"]))
    freeze_sha = _required_prefixed_sha256(
        authority.get("operator_freeze_authority_sha256"),
        label="recovery operator_freeze_authority_sha256",
    )
    freeze = _read_regular_json(freeze_path, label="recovery operator freeze authority")
    if "sha256:" + _sha256(freeze_path) != freeze_sha:
        raise DeliveryBrandingAuthorityError("recovery operator freeze authority hash drifted")
    _validate_freeze_contract(freeze, candidate_id=candidate_id)

    incident_path = Path(str(authority["incident_correction_manifest_path"]))
    incident_sha = _required_prefixed_sha256(
        authority.get("incident_correction_manifest_sha256"),
        label="recovery incident_correction_manifest_sha256",
    )
    prior_binding = {
        "status": "PREPENDED",
        "intro_id": authority["intro_id"],
        "intro_media_sha256": authority["intro_media_sha256"],
        "intro_offset_ms": authority["intro_offset_ms"],
    }
    successor_path: Path | None = None
    successor_sha: str | None = None
    if authority["schema_version"] == "delivery-branding-recovery-authority.v2":
        successor_path, successor_sha = _validate_v2_successor_chain(
            authority=authority,
            working_record_path=working_record_path,
            candidate_id=candidate_id,
            incident_path=incident_path,
            incident_sha=incident_sha,
            after_srt=after_srt,
            prior_binding=prior_binding,
        )
    else:
        incident = _read_regular_json(incident_path, label="recovery incident correction manifest")
        if "sha256:" + _sha256(incident_path) != incident_sha:
            raise DeliveryBrandingAuthorityError("recovery incident correction manifest hash drifted")
        if incident.get("candidate_id") != candidate_id:
            raise DeliveryBrandingAuthorityError("incident correction manifest candidate_id drifted")
        if (
            "sha256:" + str(incident.get("before_srt_sha256") or "") != before_srt
            or "sha256:" + str(incident.get("after_srt_sha256") or "") != after_srt
            or "sha256:" + str(incident.get("burned_media_sha256") or "") != new_burned
        ):
            raise DeliveryBrandingAuthorityError(
                "incident correction manifest does not match the recovery chain"
            )
        current_hashes = working_record.get("artifact_hashes")
        if not isinstance(current_hashes, Mapping):
            raise DeliveryBrandingAuthorityError("current correction record lacks artifact_hashes")
        if (
            _required_prefixed_sha256(
                current_hashes.get("subtitle_sha256"), label="current record subtitle_sha256"
            )
            != after_srt
            or _required_prefixed_sha256(
                current_hashes.get("burned_video_sha256"), label="current record burned_video_sha256"
            )
            != new_burned
        ):
            raise DeliveryBrandingAuthorityError(
                "current correction record is not the documented incident successor"
            )
        if (
            working_record.get("human_text_correction_manifest_path") != str(incident_path)
            or working_record.get("human_text_correction_manifest_sha256") != incident_sha
        ):
            raise DeliveryBrandingAuthorityError(
                "current record does not point to the documented incident correction manifest"
            )
    if _same_regular_file(working_record_path, authority_path):
        raise DeliveryBrandingAuthorityError("current record may not be the recovery authority")
    try:
        pinned_context = pin_existing_delivery_intro(branding_intro, prior_binding)
    except BrandingIntroError as exc:
        raise DeliveryBrandingAuthorityError(str(exc)) from exc
    return pinned_context, {
        "schema_version": authority["schema_version"],
        "authority_path": str(authority_path),
        "authority_sha256": str(repository_seal["sha256"]),
        "authority_repository_seal": repository_seal,
        "prior_burned_video_sha256": prior_burned,
        "prior_subtitle_sha256": prior_subtitle,
        "prior_record_sha256": prior_record,
        "prior_package_authority_path": str(prior_package_path),
        "prior_package_authority_sha256": prior_package_sha,
        "operator_freeze_authority_path": str(freeze_path),
        "operator_freeze_authority_sha256": freeze_sha,
        "incident_correction_manifest_path": str(incident_path),
        "incident_correction_manifest_sha256": incident_sha,
        **(
            {
                "successor_correction_manifest_path": str(successor_path),
                "successor_correction_manifest_sha256": successor_sha,
            }
            if successor_path is not None and successor_sha is not None
            else {}
        ),
        "branding_intro": prior_binding,
    }


def _existing_delivery_branding_context(
    *,
    delivery: Path,
    working_record_path: Path,
    candidate_id: str,
    authority_record_path: Path | None,
    authority_publish_path: Path | None,
    recovery_authority_path: Path | None,
    branding_intro: Mapping[str, object] | None,
) -> tuple[Mapping[str, object] | None, dict[str, object] | None]:
    """Return a normal or prior-delivery-pinned intro context before mutation.

    The mutable correction ``record_path`` is intentionally never accepted as
    authority: a prior failed correction may already have rewritten it.  An
    existing delivery therefore needs an independently preserved package record
    and publish draft whose hashes cross-bind the old burned bytes.
    """

    supplied = (authority_record_path is not None, authority_publish_path is not None)
    if supplied[0] != supplied[1]:
        raise DeliveryBrandingAuthorityError(
            "--delivery-authority-record and --delivery-authority-publish must be supplied together"
        )
    if recovery_authority_path is not None and any(supplied):
        raise DeliveryBrandingAuthorityError(
            "recovery branding authority cannot be combined with normal delivery authority inputs"
        )
    if not delivery.exists():
        if any(supplied) or recovery_authority_path is not None:
            raise DeliveryBrandingAuthorityError(
                "delivery branding authority was supplied but the delivery video does not yet exist"
            )
        return branding_intro, None
    if recovery_authority_path is not None:
        working_record = _read_regular_json(
            working_record_path, label="current correction record"
        )
        return _load_recovery_branding_authority(
            authority_path=recovery_authority_path,
            working_record_path=working_record_path,
            working_record=working_record,
            candidate_id=candidate_id,
            branding_intro=branding_intro,
        )
    if not all(supplied):
        raise DeliveryBrandingAuthorityError(
            "existing delivery requires independent --delivery-authority-record and "
            "--delivery-authority-publish"
        )
    assert authority_record_path is not None and authority_publish_path is not None
    if _same_regular_file(authority_record_path, working_record_path):
        raise DeliveryBrandingAuthorityError(
            "delivery authority record must be independent of this correction's mutable record_path"
        )
    authority_record = _read_regular_json(
        authority_record_path, label="delivery authority record"
    )
    authority_publish = _read_regular_json(
        authority_publish_path, label="delivery authority publish draft"
    )
    publish_candidate = authority_publish.get("candidate_id")
    if publish_candidate != candidate_id:
        raise DeliveryBrandingAuthorityError(
            "delivery authority publish candidate_id does not match --cid"
        )
    record_candidate = authority_record.get("candidate_id")
    if record_candidate not in (None, candidate_id):
        raise DeliveryBrandingAuthorityError(
            "delivery authority record candidate_id does not match --cid"
        )
    record_hashes = authority_record.get("artifact_hashes")
    publish_hashes = authority_publish.get("artifact_hashes")
    if not isinstance(record_hashes, Mapping) or not isinstance(publish_hashes, Mapping):
        raise DeliveryBrandingAuthorityError(
            "delivery authority record/publish draft lacks artifact_hashes"
        )
    published_record_hash = _required_prefixed_sha256(
        record_hashes.get("publish_draft_sha256"),
        label="delivery authority record publish_draft_sha256",
    )
    actual_publish_hash = "sha256:" + _sha256(authority_publish_path)
    if published_record_hash != actual_publish_hash:
        raise DeliveryBrandingAuthorityError(
            "delivery authority record does not hash-bind the supplied publish draft"
        )
    record_burned_sha = _required_prefixed_sha256(
        record_hashes.get("burned_video_sha256"),
        label="delivery authority record burned_video_sha256",
    )
    publish_burned_sha = _required_prefixed_sha256(
        publish_hashes.get("burned_video_sha256"),
        label="delivery authority publish burned_video_sha256",
    )
    burned_preview = authority_record.get("burned_preview")
    if not isinstance(burned_preview, Mapping):
        raise DeliveryBrandingAuthorityError(
            "delivery authority record has no burned_preview"
        )
    preview_burned_sha = _required_prefixed_sha256(
        burned_preview.get("burned_sha256"),
        label="delivery authority record burned_preview.burned_sha256",
    )
    if len({record_burned_sha, publish_burned_sha, preview_burned_sha}) != 1:
        raise DeliveryBrandingAuthorityError(
            "delivery authority record/publish burned hashes do not agree"
        )
    prior_binding = burned_preview.get("branding_intro")
    if not isinstance(prior_binding, Mapping):
        raise DeliveryBrandingAuthorityError(
            "delivery authority record has no branding intro binding"
        )
    try:
        pinned_context = pin_existing_delivery_intro(branding_intro, prior_binding)
    except BrandingIntroError as exc:
        raise DeliveryBrandingAuthorityError(str(exc)) from exc
    return pinned_context, {
        "schema_version": "delivery-branding-authority.v1",
        "record_path": str(authority_record_path),
        "record_sha256": "sha256:" + _sha256(authority_record_path),
        "publish_path": str(authority_publish_path),
        "publish_sha256": actual_publish_hash,
        "burned_video_sha256": record_burned_sha,
        "branding_intro": {
            "intro_id": prior_binding["intro_id"],
            "intro_media_sha256": prior_binding["intro_media_sha256"],
            "intro_offset_ms": prior_binding["intro_offset_ms"],
        },
    }


def _verify_reburn_kept_authorized_intro(
    burned_preview: Mapping[str, object],
    authority: Mapping[str, object] | None,
) -> None:
    """Refuse a render whose result contradicts the pre-mutation authority."""

    if authority is None:
        return
    expected = authority.get("branding_intro")
    actual = burned_preview.get("branding_intro")
    if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
        raise DeliveryBrandingAuthorityError(
            "reburn has no branding intro binding for an existing delivery"
        )
    for field in ("intro_id", "intro_media_sha256", "intro_offset_ms"):
        actual_value = actual.get(field)
        expected_value = expected.get(field)
        if field == "intro_media_sha256" and isinstance(actual_value, str) and isinstance(expected_value, str):
            actual_value = actual_value.removeprefix("sha256:")
            expected_value = expected_value.removeprefix("sha256:")
        if actual_value != expected_value:
            raise DeliveryBrandingAuthorityError(
                f"reburn branding {field} drifted from delivery authority"
            )


def _prepare_correction_branding(
    *,
    delivery: Path,
    working_record_path: Path,
    candidate_id: str,
    authority_record_path: Path | None,
    authority_publish_path: Path | None,
    recovery_authority_path: Path | None,
) -> tuple[Mapping[str, object] | None, dict[str, object] | None]:
    """Resolve and pin delivery branding before any correction output is written."""

    resolved_branding_intro = require_branding_intro(ROOT)
    return _existing_delivery_branding_context(
        delivery=delivery,
        working_record_path=working_record_path,
        candidate_id=candidate_id,
        authority_record_path=authority_record_path,
        authority_publish_path=authority_publish_path,
        recovery_authority_path=recovery_authority_path,
        branding_intro=resolved_branding_intro,
    )


def _write_correction_manifest(
    *,
    args: argparse.Namespace,
    before_hash: str,
    srt_path: Path,
    speaker_mode: str,
    speaker_manifest: object,
    speaker_manifest_path: Path,
    speaker_manifest_hash_path: Path | None,
    burned: Path,
    burned_receipt_path: Path,
    delivery_branding_authority: Mapping[str, object] | None,
    correction_manifest_path: Path,
    text_override_manifest_path: Path | None,
    text_override_output_path: Path | None,
    text_override_decision_output_path: Path | None,
) -> None:
    """Persist the correction receipt only after its re-burn has been verified."""

    correction_manifest = {
        "schema_version": "human-subtitle-correction.v2",
        "stage_order": "human_text_then_speaker_then_burn",
        "corrected_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": args.cid,
        "before_srt_sha256": before_hash,
        "after_srt_sha256": _sha256(srt_path),
        "replace_operations": args.replace,
        "set_line_operations": args.set_line,
        "refresh_only": args.refresh_only,
        "text_source": str(args.text_source) if args.text_source else None,
        "text_source_sha256": _sha256(args.text_source) if args.text_source else None,
        "text_override": str(args.text_override) if args.text_override else None,
        "text_override_sha256": _sha256(args.text_override) if args.text_override else None,
        "text_override_manifest": (
            str(text_override_manifest_path) if text_override_manifest_path else None
        ),
        "text_override_manifest_sha256": (
            _sha256(text_override_manifest_path) if text_override_manifest_path else None
        ),
        "timing_source": str(args.timing_source) if args.timing_source else None,
        "timing_source_sha256": _sha256(args.timing_source) if args.timing_source else None,
        "text_override_decision_output": (
            str(text_override_decision_output_path)
            if text_override_decision_output_path
            else None
        ),
        "text_override_decision_output_sha256": (
            _sha256(text_override_decision_output_path)
            if text_override_decision_output_path
            else None
        ),
        "text_override_output": (
            str(text_override_output_path) if text_override_output_path else None
        ),
        "text_override_output_sha256": (
            _sha256(text_override_output_path) if text_override_output_path else None
        ),
        "speaker_mode": speaker_mode,
        "speaker_manifest": str(speaker_manifest_path) if speaker_manifest is not None else None,
        "speaker_manifest_sha256": (
            _sha256(speaker_manifest_hash_path) if speaker_manifest_hash_path is not None else None
        ),
        "burned_media": str(burned_receipt_path),
        "burned_media_sha256": _sha256(burned),
        "delivery_branding_authority": delivery_branding_authority,
        "upload_enabled": False,
    }
    correction_manifest_path.write_text(
        json.dumps(correction_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _stage_media_input(source: Path, staging_dir: Path) -> Path:
    """Provide a private render input without ever naming a live burn target."""

    _regular_or_absent(source, label="correction source media")
    if not source.is_file():
        raise DeliveryCopyError(f"correction source media is missing: {source}")
    staged = staging_dir / source.name
    try:
        os.link(source, staged)
    except OSError:
        try:
            shutil.copy2(source, staged)
        except OSError as exc:
            raise DeliveryCopyError("cannot stage correction source media") from exc
    return staged


def _render_correction_in_staging(
    *,
    record: Mapping[str, object],
    srt: str,
    recut_dir: Path,
    candidate_id: str,
    speaker_mode: str,
    speaker_overrides: Path | None,
    speaker_python: Path,
    branding_intro: Mapping[str, object] | None,
) -> tuple[Path, Path, Path | None, Path | None, Path | None, object, Mapping[str, object]]:
    """Render every mutable correction artifact in a private sibling directory."""

    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=".subtitle-correction-stage-", dir=recut_dir))
        media = _stage_media_input(Path(str(record["media_path"])), staging_dir)
        staged_srt = staging_dir / Path(str(record["subtitle_path"])).name
        staged_srt.write_text(srt, encoding="utf-8")
        if speaker_mode == "uniform_host":
            reburn = _burn_preview_subtitles(
                {"status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(staged_srt)},
                run_ffmpeg=True,
                branding_intro=branding_intro,
            )
            return staging_dir, staged_srt, None, None, None, None, reburn or {}
        speaker_srt = staged_srt.with_suffix(".speaker-final.srt")
        speaker_ass = staged_srt.with_suffix(".speaker-final.ass")
        speaker_manifest_path = staged_srt.with_suffix(".speaker-final.json")
        speaker_manifest = run_speaker_finalizer(
            host="localhost",
            candidate_id=candidate_id,
            media_path=media,
            text_srt_path=staged_srt,
            output_srt_path=speaker_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=staging_dir / f"{candidate_id}.speaker-work",
            override_path=speaker_overrides,
            speaker_python=speaker_python,
        )
        reburn = _burn_preview_subtitles(
            {
                "status": "MATERIALIZED", "media_path": str(media), "subtitle_path": str(staged_srt),
                "subtitle_ass_path": str(speaker_ass), "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
                "artifact_hashes": {"ass_sha256": "sha256:" + _sha256(speaker_ass)},
            },
            run_ffmpeg=True,
            branding_intro=branding_intro,
        )
        return (
            staging_dir, staged_srt, speaker_srt, speaker_ass, speaker_manifest_path,
            speaker_manifest, reburn or {},
        )
    except Exception:
        if "staging_dir" in locals():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _commit_staged_files(
    replacements: list[tuple[Path, Path]], removals: list[Path]
) -> None:
    """Atomically replace each prepared file, rolling all targets back on error."""

    planned: dict[Path, Path | None] = {}
    for source, target in replacements:
        _regular_or_absent(source, label="staged correction source")
        _regular_or_absent(target, label="correction commit target")
        previous = planned.get(target)
        if previous is not None and _sha256(previous) != _sha256(source):
            raise DeliveryCopyError(f"conflicting correction replacements for {target}")
        planned[target] = source
    for target in removals:
        _regular_or_absent(target, label="correction removal target")
        if target in planned:
            raise DeliveryCopyError(f"correction target is both replaced and removed: {target}")
        planned[target] = None
    prepared: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    commit_succeeded = False
    try:
        for target, source in planned.items():
            if source is None:
                continue
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.subtitle-correction-", dir=target.parent)
            os.close(fd)
            temporary = Path(temp_name)
            shutil.copy2(source, temporary)
            prepared[target] = temporary
        for target, source in planned.items():
            if target.exists():
                backup = target.with_name(f".{target.name}.subtitle-correction-backup-{uuid.uuid4().hex}")
                os.replace(target, backup)
                backups[target] = backup
            if source is not None:
                os.replace(prepared[target], target)
                installed.append(target)
        commit_succeeded = True
    except OSError as exc:
        rollback_error: OSError | None = None
        for target in reversed(list(planned)):
            try:
                if target in backups:
                    if target.exists():
                        target.unlink()
                    os.replace(backups[target], target)
                elif target in installed and target.exists():
                    target.unlink()
            except OSError as restore_exc:
                rollback_error = restore_exc
        if rollback_error is not None:
            preserved = ", ".join(str(path) for path in backups.values() if path.exists())
            raise DeliveryCopyError(
                "correction commit failed and rollback failed; preserved backups: " + preserved
            ) from rollback_error
        raise DeliveryCopyError("correction commit failed; package restored") from exc
    finally:
        for temporary in prepared.values():
            temporary.unlink(missing_ok=True)
        if commit_succeeded:
            for backup in backups.values():
                backup.unlink(missing_ok=True)


def _srt_blocks(text: str):
    return [b for b in text.replace("\r\n", "\n").strip().split("\n\n") if b.strip()]


def _srt_time(value_ms: int) -> str:
    hours, rem = divmod(int(value_ms), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _project_reviewed_text_onto_timing(
    *,
    text_source: Path,
    decision_output: Path,
    timing_source: Path,
    text_override: Path,
) -> str:
    source_cues = parse_srt_cues(text_source.read_text(encoding="utf-8"))
    decision_cues = parse_srt_cues(decision_output.read_text(encoding="utf-8"))
    timing_cues = parse_srt_cues(timing_source.read_text(encoding="utf-8"))
    if len(source_cues) != len(timing_cues):
        raise ValueError(
            "text source and authoritative timing source have different cue counts"
        )
    document = json.loads(text_override.read_text(encoding="utf-8"))
    dropped = {
        int(row["source_cue"])
        for row in document.get("overrides", [])
        if isinstance(row, dict) and row.get("action") == "drop"
    }
    expected_output_count = len(source_cues) - len(dropped)
    if len(decision_cues) != expected_output_count:
        raise ValueError(
            "text decision output does not preserve source cue lineage"
        )
    projected: list[str] = []
    decision_offset = 0
    for source_index, timing_cue in enumerate(timing_cues, start=1):
        if source_index in dropped:
            continue
        decision_cue = decision_cues[decision_offset]
        decision_offset += 1
        projected.append(
            f"{len(projected) + 1}\n"
            f"{_srt_time(timing_cue.start_ms)} --> {_srt_time(timing_cue.end_ms)}\n"
            f"{decision_cue.text}\n"
        )
    return "\n".join(projected)


def _project_existing_text_onto_timing(
    *,
    text_srt: str,
    timing_srt: str,
) -> str:
    text_cues = parse_srt_cues(text_srt)
    timing_cues = parse_srt_cues(timing_srt)
    if len(text_cues) != len(timing_cues):
        raise ValueError(
            "current text and authoritative timing source have different cue counts"
        )
    return "\n".join(
        f"{index}\n"
        f"{_srt_time(timing_cue.start_ms)} --> "
        f"{_srt_time(timing_cue.end_ms)}\n"
        f"{text_cue.text}\n"
        for index, (text_cue, timing_cue) in enumerate(
            zip(text_cues, timing_cues),
            start=1,
        )
    )


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cid", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--delivery", required=True, type=Path, help="delivered .mp4 to refresh")
    p.add_argument(
        "--delivery-authority-record",
        type=Path,
        help=(
            "independent pre-correction package record required when --delivery already exists; "
            "must hash-bind the supplied publish draft"
        ),
    )
    p.add_argument(
        "--delivery-authority-publish",
        type=Path,
        help=(
            "independent pre-correction package publish draft required when --delivery already exists"
        ),
    )
    p.add_argument(
        "--recovery-branding-authority",
        type=Path,
        help=(
            "narrow incident-recovery authority for an already polluted delivery; "
            "cannot replace the normal record+publish authority path"
        ),
    )
    p.add_argument("--replace", action="append", default=[], metavar="OLD=NEW", help="surgical text swap across all cues")
    p.add_argument("--set-line", action="append", default=[], metavar="N=TEXT", help="replace the whole text of 1-based cue N")
    p.add_argument(
        "--refresh-only",
        action="store_true",
        help="re-burn the current SRT and refresh a stale delivery mirror without changing text",
    )
    p.add_argument(
        "--text-source",
        type=Path,
        help="automatic text SRT consumed by a hash-bound --text-override",
    )
    p.add_argument(
        "--text-override",
        type=Path,
        help="schema-v3 hash-bound override document to apply as the complete text repair",
    )
    p.add_argument(
        "--timing-source",
        type=Path,
        help="authoritative BCUT/v2 SRT whose cue boundaries remain unchanged",
    )
    p.add_argument("--out-base", type=Path, default=BASE)
    p.add_argument("--speaker-overrides", type=Path, help="optional hash-bound reviewed turn/split/overlap decisions")
    p.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    return p.parse_args(argv)


def _should_regenerate_upload_tags(sealed_transaction_context: object | None) -> bool:
    """Only ordinary correction invocations may request tag-generation work."""

    return sealed_transaction_context is None


def main(
    argv=None,
    *,
    _sealed_transaction_context: object | None = None,
) -> int:
    """Run a correction transaction.

    ``_sealed_transaction_context`` is deliberately private and accepted only
    from a candidate-sealed runner.  It is revalidated against the deployed
    authority after normal CLI parsing and before any staging path is created;
    raw branding mappings are never injectable through this seam or any public
    command-line flag.
    """
    args = _parse_args(argv)

    recut_dir = args.out_base / "out" / args.date / args.cid / "replacement_recuts"
    record_path = recut_dir / f"{args.cid}.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    srt_path = Path(str(record["subtitle_path"]))
    srt = srt_path.read_text(encoding="utf-8")

    if _sealed_transaction_context is None:
        try:
            branding_intro, delivery_branding_authority = _prepare_correction_branding(
                delivery=args.delivery,
                working_record_path=record_path,
                candidate_id=args.cid,
                authority_record_path=args.delivery_authority_record,
                authority_publish_path=args.delivery_authority_publish,
                recovery_authority_path=args.recovery_branding_authority,
            )
        except (
            BrandingIntroError,
            DeliveryBrandingAuthorityError,
            DeliveryCopyError,
        ) as exc:
            print(f"DELIVERY_BRANDING_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
            return 1
    else:
        if any(
            value is not None
            for value in (
                args.delivery_authority_record,
                args.delivery_authority_publish,
                args.recovery_branding_authority,
            )
        ):
            print(
                "prepared delivery authority cannot be mixed with CLI authority inputs",
                file=sys.stderr,
            )
            return 2
        from src.autoslice.sealed_subtitle_correction import (
            SealedSubtitleCorrectionTransactionContext,
        )

        if not isinstance(
            _sealed_transaction_context, SealedSubtitleCorrectionTransactionContext
        ):
            print("invalid sealed subtitle correction context", file=sys.stderr)
            return 2
        try:
            branding_intro, delivery_branding_authority = (
                _sealed_transaction_context.prepare_for_transaction(
                    args=args,
                    working_record_path=record_path,
                )
            )
        except (BrandingIntroError, DeliveryBrandingAuthorityError, ValueError) as exc:
            print(f"SEALED_DELIVERY_BRANDING_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
            return 1

    before = srt
    if (args.text_source is None) != (args.text_override is None):
        print(
            "--text-source and --text-override must be supplied together",
            file=sys.stderr,
        )
        return 2
    if args.text_source is not None and args.timing_source is None:
        print(
            "hash-bound text repair requires an authoritative --timing-source",
            file=sys.stderr,
        )
        return 2
    if args.text_source is not None and args.delivery.exists():
        print(
            "hash-bound text override is not supported for an existing delivery; "
            "use the transactional reviewed-baseline replay path",
            file=sys.stderr,
        )
        return 2
    if (
        args.timing_source is not None
        and args.text_source is None
        and not args.refresh_only
    ):
        print(
            "timing-only projection requires --refresh-only",
            file=sys.stderr,
        )
        return 2
    text_override_manifest = None
    text_override_manifest_path = None
    text_override_output_path = None
    text_override_decision_output_path = None
    if (
        args.text_source is not None
        and args.timing_source is not None
    ):
        if args.replace or args.set_line:
            print(
                "hash-bound text repair cannot be mixed with --replace/--set-line",
                file=sys.stderr,
            )
            return 2
        text_override_decision_output_path = (
            recut_dir / f"{args.cid}.human-reviewed-decision-output.srt"
        )
        text_override_output_path = recut_dir / f"{args.cid}.human-reviewed-text.srt"
        text_override_manifest_path = (
            recut_dir / f"{args.cid}.human-reviewed-text.json"
        )
        text_override_manifest = apply_text_override_document(
            args.text_source,
            args.text_override,
            text_override_decision_output_path,
            text_override_manifest_path,
            expected_candidate_id=args.cid,
        )
        if text_override_manifest.get("candidate_id") != args.cid:
            print("text override candidate_id mismatch", file=sys.stderr)
            return 2
        projected_text = _project_reviewed_text_onto_timing(
            text_source=args.text_source,
            decision_output=text_override_decision_output_path,
            timing_source=args.timing_source,
            text_override=args.text_override,
        )
        text_override_output_path.write_text(projected_text, encoding="utf-8")
        srt = text_override_output_path.read_text(encoding="utf-8")
    elif args.refresh_only and args.timing_source is not None:
        srt = _project_existing_text_onto_timing(
            text_srt=srt,
            timing_srt=args.timing_source.read_text(encoding="utf-8"),
        )
    for pair in args.replace:
        old, _, new = pair.partition("=")
        srt = srt.replace(old, new)
    if args.set_line:
        blocks = _srt_blocks(srt)
        wants = {int(n): t for n, _, t in (s.partition("=") for s in args.set_line)}
        for i, b in enumerate(blocks, start=1):
            if i in wants:
                lines = b.split("\n")
                blocks[i - 1] = "\n".join(lines[:2] + [wants[i]])  # keep index + timing, swap text
        srt = "\n\n".join(blocks) + "\n"
    if srt == before and not args.refresh_only:
        print("NO_CHANGE: nothing matched the correction — check --replace/--set-line", file=sys.stderr)
        return 2
    speaker_mode = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    if speaker_mode not in ("uniform_host", "required", "auto"):
        speaker_mode = "uniform_host"
    try:
        (
            speaker_srt,
            speaker_ass,
            speaker_manifest_path,
            correction_manifest_path,
        ) = _preflight_correction_delivery(
            delivery=args.delivery,
            srt_path=srt_path,
            record_path=record_path,
            recut_dir=recut_dir,
            candidate_id=args.cid,
        )
    except DeliveryCopyError as exc:
        print(f"DELIVERY_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
        return 1
    before_hash = hashlib.sha256(before.encode("utf-8")).hexdigest()
    try:
        (
            staging_dir, staged_srt, staged_speaker_srt, staged_speaker_ass,
            staged_speaker_manifest, speaker_manifest, reburn,
        ) = _render_correction_in_staging(
            record=record, srt=srt, recut_dir=recut_dir, candidate_id=args.cid,
            speaker_mode=speaker_mode, speaker_overrides=args.speaker_overrides,
            speaker_python=args.speaker_python, branding_intro=branding_intro,
        )
    except (DeliveryCopyError, BrandingIntroError) as exc:
        print(f"BURN_FAILED: {exc}", file=sys.stderr)
        return 1
    try:
        burned_value = reburn.get("burned_preview") if isinstance(reburn, Mapping) else None
        burned = Path(str(burned_value.get("path"))) if isinstance(burned_value, Mapping) and burned_value.get("path") else None
        if not burned or not burned.is_file() or burned_value.get("status") != "BURNED":
            print(f"BURN_FAILED: {burned_value}", file=sys.stderr)
            return 1
        _verify_reburn_kept_authorized_intro(burned_value, delivery_branding_authority)
        media_path = Path(str(record["media_path"]))
        burned_target = media_path.with_name(burned.name)
        staged_ass = Path(str(burned_value["ass_path"]))
        _regular_or_absent(staged_ass, label="reburn final ASS")
        if not staged_ass.is_file():
            print(f"BURN_FAILED: final ASS is missing: {staged_ass}", file=sys.stderr)
            return 1
        ass_target = media_path.with_name(staged_ass.name)
        final_burned_value = dict(burned_value)
        final_burned_value.update({"path": str(burned_target), "ass_path": str(ass_target)})
        staged_manifest = staging_dir / correction_manifest_path.name
        _write_correction_manifest(
            args=args, before_hash=before_hash, srt_path=staged_srt,
            speaker_mode=speaker_mode, speaker_manifest=speaker_manifest,
            speaker_manifest_path=speaker_manifest_path,
            speaker_manifest_hash_path=staged_speaker_manifest, burned=burned,
            burned_receipt_path=burned_target,
            delivery_branding_authority=delivery_branding_authority,
            correction_manifest_path=staged_manifest,
            text_override_manifest_path=text_override_manifest_path,
            text_override_output_path=text_override_output_path,
            text_override_decision_output_path=text_override_decision_output_path,
        )
        updated = dict(record)
        hashes = dict(updated.get("artifact_hashes") or {})
        hashes.update({
            "subtitle_sha256": "sha256:" + _sha256(staged_srt),
            "burned_video_sha256": "sha256:" + _sha256(burned),
            "ass_sha256": "sha256:" + _sha256(staged_ass),
        })
        if speaker_manifest is not None:
            assert staged_speaker_srt is not None and staged_speaker_ass is not None
            hashes["speaker_review_srt_sha256"] = "sha256:" + _sha256(staged_speaker_srt)
        updated.update({
            "artifact_hashes": hashes, "speaker_mode": speaker_mode,
            "speaker_review_srt_path": str(speaker_srt) if speaker_manifest is not None else None,
            "subtitle_ass_path": str(ass_target),
            "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID if speaker_manifest is not None else "lidousha-final-sapphire72",
            "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest is not None else None,
            "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(staged_speaker_manifest)) if staged_speaker_manifest else None,
            "speaker_finalization": speaker_manifest,
            "human_text_correction_manifest_path": str(correction_manifest_path),
            "human_text_correction_manifest_sha256": "sha256:" + _sha256(staged_manifest),
            "burned_preview": final_burned_value,
        })
        staging_title = str(((record.get("publish_staging") or {}).get("title")) or "")
        if staging_title and _should_regenerate_upload_tags(_sealed_transaction_context):
            updated["upload_tags"] = generate_upload_tags(staging_title, staged_srt, timeout=180.0)
        if _sealed_transaction_context is not None:
            try:
                _sealed_transaction_context.validate_record_delta(
                    before=record, after=updated
                )
            except ValueError as exc:
                print(f"SEALED_RECORD_DELTA_PREFLIGHT_FAILED: {exc}", file=sys.stderr)
                return 1
        staged_record = staging_dir / record_path.name
        staged_record.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        replacements = [
            (staged_srt, srt_path), (burned, burned_target), (staged_ass, ass_target),
            (staged_manifest, correction_manifest_path), (staged_record, record_path),
            (burned, args.delivery), (staged_srt, args.delivery.with_suffix(".srt")),
            (staged_manifest, args.delivery.with_suffix(".human-text-correction.json")),
            (staged_record, args.delivery.with_suffix(".record.json")),
        ]
        removals: list[Path] = []
        if speaker_manifest is not None:
            assert staged_speaker_srt and staged_speaker_ass and staged_speaker_manifest
            replacements.extend([
                (staged_speaker_srt, speaker_srt), (staged_speaker_ass, speaker_ass),
                (staged_speaker_manifest, speaker_manifest_path),
                (staged_speaker_srt, args.delivery.with_suffix(".speaker.srt")),
                (staged_speaker_ass, args.delivery.with_suffix(".speaker.ass")),
                (staged_speaker_manifest, args.delivery.with_suffix(".speaker.json")),
            ])
        else:
            removals = [args.delivery.with_suffix(suffix) for suffix in (".speaker.srt", ".speaker.ass", ".speaker.json")]
        _commit_staged_files(replacements, removals)
    except (DeliveryCopyError, DeliveryBrandingAuthorityError) as exc:
        print(f"DELIVERY_CORRECTION_COMMIT_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    print(f"speaker-final re-burn + delivery refreshed (STAGED_COMMIT) → {args.delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
