"""Create-only, byte-identical screenshot evidence successors; never new opinions."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from src.autoslice.cover_font_paths import resolve_trusted_cover_font
from src.autoslice.cover_host_identity_gate import validate_final_host_identity_verification
from src.autoslice.cover_text_pixel_evidence import (
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.story_contract import cover_story_contract_binding_matches
from src.autoslice.surface_canon import CHANNEL_PROFILE

ROOT = Path(__file__).resolve().parents[2]
KEY = "pixel_preserving_successor"
SCHEMA = "screenshot-cover-pixel-preservation.v1"


def _regular(path: Path) -> bytes:
    path = path.absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("pixel preservation refuses symlink paths")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("pixel preservation requires regular input files")
    return path.read_bytes()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _generation(record: object) -> dict:
    if not isinstance(record, dict):
        raise ValueError("pixel source record must be an object")
    staging = record.get("publish_staging")
    value = staging.get("cover_generation") if isinstance(staging, dict) else None
    value = value or record.get("cover_generation")
    if not isinstance(value, dict) or KEY in value:
        raise ValueError("use the existing successor; source must be an original generation")
    return value


def _verify_pixels(generation: dict) -> dict[Path, bytes]:
    from src.autoslice.cover_repair_route_lineage import validate_screenshot_generation_document

    cover = Path(str(generation.get("final_cover") or ""))
    evidence = generation.get("rendered_text_pixels")
    if not isinstance(evidence, dict):
        raise ValueError("pixel preservation requires existing rendered-text evidence")
    paths = {
        "cover": cover,
        "pre": Path(str(generation.get("pre_overlay_path") or "")),
        "mask": Path(str(evidence.get("mask_path") or "")),
        "background": Path(str(generation.get("ai_background") or "")),
        "reference": Path(str(generation.get("reference_image") or "")),
    }
    frozen = {path: _regular(path) for path in paths.values()}
    validate_screenshot_generation_document(
        generation, cover=cover, candidate_id=str(generation.get("candidate_id") or ""),
        title=str(generation.get("title") or ""), method=str(generation.get("method") or ""),
    )
    font = resolve_trusted_cover_font(
        file_name=str(evidence.get("font_file_name") or ""),
        expected_sha256=str(evidence.get("font_file_sha256") or ""),
        channel_profile=CHANNEL_PROFILE, root=ROOT,
    )
    frozen[font] = _regular(font)
    if not (validate_final_host_identity_verification(generation)
            and verify_rendered_text_pixel_artifacts(
                evidence, final_cover_path=cover, pre_overlay_path=paths["pre"],
                mask_path=paths["mask"], font_path=font,
                expected_pre_overlay_sha256=generation.get("pre_overlay_sha256"))
            and verify_pre_overlay_route_background(
                route_background_path=paths["background"], pre_overlay_path=paths["pre"],
                expected_route_background_sha256=generation.get("ai_background_sha256"),
                text_backing=generation.get("text_backing"), scrim=generation.get("scrim"))):
        raise ValueError("existing screenshot pixels or host evidence do not replay")
    return frozen


def validate_pixel_preserving_successor(document: dict, *, cover: Path) -> None:
    """Replay an immutable preimage, not the subsequently rebound live record."""
    receipt = document.get(KEY)
    if not isinstance(receipt, Mapping) or set(receipt) != {
        "schema_version", "source_record_path", "record_preimage_path", "record_sha256",
        "record_bytes", "final_cover_sha256", "provider_calls", "rendered_new_pixels", "created_at",
    }:
        raise ValueError("pixel-preservation receipt shape is invalid")
    if (receipt["schema_version"] != SCHEMA or type(receipt["provider_calls"]) is not int
            or receipt["provider_calls"] != 0 or receipt["rendered_new_pixels"] is not False):
        raise ValueError("pixel preservation cannot claim new model/render work")
    if (type(receipt["record_bytes"]) is not int or receipt["record_bytes"] <= 0
            or not isinstance(receipt["source_record_path"], str)
            or not Path(receipt["source_record_path"]).is_absolute()
            or not isinstance(receipt["created_at"], str)
            or datetime.fromisoformat(receipt["created_at"]).tzinfo is None):
        raise ValueError("pixel preservation provenance metadata is invalid")
    preimage = cover.with_suffix(".pixel-source-record.json")
    if receipt["record_preimage_path"] != str(preimage):
        raise ValueError("pixel preservation preimage is not the dedicated sibling")
    raw = _regular(preimage)
    if receipt["record_sha256"] != _sha(raw) or receipt["record_bytes"] != len(raw):
        raise ValueError("pixel preservation source record hash drift")
    prior = _generation(json.loads(raw))
    _verify_pixels(prior)
    if (receipt["final_cover_sha256"] != prior.get("final_cover_sha256")
            or _sha(_regular(cover)) != prior.get("final_cover_sha256")):
        raise ValueError("pixel preservation final bytes differ from the original")
    expected = copy.deepcopy(prior)
    expected["final_cover"] = str(cover)
    # Native binding may enrich a compact StoryContract from its active full one.
    if expected.get("story_contract") != document.get("story_contract"):
        if not cover_story_contract_binding_matches(
            document.get("story_contract"), expected.get("story_contract"),
        ):
            raise ValueError("pixel preservation story authority drift")
        expected["story_contract"] = copy.deepcopy(document.get("story_contract"))
    expected[KEY] = dict(receipt)
    if document != expected:
        raise ValueError("pixel preservation changed fields other than the final locator")


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def preserve_existing_pixels(
    *, record_path: Path, generation: dict, candidate_id: str, title: str, out: Path,
) -> dict:
    """Verify first, then write one new private namespace without touching parents."""
    raw = _regular(record_path)
    source = _generation(json.loads(raw))
    if (source != generation or source.get("candidate_id") != candidate_id
            or source.get("title") != title):
        raise ValueError("pixel preservation source identity changed")
    frozen = _verify_pixels(source)
    frozen[record_path] = raw
    out = out.absolute()
    if (out.suffix != ".png" or any(p.is_symlink() for p in (out, *out.parents))
            or os.path.lexists(out.parent) or not out.parent.parent.is_dir()):
        raise ValueError("pixel preservation requires a new dedicated output directory")
    if any(_regular(path) != data for path, data in frozen.items()):
        raise ValueError("pixel preservation input drift before write")
    preimage = out.with_suffix(".pixel-source-record.json")
    receipt = {
        "schema_version": SCHEMA, "source_record_path": str(record_path.absolute()),
        "record_preimage_path": str(preimage), "record_sha256": _sha(raw), "record_bytes": len(raw),
        "final_cover_sha256": source["final_cover_sha256"], "provider_calls": 0,
        "rendered_new_pixels": False, "created_at": datetime.now(timezone.utc).isoformat(),
    }
    successor = copy.deepcopy(source)
    successor.update(final_cover=str(out), pixel_preserving_successor=receipt)
    # A partial write remains in this owned namespace for readback, never overwrites a prior result.
    out.parent.mkdir(mode=0o700)
    _write_new(preimage, raw)
    _write_new(out, frozen[Path(source["final_cover"])])
    manifest = out.with_suffix(".cover_generation.json")
    _write_new(manifest, (json.dumps(successor, ensure_ascii=False, indent=2) + "\n").encode())
    from src.autoslice.cover_repair_route_lineage import validate_cover_generation_for_binding

    validate_cover_generation_for_binding(cover=out, title=title, candidate_id=candidate_id)
    if any(_regular(path) != data for path, data in frozen.items()):
        raise ValueError("pixel preservation source drift after write")
    result = {"status": "PIXELS_PRESERVED", "candidate_id": candidate_id,
              "cover_path": str(out), "cover_sha256": _sha(_regular(out)),
              "generation_path": str(manifest), "generation_sha256": _sha(_regular(manifest)),
              "provider_calls": 0, "rendered_new_pixels": False,
              "media_binding_created": False, "upload_allowed": False}
    _write_new(out.with_suffix(".pixel-preservation.json"),
               (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode())
    fd = os.open(out.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return result
