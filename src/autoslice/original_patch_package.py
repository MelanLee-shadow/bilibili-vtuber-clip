"""Native technical closure for original-reviewed, fixed-interval corrections.

This is not a fresh human/perceptual review. The original transcript and explicit
patches remain the text authority; native audio, render and metadata witnesses
are independently checked. Only a committed, existing-BV target may use it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.fastlane_original_patch import load_original_patch
from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.subtitle_validation import validate_srt_file

MANIFEST_SCHEMA = "original-reviewed-fastlane-package.v1"
CONTRACT_SCHEMA = "original-reviewed-fastlane-closure.v1"
TARGET_SCHEMA = "original-fastlane-same-bv-target.v1"
AUTHORITY_SCHEMA = "original-fastlane-authorized-same-bv.v1"
TECHNICAL_SCHEMA = "original-fastlane-delta-technical-review.v1"
ROOT = Path(__file__).resolve().parents[2]
_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


class OriginalPackageError(ValueError):
    pass


def need(value, message):
    if not value:
        raise OriginalPackageError(message)


def sha_file(path):
    path = Path(path)
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink input")
    before = path.stat()
    need(stat.S_ISREG(before.st_mode), "input is not a file")
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    after = path.stat()

    def stable(s):
        return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    need(stable(before) == stable(after), "input changed while reading")
    return h.hexdigest()


def json_file(path):
    path = Path(path)
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink JSON input")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(
            stat.S_ISREG(before.st_mode) and before.st_size <= 8 * 1024 * 1024,
            "oversized/nonregular JSON",
        )
        chunks = []
        total = 0
        while block := os.read(fd, 1024 * 1024):
            total += len(block)
            need(total <= 8 * 1024 * 1024, "JSON grew beyond bound")
            chunks.append(block)
        after = os.fstat(fd)

        def stable(info):
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        need(stable(before) == stable(after) == stable(path.lstat()), "JSON changed during read")
    finally:
        os.close(fd)
    obj = json.loads(b"".join(chunks))
    need(isinstance(obj, dict), "JSON object required")
    return obj


def within(root, name):
    need(isinstance(name, str) and bool(name), "missing relative path")
    rel = Path(name)
    need(
        not rel.is_absolute() and ".." not in rel.parts and "." not in rel.parts,
        "unsafe relative path",
    )
    p = root / rel
    need(p.resolve().is_relative_to(root.resolve()), "path escapes package")
    sha_file(p)
    return p


def sealed_asset(repo_root, candidate, suffix):
    need(isinstance(candidate, str) and _ID.fullmatch(candidate), "candidate id")
    folder = load_channel_profile(repo_root).asset_directory("reviewed_subtitle_baselines")
    path = folder / (candidate + suffix)
    raw = path.read_bytes()
    require_repository_asset_authority(
        repo_root=repo_root, relative_path=path.relative_to(repo_root), observed_bytes=raw
    )
    result = json.loads(raw)
    need(
        isinstance(result, dict) and result.get("candidate_id") == candidate,
        "asset candidate drift",
    )
    return result, hashlib.sha256(raw).hexdigest()


def publication_authority(candidate, *, repo_root=ROOT):
    target, seal = sealed_asset(repo_root, candidate, ".original-repair-target.v1.json")
    need(
        set(target)
        == {
            "schema_version",
            "candidate_id",
            "recording_date",
            "bvid",
            "aid",
            "cid",
            "old_title",
            "final_title",
            "public_snapshot_sha256",
            "authorization_ref",
        },
        "target fields",
    )
    need(target["schema_version"] == TARGET_SCHEMA, "target schema")
    need(re.fullmatch(r"BV[A-Za-z0-9]{10}", target["bvid"]), "target BV")
    need(
        all(type(target[k]) is int and target[k] > 0 for k in ["aid", "cid"]),
        "target numeric identity",
    )
    from src.autoslice.title_policy import publish_title_policy_violations

    need(
        not publish_title_policy_violations(target["final_title"], lane="talk"),
        "target title policy",
    )
    reference = target["authorization_ref"]
    need(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "operator reference")
    p = within(repo_root, reference["path"])
    need(sha_file(p) == reference["sha256"], "operator source drift")
    require_repository_asset_authority(
        repo_root=repo_root, relative_path=p.relative_to(repo_root), observed_bytes=p.read_bytes()
    )
    return {
        "schema_version": AUTHORITY_SCHEMA,
        "candidate_id": candidate,
        "bvid": target["bvid"],
        "aid": target["aid"],
        "cid": target["cid"],
        "recording_date": target["recording_date"],
        "observed_public_title": target["old_title"],
        "title": target["final_title"],
        "final_title": target["final_title"],
        "authority_sha256": "sha256:" + seal,
        "same_bv_only": True,
    }


def validate_publication(value, *, candidate_id, expected_final_title=None, repo_root=ROOT):
    expected = publication_authority(candidate_id, repo_root=repo_root)
    need(value == expected, "original-patch publication authority drift")
    need(
        expected_final_title is None or expected_final_title == expected["final_title"],
        "publication title drift",
    )
    return expected


def _pcm_hash(video):
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "pcm_s16le",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        # Audit callers may carry JSON or a live PTY on stdin. Never consume
        # that stream as FFmpeg hotkeys (q silently returns a partial hash).
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )
    need(proc.returncode == 0, "audio decode failed")
    result = proc.stdout.strip()
    need(re.fullmatch(r"SHA256=[a-f0-9]{64}", result), "audio hash output invalid")
    return result


def _ass_milliseconds(value):
    need(
        isinstance(value, str) and re.fullmatch(r"[0-9]+:[0-5][0-9]:[0-5][0-9]\.[0-9]{2}", value),
        "invalid native ASS timestamp",
    )
    hours, minutes, rest = value.split(":")
    seconds, centiseconds = rest.split(".")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(centiseconds) * 10


def verify_original_continuity(root, contract, item, record, native, replay):
    """Preserve the actual previous media, rather than demand a fictitious recut.

    This lane never selects a new source interval. It reuses the very same
    main-video bytes named by the immutable prior record and native render
    intent. Both original and corrected complete soundtracks are decoded.
    """
    evidence = contract["evidence"]
    before_path = within(root, evidence["old_record"])
    before = json_file(before_path)
    intent = json_file(within(root, evidence["render_intent"]))
    interval = contract["source_interval_ms"]
    need(
        isinstance(interval, list)
        and len(interval) == 2
        and all(type(v) is int for v in interval)
        and 0 <= interval[0] < interval[1],
        "invalid original main interval",
    )
    need(
        [record["start_ms"], record["end_ms"]]
        == [before["start_ms"], before["end_ms"]]
        == interval,
        "original reviewed window changed",
    )
    need(
        record["duration_ms"] == before["duration_ms"] == interval[1] - interval[0],
        "original main duration changed",
    )
    original_input = within(root, evidence["original_review_input"])
    need(
        sha_file(original_input) == replay[1]["original_review_input_sha256"],
        "actual reviewed original not preserved",
    )
    main = within(root, evidence["source_main"])
    expected_main = contract["source_main_sha256"]
    need(
        sha_file(main)
        == expected_main
        == intent["raw_main_sha256"]
        == before["artifact_hashes"]["video_sha256"].removeprefix("sha256:")
        == record["artifact_hashes"]["video_sha256"].removeprefix("sha256:"),
        "source main bytes changed",
    )
    need(
        intent["candidate_id"] == item["candidate_id"]
        and intent["corrected_srt_sha256"] == replay[1]["release_srt_sha256"],
        "native render intent targets different correction",
    )
    frozen = intent["frozen_published_inputs"]
    need(
        isinstance(frozen, dict)
        and frozen.get(intent["branding_authority"]["record_path"]) == sha_file(before_path),
        "native render prior-record preimage changed",
    )
    old_video = within(root, evidence["old_video"])
    old_sha = before["artifact_hashes"]["burned_video_sha256"].removeprefix("sha256:")
    need(
        sha_file(old_video)
        == old_sha
        == intent["branding_authority"]["burned_video_sha256"].removeprefix("sha256:")
        and old_sha in frozen.values(),
        "prior published video changed",
    )
    need(
        _pcm_hash(old_video) == contract["audio_pcm_sha256"],
        "old published audio no longer matches verified audio",
    )
    old_intro = before["burned_preview"]["branding_intro"]
    new_intro = record["burned_preview"]["branding_intro"]
    for key in ["intro_id", "intro_media_sha256", "intro_offset_ms"]:
        need(
            old_intro[key] == new_intro[key] == intent["branding_authority"]["branding_intro"][key],
            "original intro changed",
        )
    return before


def verify_original_display_events(cues, events):
    """Rebuild presentation groups without equating ASS rows with source cues."""
    from src.autoslice.subtitle_rendering import _layout_cue_sequence_for_display

    expected = _layout_cue_sequence_for_display(
        [(cue.start_ms, cue.end_ms, cue.text) for cue in cues]
    )
    need(len(events) == len(expected), "ASS display group count drift")

    def clean(text):
        # Whitespace is cosmetic; explicit physical line breaks are not.
        return re.sub(r"\s+", "", text.replace("\n", r"\N"))

    for event, (_, start_ms, end_ms, text) in zip(events, expected, strict=True):
        need(
            len(event) == 10 and clean(event[-1]) == clean(text),
            "burned caption wording or word boundary drift",
        )
        need(
            abs(_ass_milliseconds(event[1]) - start_ms) < 10
            and abs(_ass_milliseconds(event[2]) - end_ms) < 10,
            "burned caption group timing drift",
        )


def _verify_source_and_presentation(root, contract, item, record, native, replay):
    evidence = contract["evidence"]
    before = verify_original_continuity(root, contract, item, record, native, replay)
    ass = within(root, item["ass_path"])
    need(
        sha_file(ass)
        == native["ass_sha256"]
        == record["artifact_hashes"]["ass_sha256"].removeprefix("sha256:"),
        "ASS not native render input",
    )
    from src.autoslice.jingting_chunker import parse_srt_cues

    cues = parse_srt_cues(within(root, item["subtitle_srt"]).read_text())
    events = [
        row.split(",", 9) for row in ass.read_text().splitlines() if row.startswith("Dialogue:")
    ]
    verify_original_display_events(cues, events)
    before = json_file(within(root, evidence["old_record"]))
    need(
        sha_file(within(root, item["cover"]))
        == before["artifact_hashes"]["cover_sha256"].removeprefix("sha256:"),
        "old cover bytes not preserved",
    )
    old_subtitle = within(root, evidence["old_subtitle"])
    need(
        sha_file(old_subtitle)
        == before["artifact_hashes"]["subtitle_sha256"].removeprefix("sha256:"),
        "published preimage subtitle drift",
    )
    need(
        native["burned_preview"]["branding_intro"]["verification"]["full_decode"] == "clean",
        "native full decode missing",
    )


def verify_publish_artifact_bindings(root, item, record):
    """Hash agreement is insufficient if the reviewed draft and record disagree."""
    hashes = record.get("artifact_hashes")
    staging = record.get("publish_staging")
    need(isinstance(hashes, dict) and isinstance(staging, dict), "missing record artifact bindings")
    primary = {
        "burned_video_sha256": "media",
        "subtitle_sha256": "subtitle_srt",
        "ass_sha256": "ass_path",
        "cover_sha256": "cover",
    }
    actual = {}
    for key, role in primary.items():
        path = within(root, item.get(role))
        actual[key] = "sha256:" + sha_file(path)
        need(hashes.get(key) == actual[key], "record artifact drift: " + key)
    draft_path = within(root, item.get("publish_json"))
    need(
        hashes.get("publish_draft_sha256") == "sha256:" + sha_file(draft_path),
        "record publish draft hash drift",
    )
    draft = json_file(draft_path)
    need(
        draft.get("title") == item.get("title") == staging.get("title"),
        "publish draft title differs from reviewed title",
    )
    need(
        isinstance(draft.get("source_fact_review"), dict)
        and draft["source_fact_review"] == staging.get("source_fact_review"),
        "publish draft source facts differ from reviewed facts",
    )
    need(
        isinstance(draft.get("artifact_hashes"), dict)
        and all(draft["artifact_hashes"].get(key) == value for key, value in actual.items()),
        "publish draft artifact bindings differ from actual bytes",
    )
    raw_cover = draft.get("cover_path")
    need(isinstance(raw_cover, str) and bool(raw_cover), "publish cover locator missing")
    cover = Path(raw_cover)
    cover = cover if cover.is_absolute() else root / cover
    expected_cover = within(root, item["cover"])
    need(cover.absolute() == expected_cover.absolute(), "publish cover locator points elsewhere")


def _verify_metadata(root, contract, item, record, candidate, *, repo_root):
    authority = publication_authority(candidate, repo_root=repo_root)
    verify_publish_artifact_bindings(root, item, record)
    need(
        item.get("recovery_publication_authority")
        == record.get("recovery_publication_authority")
        == authority,
        "record/review target missing or different",
    )
    need(
        item["title"] == record["publish_staging"]["title"] == authority["final_title"],
        "title mismatch",
    )
    old_public = json_file(within(root, contract["evidence"]["public_before"]))
    snapshot = old_public.get("snapshot", {})
    need(
        old_public.get("read_only") is True
        and old_public.get("account", {}).get("isLogin") is True,
        "public identity has no read-only login proof",
    )
    public, creator, section = (snapshot.get(k, {}) for k in ("public", "creator", "section"))
    for surface in (public, creator):
        need(
            surface.get("available") is True
            and surface.get("state") == 0
            and surface.get("bvid") == authority["bvid"]
            and surface.get("aid") == authority["aid"],
            "before-public identity mismatch",
        )
        need(
            surface.get("metadata", {}).get("title") == authority["observed_public_title"],
            "before title mismatch",
        )
    videos = creator.get("videos", [])
    need(
        len(videos) == 1 and videos[0]["cid"] == public.get("cid") == authority["cid"],
        "before CID mismatch",
    )
    need(
        section.get("available") is True and len(section.get("matches", [])) == 1,
        "before section ambiguity",
    )
    need(section["matches"][0]["bvid"] == authority["bvid"], "wrong section member")
    target, _ = sealed_asset(repo_root, candidate, ".original-repair-target.v1.json")
    need(
        sha_file(within(root, contract["evidence"]["public_before"]))
        == target["public_snapshot_sha256"],
        "public snapshot is not operator target source",
    )
    from src.autoslice.source_fact_review import source_fact_review_passes

    fact = json_file(within(root, contract["evidence"]["source_fact"]))
    need(
        source_fact_review_passes(fact) and fact.get("final_title") == item["title"],
        "fresh title facts not accepted",
    )
    need(
        record["publish_staging"]["source_fact_review"] == fact,
        "record source-fact projection changed",
    )
    # This is the same native joint-check validator used at the uploader.
    from scripts.authorized_upload import (
        _sha_entry,
        _attach_title_cover_qc,
        _title_cover_qc_attestation_problems,
    )

    check = {
        "title": item["title"],
        "cover": _sha_entry(within(root, item["cover"])),
        "package_attestation": {
            "package_root": str(root),
            "record": _sha_entry(within(root, item["record"])),
        },
    }
    errors = _attach_title_cover_qc(check, str(within(root, contract["evidence"]["joint_qc"])))
    errors += _title_cover_qc_attestation_problems(check, required=True)
    need(not errors, "title/cover witness rejected: " + "; ".join(errors))
    return authority


def validate_package(root, manifest=None, *, repo_root=ROOT):
    root = Path(root).resolve()
    review = manifest if manifest is not None else json_file(root / "review_manifest.json")
    need(isinstance(review, dict), "review manifest must be object")
    need(
        review.get("schema_version") == MANIFEST_SCHEMA and review.get("upload_allowed") is False,
        "not an original-patch no-new-upload package",
    )
    need(
        isinstance(review.get("items"), list) and len(review["items"]) == 1,
        "one candidate per correction",
    )
    item = review["items"][0]
    need(isinstance(item, dict), "review item must be object")
    candidate = item.get("candidate_id")
    contract, seal = sealed_asset(repo_root, candidate, ".original-delivery-closure.v1.json")
    need(
        set(contract)
        == {
            "schema_version",
            "candidate_id",
            "files",
            "evidence",
            "original_recipe_sha256",
            "source_main_sha256",
            "source_interval_ms",
            "audio_pcm_sha256",
            "intro_offset_ms",
            "final_duration_ms",
        },
        "closure fields",
    )
    need(contract["schema_version"] == CONTRACT_SCHEMA, "closure schema")
    required_evidence = {
        "render",
        "render_intent",
        "source_main",
        "original_review_input",
        "old_video",
        "pcm_audio",
        "public_before",
        "source_fact",
        "joint_qc",
        "old_record",
        "old_subtitle",
    }
    need(
        isinstance(contract["evidence"], dict) and set(contract["evidence"]) == required_evidence,
        "missing or extra technical evidence",
    )
    need(
        isinstance(contract["files"], dict)
        and all(
            isinstance(name, str) and name in contract["files"]
            for name in contract["evidence"].values()
        ),
        "technical evidence is not sealed",
    )

    need(
        isinstance(contract["files"], dict) and 12 <= len(contract["files"]) <= 100,
        "incomplete closure files",
    )
    for name, entry in contract["files"].items():
        path = within(root, name)
        need(
            set(entry) == {"sha256", "bytes"}
            and type(entry["bytes"]) is int
            and entry["bytes"] > 0,
            "file seal shape",
        )
        need(
            path.stat().st_size == entry["bytes"] and sha_file(path) == entry["sha256"],
            "closure file drift: " + name,
        )
    required = ["media", "subtitle_srt", "ass_path", "record", "publish_json", "cover"]
    for role in required:
        need(item.get(role) in contract["files"], "unsealed primary artifact: " + role)
    record = json_file(within(root, item["record"]))
    need(
        record.get("candidate_id") == candidate
        and record.get("schema_version") == "original-reviewed-fastlane-record.v1",
        "record candidate/type drift",
    )
    originals = load_channel_profile(repo_root).asset_directory("reviewed_subtitle_baselines")
    replay = load_original_patch(repo_root, originals, candidate)
    need(
        replay is not None and replay[1]["recipe_sha256"] == contract["original_recipe_sha256"],
        "original recipe drift",
    )
    subtitle = within(root, item["subtitle_srt"])
    need(
        subtitle.read_bytes() == replay[0] and validate_srt_file(subtitle)["status"] == "PASS",
        "original+patch text drift",
    )
    native = json_file(within(root, contract["evidence"]["render"]))
    intent = json_file(within(root, contract["evidence"]["render_intent"]))
    video = within(root, item["media"])
    need(
        native["final_srt_sha256"] == sha_file(subtitle)
        and native["final_video_sha256"] == sha_file(video),
        "native render hash drift",
    )
    need(
        native.get("source_and_prior_published_files_unchanged") is True
        and native.get("old_intro_preserved") is True,
        "native render did not preserve prior inputs",
    )
    need(intent["raw_main_sha256"] == contract["source_main_sha256"], "raw source changed")
    from src.autoslice.producer_media import _validated_burned_artifact

    # Portable relocation changes only in-memory locators. The stored record
    # remains frozen and its hashes are checked above.
    bound_record = dict(record)
    bound_record["burned_preview"] = {**record["burned_preview"], "path": str(video)}
    need(_validated_burned_artifact(bound_record).resolve() == video, "native final burn not bound")
    _verify_source_and_presentation(root, contract, item, record, native, replay)
    from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check

    validate_final_subtitle_audio_check(
        record, final_srt=subtitle, actual_media=video, package_root=root
    )
    intro = record["burned_preview"]["branding_intro"]
    need(
        intro["intro_offset_ms"]
        == contract["intro_offset_ms"]
        == native["burned_preview"]["branding_intro"]["intro_offset_ms"],
        "intro offset drift",
    )
    need(
        intro["intro_media_sha256"]
        == native["burned_preview"]["branding_intro"]["intro_media_sha256"],
        "intro bytes changed",
    )
    need(
        _pcm_hash(video) == contract["audio_pcm_sha256"],
        "complete decoded audio differs from original",
    )
    pcm = json_file(within(root, contract["evidence"]["pcm_audio"]))
    need(
        pcm["audio_pcm_byte_identical"] is True
        and pcm["decoded_complete_audio_sha256_old"]
        == pcm["decoded_complete_audio_sha256_new"]
        == contract["audio_pcm_sha256"],
        "original audio witness drift",
    )
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    need(probe.returncode == 0, "media probe failed")
    need(
        round(float(json.loads(probe.stdout)["format"]["duration"]) * 1000)
        == contract["final_duration_ms"],
        "duration changed",
    )
    authority = _verify_metadata(root, contract, item, record, candidate, repo_root=repo_root)
    return {
        "candidate_id": candidate,
        "closure_sha256": seal,
        "authority": authority,
        "original_review_input_sha256": replay[1]["original_review_input_sha256"],
        "release_srt_sha256": sha_file(subtitle),
        "video_sha256": sha_file(video),
        "checked_cue_count": replay[1]["cue_count"],
        "local_patch_count": len(replay[1]["changed_cues"]),
        "new_ASR_text_used": False,
        "full_fresh_human_playback_claimed": False,
        "technical_review_kind": "original_owner_review_plus_verified_delta",
    }


def audit_if_original(root, manifest):
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        return None
    try:
        validate_package(root, manifest)
        return []
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        return [
            {
                "code": "ORIGINAL_REVIEW_PATCH_CLOSURE_INVALID",
                "severity": "ERROR",
                "path": str(root),
                "detail": str(exc),
            }
        ]
