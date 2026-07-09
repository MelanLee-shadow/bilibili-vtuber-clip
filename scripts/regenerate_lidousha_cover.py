#!/usr/bin/env python3
"""Canonical Li Dousha cover (re)generation workflow — a thin, reusable entry
that wraps the SAME functions the main autoslice/publish pipeline uses
(`_lidousha_cover_art_direction` / `_lidousha_cover_prompt` /
`_call_cpa_image_edit` / `_overlay_lidousha_cover_title` in
`scripts/run_auto_review_shadow_pipeline.py`).  Use it to regenerate a cover for
one clip from its reference frame or media, in the redesigned persona-driven
style (half-body bust one side + multi-color artistic title + varied background;
per-clip skin from the reference; never tongue-out).

Fail-closed like production: real CPA gpt-image-2 only, never a frame-grab fake.

Examples:
  # from a per-clip reference frame (fastest — reuses an existing cover-ref):
  python scripts/regenerate_lidousha_cover.py \
      --title "【李豆沙】反沙，不是反李豆沙！" --candidate-id semantictalk_1324178_1414598 \
      --ref path/to/xxx.cover-ref.png --out out/xxx.cover.png

  # from the clip media (extracts a fresh identity frame first):
  python scripts/regenerate_lidousha_cover.py --title "..." --media clip.recut.mp4 --out cover.png

  # re-overlay only (no CPA call) onto an existing no-text AI background:
  python scripts/regenerate_lidousha_cover.py --title "..." --reuse-bg --ai-bg bg.png --out cover.png
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.llm_client import LlmConfig, build_llm_call
from scripts.run_auto_review_shadow_pipeline import (
    _call_cpa_image_edit,
    _lidousha_cover_art_direction,
    _lidousha_cover_prompt,
    _lidousha_cover_text,
    _overlay_lidousha_cover_title,
    _sha256,
)

_CPA_ART_DIRECTION_LLM = "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file}"


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
    out_path: Path,
    reference_path: Path | None = None,
    media_path: Path | None = None,
    candidate_id: str | None = None,
    ai_bg_path: Path | None = None,
    reuse_bg: bool = False,
    use_llm: bool = True,
    layout: str | None = None,
) -> dict:
    """Produce one redesigned cover. Returns a metadata dict (also written next to
    the cover as ``<out>.cover_generation.json``)."""
    cover_text = _lidousha_cover_text(title)
    candidate_id = candidate_id or out_path.stem
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ai_bg_path = ai_bg_path or out_path.with_suffix(".ai-bg.png")

    art_direction_llm = None
    if use_llm:
        art_direction_llm = build_llm_call(
            LlmConfig(transport="command", command_template=_CPA_ART_DIRECTION_LLM, timeout_seconds=180.0)
        )
    art_direction = _lidousha_cover_art_direction(
        candidate_id=candidate_id, title=title, cover_text=cover_text, art_direction_llm_call=art_direction_llm
    )
    if layout:
        # Force the text side (the AI bg's character is fixed; put text OPPOSITE it):
        # right-split = text LEFT (character on the right), left-split = text RIGHT.
        import dataclasses

        art_direction = dataclasses.replace(art_direction, layout=layout)

    if reuse_bg:
        if not ai_bg_path.is_file():
            raise SystemExit(f"--reuse-bg but no AI background at {ai_bg_path}")
    else:
        base_url = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
        api_key = os.environ.get("CPA_API_KEY", "").strip()
        if not base_url or not api_key:
            raise SystemExit("BLOCKED_AI_COVER_REQUIRED: CPA_BASE_URL/CPA_API_KEY missing (no frame-grab fakery)")
        if reference_path is None:
            if media_path is None:
                raise SystemExit("need --ref or --media (or --reuse-bg)")
            reference_path = out_path.with_suffix(".cover-ref.png")
            _extract_reference_frame(media_path, reference_path)
        result = _call_cpa_image_edit(
            base_url=base_url,
            api_key=api_key,
            reference_path=reference_path,
            output_path=ai_bg_path,
            prompt=_lidousha_cover_prompt(title=title, cover_text=cover_text, art_direction=art_direction),
            request_path=out_path.with_suffix(".cpa-request.redacted.json"),
            response_path=out_path.with_suffix(".cpa-response.redacted.json"),
        )
        if result.get("status") != "AI_BACKGROUND_READY" or not ai_bg_path.is_file():
            raise SystemExit(f"BLOCKED_AI_COVER_REQUIRED: {result.get('reason_code')} {result.get('detail')}")

    overlay = _overlay_lidousha_cover_title(ai_bg_path, out_path, cover_text=cover_text, art_direction=art_direction)
    meta = {
        "workflow": "regenerate_lidousha_cover",
        "model": "gpt-image-2",
        "method": "images.edit",
        "fallback_used": False,
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
        },
        "ai_background": str(ai_bg_path),
        "ai_background_sha256": "sha256:" + _sha256(ai_bg_path),
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
    p.add_argument("--out", required=True, type=Path, help="Output cover PNG path.")
    p.add_argument("--ref", type=Path, help="Per-clip reference frame (identity/skin).")
    p.add_argument("--media", type=Path, help="Clip media to extract a fresh reference frame from.")
    p.add_argument("--candidate-id", help="Stable id for deterministic layout/hook rotation (default: out filename stem).")
    p.add_argument("--ai-bg", type=Path, help="Path for the no-text AI background (default: <out>.ai-bg.png).")
    p.add_argument("--reuse-bg", action="store_true", help="Re-overlay onto an existing --ai-bg without calling CPA.")
    p.add_argument("--no-llm", action="store_true", help="Skip the CPA art-direction judge; use the deterministic baseline.")
    p.add_argument("--layout", choices=("left-split", "right-split", "banner", "song-clean"),
                   help="force the text layout (right-split=text LEFT/character RIGHT; left-split=text RIGHT). Use when the reused AI bg's character is on the side the auto-layout put text.")
    args = p.parse_args(argv)

    meta = regenerate_cover(
        title=args.title,
        out_path=args.out,
        reference_path=args.ref,
        media_path=args.media,
        candidate_id=args.candidate_id,
        ai_bg_path=args.ai_bg,
        reuse_bg=args.reuse_bg,
        use_llm=not args.no_llm,
        layout=args.layout,
    )
    ad = meta["art_direction"]
    print(json.dumps({"out": str(args.out), "layout": ad["layout"], "role": ad["role"],
                      "background_style": ad["background_style"], "hook_color": ad["hook_color"],
                      "hook_word": ad["hook_word"], "cover_sha256": meta["final_cover_sha256"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
