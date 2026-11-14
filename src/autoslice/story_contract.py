"""Shared fact contract for selection, subtitles, titles, and covers."""

from __future__ import annotations

from typing import Mapping


_PUBLIC_TEXT_RELATION_TERMS = (
    "联动",
    "连麦",
    "连线",
    "当面对质",
    "当面追问",
    "搭档",
)


def public_text_relation_prompt(story_contract: object) -> str:
    """Bind generated public text to the StoryContract relation authority."""

    if not isinstance(story_contract, Mapping):
        return ""
    confirmed = (
        story_contract.get("relation_state") == "CONFIRMED"
        and story_contract.get("relation_claim_allowed") is True
    )
    if confirmed:
        participants = [
            str(row.get("display_name") or row.get("canonical_id") or "").strip()
            if isinstance(row, Mapping)
            else str(row).strip()
            for row in (story_contract.get("participants") or [])
        ]
        names = "、".join(value for value in participants if value)
        participant_rule = (
            f"已确认参与者仅为：{names}。"
            if names
            else "StoryContract 未列出可写入的参与者身份。"
        )
        return (
            "公共文案关系约束（StoryContract）：关系已 CONFIRMED。"
            + participant_rule
            + "自动标题与 source-fact 联合复审的最终文案只能把关系声明绑定到这些已确认参与者；"
            "不得补写、替换或推断其他人物。此段只是政策指令，不是字幕或 hash-bound 上下文证据，"
            "不得把它写入 supported_by 或 changed_surfaces.evidence。\n"
        )
    terms = "/".join(_PUBLIC_TEXT_RELATION_TERMS)
    return (
        "公共文案关系约束（StoryContract）：关系缺失或未确认。自动标题与 source-fact 联合复审的"
        "公共文案不得把人物写成已联动、连麦、连线、当面对质、当面追问或搭档，也不得用等价的"
        "共同在场、合作或联络断言替代这些词面："
        + terms
        + "。这只限制未经确认的共同在场、合作或联络声明，不是对所有关系话题的禁令：有字幕或同片证据支持的个人事实、提及某人、关系话题或阅读观众聊天仍可描述，"
        "但不得据此断言双方已经共同在场、合作或联络。source text 保持不变。"
        "此段只是政策指令，不是字幕或 hash-bound 上下文证据，不得把它写入 supported_by 或 changed_surfaces.evidence。\n"
    )


