#!/usr/bin/env python3
"""Apply a HUMAN subtitle correction to an already-produced slice and re-burn —
no full re-produce, no cover regen.  For the BCUT-layer mishearings the automatic
BCUT+AGY+CPA pipeline can't fix (context-ambiguous speech names, 核听-only calls):
Ivan gives the right text, this edits the recut .srt, re-burns the sapphire72
subtitle, and refreshes the delivered .mp4.  Upload stays OFF.

Usage:
  apply_subtitle_correction.py --cid <cid> --date <YYYY-MM-DD> \
      --delivery '/…/lidousha/<date>/<name>.mp4' \
      --replace '旧文字=新文字' [--replace ... ]     # surgical text swaps
  # or replace whole cues by 1-based index:
      --set-line 3='李姐能帮忙跟李豆沙说句'
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.run_auto_review_shadow_pipeline import _burn_preview_subtitles  # noqa: E402

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
    p.add_argument("--out-base", type=Path, default=BASE)
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
    if srt == before:
        print("NO_CHANGE: nothing matched the correction — check --replace/--set-line", file=sys.stderr)
        return 2
    srt_path.write_text(srt, encoding="utf-8")
    print(f"corrected {srt_path.name}")

    reburn = _burn_preview_subtitles(
        {"status": "MATERIALIZED", "media_path": record["media_path"], "subtitle_path": str(srt_path)},
        run_ffmpeg=True,
    )
    burned = next(recut_dir.glob(f"{args.cid}.recut.burned-final-*.mp4"), None)
    if not burned or not burned.is_file():
        print(f"BURN_FAILED: {reburn.get('burned_preview') if reburn else None}", file=sys.stderr)
        return 1
    shutil.copy2(burned, args.delivery)
    print(f"re-burned + delivered → {args.delivery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
