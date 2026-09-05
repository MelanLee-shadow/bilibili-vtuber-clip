#!/usr/bin/env python3
"""Recompose one screenshot-route cover from its existing hash-bound polish
artifact, through the SAME production compose/overlay/verify functions.

Use case  the fixed fit-crop card decapitated
a camera-window polish output. This driver re-runs the deterministic poster
composition with the face-safe contain card, re-renders the exact same title
art, and demands a PASS CPA face-integrity verdict bound to the new final
bytes before it reports success. It never re-runs the polish model — the input
is the already-evidenced polished PNG, so the repair is pixel-reproducible.

Writes next to --out: the new final cover PNG plus a
``<stem>.cover-repair.receipt.json`` disclosure (old/new sha, card fit,
verification receipt) and the sibling ``.cover_generation.json`` manifest
needed by the existing transactional cover binder for active maintenance.
Published replacement packages use the existing ``authorized_upload
cover-repair-*`` transaction entrypoint
(``src/autoslice/authorized_upload_cover_repair_cli.py``).
"""
from __future__ import annotations

import argparse
import copy
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
    _overlay_cover_title,
)
from src.autoslice.cover_screenshot_poster import (  # noqa: E402
    _compose_screenshot_poster_background,
)
from src.autoslice.cover_polish_gate import (  # noqa: E402
    _polish_face_binding_failure,
)
from src.autoslice.cover_host_identity_gate import (  # noqa: E402
    verify_final_host_identity,
)
from src.autoslice.publish_staging import _verify_polish_face_integrity  # noqa: E402
from src.autoslice.cover_route_evidence import (  # noqa: E402
    record_cover_route_execution,
    validate_cover_route_decision,
)
from src.autoslice.cover_repair_route_lineage import (  # noqa: E402
    screenshot_polish_source_input_sha256,
)
from src.autoslice.shadow_review import _sha256  # noqa: E402
from src.autoslice.verified_io import _matches_sha256  # noqa: E402


def _art_direction_from_record(payload: dict) -> LidoushaCoverArtDirection:
    fields = {field.name for field in dataclasses.fields(LidoushaCoverArtDirection)}
    kwargs = {k: v for k, v in payload.items() if k in fields}
    if isinstance(kwargs.get("cover_punch"), list):
        kwargs["cover_punch"] = tuple(kwargs["cover_punch"])
    return LidoushaCoverArtDirection(**kwargs)


def _adjacent_publish_document(record: dict) -> dict | None:
    staging = record.get("publish_staging")
    path_value = staging.get("publish_json_path") if isinstance(staging, dict) else None
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return None
    hashes = record.get("artifact_hashes")
    expected_hash = hashes.get("publish_draft_sha256") if isinstance(hashes, dict) else None
    if not isinstance(expected_hash, str) or not _matches_sha256(path, expected_hash):
        raise ValueError("adjacent publish draft hash is not bound by the record")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"adjacent publish draft is invalid: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("adjacent publish draft must be an object")
    if document.get("schema_version") != "shadow-publish-draft.v1":
        raise ValueError("adjacent publish draft schema mismatch")
    return document


def _resolve_candidate_and_title(
    record: dict, generation: dict, explicit_candidate_id: str
) -> tuple[str, str]:
    staging = record.get("publish_staging")
    publish = _adjacent_publish_document(record)
    candidates: list[str] = []
    for value in (
        record.get("candidate_id"),
        record.get("cid"),
        generation.get("candidate_id"),
        record.get("story_contract", {}).get("candidate_id")
        if isinstance(record.get("story_contract"), dict)
        else None,
        publish.get("candidate_id") if publish else None,
    ):
        if isinstance(value, str) and value.strip() and value.strip() not in candidates:
            candidates.append(value.strip())
    requested = explicit_candidate_id.strip()
    if requested:
        if len(candidates) != 1 or candidates[0] != requested:
            raise ValueError("explicit candidate_id disagrees with bound authority")
        candidate_id = requested
    elif len(candidates) == 1:
        candidate_id = candidates[0]
    else:
        raise ValueError("record has no unique bound candidate_id; pass verified --candidate-id")
    titles: list[str] = []
    for value in (
        record.get("title"),
        staging.get("title") if isinstance(staging, dict) else None,
        generation.get("title"),
        publish.get("title") if publish else None,
    ):
        if isinstance(value, str) and value.strip() and value.strip() not in titles:
            titles.append(value.strip())
    if len(titles) != 1:
        raise ValueError("record has conflicting or missing bound cover title")
    return candidate_id, titles[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, help="burned-final record.json")
    parser.add_argument(
        "--polished", default="", help="existing hash-bound polished PNG"
    )
    parser.add_argument(
        "--media", default="",
        help="legacy re-extraction input (rejected for formal screenshot binding)",
    )
    parser.add_argument(
        "--candidate-id", default="", help="verified candidate id from active authority"
    )
    parser.add_argument("--out", required=True, help="new final cover PNG path")
    parser.add_argument(
        "--host-identity-receipt",
        default="",
        help="optional final-host witness JSON for the new exact cover bytes",
    )
    parser.add_argument(
        "--face-verification-receipt",
        default="",
        help="optional existing PASS face witness JSON for the new exact cover bytes",
    )
    parser.add_argument(
        "--identity-landmark-title-exclusion",
        default="",
        help="existing hash-bound title zone/protected-landmark JSON",
    )
    parser.add_argument(
        "--cpa-base", default=os.environ.get("CPA_BASE_URL", ""),
    )
    parser.add_argument(
        "--cpa-key", default=os.environ.get("CPA_API_KEY", ""),
    )
    args = parser.parse_args()

    if args.media:
        print(
            "REFUSE: --media re-extracts/re-polishes pixels and cannot produce a "
            "formal screenshot binding; supply the existing --polished artifact",
            file=sys.stderr,
        )
        return 2

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
    try:
        candidate_id, title = _resolve_candidate_and_title(
            record, generation, args.candidate_id
        )
    except ValueError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    identity_exclusion = generation.get("identity_landmark_title_exclusion")
    if args.identity_landmark_title_exclusion:
        try:
            identity_exclusion = json.loads(
                Path(args.identity_landmark_title_exclusion).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"REFUSE: identity exclusion is invalid: {exc}", file=sys.stderr)
            return 2
    if not isinstance(identity_exclusion, dict):
        print(
            "REFUSE: formal screenshot repair needs an existing "
            "identity-landmark exclusion input",
            file=sys.stderr,
        )
        return 2
    route = generation.get("route_decision")
    if not isinstance(route, dict) or route.get("selected_treatment") != "screenshot_polish":
        print(
            "REFUSE: route is not authorized for screenshot_polish repair",
            file=sys.stderr,
        )
        return 2
    if route.get("actual_treatment") not in (None, "screenshot_polish"):
        print(
            "REFUSE: existing route already produced a different treatment",
            file=sys.stderr,
        )
        return 2
    cover_text = str(generation.get("cover_text") or "")
    art_payload = generation.get("art_direction")
    if not cover_text or not isinstance(art_payload, dict):
        print("REFUSE: record lacks cover_text/art_direction", file=sys.stderr)
        return 2
    art_direction = _art_direction_from_record(art_payload)
    out_path = Path(args.out)
    poster_path = out_path.with_suffix(".poster.png")

    if args.polished:
        polished = Path(args.polished)
    else:
        print("REFUSE: need --polished", file=sys.stderr)
        return 2
    source_input_sha256 = screenshot_polish_source_input_sha256(generation)
    if source_input_sha256 is None or not _matches_sha256(polished, source_input_sha256):
        print(
            "REFUSE: --polished does not match the frozen screenshot source input hash",
            file=sys.stderr,
        )
        return 2

    poster_evidence = _compose_screenshot_poster_background(
        polished,
        poster_path,
        art_direction=art_direction,
        source_ai_modified=True,
        face_safe_contain=True,
    )
    # renderer 的分行权威门在这里是 typed 退出码，不是 traceback。
    # CLI 没有状态机可转，正确表面就是 rc≠0 + 可读原因（Fable 裁定 3.3）。
    try:
        overlay = _overlay_cover_title(
            poster_path,
            out_path,
            cover_text=cover_text,
            art_direction=art_direction,
            full_text_cover_contract=generation.get("full_text_cover_contract"),
            identity_landmark_title_exclusion=identity_exclusion,
        )
    except ValueError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    new_sha = "sha256:" + _sha256(out_path)
    if args.face_verification_receipt:
        try:
            verification = json.loads(
                Path(args.face_verification_receipt).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"REFUSE: face verification receipt is invalid: {exc}", file=sys.stderr)
            return 2
        if not isinstance(verification, dict):
            print("REFUSE: face verification receipt must be an object", file=sys.stderr)
            return 2
    else:
        verification = _verify_polish_face_integrity(
            out_path, base_url=args.cpa_base, api_key=args.cpa_key
        )
    witness = verification.get("witness") if isinstance(verification, dict) else None
    witness_sha = (
        "sha256:" + str(witness.get("image_sha256"))
        if isinstance(witness, dict) and witness.get("image_sha256")
        else None
    )
    ok = verification.get("status") == "PASS" and witness_sha == new_sha
    repaired_generation = copy.deepcopy(generation)
    repaired_generation.update(
        {
            "status": "AI_COVER_READY",
            "candidate_id": candidate_id,
            "title": title,
            "final_cover": str(out_path.resolve()),
            "final_cover_sha256": new_sha,
            "ai_background": str(poster_path.resolve()),
            "ai_background_sha256": "sha256:" + _sha256(poster_path),
            "screenshot_graphic_poster": poster_evidence,
            "polish_face_verification": verification,
            **overlay,
        }
    )
    if args.face_verification_receipt:
        face_failure = _polish_face_binding_failure(
            repaired_generation, verification, "screenshot_polish"
        )
        if face_failure is not None:
            print(f"REFUSE: {face_failure}", file=sys.stderr)
            return 2
    host_receipt = None
    if args.host_identity_receipt:
        try:
            host_receipt = json.loads(
                Path(args.host_identity_receipt).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            print(f"REFUSE: host identity receipt is invalid: {exc}", file=sys.stderr)
            return 2
        if not isinstance(host_receipt, dict):
            print("REFUSE: host identity receipt must be an object", file=sys.stderr)
            return 2
        if host_receipt.get("final_cover_sha256") != new_sha:
            print("REFUSE: host identity receipt is not bound to new cover", file=sys.stderr)
            return 2
    elif ok and route.get("host_identity_required") is True:
        host_receipt = verify_final_host_identity(
            final_cover_path=out_path,
            final_cover_sha256=new_sha,
            reference_path=Path(str(generation["reference_image"])),
            base_url=args.cpa_base,
            api_key=args.cpa_key,
        )
    if host_receipt is not None:
        repaired_generation["final_host_identity_verification"] = host_receipt
    generation_path = out_path.with_suffix(".cover_generation.json")
    if ok:
        record_cover_route_execution(
            repaired_generation,
            actual_treatment="screenshot_polish",
            execution_status="READY",
            image_generation_attempted=True,
            image_generation_used=True,
            detail="deterministic screenshot title-layout repair completed",
        )
        if not validate_cover_route_decision(
            repaired_generation, allow_legacy_v1=False
        ):
            print(
                "REFUSE: repaired screenshot route evidence is not READY/host-bound",
                file=sys.stderr,
            )
            return 2
        generation_path.write_text(
            json.dumps(repaired_generation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    receipt = {
        "schema_version": "repair-screenshot-cover.v1",
        "repaired_at": datetime.now(timezone.utc).isoformat(),
        "record_path": str(Path(args.record).resolve()),
        "candidate_id": candidate_id,
        "previous_final_cover_sha256": generation.get("final_cover_sha256"),
        "polished_input_sha256": "sha256:" + _sha256(polished),
        "new_final_cover": str(out_path.resolve()),
        "new_final_cover_sha256": new_sha,
        "card_fit": (poster_evidence.get("source_frame_transform") or {}).get(
            "card_fit"
        ),
        "rendered_lines": overlay.get("rendered_lines"),
        "polish_face_verification": verification,
        "generation_manifest": str(generation_path.resolve()) if ok else None,
        "generation_manifest_sha256": (
            "sha256:" + _sha256(generation_path) if ok else None
        ),
        "host_identity_receipt_path": (
            str(Path(args.host_identity_receipt).resolve())
            if args.host_identity_receipt
            else None
        ),
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
