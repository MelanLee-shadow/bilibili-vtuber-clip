from __future__ import annotations

import hashlib
import json

import pytest

from src.autoslice.redelivery_boundary_projection import (
    AUTHORITY_CONFIG_KEY,
    PROJECTION_MODE,
    PROJECTION_MODE_CONFIG_KEY,
    RedeliveryBoundaryProjectionError,
    build_terminal_projection_authority,
    materialization_spec_for_selected_projection,
    projection_scope_binding,
    terminal_projection_relaxation,
    validate_terminal_projection_authority,
)


def _srt(*rows: tuple[int, int, str]) -> str:
    def stamp(ms: int) -> str:
        seconds, millis = divmod(ms, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    return "\n\n".join(
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}"
        for index, (start, end, text) in enumerate(rows, start=1)
    ) + "\n"


def _fixture(tmp_path):
    baseline = tmp_path / "reviewed.srt"
    baseline.write_text(
        _srt((0, 1_000, "第一句"), (1_000, 2_000, "审定收尾")),
        encoding="utf-8",
    )
    config = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "exact_interval_replay": True,
        PROJECTION_MODE_CONFIG_KEY: PROJECTION_MODE,
        "path": str(baseline),
        "sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
        "authority": "synthetic 维护者-reviewed delivery",
        "source_recording_basename": "recording.mp4",
        "source_sha256": "a" * 64,
        "absolute_source_start_ms": 101_000,
        "absolute_source_end_ms": 103_000,
    }
    spec = {
        "candidate_id": "candidate-projection",
        "pieces": [
            {
                "remote_media": "/recordings/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 105_000,
            }
        ],
        "subtitle_redelivery_baseline": config,
    }
    provenance = [
        {
            "source_path": "/recordings/recording.mp4",
            "source_sha256": "a" * 64,
        }
    ]
    return baseline, config, spec, provenance


def test_projection_authority_binds_baseline_source_and_terminal_cue(tmp_path):
    _baseline, config, spec, provenance = _fixture(tmp_path)

    authority = build_terminal_projection_authority(
        spec=spec,
        piece_provenance_rows=provenance,
        spec_parent=tmp_path,
    )

    assert authority is not None
    assert authority["status"] == "BOUND"
    assert authority["baseline_terminal_cue_index"] == 2
    assert authority["baseline_terminal_end_ms"] == 2_000
    assert authority["local_source_start_ms"] == 1_000
    assert authority["local_source_end_ms"] == 3_000
    assert validate_terminal_projection_authority(authority) == authority
    scope = projection_scope_binding(authority)
    assert scope["reviewed_endpoint_ms"] == 3_000
    assert scope["max_terminal_drift_ms"] == 250

    config[AUTHORITY_CONFIG_KEY] = authority
    with pytest.raises(
        RedeliveryBoundaryProjectionError,
        match="CALLER_SUPPLIED_AUTHORITY",
    ):
        build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=provenance,
            spec_parent=tmp_path,
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda baseline, config, spec, provenance: config.update(
                sha256="0" * 64
            ),
            "BASELINE_SHA256_MISMATCH",
        ),
        (
            lambda baseline, config, spec, provenance: config.update(
                source_recording_basename="other.mp4"
            ),
            "SOURCE_BASENAME_MISMATCH",
        ),
        (
            lambda baseline, config, spec, provenance: provenance[0].update(
                source_sha256="b" * 64
            ),
            "SOURCE_SHA256_MISMATCH",
        ),
        (
            lambda baseline, config, spec, provenance: (
                baseline.write_text(
                    _srt((0, 1_000, "第一句"), (1_000, 1_900, "短尾")),
                    encoding="utf-8",
                ),
                config.update(
                    sha256=hashlib.sha256(baseline.read_bytes()).hexdigest()
                ),
            ),
            "BASELINE_TERMINAL_END_MISMATCH",
        ),
    ],
)
def test_projection_authority_rejects_binding_drift(
    tmp_path, mutation, reason
):
    baseline, config, spec, provenance = _fixture(tmp_path)
    mutation(baseline, config, spec, provenance)

    with pytest.raises(RedeliveryBoundaryProjectionError, match=reason):
        build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=provenance,
            spec_parent=tmp_path,
        )


def test_projection_authority_requires_literal_exact_replay(tmp_path):
    _baseline, config, spec, provenance = _fixture(tmp_path)
    config.pop(PROJECTION_MODE_CONFIG_KEY)
    assert (
        build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=provenance,
            spec_parent=tmp_path,
        )
        is None
    )

    config[PROJECTION_MODE_CONFIG_KEY] = PROJECTION_MODE
    config["exact_interval_replay"] = False
    with pytest.raises(
        RedeliveryBoundaryProjectionError,
        match="REQUIRES_EXACT_INTERVAL_REPLAY",
    ):
        build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=provenance,
            spec_parent=tmp_path,
        )

    config["exact_interval_replay"] = "true"
    with pytest.raises(
        RedeliveryBoundaryProjectionError,
        match="EXACT_INTERVAL_REPLAY_INVALID",
    ):
        build_terminal_projection_authority(
            spec=spec,
            piece_provenance_rows=provenance,
            spec_parent=tmp_path,
        )


def test_projection_exact_materialization_activates_only_after_selection(
    tmp_path,
):
    _baseline, config, spec, provenance = _fixture(tmp_path)
    authority = build_terminal_projection_authority(
        spec=spec,
        piece_provenance_rows=provenance,
        spec_parent=tmp_path,
    )
    assert authority is not None
    config[AUTHORITY_CONFIG_KEY] = authority
    projection_scope = projection_scope_binding(authority)
    grid_sha256 = "sha256:" + "c" * 64
    receipt = terminal_projection_relaxation(
        [
            {
                "cue_index": 1,
                "start_ms": 1_000,
                "end_ms": 2_900,
                "text": "故事闭合",
            },
            {
                "cue_index": 2,
                "start_ms": 2_900,
                "end_ms": 3_500,
                "text": "下一话题",
            },
        ],
        projection_scope=projection_scope,
        minimum_end_ms=3_000,
        cap_end_ms=3_000,
        cue_grid_sha256=grid_sha256,
    )
    assert receipt is not None
    review = {
        "cue_grid_sha256": grid_sha256,
        "recommended_end_cue_index": 1,
        "recommended_end_ms": 3_000,
        "recommendation_relaxations": [receipt],
        "selected_terminal_projection_binding": receipt,
        "evidence_cue_indexes": [1, 2],
        "boundary_search_scope": {
            "reviewed_exact_interval_projection": projection_scope,
        },
        "final_endpoint_binding": {
            "final_snapped_end_ms": 2_900,
            "final_end_ms": 3_000,
            "closure_text_sha256": receipt["cue_text_sha256"],
        },
    }

    ordinary = materialization_spec_for_selected_projection(
        spec,
        {"boundary_semantic_review": {}},
    )
    assert ordinary is not spec
    assert AUTHORITY_CONFIG_KEY not in ordinary[
        "subtitle_redelivery_baseline"
    ]
    assert AUTHORITY_CONFIG_KEY in config
    assert (
        materialization_spec_for_selected_projection(
            spec,
            {"boundary_semantic_review": review},
        )
        is spec
    )

    review["evidence_cue_indexes"] = [1]
    with pytest.raises(
        RedeliveryBoundaryProjectionError,
        match="SELECTION_INVALID",
    ):
        materialization_spec_for_selected_projection(
            spec,
            {"boundary_semantic_review": review},
        )


def test_projection_authority_rejects_self_hashed_shape_forgery(tmp_path):
    _baseline, _config, spec, provenance = _fixture(tmp_path)
    authority = build_terminal_projection_authority(
        spec=spec,
        piece_provenance_rows=provenance,
        spec_parent=tmp_path,
    )
    assert authority is not None

    for mutation in (
        lambda row: row.update(local_source_end_ms=9_999),
        lambda row: row.update(extra_grant="trust me"),
        lambda row: row.update(source_recording_basename="../recording.mp4"),
    ):
        forged = dict(authority)
        forged.pop("authority_sha256")
        mutation(forged)
        encoded = json.dumps(
            forged,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        forged["authority_sha256"] = (
            "sha256:" + hashlib.sha256(encoded).hexdigest()
        )
        with pytest.raises(
            RedeliveryBoundaryProjectionError,
            match="AUTHORITY_INVALID",
        ):
            validate_terminal_projection_authority(forged)
