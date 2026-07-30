"""Text-only CPA authority for semantically complete short cover hooks."""

from __future__ import annotations

import hashlib
import re
from typing import Callable, Mapping, Sequence

from src.autoslice.llm_client import LlmCall, extract_json_object


SCHEMA_VERSION = "lidousha-cover-punch-semantic-review.v1"
PUNCH_LINE_MAX_EM = 9.0
_CLOSING_PUNCT = tuple("，,、；;！!？?。）》】”’")
_OPENING_PUNCT = tuple("“‘《〈「『（(【[｛{")


def extractive_punch_fragment_is_source_safe(
    fragment: str,
    cover_text: str,
) -> bool:
    """Reject extractive fragments cut immediately before a bracketed atom.

    An ordinary substring check accepted ``让新3D永久保留`` from
    ``让新3D永久保留“白色奶龙”表情``.  The text was source-bound but
    had dropped the verb's concrete object.  A left bracket/quote immediately
    after an extracted occurrence is a deterministic proof that the fragment
    stopped before one source atom.  Accept repeated text when at least one
    occurrence has a safe right boundary.
    """

    needle = _canon(fragment)
    haystack = _canon(cover_text)
    if not needle:
        return False
    start = haystack.find(needle)
    while start >= 0:
        end = start + len(needle)
        if end >= len(haystack) or haystack[end] not in _OPENING_PUNCT:
            return True
        start = haystack.find(needle, start + 1)
    return False


def _canon(text: str) -> str:
    return re.sub(r"\s+", "", text)


def punch_line_em_width(text: str) -> float:
    """Approximate one rendered punch line in full-width em units."""

    return sum(0.5 if " " <= char <= "~" else 1.0 for char in text)


def _validated_extractive_punch(
    value: object,
    cover_text: str,
) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    haystack = _canon(cover_text)
    lines: list[str] = []
    for key in ("main", "sub"):
        raw = value.get(key)
        if raw is None:
            if key == "main":
                return ()
            continue
        if not isinstance(raw, str):
            return ()
        fragment = raw.strip()
        canonical = _canon(fragment)
        if (
            not (2 <= len(canonical) <= 12)
            or punch_line_em_width(fragment) > PUNCH_LINE_MAX_EM
            or "\n" in fragment
            or canonical not in haystack
            or not extractive_punch_fragment_is_source_safe(
                fragment,
                cover_text,
            )
            or fragment.startswith(_CLOSING_PUNCT)
            or fragment.endswith(_OPENING_PUNCT)
        ):
            return ()
        lines.append(fragment)
    if len(lines) == 2 and lines[0] == lines[1]:
        lines = lines[:1]
    return tuple(lines)


def _review_prompt(
    *,
    title: str,
    cover_text: str,
    story_hook: str,
    punch: Sequence[str],
) -> str:
    return (
        "你是李豆沙切片封面的最终文字语义裁决者。你没有音频或图像输入，"
        "只裁决封面梗字；不要声称听见或看见任何内容。\n"
        "陌生观众在信息流里只会先看到通用主播主体和下面 1-2 行梗字。"
        "梗字必须让他理解一件具体发生了什么的事、冲突/反差/荒诞因果，以及"
        "为什么值得点开；不能只是两个各自来自标题、合起来却不成事件的关键词。"
        "例如“生豆角 / 熊猫头下播”含具象名词但没有说明她要拉谁一起中招，"
        "必须 REVISE。不要把背景可能会画出的道具当作文字语义缺口的补丁。\n"
        f"完整投稿标题: {title}\n"
        f"完整封面文案: {cover_text}\n"
        f"StoryContract selection_hook: {story_hook or '(未提供，按标题裁决)'}\n"
        f"初选梗字: {' / '.join(punch)}\n"
        "输出一个 JSON 对象。status 只能是 PASS、REVISE、REJECT。PASS 时"
        " final_punch 必须与初选逐字相同；REVISE 时给更自足的 1-2 行；"
        "REJECT 表示标题里不存在合格的抽取式短梗字。final_punch.main 必填、"
        "sub 可为 null；每行必须是完整封面文案中的逐字连续片段、2-12 字，"
        "并且必须能作为一条物理行直接渲染（最多 9 个全角字宽；ASCII 字符约半个"
        "全角字），不得增删改，也不能依赖渲染器在词中间二次断行。例如不要返回"
        "“被粉色小姐姐布下迷魂阵”，应从原文抽取较短但仍自足的"
        "“小姐姐布下迷魂阵 / 我是侄女啊”。如果原文在候选片段后紧接"
        "引号、书名号或括号成分，该成分就是未完的语义原子，不得在左括号前"
        "截断；例如“让新3D永久保留”必须改为带有“白色奶龙”对象的片段。"
        "stranger_can_infer_event、"
        "contains_concrete_subject、"
        "contains_action_or_conflict 三项只有确实成立才给 true。"
        "story_summary 用一句话说明梗字表达的具体事件，click_motivation 说明"
        "陌生观众为何会想点开；不要复述规则。\n"
        '{"schema_version":"lidousha-cover-punch-semantic-review.v1",'
        '"status":"PASS|REVISE|REJECT",'
        '"final_punch":{"main":"...","sub":null|"..."}|null,'
        '"stranger_can_infer_event":true|false,'
        '"contains_concrete_subject":true|false,'
        '"contains_action_or_conflict":true|false,'
        '"story_summary":"...","click_motivation":"..."}'
    )


def validate_cover_punch_semantic_review(
    review: object,
    *,
    rendered_lines: object,
    cover_text: str,
    story_hook: str,
) -> bool:
    """Recheck the CPA receipt without trusting producer-selected fields."""

    if not isinstance(review, Mapping):
        return False
    if (
        review.get("schema_version") != SCHEMA_VERSION
        or review.get("status") not in {"PASS", "REVISED"}
        or review.get("stranger_can_infer_event") is not True
        or review.get("contains_concrete_subject") is not True
        or review.get("contains_action_or_conflict") is not True
    ):
        return False
    if not all(
        isinstance(review.get(key), str)
        and len(str(review.get(key)).strip()) >= minimum
        for key, minimum in (
            ("story_summary", 6),
            ("click_motivation", 6),
        )
    ):
        return False
    final_punch = review.get("final_punch")
    if (
        not isinstance(final_punch, list)
        or not isinstance(rendered_lines, list)
        or final_punch != rendered_lines
        or not (1 <= len(final_punch) <= 2)
    ):
        return False
    if _validated_extractive_punch(
        {
            "main": final_punch[0],
            "sub": final_punch[1] if len(final_punch) > 1 else None,
        },
        cover_text,
    ) != tuple(final_punch):
        return False
    expected_hashes = {
        "cover_text_sha256": hashlib.sha256(
            cover_text.encode("utf-8")
        ).hexdigest(),
        "story_hook_sha256": hashlib.sha256(
            story_hook.encode("utf-8")
        ).hexdigest(),
    }
    return all(
        review.get(key) == value for key, value in expected_hashes.items()
    )


def review_cover_punch_semantics(
    *,
    title: str,
    cover_text: str,
    story_hook: str,
    punch: tuple[str, ...],
    llm_call: LlmCall | None,
    punch_validator: Callable[
        [object, str], tuple[str, ...]
    ] = _validated_extractive_punch,
) -> tuple[tuple[str, ...], dict[str, object]]:
    """Ask CPA to make the final stranger-comprehension decision."""

    if not punch:
        return (), {
            "schema_version": SCHEMA_VERSION,
            "status": "NOT_APPLICABLE",
            "reason_code": "NO_EXTRACTIVE_PUNCH_CANDIDATE",
        }
    if llm_call is None:
        return (), {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "reason_code": "CPA_TEXT_REVIEW_UNAVAILABLE",
            "original_punch": list(punch),
        }
    prompt = _review_prompt(
        title=title,
        cover_text=cover_text,
        story_hook=story_hook,
        punch=punch,
    )
    request_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    try:
        raw = llm_call(prompt)
        payload = extract_json_object(raw)
    except Exception:
        return (), {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "reason_code": "CPA_TEXT_REVIEW_CALL_FAILED",
            "original_punch": list(punch),
            "request_sha256": request_sha256,
        }
    response_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    model_status = payload.get("status")
    final = punch_validator(payload.get("final_punch"), cover_text)
    booleans_ok = all(
        payload.get(key) is True
        for key in (
            "stranger_can_infer_event",
            "contains_concrete_subject",
            "contains_action_or_conflict",
        )
    )
    story_summary = str(payload.get("story_summary") or "").strip()
    click_motivation = str(payload.get("click_motivation") or "").strip()
    status_shape_ok = bool(
        (model_status == "PASS" and final == punch)
        or (model_status == "REVISE" and final and final != punch)
    )
    accepted = bool(
        payload.get("schema_version") == SCHEMA_VERSION
        and status_shape_ok
        and booleans_ok
        and len(story_summary) >= 6
        and len(click_motivation) >= 6
    )
    proof: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "PASS"
            if accepted and model_status == "PASS"
            else "REVISED"
            if accepted
            else "FAILED"
        ),
        "reason_code": (
            None if accepted else "CPA_PUNCH_SEMANTIC_REVIEW_REJECTED"
        ),
        "original_punch": list(punch),
        "final_punch": list(final),
        "stranger_can_infer_event": (
            payload.get("stranger_can_infer_event") is True
        ),
        "contains_concrete_subject": (
            payload.get("contains_concrete_subject") is True
        ),
        "contains_action_or_conflict": (
            payload.get("contains_action_or_conflict") is True
        ),
        "story_summary": story_summary,
        "click_motivation": click_motivation,
        "cover_text_sha256": hashlib.sha256(
            cover_text.encode("utf-8")
        ).hexdigest(),
        "story_hook_sha256": hashlib.sha256(
            story_hook.encode("utf-8")
        ).hexdigest(),
        "request_sha256": request_sha256,
        "response_sha256": response_sha256,
    }
    return (final if accepted else ()), proof
