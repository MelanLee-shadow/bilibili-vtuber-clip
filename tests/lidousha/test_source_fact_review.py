import json

from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    review_and_repair_source_facts,
    source_fact_review_passes,
    validate_source_fact_review,
)
from src.autoslice.story_contract import build_story_contract


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
                or {
                    "status": "NOT_NEEDED",
                    "reason": "selection hook remains unchanged",
                }
            ),
            "summary": "相邻的同片文字证据足以完成事实裁决。",
        },
        ensure_ascii=False,
    )


def test_adjacent_structured_chat_supports_beidian_keep() -> None:
    hook = "刚用“喜欢点兔”解释自己为什么被电，小李就连声撤回。"
    title = "【李豆沙】刚用“喜欢点兔”解释自己为什么被电，小李就连声撤回"
    context = "\n".join(
        [
            "- danmaku @1576ms: 小李你怎么被点了！",
            "- danmaku @4115ms: 电",
            "- danmaku @35314ms: 这个雷可以劈小k",
            "- danmaku @43401ms: 大大方方电",
        ]
    )

    def cpa(prompt: str) -> str:
        assert "小李你怎么被点了！" in prompt
        assert "danmaku @4115ms: 电" in prompt
        assert "这个雷可以劈小k" in prompt
        assert hook in prompt and title in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript", "structured_chat"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="小李你怎么被点了\n可能因为我喜欢看点兔吧",
        clip_context_prompt=context,
        llm_call=cpa,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "KEEP"
    assert review["final_selection_hook"] == hook
    assert review["final_title"] == title
    assert len(review["passes"]) == 1


def test_source_fact_prompt_forbids_birthday_forwarding_role_drift() -> None:
    hook = "SC说转发佐伯沙弥香生日能拿菲尔兹奖。"
    title = "【李豆沙】SC说转发佐伯沙弥香生日能拿菲尔兹奖"

    def cpa(prompt: str) -> str:
        assert "不得概括为“转发某人的生日能得奖”" in prompt
        assert "必须保留“消息/信息”作为转发宾语" in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="今天是她的生日\n转发这条信息能得奖",
        clip_context_prompt="",
        llm_call=cpa,
    )


def test_title_policy_violation_must_be_repaired_before_keep() -> None:
    hook = (
        "SC说今天是佐伯沙弥香的生日，转发这条信息能拿菲尔兹奖，李豆沙追问自己也磕原点组怎么没有。"
    )
    long_title = (
        "【李豆沙】SC说今天是佐伯沙弥香的生日，转发这条信息能拿菲尔兹奖，"
        "百合专家小李：我也磕了怎么没有，是不是磕错了？"
    )
    fixed_title = "【李豆沙】SC说转发生日信息能拿菲尔兹奖，小李追问：我也磕原点组怎么没有"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            assert "publish_title_length_out_of_bounds" in prompt
            return _completion(
                status="REPAIR",
                final_hook=hook,
                final_title=fixed_title,
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "title",
                        "before": long_title.removeprefix("【李豆沙】"),
                        "after": fixed_title.removeprefix("【李豆沙】"),
                        "reason": "压缩标题但保留获奖条件和追问。",
                        "evidence": [
                            "转发这条信息你就会获得菲尔兹奖",
                            "老师，我也磕了，我怎么没有",
                        ],
                    }
                ],
            )
        assert "deterministic_title_policy_violations: []" in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=fixed_title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=long_title,
        final_transcript=(
            "今天是佐伯沙弥香的生日\n转发这条信息你就会获得菲尔兹奖\n老师，我也磕了，我怎么没有"
        ),
        clip_context_prompt="",
        llm_call=cpa,
        enforce_automatic_title_style=True,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_title"] == fixed_title
    assert len(review["passes"]) == 2


def test_proposal_must_not_become_completed_naming() -> None:
    bad_hook = "弹幕又把她的技能强行命名成“李姐拉拉”。"
    bad_title = "【李豆沙】话还没说完，技能又被命名成“李姐拉拉”"
    fixed_hook = "弹幕提议把技能叫“李姐拉拉”，她随即拒绝。"
    fixed_title = "【李豆沙】话还没说完，弹幕又提议把技能叫“李姐拉拉”"
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "强行命名成",
                        "after": "提议把技能叫",
                        "reason": "SC 是提议，主播随后拒绝。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "我这个应该叫李姐网",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "技能又被命名成",
                        "after": "弹幕又提议把技能叫",
                        "reason": "保留提议而非既成事实的模态。",
                        "evidence": ["技能可以叫李姐拉拉吗"],
                    },
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=fixed_hook,
            final_title=fixed_title,
            supported_by=["final_transcript", "structured_chat"],
        )

    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="不不不\n我这个应该叫李姐网\n不要不要",
        clip_context_prompt="- superchat: 技能可以叫李姐拉拉吗",
        llm_call=cpa,
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title
    assert len(review["passes"]) == 2
    assert "review_pass: 2" in calls[1]


def test_repair_accepts_source_labels_and_bound_chat_citation_format() -> None:
    bad_hook = "熊猫头发明“李豆沙型侄女”。"
    bad_title = "【李豆沙】熊猫头发明“李豆沙型侄女”"
    fixed_hook = "李豆沙聊起“李豆沙型侄女”。"
    fixed_title = "【李豆沙】李豆沙聊起“李豆沙型侄女”"
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "弹幕先提出该称谓，字幕只支持李豆沙聊起。",
                        "evidence": [
                            "danmaku @29810ms: 这是李豆沙型侄女",
                            "final_transcript: 有点像那个李豆沙型侄女",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "标题删除没有来源支持的身份与发明归属。",
                        "evidence": [
                            "danmaku @29810ms: 这是李豆沙型侄女",
                            "final_transcript: 有点像那个李豆沙型侄女",
                        ],
                    },
                ],
            ),
            _completion(
                status="KEEP",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
            ),
        ]
    )

    review = review_and_repair_source_facts(
        selection_hook=bad_hook,
        title=bad_title,
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("- danmaku @29810ms event=: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: next(responses),
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == fixed_hook
    assert review["final_title"] == fixed_title


def test_repair_rejects_chat_citation_with_unbound_offset() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("- danmaku @29810ms event=: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": [
                        "danmaku @99999ms: 这是李豆沙型侄女",
                    ],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": [
                        "danmaku @99999ms: 这是李豆沙型侄女",
                    ],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_repair_rejects_chat_citation_with_wrong_event_or_text() -> None:
    context = "- danmaku @29810ms event=evt-1: 这是李豆沙型侄女"
    for evidence in (
        "danmaku @29810ms event=evt-2: 这是李豆沙型侄女",
        "danmaku @29810ms event=evt-1: 这是熊猫头型侄女",
    ):
        review = review_and_repair_source_facts(
            selection_hook="熊猫头发明“李豆沙型侄女”。",
            title="【李豆沙】熊猫头发明“李豆沙型侄女”",
            final_transcript="有点像那个李豆沙型侄女",
            clip_context_prompt=context,
            llm_call=lambda _prompt, evidence=evidence: _completion(
                status="REPAIR",
                final_hook="李豆沙聊起“李豆沙型侄女”。",
                final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "弹幕先提出该称谓。",
                        "evidence": [evidence],
                    },
                    {
                        "artifact": "title",
                        "before": "熊猫头发明",
                        "after": "李豆沙聊起",
                        "reason": "标题删除无来源支持的归属。",
                        "evidence": [evidence],
                    },
                ],
            ),
        )

        assert not source_fact_review_passes(review)
        assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_repair_rejects_generic_context_label() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="有点像那个李豆沙型侄女",
        clip_context_prompt=("structured_chat: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": ["structured_chat: 这是李豆沙型侄女"],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": ["structured_chat: 这是李豆沙型侄女"],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_repair_rejects_source_label_bound_only_in_other_corpus() -> None:
    review = review_and_repair_source_facts(
        selection_hook="熊猫头发明“李豆沙型侄女”。",
        title="【李豆沙】熊猫头发明“李豆沙型侄女”",
        final_transcript="李豆沙聊起这个称呼",
        clip_context_prompt=("- danmaku @29810ms event=: final_transcript: 这是李豆沙型侄女"),
        llm_call=lambda _prompt: _completion(
            status="REPAIR",
            final_hook="李豆沙聊起“李豆沙型侄女”。",
            final_title="【李豆沙】李豆沙聊起“李豆沙型侄女”",
            supported_by=["final_transcript", "structured_chat"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "弹幕先提出该称谓。",
                    "evidence": [
                        "final_transcript: 这是李豆沙型侄女",
                    ],
                },
                {
                    "artifact": "title",
                    "before": "熊猫头发明",
                    "after": "李豆沙聊起",
                    "reason": "标题删除无来源支持的归属。",
                    "evidence": [
                        "final_transcript: 这是李豆沙型侄女",
                    ],
                },
            ],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_provider_failure_fails_closed_without_repair_loop() -> None:
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider unavailable")

    review = review_and_repair_source_facts(
        selection_hook="测试钩子",
        title="【李豆沙】测试标题",
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
    assert calls == 1


def test_multiple_repairs_converge_before_bounded_keep() -> None:
    responses = iter(
        [
            _completion(
                status="REPAIR",
                final_hook="小李当场被点名了。",
                final_title="【李豆沙】小李当场被点名了",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "当场被电了",
                        "after": "当场被点名了",
                        "reason": "按最终字幕修复。",
                        "evidence": ["小李你怎么被点了"],
                    },
                    {
                        "artifact": "title",
                        "before": "小李当场被电了",
                        "after": "小李当场被点名了",
                        "reason": "标题与修复后的钩子保持同一事实。",
                        "evidence": ["小李你怎么被点了"],
                    },
                ],
            ),
            _completion(
                status="REPAIR",
                final_hook="小李当场被叫到了。",
                final_title="【李豆沙】小李当场被叫到了",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "被点名了",
                        "after": "被叫到了",
                        "reason": "第二次仍要求改写。",
                        "evidence": ["小李你怎么被点了"],
                    },
                    {
                        "artifact": "title",
                        "before": "小李当场被点名了",
                        "after": "小李当场被叫到了",
                        "reason": "标题与二次修复后的钩子保持一致。",
                        "evidence": ["小李你怎么被点了"],
                    },
                ],
            ),
            _completion(
                status="KEEP",
                final_hook="小李当场被叫到了。",
                final_title="【李豆沙】小李当场被叫到了",
                supported_by=["final_transcript"],
            ),
        ]
    )
    review = review_and_repair_source_facts(
        selection_hook="小李当场被电了。",
        title="【李豆沙】小李当场被电了",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
        llm_call=lambda _prompt: next(responses),
    )

    assert source_fact_review_passes(review)
    assert review["decision"] == "REPAIRED"
    assert review["final_selection_hook"] == "小李当场被叫到了。"
    assert review["final_title"] == "【李豆沙】小李当场被叫到了"
    assert len(review["passes"]) == 3
    assert validate_source_fact_review(
        review,
        selection_hook="小李当场被叫到了。",
        title="【李豆沙】小李当场被叫到了",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
    )


def test_source_fact_repair_stops_after_five_nonconverging_passes() -> None:
    calls: list[str] = []

    def cpa(prompt: str) -> str:
        calls.append(prompt)
        index = len(calls) - 1
        before = f"版本{index}"
        after = f"版本{index + 1}"
        return _completion(
            status="REPAIR",
            final_hook=f"{after}。",
            final_title=f"【李豆沙】{after}",
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "selection_hook",
                    "before": before,
                    "after": after,
                    "reason": "继续删除没有来源支持的事实。",
                    "evidence": ["来源原文"],
                },
                {
                    "artifact": "title",
                    "before": before,
                    "after": after,
                    "reason": "标题同步删除没有来源支持的事实。",
                    "evidence": ["来源原文"],
                },
            ],
        )

    review = review_and_repair_source_facts(
        selection_hook="版本0。",
        title="【李豆沙】版本0",
        final_transcript="来源原文",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["decision"] == "REPAIR_EXHAUSTED"
    assert review["reason_code"] == "CPA_SOURCE_FACT_REPAIR_EXHAUSTED"
    assert len(review["passes"]) == 5
    assert "review_pass: 5" in calls[-1]


def test_repaired_hook_refuses_stale_selection_scorecard() -> None:
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
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
                    "evidence": [
                        "技能可以叫李姐拉拉吗",
                        "不行，我这个应该叫李姐网",
                    ],
                },
            ],
            selection_scorecard_review={
                "status": "INCOMPATIBLE",
                "reason": "旧评分卡围绕已经完成的命名，核心模态已经改变。",
            },
        )

    review = review_and_repair_source_facts(
        selection_hook="技能已经被命名成李姐拉拉。",
        title="【李豆沙】技能已经被命名成李姐拉拉",
        final_transcript="不行，我这个应该叫李姐网",
        clip_context_prompt="- superchat: 技能可以叫李姐拉拉吗",
        selection_scorecard={
            "tier_basis": "audience_driven_performance",
            "tier_reason": "弹幕完成命名",
        },
        llm_call=cpa,
    )

    assert not source_fact_review_passes(review)
    assert review["reason_code"] == ("SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE")
    assert calls == 1


def test_non_text_evidence_claim_is_invalid() -> None:
    review = review_and_repair_source_facts(
        selection_hook="小李被电。",
        title="【李豆沙】小李被电",
        final_transcript="小李你怎么被点了",
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook="小李被电。",
            final_title="【李豆沙】小李被电",
            supported_by=["audio"],
        ),
    )

    assert not source_fact_review_passes(review)
    assert review["passes"][0]["reason_code"] == "CPA_TEXT_REVIEW_INVALID"


def test_persisted_receipt_rejects_bound_surface_tamper() -> None:
    hook = "小李被电后连声撤回。"
    title = "【李豆沙】小李被电后连声撤回"
    transcript = "小李你怎么被点了\n电"
    context = "- danmaku @100ms: 小李你怎么被点了\n- danmaku @200ms: 电"
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript", "structured_chat"],
        ),
    )

    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt=context,
    )
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title + "！",
        final_transcript=transcript,
        clip_context_prompt=context,
    )
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=transcript + "\n新增未审字幕",
        clip_context_prompt=context,
    )


def test_publish_choke_rebuilds_story_before_cover_after_joint_repair(
    tmp_path,
) -> None:
    bad_hook = "弹幕把技能命名成李姐拉拉。"
    bad_title = "【李豆沙】弹幕把技能命名成李姐拉拉，主播继续玩游戏"
    fixed_hook = "弹幕提议把技能叫李姐拉拉，主播随即拒绝。"
    fixed_title = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"
    transcript = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"
    context = "- superchat: 技能可以叫李姐拉拉吗"

    def contract(hook: str) -> dict[str, object]:
        value = build_story_contract(
            candidate_id="candidate-source-fact",
            selection_hook=hook,
            transcript_text=transcript,
            selection_scorecard=None,
            session_relation_authority=None,
        )
        value["clip_context_prompt"] = context
        return value

    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _completion(
                status="REPAIR",
                final_hook=fixed_hook,
                final_title=fixed_title,
                supported_by=["final_transcript", "structured_chat"],
                changed_surfaces=[
                    {
                        "artifact": "selection_hook",
                        "before": "命名成",
                        "after": "提议把技能叫",
                        "reason": "SC 是提议且主播拒绝。",
                        "evidence": [
                            "技能可以叫李姐拉拉吗",
                            "不行，我这个应该叫李姐网",
                        ],
                    },
                    {
                        "artifact": "title",
                        "before": "命名成",
                        "after": "提议把技能叫",
                        "reason": "保留事件模态。",
                        "evidence": ["技能可以叫李姐拉拉吗"],
                    },
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=fixed_hook,
            final_title=fixed_title,
            supported_by=["final_transcript", "structured_chat"],
        )

    cover_calls: list[tuple[dict[str, object], dict[str, object]]] = []

    def stage_cover(
        record: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        cover_calls.append((record, kwargs))
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")
    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "story_contract": contract(bad_hook),
            "artifact_hashes": {},
        },
        candidate_id="candidate-source-fact",
        title=bad_title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=lambda _prompt: json.dumps(
            {
                "title": bad_title,
                "selection_hook_anchor": "命名成李姐拉拉",
            },
            ensure_ascii=False,
        ),
        selection_hook=bad_hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=contract,
        stage_cover=stage_cover,
    )

    assert staged is not None
    assert staged["story_contract"]["selection_hook"] == fixed_hook
    assert staged["story_contract"]["source_fact_review"]["decision"] == ("REPAIRED")
    publish = staged["publish_staging"]
    assert publish["title"] == fixed_title
    assert publish["title_authority_status"] == ("RESOLVED_CPA_SOURCE_FACT_REPAIR")
    assert publish["cover_status"] == "AI_COVER_READY"
    assert len(cover_calls) == 1
    cover_record, cover_kwargs = cover_calls[0]
    assert cover_kwargs["title"] == fixed_title
    assert cover_record["story_contract"]["selection_hook"] == fixed_hook
    assert cover_record["story_contract"]["source_fact_review"]["decision"] == "REPAIRED"
    assert calls == 2


def test_publish_choke_blocks_cover_when_joint_review_provider_fails(
    tmp_path,
) -> None:
    hook = "李豆沙讲了一件事。"
    title = "【李豆沙】李豆沙讲了一件具体而完整的事情"
    transcript = "李豆沙讲了一件事"
    story = build_story_contract(
        candidate_id="candidate-source-fact-fail",
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")

    def cover_must_not_run(*_args, **_kwargs):
        raise AssertionError("cover ran before source-fact authority")

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id="candidate-source-fact-fail",
        title=title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=lambda _prompt: (_ for _ in ()).throw(
            RuntimeError("provider unavailable")
        ),
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=cover_must_not_run,
    )

    assert staged is not None
    publish = staged["publish_staging"]
    assert publish["title_authority_status"] == ("BLOCKED_SOURCE_FACT_REVIEW")
    assert publish["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert publish["source_fact_review"]["status"] == "FAILED"


def test_manual_exact_title_repair_requires_new_authority_before_cover(
    tmp_path,
) -> None:
    hook = "弹幕提议把技能叫李姐拉拉。"
    manual_title = "【李豆沙】短标题"
    repaired_title = "【李豆沙】弹幕提议把技能叫李姐拉拉，主播随即拒绝"
    transcript = "技能可以叫李姐拉拉吗\n不行，我这个应该叫李姐网"
    story = build_story_contract(
        candidate_id="candidate-manual-authority",
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )
    media = tmp_path / "candidate.recut.mp4"
    media.write_bytes(b"video")
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _completion(
            status="REPAIR",
            final_hook=hook,
            final_title=repaired_title,
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "title",
                    "before": "短标题",
                    "after": "弹幕提议把技能叫李姐拉拉，主播随即拒绝",
                    "reason": "补足最终字幕明确支持的完整事件。",
                    "evidence": [
                        "技能可以叫李姐拉拉吗",
                        "不行，我这个应该叫李姐网",
                    ],
                }
            ],
        )

    def cover_must_not_run(*_args, **_kwargs):
        raise AssertionError("cover ran without a new manual title authority")

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "media_path": str(media),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id="candidate-manual-authority",
        title=manual_title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=cover_must_not_run,
    )

    assert staged is not None
    publish = staged["publish_staging"]
    assert publish["title"] == manual_title
    assert publish["title_authority_status"] == ("BLOCKED_SOURCE_FACT_REVIEW")
    assert publish["source_fact_review"]["reason_code"] == ("SOURCE_FACT_TITLE_AUTHORITY_REQUIRED")
    assert publish["cover_status"] == "BLOCKED_TITLE_AUTHORITY"
    assert calls == 1
