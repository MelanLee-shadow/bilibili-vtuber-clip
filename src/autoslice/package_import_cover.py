"""Project a natively bound, unchanged cover's existing portable copies in memory."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


def project_preserved_cover_locators(
    record: dict[str, Any], publish: dict[str, Any], *, package_root: Path, candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replay source authority before allowing the ordinary relocation transaction.

    Raw source documents stay untouched. The import journal records their actual
    before-images, while the existing relocation only rewrites approved locators.
    """
    generation = publish.get("cover_generation")
    verification = (
        generation.get("final_host_identity_verification")
        if isinstance(generation, dict)
        else None
    )
    if isinstance(verification, dict):
        from src.autoslice.cover_host_identity_gate import HOST_ONLY_SCHEMA_VERSION

        if verification.get("schema_version") == HOST_ONLY_SCHEMA_VERSION:
            from src.autoslice.package_import_v4_cover import project_v4_cover_locators

            record, publish = project_v4_cover_locators(
                record,
                publish,
                package_root=package_root,
                candidate_id=candidate_id,
            )
            generation = publish.get("cover_generation")
    if isinstance(generation, dict) and generation.get("method") == "image_gen.imagegen":
        from src.autoslice.package_import_builtin_imagegen import (
            project_builtin_imagegen_locators,
        )

        return project_builtin_imagegen_locators(
            record,
            publish,
            package_root=package_root,
            candidate_id=candidate_id,
        )
    if not isinstance(generation, dict) or "pixel_preserving_successor" not in generation:
        return record, publish
    from src.autoslice import package_import as pi

    try:
        source = Path(record["subtitle_path"]).parent
        final = Path(generation["final_cover"])
        if final.is_relative_to(source):
            return record, publish  # Already portable; normal import gates still apply.
        return _project(record, publish, package_root, candidate_id, source)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise pi.PackageImportError(
            "SOURCE_ROOT_INCONSISTENT", "bound cover portability refused: " + str(exc),
        ) from exc


def _project(record: dict, publish: dict, physical: Path, cid: str, source: Path) -> tuple[dict, dict]:
    from src.autoslice import cover_repair as repair, package_import as pi
    from src.autoslice.package_relocation_contract import get_value, set_value, is_mutable_pointer

    generation = publish["cover_generation"]
    if (not source.is_absolute() or ".." in source.parts or source.name != "replacement_recuts"
            or source.parent.name != cid or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", cid) is None
            or source.parents[2].name != "out"
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", source.parents[1].name) is None):
        raise ValueError("source candidate/day namespace is invalid")
    date, base = source.parents[1].name, source.parents[3]
    frozen: dict[Path, bytes] = {}

    def read(path: Path) -> bytes:
        raw = pi.require_regular_file(path, label="bound cover portability").read_bytes()
        frozen[path] = raw
        return raw

    # A copied/staged package may still declare its live, hash-bound source.
    # Do not interpret unverified JSON as a source namespace capability.
    if (json.loads(read(source / f"{cid}.record.json")) != record
            or json.loads(read(source / f"{cid}.recut.publish.json")) != publish):
        raise ValueError("source documents differ from the imported raw documents")
    pointer = record.get("cover_repair_binding")
    if not isinstance(pointer, dict) or pointer != publish.get("cover_repair_binding"):
        raise ValueError("source binding mirrors disagree")
    binding_path = Path(str(pointer.get("path") or ""))
    if not binding_path.is_relative_to(source.parent / "cover_repair/generations"):
        raise ValueError("cover binding is outside this candidate")
    binding_raw = read(binding_path)
    if pointer.get("sha256") != "sha256:" + pi.sha256_bytes(binding_raw):
        raise ValueError("source binding hash drift")
    binding = json.loads(binding_raw)
    media = Path(str(record.get("burned_preview", {}).get("path") or ""))
    if media.parent != source or not media.name.startswith(cid + ".recut."):
        raise ValueError("bound source media is not package-local")
    cover = Path(str(binding.get("cover_path") or ""))
    if cover.parent != source or cover != media.with_suffix(".cover.png"):
        raise ValueError("native binding does not target the package same-stem cover")
    rec = {
        "candidate_id": cid, "title": publish.get("title"),
        "cover_path": str(cover), "cover_sha256": binding.get("cover_sha256"),
        "cover_generation": generation,
        "cover_generation_path": binding.get("generation_manifest_path"),
        "cover_generation_sha256": binding.get("generation_manifest_sha256"),
        "cover_binding_path": str(binding_path), "cover_binding_sha256": pointer["sha256"],
    }
    if (binding.get("authority_type") != "talk_delivery_record"
            or not repair._cover_binding_valid(date, rec, media, cover, runtime_root=base)):
        raise ValueError("native source cover/media binding does not replay")
    manifest = json.loads(read(source / "review_manifest.json"))
    items = manifest.get("items")
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or items[0].get("candidate_id") != cid or items[0].get("title") != publish.get("title")):
        raise ValueError("source manifest identity is not exact")
    item = items[0]

    def alias(relative: object, expected: object, original: object) -> str:
        rel = Path(str(relative or ""))
        if rel.is_absolute() or ".." in rel.parts or len(rel.parts) != 2:
            raise ValueError("portable cover alias has unsafe role/path")
        if rel.parts[0] not in {"covers", "covers_ai_original", "cover_refs"}:
            raise ValueError("portable cover alias is outside cover artifact roles")
        digest = pi.declared_digest(expected, label="bound cover artifact")
        if (pi.sha256_bytes(read(Path(str(original)))) != digest
                or pi.sha256_bytes(read(source / rel)) != digest
                or pi.sha256_bytes(read(physical / rel)) != digest):
            raise ValueError("portable cover alias differs from original bytes")
        return str(source / rel)

    fields = {
        ("final_cover",): ("covers/" + Path(generation["final_cover"]).name,
                           generation["final_cover_sha256"], generation["final_cover"]),
        ("pre_overlay_path",): (item["cover_pre_overlay"], generation["pre_overlay_sha256"],
                                generation["pre_overlay_path"]),
        ("ai_background",): (item["cover_route_background"], generation["ai_background_sha256"],
                             generation["ai_background"]),
        ("rendered_text_pixels", "mask_path"): (
            item["cover_title_mask"], generation["rendered_text_pixels"]["mask_sha256"],
            generation["rendered_text_pixels"]["mask_path"]),
        ("rendered_text_pixels", "pre_overlay_path"): (
            item["cover_pre_overlay"], generation["pre_overlay_sha256"], generation["pre_overlay_path"]),
        ("reference_image",): ("cover_refs/" + Path(generation["reference_image"]).name,
                                generation["reference_sha256"], generation["reference_image"]),
    }
    new_generation = copy.deepcopy(generation)
    for pointer, (rel, digest, original) in fields.items():
        if get_value(generation, pointer) is not None:
            set_value(new_generation, pointer, alias(rel, digest, original))
    after_record, after_publish = copy.deepcopy(record), copy.deepcopy(publish)
    after_publish["cover_generation"] = new_generation
    after_publish["cover_path"] = new_generation["final_cover"]
    after_record["publish_staging"]["cover_generation"] = copy.deepcopy(new_generation)
    after_record["publish_staging"]["cover_path"] = new_generation["final_cover"]
    for kind, old, new in (("record", record, after_record), ("publish", publish, after_publish)):
        if any(not is_mutable_pointer(kind, p) for p in pi._changed_pointers(old, new)):
            raise ValueError("cover portability changed non-locator authority")
    if any(pi.require_regular_file(p, label="bound cover recheck").read_bytes() != raw
           for p, raw in frozen.items()):
        raise ValueError("bound cover source changed during projection")
    return after_record, after_publish
