"""Bind newly reviewed words to an unchanged direct screenshot background."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from collections.abc import Mapping

from src.autoslice.cover_pixel_preservation import _regular, _sha, _generation
from src.autoslice.cover_punch_semantics import validate_cover_punch_semantic_review
from src.autoslice.cover_font_paths import resolve_trusted_cover_font
from src.autoslice.cover_text_pixel_evidence import (
    materialize_rendered_text_pixel_evidence,
    verify_rendered_text_pixel_artifacts,
    verify_pre_overlay_route_background,
)
from src.autoslice.story_contract import cover_story_contract_binding_matches
from src.autoslice.surface_canon import CHANNEL_PROFILE

KEY = "approved_punch_successor"
SCHEMA = "screenshot-approved-punch-successor.v1"
ROOT = Path(__file__).resolve().parents[2]
# Locators may change only while their independently checked hashes stay fixed.
MUTABLE = {
    KEY, "status", "candidate_id", "cover_punch", "rendered_lines",
    "final_cover", "final_cover_sha256", "rendered_text_pixels", "font_size",
    "overlay_position", "pre_overlay_path", "ai_background", "reference_image",
    "polish_face_verification", "final_host_identity_verification",
}


def _frozen(generation: Mapping) -> dict:
    value = copy.deepcopy(dict(generation))
    for key in MUTABLE:
        value.pop(key, None)
    value.pop("story_contract", None)  # checked separately, including enrichment
    art = value.get("art_direction")
    if isinstance(art, dict):
        art.pop("cover_punch", None)
        art.pop("cover_punch_semantic_review", None)
    return value


def _read_bound(receipt: Mapping, name: str) -> dict:
    raw = _regular(Path(str(receipt[name + "_path"])))
    if _sha(raw) != receipt[name + "_sha256"]:
        raise ValueError(f"approved punch {name} hash drift")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"approved punch {name} must be an object")
    return value


def validate_successor(generation: dict, *, active_parent: Mapping | None = None) -> dict:
    """Replay real preimages and existing semantic/pixel validators; never a model call."""
    receipt = generation.get(KEY)
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != SCHEMA:
        raise ValueError("approved punch successor receipt missing or invalid")
    if receipt.get("provider_calls") != 0 or receipt.get("image_generation_calls") != 0:
        raise ValueError("approved punch successor must be a zero-call binding")
    record = _read_bound(receipt, "parent_record")
    parent = _generation(record)
    if KEY in parent or parent.get("method") != "screenshot_direct":
        raise ValueError("approved punch successor requires an original direct screenshot")
    if active_parent is not None and dict(active_parent) != parent:
        raise ValueError("approved punch parent differs from active package authority")
    if _frozen(generation) != _frozen(parent):
        raise ValueError("approved punch changed frozen source, route, title, or design")
    old_story, new_story = parent.get("story_contract"), generation.get("story_contract")
    if old_story != new_story and not cover_story_contract_binding_matches(new_story, old_story):
        raise ValueError("approved punch changed the source StoryContract")
    staging = record.get("publish_staging") or {}
    cid = receipt.get("candidate_id")
    if not isinstance(cid, str) or Path(str(receipt.get("source_record_path") or "")).name != cid + ".record.json":
        raise ValueError("approved punch source candidate locator mismatch")
    if (generation.get("candidate_id") != cid
            or generation.get("title") != staging.get("title")):
        raise ValueError("approved punch candidate/title differs from source record")
    story = record.get("story_contract") or parent.get("story_contract") or {}
    hook = str(story.get("selection_hook") or "")
    result = _read_bound(receipt, "semantic_result")
    proof = result.get("proof", result)
    lines = generation.get("rendered_lines")
    art = generation.get("art_direction") or {}
    if (not hook or generation.get("cover_punch") != lines
            or art.get("cover_punch") != lines
            or art.get("cover_punch_semantic_review") != proof
            or not validate_cover_punch_semantic_review(
                proof, rendered_lines=lines,
                cover_text=str(generation.get("cover_text") or ""), story_hook=hook)):
        raise ValueError("approved punch lacks matching canonical semantic approval")
    if result.get("candidate_id", cid) != cid:
        raise ValueError("approved punch semantic candidate mismatch")
    evidence = generation.get("rendered_text_pixels") or {}
    old_pixels = parent.get("rendered_text_pixels") or {}
    for key in ("font_file_name", "font_file_sha256"):
        if evidence.get(key) != old_pixels.get(key):
            raise ValueError("approved punch changed the frozen title font")
    font = resolve_trusted_cover_font(
        file_name=str(evidence.get("font_file_name") or ""),
        expected_sha256=str(evidence.get("font_file_sha256") or ""),
        channel_profile=CHANNEL_PROFILE, root=ROOT,
    )
    cover = Path(str(generation.get("final_cover") or ""))
    pre = Path(str(generation.get("pre_overlay_path") or ""))
    background = Path(str(generation.get("ai_background") or ""))
    reference = Path(str(generation.get("reference_image") or ""))
    mask = Path(str(evidence.get("mask_path") or ""))
    for path, expected in (
        (cover, generation.get("final_cover_sha256")),
        (pre, parent.get("pre_overlay_sha256")),
        (background, parent.get("ai_background_sha256")),
        (reference, parent.get("reference_sha256")),
        (mask, evidence.get("mask_sha256")),
    ):
        if _sha(_regular(path)) != expected:
            raise ValueError("approved punch screenshot/pixel file hash drift")
    if not (verify_rendered_text_pixel_artifacts(
            evidence, final_cover_path=cover, pre_overlay_path=pre,
            mask_path=mask, font_path=font,
            expected_pre_overlay_sha256=parent.get("pre_overlay_sha256"))
            and verify_pre_overlay_route_background(
                route_background_path=background, pre_overlay_path=pre,
                expected_route_background_sha256=parent.get("ai_background_sha256"),
                text_backing=generation.get("text_backing"), scrim=generation.get("scrim"))):
        raise ValueError("approved punch exact title-layer replay failed")
    return parent


def prepare_successor(*, candidate_id: str, source_record: Path, cover: Path, reference: Path,
                      semantic_result: Path, render_receipt: Path,
                      face_receipt: Path, host_receipt: Path, out: Path) -> dict:
    """Prepare a create-only generation using an already-rendered approved PNG.

    The package binder still verifies this preimage against active record/publish
    documents and unchanged media before any package write. Final face/host
    receipts are consumed verbatim by its existing validators. Joint QC remains
    the independent publication gate.
    """
    from src.autoslice.cover_repair_route_lineage import validate_screenshot_generation_document

    record_bytes = _regular(source_record)
    record = json.loads(record_bytes)
    parent = _generation(record)
    semantic_bytes = _regular(semantic_result)
    result = json.loads(semantic_bytes)
    proof = result.get("proof", result)
    lines = proof.get("final_punch")
    render = json.loads(_regular(render_receipt))
    pixels = render.get("native_pixel_evidence", render)
    png = _regular(cover)
    if _sha(png) != pixels.get("final_cover_sha256"):
        raise ValueError("approved punch candidate differs from rendered evidence")
    inputs = {
        "pre-overlay.png": (_regular(Path(parent["pre_overlay_path"])), parent["pre_overlay_sha256"]),
        "ai-bg.png": (_regular(Path(parent["ai_background"])), parent["ai_background_sha256"]),
        "reference.png": (_regular(reference), parent["reference_sha256"]),
    }
    if any(_sha(data) != expected for data, expected in inputs.values()):
        raise ValueError("approved punch source input hash mismatch")
    out = out.absolute()
    if not out.parent.is_dir() or any(out.parent.iterdir()):
        raise ValueError("approved punch output requires an empty private directory")
    out.write_bytes(png)
    for suffix, (data, _) in inputs.items():
        out.with_suffix("." + suffix).write_bytes(data)
    preimage = out.with_suffix(".punch-source-record.json")
    preimage.write_bytes(record_bytes)
    semantic_copy = out.with_suffix(".punch-semantic-result.json")
    semantic_copy.write_bytes(semantic_bytes)
    font = resolve_trusted_cover_font(
        file_name=pixels["font_file_name"], expected_sha256=pixels["font_file_sha256"],
        channel_profile=CHANNEL_PROFILE, root=ROOT,
    )
    position = pixels["overlay_position"]
    evidence = materialize_rendered_text_pixel_evidence(
        final_cover_path=out, pre_overlay_path=out.with_suffix(".pre-overlay.png"),
        font_path=font, render_spec=pixels["render_spec"],
        paste_xy=(position["x"], position["y"]), font_size=pixels["font_size"],
        rendered_text="".join(lines),
    )
    generation = copy.deepcopy(parent)
    generation.update(
        status="AI_COVER_READY", candidate_id=candidate_id,
        cover_punch=lines, rendered_lines=lines, final_cover=str(out), final_cover_sha256=_sha(png),
        ai_background=str(out.with_suffix(".ai-bg.png")),
        reference_image=str(out.with_suffix(".reference.png")),
        pre_overlay_path=str(out.with_suffix(".pre-overlay.png")),
        rendered_text_pixels=evidence, font_size=evidence["font_size"],
        overlay_position=evidence["overlay_position"],
        polish_face_verification=json.loads(_regular(face_receipt)),
        final_host_identity_verification=json.loads(_regular(host_receipt)),
    )
    generation["art_direction"].update(cover_punch=lines, cover_punch_semantic_review=proof)
    generation[KEY] = {
        "schema_version": SCHEMA, "candidate_id": candidate_id,
        "source_record_path": str(source_record.absolute()), "parent_record_path": str(preimage),
        "parent_record_sha256": _sha(record_bytes), "semantic_result_path": str(semantic_copy),
        "semantic_result_sha256": _sha(semantic_bytes), "provider_calls": 0,
        "image_generation_calls": 0,
    }
    validate_successor(generation)
    validate_screenshot_generation_document(
        generation, cover=out, title=generation["title"],
        candidate_id=generation["candidate_id"], method="screenshot_direct",
    )
    manifest = out.with_suffix(".cover_generation.json")
    with manifest.open("x") as f:
        json.dump(generation, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return generation
