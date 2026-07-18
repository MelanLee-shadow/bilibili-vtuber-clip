#!/usr/bin/env python3
"""Apply a HUMAN text correction, rerun speaker finalization, then re-burn.

No full re-produce and no cover regeneration.  The ordering is mandatory:
human text is committed to the clean SRT first, then CAM++/context/turn
overrides regenerate the speaker SRT and colour ASS, and only that ASS is
burned.  Upload stays OFF.

Usage:
  apply_subtitle_correction.py --cid <cid> --date <YYYY-MM-DD> \
      --delivery '/…/lidousha/<date>/<name>.mp4' \
      --replace '旧文字=新文字' [--replace ... ]     # surgical text swaps
  # or replace whole cues by 1-based index:
      --set-line 3='李姐能帮忙跟李豆沙说句'
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.run_auto_review_shadow_pipeline import _burn_preview_subtitles  # noqa: E402
from scripts.run_auto_review_shadow_pipeline import _sha256  # noqa: E402
from scripts.produce_slice_package import run_speaker_finalizer  # noqa: E402
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID  # noqa: E402
from scripts.suggest_upload_tags import generate_upload_tags  # noqa: E402
from src.autoslice.branding_intro import BrandingIntroError, require_branding_intro  # noqa: E402

BASE = Path("/opt/bilive/autoslice")


def _srt_blocks(text: str):
    return [b for b in text.replace("\r\n", "\n").strip().split("\n\n") if b.strip()]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cid", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--delivery", required=True, type=Path, help="delivered .mp4 to refresh")
    p.add_argument("--replace", action="append", default=[], metavar="OLD=NEW", help="surgical text swap across all cues")
    p.add_argument("--set-line", action="append", default=[], metavar="N=TEXT", help="replace the whole text of 1-based cue N")
    p.add_argument(
        "--refresh-only",
        action="store_true",
        help="re-burn the current SRT and refresh a stale delivery mirror without changing text",
    )
    p.add_argument("--out-base", type=Path, default=BASE)
    p.add_argument("--speaker-overrides", type=Path, help="optional hash-bound reviewed turn/split/overlap decisions")
    p.add_argument(
        "--speaker-python",
        type=Path,
        default=Path(os.environ.get("AUTOSLICE_SPEAKER_PYTHON", "/opt/bilive/autoslice/venv-diar/bin/python")),
    )
    args = p.parse_args(argv)

    recut_dir = args.out_base / "out" / args.date / args.cid / "replacement_recuts"
    record_path = recut_dir / f"{args.cid}.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    srt_path = Path(str(record["subtitle_path"]))
    srt = srt_path.read_text(encoding="utf-8")

    before = srt
    for pair in args.replace:
        old, _, new = pair.partition("=")
        srt = srt.replace(old, new)
    if args.set_line:
        blocks = _srt_blocks(srt)
        wants = {int(n): t for n, _, t in (s.partition("=") for s in args.set_line)}
        for i, b in enumerate(blocks, start=1):
            if i in wants:
                lines = b.split("\n")
                blocks[i - 1] = "\n".join(lines[:2] + [wants[i]])  # keep index + timing, swap text
        srt = "\n\n".join(blocks) + "\n"
    if srt == before and not args.refresh_only:
        print("NO_CHANGE: nothing matched the correction — check --replace/--set-line", file=sys.stderr)
        return 2
    before_hash = hashlib.sha256(before.encode("utf-8")).hexdigest()
    try:
        branding_intro = require_branding_intro(ROOT)
    except BrandingIntroError as exc:
        print(f"BRANDING_INTRO_UNAVAILABLE: {exc}", file=sys.stderr)
        return 1
    srt_path.write_text(srt, encoding="utf-8")
    speaker_mode = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    if speaker_mode not in ("uniform_host", "required", "auto"):
        speaker_mode = "uniform_host"
    print(f"corrected {srt_path.name}; speaker_mode={speaker_mode}")

    speaker_srt = srt_path.with_suffix(".speaker-final.srt")
    speaker_ass = srt_path.with_suffix(".speaker-final.ass")
    speaker_manifest_path = srt_path.with_suffix(".speaker-final.json")
    if speaker_mode == "uniform_host":
        # Ivan 2026-07-13 policy: no speaker separation in any deliverable —
        # burn the corrected text directly in the single host style.
        try:
            reburn = _burn_preview_subtitles(
                {
                    "status": "MATERIALIZED",
                    "media_path": record["media_path"],
                    "subtitle_path": str(srt_path),
                },
                run_ffmpeg=True,
                branding_intro=branding_intro,
            )
        except Exception:
            srt_path.write_text(before, encoding="utf-8")
            raise
        speaker_manifest = None
    else:
      try:
        speaker_manifest = run_speaker_finalizer(
            host="localhost",
            candidate_id=args.cid,
            media_path=Path(str(record["media_path"])),
            text_srt_path=srt_path,
            output_srt_path=speaker_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=recut_dir / f"{args.cid}.speaker-work",
            override_path=args.speaker_overrides,
            speaker_python=args.speaker_python,
        )
        reburn = _burn_preview_subtitles(
            {
                "status": "MATERIALIZED",
                "media_path": record["media_path"],
                "subtitle_path": str(srt_path),
                "subtitle_ass_path": str(speaker_ass),
                "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
                "artifact_hashes": {"ass_sha256": "sha256:" + _sha256(speaker_ass)},
            },
            run_ffmpeg=True,
            branding_intro=branding_intro,
        )
      except Exception:
        srt_path.write_text(before, encoding="utf-8")
        raise
    burned_value = (reburn or {}).get("burned_preview") if isinstance(reburn, dict) else None
    burned = Path(str(burned_value.get("path"))) if isinstance(burned_value, dict) and burned_value.get("path") else None
    if not burned or not burned.is_file() or burned_value.get("status") != "BURNED":
        srt_path.write_text(before, encoding="utf-8")
        print(f"BURN_FAILED: {burned_value}", file=sys.stderr)
        return 1

    correction_manifest = {
        "schema_version": "human-subtitle-correction.v2",
        "stage_order": "human_text_then_speaker_then_burn",
        "corrected_at": datetime.now(timezone.utc).isoformat(),
        "candidate_id": args.cid,
        "before_srt_sha256": before_hash,
        "after_srt_sha256": _sha256(srt_path),
        "replace_operations": args.replace,
        "set_line_operations": args.set_line,
        "refresh_only": args.refresh_only,
        "speaker_mode": speaker_mode,
        "speaker_manifest": str(speaker_manifest_path) if speaker_manifest is not None else None,
        "speaker_manifest_sha256": _sha256(speaker_manifest_path) if speaker_manifest is not None else None,
        "burned_media": str(burned),
        "burned_media_sha256": _sha256(burned),
        "upload_enabled": False,
    }
    correction_manifest_path = recut_dir / f"{args.cid}.human-text-correction.json"
    correction_manifest_path.write_text(
        json.dumps(correction_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    updated = dict(record)
    hashes = dict(updated.get("artifact_hashes") or {})
    hashes.update({"subtitle_sha256": "sha256:" + _sha256(srt_path),
                   "burned_video_sha256": "sha256:" + _sha256(burned)})
    if speaker_manifest is not None:
        hashes.update(
            {
                "speaker_review_srt_sha256": "sha256:" + _sha256(speaker_srt),
                "ass_sha256": "sha256:" + _sha256(speaker_ass),
            }
        )
    updated.update(
        {
            "artifact_hashes": hashes,
            "speaker_mode": speaker_mode,
            "speaker_review_srt_path": str(speaker_srt) if speaker_manifest is not None else None,
            "subtitle_ass_path": str(speaker_ass) if speaker_manifest is not None else None,
            "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID if speaker_manifest is not None else "lidousha-final-sapphire72",
            "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest is not None else None,
            "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest is not None else None,
            "speaker_finalization": speaker_manifest,
            "human_text_correction_manifest_path": str(correction_manifest_path),
            "human_text_correction_manifest_sha256": "sha256:" + _sha256(correction_manifest_path),
            "burned_preview": burned_value,
        }
    )
    # 字幕文本变了 → tag 必须按修正后的成品字幕重算(Ivan 2026-07-13 铁律);
    # fail-safe: 重算失败记 FAILED, 不阻塞修正交付。已发布稿件的 B 站侧 tag
    # 同步走 bili_update_tags.py 编辑, 不在本脚本职责内。
    staging_title = str(((record.get("publish_staging") or {}).get("title")) or "")
    if staging_title:
        updated["upload_tags"] = generate_upload_tags(staging_title, srt_path, timeout=180.0)
    record_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    shutil.copy2(burned, args.delivery)
    sidecars = [
        (srt_path, ".srt"),
        (correction_manifest_path, ".human-text-correction.json"),
        (record_path, ".record.json"),
    ]
    if speaker_manifest is not None:
        sidecars[1:1] = [
            (speaker_srt, ".speaker.srt"),
            (speaker_ass, ".speaker.ass"),
            (speaker_manifest_path, ".speaker.json"),
        ]
    else:
        # uniform_host: stale speaker sidecars from an older run must not
        # outlive the correction they no longer describe.
        for suffix in (".speaker.srt", ".speaker.ass", ".speaker.json"):
            args.delivery.with_suffix(suffix).unlink(missing_ok=True)
    for source, suffix in sidecars:
        shutil.copy2(source, args.delivery.with_suffix(suffix))
    print(f"speaker-final re-burn + delivery refreshed → {args.delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
