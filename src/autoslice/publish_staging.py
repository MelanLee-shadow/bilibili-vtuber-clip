"""Fail-closed title, AI-cover, and publish-draft staging.

This module can prepare local evidence only. Every emitted publish document keeps
``upload_enabled`` false and remains downstream of the release decision gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, replace as dataclasses_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence

from .auto_review import DecisionAction, ReviewDecision
from .channel_profile import load_channel_profile
from .chat_authority import canonicalize_hard_surfaces
from .cover_emote import (
    EmoteLibrary,
    compose_companion_reference,
    load_emote_library,
    resolve_emote_reference,
)
from .cover_frame_selection import (
    DEFAULT_SKIP_HEAD_MS,
    extract_zoomed_cover_frame,
    select_expressive_cover_frame,
)
from .cover_generation import (
    LidoushaCoverArtDirection,
    _call_cpa_image_edit as _cover_call_cpa_image_edit,
    _cpa_image_model_candidates,
    _lidousha_cover_art_direction,
    _lidousha_cover_prompt,
    _lidousha_cover_text,
    _overlay_lidousha_cover_title,
)
from .cover_route_evidence import (
    build_no_crop_participant_verification,
    build_cover_route_decision,
    is_hash_bound_reference_authority,
    relationship_source_participants_verified,
    relationship_visual_safety_required,
    record_cover_route_execution,
    source_visible_participant_ids,
    story_participant_ids,
    validate_final_participant_verification,
)
from .cover_host_identity_gate import (
    final_host_identity_witness_unavailable,
    validate_final_host_identity_verification,
)
from .cover_polish_gate import (
    _compose_screenshot_cover_with_face_gate,
    _degrade_rejected_polish_to_direct,
    _materialize_screenshot_polish,
    _polish_face_binding_failure,
    _verify_polish_face_integrity,
)
from .llm_client import LlmCall, extract_json_object
from .review_evidence import SourceCue
from .recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from .shadow_review import _sha256, _write_json_file
from .story_contract import (
    audit_story_artifact,
    cover_relation_prompt,
    cover_story_contract_binding,
)
from .title_policy import (
    _TITLE_MAX_ATTEMPTS,
    _TITLE_MAX_LEN,
    _TITLE_MIN_LEN,
    _ensure_lidousha_prefix,
    canonicalize_automatic_title_fillers,
    canonicalize_publish_title,
    canonicalize_song_catalog_title,
    manual_title_override,
    publish_title_policy_violations,
    _selection_hook_anchor_valid,
    _selection_hook_fallback_title,
    _selection_hook_first_clause,
    _title_policy_violations,
)

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id


def profile_asset_text(key: str) -> str:
    try:
        return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT).read_text(encoding="utf-8").strip()
    except OSError:
        return "(资产文件缺失)"


LIDOUSHA_COVER_WORKFLOW = "cpa-openai-compatible-image-edit-cover-plus-approved-local-title-overlay"


class _AutomaticTitleResult(NamedTuple):
    staged_title: str | None
    title_source: str
    title_authority_status: str | None
    title_authority_error: str | None
    title_policy_violations: list[str]


def _selection_hook_has_inferable_anchor(
    *, selection_hook: str, title: str
) -> bool:
    """Prove main-hook retention from exact shared text.

    The model-provided anchor is useful disclosure, but a bad anchor must not
    invalidate a mechanically cleaned title when the title itself still
    contains a concrete phrase from the authoritative first hook clause.
    """

    first_clause = _selection_hook_first_clause(selection_hook)
    for width in range(min(12, len(first_clause)), 1, -1):
        for start in range(0, len(first_clause) - width + 1):
            anchor = first_clause[start : start + width]
            if _selection_hook_anchor_valid(
                anchor=anchor,
                selection_hook=selection_hook,
                title=title,
            ):
                return True
    return False


def _resolve_automatic_title(
    *,
    base_prompt: str,
    title_llm_call: LlmCall,
    selection_hook: str,
    initial_title_source: str,
) -> _AutomaticTitleResult:
    llm_title = ""
    llm_error: str | None = None
    violations: list[str] = []
    deterministic_filler_repair = False
    for attempt in range(_TITLE_MAX_ATTEMPTS):
        deterministic_filler_repair = False
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n注意：上一次标题违反了硬约束（违禁词、成对符号未闭合，或没有保留选片第一分句的具体核心短语），已被否决。"
                "不要用任何万能强调词，也不要把后续陪衬话题改成主标题；"
                "书名号、引号、括号必须左右成对；写她具体做了/说了什么，"
                "并按要求重新只输出 JSON。"
            )
        try:
            payload = extract_json_object(title_llm_call(prompt))
        except Exception as exc:
            llm_error = type(exc).__name__
            break
        candidate = str(payload.get("title") or "").strip()
        if not candidate:
            llm_error = "empty_title"
            break
        repaired = canonicalize_automatic_title_fillers(candidate)
        repaired_anchor = canonicalize_automatic_title_fillers(
            str(payload.get("selection_hook_anchor") or "")
        )
        repaired_hook_valid = (
            not selection_hook
            or _selection_hook_anchor_valid(
                anchor=repaired_anchor,
                selection_hook=selection_hook,
                title=repaired,
            )
            or _selection_hook_has_inferable_anchor(
                selection_hook=selection_hook,
                title=repaired,
            )
        )
        if (
            repaired != candidate
            and not _title_policy_violations(repaired)
            and repaired_hook_valid
            and _TITLE_MIN_LEN
            <= len(_ensure_lidousha_prefix(repaired))
            <= _TITLE_MAX_LEN
        ):
            candidate = repaired
            deterministic_filler_repair = True
        llm_title = candidate
        violations = _title_policy_violations(candidate)
        hook_valid = (
            repaired_hook_valid
            if deterministic_filler_repair
            else _selection_hook_anchor_valid(
                anchor=payload.get("selection_hook_anchor"),
                selection_hook=selection_hook,
                title=candidate,
            )
        )
        if selection_hook and not hook_valid:
            violations.append("selection_hook_anchor_missing")
        if not violations:
            break

    title_source = initial_title_source
    if not llm_title:
        error = llm_error or "empty_title"
        return _AutomaticTitleResult(
            None,
            f"job_title(llm_failed: {error})",
            None,
            error,
            violations,
        )
    if selection_hook and "selection_hook_anchor_missing" in violations:
        fallback = _selection_hook_fallback_title(selection_hook)
        if fallback is not None:
            llm_title = fallback
            violations = _title_policy_violations(fallback)
            title_source = "selection_hook_fallback_after_llm_mismatch"
            deterministic_filler_repair = False
    prefixed = _ensure_lidousha_prefix(llm_title)
    if not _TITLE_MIN_LEN <= len(prefixed) <= _TITLE_MAX_LEN:
        return _AutomaticTitleResult(
            None,
            f"job_title(llm_length_out_of_bounds:{len(prefixed)})",
            None,
            f"title_length_out_of_bounds:{len(prefixed)}",
            violations,
        )
    if deterministic_filler_repair:
        title_source = (
            f"llm+{PROFILE_ID}_style_asset+deterministic_filler_removal"
        )
    elif title_source == "job_title":
        title_source = f"llm+{PROFILE_ID}_style_asset"
    if violations:
        return _AutomaticTitleResult(
            prefixed,
            f"llm+{PROFILE_ID}_style_asset(title_policy_violation)",
            None,
            "title_policy_violation:" + ",".join(violations),
            violations,
        )
    status = (
        "RESOLVED_DETERMINISTIC_FALLBACK"
        if title_source == "selection_hook_fallback_after_llm_mismatch"
        else (
            "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
            if deterministic_filler_repair
            else "RESOLVED_LLM"
        )
    )
    return _AutomaticTitleResult(
        prefixed, title_source, status, None, violations
    )


def _stage_publish_after_release_gate(
    materialized_recut: dict[str, object] | None,
    *,
    decision: ReviewDecision,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    output_dir: Path,
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
    stage_publish: Callable[..., dict[str, object] | None] | None = None,
) -> dict[str, object] | None:
    """Persist the final content decision before any title or cover side effect."""

    recut = dict(materialized_recut or {})
    reason_codes = list(decision.reason_codes)
    gate_satisfied = bool(
        decision.action == DecisionAction.AUTO_UPLOAD
        and not reason_codes
        and recut.get("status") == "MATERIALIZED"
    )
    snapshot_path = output_dir / f"{candidate_id}.cover-release-gate.json"
    snapshot = {
        "schema_version": "slice-cover-release-gate.v1",
        "candidate_id": candidate_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision_action": decision.action.value,
        "reason_codes": reason_codes,
        "materialized_recut_status": recut.get("status"),
        "artifact_hashes": dict(recut.get("artifact_hashes") or {}),
        "satisfied": gate_satisfied,
    }
    _write_json_file(snapshot_path, snapshot)
    if materialized_recut is None:
        # Keep the release decision snapshot as audit evidence, but do not
        # manufacture a recut-shaped record when the singing gate prevented
        # materialization in the first place.
        return None
    recut["cover_release_gate"] = {**snapshot, "path": str(snapshot_path)}

    if not gate_satisfied:
        recut["publish_staging"] = {
            "status": "SKIPPED_RELEASE_GATE",
            "decision_action": decision.action.value,
            "reason_codes": reason_codes,
            "release_gate_path": str(snapshot_path),
            "upload_enabled": False,
        }
        return recut

    staged = (stage_publish or _stage_publish_draft)(
        recut,
        candidate_id=candidate_id,
        title=title,
        cues=cues,
        run_ffmpeg=run_ffmpeg,
        title_llm_call=title_llm_call,
        art_direction_llm_call=art_direction_llm_call,
    )
    if staged is not None:
        staged = dict(staged)
        staged["cover_release_gate"] = recut["cover_release_gate"]
    return staged


def _recovery_publication_staging_state(
    *,
    candidate_id: str,
    title: str,
    title_llm_call: LlmCall | None,
    recovery_publication_authority: Mapping[str, object] | None,
) -> tuple[str, str, dict[str, object] | None]:
    source = "job_title"
    status = (
        "RESOLVED_MANUAL"
        if title_llm_call is None
        else "UNRESOLVED_AUTO"
    )
    if recovery_publication_authority is None:
        return source, status, None
    if title_llm_call is not None:
        raise ValueError(
            "recovery publication authority requires a non-LLM title path"
        )
    try:
        authority = validate_recovery_publication_authority(
            recovery_publication_authority,
            candidate_id=candidate_id,
            expected_final_title=title,
        )
    except RecoveryTitleAuthorityError as exc:
        raise ValueError(
            f"recovery publication authority invalid: {exc}"
        ) from exc
    if authority["title_mode"] == "ivan_manual_override":
        return ("ivan_manual_override", "RESOLVED_MANUAL", authority)
    return (
        "recovery_verified_same_bv_public_title",
        "RESOLVED_RECOVERY_PUBLIC",
        authority,
    )


def _stage_publish_draft(
    materialized_recut: dict[str, object] | None,
    *,
    candidate_id: str,
    title: str,
    cues: Sequence[SourceCue],
    run_ffmpeg: bool,
    title_llm_call: LlmCall | None,
    art_direction_llm_call: LlmCall | None = None,
    skip_cover: bool = False,
    selection_hook: str | None = None,
    cover_diversity_slot: int | None = None,
    recovery_publication_authority: Mapping[str, object] | None = None,
    stage_cover: Callable[..., dict[str, object]] | None = None,
) -> dict[str, object] | None:
    """Mirror production local_prepare: AI title + cover + publish.json draft.

    Always writes ``upload_enabled: false`` — publishing stays behind the
    AUTO_UPLOAD manifest/hash gate and is out of scope for the shadow lane.
    """
    if not materialized_recut or materialized_recut.get("status") != "MATERIALIZED":
        return materialized_recut
    record = dict(materialized_recut)
    media_path = Path(str(record["media_path"]))
    publish_json_path = media_path.with_suffix(".publish.json")
    # Ivan's manual title owns its body. It does not bypass the shared archive
    # envelope: every title receives the channel prefix and the same structural
    # postcondition before cover generation or delivery.
    staged_title = title
    title_policy_violations: list[str] = []
    title_authority_error: str | None = None
    title_state = _recovery_publication_staging_state(
        candidate_id=candidate_id,
        title=title,
        title_llm_call=title_llm_call,
        recovery_publication_authority=recovery_publication_authority,
    )
    title_source, title_authority_status, normalized_recovery_publication_authority = title_state
    story_contract = record.get("story_contract")
    # Ivan 手定标题正文按 candidate 注入：命中后 LLM 不再改正文，但共享
    # publication envelope / structure gate 仍在后面运行。
    manual_override = manual_title_override(candidate_id)
    if manual_override is not None:
        staged_title = manual_override
        title_source = "ivan_manual_override"
        title_authority_status = "RESOLVED_MANUAL"
        title_llm_call = None
    if title_llm_call is not None:
        selection_hook = str(selection_hook or "").strip()
        selection_hook_clause = _selection_hook_first_clause(selection_hook)
        transcript_sample = _staged_transcript_sample(record, cues)
        style_asset = profile_asset_text("title_style")
        persona_asset = profile_asset_text("persona")
        selection_hook_contract = ""
        output_contract = '{"title": "标题"}'
        clip_context_contract = ""
        if isinstance(story_contract, Mapping):
            context_prompt = str(story_contract.get("clip_context_prompt") or "").strip()
            if context_prompt:
                clip_context_contract = (
                    "\n同一份 hash-bound 长程语境（用于整片回指、口癖和专名候选；"
                    "它本身不授权改字幕）：\n" + context_prompt + "\n"
                )
        if selection_hook:
            selection_hook_contract = (
                f"\n选片主钩子（这是为什么选中本片，权威高于后续陪衬话题）: {selection_hook}\n"
                f"标题必须保留第一分句的核心事件: {selection_hook_clause}\n"
                "同时输出 selection_hook_anchor：从该第一分句原样复制的 2–12 字具体短语，"
                f"避开‘{CHANNEL_PROFILE.display_name}/{CHANNEL_PROFILE.short_name}/主播/直播/弹幕/观众/自己/这个/那个/然后/时候/表演’等泛词；"
                "该短语必须逐字出现在标题里。不得把片段后半段的陪衬话题偷换成主标题。\n"
            )
            output_contract = '{"title": "标题", "selection_hook_anchor": "第一分句中的具体短语"}'
        base_prompt = (
            f"为一条{CHANNEL_PROFILE.display_name}(B站虚拟主播)的直播切片起中文标题。\n"
            f"最重要的原则：观众是因为'这是{CHANNEL_PROFILE.display_name}'才点进来的,不是因为内容——标题必须围绕{CHANNEL_PROFILE.display_name}本人"
            "(她的反应、气质、口癖、梗、名字谐音),切片内容只是辅助素材。引人注目为先。\n"
            f"\n{CHANNEL_PROFILE.display_name}特质:\n{persona_asset}\n"
            f"\n标题风格规范与历史标题范例(严格模仿这个风格):\n{style_asset}\n"
            f"\n本切片转写内容节选(辅助素材): {transcript_sample}\n"
            f"{selection_hook_contract}"
            f"{clip_context_contract}"
            f"硬性要求：含{CHANNEL_PROFILE.talk_title_prefix}前缀后 {_TITLE_MIN_LEN}–{_TITLE_MAX_LEN} 字"
            "（Ivan 手定语料的主力带是 25–45 字的三拍叙事，不要为了凑短把梗压没；"
            "只有梗足够硬的短爆点才走 20 字以下）；"
            "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
            "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
            "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
            f"只输出一个 JSON 对象：{output_contract}"
        )
        automatic = _resolve_automatic_title(
            base_prompt=base_prompt,
            title_llm_call=title_llm_call,
            selection_hook=selection_hook,
            initial_title_source=title_source,
        )
        if automatic.staged_title is not None:
            staged_title = automatic.staged_title
        title_source = automatic.title_source
        title_authority_error = automatic.title_authority_error
        title_policy_violations = automatic.title_policy_violations
        if automatic.title_authority_status is not None:
            title_authority_status = automatic.title_authority_status

        # Keep the shared publication choke point authoritative even if a
        # recovery attempt reaches it with an older/blocked automatic-title
        # result.  This is deliberately narrower than regenerating a title:
        # only profile-declared disposable filler words may change, and the
        # cleaned title must independently retain an exact phrase from the
        # authoritative selection hook before the prior block is cleared.
        choke_repaired_title = canonicalize_automatic_title_fillers(
            staged_title
        )
        choke_hook_valid = (
            not selection_hook
            or _selection_hook_has_inferable_anchor(
                selection_hook=selection_hook,
                title=choke_repaired_title,
            )
        )
        if (
            choke_repaired_title != staged_title
            and not _title_policy_violations(choke_repaired_title)
            and choke_hook_valid
            and _TITLE_MIN_LEN
            <= len(_ensure_lidousha_prefix(choke_repaired_title))
            <= _TITLE_MAX_LEN
        ):
            staged_title = choke_repaired_title
            title_source = (
                f"llm+{PROFILE_ID}_style_asset"
                "+deterministic_filler_removal_at_publish_choke"
            )
            title_authority_status = (
                "RESOLVED_DETERMINISTIC_FILLER_REMOVAL"
            )
            title_authority_error = None
            title_policy_violations = []

    # Automatic titles receive deterministic surface canon. A human title body
    # is not silently rewritten; only the channel-owned publish envelope below
    # may be added.
    if title_llm_call is not None:
        staged_title = canonicalize_hard_surfaces(staged_title)
    # Ivan 2026-07-14/19 歌切标题铁律 choke point：自动标题只要带歌切前缀就
    # 折叠成「前缀《歌名》」，任何「｜副标题」/hook 尾巴在这里被最终清除。
    # This automatic-title helper is retained for the retry path; the common
    # publish canonicalizer below applies to manual and automatic titles alike.
    if title_llm_call is not None:
        staged_title = canonicalize_song_catalog_title(staged_title)
    explicit_lane = (
        "song"
        if (
            staged_title.startswith(CHANNEL_PROFILE.song_title_prefix)
            or str(record.get("classification") or "").lower() == "song"
        )
        else "talk"
    )
    staged_title = canonicalize_publish_title(staged_title, lane=explicit_lane)
    common_title_violations = publish_title_policy_violations(
        staged_title,
        lane=explicit_lane,
        enforce_automatic_style=title_llm_call is not None,
    )
    title_policy_violations.extend(
        code
        for code in common_title_violations
        if code not in title_policy_violations
    )
    if common_title_violations:
        title_authority_error = "publish_title_policy_violation:" + ",".join(
            common_title_violations
        )
        title_authority_status = "BLOCKED_PUBLISH_TITLE_POLICY"
    title_story_audit = None
    if isinstance(story_contract, dict):
        title_story_audit = audit_story_artifact(
            staged_title,
            story_contract=story_contract,
            artifact_kind="title",
        )
        if title_story_audit["status"] != "PASS":
            story_codes = sorted(
                {
                    str(row.get("reason_code") or "STORY_CONTRACT_TITLE_FAILED")
                    for row in title_story_audit["violations"]
                    if isinstance(row, dict)
                }
            )
            title_policy_violations.extend(
                code for code in story_codes if code not in title_policy_violations
            )
            title_authority_error = "story_contract_violation:" + ",".join(story_codes)
            title_authority_status = "BLOCKED_STORY_CONTRACT"
    cover_text = _lidousha_cover_text(staged_title)
    if title_authority_error is not None:
        # A candidate id / job fallback is not publish-title authority.  Fail
        # before art direction or any paid image request; the runner will keep
        # this attempt as title_failed and retry it under the bounded policy.
        cover_result = {
            "status": "BLOCKED_TITLE_AUTHORITY",
            "cover_path": None,
            "cover_generation": {
                "status": "NOT_ATTEMPTED",
                "reason": "title authority unresolved before cover generation",
                "attempted_models": [],
            },
            "reason_codes": ["TITLE_AUTHORITY_UNRESOLVED"],
        }
    elif skip_cover:
        # Subtitle-only re-run: keep the existing delivered cover, skip the
        # expensive AI cover (art-direction LLM + gpt-image-2 ~90s/clip).
        cover_result = {
            "status": "REUSED_COVER",
            "cover_path": None,
            "cover_generation": {"status": "REUSED", "note": "subtitle-only re-run: existing cover kept"},
            "reason_codes": [],
        }
    else:
        cover_result = (stage_cover or _stage_lidousha_ai_cover)(
            record,
            media_path=media_path,
            candidate_id=candidate_id,
            title=staged_title,
            cover_text=cover_text,
            run_ffmpeg=run_ffmpeg,
            art_direction_llm_call=art_direction_llm_call,
            # 普通 Ivan 手定标题仍走“每个成分都不许丢”的封面铁律。
            # same-BV recovery 的 verified public title 则只约束公开标题字段：
            # 它可能远长于封面 120px 可读性下限，允许封面从该冻结标题中选择
            # 逐字连续的短梗，但 staged_title 本身仍由 authority 一字不改。
            punch_allowed=(
                title_llm_call is not None
                or title_source
                == "recovery_verified_same_bv_public_title"
            ),
            diversity_slot=cover_diversity_slot,
        )
    cover_status = str(cover_result["status"])
    cover_path_value = cover_result.get("cover_path") if cover_status == "AI_COVER_READY" else None
    cover_generation = cover_result["cover_generation"]
    raw_reason_codes = cover_result.get("reason_codes")
    reason_codes = [str(value) for value in raw_reason_codes] if isinstance(raw_reason_codes, list) else []
    artifact_hashes = {str(k): str(v) for k, v in dict(record.get("artifact_hashes") or {}).items()}
    for key in ("cover_sha256", "ai_background_sha256", "cover_reference_sha256"):
        value = cover_result.get(key)
        if isinstance(value, str) and value:
            artifact_hashes[key] = value

    publish_draft = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": candidate_id,
        "upload_enabled": False,
        "title": staged_title,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "recovery_publication_authority": normalized_recovery_publication_authority,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "title_story_audit": title_story_audit,
        "video_path": str(media_path),
        "cover_text": cover_text,
        "cover_path": cover_path_value,
        "cover_status": cover_status,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "artifact_hashes": artifact_hashes,
    }
    publish_json_path.write_text(json.dumps(publish_draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record["artifact_hashes"] = artifact_hashes
    record["publish_staging"] = {
        "status": "STAGED" if title_authority_error is None else "BLOCKED_TITLE_AUTHORITY",
        "title": staged_title,
        "title_source": title_source,
        "title_authority_status": title_authority_status,
        "recovery_publication_authority": normalized_recovery_publication_authority,
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "title_story_audit": title_story_audit,
        "cover_status": cover_status,
        "cover_path": cover_path_value,
        "cover_text": cover_text,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "publish_json_path": str(publish_json_path),
        "upload_enabled": False,
    }
    return record


def _prepare_lidousha_cover_reference(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    story_contract: Mapping[str, object] | None,
    cover_refs_dir: Path,
    cover_generation: dict[str, object],
) -> tuple[
    Path | None,
    dict[str, object] | None,
    Mapping[str, object] | None,
    dict[str, object] | None,
]:
    """Select, extract, and verify the source-bound cover reference frame."""

    reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"
    # 受监督重产时可指定封面参考帧（内容时间轴毫秒，Ivan 点名画面用）；
    # 未设置则先跑表现力选帧（2026-07-21：动作能量×人声响度×字幕情绪×清晰度，
    # 跳过片头/结尾），失败才落回 thumbnail 代表帧。
    cover_ref_override = os.environ.get("AUTOSLICE_COVER_REF_MS", "").strip()
    reference_authority = (
        story_contract.get("cover_reference_authority")
        if isinstance(story_contract, Mapping)
        and isinstance(story_contract.get("cover_reference_authority"), Mapping)
        else None
    )
    authority_ref_ms = (
        int(reference_authority["content_time_ms"])
        if reference_authority is not None
        else None
    )
    if (
        cover_ref_override.isdigit()
        and authority_ref_ms is not None
        and int(cover_ref_override) != authority_ref_ms
    ):
        return (
            None,
            None,
            reference_authority,
            _blocked_ai_cover_result(
                cover_generation,
                ["COVER_REFERENCE_AUTHORITY_CONFLICT"],
                "AUTOSLICE_COVER_REF_MS conflicts with the hash-bound candidate authority",
            ),
        )
    selected_override_ms = (
        authority_ref_ms
        if authority_ref_ms is not None
        else (int(cover_ref_override) if cover_ref_override.isdigit() else None)
    )
    if reference_authority is not None:
        cover_generation["reference_authority"] = dict(reference_authority)
    frame_selection: dict[str, object] | None = None
    if selected_override_ms is None:
        srt_value = materialized_recut.get("subtitle_path")
        # 片头跳过的权威来源=burn 阶段记录的 intro_offset_ms（片头版本轮换、
        # 时长不一，z1-budui=5749ms 曾越过固定 skip 造成过渡假峰）；record 缺失
        # 时由选帧器内置的切点探测兜底。
        intro_offset = (
            (materialized_recut.get("burned_preview") or {}).get("branding_intro") or {}
        ).get("intro_offset_ms")
        skip_head_ms = DEFAULT_SKIP_HEAD_MS
        if isinstance(intro_offset, (int, float)) and intro_offset > 0:
            skip_head_ms = max(skip_head_ms, int(intro_offset) + 1_500)
        try:
            frame_selection = select_expressive_cover_frame(
                media_path,
                workdir=cover_refs_dir,
                skip_head_ms=skip_head_ms,
                srt_path=(
                    Path(srt_value)
                    if isinstance(srt_value, str) and srt_value and Path(srt_value).is_file()
                    else None
                ),
            )
            cover_generation["reference_selection"] = frame_selection
        except Exception as exc:
            cover_generation["reference_selection"] = {
                "status": "FALLBACK_THUMBNAIL",
                "detail": f"{type(exc).__name__}: {exc}",
            }
    else:
        frame_selection = {
            "schema": "cover-frame-selection.v1",
            "status": (
                "HASH_BOUND_AUTHORITY_OVERRIDE"
                if reference_authority is not None
                else "OPERATOR_OVERRIDE"
            ),
            "best_ms": selected_override_ms,
            "candidates": [{"ms": selected_override_ms, "score": None}],
            "subject_confident": False,
            "authority_override": (
                dict(reference_authority)
                if reference_authority is not None
                else None
            ),
        }
        cover_generation["reference_selection"] = frame_selection
    ref_command = _cover_reference_command(
        media_path=media_path,
        reference_path=reference_path,
        override_ms=selected_override_ms,
        selected_ms=(
            int(frame_selection["best_ms"]) if frame_selection is not None else None
        ),
    )
    completed = subprocess.run(ref_command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not reference_path.is_file():
        cover_generation["reference_command"] = ref_command
        return (
            None,
            frame_selection,
            reference_authority,
            _blocked_ai_cover_result(
                cover_generation,
                ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_FAILED"],
                completed.stderr[-500:] or "reference frame extraction failed",
            ),
        )
    if reference_authority is not None:
        actual_reference_sha256 = "sha256:" + _sha256(reference_path)
        expected_reference_sha256 = str(
            reference_authority["reference_png_sha256"]
        )
        if str(reference_authority["source_sha256"]) not in (
            story_contract.get("source_media_sha256s") or []
        ):
            return (
                None,
                frame_selection,
                reference_authority,
                _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_REFERENCE_SOURCE_BINDING_MISSING"],
                    "the producer-verified source bytes do not match the cover authority",
                ),
            )
        if actual_reference_sha256 != expected_reference_sha256:
            return (
                None,
                frame_selection,
                reference_authority,
                _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_REFERENCE_AUTHORITY_HASH_MISMATCH"],
                    (
                        f"expected {expected_reference_sha256}, got "
                        f"{actual_reference_sha256}"
                    ),
                ),
            )
    return reference_path, frame_selection, reference_authority, None


def _stage_cpa_redraw_cover(
    *,
    candidate_id: str,
    title: str,
    cover_text: str,
    story_contract: Mapping[str, object] | None,
    cover_refs_dir: Path,
    ai_dir: Path,
    covers_dir: Path,
    evidence_dir: Path,
    reference_path: Path,
    emote_library: EmoteLibrary,
    art_direction: LidoushaCoverArtDirection,
    cover_generation: dict[str, object],
    image_edit: Callable[..., dict[str, object]],
    final_participant_verifier: (
        Callable[..., Mapping[str, object]] | None
    ),
    final_host_identity_verifier: (
        Callable[..., Mapping[str, object]] | None
    ),
    base_url: str,
    api_key: str,
) -> dict[str, object]:
    """Resolve optional emote direction and materialize the CPA redraw lane."""

    # Strong-reason emote pick (Ivan 2026-07-19): "replace" swaps the CPA
    # reference from the live frame to the official sticker (subject swap,
    # mutually exclusive with the character redraw); "companion" keeps the
    # frame and insets the sticker (分身 / kmx stand-in).  Any resolution
    # Once the router selected an emote-backed redraw, reference failure is a
    # route failure.  Do not silently change the subject back to the character
    # redraw after the decision receipt has already been issued.
    emote_entry = None
    if art_direction.emote_id:
        entry = emote_library.get(art_direction.emote_id)
        resolved, resolve_detail = (
            resolve_emote_reference(
                entry,
                repo_root=ROOT,
                runtime_roots=emote_library.runtime_roots,
            )
            if entry is not None
            else (None, "EMOTE_ID_UNKNOWN")
        )
        emote_evidence: dict[str, object] = {
            "id": art_direction.emote_id,
            "label": entry.label if entry is not None else None,
            "mode": art_direction.emote_mode,
            "reason": art_direction.emote_reason,
        }
        if resolved is None:
            emote_evidence.update(
                {"status": "BLOCKED_REFERENCE", "detail": resolve_detail}
            )
            cover_generation["emote"] = emote_evidence
            cover_generation["art_direction"] = asdict(art_direction)
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=False,
                image_generation_used=False,
                detail=str(resolve_detail),
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["EMOTE_REFERENCE_REQUIRED", str(resolve_detail)],
                str(resolve_detail),
            )
        else:
            try:
                if art_direction.emote_mode == "companion":
                    reference_path = compose_companion_reference(
                        reference_path,
                        resolved,
                        cover_refs_dir / f"{candidate_id}.cover-ref.with-emote.png",
                    )
                else:
                    reference_path = resolved
                emote_entry = entry
                emote_evidence.update(
                    {
                        "status": "EMOTE_REFERENCE_READY",
                        "hd_file": str(resolved),
                        "hd_sha256": "sha256:" + entry.hd_sha256,
                    }
                )
            except Exception as exc:
                detail = (
                    "EMOTE_COMPANION_COMPOSE_FAILED: "
                    f"{type(exc).__name__}: {exc}"
                )
                emote_evidence.update(
                    {
                        "status": "BLOCKED_REFERENCE",
                        "detail": detail,
                    }
                )
                cover_generation["emote"] = emote_evidence
                cover_generation["art_direction"] = asdict(
                    art_direction
                )
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=False,
                    image_generation_used=False,
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    [
                        "EMOTE_REFERENCE_REQUIRED",
                        "EMOTE_COMPANION_COMPOSE_FAILED",
                    ],
                    detail,
                )
        cover_generation["emote"] = emote_evidence
    cover_generation["art_direction"] = asdict(art_direction)

    ai_background_path = ai_dir / f"{candidate_id}.ai-bg.cpa-image-edit.png"
    request_path = evidence_dir / f"{candidate_id}.cover-cpa-request.redacted.json"
    response_path = evidence_dir / f"{candidate_id}.cover-cpa-response.redacted.json"
    cover_generation.update(
        {
            "method": "images.edit",
        }
    )
    record_cover_route_execution(
        cover_generation,
        actual_treatment=None,
        execution_status="IMAGE_GENERATION_IN_PROGRESS",
        image_generation_attempted=True,
        image_generation_used=False,
    )
    generation_prompt = _lidousha_cover_prompt(
        title=title,
        cover_text=cover_text,
        art_direction=art_direction,
        emote=emote_entry,
    ) + cover_relation_prompt(story_contract)
    try:
        cpa_result = image_edit(
            base_url=base_url,
            api_key=api_key,
            reference_path=reference_path,
            output_path=ai_background_path,
            prompt=generation_prompt,
            request_path=request_path,
            response_path=response_path,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=True,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_IMAGE_EDIT_EXCEPTION"],
            detail,
        )
    cover_generation.update(
        {
            "reference_image": str(reference_path),
            "reference_sha256": "sha256:" + _sha256(reference_path),
            "request_path": str(request_path),
            "response_path": str(response_path),
            "attempted_models": list(cpa_result.get("attempted_models") or []),
            "model_fallback_used": bool(cpa_result.get("model_fallback_used")),
        }
    )
    if cpa_result.get("selected_model"):
        cover_generation["model"] = str(cpa_result["selected_model"])
    if cpa_result.get("status") != "AI_BACKGROUND_READY" or not ai_background_path.is_file():
        cover_generation["cpa_status"] = cpa_result.get("status")
        detail = str(
            cpa_result.get("detail") or "CPA image edit did not return an image"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=True,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            [
                "CPA_AI_COVER_REQUIRED",
                str(cpa_result.get("reason_code") or "CPA_IMAGE_EDIT_FAILED"),
            ],
            detail,
        )

    final_cover_path = covers_dir / f"{candidate_id}.ai-title.cover.png"
    overlay = _overlay_lidousha_cover_title(
        ai_background_path,
        final_cover_path,
        cover_text=cover_text,
        art_direction=art_direction,
    )
    cover_generation.update(
        {
            "method": "images.edit",
            "cover_origin": "AI_REDRAW",
            "image_generation_used": True,
            "ai_background": str(ai_background_path),
            "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
            "final_cover": str(final_cover_path),
            "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
            **overlay,
        }
    )
    route = cover_generation.get("route_decision")
    if (
        isinstance(route, Mapping)
        and route.get("host_identity_required") is True
    ):
        assert final_host_identity_verifier is not None
        def verify_current_final() -> None:
            try:
                cover_generation[
                    "final_host_identity_verification"
                ] = dict(
                    final_host_identity_verifier(
                        final_cover_path=Path(
                            str(cover_generation["final_cover"])
                        ),
                        final_cover_sha256=cover_generation[
                            "final_cover_sha256"
                        ],
                        reference_path=reference_path,
                        base_url=base_url,
                        api_key=api_key,
                    )
                )
            except Exception as exc:
                cover_generation[
                    "final_host_identity_verification"
                ] = {
                    "status": "FAIL",
                    "reason_code": "HOST_IDENTITY_VERIFIER_EXCEPTION",
                    "detail": f"{type(exc).__name__}: {exc}",
                }

        verify_current_final()
        if (
            not validate_final_host_identity_verification(cover_generation)
            and not final_host_identity_witness_unavailable(cover_generation)
        ):
            first_verification = cover_generation.get(
                "final_host_identity_verification"
            )
            cover_generation[
                "rejected_final_host_identity_verification"
            ] = dict(
                first_verification
                if isinstance(first_verification, Mapping)
                else {}
            )
            retry_background = (
                ai_dir / f"{candidate_id}.ai-bg.host-identity-retry.png"
            )
            retry_final = (
                covers_dir / f"{candidate_id}.ai-title.host-identity-retry.cover.png"
            )
            retry_request = (
                evidence_dir
                / f"{candidate_id}.cover-cpa-host-identity-retry.request.redacted.json"
            )
            retry_response = (
                evidence_dir
                / f"{candidate_id}.cover-cpa-host-identity-retry.response.redacted.json"
            )
            retry_result: Mapping[str, object]
            try:
                retry_result = image_edit(
                    base_url=base_url,
                    api_key=api_key,
                    reference_path=reference_path,
                    output_path=retry_background,
                    prompt=(
                        generation_prompt
                        + " IDENTITY RETRY AFTER FAILED FINAL-PIXEL CHECK: "
                        "the previous output copied a different source participant. "
                        "Re-read visible source nameplates. The large protagonist "
                        "must be the person labelled 李豆沙; do not hybridize her "
                        "with any other participant, even if panda ears are added."
                    ),
                    request_path=retry_request,
                    response_path=retry_response,
                )
            except Exception as exc:
                retry_result = {
                    "status": "FAILED",
                    "reason_code": "HOST_IDENTITY_RETRY_EXCEPTION",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            retry_ready = bool(
                retry_result.get("status") == "AI_BACKGROUND_READY"
                and retry_background.is_file()
            )
            cover_generation["host_identity_retry"] = {
                "schema_version": "lidousha-cover-host-identity-retry.v1",
                "attempted": True,
                "request_path": str(retry_request),
                "response_path": str(retry_response),
                "result_status": retry_result.get("status"),
                "reason_code": retry_result.get("reason_code"),
                "status": "GENERATED_PENDING_VERIFICATION" if retry_ready else "FAILED",
            }
            if retry_ready:
                retry_overlay = _overlay_lidousha_cover_title(
                    retry_background,
                    retry_final,
                    cover_text=cover_text,
                    art_direction=art_direction,
                )
                cover_generation.update(
                    {
                        "ai_background": str(retry_background),
                        "ai_background_sha256": "sha256:"
                        + _sha256(retry_background),
                        "final_cover": str(retry_final),
                        "final_cover_sha256": "sha256:"
                        + _sha256(retry_final),
                        "identity_retry_request_path": str(retry_request),
                        "identity_retry_response_path": str(retry_response),
                        **retry_overlay,
                    }
                )
                verify_current_final()
                cover_generation["host_identity_retry"]["status"] = (
                    "PASS"
                    if validate_final_host_identity_verification(
                        cover_generation
                    )
                    else "FAILED_FINAL_IDENTITY"
                )
                final_cover_path = retry_final
                ai_background_path = retry_background
        if not validate_final_host_identity_verification(cover_generation):
            verification = cover_generation.get(
                "final_host_identity_verification"
            )
            detail = (
                "AI cover final pixels do not have a PASS Li Dousha host "
                "identity verdict: "
                + str(
                    (
                        verification
                        if isinstance(verification, Mapping)
                        else {}
                    ).get("reason_code")
                    or "VERIFICATION_MISSING"
                )
            )
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"],
                detail,
            )
    relationship_visual_required = relationship_visual_safety_required(
        story_contract,
        route_decision=route,
    )
    final_participant_verification = None
    if relationship_visual_required:
        assert final_participant_verifier is not None
        try:
            final_participant_verification = dict(
                final_participant_verifier(
                    final_cover_path=final_cover_path,
                    final_cover_sha256=cover_generation[
                        "final_cover_sha256"
                    ],
                    required_participant_ids=list(
                        route.get("required_participant_ids") or []
                    ),
                    source_reference_authority=(
                        story_contract.get("cover_reference_authority")
                        if isinstance(story_contract, Mapping)
                        else None
                    ),
                )
            )
        except Exception as exc:
            detail = (
                "final participant verification failed: "
                f"{type(exc).__name__}: {exc}"
            )
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["RELATION_COVER_FINAL_PARTICIPANT_VERIFICATION_FAILED"],
                detail,
            )
        cover_generation["final_participant_verification"] = (
            final_participant_verification
        )
        if not validate_final_participant_verification(cover_generation):
            detail = (
                "AI redraw lacks a PASS verdict bound to the final cover hash "
                "that verifies every required participant"
            )
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=True,
                image_generation_used=True,
                detail=detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["RELATION_COVER_FINAL_PARTICIPANTS_UNVERIFIED"],
                detail,
            )
    record_cover_route_execution(
        cover_generation,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        final_participant_verification=final_participant_verification,
    )
    return {
        "status": "AI_COVER_READY",
        "reason_codes": [],
        "cover_path": str(final_cover_path),
        "cover_generation": cover_generation,
        "cover_sha256": "sha256:" + _sha256(final_cover_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
        "cover_reference_sha256": "sha256:" + _sha256(reference_path),
    }


def _build_lidousha_cover_route(
    *,
    cover_generation: dict[str, object],
    story_contract: object,
    title: str,
    cover_text: str,
    cover_mode: str,
    art_direction: LidoushaCoverArtDirection,
    punch_allowed: bool,
    frame_selection: Mapping[str, object] | None,
    reference_authority: Mapping[str, object] | None,
    enforce_final_host_identity: bool = False,
) -> tuple[str, dict[str, object]]:
    """Build the semantic-first route record before any cover materialization."""

    required_participant_ids = story_participant_ids(story_contract)
    visible_participant_ids = source_visible_participant_ids(
        reference_authority
    )
    relationship_source_verified = bool(
        required_participant_ids
        and set(required_participant_ids) == set(visible_participant_ids)
    )
    verified_stream_frame = is_hash_bound_reference_authority(
        reference_authority
    )
    treatment, treatment_reason = _decide_cover_treatment(
        cover_mode=cover_mode,
        is_song=art_direction.is_song,
        punch_allowed=punch_allowed,
        frame_selection=frame_selection,
        verified_stream_frame=verified_stream_frame,
        relationship_visual_required=(
            relationship_visual_safety_required(story_contract)
        ),
        relationship_source_verified=relationship_source_verified,
    )
    cover_generation["cover_treatment"] = {
        "treatment": treatment,
        "reason": treatment_reason,
    }
    candidates = (
        frame_selection.get("candidates")
        if isinstance(frame_selection, Mapping)
        else None
    )
    first_candidate = (
        candidates[0]
        if isinstance(candidates, list)
        and candidates
        and isinstance(candidates[0], Mapping)
        else {}
    )
    route = build_cover_route_decision(
        selected_treatment=treatment,
        selected_rationale=treatment_reason,
        story_contract=story_contract,
        reference_authority=reference_authority,
        title=title,
        cover_text=cover_text,
        decision_inputs={
            "cover_mode": cover_mode,
            "is_song": art_direction.is_song,
            "manual_title_or_full_text_contract": not punch_allowed,
            "frame_score": first_candidate.get("score"),
            "frame_emotion": first_candidate.get("emotion"),
            "subject_confident": (
                frame_selection.get("subject_confident")
                if isinstance(frame_selection, Mapping)
                else None
            ),
            "motion_dispersion_frac": (
                frame_selection.get("motion_dispersion_frac")
                if isinstance(frame_selection, Mapping)
                else None
            ),
            "verified_stream_frame": verified_stream_frame,
            "reference_authority_id": (
                reference_authority.get("candidate_id")
                if reference_authority is not None
                else None
            ),
            "host_identity_required": bool(
                enforce_final_host_identity
                and treatment in ("screenshot_polish", "cpa_redraw")
            ),
        },
    )
    cover_generation["route_decision"] = route
    record_cover_route_execution(
        cover_generation,
        actual_treatment=None,
        execution_status="PENDING",
        image_generation_attempted=False,
        image_generation_used=False,
    )
    return treatment, route


def _stage_lidousha_ai_cover(
    materialized_recut: Mapping[str, object],
    *,
    media_path: Path,
    candidate_id: str,
    title: str,
    cover_text: str,
    run_ffmpeg: bool,
    art_direction_llm_call: LlmCall | None = None,
    image_edit: Callable[..., dict[str, object]] = _cover_call_cpa_image_edit,
    final_participant_verifier: (
        Callable[..., Mapping[str, object]] | None
    ) = None,
    final_host_identity_verifier: (
        Callable[..., Mapping[str, object]] | None
    ) = None,
    enforce_final_host_identity: bool = False,
    punch_allowed: bool = False,
    diversity_slot: int | None = None,
) -> dict[str, object]:
    cover_generation: dict[str, object] = {
        "workflow": LIDOUSHA_COVER_WORKFLOW,
        "method": "images.edit",
        "model": _cpa_image_model_candidates()[0],
        "image_gen_model": "cpa",
        "fallback_used": False,
        "model_fallback_used": False,
        "cover_text": cover_text,
        "cover_punch_allowed": punch_allowed,
        "cover_diversity_slot": diversity_slot,
        "title": title,
    }
    story_contract = materialized_recut.get("story_contract")
    if isinstance(story_contract, Mapping):
        cover_generation["story_contract"] = cover_story_contract_binding(
            story_contract
        )
    # 封面路线（2026-07-21 Ivan："加入判断，哪些适合全图 CPA 重做、哪些适合截图"）：
    # AUTOSLICE_COVER_MODE = auto（默认，按名场面强度路由）| screenshot（强制直出）
    # | polish（强制截图+CPA 轻微调）| cpa（强制全图重绘，旧行为）。
    # 凭据门只对强制 cpa 模式前置；其余路线推迟到真正要调 CPA 时再卡。
    cover_mode = (os.environ.get("AUTOSLICE_COVER_MODE", "").strip().lower() or "auto")
    if cover_mode not in ("auto", "screenshot", "polish", "cpa"):
        cover_mode = "auto"
    cover_generation["cover_mode"] = cover_mode
    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if (not base_url or not api_key) and cover_mode == "cpa":
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            "CPA_BASE_URL/CPA_API_KEY missing; deterministic frame covers are not publish-grade",
        )
    if not run_ffmpeg:
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_DISABLED"],
            "ffmpeg disabled, so no identity/reference frame can be extracted for CPA images.edit",
        )

    artifact_root = _materialized_artifact_root(materialized_recut, media_path)
    cover_refs_dir = artifact_root / "cover_refs"
    ai_dir = artifact_root / "covers_ai_original"
    covers_dir = artifact_root / "covers"
    evidence_dir = artifact_root / "evidence"
    for directory in (cover_refs_dir, ai_dir, covers_dir, evidence_dir):
        directory.mkdir(parents=True, exist_ok=True)

    (
        reference_path,
        frame_selection,
        reference_authority,
        reference_block,
    ) = _prepare_lidousha_cover_reference(
        materialized_recut,
        media_path=media_path,
        candidate_id=candidate_id,
        story_contract=(
            story_contract if isinstance(story_contract, Mapping) else None
        ),
        cover_refs_dir=cover_refs_dir,
        cover_generation=cover_generation,
    )
    if reference_block is not None:
        return reference_block
    assert reference_path is not None

    # Art direction is picked AFTER the fail-closed gates (creds/ffmpeg/ref frame)
    # so a blocked cover never spends an LLM call. It is fail-OPEN (deterministic
    # baseline) while the cover IMAGE stays fail-closed.
    emote_library = load_emote_library(ROOT)
    art_direction = _lidousha_cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        art_direction_llm_call=art_direction_llm_call,
        emote_library=emote_library,
        allow_punch=punch_allowed,
        diversity_slot=diversity_slot,
        story_hook=(
            str(story_contract.get("selection_hook") or "")
            if isinstance(story_contract, Mapping)
            else ""
        ),
    )

    # 路由：语义/人物证据先行，几何只决定已经验真人物的构图处理。
    treatment, route = _build_lidousha_cover_route(
        cover_generation=cover_generation,
        story_contract=story_contract,
        title=title,
        cover_text=cover_text,
        cover_mode=cover_mode,
        art_direction=art_direction,
        punch_allowed=punch_allowed,
        frame_selection=frame_selection,
        reference_authority=reference_authority,
        enforce_final_host_identity=enforce_final_host_identity,
    )
    if reference_authority is not None and treatment != reference_authority.get(
        "required_treatment"
    ):
        detail = (
            f"authority requires {reference_authority.get('required_treatment')}, "
            f"router selected {treatment}"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["COVER_REFERENCE_REQUIRED_TREATMENT_NOT_SELECTED"],
            detail,
        )
    relationship_visual_required = relationship_visual_safety_required(
        story_contract,
        route_decision=route,
    )
    if relationship_visual_required and not (
        relationship_source_participants_verified(route)
    ):
        detail = (
            "relationship cover requires a hash-bound source reference that "
            "visibly verifies every required participant before composition "
            "geometry or image generation can be considered"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["RELATION_COVER_SOURCE_PARTICIPANTS_UNVERIFIED"],
            detail,
        )
    if isinstance(story_contract, Mapping):
        cover_generation["relation_cover_mode"] = (
            "VERIFIED_DUAL_STREAM_FRAME"
            if reference_authority is not None
            and treatment in ("screenshot_direct", "screenshot_polish")
            else "VERIFIED_STREAM_FRAME"
            if treatment in ("screenshot_direct", "screenshot_polish")
            else str(story_contract.get("cover_fallback_mode") or "HOST_ONLY_GENERIC")
        )
    if treatment in ("screenshot_direct", "screenshot_polish"):
        # The selected route is an authorization boundary.  A materialization
        # failure blocks this cover; it never authorizes a silent CPA redraw.
        return _stage_screenshot_direct_cover(
            media_path=media_path,
            candidate_id=candidate_id,
            cover_text=cover_text,
            art_direction=art_direction,
            frame_selection=frame_selection,
            reference_path=reference_path,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            evidence_dir=evidence_dir,
            cover_generation=cover_generation,
            polish=(treatment == "screenshot_polish"),
            image_edit=image_edit,
            final_participant_verifier=final_participant_verifier,
            final_host_identity_verifier=final_host_identity_verifier,
            base_url=base_url,
            api_key=api_key,
        )
    if (
        route.get("host_identity_required") is True
        and final_host_identity_verifier is None
    ):
        detail = (
            "AI-modified cover requires an AGY source/final Li Dousha "
            "identity verifier bound to the final cover hash"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["COVER_FINAL_HOST_IDENTITY_VERIFIER_REQUIRED"],
            detail,
        )
    if relationship_visual_required and final_participant_verifier is None:
        detail = (
            "relationship AI redraw requires an independent verifier bound "
            "to the final cover hash and every required participant"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["RELATION_COVER_FINAL_PARTICIPANT_VERIFIER_REQUIRED"],
            detail,
        )
    if not base_url or not api_key:
        detail = (
            "CPA_BASE_URL/CPA_API_KEY missing for the selected cpa_redraw route"
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=False,
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "CPA_CREDENTIALS_MISSING"],
            detail,
        )

    redraw_result = _stage_cpa_redraw_cover(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        story_contract=(
            story_contract if isinstance(story_contract, Mapping) else None
        ),
        cover_refs_dir=cover_refs_dir,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        reference_path=reference_path,
        emote_library=emote_library,
        art_direction=art_direction,
        cover_generation=cover_generation,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
    )
    return _degrade_unavailable_redraw_identity_to_direct(
        redraw_result=redraw_result,
        media_path=media_path,
        candidate_id=candidate_id,
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
    )

def _cover_reference_command(
    *,
    media_path: Path,
    reference_path: Path,
    override_ms: int | None,
    selected_ms: int | None,
) -> list[str]:
    """封面参考帧的 ffmpeg 命令：人工点名帧 > 表现力选帧 > thumbnail 代表帧。"""

    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    at_ms = override_ms if override_ms is not None else selected_ms
    if at_ms is not None:
        return base + [
            "-ss", f"{at_ms / 1000:.3f}", "-i", str(media_path),
            "-vf", "scale=1920:-2", "-frames:v", "1", str(reference_path),
        ]
    return base + [
        "-i", str(media_path),
        "-vf", "thumbnail=120,scale=1920:-2", "-frames:v", "1", str(reference_path),
    ]


_COVER_TREATMENT_SCORE_HI = 4.5
_COVER_TREATMENT_SCORE_LO = 2.6
_COVER_SUBJECT_MAX_MOTION_DISPERSION = 0.50


def _decide_cover_treatment(
    *,
    cover_mode: str,
    is_song: bool,
    punch_allowed: bool,
    frame_selection: Mapping[str, object] | None,
    verified_stream_frame: bool = False,
    relationship_visual_required: bool = False,
    relationship_source_verified: bool = False,
) -> tuple[str, str]:
    """每条切片选封面路线（2026-07-21 Ivan：哪些适合全图 CPA 重做、哪些适合截图）。

    判据=表现力选帧最高分（"这条片有没有值得原样示人的真名场面"）：
    - 歌切 / 选帧失败 → cpa_redraw（唱歌净美学 / 无帧可用）
    - 强名场面（≥4.5，或 ≥3.2 且命中情绪字幕段）→ screenshot_direct：真表情就是
      封面，重绘反而丢梗
    - 中等瞬间（≥2.6）→ screenshot_polish：保真帧构图，CPA 只清杂物修画质
    - 更低 → cpa_redraw：没有好瞬间，插画重做的承载力更强
    阈值标定自 2026-07-15/19 十七条本地成片修掉片头假峰后的分数分布
    （强：5.1-9.1；中：2.8-3.9；弱：1.9-2.1）。
    """

    if cover_mode == "cpa":
        return "cpa_redraw", "mode=cpa (forced)"
    if is_song:
        return "cpa_redraw", "song keeps the clean CPA aesthetic"
    if relationship_visual_required:
        if cover_mode == "polish":
            return "screenshot_polish", "mode=polish (forced)"
        if relationship_source_verified:
            return (
                "screenshot_direct",
                "hash-bound source frame verifies all required participants",
            )
        return (
            "screenshot_direct",
            "relationship hook requires source-verified participants before "
            "composition scoring",
        )
    if verified_stream_frame:
        return (
            "screenshot_direct",
            "hash-bound source frame verifies all required participants",
        )
    if frame_selection is None:
        return "cpa_redraw", "frame selection unavailable"
    if cover_mode == "screenshot":
        return "screenshot_direct", "mode=screenshot (forced)"
    if cover_mode == "polish":
        return "screenshot_polish", "mode=polish (forced)"
    candidates = frame_selection.get("candidates") or []
    best = float(candidates[0]["score"]) if candidates else 0.0
    emotional = bool(candidates and candidates[0].get("emotion"))
    # A narrow local motion box is not sufficient proof of a usable cover
    # subject when the rest of the scene is moving across most of the canvas.
    # The 2026-07-22 game-UI miss reported a plausible local box but a 0.6033
    # accumulated-motion footprint; screenshot polish consequently preserved a
    # mostly empty game panel with tiny avatars in one corner.  Prefer the CPA
    # big-face redraw whenever global motion is that dispersed.  Missing legacy
    # evidence remains compatible with the earlier subject-confidence gate.
    raw_dispersion = frame_selection.get("motion_dispersion_frac")
    try:
        motion_dispersion = (
            float(raw_dispersion) if raw_dispersion is not None else None
        )
    except (TypeError, ValueError):
        motion_dispersion = None
    subject_confident = frame_selection.get("subject_confident") is True and (
        motion_dispersion is None
        or motion_dispersion <= _COVER_SUBJECT_MAX_MOTION_DISPERSION
    )
    if subject_confident and (
        best >= _COVER_TREATMENT_SCORE_HI or (emotional and best >= 3.2)
    ):
        return "screenshot_direct", f"strong real moment (score={best:.2f})"
    if subject_confident and best >= _COVER_TREATMENT_SCORE_LO:
        return "screenshot_polish", f"usable moment + CPA touch-up (score={best:.2f})"
    if best >= _COVER_TREATMENT_SCORE_LO:
        # 游戏场小窗回归（2026-07-25 Ivan：截图修图优先于重绘）：全局运动
        # 弥散但探测到位置固定的立绘小窗时，裁窗放大做截图底走 polish，
        # 真名场面不再被"无自信主体"一票否决；无窗才落重绘。
        if frame_selection.get("camera_window_bbox_frac"):
            return (
                "screenshot_polish",
                f"camera window crop + CPA touch-up (score={best:.2f})",
            )
        return "cpa_redraw", f"motion without confident cover subject (score={best:.2f})"
    return "cpa_redraw", f"no strong real moment (score={best:.2f})"


def _screenshot_base_and_crop(
    *,
    media_path: Path,
    candidate_id: str,
    ai_dir: Path,
    reference_path: Path,
    frame_selection: Mapping[str, object],
    art_direction,
    relationship_visual_required: bool,
):
    """Materialize the screenshot base frame and its hash-bound crop proof."""

    if relationship_visual_required:
        # A relationship cover cannot inherit the reference frame's
        # participant verdict through a zoom crop.  Keep the exact,
        # hash-bound full reference and reserve a separate banner for text.
        art_direction = dataclasses_replace(
            art_direction,
            layout="banner",
        )
        screenshot_base = reference_path
        crop_evidence = {
            "schema": "cover-frame-transfer.v1",
            "status": "HASH_BOUND_FULL_FRAME",
            # This stays on the content timeline.  The hash-bound source
            # authority may also carry an absolute source_time_ms, but the
            # transfer proof must bind the exact frame selected from the
            # materialized clip just like the cropped screenshot route.
            "frame_ms": int(frame_selection["best_ms"]),
            "source_path": str(reference_path),
            "source_sha256": "sha256:" + _sha256(reference_path),
            "crop_applied": False,
            "zoom": 1.0,
        }
    else:
        screenshot_base = ai_dir / f"{candidate_id}.screenshot-base.png"
    # 裁切策略（2026-07-21 辣妹案标定）：运动几何分不开"皮套大身位"和竖版
    # 手游列（都窄而高），真正的脸部识别放大要等 CPA-primary 图像见证。v1 保守：
    # 默认 1.16x 顶部锚定——恰好裁掉底部烧录字幕带、微裁两侧，任何场景都
    # 安全；只有局部运动呈高置信单主体块时才 1.32x 锚定主体（宁欠勿错）。
    if not relationship_visual_required:
        confident = bool(frame_selection.get("subject_confident"))
        camera_window = (
            frame_selection.get("camera_window_bbox_frac")
            if not confident
            else None
        )
        crop_evidence = extract_zoomed_cover_frame(
            media_path,
            int(frame_selection["best_ms"]),
            screenshot_base,
            zoom=1.32 if confident else 1.16,
            anchor_x_frac=(
                float(frame_selection["subject_anchor_x_frac"])
                if confident and frame_selection.get("subject_anchor_x_frac") is not None
                # 本频道版式皮套居中偏右、弹幕栏在左：右倾锚点让 1.16x 裁切
                # 优先吃掉左侧弹幕栏。
                else 0.58
            ),
            head_top_frac=(
                float(frame_selection["subject_head_top_frac"])
                if confident and frame_selection.get("subject_head_top_frac") is not None
                else 0.0
            ),
            window_bbox_frac=camera_window,
        )
    return art_direction, screenshot_base, crop_evidence


def _stage_screenshot_direct_cover(
    *,
    media_path: Path,
    candidate_id: str,
    cover_text: str,
    art_direction,
    frame_selection: Mapping[str, object],
    reference_path: Path,
    ai_dir: Path,
    covers_dir: Path,
    cover_generation: dict[str, object],
    evidence_dir: Path | None = None,
    polish: bool = False,
    image_edit: Callable[..., dict[str, object]] | None = None,
    final_participant_verifier: (
        Callable[..., Mapping[str, object]] | None
    ) = None,
    final_host_identity_verifier: (
        Callable[..., Mapping[str, object]] | None
    ) = None,
    base_url: str = "",
    api_key: str = "",
) -> dict[str, object]:
    """截图路线封面：直出或 +CPA 轻微调；物化失败原路线内阻断。

    表现力选帧的最佳帧 → 裁切（吃掉弹幕栏/字幕带）→ [polish：CPA 逐像素保真
    修图（清 UI 杂物+画质），失败显式降级直出] → 叠梗字。截图/裁切/叠字
    任一步失败都保留证据并 fail closed，绝不静默切换成全图 AI 重绘。
    """

    # 手定标题只锁文字 authority，不再偷偷决定视觉路线。自动标题有短梗字时
    # 仍用 punch 版式；手定标题没有 punch 时，复用成熟的完整 cover_text
    # banner 叠字器，确保所有成分一字不丢，同时保留真实截图。
    cover_generation.update(
        {
            "method": "screenshot_polish" if polish else "screenshot_direct",
            "model": "cpa" if polish else "none",
            "image_gen_model": "cpa" if polish else "none",
            "image_generation_used": False,
        }
    )
    try:
        route = cover_generation.get("route_decision")
        relationship_visual_required = relationship_visual_safety_required(
            cover_generation.get("story_contract"),
            route_decision=route,
        )
        art_direction, screenshot_base, crop_evidence = (
            _screenshot_base_and_crop(
                media_path=media_path,
                candidate_id=candidate_id,
                ai_dir=ai_dir,
                reference_path=reference_path,
                frame_selection=frame_selection,
                art_direction=art_direction,
                relationship_visual_required=relationship_visual_required,
            )
        )
        # polish：CPA 保真修图（清 UI 杂物+画质），任何失败降级为直出。
        (
            overlay_source,
            method,
            selected_model,
            attempted_models,
            polish_attempted,
        ) = _materialize_screenshot_polish(
            screenshot_base=screenshot_base,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            evidence_dir=evidence_dir,
            polish=polish,
            image_edit=image_edit,
            base_url=base_url,
            api_key=api_key,
            cover_generation=cover_generation,
        )
        (
            poster_evidence,
            overlay,
            face_verification,
        ) = _compose_screenshot_cover_with_face_gate(
            overlay_source=overlay_source,
            crop_evidence=crop_evidence,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            cover_text=cover_text,
            art_direction=art_direction,
            relationship_visual_required=relationship_visual_required,
            method=method,
            base_url=base_url,
            api_key=api_key,
            verifier=_verify_polish_face_integrity,
        )
        (
            poster_evidence,
            overlay,
            face_verification,
            method,
            selected_model,
        ) = _degrade_rejected_polish_to_direct(
            poster_evidence=poster_evidence,
            overlay=overlay,
            face_verification=face_verification,
            method=method,
            selected_model=selected_model,
            screenshot_base=screenshot_base,
            crop_evidence=crop_evidence,
            candidate_id=candidate_id,
            ai_dir=ai_dir,
            covers_dir=covers_dir,
            cover_text=cover_text,
            art_direction=art_direction,
            relationship_visual_required=relationship_visual_required,
            base_url=base_url,
            api_key=api_key,
            cover_generation=cover_generation,
            verifier=_verify_polish_face_integrity,
        )
        poster_path = ai_dir / f"{candidate_id}.screenshot-poster.png"
        final_cover_path = covers_dir / f"{candidate_id}.screenshot-title.cover.png"
        if face_verification is not None:
            cover_generation["polish_face_verification"] = face_verification
        overlay_source = poster_path
        cover_generation["screenshot_graphic_poster"] = poster_evidence
        cover_generation["art_direction"] = asdict(art_direction)
        if art_direction.emote_id:
            cover_generation["emote"] = {"status": "IGNORED_SCREENSHOT_MODE"}
        cover_generation.update(
            {
                "method": method,
                "model": selected_model,
                "image_gen_model": selected_model,
                "cover_origin": (
                    "SOURCE_SCREENSHOT"
                    if method == "screenshot_direct"
                    else "SOURCE_SCREENSHOT_AI_POLISH"
                ),
                "image_generation_used": method == "screenshot_polish",
                "screenshot_frame": crop_evidence,
                "reference_image": str(reference_path),
                "reference_sha256": "sha256:" + _sha256(reference_path),
                "ai_background": str(overlay_source),
                "ai_background_sha256": "sha256:" + _sha256(overlay_source),
                "final_cover": str(final_cover_path),
                "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
                "attempted_models": attempted_models,
                **overlay,
            }
        )
        if (
            method == "screenshot_polish"
            and isinstance(route, Mapping)
            and route.get("host_identity_required") is True
        ):
            if final_host_identity_verifier is None:
                host_identity_verification: Mapping[str, object] = {
                    "status": "FAIL",
                    "reason_code": "HOST_IDENTITY_VERIFIER_MISSING",
                }
            else:
                host_identity_verification = dict(
                    final_host_identity_verifier(
                        final_cover_path=final_cover_path,
                        final_cover_sha256=cover_generation[
                            "final_cover_sha256"
                        ],
                        reference_path=reference_path,
                        base_url=base_url,
                        api_key=api_key,
                    )
                )
            cover_generation["final_host_identity_verification"] = dict(
                host_identity_verification
            )
            if not validate_final_host_identity_verification(
                cover_generation
            ):
                detail = (
                    "AI-polished cover final pixels do not have a PASS "
                    "Li Dousha host identity verdict: "
                    + str(
                        host_identity_verification.get("reason_code")
                        or "VERIFICATION_MISSING"
                    )
                )
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=polish_attempted,
                    image_generation_used=True,
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    ["COVER_FINAL_HOST_IDENTITY_UNVERIFIED"],
                    detail,
                )
        polish_face_detail = _polish_face_binding_failure(
            cover_generation, face_verification, method
        )
        if polish_face_detail is not None:
            record_cover_route_execution(
                cover_generation,
                actual_treatment=None,
                execution_status="BLOCKED",
                image_generation_attempted=polish_attempted,
                image_generation_used=True,
                detail=polish_face_detail,
            )
            return _blocked_ai_cover_result(
                cover_generation,
                ["COVER_POLISH_FACE_UNVERIFIED"],
                polish_face_detail,
            )
        final_participant_verification = None
        if relationship_visual_required:
            if method == "screenshot_direct":
                final_participant_verification = (
                    build_no_crop_participant_verification(
                        cover_generation
                    )
                )
            elif final_participant_verifier is not None:
                final_participant_verification = dict(
                    final_participant_verifier(
                        final_cover_path=final_cover_path,
                        final_cover_sha256=cover_generation[
                            "final_cover_sha256"
                        ],
                        required_participant_ids=list(
                            route.get("required_participant_ids") or []
                        ),
                        source_reference_authority=(
                            cover_generation.get("story_contract", {}).get(
                                "cover_reference_authority"
                            )
                            if isinstance(
                                cover_generation.get("story_contract"),
                                Mapping,
                            )
                            else None
                        ),
                    )
                )
            if final_participant_verification is not None:
                cover_generation["final_participant_verification"] = (
                    final_participant_verification
                )
            if not validate_final_participant_verification(
                cover_generation
            ):
                detail = (
                    "relationship screenshot lacks a PASS final-pixel "
                    "participant verdict bound to the final cover hash"
                )
                record_cover_route_execution(
                    cover_generation,
                    actual_treatment=None,
                    execution_status="BLOCKED",
                    image_generation_attempted=polish_attempted,
                    image_generation_used=method == "screenshot_polish",
                    detail=detail,
                )
                return _blocked_ai_cover_result(
                    cover_generation,
                    [
                        "RELATION_COVER_FINAL_PARTICIPANTS_UNVERIFIED"
                    ],
                    detail,
                )
        degraded = polish and method == "screenshot_direct"
        record_cover_route_execution(
            cover_generation,
            actual_treatment=method,
            execution_status="READY_DEGRADED" if degraded else "READY",
            image_generation_attempted=polish_attempted,
            image_generation_used=method == "screenshot_polish",
            final_participant_verification=final_participant_verification,
            detail=(
                str(
                    (
                        cover_generation.get("screenshot_polish")
                        if isinstance(
                            cover_generation.get("screenshot_polish"), Mapping
                        )
                        else {}
                    ).get("detail")
                    or "screenshot polish failed; direct source screenshot retained"
                )
                if degraded
                else None
            ),
        )
        return {
            "status": "AI_COVER_READY",
            "reason_codes": [],
            "cover_path": str(final_cover_path),
            "cover_generation": cover_generation,
            "cover_sha256": "sha256:" + _sha256(final_cover_path),
            "ai_background_sha256": "sha256:" + _sha256(overlay_source),
            "cover_reference_sha256": "sha256:" + _sha256(reference_path),
        }
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        cover_generation["screenshot_direct"] = {
            "status": "BLOCKED",
            "reason_code": "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
            "detail": detail,
        }
        route = cover_generation.get("route_decision")
        selected = (
            str(route.get("selected_treatment") or "")
            if isinstance(route, Mapping)
            else ""
        )
        record_cover_route_execution(
            cover_generation,
            actual_treatment=None,
            execution_status="BLOCKED",
            image_generation_attempted=bool(
                isinstance(
                    cover_generation.get("screenshot_polish"), Mapping
                )
                and cover_generation["screenshot_polish"].get(
                    "image_generation_attempted"
                )
            ),
            image_generation_used=False,
            detail=detail,
        )
        return _blocked_ai_cover_result(
            cover_generation,
            [
                "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
                (
                    "SCREENSHOT_POLISH_ROUTE_FAILED"
                    if selected == "screenshot_polish"
                    else "SCREENSHOT_DIRECT_ROUTE_FAILED"
                ),
            ],
            detail,
        )


def _degrade_unavailable_redraw_identity_to_direct(
    *,
    redraw_result: dict[str, object],
    media_path: Path,
    candidate_id: str,
    cover_text: str,
    art_direction: LidoushaCoverArtDirection,
    frame_selection: Mapping[str, object],
    reference_path: Path,
    ai_dir: Path,
    covers_dir: Path,
    evidence_dir: Path,
    image_edit: Callable[..., dict[str, object]],
    final_participant_verifier: Callable[..., Mapping[str, object]] | None,
    final_host_identity_verifier: Callable[..., Mapping[str, object]] | None,
    base_url: str,
    api_key: str,
) -> dict[str, object]:
    """Keep source pixels when the independent redraw identity witness is down."""

    generation = redraw_result.get("cover_generation")
    if not isinstance(generation, dict) or not (
        redraw_result.get("status") == "BLOCKED_AI_COVER_REQUIRED"
        and final_host_identity_witness_unavailable(generation)
    ):
        return redraw_result
    verification = generation.get("final_host_identity_verification")
    generation["cpa_redraw"] = {
        "schema_version": "lidousha-cpa-redraw-degradation.v1",
        "status": "DEGRADED_TO_DIRECT_IDENTITY_WITNESS_UNAVAILABLE",
        "reason_code": (
            verification.get("reason_code")
            if isinstance(verification, Mapping)
            else "HOST_IDENTITY_WITNESS_UNAVAILABLE"
        ),
        "attempted_final_cover": generation.get("final_cover"),
        "attempted_final_cover_sha256": generation.get("final_cover_sha256"),
        "identity_verification": (
            dict(verification) if isinstance(verification, Mapping) else {}
        ),
    }
    generation.pop("final_host_identity_verification", None)
    direct_result = _stage_screenshot_direct_cover(
        media_path=media_path,
        candidate_id=candidate_id,
        cover_text=cover_text,
        art_direction=art_direction,
        frame_selection=frame_selection,
        reference_path=reference_path,
        ai_dir=ai_dir,
        covers_dir=covers_dir,
        evidence_dir=evidence_dir,
        cover_generation=generation,
        polish=False,
        image_edit=image_edit,
        final_participant_verifier=final_participant_verifier,
        final_host_identity_verifier=final_host_identity_verifier,
        base_url=base_url,
        api_key=api_key,
    )
    if direct_result.get("status") != "AI_COVER_READY":
        return direct_result
    direct_generation = direct_result.get("cover_generation")
    if not isinstance(direct_generation, dict):
        return direct_result
    record_cover_route_execution(
        direct_generation,
        actual_treatment="screenshot_direct",
        execution_status="READY_DEGRADED",
        image_generation_attempted=True,
        image_generation_used=False,
        detail=(
            "CPA redraw identity witness was unavailable; retained a "
            "hash-bound source screenshot instead of unverified AI pixels"
        ),
        final_participant_verification=(
            direct_generation.get("final_participant_verification")
            if isinstance(
                direct_generation.get("final_participant_verification"),
                Mapping,
            )
            else None
        ),
    )
    direct_generation["status"] = "READY_DEGRADED"
    direct_generation["detail"] = (
        "CPA redraw identity witness was unavailable; retained a hash-bound "
        "source screenshot instead of unverified AI pixels"
    )
    return direct_result


def _blocked_ai_cover_result(cover_generation: Mapping[str, object], reason_codes: Sequence[str], detail: str) -> dict[str, object]:
    generation = {**dict(cover_generation), "status": "BLOCKED", "detail": detail}
    return {
        "status": "BLOCKED_AI_COVER_REQUIRED",
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "cover_path": None,
        "cover_generation": generation,
    }

def _materialized_artifact_root(materialized_recut: Mapping[str, object], media_path: Path) -> Path:
    manifest_value = materialized_recut.get("manifest_path")
    if isinstance(manifest_value, str) and manifest_value:
        manifest_path = Path(manifest_value)
        if manifest_path.parent.name in {"recuts", "media"}:
            return manifest_path.parent.parent
        return manifest_path.parent
    if media_path.parent.name in {"recuts", "media"}:
        return media_path.parent.parent
    return media_path.parent

def _staged_transcript_sample(record: Mapping[str, object], cues: Sequence[SourceCue]) -> str:
    """Title/cover text sample: prefer the FINAL subtitle (fresh transcription
    with glossary corrections) over the context cues, so the title uses the
    corrected names (Ado, 小室) rather than the coarse-ASR spellings."""

    subtitle_path = record.get("subtitle_path")
    if isinstance(subtitle_path, str) and Path(subtitle_path).is_file():
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues

            parsed = parse_srt_cues(Path(subtitle_path).read_text(encoding="utf-8"))
            sample = " ".join(" ".join(cue.text.split()) for cue in parsed if cue.text.strip())[:600]
            if sample:
                return sample
        except OSError:
            pass
    return " ".join(cue.text.strip() for cue in cues if cue.text.strip())[:600]
