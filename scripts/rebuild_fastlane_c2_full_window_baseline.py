#!/usr/bin/env python3
"""Materialize C2's full padded-window reviewed baseline and delivery projection.

The C2 predecessor delivery SRT starts at the final cut, whereas replay's
source truth is the 118.92-second padded window.  This narrow builder makes
that distinction explicit: the 44-cue padded SRT is the only diagnostic
source, and the 22 retained cues are derived by an exact 9.750-second crop.
It has no upload or runtime side effects.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.materialize_operator_reviewed_subtitle_baseline import (
    compile_operator_baseline,
)
from src.autoslice.jingting_chunker import parse_srt_cues

CID = "auto_203011_328_389"
PADDED_SHA256 = "82a2acf76995030215e33cd03dc99aacc25f78eb9f619f7177675a74acd2247c"
PADDED_DURATION_MS = 118_920
FINAL_START_MS, FINAL_END_MS = 9_750, 71_070
SOURCE_RECORDING_SHA256 = "b5a7861c7283914742783ccc740531d9a21bdf78da7f5b8f90a463cb9d7f209e"
AUTHORITY = (
    "Claude raw line e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa; "
    "content 0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b; "
    "exhaustive C2 fastlane repair."
)
OPERATOR_AUTHORITY = {
    "kind": "REVIEWER_OPERATOR",
    "evidence_ref": (
        "维护者 source event: Claude JSONL session 0df2296b-500a-4681-ab5e-6fb46dc39579; "
        "user 555195ed-ec18-418d-a311-558f7e54291f; raw JSONL line sha256 including LF "
        "e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa; "
        "the exact C2 repairs are 0:14 小豆老公；； 不是你老公 and the 1:04 "
        "direct response to danmaku 小豆好吵（ as 小豆哪有好吵."
    ),
}
CHANGES = {
    7: "小豆老公；； 不是你老公",
    23: "小豆哪有好吵",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(cues) -> str:
    def stamp(value: int) -> str:
        h, rem = divmod(value, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1_000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return "\n\n".join(
        f"{i}\n{stamp(cue.start_ms)} --> {stamp(cue.end_ms)}\n{cue.text}"
        for i, cue in enumerate(cues, start=1)
    ) + "\n"


def project_delivery(cues, *, final_start_ms: int, final_end_ms: int) -> list[dict[str, object]]:
    """Return the strict full-grid crop witness; never clip a straddling cue."""
    straddlers = [cue for cue in cues if cue.start_ms < final_start_ms < cue.end_ms or cue.start_ms < final_end_ms < cue.end_ms]
    if straddlers:
        raise ValueError("C2_DELIVERY_PROJECTION_STRADDLER")
    delivery = [cue for cue in cues if cue.start_ms >= final_start_ms and cue.end_ms <= final_end_ms]
    if len(delivery) != 22:
        raise ValueError("C2_DELIVERY_PROJECTION_GRID_INVALID")
    if delivery[0].start_ms != 10_000 or delivery[-1].end_ms != 70_990:
        raise ValueError("C2_DELIVERY_PROJECTION_GEOMETRY_INVALID")
    return [
        {"full_cue": int(cue.index), "delivery_cue": n, "full_start_ms": cue.start_ms,
         "full_end_ms": cue.end_ms, "delivery_start_ms": cue.start_ms - final_start_ms,
         "delivery_end_ms": cue.end_ms - final_start_ms, "text": cue.text}
        for n, cue in enumerate(delivery, start=1)
    ]


def build(source: Path) -> tuple[str, str, dict[str, object], list[dict[str, object]]]:
    if source.is_symlink() or not source.is_file() or sha256(source) != PADDED_SHA256:
        raise ValueError("C2_PADDED_FRESH_SHA256_DRIFT")
    cues = parse_srt_cues(source.read_text(encoding="utf-8"))
    if len(cues) != 44 or cues[-1].end_ms > PADDED_DURATION_MS:
        raise ValueError("C2_PADDED_FULL_GRID_INVALID")
    reviewed = []
    rows = []
    for ordinal, cue in enumerate(cues, start=1):
        text = CHANGES.get(ordinal, cue.text)
        reviewed.append(type(cue)(index=str(ordinal), start_ms=cue.start_ms, end_ms=cue.end_ms, text=text))
        row: dict[str, object] = {"cue": ordinal, "disposition": "OPERATOR_UNCHANGED_FREEZE"}
        if ordinal in CHANGES:
            row = {
                "cue": ordinal,
                "disposition": "OPERATOR_EXACT_TEXT",
                "release_text": text,
                "decision_authority": "LEDGER_OPERATOR_AUTHORITY",
            }
        rows.append(row)
    diagnostic = source.read_text(encoding="utf-8")
    reviewed_text = render(reviewed)
    projection = project_delivery(
        reviewed, final_start_ms=FINAL_START_MS, final_end_ms=FINAL_END_MS,
    )
    # This is a pure witness for the canonical full-window crop; it is not an
    # input accepted by the replay CLI.
    ledger = {
        "schema_version": "operator-reviewed-subtitle-decisions.v3",
        "candidate_id": CID,
        "report_scope": "EXHAUSTIVE",
        "pipeline_srt_sha256": PADDED_SHA256,
        "operator_authority": OPERATOR_AUTHORITY,
        "cue_decisions": rows,
    }
    return diagnostic, reviewed_text, ledger, projection


def write_bundle(source: Path, out: Path, *, replace: bool) -> dict[str, object]:
    diagnostic, reviewed, ledger, projection = build(source)
    if not out.exists():
        out.mkdir(parents=True, mode=0o700)
    elif not out.is_dir() or out.is_symlink():
        raise ValueError("C2_BASELINE_OUTPUT_UNSAFE")
    if any(out.iterdir()) and not replace:
        raise ValueError("C2_BASELINE_OUTPUT_NOT_EMPTY")
    with tempfile.TemporaryDirectory(prefix="c2-full-window-", dir=out.parent) as temp:
        stage = Path(temp)
        source_stage = stage / "source.srt"
        reviewed_stage = stage / "reviewed.srt"
        source_stage.write_text(diagnostic, encoding="utf-8")
        reviewed_stage.write_text(reviewed, encoding="utf-8")
        ledger_path = stage / "ledger.json"
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = compile_operator_baseline(
            source_srt=source_stage, reviewed_srt=reviewed_stage, candidate_id=CID,
            authority=AUTHORITY, source_recording_basename="22966160_20260813-20-30-11.mp4",
            source_recording_sha256=SOURCE_RECORDING_SHA256, absolute_source_start_ms=318_740,
            absolute_source_end_ms=437_660, time_domain="PIECE_LOCAL",
            decision_ledger=ledger,
        )
    manifest = dict(result["baseline_manifest"])
    lanes = dict(manifest["operator_truth_lanes"])
    lanes["pipeline_diagnostic"] = {
        **dict(lanes["pipeline_diagnostic"]), "path": f"{CID}.pipeline-diagnostic.srt",
    }
    lanes["decision_ledger"] = {
        **dict(lanes["decision_ledger"]), "path": f"{CID}.operator-decisions.v3.json",
    }
    lanes["diff_receipt"] = {
        **dict(lanes["diff_receipt"]), "path": f"{CID}.operator-truth-diff.v2.json",
    }
    manifest["operator_truth_lanes"] = lanes
    outputs = {
        f"{CID}.reviewed.srt": result["baseline_srt"],
        f"{CID}.pipeline-diagnostic.srt": result["pipeline_diagnostic_srt"],
        f"{CID}.operator-decisions.v3.json": result["decision_ledger"],
        f"{CID}.operator-truth-diff.v2.json": result["diagnostic_diff"],
        f"{CID}.subtitle-baseline.v1.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        f"{CID}.operator-reviewed-subtitle-baseline-delivery.v1.json": json.dumps(result["receipt"], ensure_ascii=False, indent=2) + "\n",
        f"{CID}.operator-reviewed-subtitle-baseline-delivery.v1.json": json.dumps(result["receipt"], ensure_ascii=False, indent=2) + "\n",
        f"{CID}.full-window-delivery-projection.v1.json": json.dumps({
            "schema_version": "fastlane-c2-full-window-delivery-projection.v1", "candidate_id": CID,
            "padded_fresh_sha256": PADDED_SHA256, "final_start_ms": FINAL_START_MS,
            "final_end_ms": FINAL_END_MS, "delivery_cue_count": len(projection), "rows": projection,
        }, ensure_ascii=False, indent=2) + "\n",
    }
    for name, payload in outputs.items():
        path = out / name
        if path.exists() and not replace:
            raise ValueError("C2_BASELINE_OUTPUT_EXISTS")
        path.write_text(payload, encoding="utf-8")
    return {"candidate_id": CID, "source_sha256": PADDED_SHA256, "full_cue_count": 44,
            "delivery_cue_count": 22, "changed_full_cues": sorted(CHANGES),
            "artifacts": {name: sha256(out / name) for name in sorted(outputs)}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "assets/lidousha/fastlane_c2_private/auto_203011_328_389.padded.fresh.srt")
    parser.add_argument("--out", type=Path, default=ROOT / "assets/lidousha/reviewed_subtitle_baselines")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    print(json.dumps(write_bundle(args.source, args.out, replace=args.replace), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
