#!/usr/bin/env python3
"""Scan free's autoslice state for media references whose target is gone.

Dangling authority references are how the mis-delete and the
ENOSPC cleanup leave their mark: a capacity pass removes media on a
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.review_evidence_store import expand
from src.autoslice.batch_items import ItemBatch
from src.autoslice import evidence_lease
from src.autoslice.evidence_identity import same_sha256

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


def surviving_stem_index(base: str, *, issues: ItemBatch | None = None) -> dict[tuple[str, str], set[str]]:
    """candidate_id -> delivered stem, read out of each day dir's *.record.json.

    Deliverable filenames come from the recall hook, so hook/title surgery
    renames the file while state keeps the old name.  record.json's media_path
    still carries the candidate id, which is what lets a rename be told apart
    from a deletion.
    """
    index: dict[tuple[str, str], set[str]] = {}
    issues = issues if issues is not None else ItemBatch()
    for record in glob.glob(f"{base}/repo/lidousha/*/*.record.json"):
        doc = None
        with issues.isolate(record):
            with open(record, encoding="utf-8") as fh:
                doc = expand(json.load(fh))
            if not isinstance(doc, dict):
                doc = None
                raise ValueError("MEDIA_REFERENCE_RECORD_OBJECT_REQUIRED")
        if doc is None:
            continue
        day_dir = os.path.dirname(record)
        stem = os.path.basename(record)[: -len(".record.json")]
        path_fields = json.dumps(
            {k: v for k, v in doc.items() if "path" in k.lower()}
        )
        for cid in set(CANDIDATE_ID.findall(path_fields)) | set(CANDIDATE_ID.findall(stem)):
            index.setdefault((day_dir, cid), set()).add(stem)
    return index


def relocated_index(base: str) -> dict[tuple[str, str], str]:
    """(day dir, basename) -> where the file actually sits now.

    A day dir retires artifacts into `_superseded/` and `_quarantine/<tag>/`
    rather than deleting them, and pre-record.json days (and older)
    have no sidecar to match a candidate id against.  Matching on basename
    catches both: same filename, different directory means moved, not lost.
    """
    index: dict[tuple[str, str], str] = {}
    for retired in glob.glob(f"{base}/repo/lidousha/*/_*/**/*", recursive=True):
        if not os.path.isfile(retired):
            continue
        parts = retired.split(os.sep)
        try:
            day_dir = os.sep.join(parts[: parts.index("lidousha") + 2])
        except ValueError:
            continue
        index.setdefault((day_dir, os.path.basename(retired)), retired)
    return index


def row_for(doc, path):
    if len(path) >= 2 and path[0] in ROW_CONTAINERS:
        try:
            return doc[path[0]][int(path[1].strip("[]"))]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return None


def classify(base, index, retired, missing_path, row):
    cid = (row or {}).get("candidate_id")
    if not missing_path.startswith(base + "/"):
        return "C_foreign_workspace", None, (
            "referenced tree does not exist on this host; "
            "production state recorded an off-host workspace path"
        )
    if missing_path.startswith(base + "/repo/"):
        day_dir = os.path.dirname(missing_path)
        moved = retired.get((day_dir, os.path.basename(missing_path)))
        if moved:
            return "A_retired_in_place", moved, (
                "same filename retired into the day dir's _superseded/_quarantine subtree"
            )
        survivors = sorted(index.get((day_dir, cid), []))
        if survivors:
            return "A_rename_drift", survivors[0], (
                "deliverable survives under a renamed stem (hook/title surgery)"
            )
        return "A_repo_unresolved", None, (
            "no surviving stem in the day dir carries this candidate_id; "
            "usually a boundary re-cut or merge under a new candidate id — "
            "confirm before calling it a loss"
        )
    return "B_cleanup_deleted", None, "out/ intermediate removed by a capacity cleanup"


STAGE_SCHEMAS = {f"same-bv-stage-{kind}.v1" for kind in ("request", "plan", "completed")}


def structured_files(root: Path):
    """Only the explicitly selected runtime/administrative tree, without links."""
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".git", "evidence-leases"}
                   and not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and (path.suffix.lower() == ".json" or name == "COMMANDS.md"):
                yield path


command_declarations = evidence_lease.command_declarations
input_bindings = evidence_lease.input_bindings
stage_bindings = evidence_lease.stage_bindings


def unleased_bindings(base: Path, *, stage_roots=(), now=None) -> list[dict]:
    """Report missing/expired/hash-mismatched leases; do not issue or renew them."""
    leases = evidence_lease.read_leases(base, now=now)
    private = base / "private-runs"
    sources = set(structured_files(private))
    for root in (base / "state", *stage_roots):
        sources.update(structured_files(Path(root)))
    rows = []
    for source in sorted(sources):
        is_private = private in source.parents
        if source.name == "COMMANDS.md":
            if not is_private:
                continue
            documents = list(command_declarations(source.read_text(encoding="utf-8")))
        else:
            document = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                continue
            is_stage = document.get("schema_version", document.get("schema")) in STAGE_SCHEMAS
            if not is_stage and not (is_private and source.name == "RESULT.json"):
                continue
            documents = [document]
        hits = set()
        for document in documents:
            stage = isinstance(document, dict) and document.get("schema_version", document.get("schema")) in STAGE_SCHEMAS
            hits.update(stage_bindings(document) if stage else input_bindings(document))
        for location, value, sha256 in sorted(hits, key=lambda hit: (hit[0], hit[1], str(hit[2]))):
            path = Path(value)
            path = (path if path.is_absolute() else source.parent / path).resolve()
            covering = [lease for lease in leases.values() if evidence_lease.covers(lease, path)]
            if any(not sha256 or same_sha256(lease["sha256"], sha256) or Path(lease["path"]).resolve() != path
                   for lease in covering):
                continue
            rows.append({"binding_file": str(source), "json_path": ".".join(location),
                         "bound_path": str(path), "sha256": sha256, "exists": path.exists(),
                         "reason": "sha256-mismatch" if covering else "no-active-lease"})
    return rows




def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="/opt/bilive/autoslice")
    parser.add_argument("--json", help="write the full classified row list here")
    parser.add_argument(
        "--include-backups", action="store_true",
        help="also scan state/*.pre-*.json snapshots (excluded by default)",
    )
    parser.add_argument('--unleased-bindings', action='store_true')
    parser.add_argument('--stage-root', type=Path, action='append', default=[])
    parser.add_argument('--issue-source', type=Path, action='append', default=[],
                        help='selected live SPEC/structured input declaration (repeatable)')
    parser.add_argument('--lease-base', type=Path, help='lease output base, defaults to --base')
    parser.add_argument('--holder')
    parser.add_argument('--purpose')
    parser.add_argument('--until-utc')
    args = parser.parse_args(argv)
    if args.issue_source:
        if not all((args.holder, args.purpose, args.until_utc)):
            parser.error('issuing requires --holder, --purpose and --until-utc')
        leases = evidence_lease.issue_from_sources(Path(args.base), args.issue_source,
                    lease_base=args.lease_base, holder=args.holder, purpose=args.purpose,
                    until_utc=args.until_utc)
        print(json.dumps({'issued': len(leases), 'lease_base': str(args.lease_base or args.base)}))
        return 0
    if args.unleased_bindings:
        rows = unleased_bindings(Path(args.base).resolve(), stage_roots=args.stage_root)
        print(f"unleased structured bindings: {len(rows)}")
        for row in rows:
            print(json.dumps(row, ensure_ascii=False))
        if args.json:
            Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
        return 0

    base = args.base.rstrip("/")
    issues = ItemBatch()
    index = surviving_stem_index(base, issues=issues)
    retired = relocated_index(base)
    rows = []
    for state_file in sorted(glob.glob(f"{base}/state/*.json")):
        name = os.path.basename(state_file)
        if ".pre-" in name and not args.include_backups:
            continue
        doc = None
        with issues.isolate(state_file):
            with open(state_file, encoding="utf-8") as fh:
                doc = expand(json.load(fh))
            if not isinstance(doc, dict):
                doc = None
                raise ValueError("MEDIA_REFERENCE_STATE_OBJECT_REQUIRED")
        if doc is None:
            continue
        hits: list = []
        walk(doc, [], hits)
        for path, value in hits:
            if os.path.exists(value):
                continue
            row = row_for(doc, path) or {}
            cls, survivor, resolution = classify(base, index, retired, value, row)
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

    for failure in issues.as_list():
        print(json.dumps({"event": "BATCH_ITEM_ISOLATED", **failure}, ensure_ascii=False), file=sys.stderr)

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
