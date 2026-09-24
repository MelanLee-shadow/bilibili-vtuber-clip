"""Project bound package-internal runtime locators without mutating source bytes.

Some review-package assemblers copy final publish/context evidence into the
package but leave mutable locator fields naming a transient assembly directory.
The ordinary importer has one package/candidate/repo source namespace; those
stale labels would otherwise make a byte-complete package impossible to move.
This module intervenes only when one of those approved locators actually escapes
the declared package/candidate roots. Normal packages retain the established
validation and error ordering.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.package_relocation_contract import is_mutable_pointer


def project_package_internal_locators(
    record: dict[str, Any],
    publish: dict[str, Any],
    *,
    package_root: Path,
    candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Normalize escaped publish/chat/context locators into one source namespace.

    Projection is permitted only after the corresponding physical package (or
    candidate-root) copy is a regular file and matches its declared SHA-256.
    Raw JSON files and frozen evidence remain byte-for-byte unchanged.
    """

    from src.autoslice import package_import as pi

    frozen: dict[Path, bytes] = {}
    try:
        subtitle = Path(str(record.get("subtitle_path") or ""))
        source_package = subtitle.parent
        source_candidate = source_package.parent
        if (
            not subtitle.is_absolute()
            or ".." in subtitle.parts
            or subtitle.name != f"{candidate_id}.recut.srt"
            or source_package.name != "replacement_recuts"
            or source_candidate.name != candidate_id
        ):
            raise ValueError("declared source package namespace is invalid")

        artifact_hashes = record.get("artifact_hashes")
        if not isinstance(artifact_hashes, Mapping):
            raise ValueError("record.artifact_hashes is not an object")

        def read_bound(
            path: Path,
            *,
            digest_value: object,
            label: str,
        ) -> bytes:
            raw = pi.require_regular_file(path, label=label).read_bytes()
            expected = pi.declared_digest(digest_value, label=label)
            if pi.sha256_bytes(raw) != expected:
                raise ValueError(f"{label} hash drift")
            frozen[path] = raw
            return raw

        def contained_evidence(
            name: str,
            *,
            digest_key: str,
            label: str,
        ) -> tuple[str, Path]:
            package_path = package_root / name
            candidate_path = package_root.parent / name
            # Package-local evidence wins. If it exists but is unsafe or drifts,
            # fail rather than silently selecting a different same-named file.
            if package_path.exists() or package_path.is_symlink():
                read_bound(
                    package_path,
                    digest_value=artifact_hashes.get(digest_key),
                    label=label,
                )
                return "package", package_path
            read_bound(
                candidate_path,
                digest_value=artifact_hashes.get(digest_key),
                label=label,
            )
            return "candidate", candidate_path

        new_record = copy.deepcopy(record)
        new_publish = copy.deepcopy(publish)
        publish_pointer = new_record.get("publish_staging")
        if not isinstance(publish_pointer, dict):
            raise ValueError("record.publish_staging is not an object")

        publish_name = f"{candidate_id}.recut.publish.json"
        current_publish = Path(str(publish_pointer.get("publish_json_path") or ""))
        if (
            not current_publish.is_absolute()
            or ".." in current_publish.parts
            or current_publish.name != publish_name
        ):
            raise ValueError("publish_json_path is not a safe bound draft locator")
        expected_publish = source_package / publish_name
        if current_publish != expected_publish:
            # Only escaped runtime locators need this additional package-byte
            # proof. Normal packages preserve the older validation ordering.
            read_bound(
                package_root / publish_name,
                digest_value=artifact_hashes.get("publish_draft_sha256"),
                label="package publish draft",
            )
            publish_pointer["publish_json_path"] = str(expected_publish)

        for pointer, name, digest_key, label in (
            (
                "chat_authority_audit_path",
                f"{candidate_id}.chat-authority.json",
                "chat_authority_audit_sha256",
                "package chat authority",
            ),
            (
                "clip_context_path",
                f"{candidate_id}.clip-context.json",
                "clip_context_file_sha256",
                "package clip context",
            ),
        ):
            current = Path(str(new_record.get(pointer) or ""))
            if not current.is_absolute() or ".." in current.parts or current.name != name:
                raise ValueError(f"{pointer} is not a safe candidate-bound locator")
            if current in (source_package / name, source_candidate / name):
                continue
            role, _physical = contained_evidence(name, digest_key=digest_key, label=label)
            root = source_package if role == "package" else source_candidate
            new_record[pointer] = str(root / name)

        changed = pi._changed_pointers(record, new_record)
        if not changed:
            return record, publish
        invalid = sorted(
            pi.pointer_text(pointer)
            for pointer in changed
            if not is_mutable_pointer("record", pointer)
        )
        if invalid:
            raise ValueError(
                "package-internal projection changed immutable fields: " + ",".join(invalid)
            )
        if new_publish != publish:
            raise ValueError("package-internal projection changed publish authority")
        for path, raw in frozen.items():
            if (
                pi.require_regular_file(path, label="package internal final readback").read_bytes()
                != raw
            ):
                raise ValueError("package-internal source changed during projection")
        return new_record, new_publish
    except pi.PackageImportError as exc:
        raise pi.PackageImportError(
            "PACKAGE_INTERNAL_LOCATOR_INCONSISTENT",
            f"hash-bound package-internal locator projection refused: {exc}",
        ) from exc
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise pi.PackageImportError(
            "PACKAGE_INTERNAL_LOCATOR_INCONSISTENT",
            f"hash-bound package-internal locator projection refused: {exc}",
        ) from exc
