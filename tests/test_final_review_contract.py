

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

    A typed keep-current branch must stay registered in the public
    disclosure allowlist so an unchanged decided finding can resolve.
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


# Representative fixtures cover observed and timing-inconsistent witnesses.
_OBSERVED_BLIND_WITNESS = {
    "schema_version": "subtitle-span-acoustic-witness.v1",
    "witness_protocol": "blind_pinyin",
    "status": "OBSERVED",
    "target_audible": True,
    "heard_pinyin": "ta suan qi fu wo",
    "uncertain_positions": [],
    "syllable_count": 5,
    "self_count_mismatch": False,
    "confidence": 0.95,
    "provider": "agy",
    "model": "Gemini 3.6 Flash (High)",
    "reason": "five clear syllables heard in target window",
    "request_sha256": "c" * 64,
    "response_sha256": "f" * 64,
    "prompt_sha256": "a" * 64,
    "audio_clip_sha256": "1" * 64,
    "source_media_sha256": "e" * 64,
    "audio_start_ms": 118400,
    "audio_end_ms": 120600,
}
_UNCERTAIN_BLIND_WITNESS = {
    "schema_version": "subtitle-span-acoustic-witness.v1",
    "witness_protocol": "blind_pinyin",
    "status": "UNCERTAIN",
    "reason_code": "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE",
    "detail": "11 syllables over 0.92s target",
    "request_sha256": "3" * 64,
}


def _downgraded_row(verdict, **adjudication_overrides):
    """用引擎自己的产出函数造样本，别手抄 typed 形状。

    分支名/字段名再改，这里 import 就断或形态自动跟着变，不会和引擎静默脱节。
    """

    from src.autoslice.exact_final_witness_authority import (
        convergence_witness_gate,
        downgrade_convergence_finding,
    )

    window = (109060, 110390)
    gate = convergence_witness_gate(
        proposed="他算欺负我",
        window=window,
        findings=[],
        history=[],
    )
    # 前提自检：没有任何合格盲见证时这道门必须 BLOCK，降级才会发生。
    assert gate["status"] == "BLOCK"
    adjudication = {
        "schema_version": "subtitle-span-adjudication.v1",
        "timing_immutable": True,
        "verdict": dict(verdict),
    }
    row = downgrade_convergence_finding(
        {
            "cue_index": 39,
            "repair_class": "phonetic",
            "suspect": "她自己打了70",
            "proposed_full_cue": "他算欺负我",
            "exact_release_adjudication": adjudication,
        },
        gate=gate,
    )
    row["exact_release_adjudication"].update(adjudication_overrides)
    return row


def test_history_convergence_downgrade_discloses_only_with_an_observed_ear():
    """审片员说「别改、只披露」，出口就不能反过来说「不许披露」。

    ``HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY`` 把 repair_class 改成
    disclosure_only、mutation 置 NOT_APPLIED、一个字节都没动，却因为
    adjudication.status 被写成 UNCERTAIN 而进不了披露出口 —— 既不能改也不能
    披露，整条候选会永久悬停。

    放行判据只认一件事：声学机器是否真的跑完并交出观测。耳朵给了 OBSERVED
    才算「证据在手做出的保留原文」；耳朵自己 UNCERTAIN 就是机器没能决定，
    继续拦死。
    """

    from src.autoslice.exact_final_witness_authority import DOWNGRADE_BRANCH
    from src.autoslice.final_review_contract import (
        _DECIDED_KEEP_CURRENT_BRANCHES,
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    # 整支塞进 decided-keep 白名单会连「没听清」一起放走——永远不许这么修。
    assert DOWNGRADE_BRANCH not in _DECIDED_KEEP_CURRENT_BRANCHES

    observed = _downgraded_row(_OBSERVED_BLIND_WITNESS)
    adjudication = observed["exact_release_adjudication"]
    assert observed["repair_class"] == "disclosure_only"
    assert adjudication["policy_branch"] == DOWNGRADE_BRANCH
    assert adjudication["status"] == "UNCERTAIN"
    assert adjudication["repaired"] is False
    assert adjudication["mutation_authority"]["status"] == "NOT_APPLIED"
    assert _is_keep_current_disclosed(observed) is True

    # 耳朵没能给出观测 = 机器没能决定，fail-closed 一格不让。
    assert _is_keep_current_disclosed(
        _downgraded_row(_UNCERTAIN_BLIND_WITNESS)
    ) is False
    assert _is_keep_current_disclosed(_downgraded_row({})) is False
    assert _is_keep_current_disclosed(
        _downgraded_row(
            {**_OBSERVED_BLIND_WITNESS, "status": "UNAVAILABLE"}
        )
    ) is False
    # 睁眼证人（legacy_sighted）不是这条出口认的证据。
    assert _is_keep_current_disclosed(
        _downgraded_row(
            {**_OBSERVED_BLIND_WITNESS, "witness_protocol": "legacy_sighted"}
        )
    ) is False


def test_history_convergence_disclosure_rejects_forged_downgrade_shells():
    """只贴分支名不算降级——门/授权/证词形态逐项对不上一律拦。"""

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    assert _is_keep_current_disclosed(
        _downgraded_row(_OBSERVED_BLIND_WITNESS)
    ) is True

    for forgery in (
        # 见证门被改成 PASS：真 PASS 时引擎根本不会走降级，这是伪造壳子。
        {"history_convergence_acoustic_witness": {"status": "PASS"}},
        {"history_convergence_acoustic_witness": None},
        # 变更授权被行使过 —— 已经改字节的东西不许当 keep-current 披露。
        {
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "CPA_HISTORY_CONVERGENCE_APPLY_PROPOSED",
            }
        },
        {"repaired": True},
        # 时间轴动过。
        {"timing_immutable": False},
        # 决定权/证人权威被改写成别的车道。
        {"decision_authority": "CPA_JUDGE"},
        {"witness_authority": "EVIDENCE_ONLY"},
        {"reason_code": "SOMETHING_ELSE"},
        # 借降级壳子夹带一个真正未决的分支名。
        {"policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT"},
        {"policy_branch": "GLOSSARY_CANDIDATE_WITNESS_CONFLICT_ORTHOGRAPHY_NOT_DECIDABLE"},
    ):
        row = _downgraded_row(_OBSERVED_BLIND_WITNESS, **forgery)
        assert _is_keep_current_disclosed(row) is False, forgery


def test_infra_incomplete_branches_stay_blocked_even_dressed_as_keep_current():
    """护栏：给「机器没能决定」套上完整的已决 keep-current 外壳，仍必须拦死。

    这条测试存在的唯一目的是防止后来的人为了多解锁几条候选把门修松。
    """

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    def dressed(**overrides):
        adjudication = {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED",
            "policy_branch": "JUDGE_KEEPS_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "decision_authority": "CPA_JUDGE",
            "verdict": dict(_OBSERVED_BLIND_WITNESS),
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "NOT_APPLIED",
            },
        }
        adjudication.update(overrides)
        return {"cue_index": 1, "exact_release_adjudication": adjudication}

    # 基线：这套外壳本身是能放行的，所以下面每一条都只差 policy_branch/status。
    assert _is_keep_current_disclosed(dressed()) is True

    for infra_branch in (
        # 预算跳过 / 后端不可用 / stale base / 响应非法：基础设施没走完。
        "INVALID_OR_UNCERTAIN_KEEP_CURRENT",
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "JUDGE_UNAVAILABLE_KEEP_CURRENT",
        "PINYIN_BACKEND_UNAVAILABLE_KEEP_CURRENT",
        "STALE_BASE_KEEP_CURRENT",
        "SKIPPED_BUDGET",
        # judge 自己没把握，不是「决定保留」。
        "JUDGE_UNCERTAIN_KEEP_CURRENT",
        # 判官说两个都不对。
        "JUDGE_REJECTS_CLOSED_SET",
        # 正字法本身无法从音频判定：名字里就写着没能决定。
        "GLOSSARY_CANDIDATE_WITNESS_CONFLICT_ORTHOGRAPHY_NOT_DECIDABLE",
        "ORTHOGRAPHY_NOT_DECIDABLE_FROM_AUDIO",
        "INAUDIBLE_DROP_SELECTION_AUTHORITY_INVALID",
        "INAUDIBLE_DROP_REQUEST_INVALID",
        "INAUDIBLE_DROP_CHANGED_WITNESS_WINDOW",
    ):
        assert _is_keep_current_disclosed(
            dressed(policy_branch=infra_branch)
        ) is False, infra_branch

    # 没有 policy_branch / 整条 adjudication 缺席同样拦死。
    assert _is_keep_current_disclosed(dressed(policy_branch=None)) is False
    assert _is_keep_current_disclosed(
        {"cue_index": 1, "exact_release_adjudication": {"status": "SKIPPED_BUDGET"}}
    ) is False
    # 白名单里的名字配上非 OBSERVED 状态也不行（史收敛降级是唯一例外）。
    assert _is_keep_current_disclosed(dressed(status="UNCERTAIN")) is False
    assert _is_keep_current_disclosed(dressed(status="SKIPPED_BUDGET")) is False


def test_release_accepts_disclosed_history_convergence_downgrade():
    """合同层放行：耳朵听见的降级条目随包披露；耳朵没听清的仍是合同违规。

    只测谓词不够——真正卡住交付的是 ``validate_final_review_release`` 里
    ``unresolved_findings_disclosed`` 那一圈校验；这条测的是整份回执能不能
    带着降级条目通过发布门。
    """

    import pytest

    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        validate_final_review_release,
    )

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
        "unresolved_findings_disclosed": [
            _downgraded_row(_OBSERVED_BLIND_WITNESS)
        ],
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
                "source_final_start_ms": 388000,
                "source_final_end_ms": 526000,
                "reason_codes": [],
            },
        },
    }

    assert validate_final_review_release(
        audit,
        expected_srt_sha256="sha256:" + "b" * 64,
    )["release_gate"] == "PASS"

    # 耳朵没能给出观测的同名降级条目混进披露 —— 仍是合同违规,拦死。
    audit["unresolved_findings_disclosed"] = [
        _downgraded_row(_UNCERTAIN_BLIND_WITNESS)
    ]
    with pytest.raises(
        FinalReviewContractError,
        match="FINAL_REVIEW_FINDINGS_CONTRACT_INVALID",
    ):
        validate_final_review_release(
            audit,
            expected_srt_sha256="sha256:" + "b" * 64,
        )
