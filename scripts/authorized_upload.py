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
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
DEFAULT_LEDGER = DEFAULT_BASE / "reports" / "upload_ledger.jsonl"
DEFAULT_UPLOAD_LOCK = DEFAULT_BASE / "upload.lock"
DEFAULT_UPLOADER = "/opt/bilive/app/tmp_manual_upload/do_upload.sh"


class UploadLockBusy(RuntimeError):
    """Another upload/repair transaction owns the shared critical section."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Tag policy (Ivan 2026-07-13, see scripts/suggest_upload_tags.py + memory
# lidousha-upload-tags-policy): per-archive cap 12 (empirically probed via a
# 12-tag edit on BV1EQNk6KErE), per-tag <=20 chars, no separators, no dups.
MAX_TAGS = 12
MAX_TAG_CHARS = 20


def validate_tags(tags: list[str]) -> list[str]:
    """Return problems; empty list means the tag list is manifest-worthy."""
    problems: list[str] = []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        return ["tags must be a list of strings"]
    cleaned = [t.strip() for t in tags]
    if any(not t for t in cleaned):
        problems.append("tags contain an empty item")
    if len(cleaned) > MAX_TAGS:
        problems.append(f"{len(cleaned)} tags exceed the cap of {MAX_TAGS}")
    if len({t.casefold() for t in cleaned}) != len(cleaned):
        problems.append("tags contain duplicates")
    for tag in cleaned:
        if len(tag) > MAX_TAG_CHARS:
            problems.append(f"tag too long (>{MAX_TAG_CHARS} chars): {tag!r}")
        if any(ch in tag for ch in ",，\n\t"):
            problems.append(f"tag contains a separator character: {tag!r}")
    return problems


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
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    if tags:
        tag_problems = validate_tags(tags)
        if tag_problems:
            for problem in tag_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
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
    if tags:
        manifest["tags"] = tags
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
    if "tags" in manifest:
        problems.extend(validate_tags(manifest["tags"]))
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


def read_ledger(ledger: Path) -> tuple[list[dict], list[str]]:
    """Read the append-only ledger strictly enough for side-effect safety."""

    if not ledger.exists():
        return [], []
    try:
        raw = ledger.read_bytes()
    except OSError as exc:
        return [], [f"upload ledger unreadable: {exc}"]
    if raw and not raw.endswith(b"\n"):
        return [], ["upload ledger has a partial final row"]
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return [], [f"upload ledger is not UTF-8: {exc}"]
    entries: list[dict] = []
    problems: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        if not line:
            problems.append(f"upload ledger row {line_no} is empty")
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            problems.append(f"upload ledger row {line_no} is invalid JSON")
            continue
        if not isinstance(entry, dict):
            problems.append(f"upload ledger row {line_no} is not an object")
            continue
        entries.append(entry)
    return entries, problems


def ledger_guard(ledger: Path, video_sha256: str) -> tuple[str | None, dict | None, list[str]]:
    """Return ``uploaded``/``unresolved`` or a malformed-ledger problem.

    Legacy one-row entries remain readable.  New two-phase entries make an
    uploader crash distinguishable from a known failed attempt.  Any STARTED
    intent without its matching FINISHED row is globally ambiguous and blocks
    all further uploads until a human reconciles the external Bilibili state.
    """

    entries, problems = read_ledger(ledger)
    if problems:
        return None, None, problems
    started: dict[str, dict] = {}
    finished: set[str] = set()
    successful: dict | None = None
    for row_no, entry in enumerate(entries, start=1):
        event = entry.get("event")
        if event is None:
            # Legacy terminal-only row.
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                successful = entry
            continue
        attempt_id = entry.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            problems.append(f"upload ledger row {row_no} has no attempt_id")
            continue
        if event == "UPLOAD_ATTEMPT_STARTED":
            if attempt_id in started or attempt_id in finished:
                problems.append(f"upload ledger attempt {attempt_id} has a duplicate/out-of-order STARTED row")
                continue
            required_started = (
                "artifact_id",
                "video_sha256",
                "cover_sha256",
                "manifest",
                "manifest_sha256",
                "uploader",
            )
            if any(not isinstance(entry.get(key), str) or not entry.get(key) for key in required_started):
                problems.append(f"upload ledger attempt {attempt_id} has an incomplete STARTED row")
                continue
            started[attempt_id] = entry
        elif event == "UPLOAD_ATTEMPT_FINISHED":
            if attempt_id not in started or attempt_id in finished:
                problems.append(f"upload ledger attempt {attempt_id} has an unmatched/duplicate FINISHED row")
                continue
            finished.add(attempt_id)
            stable_keys = (
                "artifact_id",
                "video_sha256",
                "cover_sha256",
                "manifest",
                "manifest_sha256",
                "uploader",
            )
            changed = [key for key in stable_keys if entry.get(key) != started[attempt_id].get(key)]
            if changed:
                problems.append(f"upload ledger attempt {attempt_id} changed bound fields: {changed}")
            if isinstance(entry.get("rc"), bool) or not isinstance(entry.get("rc"), int):
                problems.append(f"upload ledger attempt {attempt_id} has no integer terminal rc")
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                successful = entry
        else:
            problems.append(f"upload ledger row {row_no} has unknown event {event!r}")
    if problems:
        return None, None, problems
    unresolved = [entry for attempt_id, entry in started.items() if attempt_id not in finished]
    if unresolved:
        return "unresolved", unresolved[0], []
    if successful is not None:
        return "uploaded", successful, []
    return None, None, []


def append_ledger(ledger: Path, entry: dict) -> None:
    """Durably append one JSONL row before releasing the shared upload lock."""

    ledger.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        view = memoryview(payload)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short write while appending upload ledger")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    parent_fd = os.open(ledger.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


@contextmanager
def exclusive_upload_lock(path: Path) -> Iterator[None]:
    """Serialize verified upload+ledger writes with false-green repair.

    The repair transaction takes this same advisory lock while it proves that
    the rejected song was not uploaded and commits its tombstones.  Holding the
    lock from manifest verification through ledger append prevents either side
    from observing a half-finished upload decision.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            raise UploadLockBusy(
                f"shared upload/repair lock is busy: {path}; retry after the active transaction finishes"
            ) from exc
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def upload(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    ledger = Path(args.ledger)
    # The default is deliberately independent of --ledger.  A caller must not
    # bypass repair/upload serialization by pointing the ledger somewhere else.
    # Tests or genuinely isolated channels may opt into another lock explicitly.
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK

    with exclusive_upload_lock(lock_path):
        manifest, problems = load_and_verify(manifest_path)
        if problems:
            for p in problems:
                print(f"REFUSE: {p}", file=sys.stderr)
            return 2
        ledger = Path(args.ledger)
        video_sha = manifest["video"]["sha256"]
        guard_status, guard_row, ledger_problems = ledger_guard(ledger, video_sha)
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5
        if guard_status == "unresolved":
            print(
                "REFUSE: upload ledger has an unresolved UPLOAD_ATTEMPT_STARTED "
                f"(attempt_id={guard_row.get('attempt_id')}); reconcile the external upload state before retrying",
                file=sys.stderr,
            )
            return 5
        if guard_status == "uploaded":
            print(
                f"REFUSE: this exact video was already uploaded at {guard_row.get('at')}"
                f" (bvid={guard_row.get('bvid') or '?'}) — duplicate posts are the 充电器 incident; not repeating it",
                file=sys.stderr,
            )
            return 3
        cmd = [args.uploader, manifest["video"]["path"], manifest["cover"]["path"], manifest["title"]]
        manifest_tags = manifest.get("tags") or []
        if manifest_tags:
            # The uploader receives ONLY manifest-bound args; the tag line is
            # frozen at review time exactly like title/hashes.
            cmd.append(",".join(manifest_tags))
        env = os.environ.copy()
        env["AUTHORIZED_UPLOAD"] = "1"
        attempt_id = uuid.uuid4().hex
        manifest_resolved = manifest_path.resolve()
        manifest_sha = sha256_file(manifest_resolved)
        common_ledger_fields = {
            "attempt_id": attempt_id,
            "artifact_id": manifest["artifact_id"],
            "video_sha256": video_sha,
            "cover_sha256": manifest["cover"]["sha256"],
            "title": manifest["title"],
            "authorized_by": manifest["authorization"]["by"],
            "authorization_quote": manifest["authorization"]["quote"],
            "manifest": str(manifest_resolved),
            "manifest_sha256": manifest_sha,
            "uploader": args.uploader,
        }
        if manifest_tags:
            common_ledger_fields["tags"] = ",".join(manifest_tags)
        append_ledger(
            ledger,
            {
                "event": "UPLOAD_ATTEMPT_STARTED",
                "at": now(),
                **common_ledger_fields,
            },
        )
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
                "event": "UPLOAD_ATTEMPT_FINISHED",
                "at": now(),
                **common_ledger_fields,
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
    mk.add_argument(
        "--tags",
        default="",
        help="comma-joined FULL tag line (base tags included), e.g. from scripts/suggest_upload_tags.py; "
        "optional — without it the uploader falls back to the base-4 line",
    )
    mk.add_argument("--out", default=None)
    mk.set_defaults(func=make_manifest)

    up = sub.add_parser("upload", help="verify hashes + ledger, then run the uploader with manifest args")
    up.add_argument("--manifest", required=True)
    up.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    up.add_argument("--lock", default=None, help="shared upload/repair lock (default: production base/upload.lock)")
    up.add_argument("--uploader", default=DEFAULT_UPLOADER)
    up.set_defaults(func=upload)

    ve = sub.add_parser("verify", help="pre-flight hash/authorization check only")
    ve.add_argument("--manifest", required=True)
    ve.set_defaults(func=verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UploadLockBusy as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
