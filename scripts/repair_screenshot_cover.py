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
        "--polished", required=True, help="existing hash-bound polished PNG"
    )
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
    polished = Path(args.polished)
    out_path = Path(args.out)
    poster_path = out_path.with_suffix(".poster.png")

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
