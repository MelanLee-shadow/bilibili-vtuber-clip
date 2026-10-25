"""Project only the current candidate-cover schema's package file locators."""

from __future__ import annotations
import copy
import json
from pathlib import Path
from collections.abc import Mapping

from .title_cover_candidates import strict_file, sha
from .title_cover_package import package_choice_problems


def candidate_item(package_root: Path, candidate_id: str) -> dict:
    review = json.loads(strict_file(package_root, "review_manifest.json").read_text())
    items = [
        item
        for item in review.get("items", [])
        if isinstance(item, Mapping)
        and (item.get("candidate_id") or item.get("id")) == candidate_id
    ]
    if len(items) != 1:
        raise ValueError("COVER_CHOICE_PACKAGE_CANDIDATE_AMBIGUOUS")
    return dict(items[0])


def project_candidate_cover_locators(
    record: dict, publish: dict, *, package_root: Path, candidate_id: str
) -> tuple[dict, dict]:
    generation = publish["cover_generation"]
    if (record.get("publish_staging") or {}).get("cover_generation") != generation:
        raise ValueError("COVER_CHOICE_RECORD_PUBLISH_DRIFT")
    item = candidate_item(package_root, candidate_id)
    problems = package_choice_problems(package_root, item, generation)
    if problems:
        raise ValueError("COVER_CHOICE_IMPORT_REJECTED: " + "; ".join(problems))
    bundle = Path(item["cover_choice"]).parent
    payload = json.loads(strict_file(package_root, item["cover_choice"]).read_text())
    selected = next(row for row in payload["candidates"] if row["id"] == payload["selected"])
    source = Path(str(record.get("subtitle_path") or "")).parent
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("COVER_CHOICE_SOURCE_NAMESPACE_INVALID")
    sources = json.loads(strict_file(package_root, str(bundle / "sources.json")).read_text())

    def located(relative):
        # Verify the physical package file, then express its source role.
        strict_file(package_root, str(relative))
        return str(source / relative)

    after = copy.deepcopy(generation)
    after["candidate_selection"]["choose_path"] = located(item["cover_choice"])
    after["final_cover"] = located(item["cover"])
    after["reference_image"] = located(bundle / sources["frame"]["file"])
    after["pre_overlay_path"] = located(bundle / selected["pre_overlay_path"])
    after["ai_background"] = located(bundle / selected["background_path"])
    after["rendered_text_pixels"]["mask_path"] = located(bundle / selected["mask_path"])
    after["rendered_text_pixels"]["pre_overlay_path"] = after["pre_overlay_path"]
    after["request_path"] = located(bundle / "candidates" / selected["id"] / "score.request.json")
    after["response_path"] = located(bundle / selected["witness_path"])
    if after["request_path"] == after["response_path"]:
        raise ValueError("COVER_CHOICE_NETWORK_ROLE_COLLISION")
    record_after, publish_after = copy.deepcopy(record), copy.deepcopy(publish)
    publish_after["cover_generation"] = after
    publish_after["cover_path"] = after["final_cover"]
    record_after["publish_staging"]["cover_generation"] = copy.deepcopy(after)
    record_after["publish_staging"]["cover_path"] = after["final_cover"]
    from .package_relocation_contract import is_mutable_pointer
    from .package_import import _changed_pointers

    for kind, old, new in (("record", record, record_after), ("publish", publish, publish_after)):
        if any(not is_mutable_pointer(kind, p, old) for p in _changed_pointers(old, new)):
            raise ValueError("COVER_CHOICE_IMPORT_CHANGED_IMMUTABLE_EVIDENCE")
    return record_after, publish_after


def package_candidate_cover(package_root: Path, generation: Mapping) -> Path:
    binding = generation["candidate_selection"]
    item = candidate_item(package_root, binding["candidate_id"])
    final = strict_file(package_root, item["cover"])
    if sha(final) != generation["final_cover_sha256"]:
        raise ValueError("COVER_CHOICE_IMPORTED_FINAL_HASH_DRIFT")
    problems = package_choice_problems(package_root, item, generation)
    if problems:
        raise ValueError("; ".join(problems))
    return final
