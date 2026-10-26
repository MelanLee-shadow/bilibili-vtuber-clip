#!/usr/bin/env python3
"""Canonical channel-cover (re)generation workflow — a thin, reusable entry
that wraps the SAME functions the main autoslice/publish pipeline uses
(`_cover_art_direction` / `_cover_prompt` /
`_call_cpa_image_edit` / `_overlay_cover_title` in
`scripts/run_auto_review_shadow_pipeline.py`).  Use it to regenerate a cover for
one clip from its reference frame or media, in the redesigned persona-driven
style (half-body bust one side + multi-color artistic title + varied background;
per-clip skin from the reference; never tongue-out).

Fail-closed like production: real CPA image edit only, never a frame-grab fake.
The compatibility model is tried only after an explicit model-unavailable
response from the preferred route.

Examples:
  # from a per-clip reference frame (fastest — reuses an existing cover-ref):
  python scripts/regenerate_channel_cover.py \
      --title "【李豆沙】反沙，不是反李豆沙！" --candidate-id semantictalk_1324178_1414598 \
      --ref path/to/xxx.cover-ref.png --out out/xxx.cover.png

  # from the clip media (extracts a fresh identity frame first):
  python scripts/regenerate_channel_cover.py --title "..." --media clip.recut.mp4 --out cover.png

Re-overlaying an existing background is deliberately fail-closed here: without
the original generation manifest and hashes it would falsely claim a new
``images.edit`` result.  Generate a new immutable cover attempt instead.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL, cpa_call_timeout_seconds
from src.autoslice.cover_emote import (
    compose_companion_reference,
    load_emote_library,
    resolve_emote_reference,
)
from src.autoslice.cover_punch_semantics import (
    cover_text_requires_punch_for_thumbnail,
    validate_cover_punch_semantic_review,
)
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.cover_host_identity_gate import story_contract_requires_host_only_final
from src.autoslice.cover_repair_route_lineage import active_story_contract
from src.autoslice.cover_route_evidence import relationship_visual_safety_required
from src.autoslice.cover_generation import ordinary_talk_redraw_style_binding
from scripts.run_auto_review_shadow_pipeline import (
    _call_cpa_image_edit,
    _cpa_image_model_candidates,
    _cover_art_direction,
    _cover_prompt,
    _cover_text,
    _overlay_cover_title,
    _sha256,
)

# Art direction is a structured pick with a known good shape (deterministic
# fallback + judge guardrails) → gpt-6.1-sol, the current quality-default lane.
_CPA_ART_DIRECTION_LLM = f"bash scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} '{TOP_CPA_MODEL}' medium"


def _extract_reference_frame(media: Path, out: Path) -> None:
    """Same identity-frame extraction the pipeline uses (thumbnail of the clip)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
        "-vf", "thumbnail=120,scale=1920:-2", "-frames:v", "1", str(out),
    ]
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if completed.returncode != 0 or not out.is_file():
        raise SystemExit(f"COVER_REFERENCE_EXTRACTION_FAILED: {completed.stderr[-400:]}")


def _art_direction_metadata(art_direction) -> dict[str, object]:
    """Persist the full structured direction, including the research choice."""

    return {
        "role": art_direction.role,
        "expression_en": art_direction.expression_en,
        "background_style": art_direction.background_style,
        "layout": art_direction.layout,
        "visual_brief": art_direction.visual_brief,
        "title_style": art_direction.title_style,
        "hook_color": art_direction.hook_color,
        "hook_word": art_direction.hook_word,
        "is_song": art_direction.is_song,
        "emote_id": art_direction.emote_id,
        "emote_mode": art_direction.emote_mode,
        "emote_reason": art_direction.emote_reason,
        "cover_punch": list(art_direction.cover_punch),
        "cover_punch_semantic_review": art_direction.cover_punch_semantic_review,
        "scene_props": list(art_direction.scene_props),
        "research_application": copy.deepcopy(art_direction.research_application),
    }


def regenerate_cover(
    *,
    title: str,
    cover_text: str | None = None,
    story_hook: str = "",
    revision_brief: str = "",
    out_path: Path,
    reference_path: Path | None = None,
    style_reference_path: Path | None = None,
    media_path: Path | None = None,
    candidate_id: str | None = None,
    ai_bg_path: Path | None = None,
    reuse_bg: bool = False,
    use_llm: bool = True,
    layout: str | None = None,
    emote_id: str | None = None,
    emote_mode: str = "replace",
    emote_reason: str = "",
    diversity_slot: int | None = None,
    allow_punch: bool = False,
    story_contract: dict | None = None,
    research_story_context: Mapping[str, object] | None = None,
    final_srt_text: str | None = None,
) -> dict:
    """Produce one redesigned cover. Returns a metadata dict (also written next to
    the cover as ``<out>.cover_generation.json``)."""
    cover_text = (
        cover_text.strip()
        if isinstance(cover_text, str)
        else _cover_text(title)
    )
    revision_brief = revision_brief if isinstance(revision_brief, str) else ""
    revision_brief_sha256 = (
        "sha256:" + hashlib.sha256(revision_brief.encode("utf-8")).hexdigest()
        if revision_brief.strip()
        else None
    )
    if not cover_text:
        raise SystemExit("COVER_TEXT_EMPTY")
    candidate_id = candidate_id or out_path.stem
    if story_contract is not None and (
        not isinstance(story_contract, dict)
        or story_contract.get("candidate_id") != candidate_id
    ):
        raise SystemExit("COVER_STORY_CONTRACT_CANDIDATE_MISMATCH")
    host_only_required = story_contract_requires_host_only_final(story_contract)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ai_bg_path = ai_bg_path or out_path.with_suffix(".ai-bg.png")
    selected_model = _cpa_image_model_candidates()[0]
    attempted_models: list[str] = []
    model_fallback_used = False
    request_path = out_path.with_suffix(".cpa-request.redacted.json")
    response_path = out_path.with_suffix(".cpa-response.redacted.json")

    if reuse_bg:
        raise SystemExit("REUSE_BG_REQUIRES_BOUND_SOURCE_MANIFEST")

    # Check the actual image transport before spending an art-direction call.
    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not base_url or not api_key:
        raise SystemExit("BLOCKED_AI_COVER_REQUIRED: CPA_BASE_URL/CPA_API_KEY missing (no frame-grab fakery)")

    art_direction_llm = None
    if use_llm:
        art_direction_llm = build_llm_call(
            LlmConfig(transport="command", command_template=_CPA_ART_DIRECTION_LLM, timeout_seconds=cpa_call_timeout_seconds())
        )
    emote_library = load_emote_library(ROOT)
    # 梗字门由**内容属性**触发，不再由调用方 opt-in。
    #
    # docs/pipeline/70-cover.md:44-45 明令「不得因标题是人工权威、
    # cover_punch_allowed=false、回执为空或回执失败而把长 cover_text 当作封面放行」，
    # 但该门此前的唯一实现是默认关闭的 `--allow-punch`。
    # auto_192000_909_1014（白色奶龙，BV1s7326qEc9）的 cover-only 修复没带该 flag，
    # 于是选择器从未被调用，操作者改为手写文案与分行（丢掉「观众想让新3D永久保留」
    # 这个事件主语），并写了一份把该手写结果硬编码为期望值的 QA——一个不可能失败的
    # 自我认证检查。门必须由内容触发，调用方只能加严不能关闭。
    punch_required = cover_text_requires_punch_for_thumbnail(cover_text)
    art_direction = _cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        art_direction_llm_call=art_direction_llm,
        emote_library=emote_library,
        diversity_slot=diversity_slot,
        allow_punch=allow_punch or punch_required,
        story_hook=story_hook,
        revision_brief=revision_brief,
        research_story_context=research_story_context,
        final_srt_text=final_srt_text,
    )
    if punch_required and not art_direction.is_song:
        if not art_direction.cover_punch:
            raise SystemExit(
                "COVER_PUNCH_REVIEW_REQUIRED: cover_text 超出 1-2 行缩略图合同但没有"
                " 产出 cover_punch；不得以整段 cover_text 放行"
                "（docs/pipeline/70-cover.md:44-45）"
            )
        if not validate_cover_punch_semantic_review(
            art_direction.cover_punch_semantic_review,
            rendered_lines=list(art_direction.cover_punch),
            cover_text=cover_text,
            story_hook=story_hook or "",
        ):
            raise SystemExit(
                "COVER_PUNCH_REVIEW_INVALID: cover_punch 缺少通过校验的"
                " lidousha-cover-punch-semantic-review.v2 回执；回执为空/失败时"
                "必须重试或阻断，不得落盘任何像素"
            )
    import dataclasses

    if layout:
        # Force the text side (the AI bg's character is fixed; put text OPPOSITE it):
        # right-split = text LEFT (character on the right), left-split = text RIGHT.
        art_direction = dataclasses.replace(art_direction, layout=layout)
    if emote_id:

        if art_direction.is_song:
            raise SystemExit("EMOTE_NOT_ALLOWED_ON_SONG_COVERS")
        if emote_library.get(emote_id) is None:
            raise SystemExit(f"EMOTE_ID_UNKNOWN: {emote_id!r} not in the emote library")
        art_direction = dataclasses.replace(
            art_direction,
            emote_id=emote_id,
            emote_mode=emote_mode,
            emote_reason=emote_reason or "维护者 manual pick (--emote)",
        )






    emote_entry = None
    emote_meta: dict | None = None
    emote_reference: Path | None = None
    if art_direction.emote_id:
        entry = emote_library.get(art_direction.emote_id)
        resolved, resolve_detail = (
            resolve_emote_reference(entry, repo_root=ROOT, runtime_roots=emote_library.runtime_roots)
            if entry is not None
            else (None, "EMOTE_ID_UNKNOWN")
        )
        if resolved is None:
            raise SystemExit(
                f"EMOTE_REFERENCE_UNAVAILABLE: {resolve_detail}"
            )
        else:
            emote_entry = entry
            emote_reference = resolved
            emote_meta = {
                "id": entry.id,
                "label": entry.label,
                "mode": art_direction.emote_mode,
                "reason": art_direction.emote_reason,
                "status": "EMOTE_REFERENCE_READY",
                "hd_file": str(resolved),
                "hd_sha256": "sha256:" + entry.hd_sha256,
            }

    needs_frame = emote_entry is None or art_direction.emote_mode == "companion"
    if needs_frame and reference_path is None:
        if media_path is None:
            raise SystemExit("need --ref or --media")
        reference_path = out_path.with_suffix(".cover-ref.png")
        _extract_reference_frame(media_path, reference_path)
    if emote_entry is not None and emote_reference is not None:
        if art_direction.emote_mode == "companion":
            reference_path = compose_companion_reference(
                reference_path, emote_reference, out_path.with_suffix(".cover-ref.with-emote.png")
            )
        else:
            reference_path = emote_reference
    scene_kind = str((story_contract or {}).get("scene_kind") or "talk")
    relation_required = relationship_visual_safety_required(story_contract)
    style_binding = (
        ordinary_talk_redraw_style_binding(
            scene_kind=scene_kind,
            relationship_visual_required=relation_required,
            is_song=art_direction.is_song, hook_color=art_direction.hook_color,
            style_reference_path=style_reference_path,
        ) if art_direction.emote_mode != "replace" else None
    )
    if style_reference_path is not None and (
        style_binding is None or style_binding.get("reference_image_unavailable")
    ):
        raise SystemExit("COVER_STYLE_REFERENCE_UNAVAILABLE")
    if style_binding is not None and style_binding.get("status") != "READY":
        raise SystemExit("COVER_STYLE_REFERENCE_UNAVAILABLE")
    if style_binding is not None:
        palette = style_binding["text_palette"]
        art_direction = dataclasses.replace(
            art_direction, hook_color=palette["primary"], secondary_fill=palette["secondary"],
        )
    image_prompt = _cover_prompt(
        title=title, cover_text=cover_text, art_direction=art_direction, emote=emote_entry,
        revision_brief=revision_brief,
        style_binding=style_binding, scene_kind=scene_kind,
        relationship_visual_required=relation_required,
    )
    if host_only_required:
        image_prompt += (
            "\nHOST_ONLY: The final cover must contain only the referenced streamer. "
            "Do not add other people, avatars, human silhouettes, faces in screens, "
            "or human-shaped background decoration. Use unoccupied furniture or "
            "non-human objects to express the scene. Explicit before/after depictions "
            "of the SAME host are permitted; every depiction must retain the same "
            "source-visible identity and outfit and must not imply a new participant. "
            "Preserve the reference identity "
            "and the approved story; do not invent another participant."
        )
    style_reference = style_binding.get("reference_image") if style_binding else None
    style_kwargs = (
        {"style_reference_paths": (Path(str(style_reference["path"])),)}
        if isinstance(style_reference, dict) else {}
    )
    result = _call_cpa_image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=ai_bg_path,
        prompt=image_prompt,
        request_path=request_path,
        response_path=response_path,
        **style_kwargs,
    )
    if result.get("status") != "AI_BACKGROUND_READY" or not ai_bg_path.is_file():
        raise SystemExit(f"BLOCKED_AI_COVER_REQUIRED: {result.get('reason_code')} {result.get('detail')}")
    selected_model = str(result.get("selected_model") or selected_model)
    attempted_models = [str(item) for item in result.get("attempted_models") or []]
    model_fallback_used = bool(result.get("model_fallback_used"))

    overlay = _overlay_cover_title(ai_bg_path, out_path, cover_text=cover_text, art_direction=art_direction)
    meta = {
        "workflow": "regenerate_channel_cover",
        "status": "AI_COVER_READY",
        "model": selected_model,
        "method": "images.edit",
        "image_gen_model": "cpa",
        "fallback_used": False,
        "model_fallback_used": model_fallback_used,
        "attempted_models": attempted_models,
        "title": title,
        "cover_text": cover_text,
        "candidate_id": candidate_id,
        "art_direction": _art_direction_metadata(art_direction),
        "cover_diversity_slot": diversity_slot,
        "cover_punch": list(art_direction.cover_punch),
        "emote": emote_meta,
        "ai_background": str(ai_bg_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_bg_path),
        "reference_image": str(reference_path) if reference_path is not None else None,
        "reference_sha256": (
            "sha256:" + _sha256(reference_path)
            if reference_path is not None and reference_path.is_file()
            else None
        ),
        "request_path": str(request_path) if request_path.is_file() else None,
        "request_sha256": (
            "sha256:" + _sha256(request_path) if request_path.is_file() else None
        ),
        "response_path": str(response_path) if response_path.is_file() else None,
        "response_sha256": (
            "sha256:" + _sha256(response_path) if response_path.is_file() else None
        ),
        "final_cover": str(out_path),
        "final_cover_sha256": "sha256:" + _sha256(out_path),
        **overlay,
    }
    if story_contract is not None:
        meta["story_contract"] = copy.deepcopy(story_contract)
    if style_binding is not None:
        meta["art_direction_style"] = style_binding
        meta["style_reference_images"] = result.get("style_reference_images", [])
    if revision_brief.strip():
        meta["revision_brief"] = revision_brief
        meta["revision_brief_sha256"] = revision_brief_sha256
    from src.autoslice.cover_host_identity_gate import (
        validate_final_host_identity_verification,
        verify_final_host_identity,
    )

    if reference_path is None:
        raise SystemExit("FINAL_HOST_IDENTITY_REFERENCE_MISSING")
    meta["final_host_identity_verification"] = (
        verify_final_host_identity(
            final_cover_path=out_path,
            final_cover_sha256=meta["final_cover_sha256"],
            reference_path=reference_path,
            base_url=base_url,
            api_key=api_key,
            scene_kind="song" if art_direction.is_song else "talk",
            host_only_required=host_only_required,
        )
    )
    out_path.with_suffix(".cover_generation.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not validate_final_host_identity_verification(meta):
        verification = meta["final_host_identity_verification"]
        raise SystemExit(
            "COVER_FINAL_HOST_IDENTITY_UNVERIFIED: "
            + str(
                verification.get("reason_code")
                or verification.get("detail")
                or "FAIL"
            )
        )
    return meta


def _package_story_contract(package_dir: Path, *, candidate_id: str, title: str) -> dict | None:
    """Read the same active documents used by the post-generation binder."""
    publishes = [
        path for path in package_dir.glob("*.publish.json")
        if not path.name.endswith(".recut.publish.json")
    ]
    if len(publishes) != 1:
        raise SystemExit("COVER_PACKAGE_PUBLISH_NOT_UNIQUE")
    publish_path = publishes[0]
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    if (
        publish.get("schema_version") != "shadow-publish-draft.v1"
        or publish.get("upload_enabled") is not False
        or publish.get("candidate_id") != candidate_id
        or publish.get("title") != title
    ):
        raise SystemExit("COVER_PACKAGE_CANDIDATE_TITLE_MISMATCH")
    documents = [(publish_path, publish)]
    record_path = publish_path.with_name(publish_path.name[:-len(".publish.json")] + ".record.json")
    if record_path.is_file():
        documents.insert(0, (record_path, json.loads(record_path.read_text(encoding="utf-8"))))
    return active_story_contract(documents)


def _cli_editorial_inputs(args) -> tuple[dict | None, dict | None, str | None, dict | None, dict]:
    """Resolve actual inputs before providers; explicit/package mirrors must agree."""
    from src.autoslice.cover_repair import snapshot_cover_repair_inputs
    from src.autoslice.publish_staging_paths import editorial_input_binding

    paths = []
    read_snapshot = {}
    def read(path: Path, *, as_json: bool = False):
        if path.is_symlink() or not path.is_file():
            raise SystemExit("COVER_CURRENT_INPUT_MISSING_OR_UNSAFE")
        paths.append(path)
        raw = path.read_bytes()
        observed = "sha256:" + hashlib.sha256(raw).hexdigest()
        key = str(path.absolute())
        if key in read_snapshot and read_snapshot[key] != observed:
            raise SystemExit("COVER_CURRENT_INPUT_CHANGED_DURING_READ")
        read_snapshot[key] = observed
        value = json.loads(raw) if as_json else raw.decode("utf-8")
        if as_json and not isinstance(value, dict):
            raise SystemExit("COVER_CURRENT_INPUT_NOT_OBJECT")
        return value

    candidate_id = args.candidate_id or args.out.stem
    story = read(args.story_contract, as_json=True) if args.story_contract else None
    research = read(args.editorial_research, as_json=True) if args.editorial_research else None
    text = read(args.final_srt) if args.final_srt else None
    srt_path = args.final_srt
    package_binding = None
    if args.bind_package:
        package_story = _package_story_contract(args.bind_package, candidate_id=candidate_id, title=args.title)
        publish_path = next(path for path in args.bind_package.glob("*.publish.json") if not path.name.endswith(".recut.publish.json"))
        publish = read(publish_path, as_json=True)
        record_path = Path(str(publish_path).removesuffix(".publish.json") + ".record.json")
        if record_path.is_file():
            read(record_path, as_json=True)
        package_research = publish.get("editorial_research") or (package_story or {}).get("editorial_research")
        if args.story_contract and story != package_story:
            raise SystemExit("COVER_CURRENT_STORY_PACKAGE_CONFLICT")
        if args.editorial_research and research != package_research:
            raise SystemExit("COVER_CURRENT_RESEARCH_PACKAGE_CONFLICT")
        story, research = package_story, package_research
        package_srt = Path(str(publish_path).removesuffix(".publish.json") + ".srt")
        package_binding = publish.get("current_binding")
        expected = publish.get("subtitle_sha256") or (package_binding or {}).get("subtitle_sha256")
        if package_srt.is_file():
            package_text = read(package_srt)
            sha = "sha256:" + hashlib.sha256(package_text.encode("utf-8")).hexdigest()
            if expected is not None and expected != sha:
                raise SystemExit("COVER_CURRENT_PACKAGE_SUBTITLE_DRIFT")
            if text is not None and text != package_text:
                raise SystemExit("COVER_CURRENT_SUBTITLE_PACKAGE_CONFLICT")
            text, srt_path = package_text, package_srt
        elif expected is not None or args.final_srt:
            raise SystemExit("COVER_CURRENT_PACKAGE_SUBTITLE_MISSING")
    if story is not None and story.get("candidate_id") != candidate_id:
        raise SystemExit("COVER_STORY_CONTRACT_CANDIDATE_MISMATCH")
    if research is not None and research.get("candidate_id") not in (None, candidate_id):
        raise SystemExit("COVER_CURRENT_RESEARCH_CANDIDATE_MISMATCH")
    explicit = bool(args.final_srt or args.story_contract or args.editorial_research)
    if explicit and text is None and (story or {}).get("scene_kind") != "song":
        raise SystemExit("COVER_CURRENT_FINAL_SRT_MISSING")
    hook = str((story or {}).get("selection_hook") or args.story_hook or "")
    if story and args.story_hook and args.story_hook != hook:
        raise SystemExit("COVER_CURRENT_STORY_HOOK_CONFLICT")
    args.story_hook = hook
    binding = None
    if text is not None or explicit:
        binding = editorial_input_binding(
            {"subtitle_path": str(srt_path) if srt_path else None, "story_contract": story},
            candidate_id=candidate_id, title=args.title, selection_hook=hook,
            editorial_research=research,
        )
        if text is not None and binding["subtitle_sha256"] != "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise SystemExit("COVER_CURRENT_INPUT_CHANGED_DURING_READ")
        if package_binding is not None and binding != {key: value for key, value in package_binding.items() if key != "cover_sha256"}:
            raise SystemExit("COVER_CURRENT_PACKAGE_INPUT_DRIFT")
    context = {**(story or {}), "editorial_research": research} if story or research is not None else None
    if snapshot_cover_repair_inputs(paths) != read_snapshot:
        raise SystemExit("COVER_CURRENT_INPUT_CHANGED_DURING_READ")
    return story, context, text, binding, read_snapshot


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Regenerate a channel cover in the redesigned persona-driven style.")
    p.add_argument("--title", required=True, help="Archive title (【李豆沙】… prefix is auto-stripped for the cover text).")
    p.add_argument(
        "--cover-text",
        help="reviewed cover-only copy; defaults to the title with the channel prefix removed",
    )
    p.add_argument(
        "--story-hook",
        default="",
        help="full StoryContract selection_hook for the CPA punch semantic gate",
    )
    p.add_argument(
        "--revision-brief",
        default="",
        help="explicit director correction for this regeneration; it takes precedence over conflicting optional scene props",
    )
    p.add_argument("--out", required=True, type=Path, help="Output cover PNG path.")
    p.add_argument("--ref", type=Path, help="Per-clip reference frame (identity/skin).")
    p.add_argument("--style-ref", type=Path, help="Optional separate STYLE_ONLY sample for ordinary talk redraw; never an identity reference.")
    p.add_argument("--media", type=Path, help="Clip media to extract a fresh reference frame from.")
    p.add_argument("--candidate-id", help="Stable id for deterministic layout/hook rotation (default: out filename stem).")
    p.add_argument("--final-srt", type=Path, help="Exact current final subtitle bytes for the director.")
    p.add_argument("--story-contract", type=Path, help="Current candidate StoryContract JSON; must agree with --bind-package.")
    p.add_argument("--editorial-research", type=Path, help="Current research applicability JSON; must agree with --bind-package.")
    p.add_argument("--ai-bg", type=Path, help="Path for the no-text AI background (default: <out>.ai-bg.png).")
    p.add_argument("--reuse-bg", action="store_true", help="Fail closed unless a future bound-source-manifest workflow is implemented.")
    p.add_argument("--no-llm", action="store_true", help="Skip the CPA art-direction judge; use the deterministic baseline.")
    p.add_argument("--layout", choices=("left-split", "right-split", "banner", "footer", "song-clean"),
                   help="force the text layout (right-split=text LEFT/character RIGHT; left-split=text RIGHT). Use when the reused AI bg's character is on the side the auto-layout put text.")
    p.add_argument("--emote", help="force an emote sticker as the cover subject by library id (e.g. 09); 维护者's manual strong-reason channel. replace mode needs no --ref/--media at all.")
    p.add_argument("--emote-mode", choices=("replace", "companion"), default="replace",
                   help="replace = the sticker IS the subject (no character redraw); companion = sticker inset beside the character (分身/代画粉丝kmx; needs --ref or --media).")
    p.add_argument("--emote-reason", default="", help="one-line strong reason recorded in the evidence manifest.")
    p.add_argument("--diversity-slot", type=int, help="non-negative same-session fallback slot for layout and accent; explicit art direction takes precedence.")
    p.add_argument("--allow-punch", action="store_true",
                   help="opt IN to the punch lane for short cover_text. NOT an off switch: "
                        "cover_text that cannot fit the 1-2 line thumbnail contract turns the "
                        "punch review on regardless, and a missing/invalid semantic receipt "
                        "then fails closed before any pixel is written.")
    p.add_argument("--bind-package", type=Path,
                   help="produce_slice_package 交付目录：出图成功后把封面回写进包内 "
                        "publish/record（与 runner cover-only repair 同一套校验与 "
                        "binding 回执；任何校验失败包保持原样，出图产物仍在）。")
    args = p.parse_args(argv)
    if args.diversity_slot is not None and args.diversity_slot < 0:
        p.error("--diversity-slot must be non-negative")

    story_contract, research_context, final_srt_text, input_binding, input_snapshot = _cli_editorial_inputs(args)

    meta = regenerate_cover(
        title=args.title,
        cover_text=args.cover_text,
        story_hook=args.story_hook,
        revision_brief=args.revision_brief,
        out_path=args.out,
        reference_path=args.ref,
        style_reference_path=args.style_ref,
        media_path=args.media,
        candidate_id=args.candidate_id,
        ai_bg_path=args.ai_bg,
        reuse_bg=args.reuse_bg,
        use_llm=not args.no_llm,
        layout=args.layout,
        emote_id=args.emote,
        emote_mode=args.emote_mode,
        emote_reason=args.emote_reason,
        diversity_slot=args.diversity_slot,
        allow_punch=args.allow_punch,
        story_contract=story_contract,
        research_story_context=research_context,
        final_srt_text=final_srt_text,
    )
    from src.autoslice.cover_repair import validate_cover_repair_inputs
    validate_cover_repair_inputs(input_snapshot)
    if input_binding is not None:
        meta["input_binding"] = input_binding
        args.out.with_suffix(".cover_generation.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
    ad = meta["art_direction"]
    summary = {"out": str(args.out), "layout": ad["layout"], "role": ad["role"],
               "background_style": ad["background_style"], "hook_color": ad["hook_color"],
               "hook_word": ad["hook_word"], "cover_sha256": meta["final_cover_sha256"]}
    if args.bind_package:
        from src.autoslice.cover_repair import bind_manual_package_cover

        summary["package_bind"] = bind_manual_package_cover(
            package_dir=args.bind_package, cover=args.out
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
