"""Phase 1 acoustic-witness adjudication: the audio model witnesses, CPA judges.

Ivan's 2026-07-25 architecture ruling (刮/乖/歪、省了/神了、我们/我 cases): the
closed-set acoustic "fit" check kept acting as the final judge while seeing the
candidate sentences — a priming channel — and its ±1-cue context window was too
poor for discourse reasoning. This module splits the roles:

- the audio model runs in pure dictation mode (``entity_audio_verifier``
  witness schema): suspected toneless pinyin only, no candidates shown,
  no hanzi allowed out;
- word choice is reasoned by the CPA judge (gpt-5.6-sol) from the CLOSED
  candidate set with wide subtitle context;
- code — not any model — enforces that the judged choice stays compatible
  with the witnessed pinyin. Every layer fails toward keeping current text.

No acoustic/text witness may choose the delivered text.  A non-operator
mutation must carry a CPA ``PROPOSED`` verdict; witness confidence and typed
text provenance are evidence presented to CPA, never competing decision
authorities.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.llm_client import extract_json_object

try:  # 生产已装（song_name_pin/T1 同款可选依赖）；缺失时拼音校验不可用 → fail closed
    from pypinyin import lazy_pinyin as _lazy_pinyin
except Exception:  # pragma: no cover - environment-dependent
    _lazy_pinyin = None


WITNESS_REQUEST_SCHEMA = "subtitle-span-acoustic-witness-request.v1"
ADJUDICATION_SCHEMA = "acoustic-witness-adjudication.v1"

# Retained as a diagnostic threshold; CPA, not the witness, owns the decision.
MIN_CHOICE_COMPATIBILITY = 0.55


def valid_cpa_witness_adjudication(verdict: Mapping[str, Any]) -> bool:
    """Validate the typed evidence chain for a CPA-owned acoustic choice."""

    confidence = verdict.get("confidence")
    required_hashes = (
        "witness_request_sha256",
        "judge_prompt_sha256",
        "judge_completion_sha256",
    )
    return bool(
        not isinstance(confidence, bool)
        and isinstance(confidence, (int, float))
        and 0.0 <= float(confidence) <= 1.0
        and verdict.get("decision_authority") == "CPA_JUDGE"
        and verdict.get("witness_authority") == "EVIDENCE_ONLY"
        and verdict.get("witness_status") in {"OBSERVED", "UNCERTAIN"}
        and all(
            isinstance(verdict.get(key), str)
            and len(str(verdict[key]).removeprefix("sha256:")) == 64
            and all(
                char in "0123456789abcdef"
                for char in str(verdict[key]).removeprefix("sha256:").lower()
            )
            for key in required_hashes
        )
    )


def build_witness_request(check_request: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a candidate-free dictation request from a check request.

    Only the audio-window geometry survives; every textual field (current,
    proposed, contexts, candidate entities) is stripped so the witness request
    physically cannot prime the transcription.
    """

    request: dict[str, Any] = {
        "schema_version": WITNESS_REQUEST_SCHEMA,
        "kind": "subtitle_span_acoustic_witness",
        "evidence_id": str(check_request.get("evidence_id") or ""),
        "cue_indexes": list(check_request.get("cue_indexes") or []),
        "matched_start_ms": int(check_request["matched_start_ms"]),
        "matched_end_ms": int(check_request["matched_end_ms"]),
        "context_start_ms": int(check_request["context_start_ms"]),
        "context_end_ms": int(check_request["context_end_ms"]),
        "source_media_timeline_offset_ms": int(
            check_request["source_media_timeline_offset_ms"]
        ),
    }
    request["request_sha256"] = hashlib.sha256(
        json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return request


def _pinyin_tokens(text: str) -> list[str] | None:
    if _lazy_pinyin is None:
        return None
    tokens = [
        token.strip().lower()
        for token in _lazy_pinyin(text)
        if token and token.strip()
    ]
    # non-hanzi spans (latin letters, digits) come back verbatim; keep them
    # as single tokens so KO/N-style spellings still participate.
    return [re.sub(r"\s+", "", token) for token in tokens if token]


def pinyin_compatibility(
    candidate_text: str,
    *,
    heard_pinyin: str,
    uncertain_positions: list[int] | tuple[int, ...] = (),
) -> float | None:
    """Token-level similarity between a candidate's pinyin and the dictation.

    ``?`` witness tokens and tokens listed in ``uncertain_positions`` are
    wildcards: they match whatever the candidate has at the aligned position
    (the witness itself declared no knowledge there). Returns None when the
    pinyin backend is unavailable — callers must fail closed on None.
    """

    candidate = _pinyin_tokens(candidate_text)
    if candidate is None:
        return None
    heard = [token for token in heard_pinyin.strip().lower().split() if token]
    uncertain = {
        int(value)
        for value in uncertain_positions
        if isinstance(value, int) and not isinstance(value, bool)
    }
    if not candidate or not heard:
        return 0.0
    normalized_heard = list(heard)
    matcher = difflib.SequenceMatcher(None, normalized_heard, candidate)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    # grant wildcard credit for unmatched witness positions declared unsure
    unmatched_wild = 0
    matched_positions: set[int] = set()
    for block in matcher.get_matching_blocks():
        matched_positions.update(range(block.a, block.a + block.size))
    for index, token in enumerate(normalized_heard):
        if index in matched_positions:
            continue
        if token == "?" or index in uncertain:
            unmatched_wild += 1
    effective = matched + min(unmatched_wild, max(0, len(candidate) - matched))
    return (2.0 * effective) / (len(candidate) + len(heard))


_JUDGE_PROMPT = """# 字幕选字裁决（闭集）

你是字幕修复的最终选字法官。一名听写证人已经把目标区间的音节按拼音记录如下；
证人从未见过任何候选文本。你的任务：结合语篇推理，从闭集中选出最符合
「拼音证据 + 语境」的候选。铁律：

1. 只能选择 CURRENT、PROPOSED 或 NEITHER。NEITHER 表示听写拼音与
   两个候选都明显不符；它会把问题退回提案层重建闭集，不会自动保留
   CURRENT，也不授权任何文本修改。绝不生成新文本。
2. 拼音证据优先：与听写音节明显冲突的候选不能当选，语义再通也不行。
3. 语境（前后句、弹幕、平行句）在拼音无法区分候选、或听写证据被标记为
   受污染/不可用时，可以在闭集内定夺。
4. 目标区间外出现过相同词语，本身不证明目标区间内说了它；但相邻句对同一
   词的重复、呼应或应答，可作为闭集内选择的佐证——仍绝不引入闭集外新字。
5. 真实的中英/中日混杂是存在的（Ivan 2026-07-26）：外语候选若在语境中
   **语义通顺**，应正常参与裁决、可以当选；只有当外语读法在语境里根本
   不通顺、而拼音证据又与中文候选相容时，才判定为中文被拉丁化误转写、
   选择中文候选。分辨的根本理由是语义，不是文字系统。
6. 「绑定文字证据」只证明候选的规范写法，不单独证明目标区间说了它。若
   拼音/语篇确认目标指向该实体或原文，必须采用其规范写法；AGY、ASR、
   glossary、roster、弹幕、OCR 都只是证据，最终闭集选择仍由你作出。

## 听写证人报告（未见候选）
- 目标区间可闻人声: {target_audible}
- 疑似拼音: {heard_pinyin}
- 音节数: {syllable_count}
- 不确定位置: {uncertain_positions}
- 证人置信: {confidence}

## 闭集候选
- CURRENT（现字幕整句）: {current_cue}
- PROPOSED（提案整句）: {proposed_cue}
（差异点：suspect={suspect!r} → replacement={replacement!r}；repair_class={repair_class}）

## 语境（转写自同一音频；是语境不是文本权威）
前文:
{context_before}
目标句: <待裁决>
后文:
{context_after}

## 绑定文字证据（证据，不是先行裁决）
{text_evidence}

{structured_chat_block}
按概率排序并**必须选概率最高者**（Ivan 2026-07-27：不许拿不准就保持原样——
原样可能是最差的；把 CURRENT、PROPOSED 和“两者均非原话”NEITHER 的概率
全部写出来，选最高）。
只回一个 JSON 对象（无 markdown 围栏、无其他文字）:
{{"ranking": [{{"choice": "CURRENT"或"PROPOSED"或"NEITHER", "p": 0.0到1.0}}, ...全部候选],
 "choice": "排序第一的那个", "reason": "引用拼音/语境证据的一句话理由"}}
"""


_JUDGE_CACHE_SCHEMA = "judge-verdict-cache.v1"


def _judge_cache_path(prompt_sha256: str) -> Path | None:
    """Content-addressed CPA-judge cache entry, or None when disabled.

    Ivan 2026-07-27 自修复成本令：重试/边界自修复轮对**同一个问题**（同
    听写+同候选+同语境，即同 prompt_sha）不得再发新请求。与声学缓存同构：
    键=prompt 内容 sha，输入任何一处变化自然失效；根=AUTOSLICE_BASE（主
    树与 V15 恢复树各自命中自己的缓存），测试环境不设根则完全旁路。
    """

    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "judge-verdicts"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def judge_word_choice(
    *,
    llm_call: Callable[[str], str],
    check_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    context_before: str = "",
    context_after: str = "",
    structured_chat_context: str = "",
) -> dict[str, Any]:
    """Ask the CPA judge to pick from the closed set; never trusts free text."""

    chat_block = (
        f"## 结构化弹幕/SC（平台记录）\n{structured_chat_context}\n\n"
        if structured_chat_context.strip()
        else ""
    )
    prompt = _JUDGE_PROMPT.format(
        target_audible=witness.get("target_audible"),
        heard_pinyin=str(witness.get("heard_pinyin") or ""),
        syllable_count=witness.get("syllable_count"),
        uncertain_positions=witness.get("uncertain_positions"),
        confidence=witness.get("confidence"),
        current_cue=str(check_request.get("current_cue") or ""),
        proposed_cue=str(check_request.get("proposed_cue") or ""),
        suspect=str(check_request.get("suspect") or ""),
        replacement=str(check_request.get("replacement") or ""),
        repair_class=str(check_request.get("repair_class") or ""),
        context_before=context_before or str(check_request.get("context_before") or "（无）"),
        context_after=context_after or str(check_request.get("context_after") or "（无）"),
        text_evidence=json.dumps(
            {
                "candidate_provenance": check_request.get(
                    "candidate_provenance"
                ),
                "orthography_authority": check_request.get(
                    "orthography_authority"
                ),
                "reviewer_reason": check_request.get("reason"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        structured_chat_block=chat_block,
    )
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    cache_path = _judge_cache_path(prompt_sha256)
    if cache_path is not None:
        try:
            entry = json.loads(cache_path.read_text(encoding="utf-8"))
            stored = entry.get("verdict")
            if (
                entry.get("schema_version") == _JUDGE_CACHE_SCHEMA
                and entry.get("prompt_sha256") == prompt_sha256
                and isinstance(stored, dict)
                and stored.get("status") == "JUDGED"
            ):
                served = dict(stored)
                served["served_from_cache"] = True
                return served
        except (OSError, ValueError):
            pass
    try:
        completion = llm_call(prompt)
        payload = extract_json_object(completion)
    except Exception as exc:
        return {
            "schema_version": ADJUDICATION_SCHEMA,
            "status": "JUDGE_UNAVAILABLE",
            "choice": "UNCERTAIN",
            "reason_code": "JUDGE_CALL_FAILED",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "prompt_sha256": prompt_sha256,
        }
    choice = str(payload.get("choice") or "").strip().upper()
    ranking_raw = payload.get("ranking")
    ranking: list[dict[str, object]] = []
    if isinstance(ranking_raw, list):
        for row in ranking_raw:
            if not isinstance(row, dict):
                continue
            row_choice = str(row.get("choice") or "").strip().upper()
            probability = row.get("p")
            if row_choice in {"CURRENT", "PROPOSED", "NEITHER"} and isinstance(
                probability, (int, float)
            ) and not isinstance(probability, bool) and 0.0 <= float(
                probability
            ) <= 1.0:
                ranking.append({"choice": row_choice, "p": float(probability)})
    # Ivan 2026-07-27：必须按概率排序选最高——排序有效时它就是裁决；
    # UNCERTAIN 不再是合法终点（模型仍拒绝时以排序第一顶上）。
    if ranking:
        top = max(ranking, key=lambda row: row["p"])
        if choice not in {"CURRENT", "PROPOSED", "NEITHER"} or (
            choice != top["choice"]
        ):
            choice = str(top["choice"])
    if choice not in {"CURRENT", "PROPOSED", "NEITHER"}:
        # No usable ranking and an out-of-set/uncertain answer: refusal.
        return {
            "schema_version": ADJUDICATION_SCHEMA,
            "status": "JUDGE_OUT_OF_SET",
            "choice": "UNCERTAIN",
            "reason_code": "JUDGE_CHOICE_OUT_OF_SET",
            "raw_choice": choice[:80],
            "prompt_sha256": prompt_sha256,
        }
    verdict = {
        "schema_version": ADJUDICATION_SCHEMA,
        "status": "JUDGED",
        "choice": choice,
        "ranking": ranking,
        "reason": str(payload.get("reason") or "")[:400],
        "prompt_sha256": prompt_sha256,
        "completion_sha256": hashlib.sha256(
            completion.encode("utf-8")
        ).hexdigest(),
    }
    if cache_path is not None:
        # 只缓存 JUDGED 终态；写失败绝不影响生产（与声学缓存同约定）。
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema_version": _JUDGE_CACHE_SCHEMA,
                        "prompt_sha256": prompt_sha256,
                        "verdict": verdict,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return verdict


def adjudicate_with_witness(
    *,
    check_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    llm_call: Callable[[str], str] | None,
    structured_chat_context: str = "",
) -> tuple[bool, str, dict[str, Any]]:
    """Fuse witness dictation + CPA word choice + code-level pinyin gate.

    Returns (repaired, policy_branch, audit). Every uncertainty keeps the
    current text; only a judged PROPOSED that stays pinyin-compatible — and
    not clearly worse than CURRENT — may repair.
    """

    audit: dict[str, Any] = {
        "schema_version": ADJUDICATION_SCHEMA,
        "witness_request_sha256": witness.get("request_sha256"),
        "witness_status": witness.get("status"),
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
    }
    repair_class = str(check_request.get("repair_class") or "")
    witness_valid = (
        isinstance(witness, Mapping)
        and witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and isinstance(witness.get("target_audible"), bool)
    )
    if not witness_valid:
        return False, "WITNESS_UNAVAILABLE_KEEP_CURRENT", audit
    if llm_call is None:
        return False, "JUDGE_UNAVAILABLE_KEEP_CURRENT", audit

    verdict = judge_word_choice(
        llm_call=llm_call,
        check_request=check_request,
        witness=witness,
        structured_chat_context=structured_chat_context,
    )
    audit["judge"] = verdict
    if not witness["target_audible"]:
        if verdict.get("choice") == "NEITHER":
            return False, "JUDGE_REJECTS_CLOSED_SET", audit
        if verdict.get("choice") != "PROPOSED":
            branch = (
                "JUDGE_KEEPS_CURRENT"
                if verdict.get("choice") == "CURRENT"
                else "JUDGE_UNCERTAIN_KEEP_CURRENT"
            )
            return False, branch, audit
        # Silence is evidence for a deletion proposal, not a decision.  CPA
        # must still choose PROPOSED from the closed set before any bytes move.
        if repair_class == "acoustic_drop_cue":
            return True, "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE", audit
        if repair_class == "acoustic_delete":
            return True, "CPA_JUDGE_APPLY_INAUDIBLE_DELETE_SPAN", audit
        # Audibility is evidence presented to CPA, not a second final vote.
        return True, "CPA_JUDGE_APPLY_PROPOSED_INAUDIBLE_WITNESS", audit
    heard = str(witness.get("heard_pinyin") or "")
    uncertain = list(witness.get("uncertain_positions") or [])
    compat_proposed = pinyin_compatibility(
        str(check_request.get("proposed_cue") or ""),
        heard_pinyin=heard,
        uncertain_positions=uncertain,
    )
    compat_current = pinyin_compatibility(
        str(check_request.get("current_cue") or ""),
        heard_pinyin=heard,
        uncertain_positions=uncertain,
    )
    audit["pinyin_compatibility"] = {
        "proposed": compat_proposed,
        "current": compat_current,
    }
    if verdict.get("choice") == "NEITHER":
        return False, "JUDGE_REJECTS_CLOSED_SET", audit
    if verdict.get("choice") != "PROPOSED":
        branch = (
            "JUDGE_KEEPS_CURRENT"
            if verdict.get("choice") == "CURRENT"
            else "JUDGE_UNCERTAIN_KEEP_CURRENT"
        )
        return False, branch, audit
    witness_conflict = bool(
        compat_proposed is None
        or compat_current is None
        or compat_proposed < MIN_CHOICE_COMPATIBILITY
        or compat_proposed < compat_current
    )
    audit["witness_diagnostic_conflict"] = witness_conflict
    if witness_conflict:
        # CPA already received the witness, closed candidate set and context.
        # AGY pinyin remains a diagnostic, including for deletion proposals,
        # but cannot overturn CPA's explicit PROPOSED choice.
        return True, "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT", audit
    return True, "WITNESS_JUDGE_APPLY_PROPOSED", audit
