

def test_keep_current_disclosed_classifier():
    """只有 CPA 明确选 CURRENT 才是可披露的已完成裁定。

    UNCERTAIN 不是决定，不能再由旧 fallback 偷换成 KEEP_CURRENT。
    """

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    def row(**overrides):
        adjudication = {
            "status": "OBSERVED",
            "policy_branch": "JUDGE_KEEPS_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        }
        adjudication.update(overrides)
        return {"cue_index": 1, "exact_release_adjudication": adjudication}

    assert _is_keep_current_disclosed(row()) is True
    assert _is_keep_current_disclosed(
        row(policy_branch="JUDGE_UNCERTAIN_KEEP_CURRENT")
    ) is False
    assert _is_keep_current_disclosed(row(repaired=True)) is False
    assert _is_keep_current_disclosed(row(status="UNAVAILABLE")) is False
    assert _is_keep_current_disclosed(
        row(policy_branch="JUDGE_CHOSE_PROPOSED")
    ) is False
    assert _is_keep_current_disclosed(
        row(mutation_authority={"status": "APPLIED"})
    ) is False
    assert _is_keep_current_disclosed({"cue_index": 1}) is False
    real_auditor_shape = row()
    real_auditor_shape["timing_immutable"] = True
    real_auditor_shape["exact_release_adjudication"].pop(
        "timing_immutable"
    )
    assert _is_keep_current_disclosed(real_auditor_shape) is True


def test_decided_keep_current_branches_disclose_and_infra_branches_block():
    """CPA 明确保留、拼音门否决和不可闻证据门可披露。

    缺 CPA 裁定、基础设施未走完或旧文字权威旁路仍是 blocker。
    """

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    def row(key="exact_release_adjudication", **overrides):
        adjudication = {
            "status": "OBSERVED",
            "policy_branch": "JUDGE_KEEPS_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        }
        adjudication.update(overrides)
        return {"cue_index": 1, key: adjudication}

    for branch in (
        "JUDGE_KEEPS_CURRENT",
        "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT",
        "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT",
        "TARGET_INAUDIBLE_KEEP_CURRENT",
    ):
        assert _is_keep_current_disclosed(row(policy_branch=branch)) is True
        assert _is_keep_current_disclosed(
            row("context_audio_adjudication", policy_branch=branch)
        ) is True

    # 全部取自 src 真实吐出的分支名。此前这张表里的
    # ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED_KEEP_CURRENT 与
    # PINYIN_BACKEND_UNAVAILABLE_KEEP_CURRENT 在 src/ 中没有任何产出点，
    # 断言的是不存在的形态（白名单与引擎脱节的同源症状）。
    for infra_branch in (
        "JUDGE_UNCERTAIN_KEEP_CURRENT",
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "JUDGE_UNAVAILABLE_KEEP_CURRENT",
        "STALE_BASE_KEEP_CURRENT",
        "INVALID_OR_UNCERTAIN_KEEP_CURRENT",
        # judge 说 CURRENT 和 PROPOSED 都不对——这不是「保留原文」的裁定。
        "JUDGE_REJECTS_CLOSED_SET",
    ):
        assert _is_keep_current_disclosed(
            row(policy_branch=infra_branch)
        ) is False


def test_witness_conflict_keep_current_is_a_decided_keep():
    """8/8 F3 证据门否决 CPA PROPOSED = 已完成的机器决定，可披露交付。

    2026-08-09 谈话切 4/4 全灭的真判据：`5a43ea3`(8/8) 新开的 typed 分支
    WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT 没登记进白名单，
    于是一条一个字节都没改的已裁 finding 结构上永远回不到 resolved。
    形态取自 free 真实回执 out/2026-08-09/auto_190617_473_766。
    """

    from src.autoslice.acoustic_witness_adjudication import (
        WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
    )
    from src.autoslice.final_review_contract import (
        _DECIDED_KEEP_CURRENT_BRANCHES,
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    # 白名单直接绑引擎常量：引擎再改名时 import 就断，不会静默失配。
    assert (
        WITNESS_CONFLICT_UNSUPPORTED_PROPOSED
        in _DECIDED_KEEP_CURRENT_BRANCHES
    )

    def finding(**overrides):
        adjudication = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "policy_branch": WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
            "repaired": False,
            "timing_immutable": True,
            "decision_authority": "CPA_JUDGE",
            "witness_authority": "EVIDENCE_ONLY",
            "orthography_ambiguous": False,
            # 真实回执里 orthography_authority 既可能 PASS 也可能 BLOCK：
            # 它不是判据，不许参与 decided-keep 判定。
            "orthography_authority": {
                "schema_version": "subtitle-orthography-authority.v1",
                "status": "BLOCK",
                "reason_code": "ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED",
                "provenance_kind": None,
            },
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "NOT_APPLIED",
                "basis": None,
            },
            "request": {
                "current_cue": "想要额田文雄有什么想要看小李",
                "proposed_cue": "想要额，大家有什么想要看小李",
            },
        }
        adjudication.update(overrides)
        return {
            "cue_index": 2,
            "suspect": "田文雄",
            "suggestion": "，大家",
            "exact_release_adjudication": adjudication,
        }

    assert _is_keep_current_disclosed(finding()) is True

    # 反向门：判官没授权 / 改过字节 / 时间轴动过 / 机器没能决定 —— 仍必须拦住。
    assert _is_keep_current_disclosed(
        finding(mutation_authority=None)
    ) is False
    assert _is_keep_current_disclosed(
        finding(
            mutation_authority={
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "BLOCK",
            }
        )
    ) is False
    assert _is_keep_current_disclosed(
        finding(
            mutation_authority={
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "PASS",
                "basis": "SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK",
            }
        )
    ) is False
    assert _is_keep_current_disclosed(finding(repaired=True)) is False
    assert _is_keep_current_disclosed(finding(timing_immutable=False)) is False
    assert _is_keep_current_disclosed(finding(status="UNCERTAIN")) is False
    assert _is_keep_current_disclosed(finding(status="UNAVAILABLE")) is False


def test_release_accepts_disclosed_witness_conflict_and_still_blocks_unauthorized():
    """已裁 keep-current 随包披露放行；未授权形态混进披露仍是合同违规。"""

    import pytest

    from src.autoslice.acoustic_witness_adjudication import (
        WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
    )
    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        validate_final_review_release,
    )

    disclosed_row = {
        "cue_index": 2,
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "policy_branch": WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {
                "schema_version": (
                    "subtitle-correction-mutation-authority.v1"
                ),
                "status": "NOT_APPLIED",
            },
        },
    }
    grid = "sha256:" + "d" * 64
    audit = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": "sha256:" + "b" * 64,
        "status": "CLEAN",
        "release_gate": "PASS",
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "findings": [],
        "validated_finding_count": 0,
        "unresolved_findings_disclosed": [disclosed_row],
        "boundary_semantic_review": {
            "status": "PASS",
            "review_scope": "final_delivery",
            "request_sha256": "sha256:" + "e" * 64,
            "cue_grid_sha256": grid,
            "final_endpoint_binding": {
                "schema_version": (
                    "talk-boundary-final-endpoint-binding.v1"
                ),
                "status": "PASS",
                "recommended_end_cue_index": 7,
                "recommended_end_ms": 81000,
                "final_closure_cue_index": 7,
                "final_snapped_end_ms": 81000,
                "reason_codes": [],
                "semantic_cue_grid_sha256": grid,
                "final_cue_grid_sha256": grid,
            },
            "source_separation_witness": {
                "schema_version": (
                    "talk-boundary-source-separation-witness.v1"
                ),
                "status": "PASS",
                "source_review_sha256": "sha256:" + "f" * 64,
                "source_request_sha256": "sha256:" + "0" * 64,
                "source_cue_grid_sha256": "sha256:" + "1" * 64,
                "source_final_start_ms": 473000,
                "source_final_end_ms": 766000,
                "reason_codes": [],
            },
        },
    }

    assert validate_final_review_release(
        audit,
        expected_srt_sha256="sha256:" + "b" * 64,
    )["release_gate"] == "PASS"

    # 同一分支名但判官授权已行使（改过字节）——不得借披露出口溜走。
    audit["unresolved_findings_disclosed"] = [
        {
            "cue_index": 2,
            "exact_release_adjudication": {
                **disclosed_row["exact_release_adjudication"],
                "repaired": True,
                "mutation_authority": {
                    "schema_version": (
                        "subtitle-correction-mutation-authority.v1"
                    ),
                    "status": "PASS",
                },
            },
        }
    ]
    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_FINDINGS_CONTRACT_INVALID",
    ):
        validate_final_review_release(
            audit,
            expected_srt_sha256="sha256:" + "b" * 64,
        )


def test_release_rejects_unconsumed_remapped_carryover():
    import pytest

    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        unconsumed_correction_carryover_count,
        validate_final_review_release,
    )

    replay = {
        "cue_index": 15,
        "base_text_sha256": "a" * 64,
        "suspect": "面部的时候没有什么",
        "proposed_full_cue": "就是制作这个机体",
        "carryover_replay_remap": {
            "schema_version": "final-review-carryover-remap.v1",
            "status": "PASS",
            "basis": "base_text_sha256",
        },
        "routed": "disclosure",
    }
    audit = {
        "schema_version": "final-review-audit.v2",
        "reviewed_srt_sha256": "sha256:" + "b" * 64,
        "status": "CLEAN",
        "release_gate": "PASS",
        "discovery": {"status": "COMPLETE"},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "correction_pass": {"findings": [replay]},
        "findings": [],
        "validated_finding_count": 0,
    }

    assert unconsumed_correction_carryover_count(audit) == 1
    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_CARRYOVER_UNCONSUMED",
    ):
        validate_final_review_release(
            audit,
            expected_srt_sha256="sha256:" + "b" * 64,
        )

    replay["context_audio_adjudication"] = {
        "status": "OBSERVED",
        "policy_branch": "JUDGE_KEEPS_CURRENT",
        "repaired": False,
        "timing_immutable": True,
        "mutation_authority": {"status": "NOT_APPLIED"},
    }
    assert unconsumed_correction_carryover_count(audit) == 0

    replay.pop("context_audio_adjudication")
    replay["carryover_consumption"] = {
        "schema_version": "exact-final-carryover-consumption.v1",
        "status": "CONSUMED_BY_EXACT_FINAL_CPA",
        "before_sha256": "sha256:" + "a" * 64,
        "after_sha256": "sha256:" + "c" * 64,
    }
    assert unconsumed_correction_carryover_count(audit) == 0
