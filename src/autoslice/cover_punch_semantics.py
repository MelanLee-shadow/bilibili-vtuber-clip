"""Text-only CPA authority for semantically complete short cover hooks."""

from __future__ import annotations

import hashlib
import re
from typing import Callable, Mapping, Sequence

from src.autoslice.llm_client import LlmCall, extract_json_object


# v2（Ivan 2026-08-10 逐字裁定）：「梗字从来没有要求过必须是标题的连续子串吧，
# 我不记得我要求过，事实上很多高播放量的切片，封面字块里的梗字和标题不一致，
# 反而可能承接了一些解释原因或者补充说明的感觉，不需要与标题一致重复。」
# 抽取式（连续子串）硬约束因此退役——它从来不是 Ivan 的要求，是 2026-07-21
# 实现梗字模式时为「防 LLM 编造封面字」自造的机械代理（`409e22f`，docstring
# 自述「防的是 LLM 编造封面字」），2026-07-28 `e35b74a` 又在终审层复制加固。
# 那个防编造动机**仍然成立**，只是改由判官的 `no_fabricated_fact` 承担：梗字
# 可以改写、解释原因、补充说明，但不得断言片中没有的事。
SCHEMA_VERSION = "lidousha-cover-punch-semantic-review.v2"
LEGACY_SCHEMA_VERSIONS = ("lidousha-cover-punch-semantic-review.v1",)
FULL_TEXT_COVER_CONTRACT_SCHEMA = "lidousha-full-text-cover-contract.v1"
FULL_TEXT_COVER_AUTHORITY = "IVAN_EXPLICIT"
PUNCH_LINE_MAX_EM = 9.0
COVER_THUMBNAIL_MAX_LINES = 2
# 判官必须逐项为真的语义门。前三项自 2026-07-24「生豆角 / 熊猫头下播」案起生效；
# `no_fabricated_fact` 是 v2 新增的反编造门（fail-closed：缺字段=不通过）。
REQUIRED_REVIEW_BOOLEANS = (
    "stranger_can_infer_event",
    "contains_concrete_subject",
    "contains_action_or_conflict",
    "no_fabricated_fact",
)
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


def cover_thumbnail_lines_are_readable(rendered_lines: object) -> bool:
    """Return whether the final hook is a literal 1-2 line thumbnail unit."""

    return bool(
        isinstance(rendered_lines, list)
        and 1 <= len(rendered_lines) <= COVER_THUMBNAIL_MAX_LINES
        and all(
            isinstance(line, str)
            and line.strip()
            and punch_line_em_width(line.strip()) <= PUNCH_LINE_MAX_EM
            for line in rendered_lines
        )
    )


def cover_text_requires_punch_for_thumbnail(cover_text: str) -> bool:
    """Detect full text that cannot fit the 1-2 line thumbnail contract."""

    explicit_lines = [
        line.strip() for line in str(cover_text or "").splitlines() if line.strip()
    ]
    if not explicit_lines:
        return False
    if len(explicit_lines) > COVER_THUMBNAIL_MAX_LINES:
        return True
    if len(explicit_lines) > 1:
        return any(
            punch_line_em_width(line) > PUNCH_LINE_MAX_EM
            for line in explicit_lines
        )
    return (
        punch_line_em_width(explicit_lines[0])
        > COVER_THUMBNAIL_MAX_LINES * PUNCH_LINE_MAX_EM
    )


def validate_full_text_cover_contract(
    contract: object,
    *,
    cover_text: str,
) -> bool:
    """Validate a separate, explicit authority to render a full talk title.

    A manual publish title is not this contract.  The exception must name its
    own visual scope and bind the exact cover text, so title authority cannot
    accidentally disable the universal thumbnail-text gate.
    """

    if not isinstance(contract, Mapping):
        return False
    expected_sha256 = "sha256:" + hashlib.sha256(
        cover_text.encode("utf-8")
    ).hexdigest()
    return bool(
        contract.get("schema_version") == FULL_TEXT_COVER_CONTRACT_SCHEMA
        and contract.get("status") == "AUTHORIZED"
        and contract.get("authority") == FULL_TEXT_COVER_AUTHORITY
        and contract.get("scope") == "FULL_TEXT_COVER"
        and contract.get("cover_text_sha256") == expected_sha256
        and isinstance(contract.get("reason"), str)
        and str(contract.get("reason")).strip()
    )


def talk_cover_thumbnail_gate_violations(
    generation: object,
    *,
    cover_text: str,
) -> tuple[str, ...]:
    """Return universal final text-gate violations for one talk cover.

    Every ordinary talk cover must render one or two literal lines, each no
    wider than ``PUNCH_LINE_MAX_EM``.  Neither a manual title nor a missing/
    failed punch review changes that contract.  Only a separately authorized,
    exact-text-bound full-cover contract may exempt a full-text cover.
    """

    if not isinstance(generation, Mapping):
        return ("COVER_THUMBNAIL_TEXT_UNREADABLE",)
    art_direction = generation.get("art_direction")
    if (
        isinstance(art_direction, Mapping)
        and art_direction.get("is_song") is True
    ):
        return ()
    rendered_lines = generation.get("rendered_lines")
    readable = cover_thumbnail_lines_are_readable(rendered_lines)
    if readable:
        return ()
    if generation.get("cover_text_mode") == "full":
        if validate_full_text_cover_contract(
            generation.get("full_text_cover_contract"),
            cover_text=cover_text,
        ):
            return ()
        violations = [
            "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL",
            "COVER_FULL_TEXT_CONTRACT_MISSING_OR_INVALID",
            "COVER_THUMBNAIL_TEXT_UNREADABLE",
        ]
        return tuple(violations)
    violations = ["COVER_THUMBNAIL_TEXT_UNREADABLE"]
    if cover_text_requires_punch_for_thumbnail(cover_text):
        violations.append(
            "COVER_PUNCH_REQUIRED_FOR_THUMBNAIL"
        )
    return tuple(violations)


def _validated_punch_lines(
    value: object,
    cover_text: str,
) -> tuple[str, ...]:
    """Mechanically validate the punch **shape** only (v2).

    梗字不必是 ``cover_text`` 的连续子串（Ivan 2026-08-10）。这一层只管可渲染性
    与断字安全：2-12 字、单物理行、行首禁闭标点/行末禁开标点。语义真实性由判官
    的 ``REQUIRED_REVIEW_BOOLEANS``（含 ``no_fabricated_fact``）承担。

    白色奶龙护栏保留但**收窄到它原本的射程**：只有当片段确实是原文的连续子串时，
    「正好停在左引号/书名号/括号前」才是"截掉了一个语义原子"的确定性证据。对
    自由改写的梗字，原文里的括号位置说明不了任何事，套用只会误杀。
    """

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
        is_extractive = canonical in haystack
        if (
            not (2 <= len(canonical) <= 12)
            or punch_line_em_width(fragment) > PUNCH_LINE_MAX_EM
            or "\n" in fragment
            or (
                is_extractive
                and not extractive_punch_fragment_is_source_safe(
                    fragment,
                    cover_text,
                )
            )
            or fragment.startswith(_CLOSING_PUNCT)
            or fragment.endswith(_OPENING_PUNCT)
        ):
            return ()
        lines.append(fragment)
    if len(lines) == 2 and lines[0] == lines[1]:
        lines = lines[:1]
    return tuple(lines)


# 旧名保留为别名：`_validated_extractive_punch` 曾是"必须抽取式"这条已退役规则的
# 载体，外部只按名字引用它做形状校验。
_validated_extractive_punch = _validated_punch_lines


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
        "条件和宾语也不能被截掉后生成新因果。例如完整故事是“转发这条生日消息"
        "能拿菲尔兹奖”，梗字若只写“生日能拿菲尔兹奖”就把生日误写成获奖原因，"
        "必须 REVISE 为同时保留“转发”和“消息/信息”的文字原子。\n"
        f"完整投稿标题: {title}\n"
        f"完整封面文案: {cover_text}\n"
        f"StoryContract selection_hook: {story_hook or '(未提供，按标题裁决)'}\n"
        f"初选梗字: {' / '.join(punch)}\n"
        "输出一个 JSON 对象。status 只能是 PASS、REVISE、REJECT。PASS 时"
        " final_punch 必须与初选逐字相同；REVISE 时给更自足的 1-2 行；"
        "REJECT 表示这条片子配不出任何合格的短梗字。final_punch.main 必填、"
        "sub 可为 null；每行 2-12 字，"
        "并且必须能作为一条物理行直接渲染（最多 9 个全角字宽；ASCII 字符约半个"
        "全角字），也不能依赖渲染器在词中间二次断行。例如不要返回"
        "“被粉色小姐姐布下迷魂阵”，应缩短为仍然自足的"
        "“小姐姐布下迷魂阵 / 我是侄女啊”。\n"
        "梗字**不要求**是标题或封面文案的逐字连续片段，也不必与标题一致重复"
        "（Ivan 2026-08-10 逐字裁定：「梗字从来没有要求过必须是标题的连续子串吧，"
        "我不记得我要求过，事实上很多高播放量的切片，封面字块里的梗字和标题不一致，"
        "反而可能承接了一些解释原因或者补充说明的感觉，不需要与标题一致重复。」）。"
        "你可以改写、缩写、换更口语的说法，第二行也可以承接解释原因或补充说明，"
        "而不是把第一行的词再抄一遍。\n"
        "但梗字**只能说片里真有的事**：其中每一个具体指涉（人、物、动作、数字、"
        "结论）都必须能由上面的完整标题或 StoryContract selection_hook 支撑。"
        "不得新增没出现过的人物/物件/情节，不得把推测写成已发生的事实，不得升级"
        "程度或结果（把「有点无语」写成「当场翻脸」），也不得用背景图也许会画出的"
        "道具去补文字语义缺口。做不到就 REVISE 成有支撑的写法，仍做不到才 REJECT。\n"
        "如果你选择直接抽取原文片段，那就不要停在紧随其后的引号、书名号或括号成分"
        "之前——那个成分是未完的语义原子；例如抽“让新3D永久保留”必须改为带有"
        "“白色奶龙”对象的片段。\n"
        "stranger_can_infer_event、"
        "contains_concrete_subject、"
        "contains_action_or_conflict、"
        "no_fabricated_fact 四项只有确实成立才给 true。"
        "story_summary 用一句话说明梗字表达的具体事件，click_motivation 说明"
        "陌生观众为何会想点开；不要复述规则。\n"
        '{"schema_version":"lidousha-cover-punch-semantic-review.v2",'
        '"status":"PASS|REVISE|REJECT",'
        '"final_punch":{"main":"...","sub":null|"..."}|null,'
        '"stranger_can_infer_event":true|false,'
        '"contains_concrete_subject":true|false,'
        '"contains_action_or_conflict":true|false,'
        '"no_fabricated_fact":true|false,'
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
    if review.get("schema_version") != SCHEMA_VERSION or review.get("status") not in {
        "PASS",
        "REVISED",
    }:
        return False
    # fail-closed：缺字段（例如 v1 老回执被塞进 v2 校验）一律不通过。
    if any(review.get(field) is not True for field in REQUIRED_REVIEW_BOOLEANS):
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
    if _validated_punch_lines(
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
    ] = _validated_punch_lines,
) -> tuple[tuple[str, ...], dict[str, object]]:
    """Ask CPA to make the final stranger-comprehension decision."""

    if not punch:
        return (), {
            "schema_version": SCHEMA_VERSION,
            "status": "NOT_APPLICABLE",
            "reason_code": "NO_PUNCH_CANDIDATE",
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
    # fail-closed：`no_fabricated_fact` 缺席（老 prompt / 老模型）也算不通过。
    booleans_ok = all(
        payload.get(key) is True for key in REQUIRED_REVIEW_BOOLEANS
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
        "no_fabricated_fact": (
            payload.get("no_fabricated_fact") is True
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
