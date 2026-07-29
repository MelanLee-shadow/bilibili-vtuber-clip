

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
