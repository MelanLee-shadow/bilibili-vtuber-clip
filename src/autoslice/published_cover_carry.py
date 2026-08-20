"""Fail-closed carry of an already public cover into one isolated repair.

This deliberately does not mint a cover decision.  It only copies the exact
bytes and v2 evidence named by a deploy-sealed, candidate-specific authority.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.cover_host_identity_gate import validate_final_host_identity_verification
from src.autoslice.cover_route_evidence import validate_cover_route_decision
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    require_repository_asset_authority,
)


ASSET_RELATIVE_PATH = Path("assets/lidousha/daily_same_bv_published_cover_carry_authority.v1.json")
SCHEMA = "daily-same-bv-published-cover-carry-authority.v1"
MARKER_SCHEMA = "published-cover-carry-binding.v1"


class PublishedCoverCarryError(ValueError):
    pass


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _regular(path: Path, *, label: str) -> bytes:
    try:
        meta = path.lstat()
    except OSError as exc:
        raise PublishedCoverCarryError(f"{label}_MISSING") from exc
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode):
        raise PublishedCoverCarryError(f"{label}_UNSAFE")
    return path.read_bytes()


def _pointer(value: Mapping[str, object], *parts: str) -> object:
    current: object = value
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _entry(*, repo_root: Path, candidate_id: str) -> dict[str, object]:
    path = repo_root / ASSET_RELATIVE_PATH
    raw = _regular(path, label="PUBLISHED_COVER_CARRY_AUTHORITY")
    try:
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=ASSET_RELATIVE_PATH, observed_bytes=raw
        )
        document = json.loads(raw)
    except (RepositoryAssetAuthorityError, ValueError, json.JSONDecodeError) as exc:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_AUTHORITY_UNSEALED") from exc
    if not isinstance(document, dict) or set(document) != {"schema_version", "authority", "entries"} or document.get("schema_version") != SCHEMA:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_AUTHORITY_INVALID")
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) != 1:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_AUTHORITY_SCOPE")
    entry = entries[0]
    if not isinstance(entry, dict) or set(entry) != {"candidate_id", "recording_date", "source_state_sha256", "published_identity", "cover_generation_sha256", "artifacts"} or entry.get("candidate_id") != candidate_id:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_CANDIDATE_MISMATCH")
    return entry


def _source_relative_regular(
    *, source_base: Path, relative_value: object, label: str
) -> bytes:
    """Read a canonical regular file below the runtime recovery source.

    The carry authority is repository-sealed, but its public verification
    receipt is runtime evidence.  Keep that boundary explicit: a receipt may
    only be named relative to the exact recovery source base, with no symlink
    in any path component and no resolution outside that base.
    """

    if not isinstance(relative_value, str) or not relative_value:
        raise PublishedCoverCarryError(f"{label}_PATH_INVALID")
    relative = Path(relative_value)
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
        or relative.as_posix() != relative_value
    ):
        raise PublishedCoverCarryError(f"{label}_PATH_INVALID")
    try:
        if source_base.is_symlink() or not source_base.is_dir():
            raise OSError("source base is not a regular directory")
        source_root = source_base.resolve(strict=True)
        cursor = source_base
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise OSError("source receipt path contains a symlink")
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise PublishedCoverCarryError(f"{label}_PATH_INVALID") from exc
    return _regular(resolved, label=label)


def validate_source_cover_carry(
    *,
    repo_root: Path,
    source_base: Path,
    state_bytes: bytes,
    state: Mapping[str, object],
    candidate_id: str,
    date: str,
) -> dict[str, object]:
    """Validate the source's public identity and original cover evidence."""

    entry = _entry(repo_root=repo_root, candidate_id=candidate_id)
    if entry.get("recording_date") != date or entry.get("source_state_sha256") != _sha(state_bytes):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_SOURCE_STATE_MISMATCH")
    picks = state.get("picks")
    rows = [row for row in picks or () if isinstance(row, dict) and row.get("candidate_id") == candidate_id]
    if len(rows) != 1:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PICK_MISMATCH")
    record = rows[0]
    required = entry.get("published_identity")
    if not isinstance(required, dict) or set(required) != {"bvid", "aid", "published_cid", "title", "public_verify_source_relative_path", "public_verify_sha256"}:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_IDENTITY_INVALID")
    if not isinstance(required, dict) or any(record.get(key) != required.get(key) for key in ("bvid", "aid", "published_cid", "title")):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_IDENTITY_MISMATCH")
    if not (
        record.get("status") == "published"
        and record.get("prepublication_status") == "review_ready"
        and record.get("bundle_lifecycle") == "CURRENT"
        and record.get("bundle_compliance") == "COMPLIANT"
        and record.get("rc") == 0
    ):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PICK_INELIGIBLE")
    verify_bytes = _source_relative_regular(
        source_base=source_base,
        relative_value=required.get("public_verify_source_relative_path"),
        label="PUBLISHED_COVER_CARRY_PUBLIC_VERIFY",
    )
    if required.get("public_verify_sha256") != _sha(verify_bytes):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_VERIFY_HASH_MISMATCH")
    try:
        verify = json.loads(verify_bytes)
    except (ValueError, json.JSONDecodeError) as exc:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_VERIFY_INVALID") from exc
    public = verify.get("public_view") if isinstance(verify, dict) else None
    expected = verify.get("expected") if isinstance(verify, dict) else None
    archive = verify.get("member_archive") if isinstance(verify, dict) else None
    if not isinstance(public, dict) or not isinstance(expected, dict) or not isinstance(archive, dict) or verify.get("schema_version") != "authorized-upload-public-verify.v2" or verify.get("status") != "VERIFIED_PUBLIC":
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_VERIFY_INVALID")
    section = verify.get("section_api") if isinstance(verify, dict) else None
    episode_titles = section.get("episode_titles") if isinstance(section, dict) else None
    if (
        verify.get("bvid") != required.get("bvid")
        or str(public.get("aid")) != str(required.get("aid"))
        or str(public.get("cid")) != str(required.get("published_cid"))
        or public.get("title") != required.get("title")
        or archive.get("bvid") != required.get("bvid")
        or str(archive.get("aid")) != str(required.get("aid"))
        or archive.get("title") != required.get("title")
        or expected.get("title") != required.get("title")
        or verify.get("manifest_title") != required.get("title")
        or not isinstance(episode_titles, list)
        or episode_titles != [required.get("title")]
    ):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_PUBLIC_VERIFY_IDENTITY_MISMATCH")
    generation = record.get("cover_generation")
    canonical = json.dumps(generation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if not isinstance(generation, dict) or entry.get("cover_generation_sha256") != _sha(canonical):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_GENERATION_MISMATCH")
    if not validate_cover_route_decision(generation, allow_legacy_v1=False) or not validate_final_host_identity_verification(generation):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_ROUTE_INVALID")
    artifacts = entry.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"cover", "pre_overlay", "title_mask", "route_background", "reference", "host_witness", "source_composition_receipt"}:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_ARTIFACT_SET_INVALID")
    for name, binding in artifacts.items():
        if not isinstance(binding, dict) or set(binding) != {"source_path", "sha256", "bytes"}:
            raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_ARTIFACT_INVALID")
        payload = _regular(Path(str(binding.get("source_path") or "")), label=f"PUBLISHED_COVER_CARRY_{name.upper()}")
        if binding.get("bytes") != len(payload) or binding.get("sha256") != _sha(payload):
            raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_ARTIFACT_HASH_MISMATCH")
    pointers = {
        "cover": (("final_cover",), ("final_cover_sha256",)),
        "pre_overlay": (("pre_overlay_path",), ("pre_overlay_sha256",)),
        "title_mask": (("rendered_text_pixels", "mask_path"), ("rendered_text_pixels", "mask_sha256")),
        "route_background": (("screenshot_graphic_poster", "output_path"), ("screenshot_graphic_poster", "output_sha256")),
        "reference": (("reference_image",), ("reference_sha256",)),
        "host_witness": (("final_host_identity_verification", "comparison_path"), ("final_host_identity_verification", "comparison_sha256")),
        "source_composition_receipt": (("source_composition_receipt", "path"), ("source_composition_receipt", "sha256")),
    }
    for name, (path_pointer, hash_pointer) in pointers.items():
        binding = artifacts[name]
        if _pointer(generation, *path_pointer) != binding["source_path"] or _pointer(generation, *hash_pointer) != binding["sha256"]:
            raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_GENERATION_POINTER_MISMATCH")
    if (
        generation.get("ai_background") != artifacts["route_background"]["source_path"]
        or generation.get("ai_background_sha256") != artifacts["route_background"]["sha256"]
    ):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_GENERATION_POINTER_MISMATCH")
    def absolute_paths(value: object) -> set[str]:
        if isinstance(value, dict):
            return set().union(*(absolute_paths(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(absolute_paths(item) for item in value))
        return {value} if isinstance(value, str) and Path(value).is_absolute() else set()
    if absolute_paths(generation) != {str(binding["source_path"]) for binding in artifacts.values()}:
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_GENERATION_PATH_CLOSURE_MISMATCH")
    cover = artifacts["cover"]
    if record.get("cover_sha256") != cover.get("sha256") or record.get("cover_path") != cover.get("source_path"):
        raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_RECORD_COVER_MISMATCH")
    # The authority pins this exact canonical object by hash; keeping its bytes
    # in the source state avoids duplicating a mutable 37KiB receipt in Git.
    return {**entry, "cover_generation": generation}


def materialized_marker(*, entry: Mapping[str, object], target_base: Path, date: str, candidate_id: str, create) -> tuple[dict[str, object], list[tuple[Path, tuple[int, int]]]]:
    """Copy sealed evidence into target-local paths via caller's create-only primitive."""
    artifacts = entry["artifacts"]
    assert isinstance(artifacts, dict)
    # publish_staging resolves media_path.parent / covers.  The replay package
    # therefore owns the carried final cover, while its replay evidence sits
    # beside that package rather than in a date-global cache.
    package = target_base / "out" / date / candidate_id / "replacement_recuts"
    cache = package / "published-cover-carry"
    covers = package / "covers"
    destinations = {
        "cover": covers / f"{candidate_id}.published-carry.cover.png",
        "pre_overlay": cache / "pre-overlay.png", "title_mask": cache / "title-mask.png",
        "route_background": cache / "route-background.png", "reference": cache / "reference.png",
        "host_witness": cache / "host-witness.png", "source_composition_receipt": cache / "source-composition.json",
    }
    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        for name, target in destinations.items():
            binding = artifacts[name]
            assert isinstance(binding, dict)
            payload = _regular(Path(str(binding["source_path"])), label=f"PUBLISHED_COVER_CARRY_{name.upper()}")
            if len(payload) != binding["bytes"] or _sha(payload) != binding["sha256"]:
                raise PublishedCoverCarryError("PUBLISHED_COVER_CARRY_COPY_SOURCE_DRIFT")
            create(target, payload)
            meta = target.lstat()
            created.append((target, (meta.st_dev, meta.st_ino)))
        generation = json.loads(json.dumps(entry["cover_generation"], ensure_ascii=False))
        assert isinstance(generation, dict)
        # Rebind every path only after all source bytes were copied; consumers then
        # see only target-local evidence, never a remote/public package path.
        replacements = {str(artifacts[name]["source_path"]): str(path) for name, path in destinations.items()}
        def rebind(value):
            if isinstance(value, dict):
                return {k: rebind(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rebind(v) for v in value]
            return replacements.get(value, value) if isinstance(value, str) else value
        generation = rebind(generation)
        generation["published_cover_carry_strict"] = True
        generation["final_cover_sha256"] = artifacts["cover"]["sha256"]
        sidecar = covers / f"{candidate_id}.published-cover-generation.json"
        create(sidecar, (json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())
        meta = sidecar.lstat()
        created.append((sidecar, (meta.st_dev, meta.st_ino)))
    except BaseException:
        for target, identity in reversed(created):
            try:
                meta = target.lstat()
                if (
                    stat.S_ISLNK(meta.st_mode)
                    or not stat.S_ISREG(meta.st_mode)
                    or (meta.st_dev, meta.st_ino) != identity
                ):
                    raise PublishedCoverCarryError(
                        f"PUBLISHED_COVER_CARRY_ROLLBACK_OWNERSHIP_LOST:{target}"
                    )
                target.unlink()
            except PublishedCoverCarryError:
                raise
            except OSError as exc:
                raise PublishedCoverCarryError(
                    f"PUBLISHED_COVER_CARRY_ROLLBACK_FAILED:{target}"
                ) from exc
        raise
    return ({"schema_version": MARKER_SCHEMA, "status": "MATERIALIZED", "candidate_id": candidate_id, "recording_date": date, "cover_path": str(destinations["cover"]), "cover_sha256": artifacts["cover"]["sha256"], "generation_path": str(sidecar), "generation_sha256": _sha(sidecar.read_bytes()), "artifacts": {name: {"path": str(path), "sha256": artifacts[name]["sha256"], "bytes": artifacts[name]["bytes"]} for name, path in destinations.items()}}, created)


def validate_materialized_marker(marker: object, *, base: Path, date: str, candidate_id: str) -> bool:
    required = {"schema_version", "status", "candidate_id", "recording_date", "cover_path", "cover_sha256", "generation_path", "generation_sha256", "artifacts"}
    if not isinstance(marker, dict) or set(marker) != required or marker.get("schema_version") != MARKER_SCHEMA or marker.get("status") != "MATERIALIZED" or marker.get("candidate_id") != candidate_id or marker.get("recording_date") != date:
        return False
    try:
        relative_cover = Path(str(marker["cover_path"])).resolve().relative_to(base.resolve())
        if len(relative_cover.parts) != 6 or relative_cover.parts[0] != "out" or relative_cover.parts[1] != date or relative_cover.parts[2] != candidate_id or relative_cover.parts[3:5] != ("replacement_recuts", "covers"):
            return False
        package = base.resolve() / "out" / relative_cover.parts[1] / candidate_id / "replacement_recuts"
        if Path(str(marker["generation_path"])).resolve() != package / "covers" / f"{candidate_id}.published-cover-generation.json":
            return False
        artifacts = marker["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != {"cover", "pre_overlay", "title_mask", "route_background", "reference", "host_witness", "source_composition_receipt"}:
            return False
        for field in ("cover_path", "generation_path"):
            path = Path(str(marker[field]))
            path.resolve().relative_to(base.resolve())
            _regular(path, label="PUBLISHED_COVER_CARRY_TARGET")
        expected_artifact_paths = {
            "cover": Path(str(marker["cover_path"])),
            "pre_overlay": package / "published-cover-carry/pre-overlay.png",
            "title_mask": package / "published-cover-carry/title-mask.png",
            "route_background": package / "published-cover-carry/route-background.png",
            "reference": package / "published-cover-carry/reference.png",
            "host_witness": package / "published-cover-carry/host-witness.png",
            "source_composition_receipt": package / "published-cover-carry/source-composition.json",
        }
        for name, binding in artifacts.items():
            if not isinstance(binding, dict) or set(binding) != {"path", "sha256", "bytes"}:
                return False
            if Path(str(binding["path"])).resolve() != expected_artifact_paths[name].resolve():
                return False
            payload = _regular(Path(str(binding["path"])), label="PUBLISHED_COVER_CARRY_TARGET")
            if len(payload) != binding["bytes"] or _sha(payload) != binding["sha256"]:
                return False
        cover_payload = _regular(Path(str(marker["cover_path"])), label="PUBLISHED_COVER_CARRY_TARGET")
        generation_payload = _regular(Path(str(marker["generation_path"])), label="PUBLISHED_COVER_CARRY_TARGET")
        if _sha(cover_payload) != marker.get("cover_sha256") or _sha(generation_payload) != marker.get("generation_sha256"):
            return False
        generation = json.loads(generation_payload)
        if not isinstance(generation, dict) or generation.get("published_cover_carry_strict") is not True:
            return False
        if not validate_cover_route_decision(generation, allow_legacy_v1=False) or not validate_final_host_identity_verification(generation):
            return False
        pointers = {
            "cover": (("final_cover",), ("final_cover_sha256",)),
            "pre_overlay": (("pre_overlay_path",), ("pre_overlay_sha256",)),
            "title_mask": (("rendered_text_pixels", "mask_path"), ("rendered_text_pixels", "mask_sha256")),
            "route_background": (("screenshot_graphic_poster", "output_path"), ("screenshot_graphic_poster", "output_sha256")),
            "reference": (("reference_image",), ("reference_sha256",)),
            "host_witness": (("final_host_identity_verification", "comparison_path"), ("final_host_identity_verification", "comparison_sha256")),
            "source_composition_receipt": (("source_composition_receipt", "path"), ("source_composition_receipt", "sha256")),
        }
        for name, (path_pointer, hash_pointer) in pointers.items():
            if _pointer(generation, *path_pointer) != artifacts[name]["path"] or _pointer(generation, *hash_pointer) != artifacts[name]["sha256"]:
                return False
        if generation.get("ai_background") != artifacts["route_background"]["path"] or generation.get("ai_background_sha256") != artifacts["route_background"]["sha256"]:
            return False
        return generation.get("final_cover_sha256") == marker["cover_sha256"] and generation.get("final_cover") == marker["cover_path"]
    except (KeyError, ValueError, OSError, PublishedCoverCarryError):
        return False


def queue_marker_is_valid(marker: object, *, base: Path, date: str, candidate_id: str) -> bool:
    """Small recovery seam: queue code cannot downgrade a typed carry."""
    return validate_materialized_marker(marker, base=base, date=date, candidate_id=candidate_id)


def queue_plan_projection(queue: list[dict]) -> dict[str, dict[str, object]]:
    """Project only stable marker pins into the recovery plan receipt."""
    return {
        str(item["cid"]): {
            "recording_date": item["published_cover_carry"]["recording_date"],
            "cover_sha256": item["published_cover_carry"]["cover_sha256"],
            "generation_sha256": item["published_cover_carry"]["generation_sha256"],
        }
        for item in queue
        if item.get("published_cover_carry_required") is True
    }
