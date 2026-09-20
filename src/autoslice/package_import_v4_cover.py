"""Normalize only runtime cover locators from an existing package-bound V4 proof.

The package manifest, canonical witness and actual image bytes are the authority;
producer paths are historical labels, never filesystem capabilities. No provider,
rendering, source mutation or production-state operation is performed here.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from src.autoslice.cover_host_identity_gate import validate_final_host_identity_verification
from src.autoslice.host_only_v4_package_binding import (
    BINDING_ITEM_KEY,
    parse_json_object,
    read_package_file_once,
    sha256_bytes,
    validate_package_binding,
)
from src.autoslice.package_relocation_contract import get_value, is_mutable_pointer, set_value


def project_v4_cover_locators(
    record: dict[str, Any], publish: dict[str, Any], *, package_root: Path, candidate_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project a witnessed identity-card cover onto its declared media namespace.

    This is a virtual source namespace used by the existing relocation transaction.
    Every projected suffix has been proven against a contained physical package
    file first. Raw records and the original V4 receipt remain unmodified.
    """
    from src.autoslice import package_import as pi

    try:
        source_file = Path(str(record.get("subtitle_path") or ""))
        source = source_file.parent
        if (
            not source.is_absolute()
            or ".." in source.parts
            or source.name != "replacement_recuts"
            or source.parent.name != candidate_id
            or source_file.name != candidate_id + ".recut.srt"
        ):
            raise ValueError("V4 source media namespace is invalid")
        generation = publish["cover_generation"]
        final = Path(str(generation.get("final_cover") or ""))
        if final.is_relative_to(source):
            return record, publish  # Already normalized: ordinary gates remain active.
        frozen: dict[str, bytes] = {}

        def read(relative: object, label: str) -> tuple[Path, bytes]:
            rel, raw = read_package_file_once(package_root, relative, label=label)
            if rel.as_posix() in frozen and frozen[rel.as_posix()] != raw:
                raise ValueError("V4 source changed during normalization")
            frozen[rel.as_posix()] = raw
            return rel, raw

        _, raw_manifest = read("review_manifest.json", "V4 import manifest")
        manifest = parse_json_object(raw_manifest, label="V4 import manifest")
        items = manifest.get("items")
        if (
            not isinstance(items, list)
            or len(items) != 1
            or not isinstance(items[0], dict)
            or items[0].get("candidate_id") != candidate_id
        ):
            raise ValueError("V4 import manifest candidate is absent or ambiguous")
        item = items[0]
        documents = {}
        for kind in ("evidence_json", "record", "publish_json"):
            _, data = read(item.get(kind), "V4 import " + kind)
            documents[kind] = parse_json_object(data, label=kind)
            if kind in ("evidence_json", "publish_json"):
                declared = str(item.get("sha256", {}).get(kind, "")).removeprefix("sha256:")
                if sha256_bytes(data).removeprefix("sha256:") != declared:
                    raise ValueError("V4 manifest document hash drift: " + kind)
        if documents["evidence_json"] != record or documents["publish_json"] != publish:
            raise ValueError("V4 raw documents disagree with import input")
        if documents["record"].get("publish_staging", {}).get("cover_generation") != generation:
            raise ValueError("V4 delivery record generation disagrees")
        if record.get("publish_staging", {}).get("cover_generation") != generation:
            raise ValueError("V4 evidence record generation disagrees")
        if item.get("title") != publish.get("title"):
            raise ValueError("V4 title binding disagrees")
        if not validate_final_host_identity_verification(generation):
            raise ValueError("V4 identity verdict is not valid")
        validate_package_binding(root=package_root, item=item, generation=generation)
        binding = item[BINDING_ITEM_KEY]
        for key in ("receipt", "comparison", "reference", "final_cover"):
            _, data = read(binding[key + "_path"], "V4 bound " + key)
            if sha256_bytes(data) != binding[key + "_sha256"]:
                raise ValueError("V4 bound image/receipt drift: " + key)
        pixels = generation["rendered_text_pixels"]
        roles = {
            ("final_cover",): (item["cover"], generation["final_cover_sha256"]),
            ("pre_overlay_path",): (item["cover_pre_overlay"], generation["pre_overlay_sha256"]),
            ("ai_background",): (
                item["cover_route_background"],
                generation["ai_background_sha256"],
            ),
            ("reference_image",): (binding["reference_path"], generation["reference_sha256"]),
            ("rendered_text_pixels", "mask_path"): (
                item["cover_title_mask"],
                pixels["mask_sha256"],
            ),
            ("rendered_text_pixels", "pre_overlay_path"): (
                item["cover_pre_overlay"],
                generation["pre_overlay_sha256"],
            ),
        }
        new_generation = copy.deepcopy(generation)
        for pointer, (relative, expected) in roles.items():
            rel, data = read(relative, "V4 cover locator " + "/".join(pointer))
            if sha256_bytes(data) != expected:
                raise ValueError("V4 cover alias bytes drift: " + "/".join(pointer))
            if get_value(generation, pointer) is not None:
                set_value(new_generation, pointer, str(source / rel))
        new_record, new_publish = copy.deepcopy(record), copy.deepcopy(publish)
        new_publish["cover_generation"] = new_generation
        new_publish["cover_path"] = new_generation["final_cover"]
        new_record["publish_staging"]["cover_generation"] = copy.deepcopy(new_generation)
        new_record["publish_staging"]["cover_path"] = new_generation["final_cover"]
        for kind, old, new in (("record", record, new_record), ("publish", publish, new_publish)):
            if any(not is_mutable_pointer(kind, ptr) for ptr in pi._changed_pointers(old, new)):
                raise ValueError("V4 normalization changed immutable evidence")
        for relative, data in frozen.items():
            if read_package_file_once(package_root, relative, label="V4 final readback")[1] != data:
                raise ValueError("V4 package changed during normalization")
        return new_record, new_publish
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise pi.PackageImportError(
            "SOURCE_ROOT_INCONSISTENT", "V4 cover portability refused: " + str(exc)
        ) from exc
