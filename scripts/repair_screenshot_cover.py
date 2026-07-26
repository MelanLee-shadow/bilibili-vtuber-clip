#!/usr/bin/env python3
"""Recompose one screenshot-route cover from its existing hash-bound polish
artifact, through the SAME production compose/overlay/verify functions.

Use case (first: 2026-07-26 BV1E93L6rErV): the fixed fit-crop card decapitated
a camera-window polish output. This driver re-runs the deterministic poster
composition with the face-safe contain card, re-renders the exact same title
art, and demands a PASS CPA face-integrity verdict bound to the new final
bytes before it reports success. It never re-runs the polish model — the input
is the already-evidenced polished PNG, so the repair is pixel-reproducible.

Writes next to --out: the new final cover PNG plus a
``<stem>.cover-repair.receipt.json`` disclosure (old/new sha, card fit,
verification receipt). Applying the new cover to a published BV is a separate
authorized step (scripts/bili_cover_edit.py).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.cover_generation import (  # noqa: E402
    LidoushaCoverArtDirection,
    _overlay_lidousha_cover_title,
)
from src.autoslice.cover_screenshot_poster import (  # noqa: E402
    _compose_screenshot_poster_background,
)
from src.autoslice.publish_staging import _verify_polish_face_integrity  # noqa: E402
from src.autoslice.shadow_review import _sha256  # noqa: E402


def _art_direction_from_record(payload: dict) -> LidoushaCoverArtDirection:
    fields = {field.name for field in dataclasses.fields(LidoushaCoverArtDirection)}
    kwargs = {k: v for k, v in payload.items() if k in fields}
    if isinstance(kwargs.get("cover_punch"), list):
        kwargs["cover_punch"] = tuple(kwargs["cover_punch"])
    return LidoushaCoverArtDirection(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, help="burned-final record.json")
    parser.add_argument(
        "--polished", default="", help="existing hash-bound polished PNG"
    )
    parser.add_argument(
        "--media", default="",
        help="delivered recut MP4: re-extract the record's camera window with "
             "extra bottom padding and re-polish (when the old polish source "
             "is itself chin-flush)",
    )
    parser.add_argument("--expand-bottom-frac", type=float, default=0.20)
    parser.add_argument("--expand-top-frac", type=float, default=0.06)
    parser.add_argument("--out", required=True, help="new final cover PNG path")
    parser.add_argument(
        "--cpa-base", default=os.environ.get("CPA_BASE_URL", ""),
    )
    parser.add_argument(
        "--cpa-key", default=os.environ.get("CPA_API_KEY", ""),
    )
    args = parser.parse_args()

    record = json.loads(Path(args.record).read_text(encoding="utf-8"))
    generation = (
        (record.get("publish_staging") or {}).get("cover_generation")
        or record.get("cover_generation")
        or {}
    )
    if generation.get("method") != "screenshot_polish":
        print(f"REFUSE: record method is {generation.get('method')!r}, "
              "this driver only repairs screenshot_polish covers", file=sys.stderr)
        return 2
    cover_text = str(generation.get("cover_text") or "")
    art_payload = generation.get("art_direction")
    if not cover_text or not isinstance(art_payload, dict):
        print("REFUSE: record lacks cover_text/art_direction", file=sys.stderr)
        return 2
    art_direction = _art_direction_from_record(art_payload)
    out_path = Path(args.out)
    poster_path = out_path.with_suffix(".poster.png")

    if args.media:
        frame_evidence = generation.get("screenshot_frame") or {}
        frame_ms = int(frame_evidence.get("frame_ms"))
        crop_box = frame_evidence.get("crop_box")
        source_size = frame_evidence.get("source_size") or [1920, 1080]
        if not (isinstance(crop_box, list) and len(crop_box) == 4):
            print("REFUSE: record has no crop_box for re-extraction",
                  file=sys.stderr)
            return 2
        # Re-extract the recorded camera window with breathing room below the
        # chin (and a little above), then re-polish. Same screenshot lane.
        x0, y0, x1, y1 = (int(v) for v in crop_box)
        height = y1 - y0
        y0 = max(0, y0 - int(height * args.expand_top_frac))
        y1 = min(int(source_size[1]), y1 + int(height * args.expand_bottom_frac))
        window = [
            x0 / source_size[0], y0 / source_size[1],
            x1 / source_size[0], y1 / source_size[1],
        ]
        from src.autoslice.cover_frame_selection import extract_zoomed_cover_frame
        from src.autoslice.cover_generation import _cover_screenshot_polish_prompt
        from scripts.run_auto_review_shadow_pipeline import _call_cpa_image_edit

        base_path = out_path.with_suffix(".rebase.png")
        crop_evidence = extract_zoomed_cover_frame(
            Path(args.media), frame_ms, base_path, window_bbox_frac=window
        )
        print(json.dumps({"re_extracted": crop_evidence}, ensure_ascii=False))
        polished = out_path.with_suffix(".repolished.png")
        polish_result = _call_cpa_image_edit(
            base_url=args.cpa_base,
            api_key=args.cpa_key,
            reference_path=base_path,
            output_path=polished,
            prompt=_cover_screenshot_polish_prompt(),
            request_path=out_path.with_suffix(".repolish-request.redacted.json"),
            response_path=out_path.with_suffix(".repolish-response.redacted.json"),
        )
        if polish_result.get("status") != "AI_BACKGROUND_READY":
            print(f"REFUSE: re-polish failed: {polish_result.get('detail') or polish_result.get('status')}",
                  file=sys.stderr)
            return 2
    elif args.polished:
        polished = Path(args.polished)
    else:
        print("REFUSE: need --polished or --media", file=sys.stderr)
        return 2

    poster_evidence = _compose_screenshot_poster_background(
        polished,
        poster_path,
        art_direction=art_direction,
        source_ai_modified=True,
        face_safe_contain=True,
    )
    overlay = _overlay_lidousha_cover_title(
        poster_path, out_path, cover_text=cover_text, art_direction=art_direction
    )
    verification = _verify_polish_face_integrity(
        out_path, base_url=args.cpa_base, api_key=args.cpa_key
    )
    new_sha = "sha256:" + _sha256(out_path)
    witness = verification.get("witness") if isinstance(verification, dict) else None
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if isinstance(witness, dict) and witness.get("image_sha256")
        else None
    )
    ok = verification.get("status") == "PASS" and witness_sha == new_sha
    receipt = {
        "schema_version": "repair-screenshot-cover.v1",
        "repaired_at": datetime.now(timezone.utc).isoformat(),
        "record_path": str(Path(args.record).resolve()),
        "candidate_id": record.get("candidate_id") or record.get("cid"),
        "previous_final_cover_sha256": generation.get("final_cover_sha256"),
        "polished_input_sha256": "sha256:" + _sha256(polished),
        "new_final_cover": str(out_path.resolve()),
        "new_final_cover_sha256": new_sha,
        "card_fit": (poster_evidence.get("source_frame_transform") or {}).get(
            "card_fit"
        ),
        "rendered_lines": overlay.get("rendered_lines"),
        "polish_face_verification": verification,
        "status": "REPAIRED" if ok else "FACE_VERIFICATION_FAILED",
    }
    receipt_path = out_path.with_suffix(".cover-repair.receipt.json")
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: receipt[k] for k in (
        "status", "card_fit", "new_final_cover_sha256")}, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
