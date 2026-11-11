#!/usr/bin/env python3
"""Finished-media review lane CLI (see docs/pipeline/85-finished-media-review.md).

init      member manifest -> package directory (files hard-linked, bytes unchanged)
check     run the automatic checks, write evidence/, nominations and the record
attest    bind REVIEW.md (the reviewer's free-form report), verdict and reviewer id into the
          hash-bound receipt; the reviewer must differ from the maker
status    print findings.md
seal      PASS receipt -> review_manifest.json + package audit JSON
          (check and seal take --repair-authority when the package replaces an already published BV)

This lane is only the upload-compatibility interface; how to review is not specified here.
No subcommand ever edits subtitles or media; findings go back to the maker.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.finished_media_review import checks, member as member_mod, package, receipt
from src.autoslice.finished_media_review.common import FmrError, load_json


def _context(root: Path, adapters: checks.Adapters) -> checks.Context:
    candidates = sorted(Path(root).glob("*.member.json"))
    if len(candidates) != 1:
        raise FmrError("FMR_PACKAGE_MEMBER_COPY_NOT_UNIQUE")
    member = member_mod.load_member(candidates[0])
    return checks.Context(root=Path(root).resolve(), cid=member_mod.member_candidate_id(member),
                          member=member, adapters=adapters)


def _recorded_adapters(args) -> checks.Adapters:
    from src.autoslice.final_subtitle_audio_gate import (
        FinalSubtitleAudioGateAdapters,
        _default_extract_audio,
    )
    from scripts import free_asr_client

    adapters = checks.Adapters(use_jianying=not args.no_jianying)
    if args.bcut_recorded:
        raw = json.loads(Path(args.bcut_recorded).read_text(encoding="utf-8"))
        adapters.gate = FinalSubtitleAudioGateAdapters(
            extract_audio=_default_extract_audio,
            transcribe_bcut=lambda _audio: raw,
            to_srt=free_asr_client.to_srt,
        )
    if args.jianying_recorded:
        recorded = json.loads(Path(args.jianying_recorded).read_text(encoding="utf-8"))
        adapters.jianying = lambda _audio: recorded
    return adapters


def _write_findings(root: Path, lines: list[str]) -> None:
    body = "# 审查发现（交回制作方修改）\n\n" + ("\n".join(f"- {line}" for line in lines) if lines else "无") + "\n"
    (Path(root) / "findings.md").write_text(body, encoding="utf-8")


def _repair_authority(args, root: Path, cid: str) -> dict | None:
    """Validated same-BV repair authority from ``--repair-authority`` (``None`` when not given)."""
    if not getattr(args, "repair_authority", None):
        return None
    title_file = Path(root) / f"{cid}.title.txt"
    title = title_file.read_text(encoding="utf-8").strip() if title_file.is_file() else ""
    return package.load_repair_authority(
        args.repair_authority, cid=cid, title=title,
        replay_evidence=not getattr(args, "authority_shape_only", False))


def _title_receipt(args, root: Path, cid: str, authority: dict | None) -> dict[str, str] | None:
    raw = getattr(args, "title_receipt", None)
    if not raw:
        return None
    if authority is None:
        raise FmrError("FMR_TITLE_RECEIPT_REQUIRES_REPAIR_AUTHORITY: pass --repair-authority")
    title = (root / f"{cid}.title.txt").read_text(encoding="utf-8").strip()
    return package.load_title_receipt_reference(raw, authority=authority, cid=cid, title=title)


def cmd_init(args) -> int:
    try:
        root = member_mod.init_package(args.member, args.out_root)
    except FmrError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    print(root)
    return 0


def cmd_check(args) -> int:
    try:
        ctx = _context(Path(args.package), _recorded_adapters(args))
        authority = _repair_authority(args, ctx.root, ctx.cid)
        title_receipt = _title_receipt(args, ctx.root, ctx.cid, authority)
        auto = checks.run_checks(ctx, tags_from=Path(args.tags_from) if args.tags_from else None)
    except FmrError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    record = package.write_record(ctx, repair_authority=authority, title_receipt=title_receipt)
    _, findings = receipt.auto_problems_and_findings(auto)
    if record is None:
        findings.append("RECORD_NOT_WRITTEN: a field-producing check failed")
    _write_findings(ctx.root, findings)
    for row in auto["checks"]:
        print(f"{row['id']:4} {row['status']:18} {','.join(row['reason_codes'])}")
    return 1 if findings else 0


def cmd_attest(args) -> int:
    root = Path(args.package).resolve()
    try:
        ctx = _context(root, checks.Adapters())
        doc = receipt.build_receipt(
            root, ctx.cid, ctx.member, review=Path(args.review), reviewer_id=args.reviewer_id,
            verdict=args.verdict, reviewer_kind=args.reviewer_kind)
    except FmrError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    path = receipt.write_receipt(root, ctx.cid, doc)
    print(f"{doc['verdict']} {path}")
    return 0 if doc["verdict"] == "PASS" else 1


def cmd_status(args) -> int:
    root = Path(args.package)
    findings = (root / "findings.md")
    print(findings.read_text(encoding="utf-8") if findings.is_file() else "no findings.md yet")
    return 0


def cmd_seal(args) -> int:
    from src.autoslice.finished_media_review.audit import package_problems

    root = Path(args.package).resolve()
    try:
        ctx = _context(root, checks.Adapters())
        if load_json(member_mod.receipt_path(root, ctx.cid)).get("verdict") != "PASS":
            print("REFUSE: only a PASS receipt can be sealed", file=sys.stderr)
            return 1
        authority = _repair_authority(args, root, ctx.cid)
        current_record = load_json(member_mod.record_path(root, ctx.cid))
        record_authority = current_record.get("recovery_publication_authority")
        if authority is None and record_authority is not None:
            raise FmrError("FMR_REPAIR_AUTHORITY_MISSING: the record carries a repair authority; "
                           "pass the same --repair-authority to seal")
        if authority is not None and record_authority != dict(authority):
            raise FmrError("FMR_REPAIR_AUTHORITY_RECORD_MISMATCH: pass the same --repair-authority to seal")
        record_title_receipt = current_record.get("verified_title_repair")
        title_receipt = _title_receipt(args, root, ctx.cid, authority)
        if record_title_receipt is not None:
            if title_receipt is None:
                raise FmrError("FMR_TITLE_RECEIPT_MISSING: pass the same --title-receipt to seal")
            if record_title_receipt != {"receipt": title_receipt}:
                raise FmrError("FMR_TITLE_RECEIPT_RECORD_MISMATCH: pass the same --title-receipt to seal")
        elif title_receipt is not None:
            raise FmrError("FMR_TITLE_RECEIPT_RECORD_MISMATCH: record has no title receipt; re-run check")
        package.write_review_manifest(root, ctx.cid, ctx.member, repair_authority=authority)
    except FmrError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    issues = package_problems(root)
    audit_path = root / "package_audit.json"
    if issues:
        for issue in issues:
            print(f"BLOCK {issue['code']} {issue.get('detail', '')}", file=sys.stderr)
        return 1
    print(f"package accepted by the finished-media lane; now run: python scripts/audit_review_package.py {root} --json > {audit_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("--member", required=True); p.add_argument("--out-root", required=True)
    p.set_defaults(func=cmd_init)
    p = sub.add_parser("check"); p.add_argument("--package", required=True)
    p.add_argument("--tags-from"); p.add_argument("--no-jianying", action="store_true")
    p.add_argument("--bcut-recorded"); p.add_argument("--jianying-recorded")
    p.add_argument("--repair-authority", help="same-BV repair authority JSON (replacing an already published BV)")
    p.add_argument("--title-receipt", help="verified FMR title-repair receipt JSON")
    p.add_argument("--authority-shape-only", action="store_true",
                   help="do not replay the first-publication evidence files (not visible on this host); "
                        "make-manifest/verify/repair-plan still replay them")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("attest"); p.add_argument("--package", required=True); p.add_argument("--review", required=True)
    p.add_argument("--reviewer-id", required=True); p.add_argument("--verdict", required=True, choices=["PASS", "FAIL"])
    p.add_argument("--reviewer-kind", default="AGENT", choices=["AGENT", "维护者"])
    p.set_defaults(func=cmd_attest)
    p = sub.add_parser("status"); p.add_argument("--package", required=True)
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("seal"); p.add_argument("--package", required=True)
    p.add_argument("--repair-authority", help="the same file given to `check`")
    p.add_argument("--title-receipt", help="the same file given to `check`")
    p.add_argument("--authority-shape-only", action="store_true", help="as for `check`")
    p.set_defaults(func=cmd_seal)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
