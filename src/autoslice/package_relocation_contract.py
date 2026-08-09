"""Field-level locator and immutability contract for package relocation."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any


class PackageRelocationError(RuntimeError):
    """The package could not be relocated without weakening its bindings."""


JsonPointer = tuple[str, ...]
ROOT_ROLES = ("package", "candidate", "repo")

RECORD_PATH_POINTERS: frozenset[JsonPointer] = frozenset(
    {
        ("media_path",),
        ("subtitle_path",),
        ("chat_authority_audit_path",),
        ("clip_context_path",),
        ("redelivery_baseline_audit_path",),
        ("subtitle_regression_audit_path",),
        ("talk_filler_audit_path",),
        ("text_finalization_manifest_path",),
        ("speaker_review_srt_path",),
        ("subtitle_ass_path",),
        ("speaker_finalization_manifest_path",),
        ("burned_preview", "path"),
        ("burned_preview", "ass_path"),
        ("publish_staging", "cover_path"),
        ("publish_staging", "publish_json_path"),
        ("publish_staging", "cover_generation", "final_cover"),
        ("publish_staging", "cover_generation", "pre_overlay_path"),
        ("publish_staging", "cover_generation", "ai_background"),
        ("publish_staging", "cover_generation", "reference_image"),
        (
            "publish_staging",
            "cover_generation",
            "rendered_text_pixels",
            "mask_path",
        ),
        (
            "publish_staging",
            "cover_generation",
            "rendered_text_pixels",
            "pre_overlay_path",
        ),
    }
)
PUBLISH_PATH_POINTERS: frozenset[JsonPointer] = frozenset(
    {
        ("video_path",),
        ("cover_path",),
        ("cover_generation", "final_cover"),
        ("cover_generation", "pre_overlay_path"),
        ("cover_generation", "ai_background"),
        ("cover_generation", "reference_image"),
        ("cover_generation", "rendered_text_pixels", "mask_path"),
        ("cover_generation", "rendered_text_pixels", "pre_overlay_path"),
    }
)
SPEAKER_PATH_KEYS = frozenset(
    {
        "source_media",
        "text_final_srt",
        "profile",
        "speaker_override",
        "source_session_anchor_manifest",
        "mixed_overlap_evidence",
        "output_review_srt",
        "output_ass",
    }
)
SPEAKER_PATH_POINTERS: frozenset[JsonPointer] = frozenset(
    {(key,) for key in SPEAKER_PATH_KEYS}
)

# Same-hash artifacts are still not interchangeable across physical roles.
_RECORD_ROOT_ROLES: Mapping[JsonPointer, frozenset[str]] = {
    **{pointer: frozenset({"package"}) for pointer in RECORD_PATH_POINTERS},
    ("chat_authority_audit_path",): frozenset({"package", "candidate"}),
    ("clip_context_path",): frozenset({"package", "candidate"}),
    ("talk_filler_audit_path",): frozenset({"candidate"}),
}
_PUBLISH_ROOT_ROLES: Mapping[JsonPointer, frozenset[str]] = {
    pointer: frozenset({"package"}) for pointer in PUBLISH_PATH_POINTERS
}
_SPEAKER_ROOT_ROLES: Mapping[JsonPointer, frozenset[str]] = {
    ("source_media",): frozenset({"package"}),
    ("text_final_srt",): frozenset({"package"}),
    ("profile",): frozenset({"repo"}),
    ("speaker_override",): frozenset({"candidate", "repo"}),
    ("source_session_anchor_manifest",): frozenset({"candidate", "repo"}),
    ("mixed_overlap_evidence",): frozenset({"candidate", "repo"}),
    ("output_review_srt",): frozenset({"package"}),
    ("output_ass",): frozenset({"package"}),
}

_RECORD_FROZEN_PREFIXES: tuple[JsonPointer, ...] = (
    ("burned_preview", "command"),
    ("burned_preview", "branding_intro"),
    ("redelivery_baseline",),
    ("subtitle_regression",),
    ("boundary_audit",),
    ("story_contract",),
    ("selection_scorecard",),
    ("subtitle_timing_qa",),
    ("upload_tags",),
    ("publish_staging", "cover_generation"),
    ("publish_staging", "source_fact_review"),
    ("publish_staging", "title_story_audit"),
    ("publish_staging", "manual_title_repair_authority_consumption"),
    ("publish_staging", "recovery_publication_authority"),
)
_PUBLISH_FROZEN_PREFIXES: tuple[JsonPointer, ...] = (
    ("cover_generation",),
    ("source_fact_review",),
    ("title_story_audit",),
    ("manual_title_repair_authority_consumption",),
    ("recovery_publication_authority",),
)
_SPEAKER_FROZEN_PREFIXES: tuple[JsonPointer, ...] = (
    ("analysis",),
    ("final_decisions",),
    ("ephemeral_runtime_paths",),
    ("runtime_host",),
)


def pointer_text(pointer: JsonPointer) -> str:
    return "/" + "/".join(
        value.replace("~", "~0").replace("/", "~1") for value in pointer
    )


def parse_pointer(value: object, *, label: str) -> JsonPointer:
    if not isinstance(value, str) or not value.startswith("/"):
        raise PackageRelocationError(f"{label}: invalid JSON pointer")
    parts = []
    for raw in value[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise PackageRelocationError(f"{label}: invalid JSON pointer")
        parts.append(raw.replace("~1", "/").replace("~0", "~"))
    pointer = tuple(parts)
    if pointer_text(pointer) != value:
        raise PackageRelocationError(f"{label}: non-canonical JSON pointer")
    return pointer


def get_value(document: Mapping[str, Any], pointer: JsonPointer) -> object:
    value: object = document
    for key in pointer:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return value


def set_value(
    document: dict[str, Any], pointer: JsonPointer, value: object
) -> None:
    parent: object = document
    for key in pointer[:-1]:
        if not isinstance(parent, dict) or key not in parent:
            raise PackageRelocationError(
                f"missing relocation parent: {pointer_text(pointer)}"
            )
        parent = parent[key]
    if not isinstance(parent, dict) or pointer[-1] not in parent:
        raise PackageRelocationError(
            f"missing relocation pointer: {pointer_text(pointer)}"
        )
    parent[pointer[-1]] = value


def _walk_strings(
    value: object, pointer: JsonPointer = ()
) -> Iterator[tuple[JsonPointer, str]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk_strings(child, (*pointer, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, (*pointer, str(index)))
    elif isinstance(value, str):
        yield pointer, value


def speaker_record_pointer(pointer: JsonPointer) -> bool:
    return (
        len(pointer) == 2
        and pointer[0] == "speaker_finalization"
        and (pointer[1],) in SPEAKER_PATH_POINTERS
    )


def is_mutable_pointer(kind: str, pointer: JsonPointer) -> bool:
    if kind == "record":
        return pointer in RECORD_PATH_POINTERS or speaker_record_pointer(pointer)
    if kind == "publish":
        return pointer in PUBLISH_PATH_POINTERS
    return pointer in SPEAKER_PATH_POINTERS


def _under(pointer: JsonPointer, prefix: JsonPointer) -> bool:
    return pointer[: len(prefix)] == prefix


def _is_frozen_pointer(kind: str, pointer: JsonPointer) -> bool:
    if kind == "record" and pointer[:1] == ("speaker_finalization",):
        nested = pointer[1:]
        if nested and not speaker_record_pointer(pointer):
            return any(_under(nested, prefix) for prefix in _SPEAKER_FROZEN_PREFIXES)
    prefixes = {
        "record": _RECORD_FROZEN_PREFIXES,
        "publish": _PUBLISH_FROZEN_PREFIXES,
        "speaker": _SPEAKER_FROZEN_PREFIXES,
    }[kind]
    return not is_mutable_pointer(kind, pointer) and any(
        _under(pointer, prefix) for prefix in prefixes
    )


def reject_unknown_wsl_paths(
    document: Mapping[str, Any], *, kind: str, source_workspace_root: str
) -> None:
    marker = source_workspace_root.rstrip("/") + "/"
    for pointer, value in _walk_strings(document):
        if marker not in value and value != source_workspace_root.rstrip("/"):
            continue
        if is_mutable_pointer(kind, pointer) or _is_frozen_pointer(kind, pointer):
            continue
        raise PackageRelocationError(
            f"{kind}: unknown WSL path at {pointer_text(pointer)}"
        )


def _pointer_root_roles(kind: str, pointer: JsonPointer) -> frozenset[str]:
    if kind == "record" and speaker_record_pointer(pointer):
        roles = _SPEAKER_ROOT_ROLES.get(pointer[1:])
    else:
        roles = {
            "record": _RECORD_ROOT_ROLES,
            "publish": _PUBLISH_ROOT_ROLES,
            "speaker": _SPEAKER_ROOT_ROLES,
        }[kind].get(pointer)
    if roles is None:
        raise PackageRelocationError(
            f"{kind}: missing root-role contract at {pointer_text(pointer)}"
        )
    return roles


def match_root_role(
    value: str, *, mappings: Sequence[tuple[str, str]]
) -> tuple[str, str, str] | None:
    """Return (role, source|destination, relative suffix), longest root first."""
    candidates: list[tuple[int, str, str, str]] = []
    for role, (source, destination) in zip(ROOT_ROLES, mappings, strict=True):
        for side, root in (("source", source), ("destination", destination)):
            if value == root or value.startswith(root + "/"):
                candidates.append((len(root), role, side, value[len(root) :]))
    if not candidates:
        return None
    longest = max(row[0] for row in candidates)
    winners = [row for row in candidates if row[0] == longest]
    if len({row[1:] for row in winners}) != 1:
        raise PackageRelocationError("ambiguous relocation root role")
    _length, role, side, suffix = winners[0]
    return role, side, suffix


def validate_path_root_role(
    value: object,
    *,
    kind: str,
    pointer: JsonPointer,
    mappings: Sequence[tuple[str, str]],
) -> tuple[str, str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("/"):
        raise PackageRelocationError(f"path is not absolute at {pointer_text(pointer)}")
    matched = match_root_role(value, mappings=mappings)
    if matched is None:
        raise PackageRelocationError(
            "path outside relocation roots (escapes trusted roots) at "
            f"{pointer_text(pointer)}"
        )
    role, _side, _suffix = matched
    allowed = _pointer_root_roles(kind, pointer)
    if role not in allowed:
        raise PackageRelocationError(
            f"{kind}: wrong root role at {pointer_text(pointer)}: "
            f"expected={','.join(sorted(allowed))} actual={role}"
        )
    return matched


def validate_document_root_roles(
    document: Mapping[str, Any],
    *,
    kind: str,
    mappings: Sequence[tuple[str, str]],
) -> None:
    pointers = {
        "record": RECORD_PATH_POINTERS,
        "publish": PUBLISH_PATH_POINTERS,
        "speaker": SPEAKER_PATH_POINTERS,
    }[kind]
    for pointer in pointers:
        validate_path_root_role(
            get_value(document, pointer),
            kind=kind,
            pointer=pointer,
            mappings=mappings,
        )
    if kind == "record":
        embedded = document.get("speaker_finalization")
        if isinstance(embedded, Mapping):
            for pointer in SPEAKER_PATH_POINTERS:
                validate_path_root_role(
                    get_value(embedded, pointer),
                    kind="record",
                    pointer=("speaker_finalization", *pointer),
                    mappings=mappings,
                )
