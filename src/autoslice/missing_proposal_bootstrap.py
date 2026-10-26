"""CPA-only proposal bootstrap for exact-final findings without candidates.

The final reviewer may correctly identify a bounded nonword while declining to
guess its replacement.  This module creates a bounded context proposal;
it never hears audio and never authorizes a subtitle mutation.  The caller must
still route the proposal through candidate-blind AGY evidence and a separate CPA
CURRENT/PROPOSED judgment.
"""

from __future__ import annotations

from src.autoslice.final_review_agenda import reviewer_reason
from src.autoslice import missing_convergence_progress as convergence_progress

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from dataclasses import dataclass

from src.autoslice.closed_set_evidence import (
    closed_set_structured_evidence,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import extract_json_object
from src.autoslice.structured_output import call_json
from src.autoslice.term_authority import terminology_prompt_context
from src.autoslice.closed_set_proposal_rebuild import (
    TOPIC_PHONEME_POLICY, proposal_clip_context, proposal_phonemes, topic_proposal_error,
)


PROPOSAL_REBUILD_SCHEMA = "subtitle-closed-set-proposal-rebuild.v1"
PROPOSAL_REBUILD_CACHE_SCHEMA = "proposal-rebuild-cache.v1"
CPA_CONVERGENCE_SCHEMA = "subtitle-missing-proposal-cpa-convergence.v1"
CPA_CONVERGENCE_CACHE_SCHEMA = "missing-proposal-cpa-convergence-cache.v1"


@dataclass(frozen=True)
class MissingProposalAnswerContract:
    current: str
    rejected_candidates: tuple = ()
    convergence: bool = False

    @property
    def response_schema(self):
        from .response_schemas import proposal_schema
        return proposal_schema(convergence=self.convergence)

    def schema_projection(self, *_args, **_kwargs):
        """The legacy transport signature projects this already-bound contract."""
        return self.response_schema

    @property
    def prompt_fragment(self):
        from .contract_schema_projection import schema_prompt_fields
        if self.convergence:
            descriptions={'replacement_text':'完整单行 cue；KEEP_EXISTING/UNRESOLVED 时为空',
                          'reason':'一句话说明依据或仍缺的证据'}
        else:
            descriptions={'proposed_cue':'仅 PROPOSED 时填写单行完整候选，其余为空',
                          'topic_summary':'从提供的整片材料归纳的一句话主题',
                          'reason':'证据与整句判断的理由；needs_audio 只表示是否有具体值得补听的问题'}
        fields=schema_prompt_fields(self.response_schema,descriptions=descriptions,required_only=True,
            order=('status','proposed_cue','topic_summary','decision','replacement_text','needs_audio','reason'),
            enum_separator='或',boolean='true或false')
        return '{'+','.join(fields)+'}'

    @property
    def prompt_template(self):
        template = _CONVERGENCE_PROMPT if self.convergence else _PROMPT
        return template.replace('__ANSWER_CONTRACT__', self.prompt_fragment.replace('{', '{{').replace('}', '}}'))

    def valid_text(self, text):
        return (isinstance(text, str) and text == text.strip() and bool(text)
                and text != self.current and text not in self.rejected_candidates
                and '\n' not in text and '\r' not in text and '-->' not in text
                and len(text) <= 96 and not any(ord(c) < 32 for c in text))

    @staticmethod
    def covers_suspect(start, end, original_span, error):
        return error is None and start <= original_span[0] and end >= original_span[1]

    @staticmethod
    def keep_requires_audio(payload):
        return payload.get('needs_audio') is True


def missing_proposal_answer_contract(current, *, rejected_candidates=()):
    return MissingProposalAnswerContract(current, tuple(rejected_candidates))


def proposal_convergence_contract(current):
    return MissingProposalAnswerContract(current, convergence=True)


@dataclass(frozen=True)
class ProposalAudioPromptPayload:
    materials: Mapping

    @property
    def prompt_fragment(self):
        return json.dumps(dict(self.materials), ensure_ascii=False, sort_keys=True)

    def missing_from(self, prompt):
        for slot in ("phonetic_evidence", "original_language_observation"):
            row = self.materials.get(slot)
            if isinstance(row, Mapping) and row.get("status") == "OBSERVED":
                if json.dumps(dict(row), ensure_ascii=False, sort_keys=True) not in prompt:
                    return True
        return False


def proposal_audio_prompt_payload(proposal_audio):
    return ProposalAudioPromptPayload(proposal_audio)


def collect_proposal_audio(
    *, request: Mapping[str, Any], finding: Mapping[str, Any],
    entity_verifier: Callable | None, transcript_provider: Callable | None,
    cached_only: bool = False,
) -> dict[str, Any]:
    """Use ordinary blind witnesses, including their AGY -> Gemini fallback.

    Only geometry reaches either provider. The original-language witness is
    the existing exact-source transcript seam (shared AGY/Gemini transport),
    so foreign speech is not forced through a Mandarin pinyin reconstruction.
    """
    from src.autoslice.acoustic_witness_adjudication import build_witness_request, valid_witness_evidence
    from src.autoslice.exact_source_transcript_contract import (
        build_exact_source_transcript_request, valid_exact_source_transcript_observation,
    )

    blind_request = build_witness_request(request)
    audio: dict[str, Any] = {"mutation_authorized": False, "authority": "EVIDENCE_ONLY"}
    witness: Mapping[str, Any] = {}
    reader = getattr(entity_verifier, "probe_witness_cache", None) if cached_only else entity_verifier
    if callable(reader):
        try:
            result = reader(blind_request)
            if cached_only and (not isinstance(result, Mapping) or result.get("served_from_cache") is not True):
                result = None
            if isinstance(result, Mapping) and valid_witness_evidence(
                result, request_sha256=blind_request["request_sha256"],
            ):
                witness = result
            else:
                audio["pinyin_failure"] = "PROPOSAL_WITNESS_BINDING_INVALID"
        except Exception as exc:
            audio["pinyin_failure"] = type(exc).__name__
    audio["phonetic_evidence"] = proposal_phonemes(str(request.get("current_cue") or ""), witness)
    if (witness.get("status") == "OBSERVED"
            and witness.get("witness_protocol") == "candidate_blind_transcript"
            and witness.get("exact_transcript")):
        audio["original_language_observation"] = dict(witness)
    provenance = finding.get('candidate_provenance') or {}
    from .closed_set_proposal_rebuild import requires_original_language_evidence
    # A reviewer saying "不是日语" is not a foreign-language routing claim.
    # Ordinary language nomination producers already carry typed provenance.
    foreign = (requires_original_language_evidence(str(request.get('current_cue') or ''),
                    str(request.get('proposed_cue') or finding.get('proposed_full_cue') or ''))
        or finding.get('repair_class') in {
        'foreign_source_language_preservation', 'foreign_script_retranscription'}
        or (isinstance(provenance, Mapping) and (
            provenance.get('kind') == 'audio_language_discovery'
            or provenance.get('audible_language') in {'ja', 'en', 'mixed', 'other'})))
    transcript_reader = (getattr(entity_verifier, "probe_exact_source_transcript_cache", None)
                         if cached_only else transcript_provider)
    if ("original_language_observation" not in audio and callable(transcript_reader)
            and (foreign or audio["phonetic_evidence"]["evidence_level"] != "AUDIO_OBSERVED")):
        exact_request = build_exact_source_transcript_request(blind_request)
        try:
            result = transcript_reader(exact_request)
            if cached_only and (not isinstance(result, Mapping) or result.get("served_from_cache") is not True):
                result = None
            if isinstance(result, Mapping) and valid_exact_source_transcript_observation(result, request=exact_request):
                audio["original_language_observation"] = dict(result)
            else:
                audio["transcript_failure"] = str((result or {}).get("reason_code") or "PROPOSAL_TRANSCRIPT_UNAVAILABLE")
        except Exception as exc:
            audio["transcript_failure"] = type(exc).__name__
    return audio


def finding_suspect_span(
    current: str, finding: Mapping[str, Any],
) -> tuple[int, int] | None:
    """Use a checked explicit occurrence, or an unambiguous legacy span.

    A repeated surface is not ambiguous when the reviewer supplied its actual
    codepoint range. Invalid explicit geometry never falls back to a search.
    This binds a doubt, not a replacement or mutation authority.
    """
    suspect = str(finding.get("suspect") or "")
    start = finding.get("span_start_codepoint")
    end = finding.get("span_end_codepoint")
    if start is not None or end is not None:
        if (
            type(start) is int and type(end) is int
            and 0 <= start < end <= len(current)
            and suspect and current[start:end] == suspect
        ):
            return start, end
        return None
    if suspect and current.count(suspect) == 1:
        start = current.index(suspect)
        return start, start + len(suspect)
    return None


_PROMPT = """# 字幕缺失候选重建（只提案，不裁决）

独立成品审片员确认目标 cue 中有一个可疑片段，但为了避免瞎猜，没有给出
替换候选。你现在是 CPA 的文字提案层：只能根据整片转写语境和绑定的结构化
聊天及已有的绑定补听观察，提出一个有界的完整 cue 候选；你没有直接听过音频，也没有改字权限。

约束：
1. 只改目标 cue，输出单行完整字幕；不得改时间轴、合并或拆分 cue。
2. 修改必须覆盖审片员标出的 SUSPECT，且尽量最小；不得润色或概括。
3. 保留口语、专名、数字和中英日混说。可辨认的外语普通短语使用惯用原语言字形：
   日语用假名或惯用日文汉字，英语保留英文；不得改写为中文谐音或翻译。
   现稿全汉字不证明它是中文或昵称，不能为异常尾串编造用户名。
   证据支持完整现稿、或不足以提出不同于现稿的候选时，返回 CURRENT_SUPPORTED 及理由。
   CURRENT_SUPPORTED 不改字，只把 CURRENT 送入下一轮闭集裁决；真正缺证且不能判断才 UNRESOLVED。
4. 候选产生后还必须经过 AGY 无候选盲听，再由另一轮 CPA 在 CURRENT 与
   PROPOSED 中作最终裁决；本轮提案绝不授权修改。

## 当前与可疑片段
- CURRENT: {current}
- SUSPECT: {suspect}
- 审片理由: {reviewer_reason}

## 同片转写语境（不是逐字真值）
前文:
{before}
目标 cue: {current}
后文:
{after}

## 绑定结构化聊天（可能为空，只作语境）
{structured_chat}

只回 JSON（无 markdown、无其他文字）：
__ANSWER_CONTRACT__
"""

_CONVERGENCE_PROMPT = """# 字幕缺失候选最终收敛（CPA 文字候选裁决）

上一轮文字提案没有产生一个满足局部 span 合同的候选。你现在是文字候选裁决者；
此轮根据完整局部语境和已绑定的补听观察，选择有证据支持的动作：

- KEEP_EXISTING：现有完整 cue 在语境中合理，保留原文。
- REPLACE_WITH_EXACT_TEXT：现有 cue 确有语境错误，给出应替换成的单行完整 cue。
- UNRESOLVED：现有证据仍不能确定具体文字，保留疑点，不能为了结束审查而选边。
  若需要真实补听而非更多文字猜测，明确 needs_audio=true；正常声学链将接续取证。

约束：
1. 只裁决目标 cue；不得改时间轴、合并或拆分 cue。
2. replacement_text 必须是完整单行 cue；不得润色、概括或补写没有语境依据的信息。
   可辨认的外语普通短语必须用惯用原语言字形（日语假名/惯用日文汉字、英语英文），
   不得输出中文谐音或翻译；全汉字现稿不能单独证明中文或昵称身份。
3. 结构化聊天只是绑定语境，不是逐字真值；必须结合相邻消息链与 cue 上下文判断。
4. KEEP_EXISTING 时 replacement_text 必须为空；REPLACE_WITH_EXACT_TEXT 时必须与
   CURRENT 不同，并覆盖审片员指出的 SUSPECT。
5. 你的决定将绑定 cue/current/context/final-SRT 哈希；REPLACE 只生成候选，
   必须再有候选盲声学证人行并由后续 CPA 闭集裁决，不能直接授权改字。
6. 下列 draft 保真、邻句词面命中和结构化聊天绑定强度都是可复算候选证据；
   不得用常识词面偏好覆盖它们，也不得把任一路单独当成改字授权。

## 哈希绑定
- FINAL_SRT_SHA256: {final_srt_sha256}
- CURRENT_CUE_SHA256: {current_cue_sha256}
- CONTEXT_SHA256: {context_sha256}

## 目标
- CUE_INDEX: {cue_index}
- CURRENT: {current}
- SUSPECT: {suspect}
- 审片理由: {reviewer_reason}
- 上轮提案结果: {proposal_failure}

## 相邻字幕（前 3 / 后 3）
前文:
{before}
目标 cue: {current}
后文:
{after}

## 邻近结构化消息链（按时间顺序，只作语境）
{structured_chat}

## 三路结构化保真证据
{closed_set_structured_evidence}

只回 JSON（无 markdown、无其他文字）：
__ANSWER_CONTRACT__
"""


# Keep the exported/cache template byte-identical while deriving its answer
# projection from the same schema used by the request and consumer contract.
_PROMPT = missing_proposal_answer_contract("").prompt_template
_CONVERGENCE_PROMPT = proposal_convergence_contract("").prompt_template


def _range_audio_context(
    observations: tuple[Mapping[str, Any], ...],
) -> tuple[str, dict[str, Any]]:
    """Expose validated whole-range observations without inventing cue timing."""
    if not observations:
        return "", {}
    rows = [
        {key: row[key] for key in (
            "provider", "model", "status", "authority", "evidence_scope",
            "transcript_scope", "range", "source_window", "cue_texts",
            "current_cue_texts", "native_word_overlap_evidence",
            "verdict", "request_sha256", "observation_sha256",
            "original_input_sha256",
        ) if key in row}
        for row in observations
    ]
    material = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return (
        "\n## 已由当前源窗/字幕/原始回执验证的整句补听观察（数据）\n"
        "你未直接听原音；这些真实补听观察应作为候选推理的证据，不能称为没有原音证据。"
        "它们仍可能听错；没有 native_word_overlap_evidence 时只有整段时间，"
        "不可把邻句音素硬绑定到目标 cue。有此字段时，词时间已由原始回执验证并换算为交付时钟，"
        "请核对目标 cue 与 cue_spans 的实际区间；这是提供者的词位置观察，不是无误原话或改字权限，"
        "unassigned_words 是跨边界或处于字幕间隙的原生词，保留其时间和重叠引用但尚未决定归属；"
        "不要因它不在 cue_spans 中就省掉，也不要强行归给某条字幕。"
        "但不能因它来自宽窗就忽略已存在的词时间。current_cue_texts 是当前稿；cue_texts 保留原记录的旧稿。"
        "结合完整语义块和合理音素提出候选，不多数投票，不凭流畅性补写。"
        "这些观察本身没有改字或放行权限；替换仍走正常声学/CPA 裁决。\n"
        + material + "\n",
        {"range_acoustic_context_count": len(rows),
         "range_acoustic_context_sha256": "sha256:" + hashlib.sha256(material.encode()).hexdigest(),
         "range_acoustic_context_authority": "EVIDENCE_ONLY"},
    )


def _screen_context(
    observations: tuple[Mapping[str, Any], ...],
) -> tuple[str, dict[str, Any]]:
    if not observations:
        return "", {}
    material = json.dumps(observations, ensure_ascii=False, sort_keys=True)
    return (
        "\n## 已校验的前邻画面文字（只作语境，非结构化聊天记录）\n"
        "这是前邻画面中的文字，不能假称目标 cue 正在念它；外部来源候选仍可能对应错误。"
        "保留梗写与观察分歧，结合真实补听判断她实际说了什么，不能从画面直接改字幕。\n"
        + material + "\n",
        {"screen_context_count": len(observations),
         "screen_context_sha256": "sha256:" + hashlib.sha256(material.encode()).hexdigest(),
         "screen_context_authority": "EVIDENCE_ONLY"},
    )


def _exact_audio_context(
    observations: tuple[Mapping[str, Any], ...],
) -> tuple[str, dict[str, Any]]:
    if not observations:
        return "", {}
    material = json.dumps(observations, ensure_ascii=False, sort_keys=True)
    return (
        "\n## 本轮正常补听返回的目标 cue 原音观察（已验证，只作证据）\n"
        "这些观察是在上一轮缺证据裁决之后取得的，源窗、当前请求和原生回执已验证。"
        "你未直接听原音；精确裁剪不保证模型听写完整或正确，也不能由多数投票选字。"
        "请结合完整现稿、相邻语境和画面观察重新判断，不能继续声称没有这些原音证据。"
        "KEEP 必须支持完整 CURRENT；REPLACE 只是候选，仍须正常声学/CPA 改字合同。"
        "分歧无法解决时保留 UNRESOLVED，不能为了结束审查而猜字。\n"
        + material + "\n",
        {"exact_acoustic_context_count": len(observations),
         "exact_acoustic_context_sha256": "sha256:" + hashlib.sha256(material.encode()).hexdigest(),
         "exact_acoustic_context_authority": "EVIDENCE_ONLY"},
    )


def _cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "proposal-rebuilds"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def _convergence_cache_path(prompt_sha256: str) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    return (
        Path(base)
        / "cache"
        / "missing-proposal-cpa-convergence"
        / prompt_sha256[:2]
        / f"{prompt_sha256}.json"
    )


def _save_payload_cache(
    path: Path | None, schema: str, prompt_sha256: str,
    completion_sha256: str, payload: Mapping[str, Any], *, served_from_cache: bool,
) -> None:
    if path is None or served_from_cache:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema_version": schema, "prompt_sha256": prompt_sha256,
            "completion_sha256": completion_sha256, "payload": dict(payload),
        }, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def converge_missing_proposal(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    clip_context: Mapping[str, object] | None = None,
    source_media_timeline_offset_ms: int = 0,
    proposal_audit: Mapping[str, Any],
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
    range_acoustic_observations: tuple[Mapping[str, Any], ...] = (),
    screen_context_observations: tuple[Mapping[str, Any], ...] = (),
    exact_acoustic_observations: tuple[Mapping[str, Any], ...] = (),
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask a second CPA pass to assess current text or an exact candidate.

    This pass does not ask AGY to choose words and never authorizes mutation.
    A replacement still needs a typed acoustic witness row and the downstream
    CPA closed-set decision. Provider/schema failure remains fail-closed.
    """

    audit: dict[str, Any] = {
        "schema_version": CPA_CONVERGENCE_SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "timing_immutable": True,
        "mutation_authorized": False,
    }
    if llm_call is None:
        audit["reason_code"] = "CPA_CONVERGENCE_LLM_UNAVAILABLE"
        return None, audit
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    if not 1 <= cue_index <= len(cues):
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code="CONTEXT_CUE_INDEX_INVALID")
        return None, audit
    current = cues[cue_index - 1].text
    current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if finding.get("base_text_sha256") not in {None, current_sha256}:
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code="STALE_BASE")
        return None, audit
    suspect = str(finding.get("suspect") or "")
    suspect_occurrence_count = current.count(suspect) if suspect else 0
    if suspect_occurrence_count < 1:
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code="SUSPECT_NOT_FOUND")
        return None, audit
    before_rows = cues[max(0, cue_index - 4) : cue_index - 1]
    after_rows = cues[cue_index : min(len(cues), cue_index + 3)]
    before = "\n".join(row.text for row in before_rows) or "（无）"
    after = "\n".join(row.text for row in after_rows) or "（无）"
    structured = structured_chat or "（无）"
    final_srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    proposed_hint = finding.get("proposed_full_cue")
    structured_evidence = closed_set_structured_evidence(
        srt_text,
        finding,
        proposed_cue=(
            proposed_hint if isinstance(proposed_hint, str) else current
        ),
        clip_context=clip_context,
    )
    context_binding = {
        "cue_index": cue_index,
        "current": current,
        "before": [row.text for row in before_rows],
        "after": [row.text for row in after_rows],
        "structured_chat": structured,
        "closed_set_structured_evidence": structured_evidence,
        "proposal_clip_context": proposal_clip_context(srt_text, cue_index, clip_context),
    }
    if proposal_audit.get("proposal_audio"):
        context_binding["proposal_audio"] = proposal_audit["proposal_audio"]
    phonemes = ((proposal_audit.get("proposal_audio") or {}).get("phonetic_evidence")
                or proposal_audit.get("phonetic_evidence") or proposal_phonemes(current, None))
    context_binding["phonetic_evidence"] = phonemes
    audit["phonetic_evidence"] = phonemes
    audio_context, audio_audit = _range_audio_context(range_acoustic_observations)
    screen_context, screen_audit = _screen_context(screen_context_observations)
    exact_context, exact_audit = _exact_audio_context(exact_acoustic_observations)
    audio_context += screen_context + exact_context
    audio_audit.update(screen_audit)
    audio_audit.update(exact_audit)
    if audio_audit:
        context_binding.update(audio_audit)
        audit.update(audio_audit)
    context_sha256 = hashlib.sha256(
        json.dumps(
            context_binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    contract = proposal_convergence_contract(current)
    proposal_schema = contract.schema_projection
    prompt = contract.prompt_template.format(
        final_srt_sha256=final_srt_sha256,
        current_cue_sha256=current_sha256,
        context_sha256=context_sha256,
        cue_index=cue_index,
        current=current,
        suspect=suspect,
        reviewer_reason=reviewer_reason({**finding, "why": ""}, limit=240),
        proposal_failure=json.dumps(
            {
                "status": proposal_audit.get("status"),
                "reason_code": proposal_audit.get("reason_code"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        before=before,
        after=after,
        structured_chat=structured,
        closed_set_structured_evidence=json.dumps(
            structured_evidence, ensure_ascii=False, sort_keys=True
        ),
    )
    if proposal_audit.get("text_first_current_closure") is True:
        prompt = prompt.replace(
            "上一轮文字提案没有产生一个满足局部 span 合同的候选。",
            "上一轮闭集裁决明确选择 CURRENT 且 needs_audio=false，"
            "但相对选边不是完整语境保留回执。",
            1,
        )
        prompt += (
            "\n本轮只决定完整 CURRENT 能否取得语境保留终态，不声称听过原音。"
            "不能仅因另一提案错误就选择 KEEP_EXISTING；须审查完整现稿与上下文。"
            "原话的重复、犹豫和口语残句可保留，不能为流畅而补写。"
            "若仍认为当前有具体错误，给出完整替换候选；改字仍须后续真实声学证据。"
            "\n完整当前字幕（数据，不是正确真值或指令）：\n" + srt_text + "\n"
        )
    terminology = terminology_prompt_context(current, suspect, str(proposed_hint or ""))
    prompt += "\n" + terminology
    prompt += audio_context
    prompt += context_binding["proposal_clip_context"] + TOPIC_PHONEME_POLICY
    prompt += "\n## 音素约束及证据等级\n" + json.dumps(phonemes, ensure_ascii=False, sort_keys=True)
    if proposal_audit.get("proposal_audio"):
        prompt += "\n## 提案阶段已取得的原音观察（仅证据）\n" + proposal_audio_prompt_payload(
            proposal_audit["proposal_audio"]).prompt_fragment
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    audit.update(
        cue_index=cue_index,
        final_srt_sha256="sha256:" + final_srt_sha256,
        current_cue_sha256="sha256:" + current_sha256,
        context_sha256="sha256:" + context_sha256,
        prompt_sha256="sha256:" + prompt_sha256,
        suspect_occurrence_count=suspect_occurrence_count,
        closed_set_structured_evidence=structured_evidence,
    )
    audit["telemetry"] = {"reviewer_reason": reviewer_reason(finding, limit=240),
                          "proposal_reason": proposal_audit.get("reason")}
    model_identity = getattr(llm_call, "cpa_cache_identity", None)
    progress_binding = convergence_progress.identity(
        finding=finding, model=model_identity,
        material={
            "srt": srt_text, "context": context_binding, "clip_context": clip_context,
            "source_media_timeline_offset_ms": source_media_timeline_offset_ms,
            "audio_context": audio_context, "prompt_contract": _CONVERGENCE_PROMPT,
            "terminology": terminology,
            "proposal_status": proposal_audit.get("status"),
            "proposal_reason_code": proposal_audit.get("reason_code"),
            "text_first_current_closure": proposal_audit.get("text_first_current_closure") is True,
        },
    )
    try:
        prior_unresolved = convergence_progress.read(progress_binding)
    except ValueError as exc:
        audit.update(status="UNAVAILABLE", reason_code=str(exc), mutation_authorized=False)
        return None, audit
    prior_raw = None
    if prior_unresolved is not None:
        prior_unresolved["no_new_evidence"]["prospective_prompt_sha256"] = "sha256:" + prompt_sha256
        prior_raw = prior_unresolved.pop("original_raw_completion", None)
        audit["no_new_evidence"] = prior_unresolved["no_new_evidence"]
    cache_key = convergence_progress.digest({
        "prompt_sha256": prompt_sha256, "model": model_identity,
        "source_media_timeline_offset_ms": source_media_timeline_offset_ms,
        "clip_context": clip_context,
    })
    cache_path = _convergence_cache_path(cache_key)
    if isinstance(model_identity, Mapping) and model_identity.get("models"):
        cache_path = None  # Model-bound raw answers supersede parsed legacy entries.
    payload: Mapping[str, Any] | None = None
    completion: str | None = None
    completion_sha256: str | None = None
    served_from_cache = False
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == CPA_CONVERGENCE_CACHE_SCHEMA
                and cached.get("prompt_sha256") == prompt_sha256
                and isinstance(cached.get("payload"), Mapping)
                and isinstance(cached.get("completion_sha256"), str)
            ):
                payload = cached["payload"]
                completion_sha256 = str(cached["completion_sha256"])
                served_from_cache = True
        except (OSError, ValueError):
            pass
    if payload is None:
        from src.autoslice.semantic_retry_progress import RetryNeedsPlan
        try:
            from .completed_review_checkpoint import ruling_response
            completion, served_from_cache = ((prior_raw, True) if prior_raw is not None else ruling_response(
                stage='missing-proposal-convergence', prompt=prompt, llm_call=llm_call,
                inputs={"material": progress_binding["input"]["material"] if progress_binding else {}},
                parser_path=Path(__file__), response_schema=proposal_schema(convergence=True),
                name='missing_proposal_convergence'))
            completion_sha256 = hashlib.sha256(
                completion.encode("utf-8")
            ).hexdigest()
            payload = extract_json_object(completion)
            from .llm_client import completion_identity
            answered = completion_identity(model_identity, completion)
            if progress_binding is not None and answered != model_identity:
                bound_input = {**progress_binding["input"], "model": answered}
                progress_binding = {"input": bound_input, "sha256": convergence_progress.digest(bound_input)}
        except RetryNeedsPlan as exc:
            audit.update(status='RETRY_BLOCKED',reason_code=str(exc),error_type=type(exc).__name__,
                provider_calls_this_attempt=0,dispatch_outcome='NOT_DISPATCHED')
            return None,audit
        except Exception as exc:
            audit.update(
                reason_code="CPA_CONVERGENCE_PROVIDER_OR_JSON_UNAVAILABLE",
                error_type=type(exc).__name__,
                failure_layer="CONTRACT_ERROR" if completion is not None else "CALL_FAILURE",
            )
            return None, audit
    audit["completion_sha256"] = "sha256:" + str(completion_sha256)
    if served_from_cache:
        audit["served_from_cache"] = True
    decision = str(payload.get("decision") or "")
    replacement_text = payload.get("replacement_text")
    reason = str(payload.get("reason") or "")[:240]
    if decision == "UNRESOLVED" and (replacement_text is None or replacement_text == ""):
        audit.update(
            status="UNRESOLVED", decision=decision, replacement_text="",
            reason=reason, mutation_authorized=False,
            reason_code="CPA_CONVERGENCE_UNRESOLVED",
            needs_audio=payload.get("needs_audio") is True,
        )
        rebuilt = None
    elif (decision == "KEEP_EXISTING" and replacement_text in {"", None}
          and contract.keep_requires_audio(payload)):
        audit.update(
            status="UNRESOLVED", decision="UNRESOLVED", replacement_text="",
            reason=reason, mutation_authorized=False, needs_audio=True,
            reason_code="CPA_CONVERGENCE_KEEP_REQUIRES_AUDIO",
        )
        rebuilt = None
    elif decision == "KEEP_EXISTING" and replacement_text in {"", None}:
        audit.update(
            status="RESOLVED",
            decision=decision,
            replacement_text="",
            reason=reason,
            mutation_authorized=False,
        )
        rebuilt = dict(finding)
        rebuilt["_cpa_missing_proposal_convergence"] = audit
    elif decision == "REPLACE_WITH_EXACT_TEXT":
        if not contract.valid_text(replacement_text):
            audit.update(
                status="INVALID", failure_layer="CONTRACT_ERROR",
                reason_code="CPA_CONVERGENCE_TEXT_CONTRACT_INVALID",
                reason=reason,
            )
            return None, audit
        topic_error = topic_proposal_error(
            payload, current=current, proposed=replacement_text, evidence=phonemes,
            topic_context=context_binding["proposal_clip_context"] + structured_chat,
        )
        if topic_error:
            audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code=topic_error, reason=reason)
            return None, audit
        edit_suspect, replacement, start, end, error = derive_single_span_edit(
            current,
            replacement_text,
            allow_insertion=True,
            allow_deletion=True,
        )
        full_cue_audit_fallback = error is not None
        if full_cue_audit_fallback:
            edit_suspect = current
            replacement = replacement_text
            start = 0
            end = len(current)
        # The complete cue decision is a hash-bound candidate, not mutation
        # authority. A local SequenceMatcher span is only audit metadata here:
        # shared prefixes/repeated suspects must not recreate suggestion=null,
        # while the final reviewer still requires an acoustic witness row.
        audit.update(
            status="RESOLVED",
            decision=decision,
            replacement_text=replacement_text,
            replacement_text_sha256="sha256:"
            + hashlib.sha256(replacement_text.encode("utf-8")).hexdigest(),
            reason=reason,
            mutation_authorized=False,
        )
        if full_cue_audit_fallback:
            audit["full_cue_audit_fallback"] = True
            audit["span_derivation_reason_code"] = error
        rebuilt = dict(finding)
        rebuilt.update(
            suspect=edit_suspect,
            suggestion=replacement,
            span_start_codepoint=start,
            span_end_codepoint=end,
            proposed_full_cue=replacement_text,
            base_text_sha256=current_sha256,
            candidate_provenance={
                "kind": "cpa_context_exact_text_candidate",
                "mutation_authorized": False,
                "prompt_sha256": "sha256:" + prompt_sha256,
                "context_sha256": "sha256:" + context_sha256,
                "final_srt_sha256": "sha256:" + final_srt_sha256,
            },
            candidate_memory_id=None,
            why=(f"[CPA 最终文字裁决] {reason or finding.get('why') or ''}")[
                :240
            ],
            _cpa_missing_proposal_convergence=audit,
        )
        if rebuilt.get("repair_class") == "disclosure_only":
            rebuilt["repair_class"] = "phonetic"
    else:
        audit.update(
            status="INVALID", failure_layer="CONTRACT_ERROR",
            reason_code="CPA_CONVERGENCE_DECISION_CONTRACT_INVALID",
            reason=reason,
        )
        return None, audit
    _save_payload_cache(
        cache_path, CPA_CONVERGENCE_CACHE_SCHEMA, prompt_sha256,
        str(completion_sha256), payload, served_from_cache=served_from_cache,
    )
    try:
        convergence_progress.save(progress_binding, prompt=prompt, completion=completion, audit=audit)
    except OSError as exc:
        # Preserve the real unresolved result and disclose that durable reuse
        # was not achieved; never claim a successful checkpoint.
        audit["progress_checkpoint_error"] = type(exc).__name__
    return rebuilt, audit


def bootstrap_missing_proposal(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
    range_acoustic_observations: tuple[Mapping[str, Any], ...] = (),
    screen_context_observations: tuple[Mapping[str, Any], ...] = (),
    rejected_candidates: tuple[str, ...] = (),
    clip_context: Mapping[str, Any] | None = None,
    proposal_audio: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask CPA for one bounded text candidate without mutation authority."""

    audit: dict[str, Any] = {
        "schema_version": PROPOSAL_REBUILD_SCHEMA,
        "status": "UNAVAILABLE",
        "decision_authority": "CPA_PROPOSAL_ONLY",
        "mutation_authorized": False,
    }
    if llm_call is None:
        audit["reason_code"] = "MISSING_PROPOSAL_LLM_UNAVAILABLE"
        return None, audit
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    try:
        cue_index = int(finding.get("cue_index") or 0)
    except (TypeError, ValueError):
        cue_index = 0
    if not 1 <= cue_index <= len(cues):
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code="CONTEXT_CUE_INDEX_INVALID")
        return None, audit
    current = cues[cue_index - 1].text
    current_sha256 = hashlib.sha256(current.encode("utf-8")).hexdigest()
    if finding.get("base_text_sha256") not in {None, current_sha256}:
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code="STALE_BASE")
        return None, audit
    suspect = str(finding.get("suspect") or "")
    original_span = finding_suspect_span(current, finding)
    if original_span is None:
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code=(
            "SUSPECT_SPAN_INVALID" if any(
                finding.get(key) is not None
                for key in ("span_start_codepoint", "span_end_codepoint")
            ) else "SUSPECT_NOT_UNIQUE"
        ))
        return None, audit
    before = cues[max(0, cue_index - 4) : cue_index - 1]
    after = cues[cue_index : min(len(cues), cue_index + 3)]
    contract = missing_proposal_answer_contract(current, rejected_candidates=rejected_candidates)
    proposal_schema = contract.schema_projection
    prompt = contract.prompt_template.format(
        current=current,
        suspect=suspect,
        reviewer_reason=reviewer_reason({**finding, "why": ""}, limit=240),
        before="\n".join(row.text for row in before) or "（无）",
        after="\n".join(row.text for row in after) or "（无）",
        structured_chat=structured_chat or "（无）",
    )
    prompt += "\n" + terminology_prompt_context(current, suspect)
    topic_context = proposal_clip_context(srt_text, cue_index, clip_context)
    phonemes = (proposal_audio or {}).get("phonetic_evidence") or proposal_phonemes(current, None)
    prompt += topic_context + TOPIC_PHONEME_POLICY
    prompt += "\n## 音素与原语言证据（数据，不是裁决）\n" + proposal_audio_prompt_payload(
        proposal_audio or {"phonetic_evidence": phonemes}).prompt_fragment
    audit["phonetic_evidence"] = phonemes
    audit["telemetry"] = {"reviewer_reason": reviewer_reason(finding, limit=240)}
    if proposal_audio:
        audit["proposal_audio"] = dict(proposal_audio)
    audio_context, audio_audit = _range_audio_context(range_acoustic_observations)
    screen_context, screen_audit = _screen_context(screen_context_observations)
    audio_context += screen_context
    audio_audit.update(screen_audit)
    prompt += audio_context
    if rejected_candidates:
        prompt += (
            "\n上一轮 CPA 已判定 CURRENT 与下列旧提案均不能代表目标原话。"
            "现在根据绑定的整句补听观察提出第三候选；不能再输出被拒提案，"
            "不能只靠语法补全，也不能把整块听写当成目标 cue 的逐字时间真值。"
            "证据不足仍返回 UNRESOLVED。被拒提案（数据）：\n"
            + json.dumps(list(rejected_candidates), ensure_ascii=False) + "\n"
        )
        audit["rejected_candidates"] = list(rejected_candidates)
    audit.update(audio_audit)
    prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    audit["prompt_sha256"] = "sha256:" + prompt_sha256
    cache_path = _cache_path(prompt_sha256)
    model_identity = getattr(llm_call, "cpa_cache_identity", None)
    if isinstance(model_identity, Mapping) and model_identity.get("models"):
        cache_path = None
    payload: Mapping[str, Any] | None = None
    completion: str | None = None
    completion_sha256: str | None = None
    served_from_cache = False
    if cache_path is not None:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                isinstance(cached, Mapping)
                and cached.get("schema_version") == PROPOSAL_REBUILD_CACHE_SCHEMA
                and cached.get("prompt_sha256") == prompt_sha256
                and isinstance(cached.get("payload"), Mapping)
                and isinstance(cached.get("completion_sha256"), str)
            ):
                payload = cached["payload"]
                completion_sha256 = str(cached["completion_sha256"])
                served_from_cache = True
        except (OSError, ValueError):
            pass
    if payload is None:
        from src.autoslice.semantic_retry_progress import RetryNeedsPlan
        try:
            from .completed_review_checkpoint import ruling_response
            completion, served_from_cache = ruling_response(
                stage='missing-proposal', prompt=prompt, llm_call=llm_call,
                inputs={}, parser_path=Path(__file__), response_schema=proposal_schema(),
                name='missing_proposal')
            completion_sha256 = hashlib.sha256(completion.encode("utf-8")).hexdigest()
            payload = extract_json_object(completion)
        except RetryNeedsPlan as exc:
            audit.update(status='RETRY_BLOCKED',reason_code=str(exc),error_type=type(exc).__name__,
                provider_calls_this_attempt=0,dispatch_outcome='NOT_DISPATCHED')
            return None,audit
        except Exception as exc:
            audit.update(
                reason_code="MISSING_PROPOSAL_PROVIDER_OR_JSON_UNAVAILABLE",
                error_type=type(exc).__name__,
                failure_layer="CONTRACT_ERROR" if completion is not None else "CALL_FAILURE",
            )
            return None, audit
    audit["completion_sha256"] = "sha256:" + str(completion_sha256)
    if served_from_cache:
        audit["served_from_cache"] = True
    from src.autoslice.semantic_answer import proposal_status
    status = proposal_status(payload)
    proposed = payload.get("proposed_cue")
    reason = str(payload.get("reason") or "")[:240]
    audit["needs_audio"] = payload.get("needs_audio") is True
    audit["topic_summary"] = str(payload.get("topic_summary") or "")[:300]
    if status == 'CURRENT_SUPPORTED' and not proposed and reason:
        span = finding_suspect_span(current, finding)
        if span is not None:
            _save_payload_cache(cache_path, PROPOSAL_REBUILD_CACHE_SCHEMA, prompt_sha256,
                str(completion_sha256), payload, served_from_cache=served_from_cache)
            audit.update(status='CURRENT_SUPPORTED', reason=reason, mutation_authorized=False,
                next_action='CLOSED_SET_WITH_CURRENT')
            return {**finding, 'suggestion': str(finding['suspect']),
                'proposed_full_cue': current, 'span_start_codepoint': span[0],
                'span_end_codepoint': span[1], 'candidate_blind_observation_scaffolding': True,
                'candidate_memory_id': None,
                'candidate_provenance': {'kind': 'candidate_blind_observation_scaffolding',
                    'basis': 'CURRENT_SUPPORTED_FOR_CLOSED_SET', 'mutation_authorized': False}}, audit
    if status != "PROPOSED":
        audit.update(status="UNRESOLVED", reason=reason)
        if status == "UNRESOLVED" and (proposed is None or proposed == ""):
            _save_payload_cache(
                cache_path, PROPOSAL_REBUILD_CACHE_SCHEMA, prompt_sha256,
                str(completion_sha256), payload, served_from_cache=served_from_cache,
            )
        return None, audit
    if not contract.valid_text(proposed):
        audit.update(
            status="INVALID", failure_layer="CONTRACT_ERROR",
            reason_code="MISSING_PROPOSAL_TEXT_CONTRACT_INVALID",
            reason=reason,
        )
        return None, audit
    topic_error = topic_proposal_error(
        payload, current=current, proposed=proposed, evidence=phonemes,
        topic_context=topic_context + structured_chat,
    )
    if topic_error:
        audit.update(status="INVALID", failure_layer="CONTRACT_ERROR", reason_code=topic_error, reason=reason)
        return None, audit
    edit_suspect, replacement, start, end, error = derive_single_span_edit(
        current,
        proposed,
        allow_insertion=True,
        allow_deletion=True,
    )
    bounded_full_cue_repair = bool(
        error == "EDIT_LENGTH_DELTA_TOO_LARGE"
        and suspect == current
        and len(current) <= 24
        and len(proposed) <= 24
    )
    if bounded_full_cue_repair:
        edit_suspect = current
        replacement = proposed
        start = 0
        end = len(current)
        error = None
    original_start, original_end = original_span
    insertion_inside_suspect = (
        error is None
        and start == end
        and original_start <= start <= original_end
    )
    partial_edit_inside_suspect = (
        error is None
        and original_start <= start < end <= original_end
        and (start > original_start or end < original_end)
    )
    if insertion_inside_suspect or partial_edit_inside_suspect:
        # The reviewer can flag a whole spoken unit even when CPA changes
        # only a word inside it.  A minimal diff excludes unchanged affixes;
        # retain those affixes in the declared suspect replacement instead
        # of rejecting the complete candidate or demanding gratuitous edits.
        # This also covers a CPA insertion at either edge or inside the
        # declared suspect: the empty minimal span is safe only when the
        # expanded suspect reconstruction reproduces the complete proposal.
        replacement_end = original_end + len(proposed) - len(current)
        scoped_replacement = proposed[original_start:replacement_end]
        if (
            current[:original_start] + scoped_replacement + current[original_end:]
            == proposed
        ):
            edit_suspect, replacement = suspect, scoped_replacement
            start, end = original_start, original_end
            audit["minimal_edit_expanded_to_suspect"] = True
    if not contract.covers_suspect(start, end, original_span, error):
        audit.update(
            status="INVALID", failure_layer="CONTRACT_ERROR",
            reason_code=error or "MISSING_PROPOSAL_DOES_NOT_COVER_SUSPECT",
            reason=reason,
        )
        return None, audit
    rebuilt = dict(finding)
    rebuilt.update(
        suspect=edit_suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        proposed_full_cue=proposed,
        base_text_sha256=current_sha256,
        candidate_provenance={
            "kind": "cpa_context_proposal",
            "mutation_authorized": False,
            "prompt_sha256": "sha256:" + prompt_sha256,
            **({"proposal_audio": dict(proposal_audio)} if proposal_audio else {}),
        },
        candidate_memory_id=None,
        why=(f"[CPA 文字候选] {reason or finding.get('why') or ''}")[:240],
    )
    # A new range/text proposal cannot inherit an earlier transcript's
    # candidate-bound handoff. Keep that transcript as unmodified evidence.
    previous_handoff = rebuilt.pop("_exact_source_transcript_handoff", None)
    if isinstance(previous_handoff, Mapping):
        prior = list(rebuilt.get("_prior_exact_source_transcript_handoffs") or [])
        if previous_handoff not in prior:
            prior.append(previous_handoff)
        rebuilt["_prior_exact_source_transcript_handoffs"] = prior
    if rebuilt.get("repair_class") == "disclosure_only":
        rebuilt["repair_class"] = "phonetic"
    audit.update(
        status="PROPOSED",
        proposed_cue=proposed,
        proposed_cue_sha256="sha256:"
        + hashlib.sha256(proposed.encode("utf-8")).hexdigest(),
        suspect=edit_suspect,
        suggestion=replacement,
        span_start_codepoint=start,
        span_end_codepoint=end,
        reason=reason,
    )
    if bounded_full_cue_repair:
        audit["bounded_full_cue_repair"] = True
    _save_payload_cache(
        cache_path, PROPOSAL_REBUILD_CACHE_SCHEMA, prompt_sha256,
        str(completion_sha256), payload, served_from_cache=served_from_cache,
    )
    return rebuilt, audit


def _proposal_audio_recorded_call(llm_call, check_request, proposal_audio):
    """Keep the existing recorded callback and bind only actual prompt evidence."""
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan, SemanticAttemptRecorder

    recorder = getattr(llm_call, "_semantic_attempt_recorder", None)
    if not isinstance(recorder, SemanticAttemptRecorder):
        return llm_call
    # Do not append evidence behind the producer's prompt/cache hash boundary.
    # Both native observations must be present as the exact JSON data supplied
    # to the proposal producer; otherwise this candidate seam fails closed.
    def bound_call(prompt):
        if proposal_audio_prompt_payload(proposal_audio).missing_from(prompt):
            recorder.blocked = "PROPOSAL_AUDIO_PROMPT_BINDING_MISSING"
            recorder.events.append({"status": recorder.blocked, "provider_calls": 0})
            raise RetryNeedsPlan(recorder.blocked)
        recorder.sync_proposal_audio(check_request=check_request, proposal_audio=proposal_audio)
        return llm_call(prompt)

    from .completed_review_checkpoint import inherit_ruling_context
    inherit_ruling_context(bound_call, llm_call)
    return bound_call


def _original_audio_requests(llm_call) -> list[str]:
    recorder = getattr(llm_call, "_semantic_attempt_recorder", None)
    reader = getattr(recorder, "original_audio_requests", None)
    return reader() if callable(reader) else []


def prepare_missing_proposal_candidate(
    *,
    srt_text: str,
    finding: Mapping[str, Any],
    structured_chat: str,
    clip_context: Mapping[str, object] | None = None,
    source_media_timeline_offset_ms: int = 0,
    llm_call: Callable[[str], str] | None,
    derive_single_span_edit: Callable[..., tuple[str, str, int, int, str | None]],
    range_acoustic_observations: tuple[Mapping[str, Any], ...] = (),
    screen_context_observations: tuple[Mapping[str, Any], ...] = (),
    check_request: Mapping[str, Any] | None = None,
    entity_verifier: Callable | None = None,
    exact_source_transcript_provider: Callable | None = None,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    """Prepare a finding and a ready fail-closed response for the caller."""

    proposal_call = llm_call
    missing = (
        finding.get("proposed_full_cue") is None
        and not str(finding.get("suggestion") or "")
        and bool(str(finding.get("suspect") or ""))
    )
    if not missing:
        return dict(finding), None, None
    # A previous text-only RetryNeedsPlan must not strand actual cached audio.
    # Probe-only: no new acquisition is authorized by this preparation step.
    cached_audio = None
    if check_request is not None:
        supplied = collect_proposal_audio(request=check_request, finding=finding,
            entity_verifier=entity_verifier, transcript_provider=None, cached_only=True)
        if (supplied.get("original_language_observation")
                or supplied["phonetic_evidence"]["evidence_level"] == "AUDIO_OBSERVED"):
            cached_audio = supplied
            recorder = getattr(llm_call, "_semantic_attempt_recorder", None)
            planner = getattr(recorder, "plan_cached_proposal_audio", None)
            if callable(planner):
                planner(check_request=check_request, proposal_audio=cached_audio)
            proposal_call = _proposal_audio_recorded_call(llm_call, check_request, cached_audio)
            recall = getattr(recorder, "retained_current_proposal", None)
            original = recall(check_request=check_request, proposal_audio=cached_audio) if callable(recall) else None
            if original is not None:
                # This is a source-bound old candidate, never a new model reply
                # or a KEEP decision. The ordinary CURRENT-only verifier still
                # sees the present critical finding and all dissenting evidence.
                current = check_request["current_cue"]
                span = finding_suspect_span(current, finding)
                if span is not None:
                    rebuilt = {**finding, "suggestion": str(finding["suspect"]),
                        "proposed_full_cue": current, "span_start_codepoint": span[0],
                        "span_end_codepoint": span[1], "candidate_blind_observation_scaffolding": True,
                        "candidate_memory_id": None,
                        "candidate_provenance": {"kind": "candidate_blind_observation_scaffolding",
                            "basis": "CURRENT_ONLY_DUPLICATED", "mutation_authorized": False},
                        "retained_current_proposal": original}
                    return rebuilt, {"schema_version": PROPOSAL_REBUILD_SCHEMA,
                        "status": ('CURRENT_SUPPORTED' if original.get('status') == 'CURRENT_SUPPORTED'
                            else 'RECORDED_CANDIDATE_FOR_VERIFICATION'), "decision_authority": "CPA_PROPOSAL_ONLY",
                        "proposal_audio": cached_audio, "recorded_candidate": original,
                        "mutation_authorized": False, "new_provider_calls": 0}, None
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=srt_text,
        finding=finding,
        structured_chat=structured_chat,
        llm_call=proposal_call,
        derive_single_span_edit=derive_single_span_edit,
        range_acoustic_observations=range_acoustic_observations,
        screen_context_observations=screen_context_observations,
        clip_context=clip_context, proposal_audio=cached_audio,
    )
    # Keep text-first candidates cheap. Only acquire missing phonemes/original
    # language when the proposal actually requests them or fails the phonetic
    # constraint, then consume that new observation before context convergence.
    # A K04 stop (>=3 unresolved answers, no new material, no plan) on a target
    # whose own original CPA answers already asked for audio is that request:
    # acquisition must happen here, or the stop could never be lifted (41
    # targeted relisten §1/§4). K04 itself is unchanged; the normal recorder
    # still decides whether the acquired material opens a plan.
    # 40:57 / 41:1541: under a continuation stop only an owner that reuses its
    # own completed record first (a request-exact witness cache) may be asked;
    # its identical already-acquired input is then served, never re-bought. A
    # provider without that seam cannot show novelty, so K04 simply stays.
    material_stop = (rebuilt is None and check_request is not None and cached_audio is None
                     and audit.get("status") == "RETRY_BLOCKED"
                     and audit.get("reason_code") == "CONTINUATION_VALUE_PLAN_REQUIRED"
                     and callable(getattr(entity_verifier, "probe_witness_cache", None))
                     and _original_audio_requests(llm_call))
    if (rebuilt is None and check_request is not None
            and (audit.get("needs_audio") is True or material_stop
                 or audit.get("reason_code") == "TOPIC_PROPOSAL_PHONEME_MISMATCH")):
        proposal_audio = collect_proposal_audio(
            request=check_request, finding=finding, entity_verifier=entity_verifier,
            transcript_provider=exact_source_transcript_provider,
        )
        observed = bool(proposal_audio.get("original_language_observation")
                        or proposal_audio["phonetic_evidence"]["evidence_level"] == "AUDIO_OBSERVED")
        if material_stop:
            # The acquired audio only reaches CPA through the recorder's own
            # proposal-audio plan; already offered material keeps K04.
            audio_observed = observed
            if observed:
                recorder = getattr(llm_call, "_semantic_attempt_recorder", None)
                planner = getattr(recorder, "plan_cached_proposal_audio", None)
                if callable(planner):
                    planner(check_request=check_request, proposal_audio=proposal_audio, acquired=True)
                observed = getattr(recorder, "plan", None) is not None
            audit["k04_material_acquisition"] = {
                "basis": "ORIGINAL_CPA_NEEDS_AUDIO_UNDER_CONTINUATION_STOP",
                "original_audio_request_completion_sha256s": _original_audio_requests(llm_call),
                "acquisition_attempted": True, "audio_observed": audio_observed,
                "pinyin_failure": proposal_audio.get("pinyin_failure"),
                "transcript_failure": proposal_audio.get("transcript_failure"),
                "plan_opened": observed, "mutation_authorized": False}
        if observed:
            prior = audit
            proposal_call = _proposal_audio_recorded_call(llm_call, check_request, proposal_audio)
            rebuilt, audit = bootstrap_missing_proposal(
                srt_text=srt_text, finding=finding, structured_chat=structured_chat,
                llm_call=proposal_call, derive_single_span_edit=derive_single_span_edit,
                range_acoustic_observations=range_acoustic_observations,
                screen_context_observations=screen_context_observations,
                clip_context=clip_context, proposal_audio=proposal_audio,
            )
            # The same native envelope must reach convergence even when the
            # second proposal is uncertain or the recorded call was refused.
            audit["proposal_audio"] = dict(proposal_audio)
            audit["prior_text_proposal"] = prior
            if "k04_material_acquisition" in prior:
                audit["k04_material_acquisition"] = dict(prior["k04_material_acquisition"])
        else:
            # Provider failure alone is not new phonetic evidence for another
            # identical proposal call. Convergence still sees the weak constraint.
            audit["proposal_audio"] = proposal_audio
    if rebuilt is not None:
        return rebuilt, audit, None
    converged, convergence_audit = converge_missing_proposal(
        srt_text=srt_text,
        finding=finding,
        structured_chat=structured_chat,
        clip_context=clip_context,
        source_media_timeline_offset_ms=source_media_timeline_offset_ms,
        proposal_audit=audit,
        llm_call=proposal_call,
        derive_single_span_edit=derive_single_span_edit,
        range_acoustic_observations=range_acoustic_observations,
        screen_context_observations=screen_context_observations,
    )
    if converged is not None:
        return converged, audit, None
    if (convergence_audit.get("status") == "UNRESOLVED"
        and convergence_audit.get("needs_audio") is True):
        # Carry the request for evidence, not a guessed replacement.  The
        # caller's CURRENT-only scaffold enters the ordinary blind witness
        # and CPA chain, retaining all source/window and closure checks.
        pending = {**finding, "_cpa_missing_proposal_convergence": convergence_audit}
        return pending, audit, None
    return None, audit, {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "MISSING_PROPOSAL_UNRESOLVED",
        "repaired": False,
        "reason_code": convergence_audit.get("reason_code"),
        "proposal_bootstrap": audit,
        "cpa_missing_proposal_convergence": convergence_audit,
        "decision_authority": "CPA_JUDGE_FAILED",
    }


def context_only_keep_existing_adjudication(
    *,
    convergence: Mapping[str, Any],
    finding: Mapping[str, Any],
    proposal_bootstrap_audit: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the non-mutating receipt for CPA context-only convergence."""

    current_request = {
        "schema_version": "subtitle-context-convergence-request.v1",
        "kind": "missing_proposal_context_only_convergence",
        "cue_index": convergence.get("cue_index"),
        "current_cue_sha256": str(
            convergence.get("current_cue_sha256") or ""
        ).removeprefix("sha256:"),
        "final_srt_sha256": str(
            convergence.get("final_srt_sha256") or ""
        ).removeprefix("sha256:"),
        "context_sha256": str(
            convergence.get("context_sha256") or ""
        ).removeprefix("sha256:"),
        "closed_set_structured_evidence": convergence.get("closed_set_structured_evidence"),
        "decision": "KEEP_EXISTING",
        "timing_immutable": True,
    }
    current_request["request_sha256"] = hashlib.sha256(
        json.dumps(
            current_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "OBSERVED",
        "repaired": False,
        "policy_branch": "JUDGE_KEEPS_CURRENT",
        "timing_immutable": True,
        "request": current_request,
        "verdict": {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "NOT_REQUIRED",
            "target_audible": None,
            "reason_code": "CPA_CONTEXT_ONLY_FINAL_CONVERGENCE",
        },
        "witness_judge": {
            "witness_status": "NOT_REQUIRED",
            "judge": {
                "schema_version": "subtitle-cpa-context-judge.v1",
                "status": "JUDGED",
                "choice": "CURRENT",
                "reason": convergence.get("reason"),
                "prompt_sha256": convergence.get("prompt_sha256"),
                "completion_sha256": convergence.get(
                    "completion_sha256"
                ),
            },
        },
        "proposal_bootstrap": proposal_bootstrap_audit,
        "cpa_missing_proposal_convergence": dict(convergence),
        "rebuilt_finding": dict(finding),
        "decision_authority": "CPA_JUDGE",
        "witness_authority": "EVIDENCE_ONLY",
        "mutation_authority": {
            "schema_version": (
                "subtitle-correction-mutation-authority.v1"
            ),
            "status": "NOT_APPLIED",
            "basis": "CPA_CONTEXT_ONLY_KEEP_EXISTING",
        },
    }
