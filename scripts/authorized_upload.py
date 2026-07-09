#!/usr/bin/env python3
"""Manifest-bound manual upload channel (2026-07-09 external audit).

Problem: `do_upload.sh <video> <cover> <title>` took free-form arguments — no
proof that the file Ivan REVIEWED is the file that got UPLOADED, no idempotency
(the 充电器 clip was double-posted once), authorization lived only in chat.

This tool binds the whole chain to one manifest file:

    1. make-manifest   — at review time: records sha256 of the exact video and
                         cover plus the verbatim title and Ivan's authorization
                         quote.  The manifest IS the reviewed artifact's identity.
    2. upload          — at upload time: RECOMPUTES the hashes; any drift since
                         review refuses loudly.  A ledger (jsonl, pulled to the
                         Mac with the other reports) makes re-uploading the same
                         video a hard error.  The uploader command receives the
                         manifest's paths/title — never hand-typed ones — and
                         runs with AUTHORIZED_UPLOAD=1 (do_upload.sh refuses to
                         run without it).
    3. verify          — hash re-check only (pre-flight).

Upload authorization remains per-clip and human (Ivan): this tool cannot invent
an authorization, it only makes the authorized artifact tamper-evident.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
DEFAULT_LEDGER = DEFAULT_BASE / "reports" / "upload_ledger.jsonl"
DEFAULT_UPLOADER = "/opt/bilive/app/tmp_manual_upload/do_upload.sh"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def make_manifest(args: argparse.Namespace) -> int:
    video, cover = Path(args.video), Path(args.cover)
    for path in (video, cover):
        if not path.is_file():
            print(f"REFUSE: missing artifact {path}", file=sys.stderr)
            return 2
    if not args.title.strip() or not args.quote.strip():
        print("REFUSE: --title and --quote (Ivan's authorization words) are required non-empty", file=sys.stderr)
        return 2
    video_sha = sha256_file(video)
    manifest = {
        "manifest_version": 1,
        "artifact_id": video_sha[:12],
        "video": {"path": str(video.resolve()), "sha256": video_sha, "bytes": video.stat().st_size},
        "cover": {"path": str(cover.resolve()), "sha256": sha256_file(cover), "bytes": cover.stat().st_size},
        "title": args.title,
        "authorization": {"by": args.authorized_by, "quote": args.quote, "at": now()},
        "created_at": now(),
    }
    out = Path(args.out) if args.out else video.with_suffix(".upload_manifest.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(out), "artifact_id": manifest["artifact_id"]}, ensure_ascii=False))
    return 0


def load_and_verify(manifest_path: Path) -> tuple[dict | None, list[str]]:
    """(manifest, problems) — problems non-empty means REFUSE."""
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"manifest unreadable: {exc}"]
    auth = manifest.get("authorization") or {}
    if not str(auth.get("quote") or "").strip() or not str(auth.get("by") or "").strip():
        problems.append("manifest carries no authorization (by+quote required)")
    if not str(manifest.get("title") or "").strip():
        problems.append("manifest has no title")
    for kind in ("video", "cover"):
        entry = manifest.get(kind) or {}
        path = Path(entry.get("path") or "")
        if not path.is_file():
            problems.append(f"{kind} missing: {path}")
            continue
        actual = sha256_file(path)
        if actual != entry.get("sha256"):
            problems.append(
                f"{kind} HASH DRIFT since review: manifest={str(entry.get('sha256'))[:12]} actual={actual[:12]} ({path})"
            )
    return manifest, problems


def ledger_duplicate(ledger: Path, video_sha256: str) -> dict | None:
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                return entry
    except OSError:
        pass
    return None


def append_ledger(ledger: Path, entry: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(entry, ensure_ascii=False) + "\n")


def upload(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest, problems = load_and_verify(manifest_path)
    if problems:
        for p in problems:
            print(f"REFUSE: {p}", file=sys.stderr)
        return 2
    ledger = Path(args.ledger)
    video_sha = manifest["video"]["sha256"]
    dup = ledger_duplicate(ledger, video_sha)
    if dup:
        print(
            f"REFUSE: this exact video was already uploaded at {dup.get('at')}"
            f" (bvid={dup.get('bvid') or '?'}) — duplicate posts are the 充电器 incident; not repeating it",
            file=sys.stderr,
        )
        return 3
    cmd = [args.uploader, manifest["video"]["path"], manifest["cover"]["path"], manifest["title"]]
    env = os.environ.copy()
    env["AUTHORIZED_UPLOAD"] = "1"
    print(f"uploading artifact {manifest['artifact_id']} via {args.uploader}", flush=True)
    completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=3600, env=env)
    output = (completed.stdout or "") + (completed.stderr or "")
    sys.stdout.write(output)
    bvid = None
    for token in output.split():
        if token.startswith("BVID="):
            bvid = token.removeprefix("BVID=")
    append_ledger(
        ledger,
        {
            "at": now(),
            "artifact_id": manifest["artifact_id"],
            "video_sha256": video_sha,
            "cover_sha256": manifest["cover"]["sha256"],
            "title": manifest["title"],
            "authorized_by": manifest["authorization"]["by"],
            "authorization_quote": manifest["authorization"]["quote"],
            "manifest": str(manifest_path.resolve()),
            "uploader": args.uploader,
            "rc": completed.returncode,
            "bvid": bvid,
        },
    )
    print(f"ledger += artifact {manifest['artifact_id']} rc={completed.returncode} bvid={bvid or '?'}")
    return completed.returncode


def verify(args: argparse.Namespace) -> int:
    manifest, problems = load_and_verify(Path(args.manifest))
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 2
    print(f"OK: artifact {manifest['artifact_id']} matches its manifest (video+cover hashes, title, authorization present)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("make-manifest", help="freeze the reviewed artifact's identity + authorization")
    mk.add_argument("--video", required=True)
    mk.add_argument("--cover", required=True)
    mk.add_argument("--title", required=True)
    mk.add_argument("--authorized-by", default="Ivan")
    mk.add_argument("--quote", required=True, help="the verbatim authorization words")
    mk.add_argument("--out", default=None)
    mk.set_defaults(func=make_manifest)

    up = sub.add_parser("upload", help="verify hashes + ledger, then run the uploader with manifest args")
    up.add_argument("--manifest", required=True)
    up.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    up.add_argument("--uploader", default=DEFAULT_UPLOADER)
    up.set_defaults(func=upload)

    ve = sub.add_parser("verify", help="pre-flight hash/authorization check only")
    ve.add_argument("--manifest", required=True)
    ve.set_defaults(func=verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
