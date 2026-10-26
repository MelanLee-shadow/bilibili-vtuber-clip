import copy
import hashlib

from src.autoslice.final_review_meaning_policy import POLICY_VERSION, bind_current_risk


_EQUIVALENT_SRT = "1\n00:00:01,000 --> 00:00:03,000\n我先喝口水\n"
_EQUIVALENT_SRT_SHA256 = "sha256:" + hashlib.sha256(_EQUIVALENT_SRT.encode()).hexdigest()


def _with_current_equivalence(row):
    """Controlled drink-request fixture, never attached to historical raw rows."""
    row = copy.deepcopy(row)
    row.update(cue_index=1, base_text_sha256=hashlib.sha256("我先喝口水".encode()).hexdigest(),
               content_risk="noncritical_uncertainty",
               risk_basis="当前句与补全量词的说法都表示说话人先喝少量水，人物和动作不变。")
    row["current_content_risk"] = bind_current_risk(
        row, srt_text=_EQUIVALENT_SRT, cue_count=1, prompt=_EQUIVALENT_SRT,
        meaning_equivalence={
            "status": "EQUIVALENT", "current_meaning": "说话人先喝少量水。",
            "alternative_meaning": "我先喝一口水也表示说话人先喝少量水。",
            "shared_meaning": "说话人先进行喝少量水的动作。", "uncertain_detail": "量词前是否有一字。",
            "context_basis": "本例完整字幕仅为该离席说明；两种说法的人物、动作及先后相同。",
            "evidence_cue_ids": [1],
        },
    )
    return row


def test_noncritical_no_new_dispatch_plan_gate_is_disclosed_without_clearing_history():
    import copy
    from src.autoslice.final_review_contract import is_noncritical_disclosed
    finding = {'content_risk': 'noncritical_uncertainty', 'risk_basis': '普通未知，没有具体严重错误依据',
               'exact_release_adjudication': {'status': 'UNCERTAIN', 'repaired': False,
                    'reason_code': 'RetryNeedsPlan', 'semantic_retry_progress': {
                        'schema_version': 'semantic-retry-progress.v1', 'mutation_authorized': False,
                        'status': 'CONTINUATION_VALUE_PLAN_REQUIRED', 'events': [{
                            'status': 'CONTINUATION_VALUE_PLAN_REQUIRED', 'provider_calls': 0,
                            'prior_unresolved_answers': 3}]}}}
    assert not is_noncritical_disclosed(finding)
    finding = _with_current_equivalence(finding)
    before = copy.deepcopy(finding)
    assert is_noncritical_disclosed(finding)
    assert finding == before
    for risk in ('meaning_reversal', 'factual_error', 'potential_harm'):
        assert not is_noncritical_disclosed({**finding, 'content_risk': risk})
    for status, calls in [('CONTINUATION_VALUE_PLAN_REQUIRED', 1), ('OUTCOME_UNKNOWN', 0),
                          ('STARTED', 0), ('CONTINUATION_VALUE_PLAN_REQUIRED', False)]:
        corrupt = copy.deepcopy(finding)
        corrupt['exact_release_adjudication']['semantic_retry_progress']['events'][0].update(
            status=status, provider_calls=calls)
        assert not is_noncritical_disclosed(corrupt)
    for state in ('OUTCOME_UNKNOWN', 'ERROR', 'STARTED'):
        corrupt = copy.deepcopy(finding)
        corrupt['exact_release_adjudication']['status'] = state
        assert not is_noncritical_disclosed(corrupt)


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
        return _with_current_equivalence({"exact_release_adjudication": adjudication})

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


def test_exact_keep_current_contract_requires_complete_support_and_bound_receipts():
    """Exact-final disclosure rejects support, request, and witness drift."""

    import copy

    from src.autoslice.acoustic_witness_adjudication import (
        REQUIRE_COMPLETE_UTTERANCE_SUPPORT,
        build_witness_request,
    )
    from src.autoslice.final_review_auditor import (
        build_context_adjudication_request,
    )
    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    source = _EQUIVALENT_SRT
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 1,
            "suspect": "口",
            "suggestion": "一口",
            "proposed_full_cue": "我先喝一口水",
            "repair_class": "phonetic",
        },
        require_complete_utterance_support=True,
    )
    witness_request = build_witness_request(request)
    judge = {
        "status": "JUDGED",
        "choice": "CURRENT",
        "check_request_sha256": request["request_sha256"],
        "current_utterance_supported": True,
        "current_utterance_support_reason": "The complete current utterance is supported.",
    }
    adjudication = {
        "status": "OBSERVED",
        "policy_branch": "JUDGE_KEEPS_CURRENT",
        "repaired": False,
        "timing_immutable": True,
        "request": request,
        "witness_judge": {
            "witness_request_sha256": witness_request["request_sha256"],
            "judge": judge,
        },
        "mutation_authority": {"status": "NOT_APPLIED"},
    }
    row = _with_current_equivalence({"exact_release_adjudication": adjudication})

    assert _is_keep_current_disclosed(row) is True

    def mutate_support_false(candidate):
        candidate["exact_release_adjudication"]["witness_judge"]["judge"][
            "current_utterance_supported"
        ] = False

    def mutate_support_missing(candidate):
        candidate["exact_release_adjudication"]["witness_judge"]["judge"].pop(
            "current_utterance_supported"
        )
        candidate["exact_release_adjudication"]["witness_judge"]["judge"].pop(
            "current_utterance_support_reason"
        )

    def mutate_choice(candidate):
        candidate["exact_release_adjudication"]["witness_judge"]["judge"][
            "choice"
        ] = "PROPOSED"

    def mutate_current(candidate):
        candidate["exact_release_adjudication"]["request"]["current_cue"] = (
            "漂移后的整句"
        )

    def mutate_window(candidate):
        candidate["exact_release_adjudication"]["request"]["matched_start_ms"] += 1

    def mutate_request_sha(candidate):
        candidate["exact_release_adjudication"]["request"]["request_sha256"] = (
            "0" * 64
        )

    def mutate_judge_sha(candidate):
        candidate["exact_release_adjudication"]["witness_judge"]["judge"][
            "check_request_sha256"
        ] = "1" * 64

    def mutate_witness_sha(candidate):
        candidate["exact_release_adjudication"]["witness_judge"][
            "witness_request_sha256"
        ] = "2" * 64

    for mutate in (
        mutate_support_false,
        mutate_support_missing,
        mutate_choice,
        mutate_current,
        mutate_window,
        mutate_request_sha,
        mutate_judge_sha,
        mutate_witness_sha,
    ):
        candidate = copy.deepcopy(row)
        mutate(candidate)
        assert _is_keep_current_disclosed(candidate) is False

    # Ordinary typed decisions retain their branch contract only alongside
    # the independent current meaning-equivalence receipt.
    legacy = copy.deepcopy(row)
    legacy["exact_release_adjudication"]["request"].pop(
        REQUIRE_COMPLETE_UTTERANCE_SUPPORT
    )
    assert _is_keep_current_disclosed(legacy) is True


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
        return _with_current_equivalence({key: adjudication})

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


def test_historical_witness_conflict_is_decided_but_requires_current_meaning_review():
    """The historical typed decision survives, but cannot disclose damaged text.

    A representative historical sample retains its decision and
    must not acquire a fabricated current equivalence assessment.
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

    from src.autoslice.final_review_contract import decided_keep_current_adjudication
    assert decided_keep_current_adjudication(
        finding()["exact_release_adjudication"], timing_immutable=True,
    )
    assert _is_keep_current_disclosed(finding()) is False

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
    disclosed_row = _with_current_equivalence(disclosed_row)
    grid = "sha256:" + "d" * 64
    audit = {
        "schema_version": "final-review-audit.v2",
        "meaning_policy": POLICY_VERSION,
        "reviewed_srt_sha256": _EQUIVALENT_SRT_SHA256,
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
        expected_srt_sha256=_EQUIVALENT_SRT_SHA256,
    )["release_gate"] == "PASS"

    # 同一分支名但判官授权已行使（改过字节）——不得借披露出口溜走。
    audit["unresolved_findings_disclosed"] = [
        {
            **disclosed_row,
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
            expected_srt_sha256=_EQUIVALENT_SRT_SHA256,
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
        "meaning_policy": POLICY_VERSION,
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
    # An old closed-set choice still lacks the current meaning review.
    assert unconsumed_correction_carryover_count(audit) == 1

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


def test_historical_convergence_downgrade_needs_observation_and_current_meaning_review():
    """Historical observed audio keeps its typed decision, not current release authority."""

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
    from src.autoslice.final_review_contract import decided_history_convergence_disclosure
    assert decided_history_convergence_disclosure(adjudication, timing_immutable=True)
    assert _is_keep_current_disclosed(observed) is False

    # Keep the original typed observation checks meaningful even though all
    # these historical rows independently lack a current meaning judgment.
    for verdict in (
        _UNCERTAIN_BLIND_WITNESS,
        {},
        {**_OBSERVED_BLIND_WITNESS, "status": "UNAVAILABLE"},
        {**_OBSERVED_BLIND_WITNESS, "witness_protocol": "legacy_sighted"},
    ):
        row = _downgraded_row(verdict)
        assert not decided_history_convergence_disclosure(
            row["exact_release_adjudication"], timing_immutable=True,
        )
        assert not _is_keep_current_disclosed(row)


def test_history_convergence_disclosure_rejects_forged_downgrade_shells():
    """只贴分支名不算降级——门/授权/证词形态逐项对不上一律拦。"""

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    from src.autoslice.final_review_contract import decided_history_convergence_disclosure
    assert decided_history_convergence_disclosure(
        _downgraded_row(_OBSERVED_BLIND_WITNESS)["exact_release_adjudication"],
        timing_immutable=True,
    )
    assert not _is_keep_current_disclosed(_downgraded_row(_OBSERVED_BLIND_WITNESS))

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
        assert not decided_history_convergence_disclosure(
            row["exact_release_adjudication"],
            timing_immutable=row["exact_release_adjudication"].get("timing_immutable") is True,
        ), forgery
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
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "NOT_APPLIED",
            },
        }
        adjudication.update(overrides)
        return _with_current_equivalence({"exact_release_adjudication": adjudication})

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


def test_exact_final_cycle_return_memo_closes_pending_then_pass_chain():
    """A new CPA memo may close A->B->A; an ordinary cycle remains blocked."""

    import copy
    import hashlib
    import json
    import pytest

    from src.autoslice.exact_final_convergence import (
        collect_exact_final_convergence_memos,
        converge_reconsidered_exact_final_findings,
    )
    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        _validate_exact_final_cpa_self_heal,
    )
    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs,
    )
    from tests.test_exact_final_convergence import _finding

    def srt(text: str) -> str:
        return f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n"

    def digest(text: str) -> str:
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    first_text = "版本0"
    second_text = "版本1"
    first_finding = _finding(
        cue_index=1,
        start_ms=0,
        end_ms=1_000,
        current=first_text,
        proposed=second_text,
        salt="contract-first",
    )
    _first_srt, first_repairs = _apply_exact_final_cpa_repairs(
        srt(first_text),
        {"findings": [first_finding]},
    )
    assert len(first_repairs) == 1
    first_pass = {
        "schema_version": "exact-final-cpa-self-heal-pass.v1",
        "pass_index": 1,
        "input_srt_sha256": digest(srt(first_text)),
        "output_srt_sha256": digest(srt(second_text)),
        "repairs": first_repairs,
    }
    second_finding = _finding(
        cue_index=1,
        start_ms=0,
        end_ms=1_000,
        current=second_text,
        proposed=first_text,
        salt="contract-cycle",
    )
    authority = {
        "exact_final_cpa_self_heal": {
            "schema_version": "exact-final-cpa-self-heal-audit.v1",
            "status": "REVIEW_PENDING",
            "passes": [first_pass],
        }
    }
    unresolved, resolved = converge_reconsidered_exact_final_findings(
        srt(second_text),
        [second_finding],
        authority_audit=authority,
        judge_llm_call=lambda _prompt: json.dumps(
            {"choice": "PROPOSED", "reason": "新的完整 CPA 证据闭合返回"},
            ensure_ascii=False,
        ),
    )
    assert resolved == []
    assert len(unresolved) == 1
    _second_srt, second_repairs = _apply_exact_final_cpa_repairs(
        srt(second_text),
        {"findings": unresolved},
    )
    assert len(second_repairs) == 1
    second_repairs[0]["exact_final_cpa_cycle_memo"] = (
        collect_exact_final_convergence_memos({"findings": unresolved})[0]
    )
    pending = {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "REVIEW_PENDING",
        "passes": [
            first_pass,
            {
                "schema_version": "exact-final-cpa-self-heal-pass.v1",
                "pass_index": 2,
                "input_srt_sha256": digest(srt(second_text)),
                "output_srt_sha256": digest(srt(first_text)),
                "repairs": second_repairs,
            },
        ],
    }
    pending_audit = {"exact_final_cpa_self_heal": pending}
    _validate_exact_final_cpa_self_heal(
        pending_audit,
        expected_srt_sha256=digest(srt(first_text)),
        allow_pending=True,
    )
    complete = copy.deepcopy(pending_audit)
    complete["exact_final_cpa_self_heal"].update(
        status="PASS",
        final_srt_sha256=digest(srt(first_text)),
    )
    _validate_exact_final_cpa_self_heal(
        complete,
        expected_srt_sha256=digest(srt(first_text)),
    )

    ordinary_cycle = copy.deepcopy(pending_audit)
    ordinary_cycle["exact_final_cpa_self_heal"]["passes"][1]["repairs"][0].pop(
        "exact_final_cpa_cycle_memo"
    )
    with pytest.raises(
        FinalReviewContractError,
        match="CYCLE_RETURN_UNAUTHORIZED",
    ):
        _validate_exact_final_cpa_self_heal(
            ordinary_cycle,
            expected_srt_sha256=digest(srt(first_text)),
            allow_pending=True,
        )

    tampered = copy.deepcopy(pending_audit)
    tampered["exact_final_cpa_self_heal"]["passes"][1]["repairs"][0][
        "exact_final_cpa_cycle_memo"
    ]["final_text"] = "篡改"
    with pytest.raises(FinalReviewContractError, match="CYCLE_RETURN_BINDING_INVALID"):
        _validate_exact_final_cpa_self_heal(
            tampered,
            expected_srt_sha256=digest(srt(first_text)),
            allow_pending=True,
        )

    replayed = copy.deepcopy(pending_audit)
    replayed["exact_final_cpa_self_heal"]["passes"][1]["repairs"].append(
        copy.deepcopy(
            replayed["exact_final_cpa_self_heal"]["passes"][1]["repairs"][0]
        )
    )
    with pytest.raises(FinalReviewContractError, match="CYCLE_RETURN_MEMO_REPLAY"):
        _validate_exact_final_cpa_self_heal(
            replayed,
            expected_srt_sha256=digest(srt(first_text)),
            allow_pending=True,
        )

    unrelated = copy.deepcopy(pending_audit)
    unrelated_memo = unrelated["exact_final_cpa_self_heal"]["passes"][1][
        "repairs"
    ][0]["exact_final_cpa_cycle_memo"]
    unrelated_memo["matched_start_ms"] = 10
    unrelated_memo["matched_end_ms"] = 20
    with pytest.raises(
        FinalReviewContractError,
        match="CYCLE_RETURN_BINDING_INVALID",
    ):
        _validate_exact_final_cpa_self_heal(
            unrelated,
            expected_srt_sha256=digest(srt(first_text)),
            allow_pending=True,
        )


def test_release_rejects_historical_convergence_without_current_meaning_review():
    """Neither observed historical audio nor a new policy label certifies current meaning."""

    import pytest

    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        validate_final_review_release,
    )

    grid = "sha256:" + "d" * 64
    audit = {
        "schema_version": "final-review-audit.v2",
        "meaning_policy": POLICY_VERSION,
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

    with pytest.raises(FinalReviewContractError, match="FINAL_REVIEW_FINDINGS_CONTRACT_INVALID"):
        validate_final_review_release(audit, expected_srt_sha256="sha256:" + "b" * 64)

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
