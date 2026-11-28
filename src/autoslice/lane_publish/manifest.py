"""The upload manifest: what goes online, every file bound by sha256 at the time it was made, and who authorized it.

A first publication's manifest carries the final tags step 7 chose; a revision's (`--final-human-review`) carries the package's
same-BV authority (record.json `recovery_publication_authority`, built by step 8 from the first-publication receipt), which
names the BV it may replace.  The collection is the channel profile's `season_routes` asset (route `talk`); a channel without
that collection stops here, before anything is sent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.autoslice.unresolved_findings_gate import unresolved_findings_problem
from src.autoslice.upload_tag_policy import apply_tag_policy

from . import authority, cover_rotation
from .common import GATE, REFUSED, Publish, Refused, bind, drift, now, read_json, write_json

SCHEMA = "lane-publish-manifest.v1"


ROUTE = "talk"       # the lane delivers talk clips; a song or a related-streamer clip is not this lane's


def _season() -> dict[str, Any]:
    """The channel's collection for the lane's clips, route `talk`: the profile's `season_routes` asset; the default channel
    without one has its routes where every other uploader reads them (clip_profile.season_ids); never another channel's."""
    import json

    from src.autoslice.channel_profile import DEFAULT_CHANNEL_PROFILE_ID, load_channel_profile

    channel = load_channel_profile(Path(__file__).resolve().parents[3])
    try:
        if "season_routes" in channel.asset_files or channel.profile_id != DEFAULT_CHANNEL_PROFILE_ID:
            block = json.loads(channel.asset_file("season_routes").read_text(encoding="utf-8"))["routes"][ROUTE]
            title = block["season_title"]
        else:     # the first real run refused the default channel, whose collection is clip_profile's own route
            from src.autoslice import clip_profile

            block, title = clip_profile.season_ids()[ROUTE], clip_profile.SEASON_TITLES[ROUTE]
        return {"lane": ROUTE, "season_title": str(title), "season_id": int(block["season_id"]),
                "section_id": int(block["section_id"]), "source": f"profile:{channel.profile_id}"}
    except (Exception, SystemExit) as exc:   # no route, a malformed asset, or (public build) no AUTOSLICE_SEASON_IDS
        raise Refused(REFUSED, f"频道 {channel.profile_id} 的 profile 没有给谈话切片的合集（season_routes 资产的 routes.{ROUTE}，"
                      f"要 season_title、season_id、section_id）：{type(exc).__name__}: {exc}") from exc


def make(*, video: Path, cover: Path, package_audit: Path, title: str, tags: str | None, final_human_review: Path | None,
         authorized_by: str, quote: str, out: Path) -> dict[str, Any]:
    package = video.absolute().parent
    name = package.name
    for path, suffix in ((video, ".mp4"), (cover, ".cover.png")):
        if path.absolute() != package / f"{name}{suffix}":
            raise Refused(REFUSED, f"{path} 不是成片包 {package} 里的 {name}{suffix}")
    audit = read_json(package_audit, "包审计")
    if audit.get("passed") is not True or Path(str(audit.get("root"))) != package:
        raise Refused(GATE, f"包审计没有通过，或审的不是这个包：passed={audit.get('passed')} root={audit.get('root')}")
    if not title.strip() or not authorized_by.strip() or not quote.strip():
        raise Refused(REFUSED, "标题、授权人、授权原话都不能为空")
    seal = read_json(package / "review_manifest.json", "封存清单")
    record_path = package / f"{name}.record.json"
    record = read_json(record_path, "record.json")
    if doubt := unresolved_findings_problem(record):
        raise Refused(GATE, doubt)
    sealed_title = (package / f"{name}.title.txt").read_text(encoding="utf-8").rstrip("\n")
    if title != sealed_title:
        raise Refused(GATE, f"要上的标题「{title}」不是包里封存的标题「{sealed_title}」")
    attestation: dict[str, Any] = {"package_root": str(package), "package_audit": bind(package_audit),
                                   "review_manifest": bind(package / "review_manifest.json"), "record": bind(record_path)}
    doc: dict[str, Any] = {
        "schema_version": SCHEMA, "candidate_id": str(seal.get("candidate_id") or name),
        "recording_date": str(record.get("recording_date") or seal.get("date") or ""),
        "video": bind(video), "cover": bind(cover), "title": title,
        "description": (package / f"{name}.description.txt").read_text(encoding="utf-8").strip(),
        "season": _season(), "package_attestation": attestation,
        "authorization": {"by": authorized_by, "quote": quote, "at": now()}, "created_at": now()}
    if final_human_review is None and (selection := cover_rotation.freeze(out.absolute(), doc)):
        doc["cover_rotation"] = selection
    if doc["candidate_id"] != name:
        raise Refused(GATE, f"封存清单的 candidate_id「{doc['candidate_id']}」不是包名「{name}」")
    if final_human_review is None:
        raw_tags = [t.strip() for t in (tags or "").split(",")] if (tags or "").strip() else []
        if not raw_tags:
            raise Refused(REFUSED, "首发要有标签（--tags）")
        record_tags = (record.get("upload_tags") or {}).get("final_tags")
        settled = apply_tag_policy(
            raw_tags,
            previous=record_tags if isinstance(record_tags, list) else None,
            on_overflow="refuse",
        )
        if not settled.ok:
            raise Refused(REFUSED, f"标签不符合策略（{'；'.join(settled.reasons)}）：{raw_tags}")
        doc["tags"] = list(settled.tags)
        # what the new BV is posted with (upload refuses a manifest whose policy is no longer the rule's, and reads the
        # public page back against it); a revision posts none and leaves the published archive's own zone, type and source
        doc["publish_policy"] = Publish.load().archive_policy()
    else:
        review = read_json(final_human_review, "成品审查回执")
        if review.get("verdict") != "PASS":
            raise Refused(GATE, f"成品审查回执的结论是 {review.get('verdict')!r}，不是 PASS")
        attestation["final_human_review"] = bind(final_human_review)
        doc["tags"] = list((record.get("upload_tags") or {}).get("final_tags") or [])
        try:
            doc["recovery_publication_authority"] = authority.validate(
                record.get("recovery_publication_authority"), candidate_id=name, expected_final_title=title)
        except authority.AuthorityError as exc:
            raise Refused(GATE, f"修订包的同稿件授权不成立（record.json recovery_publication_authority）：{exc}") from exc
    write_json(out, doc)
    return doc


def problems(doc: dict[str, Any]) -> list[str]:
    """Why the manifest no longer describes the files it bound ([] when it does)."""
    found = [p for p in (drift(doc.get("video"), "视频"), drift(doc.get("cover"), "封面")) if p]
    attestation = doc.get("package_attestation") or {}
    found += [p for key in ("package_audit", "review_manifest", "record", "final_human_review") if key in attestation
              for p in [drift(attestation[key], key)] if p]
    if doc.get("schema_version") != SCHEMA:
        found.append(f"清单 schema 是 {doc.get('schema_version')!r}，不是 {SCHEMA}")
    if not isinstance(doc.get("season"), dict) or not doc["season"].get("section_id"):
        found.append("清单没有合集")


    record = (attestation.get("record") or {}).get("path")
    if record and not found and (doubt := unresolved_findings_problem(read_json(Path(record), "record.json"))):
        found.append(doubt)
    return found


def load_checked(path: Path) -> dict[str, Any]:
    doc = read_json(path, "上传清单")
    if found := problems(doc):
        raise Refused(GATE, "上传清单与它绑定的文件对不上：" + "；".join(found))
    return doc
