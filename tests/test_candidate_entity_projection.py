from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.autoslice.candidate_entity_projection import (
    CandidateEntityProjectionError,
    SCHEMA_VERSION,
    freeze_candidate_entity_projection,
    load_candidate_entity_projection,
)


CANDIDATE_ID = "auto_223750_578_734"
SRT_SHA256 = "a" * 64
SOURCE_RECORDING_BASENAME = "22966160_20260807-22-37-50.mp4"
SOURCE_SHA256 = "b" * 64
SOURCE_START_MS = 577_780
SOURCE_END_MS = 735_090
XXSK_ID = "ent_11111111111111111111111111111111"
NANCHOU_ID = "ent_22222222222222222222222222222222"
LIA_ID = "ent_33333333333333333333333333333333"


def _entity_rows(
    entity_id: str,
    *,
    official: str,
    session: str,
    mentions: list[tuple[int, str, str]],
    reviewed: str,
    speaker: str,
    title_cover: str,
    publication: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {"entity_id": entity_id, "surface_type": "official", "surface": official},
        {"entity_id": entity_id, "surface_type": "session", "surface": session},
        {"entity_id": entity_id, "surface_type": "reviewed", "surface": reviewed},
        {"entity_id": entity_id, "surface_type": "speaker", "surface": speaker},
        {
            "entity_id": entity_id,
            "surface_type": "title_cover",
            "surface": title_cover,
        },
        {
            "entity_id": entity_id,
            "surface_type": "publication",
            "surface": publication,
        },
    ]
    rows.extend(
        {
            "entity_id": entity_id,
            "surface_type": "mention",
            "surface": surface,
            "cue_index": cue_index,
            "mention_id": mention_id,
        }
        for cue_index, mention_id, surface in mentions
    )
    return rows


def _document() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_binding": {
            "candidate_id": CANDIDATE_ID,
            "reviewed_srt_sha256": SRT_SHA256,
            "source_recording_basename": SOURCE_RECORDING_BASENAME,
            "source_sha256": SOURCE_SHA256,
            "absolute_source_start_ms": SOURCE_START_MS,
            "absolute_source_end_ms": SOURCE_END_MS,
        },
        "identity_equivalences": [
            {
                "entity_id": XXSK_ID,
                "equivalent_surfaces": ["星汐 seki", "星汐", "xxsk"],
            },
            {
                "entity_id": NANCHOU_ID,
                "equivalent_surfaces": ["南町nightin", "南町", "NT", "nt"],
            },
            {
                "entity_id": LIA_ID,
                "equivalent_surfaces": ["莉娅", "莉亚"],
            },
        ],
        "surface_projections": [
            *_entity_rows(
                XXSK_ID,
                official="星汐 seki",
                session="xxsk",
                mentions=[(8, "m1", "星汐"), (15, "m1", "xxsk")],
                reviewed="xxsk",
                speaker="xxsk",
                title_cover="xxsk",
                publication="xxsk",
            ),
            *_entity_rows(
                NANCHOU_ID,
                official="南町nightin",
                session="南町",
                mentions=[(23, "m1", "NT"), (33, "m1", "南町")],
                reviewed="南町nightin",
                speaker="南町nightin",
                title_cover="南町nightin",
                publication="南町nightin",
            ),
            *_entity_rows(
                LIA_ID,
                official="莉娅",
                session="莉娅",
                mentions=[(4, "m1", "莉亚")],
                reviewed="莉亚",
                speaker="莉娅",
                title_cover="莉娅",
                publication="莉娅",
            ),
        ],
    }


def _freeze(
    document: dict[str, object] | None = None,
    **expected_overrides: object,
):
    expected: dict[str, object] = {
        "expected_candidate_id": CANDIDATE_ID,
        "expected_reviewed_srt_sha256": SRT_SHA256,
        "expected_source_recording_basename": SOURCE_RECORDING_BASENAME,
        "expected_source_sha256": SOURCE_SHA256,
        "expected_absolute_source_start_ms": SOURCE_START_MS,
        "expected_absolute_source_end_ms": SOURCE_END_MS,
    }
    expected.update(expected_overrides)
    return freeze_candidate_entity_projection(
        document if document is not None else _document(),
        **expected,
    )


def test_freezes_exact_candidate_srt_projection_deterministically() -> None:
    first = _freeze()
    second = _freeze(copy.deepcopy(_document()))

    assert first == second
    assert first.binding.candidate_id == CANDIDATE_ID
    assert first.binding.reviewed_srt_sha256 == SRT_SHA256
    assert first.binding.source_recording_basename == SOURCE_RECORDING_BASENAME
    assert first.binding.source_sha256 == SOURCE_SHA256
    assert (
        first.binding.absolute_source_start_ms,
        first.binding.absolute_source_end_ms,
    ) == (SOURCE_START_MS, SOURCE_END_MS)
    assert first.projection_sha256 == second.projection_sha256
    assert len(first.projection_sha256) == 64
    with pytest.raises(FrozenInstanceError):
        first.binding.candidate_id = "auto_other"  # type: ignore[misc]


def test_rejects_same_entity_starxi_when_xxsk_is_reviewed_projection() -> None:
    frozen = _freeze()

    assert (
        frozen.require_surface(
            entity_id=XXSK_ID,
            surface_type="reviewed",
            actual_surface="xxsk",
        )
        == "xxsk"
    )
    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"^IDENTITY_EQUIVALENT_SURFACE_NOT_PROJECTED:.*expected=xxsk:actual=星汐$",
    ):
        frozen.require_surface(
            entity_id=XXSK_ID,
            surface_type="reviewed",
            actual_surface="星汐",
        )


def test_equivalent_nt_mention_cannot_expand_to_full_name() -> None:
    frozen = _freeze()

    assert (
        frozen.require_surface(
            entity_id=NANCHOU_ID,
            surface_type="mention",
            cue_index=23,
            mention_id="m1",
            actual_surface="NT",
        )
        == "NT"
    )
    with pytest.raises(
        CandidateEntityProjectionError,
        match=(
            r"^IDENTITY_EQUIVALENT_SURFACE_NOT_PROJECTED:"
            r".*expected=NT:actual=南町nightin$"
        ),
    ):
        frozen.require_surface(
            entity_id=NANCHOU_ID,
            surface_type="mention",
            cue_index=23,
            mention_id="m1",
            actual_surface="南町nightin",
        )


def test_title_cover_rejects_lia_typo_even_when_reviewed_srt_keeps_it() -> None:
    frozen = _freeze()

    assert (
        frozen.require_surface(
            entity_id=LIA_ID,
            surface_type="reviewed",
            actual_surface="莉亚",
        )
        == "莉亚"
    )
    assert (
        frozen.require_surface(
            entity_id=LIA_ID,
            surface_type="title_cover",
            actual_surface="莉娅",
        )
        == "莉娅"
    )
    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"^IDENTITY_EQUIVALENT_SURFACE_NOT_PROJECTED:.*expected=莉娅:actual=莉亚$",
    ):
        frozen.require_surface(
            entity_id=LIA_ID,
            surface_type="title_cover",
            actual_surface="莉亚",
        )


def test_projected_text_allows_full_surface_containing_short_alias() -> None:
    frozen = _freeze()

    assert frozen.require_text_surfaces(
        surface_type="title_cover",
        text="和南町nightin一起行动",
    ) == "和南町nightin一起行动"


def test_projected_text_rejects_wrong_alias_but_not_ascii_substring() -> None:
    frozen = _freeze()

    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"PROJECTED_TEXT_SURFACE_CONFLICT:.*expected=莉娅:observed=莉亚",
    ):
        frozen.require_text_surfaces(
            surface_type="title_cover",
            text="莉亚求小李放过她",
        )
    # The short alias ``nt`` must not match the letters inside an unrelated
    # English word; only an exact ASCII token can nominate the identity.
    assert frozen.require_text_surfaces(
        surface_type="title_cover",
        text="content creator",
    ) == "content creator"


@pytest.mark.parametrize(
    ("expected_overrides", "error"),
    [
        (
            {"expected_candidate_id": "auto_other"},
            "CANDIDATE_BINDING_MISMATCH",
        ),
        (
            {"expected_reviewed_srt_sha256": "c" * 64},
            "REVIEWED_SRT_BINDING_MISMATCH",
        ),
        (
            {"expected_source_recording_basename": "other.mp4"},
            "SOURCE_RECORDING_BINDING_MISMATCH",
        ),
        (
            {"expected_source_sha256": "c" * 64},
            "SOURCE_SHA256_BINDING_MISMATCH",
        ),
        (
            {"expected_absolute_source_start_ms": SOURCE_START_MS + 1},
            "ABSOLUTE_SOURCE_INTERVAL_BINDING_MISMATCH",
        ),
        (
            {"expected_absolute_source_end_ms": SOURCE_END_MS + 1},
            "ABSOLUTE_SOURCE_INTERVAL_BINDING_MISMATCH",
        ),
    ],
)
def test_rejects_candidate_or_srt_binding_drift(
    expected_overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(CandidateEntityProjectionError, match=f"^{error}"):
        _freeze(**expected_overrides)


def _write_loader_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    baseline_root = tmp_path / "reviewed_subtitle_baselines"
    projection_root = tmp_path / "candidate_entity_projections"
    baseline_root.mkdir()
    projection_root.mkdir()
    reviewed_srt = baseline_root / f"{CANDIDATE_ID}.reviewed.srt"
    reviewed_srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n莉亚 活着\n",
        encoding="utf-8",
    )
    reviewed_sha256 = hashlib.sha256(reviewed_srt.read_bytes()).hexdigest()
    manifest_path = baseline_root / f"{CANDIDATE_ID}.subtitle-baseline.v1.json"
    manifest_path.write_text(
        json.dumps(
            {
                "registry_schema_version": "candidate-reviewed-subtitle-baseline.v1",
                "candidate_id": CANDIDATE_ID,
                "schema_version": "subtitle-redelivery-baseline.v2",
                "mode": "preserve_text_outside_source_truth",
                "path": reviewed_srt.name,
                "sha256": reviewed_sha256,
                "authority": "维护者 reviewed exact candidate SRT",
                "exact_interval_replay": True,
                "source_recording_basename": SOURCE_RECORDING_BASENAME,
                "source_sha256": SOURCE_SHA256,
                "absolute_source_start_ms": SOURCE_START_MS,
                "absolute_source_end_ms": SOURCE_END_MS,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    document = _document()
    binding = document["candidate_binding"]
    assert isinstance(binding, dict)
    binding["reviewed_srt_sha256"] = reviewed_sha256
    projection_path = projection_root / f"{CANDIDATE_ID}.entity-projection.v1.json"
    projection_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return projection_path, reviewed_srt, manifest_path


def test_loader_closes_projection_srt_manifest_and_source_binding(tmp_path: Path) -> None:
    projection_path, reviewed_srt, _manifest_path = _write_loader_fixture(tmp_path)

    frozen = load_candidate_entity_projection(
        projection_path=projection_path,
        candidate_id=CANDIDATE_ID,
        reviewed_srt_path=reviewed_srt,
    )

    assert frozen.binding.reviewed_srt_sha256 == hashlib.sha256(
        reviewed_srt.read_bytes()
    ).hexdigest()
    assert frozen.binding.source_recording_basename == SOURCE_RECORDING_BASENAME
    assert frozen.binding.source_sha256 == SOURCE_SHA256
    assert (
        frozen.binding.absolute_source_start_ms,
        frozen.binding.absolute_source_end_ms,
    ) == (SOURCE_START_MS, SOURCE_END_MS)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_recording_basename", "other.mp4", "SOURCE_RECORDING_BINDING_MISMATCH"),
        ("source_sha256", "c" * 64, "SOURCE_SHA256_BINDING_MISMATCH"),
        (
            "absolute_source_start_ms",
            SOURCE_START_MS + 1,
            "ABSOLUTE_SOURCE_INTERVAL_BINDING_MISMATCH",
        ),
        (
            "absolute_source_end_ms",
            SOURCE_END_MS + 1,
            "ABSOLUTE_SOURCE_INTERVAL_BINDING_MISMATCH",
        ),
    ],
)
def test_loader_rejects_source_drift_from_reviewed_baseline_manifest(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    projection_path, reviewed_srt, manifest_path = _write_loader_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CandidateEntityProjectionError, match=f"^{error}"):
        load_candidate_entity_projection(
            projection_path=projection_path,
            candidate_id=CANDIDATE_ID,
            reviewed_srt_path=reviewed_srt,
        )


@pytest.mark.parametrize(
    "manifest_mutation",
    [
        lambda manifest: manifest.update(unexpected_global_aliases={"莉亚": "莉娅"}),
        lambda manifest: manifest.update(candidate_id="auto_other"),
        lambda manifest: manifest.update(sha256="d" * 64),
        lambda manifest: manifest.update(schema_version="subtitle-redelivery-baseline.v1"),
        lambda manifest: manifest.update(exact_interval_replay=False),
    ],
)
def test_loader_fails_closed_on_unknown_drifted_or_wrong_manifest(
    tmp_path: Path,
    manifest_mutation,
) -> None:
    projection_path, reviewed_srt, manifest_path = _write_loader_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_mutation(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CandidateEntityProjectionError, match="ENTITY_PROJECTION_"):
        load_candidate_entity_projection(
            projection_path=projection_path,
            candidate_id=CANDIDATE_ID,
            reviewed_srt_path=reviewed_srt,
        )


def test_strict_shape_has_no_global_rewrite_or_canonical_escape_hatch() -> None:
    document = _document()
    document["global_rewrites"] = {"星汐": "xxsk"}
    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"^ENTITY_PROJECTION_SCHEMA_INVALID:ROOT_FIELDS$",
    ):
        _freeze(document)

    document = _document()
    identities = document["identity_equivalences"]
    assert isinstance(identities, list)
    identities[0]["canonical"] = "xxsk"
    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"^ENTITY_PROJECTION_SCHEMA_INVALID:IDENTITY_0_FIELDS$",
    ):
        _freeze(document)


@pytest.mark.parametrize(
    "mutate_binding",
    [
        lambda binding: binding.pop("source_sha256"),
        lambda binding: binding.update(source_recording_basename="../recording.mp4"),
        lambda binding: binding.update(source_sha256="not-a-sha256"),
        lambda binding: binding.update(absolute_source_start_ms=True),
        lambda binding: binding.update(absolute_source_end_ms=SOURCE_START_MS),
        lambda binding: binding.update(global_rewrite_scope="all_candidates"),
    ],
)
def test_projection_source_binding_shape_fails_closed(mutate_binding) -> None:
    document = _document()
    binding = document["candidate_binding"]
    assert isinstance(binding, dict)
    mutate_binding(binding)

    with pytest.raises(
        CandidateEntityProjectionError,
        match=r"^ENTITY_PROJECTION_SCHEMA_INVALID:",
    ):
        _freeze(document)
