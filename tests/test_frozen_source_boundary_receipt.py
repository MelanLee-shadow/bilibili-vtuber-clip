from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
    review_talk_boundary_semantics,
)
from src.autoslice.frozen_boundary_receipt import (
    _canonical_sha256,
    redelivery_baseline_boundary_binding,
)
from src.autoslice.frozen_source_boundary_receipt import (
    REFERENCE_SCHEMA_VERSION,
    SOURCE_DECISION_AUTHORITY,
    SOURCE_EXACT_REPLAY_MODE,
    SOURCE_REPLAY_REASON_CODE,
    load_boundary_review_authorities,
    load_frozen_source_boundary_receipt,
    normalized_generation_inputs,
)
from src.autoslice.jingting_chunker import SrtCue
from src.autoslice.producer_boundary_review_stage import (
    exact_delivery_correction_audit,
)


CID = "source-only-boundary-candidate"
SOURCE_NAME = "source.mp4"
SOURCE_SHA256 = "sha256:" + "a" * 64
PIECE_START_MS = 100_000
ABSOLUTE_START_MS = 101_000
ABSOLUTE_END_MS = 106_000
HOOK = "故事闭环后明确切换话题"
SCORECARD = {
    "schema_version": "lidousha-selection-scorecard.v1",
    "status": "VALID",
    "dimensions": {"self_contained": 4, "comedic_payoff": 4},
}


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _pass_response(recommended_index: int, evidence: list[int]) -> str:
    return json.dumps(
        {
            "syntax_complete": True,
            "story_closed": True,
            "next_topic_separated": True,
            "content_anchor_covered": True,
            "recommended_end_cue_index": recommended_index,
            "evidence_cue_indexes": evidence,
            "same_topic_continues_after_target": False,
            "needs_more_context": False,
            "reason_codes": ["SYNTHETIC_PASS"],
            "summary": "故事闭环，下一条已经换题。",
        },
        ensure_ascii=False,
    )


def _source_cues(*, shifted: bool = False) -> list[SrtCue]:
    if shifted:
        return [
            SrtCue("1", 1_000, 1_700, "开场"),
            SrtCue("2", 1_700, 2_500, "新增过渡"),
            SrtCue("3", 2_500, 5_600, "故事完整收束"),
            SrtCue("4", 6_500, 8_000, "下一个话题开始"),
        ]
    return [
        SrtCue("1", 1_000, 2_500, "开场铺垫"),
        SrtCue("2", 2_500, 5_600, "故事完整收束"),
        SrtCue("3", 6_500, 8_000, "下一个话题开始"),
    ]


def _scope() -> dict[str, object]:
    return build_boundary_search_scope(
        semantic_target_ms=5_600,
        repair_cap_ms=30_000,
        manual_lower_bound_ms=None,
        required_owner_end_ms=None,
        semantic_tail_trim_cap_ms=0,
        last_piece_start_ms=PIECE_START_MS,
    )


def _endpoint_reference(review: dict[str, object]) -> dict[str, object]:
    endpoint = review["final_endpoint_binding"]
    scope = review["boundary_search_scope"]
    assert isinstance(endpoint, dict) and isinstance(scope, dict)
    return {
        "request_sha256": review["request_sha256"],
        "cue_grid_sha256": review["cue_grid_sha256"],
        "boundary_search_scope_sha256": scope["scope_sha256"],
        "final_start_ms": endpoint["final_start_ms"],
        "final_end_ms": endpoint["final_end_ms"],
    }


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_source_only_spec(tmp_path: Path) -> tuple[dict, Path, Path]:
    cues = _source_cues()
    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=5_600,
        candidate_id=CID,
        selection_hook=HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="synthetic",
        llm_call=lambda _prompt: _pass_response(2, [2, 3]),
        extract_json=json.loads,
        max_forward_ms=30_000,
        boundary_search_scope=_scope(),
    )
    review, reasons = bind_final_semantic_endpoint(
        semantic_review=review,
        cues=cues,
        closure_cue=cues[1],
        snapped_end_ms=5_600,
        final_start_ms=1_000,
        final_end_ms=6_000,
    )
    assert reasons == []
    baseline_srt = (
        "1\n00:00:00,000 --> 00:00:01,500\n开场铺垫\n\n"
        "2\n00:00:01,500 --> 00:00:04,600\n故事完整收束\n"
    )
    baseline_path = tmp_path / "reviewed.srt"
    baseline_path.write_text(baseline_srt, encoding="utf-8")
    baseline = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "mode": "preserve_text_outside_source_truth",
        "exact_interval_replay": True,
        "path": str(baseline_path),
        "sha256": _sha256(baseline_path.read_bytes()),
        "authority": "synthetic reviewed bytes",
        "source_recording_basename": SOURCE_NAME,
        "source_sha256": SOURCE_SHA256,
        "absolute_source_start_ms": ABSOLUTE_START_MS,
        "absolute_source_end_ms": ABSOLUTE_END_MS,
    }
    spec = {
        "candidate_id": CID,
        "selection_hook": HOOK,
        "selection_scorecard": SCORECARD,
        "lead_pad_ms": 10_000,
        "semantic_start_ms": 11_000,
        "semantic_end_ms": 15_600,
        "boundary_repair_extend_cap_ms": 30_000,
        "semantic_tail_trim_cap_ms": 0,
        "subtitle_redelivery_baseline": baseline,
        "pieces": [
            {
                "remote_media": f"/recordings/{SOURCE_NAME}",
                "source_media_sha256": SOURCE_SHA256,
                "start_ms": PIECE_START_MS,
                "end_ms": 120_000,
            }
        ],
    }
    generation_path = tmp_path / "attempt-generation-spec.json"
    _write_json(generation_path, spec)
    audit_path = tmp_path / "attempt1.boundary-audit.json"
    _write_json(audit_path, {"boundary_semantic_review": review})
    normalized = normalized_generation_inputs(spec)
    assert normalized is not None
    baseline["frozen_source_boundary_receipt"] = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "candidate_id": CID,
        "path": str(audit_path),
        "sha256": _sha256(audit_path.read_bytes()),
        "json_path": "boundary_semantic_review",
        "source_review_sha256": _canonical_sha256(review),
        "redelivery_baseline": redelivery_baseline_boundary_binding(baseline),
        "source_full_window": _endpoint_reference(review),
        "generation_spec": {
            "path": str(generation_path),
            "sha256": _sha256(generation_path.read_bytes()),
            "normalized_inputs": normalized,
        },
        "final_delivery_policy": "FRESH_REQUIRED",
    }
    return spec, audit_path, generation_path


def _review_source(spec: dict, frozen_review: object, *, shifted: bool = False):
    replay: dict[str, object] = {}
    cues = _source_cues(shifted=shifted)
    calls = 0

    def forbidden(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("source-only carry must skip the source LLM")

    review = review_talk_boundary_semantics(
        cues=cues,
        target_ms=5_600,
        candidate_id=CID,
        selection_hook=HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="synthetic",
        llm_call=forbidden,
        extract_json=json.loads,
        max_forward_ms=30_000,
        boundary_search_scope=_scope(),
        frozen_review=frozen_review,
        replay_audit=replay,
    )
    return review, replay, calls


def test_source_only_carries_source_but_final_review_is_fresh(tmp_path: Path):
    spec, _audit_path, _generation_path = _build_source_only_spec(tmp_path)
    full, source = load_boundary_review_authorities(spec, candidate_id=CID)
    assert full is None and source is not None
    source_review, replay, source_calls = _review_source(spec, source)
    assert source_calls == 0
    assert replay["replay_mode"] == SOURCE_EXACT_REPLAY_MODE
    assert replay["decision_authority"] == SOURCE_DECISION_AUTHORITY
    assert replay["reason_code"] == SOURCE_REPLAY_REASON_CODE
    assert replay["authority_kind"] == "SOURCE_ONLY"
    assert replay["final_delivery_policy"] == "FRESH_REQUIRED"
    source_review, reasons = bind_final_semantic_endpoint(
        semantic_review=source_review,
        cues=_source_cues(),
        closure_cue=_source_cues()[1],
        snapped_end_ms=5_600,
        final_start_ms=1_000,
        final_end_ms=6_000,
    )
    assert reasons == []

    final_calls = 0

    def final_llm(_prompt: str) -> str:
        nonlocal final_calls
        final_calls += 1
        return _pass_response(2, [1, 2])

    result = exact_delivery_correction_audit(
        final_srt_text=(
            "1\n00:00:00,000 --> 00:00:01,500\n开场铺垫\n\n"
            "2\n00:00:01,500 --> 00:00:04,600\n故事完整收束\n"
        ),
        correction_audit={"boundary_semantic_review": source_review},
        source_final_start_ms=1_000,
        source_final_end_ms=6_000,
        candidate_id=CID,
        selection_hook=HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="fresh",
        boundary_max_forward_ms=30_000,
        llm_call=final_llm,
        extract_json=json.loads,
        frozen_boundary_receipt=full,
    )
    assert final_calls == 1
    assert result["boundary_semantic_review"]["status"] == "PASS"
    assert "frozen_decision_binding" not in result["boundary_semantic_review"]
    assert "boundary_receipt_replay" not in result


def test_source_only_projects_unique_closure_across_cue_grid_drift(tmp_path: Path):
    spec, _audit_path, _generation_path = _build_source_only_spec(tmp_path)
    receipt = load_frozen_source_boundary_receipt(spec, candidate_id=CID)
    assert receipt is not None
    review, replay, calls = _review_source(
        spec, receipt.source_full_window, shifted=True
    )
    assert calls == 0
    assert review["recommended_end_cue_index"] == 3
    assert replay["replay_mode"] == (
        "EXACT_INTERVAL_FROZEN_SOURCE_VERDICT_PROJECTION"
    )


def test_frozen_generation_legacy_piece_hash_uses_bound_v2_baseline(
    tmp_path: Path,
):
    spec, _audit_path, generation_path = _build_source_only_spec(tmp_path)
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    del generation["pieces"][0]["source_media_sha256"]
    _write_json(generation_path, generation)
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_source_boundary_receipt"
    ]
    normalized = normalized_generation_inputs(generation)
    assert normalized is not None
    assert normalized["pieces"][0]["source_media_sha256"] == SOURCE_SHA256
    reference["generation_spec"]["sha256"] = _sha256(
        generation_path.read_bytes()
    )
    reference["generation_spec"]["normalized_inputs"] = normalized

    assert load_frozen_source_boundary_receipt(spec, candidate_id=CID) is not None


def test_frozen_generation_explicit_piece_hash_cannot_disagree_with_baseline(
    tmp_path: Path,
):
    spec, _audit_path, generation_path = _build_source_only_spec(tmp_path)
    generation = json.loads(generation_path.read_text(encoding="utf-8"))
    generation["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    _write_json(generation_path, generation)
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_source_boundary_receipt"
    ]
    reference["generation_spec"]["sha256"] = _sha256(
        generation_path.read_bytes()
    )

    assert normalized_generation_inputs(generation) is None
    assert load_frozen_source_boundary_receipt(spec, candidate_id=CID) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "audit_bytes",
        "generation_bytes",
        "review_digest",
        "baseline_sha",
        "source_media",
        "piece_geometry",
        "selection_hook",
        "selection_scorecard",
        "closure_text",
        "endpoint",
        "missing_source_sha",
        "embed_final",
        "ambiguous_full",
        "given_end_ms",
        "given_end_mode",
        "given_end_authority",
    ],
)
def test_source_only_authority_tampering_falls_back_closed(
    tmp_path: Path, mutation: str
):
    spec, audit_path, generation_path = _build_source_only_spec(tmp_path)
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_source_boundary_receipt"
    ]
    if mutation == "audit_bytes":
        audit_path.write_bytes(audit_path.read_bytes() + b" ")
    elif mutation == "generation_bytes":
        generation_path.write_bytes(generation_path.read_bytes() + b" ")
    elif mutation == "review_digest":
        reference["source_review_sha256"] = "sha256:" + "b" * 64
    elif mutation == "baseline_sha":
        spec["subtitle_redelivery_baseline"]["sha256"] = "sha256:" + "b" * 64
    elif mutation == "source_media":
        spec["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    elif mutation == "piece_geometry":
        spec["pieces"][0]["start_ms"] += 1
    elif mutation == "selection_hook":
        spec["selection_hook"] += "篡改"
    elif mutation == "selection_scorecard":
        spec["selection_scorecard"]["dimensions"]["self_contained"] = 3
    elif mutation == "closure_text":
        baseline_path = Path(spec["subtitle_redelivery_baseline"]["path"])
        baseline_path.write_text(
            baseline_path.read_text(encoding="utf-8").replace(
                "故事完整收束", "故事完整收束呀"
            ),
            encoding="utf-8",
        )
        spec["subtitle_redelivery_baseline"]["sha256"] = _sha256(
            baseline_path.read_bytes()
        )
    elif mutation == "endpoint":
        reference["source_full_window"]["final_end_ms"] += 1
    elif mutation == "missing_source_sha":
        del spec["pieces"][0]["source_media_sha256"]
    elif mutation == "embed_final":
        reference["final_delivery"] = {"status": "PASS"}
    elif mutation == "ambiguous_full":
        spec["subtitle_redelivery_baseline"]["frozen_boundary_receipt"] = {}
    elif mutation == "given_end_ms":
        spec["given_end_ms"] = 119_000
    elif mutation == "given_end_mode":
        spec["given_end_mode"] = "published_recall_anchor"
    elif mutation == "given_end_authority":
        spec["given_end_authority"] = "synthetic drift"
    assert load_frozen_source_boundary_receipt(spec, candidate_id=CID) is None


@pytest.mark.parametrize("full_location", ["baseline", "top_level"])
def test_dual_boundary_authority_fails_closed_before_loading_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_location: str
):
    spec, _audit_path, _generation_path = _build_source_only_spec(tmp_path)
    if full_location == "baseline":
        spec["subtitle_redelivery_baseline"]["frozen_boundary_receipt"] = {
            "schema_version": "talk-boundary-frozen-receipt-ref.v1",
            "candidate_id": CID,
            "path": "/synthetic/valid-full-receipt.json",
            "sha256": "sha256:" + "c" * 64,
        }
    else:
        spec["frozen_boundary_receipt"] = {
            "schema_version": "talk-boundary-frozen-receipt-ref.v1",
            "candidate_id": CID,
            "path": "/synthetic/valid-full-receipt.json",
            "sha256": "sha256:" + "c" * 64,
        }

    def forbidden(*_args, **_kwargs):
        raise AssertionError("ambiguous authority must be rejected before loading")

    monkeypatch.setattr(
        "src.autoslice.frozen_source_boundary_receipt.load_frozen_boundary_receipt",
        forbidden,
    )
    monkeypatch.setattr(
        "src.autoslice.frozen_source_boundary_receipt.load_frozen_source_boundary_receipt",
        forbidden,
    )

    assert load_boundary_review_authorities(spec, candidate_id=CID) == (
        None,
        None,
    )


def test_source_only_rejects_self_hashed_invalid_scope(tmp_path: Path):
    spec, audit_path, _generation_path = _build_source_only_spec(tmp_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    review = audit["boundary_semantic_review"]
    scope = review["boundary_search_scope"]
    scope["required_local_source_context_end_ms"] = 1
    scope["scope_sha256"] = _canonical_sha256(
        {key: value for key, value in scope.items() if key != "scope_sha256"}
    )
    _write_json(audit_path, audit)
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_source_boundary_receipt"
    ]
    reference["sha256"] = _sha256(audit_path.read_bytes())
    reference["source_review_sha256"] = _canonical_sha256(review)
    reference["source_full_window"] = _endpoint_reference(review)
    assert load_frozen_source_boundary_receipt(spec, candidate_id=CID) is None
