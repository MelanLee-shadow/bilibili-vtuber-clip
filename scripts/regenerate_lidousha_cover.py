#!/usr/bin/env python3
"""Canonical Li Dousha cover (re)generation workflow — a thin, reusable entry
that wraps the SAME functions the main autoslice/publish pipeline uses
(`_lidousha_cover_art_direction` / `_lidousha_cover_prompt` /
`_call_cpa_image_edit` / `_overlay_lidousha_cover_title` in
`scripts/run_auto_review_shadow_pipeline.py`).  Use it to regenerate a cover for
one clip from its reference frame or media, in the redesigned persona-driven
style (half-body bust one side + multi-color artistic title + varied background;
per-clip skin from the reference; never tongue-out).

Fail-closed like production: real CPA image edit only, never a frame-grab fake.
The compatibility model is tried only after an explicit model-unavailable
response from the preferred route.

Examples:
  # from a per-clip reference frame (fastest — reuses an existing cover-ref):
  python scripts/regenerate_lidousha_cover.py \
      --title "【李豆沙】反沙，不是反李豆沙！" --candidate-id semantictalk_1324178_1414598 \
      --ref path/to/xxx.cover-ref.png --out out/xxx.cover.png

  # from the clip media (extracts a fresh identity frame first):
  python scripts/regenerate_lidousha_cover.py --title "..." --media clip.recut.mp4 --out cover.png

Re-overlaying an existing background is deliberately fail-closed here: without
the original generation manifest and hashes it would falsely claim a new
``images.edit`` result.  Generate a new immutable cover attempt instead.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_emote import (
    compose_companion_reference,
    load_emote_library,
    resolve_emote_reference,
)
from src.autoslice.llm_client import LlmConfig, build_llm_call
from scripts.run_auto_review_shadow_pipeline import (
    _call_cpa_image_edit,
    _cpa_image_model_candidates,
    _lidousha_cover_art_direction,
    _lidousha_cover_prompt,
    _lidousha_cover_text,
    _overlay_lidousha_cover_title,
    _sha256,
)

# Art direction is a structured pick with a known good shape (deterministic
# fallback + judge guardrails) → gpt-5.6-luna, the doc-exact luna lane.
_CPA_ART_DIRECTION_LLM = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium"


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


def regenerate_cover(
    *,
    title: str,
    cover_text: str | None = None,
    out_path: Path,
    reference_path: Path | None = None,
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
) -> dict:
    """Produce one redesigned cover. Returns a metadata dict (also written next to
    the cover as ``<out>.cover_generation.json``)."""
    cover_text = (
        cover_text.strip()
        if isinstance(cover_text, str)
        else _lidousha_cover_text(title)
    )
    if not cover_text:
        raise SystemExit("COVER_TEXT_EMPTY")
    candidate_id = candidate_id or out_path.stem
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ai_bg_path = ai_bg_path or out_path.with_suffix(".ai-bg.png")
    selected_model = _cpa_image_model_candidates()[0]
    attempted_models: list[str] = []
    model_fallback_used = False
    request_path = out_path.with_suffix(".cpa-request.redacted.json")
    response_path = out_path.with_suffix(".cpa-response.redacted.json")

    if reuse_bg:
        raise SystemExit("REUSE_BG_REQUIRES_BOUND_SOURCE_MANIFEST")

    art_direction_llm = None
    if use_llm:
        art_direction_llm = build_llm_call(
            LlmConfig(transport="command", command_template=_CPA_ART_DIRECTION_LLM, timeout_seconds=180.0)
        )
    emote_library = load_emote_library(ROOT)
    art_direction = _lidousha_cover_art_direction(
        candidate_id=candidate_id,
        title=title,
        cover_text=cover_text,
        art_direction_llm_call=art_direction_llm,
        emote_library=emote_library,
        diversity_slot=diversity_slot,
        allow_punch=allow_punch,
    )
    import dataclasses

    if layout:
        # Force the text side (the AI bg's character is fixed; put text OPPOSITE it):
        # right-split = text LEFT (character on the right), left-split = text RIGHT.
        art_direction = dataclasses.replace(art_direction, layout=layout)
    if emote_id:
        # Ivan 点名表情包（人工强理由通道）：--emote 覆盖 judge 的选择。歌切照旧禁用。
        if art_direction.is_song:
            raise SystemExit("EMOTE_NOT_ALLOWED_ON_SONG_COVERS")
        if emote_library.get(emote_id) is None:
            raise SystemExit(f"EMOTE_ID_UNKNOWN: {emote_id!r} not in the emote library")
        art_direction = dataclasses.replace(
            art_direction,
            emote_id=emote_id,
            emote_mode=emote_mode,
            emote_reason=emote_reason or "Ivan manual pick (--emote)",
        )

    base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not base_url or not api_key:
        raise SystemExit("BLOCKED_AI_COVER_REQUIRED: CPA_BASE_URL/CPA_API_KEY missing (no frame-grab fakery)")

    # Emote reference resolution: replace mode swaps the reference to the
    # sticker (no live frame needed at all); companion insets the sticker into
    # the frame.  Judge picks degrade to the default redraw; an explicit
    # --emote must fail loudly instead of shipping something Ivan didn't ask.
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
            if emote_id:
                raise SystemExit(f"EMOTE_REFERENCE_UNAVAILABLE: {resolve_detail}")
            emote_meta = {
                "id": art_direction.emote_id,
                "status": "FALLBACK_DEFAULT_REDRAW",
                "detail": resolve_detail,
            }
            art_direction = dataclasses.replace(art_direction, emote_id="", emote_mode="", emote_reason="")
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
    result = _call_cpa_image_edit(
        base_url=base_url,
        api_key=api_key,
        reference_path=reference_path,
        output_path=ai_bg_path,
        prompt=_lidousha_cover_prompt(
            title=title, cover_text=cover_text, art_direction=art_direction, emote=emote_entry
        ),
        request_path=request_path,
        response_path=response_path,
    )
    if result.get("status") != "AI_BACKGROUND_READY" or not ai_bg_path.is_file():
        raise SystemExit(f"BLOCKED_AI_COVER_REQUIRED: {result.get('reason_code')} {result.get('detail')}")
    selected_model = str(result.get("selected_model") or selected_model)
    attempted_models = [str(item) for item in result.get("attempted_models") or []]
    model_fallback_used = bool(result.get("model_fallback_used"))

    overlay = _overlay_lidousha_cover_title(ai_bg_path, out_path, cover_text=cover_text, art_direction=art_direction)
    meta = {
        "workflow": "regenerate_lidousha_cover",
        "status": "AI_COVER_READY",
        "model": selected_model,
        "method": "images.edit",
        "fallback_used": False,
        "model_fallback_used": model_fallback_used,
        "attempted_models": attempted_models,
        "title": title,
        "cover_text": cover_text,
        "candidate_id": candidate_id,
        "art_direction": {
            "role": art_direction.role,
            "expression_en": art_direction.expression_en,
            "background_style": art_direction.background_style,
            "layout": art_direction.layout,
            "hook_color": art_direction.hook_color,
            "hook_word": art_direction.hook_word,
            "is_song": art_direction.is_song,
            "emote_id": art_direction.emote_id,
            "emote_mode": art_direction.emote_mode,
            "emote_reason": art_direction.emote_reason,
            "cover_punch": list(art_direction.cover_punch),
        },
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
    out_path.with_suffix(".cover_generation.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Regenerate a Li Dousha cover in the redesigned persona-driven style.")
    p.add_argument("--title", required=True, help="Archive title (【李豆沙】… prefix is auto-stripped for the cover text).")
    p.add_argument(
        "--cover-text",
        help="reviewed cover-only copy; defaults to the title with the channel prefix removed",
    )
    p.add_argument("--out", required=True, type=Path, help="Output cover PNG path.")
    p.add_argument("--ref", type=Path, help="Per-clip reference frame (identity/skin).")
    p.add_argument("--media", type=Path, help="Clip media to extract a fresh reference frame from.")
    p.add_argument("--candidate-id", help="Stable id for deterministic layout/hook rotation (default: out filename stem).")
    p.add_argument("--ai-bg", type=Path, help="Path for the no-text AI background (default: <out>.ai-bg.png).")
    p.add_argument("--reuse-bg", action="store_true", help="Fail closed unless a future bound-source-manifest workflow is implemented.")
    p.add_argument("--no-llm", action="store_true", help="Skip the CPA art-direction judge; use the deterministic baseline.")
    p.add_argument("--layout", choices=("left-split", "right-split", "banner", "song-clean"),
                   help="force the text layout (right-split=text LEFT/character RIGHT; left-split=text RIGHT). Use when the reused AI bg's character is on the side the auto-layout put text.")
    p.add_argument("--emote", help="force an emote sticker as the cover subject by library id (e.g. 09); Ivan's manual strong-reason channel. replace mode needs no --ref/--media at all.")
    p.add_argument("--emote-mode", choices=("replace", "companion"), default="replace",
                   help="replace = the sticker IS the subject (no character redraw); companion = sticker inset beside the character (分身/代画粉丝kmx; needs --ref or --media).")
    p.add_argument("--emote-reason", default="", help="one-line strong reason recorded in the evidence manifest.")
    p.add_argument("--diversity-slot", type=int, help="stable same-session cover slot; slots 0-5 map to distinct background families.")
    p.add_argument("--allow-punch", action="store_true", help="render the source-bound 2-12 character cover punch used by automatic talk titles.")
    args = p.parse_args(argv)

    meta = regenerate_cover(
        title=args.title,
        cover_text=args.cover_text,
        out_path=args.out,
        reference_path=args.ref,
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
    )
    ad = meta["art_direction"]
    print(json.dumps({"out": str(args.out), "layout": ad["layout"], "role": ad["role"],
                      "background_style": ad["background_style"], "hook_color": ad["hook_color"],
                      "hook_word": ad["hook_word"], "cover_sha256": meta["final_cover_sha256"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
