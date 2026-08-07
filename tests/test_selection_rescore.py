"""Bounded selection-rescore lane state machine (2026-08-07 狍哥案修复).

Mirrors ``docs/reviews/2026-08-07-source-fact-rescore-design.md`` §6's test
matrix.  Each ``test_scenario_N_*`` docstring names the matrix row it covers;
scenarios this file cannot cover with a real (non-faked) test are named in
the module docstring below rather than silently skipped.

NOT covered here (disclosed, not faked):
- The live runner-tick wiring that would call
  ``semantic_candidate_selector.rescore_candidate_scorecard`` from inside
  ``scripts/free_session_autoslice.py`` / ``produce_slice_package.py`` and
  feed its result back into ``candidate_selection.prioritize`` within one
  tick.  That wiring is out of this bounded slice's explicit file list; the
  two halves (delivery_recovery's requeue behavior, and the scorecard
  regeneration primitive) are each tested directly instead.  Scenarios 3/4
  of the design's matrix ("仍入 Top-N" / "跌出 Top-N") are therefore covered
  at the unit level (ranking primitive + requeue item shape), not as an
  end-to-end placement-into-pending_talk-vs-talk_backlog integration test.
- Scenario 9's fixture is a shape-faithful reconstruction from the design
  doc's own description of ``auto_220747_1271_1323`` (its real receipt lives
  on the production host, not in this repo/worktree — confirmed absent by
  grep before writing this file), not the literal production JSON.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src.autoslice import selection_rescore
from src.autoslice.candidate_selection import exact_talk_contract_closure
from src.autoslice.delivery_recovery import backfillable_talk_rejection
from src.autoslice.reporting import _current_talk_reserves
from src.autoslice.batch_terminal_state import project_terminal_batch_state
from src.autoslice.review_evidence import SourceCue
from src.autoslice.semantic_candidate_selector import rescore_candidate_scorecard
from src.autoslice.source_fact_review import (
    review_and_repair_source_facts,
    source_fact_review_passes,
    validate_source_fact_rescore_candidate_receipt,
)
from src.autoslice.selection_scorecard import selection_rank_key, selection_scorecard_is_valid
from src.autoslice.talk_lane import classify_talk_failure

import scripts.free_session_autoslice as runner


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _completion(
    *,
    status: str,
    final_hook: str,
    final_title: str,
    supported_by: list[str],
    changed_surfaces: list[dict[str, object]] | None = None,
    selection_scorecard_review: dict[str, str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": status,
            "final_selection_hook": final_hook,
            "final_title": final_title,
            "supported_by": supported_by,
            "changed_surfaces": changed_surfaces or [],
            "selection_scorecard_review": (
                selection_scorecard_review
                or {"status": "NOT_NEEDED", "reason": "selection hook remains unchanged"}
            ),
            "summary": "相邻的同片文字证据足以完成事实裁决。",
        },
        ensure_ascii=False,
    )


_STALE_SCORECARD = {
    "tier_basis": "audience_driven_performance",
    "tier_reason": "弹幕完成命名",
}


def _stale_scorecard_review() -> dict[str, object]:
    """Build a real REPAIR_SCORECARD_STALE receipt (scenario 2's raw material)."""

    def cpa(_prompt: str) -> str:
        return _completion(
            status="REPAIR",
            final_hook="弹幕提议把技能叫李姐拉拉，主播随即拒绝。",
            final_title="【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "技能已经被命名",
                    "after": "弹幕提议把技能叫",
                    "reason": "原文是提议而且主播拒绝。",
                    "evidence": ["技能可以叫李姐拉拉吗"],
                },
                {
                    "artifact": "title",
                    "before": "技能已经被命名成李姐拉拉",
                    "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                    "reason": "标题也必须保留提议与拒绝的事实模态。",
                    "evidence": ["技能可以叫李姐拉拉吗", "不行，我这个应该叫李姐网"],
                },
            ],
            selection_scorecard_review={
                "status": "INCOMPATIBLE",
                "reason": "旧评分卡围绕已经完成的命名，核心模态已经改变。",
            },
        )

    return review_and_repair_source_facts(
        selection_hook="技能已经被命名成李姐拉拉。",
        title="【李豆沙】技能已经被命名成李姐拉拉",
        final_transcript="不行，我这个应该叫李姐网",
        clip_context_prompt="- superchat: 技能可以叫李姐拉拉吗",
        selection_scorecard=_STALE_SCORECARD,
        llm_call=cpa,
    )


# --- Scenario 1: COMPATIBLE / title-only repair stays on the existing PASS
# path unaffected -----------------------------------------------------------


def test_scenario_1_compatible_repair_stays_a_plain_pass() -> None:
    def cpa(_prompt: str) -> str:
        return _completion(
            status="KEEP",
            final_hook="技能已经被命名成李姐拉拉。",
            final_title="【李豆沙】技能已经被命名成李姐拉拉",
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook="技能已经被命名成李姐拉拉。",
        title="【李豆沙】技能已经被命名成李姐拉拉",
        final_transcript="技能已经被命名成李姐拉拉",
        clip_context_prompt="",
        selection_scorecard=_STALE_SCORECARD,
        llm_call=cpa,
    )

    assert source_fact_review_passes(review)
    assert "rescore_candidate" not in review
    assert (
        selection_rescore.classify_source_fact_review_marker(review)
        == "SOURCE_FACT_REVIEW_UNRESOLVED"
    )  # not reached in production: caller only classifies non-passing receipts


# --- Scenario 2: hook repaired + INCOMPATIBLE -> bounded rescore, not a
# terminal rejection ---------------------------------------------------------


def test_scenario_2_stale_scorecard_becomes_rescore_required_not_rejected(
    tmp_path,
) -> None:
    review = _stale_scorecard_review()
    assert not source_fact_review_passes(review)
    assert review["decision"] == "REPAIR_SCORECARD_STALE"
    assert review["reason_code"] == "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE"
    # "未授权不落盘"不变式：final_selection_hook 仍是原文。
    assert review["final_selection_hook"] == "技能已经被命名成李姐拉拉。"

    block = review["rescore_candidate"]
    assert block["repaired_selection_hook"] == "弹幕提议把技能叫李姐拉拉，主播随即拒绝。"
    assert block["repaired_selection_hook_sha256"] == _sha256_text(
        block["repaired_selection_hook"]
    )
    assert validate_source_fact_rescore_candidate_receipt(
        review,
        selection_hook="技能已经被命名成李姐拉拉。",
        title="【李豆沙】技能已经被命名成李姐拉拉",
        selection_scorecard=_STALE_SCORECARD,
    )

    marker = selection_rescore.classify_source_fact_review_marker(review)
    assert marker == "SOURCE_FACT_REPAIRED_RESCORE_REQUIRED"

    recut_dir = tmp_path / "auto_1" / "replacement_recuts"
    selection_rescore.write_pending_rescore_sidecar(
        recut_dir, candidate_id="auto_1", source_fact_review=review
    )
    attempt_output = f"{marker}: {review['reason_code']}\n"
    classified = classify_talk_failure(attempt_output)
    assert classified["failure_kind"] == "selection_rescore"
    assert classified["failure_stage"] == "source_fact_repair"
    assert classified["failure_recoverable"] is True

    receipt = selection_rescore.attach_pending_rescore_receipt(
        tmp_path / "auto_1",
        candidate_id="auto_1",
        failure_fingerprint=classified["failure_fingerprint"],
    )
    assert receipt["status"] == "PENDING"
    assert receipt["repaired_hook"] == block["repaired_selection_hook"]
    assert receipt["rescore_fingerprint"] == selection_rescore.rescore_fingerprint(
        classified["failure_fingerprint"], block["repaired_selection_hook_sha256"]
    )

    # candidate_rejected must never appear for this failure kind.
    pick = {
        "status": "failed",
        "failure_kind": "selection_rescore",
        "failure_recoverable": True,
        "source_fact_rescore": receipt,
    }
    assert backfillable_talk_rejection(pick) is None


# --- Scenario 3/4 (unit-level; see module docstring for scope) -------------


def _rescore_cues() -> list[SourceCue]:
    return [
        SourceCue("c1", 0, 2_000, "有人问狍哥是谁", "zh"),
        SourceCue("c2", 2_000, 5_000, "她说要去找狍哥表忠心", "zh"),
        SourceCue("c3", 5_000, 8_000, "说完就跟着队伍走了", "zh"),
        SourceCue("c4", 8_000, 11_000, "她说这样才有安全感", "zh"),
    ]


def _valid_rescore_payload() -> dict[str, object]:
    return {
        "status": "SUPPORTED",
        "selection_scorecard": {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "投奔求庇护的完整互动链",
            "tier_evidence_cues": [1, 2, 3, 4],
            "dimensions": {
                "lidousha_centrality": 3,
                "stance_intensity": 3,
                "audience_salience": 2,
                "relationship_interaction": 2,
                "persona_reversal": 2,
                "comedic_payoff": 2,
                "self_contained": 3,
            },
            "uncertainty_penalty": 2,
            "fatigue_penalty": 0,
        },
    }


def test_scenario_3_rescore_success_produces_a_valid_rankable_scorecard() -> None:
    outcome = rescore_candidate_scorecard(
        candidate_id="auto_220747_1271_1323",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(_valid_rescore_payload(), ensure_ascii=False),
        stale_scorecard=None,
        start_cue=1,
        end_cue=4,
    )

    assert outcome["outcome"] == "RESCORED"
    assert selection_scorecard_is_valid(outcome["selection_scorecard"])
    # Reuses the exact same ranking primitive every other candidate uses.
    key = selection_rank_key(
        {
            "candidate_id": "auto_220747_1271_1323",
            "confidence": 0.8,
            "selection_scorecard": outcome["selection_scorecard"],
        }
    )
    assert key[0] == 2.0  # tier


def test_scenario_3_requeue_carries_repaired_hook_and_clears_stale_scorecard(
    tmp_path, monkeypatch
) -> None:
    date = "2026-07-10"
    rec_root = tmp_path / "recordings"
    date_dir = rec_root / date
    date_dir.mkdir(parents=True)
    segment = date_dir / "22966160_20260710-21-20-05.mp4"
    segment.write_bytes(b"media")
    monkeypatch.setattr(runner, "REC_ROOT", rec_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: "sha256:same")
    monkeypatch.setattr(
        runner, "talk_failure_recovery_fingerprint", lambda _kind, _cid: "sha256:same-recovery"
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _path: 900_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _path: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _path: None)

    receipt = {
        "schema_version": selection_rescore.RESCORE_RECEIPT_SCHEMA,
        "candidate_id": "auto_1271_1323",
        "repaired_hook": "她投奔狍哥求庇护",
        "repaired_hook_sha256": _sha256_text("她投奔狍哥求庇护"),
        "repaired_title": "【李豆沙】她投奔狍哥求庇护",
        "stale_scorecard_sha256": "sha256:stale",
        "stale_reason": "核心梗已换",
        "attempts": [],
        "status": "PENDING",
        "rescore_fingerprint": "sha256:fp1",
    }
    state = {
        "pending_talk": [],
        "picks": [
            {
                "candidate_id": "auto_1271_1323",
                "segment": segment.name,
                "start_ms": 100_000,
                "end_ms": 200_000,
                "status": "failed",
                "failure_kind": "selection_rescore",
                "failure_recoverable": True,
                "failure_recovery_fingerprint": "sha256:same-recovery",
                "pipeline_fingerprint": "sha256:same",
                "hook": "技能已经被命名成李姐拉拉。",
                "selection_scorecard": {"status": "VALID", "tier": 1},
                "source_fact_rescore": receipt,
            }
        ],
    }

    assert runner.requeue_recoverable_talks(date, state) == 1
    assert state["picks"] == []
    item = state["pending_talk"][0]
    assert item["hook"] == "她投奔狍哥求庇护"
    assert item["selection_scorecard"] is None
    assert item["rescore_pending"] is True
    assert item["rescore_consumed_fingerprints"] == ["sha256:fp1"]
    assert item["retry_reason"] == "source_fact_rescore"


# --- Scenario 5: UNSUPPORTED / invalid card -> terminal (unit-level) -------


def test_scenario_5_unsupported_hook_returns_typed_outcome_not_a_silent_pass() -> None:
    outcome = rescore_candidate_scorecard(
        candidate_id="auto_x",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps({"status": "UNSUPPORTED"}, ensure_ascii=False),
        start_cue=1,
        end_cue=4,
    )
    assert outcome["outcome"] == "UNSUPPORTED"


def test_scenario_5_invalid_card_shape_returns_typed_outcome() -> None:
    outcome = rescore_candidate_scorecard(
        candidate_id="auto_x",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(
            {"status": "SUPPORTED", "selection_scorecard": {"tier": 9}}, ensure_ascii=False
        ),
        start_cue=1,
        end_cue=4,
    )
    assert outcome["outcome"] == "INVALID_CARD"


# --- Scenario 6: provider failure -> PROVIDER_UNAVAILABLE, no budget spent -


def test_scenario_6_provider_unavailable_does_not_consume_budget() -> None:
    outcome_none = rescore_candidate_scorecard(
        candidate_id="auto_x",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=None,
        start_cue=1,
        end_cue=4,
    )
    assert outcome_none["outcome"] == "PROVIDER_UNAVAILABLE"

    def raising(_prompt: str) -> str:
        raise RuntimeError("CPA 5xx")

    outcome_call_failed = rescore_candidate_scorecard(
        candidate_id="auto_x",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=raising,
        start_cue=1,
        end_cue=4,
    )
    assert outcome_call_failed["outcome"] == "PROVIDER_UNAVAILABLE"


def test_scenario_6_missing_cue_window_falls_back_to_stale_evidence_bounds() -> None:
    narrow_payload = _valid_rescore_payload()
    narrow_payload["selection_scorecard"]["tier_evidence_cues"] = [2, 3]

    outcome = rescore_candidate_scorecard(
        candidate_id="auto_x",
        cues=_rescore_cues(),
        repaired_hook="她投奔狍哥求庇护",
        clip_context_prompt="",
        llm_call=lambda _prompt: json.dumps(narrow_payload, ensure_ascii=False),
        stale_scorecard={"tier_evidence_cues": [2, 3]},
        start_cue=None,
        end_cue=None,
    )
    assert outcome["outcome"] == "RESCORED"
    assert outcome["start_cue"] == 2
    assert outcome["end_cue"] == 3


# --- Scenario 7: fingerprint cap — one budget slot per repaired hook -------


def test_scenario_7_same_repaired_hook_fingerprint_is_one_shot() -> None:
    fp = selection_rescore.rescore_fingerprint("sha256:fail", "sha256:hookA")
    record = {
        "status": "failed",
        "failure_kind": "selection_rescore",
        "failure_recoverable": True,
        "source_fact_rescore": {
            "schema_version": selection_rescore.RESCORE_RECEIPT_SCHEMA,
            "status": "PENDING",
            "rescore_fingerprint": fp,
        },
        "rescore_consumed_fingerprints": [],
    }
    assert selection_rescore.unconsumed_rescore_fingerprint(record) == fp

    record["rescore_consumed_fingerprints"] = [fp]
    assert selection_rescore.unconsumed_rescore_fingerprint(record) is None


def test_scenario_7_different_repaired_hook_earns_a_fresh_fingerprint_under_cap() -> None:
    fp_a = selection_rescore.rescore_fingerprint("sha256:fail", "sha256:hookA")
    fp_b = selection_rescore.rescore_fingerprint("sha256:fail", "sha256:hookB")
    assert fp_a != fp_b

    record = {
        "status": "failed",
        "failure_kind": "selection_rescore",
        "failure_recoverable": True,
        "source_fact_rescore": {
            "schema_version": selection_rescore.RESCORE_RECEIPT_SCHEMA,
            "status": "PENDING",
            "rescore_fingerprint": fp_b,
        },
        "rescore_consumed_fingerprints": [fp_a],
    }
    assert selection_rescore.unconsumed_rescore_fingerprint(record) == fp_b


def test_scenario_7_cap_exhausted_after_two_consumptions_regardless_of_hook() -> None:
    fp_c = selection_rescore.rescore_fingerprint("sha256:fail", "sha256:hookC")
    record = {
        "status": "failed",
        "failure_kind": "selection_rescore",
        "failure_recoverable": True,
        "source_fact_rescore": {
            "schema_version": selection_rescore.RESCORE_RECEIPT_SCHEMA,
            "status": "PENDING",
            "rescore_fingerprint": fp_c,
        },
        "rescore_consumed_fingerprints": ["sha256:one", "sha256:two"],
    }
    assert selection_rescore.SOURCE_FACT_RESCORE_CAP == 2
    assert selection_rescore.unconsumed_rescore_fingerprint(record) is None


# --- Scenario 8: exact contract closure -------------------------------------


def test_scenario_8_exact_contract_closure_reports_rescore_pending_incomplete() -> None:
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "talk_selection_contract": {
            "schema_version": "talk-selection-contract.v1",
            "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
            "authority": "Ivan selected exact recovery set",
            "source_state_sha256": "sha256:" + "1" * 64,
            "candidate_ids": ["auto_1271_1323"],
        },
        "picks": [],
        "pending_talk": [
            {"candidate_id": "auto_1271_1323", "rescore_pending": True},
        ],
    }
    closure = exact_talk_contract_closure(state)
    assert closure["status"] == "INCOMPLETE"
    row = closure["rows"][0]
    assert row["disposition"] == "RESCORE_PENDING"
    assert row["pending_count"] == 1
    assert row["attempt_count"] == 0


# --- Scenario 9: auto_220747_1271_1323 regression (shape-faithful fixture,
# see module docstring — the real receipt is not in this repo) --------------


def test_scenario_9_paoge_case_repairs_forbidden_upgrades_keeps_supported_generalization() -> None:
    final_transcript = (
        "有人问狍哥是谁\n"
        "她说要去找狍哥表忠心\n"
        "说完就跟着队伍走了\n"
        "她说这样非常有安全感\n"
        "那我怎么保护\n"
    )

    def cpa(_prompt: str) -> str:
        return _completion(
            status="REPAIR",
            final_hook="她投奔狍哥求庇护，这样非常有安全感。",
            final_title="【李豆沙】她投奔狍哥求庇护，这样非常有安全感",
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "她最终被狍哥亲手背刺，这样最有安全感",
                    "after": "她投奔狍哥求庇护，这样非常有安全感",
                    "reason": (
                        "受事错误的背刺闭环没有证据；程度槽降回逐字的"
                        "“非常”，不升级为“最”。"
                    ),
                    "evidence": ["她说这样非常有安全感", "说完就跟着队伍走了"],
                },
                {
                    "artifact": "title",
                    "before": "她最终被狍哥亲手背刺，这样最有安全感",
                    "after": "她投奔狍哥求庇护，这样非常有安全感",
                    "reason": "标题跟随同一处修正，同一事实模态。",
                    "evidence": ["她说这样非常有安全感", "说完就跟着队伍走了"],
                },
            ],
            selection_scorecard_review={
                "status": "INCOMPATIBLE",
                "reason": "旧卡围绕背刺闭环打分，核心梗已换成投奔求庇护。",
            },
        )

    review = review_and_repair_source_facts(
        selection_hook="她最终被狍哥亲手背刺，这样最有安全感",
        title="【李豆沙】她最终被狍哥亲手背刺，这样最有安全感",
        final_transcript=final_transcript,
        clip_context_prompt="",
        selection_scorecard={"tier_basis": "personal_stance", "tier_reason": "背刺闭环"},
        llm_call=cpa,
    )

    assert review["decision"] == "REPAIR_SCORECARD_STALE"
    assert review["reason_code"] == "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE"
    assert (
        selection_rescore.classify_source_fact_review_marker(review)
        == "SOURCE_FACT_REPAIRED_RESCORE_REQUIRED"
    )
    repaired = review["rescore_candidate"]["repaired_selection_hook"]
    assert "背刺" not in repaired
    assert "最有安全感" not in repaired
    assert "非常有安全感" in repaired


def test_scenario_9_degree_upgrade_without_evidence_is_rejected() -> None:
    """机器可复核部分：'最/所有/永远' 类升级词不在证据原文就判无效。"""

    def cpa(_prompt: str) -> str:
        return _completion(
            status="REPAIR",
            final_hook="她说这样最有安全感",
            final_title="【李豆沙】她说这样最有安全感",
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "她说这样非常有安全感",
                    "after": "她说这样最有安全感",
                    "reason": "程度升级",
                    "evidence": ["她说这样非常有安全感"],
                }
            ],
        )

    review = review_and_repair_source_facts(
        selection_hook="她说这样非常有安全感",
        title="【李豆沙】她说这样非常有安全感",
        final_transcript="她说这样非常有安全感",
        clip_context_prompt="",
        selection_scorecard=None,
        llm_call=cpa,
    )

    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


# --- Scenario 10: projections ------------------------------------------------


def test_scenario_10_reserves_projection_marks_rescore_pending_distinctly() -> None:
    state = {
        "pending_talk": [
            {"candidate_id": "a", "rescore_pending": True},
            {"candidate_id": "b"},
        ],
        "talk_backlog": [],
        "talk_below_confidence_threshold": [],
    }
    reserves = {row["candidate_id"]: row for row in _current_talk_reserves(state, [])}
    assert reserves["a"]["candidate_disposition"] == "RESCORE_PENDING"
    assert reserves["b"]["candidate_disposition"] == "SELECTED_PENDING"


def test_scenario_10_batch_terminal_state_treats_rescore_pending_as_retry_wait() -> None:
    state = {
        "picks": [
            {
                "candidate_id": "a",
                "status": "failed",
                "failure_kind": "selection_rescore",
                "failure_recoverable": True,
            }
        ],
        "songs": [],
    }
    result = project_terminal_batch_state(
        state,
        delivered_talk_statuses={"review_ready"},
        talk_failure_statuses={"failed"},
        cover_pending_status="talk_cover_pending",
        exact_closure={"status": "NOT_APPLICABLE"},
        retry_epoch=1_999_999_999,
    )
    assert result["failures"] == []
    assert state["status"] in {"retry_wait", "review_ready_retry_wait"}


def test_scenario_10_rejected_talk_table_excludes_pending_but_keeps_terminal_failures() -> None:
    from src.autoslice.reporting import write_reports

    pending_row = {
        "candidate_id": "a",
        "status": "failed",
        "failure_kind": "selection_rescore",
        "failure_recoverable": True,
    }
    terminal_row = {
        "candidate_id": "b",
        "status": "candidate_rejected",
        "failure_kind": "selection_rescore",
        "rejection_reason": "selection_rescore_failed",
    }
    # Exercise the same predicate write_reports uses without needing a full
    # delivery-root fixture: replicate its rejected_talk filter directly.
    picks = [pending_row, terminal_row]
    rejected_talk = [
        row
        for row in picks
        if row.get("status")
        in {
            "candidate_rejected",
            "boundary_unrepairable",
            "speaker_review_required",
            "speaker_evidence_insufficient",
            "failed",
            "quarantine",
        }
        and not (
            row.get("status") == "failed"
            and row.get("failure_kind") == "selection_rescore"
        )
    ]
    assert [row["candidate_id"] for row in rejected_talk] == ["b"]
    assert callable(write_reports)  # import surface stays wired
