"""Argument parser for the manifest-bound upload/repair entry point."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence


def parse_args(
    argv: Sequence[str] | None,
    *,
    description: str,
    handlers: Mapping[str, object],
    defaults: Mapping[str, object],
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    sub = parser.add_subparsers(dest="command", required=True)

    mk = sub.add_parser(
        "make-manifest",
        help="freeze the reviewed artifact's identity + authorization",
    )
    mk.add_argument("--video", required=True)
    mk.add_argument("--cover", required=True)
    mk.add_argument(
        "--package-audit",
        required=True,
        help=(
            "machine-readable passed audit JSON for the package containing "
            "this same-stem bundle"
        ),
    )
    mk.add_argument("--title", required=True)
    mk.add_argument("--authorized-by", default="维护者")
    mk.add_argument(
        "--quote",
        required=True,
        help="the verbatim authorization words",
    )
    mk.add_argument(
        "--final-human-review",
        default=None,
        help=(
            "hash-bound final perceptual-review receipt; required for "
            "same-BV recovery"
        ),
    )
    mk.add_argument(
        "--title-cover-qc",
        default=None,
        help=(
            "hash-bound CPA title+final-cover joint-QC receipt; required for "
            "every new-BV upload manifest"
        ),
    )
    mk.add_argument(
        "--tags",
        default="",
        help=(
            "optional comma-joined FULL tag line; when present it must exactly "
            "match the audited sibling record.json final_tags"
        ),
    )
    mk.add_argument(
        "--no-tags",
        action="store_true",
        help=(
            "legacy spelling retained for a loud v3 refusal; v3 never "
            "permits missing tags"
        ),
    )
    mk.add_argument(
        "--season",
        default="auto",
        choices=["auto", "talk", "song", "none"],
        help=(
            "season binding frozen into the manifest; auto derives from the "
            "title; none opts out explicitly"
        ),
    )
    mk.add_argument("--out", default=None)
    mk.set_defaults(func=handlers["make_manifest"])

    def season_args(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--cookie-json",
            default=str(defaults["cookie_json"]),
            help="bilibili login-API cookie file",
        )
        command.add_argument("--season-wait", type=float, default=900.0)
        command.add_argument("--season-poll", type=float, default=30.0)
        command.add_argument("--public-wait", type=float, default=240.0)

    up = sub.add_parser(
        "upload",
        help=(
            "verify hashes + ledger, run the uploader, then finish the season "
            "add (exit 6 = posted but season incomplete)"
        ),
    )
    up.add_argument("--manifest", required=True)
    up.add_argument("--ledger", default=str(defaults["ledger"]))
    up.add_argument("--lock", default=None)
    up.add_argument("--uploader", default=defaults["uploader"])
    up.add_argument("--skip-season", action="store_true")
    season_args(up)
    up.set_defaults(func=handlers["upload"])

    se = sub.add_parser(
        "season-add",
        help="idempotently finish/re-verify season membership",
    )
    se.add_argument("--manifest", required=True)
    se.add_argument("--bvid", default=None)
    se.add_argument("--ledger", default=str(defaults["ledger"]))
    season_args(se)
    se.set_defaults(func=handlers["season_add"])

    ve = sub.add_parser("verify", help="pre-flight hash/authorization check")
    ve.add_argument("--manifest", required=True)
    ve.set_defaults(func=handlers["verify"])

    rp = sub.add_parser(
        "repair-plan",
        help="freeze a manifest-bound existing-BV repair; no remote mutation",
    )
    rp.add_argument("--manifest", required=True)
    rp.add_argument("--bvid", required=True)
    rp.add_argument("--out", required=True)
    rp.add_argument("--journal", default=str(defaults["repair_ledger"]))
    rp.add_argument("--lock", default=None)
    rp.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    rp.add_argument(
        "--biliup-cookie-json",
        default=str(defaults["biliup_cookie_json"]),
    )
    rp.add_argument(
        "--predecessor-completed",
        default=None,
        help=(
            "explicit same-bv-repair-completed.v1 proof for a later repair "
            "whose current live CID no longer equals the original registry CID"
        ),
    )
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument(
        "--preserve-existing-tags",
        action="store_true",
        help=(
            "same-BV only: freeze an exactly matching, non-empty Creator/public "
            "live tag set as the sole metadata-preservation exception"
        ),
    )
    rp.set_defaults(func=handlers["repair_plan"])

    rr = sub.add_parser(
        "repair-run",
        help="resume an append-at-most-once same-BV repair transaction",
    )
    rr.add_argument("--plan", required=True)
    rr.add_argument("--journal", default=str(defaults["repair_ledger"]))
    rr.add_argument("--lock", default=None)
    rr.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    rr.add_argument(
        "--biliup-cookie-json",
        default=str(defaults["biliup_cookie_json"]),
    )
    rr.add_argument("--wait", type=float, default=900.0)
    rr.add_argument("--poll", type=float, default=15.0)
    rr.add_argument("--dry-run", action="store_true")
    rr.set_defaults(func=handlers["repair_run"])

    rc = sub.add_parser(
        "repair-reconcile-blocked",
        help=(
            "read-only proof and journal correction for the known Bilibili "
            "cover-CDN-alias false block"
        ),
    )
    rc.add_argument("--plan", required=True)
    rc.add_argument("--journal", default=str(defaults["repair_ledger"]))
    rc.add_argument("--lock", default=None)
    rc.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    rc.add_argument(
        "--biliup-cookie-json",
        default=str(defaults["biliup_cookie_json"]),
    )
    rc.add_argument("--dry-run", action="store_true")
    rc.set_defaults(func=handlers["repair_reconcile_blocked"])

    rs = sub.add_parser(
        "repair-status",
        help="validate the local same-BV repair state; no remote access",
    )
    rs.add_argument("--plan", required=True)
    rs.add_argument("--journal", default=str(defaults["repair_ledger"]))
    rs.set_defaults(func=handlers["repair_status"])

    rv = sub.add_parser(
        "repair-verify-live",
        help=(
            "freshly re-observe Creator/public/tags/exact-section and create "
            "a completed sidecar; no remote mutation"
        ),
    )
    rv.add_argument("--plan", required=True)
    rv.add_argument("--journal", default=str(defaults["repair_ledger"]))
    rv.add_argument("--out", required=True)
    rv.add_argument("--lock", default=None)
    rv.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    rv.add_argument(
        "--biliup-cookie-json",
        default=str(defaults["biliup_cookie_json"]),
    )
    rv.set_defaults(func=handlers["repair_verify_live"])

    cp = sub.add_parser(
        "cover-repair-plan",
        help=(
            "freeze a manifest/QC-bound same-BV cover-only edit; no remote "
            "mutation"
        ),
    )
    cp.add_argument("--manifest", required=True)
    cp.add_argument("--bvid", required=True)
    cp.add_argument("--out", required=True)
    cp.add_argument(
        "--journal", default=str(defaults["cover_repair_ledger"])
    )
    cp.add_argument("--lock", default=None)
    cp.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    cp.add_argument(
        "--predecessor-completed",
        default=None,
        help=(
            "prior same-bv-repair-completed.v1 proving the current CID after "
            "an earlier full replacement"
        ),
    )
    cp.add_argument("--dry-run", action="store_true")
    cp.set_defaults(func=handlers["cover_repair_plan"])

    cr = sub.add_parser(
        "cover-repair-run",
        help="resume one cover-only edit; EDIT_INTENT is never blindly retried",
    )
    cr.add_argument("--plan", required=True)
    cr.add_argument(
        "--journal", default=str(defaults["cover_repair_ledger"])
    )
    cr.add_argument("--lock", default=None)
    cr.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    cr.add_argument("--wait", type=float, default=240.0)
    cr.add_argument("--poll", type=float, default=10.0)
    cr.add_argument("--dry-run", action="store_true")
    cr.set_defaults(func=handlers["cover_repair_run"])

    cs = sub.add_parser(
        "cover-repair-status",
        help="validate local cover-only plan/journal state; no remote access",
    )
    cs.add_argument("--plan", required=True)
    cs.add_argument(
        "--journal", default=str(defaults["cover_repair_ledger"])
    )
    cs.set_defaults(func=handlers["cover_repair_status"])

    cv = sub.add_parser(
        "cover-repair-verify-live",
        help=(
            "freshly re-observe unchanged CID/metadata plus the new cover and "
            "create a completed receipt"
        ),
    )
    cv.add_argument("--plan", required=True)
    cv.add_argument(
        "--journal", default=str(defaults["cover_repair_ledger"])
    )
    cv.add_argument("--out", required=True)
    cv.add_argument("--lock", default=None)
    cv.add_argument("--cookie-json", default=str(defaults["cookie_json"]))
    cv.set_defaults(func=handlers["cover_repair_verify_live"])
    return parser.parse_args(argv)
