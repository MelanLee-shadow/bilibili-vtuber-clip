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
from typing import Callable, Mapping, Sequence

from .auto_review import DecisionAction, ReviewDecision
from .channel_profile import load_channel_profile
from .chat_authority import canonicalize_hard_surfaces
from .cover_emote import (
    compose_companion_reference,
    load_emote_library,
    resolve_emote_reference,
)
from .cover_generation import (
    _call_cpa_image_edit as _cover_call_cpa_image_edit,
    _cpa_image_model_candidates,
    _lidousha_cover_art_direction,
    _lidousha_cover_prompt,
    _lidousha_cover_text,
    _overlay_lidousha_cover_title,
)
from .llm_client import LlmCall, extract_json_object
from .review_evidence import SourceCue
from .shadow_review import _sha256, _write_json_file
from .title_policy import (
    _TITLE_MAX_ATTEMPTS,
    _TITLE_MAX_LEN,
    _TITLE_MIN_LEN,
    _ensure_lidousha_prefix,
    canonicalize_song_catalog_title,
    manual_title_override,
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

    # Iron rule: Ivan's manual title (title_llm_call=None) is final and passes
    # through a字不改 — no prefix forcing, no length gate, no policy check.
    # Prefix / length / banned-word enforcement applies ONLY to auto titles.
    staged_title = title
    title_source = "job_title"
    title_policy_violations: list[str] = []
    title_authority_error: str | None = None
    title_authority_status = "RESOLVED_MANUAL" if title_llm_call is None else "UNRESOLVED_AUTO"
    # Ivan 手定标题按 candidate 注入（2026-07-19）：命中即定稿，LLM 不再跑。
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
            f"硬性要求：含{CHANNEL_PROFILE.talk_title_prefix}前缀后 {_TITLE_MIN_LEN}–{_TITLE_MAX_LEN} 字"
            "（Ivan 手定语料的主力带是 25–45 字的三拍叙事，不要为了凑短把梗压没；"
            "只有梗足够硬的短爆点才走 20 字以下）；"
            "禁用空洞夸张词(炸裂/震惊/天花板/绝了/犯规/太顶),"
            "更不许用'X到犯规/炸裂/离谱'这种万能后缀——标题必须具体到这条切片里到底发生了什么"
            "(描述性的'越看越离谱/越整越离谱'这类是可以的,禁的是空洞的'X到离谱'后缀)。\n"
            f"只输出一个 JSON 对象：{output_contract}"
        )
        llm_title = ""
        llm_error: str | None = None
        # Bounded retry: regenerate up to _TITLE_MAX_ATTEMPTS times, calling out
        # the banned-word violation each retry so the model rewrites concretely.
        for attempt in range(_TITLE_MAX_ATTEMPTS):
            prompt = base_prompt
            if attempt > 0:
                prompt = (
                    base_prompt
                    + "\n注意：上一次标题违反了硬约束（违禁词，或没有保留选片第一分句的具体核心短语），已被否决。"
                    "不要用任何万能强调词，也不要把后续陪衬话题改成主标题；"
                    "写她具体做了/说了什么，并按要求重新只输出 JSON。"
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
            llm_title = candidate
            title_policy_violations = _title_policy_violations(candidate)
            if selection_hook and not _selection_hook_anchor_valid(
                anchor=payload.get("selection_hook_anchor"),
                selection_hook=selection_hook,
                title=candidate,
            ):
                title_policy_violations.append("selection_hook_anchor_missing")
            if not title_policy_violations:
                break

        if llm_title:
            if selection_hook and "selection_hook_anchor_missing" in title_policy_violations:
                fallback = _selection_hook_fallback_title(selection_hook)
                if fallback is not None:
                    llm_title = fallback
                    title_policy_violations = _title_policy_violations(fallback)
                    title_source = "selection_hook_fallback_after_llm_mismatch"
            prefixed = _ensure_lidousha_prefix(llm_title)
            if _TITLE_MIN_LEN <= len(prefixed) <= _TITLE_MAX_LEN:
                staged_title = prefixed
                if title_source == "job_title":
                    title_source = f"llm+{PROFILE_ID}_style_asset"
                if title_policy_violations:
                    title_source = f"llm+{PROFILE_ID}_style_asset(title_policy_violation)"
                    title_authority_error = "title_policy_violation:" + ",".join(title_policy_violations)
                elif title_source == "selection_hook_fallback_after_llm_mismatch":
                    title_authority_status = "RESOLVED_DETERMINISTIC_FALLBACK"
                else:
                    title_authority_status = "RESOLVED_LLM"
            else:
                # Length gate rejects the auto title → fall back to the job title
                # untouched (prefix forcing never touches non-LLM titles).
                title_source = f"job_title(llm_length_out_of_bounds:{len(prefixed)})"
                title_authority_error = f"title_length_out_of_bounds:{len(prefixed)}"
        elif llm_error is not None:
            title_source = f"job_title(llm_failed: {llm_error})"
            title_authority_error = llm_error

    # Ivan 2026-07-13 梗词铁律的标题/封面确定性兜底（字幕面在
    # normalize_code_switch_surfaces；LLM 标题若仍写出「直女」这里回正）。
    staged_title = canonicalize_hard_surfaces(staged_title)
    # Ivan 2026-07-14/19 歌切标题铁律 choke point：自动标题只要带歌切前缀就
    # 折叠成「前缀《歌名》」，任何「｜副标题」/hook 尾巴在这里被最终清除。
    # 手定标题（title_llm_call=None）保持一字不改的铁律，不进此函数。
    if title_llm_call is not None:
        staged_title = canonicalize_song_catalog_title(staged_title)
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
            # 梗字封面只对自动标题开放：Ivan 手定标题（title_llm_call=None）的
            # 封面仍走"每个成分都不许丢"的短句化铁律（2026-07-06 22966160 案）。
            punch_allowed=title_llm_call is not None,
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
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
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
        "title_authority_error": title_authority_error,
        "title_policy_violations": title_policy_violations,
        "cover_status": cover_status,
        "cover_path": cover_path_value,
        "cover_text": cover_text,
        "cover_generation": cover_generation,
        "reason_codes": reason_codes,
        "publish_json_path": str(publish_json_path),
        "upload_enabled": False,
    }
    return record

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
    punch_allowed: bool = False,
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
        "title": title,
    }
    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not base_url or not api_key:
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

    reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"
    # 受监督重产时可指定封面参考帧（内容时间轴毫秒，Ivan 点名画面用）；
    # 未设置则维持 thumbnail 自动代表帧。
    cover_ref_override = os.environ.get("AUTOSLICE_COVER_REF_MS", "").strip()
    if cover_ref_override.isdigit():
        ref_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{int(cover_ref_override) / 1000:.3f}",
            "-i",
            str(media_path),
            "-vf",
            "scale=1920:-2",
            "-frames:v",
            "1",
            str(reference_path),
        ]
    else:
        ref_command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vf",
            "thumbnail=120,scale=1920:-2",
            "-frames:v",
            "1",
            str(reference_path),
        ]
    completed = subprocess.run(ref_command, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not reference_path.is_file():
        cover_generation["reference_command"] = ref_command
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", "COVER_REFERENCE_EXTRACTION_FAILED"],
            completed.stderr[-500:] or "reference frame extraction failed",
        )

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
    )

    # Strong-reason emote pick (Ivan 2026-07-19): "replace" swaps the CPA
    # reference from the live frame to the official sticker (subject swap,
    # mutually exclusive with the character redraw); "companion" keeps the
    # frame and insets the sticker (分身 / kmx stand-in).  Any resolution
    # failure downgrades the ART DIRECTION back to the default character
    # redraw with disclosed evidence — never a blocked cover.
    emote_entry = None
    if art_direction.emote_id:
        entry = emote_library.get(art_direction.emote_id)
        resolved, resolve_detail = (
            resolve_emote_reference(entry, repo_root=ROOT, runtime_roots=emote_library.runtime_roots)
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
            emote_evidence.update({"status": "FALLBACK_DEFAULT_REDRAW", "detail": resolve_detail})
            art_direction = dataclasses_replace(
                art_direction, emote_id="", emote_mode="", emote_reason=""
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
            except Exception as exc:  # companion composite failed → default redraw
                emote_evidence.update(
                    {
                        "status": "FALLBACK_DEFAULT_REDRAW",
                        "detail": f"EMOTE_COMPANION_COMPOSE_FAILED: {type(exc).__name__}: {exc}",
                    }
                )
                reference_path = cover_refs_dir / f"{candidate_id}.cover-ref.png"
                art_direction = dataclasses_replace(
                    art_direction, emote_id="", emote_mode="", emote_reason=""
                )
        cover_generation["emote"] = emote_evidence
    cover_generation["art_direction"] = asdict(art_direction)

    ai_background_path = ai_dir / f"{candidate_id}.ai-bg.cpa-image-edit.png"
    request_path = evidence_dir / f"{candidate_id}.cover-cpa-request.redacted.json"
    response_path = evidence_dir / f"{candidate_id}.cover-cpa-response.redacted.json"
    cpa_result = image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=ai_background_path,
        prompt=_lidousha_cover_prompt(
            title=title,
            cover_text=cover_text,
            art_direction=art_direction,
            emote=emote_entry,
        ),
        request_path=request_path,
        response_path=response_path,
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
        return _blocked_ai_cover_result(
            cover_generation,
            ["CPA_AI_COVER_REQUIRED", str(cpa_result.get("reason_code") or "CPA_IMAGE_EDIT_FAILED")],
            str(cpa_result.get("detail") or "CPA image edit did not return an image"),
        )

    final_cover_path = covers_dir / f"{candidate_id}.ai-title.cover.png"
    overlay = _overlay_lidousha_cover_title(ai_background_path, final_cover_path, cover_text=cover_text, art_direction=art_direction)
    cover_generation.update(
        {
            "ai_background": str(ai_background_path),
            "ai_background_sha256": "sha256:" + _sha256(ai_background_path),
            "final_cover": str(final_cover_path),
            "final_cover_sha256": "sha256:" + _sha256(final_cover_path),
            **overlay,
        }
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
