"""Candidate-scoped, hash-bound entity and surface projections.

Identity and spelling are deliberately different authorities here.  An opaque
``entity_id`` says that two observed strings refer to the same entity; it does
*not* authorize replacing one string with the other.  Every downstream surface
is selected explicitly for one candidate and one typed use.  Mention surfaces
are additionally cue-bound so a short form such as ``NT`` cannot be expanded
across the transcript merely because it is equivalent to ``南町nightin``.

The loader returns frozen dataclasses and a deterministic projection digest.
Callers must supply the candidate id, reviewed-SRT hash, source recording hash,
and absolute source interval they are consuming.  This prevents a reviewed
decision from drifting to another candidate, SRT, or source cut.  The strict
JSON shape intentionally has no global rewrite or ``canonical`` field.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.reviewed_subtitle_baseline_registry import (
    ReviewedSubtitleBaselineRegistryError,
    load_candidate_reviewed_subtitle_baseline,
)


SCHEMA_VERSION = "candidate-entity-surface-projection.v1"
SURFACE_TYPES = frozenset(
    {
        "official",
        "session",
        "mention",
        "reviewed",
        "speaker",
        "title_cover",
        "publication",
    }
)
_SINGLETON_SURFACE_TYPES = SURFACE_TYPES - {"mention"}
_ROOT_FIELDS = {
    "schema_version",
    "candidate_binding",
    "identity_equivalences",
    "surface_projections",
}
_BINDING_FIELDS = {
    "candidate_id",
    "reviewed_srt_sha256",
    "source_recording_basename",
    "source_sha256",
    "absolute_source_start_ms",
    "absolute_source_end_ms",
}
_IDENTITY_FIELDS = {"entity_id", "equivalent_surfaces"}
_PROJECTION_FIELDS = {"entity_id", "surface_type", "surface"}
_MENTION_PROJECTION_FIELDS = _PROJECTION_FIELDS | {"cue_index", "mention_id"}
_ENTITY_ID_RX = re.compile(r"^ent_[0-9a-f]{32}$")
_CANDIDATE_ID_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_SHA256_RX = re.compile(r"^[0-9a-f]{64}$")
_BASELINE_MANIFEST_REQUIRED_FIELDS = {
    "registry_schema_version",
    "candidate_id",
    "schema_version",
    "mode",
    "path",
    "sha256",
    "authority",
    "exact_interval_replay",
    "source_recording_basename",
    "source_sha256",
    "absolute_source_start_ms",
    "absolute_source_end_ms",
}
_BASELINE_MANIFEST_OPTIONAL_FIELDS = {
    "terminal_projection_mode",
    "truth_full_ownership",
}


class CandidateEntityProjectionError(ValueError):
    """A candidate entity projection is malformed or used outside its scope."""


@dataclass(frozen=True, slots=True)
class CandidateSrtBinding:
    candidate_id: str
    reviewed_srt_sha256: str
    source_recording_basename: str
    source_sha256: str
    absolute_source_start_ms: int
    absolute_source_end_ms: int


@dataclass(frozen=True, slots=True)
class FrozenIdentityEquivalence:
    entity_id: str
    equivalent_surfaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FrozenSurfaceProjection:
    entity_id: str
    surface_type: str
    surface: str
    cue_index: int | None = None
    mention_id: str | None = None


@dataclass(frozen=True, slots=True)
class FrozenCandidateEntityProjection:
    """Immutable candidate decisions for downstream subtitle/title consumers."""

    schema_version: str
    binding: CandidateSrtBinding
    identity_equivalences: tuple[FrozenIdentityEquivalence, ...]
    surface_projections: tuple[FrozenSurfaceProjection, ...]
    projection_sha256: str

    def _identity(self, entity_id: str) -> FrozenIdentityEquivalence:
        for identity in self.identity_equivalences:
            if identity.entity_id == entity_id:
                return identity
        raise CandidateEntityProjectionError(f"ENTITY_ID_UNKNOWN:{entity_id}")

    def projected_surface(
        self,
        *,
        entity_id: str,
        surface_type: str,
        cue_index: int | None = None,
        mention_id: str | None = None,
    ) -> str:
        """Return the one frozen surface for an exact downstream use.

        Mention lookup requires both cue and mention ids.  This keeps two
        aliases in the same cue distinguishable and prohibits transcript-wide
        alias expansion.
        """

        self._identity(entity_id)
        if surface_type not in SURFACE_TYPES:
            raise CandidateEntityProjectionError(
                f"SURFACE_TYPE_UNKNOWN:{surface_type}"
            )
        if surface_type == "mention":
            if cue_index is None or mention_id is None:
                raise CandidateEntityProjectionError("MENTION_SCOPE_REQUIRED")
        elif cue_index is not None or mention_id is not None:
            raise CandidateEntityProjectionError("NON_MENTION_SCOPE_FORBIDDEN")

        matches = [
            row
            for row in self.surface_projections
            if row.entity_id == entity_id
            and row.surface_type == surface_type
            and row.cue_index == cue_index
            and row.mention_id == mention_id
        ]
        if len(matches) != 1:
            raise CandidateEntityProjectionError(
                f"SURFACE_PROJECTION_NOT_UNIQUE:{entity_id}:{surface_type}"
            )
        return matches[0].surface

    def require_surface(
        self,
        *,
        entity_id: str,
        surface_type: str,
        actual_surface: str,
        cue_index: int | None = None,
        mention_id: str | None = None,
    ) -> str:
        """Fail closed unless ``actual_surface`` equals the frozen projection.

        Identity-equivalent alternatives are intentionally rejected.  Their
        equivalence establishes *who* was referenced, not *how* this candidate
        may spell the reference in a particular channel.
        """

        expected = self.projected_surface(
            entity_id=entity_id,
            surface_type=surface_type,
            cue_index=cue_index,
            mention_id=mention_id,
        )
        if actual_surface == expected:
            return expected
        identity = self._identity(entity_id)
        reason = (
            "IDENTITY_EQUIVALENT_SURFACE_NOT_PROJECTED"
            if actual_surface in identity.equivalent_surfaces
            else "UNRECOGNIZED_ENTITY_SURFACE"
        )
        raise CandidateEntityProjectionError(
            f"{reason}:{entity_id}:{surface_type}:expected={expected}:actual={actual_surface}"
        )

    def require_text_surfaces(self, *, surface_type: str, text: str) -> str:
        """Reject a named equivalent that differs from the frozen channel surface.

        Entities not named in ``text`` are irrelevant to this artifact.  A shorter
        equivalent fully contained in the projected surface is ignored, so the
        valid full surface ``南町nightin`` is not rejected merely because it also
        contains ``南町``.  Latin aliases use ASCII-token boundaries to avoid
        matching common substrings inside an unrelated word.
        """

        if surface_type not in _SINGLETON_SURFACE_TYPES:
            raise CandidateEntityProjectionError(
                f"TEXT_SURFACE_TYPE_UNSUPPORTED:{surface_type}"
            )
        if not isinstance(text, str) or not text:
            raise CandidateEntityProjectionError("PROJECTED_TEXT_REQUIRED")
        for identity in self.identity_equivalences:
            expected = self.projected_surface(
                entity_id=identity.entity_id,
                surface_type=surface_type,
            )
            expected_spans = _surface_spans(text, expected)
            wrong: list[str] = []
            observed_any = bool(expected_spans)
            for surface in identity.equivalent_surfaces:
                spans = _surface_spans(text, surface)
                observed_any = observed_any or bool(spans)
                if surface == expected:
                    continue
                if any(
                    not any(start >= left and end <= right for left, right in expected_spans)
                    for start, end in spans
                ):
                    wrong.append(surface)
            if observed_any and (not expected_spans or wrong):
                raise CandidateEntityProjectionError(
                    "PROJECTED_TEXT_SURFACE_CONFLICT:"
                    f"{identity.entity_id}:{surface_type}:expected={expected}:"
                    f"observed={','.join(sorted(set(wrong)))}"
                )
        return text


def _schema_error(reason: str) -> CandidateEntityProjectionError:
    return CandidateEntityProjectionError(f"ENTITY_PROJECTION_SCHEMA_INVALID:{reason}")


def _exact_fields(value: object, expected: set[str], *, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _schema_error(f"{location}_FIELDS")
    return value


def _clean_surface(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _schema_error(f"{location}_SURFACE")
    if len(value) > 256 or any(ord(character) < 32 for character in value):
        raise _schema_error(f"{location}_SURFACE")
    return value


def _clean_expected_sha256(value: str, *, label: str) -> str:
    cleaned = value.removeprefix("sha256:")
    if not _SHA256_RX.fullmatch(cleaned):
        raise CandidateEntityProjectionError(f"EXPECTED_{label}_SHA256_INVALID")
    return cleaned


def _clean_recording_basename(value: object, *, location: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or Path(value).name != value
        or "/" in value
        or "\\" in value
    ):
        raise _schema_error(f"{location}_SOURCE_RECORDING_BASENAME")
    return value


def _clean_interval(
    start: object,
    end: object,
    *,
    location: str,
) -> tuple[int, int]:
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
    ):
        raise _schema_error(f"{location}_ABSOLUTE_SOURCE_INTERVAL")
    return start, end


def _canonical_document_sha256(document: Mapping[str, object]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _surface_spans(text: str, surface: str) -> tuple[tuple[int, int], ...]:
    if surface.isascii() and all(character.isalnum() or character == "_" for character in surface):
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])"
        )
        return tuple((match.start(), match.end()) for match in pattern.finditer(text))
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        position = text.find(surface, start)
        if position < 0:
            return tuple(spans)
        spans.append((position, position + len(surface)))
        start = position + 1


def load_candidate_entity_projection(
    *,
    projection_path: Path,
    candidate_id: str,
    reviewed_srt_path: Path,
) -> FrozenCandidateEntityProjection:
    """Load one projection through its same-candidate reviewed-baseline manifest.

    The reviewed SRT is never trusted by path or bytes alone.  Its canonical
    v2 manifest must validate, name this exact candidate/SRT sibling, and carry
    the same source recording/hash/absolute interval as the projection.
    """

    expected_projection_name = f"{candidate_id}.entity-projection.v1.json"
    expected_srt_name = f"{candidate_id}.reviewed.srt"
    if projection_path.name != expected_projection_name:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_FILENAME_CANDIDATE_MISMATCH"
        )
    if reviewed_srt_path.name != expected_srt_name:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_REVIEWED_SRT_FILENAME_CANDIDATE_MISMATCH"
        )

    for path, label in (
        (projection_path, "projection"),
        (reviewed_srt_path, "reviewed SRT"),
    ):
        if path.is_symlink() or not path.is_file():
            raise CandidateEntityProjectionError(
                f"ENTITY_PROJECTION_{label.upper().replace(' ', '_')}_UNAVAILABLE"
            )
    try:
        document = json.loads(projection_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_DOCUMENT_INVALID"
        ) from exc
    if not isinstance(document, Mapping):
        raise CandidateEntityProjectionError("ENTITY_PROJECTION_DOCUMENT_INVALID")

    baseline_root = reviewed_srt_path.parent
    manifest_path = baseline_root / f"{candidate_id}.subtitle-baseline.v1.json"
    try:
        manifest_document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_INVALID"
        ) from exc
    if not isinstance(manifest_document, Mapping):
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_INVALID"
        )
    manifest_fields = set(manifest_document)
    if (
        not _BASELINE_MANIFEST_REQUIRED_FIELDS.issubset(manifest_fields)
        or manifest_fields
        - _BASELINE_MANIFEST_REQUIRED_FIELDS
        - _BASELINE_MANIFEST_OPTIONAL_FIELDS
    ):
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_FIELDS_INVALID"
        )
    if (
        manifest_document.get("schema_version") != "subtitle-redelivery-baseline.v2"
        or manifest_document.get("exact_interval_replay") is not True
    ):
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_NOT_EXACT_V2"
        )
    try:
        reviewed = load_candidate_reviewed_subtitle_baseline(
            baseline_root,
            candidate_id,
        )
    except ReviewedSubtitleBaselineRegistryError as exc:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_INVALID"
        ) from exc
    if reviewed is None:
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_MANIFEST_UNAVAILABLE"
        )
    if reviewed.baseline_path != reviewed_srt_path.resolve():
        raise CandidateEntityProjectionError(
            "ENTITY_PROJECTION_BASELINE_SRT_PATH_MISMATCH"
        )

    reviewed_sha256 = str(reviewed.config.get("sha256") or "")
    source_recording_basename = str(
        reviewed.config.get("source_recording_basename") or ""
    )
    source_sha256 = str(reviewed.config.get("source_sha256") or "")
    absolute_source_start_ms = reviewed.config.get("absolute_source_start_ms")
    absolute_source_end_ms = reviewed.config.get("absolute_source_end_ms")
    return freeze_candidate_entity_projection(
        document,
        expected_candidate_id=candidate_id,
        expected_reviewed_srt_sha256=reviewed_sha256,
        expected_source_recording_basename=source_recording_basename,
        expected_source_sha256=source_sha256,
        expected_absolute_source_start_ms=absolute_source_start_ms,
        expected_absolute_source_end_ms=absolute_source_end_ms,
    )


def freeze_candidate_entity_projection(
    document: Mapping[str, object],
    *,
    expected_candidate_id: str,
    expected_reviewed_srt_sha256: str,
    expected_source_recording_basename: str,
    expected_source_sha256: str,
    expected_absolute_source_start_ms: object,
    expected_absolute_source_end_ms: object,
) -> FrozenCandidateEntityProjection:
    """Validate and freeze one exact candidate/SRT/source identity projection.

    Unknown fields fail closed.  In particular, global rewrite tables,
    canonical names, and alias-to-output rules cannot be smuggled into this
    candidate-scoped contract.
    """

    root = _exact_fields(document, _ROOT_FIELDS, location="ROOT")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise _schema_error("SCHEMA_VERSION")

    binding = _exact_fields(
        root.get("candidate_binding"), _BINDING_FIELDS, location="CANDIDATE_BINDING"
    )
    candidate_id = binding.get("candidate_id")
    reviewed_srt_sha256 = binding.get("reviewed_srt_sha256")
    source_recording_basename = _clean_recording_basename(
        binding.get("source_recording_basename"),
        location="CANDIDATE_BINDING",
    )
    source_sha256 = binding.get("source_sha256")
    absolute_source_start_ms, absolute_source_end_ms = _clean_interval(
        binding.get("absolute_source_start_ms"),
        binding.get("absolute_source_end_ms"),
        location="CANDIDATE_BINDING",
    )
    if not isinstance(candidate_id, str) or not _CANDIDATE_ID_RX.fullmatch(candidate_id):
        raise _schema_error("CANDIDATE_ID")
    if not isinstance(reviewed_srt_sha256, str) or not _SHA256_RX.fullmatch(
        reviewed_srt_sha256
    ):
        raise _schema_error("REVIEWED_SRT_SHA256")
    if candidate_id != expected_candidate_id:
        raise CandidateEntityProjectionError(
            f"CANDIDATE_BINDING_MISMATCH:expected={expected_candidate_id}:actual={candidate_id}"
        )
    expected_srt_sha256 = _clean_expected_sha256(
        expected_reviewed_srt_sha256,
        label="REVIEWED_SRT",
    )
    if reviewed_srt_sha256 != expected_srt_sha256:
        raise CandidateEntityProjectionError(
            "REVIEWED_SRT_BINDING_MISMATCH:"
            f"expected={expected_srt_sha256}:actual={reviewed_srt_sha256}"
        )
    expected_recording_basename = _clean_recording_basename(
        expected_source_recording_basename,
        location="EXPECTED",
    )
    if source_recording_basename != expected_recording_basename:
        raise CandidateEntityProjectionError(
            "SOURCE_RECORDING_BINDING_MISMATCH:"
            f"expected={expected_recording_basename}:actual={source_recording_basename}"
        )
    if not isinstance(source_sha256, str) or not _SHA256_RX.fullmatch(source_sha256):
        raise _schema_error("CANDIDATE_BINDING_SOURCE_SHA256")
    expected_source_hash = _clean_expected_sha256(
        expected_source_sha256,
        label="SOURCE",
    )
    if source_sha256 != expected_source_hash:
        raise CandidateEntityProjectionError(
            "SOURCE_SHA256_BINDING_MISMATCH:"
            f"expected={expected_source_hash}:actual={source_sha256}"
        )
    expected_source_start_ms, expected_source_end_ms = _clean_interval(
        expected_absolute_source_start_ms,
        expected_absolute_source_end_ms,
        location="EXPECTED",
    )
    if (
        absolute_source_start_ms != expected_source_start_ms
        or absolute_source_end_ms != expected_source_end_ms
    ):
        raise CandidateEntityProjectionError(
            "ABSOLUTE_SOURCE_INTERVAL_BINDING_MISMATCH:"
            f"expected={expected_source_start_ms}-{expected_source_end_ms}:"
            f"actual={absolute_source_start_ms}-{absolute_source_end_ms}"
        )

    identity_rows = root.get("identity_equivalences")
    if (
        not isinstance(identity_rows, Sequence)
        or isinstance(identity_rows, (str, bytes))
        or not identity_rows
    ):
        raise _schema_error("IDENTITY_EQUIVALENCES")

    identities: list[FrozenIdentityEquivalence] = []
    identity_surfaces: dict[str, frozenset[str]] = {}
    surface_owners: dict[str, str] = {}
    for index, raw_row in enumerate(identity_rows):
        row = _exact_fields(raw_row, _IDENTITY_FIELDS, location=f"IDENTITY_{index}")
        entity_id = row.get("entity_id")
        if not isinstance(entity_id, str) or not _ENTITY_ID_RX.fullmatch(entity_id):
            raise _schema_error(f"IDENTITY_{index}_OPAQUE_ENTITY_ID")
        if entity_id in identity_surfaces:
            raise _schema_error(f"IDENTITY_{index}_DUPLICATE_ENTITY_ID")
        raw_surfaces = row.get("equivalent_surfaces")
        if (
            not isinstance(raw_surfaces, Sequence)
            or isinstance(raw_surfaces, (str, bytes))
            or not raw_surfaces
        ):
            raise _schema_error(f"IDENTITY_{index}_EQUIVALENT_SURFACES")
        surfaces = tuple(
            _clean_surface(value, location=f"IDENTITY_{index}") for value in raw_surfaces
        )
        if len(set(surfaces)) != len(surfaces):
            raise _schema_error(f"IDENTITY_{index}_DUPLICATE_SURFACE")
        if entity_id in surfaces:
            raise _schema_error(f"IDENTITY_{index}_ENTITY_ID_IS_SURFACE")
        for surface in surfaces:
            prior_owner = surface_owners.get(surface)
            if prior_owner is not None:
                raise _schema_error(
                    f"IDENTITY_SURFACE_AMBIGUOUS:{surface}:{prior_owner}:{entity_id}"
                )
            surface_owners[surface] = entity_id
        identity_surfaces[entity_id] = frozenset(surfaces)
        identities.append(FrozenIdentityEquivalence(entity_id, surfaces))

    projection_rows = root.get("surface_projections")
    if (
        not isinstance(projection_rows, Sequence)
        or isinstance(projection_rows, (str, bytes))
        or not projection_rows
    ):
        raise _schema_error("SURFACE_PROJECTIONS")

    projections: list[FrozenSurfaceProjection] = []
    singleton_keys: set[tuple[str, str]] = set()
    mention_keys: set[tuple[str, int, str]] = set()
    types_by_entity: dict[str, set[str]] = {entity_id: set() for entity_id in identity_surfaces}
    for index, raw_row in enumerate(projection_rows):
        if not isinstance(raw_row, Mapping):
            raise _schema_error(f"PROJECTION_{index}_FIELDS")
        surface_type = raw_row.get("surface_type")
        expected_fields = (
            _MENTION_PROJECTION_FIELDS if surface_type == "mention" else _PROJECTION_FIELDS
        )
        row = _exact_fields(raw_row, expected_fields, location=f"PROJECTION_{index}")
        entity_id = row.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in identity_surfaces:
            raise _schema_error(f"PROJECTION_{index}_ENTITY_ID")
        if not isinstance(surface_type, str) or surface_type not in SURFACE_TYPES:
            raise _schema_error(f"PROJECTION_{index}_SURFACE_TYPE")
        surface = _clean_surface(row.get("surface"), location=f"PROJECTION_{index}")
        if surface not in identity_surfaces[entity_id]:
            raise _schema_error(f"PROJECTION_{index}_SURFACE_NOT_EQUIVALENT")

        cue_index: int | None = None
        mention_id: str | None = None
        if surface_type == "mention":
            cue_index = row.get("cue_index")  # type: ignore[assignment]
            mention_id = row.get("mention_id")  # type: ignore[assignment]
            if (
                isinstance(cue_index, bool)
                or not isinstance(cue_index, int)
                or cue_index <= 0
            ):
                raise _schema_error(f"PROJECTION_{index}_CUE_INDEX")
            if (
                not isinstance(mention_id, str)
                or not re.fullmatch(r"m[1-9][0-9]*", mention_id)
            ):
                raise _schema_error(f"PROJECTION_{index}_MENTION_ID")
            mention_key = (entity_id, cue_index, mention_id)
            if mention_key in mention_keys:
                raise _schema_error(f"PROJECTION_{index}_DUPLICATE_MENTION")
            mention_keys.add(mention_key)
        else:
            singleton_key = (entity_id, surface_type)
            if singleton_key in singleton_keys:
                raise _schema_error(f"PROJECTION_{index}_DUPLICATE_SINGLETON")
            singleton_keys.add(singleton_key)
        types_by_entity[entity_id].add(surface_type)
        projections.append(
            FrozenSurfaceProjection(
                entity_id=entity_id,
                surface_type=surface_type,
                surface=surface,
                cue_index=cue_index,
                mention_id=mention_id,
            )
        )

    for entity_id, surface_types in types_by_entity.items():
        missing = SURFACE_TYPES - surface_types
        if missing:
            raise _schema_error(
                f"ENTITY_SURFACE_TYPES_MISSING:{entity_id}:{','.join(sorted(missing))}"
            )
        if any((entity_id, surface_type) not in singleton_keys for surface_type in _SINGLETON_SURFACE_TYPES):
            raise _schema_error(f"ENTITY_SINGLETON_SURFACE_MISSING:{entity_id}")

    return FrozenCandidateEntityProjection(
        schema_version=SCHEMA_VERSION,
        binding=CandidateSrtBinding(
            candidate_id=candidate_id,
            reviewed_srt_sha256=reviewed_srt_sha256,
            source_recording_basename=source_recording_basename,
            source_sha256=source_sha256,
            absolute_source_start_ms=absolute_source_start_ms,
            absolute_source_end_ms=absolute_source_end_ms,
        ),
        identity_equivalences=tuple(identities),
        surface_projections=tuple(projections),
        projection_sha256=_canonical_document_sha256(root),
    )
