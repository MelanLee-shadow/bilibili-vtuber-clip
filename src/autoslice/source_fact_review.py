"""Joint CPA text gate for source-bound facts in hooks and titles."""

from __future__ import annotations

from src.autoslice.structured_output import call_json
from src.autoslice import title_policy

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.completed_review_checkpoint import (
    CompletedReviewCheckpoint,
    CompletedReviewCheckpointError,
)

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
from src.autoslice.channel_envelope import leading_envelope_preserved
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
from src.autoslice.llm_client import LlmCall, LlmJsonParseError, extract_json_object
from src.autoslice.provider_failure import describe_provider_exception
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
from src.autoslice.source_fact_rescore_candidate_validation import (
    source_fact_rescore_candidate_shape_is_valid,
)
from src.autoslice.source_fact_review_shape import source_fact_review_passes_shape
from src.autoslice.surface_canon import (
    CHANNEL_PROFILE, _hard_meme_canon_prompt_block,
    canonicalize_hard_meme_surfaces,
)
from src.autoslice.story_contract import public_text_relation_prompt
from src.autoslice.title_policy import publish_title_policy_violations
SCHEMA_VERSION = "lidousha-source-fact-review.v1"
RESCORE_CANDIDATE_SCHEMA_VERSION = "source-fact-rescore-candidate.v1"
ENTITY_CONTEXT_SCHEMA_VERSION = "source-fact-entity-context.v1"

# Existing read-only editorial research is projected into normal staging/tag
# receipts as method provenance. These references identify the already
# consumed source material; production must not read the report tree as a
# runtime input or treat its prose as factual authority.
EDITORIAL_RESEARCH_METHOD_PROVENANCE: tuple[dict[str, str], ...] = (
    {
        "id": "research-source-manifest",
        "schema": "research-source-consumption.v2",
        "version": "research-source-consumption.v2",
        "path": (
            "reports/lidousha-performance-20261007/title-packaging-followup/"
            "active-editorial-consumption/source-manifest.json"
        ),
        "sha256": "sha256:64c138eef0ff239aec284e0a8101fc0efc05312e95a09d1e4a41543c8c7c943d",
        "role": "bounded_method_and_source_consumption_manifest",
    },
    {
        "id": "candidate-editorial-applications",
        "schema": "research-editorial-applications.v1",
        "version": "research-editorial-applications.v1",
        "path": (
            "reports/lidousha-performance-20261007/title-packaging-followup/"
            "active-editorial-consumption/candidate-applications.json"
        ),
        "sha256": "sha256:ce7c96aed115e7787d53b5b99e1d627a6f5ffad7fbc60e82f3f6070bc62c8561",
        "role": "source_bound_candidate_comparison_method",
    },
    {
        "id": "e04-official-work-authority",
        "schema": "official-source-reference.v1",
        "version": "official-source-reference.v1",
        "url": "https://www.shinchosha.co.jp/book/105049/",
        "role": "E04_春の雪_and_三島由纪夫_source_check",
    },
)
MAX_REVIEW_PASSES = 5
# 程度升级词面：狍哥案（§7）机器可复核部分——degree 槽只准逐字或
# 降级，不得升级；升级词若不在 before 原文也不在任何 evidence 原文中出现，
# 判定该 changed_surface 无效（CPA_TEXT_REVIEW_INVALID），逼判者要么不升级
# 要么必须逐字引用来源。这条不依赖 claim_decomposition 字段本身，纯词面
# 比对，保持对既有回执/测试 fixture 的完全向后兼容。
_DEGREE_UPGRADE_WORDS = ("最", "所有", "永远", "绝对", "彻底", "唯一", "一定", "必然")
# 同一 review pass 内的 provider 级重试帽：这道门是 LLM 采样，同一份输入一次
# 形状无效就把整次产线判死是把骰子当结论。重试只针对 provider 层
# 形状失败，语义结果（KEEP/REPAIR）永不重掷；UNAVAILABLE
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


def _editorial_research_from_scorecard(value: object) -> Mapping[str, object] | None:
    """Return the already-bound research input carried by the scorecard.

    The title/tag consumer may use completed research to choose among source
    supported hooks.  This review only records and judges that input; it never
    treats a research recommendation as transcript evidence.
    """

    if not isinstance(value, Mapping):
        return None
    research = value.get("editorial_research")
    return research if isinstance(research, Mapping) else None


_SOURCE_ENTITY_ROLES = frozenset({"author", "work", "person", "character", "group"})
_SOURCE_ENTITY_MAX_ROWS = 12
_SOURCE_ENTITY_MAX_SURFACES = 8
_SOURCE_ENTITY_MAX_EVIDENCE = 8


def _subtitle_text_body(value: str) -> str:
    """Return subtitle text without SRT indexes and timestamp lines."""

    rows: list[str] = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line or line.isdigit() or re.fullmatch(
            r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}(?:\s+.*)?",
            line,
        ):
            continue
        rows.append(line)
    return "\n".join(rows)


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
    operator_fact = context.document.get("operator_public_fact_authority")
    if isinstance(operator_fact, Mapping):
        return (
            "候选级人工事实纠正（已验证并绑定本 candidate、源区间与原话）：\n"
            f"entity_context_sha256: {context.context_sha256}\n"
            + json.dumps(operator_fact, ensure_ascii=False, sort_keys=True)
            + "\n只将原话明确纠正的事件事实作为本片派生文案的事实依据，"
            "归入 same_clip_context；不得因为它未逐字出现在字幕或弹幕中就将其抹去。"
            "这不是仅有专名拼写的约束，也不是无条件批准整条标题。"
            "其余事件、因果、程度仍须逐项由同片证据支持；"
            "该原话不授权修改最终字幕、说话人、边界或发布状态。\n"
        )
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


def _prompt(
    *,
    selection_hook: str,
    title: str,
    final_transcript: str,
    clip_context_prompt: str,
    selection_scorecard: object,
    review_pass: int,
    enforce_automatic_title_style: bool,
    title_policy_violations: list[str],
    speaker_transcript: str | None,
    entity_context: _ResolvedEntityContext | None,
    story_contract: Mapping[str, object] | None = None,
) -> str:
    fluency_prompt = (
        "自动文案审查的通顺要求：事实支持不等于标题通顺；标题或钩子即使每个词都有来源，"
        "若主语、指代、转折或引语边界仍要靠读者把并列片段自行拼接，必须 REPAIR 对应的 "
        "title 或 selection_hook。修复只能重排、连接或删去重复的现有事实，不得新增事实，"
        "也不一律禁用引号、问号或口语省略；不强制书面语或三拍结构。只有两份文案的事实、模态"
        "和中文句法都成立时，status=KEEP，两个 final 字段才必须逐字等于输入。\n"
        if enforce_automatic_title_style
        else (
            "当前是 baseline/manual 文案审查：继续检查事实、模态、角色、条件和因果；"
            "只有这些内容成立时才 KEEP，不因纯句法风格偏好改写精确手定的 title 或 selection_hook。\n"
        )
    )
    editorial_research = _editorial_research_from_scorecard(selection_scorecard)
    research_block = ""
    if editorial_research is not None:
        if str(editorial_research.get("status") or "").upper() == "INAPPLICABLE":
            research_intro = (
                "编辑研究输入明确标记为 INAPPLICABLE：当前普通记录没有已部署、"
                "适用于本片的 source-specific 研究投影。以下 source/version/hash、"
                "applicability 与 inapplicable_reason 只用于披露缺口，不是 source-fact "
                "authority；不要读取未部署 reports，也不要把缺口当作研究结论。\n"
            )
        else:
            candidate_projection = editorial_research.get("candidate_projection")
            if (
                isinstance(candidate_projection, Mapping)
                and str(candidate_projection.get("status") or "").upper()
                == "INAPPLICABLE"
            ):
                research_intro = (
                    "已部署的通用标题/tag 编辑方法适用于本条，但当前记录没有绑定本片的 "
                    "source-specific candidate application；candidate_projection 只披露这层缺口。"
                    "研究输入不是 source-fact authority，不能代替最终字幕/同片上下文；不要读取未部署 reports，"
                    "也不要把缺口当作事实或研究结论。\n"
                )
            else:
                research_intro = (
                    "编辑研究输入（仅用于核对候选取舍；不是 source-fact authority，不能代替最终字幕/"
                    "同片上下文，也不要求把研究词写入标题）：\n"
                )
        research_block = (
            research_intro
            + json.dumps(dict(editorial_research), ensure_ascii=False, sort_keys=True)
            + "\n"
        )
    return (
        f"你是{CHANNEL_PROFILE.display_name}切片派生文案的 source-fact 最终裁决者。你只有文字输入，"
        "不要声称听见音频或看见画面。一次联合裁决 selection_hook 与投稿标题，"
        "不是字幕改写任务。\n"
        + _hard_meme_canon_prompt_block()
        + "判断两份文案里的每个具体事件、对象、因果、身份、专名、事实模态和同音"
        "释义，是否能由最终字幕或同片 hash-bound 结构化弹幕/SC/上下文支持。"
        "允许不逐字的自然概括，但不允许把提议写成既成事实、把猜测写成断言，"
        "也不允许凭空把同音词换成另一个含义。\n"
        "念出的文字不等于主播本人的经历、想法或立场。游戏台词、角色内心独白、"
        "视频对白、观众弹幕和 SC 引文，都须分别核对原话的叙事主体与主播自己的"
        "反应；uniform_host 只说明显示样式，不证明这些句子的语义主体是主播。"
        "第一人称‘我’或‘觉得’不能单独支持‘主播以为/脑补/想要’这样的文案。"
        "结合邻近 cue 中的角色称呼、朗读与评论转换核对归属；缺少归属证据时"
        "保留为游戏/视频/观众的内容，或收窄到有证据的主播反应，不得默认安到"
        "主播本人头上。受话人标签不可验证也不免除这项叙事主体核对。\n"
        "投稿标题必须脱离 selection_hook 和内部上下文也能辨认事件属于现实、游戏、"
        "视频或其他作品。若关系、对白或经历属于作品角色，标题本身须保留理解必需的"
        "游戏/作品/角色限定；钩子已写游戏、标题没直接说是主播本人、或使用‘上帝视角’"
        "都不能补足这个限定。省略会让陌生读者误读成现实关系时，必须 REPAIR 标题，"
        "不能因原句确实念过而 KEEP。只写主播自身有证据的反应、没有借用作品事件的"
        "标题不机械追加场景词；现实经历同样不得凭空改成游戏剧情。\n"
        + _entity_context_prompt_block(entity_context)
        + public_text_relation_prompt(story_contract)
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
        + fluency_prompt
        + "还要检查省略是否改变了语法角色、条件或因果：来源若是“今天是某人的生日，"
        "转发这条信息才能得奖”，不得概括为“转发某人的生日能得奖”或“生日能得奖”；"
        "必须保留“消息/信息”作为转发宾语，并保留转发这一获奖条件。"
        "若任一不受支持，status=REPAIR，并给出完整、可直接替换、只使用现有"
        "source 事实的 final_selection_hook 与 final_title；标题必须保留原有"
        "频道前缀。禁止只解释问题却不给两份完整文案，也禁止添加 evidence 中没有"
        "的新事实。修复后会继续用同一份 source authority 复审；只要仍有受证据"
        "支持的改动，就可以继续 REPAIR，直到明确 KEEP。最多复审 5 轮；不得为了"
        "结束复审而放弃仍然存在的事实问题，也不得在不同文案之间来回振荡。\n"
        + addressee_prompt_block(speaker_transcript)
        + f"投稿标题总长度必须为 {title_policy._TITLE_MIN_LEN}–{title_policy._TITLE_MAX_LEN} 个字符并满足下方 deterministic title policy。"
        "若 violations 非空，当前标题不能 KEEP；必须在不丢失核心事实与事实模态的"
        "前提下压缩或修正完整标题，并用 REPAIR 返回。只有 violations 为空且所有"
        "source facts 都正确时才能 KEEP。\n"
        "deterministic_title_policy_violations: "
        + json.dumps(title_policy_violations, ensure_ascii=False)
        + "\n"
        "可选 source_entity_candidates 是给后续普通标签层的候选提名，不是最终标签决定。"
        "只有最终字幕逐字出现、且当前 source-fact 正文明确说明其语义角色的作者、作品、"
        "人物、角色或团体才能提名；role 只能是 author、work、person、character、group。"
        "tag、surfaces、evidence 必须逐字复制 final_transcript 中的现有文字。不要从"
        "identity/spelling-only entity_context、长期记忆、topic graph 或研究 prose 猜名字，"
        "不要把未在最终字幕出现的名字补进来。候选不需要决定 primary/necessary/incidental；"
        "后续 tags CPA 会逐项 INCLUDE/EXCLUDE 并作相关性判断。没有合格候选时返回空数组。\n"
        f"selection_hook:\n{selection_hook}\n"
        f"title:\n{title}\n"
        f"最终字幕（文字 authority）:\n{final_transcript}\n"
        f"同片 hash-bound 上下文（含按时间排列的结构化弹幕/SC）:\n"
        f"{clip_context_prompt}\n"
        + research_block
        + "原 selection_scorecard:\n"
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
        '"source_entity_candidates":[{"tag":"逐字专名","role":"author|work|person|character|group",'
        '"status":"CONFIRMED","surfaces":["逐字表面"],"evidence":["final_transcript 中的逐字证据"]}],'
        '"summary":"..."}'
    )


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def _source_entity_candidates_from_response(
    value: object,
    *,
    final_transcript: str,
    final_reviewed_srt_text: str | None,
    final_reviewed_srt_sha256: str | None,
    final_reviewed_srt_path: Path | None,
    candidate_id: str | None,
    request_sha256: str,
    response_sha256: str,
    clip_context_prompt_sha256: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Normalize source-fact nominations without becoming a name recognizer.

    The CPA may nominate a typed literal, but only the current final transcript
    and current reviewed SRT can establish that the literal was actually
    delivered.  ``source`` is generated here from those current bytes and the
    exact CPA request/response hashes; provider-supplied source metadata is
    deliberately ignored.
    """

    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["source-entity-candidates-not-a-list"]
    if not final_reviewed_srt_text or not final_reviewed_srt_sha256:
        return [], ["source-entity-candidates-final-srt-unavailable"]
    if not all(
        _SHA256_VALUE_RX.fullmatch(str(binding or ""))
        for binding in (final_reviewed_srt_sha256, request_sha256, response_sha256, clip_context_prompt_sha256)
    ):
        return [], ["source-entity-candidates-binding-hash-invalid"]

    transcript_compact = _compact(final_transcript)
    subtitle_compact = _compact(_subtitle_text_body(final_reviewed_srt_text))
    source_path = str(final_reviewed_srt_path) if final_reviewed_srt_path is not None else ""
    source = {
        "path": source_path,
        "sha256": final_reviewed_srt_sha256,
        "source_kind": "source_fact_cpa_final_srt",
        "candidate_id": str(candidate_id or ""),
        "srt_sha256": final_reviewed_srt_sha256,
        "final_transcript_sha256": _sha256_text(final_transcript),
        "clip_context_prompt_sha256": clip_context_prompt_sha256,
        "request_sha256": request_sha256,
        "response_sha256": response_sha256,
    }
    rows: list[dict[str, object]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value[:_SOURCE_ENTITY_MAX_ROWS]):
        prefix = f"source-entity-candidate-{index}"
        if not isinstance(raw, Mapping):
            rejected.append(f"{prefix}:row")
            continue
        tag = str(raw.get("tag") or "").strip()
        role = str(raw.get("role") or "").strip().lower()
        status = str(raw.get("status") or "CONFIRMED").strip().upper()
        surfaces_value = raw.get("surfaces")
        evidence_value = raw.get("evidence")
        surfaces = (
            [str(item).strip() for item in surfaces_value[:_SOURCE_ENTITY_MAX_SURFACES] if str(item).strip()]
            if isinstance(surfaces_value, list)
            else []
        )
        evidence = (
            [str(item).strip() for item in evidence_value[:_SOURCE_ENTITY_MAX_EVIDENCE] if str(item).strip()]
            if isinstance(evidence_value, list)
            else []
        )
        if (
            not tag
            or any(ch in tag for ch in ",，\n\t")
            or role not in _SOURCE_ENTITY_ROLES
            or status not in {"CONFIRMED", "SOURCE_CONFIRMED"}
            or not surfaces
            or not evidence
        ):
            rejected.append(f"{prefix}:shape")
            continue
        key = tag.casefold()
        if key in seen:
            rejected.append(f"{prefix}:duplicate")
            continue
        if _compact(tag) not in transcript_compact or _compact(tag) not in subtitle_compact:
            rejected.append(f"{prefix}:tag-not-in-current-final-srt-and-transcript")
            continue
        matched_surfaces = [
            surface
            for surface in surfaces
            if _compact(surface) in transcript_compact and _compact(surface) in subtitle_compact
        ]
        if not matched_surfaces:
            rejected.append(f"{prefix}:surface-not-in-current-final-srt-and-transcript")
            continue
        if any(
            _compact(item) not in transcript_compact or _compact(item) not in subtitle_compact
            for item in evidence
        ):
            rejected.append(f"{prefix}:evidence-not-in-current-final-srt-and-transcript")
            continue
        if not any(
            any(_compact(surface) in _compact(item) for surface in matched_surfaces)
            for item in evidence
        ):
            rejected.append(f"{prefix}:evidence-does-not-name-candidate")
            continue
        row: dict[str, object] = {
            "tag": tag,
            "role": role,
            # Existing publish/tag consumers accept CONFIRMED/VERIFIED.  The
            # source_kind below records that this confirmation came from the
            # current source-fact response rather than an external whitelist.
            "status": "CONFIRMED",
            "surfaces": surfaces,
            "matched_surfaces": matched_surfaces,
            "evidence": evidence,
            "source": dict(source),
        }
        why = str(raw.get("why") or "").strip()
        if why:
            row["why"] = why[:512]
        rows.append(row)
        seen.add(key)
    return rows, rejected


def _final_reviewed_srt_snapshot(
    path: Path | None,
) -> tuple[str | None, str | None]:
    """Read the exact final subtitle bytes used to bind source entities.

    A missing snapshot keeps the historical source-fact lane usable, but it
    also means an entity nomination cannot enter the normal candidate pool.
    The caller records the rejection on the receipt instead of treating a
    provider supplied name as authority.
    """

    if path is None:
        return None, None
    try:
        raw = read_regular_no_symlink(Path(path))
        return raw.decode("utf-8"), "sha256:" + hashlib.sha256(raw).hexdigest()
    except (ManualTitleKeepAuthorityError, UnicodeDecodeError):
        return None, None


def _source_entity_receipt_fields(
    review: Mapping[str, object] | None,
) -> dict[str, object]:
    """Carry the final provider pass's normalized entity rows into the receipt."""

    if not isinstance(review, Mapping):
        return {}
    fields: dict[str, object] = {}
    if "source_entity_candidates" in review:
        rows = review.get("source_entity_candidates")
        fields["source_entity_candidates"] = (
            [dict(row) for row in rows if isinstance(row, Mapping)]
            if isinstance(rows, list)
            else []
        )
    if "source_entity_candidate_rejections" in review:
        rejections = review.get("source_entity_candidate_rejections")
        fields["source_entity_candidate_rejections"] = (
            [str(item) for item in rejections if str(item).strip()]
            if isinstance(rejections, list)
            else []
        )
    return fields


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
    story_contract: Mapping[str, object] | None = None,
    candidate_id: str | None = None,
    final_reviewed_srt_path: Path | None = None,
    final_reviewed_srt_text: str | None = None,
    final_reviewed_srt_sha256: str | None = None,
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
        enforce_automatic_title_style=enforce_automatic_title_style,
        title_policy_violations=title_policy_violations,
        speaker_transcript=speaker_transcript,
        entity_context=entity_context,
        story_contract=story_contract,
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
    editorial_research = _editorial_research_from_scorecard(selection_scorecard)
    if editorial_research is not None:
        base["editorial_research"] = dict(editorial_research)
        base["editorial_research_sha256"] = _sha256_json(editorial_research)
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
        inputs = {"candidate_id": candidate_id,
                  "review_input": {key: value for key, value in base.items() if key != 'review_pass'},
                  "final_reviewed_srt_sha256": final_reviewed_srt_sha256,
                  "source_binding": {
                      key: story_contract.get(key)
                      for key in ("schema_version", "candidate_id", "source_media_sha256s",
                                  "clip_context_binding", "boundary_semantic_review",
                                  "human_boundary_authority", "session_relation_authority")
                  } if isinstance(story_contract, Mapping) else None}
        observation_root = (final_reviewed_srt_path.parent / ".completed-review-observations"
                            if final_reviewed_srt_path is not None else None)
        completed_review = CompletedReviewCheckpoint.for_call(
            stage="source-fact-review", prompt=prompt, llm_call=llm_call,
            inputs=inputs, parser_path=Path(__file__), root=observation_root,
        )
        if completed_review is None:
            completed_review = CompletedReviewCheckpoint.for_ruling(
                stage='source-fact-review', prompt=prompt, llm_call=llm_call,
                inputs=inputs, parser_path=Path(__file__), root=observation_root)
        raw = (completed_review.response(lambda prompt: call_json(llm_call, prompt)) if completed_review is not None
               else call_json(llm_call, prompt, reuse=False))
    except CompletedReviewCheckpointError as exc:
        return {**base, "status": "FAILED", "reason_code": str(exc), "failure_layer": "CONTRACT_ERROR"}
    except Exception as exc:
        diagnostics = describe_provider_exception(exc)
        return {**base, "status": "FAILED", "reason_code": "CPA_TEXT_REVIEW_CALL_FAILED", **diagnostics}
    try:
        payload = extract_json_object(raw)
    except LlmJsonParseError:
        return {**base, "status": "FAILED", "reason_code": "CPA_TEXT_REVIEW_INVALID",
                "response_sha256": _sha256_text(raw), "failure_layer": "CONTRACT_ERROR"}
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
    title_prefix_preserved = leading_envelope_preserved(title, final_title)
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
    response_sha256 = _sha256_text(raw)
    source_entity_candidates, source_entity_candidate_rejections = (
        _source_entity_candidates_from_response(
            payload.get("source_entity_candidates"),
            final_transcript=final_transcript,
            final_reviewed_srt_text=final_reviewed_srt_text,
            final_reviewed_srt_sha256=final_reviewed_srt_sha256,
            final_reviewed_srt_path=final_reviewed_srt_path,
            candidate_id=candidate_id,
            request_sha256=str(base["request_sha256"]),
            response_sha256=response_sha256,
            clip_context_prompt_sha256=str(base["clip_context_prompt_sha256"]),
        )
        if "source_entity_candidates" in payload
        else ([], [])
    )
    result: dict[str, object] = {
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
        "response_sha256": response_sha256,
    }
    # Primitive protocol incompleteness is engineering failure. Do not label
    # a complete but unsupported source/content claim as a contract error.
    if (not shape_valid
            and not (status in {'KEEP', 'REPAIR'} and isinstance(final_hook, str)
                     and isinstance(final_title, str) and isinstance(supported_by, list)
                     and isinstance(changes, list) and isinstance(summary, str))):
        result['failure_layer'] = 'CONTRACT_ERROR'
    if "source_entity_candidates" in payload:
        if final_reviewed_srt_sha256 is not None:
            result["final_reviewed_srt_sha256"] = final_reviewed_srt_sha256
        result["source_entity_candidates"] = source_entity_candidates
        if source_entity_candidate_rejections:
            result["source_entity_candidate_rejections"] = source_entity_candidate_rejections
    if shape_valid and completed_review is not None:
        try:
            completed_review.save()
        except CompletedReviewCheckpointError as exc:
            return {**base, "status": "FAILED", "reason_code": str(exc),
                    "response_sha256": response_sha256}
    return result


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
    story_contract: Mapping[str, object] | None = None,
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
    final_reviewed_srt_text, final_reviewed_srt_sha256 = _final_reviewed_srt_snapshot(
        final_reviewed_srt_path
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
                story_contract=story_contract,
                candidate_id=candidate_id,
                final_reviewed_srt_path=final_reviewed_srt_path,
                final_reviewed_srt_text=final_reviewed_srt_text,
                final_reviewed_srt_sha256=final_reviewed_srt_sha256,
            )
            if (
                review.get("status") != "FAILED"
                or review.get('failure_layer') == 'CONTRACT_ERROR'
                or bool(review.get('response_sha256'))
                or bool(getattr(llm_call, 'cpa_cache_identity', None))
                or review.get("reason_code")
                not in {
                    "CPA_TEXT_REVIEW_INVALID",
                    "CPA_ENTITY_SURFACE_RESPONSE_INVALID",
                    # Legacy adapter codes remain unchanged. A complete
                    # original response is stopped above, never re-asked.
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
                    **_source_entity_receipt_fields(review),
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
                    **({'failure_layer': review['failure_layer']} if review.get('failure_layer') else {}),
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
            # Keep the repaired hook in a top-level rescore_candidate block so
            # downstream bounded rescoring does not depend on passes[-1].
            # final_selection_hook/title still retain the originals; only this
            # block carries proposed text, preserving no-unapproved-write.
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
    or turn 维护者's broad publication quote into free-form title authority.
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


def _validate_receipt_source_entity_candidates(
    review: Mapping[str, object],
    *,
    final_transcript: str,
    candidate_id: str | None,
    final_reviewed_srt_path: Path | None,
) -> bool:
    """Rebuild source-fact entity rows against the current final SRT bytes."""

    passes = review.get("passes")
    if not isinstance(passes, list) or any(not isinstance(row, Mapping) for row in passes):
        return False
    candidate_fields_present = any(
        "source_entity_candidates" in row or "final_reviewed_srt_sha256" in row
        for row in passes
        if isinstance(row, Mapping)
    )
    if "source_entity_candidates" in review:
        candidate_fields_present = True
        final_pass = passes[-1]
        if (
            not isinstance(final_pass, Mapping)
            or "source_entity_candidates" not in final_pass
            or not isinstance(review.get("source_entity_candidates"), list)
            or not isinstance(final_pass.get("source_entity_candidates"), list)
            or review.get("source_entity_candidates") != final_pass.get("source_entity_candidates")
        ):
            return False
        if "source_entity_candidate_rejections" in review and (
            review.get("source_entity_candidate_rejections")
            != final_pass.get("source_entity_candidate_rejections", [])
        ):
            return False
    if not candidate_fields_present:
        return "source_entity_candidates" not in review
    candidate_rows_present = any(
        isinstance(row, Mapping)
        and isinstance(row.get("source_entity_candidates"), list)
        and bool(row.get("source_entity_candidates"))
        for row in passes
    )
    srt_binding_present = any(
        isinstance(row, Mapping) and "final_reviewed_srt_sha256" in row for row in passes
    )
    # An explicit empty array is a valid ordinary-provider answer.  If this
    # legacy call had no reviewed SRT at all, there is no entity authority to
    # validate and no candidate to project; preserve the old receipt lane.
    if not candidate_rows_present and not srt_binding_present:
        return True
    srt_text, srt_sha256 = _final_reviewed_srt_snapshot(final_reviewed_srt_path)
    if srt_text is None or srt_sha256 is None:
        return False
    for row in passes:
        assert isinstance(row, Mapping)
        if "final_reviewed_srt_sha256" in row and row.get("final_reviewed_srt_sha256") != srt_sha256:
            return False
        if "source_entity_candidates" not in row:
            continue
        if any(
            not _SHA256_VALUE_RX.fullmatch(str(row.get(key) or ""))
            for key in ("request_sha256", "response_sha256", "clip_context_prompt_sha256")
        ):
            return False
        candidates = row.get("source_entity_candidates")
        if not isinstance(candidates, list):
            return False
        expected, rejected = _source_entity_candidates_from_response(
            candidates,
            final_transcript=final_transcript,
            final_reviewed_srt_text=srt_text,
            final_reviewed_srt_sha256=srt_sha256,
            final_reviewed_srt_path=final_reviewed_srt_path,
            candidate_id=candidate_id,
            request_sha256=str(row.get("request_sha256") or ""),
            response_sha256=str(row.get("response_sha256") or ""),
            clip_context_prompt_sha256=str(row.get("clip_context_prompt_sha256") or ""),
        )
        # The captured request/response and entity receipt stay immutable when
        # the identical final SRT is copied into a delivery package.  Its path
        # is a locator, while the current SRT bytes, candidate, transcript and
        # every evidence/binding field are reconstructed above.  Keep the
        # original locator rather than relabeling an old answer as a new call.
        if not rejected and len(expected) == len(candidates):
            for rebuilt, captured in zip(expected, candidates, strict=True):
                captured_source = captured.get("source")
                if (
                    isinstance(captured_source, Mapping)
                    and isinstance(captured_source.get("path"), str)
                    and captured_source["path"]
                ):
                    rebuilt["source"]["path"] = captured_source["path"]
        if rejected or expected != candidates:
            return False
    return True

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
    if isinstance(review, Mapping) and review.get("schema_version") == "b2-caption-source-fact-successor.v1":
        from src.autoslice.b2_caption_formal_successor import validate_source_fact_from_scope
        return bool(validate_source_fact_from_scope(locals()))
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
    if not _validate_receipt_source_entity_candidates(
        review,
        final_transcript=final_transcript,
        candidate_id=candidate_id,
        final_reviewed_srt_path=final_reviewed_srt_path,
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
    """Recheck the bounded rescore receipt without treating it as a PASS."""

    if not isinstance(review, Mapping):
        return False
    receipt = dict(review)
    declared_receipt_sha256 = receipt.pop("receipt_sha256", None)
    if (
        not isinstance(declared_receipt_sha256, str)
        or _finalize_receipt(receipt).get("receipt_sha256") != declared_receipt_sha256
        or not _validate_receipt_entity_context(
            review,
            candidate_id=candidate_id,
            final_reviewed_srt_path=final_reviewed_srt_path,
        )
    ):
        return False
    return source_fact_rescore_candidate_shape_is_valid(
        review,
        selection_hook=selection_hook,
        title=title,
        selection_scorecard_sha256=_sha256_json(selection_scorecard),
        schema_version=SCHEMA_VERSION,
        rescore_candidate_schema_version=RESCORE_CANDIDATE_SCHEMA_VERSION,
    )
