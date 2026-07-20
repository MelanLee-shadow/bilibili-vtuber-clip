#!/usr/bin/env python3
"""Manifest-bound manual upload channel (2026-07-09 external audit).

Problem: `do_upload.sh <video> <cover> <title>` took free-form arguments — no
proof that the file Ivan REVIEWED is the file that got UPLOADED, no idempotency
(the 充电器 clip was double-posted once), authorization lived only in chat.

This tool binds the whole chain to one manifest file:

    1. make-manifest   — at review time: records sha256 of the exact video and
                         cover plus the verbatim title and Ivan's authorization
                         quote.  The manifest IS the reviewed artifact's identity.
                         The target season (合集) lane is derived from the frozen
                         title (song catalog prefix → 小李歌唱, else 小李切片) and
                         frozen into the manifest — season membership is part of
                         the publish, not an afterthought (Ivan 2026-07-20).
    2. upload          — at upload time: RECOMPUTES the hashes; any drift since
                         review refuses loudly.  A ledger (jsonl, pulled to the
                         Mac with the other reports) makes re-uploading the same
                         video a hard error.  The uploader command receives the
                         manifest's paths/title — never hand-typed ones — and
                         runs with AUTHORIZED_UPLOAD=1 (do_upload.sh refuses to
                         run without it).  After a successful post it completes
                         the manifest's season add (waits for state=0, live-
                         queries the season id by title, adds the episode, then
                         PUBLICLY re-verifies) — exit 6 means "posted but season
                         membership is not publicly verified yet: run season-add".
    3. season-add      — idempotently finish/re-verify the season step for an
                         already-posted manifest (bvid resolved from the ledger).
                         发布未入集 = 流程未完成; this subcommand is the retry path.
    4. verify          — hash re-check only (pre-flight).

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
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
DEFAULT_LEDGER = DEFAULT_BASE / "reports" / "upload_ledger.jsonl"
DEFAULT_UPLOAD_LOCK = DEFAULT_BASE / "upload.lock"
DEFAULT_UPLOADER = "/opt/bilive/app/tmp_manual_upload/do_upload.sh"
DEFAULT_COOKIE_JSON = Path("/opt/bilive/app/cookie.json")

# Season (合集) policy — membership is part of the publish (Ivan 2026-07-20).
# The LANE is a deterministic choke point on the frozen title: the song catalog
# prefix is schema-enforced elsewhere (title_policy.canonicalize_song_catalog_title),
# so title→lane cannot drift from content.  Season IDs are deliberately NOT
# frozen: the skill requires live-querying them from 创作中心 before use.
SONG_TITLE_PREFIX = "【李豆沙】豆沙歌，"
SEASON_TITLES = {"talk": "小李切片", "song": "小李歌唱"}
SEASON_ADD_ALREADY_IN = 20080  # episodes/add: already in the season (idempotent OK)
VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
TAGS_API = "https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
SEASONS_API = "https://member.bilibili.com/x2/creative/web/seasons?pn=1&ps=30"
EPISODES_ADD_API = "https://member.bilibili.com/x2/creative/web/season/section/episodes/add?csrf={csrf}"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


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


def sidecar_record_path(video: Path) -> Path:
    """Delivered clips ship a `<stem>.record.json` next to `<stem>.mp4`.

    Stems may contain dots, so strip only a known media suffix instead of
    Path.with_suffix (which would eat everything after the last dot).
    """
    name = video.name
    for ext in (".mp4", ".flv", ".mkv"):
        if name.endswith(ext):
            return video.parent / (name[: -len(ext)] + ".record.json")
    return video.parent / (name + ".record.json")


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


def derive_season_lane(title: str) -> str:
    """talk|song from the frozen title — the song catalog prefix is the choke point."""
    return "song" if title.startswith(SONG_TITLE_PREFIX) else "talk"


def season_block_for(title: str, choice: str) -> dict | None:
    """The manifest's frozen season binding.  ``none`` opts out explicitly;
    an explicit talk/song that contradicts the title-derived lane is refused
    (song titles publish to 小李歌唱, everything else to 小李切片 — no exceptions
    without changing the title first)."""
    derived = derive_season_lane(title)
    if choice == "none":
        return None
    if choice == "auto":
        lane = derived
    elif choice in SEASON_TITLES:
        if choice != derived:
            raise ValueError(
                f"--season {choice} contradicts the title-derived lane {derived!r}"
                " — the title decides the season; fix the title instead"
            )
        lane = choice
    else:
        raise ValueError(f"unknown season choice {choice!r}")
    return {"lane": lane, "season_title": SEASON_TITLES[lane], "source": f"{choice}:title-prefix"}


def validate_season_block(block: object) -> list[str]:
    if block is None:
        return []
    if not isinstance(block, dict):
        return ["season block must be an object or null"]
    lane = block.get("lane")
    if lane not in SEASON_TITLES:
        return [f"season lane must be one of {sorted(SEASON_TITLES)}: {lane!r}"]
    if block.get("season_title") != SEASON_TITLES[lane]:
        return [f"season title for lane {lane!r} must be {SEASON_TITLES[lane]!r}"]
    return []


def effective_season_block(manifest: dict) -> tuple[dict | None, str]:
    """(block, provenance).  Legacy manifests (pre-2026-07-20, no season key)
    derive the lane from the frozen title so the completion contract still
    applies to them."""
    if "season" in manifest:
        return manifest["season"], "manifest"
    return season_block_for(str(manifest.get("title") or ""), "auto"), "derived-from-frozen-title"


def _build_season_http(cookie_json: Path):
    """(http, csrf) using the production bilibili cookie file.

    ``http(url, data=None, is_json=False) -> dict`` — member.* endpoints get the
    cookie jar; the public view/tags API only needs a browser UA.  Cookie values
    are never printed or embedded in results."""
    raw = json.loads(Path(cookie_json).read_text(encoding="utf-8"))
    cookies = raw["data"]["cookie_info"]["cookies"]
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    csrf = next(c["value"] for c in cookies if c["name"] == "bili_jct")

    def http(url: str, data: dict | None = None, is_json: bool = False) -> dict:
        headers = {"User-Agent": _BROWSER_UA}
        if "member.bilibili.com" in url:
            headers["Cookie"] = jar
            headers["Referer"] = "https://member.bilibili.com/"
        body: bytes | None = None
        if data is not None:
            if is_json:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                body = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    return http, csrf


def season_add_flow(
    manifest: dict,
    bvid: str,
    *,
    http,
    csrf: str,
    wait_seconds: float = 900.0,
    poll_seconds: float = 30.0,
    display_wait_seconds: float = 240.0,
    sleeper=time.sleep,
) -> dict:
    """Add the archive to its manifest-bound season and PUBLICLY verify it.

    Returns an evidence dict whose ``status`` is the completion truth:
    IN_SEASON_PUBLIC is the only success; everything else means the publish is
    not finished (re-run ``season-add``).  API pitfalls encoded here, from the
    2026-06-22/07-04 incidents: episodes/add wants camelCase ``sectionId`` +
    ``episodes`` (snake_case returns code 0 without taking effect — which is why
    this flow re-reads the PUBLIC view instead of trusting code 0), season/switch
    is dead (-404), and 20080 means already-in-season (idempotent success)."""
    block, provenance = effective_season_block(manifest)
    result: dict = {
        "schema_version": "authorized-upload-season-verify.v1",
        "bvid": bvid,
        "title": manifest.get("title"),
        "season_binding": block,
        "season_binding_source": provenance,
        "verified_at": now(),
    }
    if block is None:
        result["status"] = "SEASON_OPTED_OUT"
        return result
    expected_season = block["season_title"]

    deadline = time.monotonic() + max(0.0, wait_seconds)
    view_data: dict = {}
    while True:
        view = http(VIEW_API.format(bvid=bvid))
        view_data = view.get("data") or {}
        state = view_data.get("state")
        result["last_view_code"], result["state"] = view.get("code"), state
        if view.get("code") == 0 and state == 0:
            break
        if time.monotonic() >= deadline:
            result["status"] = "PENDING_TRANSCODE"
            return result
        sleeper(poll_seconds)
    aid, cid = view_data.get("aid"), view_data.get("cid")
    result["aid"], result["cid"] = aid, cid
    if not aid or not cid:
        result["status"] = "PENDING_VIEW_INCOMPLETE"
        return result

    seasons = http(SEASONS_API)
    season_id = section_id = None
    for entry in ((seasons.get("data") or {}).get("seasons") or []):
        season = entry.get("season") or {}
        if season.get("title") != expected_season:
            continue
        sections = ((entry.get("sections") or {}).get("sections") or [])
        chosen = next((s for s in sections if s.get("title") == "正片"), None) or (sections[0] if sections else None)
        if chosen:
            season_id, section_id = season.get("id"), chosen.get("id")
        break
    result["season_id"], result["section_id"] = season_id, section_id
    if not season_id or not section_id:
        result["status"] = "SEASON_NOT_FOUND"
        return result

    add = http(
        EPISODES_ADD_API.format(csrf=csrf),
        data={
            "sectionId": section_id,
            "episodes": [{"aid": aid, "cid": cid, "title": manifest.get("title"), "charging_pay": 0}],
        },
        is_json=True,
    )
    result["season_add_code"], result["season_add_message"] = add.get("code"), add.get("message")
    if add.get("code") not in (0, SEASON_ADD_ALREADY_IN):
        result["status"] = f"ADD_FAILED_{add.get('code')}"
        return result

    display_deadline = time.monotonic() + max(0.0, display_wait_seconds)
    while True:
        view = http(VIEW_API.format(bvid=bvid))
        data = view.get("data") or {}
        ugc_season = (data.get("ugc_season") or {}).get("title")
        displayed = bool(data.get("is_season_display"))
        result["ugc_season_title"], result["is_season_display"] = ugc_season, displayed
        if view.get("code") == 0 and ugc_season == expected_season and displayed:
            break
        if time.monotonic() >= display_deadline:
            result["status"] = "PENDING_DISPLAY"
            return result
        sleeper(poll_seconds)
    try:
        tags = http(TAGS_API.format(bvid=bvid))
        result["tags"] = [t.get("tag_name") for t in (tags.get("data") or [])]
    except Exception as exc:  # tags are evidence garnish, not the completion gate
        result["tags_error"] = str(exc)
    result["verified_at"] = now()
    result["status"] = "IN_SEASON_PUBLIC"
    return result


def season_verify_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".season_verify.json")


def _run_season_step(manifest: dict, manifest_path: Path, bvid: str | None, args: argparse.Namespace) -> int:
    """Shared by upload (post-success) and season-add.  0 = publicly in-season."""
    block, provenance = effective_season_block(manifest)
    if block is None:
        print("season: manifest explicitly opts out (season=null) — archive stays outside collections")
        return 0
    if not bvid:
        print(
            "SEASON PENDING: no bvid available (uploader output had no BVID= line); "
            "run: authorized_upload.py season-add --manifest <manifest> --bvid <BV...>",
            file=sys.stderr,
        )
        return 6
    http, csrf = _build_season_http(Path(args.cookie_json))
    result = season_add_flow(
        manifest,
        bvid,
        http=http,
        csrf=csrf,
        wait_seconds=args.season_wait,
        poll_seconds=args.season_poll,
    )
    sidecar = season_verify_sidecar_path(manifest_path)
    sidecar.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"SEASON {result['status']}: bvid={bvid} season={block['season_title']} ({provenance}) "
        f"evidence={sidecar}"
    )
    if result["status"] == "IN_SEASON_PUBLIC":
        return 0
    print(
        f"SEASON INCOMPLETE ({result['status']}): the publish is NOT finished — "
        f"re-run: authorized_upload.py season-add --manifest {manifest_path}",
        file=sys.stderr,
    )
    return 6


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
    tags_source = "cli" if tags else None
    if not tags and not args.no_tags:
        # Auto-pickup (Ivan 2026-07-13): produce_slice_package freezes generated
        # tags into the delivered <stem>.record.json; make-manifest reads them so
        # the unattended chain needs no hand-typed tag line. CLI --tags overrides;
        # --no-tags opts out; a video without a record sidecar just gets no tags
        # (uploader falls back to base-4).
        record_sidecar = sidecar_record_path(video)
        if record_sidecar.is_file():
            try:
                sidecar = json.loads(record_sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"REFUSE: tag sidecar unreadable: {record_sidecar} ({exc}); pass --tags or --no-tags", file=sys.stderr)
                return 2
            upload_tags = sidecar.get("upload_tags") or {}
            if str(upload_tags.get("status") or "").startswith("OK") and upload_tags.get("final_tags"):
                tags = [str(t).strip() for t in upload_tags["final_tags"]]
                tags_source = f"record.json:{upload_tags.get('engine') or '?'}"
    if tags:
        tag_problems = validate_tags(tags)
        if tag_problems:
            for problem in tag_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        print(f"tags: {len(tags)} from {tags_source}", file=sys.stderr)
    else:
        print("tags: none (uploader falls back to base tags)", file=sys.stderr)
    try:
        season = season_block_for(args.title, args.season)
    except ValueError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2
    if season is None:
        print("season: EXPLICITLY none — this archive will not join a collection", file=sys.stderr)
    else:
        print(f"season: {season['season_title']} ({season['source']})", file=sys.stderr)
    video_sha = sha256_file(video)
    manifest = {
        "manifest_version": 2,
        "artifact_id": video_sha[:12],
        "video": {"path": str(video.resolve()), "sha256": video_sha, "bytes": video.stat().st_size},
        "cover": {"path": str(cover.resolve()), "sha256": sha256_file(cover), "bytes": cover.stat().st_size},
        "title": args.title,
        "season": season,
        "authorization": {"by": args.authorized_by, "quote": args.quote, "at": now()},
        "created_at": now(),
    }
    if tags:
        manifest["tags"] = tags
        manifest["tags_source"] = tags_source
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
    if "season" in manifest:
        problems.extend(validate_season_block(manifest["season"]))
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
    if completed.returncode != 0:
        return completed.returncode
    # Season membership is part of the publish (发布未入集 = 流程未完成).  This
    # runs OUTSIDE the shared upload lock: it is read-mostly plus an idempotent
    # add, and the transcode wait must not serialize other transactions.
    if args.skip_season:
        print(
            "SEASON SKIPPED (--skip-season): the publish is NOT complete until "
            f"season-add succeeds for {manifest_path}",
            file=sys.stderr,
        )
        return 0
    return _run_season_step(manifest, manifest_path, bvid, args)


def verify(args: argparse.Namespace) -> int:
    manifest, problems = load_and_verify(Path(args.manifest))
    if problems:
        for p in problems:
            print(f"FAIL: {p}", file=sys.stderr)
        return 2
    print(f"OK: artifact {manifest['artifact_id']} matches its manifest (video+cover hashes, title, authorization present)")
    return 0


def season_add(args: argparse.Namespace) -> int:
    """Finish/re-verify season membership for an already-posted manifest."""
    manifest_path = Path(args.manifest)
    manifest, problems = load_and_verify(manifest_path)
    if problems:
        # The archive is already public — hash drift of the LOCAL copy must not
        # block finishing its season membership, but say it loudly.
        for p in problems:
            print(f"WARN (season-add continues): {p}", file=sys.stderr)
        if manifest is None:
            return 2
    bvid = args.bvid
    if not bvid:
        status, row, ledger_problems = ledger_guard(Path(args.ledger), manifest["video"]["sha256"])
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5
        if status != "uploaded" or not row or not row.get("bvid"):
            print(
                "REFUSE: ledger has no successful upload with a bvid for this manifest's video; "
                "pass --bvid explicitly if the post exists",
                file=sys.stderr,
            )
            return 5
        bvid = str(row["bvid"])
    return _run_season_step(manifest, manifest_path, bvid, args)


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
        "omitted → auto-pickup from the video's sibling <stem>.record.json (upload_tags), "
        "else no tags and the uploader falls back to the base-4 line",
    )
    mk.add_argument("--no-tags", action="store_true", help="skip record.json tag auto-pickup")
    mk.add_argument(
        "--season",
        default="auto",
        choices=["auto", "talk", "song", "none"],
        help="season (合集) binding frozen into the manifest; auto derives from the title "
        "(song catalog prefix → 小李歌唱, else 小李切片); none opts out explicitly",
    )
    mk.add_argument("--out", default=None)
    mk.set_defaults(func=make_manifest)

    def _season_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--cookie-json", default=str(DEFAULT_COOKIE_JSON), help="bilibili login-API cookie file")
        p.add_argument("--season-wait", type=float, default=900.0, help="seconds to wait for state=0 (transcode)")
        p.add_argument("--season-poll", type=float, default=30.0, help="poll interval seconds")

    up = sub.add_parser(
        "upload",
        help="verify hashes + ledger, run the uploader with manifest args, then finish the season add (exit 6 = posted but season incomplete)",
    )
    up.add_argument("--manifest", required=True)
    up.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    up.add_argument("--lock", default=None, help="shared upload/repair lock (default: production base/upload.lock)")
    up.add_argument("--uploader", default=DEFAULT_UPLOADER)
    up.add_argument("--skip-season", action="store_true", help="EMERGENCY ONLY: post without finishing the season step")
    _season_args(up)
    up.set_defaults(func=upload)

    se = sub.add_parser("season-add", help="idempotently finish/re-verify season membership for a posted manifest")
    se.add_argument("--manifest", required=True)
    se.add_argument("--bvid", default=None, help="override; default resolves from the ledger's successful upload")
    se.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    _season_args(se)
    se.set_defaults(func=season_add)

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
