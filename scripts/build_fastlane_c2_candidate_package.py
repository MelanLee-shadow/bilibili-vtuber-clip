#!/usr/bin/env python3
"""Create C2's private final-byte candidate package without any upload/state write.

This is intentionally a one-candidate builder.  It consumes the sealed C2
subtitle authority, an exact predecessor delivery, and the recorded C2 cover
generation attempt.  It does *not* claim that its output has a canonical
record, package audit, final-human receipt, or upload authority.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_fastlane_c2_private_successor import CID, load, project
from src.autoslice.cover_generation import (
    _lidousha_cover_art_direction,
    _overlay_lidousha_cover_title,
)
from src.autoslice.recut_materialization import _burn_preview_subtitles


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_exact(source: Path, destination: Path, expected: str) -> None:
    if not source.is_file() or sha256(source) != expected:
        raise SystemExit(f"input hash drift: {source}")
    shutil.copy2(source, destination)
    if sha256(destination) != expected:
        raise SystemExit(f"copy hash drift: {destination}")


def _replace_ass_cues(source: Path, destination: Path) -> None:
    replacements = {
        "Dialogue: 0,0:00:08.72,0:00:11.24,Default,,0,0,0,,": "小豆老公；； 不是你老公",
        "Dialogue: 0,0:00:56.08,0:00:58.86,Default,,0,0,0,,": "小豆哪有好吵",
    }
    seen = {prefix: 0 for prefix in replacements}
    result: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        for prefix, text in replacements.items():
            if line.startswith(prefix):
                line = prefix + text
                seen[prefix] += 1
        result.append(line)
    if set(seen.values()) != {1}:
        raise SystemExit(f"ASS cue binding mismatch: {seen}")
    destination.write_text("\n".join(result) + "\n", encoding="utf-8")


def _cue_graph(predecessor: Path, final_srt: Path, authority: dict[str, object]) -> dict[str, object]:
    old = predecessor.read_text(encoding="utf-8").strip().split("\n\n")
    new = final_srt.read_text(encoding="utf-8").strip().split("\n\n")
    if len(old) != len(new) or len(old) != 22:
        raise SystemExit("C2 cue count drift")
    rows = []
    for index, (before, after) in enumerate(zip(old, new), 1):
        old_lines, new_lines = before.splitlines(), after.splitlines()
        if old_lines[:2] != new_lines[:2]:
            raise SystemExit(f"C2 timing drift at cue {index}")
        rows.append({
            "cue": index,
            "time": old_lines[1],
            "before": old_lines[2],
            "after": new_lines[2],
            "disposition": "OPERATOR_REPAIR" if index == 5 else "FROZEN",
        })
    if rows[4]["after"] != authority["exact_subtitle_changes"][0]["after"]:
        raise SystemExit("C2 cue 5 authority drift")
    if rows[20]["after"] != authority["chat_context_freeze"]["release_text"]:
        raise SystemExit("C2 cue 21 direct-response freeze drift")
    if sum(row["disposition"] == "OPERATOR_REPAIR" for row in rows) != 1:
        raise SystemExit("C2 mutation scope widened")
    return {
        "schema_version": "fastlane-c2-cue-graph.v1",
        "candidate_id": CID,
        "predecessor_srt_sha256": sha256(predecessor),
        "final_srt_sha256": sha256(final_srt),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predecessor-dir", type=Path, required=True)
    parser.add_argument("--cover-attempt-dir", type=Path, required=True)
    parser.add_argument("--branding-intro", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise SystemExit("refusing to overwrite private output")
    authority = load()
    predecessor = args.predecessor_dir.resolve()
    cover_attempt = args.cover_attempt_dir.resolve()
    intro = args.branding_intro.resolve()
    if not intro.is_file() or sha256(intro) != "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5":
        raise SystemExit("C2 branding intro missing or drifted")

    source_srt = predecessor / f"{CID}.recut.srt"
    source_video = predecessor / f"{CID}.recut.mp4"
    source_ass = predecessor / f"{CID}.recut.final-sapphire72.ass"
    if not source_ass.is_file():
        raise SystemExit("C2 predecessor ASS missing")
    out.mkdir(mode=0o700, parents=True)
    final_srt = out / f"{CID}.recut.srt"
    copy_exact(source_srt, out / "predecessor.recut.srt", authority["predecessor"]["srt_sha256"])
    copy_exact(source_video, out / f"{CID}.recut.mp4", authority["predecessor"]["video_sha256"])
    final_srt.write_text(project(source_srt.read_text(encoding="utf-8"), authority), encoding="utf-8")
    final_ass = out / f"{CID}.recut.final-sapphire72.ass"
    _replace_ass_cues(source_ass, final_ass)
    graph = _cue_graph(source_srt, final_srt, authority)
    (out / "cue-graph.v1.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # The actual C2 CPA image output is copied with its redacted request/response.
    # Only its title layer is regenerated deterministically for the frozen new copy.
    background = cover_attempt / f"{CID}.cover.ai-bg.png"
    request = cover_attempt / f"{CID}.cover.cpa-request.redacted.json"
    response = cover_attempt / f"{CID}.cover.cpa-response.redacted.json"
    reference = cover_attempt / f"{CID}.cover.cover-ref.png"
    for path in (background, request, response, reference):
        if not path.is_file():
            raise SystemExit(f"C2 cover attempt input missing: {path}")
    route_background = out / f"{CID}.cover.route-background.png"
    shutil.copy2(background, route_background)
    for path in (request, response, reference):
        shutil.copy2(path, out / path.name)
    title = str(authority["identity"]["title"])
    cover_lines = tuple(str(line) for line in authority["identity"]["cover_lines"])
    direction = _lidousha_cover_art_direction(
        candidate_id=CID,
        title=title,
        cover_text="\n".join(cover_lines),
        art_direction_llm_call=None,
        allow_punch=False,
        diversity_slot=0,
        story_hook="弹幕拔智齿后求亲亲，李豆沙回应不是你老公，只会嘲笑。",
    )
    direction = dataclasses.replace(
        direction,
        layout="right-split",
        cover_punch=cover_lines,
        cover_punch_semantic_review=None,
    )
    final_cover = out / f"{CID}.cover.png"
    overlay = _overlay_lidousha_cover_title(
        route_background, final_cover, cover_text="\n".join(cover_lines), art_direction=direction
    )
    mask = final_cover.with_name(final_cover.name.removesuffix(".png") + ".title-mask.png")
    pre_overlay = final_cover.with_name(final_cover.name.removesuffix(".png") + ".pre-overlay.png")
    if not mask.is_file() or not pre_overlay.is_file():
        raise SystemExit("C2 deterministic cover overlay artifacts missing")
    (out / f"{CID}.title.txt").write_text(title + "\n", encoding="utf-8")
    cover = {
        "schema_version": "fastlane-c2-private-cover-reprojection.v1",
        "candidate_id": CID,
        "status": "TECHNICALLY_RENDERED_PENDING_CANONICAL_COVER_AUDIT",
        "upload_allowed": False,
        "actual_treatment": "cpa_redraw",
        "title": title,
        "rendered_lines": list(cover_lines),
        "route_background": route_background.name,
        "route_background_sha256": "sha256:" + sha256(route_background),
        "provider_attempt": {
            "request": request.name, "request_sha256": "sha256:" + sha256(out / request.name),
            "response": response.name, "response_sha256": "sha256:" + sha256(out / response.name),
            "reference": reference.name, "reference_sha256": "sha256:" + sha256(out / reference.name),
        },
        "final_cover": final_cover.name,
        "final_cover_sha256": "sha256:" + sha256(final_cover),
        "pre_overlay": pre_overlay.name,
        "pre_overlay_sha256": "sha256:" + sha256(pre_overlay),
        "title_mask": mask.name,
        "title_mask_sha256": "sha256:" + sha256(mask),
        "overlay": overlay,
        "known_gate_gap": "cover punch semantic review and final identity verifier must be rebuilt by the canonical C2 package path",
    }
    (out / "cover-reprojection.v1.json").write_text(json.dumps(cover, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    burned = _burn_preview_subtitles(
        {
            "status": "MATERIALIZED",
            "media_path": str(out / f"{CID}.recut.mp4"),
            "subtitle_path": str(final_srt),
            "subtitle_ass_path": str(final_ass),
            "subtitle_style": "lidousha-final-sapphire72",
            "artifact_hashes": {"ass_sha256": "sha256:" + sha256(final_ass)},
        },
        run_ffmpeg=True,
        branding_intro={
            "intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2",
            "media_path": intro,
            "media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5",
            "manifest_path": ROOT / "assets/lidousha/intro/branding_intro.v1.json",
            "manifest_sha256": sha256(ROOT / "assets/lidousha/intro/branding_intro.v1.json"),
            "candidates": [{"intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2", "media_path": intro, "media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5"}],
            "rotation_mode": "recorded-delivery-authority",
            "recorded_delivery_binding": {"intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2", "intro_media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5", "intro_offset_ms": 5749},
        },
    )
    burned_info = (burned or {}).get("burned_preview")
    if not isinstance(burned_info, dict) or burned_info.get("status") != "BURNED":
        raise SystemExit("C2 canonical subtitle burn failed")
    burned_path = Path(str(burned_info["path"]))
    artifacts = {path.name: sha256(path) for path in out.iterdir() if path.is_file()}
    proposal = {
        "schema_version": "fastlane-c2-root-technical-receipt-template.v1",
        "status": "PENDING_ROOT_EXACT_BYTE_AND_PERCEPTUAL_REVIEW",
        "candidate_id": CID,
        "upload_allowed": False,
        "title": title,
        "cover_lines": list(cover_lines),
        "artifacts": artifacts,
        "required_root_checks": [
            "cue 5 rendered text and audio window", "cue 21 direct-response window",
            "full burned playback and intro transition", "cover identity and rendered text",
            "canonical C2 record, review manifest, package audit, and upload manifest proposal",
        ],
        "not_a_final_human_review_receipt": True,
    }
    (out / "root-technical-receipt.template.v1.json").write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_id": CID, "out": str(out), "burned": burned_path.name, "artifacts": artifacts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
