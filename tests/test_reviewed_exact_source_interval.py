from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import src.autoslice.reviewed_subtitle_baseline_registry as baseline_registry
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    resolve_producer_boundary,
)
from src.autoslice.producer_boundary_review_stage import (
    review_exact_delivery_boundary_semantics,
)
from src.autoslice.review_package_boundary_validators import (
    semantic_boundary_review_is_valid,
)
from src.autoslice.review_package_boundary_contract import audit_boundary_contract
from src.autoslice.review_package_owner_audit import (
    _terminal_projection_materialization_valid,
)
from src.autoslice.reviewed_exact_source_interval import (
    CONTRADICTION_CONFIG_KEY,
    RUNTIME_CONFIG_KEY,
    ReviewedExactSourceIntervalError,
    authority_sha256,
    build_source_semantic_review,
    compile_authority_from_spec,
    exact_boundary_search_scope,
    resolve_exact_boundary,
    validate_runtime_authority,
    canonical_sha256,
)
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "auto_223750_578_734"
SOURCE_SHA = "66e9ec0707fe8d7f38cf8a552e863dafbba34fb7eadaece496adb30d04b1ad1e"
KEEP_AUTHORITY = (
    ROOT / "assets/lidousha/authorities" / f"{CANDIDATE}.manual-title-keep-authority.v1.json"
)
GIVEN_END_AUTHORITY = (
    "Ivan blind review 2026-08-10T22:41:31.794Z: "
    "auto_223750_578_654是最值得发的，这个至少应该在90分以上，"
    "这个就是典型的好切片，剧情一波三折，首先李豆沙是主角，"
    "在一开始她和奈奈莉娅说不会伤害莉娅，然后就吃了她，属于戏剧化情节，"
    "应该给高分，然后之后弹幕也出现了很多说她时坏女人，这是观众反应高光，"
    "最后，她作为食鸟鸭想吃尸体结果没吃到就被人发现了，又是一层反转，"
    "从弱，变强，再变弱，这是非常好的切片，应该得高分，"
    "而且甚至我认为这个应该再往后面切一点，补全剧情"
)


@pytest.fixture(autouse=True)
def _allow_precommit_authority_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production requires HEAD/deploy sealing; this new asset is pre-commit here."""

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        lambda **_kwargs: None,
    )


def _source_spec() -> dict:
    keep = json.loads(KEEP_AUTHORITY.read_text(encoding="utf-8"))
    blocked = keep["replay"]["blocked_source_fact_review"]
    grant = json.loads(
        (
            ROOT
            / "assets/lidousha/reviewed_exact_source_intervals"
            / f"{CANDIDATE}.reviewed-exact-source-interval.v1.json"
        ).read_text(encoding="utf-8")
    )
    selection = grant["selection"]
    spec = {
        "candidate_id": CANDIDATE,
        "semantic_start_ms": 578030,
        "semantic_end_ms": 734230,
        "given_end_ms": 734230,
        "given_end_mode": "semantic_lower_bound",
        "given_end_authority": GIVEN_END_AUTHORITY,
        "selection_hook": blocked["original_selection_hook"],
        "selection_scorecard": keep["replay"]["selection_scorecard"],
        "source_fact_scorecard_rescore_provenance": {
            "correction_authority_file_sha256": selection["correction_authority_file_sha256"],
            "correction_authority": {"authority_sha256": selection["correction_authority_sha256"]},
            "rescore_receipt": {
                "output_sha256": selection["rescore_receipt_output_sha256"],
                "input_bindings": {
                    "selection_calibration_sha256": selection["selection_calibration_sha256"]
                },
            },
        },
        "boundary_repair_extend_cap_ms": 30000,
        "pieces": [
            {
                "start_ms": 568030,
                "end_ms": 782230,
                "remote_media": ("/relocated/free/source/22966160_20260807-22-37-50.mp4"),
                "source_media_sha256": "sha256:" + SOURCE_SHA,
            }
        ],
    }
    baseline = load_candidate_reviewed_subtitle_baseline(
        ROOT / "assets/lidousha/reviewed_subtitle_baselines", CANDIDATE
    )
    assert baseline is not None
    spec["subtitle_redelivery_baseline"] = baseline.config
    spec["speaker_overrides"] = str(
        ROOT / "assets/lidousha/speaker_overrides" / f"{CANDIDATE}.speaker.v1.json"
    )
    return spec


def _compile(spec: dict) -> dict:
    authority = compile_authority_from_spec(
        spec=spec,
        piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
    )
    assert authority is not None
    spec[RUNTIME_CONFIG_KEY] = authority
    spec["required_boundary_owners"] = []
    scope = exact_boundary_search_scope(spec=spec, authority=authority, required_owner_end_ms=None)
    spec["boundary_search_scope"] = scope
    spec["boundary_semantic_review"] = build_source_semantic_review(
        spec=spec, authority=authority, boundary_search_scope=scope
    )
    return authority


def test_three_materially_different_asr_grids_cannot_move_exact_interval() -> None:
    spec = _source_spec()
    authority = _compile(spec)
    grids = [
        [],
        [SrtCue("1", 166000, 166120, "我以为都能吃掉")],
        [
            SrtCue("1", 165100, 166640, "我以为都能吃掉啊"),
            SrtCue("2", 166640, 168900, "跨过终点的下一话题"),
        ],
    ]
    results = [
        resolve_exact_boundary(
            spec=spec,
            durations=[214200],
            padded_duration_ms=214200,
            fresh_cues=grid,
            speech_spans=[],
        )
        for grid in grids
    ]
    assert all(result is not None for result in results)
    assert {(result["final_start"], result["final_end"]) for result in results} == {(9750, 167060)}
    assert {
        tuple(
            (cue.source_start_ms, cue.source_end_ms, cue.text) for cue in result["sanitized_cues"]
        )
        for result in results
    }.__len__() == 1
    assert all(
        result["audit"][RUNTIME_CONFIG_KEY]["authority_sha256"] == authority["authority_sha256"]
        for result in results
    )


def test_exclusive_dispatch_never_calls_ordinary_resolver(tmp_path: Path) -> None:
    spec = _source_spec()
    _compile(spec)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary boundary path was called")

    result = resolve_producer_boundary(
        spec=spec,
        durations=[214200],
        padded=tmp_path / "unused.mp4",
        padded_dur=214200,
        cid=CANDIDATE,
        out_root=tmp_path,
        transcriber=forbidden,
        cues=[SrtCue("1", 166000, 168000, "random crossing cue")],
        spans=[],
        boundary_repair_extend_cap_ms=30000,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=forbidden, run_command=forbidden
        ),
    )
    assert (result.final_start, result.final_end) == (9750, 167060)


def test_exact_final_review_replays_reviewed_grid_without_provider() -> None:
    spec = _source_spec()
    _compile(spec)
    source_review = spec["boundary_semantic_review"]
    final_cues = [
        SrtCue(str(index), cue.start_ms, cue.end_ms, cue.text)
        for index, cue in enumerate(
            parse_srt_cues(
                Path(spec["subtitle_redelivery_baseline"]["path"]).read_text(encoding="utf-8")
            ),
            start=1,
        )
    ]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider/live semantic review was called")

    final_review = review_exact_delivery_boundary_semantics(
        cues=final_cues,
        source_boundary_review=source_review,
        source_final_start_ms=9750,
        source_final_end_ms=167060,
        candidate_id=CANDIDATE,
        selection_hook=spec["selection_hook"],
        selection_scorecard=spec["selection_scorecard"],
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=0,
        llm_call=forbidden,
        extract_json=forbidden,
        disabled=True,
    )
    assert semantic_boundary_review_is_valid(source_review, expected_scope="source_full_window")
    assert semantic_boundary_review_is_valid(final_review, expected_scope="final_delivery")
    assert final_review["final_endpoint_binding"]["final_end_ms"] == 157310
    assert final_review["source_separation_witness"]["status"] == "PASS"
    assert final_review["source_separation_witness"]["source_final_start_ms"] == 9750
    assert final_review["source_separation_witness"]["source_final_end_ms"] == 167060

    issues: list[dict[str, object]] = []

    def add_issue(rows, code, **_kwargs):
        rows.append({"code": code})

    audit_boundary_contract(
        issue_adder=add_issue,
        issues=issues,
        stem=CANDIDATE,
        record_path=None,
        subtitle_path=Path(spec["subtitle_redelivery_baseline"]["path"]),
        exact_final_review={"boundary_semantic_review": final_review},
        record={
            "boundary_audit": {
                "boundary_semantic_review": source_review,
                "final_delivery_boundary_semantic_review": final_review,
                "boundary_authority": (
                    "operator_reviewed_exact_source_interval_plus_frozen_reviewed_timeline"
                ),
                "manual_end_mode": "reviewed_exact_source_interval_v1",
                "final_start_ms": 9750,
                "final_end_ms": 167060,
                "snapped_sentence_end_ms": 167000,
            }
        },
        story_contract={"boundary_semantic_review": final_review},
        required=True,
        is_song=False,
    )
    assert "BOUNDARY_SOURCE_SEPARATION_WITNESS_INVALID" not in {
        issue["code"] for issue in issues
    }

    wrong_source_interval = review_exact_delivery_boundary_semantics(
        cues=final_cues,
        source_boundary_review=source_review,
        source_final_start_ms=0,
        source_final_end_ms=157310,
        candidate_id=CANDIDATE,
        selection_hook=spec["selection_hook"],
        selection_scorecard=spec["selection_scorecard"],
        structured_context="",
        candidate_context="",
        boundary_max_forward_ms=0,
        llm_call=forbidden,
        extract_json=forbidden,
        disabled=True,
    )
    assert wrong_source_interval["status"] == "BLOCK"
    assert wrong_source_interval["reason_codes"] == [
        "REVIEWED_EXACT_SOURCE_INTERVAL_SOURCE_WITNESS_INTERVAL_INVALID"
    ]


def test_package_audit_accepts_exact_materialization_and_rejects_drift() -> None:
    spec = _source_spec()
    authority = _compile(spec)
    source_review = spec["boundary_semantic_review"]
    baseline_audit = {
        "status": "APPLIED",
        "application_strategy": "exact_reviewed_interval_replay",
        "baseline_sha256": authority["reviewed_subtitle"]["srt_sha256"].removeprefix("sha256:"),
        "expected_baseline_sha256": authority["reviewed_subtitle"]["srt_sha256"].removeprefix(
            "sha256:"
        ),
        "video_tail_extension_ms": 0,
        "source_recording_identity": {
            "expected_basename": authority["source"]["recording_basename"],
            "current_basename": authority["source"]["recording_basename"],
            "expected_sha256": authority["source"]["source_sha256"].removeprefix("sha256:"),
            "current_sha256": authority["source"]["source_sha256"].removeprefix("sha256:"),
        },
        "reviewed_coverage": {
            "absolute_source_start_ms": 577780,
            "absolute_source_end_ms": 735090,
        },
        "current_source_interval": {
            "absolute_source_start_ms": 577780,
            "absolute_source_end_ms": 735090,
        },
        # A full-window exact replay may subsequently crop its final delivery.
        # The owner audit still binds current/reviewed coverage to the full
        # registered interval; crop coordinates are a separate projection.
        "final_delivery_projection": {
            "absolute_source_start_ms": 587530,
            "absolute_source_end_ms": 734840,
        },
    }
    frozen = {RUNTIME_CONFIG_KEY: authority}
    assert _terminal_projection_materialization_valid(
        frozen=frozen,
        baseline_audit=baseline_audit,
        source_review=source_review,
    )
    drifted = copy.deepcopy(baseline_audit)
    drifted["current_source_interval"]["absolute_source_end_ms"] = 735089
    assert not _terminal_projection_materialization_valid(
        frozen=frozen,
        baseline_audit=drifted,
        source_review=source_review,
    )


def test_relocation_does_not_change_authority_identity() -> None:
    first = _source_spec()
    second = _source_spec()
    first["pieces"][0]["remote_media"] = "/home/ivan/wsl/22966160_20260807-22-37-50.mp4"
    second["pieces"][0]["remote_media"] = "/opt/bilive/free/22966160_20260807-22-37-50.mp4"
    assert _compile(first)["authority_sha256"] == _compile(second)["authority_sha256"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda spec: spec.update(candidate_id="other"),
            "REFERENCE_NOT_CANONICAL_REGISTRY_CONFIG",
        ),
        (lambda spec: spec.update(selection_hook="tampered"), "SELECTION_BINDING_MISMATCH"),
        (
            lambda spec: spec["selection_scorecard"].update(raw_score=1),
            "SELECTION_BINDING_MISMATCH",
        ),
        (
            lambda spec: spec["pieces"][0].update(source_media_sha256="sha256:" + "0" * 64),
            "SOURCE_BINDING_MISMATCH",
        ),
    ],
)
def test_current_binding_tampering_blocks(mutation, reason: str) -> None:
    spec = _source_spec()
    mutation(spec)
    with pytest.raises(ReviewedExactSourceIntervalError, match=reason):
        compile_authority_from_spec(
            spec=spec,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )


def test_declared_missing_and_multiple_authorities_block_without_fallback(
    tmp_path: Path,
) -> None:
    missing = _source_spec()
    missing["subtitle_redelivery_baseline"]["operator_reviewed_exact_source_interval"]["path"] = (
        str(tmp_path / "missing.json")
    )
    with pytest.raises(
        ReviewedExactSourceIntervalError,
        match="REFERENCE_NOT_CANONICAL_REGISTRY_CONFIG",
    ):
        compile_authority_from_spec(
            spec=missing,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )
    multiple = _source_spec()
    multiple[RUNTIME_CONFIG_KEY] = {"second": "authority"}
    with pytest.raises(ReviewedExactSourceIntervalError, match="MULTIPLE_ACTIVE_AUTHORITIES"):
        compile_authority_from_spec(
            spec=multiple,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )


def test_reviewed_srt_and_speaker_bytes_tampering_blocks(tmp_path: Path) -> None:
    srt_spec = _source_spec()
    baseline = Path(srt_spec["subtitle_redelivery_baseline"]["path"])
    bad_srt = tmp_path / "tampered.srt"
    bad_srt.write_bytes(baseline.read_bytes() + b"\n")
    srt_spec["subtitle_redelivery_baseline"]["path"] = str(bad_srt)
    with pytest.raises(
        ReviewedExactSourceIntervalError,
        match="REFERENCE_NOT_CANONICAL_REGISTRY_CONFIG",
    ):
        compile_authority_from_spec(
            spec=srt_spec,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )

    speaker_spec = _source_spec()
    speaker = Path(speaker_spec["speaker_overrides"])
    bad_speaker = tmp_path / "tampered-speaker.json"
    bad_speaker.write_bytes(speaker.read_bytes() + b"\n")
    speaker_spec["speaker_overrides"] = str(bad_speaker)
    with pytest.raises(
        ReviewedExactSourceIntervalError,
        match="SPEAKER_OVERRIDE_PATH_NOT_CANONICAL",
    ):
        compile_authority_from_spec(
            spec=speaker_spec,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda grant: grant["terminal"].update(media_end_anchor_ms=157309),
        lambda grant: grant["terminal"].update(last_cue_end_ms=156909, tail_ms=401),
        lambda grant: grant.update(policy_id="unsupported"),
        lambda grant: grant["frozen_semantic_verdict"].update(story_closed=False),
        lambda grant: grant["authenticated_review_receipt"].update(status="CLAIMED"),
    ],
)
def test_grant_tampering_blocks_even_with_rehashed_outer_authority(mutation) -> None:
    grant_path = (
        ROOT
        / "assets/lidousha/reviewed_exact_source_intervals"
        / f"{CANDIDATE}.reviewed-exact-source-interval.v1.json"
    )
    grant = json.loads(grant_path.read_text(encoding="utf-8"))
    mutation(grant)
    grant["authority_sha256"] = authority_sha256(grant)
    with pytest.raises(ReviewedExactSourceIntervalError):
        validate_runtime_authority(grant)


def test_output_span_overrun_blocks_without_fallback() -> None:
    spec = _source_spec()
    _compile(spec)
    with pytest.raises(ReviewedExactSourceIntervalError, match="OUTPUT_SOURCE_SPAN_OVERRUN"):
        resolve_exact_boundary(
            spec=spec,
            durations=[167059],
            padded_duration_ms=167059,
            fresh_cues=[],
            speech_spans=[],
        )


def test_half_open_authoritative_next_topic_contradiction() -> None:
    spec = _source_spec()
    _compile(spec)

    def receipt(start_ms: int) -> dict:
        value = {
            "schema_version": "operator-reviewed-next-topic-source-start.v1",
            "status": "CONFIRMED_NEXT_TOPIC",
            "candidate_id": CANDIDATE,
            "source_sha256": "sha256:" + SOURCE_SHA,
            "absolute_source_start_ms": start_ms,
            "operator_authority": "test-only independent human authority",
        }
        value["receipt_sha256"] = canonical_sha256(value)
        return value

    compatible = copy.deepcopy(spec)
    compatible[CONTRADICTION_CONFIG_KEY] = receipt(735090)
    assert (
        resolve_exact_boundary(
            spec=compatible,
            durations=[214200],
            padded_duration_ms=214200,
            fresh_cues=[],
            speech_spans=[],
        )
        is not None
    )
    contradicted = copy.deepcopy(spec)
    contradicted[CONTRADICTION_CONFIG_KEY] = receipt(735089)
    with pytest.raises(
        ReviewedExactSourceIntervalError,
        match="AUTHORITATIVE_NEXT_TOPIC_CONTRADICTION",
    ):
        resolve_exact_boundary(
            spec=contradicted,
            durations=[214200],
            padded_duration_ms=214200,
            fresh_cues=[],
            speech_spans=[],
        )


def test_tail_policy_accepts_inclusive_zero_and_400_but_rejects_401() -> None:
    grant_path = (
        ROOT
        / "assets/lidousha/reviewed_exact_source_intervals"
        / f"{CANDIDATE}.reviewed-exact-source-interval.v1.json"
    )
    base = json.loads(grant_path.read_text(encoding="utf-8"))
    for tail in (0, 400):
        grant = copy.deepcopy(base)
        anchor = grant["terminal"]["media_end_anchor_ms"]
        grant["terminal"]["last_cue_end_ms"] = anchor - tail
        grant["terminal"]["tail_ms"] = tail
        grant["authority_sha256"] = authority_sha256(grant)
        assert validate_runtime_authority(grant)["terminal"]["tail_ms"] == tail
    grant = copy.deepcopy(base)
    anchor = grant["terminal"]["media_end_anchor_ms"]
    grant["terminal"]["last_cue_end_ms"] = anchor - 401
    grant["terminal"]["tail_ms"] = 401
    grant["authority_sha256"] = authority_sha256(grant)
    with pytest.raises(ReviewedExactSourceIntervalError):
        validate_runtime_authority(grant)


def test_active_repository_seal_is_required_before_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _source_spec()

    def reject(**_kwargs):
        raise RepositoryAssetAuthorityError("not in active HEAD")

    monkeypatch.setattr(
        baseline_registry,
        "require_repository_asset_authority",
        reject,
    )
    with pytest.raises(ReviewedExactSourceIntervalError, match="AUTHORITY_ASSET_UNSEALED"):
        compile_authority_from_spec(
            spec=spec,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )


def test_direct_runtime_authority_injection_without_registry_reference_blocks() -> None:
    spec = _source_spec()
    authority = json.loads(
        (
            ROOT
            / "assets/lidousha/reviewed_exact_source_intervals"
            / f"{CANDIDATE}.reviewed-exact-source-interval.v1.json"
        ).read_text(encoding="utf-8")
    )
    spec["subtitle_redelivery_baseline"].pop("operator_reviewed_exact_source_interval")
    spec[RUNTIME_CONFIG_KEY] = authority
    with pytest.raises(
        ReviewedExactSourceIntervalError,
        match="UNSEALED_RUNTIME_AUTHORITY_INJECTION",
    ):
        compile_authority_from_spec(
            spec=spec,
            piece_provenance_rows=[{"source_sha256": SOURCE_SHA}],
        )
