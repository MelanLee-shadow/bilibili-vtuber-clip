"""The command line step 10 calls (scripts/lane_publish.py); the subcommands and return codes are in the package docstring."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, first, manifest, metadata, repair
from .common import OK, Refused


def _parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="lane_publish", description=__doc__)
    sub = top.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit")
    a.add_argument("package", type=Path)
    a.add_argument("--json", action="store_true")
    m = sub.add_parser("make-manifest")
    for name in ("--video", "--cover", "--package-audit", "--out"):
        m.add_argument(name, type=Path, required=True)
    m.add_argument("--title", required=True)
    m.add_argument("--authorized-by", required=True)
    m.add_argument("--quote", required=True)
    kind = m.add_mutually_exclusive_group(required=True)
    kind.add_argument("--tags")
    kind.add_argument("--final-human-review", type=Path)
    for name in ("verify", "upload"):
        sub.add_parser(name).add_argument("--manifest", type=Path, required=True)
    for prefix in ("", "cover-"):
        p = sub.add_parser(f"{prefix}repair-plan")
        for name in ("--manifest", "--out", "--journal"):
            p.add_argument(name, type=Path, required=True)
        p.add_argument("--bvid", required=True)
        p.add_argument("--preserve-existing-tags", action="store_true")
        p.add_argument("--predecessor-completed", type=Path)
        p.add_argument("--dry-run", action="store_true")
        for name, extra in (("repair-status", ()), ("repair-run", ("--dry-run",)), ("repair-verify-live", ("--out",))):
            q = sub.add_parser(f"{prefix}{name}")
            q.add_argument("--plan", type=Path, required=True)
            q.add_argument("--journal", type=Path, required=True)
            for flag in extra:
                q.add_argument(flag, action="store_true") if flag == "--dry-run" else q.add_argument(flag, type=Path, required=True)
    e = sub.add_parser("metadata-plan", help="only the title, tags and description of a published BV (metadata.py)")
    which = e.add_mutually_exclusive_group(required=True)
    which.add_argument("--bvid")
    which.add_argument("--cid", help="the clip or its publication id, as the registry row names it")
    e.add_argument("--title")
    e.add_argument("--tags", help="the whole new tag list, comma-separated")
    desc = e.add_mutually_exclusive_group()
    desc.add_argument("--desc-file", type=Path, help="the whole new description, as the page is to show it")
    desc.add_argument("--fix-source-line", action="store_true", help="leave one of the repeated leading source lines")
    for name in ("--authorized-by", "--quote"):
        e.add_argument(name, required=True)
    for name in ("--out", "--journal"):
        e.add_argument(name, type=Path, required=True)
    e.add_argument("--dry-run", action="store_true")
    for name, extra in (("metadata-status", ()), ("metadata-run", ("--dry-run",)), ("metadata-verify-live", ("--out",))):
        q = sub.add_parser(name)
        q.add_argument("--plan", type=Path, required=True)
        q.add_argument("--journal", type=Path, required=True)
        for flag in extra:
            q.add_argument(flag, action="store_true") if flag == "--dry-run" else q.add_argument(flag, type=Path, required=True)
    return top


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command: str = args.command
    kind = repair.COVER if command.startswith("cover-") else repair.VIDEO
    name = command.removeprefix("cover-")
    try:
        if command == "audit":
            result = audit.audit(args.package)
            _print(result)
            return OK if result["passed"] else 1
        if command == "make-manifest":
            doc = manifest.make(video=args.video, cover=args.cover, package_audit=args.package_audit, title=args.title,
                                tags=args.tags, final_human_review=args.final_human_review,
                                authorized_by=args.authorized_by, quote=args.quote, out=args.out)
            print(f"manifest {args.out}: {doc['candidate_id']} -> {doc['season']['season_title']}")
        elif command == "verify":
            manifest.load_checked(args.manifest)
            print(f"verified {args.manifest}")
        elif command == "metadata-plan":
            _print(metadata.plan(bvid=args.bvid, cid=args.cid, title=args.title, tags=args.tags, desc_file=args.desc_file,
                                 fix_source_line=args.fix_source_line, authorized_by=args.authorized_by, quote=args.quote,
                                 out=args.out, journal=args.journal, dry_run=args.dry_run))
        elif command == "metadata-status":
            rc = metadata.status(plan_path=args.plan, journal=args.journal)
            print("live" if rc == OK else "not live yet")
            return rc
        elif command == "metadata-run":
            rc = metadata.run(plan_path=args.plan, journal=args.journal, dry_run=args.dry_run)
            print(("would " if args.dry_run else "") + ("live" if rc == OK else "in progress"))
            return rc
        elif command == "metadata-verify-live":
            _print(metadata.verify_live(plan_path=args.plan, journal=args.journal, out=args.out))
        elif command == "upload":
            _print(first.upload(args.manifest))
        elif name == "repair-plan":
            _print(repair.plan(kind, manifest_path=args.manifest, bvid=args.bvid, out=args.out, journal=args.journal,
                               preserve_tags=args.preserve_existing_tags, predecessor=args.predecessor_completed,
                               dry_run=args.dry_run))
        elif name == "repair-status":
            rc = repair.status(plan_path=args.plan, journal=args.journal)
            print("live" if rc == OK else "not live yet")
            return rc
        elif name == "repair-run":
            rc = repair.run(plan_path=args.plan, journal=args.journal, dry_run=args.dry_run)
            print(("would " if args.dry_run else "") + ("live" if rc == OK else "in progress"))
            return rc
        elif name == "repair-verify-live":
            _print(repair.verify_live(plan_path=args.plan, journal=args.journal, out=args.out))
        return OK
    except Refused as refusal:
        print(refusal.words, file=sys.stderr)
        return refusal.rc
