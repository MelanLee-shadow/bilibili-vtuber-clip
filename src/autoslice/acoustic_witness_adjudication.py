"""Phase 1 acoustic-witness adjudication: the audio model witnesses, CPA judges.

维护者's architecture ruling (刮/乖/歪、省了/神了、我们/我 cases): the
closed-set acoustic "fit" check kept acting as the final judge while seeing the
candidate sentences — a priming channel — and its ±1-cue context window was too
poor for discourse reasoning. This module splits the roles:

- the audio model runs in pure dictation mode (``entity_audio_verifier``
  witness schema): suspected toneless pinyin only, no candidates shown,
  no hanzi allowed out;
- word choice is reasoned by the CPA judge (gpt-5.6-sol) from the CLOSED
  candidate set with wide subtitle context;
- code — not any model — enforces that a pinyin-incompatible PROPOSED carries
  registered or independently bound support. Every layer fails toward CURRENT.

No acoustic/text witness may choose the delivered text.  A non-operator
mutation must carry a CPA ``PROPOSED`` verdict; witness confidence and typed
text provenance are evidence presented to CPA, never competing decision
authorities.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.autoslice.acoustic_pinyin import (
    candidate_pinyin_similarities,
    neutral_syllable_count_hint,
    pinyin_compatibility as pinyin_compatibility,
)
from src.autoslice.acoustic_witness_availability import (
    AUDIO_VERIFIER_UNAVAILABLE,
)
from src.autoslice.acoustic_witness_protocol import (
    BLIND_PINYIN_PROTOCOL,
    LEGACY_SIGHTED_PROTOCOL,
    supported_witness_protocol,
    witness_protocol,
)
from src.autoslice.candidate_support import (
    orthography_ambiguous,
    registered_misheard_direction,
    structured_text_support,
)
from src.autoslice.exact_source_transcript_contract import (
    valid_exact_source_transcript_handoff,
)
from src.autoslice.closed_set_evidence import (
    session_recurrence_acoustic_gate,
)
from src.autoslice.llm_client import extract_json_object

WITNESS_REQUEST_SCHEMA = "subtitle-span-acoustic-witness-request.v1"
ADJUDICATION_SCHEMA = "acoustic-witness-adjudication.v1"
INAUDIBLE_DECISION_CONTRACT = "inaudible-current-proposed-drop.v1"
WITNESS_CONFLICT_UNSUPPORTED_PROPOSED = (
    "WITNESS_CONFLICT_UNSUPPORTED_PROPOSED_KEPT_CURRENT"
)

# Retained as a diagnostic threshold; CPA, not the witness, owns the decision.
MIN_CHOICE_COMPATIBILITY = 0.55

# 维护者 工程优化②授权：judge 供应商瞬断自动重试，不再整轮报废
# （真善美 zsm4 三模型均短暂 400 事故）。同一 judge_word_choice 调用内，一次
# provider-shaped 失败（HTTP 4xx/5xx/超时/连接类）允许一次同轮重试；语义性
# 失败（JSON 解析、非法 choice）不重试——那是模型已应答，不是供应商抖动。
JUDGE_MAX_PROVIDER_RETRIES = 1
_JUDGE_CALL_PROVIDER_MARKERS = (
    "HTTPERROR",
    "TIMEOUT",
    "TIMED OUT",
    "CONNECTION",
    "SUBPROCESS",
    "RESET",
    " 400",
    " 401",
    " 403",
    " 404",
    " 408",
    " 409",
    " 425",
    " 429",
    " 500",
    " 502",
    " 503",
    " 504",
)


def _judge_call_provider_transient(message: str) -> bool:
    upper = message.upper()
    return any(marker in upper for marker in _JUDGE_CALL_PROVIDER_MARKERS)


def _judge_error_cascade(
    messages: list[str], *, cap_per_entry: int = 300, max_entries: int = 32
) -> list[str]:
    """Preserve each attempt's own error instead of one doubly-truncated tail.

    ``llm_via_cpa.sh`` fans a single judge call out across up to three CPA
    models before failing; the underlying exception text is multi-line (one
    line per model/attempt — a full exhaustion is ~9 curl errors + 3
    "trying next model" + 1 "failed on all models" ≈ 13 lines).  Flattening
    that into a single ``str[:300]`` (as the old ``error`` field did on top
    of ``_call_command``'s own ``stderr[-400:]`` cut) threw away exactly the
    early-model evidence a human needs to see which providers were down.
    With the same-run retry above, an exhausted retry concatenates two such
    cascades (~26 lines); ``max_entries`` must clear that or the retry's own
    error history gets truncated the same way item 3 was fixing one level
    down. This keeps one capped entry per line/attempt instead.
    """

    cascade: list[str] = []
    for message in messages:
        for line in message.splitlines():
            line = line.strip()
            if not line:
                continue
            cascade.append(line[:cap_per_entry])
    return cascade[:max_entries]


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


def valid_inaudible_drop_authority(
    adjudication: Mapping[str, Any],
) -> bool:
    """Recompute the typed CPA DROP chain used by correction/exact-final."""

    request = adjudication.get("request")
    witness = adjudication.get("verdict")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    authority = adjudication.get("drop_authority")
    mutation = adjudication.get("mutation_authority")
    if not all(
        isinstance(value, Mapping)
        for value in (
            request,
            witness,
            witness_judge,
            judge,
            authority,
            mutation,
        )
    ):
        return False

    def _digest(value: object) -> str:
        return str(value or "").removeprefix("sha256:")

    request_sha256 = _digest(request.get("request_sha256"))
    judge_request_sha256 = _digest(judge.get("check_request_sha256"))
    witness_request_sha256 = _digest(witness.get("request_sha256"))
    judge_prompt_sha256 = _digest(judge.get("prompt_sha256"))
    judge_completion_sha256 = _digest(judge.get("completion_sha256"))
    digests = (
        request_sha256,
        judge_request_sha256,
        witness_request_sha256,
        judge_prompt_sha256,
        judge_completion_sha256,
    )
    return bool(
        adjudication.get("schema_version") == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("repaired") is True
        and adjudication.get("policy_branch")
        == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("witness_authority") == "EVIDENCE_ONLY"
        and adjudication.get("timing_immutable") is True
        and request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and request.get("repair_class") == "acoustic_drop_cue"
        and request.get("proposed_cue") == ""
        and isinstance(request.get("current_cue"), str)
        and bool(request.get("current_cue"))
        and witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
        and witness_judge.get("decision_authority") == "CPA_JUDGE"
        and witness_judge.get("witness_authority") == "EVIDENCE_ONLY"
        and witness_judge.get("selected_action") == "DROP_CUE"
        and witness_judge.get("selected_repair_class")
        == "acoustic_drop_cue"
        and witness_judge.get("selected_target_cue") == ""
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "DROP"
        and judge.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and set(judge.get("choice_set") or [])
        == {"CURRENT", "PROPOSED", "DROP"}
        and authority.get("schema_version")
        == "subtitle-cpa-inaudible-drop-authority.v1"
        and authority.get("status") == "PASS"
        and authority.get("decision_authority") == "CPA_JUDGE"
        and authority.get("choice") == "DROP"
        and authority.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and authority.get("target_audible") is False
        and authority.get("timing_immutable") is True
        and _digest(authority.get("original_request_sha256"))
        == judge_request_sha256
        and _digest(authority.get("effective_drop_request_sha256"))
        == request_sha256
        and _digest(authority.get("original_witness_request_sha256"))
        == witness_request_sha256
        and _digest(authority.get("judge_prompt_sha256"))
        == judge_prompt_sha256
        and _digest(authority.get("judge_completion_sha256"))
        == judge_completion_sha256
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and mutation.get("basis") == "CPA_EXPLICIT_INAUDIBLE_DROP"
        and all(
            len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest.lower())
            for digest in digests
        )
    )


def valid_inaudible_drop_repair(repair: Mapping[str, Any]) -> bool:
    """Validate the compact exact/correction owner receipt for a DROP."""

    witness = repair.get("acoustic_witness")
    judge = repair.get("judge")
    authority = repair.get("drop_authority")
    mutation = repair.get("mutation_authority")
    if not all(
        isinstance(value, Mapping)
        for value in (witness, judge, authority, mutation)
    ):
        return False

    def _digest(value: object) -> str:
        return str(value or "").removeprefix("sha256:")

    request_sha256 = _digest(repair.get("request_sha256"))
    witness_request_sha256 = _digest(witness.get("request_sha256"))
    judge_request_sha256 = _digest(judge.get("check_request_sha256"))
    judge_prompt_sha256 = _digest(judge.get("prompt_sha256"))
    judge_completion_sha256 = _digest(judge.get("completion_sha256"))
    after = repair.get("after")
    return bool(
        repair.get("action") == "DROP_CUE"
        and repair.get("repair_class") == "acoustic_drop_cue"
        and repair.get("policy_branch")
        == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
        and repair.get("decision_authority") == "CPA_JUDGE"
        and repair.get("timing_immutable") is True
        and (after == "" or after == [""])
        and witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "DROP"
        and judge.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and set(judge.get("choice_set") or [])
        == {"CURRENT", "PROPOSED", "DROP"}
        and authority.get("schema_version")
        == "subtitle-cpa-inaudible-drop-authority.v1"
        and authority.get("status") == "PASS"
        and authority.get("decision_authority") == "CPA_JUDGE"
        and authority.get("choice") == "DROP"
        and authority.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and authority.get("target_audible") is False
        and authority.get("timing_immutable") is True
        and _digest(authority.get("original_request_sha256"))
        == judge_request_sha256
        and _digest(authority.get("effective_drop_request_sha256"))
        == request_sha256
        and _digest(authority.get("original_witness_request_sha256"))
        == witness_request_sha256
        and _digest(authority.get("judge_prompt_sha256"))
        == judge_prompt_sha256
        and _digest(authority.get("judge_completion_sha256"))
        == judge_completion_sha256
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and mutation.get("basis") == "CPA_EXPLICIT_INAUDIBLE_DROP"
        and all(
            len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest.lower())
            for digest in (
                request_sha256,
                witness_request_sha256,
                judge_request_sha256,
                judge_prompt_sha256,
                judge_completion_sha256,
            )
        )
    )


def valid_inaudible_witness_override(
    *,
    check_request: Mapping[str, Any],
    witness: Mapping[str, Any],
    witness_judge: Mapping[str, Any],
) -> bool:
    """Validate CPA's explicit non-empty override of an inaudible witness."""

    judge = witness_judge.get("judge")
    override = witness_judge.get("inaudible_witness_override")
    if not isinstance(judge, Mapping) or not isinstance(override, Mapping):
        return False

    def _digest(value: object) -> str:
        return str(value or "").removeprefix("sha256:")

    request_sha256 = _digest(check_request.get("request_sha256"))
    witness_request_sha256 = _digest(witness.get("request_sha256"))
    judge_prompt_sha256 = _digest(judge.get("prompt_sha256"))
    judge_completion_sha256 = _digest(judge.get("completion_sha256"))
    return bool(
        check_request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and isinstance(check_request.get("proposed_cue"), str)
        and bool(str(check_request.get("proposed_cue") or "").strip())
        and witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and judge.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and set(judge.get("choice_set") or [])
        == {"CURRENT", "PROPOSED", "DROP"}
        and _digest(judge.get("check_request_sha256")) == request_sha256
        and override.get("schema_version")
        == "subtitle-cpa-inaudible-witness-override.v1"
        and override.get("status") == "PASS"
        and override.get("decision_authority") == "CPA_JUDGE"
        and override.get("choice") == "PROPOSED"
        and override.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and override.get("target_audible") is False
        and _digest(override.get("check_request_sha256"))
        == request_sha256
        and _digest(override.get("witness_request_sha256"))
        == witness_request_sha256
        and _digest(override.get("judge_prompt_sha256"))
        == judge_prompt_sha256
        and _digest(override.get("judge_completion_sha256"))
        == judge_completion_sha256
        and all(
            len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest.lower())
            for digest in (
                request_sha256,
                witness_request_sha256,
                judge_prompt_sha256,
                judge_completion_sha256,
            )
        )
    )


def valid_inaudible_override_repair(repair: Mapping[str, Any]) -> bool:
    """Validate a compact exact/correction receipt for CPA's non-empty override."""

    witness = repair.get("acoustic_witness")
    judge = repair.get("judge")
    override = repair.get("inaudible_witness_override")
    mutation = repair.get("mutation_authority")
    if not all(
        isinstance(value, Mapping)
        for value in (witness, judge, override, mutation)
    ):
        return False

    def _digest(value: object) -> str:
        return str(value or "").removeprefix("sha256:")

    request_sha256 = _digest(repair.get("request_sha256"))
    witness_request_sha256 = _digest(witness.get("request_sha256"))
    judge_prompt_sha256 = _digest(judge.get("prompt_sha256"))
    judge_completion_sha256 = _digest(judge.get("completion_sha256"))
    after = repair.get("after")
    after_text = (
        after[0]
        if isinstance(after, list) and len(after) == 1
        else after
    )
    return bool(
        repair.get("action") == "REPLACE_CUE_TEXT"
        and isinstance(after_text, str)
        and bool(after_text.strip())
        and repair.get("policy_branch")
        == "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
        and repair.get("decision_authority") == "CPA_JUDGE"
        and repair.get("timing_immutable") is True
        and witness.get("schema_version")
        == "subtitle-span-acoustic-witness.v1"
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and judge.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and set(judge.get("choice_set") or [])
        == {"CURRENT", "PROPOSED", "DROP"}
        and _digest(judge.get("check_request_sha256")) == request_sha256
        and override.get("schema_version")
        == "subtitle-cpa-inaudible-witness-override.v1"
        and override.get("status") == "PASS"
        and override.get("decision_authority") == "CPA_JUDGE"
        and override.get("choice") == "PROPOSED"
        and override.get("decision_contract") == INAUDIBLE_DECISION_CONTRACT
        and override.get("target_audible") is False
        and _digest(override.get("check_request_sha256"))
        == request_sha256
        and _digest(override.get("witness_request_sha256"))
        == witness_request_sha256
        and _digest(override.get("judge_prompt_sha256"))
        == judge_prompt_sha256
        and _digest(override.get("judge_completion_sha256"))
        == judge_completion_sha256
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and mutation.get("basis")
        == "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
        and all(
            len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest.lower())
            for digest in (
                request_sha256,
                witness_request_sha256,
                judge_prompt_sha256,
                judge_completion_sha256,
            )
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
        "witness_protocol": BLIND_PINYIN_PROTOCOL,
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
    syllable_hint = neutral_syllable_count_hint(
        str(check_request.get("current_cue") or ""),
        str(check_request.get("proposed_cue") or ""),
    )
    if syllable_hint is not None:
        request["syllable_count_hint"] = syllable_hint
    request["request_sha256"] = hashlib.sha256(
        json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return request


def valid_witness_evidence(
    witness: Mapping[str, Any], *, request_sha256: str
) -> bool:
    """Accept bound AGY evidence or a bound disclosure that AGY was unavailable."""

    status = witness.get("status")
    return bool(
        witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness.get("request_sha256") == request_sha256
        and supported_witness_protocol(witness)
        and status in {"OBSERVED", "UNCERTAIN"}
        and (status == "UNCERTAIN" or isinstance(witness.get("target_audible"), bool))
        and not any(
            key in witness
            for key in (
                "candidate_id",
                "canonical_entity",
                "proposed_cue",
                "rewritten_text",
                "current_fit",
                "proposed_fit",
            )
        )
    )


# 维护者「贴音优先、证据兜底」裁定（卡1结案，synthesis 文档）。
_JUDGE_PROMPT = """# 字幕选字裁决（闭集）

你是字幕修复的最终选字法官。一名听写证人已经把目标区间的音节按拼音记录如下；
证人从未见过任何候选文本。你的任务：结合语篇推理，从闭集中选出最符合
「拼音证据 + 语境」的候选。铁律：

1. {choice_rule}
2. 依据 维护者 2026-08-08 裁定，裁决必须优先在与证人听写拼音相容的候选内选择；PROPOSED 与听写明显不相容且无独立结构化证据时选择 CURRENT。
3. 拼音证据是高可信辅助，不单独拥有最终裁决权。先判断这串拼音是否真的覆盖目标
   整句；若它明显只听到邻句、半句或错位片段，必须在理由中披露错位，并由你
   结合完整语境在闭集内定夺，不能因为 AGY 与两个候选都不齐就机械选 NEITHER。
4. 语境（前后句、弹幕、平行句）在拼音无法区分候选、听写证据被标记为
   受污染/不可用、或拼音与两个候选都显示窗口错位时，可以在闭集内定夺。
   只有两个候选本身都不完整/不通顺，确实需要第三个候选时才选 NEITHER。
5. 目标区间外出现过相同词语，本身不证明目标区间内说了它；但相邻句对同一
   词的重复、呼应或应答，可作为闭集内选择的佐证——仍绝不引入闭集外新字。
6. 真实的中英/中日混杂是存在的（维护者 2026-07-26）：外语候选若在语境中
   **语义通顺**，应正常参与裁决、可以当选；只有当外语读法在语境里根本
   不通顺、而拼音证据又与中文候选相容时，才判定为中文被拉丁化误转写、
   选择中文候选。分辨的根本理由是语义，不是文字系统。
7. 「绑定文字证据」只证明候选的规范写法，不单独证明目标区间说了它。若
   拼音/语篇确认目标指向该实体或原文，必须采用其规范写法；AGY、ASR、
   glossary、roster、弹幕、OCR 都只是证据，最终闭集选择仍由你作出。
8. 证人状态为 UNCERTAIN 时表示 AGY 本轮没有提供可用听音；这不剥夺你的
   最终裁决权。必须忽略缺失的拼音、仅根据闭集、完整语境和绑定文字证据
   排序 CURRENT / PROPOSED / NEITHER，不得因为 AGY 不可用而拒绝裁决。

## 听写证人报告（未见候选）
- 证人状态: {witness_status}
- 不可用原因: {witness_unavailable_reason}
- 目标区间可闻人声: {target_audible}
- 疑似拼音: {heard_pinyin}
- 音节数: {syllable_count}
- 不确定位置: {uncertain_positions}
- 证人置信: {confidence}

## 代码计算的双候选拼音贴合（由盲听 heard_pinyin 得出）
- CURRENT: {current_pinyin_similarity}
- PROPOSED: {proposed_pinyin_similarity}

## 闭集候选
- CURRENT（现字幕整句）: {current_cue}
- PROPOSED（提案整句）: {proposed_cue}
{drop_candidate}
（差异点：suspect={suspect!r} → replacement={replacement!r}；repair_class={repair_class}）

## 语境（转写自同一音频；是语境不是文本权威）
前文:
{context_before}
目标句: <待裁决>
后文:
{context_after}

## 绑定文字证据（证据，不是先行裁决）
{text_evidence}

## 三路结构化保真证据（均为候选证据，不单独授权改字）
{closed_set_structured_evidence}

{structured_chat_block}
按概率排序并**必须选概率最高者**（维护者 2026-07-27：不许拿不准就保持原样——
原样可能是最差的；把 {ranking_description} 的概率
全部写出来，选最高）。
只回一个 JSON 对象（无 markdown 围栏、无其他文字）:
{{"ranking": [{{"choice": {choice_json}, "p": 0.0到1.0}}, ...全部候选],
 "choice": "排序第一的那个", "reason": "引用拼音/语境证据的一句话理由"}}
"""


_JUDGE_CACHE_SCHEMA = "judge-verdict-cache.v1"


def _judge_cache_path(prompt_sha256: str) -> Path | None:
    """Content-addressed CPA-judge cache entry, or None when disabled.

    维护者 自修复成本令：重试/边界自修复轮对**同一个问题**（同
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

    similarities = (
        candidate_pinyin_similarities(
            current_text=str(check_request.get("current_cue") or ""),
            proposed_text=str(check_request.get("proposed_cue") or ""),
            heard_pinyin=str(witness.get("heard_pinyin") or ""),
            uncertain_positions=list(witness.get("uncertain_positions") or []),
        )
        if witness_protocol(witness) == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is True
        else {"current": None, "proposed": None}
    )
    inaudible_three_way = bool(
        witness.get("status") == "OBSERVED"
        and witness.get("target_audible") is False
    )
    allowed_choices = (
        {"CURRENT", "PROPOSED", "DROP"}
        if inaudible_three_way
        else {"CURRENT", "PROPOSED", "NEITHER"}
    )
    decision_contract = (
        INAUDIBLE_DECISION_CONTRACT
        if inaudible_three_way
        else "current-proposed-neither.v1"
    )
    chat_block = (
        f"## 结构化弹幕/SC（平台记录）\n{structured_chat_context}\n\n"
        if structured_chat_context.strip()
        else ""
    )
    prompt = _JUDGE_PROMPT.format(
        choice_rule=(
            "证人明确报告目标区间无可闻人声；你必须在 CURRENT、PROPOSED、"
            "DROP 三项中显式选择。DROP 表示删除整个 cue，只有选择 DROP 才"
            "授权整 cue 置空。PROPOSED 仍表示非空文字提案；若你在已看到"
            "不可听证据后仍选择它，这会作为你对 AGY 辅助证据的显式覆盖被"
            "hash-bound 记录，但 CPA 仍保有最终裁决权。"
            "此三选一中 NEITHER 不是合法答案，也绝不生成新文本。"
            if inaudible_three_way
            else (
                "只能选择 CURRENT、PROPOSED 或 NEITHER。NEITHER 表示听写拼音"
                "与两个候选都明显不符；它会把问题退回提案层重建闭集，不会"
                "自动保留 CURRENT，也不授权任何文本修改。绝不生成新文本。"
            )
        ),
        witness_status=witness.get("status"),
        witness_unavailable_reason=json.dumps(
            {
                "reason_code": witness.get("reason_code"),
                "detail": witness.get("detail"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        target_audible=witness.get("target_audible"),
        heard_pinyin=str(witness.get("heard_pinyin") or ""),
        syllable_count=witness.get("syllable_count"),
        uncertain_positions=witness.get("uncertain_positions"),
        confidence=witness.get("confidence"),
        current_pinyin_similarity=similarities["current"],
        proposed_pinyin_similarity=similarities["proposed"],
        current_cue=str(check_request.get("current_cue") or ""),
        proposed_cue=(
            str(check_request.get("proposed_cue") or "")
            or "（无非空文字提案；不得把 PROPOSED 当作 DROP）"
        ),
        drop_candidate=(
            "- DROP（整条 cue 不输出字幕）: <DROP_CUE: EMPTY SUBTITLE>"
            if inaudible_three_way
            else ""
        ),
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
        closed_set_structured_evidence=json.dumps(
            check_request.get("closed_set_structured_evidence") or {},
            ensure_ascii=False,
            sort_keys=True,
        ),
        structured_chat_block=chat_block,
        ranking_description=(
            "CURRENT、PROPOSED 和“整 cue 无字幕”DROP"
            if inaudible_three_way
            else "CURRENT、PROPOSED 和“两者均非原话”NEITHER"
        ),
        choice_json=(
            '"CURRENT"或"PROPOSED"或"DROP"'
            if inaudible_three_way
            else '"CURRENT"或"PROPOSED"或"NEITHER"'
        ),
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
    call_errors: list[str] = []
    completion: str | None = None
    for call_attempt in range(JUDGE_MAX_PROVIDER_RETRIES + 1):
        try:
            completion = llm_call(prompt)
            break
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            call_errors.append(message)
            if (
                call_attempt < JUDGE_MAX_PROVIDER_RETRIES
                and _judge_call_provider_transient(message)
            ):
                continue
            return {
                "schema_version": ADJUDICATION_SCHEMA,
                "status": "JUDGE_UNAVAILABLE",
                "choice": "UNCERTAIN",
                "reason_code": "JUDGE_CALL_FAILED",
                "error": message[:300],
                "error_cascade": _judge_error_cascade(call_errors),
                "provider_retry_attempted": call_attempt > 0,
                "prompt_sha256": prompt_sha256,
                "decision_contract": decision_contract,
                "choice_set": sorted(allowed_choices),
            }
    try:
        payload = extract_json_object(completion or "")
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return {
            "schema_version": ADJUDICATION_SCHEMA,
            "status": "JUDGE_UNAVAILABLE",
            "choice": "UNCERTAIN",
            "reason_code": "JUDGE_CALL_FAILED",
            "error": message[:300],
            "error_cascade": _judge_error_cascade(call_errors + [message]),
            "provider_retry_attempted": len(call_errors) > 0,
            "prompt_sha256": prompt_sha256,
            "decision_contract": decision_contract,
            "choice_set": sorted(allowed_choices),
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
            if row_choice in allowed_choices and isinstance(
                probability, (int, float)
            ) and not isinstance(probability, bool) and 0.0 <= float(
                probability
            ) <= 1.0:
                ranking.append({"choice": row_choice, "p": float(probability)})
    # 维护者：必须按概率排序选最高——排序有效时它就是裁决；
    # UNCERTAIN 不再是合法终点（模型仍拒绝时以排序第一顶上）。
    if ranking:
        top = max(ranking, key=lambda row: row["p"])
        if choice not in allowed_choices or (
            choice != top["choice"]
        ):
            choice = str(top["choice"])
    if choice not in allowed_choices:
        # No usable ranking and an out-of-set/uncertain answer: refusal.
        return {
            "schema_version": ADJUDICATION_SCHEMA,
            "status": "JUDGE_OUT_OF_SET",
            "choice": "UNCERTAIN",
            "reason_code": "JUDGE_CHOICE_OUT_OF_SET",
            "raw_choice": choice[:80],
            "prompt_sha256": prompt_sha256,
            "decision_contract": decision_contract,
            "choice_set": sorted(allowed_choices),
            "provider_retry_attempted": bool(call_errors),
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
        "decision_contract": decision_contract,
        "choice_set": sorted(allowed_choices),
        "check_request_sha256": str(
            check_request.get("request_sha256") or ""
        ).removeprefix("sha256:"),
        # 维护者 工程优化②授权：MAX_PROVIDER_RETRIES_PER_PASS 同款
        # house pattern（source_fact_review.py）——"每次重试都进回执披露"；
        # 一个二次尝试才拿到的 JUDGED 终态不得看起来和首次成功一模一样，
        # 尤其它还会被写入 judge-verdict-cache 原样回放。
        "provider_retry_attempted": bool(call_errors),
        "candidate_pinyin_similarity": similarities,
        "closed_set_structured_evidence": check_request.get(
            "closed_set_structured_evidence"
        ),
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
    clip_context: Mapping[str, object] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Fuse optional AGY evidence with CPA-owned closed-set word choice."""

    audit: dict[str, Any] = {
        "schema_version": ADJUDICATION_SCHEMA,
        "witness_request_sha256": witness.get("request_sha256"),
        "witness_status": witness.get("status"),
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "witness_protocol": witness_protocol(witness),
        "closed_set_structured_evidence": check_request.get(
            "closed_set_structured_evidence"
        ),
    }
    witness_status = witness.get("status") if isinstance(witness, Mapping) else None
    witness_valid = (
        isinstance(witness, Mapping)
        and witness.get("schema_version") == "subtitle-span-acoustic-witness.v1"
        and witness_status in {"OBSERVED", "UNCERTAIN"}
        and supported_witness_protocol(witness)
        and (
            witness_status == "UNCERTAIN"
            or isinstance(witness.get("target_audible"), bool)
        )
    )
    if not witness_valid:
        return False, "WITNESS_UNAVAILABLE_KEEP_CURRENT", audit
    if (
        witness_status == "OBSERVED"
        and witness_protocol(witness) == LEGACY_SIGHTED_PROTOCOL
    ):
        return False, "LEGACY_SIGHTED_WITNESS_NOT_REUSABLE", audit
    recurrence_gate = session_recurrence_acoustic_gate(check_request, witness)
    if recurrence_gate is not None:
        audit["session_transcript_recurrence_acoustic_gate"] = recurrence_gate
        if recurrence_gate["status"] != "PASS":
            return False, str(recurrence_gate["reason_code"]), audit
    if llm_call is None:
        return False, "JUDGE_UNAVAILABLE_KEEP_CURRENT", audit

    verdict = judge_word_choice(
        llm_call=llm_call,
        check_request=check_request,
        witness=witness,
        structured_chat_context=structured_chat_context,
    )
    audit["judge"] = verdict
    if witness_status == "UNCERTAIN":
        witness_unavailable_reason = str(
            witness.get("reason_code") or witness.get("detail") or "UNKNOWN"
        )
        audit["witness_unavailable_reason"] = witness_unavailable_reason
        if verdict.get("choice") == "NEITHER":
            return False, "JUDGE_REJECTS_CLOSED_SET", audit
        if verdict.get("choice") != "PROPOSED":
            branch = (
                "JUDGE_KEEPS_CURRENT"
                if verdict.get("choice") == "CURRENT"
                else "JUDGE_UNCERTAIN_KEEP_CURRENT"
            )
            return False, branch, audit
        # F21 张力封口（维护者 立项时点名，默认关死待复裁）：
        # 无声学改字路 ``CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS`` 是
        # 7/25 起就存在的既有出口，语义是「provider 真的被调用过、真的失败了，
        # 语境证据仍可定夺」。F21 新开的 ``AUDIO_VERIFIER_UNAVAILABLE`` 是另一
        # 回事：证人链**从未听过**这段音频（host 门降级 / 根本没有 provider）。
        # 让这类证词继承既有改字权，等于让「接线缺陷」自动升级成「声学豁免」，
        # 与 8/8 F7 方向直接冲突。默认只许 KEEP_CURRENT + 披露，等 维护者 复裁。
        if witness_unavailable_reason == AUDIO_VERIFIER_UNAVAILABLE:
            audit["acoustic_witness_never_attempted"] = True
            return (
                False,
                "WITNESS_NEVER_ATTEMPTED_KEEP_CURRENT_DISCLOSED",
                audit,
            )
        return True, "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS", audit
    if not witness["target_audible"]:
        choice = verdict.get("choice")
        if choice == "DROP":
            audit.update(
                selected_action="DROP_CUE",
                selected_repair_class="acoustic_drop_cue",
                selected_target_cue="",
            )
            return True, "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE", audit
        if choice == "CURRENT":
            return False, "JUDGE_KEEPS_CURRENT", audit
        if choice == "PROPOSED":
            proposed = str(check_request.get("proposed_cue") or "")
            if not proposed:
                return (
                    False,
                    "INAUDIBLE_EMPTY_PROPOSED_REQUIRES_EXPLICIT_DROP",
                    audit,
                )
            audit["inaudible_witness_override"] = {
                "schema_version": (
                    "subtitle-cpa-inaudible-witness-override.v1"
                ),
                "status": "PASS",
                "decision_authority": "CPA_JUDGE",
                "choice": "PROPOSED",
                "decision_contract": INAUDIBLE_DECISION_CONTRACT,
                "target_audible": False,
                "check_request_sha256": "sha256:"
                + str(
                    verdict.get("check_request_sha256") or ""
                ).removeprefix("sha256:"),
                "witness_request_sha256": "sha256:"
                + str(
                    witness.get("request_sha256") or ""
                ).removeprefix("sha256:"),
                "judge_prompt_sha256": "sha256:"
                + str(
                    verdict.get("prompt_sha256") or ""
                ).removeprefix("sha256:"),
                "judge_completion_sha256": "sha256:"
                + str(
                    verdict.get("completion_sha256") or ""
                ).removeprefix("sha256:"),
            }
            return True, "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS", audit
        # Under the inaudible three-way contract NEITHER and every other token
        # are out of set.  They cannot trigger a text rebuild or an implicit
        # keep-current decision.
        return False, "JUDGE_UNCERTAIN_KEEP_CURRENT", audit
    heard = str(witness.get("heard_pinyin") or "")
    uncertain = list(witness.get("uncertain_positions") or [])
    similarities = candidate_pinyin_similarities(
        current_text=str(check_request.get("current_cue") or ""),
        proposed_text=str(check_request.get("proposed_cue") or ""),
        heard_pinyin=heard,
        uncertain_positions=uncertain,
    )
    compat_current = similarities["current"]
    compat_proposed = similarities["proposed"]
    audit["candidate_pinyin_similarity"] = similarities
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
        # 维护者 卡1结案：贴音优先；背离耳朵必须有第三方结构化证据。
        suspect = str(check_request.get("suspect") or "")
        replacement = str(check_request.get("replacement") or "")
        support = {
            "orthography_ambiguous": orthography_ambiguous(
                current_cue=str(check_request.get("current_cue") or ""),
                proposed_cue=str(check_request.get("proposed_cue") or ""),
                suspect=suspect,
                replacement=replacement,
            ),
            "registered_direction": registered_misheard_direction(
                suspect=suspect, replacement=replacement
            ),
            "structured_text_support": structured_text_support(
                candidate_provenance=check_request.get("candidate_provenance"),
                replacement=replacement,
                proposed_cue=str(check_request.get("proposed_cue") or ""),
                clip_context=clip_context,
            ),
            "exact_source_transcript_handoff": (
                isinstance(check_request.get("exact_source_transcript_handoff"), Mapping)
                and valid_exact_source_transcript_handoff(
                    check_request["exact_source_transcript_handoff"],
                    check_request=check_request,
                    witness=witness,
                    clip_context=clip_context,
                )
            ),
        }
        supported = any(support.values())
        audit["witness_conflict_gate"] = {
            "schema_version": "witness-conflict-proposed-support.v1",
            "status": "PASS" if supported else "BLOCK",
            **support,
            "reason_code": None if supported else WITNESS_CONFLICT_UNSUPPORTED_PROPOSED,
        }
        if not supported:
            return False, WITNESS_CONFLICT_UNSUPPORTED_PROPOSED, audit
        return True, "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT", audit
    return True, "WITNESS_JUDGE_APPLY_PROPOSED", audit
