"""Text-only CPA authority for semantically complete short cover hooks."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Callable, Mapping, Sequence

from src.autoslice.llm_client import LlmCall, extract_json_object


SCHEMA_VERSION = "lidousha-cover-punch-semantic-review.v1"
FULL_TEXT_COVER_CONTRACT_SCHEMA = "lidousha-full-text-cover-contract.v1"
FULL_TEXT_COVER_AUTHORITY = "IVAN_EXPLICIT"
PUNCH_LINE_MAX_EM = 9.0
COVER_THUMBNAIL_MAX_LINES = 2
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


def punch_fragment_whitespace_is_source_safe(
    fragment: str, cover_text: str
) -> bool:
    """Never let canonicalization invent whitespace inside a physical line."""

    if any(
        unicodedata.category(char) in {"Cc", "Zl", "Zp"}
        for char in fragment
    ):
        return False
    if any(char.isspace() and char != " " for char in fragment):
        return False
    return " " not in fragment or fragment in cover_text


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
            or not punch_fragment_whitespace_is_source_safe(fragment, cover_text)
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


def _valid_hex_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _retry_lineage_is_valid(
    review: Mapping[str, object],
    final_punch: list[object],
    *,
    allow_legacy_retryless: bool,
) -> bool:
    """Validate current retry receipts while retaining explicit legacy v1 read."""

    has_count = "attempt_count" in review
    has_attempts = "attempts" in review
    if not has_count and not has_attempts:
        # Current receipts cannot be distinguished from a current proof whose
        # retry lineage was deleted.  Fail closed even when a legacy caller asks
        # for compatibility; same-schema retryless proofs need rematerializing.
        return False
    if not has_count or not has_attempts:
        return False
    count = review.get("attempt_count")
    attempts = review.get("attempts")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not (1 <= count <= 2)
        or not isinstance(attempts, list)
        or len(attempts) != count
    ):
        return False
    accepted_rows: list[Mapping[str, object]] = []
    for index, row in enumerate(attempts, start=1):
        if not isinstance(row, Mapping) or row.get("attempt") != index:
            return False
        if not _valid_hex_digest(row.get("request_sha256")):
            return False
        status = row.get("status")
        if status not in {"CALL_OR_JSON_FAILED", "REJECTED", "ACCEPTED"}:
            return False
        if status in {"REJECTED", "ACCEPTED"}:
            if set(row) != {
                "attempt",
                "request_sha256",
                "response_sha256",
                "model_status",
                "validated_final_punch",
                "status",
            }:
                return False
            if not _valid_hex_digest(row.get("response_sha256")):
                return False
            if not isinstance(row.get("validated_final_punch"), list):
                return False
        else:
            allowed = {"attempt", "request_sha256", "response_sha256", "status"}
            if not set(row).issubset(allowed) or not {
                "attempt",
                "request_sha256",
                "status",
            }.issubset(row):
                return False
            if "response_sha256" in row and not _valid_hex_digest(
                row.get("response_sha256")
            ):
                return False
        if status == "ACCEPTED":
            accepted_rows.append(row)
    if len(accepted_rows) != 1 or accepted_rows[0] is not attempts[-1]:
        return False
    accepted = accepted_rows[0]
    expected_model_status = "PASS" if review.get("status") == "PASS" else "REVISE"
    return bool(
        accepted.get("model_status") == expected_model_status
        and accepted.get("validated_final_punch") == final_punch
        and review.get("request_sha256") == accepted.get("request_sha256")
        and review.get("response_sha256") == accepted.get("response_sha256")
    )


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
    allow_legacy_retryless: bool = False,
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
    if not _retry_lineage_is_valid(
        review,
        final_punch,
        allow_legacy_retryless=allow_legacy_retryless,
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
    base_prompt = _review_prompt(
        title=title,
        cover_text=cover_text,
        story_hook=story_hook,
        punch=punch,
    )
    attempts: list[dict[str, object]] = []
    payload: Mapping[str, object] = {}
    final: tuple[str, ...] = ()
    accepted = False
    model_status: object = None
    story_summary = ""
    click_motivation = ""
    request_sha256 = hashlib.sha256(base_prompt.encode("utf-8")).hexdigest()
    response_sha256: str | None = None
    for attempt_index in range(2):
        prompt = base_prompt
        if attempt_index:
            prompt += (
                "\n上一轮输出没有通过确定性梗字校验。这是唯一一次有界重试。"
                "请忽略碎片化的初选，重新穷举完整封面文案中可逐字连续抽取的"
                "短片段；若你已经能在 story_summary 说明一个具体事件，就应优先"
                "REVISE 成能表达该事件的 1-2 行，而不是一边说明事件一边 REJECT。"
                "仍须每行最多 9 个全角字宽，且不得编造或改写。"
            )
        attempt_request_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        attempt_response_sha256: str | None = None
        attempt_payload: Mapping[str, object] = {}
        attempt_final: tuple[str, ...] = ()
        attempt_model_status: object = None
        attempt_story_summary = ""
        attempt_click_motivation = ""
        attempt_accepted = False
        try:
            raw = llm_call(prompt)
            attempt_response_sha256 = hashlib.sha256(
                raw.encode("utf-8")
            ).hexdigest()
            attempt_payload = extract_json_object(raw)
        except Exception:
            request_sha256 = attempt_request_sha256
            response_sha256 = attempt_response_sha256
            payload = {}
            final = ()
            model_status = None
            story_summary = ""
            click_motivation = ""
            accepted = False
            failed_row: dict[str, object] = {
                "attempt": attempt_index + 1,
                "request_sha256": attempt_request_sha256,
                "status": "CALL_OR_JSON_FAILED",
            }
            if attempt_response_sha256 is not None:
                failed_row["response_sha256"] = attempt_response_sha256
            attempts.append(
                failed_row
            )
            continue
        attempt_model_status = attempt_payload.get("status")
        attempt_final = punch_validator(
            attempt_payload.get("final_punch"), cover_text
        )
        booleans_ok = all(
            attempt_payload.get(key) is True
            for key in (
                "stranger_can_infer_event",
                "contains_concrete_subject",
                "contains_action_or_conflict",
            )
        )
        attempt_story_summary = str(
            attempt_payload.get("story_summary") or ""
        ).strip()
        attempt_click_motivation = str(
            attempt_payload.get("click_motivation") or ""
        ).strip()
        status_shape_ok = bool(
            (attempt_model_status == "PASS" and attempt_final == punch)
            or (
                attempt_model_status == "REVISE"
                and attempt_final
                and attempt_final != punch
            )
        )
        attempt_accepted = bool(
            attempt_payload.get("schema_version") == SCHEMA_VERSION
            and status_shape_ok
            and booleans_ok
            and len(attempt_story_summary) >= 6
            and len(attempt_click_motivation) >= 6
        )
        request_sha256 = attempt_request_sha256
        response_sha256 = attempt_response_sha256
        payload = attempt_payload
        final = attempt_final
        model_status = attempt_model_status
        story_summary = attempt_story_summary
        click_motivation = attempt_click_motivation
        accepted = attempt_accepted
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "request_sha256": attempt_request_sha256,
                "response_sha256": attempt_response_sha256,
                "model_status": attempt_model_status,
                "validated_final_punch": list(attempt_final),
                "status": "ACCEPTED" if attempt_accepted else "REJECTED",
            }
        )
        if attempt_accepted:
            break
    if attempts[-1].get("status") == "CALL_OR_JSON_FAILED":
        failed_proof: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "reason_code": "CPA_TEXT_REVIEW_CALL_FAILED",
            "original_punch": list(punch),
            "request_sha256": request_sha256,
            "attempt_count": len(attempts),
            "attempts": attempts,
        }
        if response_sha256 is not None:
            failed_proof["response_sha256"] = response_sha256
        return (), failed_proof
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
        "attempt_count": len(attempts),
        "attempts": attempts,
    }
    return (final if accepted else ()), proof
