#!/usr/bin/env python3
"""Plan a free capacity cleanup. Never deletes — emits a gated plan.

Ivan approved this gate on 2026-08-09 after the 2026-07-30 ENOSPC pass deleted
`out/2026-07-18/*/song_selector_full/**/*.mp4` without scanning the ledger and
left a dangling authority reference in state (the 2026-07-24 mis-delete again,
one layer up). Nothing gets deleted from `out/` until it clears all four gates.

    GATE 0  quiet window   — DISABLED present, no runner/ffmpeg/produce process,
                             runner.lock free. Live production is an abort, not
                             a warning: the runner writes into out/<today>.
    GATE 1  class allowlist— only classes with committed precedent. Anything
                             unclassified is KEPT. `.recut.mp4` and
                             `.burned-final-*` are never in the allowlist: that
                             is exactly what 07-30 destroyed.
    GATE 2  authority cite — no Tier A file may reference the path. `out/` is
                             not Tier A (an intermediate cited only by its own
                             sibling manifest is self-referential), and a cited
                             *directory* protects only when specific enough to
                             name a candidate — `out/<day>` alone is a location
                             mention that would otherwise shield a whole day.
    GATE 3  owner terminal — the owning candidate must be in a terminal state,
                             and must not sit in pending_talk / pending_song.

A per-day CloudFS presence check (regenerability) is reported but must be
confirmed by the operator: a hung FUSE mount can make present sources look
absent, and that misread would authorize deleting the irreplaceable.

    python3 scripts/cleanup_preflight_scan.py [--base ...] [--json plan.json]
                                              [--allow-source-extractions]
                                              [--ignore-quiet-window]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import subprocess

TEXT_EXT = {".json", ".jsonl", ".md", ".txt", ".srt", ".ass", ".log",
            ".py", ".sh", ".lrc", ".yaml", ".yml", ".csv"}
MEDIA_EXT = {".mp4", ".m4s", ".mp3", ".wav", ".flac", ".m4a", ".mkv", ".ts", ".aac"}

# Tier A authority. `out/` is deliberately absent — see GATE 2.
TIER_A = ["state", "assets", "profiles", "src", "scripts", "docs", "cache",
          "forensics", "evals", "review_jobs", "review_packages", "upload_staging",
          "manual_review", "recovery", "quarantine", "repo", "reports"]

# Classes with committed precedent (cleanup_manifests/free_autoslice_*_2026073{0}
# and _20260810). Regenerable from retained source recordings.
ALLOWED_CLASSES = {
    "source-context.context.mp4",
    "padded_N.mp4",
    "piece_N.mp4",
    "input.complete-audio.mp3",
    "working.wav",
}
# Defensible extension of the same regenerability logic, but a NEW class:
# opt in explicitly and the manifest must say so.
SOURCE_EXTRACTION_CLASSES = {"_source.mp4", "_full_source.mp4"}

TERMINAL_STATES = {"published", "candidate_rejected", "failed", "superseded",
                   "blocked", "boundary_unrepairable", "delivery_quarantined"}

CANDIDATE_ID = re.compile(r"(auto_\d+_\d+_\d+|song_\d+_\d+|seededsong_\d+_\d+)")
OUT_PATH = re.compile(r"/opt/bilive/autoslice/out/[^\"'\s,\]\}<>()]+")


def classify(name: str) -> str:
    for suffix in ("source-context.context.mp4", "input.complete-audio.mp3",
                   "_full_source.mp4", "_source.mp4", ".burned-final-sapphire72.mp4",
                   ".burned-final-speaker.mp4", ".recut.mp4"):
        if name.endswith(suffix):
            return suffix
    if re.match(r"^padded_\d+\.mp4$", name):
        return "padded_N.mp4"
    if re.match(r"^piece_\d+\.mp4$", name):
        return "piece_N.mp4"
    if name.endswith(".wav"):
        return "working.wav"
    return "(unclassified)" + os.path.splitext(name)[1].lower()


def quiet_window(base: str) -> list[str]:
    """Reasons the host is NOT quiet. Empty list means safe to proceed."""
    problems = []
    if not os.path.exists(f"{base}/DISABLED"):
        problems.append("DISABLED flag absent — the cron runner can start a tick at any moment")
    ps = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    busy = [ln for ln in ps.splitlines()
            if re.search(r"free_session_autoslice|produce_slice_package|ffmpeg", ln)
            and "grep" not in ln and "cleanup_preflight" not in ln]
    if busy:
        problems.append(f"{len(busy)} in-flight production process(es): {busy[0].strip()[:110]}")
    lock = f"{base}/runner.lock"
    if os.path.exists(lock):
        held = subprocess.run(["fuser", lock], capture_output=True, text=True)
        if held.stdout.strip():
            problems.append(f"runner.lock is held by pid(s){held.stdout.strip()}")
    return problems


def authority_references(base: str) -> tuple[set[str], set[str]]:
    """(exact paths, specific directories) cited by Tier A sources."""
    refs: set[str] = set()
    for top in TIER_A:
        root0 = os.path.join(base, top)
        if not os.path.isdir(root0):
            continue
        for root, _, files in os.walk(root0):
            for name in files:
                if os.path.splitext(name)[1].lower() not in TEXT_EXT:
                    continue
                path = os.path.join(root, name)
                try:
                    if os.path.getsize(path) > 20 * 2**20:
                        continue
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for hit in OUT_PATH.findall(text):
                    refs.add(hit.rstrip(".,;:/"))
    out_root = f"{base}/out"
    dirs = {r for r in refs
            if not os.path.splitext(r)[1]
            and len(os.path.relpath(r, out_root).split(os.sep)) >= 3}
    return refs, dirs


def candidate_states(base: str) -> tuple[dict[str, str], set[str]]:
    """candidate_id -> status, plus the set of ids with queued work."""
    status: dict[str, str] = {}
    pending: set[str] = set()
    for state_file in glob.glob(f"{base}/state/2026-*.json"):
        if ".pre-" in os.path.basename(state_file):
            continue
        try:
            with open(state_file, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        for key in ("picks", "songs", "talk_superseded_attempts",
                    "song_superseded_attempts", "operator_superseded_picks",
                    "operator_quarantined_picks"):
            for row in doc.get(key) or []:
                if isinstance(row, dict) and row.get("candidate_id"):
                    status.setdefault(row["candidate_id"], row.get("status") or "unknown")
        for key in ("pending_talk", "pending_song"):
            for row in doc.get(key) or []:
                if isinstance(row, dict):
                    for field in ("cid", "candidate_id"):
                        if row.get(field):
                            pending.add(row[field])
    return status, pending


def owning_ids(path: str) -> list[str]:
    return CANDIDATE_ID.findall(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--json", help="write the gated plan here")
    parser.add_argument("--allow-source-extractions", action="store_true",
                        help="also plan _source.mp4/_full_source.mp4 (new class — disclose it)")
    parser.add_argument("--ignore-quiet-window", action="store_true",
                        help="plan anyway while production is live; NEVER pass this before deleting")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    out_root = f"{base}/out"

    problems = quiet_window(base)
    print("GATE 0 quiet window:", "CLEAR" if not problems else "BLOCKED")
    for problem in problems:
        print(f"   ! {problem}")
    if problems and not args.ignore_quiet_window:
        print("\nRefusing to plan against a live host. Re-run in a quiet window, "
              "or pass --ignore-quiet-window to size the surface only (never to delete).")
        return 2

    allowed = set(ALLOWED_CLASSES)
    if args.allow_source_extractions:
        allowed |= SOURCE_EXTRACTION_CLASSES

    refs, refdirs = authority_references(base)
    status, pending = candidate_states(base)
    print(f"GATE 2 authority: {len(refs)} out/ paths cited, {len(refdirs)} specific dirs")
    print(f"GATE 3 states: {len(status)} candidates known, {len(pending)} with queued work")

    plan, held = [], collections.Counter()
    for root, _, files in os.walk(out_root):
        for name in files:
            if os.path.splitext(name)[1].lower() not in MEDIA_EXT:
                continue
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            day = os.path.relpath(path, out_root).split(os.sep)[0]
            klass = classify(name)
            if klass not in allowed:
                held["gate1_class_not_allowlisted"] += size
                continue
            if path in refs or any(path.startswith(d + "/") for d in refdirs):
                held["gate2_authority_cited"] += size
                continue
            ids = owning_ids(path)
            if any(i in pending for i in ids):
                held["gate3_owner_has_queued_work"] += size
                continue
            live = [i for i in ids if status.get(i, "unknown") not in TERMINAL_STATES]
            if live or not ids:
                held["gate3_owner_not_terminal"] += size
                continue
            plan.append({"path": path, "bytes": size, "day": day, "cls": klass,
                         "owner": ids[0], "owner_status": status.get(ids[0])})

    print("\nHELD BACK:")
    for reason, size in held.most_common():
        print(f"   {size / 2**30:8.2f} GiB  {reason}")
    print(f"\nPLANNED FOR DELETION: {len(plan)} files, "
          f"{sum(p['bytes'] for p in plan) / 2**30:.2f} GiB")
    by_day = collections.Counter()
    for item in plan:
        by_day[item["day"]] += item["bytes"]
    for day, size in sorted(by_day.items()):
        print(f"   {day:13} {size / 2**30:8.2f} GiB")
        source = (f"/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/"
                  f"22966160/{day}")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            print(f"       CloudFS source dir present: {os.path.isdir(source)} "
                  "(confirm the mount is healthy before trusting a False)")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")
    print("\nThis tool never deletes. Feed the plan to a reviewed delete step, "
          "and re-run scan_state_dangling_media_refs.py afterwards: the count of "
          "dangling references must not increase.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
