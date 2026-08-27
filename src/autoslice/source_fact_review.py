"""Joint CPA text gate for source-bound facts in hooks and titles."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.addressee_attribution import (
    ABSENCE_POLICY_ID,
    ADDRESSEE_UNRESOLVED_REASON,
    ALIGNMENT_POLICY_ID,
    SPEAKER_TRANSCRIPT_LABEL,
    TEXT_PARTITION_POLICY_ID,
    TIMING_POLICY_ID,
    addressee_prompt_block,
    # 有意从本模块转出：调用方要的是"这条产线的两份转写 + 复审"这一对，
    # 输入构造器和消费它的 review 函数留在同一个 import 面更难接错。
    build_addressee_transcripts as build_addressee_transcripts,
    evaluate_addressee_attribution,
)
from src.autoslice.candidate_entity_projection import (
    CandidateEntityProjectionError,
    FrozenCandidateEntityProjection,
    load_candidate_entity_projection,
)
from src.autoslice.candidate_public_text_surface_authority import (
    CandidatePublicTextSurfaceAuthority,
    CandidatePublicTextSurfaceAuthorityError,
    build_public_text_source_fact_context,
    load_candidate_public_text_surface_authority,
)
from src.autoslice.candidate_source_fact_refresh import (
    validate_candidate_public_text_source_fact_refresh_from_source_fact,
)
from src.autoslice.fastlane_c3_source_fact_supersession import DECISION as C3_TERMINAL_SOURCE_FACT_SUPERSESSION_DECISION, validate_c3_source_fact_review_from_validation
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.deterministic_text_surface_resolution import (
    CANDIDATE_ID as DETERMINISTIC_TEXT_NARROWING_CANDIDATE_ID,
    CONSUMPTION_SCHEMA_VERSION as DETERMINISTIC_TEXT_NARROWING_CONSUMPTION_SCHEMA,
    EXACT_COMBINED_SHA256 as DETERMINISTIC_TEXT_NARROWING_COMBINED_SHA256,
    EXACT_HOOK as DETERMINISTIC_TEXT_NARROWING_HOOK,
    EXACT_TITLE as DETERMINISTIC_TEXT_NARROWING_TITLE,
    DeterministicTextSurfaceResolutionError,
    consume_deterministic_text_surface_authority,
    load_deterministic_text_surface_authority,
)
from src.autoslice.llm_client import LlmCall, extract_json_object
from src.autoslice.manual_title_keep_authority import (
    ACTION as MANUAL_TITLE_KEEP_ACTION,
    FINDING_CLASS as MANUAL_TITLE_KEEP_FINDING_CLASS,
    PASS_DECISION as MANUAL_TITLE_KEEP_PASS_DECISION,
    ManualTitleKeepAuthorityError,
    load_manual_title_keep_authority,
    read_regular_no_symlink,
    validate_manual_title_keep_authority,
)
from src.autoslice.qixi_source_fact_terminal_preservation import (
    DECISION as QIXI_TERMINAL_TEXT_PRESERVATION_DECISION,
    validate_terminal_preservation_source_fact_review,
)
from src.autoslice.source_fact_review_shape import source_fact_review_passes_shape
from src.autoslice.surface_canon import (
    canonicalize_hard_meme_surfaces,
    hard_meme_surface_rules,
)
from src.autoslice.title_policy import publish_title_policy_violations
SCHEMA_VERSION = "lidousha-source-fact-review.v1"
RESCORE_CANDIDATE_SCHEMA_VERSION = "source-fact-rescore-candidate.v1"
ENTITY_CONTEXT_SCHEMA_VERSION = "source-fact-entity-context.v1"
MAX_REVIEW_PASSES = 5
# 程度升级词面：狍哥案（2026-08-07 §7）机器可复核部分——degree 槽只准逐字或
# 降级，不得升级；升级词若不在 before 原文也不在任何 evidence 原文中出现，
# 判定该 changed_surface 无效（CPA_TEXT_REVIEW_INVALID），逼判者要么不升级
# 要么必须逐字引用来源。这条不依赖 claim_decomposition 字段本身，纯词面
# 比对，保持对既有回执/测试 fixture 的完全向后兼容。
_DEGREE_UPGRADE_WORDS = ("最", "所有", "永远", "绝对", "彻底", "唯一", "一定", "必然")
# 同一 review pass 内的 provider 级重试帽：这道门是 LLM 采样，同一份输入一次
# 形状无效/调用失败就把整次产线判死是把骰子当结论。重试只针对 provider 层
# 失败（形状无效/调用异常），语义结果（KEEP/REPAIR）永不重掷；UNAVAILABLE
# （没配 llm_call）不重试。每次重试都进回执披露。
MAX_PROVIDER_RETRIES_PER_PASS = 2
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHANNEL_PROFILE = load_channel_profile(_REPO_ROOT)
_CANDIDATE_RECUT_SUFFIX_RX = re.compile(r"r\d+$")
_SHA256_VALUE_RX = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SPEAKER_EVIDENCE_UNSET = object()
DETERMINISTIC_TEXT_NARROWING_PASS_DECISION = "DETERMINISTIC_TEXT_NARROWING"
@dataclass(frozen=True, slots=True)
class _ResolvedEntityContext:
    """Runtime projection plus its relocation-safe receipt representation."""

    projection: FrozenCandidateEntityProjection | CandidatePublicTextSurfaceAuthority
    document: dict[str, object]

    @property
    def context_sha256(self) -> str:
        return str(self.document["context_sha256"])


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


def _validated_speaker_evidence(
    value: object,
    *,
    speaker_transcript: str | None,
) -> tuple[dict[str, object], str] | None:
    """Freeze and verify one canonical speaker-evidence document.

    The alignment builder owns the evidence schema.  This boundary deliberately
    rechecks its cryptographic self-bindings before allowing the document into
    a source-fact receipt, so a caller cannot pair a trusted transcript with a
    drifted alignment or raw speaker-final artifact.
    """

    if not isinstance(value, Mapping):
        return None
    try:
        document = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    policy_ids = document.get("policy_ids")
    if not (
        isinstance(policy_ids, dict)
        and policy_ids
        and all(
            isinstance(key, str) and bool(key) and isinstance(policy_id, str) and bool(policy_id)
            for key, policy_id in policy_ids.items()
        )
    ):
        return None
    state = document.get("state")
    present_policy_ids = {
        "alignment": ALIGNMENT_POLICY_ID,
        "text": TEXT_PARTITION_POLICY_ID,
        "timing": TIMING_POLICY_ID,
    }
    if state == "AbsentAuthorized":
        if (
            document.get("reason") != "speaker_mode_uniform_host"
            or policy_ids
            != {
                **present_policy_ids,
                "absence": ABSENCE_POLICY_ID,
            }
            or speaker_transcript is not None
            or any(
                key in document
                for key in (
                    "alignment",
                    "alignment_sha256",
                    "speaker_final_srt_sha256",
                    "speaker_finalization_manifest_sha256",
                    "speaker_transcript",
                    "speaker_transcript_sha256",
                )
            )
        ):
            return None
        return document, _sha256_json(document)
    if state != "PresentValid" or not isinstance(speaker_transcript, str) or not speaker_transcript:
        return None
    # The path-rich speaker manifest is revalidated against the current record
    # before this canonical evidence is derived, but its raw bytes legitimately
    # change when WSL package paths are relocated onto free.  Persist only the
    # relocation-invariant authority it proves: speaker SRT, alignment, and the
    # exact transcript shown to the judge.
    if "speaker_finalization_manifest_sha256" in document:
        return None
    if policy_ids != present_policy_ids:
        return None
    alignment = document.get("alignment")
    if not isinstance(alignment, dict):
        return None
    if alignment.get("policy_ids") != present_policy_ids:
        return None
    if document.get("alignment_sha256") != _sha256_json(alignment):
        return None
    if document.get("speaker_transcript") != speaker_transcript or document.get(
        "speaker_transcript_sha256"
    ) != _sha256_text(speaker_transcript):
        return None
    required_hashes = (
        "speaker_final_srt_sha256",
        "alignment_sha256",
        "speaker_transcript_sha256",
    )
    if any(
        not isinstance(document.get(key), str)
        or _SHA256_VALUE_RX.fullmatch(str(document.get(key))) is None
        for key in required_hashes
    ):
        return None
    plain_srt_sha256 = document.get("plain_srt_sha256")
    if plain_srt_sha256 is not None and (
        not isinstance(plain_srt_sha256, str)
        or _SHA256_VALUE_RX.fullmatch(plain_srt_sha256) is None
    ):
        return None
    return document, _sha256_json(document)


def _speaker_receipt_fields(
    speaker_evidence: dict[str, object] | None,
    speaker_evidence_sha256: str | None,
) -> dict[str, object]:
    if speaker_evidence is None or speaker_evidence_sha256 is None:
        return {}
    return {
        "speaker_evidence": dict(speaker_evidence),
        "speaker_evidence_sha256": speaker_evidence_sha256,
    }


def _candidate_family(candidate_id: str) -> str:
    value = str(candidate_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}", value) or Path(value).name != value:
        return ""
    return _CANDIDATE_RECUT_SUFFIX_RX.sub("", value)


def _load_source_fact_entity_context(
    *,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
    clip_context_prompt: str = "",
) -> _ResolvedEntityContext | None:
    """Resolve an optional candidate projection against this run's SRT bytes.

    Absence of a checked-in projection preserves the legacy lane exactly.  Once
    a projection exists, however, neither a missing final SRT nor a byte drift
    may silently downgrade the candidate back to that legacy lane.
    """

    family = _candidate_family(str(candidate_id or ""))
    if not family:
        return None
    projection_path = (
        _CHANNEL_PROFILE.asset_root
        / "candidate_entity_projections"
        / f"{family}.entity-projection.v1.json"
    )
    if not (projection_path.exists() or projection_path.is_symlink()):
        authority = load_candidate_public_text_surface_authority(family, root=_REPO_ROOT)
        if authority is None:
            return None
        return _ResolvedEntityContext(
            projection=authority,
            document=build_public_text_source_fact_context(
                authority,
                clip_context_prompt=clip_context_prompt,
            ),
        )
    if final_reviewed_srt_path is None:
        raise CandidateEntityProjectionError("SOURCE_FACT_FINAL_REVIEWED_SRT_REQUIRED")
    final_srt = Path(final_reviewed_srt_path)
    try:
        final_srt_bytes = read_regular_no_symlink(final_srt)
    except ManualTitleKeepAuthorityError as exc:
        raise CandidateEntityProjectionError("SOURCE_FACT_FINAL_REVIEWED_SRT_UNAVAILABLE") from exc
    reviewed_baseline = (
        _CHANNEL_PROFILE.asset_directory("reviewed_subtitle_baselines") / f"{family}.reviewed.srt"
    )
    projection = load_candidate_entity_projection(
        projection_path=projection_path,
        candidate_id=family,
        reviewed_srt_path=reviewed_baseline,
    )
    observed_srt_sha256 = hashlib.sha256(final_srt_bytes).hexdigest()
    if observed_srt_sha256 != projection.binding.reviewed_srt_sha256:
        raise CandidateEntityProjectionError(
            "SOURCE_FACT_FINAL_REVIEWED_SRT_BINDING_MISMATCH:"
            f"expected={projection.binding.reviewed_srt_sha256}:"
            f"actual={observed_srt_sha256}"
        )

    rules: list[dict[str, object]] = []
    for identity in projection.identity_equivalences:
        rules.append(
            {
                "entity_id": identity.entity_id,
                "identity_equivalent_surfaces": list(identity.equivalent_surfaces),
                "reviewed_surface": projection.projected_surface(
                    entity_id=identity.entity_id,
                    surface_type="reviewed",
                ),
                "title_cover_surface": projection.projected_surface(
                    entity_id=identity.entity_id,
                    surface_type="title_cover",
                ),
            }
        )
    body: dict[str, object] = {
        "schema_version": ENTITY_CONTEXT_SCHEMA_VERSION,
        "authority_scope": "IDENTITY_AND_SPELLING_ONLY_NO_EVENT_FACT_AUTHORITY",
        "candidate_id": family,
        "final_reviewed_srt_sha256": observed_srt_sha256,
        "projection_sha256": projection.projection_sha256,
        "candidate_binding": {
            "source_recording_basename": (projection.binding.source_recording_basename),
            "source_sha256": projection.binding.source_sha256,
            "absolute_source_start_ms": (projection.binding.absolute_source_start_ms),
            "absolute_source_end_ms": (projection.binding.absolute_source_end_ms),
        },
        "identity_spelling_rules": rules,
    }
    return _ResolvedEntityContext(
        projection=projection,
        document={**body, "context_sha256": _sha256_json(body)},
    )


def resolve_source_fact_entity_context(
    *,
    candidate_id: str,
    final_reviewed_srt_path: Path,
) -> dict[str, object]:
    """Rebuild current projection context for a typed external binder.

    KEEP replay is defined only for this projection-bearing candidate; missing
    context therefore blocks instead of entering the ordinary legacy lane.
    """

    context = _load_source_fact_entity_context(
        candidate_id=candidate_id,
        final_reviewed_srt_path=final_reviewed_srt_path,
    )
    if context is None:
        raise CandidateEntityProjectionError("MANUAL_TITLE_KEEP_CURRENT_ENTITY_CONTEXT_REQUIRED")
    return dict(context.document)


def _entity_context_prompt_block(
    context: _ResolvedEntityContext | None,
) -> str:
    if context is None:
        return ""
    rules = context.document.get("identity_spelling_rules")
    assert isinstance(rules, list)
    public_only = context.document.get("authority_scope") == (
        "GENERATED_PUBLIC_TEXT_ONLY_NO_SUBTITLE_OR_SPEAKER_REVIEW"
    )
    lines = [
        "候选实体词面约束（identity/spelling only，不授权任何新事件事实）：",
        f"entity_context_sha256: {context.context_sha256}",
    ]
    for row in rules:
        assert isinstance(row, Mapping)
        equivalents = row.get("identity_equivalent_surfaces")
        assert isinstance(equivalents, list)
        joined = "↔".join(f"「{surface}」" for surface in equivalents)
        subtitle_clause = (
            "本 authority 不裁定也不修改最终字幕/说话人，"
            if public_only
            else f"最终字幕可保留 reviewed_surface「{row.get('reviewed_surface')}」，"
        )
        lines.append(
            f"- {row.get('entity_id')}: {joined} 指同一实体；{subtitle_clause}但 selection_hook、"
            f"投稿标题与封面属于 derived title_cover 文案，提到该实体时必须写"
            f"「{row.get('title_cover_surface')}」。不得为了贴合字幕拼写把 derived "
            "文案改成另一个 identity-equivalent surface。"
        )
    lines.append(
        (
            "上述 context 已绑定 candidate、clip-context 与 source pieces；它只"
            "裁定生成公共文案的专名拼写，不代表整份 SRT 或 speaker 已人工复审。"
            if public_only
            else "上述 context 已绑定本轮 final reviewed SRT 原始 bytes、candidate、source "
            "interval 与 projection；它只裁定同一实体及各表面拼写，不得作为剧情、"
            "动作、因果或说话人事实的证据。"
        )
    )
    return "\n".join(lines) + "\n"


def _entity_surface_error(
    context: _ResolvedEntityContext | None,
    *,
    selection_hook: str,
    title: str,
) -> str | None:
    if context is None:
        return None
    try:
        context.projection.require_text_surfaces(
            surface_type="title_cover",
            text=selection_hook,
        )
        context.projection.require_text_surfaces(
            surface_type="title_cover",
            text=title,
        )
    except (CandidateEntityProjectionError, CandidatePublicTextSurfaceAuthorityError) as exc:
        return str(exc)
    return None


def _entity_receipt_fields(
    context: _ResolvedEntityContext | None,
) -> dict[str, object]:
    return {"entity_context": dict(context.document)} if context else {}


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


def _hard_meme_canon_prompt_block() -> str:
    """Teach the judge the channel's unbypassable meme canon.

    Without this the literal-evidence gate and the meme canon deadlock: the
    final transcript spells the meme canonically, raw danmaku keeps the banned
    surface, and a judge that only does literal binding "repairs" derived copy
    back and forth until the candidate dies (hook rewrite then also trips the
    scorecard-stale gate).  The canon is final-output law, so the judge must
    read canonical spellings as carrying the original surface's semantics.
    """

    rules = hard_meme_surface_rules()
    if not rules:
        return ""
    listing = "；".join(f"「{rule.surface}」一律写作「{rule.canonical}」" for rule in rules)
    return (
        "频道钦定梗词规范（hard-meme-canon，最终输出铁律）：" + listing + "。"
        "规范词面是同一个梗的钦定拼写，不是换词：最终字幕与两份文案里的规范"
        "词面承载原词的完整语义，判断事实支持时必须按原词语义理解；不得因为"
        "弹幕/证据原文用了被禁拼写而判定文案不受支持，也永远不得把规范词面"
        "改回被禁拼写。你输出的一切文案必须使用规范词面。\n"
    )


def _prompt(
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    review_pass: int,
    title_policy_violations: list[str],
    speaker_transcript: str | None,
    entity_context: _ResolvedEntityContext | None,
) -> str:
    return (
        "你是李豆沙切片派生文案的 source-fact 最终裁决者。你只有文字输入，"
        "不要声称听见音频或看见画面。一次联合裁决 selection_hook 与投稿标题，"
        "不是字幕改写任务。\n"
        + _hard_meme_canon_prompt_block()
        + "判断两份文案里的每个具体事件、对象、因果、身份、专名、事实模态和同音"
        "释义，是否能由最终字幕或同片 hash-bound 结构化弹幕/SC/上下文支持。"
        "允许不逐字的自然概括，但不允许把提议写成既成事实、把猜测写成断言，"
        "也不允许凭空把同音词换成另一个含义。\n"
        + _entity_context_prompt_block(entity_context)
        + "如果你修复 selection_hook，还必须核对原 selection_scorecard 的"
        "tier_basis、tier_reason、维度和证据 cue 是否仍描述修复后的同一核心梗。"
        "若仍一致，selection_scorecard_review.status=COMPATIBLE；若核心梗已换，"
        "必须填 INCOMPATIBLE，流水线会停止而不是把旧评分卡带进新 StoryContract。"
        "selection_hook 未改时填 NOT_NEEDED。\n"
        "结构化弹幕必须按时间相邻关系整体理解：例如一条写“被点了”，紧接下一条"
        "补“电”，并且近窗还有雷/劈等同主题文字时，“被电”可以是有同片证据的"
        "语义还原，不能仅因它没有逐字出现在单独一行字幕里而拒绝。反之，只有孤立"
        "近音、没有相邻或同主题证据时必须修复。长期记忆和普通 ASR 初稿只提供"
        "候选，不能单独授权新事实。\n"
        "合理概括 vs 无依据升级：changed_surfaces 每行可选附 claim_decomposition，"
        "把改动拆成 actor/action/object/degree/outcome 五槽，每槽标"
        "SUPPORTED_BY_CUES（cue 逐字支持）、GENERALIZED_FROM（由连续/邻近 cue 链"
        "共同蕴含的间接概括）或 UNSUPPORTED。允许的概括：每槽至少一条 cue 证据、"
        "无槽为 UNSUPPORTED 的间接言语行为整体归纳（例如多条连续弹幕/发言共同"
        "蕴含“投奔求庇护”）。禁止的升级（任一即整行无效，必须改判 REPAIR 给出"
        "正确文案而不是放弃）：受事换人（把对 A 的动作写成对 B 本人）、因果虚构"
        "（把两件独立的事编成前者导致后者的戏剧闭环）、程度升级（degree 槽只能"
        "逐字或降级，不得把“非常”写成“最”“所有”“永远”这类升级词，除非升级词本身"
        "逐字出现在证据原文里）、把提案/意向写成既成事实。单个窄音频 cue 的裁决"
        "只拥有该 cue 自己的字面，不得反向抹除其他 cue 链共同蕴含的语义支持。\n"
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
        + addressee_prompt_block(speaker_transcript)
        + "投稿标题总长度必须为 12–49 个字符并满足下方 deterministic title policy。"
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
        f"structured_chat、same_clip_context、{SPEAKER_TRANSCRIPT_LABEL}；"
        "不得填写 audio 或 image。structured_chat、same_clip_context 只可出现在 supported_by，"
        "绝不可作为 changed_surfaces.evidence 的引用标签；changed_surfaces.evidence "
        "若引用弹幕或 SC，必须去掉同片上下文的行首列表符号后，逐字复制完整 "
        "danmaku|superchat @<offset>ms event=<id>: 行，禁止使用 structured_chat: 或 "
        "same_clip_context: 泛化标签。"
        "changed_surfaces 在 KEEP 时为空数组，REPAIR 时逐项列出 artifact、"
        "before、after、reason 和实际 evidence 原文。"
        "addressee_attribution 永远必须存在（无归属断言时填空数组）。\n"
        '{"schema_version":"lidousha-source-fact-review.v1",'
        '"status":"KEEP|REPAIR","final_selection_hook":"完整钩子",'
        '"final_title":"完整标题","supported_by":["final_transcript"],'
        '"changed_surfaces":[{"artifact":"selection_hook|title",'
        '"before":"...","after":"...","reason":"...",'
        '"evidence":["..."]}],'
        '"addressee_attribution":[{"assertion":"文案里逐字复制的归属断言",'
        '"verdict":"SUPPORTED|WRONG_ADDRESSEE|UNVERIFIABLE",'
        '"actual_speaker":"...","actual_addressee":"...","reason":"...",'
        f'"evidence":["{SPEAKER_TRANSCRIPT_LABEL}: 1 [说话人] 原话"]}}],'
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
    speaker_transcript: str | None = None,
) -> bool:
    """Accept exact evidence while tolerating CPA citation-label formatting."""

    source_label = re.fullmatch(
        r"\s*final_transcript\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if source_label:
        quoted_text = source_label.group(1)
        if _compact(quoted_text) and _compact(quoted_text) in _compact(final_transcript):
            return True
        # ``final_transcript`` joins bare cue text — no line numbers (unlike
        # ``speaker_transcript``; see ``build_addressee_evidence``).  With no
        # numbered transcript to anchor to (uniform_host authorized absence,
        # ``ABSENCE_POLICY_ID``) the judge invents locators: a leading "N "
        # index, or a whole fabricated SRT block (8/19 production, 七夕
        # auto_113022_354_496).  Strip at most one invented index and one
        # invented timestamp line; the remainder must still be an exact
        # literal substring — this relaxes the citation shell, never what
        # counts as evidence.
        stripped = re.sub(r"\A\s*\d+(?:\.\d+)?\s+", "", quoted_text, count=1)
        stripped = re.sub(
            r"\A\s*\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*\n?",
            "",
            stripped,
            count=1,
        )
        if stripped != quoted_text and _compact(stripped):
            return bool(_compact(stripped) in _compact(final_transcript))
        return False
    speaker_label = re.fullmatch(
        rf"\s*{re.escape(SPEAKER_TRANSCRIPT_LABEL)}\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if speaker_label:
        quoted_text = speaker_label.group(1)
        return bool(
            speaker_transcript
            and _compact(quoted_text)
            and _compact(quoted_text) in _compact(speaker_transcript)
        )
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
        compact_value
        and compact_value
        in _compact(
            final_transcript + "\n" + clip_context_prompt + "\n" + str(speaker_transcript or "")
        )
    )


def _degree_upgrade_unsupported(before: str, after: str, evidence: list[str]) -> bool:
    """Reject a degree upgrade unless the upgraded word is itself quoted evidence.

    ``_evidence_row_is_bound`` already proves each evidence row is a literal
    substring of the source; this only asks whether the *specific* upgrade
    word appears in that already-bound text, not whether the row is bound.
    """

    added = [word for word in _DEGREE_UPGRADE_WORDS if word in after and word not in before]
    if not added:
        return False
    return not any(any(word in row for word in added) for row in evidence)


def _valid_changed_surface(
    value: object,
    *,
    before_surface: str,
    after_surface: str,
    final_transcript: str,
    clip_context_prompt: str,
    speaker_transcript: str | None = None,
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
                speaker_transcript=speaker_transcript,
            )
            for row in evidence
        )
        and not _degree_upgrade_unsupported(
            str(value.get("before")), str(value.get("after")), evidence
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
    speaker_transcript: str | None = None,
    speaker_evidence_sha256: str | None = None,
    entity_context: _ResolvedEntityContext | None = None,
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
        speaker_transcript=speaker_transcript,
        entity_context=entity_context,
    )
    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "review_pass": review_pass,
        "selection_hook_sha256": _sha256_text(selection_hook),
        "title_sha256": _sha256_text(title),
        "final_transcript_sha256": _sha256_text(final_transcript),
        "speaker_transcript_sha256": (
            _sha256_text(speaker_transcript) if speaker_transcript else None
        ),
        "clip_context_prompt_sha256": _sha256_text(clip_context_prompt),
        "selection_scorecard_sha256": _sha256_json(selection_scorecard),
        "title_policy_mode": ("automatic" if enforce_automatic_title_style else "baseline"),
        "title_policy_violations": title_policy_violations,
        "request_sha256": _sha256_text(prompt),
    }
    if entity_context is not None:
        base["entity_context_sha256"] = entity_context.context_sha256
    if speaker_evidence_sha256 is not None:
        base["speaker_evidence_sha256"] = speaker_evidence_sha256
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
    # 钦定梗词铁律先于一切 shape 校验：裁决者若把规范词面改回被禁拼写，
    # 这里确定性回正（changed_surfaces.after 同步回正以保持包含性校验一致）。
    if isinstance(final_hook, str):
        final_hook, _ = canonicalize_hard_meme_surfaces(final_hook)
    if isinstance(final_title, str):
        final_title, _ = canonicalize_hard_meme_surfaces(final_title)
    entity_surface_error = (
        _entity_surface_error(
            entity_context,
            selection_hook=final_hook,
            title=final_title,
        )
        if isinstance(final_hook, str) and isinstance(final_title, str)
        else None
    )
    supported_by = payload.get("supported_by")
    changes = payload.get("changed_surfaces")
    if isinstance(changes, list):
        # before/after 同步回正：入口铁律保证被评审的两份文案永远是规范
        # 词面，被禁拼写的 before/after 只可能是裁决者笔误，回正后包含性
        # 校验才在同一词面上成立。
        changes = [
            (
                {
                    **row,
                    **{
                        key: canonicalize_hard_meme_surfaces(row[key])[0]
                        for key in ("before", "after")
                        if isinstance(row.get(key), str)
                    },
                }
                if isinstance(row, Mapping)
                else row
            )
            for row in changes
        ]
    scorecard_review = payload.get("selection_scorecard_review")
    summary = payload.get("summary")
    addressee = evaluate_addressee_attribution(
        payload.get("addressee_attribution"),
        selection_hook=selection_hook,
        title=title,
        speaker_transcript=speaker_transcript,
        status=status,
    )
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
                speaker_transcript=speaker_transcript,
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
                SPEAKER_TRANSCRIPT_LABEL,
            }
            for value in supported_by
        )
        and isinstance(changes, list)
        and isinstance(summary, str)
        and len(summary.strip()) >= 4
        and scorecard_review_valid
        and addressee.valid
        and entity_surface_error is None
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
        "reason_code": (
            None
            if shape_valid
            else (
                "CPA_ENTITY_SURFACE_RESPONSE_INVALID"
                if entity_surface_error is not None
                else (addressee.reason_code or "CPA_TEXT_REVIEW_INVALID")
            )
        ),
        "final_selection_hook": (final_hook if isinstance(final_hook, str) else ""),
        "final_title": final_title if isinstance(final_title, str) else "",
        "supported_by": (list(supported_by) if isinstance(supported_by, list) else []),
        "changed_surfaces": list(changes) if isinstance(changes, list) else [],
        "addressee_attribution": addressee.rows,
        "addressee_attribution_mode": addressee.mode,
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
    speaker_transcript: str | None = None,
    speaker_evidence: Mapping[str, object] | None = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
) -> dict[str, object]:
    """Run a bounded, evidence-bound KEEP/REPAIR convergence review."""

    # 入口即回正：hard-meme-canon 是最终输出铁律，被禁拼写不允许进入评审
    # 循环（也保证 KEEP 的逐字等式在规范词面上成立）。默认链路上游已回正，
    # 这里对手写 spec 等旁路输入兜底。
    selection_hook, _ = canonicalize_hard_meme_surfaces(selection_hook)
    title, _ = canonicalize_hard_meme_surfaces(title)
    canonical_speaker_evidence: dict[str, object] | None = None
    speaker_evidence_sha256: str | None = None
    if speaker_evidence is not None:
        validated_speaker_evidence = _validated_speaker_evidence(
            speaker_evidence,
            speaker_transcript=speaker_transcript,
        )
        if validated_speaker_evidence is None:
            return _finalize_receipt(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAILED",
                    "decision": "NONE",
                    "reason_code": "SOURCE_FACT_SPEAKER_EVIDENCE_INVALID",
                    "original_selection_hook": selection_hook,
                    "original_title": title,
                    "final_selection_hook": selection_hook,
                    "final_title": title,
                    "passes": [],
                    "provider_retries": [],
                }
            )
        canonical_speaker_evidence, speaker_evidence_sha256 = validated_speaker_evidence
    speaker_receipt_fields = _speaker_receipt_fields(
        canonical_speaker_evidence,
        speaker_evidence_sha256,
    )
    try:
        entity_context = _load_source_fact_entity_context(
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
            clip_context_prompt=clip_context_prompt,
        )
    except (
        CandidateEntityProjectionError,
        CandidatePublicTextSurfaceAuthorityError,
        OSError,
    ) as exc:
        return _finalize_receipt(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED",
                "decision": "NONE",
                "reason_code": "SOURCE_FACT_ENTITY_CONTEXT_INVALID",
                "entity_context_error": str(exc),
                "original_selection_hook": selection_hook,
                "original_title": title,
                "final_selection_hook": selection_hook,
                "final_title": title,
                "passes": [],
                "provider_retries": [],
                **speaker_receipt_fields,
            }
        )
    input_surface_error = _entity_surface_error(
        entity_context,
        selection_hook=selection_hook,
        title=title,
    )
    if input_surface_error is not None:
        return _finalize_receipt(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED",
                "decision": "NONE",
                "reason_code": "SOURCE_FACT_INPUT_ENTITY_SURFACE_INVALID",
                "entity_surface_error": input_surface_error,
                "original_selection_hook": selection_hook,
                "original_title": title,
                "final_selection_hook": selection_hook,
                "final_title": title,
                "passes": [],
                "provider_retries": [],
                **_entity_receipt_fields(entity_context),
                **speaker_receipt_fields,
            }
        )
    current_hook = selection_hook
    current_title = title
    passes: list[dict[str, object]] = []
    provider_retries: list[dict[str, object]] = []
    seen_surfaces = {(current_hook, current_title)}

    for review_pass in range(1, MAX_REVIEW_PASSES + 1):
        attempt = 0
        while True:
            review = _single_review(
                selection_hook=current_hook,
                title=current_title,
                final_transcript=final_transcript,
                clip_context_prompt=clip_context_prompt,
                selection_scorecard=selection_scorecard,
                llm_call=llm_call,
                review_pass=review_pass,
                enforce_automatic_title_style=enforce_automatic_title_style,
                speaker_transcript=speaker_transcript,
                speaker_evidence_sha256=speaker_evidence_sha256,
                entity_context=entity_context,
            )
            if (
                review.get("status") != "FAILED"
                or review.get("reason_code")
                not in {
                    "CPA_TEXT_REVIEW_INVALID",
                    "CPA_ENTITY_SURFACE_RESPONSE_INVALID",
                    "CPA_TEXT_REVIEW_CALL_FAILED",
                    # 自相矛盾的归属判项（WRONG_ADDRESSEE 却判 KEEP、或该判不判）
                    # 与形状无效同类：同一份输入重掷一次形状，不是重掷语义结论。
                    ADDRESSEE_UNRESOLVED_REASON,
                }
                or attempt >= MAX_PROVIDER_RETRIES_PER_PASS
            ):
                break
            attempt += 1
            provider_retries.append(
                {
                    "review_pass": review_pass,
                    "attempt": attempt,
                    "reason_code": review.get("reason_code"),
                    "request_sha256": review.get("request_sha256"),
                    "response_sha256": review.get("response_sha256"),
                }
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
                    "provider_retries": provider_retries,
                    **_entity_receipt_fields(entity_context),
                    **speaker_receipt_fields,
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
                    "provider_retries": provider_retries,
                    **_entity_receipt_fields(entity_context),
                    **speaker_receipt_fields,
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
                    "provider_retries": provider_retries,
                    **_entity_receipt_fields(entity_context),
                    **speaker_receipt_fields,
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
            # 狍哥案修复（2026-08-07，docs/reviews/2026-08-07-
            # source-fact-rescore-design.md §3.1）：修正成果不再只活在
            # passes[-1]——rescore_candidate 块把它挂到回执顶层，供下游有界
            # 重评分车道消费。final_selection_hook/title 仍回置原文，
            # "未授权不落盘"不变式不变；只有这个新块携带修正稿。
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
                    "provider_retries": provider_retries,
                    **_entity_receipt_fields(entity_context),
                    **speaker_receipt_fields,
                    "rescore_candidate": {
                        "schema_version": RESCORE_CANDIDATE_SCHEMA_VERSION,
                        "repaired_selection_hook": repaired_hook,
                        "repaired_selection_hook_sha256": _sha256_text(repaired_hook),
                        "repaired_title": repaired_title,
                        "repaired_title_sha256": _sha256_text(repaired_title),
                        "stale_selection_scorecard_sha256": _sha256_json(selection_scorecard),
                        "selection_scorecard_review": (
                            dict(review["selection_scorecard_review"])
                            if isinstance(review.get("selection_scorecard_review"), Mapping)
                            else None
                        ),
                    },
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
                    "provider_retries": provider_retries,
                    **_entity_receipt_fields(entity_context),
                    **speaker_receipt_fields,
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
            "provider_retries": provider_retries,
            **_entity_receipt_fields(entity_context),
            **speaker_receipt_fields,
        }
    )


def source_fact_review_passes(review: object) -> bool:
    return source_fact_review_passes_shape(
        review,
        schema_version=SCHEMA_VERSION,
        manual_title_keep_decision=MANUAL_TITLE_KEEP_PASS_DECISION,
        deterministic_text_narrowing_decision=DETERMINISTIC_TEXT_NARROWING_PASS_DECISION,
        terminal_text_preservation_decision=QIXI_TERMINAL_TEXT_PRESERVATION_DECISION, c3_terminal_source_fact_supersession_decision=C3_TERMINAL_SOURCE_FACT_SUPERSESSION_DECISION,
    )


def authorize_manual_title_repair(
    review: Mapping[str, object],
    *,
    consumption: Mapping[str, object],
) -> dict[str, object]:
    """Promote one blocked repair only after an exact external authority match.

    The blocked source-fact receipt remains hash-bound in ``consumption``; the
    new receipt deliberately records both the original CPA proposal and the
    one-off manual authorization instead of rerunning CPA or broadening the
    manual-title policy.
    """

    if (
        review.get("schema_version") != SCHEMA_VERSION
        or review.get("status") != "FAILED"
        or review.get("decision") != "REPAIR_REQUIRES_TITLE_AUTHORITY"
        or review.get("reason_code") != "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED"
        or consumption.get("status") != "CONSUMED"
    ):
        raise ValueError("MANUAL_TITLE_REPAIR_AUTHORITY_RECEIPT_INVALID")
    passes = review.get("passes")
    if not isinstance(passes, list) or len(passes) != 1 or not isinstance(passes[0], Mapping):
        raise ValueError("MANUAL_TITLE_REPAIR_AUTHORITY_RECEIPT_INVALID")
    proposed_hook = str(passes[0].get("final_selection_hook") or "")
    proposed_title = str(passes[0].get("final_title") or "")
    if (
        not proposed_hook
        or not proposed_title
        or consumption.get("final_selection_hook") != proposed_hook
        or consumption.get("final_title") != proposed_title
        or consumption.get("original_selection_hook") != review.get("original_selection_hook")
        or consumption.get("original_title") != review.get("original_title")
        or consumption.get("source_fact_receipt_sha256") != review.get("receipt_sha256")
    ):
        raise ValueError("MANUAL_TITLE_REPAIR_AUTHORITY_RECEIPT_INVALID")
    return _finalize_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "decision": "REPAIRED",
            "original_selection_hook": review["original_selection_hook"],
            "original_title": review["original_title"],
            "final_selection_hook": proposed_hook,
            "final_title": proposed_title,
            "passes": list(passes),
            "manual_title_repair_authority_consumption": dict(consumption),
            **(
                {"entity_context": dict(review["entity_context"])}
                if isinstance(review.get("entity_context"), Mapping)
                else {}
            ),
            **(
                {
                    "speaker_evidence": dict(review["speaker_evidence"]),
                    "speaker_evidence_sha256": review["speaker_evidence_sha256"],
                }
                if isinstance(review.get("speaker_evidence"), Mapping)
                and isinstance(review.get("speaker_evidence_sha256"), str)
                else {}
            ),
        }
    )


def authorize_manual_title_keep(
    review: Mapping[str, object],
    *,
    consumption: Mapping[str, object],
) -> dict[str, object]:
    """Resolve exactly one compression-hedge dissent without mutating title bytes.

    The complete FAILED receipt remains embedded and hash-bound.  This function
    accepts no prose-only grant: the stable consumption must bind the exact
    candidate, changed-surface fingerprint, immutable inputs, SRTs, and entity
    context that produced the blocked adjudication.
    """

    passes = review.get("passes")
    entity_context = review.get("entity_context")
    speaker_evidence = review.get("speaker_evidence")
    blocked_body = dict(review)
    blocked_receipt_sha256 = blocked_body.pop("receipt_sha256", None)
    if (
        review.get("schema_version") != SCHEMA_VERSION
        or review.get("status") != "FAILED"
        or review.get("decision") != "REPAIR_REQUIRES_TITLE_AUTHORITY"
        or review.get("reason_code") != "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED"
        or review.get("original_selection_hook") != review.get("final_selection_hook")
        or review.get("original_title") != review.get("final_title")
        or not isinstance(passes, list)
        or len(passes) != 1
        or not isinstance(passes[0], Mapping)
        or not isinstance(entity_context, Mapping)
        or not isinstance(speaker_evidence, Mapping)
        or not isinstance(blocked_receipt_sha256, str)
        or _finalize_receipt(blocked_body).get("receipt_sha256") != blocked_receipt_sha256
        or consumption.get("status") != "CONSUMED"
        or consumption.get("field") != "title"
        or consumption.get("action") != MANUAL_TITLE_KEEP_ACTION
        or consumption.get("finding_class") != MANUAL_TITLE_KEEP_FINDING_CLASS
        or consumption.get("disputed_span") != "结伴后"
        or consumption.get("record_dissent") is not True
        or consumption.get("candidate_id") != entity_context.get("candidate_id")
        or consumption.get("blocked_source_fact_receipt_sha256") != blocked_receipt_sha256
        or consumption.get("approved_title_sha256")
        != _sha256_text(str(review.get("original_title") or ""))
        or consumption.get("final_transcript_sha256") != passes[0].get("final_transcript_sha256")
        or consumption.get("clip_context_prompt_sha256")
        != passes[0].get("clip_context_prompt_sha256")
        or consumption.get("selection_scorecard_sha256")
        != passes[0].get("selection_scorecard_sha256")
        or consumption.get("entity_context_sha256") != entity_context.get("context_sha256")
        or consumption.get("reviewed_speaker_srt_sha256")
        != speaker_evidence.get("speaker_final_srt_sha256")
    ):
        raise ValueError("MANUAL_TITLE_KEEP_AUTHORITY_RECEIPT_INVALID")
    changed = passes[0].get("changed_surfaces")
    if (
        passes[0].get("status") != "REPAIR"
        or passes[0].get("final_selection_hook") != review.get("original_selection_hook")
        or not isinstance(changed, list)
        or len(changed) != 1
        or not isinstance(changed[0], Mapping)
        or changed[0].get("artifact") != "title"
        or changed[0].get("before") != review.get("original_title")
        or changed[0].get("after") != passes[0].get("final_title")
        or consumption.get("finding_fingerprint_sha256") != _sha256_json(dict(changed[0]))
    ):
        raise ValueError("MANUAL_TITLE_KEEP_AUTHORITY_RECEIPT_INVALID")
    blocked = json.loads(json.dumps(dict(review), ensure_ascii=False))
    dissent = {
        "schema_version": "source-fact-recorded-dissent.v1",
        "status": "RECORDED_NON_BLOCKING",
        "finding_class": MANUAL_TITLE_KEEP_FINDING_CLASS,
        "field": "title",
        "span": "结伴后",
        "action": MANUAL_TITLE_KEEP_ACTION,
        "proposed_title": changed[0]["after"],
        "reason": changed[0]["reason"],
        "evidence": list(changed[0]["evidence"]),
        "blocked_source_fact_receipt_sha256": review["receipt_sha256"],
        "finding_fingerprint_sha256": consumption["finding_fingerprint_sha256"],
    }
    return _finalize_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "decision": MANUAL_TITLE_KEEP_PASS_DECISION,
            "reason_code": "SOURCE_FACT_SUPPORTED_COMPRESSION_HEDGE_KEPT",
            "original_selection_hook": review["original_selection_hook"],
            "original_title": review["original_title"],
            "final_selection_hook": review["original_selection_hook"],
            "final_title": review["original_title"],
            "passes": json.loads(json.dumps(passes, ensure_ascii=False)),
            "provider_retries": json.loads(
                json.dumps(review.get("provider_retries") or [], ensure_ascii=False)
            ),
            "entity_context": dict(entity_context),
            "speaker_evidence": dict(speaker_evidence),
            "speaker_evidence_sha256": review["speaker_evidence_sha256"],
            "blocked_source_fact_review": blocked,
            "manual_title_keep_authority_consumption": dict(consumption),
            "recorded_dissent": dissent,
        }
    )
def authorize_deterministic_text_narrowing(
    consumption: Mapping[str, object],
) -> dict[str, object]:
    """Promote one sealed closed-plan replay without calling a provider.

    The original failed provider attempt remains embedded byte-for-byte.  The
    new PASS means only that a repository-sealed deterministic renderer
    narrowed the reviewed text surface; it does not relabel the failed attempt
    or turn Ivan's broad publication quote into free-form title authority.
    """

    blocked = consumption.get("original_source_fact_receipt")
    body = dict(consumption)
    declared_consumption_sha256 = body.pop("receipt_sha256", None)
    if (
        consumption.get("schema_version") != DETERMINISTIC_TEXT_NARROWING_CONSUMPTION_SCHEMA
        or consumption.get("status") != "VALID"
        or consumption.get("candidate_id") != DETERMINISTIC_TEXT_NARROWING_CANDIDATE_ID
        or consumption.get("provider_call_required") is not False
        or consumption.get("exact_surface_resolution") != "VALID"
        or consumption.get("publication_fact_authority") != "RESOLVED_EXACT_SURFACE"
        or not isinstance(declared_consumption_sha256, str)
        or _sha256_json(body) != declared_consumption_sha256
        or not isinstance(blocked, Mapping)
        or blocked.get("schema_version") != SCHEMA_VERSION
        or blocked.get("status") != "FAILED"
        or blocked.get("decision") != "NONE"
        or blocked.get("reason_code") != "CPA_TEXT_REVIEW_INVALID"
        or consumption.get("original_source_fact_receipt_sha256") != blocked.get("receipt_sha256")
        or consumption.get("original_source_fact_attempt") != "FAILED"
        or consumption.get("original_source_fact_reason_code") != "CPA_TEXT_REVIEW_INVALID"
        or not isinstance(consumption.get("title"), str)
        or not str(consumption.get("title") or "").strip()
        or not isinstance(consumption.get("selection_hook"), str)
        or not str(consumption.get("selection_hook") or "").strip()
    ):
        raise ValueError("DETERMINISTIC_TEXT_NARROWING_RECEIPT_INVALID")
    blocked_body = dict(blocked)
    declared_blocked_sha256 = blocked_body.pop("receipt_sha256", None)
    if (
        not isinstance(declared_blocked_sha256, str)
        or _sha256_json(blocked_body) != declared_blocked_sha256
    ):
        raise ValueError("DETERMINISTIC_TEXT_NARROWING_RECEIPT_INVALID")
    passes = blocked.get("passes")
    provider_retries = blocked.get("provider_retries")
    entity_context = blocked.get("entity_context")
    speaker_evidence = blocked.get("speaker_evidence")
    speaker_evidence_sha256 = blocked.get("speaker_evidence_sha256")
    if (
        not isinstance(passes, list)
        or not passes
        or any(not isinstance(row, Mapping) for row in passes)
        or not isinstance(provider_retries, list)
        or not isinstance(entity_context, Mapping)
        or not isinstance(speaker_evidence, Mapping)
        or not isinstance(speaker_evidence_sha256, str)
    ):
        raise ValueError("DETERMINISTIC_TEXT_NARROWING_RECEIPT_INVALID")
    return _finalize_receipt(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "decision": DETERMINISTIC_TEXT_NARROWING_PASS_DECISION,
            "reason_code": "SOURCE_FACT_DETERMINISTIC_TEXT_NARROWING",
            "original_selection_hook": blocked["original_selection_hook"],
            "original_title": blocked["original_title"],
            "final_selection_hook": consumption["selection_hook"],
            "final_title": consumption["title"],
            "passes": json.loads(json.dumps(passes, ensure_ascii=False)),
            "provider_retries": json.loads(json.dumps(provider_retries, ensure_ascii=False)),
            "entity_context": dict(entity_context),
            "speaker_evidence": dict(speaker_evidence),
            "speaker_evidence_sha256": speaker_evidence_sha256,
            "blocked_source_fact_review": json.loads(json.dumps(dict(blocked), ensure_ascii=False)),
            "deterministic_text_surface_resolution": dict(consumption),
        }
    )


def deterministic_text_narrowing_title_policy_exception_applies(
    review: object,
    *,
    candidate_id: str,
    title: str,
) -> bool:
    """Recognize only the exact sealed one-candidate 50-codepoint title.

    This structural check is intentionally insufficient to approve a package
    by itself.  Package builders/auditors must additionally call
    ``validate_source_fact_review`` against current SRT, speaker evidence, and
    story inputs.  It merely prevents the generic 49-character rule from
    rejecting the exact candidate-specific adjudication before that deeper
    replay can run.
    """

    if (
        candidate_id != DETERMINISTIC_TEXT_NARROWING_CANDIDATE_ID
        or title != DETERMINISTIC_TEXT_NARROWING_TITLE
        or len(title) != 50
        or not isinstance(review, Mapping)
        or review.get("decision") != DETERMINISTIC_TEXT_NARROWING_PASS_DECISION
        or review.get("final_title") != DETERMINISTIC_TEXT_NARROWING_TITLE
        or review.get("final_selection_hook") != DETERMINISTIC_TEXT_NARROWING_HOOK
    ):
        return False
    consumption = review.get("deterministic_text_surface_resolution")
    if (
        not isinstance(consumption, Mapping)
        or consumption.get("title") != DETERMINISTIC_TEXT_NARROWING_TITLE
        or consumption.get("selection_hook") != DETERMINISTIC_TEXT_NARROWING_HOOK
        or consumption.get("title_sha256") != _sha256_text(DETERMINISTIC_TEXT_NARROWING_TITLE)
        or consumption.get("selection_hook_sha256")
        != _sha256_text(DETERMINISTIC_TEXT_NARROWING_HOOK)
        or consumption.get("combined_surface_sha256")
        != DETERMINISTIC_TEXT_NARROWING_COMBINED_SHA256
    ):
        return False
    try:
        expected = authorize_deterministic_text_narrowing(consumption)
    except (TypeError, ValueError):
        return False
    return review == expected


def _validate_manual_title_keep_receipt(
    review: Mapping[str, object],
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
    speaker_evidence: object,
) -> bool:
    """Reload repository authority; never trust the package's consumption copy."""

    if (
        not candidate_id
        or final_reviewed_srt_path is None
        or speaker_evidence is _SPEAKER_EVIDENCE_UNSET
    ):
        return False
    try:
        authority = load_manual_title_keep_authority(candidate_id)
        if authority is None:
            return False
        consumption = validate_manual_title_keep_authority(
            authority,
            candidate_id=candidate_id,
            title=title,
            selection_hook=selection_hook,
            final_transcript=final_transcript,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            final_reviewed_srt_path=final_reviewed_srt_path,
            speaker_evidence=speaker_evidence,
            entity_context=resolve_source_fact_entity_context(
                candidate_id=candidate_id,
                final_reviewed_srt_path=final_reviewed_srt_path,
            ),
        )
        blocked = review.get("blocked_source_fact_review")
        if not isinstance(blocked, Mapping):
            return False
        expected = authorize_manual_title_keep(blocked, consumption=consumption)
    except (
        CandidateEntityProjectionError,
        ManualTitleKeepAuthorityError,
        OSError,
        ValueError,
    ):
        return False
    return review == expected


def _validate_deterministic_text_narrowing_receipt(
    review: Mapping[str, object],
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
    speaker_evidence: object,
) -> bool:
    """Rebuild the sealed closed plan against current package/runtime bytes."""

    if (
        candidate_id != DETERMINISTIC_TEXT_NARROWING_CANDIDATE_ID
        or final_reviewed_srt_path is None
        or speaker_evidence is _SPEAKER_EVIDENCE_UNSET
    ):
        return False
    try:
        authority = load_deterministic_text_surface_authority(candidate_id)
        if authority is None:
            return False
        entity_context = resolve_source_fact_entity_context(
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
        )
        binding = entity_context.get("candidate_binding")
        if not isinstance(binding, Mapping):
            return False
        consumption = consume_deterministic_text_surface_authority(
            authority,
            candidate_id=candidate_id,
            source_recording_basename=str(binding.get("source_recording_basename") or ""),
            source_sha256=str(binding.get("source_sha256") or ""),
            absolute_source_start_ms=int(binding.get("absolute_source_start_ms")),
            absolute_source_end_ms=int(binding.get("absolute_source_end_ms")),
            reviewed_srt_path=final_reviewed_srt_path,
            speaker_evidence=speaker_evidence,
            failed_source_fact_review=authority.failed_source_fact_receipt,
            original_title=str(authority.failed_source_fact_receipt.get("original_title") or ""),
            original_selection_hook=str(
                authority.failed_source_fact_receipt.get("original_selection_hook") or ""
            ),
            final_transcript=final_transcript,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            entity_context=entity_context,
        )
        expected = authorize_deterministic_text_narrowing(consumption)
    except (
        CandidateEntityProjectionError,
        DeterministicTextSurfaceResolutionError,
        OSError,
        TypeError,
        ValueError,
    ):
        return False
    return bool(
        review == expected
        and review.get("final_selection_hook") == selection_hook
        and review.get("final_title") == title
    )


def _validate_receipt_entity_context(
    review: Mapping[str, object],
    *,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
    clip_context_prompt: str = "",
) -> bool:
    """Rebuild the projection context instead of trusting its receipt copy."""

    if candidate_id is None and final_reviewed_srt_path is not None:
        return False
    try:
        context = _load_source_fact_entity_context(
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
            clip_context_prompt=clip_context_prompt,
        )
    except (
        CandidateEntityProjectionError,
        CandidatePublicTextSurfaceAuthorityError,
        OSError,
    ):
        return False
    passes = review.get("passes")
    if not isinstance(passes, list):
        return False
    if context is None:
        return bool(
            "entity_context" not in review
            and all(
                isinstance(row, Mapping) and "entity_context_sha256" not in row for row in passes
            )
        )
    if review.get("entity_context") != context.document:
        return False
    if any(
        not isinstance(row, Mapping) or row.get("entity_context_sha256") != context.context_sha256
        for row in passes
    ):
        return False
    surfaces: list[tuple[str, str]] = [
        (
            str(review.get("original_selection_hook") or ""),
            str(review.get("original_title") or ""),
        ),
        (
            str(review.get("final_selection_hook") or ""),
            str(review.get("final_title") or ""),
        ),
    ]
    for row in passes:
        assert isinstance(row, Mapping)
        hook = row.get("final_selection_hook")
        title = row.get("final_title")
        if isinstance(hook, str) and isinstance(title, str):
            surfaces.append((hook, title))
    return all(
        _entity_surface_error(
            context,
            selection_hook=hook,
            title=title,
        )
        is None
        for hook, title in surfaces
    )


def _validate_receipt_speaker_evidence(
    review: Mapping[str, object],
    *,
    speaker_evidence: object,
) -> bool:
    """Cross-link persisted pass hashes to freshly rebuilt package evidence.

    Omitting ``speaker_evidence`` is the deliberate in-process legacy mode: it
    accepts only receipts that predate the canonical evidence document and
    leaves their historical byte contract unchanged.  Package builders and
    auditors pass an explicit rebuilt document, which turns every nullable
    speaker binding into a checked three-state assertion.
    """

    passes = review.get("passes")
    if not isinstance(passes, list) or any(not isinstance(row, Mapping) for row in passes):
        return False
    top_level_fields_present = "speaker_evidence" in review or "speaker_evidence_sha256" in review
    pass_evidence_fields_present = any(
        "speaker_evidence_sha256" in row for row in passes if isinstance(row, Mapping)
    )
    if speaker_evidence is _SPEAKER_EVIDENCE_UNSET:
        return not top_level_fields_present and not pass_evidence_fields_present
    transcript = (
        speaker_evidence.get("speaker_transcript")
        if isinstance(speaker_evidence, Mapping) and speaker_evidence.get("state") == "PresentValid"
        else None
    )
    if transcript is not None and not isinstance(transcript, str):
        return False
    validated = _validated_speaker_evidence(
        speaker_evidence,
        speaker_transcript=transcript,
    )
    if validated is None:
        return False
    document, evidence_sha256 = validated
    expected_transcript_sha256 = (
        document.get("speaker_transcript_sha256")
        if document.get("state") == "PresentValid"
        else None
    )
    if any(
        row.get("speaker_transcript_sha256") != expected_transcript_sha256
        for row in passes
        if isinstance(row, Mapping)
    ):
        return False
    if not top_level_fields_present and not pass_evidence_fields_present:
        # Historical receipts bind only the transcript hash.  A package may
        # keep using one only when its freshly rebuilt present/absent state
        # reproduces that exact nullable hash across every pass.
        return True
    if not top_level_fields_present or not pass_evidence_fields_present:
        return False
    return bool(
        review.get("speaker_evidence") == document
        and review.get("speaker_evidence_sha256") == evidence_sha256
        and all(
            row.get("speaker_evidence_sha256") == evidence_sha256
            for row in passes
            if isinstance(row, Mapping)
        )
    )


def validate_source_fact_review(
    review: object,
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
    speaker_evidence: object = _SPEAKER_EVIDENCE_UNSET,
    qixi_repo_root: Path | None = None,
    story_contract: Mapping[str, object] | None = None,
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
    if review.get("decision") == QIXI_TERMINAL_TEXT_PRESERVATION_DECISION:
        if candidate_id is None or final_reviewed_srt_path is None:
            return False
        return validate_terminal_preservation_source_fact_review(
            review,
            repo_root=qixi_repo_root or _REPO_ROOT,
            final_reviewed_srt_path=final_reviewed_srt_path,
            final_transcript=final_transcript,
            title=title,
            selection_hook=selection_hook,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            candidate_id=candidate_id,
            speaker_evidence=speaker_evidence,
        )
    if review.get("decision") == "CANDIDATE_PUBLIC_TEXT_SOURCE_FACT_REFRESH":
        return validate_candidate_public_text_source_fact_refresh_from_source_fact(
            review, repo_root=qixi_repo_root or _REPO_ROOT, validation=locals()
        )
    if review.get("decision") == C3_TERMINAL_SOURCE_FACT_SUPERSESSION_DECISION:
        return validate_c3_source_fact_review_from_validation(review, validation=locals())
    if review.get("decision") == MANUAL_TITLE_KEEP_PASS_DECISION:
        return _validate_manual_title_keep_receipt(
            review,
            selection_hook=selection_hook,
            title=title,
            final_transcript=final_transcript,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
            speaker_evidence=speaker_evidence,
        )
    if review.get("decision") == DETERMINISTIC_TEXT_NARROWING_PASS_DECISION:
        return _validate_deterministic_text_narrowing_receipt(
            review,
            selection_hook=selection_hook,
            title=title,
            final_transcript=final_transcript,
            clip_context_prompt=clip_context_prompt,
            selection_scorecard=selection_scorecard,
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
            speaker_evidence=speaker_evidence,
        )
    if not _validate_receipt_entity_context(
        review,
        candidate_id=candidate_id,
        final_reviewed_srt_path=final_reviewed_srt_path,
        clip_context_prompt=clip_context_prompt,
    ):
        return False
    if not _validate_receipt_speaker_evidence(
        review,
        speaker_evidence=speaker_evidence,
    ):
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


def validate_source_fact_rescore_candidate_receipt(
    review: object,
    *,
    selection_hook: str,
    title: str,
    selection_scorecard: object = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
) -> bool:
    """Recheck a REPAIR_SCORECARD_STALE receipt's ``rescore_candidate`` block.

    Deliberately a sibling of ``validate_source_fact_review`` rather than an
    extension of it: that function's every existing call site treats a
    ``True`` result as "this receipt is a deliverable PASS", gated by
    ``source_fact_review_passes`` (status == "PASS").  A FAILED/
    REPAIR_SCORECARD_STALE receipt must never validate as deliverable through
    that gate, so the bounded rescore lane gets its own narrow verifier
    instead of widening the shared one's contract.
    """

    if not isinstance(review, Mapping):
        return False
    receipt = dict(review)
    declared_receipt_sha256 = receipt.pop("receipt_sha256", None)
    if not isinstance(declared_receipt_sha256, str):
        return False
    if _finalize_receipt(receipt).get("receipt_sha256") != declared_receipt_sha256:
        return False
    if not _validate_receipt_entity_context(
        review,
        candidate_id=candidate_id,
        final_reviewed_srt_path=final_reviewed_srt_path,
    ):
        return False
    if (
        review.get("schema_version") != SCHEMA_VERSION
        or review.get("status") != "FAILED"
        or review.get("decision") != "REPAIR_SCORECARD_STALE"
        or review.get("reason_code") != "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE"
        or review.get("final_selection_hook") != selection_hook
        or review.get("final_title") != title
    ):
        return False
    passes = review.get("passes")
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
        return False
    last_pass = passes[-1]
    if last_pass.get("status") != "REPAIR" or last_pass.get(
        "selection_scorecard_sha256"
    ) != _sha256_json(selection_scorecard):
        return False
    block = review.get("rescore_candidate")
    if not isinstance(block, Mapping):
        return False
    repaired_hook = last_pass.get("final_selection_hook")
    repaired_title = last_pass.get("final_title")
    return bool(
        block.get("schema_version") == RESCORE_CANDIDATE_SCHEMA_VERSION
        and isinstance(repaired_hook, str)
        and block.get("repaired_selection_hook") == repaired_hook
        and block.get("repaired_selection_hook_sha256") == _sha256_text(repaired_hook)
        and isinstance(repaired_title, str)
        and block.get("repaired_title") == repaired_title
        and block.get("repaired_title_sha256") == _sha256_text(repaired_title)
        and block.get("stale_selection_scorecard_sha256") == _sha256_json(selection_scorecard)
        and block.get("selection_scorecard_review") == last_pass.get("selection_scorecard_review")
    )
