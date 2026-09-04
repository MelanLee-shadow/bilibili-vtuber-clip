#!/usr/bin/env python3
"""Build C1's one-candidate, no-upload subtitle successor from sealed inputs.

This is intentionally not a general redelivery lane.  It accepts only C1's
three known predecessor bytes and writes only a new private directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.recut_materialization import _burn_preview_subtitles

AUTHORITY = ROOT / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.subtitle-correction.v1.json"
CID = "auto_173005_934_1166"
_SRT_BLOCK = re.compile(r"(\d+)\n(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)\n(.*?)(?=\n\n|\Z)", re.S)
_ASS_DIALOGUE = re.compile(r"^(Dialogue: \d+,)(\d+:\d\d:\d\d\.\d\d),(\d+:\d\d:\d\d\.\d\d)(,.*?,,0,0,0,,)(.*)$")


@dataclass(frozen=True)
class Cue:
    source_index: int
    start_ms: int
    end_ms: int
    text: str


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _srt_ms(value: str) -> int:
    hours, minutes, seconds_millis = value.split(":")
    seconds, millis = seconds_millis.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def srt_timestamp(value: int) -> str:
    hours, rest = divmod(value, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def ass_timestamp(value: int) -> str:
    centiseconds = value // 10
    hours, rest = divmod(centiseconds, 360_000)
    minutes, rest = divmod(rest, 6_000)
    seconds, cents = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cents:02d}"


def load_authority(path: Path = AUTHORITY) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "fastlane-c1-private-subtitle-correction.v1":
        raise ValueError("invalid C1 correction authority")
    if value.get("candidate_id") != CID or value.get("upload_allowed") is not False:
        raise ValueError("C1 authority scope is invalid")
    return value


def parse_srt(text: str) -> list[Cue]:
    rows = [
        Cue(int(match.group(1)), _srt_ms(match.group(2)), _srt_ms(match.group(3)), match.group(4))
        for match in _SRT_BLOCK.finditer(text.strip())
    ]
    if len(rows) != 36 or [row.source_index for row in rows] != list(range(1, 37)):
        raise ValueError("C1 predecessor SRT is not the expected 36-cue source grid")
    return rows


def project_cues(source: list[Cue], authority: dict[str, object]) -> list[Cue]:
    mutation = authority["mutations"]
    if not isinstance(mutation, dict):
        raise ValueError("invalid C1 mutations")
    replace = mutation["replace"]
    dropped = set(mutation["drop"])
    insertion = mutation["insertion"]
    if not isinstance(replace, dict) or not isinstance(insertion, dict):
        raise ValueError("invalid C1 mutation shape")
    result: list[Cue] = []
    for cue in source:
        if cue.source_index not in dropped:
            result.append(Cue(cue.source_index, cue.start_ms, cue.end_ms, str(replace.get(str(cue.source_index), cue.text))))
        if cue.source_index == mutation["insert_after_source_cue"]:
            result.append(Cue(0, int(insertion["start_ms"]), int(insertion["end_ms"]), str(insertion["text"])))
    if [cue.source_index for cue in result if cue.source_index] != [cue.source_index for cue in source if cue.source_index not in dropped]:
        raise ValueError("C1 projection lost a live source cue")
    return result


def render_srt(cues: list[Cue]) -> str:
    return "\n\n".join(
        f"{index}\n{srt_timestamp(cue.start_ms)} --> {srt_timestamp(cue.end_ms)}\n{cue.text}"
        for index, cue in enumerate(cues, 1)
    ) + "\n"


def project_ass(text: str, source: list[Cue], projected: list[Cue]) -> str:
    by_timing = {(cue.start_ms, cue.end_ms): cue for cue in projected if cue.source_index}
    kept_timings = set(by_timing)
    output: list[str] = []
    source_timings = {(cue.start_ms, cue.end_ms) for cue in source}
    source_cue_by_timing = {(cue.start_ms, cue.end_ms): cue.source_index for cue in source}
    insertion = next(cue for cue in projected if cue.source_index == 0)
    inserted = False
    for line in text.splitlines():
        match = _ASS_DIALOGUE.match(line)
        if match is None:
            output.append(line)
            continue
        start = int(match.group(2).split(":")[0]) * 3_600_000 + int(match.group(2).split(":")[1]) * 60_000 + int(match.group(2).split(":")[2].split(".")[0]) * 1000 + int(match.group(2).split(".")[1]) * 10
        end = int(match.group(3).split(":")[0]) * 3_600_000 + int(match.group(3).split(":")[1]) * 60_000 + int(match.group(3).split(":")[2].split(".")[0]) * 1000 + int(match.group(3).split(".")[1]) * 10
        timing = (start, end)
        if timing in source_timings and timing not in kept_timings:
            continue
        cue = by_timing.get(timing)
        output.append(line if cue is None else f"{match.group(1)}{match.group(2)},{match.group(3)}{match.group(4)}{cue.text}")
        if source_cue_by_timing.get(timing) == 19:
            output.append(f"Dialogue: 0,{ass_timestamp(insertion.start_ms)},{ass_timestamp(insertion.end_ms)},Default,,0,0,0,,{insertion.text}")
            inserted = True
    if not inserted:
        raise ValueError("C1 predecessor ASS lacks source cue 19 for deterministic insertion ordering")
    return "\n".join(output) + "\n"


def validate_projection(source: list[Cue], projected: list[Cue], authority: dict[str, object]) -> None:
    classification = authority["cue_classification"]
    mutation = authority["mutations"]
    if not isinstance(classification, dict) or not isinstance(mutation, dict):
        raise ValueError("invalid C1 authority")
    live = set(classification["live_lidousha_cues"])
    watched = set(classification["watched_video_cues"])
    if live | watched != set(range(1, 37)) or live & watched:
        raise ValueError("C1 cue classification must partition all 36 source cues")
    if set(mutation["drop"]) != watched:
        raise ValueError("C1 may drop only perceptually classified watched-video cues")
    if {cue.source_index for cue in projected if cue.source_index} != live:
        raise ValueError("C1 did not preserve every classified live Li Dousha cue")
    inserted = [cue for cue in projected if cue.source_index == 0]
    if len(inserted) != 1 or not (91_000 <= inserted[0].start_ms <= 91_251 <= inserted[0].end_ms <= 92_000):
        raise ValueError("C1 live insertion timing is outside the 01:37 public / 01:31 source review window")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predecessor-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--burn-preview", action="store_true", help="use the canonical recut materializer")
    parser.add_argument("--branding-intro", type=Path, help="hash-bound existing Z1 intro for a delivery-like private review render")
    args = parser.parse_args()
    authority = load_authority()
    predecessor = args.predecessor_dir.resolve()
    out = args.out.resolve()
    if out.exists():
        raise SystemExit("refusing to overwrite an existing private successor")
    inputs = {
        "video": predecessor / f"{CID}.recut.mp4",
        "srt": predecessor / f"{CID}.recut.srt",
        "ass": predecessor / f"{CID}.recut.final-sapphire72.ass",
    }
    expected = authority["predecessor"]
    if not isinstance(expected, dict) or any(not path.is_file() for path in inputs.values()):
        raise SystemExit("missing required C1 predecessor input")
    for name, path in inputs.items():
        if sha256(path) != expected[f"{name}_sha256"]:
            raise SystemExit(f"C1 predecessor {name} hash drift")
    source = parse_srt(inputs["srt"].read_text(encoding="utf-8"))
    projected = project_cues(source, authority)
    validate_projection(source, projected, authority)
    out.mkdir(mode=0o700, parents=True)
    srt = render_srt(projected)
    ass = project_ass(inputs["ass"].read_text(encoding="utf-8"), source, projected)
    (out / f"{CID}.recut.srt").write_text(srt, encoding="utf-8")
    (out / f"{CID}.recut.final-sapphire72.ass").write_text(ass, encoding="utf-8")
    shutil.copy2(inputs["video"], out / f"{CID}.recut.mp4")
    artifacts = {path.name: sha256(path) for path in out.iterdir() if path.is_file()}
    if args.branding_intro is not None and not args.burn_preview:
        raise SystemExit("--branding-intro requires --burn-preview")
    if args.burn_preview:
        intro = None
        if args.branding_intro is not None:
            intro_path = args.branding_intro.resolve()
            if not intro_path.is_file() or sha256(intro_path) != "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5":
                raise SystemExit("C1 recorded Z1 branding intro is missing or drifted")
            intro = {
                "intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2",
                "media_path": intro_path,
                "media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5",
                "manifest_path": ROOT / "assets/lidousha/intro/branding_intro.v1.json",
                "manifest_sha256": sha256(ROOT / "assets/lidousha/intro/branding_intro.v1.json"),
                "candidates": [{"intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2", "media_path": intro_path, "media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5"}],
                "rotation_mode": "recorded-delivery-authority",
                "recorded_delivery_binding": {"intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2", "intro_media_sha256": "bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5", "intro_offset_ms": 5749},
            }
        rendered = _burn_preview_subtitles({"status": "MATERIALIZED", "media_path": str(out / f"{CID}.recut.mp4"), "subtitle_path": str(out / f"{CID}.recut.srt"), "subtitle_ass_path": str(out / f"{CID}.recut.final-sapphire72.ass"), "subtitle_style": "lidousha-final-sapphire72", "artifact_hashes": {"ass_sha256": "sha256:" + sha256(out / f"{CID}.recut.final-sapphire72.ass")}}, run_ffmpeg=True, branding_intro=intro)
        burned_value = rendered.get("burned_preview") if isinstance(rendered, dict) else None
        if not isinstance(burned_value, dict) or burned_value.get("status") != "BURNED":
            raise SystemExit("C1 canonical private materializer failed")
        burned = Path(str(burned_value["path"]))
        artifacts[burned.name] = sha256(burned)
    receipt = {"schema_version": "fastlane-c1-private-successor.v1", "candidate_id": CID, "title": authority["title"], "upload_allowed": False, "future_delivery_constraint": authority["future_delivery_constraint"], "source_cue_count": len(source), "release_cue_count": len(projected), "dropped_watched_video_cue_count": len(source) - len([cue for cue in projected if cue.source_index]), "artifacts": artifacts}
    (out / "fastlane-c1-private-successor.v1.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
