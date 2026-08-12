import hashlib
import json
from pathlib import Path

from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_review import (
    _finalize_receipt,
    _valid_changed_surface,
    review_and_repair_source_facts,
    source_fact_review_passes,
    validate_source_fact_review,
)
from src.autoslice.story_contract import build_story_contract


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _present_speaker_evidence(transcript: str) -> dict[str, object]:
    policy_ids = {
        "alignment": "speaker_cue_subsegment_alignment/v1",
        "text": "compact_ws/v1",
        "timing": "half_open_integer_ms_exact/v1",
    }
    alignment = {
        "schema_version": "speaker-cue-subsegment-alignment.v1",
        "policy_ids": policy_ids,
        "rows": [
            {
                "cue_id": "1",
                "segments": [{"segment_index": 1, "speaker": "李豆沙", "text": "测试字幕"}],
            }
        ],
    }
    return {
        "state": "PresentValid",
        "policy_ids": policy_ids,
        "plain_srt_sha256": "sha256:" + "1" * 64,
        "speaker_final_srt_sha256": "sha256:" + "2" * 64,
        "alignment": alignment,
        "alignment_sha256": _canonical_sha256(alignment),
        "speaker_transcript": transcript,
        "speaker_transcript_sha256": "sha256:"
        + hashlib.sha256(transcript.encode("utf-8")).hexdigest(),
    }


def _absent_speaker_evidence() -> dict[str, object]:
    return {
        "state": "AbsentAuthorized",
        "reason": "speaker_mode_uniform_host",
        "policy_ids": {
            "alignment": "speaker_cue_subsegment_alignment/v1",
            "text": "compact_ws/v1",
            "timing": "half_open_integer_ms_exact/v1",
            "absence": "speaker_mode_uniform_host/v1",
        },
    }


def _completion(
    *,
    status: str,
    final_hook: str,
    final_title: str,
    supported_by: list[str],
    changed_surfaces: list[dict[str, object]] | None = None,
    selection_scorecard_review: dict[str, str] | None = None,
    addressee_attribution: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "schema_version": "lidousha-source-fact-review.v1",
            "status": status,
            "final_selection_hook": final_hook,
            "final_title": final_title,
            "supported_by": supported_by,
            "changed_surfaces": changed_surfaces or [],
            # F12：受话人归属判项在每份新回应里都必须存在（缺键=形状无效）。
            # 这些历史用例都没有说话人转写，判项按 UNVERIFIABLE 车道留空。
            "addressee_attribution": addressee_attribution or [],
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
    assert "entity_context" not in review
    assert "entity_context_sha256" not in review["passes"][0]


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


def test_changed_surface_evidence_can_bind_exact_speaker_transcript_rows() -> None:
    row = {
        "artifact": "title",
        "before": "小李求莉亚放过自己",
        "after": "莉亚求小李放过自己",
        "reason": "说话人标签证明求饶者是连线一方。",
        "evidence": [
            "speaker_transcript: 5 [连线] 我想活着",
            "speaker_transcript: 7 [连线] 你放过我好吗",
        ],
    }
    speaker_transcript = "\n".join(
        [
            "4 [李豆沙] 莉亚，活着",
            "5 [连线] 我想活着",
            "7 [连线] 你放过我好吗",
        ]
    )

    assert _valid_changed_surface(
        row,
        before_surface="【李豆沙】小李求莉亚放过自己",
        after_surface="【李豆沙】莉亚求小李放过自己",
        final_transcript="莉亚，活着\n我想活着\n你放过我好吗",
        clip_context_prompt="",
        speaker_transcript=speaker_transcript,
    )

    assert not _valid_changed_surface(
        {**row, "evidence": ["speaker_transcript: 5 [李豆沙] 我想活着"]},
        before_surface="【李豆沙】小李求莉亚放过自己",
        after_surface="【李豆沙】莉亚求小李放过自己",
        final_transcript="莉亚，活着\n我想活着\n你放过我好吗",
        clip_context_prompt="",
        speaker_transcript=speaker_transcript,
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


def test_provider_failure_fails_closed_after_bounded_retries() -> None:
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
    # provider 层失败在同一 pass 内有界重掷（1 次原始 + 2 次重试），仍 fail-closed
    assert calls == 3
    assert [row["attempt"] for row in review["provider_retries"]] == [1, 2]
    assert len(review["passes"]) == 1


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


def test_persisted_receipt_rebuilds_exact_speaker_evidence_binding() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    final_transcript = "测试字幕"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )

    assert source_fact_review_passes(review)
    assert review["speaker_evidence"] == evidence
    assert review["passes"][0]["speaker_evidence_sha256"] == review["speaker_evidence_sha256"]
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
        speaker_evidence=evidence,
    )
    # New receipts may never be validated through the legacy unchecked mode.
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt="",
    )


def test_persisted_receipt_rejects_rebuilt_speaker_evidence_mismatch() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    drifted = json.loads(json.dumps(evidence, ensure_ascii=False))
    drifted["speaker_final_srt_sha256"] = "sha256:" + "9" * 64

    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        clip_context_prompt="",
        speaker_evidence=drifted,
    )


def test_persisted_receipt_rejects_inconsistent_or_missing_pass_binding() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    speaker_transcript = "1 [李豆沙] 测试字幕"
    evidence = _present_speaker_evidence(speaker_transcript)
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        speaker_transcript=speaker_transcript,
        speaker_evidence=evidence,
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    inconsistent_body = json.loads(json.dumps(review, ensure_ascii=False))
    inconsistent_body.pop("receipt_sha256")
    inconsistent_body["passes"][0]["speaker_evidence_sha256"] = "sha256:" + "8" * 64
    inconsistent = _finalize_receipt(inconsistent_body)
    missing_body = json.loads(json.dumps(review, ensure_ascii=False))
    missing_body.pop("receipt_sha256")
    missing_body["passes"][0].pop("speaker_transcript_sha256")
    missing = _finalize_receipt(missing_body)

    for invalid in (inconsistent, missing):
        assert not validate_source_fact_review(
            invalid,
            selection_hook=hook,
            title=title,
            final_transcript="测试字幕",
            clip_context_prompt="",
            speaker_evidence=evidence,
        )


def test_all_null_legacy_speaker_receipt_requires_authorized_absence_when_rebuilt() -> None:
    hook = "测试字幕形成完整事实。"
    title = "【李豆沙】测试字幕形成完整事实"
    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=lambda _prompt: _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        ),
    )
    common = {
        "selection_hook": hook,
        "title": title,
        "final_transcript": "测试字幕",
        "clip_context_prompt": "",
    }

    # Omitted evidence preserves direct legacy validation, while production's
    # explicit rebuild accepts NULL only for the uniform-host absence state.
    assert validate_source_fact_review(review, **common)
    assert validate_source_fact_review(
        review,
        **common,
        speaker_evidence=_absent_speaker_evidence(),
    )
    assert not validate_source_fact_review(
        review,
        **common,
        speaker_evidence=_present_speaker_evidence("1 [李豆沙] 测试字幕"),
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
            "speaker_mode": "uniform_host",
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
            "speaker_mode": "uniform_host",
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
            "speaker_mode": "uniform_host",
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


def test_prompt_teaches_hard_meme_canon_semantics() -> None:
    hook = "小李面对百合拷问当场自曝立场，弹幕全体起立。"
    title = "【李豆沙】小李面对百合拷问当场自曝立场"
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="你真的是侄女吗，弹幕都看呆了",
        clip_context_prompt="- danmaku @1000ms: 主播是直女吗",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    prompt = seen["prompt"]
    assert "hard-meme-canon" in prompt
    assert "「直女」一律写作「侄女」" in prompt
    assert "不得把规范词面" in prompt or "永远不得把规范词面" in prompt


def _story_entity_srt() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "assets/lidousha/reviewed_subtitle_baselines"
        / "auto_223750_578_734.reviewed.srt"
    )


def test_candidate_entity_context_binds_srt_and_teaches_derived_spelling() -> None:
    hook = "莉娅求小李‘就算你是狼也放过我’，结伴后小李突然连声道歉。"
    title = "【李豆沙】莉娅求小李“就算你是狼也放过我”，结伴后小李突然连声道歉"
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="莉亚 活着\n就算你是狼\n你放过我好吗\n对不起",
        clip_context_prompt="",
        llm_call=cpa,
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=_story_entity_srt(),
    )

    assert source_fact_review_passes(review)
    context = review["entity_context"]
    assert context["schema_version"] == "source-fact-entity-context.v1"
    assert context["final_reviewed_srt_sha256"] == (
        "6d79fdab105d4c7b5edffec66fe526aa01839de342a7c96b8784f4d0100a8062"
    )
    assert review["passes"][0]["entity_context_sha256"] == (context["context_sha256"])
    prompt = seen["prompt"]
    assert "「莉娅」↔「莉亚」 指同一实体" in prompt
    assert "最终字幕可保留 reviewed_surface「莉亚」" in prompt
    assert "derived title_cover 文案" in prompt
    assert "必须写「莉娅」" in prompt
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="莉亚 活着\n就算你是狼\n你放过我好吗\n对不起",
        clip_context_prompt="",
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=_story_entity_srt(),
    )
    # A context-bearing receipt cannot be downgraded to legacy validation.
    assert not validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="莉亚 活着\n就算你是狼\n你放过我好吗\n对不起",
        clip_context_prompt="",
    )
    tampered_body = json.loads(json.dumps(review, ensure_ascii=False))
    tampered_body.pop("receipt_sha256")
    tampered_body["entity_context"]["projection_sha256"] = "0" * 64
    tampered = _finalize_receipt(tampered_body)
    assert not validate_source_fact_review(
        tampered,
        selection_hook=hook,
        title=title,
        final_transcript="莉亚 活着\n就算你是狼\n你放过我好吗\n对不起",
        clip_context_prompt="",
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=_story_entity_srt(),
    )


def test_candidate_entity_input_gate_blocks_wrong_derived_spelling_before_cpa() -> None:
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("CPA ran before the entity surface gate")

    review = review_and_repair_source_facts(
        selection_hook="莉亚求小李放过她。",
        title="【李豆沙】莉亚求小李放过她，结伴后小李突然连声道歉",
        final_transcript="莉亚 活着\n你放过我好吗",
        clip_context_prompt="",
        llm_call=cpa,
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=_story_entity_srt(),
    )

    assert calls == 0
    assert review["status"] == "FAILED"
    assert review["reason_code"] == "SOURCE_FACT_INPUT_ENTITY_SURFACE_INVALID"
    assert "expected=莉娅:observed=莉亚" in review["entity_surface_error"]


def test_candidate_entity_response_gate_rejects_cpa_reversion_to_srt_spelling() -> None:
    hook = "莉娅求小李放过她。"
    title = "【李豆沙】莉娅求小李放过她，结伴后小李突然连声道歉"
    wrong_title = "【李豆沙】莉亚求小李放过她，结伴后小李突然连声道歉"
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        return _completion(
            status="REPAIR",
            final_hook=hook,
            final_title=wrong_title,
            supported_by=["final_transcript"],
            changed_surfaces=[
                {
                    "artifact": "title",
                    "before": "莉娅",
                    "after": "莉亚",
                    "reason": "错误地贴合字幕词面。",
                    "evidence": ["莉亚 活着"],
                }
            ],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="莉亚 活着\n你放过我好吗",
        clip_context_prompt="",
        llm_call=cpa,
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=_story_entity_srt(),
    )

    assert calls == 3
    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_ENTITY_SURFACE_RESPONSE_INVALID"
    assert all(
        row["entity_context_sha256"] == review["entity_context"]["context_sha256"]
        for row in review["passes"]
    )


def test_candidate_entity_context_rejects_final_srt_byte_drift(
    tmp_path: Path,
) -> None:
    drifted = tmp_path / "final.reviewed.srt"
    drifted.write_bytes(_story_entity_srt().read_bytes() + b"\n")
    calls = 0

    def cpa(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("CPA ran with stale reviewed SRT bytes")

    review = review_and_repair_source_facts(
        selection_hook="莉娅求小李放过她。",
        title="【李豆沙】莉娅求小李放过她，结伴后小李突然连声道歉",
        final_transcript="莉亚 活着\n你放过我好吗",
        clip_context_prompt="",
        llm_call=cpa,
        candidate_id="auto_223750_578_734",
        final_reviewed_srt_path=drifted,
    )

    assert calls == 0
    assert review["status"] == "FAILED"
    assert review["reason_code"] == "SOURCE_FACT_ENTITY_CONTEXT_INVALID"
    assert "SOURCE_FACT_FINAL_REVIEWED_SRT_BINDING_MISMATCH" in (review["entity_context_error"])


def test_publish_staging_wires_exact_final_srt_into_entity_context(
    tmp_path: Path,
) -> None:
    hook = "莉娅求小李‘就算你是狼也放过我’，结伴后小李突然连声道歉。"
    title = "【李豆沙】莉娅求小李“就算你是狼也放过我”，结伴后小李突然连声道歉"
    transcript = "莉亚 活着\n我想活着\n就算你是狼\n你放过我好吗\n对不起"
    story = build_story_contract(
        candidate_id="auto_223750_578_734",
        selection_hook=hook,
        transcript_text=transcript,
        selection_scorecard=None,
        session_relation_authority=None,
    )
    media = tmp_path / "auto_223750_578_734.recut.mp4"
    media.write_bytes(b"video")

    def cpa(prompt: str) -> str:
        assert "entity_context_sha256:" in prompt
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    def stage_cover(
        _record: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        cover = tmp_path / "cover.png"
        cover.write_bytes(b"cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover),
            "cover_generation": {
                "status": "READY",
                "rendered_lines": ["莉娅求小李放过我"],
            },
            "reason_codes": [],
        }

    staged = _stage_publish_draft(
        {
            "status": "MATERIALIZED",
            "speaker_mode": "uniform_host",
            "media_path": str(media),
            "subtitle_path": str(_story_entity_srt()),
            "story_contract": story,
            "artifact_hashes": {},
        },
        candidate_id="auto_223750_578_734",
        title=title,
        cues=[SourceCue("cue-1", 0, 1_000, transcript, "zh", "speech", 1.0)],
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook=hook,
        source_fact_llm_call=cpa,
        story_contract_rebuilder=lambda _hook: story,
        stage_cover=stage_cover,
    )

    assert staged is not None
    receipt = staged["story_contract"]["source_fact_review"]
    assert receipt["status"] == "PASS"
    assert receipt["entity_context"]["candidate_id"] == "auto_223750_578_734"
    assert (
        receipt["passes"][0]["entity_context_sha256"]
        == (receipt["entity_context"]["context_sha256"])
    )


def test_judge_repair_reintroducing_banned_surface_is_recanonicalized() -> None:
    hook = "小李当场自曝侄女立场，弹幕全体起立。"
    title = "【李豆沙】小李当场自曝侄女立场引发弹幕轰动"
    transcript = "我就是侄女立场怎么了，弹幕全体起立"
    passes = {"count": 0}

    def cpa(prompt: str) -> str:
        passes["count"] += 1
        if passes["count"] == 1:
            return _completion(
                status="REPAIR",
                final_hook=hook,
                final_title="【李豆沙】小李亲口承认直女立场引发弹幕轰动",
                supported_by=["final_transcript"],
                changed_surfaces=[
                    {
                        "artifact": "title",
                        "before": "当场自曝直女立场",
                        "after": "亲口承认直女立场",
                        "reason": "字幕原话是承认而非自曝",
                        "evidence": ["final_transcript: 我就是侄女立场怎么了"],
                    }
                ],
            )
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title="【李豆沙】小李亲口承认侄女立场引发弹幕轰动",
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript=transcript,
        clip_context_prompt="- danmaku @900ms: 你是直女吗",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    assert review["decision"] == "REPAIRED"
    assert "直女" not in str(review["final_title"])
    assert "侄女" in str(review["final_title"])
    assert validate_source_fact_review(
        review,
        selection_hook=str(review["final_selection_hook"]),
        title=str(review["final_title"]),
        final_transcript=transcript,
        clip_context_prompt="- danmaku @900ms: 你是直女吗",
    )


def test_entry_canonicalizes_banned_surface_in_handwritten_spec() -> None:
    raw_hook = "小李被问是不是直女，当场语塞三秒。"
    canon_hook = "小李被问是不是侄女，当场语塞三秒。"
    raw_title = "【李豆沙】小李被问是不是直女当场语塞三秒"
    canon_title = "【李豆沙】小李被问是不是侄女当场语塞三秒"
    seen: dict[str, str] = {}

    def cpa(prompt: str) -> str:
        seen["prompt"] = prompt
        return _completion(
            status="KEEP",
            final_hook=canon_hook,
            final_title=canon_title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=raw_hook,
        title=raw_title,
        final_transcript="你是不是侄女，当场语塞了三秒",
        clip_context_prompt="- danmaku @500ms: 是不是直女",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    assert review["decision"] == "KEEP"
    assert review["original_selection_hook"] == canon_hook
    assert review["final_title"] == canon_title
    assert f"selection_hook:\n{canon_hook}" in seen["prompt"]


def test_transient_invalid_response_retried_within_same_pass() -> None:
    hook = "小李当场语塞三秒，弹幕全体起立。"
    title = "【李豆沙】小李当场语塞三秒弹幕全体起立"
    calls = {"n": 0}

    def cpa(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "这不是JSON"
        return _completion(
            status="KEEP",
            final_hook=hook,
            final_title=title,
            supported_by=["final_transcript"],
        )

    review = review_and_repair_source_facts(
        selection_hook=hook,
        title=title,
        final_transcript="当场语塞了三秒，弹幕全体起立",
        clip_context_prompt="",
        llm_call=cpa,
    )

    assert review["status"] == "PASS"
    assert review["decision"] == "KEEP"
    assert calls["n"] == 2
    assert len(review["passes"]) == 1
    assert len(review["provider_retries"]) == 1
    assert review["provider_retries"][0]["reason_code"] == "CPA_TEXT_REVIEW_CALL_FAILED"
    assert validate_source_fact_review(
        review,
        selection_hook=hook,
        title=title,
        final_transcript="当场语塞了三秒，弹幕全体起立",
        clip_context_prompt="",
    )


def test_unavailable_provider_is_not_retried() -> None:
    review = review_and_repair_source_facts(
        selection_hook="测试钩子",
        title="【李豆沙】测试标题",
        final_transcript="测试字幕",
        clip_context_prompt="",
        llm_call=None,
    )

    assert review["status"] == "FAILED"
    assert review["reason_code"] == "CPA_TEXT_REVIEW_UNAVAILABLE"
    assert review["provider_retries"] == []
