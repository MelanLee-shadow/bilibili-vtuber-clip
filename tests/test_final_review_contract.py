

def test_keep_current_disclosed_classifier():
    """Ivan 2026-07-26 无人值守裁定：judge UNCERTAIN→KEEP_CURRENT 是已完成
    的机器决定（随包披露交付）；任何结构差异都不许进披露通道。"""

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    def row(**overrides):
        adjudication = {
            "status": "OBSERVED",
            "policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        }
        adjudication.update(overrides)
        return {"cue_index": 1, "exact_release_adjudication": adjudication}

    assert _is_keep_current_disclosed(row()) is True
    assert _is_keep_current_disclosed(row(repaired=True)) is False
    assert _is_keep_current_disclosed(row(status="UNAVAILABLE")) is False
    assert _is_keep_current_disclosed(
        row(policy_branch="JUDGE_CHOSE_PROPOSED")
    ) is False
    assert _is_keep_current_disclosed(
        row(mutation_authority={"status": "APPLIED"})
    ) is False
    assert _is_keep_current_disclosed({"cue_index": 1}) is False


def test_decided_keep_current_branches_disclose_and_infra_branches_block():
    """Ivan 2026-07-27 扩展：judge 明确保留 / 拼音门否决 / 同音无 text
    authority / 目标不可闻——都是走完链条的机器决定，随包披露交付；
    基础设施没走完（witness/judge/后端不可用、stale、预算跳过）仍是
    blocker。correction pass 的 context_audio_adjudication 键同样受理。"""

    from src.autoslice.final_review_contract import (
        is_keep_current_disclosed as _is_keep_current_disclosed,
    )

    def row(key="exact_release_adjudication", **overrides):
        adjudication = {
            "status": "OBSERVED",
            "policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        }
        adjudication.update(overrides)
        return {"cue_index": 1, key: adjudication}

    for branch in (
        "JUDGE_KEEPS_CURRENT",
        "JUDGE_CHOICE_PINYIN_INCOMPATIBLE_KEEP_CURRENT",
        "ORTHOGRAPHY_TEXT_AUTHORITY_REQUIRED_KEEP_CURRENT",
        "TARGET_INAUDIBLE_KEEP_CURRENT",
    ):
        assert _is_keep_current_disclosed(row(policy_branch=branch)) is True
        assert _is_keep_current_disclosed(
            row("context_audio_adjudication", policy_branch=branch)
        ) is True

    for infra_branch in (
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "JUDGE_UNAVAILABLE_KEEP_CURRENT",
        "PINYIN_BACKEND_UNAVAILABLE_KEEP_CURRENT",
        "STALE_BASE_KEEP_CURRENT",
        "INVALID_OR_UNCERTAIN_KEEP_CURRENT",
    ):
        assert _is_keep_current_disclosed(
            row(policy_branch=infra_branch)
        ) is False
