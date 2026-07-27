#!/usr/bin/env python3
"""Manifest-bound Li Dousha publish and existing-BV repair entry point.

``make-manifest`` freezes reviewed bytes and Ivan's authorization; ``upload``
and ``season-add`` enforce hashes, idempotency, metadata, public, Creator, and
exact-section readback.  ``repair-*`` uses a separate crash-safe journal and
can only append/edit an explicitly named existing BV; it cannot create one.
``repair-verify-live`` re-observes all four public/Creator surfaces after the
transaction reaches VERIFIED and writes a create-only completed sidecar.
"""
from __future__ import annotations

import argparse
import datetime as dt
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
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_lidousha_review_package import (  # noqa: E402
    AUDIT_POLICY_EPOCH,
    AUDIT_SCHEMA_VERSION,
    audit_package,
)
from src.autoslice import authorized_upload_cli_parser  # noqa: E402
from src.autoslice import bilibili_member_api as member_api  # noqa: E402
from src.autoslice.package_audit_binding import (
    audit_content_binding as _audit_content_binding,
)
from src.autoslice import final_human_review as human_review  # noqa: E402
from src.autoslice import same_bv_repair as repair_binding  # noqa: E402
from src.autoslice import same_bv_live_verification  # noqa: E402
from src.autoslice.subtitle_validation import validate_srt_file  # noqa: E402
from src.autoslice.same_bv_repair import (  # noqa: E402
    BilibiliRepairAdapter,
    PlanInvalid,
    RepairError,
    assert_bvid_unowned as assert_same_bv_unowned,
    create_plan as create_same_bv_repair_plan,
    initialise_journal as initialise_same_bv_repair_journal,
    load_plan as load_same_bv_repair_plan,
    preview_repair as preview_same_bv_repair,
    repair_status as same_bv_repair_status,
    run_repair as run_same_bv_repair,
    write_plan as write_same_bv_repair_plan,
)
from src.autoslice.title_policy import (  # noqa: E402
    publish_title_policy_violations,
)

CookieSchemaError = member_api.CookieSchemaError
DEFAULT_BASE = Path(os.environ.get("AUTOSLICE_BASE", "/opt/bilive/autoslice"))
DEFAULT_LEDGER = DEFAULT_BASE / "reports" / "upload_ledger.jsonl"
DEFAULT_REPAIR_LEDGER = DEFAULT_BASE / "reports" / "same_bv_repair_ledger.jsonl"
DEFAULT_UPLOAD_LOCK = DEFAULT_BASE / "upload.lock"
DEFAULT_UPLOADER = "/opt/bilive/app/tmp_manual_upload/do_upload.sh"
DEFAULT_COOKIE_JSON = Path("/opt/bilive/app/cookie.json")
DEFAULT_REPAIR_BILIUP_COOKIE_JSON = member_api.DEFAULT_BILIUP_COOKIES

# Season (合集) policy — membership is part of the publish (Ivan 2026-07-20).
# The LANE is a deterministic choke point on the frozen title: the song catalog
# prefix/catalog form is enforced by the shared publish-title validator,
# so title→lane cannot drift from content.  Season IDs are deliberately NOT
# selected by a live title query, then checked against the channel's committed
# talk/song IDs so a renamed or wrong collection cannot silently become truth.
SONG_TITLE_PREFIX = "【李豆沙】豆沙歌，"
SEASON_TITLES = {"talk": "小李切片", "song": "小李歌唱"}
SEASON_ADD_ALREADY_IN = 20080  # episodes/add: already in the season (idempotent OK)
VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
TAGS_API = "https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}"
SEASONS_API = "https://member.bilibili.com/x2/creative/web/seasons?pn=1&ps=30"
EPISODES_ADD_API = "https://member.bilibili.com/x2/creative/web/season/section/episodes/add?csrf={csrf}"
SECTION_VIEW_API = "https://member.bilibili.com/x2/creative/web/season/section?id={section_id}"
MEMBER_ARCHIVE_VIEW_API = "https://member.bilibili.com/x/vupre/web/archive/view?bvid={bvid}"
CREATOR_ARCHIVES_API = (
    "https://member.bilibili.com/x/web/archives"
    "?pn={page}&ps=30&status=is_pubing,pubed,not_pubed"
)
QUOTA_FREQUENCY_CODE = 21566
ROLLING_UPLOAD_LIMIT = 10
ROLLING_UPLOAD_WINDOW_SECONDS = 24 * 60 * 60
EXPECTED_SEASON_IDS = {
    "talk": {"season_id": 8383206, "section_id": 9320779},
    "song": {"season_id": 8410735, "section_id": 9364628},
}
SUBMISSION_DESCRIPTION = (
    "李豆沙个人主页：https://space.bilibili.com/1703797642\n"
    "李豆沙直播间：https://live.bilibili.com/22966160"
)
EXPECTED_TID = 21
EXPECTED_COPYRIGHT = 2
EXPECTED_SOURCE = "https://live.bilibili.com/"
# biliup's APP submission sends the two-line description above, while
# Bilibili's public/member archive surfaces prepend the copyright source URL.
# Freeze the observed public contract instead of treating that deterministic
# server-side projection as metadata drift.
DEFAULT_DESCRIPTION = EXPECTED_SOURCE + "\n" + SUBMISSION_DESCRIPTION
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


def sidecar_subtitle_path(video: Path) -> Path:
    name = video.name
    for ext in (".mp4", ".flv", ".mkv"):
        if name.endswith(ext):
            return video.parent / (name[: -len(ext)] + ".srt")
    return video.parent / (name + ".srt")


def _sha_entry(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def _strip_sha_prefix(value: object) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _load_json_object(path: Path, label: str, problems: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{label} unreadable: {path} ({exc})")
        return {}
    if not isinstance(value, dict):
        problems.append(f"{label} must be a JSON object: {path}")
        return {}
    return value


def _zero_blocking_issues(audit: dict) -> bool:
    blocking = audit.get("blocking_issue_count")
    issue_count = audit.get("issue_count")
    issues = audit.get("issues")
    return bool(
        isinstance(blocking, int)
        and not isinstance(blocking, bool)
        and blocking == 0
        and isinstance(issue_count, int)
        and not isinstance(issue_count, bool)
        and isinstance(issues, list)
        and issue_count == len(issues)
    )


def _resolved_manifest_item_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _find_review_item(review_manifest: dict, root: Path, video: Path) -> dict | None:
    expected = video.resolve()
    for item in review_manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        media = _resolved_manifest_item_path(root, item.get("media") or item.get("video"))
        if media == expected:
            return item
    return None


def _record_artifact_hash_problems(
    record: dict,
    *,
    video: Path,
    cover: Path,
    subtitle: Path,
    title: str,
) -> list[str]:
    problems: list[str] = []
    artifact_hashes = record.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        return ["record.json has no artifact_hashes object"]

    expected = {
        "video": sha256_file(video),
        "cover": sha256_file(cover),
        "subtitle": sha256_file(subtitle),
    }
    accepted_video = {
        _strip_sha_prefix(artifact_hashes.get("burned_video_sha256")),
        _strip_sha_prefix(artifact_hashes.get("video_sha256")),
    }
    if expected["video"] not in accepted_video:
        problems.append("record artifact hashes do not bind the reviewed video")
    if expected["cover"] != _strip_sha_prefix(artifact_hashes.get("cover_sha256")):
        problems.append("record artifact hashes do not bind the reviewed cover")
    accepted_subtitle = {
        _strip_sha_prefix(artifact_hashes.get("delivery_subtitle_sha256")),
        _strip_sha_prefix(artifact_hashes.get("subtitle_sha256")),
    }
    if expected["subtitle"] not in accepted_subtitle:
        problems.append("record artifact hashes do not bind the reviewed SRT")

    publish_staging = record.get("publish_staging")
    record_title = publish_staging.get("title") if isinstance(publish_staging, dict) else None
    if record_title != title:
        problems.append(
            f"record publish title mismatch: record={record_title!r} manifest={title!r}"
        )
    story_contract = record.get("story_contract")
    if not isinstance(story_contract, dict):
        problems.append("record.json has no story_contract object")
    else:
        if not str(story_contract.get("schema_version") or "").strip():
            problems.append("record story_contract has no schema_version")
        if not str(story_contract.get("candidate_id") or "").strip():
            problems.append("record story_contract has no candidate_id")
        if not str(story_contract.get("transcript_sha256") or "").strip():
            problems.append("record story_contract has no transcript_sha256")
    return problems


def _validate_v3_package_attestation(
    manifest: dict,
    *,
    verify_hashes: bool,
) -> list[str]:
    problems: list[str] = []
    attestation = manifest.get("package_attestation")
    if not isinstance(attestation, dict):
        return ["manifest v3 has no package_attestation object"]
    if attestation.get("schema_version") != "authorized-upload-package-attestation.v1":
        problems.append("package_attestation schema_version is invalid")

    entries: dict[str, tuple[Path, dict]] = {}
    for key in ("package_audit", "review_manifest", "record", "subtitle"):
        entry = attestation.get(key)
        if not isinstance(entry, dict):
            problems.append(f"package_attestation.{key} is missing")
            continue
        path = Path(str(entry.get("path") or ""))
        entries[key] = (path, entry)
        if not path.is_file():
            problems.append(f"package_attestation.{key} missing: {path}")
            continue
        if verify_hashes:
            actual = sha256_file(path)
            if actual != entry.get("sha256"):
                problems.append(
                    f"package_attestation.{key} HASH DRIFT: "
                    f"manifest={str(entry.get('sha256'))[:12]} actual={actual[:12]} ({path})"
                )

    video_entry = manifest.get("video") or {}
    cover_entry = manifest.get("cover") or {}
    video = Path(str(video_entry.get("path") or ""))
    cover = Path(str(cover_entry.get("path") or ""))
    if not video.is_file() or not cover.is_file():
        return problems
    root_text = attestation.get("package_root")
    root = Path(str(root_text or "")).resolve()
    if not root_text or not root.is_dir():
        problems.append(f"package root missing: {root}")
        return problems
    if video.parent.resolve() != root:
        problems.append("reviewed video is not directly inside the audited package root")
    if not _is_within(cover, root):
        problems.append("reviewed cover is outside the audited package root")
    expected_stem = video.stem
    expected_paths = {
        "record": root / f"{expected_stem}.record.json",
        "subtitle": root / f"{expected_stem}.srt",
    }
    if cover.resolve() != (root / f"{expected_stem}.cover.png").resolve():
        problems.append("cover is not the same-stem <stem>.cover.png")
    for key, expected_path in expected_paths.items():
        current = entries.get(key)
        if current and current[0].resolve() != expected_path.resolve():
            problems.append(f"{key} is not the same-stem {expected_path.name}")

    audit_path = entries.get("package_audit", (Path(), {}))[0]
    audit = _load_json_object(audit_path, "package audit", problems) if audit_path.is_file() else {}
    audit_root = Path(str(audit.get("root") or "")).resolve()
    if audit.get("schema_version") != AUDIT_SCHEMA_VERSION:
        problems.append("package audit schema_version is stale or invalid")
    if audit.get("policy_epoch") != AUDIT_POLICY_EPOCH:
        problems.append("package audit policy_epoch is stale or invalid")
    if audit.get("passed") is not True:
        problems.append("package audit did not pass")
    if audit_root != root:
        problems.append(f"package audit root mismatch: audit={audit_root} manifest={root}")
    if not _zero_blocking_issues(audit):
        problems.append("package audit reports blocking issues")
    if root.is_dir():
        current_audit = audit_package(root)
        if current_audit.get("passed") is not True:
            problems.append("canonical package auditor currently rejects the package")
        if _audit_content_binding(audit) != _audit_content_binding(
            current_audit
        ):
            problems.append(
                "package audit is not the canonical current-policy result for the "
                "current package input closure"
            )

    review_path = entries.get("review_manifest", (Path(), {}))[0]
    review = _load_json_object(review_path, "review manifest", problems) if review_path.is_file() else {}
    problems.extend(human_review.final_human_review_attestation_problems(manifest))
    review_item = _find_review_item(review, root, video)
    if review_item is None:
        problems.append("review_manifest has no item for the reviewed video")
    else:
        expected_item_paths = {
            "cover": cover.resolve(),
            "record": expected_paths["record"].resolve(),
            "subtitle": expected_paths["subtitle"].resolve(),
        }
        item_paths = {
            "cover": _resolved_manifest_item_path(root, review_item.get("cover")),
            "record": _resolved_manifest_item_path(
                root, review_item.get("record") or review_item.get("record_json")
            ),
            "subtitle": _resolved_manifest_item_path(
                root, review_item.get("subtitle_srt") or review_item.get("subtitle")
            ),
        }
        for key, expected_path in expected_item_paths.items():
            if item_paths[key] != expected_path:
                problems.append(f"review_manifest {key} does not match the reviewed same-stem artifact")
        if review_item.get("title") != manifest.get("title"):
            problems.append("review_manifest title does not match the upload title")

    record_path = entries.get("record", (Path(), {}))[0]
    subtitle_path = entries.get("subtitle", (Path(), {}))[0]
    if record_path.is_file() and subtitle_path.is_file():
        subtitle_verdict = validate_srt_file(subtitle_path)
        if subtitle_verdict.get("status") != "PASS":
            codes = [
                str(row.get("code") or "SRT_RELEASE_VALIDATION_FAILED")
                for row in subtitle_verdict.get("errors") or []
                if isinstance(row, dict)
            ]
            problems.append(
                "reviewed SRT fails release validation: " + ",".join(codes)
            )
        record = _load_json_object(record_path, "record", problems)
        problems.extend(repair_binding.recovery_publication_package_problems(manifest, record, review_item))
        problems.extend(
            _record_artifact_hash_problems(
                record,
                video=video,
                cover=cover,
                subtitle=subtitle_path,
                title=str(manifest.get("title") or ""),
            )
        )
        record_tags = (record.get("upload_tags") or {}).get("final_tags")
        if record_tags != manifest.get("tags"):
            problems.append("manifest tags do not exactly match record.upload_tags.final_tags")
    return problems


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
    session = member_api.BiliSession(cookie_path=cookie_json)
    jar = session.cookie_header
    csrf = session.csrf

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
    expected_ids = EXPECTED_SEASON_IDS.get(str(block.get("lane") or ""))
    if expected_ids and (
        season_id != expected_ids["season_id"]
        or section_id != expected_ids["section_id"]
    ):
        result["expected_season_id"] = expected_ids["season_id"]
        result["expected_section_id"] = expected_ids["section_id"]
        result["status"] = "SEASON_ID_MISMATCH"
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
    result["verified_at"] = now()
    result["status"] = "IN_SEASON_PUBLIC"
    return result


def season_verify_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".season_verify.json")


def public_verify_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".public_verify.json")


def uploaded_sidecar_path(manifest_path: Path) -> Path:
    name = manifest_path.name
    stem = name[: -len(".upload_manifest.json")] if name.endswith(".upload_manifest.json") else name
    return manifest_path.parent / (stem + ".uploaded.json")


def _normalise_tags(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def _section_episode_rows(payload: object) -> list[dict]:
    rows: list[dict] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "episodes" and isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, dict))
            else:
                rows.extend(_section_episode_rows(value))
    elif isinstance(payload, list):
        for value in payload:
            rows.extend(_section_episode_rows(value))
    return rows


def _section_episode_title(row: dict) -> str:
    """Read the title from the known exact-section response shapes."""

    for key in ("title", "episode_title"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    for key in ("archive", "arc"):
        nested = row.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("title"), str):
            return str(nested["title"])
    return ""


def _section_ids(payload: object) -> set[int]:
    ids: set[int] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"section_id", "sectionId"} and isinstance(value, int):
                ids.add(value)
            elif key == "id" and isinstance(value, int) and (
                "episodes" in payload or "season_id" in payload or "seasonId" in payload
            ):
                ids.add(value)
            ids.update(_section_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            ids.update(_section_ids(value))
    return ids


def public_verify_flow(
    manifest: dict,
    bvid: str,
    *,
    http,
    season_result: dict,
    wait_seconds: float = 240.0,
    poll_seconds: float = 30.0,
    sleeper=time.sleep,
) -> dict:
    """Verify the public/member/tag/EXACT-section surfaces as one contract."""
    expected_tags = list(manifest.get("tags") or [])
    result: dict = {
        "schema_version": "authorized-upload-public-verify.v2",
        "bvid": bvid,
        "manifest_title": manifest.get("title"),
        "expected": {
            "title": manifest.get("title"),
            "description": manifest.get("description"),
            "tags": expected_tags,
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
            "season_id": season_result.get("season_id"),
            "section_id": season_result.get("section_id"),
        },
        "verified_at": now(),
    }
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        problems: list[str] = []
        public: dict = {}
        public_data: dict = {}
        public_tags: list[str] = []
        archive: dict = {}
        section: dict = {}
        try:
            public = http(VIEW_API.format(bvid=bvid))
            public_data = public.get("data") or {}
            tags_payload = http(TAGS_API.format(bvid=bvid))
            public_tags = [
                str(row.get("tag_name") or "").strip()
                for row in (tags_payload.get("data") or [])
                if isinstance(row, dict) and str(row.get("tag_name") or "").strip()
            ]
            member = http(MEMBER_ARCHIVE_VIEW_API.format(bvid=bvid))
            member_data = member.get("data") or {}
            archive = member_data.get("archive") or {}
            section_id = season_result.get("section_id")
            section = (
                http(SECTION_VIEW_API.format(section_id=section_id))
                if section_id
                else {"code": 0, "data": {}}
            )
        except Exception as exc:
            problems.append(f"verification API error: {exc}")

        result["public_view"] = {
            "code": public.get("code"),
            "state": public_data.get("state"),
            "aid": public_data.get("aid"),
            "cid": public_data.get("cid"),
            "title": public_data.get("title"),
            "desc": public_data.get("desc"),
            "tid": public_data.get("tid"),
            "copyright": public_data.get("copyright"),
            "ugc_season_id": (public_data.get("ugc_season") or {}).get("id"),
            "ugc_season_title": (public_data.get("ugc_season") or {}).get("title"),
            "is_season_display": public_data.get("is_season_display"),
        }
        result["public_tags"] = public_tags
        result["member_archive"] = {
            key: archive.get(key)
            for key in ("aid", "bvid", "title", "desc", "tag", "tid", "copyright", "source")
        }
        result["section_api"] = {
            "url": SECTION_VIEW_API.format(section_id=season_result.get("section_id")),
            "code": section.get("code") if isinstance(section, dict) else None,
        }

        expected_title = manifest.get("title")
        expected_desc = manifest.get("description")
        if public_data.get("state") != 0:
            problems.append(f"public state is {public_data.get('state')!r}, expected 0")
        for field, expected in (
            ("title", expected_title),
            ("desc", expected_desc),
            ("tid", EXPECTED_TID),
            ("copyright", EXPECTED_COPYRIGHT),
        ):
            if public_data.get(field) != expected:
                problems.append(f"public {field} mismatch")
        if public_tags != expected_tags and set(public_tags) != set(expected_tags):
            problems.append("public tags mismatch")
        for field, expected in (
            ("title", expected_title),
            ("desc", expected_desc),
            ("tid", EXPECTED_TID),
            ("copyright", EXPECTED_COPYRIGHT),
            ("source", EXPECTED_SOURCE),
        ):
            if archive.get(field) != expected:
                problems.append(f"Creator archive {field} mismatch")
        member_tags = _normalise_tags(archive.get("tag"))
        if member_tags != expected_tags and set(member_tags) != set(expected_tags):
            problems.append("Creator archive tags mismatch")
        if archive.get("bvid") not in (None, bvid):
            problems.append("Creator archive BVID mismatch")

        block, _ = effective_season_block(manifest)
        if block is not None:
            season_id = season_result.get("season_id")
            section_id = season_result.get("section_id")
            if (public_data.get("ugc_season") or {}).get("id") != season_id:
                problems.append("public season id mismatch")
            if (public_data.get("ugc_season") or {}).get("title") != block.get("season_title"):
                problems.append("public season title mismatch")
            if public_data.get("is_season_display") is not True:
                problems.append("public season is not displayed")
            section_ids = _section_ids(section)
            if section_ids and section_id not in section_ids:
                problems.append("exact section API returned a different section")
            aid = public_data.get("aid")
            episodes = _section_episode_rows(section)
            membership = [
                row
                for row in episodes
                if row.get("aid") == aid or row.get("bvid") == bvid
            ]
            result["section_api"]["episode_match_count"] = len(membership)
            result["section_api"]["section_ids_seen"] = sorted(section_ids)
            result["section_api"]["episode_titles"] = [
                _section_episode_title(row) for row in membership
            ]
            if not membership:
                problems.append("aid/BVID absent from exact section API")
            elif len(membership) != 1:
                problems.append("aid/BVID appears more than once in exact section API")
            elif _section_episode_title(membership[0]) != expected_title:
                problems.append("exact section episode title mismatch")

        result["problems"] = problems
        if not problems:
            result["status"] = "VERIFIED_PUBLIC"
            result["verified_at"] = now()
            return result
        if time.monotonic() >= deadline:
            result["status"] = "PUBLIC_VERIFY_FAILED"
            result["verified_at"] = now()
            return result
        sleeper(poll_seconds)


def _parse_ledger_time(value: object) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _recent_ledger_success_count(entries: list[dict], since_epoch: float) -> int:
    videos: set[str] = set()
    for entry in entries:
        if entry.get("rc") != 0:
            continue
        timestamp = _parse_ledger_time(entry.get("at"))
        if timestamp is None or timestamp < since_epoch:
            continue
        video_sha = entry.get("video_sha256")
        if isinstance(video_sha, str) and video_sha:
            videos.add(video_sha)
    return len(videos)


def _recent_creator_archive_count(http, since_epoch: float) -> int:
    archives: dict[str, dict] = {}
    page = 1
    while True:
        payload = http(CREATOR_ARCHIVES_API.format(page=page))
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Creator archives query failed: {payload.get('code')} {payload.get('message')}"
            )
        data = payload.get("data") or {}
        audits = data.get("arc_audits") or []
        for row in audits:
            if not isinstance(row, dict):
                continue
            archive = row.get("Archive") or row.get("archive") or {}
            if not isinstance(archive, dict):
                continue
            ptime = archive.get("ptime")
            if isinstance(ptime, (int, float)) and ptime >= since_epoch:
                key = str(archive.get("bvid") or archive.get("aid") or id(archive))
                archives[key] = archive
        page_info = data.get("page") or {}
        total = page_info.get("count") or page_info.get("total") or 0
        if not audits or page * 30 >= int(total or 0):
            break
        page += 1
    return len(archives)


def rolling_quota_guard(ledger: Path, *, http, now_epoch: float | None = None) -> tuple[dict, list[str]]:
    entries, problems = read_ledger(ledger)
    if problems:
        return {}, problems
    current = time.time() if now_epoch is None else now_epoch
    since = current - ROLLING_UPLOAD_WINDOW_SECONDS
    local_count = _recent_ledger_success_count(entries, since)
    try:
        creator_count = _recent_creator_archive_count(http, since)
    except Exception as exc:
        return {}, [f"rolling quota Creator estimate unavailable: {exc}"]
    estimate = max(local_count, creator_count)
    evidence = {
        "schema_version": "authorized-upload-rolling-quota.v1",
        "window_seconds": ROLLING_UPLOAD_WINDOW_SECONDS,
        "limit": ROLLING_UPLOAD_LIMIT,
        "since_epoch": since,
        "local_ledger_successes": local_count,
        "creator_recent_archives": creator_count,
        "estimated_used": estimate,
    }
    if estimate >= ROLLING_UPLOAD_LIMIT:
        return evidence, [
            f"rolling 24h upload estimate is {estimate}/{ROLLING_UPLOAD_LIMIT}; "
            "stop before upload (Bilibili code 21566 remains authoritative)"
        ]
    return evidence, []


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


def _write_json_sidecar(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _create_json_sidecar(path: Path, payload: dict) -> None:
    """Create one durable evidence file without overwriting prior truth."""

    path = path.resolve()
    if path.exists() or path.is_symlink():
        raise RepairError(f"completed sidecar already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    body = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        try:
            view = memoryview(body)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError(
                        "short write while creating completed sidecar"
                    )
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RepairError(
                f"completed sidecar already exists: {path}"
            ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _run_postpublish_verification(
    manifest: dict,
    manifest_path: Path,
    bvid: str | None,
    args: argparse.Namespace,
    *,
    quota_evidence: dict | None = None,
) -> tuple[int, dict | None]:
    if not bvid:
        print(
            "POSTED BUT UNVERIFIED: uploader returned no BVID; reconcile through Creator "
            "Center without re-uploading",
            file=sys.stderr,
        )
        return 6, None
    http, csrf = _build_season_http(Path(args.cookie_json))
    block, provenance = effective_season_block(manifest)
    if block is None:
        season_result = {
            "schema_version": "authorized-upload-season-verify.v1",
            "status": "SEASON_OPTED_OUT",
            "bvid": bvid,
            "season_binding": None,
            "season_binding_source": provenance,
            "verified_at": now(),
        }
    else:
        season_result = season_add_flow(
            manifest,
            bvid,
            http=http,
            csrf=csrf,
            wait_seconds=args.season_wait,
            poll_seconds=args.season_poll,
            display_wait_seconds=args.public_wait,
        )
    _write_json_sidecar(season_verify_sidecar_path(manifest_path), season_result)
    if block is not None and season_result.get("status") != "IN_SEASON_PUBLIC":
        blocked = {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "BLOCKED_BY_SEASON",
            "bvid": bvid,
            "season_status": season_result.get("status"),
            "verified_at": now(),
        }
        _write_json_sidecar(public_verify_sidecar_path(manifest_path), blocked)
        return 6, blocked

    public_result = public_verify_flow(
        manifest,
        bvid,
        http=http,
        season_result=season_result,
        wait_seconds=args.public_wait,
        poll_seconds=args.season_poll,
    )
    if quota_evidence is not None:
        public_result["preupload_quota_evidence"] = quota_evidence
    public_path = public_verify_sidecar_path(manifest_path)
    _write_json_sidecar(public_path, public_result)
    if public_result.get("status") != "VERIFIED_PUBLIC":
        print(
            f"PUBLIC VERIFY INCOMPLETE: {public_result.get('problems')}; evidence={public_path}",
            file=sys.stderr,
        )
        return 6, public_result

    attestation = manifest.get("package_attestation") or {}
    uploaded = {
        "schema_version": "authorized-upload-result.v3",
        "status": "VERIFIED_PUBLIC",
        "bvid": bvid,
        "aid": (public_result.get("public_view") or {}).get("aid"),
        "cid": (public_result.get("public_view") or {}).get("cid"),
        "title": manifest.get("title"),
        "uploaded_at": now(),
        "video_sha256": (manifest.get("video") or {}).get("sha256"),
        "cover_sha256": (manifest.get("cover") or {}).get("sha256"),
        "record_sha256": (attestation.get("record") or {}).get("sha256"),
        "subtitle_sha256": (attestation.get("subtitle") or {}).get("sha256"),
        "package_audit_sha256": (attestation.get("package_audit") or {}).get("sha256"),
        "review_manifest_sha256": (attestation.get("review_manifest") or {}).get("sha256"),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path.resolve()),
        "public_verify": str(public_path.resolve()),
        "public_verify_sha256": sha256_file(public_path),
        "authorized_by": (manifest.get("authorization") or {}).get("by"),
        "authorization_quote": (manifest.get("authorization") or {}).get("quote"),
    }
    _write_json_sidecar(uploaded_sidecar_path(manifest_path), uploaded)
    return 0, public_result


def make_manifest(args: argparse.Namespace) -> int:
    video, cover = Path(args.video), Path(args.cover)
    package_audit = Path(args.package_audit)
    for path in (video, cover, package_audit):
        if not path.is_file():
            print(f"REFUSE: missing artifact {path}", file=sys.stderr)
            return 2
    if not args.title.strip() or not args.quote.strip():
        print("REFUSE: --title and --quote (Ivan's authorization words) are required non-empty", file=sys.stderr)
        return 2
    audit_problems: list[str] = []
    audit = _load_json_object(package_audit, "package audit", audit_problems)
    package_root = Path(str(audit.get("root") or "")).resolve()
    if audit.get("passed") is not True:
        audit_problems.append("package audit did not pass")
    if not _zero_blocking_issues(audit):
        audit_problems.append("package audit reports blocking issues")
    if not package_root.is_dir():
        audit_problems.append(f"package audit root missing: {package_root}")
    if video.resolve().parent != package_root:
        audit_problems.append("video must be directly inside package audit root")
    record_sidecar = sidecar_record_path(video)
    subtitle_sidecar = sidecar_subtitle_path(video)
    review_manifest = package_root / "review_manifest.json"
    for path, label in (
        (record_sidecar, "same-stem record.json"),
        (subtitle_sidecar, "same-stem SRT"),
        (review_manifest, "review_manifest.json"),
    ):
        if not path.is_file():
            audit_problems.append(f"{label} missing: {path}")
    if audit_problems:
        for problem in audit_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2

    record = _load_json_object(record_sidecar, "record", audit_problems)
    if audit_problems:
        for problem in audit_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    tags_source = "cli" if tags else None
    upload_tags = record.get("upload_tags") or {}
    record_tags = [
        str(t).strip()
        for t in (upload_tags.get("final_tags") or [])
        if str(t).strip()
    ]
    if not tags:
        tags = record_tags
        tags_source = f"record.json:{upload_tags.get('engine') or '?'}"
    if args.no_tags:
        print("REFUSE: manifest v3 does not allow --no-tags", file=sys.stderr)
        return 2
    if not tags:
        print("REFUSE: manifest v3 requires non-empty record-bound tags", file=sys.stderr)
        return 2
    if tags != record_tags:
        print(
            "REFUSE: --tags must exactly match record.upload_tags.final_tags; "
            "update and re-audit the package instead of overriding reviewed metadata",
            file=sys.stderr,
        )
        return 2
    tag_problems = validate_tags(tags)
    if tag_problems:
        for problem in tag_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    print(f"tags: {len(tags)} from {tags_source}", file=sys.stderr)
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
        "manifest_version": 3,
        "schema_version": "authorized-upload-manifest.v3",
        "artifact_id": video_sha[:12],
        "video": {"path": str(video.resolve()), "sha256": video_sha, "bytes": video.stat().st_size},
        "cover": {"path": str(cover.resolve()), "sha256": sha256_file(cover), "bytes": cover.stat().st_size},
        "title": args.title,
        "description": DEFAULT_DESCRIPTION,
        "publish_policy": {
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
        },
        "season": season,
        "package_attestation": {
            "schema_version": "authorized-upload-package-attestation.v1",
            "package_root": str(package_root),
            "package_audit": _sha_entry(package_audit),
            "review_manifest": _sha_entry(review_manifest),
            "record": _sha_entry(record_sidecar),
            "subtitle": _sha_entry(subtitle_sidecar),
        },
        "authorization": {"by": args.authorized_by, "quote": args.quote, "at": now()},
        "created_at": now(),
        "tags": tags,
        "tags_source": tags_source,
    }
    package_problems = repair_binding.attach_package_recovery_publication_authority(manifest, record, review_manifest, video)
    package_problems.extend(human_review.attach_final_human_review(manifest, args.final_human_review, season_ids=EXPECTED_SEASON_IDS))
    package_problems.extend(_validate_v3_package_attestation(manifest, verify_hashes=True))
    if package_problems:
        for problem in package_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else video.with_suffix(".upload_manifest.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(out), "artifact_id": manifest["artifact_id"]}, ensure_ascii=False))
    return 0


def load_and_verify(manifest_path: Path, *, ordinary_upload: bool = False) -> tuple[dict | None, list[str]]:
    """(manifest, problems) — problems non-empty means REFUSE."""
    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"manifest unreadable: {exc}"]
    auth = manifest.get("authorization") or {}
    if not str(auth.get("quote") or "").strip() or not str(auth.get("by") or "").strip():
        problems.append("manifest carries no authorization (by+quote required)")
    title = str(manifest.get("title") or "")
    if not title.strip():
        problems.append("manifest has no title")
    else:
        title_lane = derive_season_lane(title)
        for code in publish_title_policy_violations(title, lane=title_lane):
            problems.append(f"manifest publish title violates {code}")
    if "season" in manifest:
        problems.extend(validate_season_block(manifest["season"]))
        if (
            isinstance(manifest.get("season"), dict)
            and manifest["season"].get("lane") != derive_season_lane(title)
        ):
            problems.append("manifest season lane contradicts the frozen title")
    if "tags" in manifest:
        problems.extend(validate_tags(manifest["tags"]))
    if manifest.get("manifest_version") == 3:
        if manifest.get("schema_version") != "authorized-upload-manifest.v3":
            problems.append("manifest v3 schema_version is invalid")
        if not manifest.get("tags"):
            problems.append("manifest v3 requires non-empty tags")
        if manifest.get("description") != DEFAULT_DESCRIPTION:
            problems.append("manifest v3 description drifted from the uploader contract")
        if manifest.get("publish_policy") != {
            "tid": EXPECTED_TID,
            "copyright": EXPECTED_COPYRIGHT,
            "source": EXPECTED_SOURCE,
        }:
            problems.append("manifest v3 publish_policy is invalid")
        problems.extend(_validate_v3_package_attestation(manifest, verify_hashes=True))
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
    if ordinary_upload:
        problems.extend(human_review.ordinary_upload_problems(manifest))
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
    posted_unverified: dict | None = None
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
                "package_audit_sha256",
                "record_sha256",
                "subtitle_sha256",
                "review_manifest_sha256",
            )
            changed = [
                key
                for key in stable_keys
                if key in started[attempt_id]
                and entry.get(key) != started[attempt_id].get(key)
            ]
            if changed:
                problems.append(f"upload ledger attempt {attempt_id} changed bound fields: {changed}")
            if isinstance(entry.get("rc"), bool) or not isinstance(entry.get("rc"), int):
                problems.append(f"upload ledger attempt {attempt_id} has no integer terminal rc")
            if entry.get("video_sha256") == video_sha256 and entry.get("rc") == 0:
                successful = entry
            elif (
                entry.get("video_sha256") == video_sha256
                and entry.get("uploader_rc") == 0
                and entry.get("bvid")
            ):
                posted_unverified = entry
        elif event == "UPLOAD_PUBLICATION_VERIFIED":
            if attempt_id not in started or attempt_id not in finished:
                problems.append(
                    f"upload ledger attempt {attempt_id} has a publication verification "
                    "without a completed upload attempt"
                )
                continue
            origin = started[attempt_id]
            changed = [
                key
                for key in (
                    "artifact_id",
                    "video_sha256",
                    "cover_sha256",
                    "manifest",
                    "manifest_sha256",
                    "uploader",
                    "package_audit_sha256",
                    "record_sha256",
                    "subtitle_sha256",
                    "review_manifest_sha256",
                )
                if key in origin and entry.get(key) != origin.get(key)
            ]
            if changed:
                problems.append(
                    f"upload ledger attempt {attempt_id} changed verified bound fields: {changed}"
                )
            if entry.get("rc") != 0:
                problems.append(
                    f"upload ledger attempt {attempt_id} publication verification is not rc=0"
                )
            if entry.get("video_sha256") == video_sha256:
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
    if posted_unverified is not None:
        return "posted_unverified", posted_unverified, []
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
        manifest, problems = load_and_verify(manifest_path, ordinary_upload=True)
        if problems:
            for p in problems:
                print(f"REFUSE: {p}", file=sys.stderr)
            return 2
        if manifest.get("manifest_version") != 3:
            print(
                "REFUSE: upload requires authorized-upload-manifest.v3; legacy manifests "
                "remain verify/season-add readable only",
                file=sys.stderr,
            )
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
        if guard_status == "posted_unverified":
            print(
                "REFUSE: this exact video already created a Bilibili archive but public "
                f"verification is incomplete (bvid={guard_row.get('bvid')}); run season-add, "
                "never re-upload it",
                file=sys.stderr,
            )
            return 6
        if guard_status == "uploaded":
            print(
                f"REFUSE: this exact video was already uploaded at {guard_row.get('at')}"
                f" (bvid={guard_row.get('bvid') or '?'}) — duplicate posts are the 充电器 incident; not repeating it",
                file=sys.stderr,
            )
            return 3
        quota_http, _ = _build_season_http(Path(args.cookie_json))
        quota_evidence, quota_problems = rolling_quota_guard(ledger, http=quota_http)
        if quota_problems:
            for problem in quota_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 8
        cmd = [args.uploader, manifest["video"]["path"], manifest["cover"]["path"], manifest["title"]]
        manifest_tags = manifest.get("tags") or []
        # v3 always has a reviewed, record-bound tag line.
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
            "manifest_version": manifest.get("manifest_version"),
            "package_audit_sha256": manifest["package_attestation"]["package_audit"]["sha256"],
            "record_sha256": manifest["package_attestation"]["record"]["sha256"],
            "subtitle_sha256": manifest["package_attestation"]["subtitle"]["sha256"],
            "review_manifest_sha256": manifest["package_attestation"]["review_manifest"]["sha256"],
            "quota_evidence": quota_evidence,
        }
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
                bvid = token.removeprefix("BVID=").strip("\"' ,")
        if completed.returncode != 0:
            quota_code = QUOTA_FREQUENCY_CODE if str(QUOTA_FREQUENCY_CODE) in output else None
            append_ledger(
                ledger,
                {
                    "event": "UPLOAD_ATTEMPT_FINISHED",
                    "at": now(),
                    **common_ledger_fields,
                    "uploader_rc": completed.returncode,
                    "rc": completed.returncode,
                    "bvid": bvid,
                    "quota_frequency_code": quota_code,
                },
            )
            if quota_code == QUOTA_FREQUENCY_CODE:
                print(
                    "BILIBILI QUOTA AUTHORITATIVE: code 21566; stop further uploads "
                    "until the rolling window frees",
                    file=sys.stderr,
                )
            print(
                f"ledger += artifact {manifest['artifact_id']} rc={completed.returncode} "
                f"bvid={bvid or '?'}"
            )
            return completed.returncode

    # The STARTED row deliberately remains unresolved until every public surface
    # passes.  Concurrent upload attempts therefore fail closed while this one
    # waits for transcode/season propagation.
    if args.skip_season:
        post_rc, public_result = 6, {
            "schema_version": "authorized-upload-public-verify.v2",
            "status": "SKIPPED_BY_EMERGENCY_FLAG",
            "bvid": bvid,
            "verified_at": now(),
        }
        _write_json_sidecar(
            public_verify_sidecar_path(manifest_path),
            public_result,
        )
    else:
        post_rc, public_result = _run_postpublish_verification(
            manifest,
            manifest_path,
            bvid,
            args,
            quota_evidence=quota_evidence,
        )
    if completed.returncode == 0 and not bvid:
        print(
            "LEDGER LEFT UNRESOLVED: uploader succeeded without a BVID; all further "
            "uploads stay blocked until Creator Center reconciliation",
            file=sys.stderr,
        )
        return 6
    with exclusive_upload_lock(lock_path):
        append_ledger(
            ledger,
            {
                "event": "UPLOAD_ATTEMPT_FINISHED",
                "at": now(),
                **common_ledger_fields,
                "uploader_rc": completed.returncode,
                "rc": post_rc,
                "bvid": bvid,
                "public_verify_status": (
                    public_result.get("status") if isinstance(public_result, dict) else None
                ),
                "public_verify_sha256": (
                    sha256_file(public_verify_sidecar_path(manifest_path))
                    if public_verify_sidecar_path(manifest_path).is_file()
                    else None
                ),
            },
        )
    print(f"ledger += artifact {manifest['artifact_id']} rc={post_rc} bvid={bvid or '?'}")
    return post_rc


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
    ledger = Path(args.ledger)
    guard_row: dict | None = None
    if not bvid:
        status, row, ledger_problems = ledger_guard(ledger, manifest["video"]["sha256"])
        if ledger_problems:
            for problem in ledger_problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 5
        if status not in {"uploaded", "posted_unverified"} or not row or not row.get("bvid"):
            print(
                "REFUSE: ledger has no successful upload with a bvid for this manifest's video; "
                "pass --bvid explicitly if the post exists",
                file=sys.stderr,
            )
            return 5
        bvid = str(row["bvid"])
        guard_row = row
    if manifest.get("manifest_version") == 3:
        rc, result = _run_postpublish_verification(manifest, manifest_path, bvid, args)
        if rc == 0 and guard_row and guard_row.get("event") == "UPLOAD_ATTEMPT_FINISHED":
            stable = {
                key: guard_row.get(key)
                for key in (
                    "attempt_id",
                    "artifact_id",
                    "video_sha256",
                    "cover_sha256",
                    "manifest",
                    "manifest_sha256",
                    "uploader",
                    "package_audit_sha256",
                    "record_sha256",
                    "subtitle_sha256",
                    "review_manifest_sha256",
                    "title",
                    "authorized_by",
                    "authorization_quote",
                    "tags",
                )
                if guard_row.get(key) is not None
            }
            append_ledger(
                ledger,
                {
                    "event": "UPLOAD_PUBLICATION_VERIFIED",
                    "at": now(),
                    **stable,
                    "rc": 0,
                    "bvid": bvid,
                    "public_verify_status": result.get("status") if result else None,
                    "public_verify_sha256": sha256_file(
                        public_verify_sidecar_path(manifest_path)
                    ),
                },
            )
        return rc
    return _run_season_step(manifest, manifest_path, bvid, args)


def _biliup_readonly_canary(cookie_json: Path, bvid: str) -> None:
    """Prove the append CLI login can read the target before APPEND_INTENT."""

    same_bv_live_verification.run_biliup_readonly_canary(
        cookie_json,
        bvid,
        validate_cookie=member_api.validate_biliup_cookie_file,
        biliup_bin=member_api.BILIUP_BIN,
        run=subprocess.run,
        repair_error=RepairError,
    )


def _same_bv_adapter(
    cookie_json: Path,
    biliup_cookie_json: Path,
    bvid: str | None = None,
) -> BilibiliRepairAdapter:
    """Build repair reads and biliup append from their explicit cookie files."""

    if bvid is not None:
        _biliup_readonly_canary(biliup_cookie_json, bvid)
    http, _csrf = _build_season_http(cookie_json)
    return BilibiliRepairAdapter(
        session=member_api.BiliSession(cookie_path=cookie_json, biliup_cookie_path=biliup_cookie_json),
        http=http,
        view_url=VIEW_API,
        tags_url=TAGS_API,
        section_url=SECTION_VIEW_API,
    )


def repair_plan(args: argparse.Namespace) -> int:
    """Freeze one exact existing-BV repair without changing remote state."""

    manifest_path = Path(args.manifest).resolve()
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK
    with exclusive_upload_lock(lock_path):
        manifest, problems = load_and_verify(manifest_path)
        problems.extend(repair_binding.repair_publication_target_problems(manifest or {}, args.bvid))
        if problems:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        assert manifest is not None
        adapter = _same_bv_adapter(
            Path(args.cookie_json),
            Path(args.biliup_cookie_json),
            args.bvid,
        )
        season = manifest.get("season") or {}
        section_id = season.get("section_id")
        if not isinstance(section_id, int):
            print(
                "REFUSE: repair manifest has no exact season section_id",
                file=sys.stderr,
            )
            return 2
        snapshot = adapter.observe(args.bvid, section_id)
        plan = create_same_bv_repair_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=args.bvid,
            snapshot=snapshot,
        )
        journal = Path(args.journal).resolve()
        assert_same_bv_unowned(
            journal,
            bvid=args.bvid,
            plan_id=str(plan["plan_id"]),
        )
        if args.dry_run:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        plan_path = Path(args.out).resolve()
        write_same_bv_repair_plan(plan_path, plan)
        initialise_same_bv_repair_journal(journal, plan_path, plan)
    print(
        json.dumps(
            {
                "status": "PLANNED",
                "bvid": args.bvid,
                "plan": str(plan_path),
                "journal": str(journal),
                "remote_mutation": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _load_repair_manifest(plan_path: Path) -> tuple[dict | None, dict | None, list[str]]:
    try:
        plan = load_same_bv_repair_plan(plan_path)
    except PlanInvalid as exc:
        return None, None, [str(exc)]
    manifest_path = Path(str((plan.get("manifest") or {}).get("path") or ""))
    manifest, problems = load_and_verify(manifest_path)
    if problems or manifest is None:
        return plan, manifest, problems
    problems.extend(repair_binding.validate_plan_problems(plan, manifest=manifest, plan_path=plan_path))
    return plan, manifest, problems


def repair_run(args: argparse.Namespace) -> int:
    """Resume a journaled same-BV transaction; never creates another BV."""

    plan_path = Path(args.plan).resolve()
    journal = Path(args.journal).resolve()
    lock_path = Path(args.lock) if args.lock else DEFAULT_UPLOAD_LOCK
    lock = nullcontext() if args.dry_run else exclusive_upload_lock(lock_path)
    with lock:
        plan, manifest, problems = _load_repair_manifest(plan_path)
        if problems or manifest is None or plan is None:
            for problem in problems:
                print(f"REFUSE: {problem}", file=sys.stderr)
            return 2
        if args.dry_run:
            result = preview_same_bv_repair(plan_path=plan_path, journal=journal, manifest=manifest)
        else:
            result = run_same_bv_repair(
                plan_path=plan_path,
                journal=journal,
                manifest=manifest,
                adapter=_same_bv_adapter(
                    Path(args.cookie_json),
                    Path(args.biliup_cookie_json),
                    str(plan["bvid"]),
                ),
                wait_seconds=args.wait,
                poll_seconds=args.poll,
            )
    print(
        json.dumps(
            {
                "state": result.state,
                "changed": result.changed,
                "message": result.message,
                "details": result.details,
                "dry_run": bool(args.dry_run),
            },
            ensure_ascii=False,
        )
    )
    if result.state == "VERIFIED":
        return 0
    if result.state == "BLOCKED_DRIFT":
        return 5
    return 6


def repair_status(args: argparse.Namespace) -> int:
    """Read and validate the local repair authority without remote writes."""

    return same_bv_live_verification.run_repair_status(
        args,
        load_repair_manifest=_load_repair_manifest,
        status_reader=same_bv_repair_status,
    )


def repair_verify_live(args: argparse.Namespace) -> int:
    """Freshly re-observe a VERIFIED repair and freeze a completed receipt."""

    return same_bv_live_verification.run_repair_verify_live(
        args,
        default_lock=DEFAULT_UPLOAD_LOCK,
        exclusive_lock=exclusive_upload_lock,
        load_repair_manifest=_load_repair_manifest,
        status_reader=same_bv_repair_status,
        journal_entries=repair_binding.plan_entries,
        adapter_factory=_same_bv_adapter,
        create_sidecar=_create_json_sidecar,
        sha256_file=sha256_file,
        now=now,
        observation_unavailable=repair_binding.ObservationUnavailable,
    )


def main(argv: list[str] | None = None) -> int:
    args = authorized_upload_cli_parser.parse_args(
        argv,
        description=__doc__ or "",
        handlers={
            "make_manifest": make_manifest,
            "upload": upload,
            "season_add": season_add,
            "verify": verify,
            "repair_plan": repair_plan,
            "repair_run": repair_run,
            "repair_status": repair_status,
            "repair_verify_live": repair_verify_live,
        },
        defaults={
            "ledger": DEFAULT_LEDGER,
            "repair_ledger": DEFAULT_REPAIR_LEDGER,
            "uploader": DEFAULT_UPLOADER,
            "cookie_json": DEFAULT_COOKIE_JSON,
            "biliup_cookie_json": DEFAULT_REPAIR_BILIUP_COOKIE_JSON,
        },
    )
    try:
        return args.func(args)
    except (UploadLockBusy, RepairError, CookieSchemaError) as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
