

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
        "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT",
        "TARGET_INAUDIBLE_KEEP_CURRENT",
    ):
        assert _is_keep_current_disclosed(row(policy_branch=branch)) is True
        assert _is_keep_current_disclosed(
            row("context_audio_adjudication", policy_branch=branch)
        ) is True

    for infra_branch in (
        "JUDGE_UNCERTAIN_KEEP_CURRENT",
        "ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED_KEEP_CURRENT",
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "JUDGE_UNAVAILABLE_KEEP_CURRENT",
        "PINYIN_BACKEND_UNAVAILABLE_KEEP_CURRENT",
        "STALE_BASE_KEEP_CURRENT",
        "INVALID_OR_UNCERTAIN_KEEP_CURRENT",
    ):
        assert _is_keep_current_disclosed(
            row(policy_branch=infra_branch)
        ) is False


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
