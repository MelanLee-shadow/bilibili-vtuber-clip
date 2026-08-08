"""Offline probe: restatement-pair landscape over the 2026-08-07 harvest.

Runs the restatement detector wide-open (low thresholds) over the pristine
machine transcripts of the five Ivan-reviewed candidates, classifies every
detected pair against the ivan-speaker-truth-diff.v2 artifacts, and prints a
threshold grid so the production defaults are picked from evidence instead of
taste. Read-only; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.autoslice.restatement_recall import (
    RestatementCue,
    find_restatement_pairs,
    score_pair,
)

_TIME_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _seconds(stamp: str) -> float:
    match = _TIME_RE.match(stamp.strip())
    if not match:
        raise ValueError(f"bad timestamp {stamp!r}")
    h, m, s, ms = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s + ms / 1000


def load_cues(srt_path: Path) -> list[RestatementCue]:
    blocks = re.split(r"\n\s*\n", srt_path.read_text(encoding="utf-8").strip())
    cues = []
    for block in blocks:
        lines = block.splitlines()
        text = "\n".join(lines[2:])
        label = None
        match = re.match(r"^\[([^\]]+)\] ?", text)
        if match:
            label, text = match.group(1), text[match.end():]
        cues.append(
            RestatementCue(
                index=int(lines[0].strip()),
                start_seconds=_seconds(lines[1].split("-->")[0]),
                label=label,
                text=text,
            )
        )
    return cues


def classify(pair, truth_by_cue) -> str:
    truth = truth_by_cue.get(pair.early_index)
    if truth is None:
        return "NO_TRUTH"
    if not truth["text_changed"]:
        return "benign(no text error)"
    truth_text = "".join(seg["text"] for seg in truth["truth_segments"])
    scored_truth = score_pair(truth_text, pair.late_text)
    scored_machine = score_pair(truth["machine_text"], pair.late_text)
    if scored_truth and scored_machine and scored_truth[0] > scored_machine[0]:
        return f"REPAIR_AGREES(truth-sim {scored_truth[0]:.2f} > machine {scored_machine[0]:.2f})"
    return "text error, repair direction unclear"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pristine-root",
        type=Path,
        default=Path.home()
        / "Project/vtuber-slice-forensics/2026-08-07-pristine/out/2026-08-07",
    )
    parser.add_argument(
        "--truth-root",
        type=Path,
        default=Path("reports/ivan_truth_harvest/2026-08-07"),
    )
    args = parser.parse_args()

    specs = [
        ("auto_203735_555_680", "recut.speaker-final.srt"),
        ("auto_220747_488_680", "recut.speaker-final.srt"),
        ("auto_223750_913_1322", "recut.srt"),
        ("auto_200736_298_383", "recut.speaker-final.srt"),
        ("auto_210739_1142_1436", "recut.speaker-final.srt"),
    ]
    all_rows = []
    for cid, suffix in specs:
        srt = args.pristine_root / cid / "replacement_recuts" / f"{cid}.{suffix}"
        truth = json.loads((args.truth_root / f"{cid}.truth-diff.v2.json").read_text("utf-8"))
        truth_by_cue = {c["cue"]: c for c in truth["cues"]}
        cues = load_cues(srt)
        pairs = find_restatement_pairs(
            cues, min_similarity=0.30, min_prefix_run=1, max_gap_seconds=120.0
        )
        print(f"\n== {cid} ({len(cues)} cues, wide-open pairs: {len(pairs)}) ==")
        for pair in pairs:
            verdict = classify(pair, truth_by_cue)
            all_rows.append((pair, verdict))
            print(
                f"  cue{pair.early_index}->{pair.late_index} sim={pair.similarity:.2f} "
                f"prefix={pair.prefix_run} gap={pair.gap_seconds:.0f}s [{verdict}]\n"
                f"    early: {pair.early_text!r}\n    late : {pair.late_text!r}"
            )

    print("\n== threshold grid (pairs kept / repair-agrees kept) ==")
    for min_sim in (0.35, 0.40, 0.45, 0.50, 0.55):
        for min_prefix in (1, 2, 3):
            kept = [
                (p, v)
                for p, v in all_rows
                if p.similarity >= min_sim and p.prefix_run >= min_prefix
            ]
            agree = sum(1 for _, v in kept if v.startswith("REPAIR_AGREES"))
            print(
                f"  sim>={min_sim:.2f} prefix>={min_prefix}: kept={len(kept)} repair_agrees={agree}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
