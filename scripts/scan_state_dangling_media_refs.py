#!/usr/bin/env python3
"""Scan free's autoslice state for media references whose target is gone.

Dangling authority references are how the 2026-07-24 mis-delete and the
2026-07-30 ENOSPC cleanup leave their mark: a capacity pass removes media on a
"regenerable from source" basis without scanning state, and a day file keeps
pointing at bytes that no longer exist.  This walks every string leaf that
looks like an absolute media path and reports the misses, classified by why
they are missing.

Read-only.  Takes no lock and touches nothing.

    python3 scripts/scan_state_dangling_media_refs.py [--base /opt/bilive/autoslice]
                                                      [--json out.json]
                                                      [--include-backups]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

MEDIA_EXT = {
    ".mp4", ".m4s", ".mp3", ".wav", ".flac", ".m4a", ".mkv", ".ts", ".aac",
    ".png", ".jpg", ".jpeg", ".webp", ".ass", ".srt",
}
CANDIDATE_ID = re.compile(r"(auto_\d+_\d+_\d+|song_\d+_\d+|seededsong_\d+_\d+)")
ROW_CONTAINERS = (
    "picks", "songs", "talk_superseded_attempts",
    "operator_superseded_picks", "operator_quarantined_picks",
)


def walk(node, path, hits):
    if isinstance(node, dict):
        for key, value in node.items():
            walk(value, path + [str(key)], hits)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            walk(value, path + [f"[{i}]"], hits)
    elif isinstance(node, str):
        if node.startswith("/") and os.path.splitext(node)[1].lower() in MEDIA_EXT:
            hits.append((path[:], node))


def surviving_stem_index(base: str) -> dict[tuple[str, str], set[str]]:
    """candidate_id -> delivered stem, read out of each day dir's *.record.json.

    Deliverable filenames come from the recall hook, so hook/title surgery
    renames the file while state keeps the old name.  record.json's media_path
    still carries the candidate id, which is what lets a rename be told apart
    from a deletion.
    """
    index: dict[tuple[str, str], set[str]] = {}
    for record in glob.glob(f"{base}/repo/lidousha/*/*.record.json"):
        try:
            with open(record, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        day_dir = os.path.dirname(record)
        stem = os.path.basename(record)[: -len(".record.json")]
        path_fields = json.dumps(
            {k: v for k, v in doc.items() if "path" in k.lower()}
        )
        for cid in set(CANDIDATE_ID.findall(path_fields)) | set(CANDIDATE_ID.findall(stem)):
            index.setdefault((day_dir, cid), set()).add(stem)
    return index


def row_for(doc, path):
    if len(path) >= 2 and path[0] in ROW_CONTAINERS:
        try:
            return doc[path[0]][int(path[1].strip("[]"))]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return None


def classify(base, index, missing_path, row):
    cid = (row or {}).get("candidate_id")
    if not missing_path.startswith(base + "/"):
        return "C_foreign_workspace", None, (
            "referenced tree does not exist on this host; "
            "production state recorded an off-host workspace path"
        )
    if missing_path.startswith(base + "/repo/"):
        survivors = sorted(index.get((os.path.dirname(missing_path), cid), []))
        if survivors:
            return "A_rename_drift", survivors[0], (
                "deliverable survives under a renamed stem (hook/title surgery)"
            )
        return "A_repo_unresolved", None, (
            "no surviving stem in the day dir carries this candidate_id"
        )
    return "B_cleanup_deleted", None, "out/ intermediate removed by a capacity cleanup"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--json", help="write the full classified row list here")
    parser.add_argument(
        "--include-backups", action="store_true",
        help="also scan state/*.pre-*.json snapshots (excluded by default)",
    )
    args = parser.parse_args()

    base = args.base.rstrip("/")
    index = surviving_stem_index(base)
    rows = []
    for state_file in sorted(glob.glob(f"{base}/state/*.json")):
        name = os.path.basename(state_file)
        if ".pre-" in name and not args.include_backups:
            continue
        try:
            with open(state_file, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"PARSE-FAIL {name}: {exc}", file=sys.stderr)
            continue
        hits: list = []
        walk(doc, [], hits)
        for path, value in hits:
            if os.path.exists(value):
                continue
            row = row_for(doc, path) or {}
            cls, survivor, resolution = classify(base, index, value, row)
            entry = {
                "state_file": name,
                "json_path": ".".join(path),
                "missing_path": value,
                "candidate_id": row.get("candidate_id"),
                "row_status": row.get("status"),
                "cls": cls,
                "resolution": resolution,
            }
            if survivor:
                entry["surviving_stem"] = survivor
            rows.append(entry)

    print(f"dangling media references: {len(rows)}")
    print(f"state files affected: {len({r['state_file'] for r in rows})}")
    print("\nby class:")
    for cls, count in collections.Counter(r["cls"] for r in rows).most_common():
        print(f"  {count:5d}  {cls}")
    print("\nby state file:")
    for name, count in sorted(collections.Counter(r["state_file"] for r in rows).items()):
        print(f"  {count:5d}  {name}")
    print("\nby row status:")
    for status, count in collections.Counter(r["row_status"] for r in rows).most_common():
        print(f"  {count:5d}  {status}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
