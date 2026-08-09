from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint
from src.autoslice.boundary_semantic_review import (
    build_boundary_search_scope,
    review_talk_boundary_semantics,
)
from src.autoslice.frozen_boundary_receipt import (
    OWNER_PROJECTION_REFERENCE_SCHEMA_VERSION,
    REFERENCE_SCHEMA_VERSION,
    _source_scope_monotone_owner_floor_relaxation,
    load_frozen_boundary_receipt,
    redelivery_baseline_boundary_binding,
)
from src.autoslice.producer_boundary_owner_contract import (
    _normalized_owner_set,
    deterministic_owner_set_sha256,
    frozen_boundary_owner_contract_sha256,
)
from src.autoslice.jingting_chunker import SrtCue, parse_srt_cues
from src.autoslice.producer_boundary_review_stage import (
    exact_delivery_correction_audit,
    review_final_boundary_semantics,
)


CID = "frozen-boundary-candidate"
SOURCE_NAME = "source.mp4"
SOURCE_SHA256 = "sha256:" + "a" * 64
PIECE_START_MS = 100_000
ABSOLUTE_START_MS = 101_000
ABSOLUTE_END_MS = 106_000
SELECTION_HOOK = "信任拉扯终于完整收束"
SCORECARD = {
    "status": "VALID",
    "dimensions": {"self_contained": 4, "comedic_payoff": 4},
}


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _text_sha256(text: str) -> str:
    return _sha256(text.encode("utf-8"))


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _source_cues(*, opening: str = "故事开场铺垫") -> list[SrtCue]:
    return [
        SrtCue("1", 1_000, 2_500, opening),
        SrtCue("2", 2_500, 5_600, "故事完整收束"),
        SrtCue("3", 6_500, 8_000, "下一个话题开始"),
    ]


def _shifted_source_cues(*, opening: str) -> list[SrtCue]:
    return [
        SrtCue("1", 1_000, 1_700, opening),
        SrtCue("2", 1_700, 2_500, "fresh ASR 新拆出一条过渡 cue"),
        SrtCue("3", 2_500, 5_600, "故事完整收束"),
        SrtCue("4", 6_500, 8_000, "下一个话题开始"),
    ]


def _final_srt(*, closure: str = "故事完整收束") -> str:
    return (
        "1\n00:00:00,000 --> 00:00:01,500\n故事开场铺垫\n\n"
        f"2\n00:00:01,500 --> 00:00:04,600\n{closure}\n"
    )


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
            "reason_codes": ["FROZEN_TEST_PASS"],
            "summary": "故事闭环，后续已经换题。",
        },
        ensure_ascii=False,
    )


def _extract(payload: str) -> dict:
    return json.loads(payload)


def _scope(
    *,
    required_owner_end_ms: int | None = None,
    semantic_tail_trim_cap_ms: int = 0,
    repair_cap_ms: int = 30_000,
    manual_lower_bound_ms: int | None = None,
) -> dict[str, object]:
    return build_boundary_search_scope(
        semantic_target_ms=5_600,
        repair_cap_ms=repair_cap_ms,
        manual_lower_bound_ms=manual_lower_bound_ms,
        required_owner_end_ms=required_owner_end_ms,
        semantic_tail_trim_cap_ms=semantic_tail_trim_cap_ms,
        last_piece_start_ms=PIECE_START_MS,
    )


def _endpoint_reference(review: dict[str, object]) -> dict[str, object]:
    endpoint = review["final_endpoint_binding"]
    scope = review["boundary_search_scope"]
    assert isinstance(endpoint, dict)
    assert isinstance(scope, dict)
    return {
        "request_sha256": review["request_sha256"],
        "cue_grid_sha256": review["cue_grid_sha256"],
        "boundary_search_scope_sha256": scope["scope_sha256"],
        "final_start_ms": endpoint["final_start_ms"],
        "final_end_ms": endpoint["final_end_ms"],
    }


def _owner_contract(
    *,
    scope: dict[str, object],
    owners: list[dict[str, object]],
) -> dict[str, object]:
    owner_scope: dict[str, object] = {
        "schema_version": "candidate-boundary-owner-scope.v1",
        "candidate_id": CID,
        "story_start_ms": 1_000,
        "story_end_ms": 5_600,
    }
    owner_scope["scope_sha256"] = _canonical_sha256(owner_scope)
    contract: dict[str, object] = {
        "schema_version": "frozen-boundary-owner-contract.v1",
        "status": "FROZEN",
        "story_start_ms": 1_000,
        "story_end_ms": 5_600,
        "boundary_review_target_ms": scope["review_target_ms"],
        "owner_discovery_end_ms": 5_600,
        "required_owner_count": len(owners),
        "owners": owners,
        "owner_eligibility_scope": owner_scope,
        "owner_set_sha256": _canonical_sha256(
            _normalized_owner_set(owners)
        ),
        "deterministic_owner_set_sha256": (
            deterministic_owner_set_sha256(owners)
        ),
        "boundary_search_scope": scope,
    }
    contract["contract_sha256"] = (
        frozen_boundary_owner_contract_sha256(contract)
    )
    return contract


def _attach_owner_projection_support(
    *,
    spec: dict,
    record_path: Path,
    support_path: Path,
    frozen_contract: dict[str, object],
) -> None:
    support_path.write_text(
        json.dumps(
            {"frozen_boundary_owner_contract": frozen_contract},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    support_sha256 = _sha256(support_path.read_bytes())
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["artifact_hashes"]["chat_authority_audit_sha256"] = (
        support_sha256
    )
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_boundary_receipt"
    ]
    reference["sha256"] = _sha256(record_path.read_bytes())
    reference["source_owner_projection"] = {
        "schema_version": OWNER_PROJECTION_REFERENCE_SCHEMA_VERSION,
        "path": str(support_path),
        "sha256": support_sha256,
        "json_path": "frozen_boundary_owner_contract",
        "contract_sha256": frozen_contract["contract_sha256"],
        "owner_set_sha256": frozen_contract["owner_set_sha256"],
        "deterministic_owner_set_sha256": frozen_contract[
            "deterministic_owner_set_sha256"
        ],
        "owner_eligibility_scope_sha256": frozen_contract[
            "owner_eligibility_scope"
        ]["scope_sha256"],
    }


def _build_spec_and_record(
    tmp_path: Path,
    *,
    source_scope: dict[str, object] | None = None,
) -> tuple[dict, Path, str, dict]:
    source_cues = _source_cues()
    source_scope = source_scope or _scope()
    source_review = review_talk_boundary_semantics(
        cues=source_cues,
        target_ms=5_600,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="frozen context",
        llm_call=lambda _prompt: _pass_response(2, [2, 3]),
        extract_json=_extract,
        max_forward_ms=30_000,
        boundary_search_scope=source_scope,
    )
    source_review, reasons = bind_final_semantic_endpoint(
        semantic_review=source_review,
        cues=source_cues,
        closure_cue=source_cues[1],
        snapped_end_ms=5_600,
        final_start_ms=1_000,
        final_end_ms=6_000,
    )
    assert reasons == []

    final_srt = _final_srt()
    correction = {"boundary_semantic_review": source_review}
    frozen_correction = exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction,
        source_final_start_ms=1_000,
        source_final_end_ms=6_000,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="frozen context",
        boundary_max_forward_ms=30_000,
        llm_call=lambda _prompt: _pass_response(2, [1, 2]),
        extract_json=_extract,
    )
    final_review = frozen_correction["boundary_semantic_review"]
    assert final_review["status"] == "PASS"

    baseline_path = tmp_path / "reviewed.srt"
    baseline_path.write_text(final_srt, encoding="utf-8")
    final_sha256 = _sha256(final_srt.encode("utf-8"))
    transcript = "\n".join(cue.text for cue in parse_srt_cues(final_srt))
    record = {
        "artifact_hashes": {"subtitle_sha256": final_sha256},
        "boundary_audit": {
            "boundary_semantic_review": source_review,
            "final_delivery_boundary_semantic_review": final_review,
        },
        "selection_scorecard": SCORECARD,
        "story_contract": {
            "candidate_id": CID,
            "selection_hook": SELECTION_HOOK,
            "selection_scorecard": SCORECARD,
            "source_media_sha256s": [SOURCE_SHA256],
            "transcript_sha256": _text_sha256(transcript),
        },
        "redelivery_baseline": {
            "application_strategy": "exact_reviewed_interval_replay",
            "current_source_interval": {
                "absolute_source_start_ms": ABSOLUTE_START_MS,
                "absolute_source_end_ms": ABSOLUTE_END_MS,
            },
        },
    }
    record_path = tmp_path / "pristine.record.json"
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    baseline = {
        "schema_version": "subtitle-redelivery-baseline.v2",
        "mode": "preserve_text_outside_source_truth",
        "exact_interval_replay": True,
        "path": str(baseline_path),
        "sha256": final_sha256,
        "authority": "test pristine reviewed bytes",
        "source_recording_basename": SOURCE_NAME,
        "source_sha256": SOURCE_SHA256,
        "absolute_source_start_ms": ABSOLUTE_START_MS,
        "absolute_source_end_ms": ABSOLUTE_END_MS,
    }
    reference = {
        "schema_version": REFERENCE_SCHEMA_VERSION,
        "candidate_id": CID,
        "path": str(record_path),
        "sha256": _sha256(record_path.read_bytes()),
        "redelivery_baseline": redelivery_baseline_boundary_binding(baseline),
        "source_full_window": _endpoint_reference(source_review),
        "final_delivery": {
            **_endpoint_reference(final_review),
            "subtitle_sha256": final_sha256,
        },
    }
    baseline["frozen_boundary_receipt"] = reference
    spec = {
        "candidate_id": CID,
        "selection_hook": SELECTION_HOOK,
        "selection_scorecard": SCORECARD,
        "subtitle_redelivery_baseline": baseline,
        "pieces": [
            {
                "start_ms": PIECE_START_MS,
                "end_ms": 120_000,
                "remote_media": f"/recordings/{SOURCE_NAME}",
                "source_media_sha256": SOURCE_SHA256,
            }
        ],
    }
    return spec, record_path, final_srt, source_review


def _review_source(
    *,
    spec: dict,
    frozen_review: object,
    llm_call,
    opening: str,
    shifted_grid: bool = False,
    source_scope: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    replay_audit: dict[str, object] = {}
    cues = (
        _shifted_source_cues(opening=opening)
        if shifted_grid
        else _source_cues(opening=opening)
    )
    review = review_final_boundary_semantics(
        cues=cues,
        boundary_target_ms=5_600,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="fresh context",
        boundary_max_forward_ms=30_000,
        llm_call=llm_call,
        extract_json=_extract,
        boundary_search_scope=source_scope or _scope(),
        available_local_source_context_end_ms=(source_scope or _scope())[
            "required_local_source_context_end_ms"
        ],
        frozen_review=frozen_review,
        replay_audit=replay_audit,
    )
    return review, replay_audit


def test_exact_replay_allows_only_monotone_source_owner_floor_relaxation(
    tmp_path: Path,
):
    frozen_scope = _scope(
        required_owner_end_ms=5_000,
        semantic_tail_trim_cap_ms=1_500,
    )
    current_scope = _scope(semantic_tail_trim_cap_ms=1_500)
    spec, record_path, _final_srt, _source = _build_spec_and_record(
        tmp_path,
        source_scope=frozen_scope,
    )
    frozen_contract = _owner_contract(
        scope=frozen_scope,
        owners=[
            {
                "owner_kind": "entity_repair",
                "owner_id": "entity-owner-at-tail",
                "required": True,
                "local_windows": [{"start_ms": 4_500, "end_ms": 5_000}],
            }
        ],
    )
    current_contract = _owner_contract(scope=current_scope, owners=[])
    _attach_owner_projection_support(
        spec=spec,
        record_path=record_path,
        support_path=tmp_path / "frozen.chat-authority.json",
        frozen_contract=frozen_contract,
    )
    receipt = load_frozen_boundary_receipt(
        spec,
        candidate_id=CID,
        current_owner_contract=current_contract,
    )
    assert receipt is not None

    calls = 0

    def forbidden_llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("monotone owner-floor replay must not call the LLM")

    review, replay = _review_source(
        spec=spec,
        frozen_review=receipt.source_full_window,
        llm_call=forbidden_llm,
        opening="故事开场铺垫",
        source_scope=current_scope,
    )

    assert calls == 0
    assert review["status"] == "PASS"
    assert replay["replay_mode"] == (
        "EXACT_INTERVAL_FROZEN_VERDICT_PROJECTION"
    )
    assert replay["scope_projection_mode"] == (
        "MONOTONE_REQUIRED_OWNER_FLOOR_RELAXATION"
    )
    assert replay["frozen_required_owner_end_ms"] == 5_000
    assert replay["current_required_owner_end_ms"] is None
    assert replay["frozen_delivery_lower_bound_ms"] == 5_000
    assert replay["current_delivery_lower_bound_ms"] == 4_100

    for unsafe_scope in (
        _scope(
            required_owner_end_ms=5_200,
            semantic_tail_trim_cap_ms=1_500,
        ),
        _scope(
            semantic_tail_trim_cap_ms=1_500,
            repair_cap_ms=29_000,
        ),
        _scope(
            required_owner_end_ms=5_000,
            semantic_tail_trim_cap_ms=1_500,
            manual_lower_bound_ms=5_800,
        ),
    ):
        assert _source_scope_monotone_owner_floor_relaxation(
            frozen_scope,
            unsafe_scope,
            owner_projection_evidence=(
                receipt.source_full_window.owner_floor_projection
            ),
        ) is None

    digest_tamper = copy.deepcopy(current_scope)
    digest_tamper["delivery_lower_bound_ms"] = 4_099
    assert _source_scope_monotone_owner_floor_relaxation(
        frozen_scope,
        digest_tamper,
        owner_projection_evidence=(
            receipt.source_full_window.owner_floor_projection
        ),
    ) is None


def test_owner_floor_projection_rejects_immutable_owner_removal(
    tmp_path: Path,
):
    frozen_scope = _scope(
        required_owner_end_ms=5_000,
        semantic_tail_trim_cap_ms=1_500,
    )
    current_scope = _scope(semantic_tail_trim_cap_ms=1_500)
    spec, record_path, _final_srt, _source = _build_spec_and_record(
        tmp_path,
        source_scope=frozen_scope,
    )
    frozen_contract = _owner_contract(
        scope=frozen_scope,
        owners=[
            {
                "owner_kind": "source_subtitle_truth",
                "owner_id": "immutable-ledger-owner",
                "required": True,
                "local_windows": [{"start_ms": 4_500, "end_ms": 5_000}],
            }
        ],
    )
    _attach_owner_projection_support(
        spec=spec,
        record_path=record_path,
        support_path=tmp_path / "immutable-owner.chat-authority.json",
        frozen_contract=frozen_contract,
    )

    assert load_frozen_boundary_receipt(
        spec,
        candidate_id=CID,
        current_owner_contract=_owner_contract(
            scope=current_scope,
            owners=[],
        ),
    ) is None
    assert load_frozen_boundary_receipt(
        spec,
        candidate_id=CID,
        current_owner_contract=None,
    ) is None


def test_owner_floor_projection_rejects_self_hashed_invalid_frozen_scope(
    tmp_path: Path,
):
    valid_frozen_scope = _scope(
        required_owner_end_ms=5_000,
        semantic_tail_trim_cap_ms=1_500,
    )
    current_scope = _scope(semantic_tail_trim_cap_ms=1_500)
    spec, record_path, _final_srt, _source = _build_spec_and_record(
        tmp_path,
        source_scope=valid_frozen_scope,
    )
    invalid_scope = copy.deepcopy(valid_frozen_scope)
    invalid_scope.update(
        required_owner_end_ms=40_000,
        delivery_lower_bound_ms=40_000,
        minimum_recommended_end_ms=40_000,
        recommendation_backward_ms=0,
    )
    invalid_scope["scope_sha256"] = _canonical_sha256(
        {
            key: value
            for key, value in invalid_scope.items()
            if key != "scope_sha256"
        }
    )
    record = json.loads(record_path.read_text(encoding="utf-8"))
    source_review = record["boundary_audit"]["boundary_semantic_review"]
    source_review["boundary_search_scope"] = invalid_scope
    final_review = record["boundary_audit"][
        "final_delivery_boundary_semantic_review"
    ]
    final_review["source_separation_witness"]["source_review_sha256"] = (
        _canonical_sha256(source_review)
    )
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    reference = spec["subtitle_redelivery_baseline"][
        "frozen_boundary_receipt"
    ]
    reference["source_full_window"][
        "boundary_search_scope_sha256"
    ] = invalid_scope["scope_sha256"]
    frozen_contract = _owner_contract(
        scope=invalid_scope,
        owners=[
            {
                "owner_kind": "entity_repair",
                "owner_id": "impossible-owner-tail",
                "required": True,
                "local_windows": [
                    {"start_ms": 39_000, "end_ms": 40_000}
                ],
            }
        ],
    )
    _attach_owner_projection_support(
        spec=spec,
        record_path=record_path,
        support_path=tmp_path / "invalid-scope.chat-authority.json",
        frozen_contract=frozen_contract,
    )

    assert load_frozen_boundary_receipt(
        spec,
        candidate_id=CID,
        current_owner_contract=_owner_contract(
            scope=current_scope,
            owners=[],
        ),
    ) is None


def test_exact_replay_carries_both_verdicts_with_zero_llm_calls(tmp_path: Path):
    spec, _record_path, final_srt, frozen_source = _build_spec_and_record(
        tmp_path
    )
    receipt = load_frozen_boundary_receipt(spec, candidate_id=CID)
    assert receipt is not None
    assert receipt.source_full_window.exact_interval_projection is True

    calls = 0

    def forbidden_llm(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("frozen replay must not call the LLM")

    source_review, source_replay = _review_source(
        spec=spec,
        frozen_review=receipt.source_full_window,
        llm_call=forbidden_llm,
        opening="fresh ASR 的非终端文字发生漂移",
        shifted_grid=True,
    )
    current_source_cues = _shifted_source_cues(
        opening="fresh ASR 的非终端文字发生漂移"
    )
    source_review, reasons = bind_final_semantic_endpoint(
        semantic_review=source_review,
        cues=current_source_cues,
        closure_cue=current_source_cues[2],
        snapped_end_ms=5_600,
        final_start_ms=1_000,
        final_end_ms=6_000,
    )
    assert reasons == []
    assert source_review["status"] == frozen_source["status"] == "PASS"
    assert source_review["summary"] == frozen_source["summary"]
    assert source_replay["replay_mode"] == (
        "EXACT_INTERVAL_FROZEN_VERDICT_PROJECTION"
    )
    assert source_replay["llm_call_skipped"] is True
    assert source_replay["frozen_cue_grid_sha256"] != source_replay[
        "current_cue_grid_sha256"
    ]
    assert source_replay["frozen_recommended_end_cue_index"] == 2
    assert source_replay["current_recommended_end_cue_index"] == 3
    assert source_review["recommended_end_cue_index"] == 3

    correction = {
        "boundary_semantic_review": source_review,
        "boundary_receipt_replay": {
            "source_full_window": source_replay,
        },
    }
    result = exact_delivery_correction_audit(
        final_srt_text=final_srt,
        correction_audit=correction,
        source_final_start_ms=1_000,
        source_final_end_ms=6_000,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="fresh context",
        boundary_max_forward_ms=30_000,
        llm_call=forbidden_llm,
        extract_json=_extract,
        frozen_boundary_receipt=receipt,
    )

    final_review = result["boundary_semantic_review"]
    assert calls == 0
    assert final_review["status"] == "PASS"
    assert final_review["frozen_decision_binding"]["llm_call_skipped"] is True
    final_replay = result["boundary_receipt_replay"]["final_delivery"]
    assert final_replay["llm_call_skipped"] is True
    assert final_replay["frozen_request_sha256"] != final_replay[
        "current_request_sha256"
    ]


def test_record_byte_tamper_falls_back_to_fresh_review(tmp_path: Path):
    spec, record_path, _final_srt, _source = _build_spec_and_record(tmp_path)
    record_path.write_bytes(record_path.read_bytes() + b" ")
    assert load_frozen_boundary_receipt(spec, candidate_id=CID) is None

    calls = 0

    def fresh(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _pass_response(2, [2, 3])

    review, replay = _review_source(
        spec=spec, frozen_review=None, llm_call=fresh, opening="故事开场铺垫"
    )
    assert calls == 1
    assert review["status"] == "PASS"
    assert replay == {}


def test_source_identity_or_terminal_anchor_mismatch_uses_fresh_review(
    tmp_path: Path,
):
    spec, _record_path, _final_srt, _source = _build_spec_and_record(tmp_path)
    wrong_source = copy.deepcopy(spec)
    wrong_source["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    assert load_frozen_boundary_receipt(wrong_source, candidate_id=CID) is None
    calls = 0

    def fresh(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _pass_response(2, [2, 3])

    wrong_review, wrong_replay = _review_source(
        spec=wrong_source,
        frozen_review=None,
        llm_call=fresh,
        opening="故事开场铺垫",
    )
    assert wrong_review["status"] == "PASS"
    assert wrong_replay == {}

    receipt = load_frozen_boundary_receipt(spec, candidate_id=CID)
    assert receipt is not None

    changed = _source_cues()
    changed[1] = SrtCue("2", 2_500, 5_600, "故事收束被改了一字")
    replay = {}
    review = review_final_boundary_semantics(
        cues=changed,
        boundary_target_ms=5_600,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="fresh context",
        boundary_max_forward_ms=30_000,
        llm_call=fresh,
        extract_json=_extract,
        boundary_search_scope=_scope(),
        available_local_source_context_end_ms=_scope()[
            "required_local_source_context_end_ms"
        ],
        frozen_review=receipt.source_full_window,
        replay_audit=replay,
    )
    assert calls == 2
    assert review["status"] == "PASS"
    assert replay == {}


def test_final_srt_one_byte_tamper_uses_fresh_review(tmp_path: Path):
    spec, _record_path, final_srt, source_review = _build_spec_and_record(tmp_path)
    receipt = load_frozen_boundary_receipt(spec, candidate_id=CID)
    assert receipt is not None
    calls = 0

    def fresh(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _pass_response(2, [1, 2])

    result = exact_delivery_correction_audit(
        final_srt_text=final_srt.replace("故事开场铺垫", "故事开场铺垫呀", 1),
        correction_audit={"boundary_semantic_review": source_review},
        source_final_start_ms=1_000,
        source_final_end_ms=6_000,
        candidate_id=CID,
        selection_hook=SELECTION_HOOK,
        selection_scorecard=SCORECARD,
        structured_context="",
        candidate_context="fresh context",
        boundary_max_forward_ms=30_000,
        llm_call=fresh,
        extract_json=_extract,
        frozen_boundary_receipt=receipt,
    )
    assert calls == 1
    assert result["boundary_semantic_review"]["status"] == "PASS"
    assert "boundary_receipt_replay" not in result


def test_missing_receipt_and_normal_spec_keep_live_behavior(tmp_path: Path):
    spec, _record_path, _final_srt, _source = _build_spec_and_record(tmp_path)
    del spec["subtitle_redelivery_baseline"]["frozen_boundary_receipt"]
    assert load_frozen_boundary_receipt(spec, candidate_id=CID) is None
    normal_spec = {
        "frozen_boundary_receipt": {
            "path": str(tmp_path / "must-not-be-read.json"),
            "sha256": "sha256:" + "0" * 64,
        }
    }
    assert load_frozen_boundary_receipt(normal_spec, candidate_id=CID) is None

    calls = 0

    def fresh(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _pass_response(2, [2, 3])

    review, replay = _review_source(
        spec=spec, frozen_review=None, llm_call=fresh, opening="故事开场铺垫"
    )
    assert calls == 1
    assert review["status"] == "PASS"
    assert replay == {}
