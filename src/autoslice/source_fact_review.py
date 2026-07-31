"""Joint CPA text gate for source-bound facts in hooks and titles."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Mapping

from src.autoslice.llm_client import LlmCall, extract_json_object
from src.autoslice.title_policy import publish_title_policy_violations


SCHEMA_VERSION = "lidousha-source-fact-review.v1"
MAX_REVIEW_PASSES = 5


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _finalize_receipt(receipt: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **receipt,
        "receipt_sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _prompt(
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    review_pass: int,
    title_policy_violations: list[str],
) -> str:
    return (
        "你是李豆沙切片派生文案的 source-fact 最终裁决者。你只有文字输入，"
        "不要声称听见音频或看见画面。一次联合裁决 selection_hook 与投稿标题，"
        "不是字幕改写任务。\n"
        "判断两份文案里的每个具体事件、对象、因果、身份、专名、事实模态和同音"
        "释义，是否能由最终字幕或同片 hash-bound 结构化弹幕/SC/上下文支持。"
        "允许不逐字的自然概括，但不允许把提议写成既成事实、把猜测写成断言，"
        "也不允许凭空把同音词换成另一个含义。\n"
        "如果你修复 selection_hook，还必须核对原 selection_scorecard 的"
        "tier_basis、tier_reason、维度和证据 cue 是否仍描述修复后的同一核心梗。"
        "若仍一致，selection_scorecard_review.status=COMPATIBLE；若核心梗已换，"
        "必须填 INCOMPATIBLE，流水线会停止而不是把旧评分卡带进新 StoryContract。"
        "selection_hook 未改时填 NOT_NEEDED。\n"
        "结构化弹幕必须按时间相邻关系整体理解：例如一条写“被点了”，紧接下一条"
        "补“电”，并且近窗还有雷/劈等同主题文字时，“被电”可以是有同片证据的"
        "语义还原，不能仅因它没有逐字出现在单独一行字幕里而拒绝。反之，只有孤立"
        "近音、没有相邻或同主题证据时必须修复。长期记忆和普通 ASR 初稿只提供"
        "候选，不能单独授权新事实。\n"
        "若两份文案全部受支持，status=KEEP，两个 final 字段都必须逐字等于输入。"
        "还要检查省略是否改变了语法角色、条件或因果：来源若是“今天是某人的生日，"
        "转发这条信息才能得奖”，不得概括为“转发某人的生日能得奖”或“生日能得奖”；"
        "必须保留“消息/信息”作为转发宾语，并保留转发这一获奖条件。"
        "若任一不受支持，status=REPAIR，并给出完整、可直接替换、只使用现有"
        "source 事实的 final_selection_hook 与 final_title；标题必须保留原有"
        "频道前缀。禁止只解释问题却不给两份完整文案，也禁止添加 evidence 中没有"
        "的新事实。修复后会继续用同一份 source authority 复审；只要仍有受证据"
        "支持的改动，就可以继续 REPAIR，直到明确 KEEP。最多复审 5 轮；不得为了"
        "结束复审而放弃仍然存在的事实问题，也不得在不同文案之间来回振荡。\n"
        "投稿标题总长度必须为 12–49 个字符并满足下方 deterministic title policy。"
        "若 violations 非空，当前标题不能 KEEP；必须在不丢失核心事实与事实模态的"
        "前提下压缩或修正完整标题，并用 REPAIR 返回。只有 violations 为空且所有"
        "source facts 都正确时才能 KEEP。\n"
        "deterministic_title_policy_violations: "
        + json.dumps(title_policy_violations, ensure_ascii=False)
        + "\n"
        f"review_pass: {review_pass}\n"
        f"selection_hook:\n{selection_hook}\n"
        f"title:\n{title}\n"
        f"最终字幕（文字 authority）:\n{final_transcript}\n"
        f"同片 hash-bound 上下文（含按时间排列的结构化弹幕/SC）:\n"
        f"{clip_context_prompt}\n"
        "原 selection_scorecard:\n"
        + json.dumps(
            selection_scorecard,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
        "只输出一个 JSON 对象。supported_by 只能使用 final_transcript、"
        "structured_chat、same_clip_context；不得填写 audio 或 image。"
        "changed_surfaces 在 KEEP 时为空数组，REPAIR 时逐项列出 artifact、"
        "before、after、reason 和实际 evidence 原文。\n"
        '{"schema_version":"lidousha-source-fact-review.v1",'
        '"status":"KEEP|REPAIR","final_selection_hook":"完整钩子",'
        '"final_title":"完整标题","supported_by":["final_transcript"],'
        '"changed_surfaces":[{"artifact":"selection_hook|title",'
        '"before":"...","after":"...","reason":"...",'
        '"evidence":["..."]}],'
        '"selection_scorecard_review":{"status":'
        '"NOT_NEEDED|COMPATIBLE|INCOMPATIBLE","reason":"..."},'
        '"summary":"..."}'
    )


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def _evidence_row_is_bound(
    value: str,
    *,
    final_transcript: str,
    clip_context_prompt: str,
) -> bool:
    """Accept exact evidence while tolerating CPA citation-label formatting."""

    source_label = re.fullmatch(
        r"\s*final_transcript\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if source_label:
        quoted_text = source_label.group(1)
        return bool(_compact(quoted_text) and _compact(quoted_text) in _compact(final_transcript))
    if re.match(
        r"\s*(structured_chat|same_clip_context)\s*:",
        value,
        flags=re.IGNORECASE,
    ):
        return False

    structured_chat = re.fullmatch(
        r"\s*(danmaku|superchat)\s*@\s*(\d+)\s*ms"
        r"(?:\s+event=([^:]*))?\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if structured_chat:
        kind, offset_ms, event_id, quoted_text = structured_chat.groups()
        rendered_chat_row = re.compile(
            r"\s*-\s*(danmaku|superchat)\s*@\s*(\d+)\s*ms"
            r"(?:\s+event=([^:]*))?\s*:\s*(.+?)\s*",
            flags=re.IGNORECASE,
        )
        return bool(
            _compact(quoted_text)
            and any(
                match
                and match.group(1).casefold() == kind.casefold()
                and match.group(2) == offset_ms
                and (event_id is None or _compact(match.group(3)) == _compact(event_id))
                and _compact(match.group(4)) == _compact(quoted_text)
                for line in clip_context_prompt.splitlines()
                if (match := rendered_chat_row.fullmatch(line))
            )
        )

    compact_value = _compact(value)
    return bool(
        compact_value and compact_value in _compact(final_transcript + "\n" + clip_context_prompt)
    )


def _valid_changed_surface(
    value: object,
    *,
    before_surface: str,
    after_surface: str,
    final_transcript: str,
    clip_context_prompt: str,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if value.get("artifact") not in {"selection_hook", "title"}:
        return False
    if not all(
        isinstance(value.get(key), str) and bool(str(value.get(key)).strip())
        for key in ("before", "after", "reason")
    ):
        return False
    evidence = value.get("evidence")
    if not (
        isinstance(evidence, list)
        and evidence
        and all(isinstance(row, str) and row.strip() for row in evidence)
    ):
        return False
    return bool(
        _compact(value.get("before")) in _compact(before_surface)
        and _compact(value.get("after")) in _compact(after_surface)
        and all(
            _evidence_row_is_bound(
                row,
                final_transcript=final_transcript,
                clip_context_prompt=clip_context_prompt,
            )
            for row in evidence
        )
    )


def _single_review(
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    llm_call: LlmCall | None,
    review_pass: int,
    enforce_automatic_title_style: bool,
) -> dict[str, object]:
    title_policy_violations = publish_title_policy_violations(
        title,
        enforce_automatic_style=enforce_automatic_title_style,
    )
    prompt = _prompt(
        selection_hook=selection_hook,
        title=title,
        final_transcript=final_transcript,
        clip_context_prompt=clip_context_prompt,
        selection_scorecard=selection_scorecard,
        review_pass=review_pass,
        title_policy_violations=title_policy_violations,
    )
    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "review_pass": review_pass,
        "selection_hook_sha256": _sha256_text(selection_hook),
        "title_sha256": _sha256_text(title),
        "final_transcript_sha256": _sha256_text(final_transcript),
        "clip_context_prompt_sha256": _sha256_text(clip_context_prompt),
        "selection_scorecard_sha256": _sha256_json(selection_scorecard),
        "title_policy_mode": ("automatic" if enforce_automatic_title_style else "baseline"),
        "title_policy_violations": title_policy_violations,
        "request_sha256": _sha256_text(prompt),
    }
    if llm_call is None:
        return {
            **base,
            "status": "FAILED",
            "reason_code": "CPA_TEXT_REVIEW_UNAVAILABLE",
        }
    try:
        raw = llm_call(prompt)
        payload = extract_json_object(raw)
    except Exception:
        return {
            **base,
            "status": "FAILED",
            "reason_code": "CPA_TEXT_REVIEW_CALL_FAILED",
        }
    status = payload.get("status")
    final_hook = payload.get("final_selection_hook")
    final_title = payload.get("final_title")
    supported_by = payload.get("supported_by")
    changes = payload.get("changed_surfaces")
    scorecard_review = payload.get("selection_scorecard_review")
    summary = payload.get("summary")
    hook_changed = bool(isinstance(final_hook, str) and final_hook != selection_hook)
    title_changed = bool(isinstance(final_title, str) and final_title != title)
    expected_changed_artifacts = {
        artifact
        for artifact, changed in (
            ("selection_hook", hook_changed),
            ("title", title_changed),
        )
        if changed
    }
    changed_artifacts = (
        {str(row.get("artifact")) for row in changes if isinstance(row, Mapping)}
        if isinstance(changes, list)
        else set()
    )
    changed_surfaces_valid = bool(
        isinstance(changes, list)
        and len(changes) == len(expected_changed_artifacts)
        and changed_artifacts == expected_changed_artifacts
        and all(
            _valid_changed_surface(
                row,
                before_surface=(
                    selection_hook if row.get("artifact") == "selection_hook" else title
                ),
                after_surface=(
                    str(final_hook) if row.get("artifact") == "selection_hook" else str(final_title)
                ),
                final_transcript=final_transcript,
                clip_context_prompt=clip_context_prompt,
            )
            for row in changes
            if isinstance(row, Mapping)
        )
    )
    scorecard_review_valid = bool(
        not hook_changed
        or not isinstance(selection_scorecard, Mapping)
        or (
            isinstance(scorecard_review, Mapping)
            and scorecard_review.get("status") in {"COMPATIBLE", "INCOMPATIBLE"}
            and isinstance(scorecard_review.get("reason"), str)
            and len(str(scorecard_review.get("reason")).strip()) >= 4
        )
    )
    title_prefix_preserved = bool(
        not title.startswith("【")
        or (isinstance(final_title, str) and final_title.startswith(title.split("】", 1)[0] + "】"))
    )
    shape_valid = bool(
        payload.get("schema_version") == SCHEMA_VERSION
        and status in {"KEEP", "REPAIR"}
        and isinstance(final_hook, str)
        and final_hook.strip()
        and isinstance(final_title, str)
        and final_title.strip()
        and title_prefix_preserved
        and isinstance(supported_by, list)
        and supported_by
        and all(
            value
            in {
                "final_transcript",
                "structured_chat",
                "same_clip_context",
            }
            for value in supported_by
        )
        and isinstance(changes, list)
        and isinstance(summary, str)
        and len(summary.strip()) >= 4
        and scorecard_review_valid
        and (
            (
                status == "KEEP"
                and not title_policy_violations
                and final_hook == selection_hook
                and final_title == title
                and not changes
            )
            or (
                status == "REPAIR"
                and (final_hook != selection_hook or final_title != title)
                and bool(changes)
                and changed_surfaces_valid
            )
        )
    )
    return {
        **base,
        "status": status if shape_valid else "FAILED",
        "reason_code": None if shape_valid else "CPA_TEXT_REVIEW_INVALID",
        "final_selection_hook": (final_hook if isinstance(final_hook, str) else ""),
        "final_title": final_title if isinstance(final_title, str) else "",
        "supported_by": (list(supported_by) if isinstance(supported_by, list) else []),
        "changed_surfaces": list(changes) if isinstance(changes, list) else [],
        "selection_scorecard_review": (
            dict(scorecard_review) if isinstance(scorecard_review, Mapping) else None
        ),
        "summary": summary if isinstance(summary, str) else "",
        "response_sha256": _sha256_text(raw),
    }


def review_and_repair_source_facts(
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    llm_call: LlmCall | None,
    selection_scorecard: object = None,
    title_repair_allowed: bool = True,
    enforce_automatic_title_style: bool = False,
) -> dict[str, object]:
    """Run a bounded, evidence-bound KEEP/REPAIR convergence review."""

    current_hook = selection_hook
    current_title = title
    passes: list[dict[str, object]] = []
    seen_surfaces = {(current_hook, current_title)}

    for review_pass in range(1, MAX_REVIEW_PASSES + 1):
        review = _single_review(
            selection_hook=current_hook,
            title=current_title,
            final_transcript=final_transcript,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            llm_call=llm_call,
            review_pass=review_pass,
            enforce_automatic_title_style=enforce_automatic_title_style,
        )
        passes.append(review)
        if review.get("status") == "KEEP":
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "PASS",
                    "decision": "KEEP" if review_pass == 1 else "REPAIRED",
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": current_hook,
                    "final_title": current_title,
                    "passes": passes,
                }
            )
        if review.get("status") != "REPAIR":
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAILED",
                    "decision": "NONE" if review_pass == 1 else "REPAIR_FAILED",
                    "reason_code": review.get("reason_code"),
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": selection_hook,
                    "final_title": title,
                    "passes": passes,
                }
            )

        repaired_hook = str(review.get("final_selection_hook") or "")
        repaired_title = str(review.get("final_title") or "")
        if repaired_title != current_title and not title_repair_allowed:
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAILED",
                    "decision": "REPAIR_REQUIRES_TITLE_AUTHORITY",
                    "reason_code": "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED",
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": selection_hook,
                    "final_title": title,
                    "passes": passes,
                }
            )
        if (
            repaired_hook != current_hook
            and isinstance(selection_scorecard, Mapping)
            and (
                not isinstance(review.get("selection_scorecard_review"), Mapping)
                or review["selection_scorecard_review"].get("status") != "COMPATIBLE"
            )
        ):
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAILED",
                    "decision": "REPAIR_SCORECARD_STALE",
                    "reason_code": "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE",
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": selection_hook,
                    "final_title": title,
                    "passes": passes,
                }
            )
        next_surfaces = (repaired_hook, repaired_title)
        if next_surfaces in seen_surfaces:
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAILED",
                    "decision": "REPAIR_CYCLE",
                    "reason_code": "CPA_SOURCE_FACT_REPAIR_CYCLE",
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": selection_hook,
                    "final_title": title,
                    "passes": passes,
                }
            )
        seen_surfaces.add(next_surfaces)
        current_hook, current_title = next_surfaces

    return _finalize_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "FAILED",
            "decision": "REPAIR_EXHAUSTED",
            "reason_code": "CPA_SOURCE_FACT_REPAIR_EXHAUSTED",
            "original_selection_hook": selection_hook,
            "original_title": title,
            "final_selection_hook": selection_hook,
            "final_title": title,
            "passes": passes,
        }
    )


def source_fact_review_passes(review: object) -> bool:
    return bool(
        isinstance(review, Mapping)
        and review.get("schema_version") == SCHEMA_VERSION
        and review.get("status") == "PASS"
        and review.get("decision") in {"KEEP", "REPAIRED"}
        and isinstance(review.get("final_selection_hook"), str)
        and bool(str(review.get("final_selection_hook")).strip())
        and isinstance(review.get("final_title"), str)
        and bool(str(review.get("final_title")).strip())
    )


def validate_source_fact_review(
    review: object,
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object = None,
) -> bool:
    """Recheck the persisted receipt without trusting selected top-level fields."""

    if not source_fact_review_passes(review) or not isinstance(review, Mapping):
        return False
    receipt = dict(review)
    declared_receipt_sha256 = receipt.pop("receipt_sha256", None)
    if not isinstance(declared_receipt_sha256, str):
        return False
    if _finalize_receipt(receipt).get("receipt_sha256") != declared_receipt_sha256:
        return False
    if review.get("final_selection_hook") != selection_hook or review.get("final_title") != title:
        return False
    passes = review.get("passes")
    if (
        not isinstance(passes, list)
        or not 1 <= len(passes) <= MAX_REVIEW_PASSES
        or any(not isinstance(row, Mapping) for row in passes)
    ):
        return False
    decision = review.get("decision")
    if decision == "KEEP" and (len(passes) != 1 or passes[0].get("status") != "KEEP"):
        return False
    if decision == "REPAIRED" and (
        len(passes) < 2
        or any(row.get("status") != "REPAIR" for row in passes[:-1])
        or passes[-1].get("status") != "KEEP"
    ):
        return False
    final_transcript_sha256 = _sha256_text(final_transcript)
    context_sha256 = _sha256_text(clip_context_prompt)
    scorecard_sha256 = _sha256_json(selection_scorecard)
    if any(
        not isinstance(row, Mapping)
        or row.get("schema_version") != SCHEMA_VERSION
        or row.get("final_transcript_sha256") != final_transcript_sha256
        or row.get("clip_context_prompt_sha256") != context_sha256
        or row.get("selection_scorecard_sha256") != scorecard_sha256
        for row in passes
    ):
        return False
    first = passes[0]
    if first.get("selection_hook_sha256") != _sha256_text(
        str(review.get("original_selection_hook") or "")
    ) or first.get("title_sha256") != _sha256_text(str(review.get("original_title") or "")):
        return False
    expected_hook = str(review.get("original_selection_hook") or "")
    expected_title = str(review.get("original_title") or "")
    seen_surfaces: set[tuple[str, str]] = set()
    for index, row in enumerate(passes):
        current_surfaces = (expected_hook, expected_title)
        if current_surfaces in seen_surfaces:
            return False
        seen_surfaces.add(current_surfaces)
        if (
            row.get("review_pass") != index + 1
            or row.get("selection_hook_sha256") != _sha256_text(expected_hook)
            or row.get("title_sha256") != _sha256_text(expected_title)
        ):
            return False
        if index == len(passes) - 1:
            continue
        next_hook = row.get("final_selection_hook")
        next_title = row.get("final_title")
        if not isinstance(next_hook, str) or not isinstance(next_title, str):
            return False
        if (next_hook, next_title) == current_surfaces:
            return False
        if (
            next_hook != expected_hook
            and isinstance(selection_scorecard, Mapping)
            and (
                not isinstance(row.get("selection_scorecard_review"), Mapping)
                or row["selection_scorecard_review"].get("status") != "COMPATIBLE"
            )
        ):
            return False
        expected_hook, expected_title = next_hook, next_title
    final_pass = passes[-1]
    return bool(
        final_pass.get("selection_hook_sha256") == _sha256_text(selection_hook)
        and final_pass.get("title_sha256") == _sha256_text(title)
    )
