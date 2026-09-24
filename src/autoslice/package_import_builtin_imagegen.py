"""Project hash-bound builtin image_gen package locators without changing evidence.

The review package already contains the original tool transcript, prompt, input
references, generated pixels and final cover.  External-package import may only
rebind runtime locators to those package-contained bytes; it must not regenerate
artwork, reinterpret an undisclosed model as CPA, or rewrite the frozen source
manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.builtin_imagegen_cover import (
    METHOD,
    _read_source,
    validate_builtin_imagegen_provenance,
)
from src.autoslice.package_relocation_contract import (
    is_mutable_pointer,
    pointer_text,
)
from src.autoslice.review_package_portable_evidence import (
    contained_package_artifact,
    portable_item_artifact_path,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _review_manifest(
    package_root: Path, candidate_id: str
) -> tuple[Path, bytes, Mapping[str, Any]]:
    candidates: list[tuple[Path, bytes, Mapping[str, Any]]] = []
    for name in ("review_manifest.json", "manifest.json"):
        path = package_root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{name} is not a regular package file")
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError(f"{name} is not an object")
        items = document.get("items")
        matches = [
            item
            for item in items or []
            if isinstance(item, Mapping)
            and str(item.get("candidate_id") or item.get("id") or "") == candidate_id
        ]
        if len(matches) != 1:
            raise ValueError(f"{name} does not contain one exact candidate item")
        candidates.append((path, raw, matches[0]))
    if not candidates:
        raise ValueError("review manifest is missing")
    if len(candidates) > 1 and candidates[0][1] != candidates[1][1]:
        raise ValueError("review_manifest.json and manifest.json disagree")
    return candidates[0]


def _declared_package_root(record: Mapping[str, Any], candidate_id: str) -> Path:
    subtitle = Path(str(record.get("subtitle_path") or ""))
    package_root = subtitle.parent
    if (
        not subtitle.is_absolute()
        or ".." in subtitle.parts
        or subtitle.name != f"{candidate_id}.recut.srt"
        or package_root.name != "replacement_recuts"
        or package_root.parent.name != candidate_id
        or re.fullmatch(r"[A-Za-z0-9_-]{1,96}", candidate_id) is None
    ):
        raise ValueError("declared builtin image_gen package namespace is invalid")
    return package_root


def _declared_path(physical: Path, *, physical_root: Path, declared_root: Path) -> str:
    resolved_root = physical_root.resolve(strict=True)
    resolved = physical.resolve(strict=True)
    relative = resolved.relative_to(resolved_root)
    return str(declared_root / relative)


def builtin_imagegen_manifest_fields(
    generation: Mapping[str, Any], *, package_root: Path
) -> dict[str, str]:
    """Bind the rebuilt manifest to an existing package-contained source receipt."""

    if generation.get("method") != METHOD:
        return {}
    declared = generation.get("builtin_imagegen_provenance_path")
    expected = str(
        generation.get("builtin_imagegen_provenance_sha256") or ""
    ).removeprefix("sha256:")
    if not isinstance(declared, str) or not declared or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("builtin image_gen provenance path/hash is missing")

    root = package_root.resolve(strict=True)
    raw = Path(declared)
    candidates: list[str] = []
    if raw.is_absolute():
        try:
            candidates.append(raw.resolve(strict=True).relative_to(root).as_posix())
        except (OSError, ValueError):
            pass
    else:
        candidates.append(raw.as_posix())
    candidates.append((Path("builtin_imagegen_source") / raw.name).as_posix())

    for relative in dict.fromkeys(candidates):
        try:
            provenance = contained_package_artifact(
                root, relative, label="builtin image_gen provenance"
            )
        except ValueError:
            continue
        if _sha256(provenance) == expected:
            return {"cover_builtin_provenance": provenance.relative_to(root).as_posix()}
    raise ValueError("package builtin image_gen provenance is missing or hash-mismatched")


def project_builtin_imagegen_locators(
    record: dict[str, Any],
    publish: dict[str, Any],
    *,
    package_root: Path,
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return in-memory locator successors bound to package-contained evidence."""

    from src.autoslice import package_import as pi

    try:
        generation = publish.get("cover_generation")
        staging = record.get("publish_staging")
        staged_generation = (
            staging.get("cover_generation") if isinstance(staging, Mapping) else None
        )
        if not isinstance(generation, Mapping) or generation.get("method") != METHOD:
            return record, publish
        if "builtin_imagegen_provenance_path" not in generation:
            raise ValueError("builtin image_gen provenance locator is missing")
        if not isinstance(staging, Mapping) or staged_generation != generation:
            raise ValueError("record/publish builtin image_gen mirrors disagree")

        declared_root = _declared_package_root(record, candidate_id)
        manifest_path, manifest_raw, item = _review_manifest(package_root, candidate_id)
        provenance = portable_item_artifact_path(package_root, item, "cover_builtin_provenance")
        cover = portable_item_artifact_path(package_root, item, "cover")
        route_background = portable_item_artifact_path(package_root, item, "cover_route_background")
        if provenance is None or cover is None or route_background is None:
            raise ValueError("package builtin image_gen artifact mapping is incomplete")

        declared_provenance = str(
            generation.get("builtin_imagegen_provenance_sha256") or ""
        ).removeprefix("sha256:")
        if _sha256(provenance) != declared_provenance:
            raise ValueError("builtin image_gen provenance hash drift")
        source, assets = _read_source(provenance)
        reference = provenance.parent / str(source["identity_reference"]["path"])
        source_background = provenance.parent / str(source["background"]["path"])
        source_final = provenance.parent / str(source["final_cover"]["path"])

        physical_generation = copy.deepcopy(dict(generation))
        physical_generation.update(
            {
                "builtin_imagegen_provenance_path": str(provenance),
                "reference_image": str(reference),
                "ai_background": str(route_background),
                "final_cover": str(cover),
            }
        )
        if not validate_builtin_imagegen_provenance(physical_generation):
            raise ValueError("package-contained builtin image_gen provenance is invalid")
        for label, left, right in (
            ("background", route_background, source_background),
            ("final cover", cover, source_final),
        ):
            if _sha256(left) != _sha256(right):
                raise ValueError(f"package {label} alias differs from provenance bytes")

        frozen_paths = list(
            dict.fromkeys([manifest_path, provenance, reference, route_background, cover, *assets])
        )
        frozen = {path: path.read_bytes() for path in frozen_paths}

        successor = copy.deepcopy(dict(generation))
        successor.update(
            {
                "builtin_imagegen_provenance_path": _declared_path(
                    provenance,
                    physical_root=package_root,
                    declared_root=declared_root,
                ),
                "reference_image": _declared_path(
                    reference,
                    physical_root=package_root,
                    declared_root=declared_root,
                ),
                "ai_background": _declared_path(
                    route_background,
                    physical_root=package_root,
                    declared_root=declared_root,
                ),
                "final_cover": _declared_path(
                    cover,
                    physical_root=package_root,
                    declared_root=declared_root,
                ),
            }
        )
        new_publish = copy.deepcopy(publish)
        new_publish["cover_generation"] = successor
        new_publish["cover_path"] = successor["final_cover"]
        new_record = copy.deepcopy(record)
        new_record["publish_staging"]["cover_generation"] = copy.deepcopy(successor)
        new_record["publish_staging"]["cover_path"] = successor["final_cover"]

        for kind, before, after in (
            ("record", record, new_record),
            ("publish", publish, new_publish),
        ):
            invalid = sorted(
                pointer_text(pointer)
                for pointer in pi._changed_pointers(before, after)
                if not is_mutable_pointer(kind, pointer)
            )
            if invalid:
                raise ValueError(
                    "builtin image_gen projection changed immutable fields: " + ",".join(invalid)
                )
        for path, raw in frozen.items():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != raw:
                raise ValueError("builtin image_gen package evidence changed during projection")
        if manifest_path.read_bytes() != manifest_raw:
            raise ValueError("review manifest changed during builtin image_gen projection")
        return new_record, new_publish
    except pi.PackageImportError as exc:
        raise pi.PackageImportError(
            "BUILTIN_IMAGEGEN_LOCATOR_INCONSISTENT",
            f"hash-bound builtin image_gen locator projection refused: {exc}",
        ) from exc
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise pi.PackageImportError(
            "BUILTIN_IMAGEGEN_LOCATOR_INCONSISTENT",
            f"hash-bound builtin image_gen locator projection refused: {exc}",
        ) from exc
