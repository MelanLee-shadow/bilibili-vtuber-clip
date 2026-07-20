"""Transcript, structured-chat, entity, and final-review stage for the producer."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from src.autoslice.chat_authority import (
    ChatEvidence,
    ReferentGroup,
    apply_audio_entity_verification,
    apply_authoritative_chat_evidence,
    build_human_text_entity_verifier,
    clip_opening_address_group,
    introduced_term_cues,
    load_clip_opening_address_config,
    load_referent_groups,
    normalize_code_switch_surfaces,
    normalize_hard_meme_surfaces,
    reconcile_contradictory_entity_repairs,
    registered_entity_names,
    repetition_divergence_groups,
    revert_unregistered_entity_repairs,
    sanitize_chat_display_text,
    witness_disagreement_cues,
)
from src.autoslice.danmaku_evidence import DanmakuItem
from src.autoslice.final_review_auditor import (
    MAX_CONTEXT_ADJUDICATIONS,
    adjudicate_context_finding,
    audit_final_subtitles,
    persist_review_audit,
    route_findings,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call, extract_json_object
from src.autoslice.producer_chat_input import (
    DANMAKU_PRE_CONTEXT_MS,
    GIFT_PRE_CONTEXT_MS,
    SC_PRE_CONTEXT_MS,
    _load_independent_chat_support_srts,
    _piece_chat_evidence,
)
from src.autoslice.producer_text_finalization import _render_cues_to_srt
from src.autoslice.song_name_pin import pin_song_names_in_srt
from src.autoslice.foreign_span_witness import (
    retranscribe_foreign_script_cluster,
    witness_foreign_script_audit,
    witness_language_preservation_audit,
)
from src.autoslice.self_reference_absorption import absorb_host_self_references
from src.autoslice.session_topic_authority import (
    absorb_session_topic_entities,
    discover_session_topic_authorities,
)
from src.autoslice.source_subtitle_truth import apply_source_subtitle_truth
from src.autoslice.subtitle_timing_qa import build_ssh_silero_vad_provider
from src.autoslice.subtitle_fidelity import (
    apply_numeric_fact_provenance_guard,
    apply_impossible_punctuation_guard,
    apply_source_language_preservation_guard,
    apply_title_mark_balance_guard,
    audit_foreign_script_consistency,
    mixed_cjk_latin_findings_covered_by_overrides,
    unproven_foreign_introductions_covered_by_overrides,
)
from src.autoslice.term_boundary import unify_terms_across_cues
from src.autoslice.topic_entity_graph import (
    TopicEvidence,
    dynamic_referent_groups,
    load_topic_entity_graph,
    merge_referent_groups,
    resolve_topic_context,
)


@dataclass(frozen=True)
class TextPipelineAdapters:
    """Repository- and script-owned seams supplied by the CLI entrypoint."""

    build_aggregate_transcriber: Callable[..., Callable]
    build_agy_transcriber: Callable[..., Callable]
    load_term_boundary_surfaces: Callable[[dict], list[str]]
    profile_asset_file: Callable[[str], Path]
    review_glossary: Callable[[], str]
    topic_graph_disabled: Callable[[], bool]
    topic_graph_path: Callable[[], Path]
    topic_graph_expected_sha256: Callable[[], str]


@dataclass(frozen=True)
class TextPipelineResult:
    srt_text: str
    cues: list[object]
    spans: list[object]
    transcriber: Callable
    chat_authority_audit: dict
    chat_authority_path: Path


@dataclass(frozen=True)
class TranscriptionDraft:
    song_name_candidates: list[str]
    transcriber: Callable
    spans: list[object]
    srt_text: str
    code_switch_audit: object
    term_boundary_moves: list[dict]
    support_srts: list[str]
    session_topic_authorities: tuple[dict[str, Any], ...]
    session_topic_absorption_audits: list[dict[str, Any]]
    source_language_witness_srt: str


@dataclass(frozen=True)
class EntityVerificationContext:
    verify_confusable_entity: Callable
    referent_groups: list[ReferentGroup]


@dataclass(frozen=True)
class EntityAuthorityResult:
    srt_text: str
    chat_authority_audit: dict
    transcript_entity_audit: dict
    handled_entity_cues: set[int]


@dataclass(frozen=True)
class TextEvidenceResult:
    srt_text: str
    cues: list[object]
    chat_authority_path: Path


def _collect_timeline_chat(
    spec: dict, durations: list[int]
) -> tuple[list[DanmakuItem], list[ChatEvidence]]:
    merged: list[DanmakuItem] = []
    authoritative_chat: list[ChatEvidence] = []
    offset = 0
    for piece, dur in zip(spec["pieces"], durations):
        for item in _piece_chat_evidence(piece):
            rel = item.offset_ms - piece["start_ms"]
            if item.kind == "superchat":
                pre_context = SC_PRE_CONTEXT_MS
            elif item.kind == "gift":
                pre_context = GIFT_PRE_CONTEXT_MS
            else:
                pre_context = DANMAKU_PRE_CONTEXT_MS
            if not (-pre_context <= rel <= dur + 1_000):
                continue
            marker = f"·{item.sender}" if item.sender else ""
            if item.kind == "superchat":
                prefix = "【SC此前" if rel < 0 else "【SC"
            elif item.kind == "gift":
                prefix = "【礼物此前" if rel < 0 else "【礼物"
            else:
                prefix = "【弹幕此前" if rel < 0 else "【弹幕"
            label = f"{prefix}{marker}】{sanitize_chat_display_text(item.text)}"
            merged.append(DanmakuItem(offset_ms=offset + max(0, rel), text=label))
            authoritative_chat.append(
                ChatEvidence(
                    item.kind,
                    offset + rel,
                    item.text,
                    item.sender,
                    item.source,
                    item.source_sha256,
                    item.source_event_id,
                )
            )
        offset += dur
    merged.sort(key=lambda item: item.offset_ms)
    return merged, authoritative_chat


def _transcribe_draft(
    *,
    spec: dict,
    padded: Path,
    padded_dur: int,
    host: str,
    substrate: str,
    correct: str,
    screen_text: bool,
    merged: list[DanmakuItem],
    adapters: TextPipelineAdapters,
) -> TranscriptionDraft:
    session_topic_authorities = discover_session_topic_authorities(spec)
    song_name_candidates = [
        str(title).strip()
        for title in (spec.get("song_name_candidates") or [])
        if isinstance(title, str) and str(title).strip()
    ]
    if substrate == "aggregate_asr":
        transcriber = adapters.build_aggregate_transcriber(
            host,
            danmaku_items=merged or None,
            window_start_ms=0,
            source_video=padded,
            correct=correct,
            screen_text=screen_text,
            recording_date=str(spec.get("date") or ""),
            topic_hint=str(spec.get("selection_hook") or ""),
            song_name_candidates=song_name_candidates,
            session_topic_authorities=session_topic_authorities,
        )
    else:
        transcriber = adapters.build_agy_transcriber(host, danmaku_items=merged or None, window_start_ms=0)
    vad = build_ssh_silero_vad_provider(host)
    spans = vad(padded, 0, padded_dur)
    srt_text = transcriber(padded, [(s.start_ms, s.end_ms) for s in spans])
    srt_text, code_switch_audit = normalize_code_switch_surfaces(srt_text)
    srt_text, session_topic_absorption_audit = absorb_session_topic_entities(
        srt_text, session_topic_authorities
    )
    # A known proper noun (e.g. 梦限大) straddled across two ASR cues can
    # never be repaired downstream: every later stage locks cue count and
    # indices 1:1, so no single cue ever contains the full surface again.
    # Fix it once, right here, before anything downstream depends on the
    # cue shape.
    term_boundary_surfaces = adapters.load_term_boundary_surfaces(spec)
    term_boundary_moves: list[dict] = []
    if term_boundary_surfaces:
        unified_cues, term_boundary_moves = unify_terms_across_cues(
            parse_srt_cues(srt_text), term_boundary_surfaces
        )
        if term_boundary_moves:
            srt_text = _render_cues_to_srt(unified_cues)
    support_srts = _load_independent_chat_support_srts(padded)
    draft_witness_path = padded.with_suffix(".asr_draft.srt")
    source_language_witness_srt = (
        draft_witness_path.read_text(encoding="utf-8", errors="replace")
        if draft_witness_path.is_file()
        else srt_text
    )
    return TranscriptionDraft(
        song_name_candidates=song_name_candidates,
        transcriber=transcriber,
        spans=spans,
        srt_text=srt_text,
        code_switch_audit=code_switch_audit,
        term_boundary_moves=term_boundary_moves,
        support_srts=support_srts,
        session_topic_authorities=session_topic_authorities,
        session_topic_absorption_audits=[session_topic_absorption_audit],
        source_language_witness_srt=source_language_witness_srt,
    )


def _build_entity_verification_context(
    *,
    spec: dict,
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    srt_text: str,
    authoritative_chat: list[ChatEvidence],
    adapters: TextPipelineAdapters,
) -> EntityVerificationContext:
    human_entity_verifier = (
        build_human_text_entity_verifier(text_override_path, candidate_id=cid)
        if text_override_path is not None
        else None
    )
    audio_entity_verifier = None
    if host in {"localhost", "127.0.0.1"}:
        from src.autoslice.entity_audio_verifier import build_local_audio_entity_verifier

        audio_entity_verifier = build_local_audio_entity_verifier(
            source_media=padded,
            output_dir=out_root,
            recording_date=str(spec.get("date") or ""),
            source_duration_ms=padded_dur,
        )

    # Read-aloud (danmaku/SC) arbitration is a pure-context judgment, so route it
    # through a general LLM on CPA *before* the audio verifier: this keeps
    # "she read this danmaku" corrections alive when AGY/Gemini is quota-exhausted
    # (2026-07-14 regression: agy jingting AND the audio arbitration are both
    # Gemini-family, so one quota wall reverted a slice to garble) and takes AGY
    # off the hot path.  Audio stays the fallback for acoustic ambiguity and for
    # non-danmaku entity confusions.  With no CPA configured the layer defers
    # everything, identical to the prior human→audio chain.
    from src.autoslice.read_aloud_llm_verifier import build_cpa_read_aloud_verifier

    read_aloud_llm_call = None
    if os.environ.get("CPA_BASE_URL") and os.environ.get("CPA_API_KEY"):
        read_aloud_llm_call = build_llm_call(
            LlmConfig(
                transport="command",
                command_template=(
                    "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                    "'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
                ),
                timeout_seconds=180.0,
            )
        )
    cpa_read_aloud_verifier = build_cpa_read_aloud_verifier(
        read_aloud_llm_call, next_verifier=audio_entity_verifier
    )

    def verify_confusable_entity(request):
        if human_entity_verifier is not None:
            verdict = human_entity_verifier(request)
            if verdict is not None:
                return verdict
        return cpa_read_aloud_verifier(request)

    static_referent_groups = load_referent_groups(adapters.profile_asset_file("entity_confusables"))
    dynamic_groups = []
    topic_resolution_audit: dict[str, object] = {
        "schema_version": "topic-resolution.v1",
        "status": "NO_GRAPH",
        "recording_date": str(spec.get("date") or ""),
        "selected_topic_ids": [],
        "selected_work_ids": [],
        "scoped_entity_ids": [],
        "evidence": [],
    }
    if not adapters.topic_graph_disabled():
        graph_path = adapters.topic_graph_path()
        if graph_path.is_file() and not graph_path.is_symlink():
            try:
                graph, graph_sha = load_topic_entity_graph(
                    graph_path,
                    expected_sha256=adapters.topic_graph_expected_sha256(),
                )
                if dt.datetime.now(dt.timezone.utc) <= dt.datetime.fromisoformat(
                    graph["expires_at"]
                ):
                    topic_evidence = [TopicEvidence("transcript", srt_text)]
                    selection_hook = str(spec.get("selection_hook") or "")
                    if selection_hook:
                        topic_evidence.append(TopicEvidence("selection_hook", selection_hook))
                    if authoritative_chat:
                        topic_evidence.append(
                            TopicEvidence(
                                "structured_chat",
                                "\n".join(item.text for item in authoritative_chat),
                            )
                        )
                    resolution = resolve_topic_context(
                        graph,
                        topic_evidence,
                        recording_date=str(spec.get("date") or ""),
                        graph_sha256=graph_sha,
                    )
                    topic_resolution_audit = resolution.as_dict()
                    dynamic_groups = dynamic_referent_groups(graph, resolution, srt_text)
                else:
                    topic_resolution_audit["status"] = "GRAPH_EXPIRED"
                    topic_resolution_audit["graph_sha256"] = graph_sha
            except (OSError, ValueError) as exc:
                topic_resolution_audit["status"] = "GRAPH_INVALID"
                topic_resolution_audit["error"] = f"{type(exc).__name__}: {exc}"
    topic_resolution_path = out_root / f"{cid}.topic-resolution.json"
    topic_resolution_path.write_text(
        json.dumps(topic_resolution_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    referent_groups = merge_referent_groups(static_referent_groups, dynamic_groups)
    return EntityVerificationContext(
        verify_confusable_entity=verify_confusable_entity,
        referent_groups=referent_groups,
    )


def _apply_entity_authority(
    *,
    srt_text: str,
    authoritative_chat: list[ChatEvidence],
    support_srts: list[str],
    referent_groups: list[ReferentGroup],
    verify_confusable_entity: Callable,
    code_switch_audit: object,
    term_boundary_moves: list[dict],
    padded: Path,
    adapters: TextPipelineAdapters,
    session_topic_absorption_audits: list[dict[str, Any]] | None = None,
) -> EntityAuthorityResult:
    draft_witness_path = padded.with_suffix(".asr_draft.srt")
    source_witness_srt = (
        draft_witness_path.read_text(encoding="utf-8", errors="replace")
        if draft_witness_path.is_file()
        else None
    )
    srt_text, self_reference_absorption_audit = absorb_host_self_references(
        srt_text,
        source_witness_srt=source_witness_srt,
    )
    srt_text, chat_authority_audit = apply_authoritative_chat_evidence(
        srt_text,
        authoritative_chat,
        support_srt_texts=support_srts,
        referent_groups=referent_groups,
        entity_verifier=verify_confusable_entity,
    )
    chat_authority_audit[
        "self_reference_absorption_audit"
    ] = self_reference_absorption_audit
    chat_authority_audit[
        "session_topic_absorption_audits"
    ] = list(session_topic_absorption_audits or [])
    if draft_witness_path.is_file():
        srt_text, numeric_fact_audit = apply_numeric_fact_provenance_guard(
            draft_witness_path.read_text(encoding="utf-8", errors="replace"),
            srt_text,
            structured_evidence=authoritative_chat,
            matched_structured_evidence=chat_authority_audit.get("applied") or (),
        )
    else:
        numeric_fact_audit = {
            "schema_version": "numeric-fact-provenance-audit.v1",
            "status": "SKIPPED_NO_INITIAL_ASR",
            "reverted": [],
            "reverted_count": 0,
        }
    chat_authority_audit["numeric_fact_provenance_audit"] = numeric_fact_audit
    handled_entity_cues = {
        int(index)
        for key in ("applied", "pending_text_overrides", "entity_repairs", "coreference_repairs")
        for row in chat_authority_audit.get(key) or []
        for index in (
            row.get("cue_indexes")
            or ([row.get("cue_index")] if row.get("cue_index") is not None else [])
        )
    }
    # 怀疑编译器（通用机制，2026-07-13）：位置先验 + 重复一致性都在 chat
    # 证据落定后的最终文本上编译成临时混淆组，与词典/话题图组共用同一个
    # 音频仲裁引擎；positions 非空使它们天然进不了 chat 证据路径。
    opening_group = clip_opening_address_group(
        srt_text,
        load_clip_opening_address_config(
            adapters.profile_asset_file("clip_opening_address")
        ),
    )
    repetition_groups = repetition_divergence_groups(srt_text)
    # 2026-07-19 欠账 #0/#5 落地：短语级重复分歧编译器（抱/帮案）+ 词表
    # 拼音候选发现层（皮毛熊/卖批案）。同为「发现≠裁决」的确定性怀疑
    # 编译器，与句级重复组共用声学仲裁；UNCERTAIN 双向保留绝不阻塞。
    from src.autoslice.phonetic_scan import (
        glossary_phonetic_candidate_groups,
        phrase_repetition_divergence_groups,
    )

    phrase_divergence_groups = phrase_repetition_divergence_groups(srt_text)
    phonetic_candidate_groups = glossary_phonetic_candidate_groups(
        srt_text, referent_groups
    )
    # AGY/CPA/词表已经完成专名语义定稿。无位置标记的静态/话题实体组只给
    # 结构化聊天匹配与终稿验证使用，绝不能再被黑帧 Gemini 按初始听写强制
    # 二选一。声学层只接明确声明为 transcript_only/clip_initial 的未决槽位，
    # 以及在终稿上新编译的片首/重复异常审计。
    explicit_post_semantic_audio_groups = [
        group
        for group in referent_groups
        if set(group.positions) & {"transcript_only", "clip_initial"}
    ]
    semantic_text_final_groups = [
        group for group in referent_groups if group not in explicit_post_semantic_audio_groups
    ]
    transcript_groups = [
        *explicit_post_semantic_audio_groups,
        *([opening_group] if opening_group is not None else []),
        *repetition_groups,
        *phrase_divergence_groups,
        *phonetic_candidate_groups,
    ]
    srt_text, transcript_entity_audit = apply_audio_entity_verification(
        srt_text,
        referent_groups=transcript_groups,
        entity_verifier=verify_confusable_entity,
        excluded_cue_indexes=handled_entity_cues,
    )
    chat_authority_audit["transcript_entity_audit"] = transcript_entity_audit
    chat_authority_audit["post_semantic_entity_policy"] = {
        "schema_version": "post-semantic-entity-policy.v1",
        "status": "SEMANTIC_TEXT_FINAL",
        "reason_code": "POST_SEMANTIC_ENTITY_AUDIO_OVERRIDE_DISABLED",
        "semantic_text_final_groups": [
            [entity.canonical for entity in group.entities]
            for group in semantic_text_final_groups
        ],
        "explicit_audio_groups": [
            [entity.canonical for entity in group.entities]
            for group in explicit_post_semantic_audio_groups
        ],
        "dynamic_audio_group_count": len(repetition_groups)
        + len(phrase_divergence_groups)
        + len(phonetic_candidate_groups)
        + (1 if opening_group is not None else 0),
        "phrase_divergence_groups": [
            [entity.canonical for entity in group.entities]
            for group in phrase_divergence_groups
        ],
        "phonetic_candidate_groups": [
            [entity.canonical for entity in group.entities]
            for group in phonetic_candidate_groups
        ],
    }
    chat_authority_audit["code_switch_surface_audit"] = code_switch_audit
    chat_authority_audit["term_boundary_audit"] = {
        "schema_version": "term-boundary-audit.v1",
        "status": "APPLIED" if term_boundary_moves else "NO_CHANGE",
        "moves": term_boundary_moves,
    }
    chat_authority_audit.setdefault("entity_repairs", []).extend(
        transcript_entity_audit.get("repairs") or []
    )
    # draft/终稿专名差异只披露，不再把语义定稿交给黑帧 Gemini 强制回归
    # 初始听写。初始 ASR 的价值是可追溯证人，不是语义修正后的 postcondition。
    wd_audits: list[dict] = []
    wd_groups = [
        group
        for group in referent_groups
        if getattr(group, "positions", ()) and "witness_disagreement" in group.positions
    ]
    if wd_groups and draft_witness_path.is_file():
        draft_witness = draft_witness_path.read_text(encoding="utf-8", errors="replace")
        for wd_group in wd_groups:
            suspicious = witness_disagreement_cues(draft_witness, srt_text, wd_group)
            if not suspicious:
                continue
            wd_audits.append(
                {
                    "schema_version": "witness-disagreement-audit.v2",
                    "status": "SEMANTIC_AUTHORITY_PRESERVED",
                    "reason_code": "DRAFT_WITNESS_IS_NOT_POST_SEMANTIC_AUTHORITY",
                    "suspicious_cue_indexes": suspicious,
                    "candidate_entities": [
                        entity.canonical for entity in wd_group.entities
                    ],
                }
            )
    chat_authority_audit["witness_disagreement_audits"] = wd_audits
    # 语义层引入词与 draft 不同是专名修正的预期结果，只留来源差异审计。
    # 禁止再构造 {终稿专名, draft 片段} 给 Gemini 强制二选一。
    introduced_term_audits: list[dict] = []
    if draft_witness_path.is_file():
        from src.autoslice.term_authority import protected_terms as _protected_terms

        draft_witness_text = draft_witness_path.read_text(encoding="utf-8", errors="replace")
        for row in introduced_term_cues(
            draft_witness_text, srt_text, _protected_terms()
        ):
            introduced_term_audits.append(
                {
                    "schema_version": "introduced-term-audit.v2",
                    "status": "SEMANTIC_AUTHORITY_PRESERVED",
                    "reason_code": "DRAFT_WITNESS_IS_NOT_POST_SEMANTIC_AUTHORITY",
                    "introduced_term": row,
                }
            )
    chat_authority_audit["introduced_term_audits"] = introduced_term_audits
    return EntityAuthorityResult(
        srt_text=srt_text,
        chat_authority_audit=chat_authority_audit,
        transcript_entity_audit=transcript_entity_audit,
        handled_entity_cues=handled_entity_cues,
    )


def _run_final_review(
    *,
    srt_text: str,
    chat_authority_audit: dict,
    handled_entity_cues: set[int],
    verify_confusable_entity: Callable,
    adapters: TextPipelineAdapters,
    authoritative_chat: list[ChatEvidence] | tuple[ChatEvidence, ...] = (),
    selection_hook: str = "",
    referent_groups: Sequence[object] = (),
) -> tuple[str, dict]:
    final_review_audit: dict[str, Any] = {"schema_version": "final-review-audit.v1", "status": "SKIPPED"}
    if os.environ.get("AUTOSLICE_DISABLE_FINAL_REVIEW") != "1":
        try:
            review_llm_call = build_llm_call(
                LlmConfig(
                    transport="command",
                    command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium",
                    timeout_seconds=300.0,
                )
            )
            review_findings = audit_final_subtitles(
                srt_text,
                llm_call=review_llm_call,
                extract_json=extract_json_object,
                glossary_text=adapters.review_glossary(),
                structured_context_text="\n".join(
                    (
                        # 选片钩子进入审片员视野（2026-07-18 kmx 漏听案）：钩子
                        # 点名的专名是漏听检查（prompt 规则7）的第一线索。
                        [f"selection_hook: {selection_hook.strip()}"]
                        if selection_hook.strip()
                        else []
                    )
                    + [
                        (
                            f"{item.kind} @{item.offset_ms}ms"
                            f"{(' sender=' + item.sender) if item.sender else ''}: "
                            f"{sanitize_chat_display_text(item.text)}"
                        )
                        for item in authoritative_chat[:160]
                    ]
                ),
            )
            protected_review_cues = set(handled_entity_cues)
            for row in chat_authority_audit.get("applied") or []:
                for index in row.get("cue_indexes") or []:
                    protected_review_cues.add(int(index))
            # 注册实体词面（canonical+surfaces）：suspect 命中即实体选边，
            # T1 纯文本车道让位声学仲裁（kmx/乒乓球保向铁律）。
            entity_surfaces = frozenset(
                surface
                for group in referent_groups
                for entity in getattr(group, "entities", ())
                for surface in (
                    getattr(entity, "canonical", ""),
                    *getattr(entity, "surfaces", ()),
                )
                if surface
            )
            srt_text, final_review_audit = route_findings(
                srt_text,
                review_findings,
                protected_cue_indexes=protected_review_cues,
                entity_surface_set=entity_surfaces,
            )
            # 无人值守自定夺：非同音建议交专用的“完整 cue + 前后语境 +
            # 上下文音频”声学相容度检查，再由固定代码规则融合。验证器不能选择或
            # 生成文本；chat/词典权威 cue 已在 route 阶段被挡。UNCERTAIN 原样
            # 保留并披露，永不阻塞。
            adjudicable = [
                row
                for row in (final_review_audit.get("findings") or [])
                if row.get("routed") == "disclosure"
                and row.get("suggestion")
                and str(row.get("suggestion")) != str(row.get("suspect"))
            ]
            adjudicated_cues: set[int] = set()
            adjudication_count = 0
            partial = False
            for row in adjudicable:
                suspect = str(row["suspect"])
                finding_cue = int(row.get("cue_index") or 0)
                if finding_cue in adjudicated_cues:
                    row["routed"] = "deferred_same_cue"
                    row["context_audio_adjudication"] = {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "DEFERRED_SAME_CUE",
                        "repaired": False,
                    }
                    partial = True
                    continue
                if adjudication_count >= MAX_CONTEXT_ADJUDICATIONS:
                    row["routed"] = "skipped_budget"
                    row["context_audio_adjudication"] = {
                        "schema_version": "subtitle-span-adjudication.v1",
                        "status": "SKIPPED_BUDGET",
                        "repaired": False,
                    }
                    partial = True
                    continue
                adjudicated_cues.add(finding_cue)
                adjudication_count += 1
                # 过期发现守卫（2026-07-14 恋青/练死案）：审片发现产自它当时
                # 看到的文本快照；若后续 pass 已改写该 cue、suspect 不在当前
                # 文本里，这条发现的前提已失效——只披露，绝不再持刀。
                live_cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
                live_text = (
                    live_cues[finding_cue - 1].text
                    if 0 < finding_cue <= len(live_cues)
                    else ""
                )
                if suspect not in live_text:
                    row["context_audio_adjudication"] = {
                        "status": "STALE_FINDING_SKIPPED",
                        "repaired": False,
                    }
                    continue
                srt_text, adj_audit = adjudicate_context_finding(
                    srt_text,
                    entity_verifier=verify_confusable_entity,
                    finding=row,
                )
                repaired = bool(adj_audit.get("repaired"))
                row["context_audio_adjudication"] = adj_audit
                if repaired:
                    row["routed"] = "context_audio_adjudicated_fix"
                    final_review_audit["applied_count"] = int(
                        final_review_audit.get("applied_count") or 0
                    ) + 1
                    final_review_audit["status"] = "APPLIED"
                    # 终稿面复证登记（delivery-divergence 防线，xinyi 案同类）：
                    # 已应用的裁决修复必须和 chat/实体修复一样被
                    # verify_chat_authority_final_surfaces 在交付工件上按原时窗
                    # 复证存活。无 expected_entity/resolved_canonical，故不会被
                    # 未注册回退或矛盾和解误伤。
                    request = adj_audit.get("request") or {}
                    chat_authority_audit.setdefault("entity_repairs", []).append(
                        {
                            "mode": "final_review_context_adjudication",
                            "evidence_id": request.get("evidence_id"),
                            "cue_indexes": [finding_cue],
                            "matched_start_ms": int(request.get("matched_start_ms") or 0),
                            "matched_end_ms": int(request.get("matched_end_ms") or 0),
                            "before": [request.get("current_cue")],
                            "after": [request.get("proposed_cue")],
                            "structured_exact_text": request.get("proposed_cue"),
                            "survived": True,
                            "verdict": adj_audit.get("verdict"),
                        }
                    )
            final_review_audit["context_adjudication_count"] = adjudication_count
            final_review_audit["context_adjudication_budget"] = MAX_CONTEXT_ADJUDICATIONS
            if partial:
                final_review_audit["status"] = "PARTIAL"
            # 2026-07-18 交付事故类机制：区分「证据裁决后的保留」与「基础设施
            # 失败导致的未决」。前者（OBSERVED 下 keep-current）是正当结论；
            # 后者（provider 额度/异常，裁决根本没发生）不许当作终局——审片员
            # 已给出高置信修复提案、只是没有法官到场。这些行记入
            # infra_unresolved，由 run_text_pipeline 在全部 provenance 落盘后
            # 拒绝带伤交付（转 runner 有界重试；付费兜底修复后通常一轮即过）。
            infra_unresolved = []
            for row in adjudicable:
                adjudication = row.get("context_audio_adjudication") or {}
                if adjudication.get("repaired") or adjudication.get("status") != "UNCERTAIN":
                    continue
                verdict = adjudication.get("verdict") or {}
                reason_code = str(verdict.get("reason_code") or "")
                if reason_code in {"ENTITY_AUDIO_PROVIDER_FAILED", "CONTEXT_VERIFIER_ERROR"}:
                    infra_unresolved.append(
                        {
                            "cue_index": row.get("cue_index"),
                            "suspect": row.get("suspect"),
                            "reason_code": reason_code,
                        }
                    )
            final_review_audit["infra_unresolved"] = infra_unresolved
            final_review_audit["infra_unresolved_count"] = len(infra_unresolved)
        except Exception as exc:
            final_review_audit = {
                "schema_version": "final-review-audit.v1",
                "status": "AUDITOR_UNAVAILABLE",
                "error_type": type(exc).__name__,
            }
    return srt_text, final_review_audit


def _finalize_text_evidence(
    *,
    spec: dict,
    durations: list[int],
    srt_text: str,
    chat_authority_audit: dict,
    transcript_entity_audit: dict,
    referent_groups: list[ReferentGroup],
    final_review_audit: dict,
    song_name_candidates: list[str],
    session_topic_authorities: tuple[dict[str, Any], ...],
    source_language_witness_srt: str,
    text_override_path: Path | None,
    source_truth_ledger_path: Path | None,
    out_root: Path,
    cid: str,
    padded: Path | None = None,
) -> TextEvidenceResult:
    srt_text, final_source_language_audit = apply_source_language_preservation_guard(
        source_language_witness_srt, srt_text
    )
    chat_authority_audit[
        "final_source_language_preservation_audit"
    ] = final_source_language_audit
    override_document: dict[str, Any] = {}
    if text_override_path is not None:
        try:
            loaded_override = json.loads(
                text_override_path.read_text(encoding="utf-8")
            )
            if isinstance(loaded_override, dict):
                override_document = loaded_override
        except (OSError, json.JSONDecodeError):
            pass
    if (
        str(final_source_language_audit["status"]).startswith(
            "BLOCKED_UNPROVEN_FOREIGN_"
        )
        and unproven_foreign_introductions_covered_by_overrides(
            final_source_language_audit,
            override_document,
        )
    ):
        final_source_language_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
        final_source_language_audit["deferred_reason"] = (
            "every un-witnessed foreign-language cue has a timeline-bound "
            "reviewed repair"
        )
    if padded is not None:
        # Machine witness (Ivan 2026-07-19): the same Gemini chain that does
        # foreign transcription listens to the exact blocked spans; a match
        # is evidence, a mismatch or provider failure keeps the block.
        witness_language_preservation_audit(
            media_path=padded,
            audit=final_source_language_audit,
            out_root=out_root,
            cid=cid,
        )
    foreign_script_audit = audit_foreign_script_consistency(srt_text)
    if (
        foreign_script_audit["status"]
        == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
        and text_override_path is not None
    ):
        foreign_script_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
        foreign_script_audit[
            "deferred_reason"
        ] = "candidate has a hash-bound reviewed text override before delivery"
    elif (
        foreign_script_audit["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
        and text_override_path is not None
    ):
        if mixed_cjk_latin_findings_covered_by_overrides(
            foreign_script_audit,
            override_document,
        ):
            foreign_script_audit["status"] = "DEFERRED_TO_BOUND_TEXT_OVERRIDE"
            foreign_script_audit["deferred_reason"] = (
                "every mixed-language cue has a timeline-bound reviewed repair"
            )
    if (
        padded is not None
        and foreign_script_audit["status"] == "BLOCKED_MIXED_FOREIGN_SCRIPT_CLUSTER"
    ):
        # Wrong-language ASR repair (Ivan 2026-07-19): the cluster text itself
        # is garbage, so re-transcribe each clustered cue from its own audio
        # and let a fresh audit judge the repaired text; unrepaired clusters
        # stay blocked.
        srt_text, cluster_repair_audit = retranscribe_foreign_script_cluster(
            media_path=padded,
            srt_text=srt_text,
            audit=foreign_script_audit,
            out_root=out_root,
            cid=cid,
        )
        if cluster_repair_audit.get("replaced_count"):
            foreign_script_audit = audit_foreign_script_consistency(srt_text)
        foreign_script_audit["cluster_retranscription"] = cluster_repair_audit
    if padded is not None:
        witness_foreign_script_audit(
            media_path=padded,
            audit=foreign_script_audit,
            out_root=out_root,
            cid=cid,
        )
    chat_authority_audit["foreign_script_consistency_audit"] = foreign_script_audit
    srt_text, title_mark_balance_audit = apply_title_mark_balance_guard(srt_text)
    chat_authority_audit["title_mark_balance_audit"] = title_mark_balance_audit
    srt_text, impossible_punctuation_audit = apply_impossible_punctuation_guard(
        srt_text
    )
    chat_authority_audit[
        "impossible_punctuation_audit"
    ] = impossible_punctuation_audit
    srt_text, final_session_topic_absorption_audit = absorb_session_topic_entities(
        srt_text, session_topic_authorities
    )
    chat_authority_audit.setdefault("session_topic_absorption_audits", []).append(
        final_session_topic_absorption_audit
    )
    srt_text, unregistered_entity_reverts = revert_unregistered_entity_repairs(
        srt_text,
        chat_authority_audit.get("entity_repairs") or [],
        registered_entity_names(referent_groups),
    )
    if unregistered_entity_reverts:
        chat_authority_audit["unregistered_entity_reverts"] = unregistered_entity_reverts
        print(
            "[chat-authority] unregistered entity repairs reverted: "
            + json.dumps(unregistered_entity_reverts, ensure_ascii=False),
            flush=True,
        )
    # 同槽矛盾裁定和解（2026-07-14 恋死/恋青/练死案）：多个 pass 对同一 cue
    # 给出互斥的 RESOLVED 实体 → 该处听证不可信，回退最早改写前的文本并披露；
    # 严禁后写者赢。
    srt_text, entity_verdict_contradictions = reconcile_contradictory_entity_repairs(
        srt_text, chat_authority_audit.get("entity_repairs") or []
    )
    if entity_verdict_contradictions:
        chat_authority_audit["entity_verdict_contradictions"] = entity_verdict_contradictions
        print(
            "[chat-authority] contradictory entity verdicts reverted: "
            + json.dumps(entity_verdict_contradictions, ensure_ascii=False),
            flush=True,
        )
    chat_authority_audit["final_review_audit"] = final_review_audit
    persist_review_audit(out_root / f"{cid}.review-flags.json", final_review_audit)
    chat_authority_audit["post_transcript_entity_output_srt_sha256"] = hashlib.sha256(
        srt_text.encode("utf-8")
    ).hexdigest()
    chat_authority_path = out_root / f"{cid}.chat-authority.json"
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if (
        str(final_source_language_audit["status"]).startswith("BLOCKED_")
        or str(foreign_script_audit["status"]).startswith("BLOCKED_")
    ):
        raise SystemExit(
            f"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED: {chat_authority_path}"
        )
    if (
        chat_authority_audit["status"]
        in {"FAILED", "ENTITY_VERDICT_REQUIRED", "SC_SENDER_VERDICT_REQUIRED"}
        or transcript_entity_audit["status"] == "ENTITY_VERDICT_REQUIRED"
    ):
        raise SystemExit(f"CHAT_AUTHORITY_FINALIZATION_FAILED: {chat_authority_path}")
    # Deterministic song-name pin (Ivan 2026-07-13): belt over the LLM prompt
    # context above.  A talk cue that signals a song mention (下一首/点歌/想唱/…)
    # gets its trailing mention span fuzzy-matched against machine-evidence
    # candidates (screen songlist + 点歌 + known-songs) and, on a strong match,
    # rewritten to 《title》.  A cue with no intent phrase or a weak match is
    # never touched — see src/autoslice/song_name_pin.py.
    if song_name_candidates:
        srt_text, song_name_pin_audit = pin_song_names_in_srt(
            srt_text, candidates=song_name_candidates
        )
        song_name_pin_path = out_root / f"{cid}.song-name-pin.json"
        song_name_pin_path.write_text(
            json.dumps(song_name_pin_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if song_name_pin_audit.get("replacements"):
            print(
                f"{cid}: pinned {len(song_name_pin_audit['replacements'])} "
                "song name(s) from screen-songlist/点歌 evidence"
            )
    # Final unbypassable meme canon (currently only 直女→侄女).  This runs after
    # every LLM/entity/song-name text stage; the later hash-bound human override
    # path independently re-runs the same policy before speaker rendering.
    srt_text, hard_meme_surface_audit = normalize_hard_meme_surfaces(srt_text)
    chat_authority_audit["final_hard_meme_surface_audit"] = (
        hard_meme_surface_audit
    )
    srt_text, source_truth_audit = apply_source_subtitle_truth(
        srt_text,
        spec=spec,
        durations=durations,
        ledger_path=source_truth_ledger_path,
    )
    chat_authority_audit["source_subtitle_truth_audit"] = source_truth_audit
    chat_authority_audit["final_output_srt_sha256"] = hashlib.sha256(
        srt_text.encode("utf-8")
    ).hexdigest()
    chat_authority_path.write_text(
        json.dumps(
            chat_authority_audit,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if source_truth_audit["status"] == "FAILED":
        raise SystemExit(
            f"SOURCE_SUBTITLE_TRUTH_REQUIRED: {chat_authority_path}"
        )
    (out_root / "padded.fresh.srt").write_text(srt_text, encoding="utf-8")
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    if len(cues) < 3:
        raise SystemExit("FRESH_TRANSCRIPTION_TOO_SPARSE")
    return TextEvidenceResult(
        srt_text=srt_text,
        cues=cues,
        chat_authority_path=chat_authority_path,
    )


def run_text_pipeline(
    *,
    spec: dict,
    durations: list[int],
    padded: Path,
    padded_dur: int,
    host: str,
    text_override_path: Path | None,
    cid: str,
    out_root: Path,
    substrate: str,
    correct: str,
    screen_text: bool,
    adapters: TextPipelineAdapters,
) -> TextPipelineResult:
    merged, authoritative_chat = _collect_timeline_chat(spec, durations)
    draft = _transcribe_draft(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        substrate=substrate,
        correct=correct,
        screen_text=screen_text,
        merged=merged,
        adapters=adapters,
    )
    entity_context = _build_entity_verification_context(
        spec=spec,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        srt_text=draft.srt_text,
        authoritative_chat=authoritative_chat,
        adapters=adapters,
    )
    authority = _apply_entity_authority(
        srt_text=draft.srt_text,
        authoritative_chat=authoritative_chat,
        support_srts=draft.support_srts,
        referent_groups=entity_context.referent_groups,
        verify_confusable_entity=entity_context.verify_confusable_entity,
        code_switch_audit=draft.code_switch_audit,
        term_boundary_moves=draft.term_boundary_moves,
        session_topic_absorption_audits=draft.session_topic_absorption_audits,
        padded=padded,
        adapters=adapters,
    )
    reviewed_srt, final_review_audit = _run_final_review(
        srt_text=authority.srt_text,
        chat_authority_audit=authority.chat_authority_audit,
        handled_entity_cues=authority.handled_entity_cues,
        verify_confusable_entity=entity_context.verify_confusable_entity,
        authoritative_chat=authoritative_chat,
        adapters=adapters,
        selection_hook=str(spec.get("selection_hook") or ""),
        referent_groups=entity_context.referent_groups,
    )
    evidence = _finalize_text_evidence(
        spec=spec,
        durations=durations,
        srt_text=reviewed_srt,
        chat_authority_audit=authority.chat_authority_audit,
        transcript_entity_audit=authority.transcript_entity_audit,
        referent_groups=entity_context.referent_groups,
        final_review_audit=final_review_audit,
        song_name_candidates=draft.song_name_candidates,
        session_topic_authorities=draft.session_topic_authorities,
        source_language_witness_srt=draft.source_language_witness_srt,
        text_override_path=text_override_path,
        source_truth_ledger_path=(
            None
            if str(spec.get("human_truth_mode") or "delivery") == "withheld"
            else adapters.profile_asset_file("subtitle_truth_ledger")
        ),
        out_root=out_root,
        cid=cid,
        padded=padded,
    )
    # 带伤交付闸（2026-07-18 醉堆/七夕/核酸天下案）：审片员的修复提案若因
    # provider 基础设施失败（而非证据裁决）未落地、且后续确定性 pass（如
    # source_subtitle_truth ledger）也没有修掉对应文本，则拒绝交付——全部
    # provenance 已在上方落盘，runner 按 provider_transient 有界重试。
    still_unresolved = [
        row
        for row in (final_review_audit.get("infra_unresolved") or [])
        if str(row.get("suspect") or "") and str(row.get("suspect")) in evidence.srt_text
    ]
    if still_unresolved:
        raise SystemExit(
            "FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED: "
            f"{len(still_unresolved)} repair proposal(s) blocked by provider failure "
            f"(cues {[row.get('cue_index') for row in still_unresolved]}); "
            "refusing to deliver known-suspect text — runner will retry"
        )
    return TextPipelineResult(
        srt_text=evidence.srt_text,
        cues=evidence.cues,
        spans=draft.spans,
        transcriber=draft.transcriber,
        chat_authority_audit=authority.chat_authority_audit,
        chat_authority_path=evidence.chat_authority_path,
    )
