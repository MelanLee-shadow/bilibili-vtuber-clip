"""One sealed private-package adapter for fastlane C1.

This is intentionally *not* a second generic subtitle/redelivery lane.  C1 is
the one historical candidate whose reviewed successor is a 36-source-cue
partition with eleven watched-video drops, three exact replacements, and one
inserted live cue.  The adapter packages that fixed successor for delegated
root technical review only.  It cannot create a new BV, upload, or certify a
root review.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.subtitle_validation import validate_srt_file


ROOT = Path(__file__).resolve().parents[2]
CID = "auto_173005_934_1166"
RECORDING_DATE = "2026-08-11"
TITLE = "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"

FORMAL_AUTHORITY_RELATIVE = Path(
    "assets/lidousha/fastlane_c1_private/"
    "auto_173005_934_1166.formal-adapter.v1.json"
)
CORRECTION_AUTHORITY_RELATIVE = Path(
    "assets/lidousha/fastlane_c1_private/"
    "auto_173005_934_1166.subtitle-correction.v1.json"
)

FORMAL_MANIFEST_SCHEMA = "fastlane-c1-formal-private-review-manifest.v1"
FORMAL_RECORD_SCHEMA = "fastlane-c1-formal-private-record.v1"
FORMAL_PUBLISH_SCHEMA = "fastlane-c1-formal-private-publish.v1"
FORMAL_RECEIPT_SCHEMA = "fastlane-c1-formal-private-adapter-receipt.v1"
RULING_SEAL_SCHEMA = "fastlane-c1-ruling-seal.v1"
TECHNICAL_TEMPLATE_SCHEMA = "fastlane-c1-delegated-root-technical-review-template.v1"
ROOT_TECHNICAL_STATUS = "ROOT_TECHNICAL_RECEIPT_REQUIRED"

SIX_NAMED_POINTS = (
    {
        "point_id": "c1-01-0046-weioula",
        "public_anchor": "~00:46",
        "source_cue": 6,
        "expected": "薇欧拉酱只是喜欢阿拉蕾酱而已",
    },
    {
        "point_id": "c1-02-0101-watched-video-absent",
        "public_anchor": "~01:01",
        "source_cues": [9, 10, 11, 12, 13, 14],
        "expected": "ABSENT_WATCHED_VIDEO_SPEECH",
    },
    {
        "point_id": "c1-03-0112-zhubao",
        "public_anchor": "~01:12",
        "source_cue": 16,
        "expected": "主包给练舞室的姐姐们推荐",
    },
    {
        "point_id": "c1-04-0137-laile",
        "public_anchor": "~01:37",
        "source_interval_ms": [91100, 91900],
        "expected": "来了",
    },
    {
        "point_id": "c1-05-0200-watched-video-absent",
        "public_anchor": "~02:00",
        "source_cues": [26, 27, 28],
        "expected": "ABSENT_WATCHED_VIDEO_SUBTITLES",
    },
    {
        "point_id": "c1-06-0226-daisuki",
        "public_anchor": "~02:26",
        "source_cue": 25,
        "expected": "daisukino ararei chan",
    },
)


class FastlaneC1FormalAdapterError(ValueError):
    """A C1-only private package is incomplete, drifted, or unsafe."""


@dataclass(frozen=True)
class C1AuditIssue:
    code: str
    detail: str = ""
    path: Path | None = None


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes, *, prefixed: bool = True) -> str:
    digest = hashlib.sha256(value).hexdigest()
    return f"sha256:{digest}" if prefixed else digest


def sha256_file(path: Path, *, prefixed: bool = True) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    return f"sha256:{value}" if prefixed else value


def _require_regular(path: Path, *, label: str) -> Path:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FastlaneC1FormalAdapterError(f"C1_{label}_MISSING") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise FastlaneC1FormalAdapterError(f"C1_{label}_NOT_REGULAR")
    return path


def _contained(root: Path, relative: str, *, label: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or ".." in raw.parts:
        raise FastlaneC1FormalAdapterError(f"C1_{label}_PATH_INVALID")
    root = root.resolve(strict=True)
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        try:
            metadata = cursor.lstat()
        except OSError as exc:
            raise FastlaneC1FormalAdapterError(f"C1_{label}_MISSING") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise FastlaneC1FormalAdapterError(f"C1_{label}_PATH_UNSAFE")
    try:
        resolved = cursor.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise FastlaneC1FormalAdapterError(f"C1_{label}_PATH_UNSAFE") from exc
    return _require_regular(resolved, label=label)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _require_regular(path, label=label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FastlaneC1FormalAdapterError(f"C1_{label}_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise FastlaneC1FormalAdapterError(f"C1_{label}_JSON_INVALID")
    return value


def _expect_sha256(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise FastlaneC1FormalAdapterError(code)
    raw = value.removeprefix("sha256:")
    if len(raw) != 64 or any(character not in "0123456789abcdef" for character in raw):
        raise FastlaneC1FormalAdapterError(code)
    return raw


def _require_exact_keys(value: Mapping[str, object], expected: set[str], *, code: str) -> None:
    if set(value) != expected:
        raise FastlaneC1FormalAdapterError(code)


def _authority_sha256(value: Mapping[str, object]) -> str:
    unsigned = dict(value)
    unsigned.pop("authority_sha256", None)
    return _sha256_bytes(_canonical_bytes(unsigned))


def _expected_constraint() -> dict[str, bool]:
    return {
        "requires_same_bv_repair": True,
        "forbid_new_bv": True,
        "preserve_public_identity_and_metadata": True,
    }


def _validate_formal_authority(value: Mapping[str, object], *, repo_root: Path) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "candidate_id",
        "recording_date",
        "title",
        "correction_authority",
        "operator_ruling",
        "predecessor",
        "successor_grid",
        "branding_intro",
        "public_identity",
        "same_bv_delivery_constraint",
        "output_names",
        "authority_sha256",
    }
    _require_exact_keys(value, expected_fields, code="C1_FORMAL_AUTHORITY_FIELDS_INVALID")
    if (
        value.get("schema_version") != "fastlane-c1-formal-adapter-authority.v1"
        or value.get("candidate_id") != CID
        or value.get("recording_date") != RECORDING_DATE
        or value.get("title") != TITLE
        or value.get("same_bv_delivery_constraint") != _expected_constraint()
        or value.get("authority_sha256") != _authority_sha256(value)
    ):
        raise FastlaneC1FormalAdapterError("C1_FORMAL_AUTHORITY_BINDING_INVALID")

    correction = value.get("correction_authority")
    if not isinstance(correction, Mapping) or dict(correction).get("relative_path") != CORRECTION_AUTHORITY_RELATIVE.as_posix():
        raise FastlaneC1FormalAdapterError("C1_CORRECTION_AUTHORITY_BINDING_INVALID")
    correction_path = repo_root / CORRECTION_AUTHORITY_RELATIVE
    if sha256_file(_require_regular(correction_path, label="CORRECTION_AUTHORITY")) != correction.get("sha256"):
        raise FastlaneC1FormalAdapterError("C1_CORRECTION_AUTHORITY_HASH_DRIFT")
    correction_document = _read_json(correction_path, label="CORRECTION_AUTHORITY")

    ruling = value.get("operator_ruling")
    if not isinstance(ruling, Mapping):
        raise FastlaneC1FormalAdapterError("C1_RULING_AUTHORITY_INVALID")
    if (
        ruling.get("raw_line947_sha256") != "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
        or ruling.get("raw_line947_content_sha256")
        != "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b"
        or ruling.get("candidate_id") != CID
        or ruling.get("candidate_scope_ordinal") != 1
        or ruling.get("document_binding_mode") != "RESEAL_AT_INTEGRATION"
        or ruling.get("ivan_rereview_required") is not False
        or not isinstance(ruling.get("raw_payload_required_fragments"), list)
        or not isinstance(ruling.get("ruling_document_required_fragments"), list)
    ):
        raise FastlaneC1FormalAdapterError("C1_RULING_AUTHORITY_INVALID")

    predecessor = value.get("predecessor")
    expected_predecessor = {
        "video": f"{CID}.recut.mp4",
        "srt": f"{CID}.recut.srt",
        "ass": f"{CID}.recut.final-sapphire72.ass",
        "record": f"{CID}.record.json",
        "publish": f"{CID}.recut.publish.json",
        "cover": f"{CID}.recut.burned-final-sapphire72.cover.png",
        "review_manifest": "review_manifest.json",
    }
    if not isinstance(predecessor, Mapping) or set(predecessor) != set(expected_predecessor):
        raise FastlaneC1FormalAdapterError("C1_PREDECESSOR_AUTHORITY_INVALID")
    for key, filename in expected_predecessor.items():
        descriptor = predecessor.get(key)
        if (
            not isinstance(descriptor, Mapping)
            or descriptor.get("filename") != filename
            or _expect_sha256(descriptor.get("sha256"), code="C1_PREDECESSOR_AUTHORITY_INVALID") is None
        ):
            raise FastlaneC1FormalAdapterError("C1_PREDECESSOR_AUTHORITY_INVALID")

    grid = value.get("successor_grid")
    expected_grid = {
        "source_cue_count": 36,
        "live_source_cues": [
            1, 2, 3, 4, 5, 6, 7, 8, 15, 16, 17, 18, 19, 20, 21, 24,
            25, 29, 30, 31, 32, 33, 34, 35, 36,
        ],
        "watched_video_source_cues": [9, 10, 11, 12, 13, 14, 22, 23, 26, 27, 28],
        "retained_live_cue_count": 25,
        "watched_video_drop_count": 11,
        "inserted_live_cue_count": 1,
        "release_cue_count": 26,
        "replacements": {
            "6": "薇欧拉酱只是喜欢阿拉蕾酱而已",
            "16": "主包给练舞室的姐姐们推荐",
            "25": "daisukino ararei chan",
        },
        "insertion": {
            "after_source_cue": 19,
            "start_ms": 91100,
            "end_ms": 91900,
            "text": "来了",
        },
    }
    if grid != expected_grid:
        raise FastlaneC1FormalAdapterError("C1_SUCCESSOR_GRID_AUTHORITY_INVALID")
    if (
        correction_document.get("candidate_id") != CID
        or correction_document.get("cue_classification")
        != {
            "source_cue_count": expected_grid["source_cue_count"],
            "live_lidousha_cues": expected_grid["live_source_cues"],
            "watched_video_cues": expected_grid["watched_video_source_cues"],
            "basis": "Perceptual classification of the hash-bound predecessor delivery: video dialogue/song is excluded; Li Dousha commentary is retained.",
        }
        or correction_document.get("mutations")
        != {
            "replace": expected_grid["replacements"],
            "drop": expected_grid["watched_video_source_cues"],
            "insert_after_source_cue": expected_grid["insertion"]["after_source_cue"],
            "insertion": {
                "start_ms": expected_grid["insertion"]["start_ms"],
                "end_ms": expected_grid["insertion"]["end_ms"],
                "text": expected_grid["insertion"]["text"],
                "basis": "Ivan ruling location: about 01:37 on the public burn; C1's source SRT is main-delivery-local and Z1 contributes 5749ms, so this is 01:31.100–01:31.900 on the source grid.",
            },
        }
    ):
        raise FastlaneC1FormalAdapterError("C1_CORRECTION_CUE_SCOPE_DRIFT")

    intro = value.get("branding_intro")
    expected_intro = {
        "intro_id": "huozi-lidousha-shiling-budui-weiaizuoyi-z1-v2",
        "media_sha256": "sha256:bbd0c7e3b34d3d5af543bb8444861ab1e18f835c9252629480b2ec2fd34e7dc5",
        "intro_offset_ms": 5749,
        "manifest_relative_path": "assets/lidousha/intro/branding_intro.v1.json",
        "manifest_sha256": "sha256:bee66d164b683451602ae3601d79d19175bc72b0d13257b3026ae2dc47ee06ef",
    }
    if intro != expected_intro:
        raise FastlaneC1FormalAdapterError("C1_BRANDING_INTRO_AUTHORITY_INVALID")
    intro_manifest = repo_root / str(intro["manifest_relative_path"])
    if sha256_file(_require_regular(intro_manifest, label="BRANDING_INTRO_MANIFEST")) != intro["manifest_sha256"]:
        raise FastlaneC1FormalAdapterError("C1_BRANDING_INTRO_MANIFEST_DRIFT")

    public = value.get("public_identity")
    expected_public = {
        "snapshot_schema_version": "c1-same-bv-public-binding.v1",
        "snapshot_sha256": "sha256:24723cae9edaa9eef039e043e5616f22138008294bdf8a33bdb2a3a7b3f04ef7",
        "response_sha256": "979073cf4c92494b7952fcee48927b6a9f6f1b7f4f5a9fe996cc3683158794de",
        "bvid": "BV1os8q61Eya",
        "aid": 117132650155234,
        "cid": 41126267272,
    }
    if public != expected_public:
        raise FastlaneC1FormalAdapterError("C1_PUBLIC_IDENTITY_AUTHORITY_INVALID")

    names = value.get("output_names")
    expected_names = {
        "video": f"{CID}.recut.mp4",
        "successor_srt": f"{CID}.recut.srt",
        "successor_ass": f"{CID}.recut.final-sapphire72.ass",
        "burned_final": f"{CID}.recut.burned-final-speaker.mp4",
        "materializer_receipt": "fastlane-c1-private-successor.v1.json",
        "cover": f"{CID}.recut.burned-final-sapphire72.cover.png",
        "record": f"{CID}.recut.c1-formal.record.json",
        "publish": f"{CID}.recut.c1-formal.publish.json",
        "title": f"{CID}.title.txt",
        "public_identity": "c1.public-identity-and-metadata.v1.json",
        "ruling_document": "ruling/2026-08-19-ivan-review-batch-rulings.md",
        "ruling_seal": "c1.ruling-seal.v1.json",
        "adapter_receipt": "c1.formal-adapter-receipt.v1.json",
        "technical_template": "delegated-root-technical-review.template.v1.json",
        "predecessor_srt": f"predecessor/{CID}.recut.srt",
        "predecessor_ass": f"predecessor/{CID}.recut.final-sapphire72.ass",
        "predecessor_record": f"predecessor/{CID}.record.json",
        "predecessor_publish": f"predecessor/{CID}.recut.publish.json",
        "predecessor_manifest": "predecessor/review_manifest.json",
    }
    if names != expected_names:
        raise FastlaneC1FormalAdapterError("C1_FORMAL_OUTPUT_NAMES_INVALID")
    return dict(value)


def load_formal_authority(*, repo_root: Path = ROOT) -> dict[str, Any]:
    path = repo_root / FORMAL_AUTHORITY_RELATIVE
    return _validate_formal_authority(
        _read_json(path, label="FORMAL_AUTHORITY"), repo_root=repo_root
    )


def _verify_input(path: Path, expected: object, *, label: str) -> None:
    actual = sha256_file(_require_regular(path, label=label), prefixed=False)
    if actual != _expect_sha256(expected, code=f"C1_{label}_HASH_INVALID"):
        raise FastlaneC1FormalAdapterError(f"C1_{label}_HASH_DRIFT")


def _public_identity_binding(snapshot_path: Path, authority: Mapping[str, object]) -> tuple[dict[str, Any], dict[str, Any]]:
    public = authority["public_identity"]
    assert isinstance(public, Mapping)
    _verify_input(snapshot_path, public["snapshot_sha256"], label="PUBLIC_IDENTITY")
    snapshot = _read_json(snapshot_path, label="PUBLIC_IDENTITY")
    identity = snapshot.get("identity")
    constraints = snapshot.get("constraints")
    if (
        snapshot.get("schema_version") != public["snapshot_schema_version"]
        or snapshot.get("response_sha256") != public["response_sha256"]
        or not isinstance(identity, dict)
        or not isinstance(constraints, dict)
        or {key: identity.get(key) for key in ("bvid", "aid", "cid", "title")}
        != {"bvid": public["bvid"], "aid": public["aid"], "cid": public["cid"], "title": TITLE}
        or constraints
        != {
            "new_bv_forbidden": True,
            "preserve_all_live_metadata_unless_ivan_changed": True,
            "same_bv_required": True,
        }
    ):
        raise FastlaneC1FormalAdapterError("C1_PUBLIC_IDENTITY_CONTENT_DRIFT")
    binding = {
        "snapshot_path": str(authority["output_names"]["public_identity"]),
        "snapshot_sha256": public["snapshot_sha256"],
        "response_sha256": public["response_sha256"],
        "identity_sha256": _sha256_bytes(_canonical_bytes(identity)),
        "identity_fields": sorted(identity),
        "bvid": public["bvid"],
        "aid": public["aid"],
        "cid": public["cid"],
        "title": TITLE,
        "metadata_policy": {
            "mode": "PRESERVE_ALL_CURRENT_LIVE_FIELDS",
            "allowed_metadata_changes": [],
            "new_bv_forbidden": True,
            "same_bv_required": True,
        },
    }
    return snapshot, binding


def _read_line_947(path: Path) -> bytes:
    _require_regular(path, label="CLAUDE_JSONL")
    try:
        lines = path.read_bytes().splitlines(keepends=True)
        return lines[946]
    except (OSError, IndexError) as exc:
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_UNAVAILABLE") from exc


def _validate_ruling_inputs(
    *, raw_line: bytes, ruling_document: Path, authority: Mapping[str, object]
) -> tuple[dict[str, Any], bytes]:
    ruling = authority["operator_ruling"]
    assert isinstance(ruling, Mapping)
    if _sha256_bytes(raw_line) != ruling["raw_line947_sha256"]:
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_HASH_DRIFT")
    try:
        line = json.loads(raw_line.decode("utf-8"))
        content = line["message"]["content"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_CONTENT_INVALID") from exc
    if (
        line.get("type") != "user"
        or line.get("uuid") != "555195ed-ec18-418d-a311-558f7e54291f"
        or line.get("timestamp") != "2026-08-19T00:08:52.249Z"
        or not isinstance(content, str)
    ):
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_SCOPE_DRIFT")
    if _sha256_bytes(content.encode("utf-8")) != ruling["raw_line947_content_sha256"]:
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_CONTENT_HASH_DRIFT")
    if any(fragment not in content for fragment in ruling["raw_payload_required_fragments"]):
        raise FastlaneC1FormalAdapterError("C1_CLAUDE_LINE947_SCOPE_DRIFT")
    raw_document = _require_regular(ruling_document, label="RULING_DOCUMENT").read_bytes()
    try:
        document = raw_document.decode("utf-8")
    except UnicodeError as exc:
        raise FastlaneC1FormalAdapterError("C1_RULING_DOCUMENT_CONTENT_INVALID") from exc
    if any(fragment not in document for fragment in ruling["ruling_document_required_fragments"]):
        raise FastlaneC1FormalAdapterError("C1_RULING_DOCUMENT_SCOPE_DRIFT")
    return (
        {
            "schema_version": RULING_SEAL_SCHEMA,
            "candidate_id": CID,
            "raw_line947_sha256": ruling["raw_line947_sha256"],
            "raw_line947_content_sha256": ruling["raw_line947_content_sha256"],
            "raw_line947": {
                "source_kind": "Claude JSONL user payload",
                "line_number": 947,
                "uuid": line["uuid"],
                "timestamp": line["timestamp"],
            },
            "ruling_document": {
                "path": str(authority["output_names"]["ruling_document"]),
                "sha256": _sha256_bytes(raw_document),
                "candidate_scope_ordinal": 1,
                "candidate_id": CID,
                "binding_mode": "RESEALED_AT_INTEGRATION",
            },
            "ivan_rereview_required": False,
        },
        raw_document,
    )


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(_canonical_bytes(value))


def _copy_checked(source: Path, target: Path, expected: object, *, label: str) -> None:
    _verify_input(source, expected, label=label)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(source, target)
    _verify_input(target, expected, label=label)


def _successor_cue_contract(
    predecessor_srt: Path,
    predecessor_ass: Path,
    successor_srt: Path,
    successor_ass: Path,
) -> None:
    # The original private C1 builder is deliberately the sole text projector;
    # importing its fixed-C1 helpers here makes the formal adapter replay the
    # exact same closed transformation rather than accepting free text.
    from scripts.build_fastlane_c1_private_successor import (
        load_authority,
        parse_srt,
        project_ass,
        project_cues,
        render_srt,
        validate_projection,
    )

    authority = load_authority()
    source = parse_srt(predecessor_srt.read_text(encoding="utf-8"))
    projected = project_cues(source, authority)
    validate_projection(source, projected, authority)
    expected_srt = render_srt(projected)
    expected_ass = project_ass(predecessor_ass.read_text(encoding="utf-8"), source, projected)
    if successor_srt.read_text(encoding="utf-8") != expected_srt:
        raise FastlaneC1FormalAdapterError("C1_SUCCESSOR_SRT_GRID_OR_SCOPE_DRIFT")
    if successor_ass.read_text(encoding="utf-8") != expected_ass:
        raise FastlaneC1FormalAdapterError("C1_SUCCESSOR_ASS_ORDER_OR_SCOPE_DRIFT")
    validation = validate_srt_file(successor_srt)
    if validation.get("errors"):
        raise FastlaneC1FormalAdapterError("C1_SUCCESSOR_SRT_VALIDATION_FAILED")
    for cue in projected:
        lines = [line.strip() for line in cue.text.splitlines() if line.strip()]
        if len(lines) > 2 or any(sum(1 for char in line if not char.isspace()) > 28 for line in lines):
            raise FastlaneC1FormalAdapterError("C1_SUCCESSOR_VISUAL_CONTRACT_DRIFT")


def _artifact_hashes(root: Path, names: Mapping[str, object]) -> dict[str, str]:
    return {
        key: sha256_file(_contained(root, str(relative), label=f"ARTIFACT_{key.upper()}"))
        for key, relative in names.items()
    }


def _build_record(
    *, authority: Mapping[str, object], artifact_hashes: Mapping[str, str], public_binding: Mapping[str, object]
) -> dict[str, object]:
    names = authority["output_names"]
    predecessor = authority["predecessor"]
    assert isinstance(names, Mapping) and isinstance(predecessor, Mapping)
    predecessor_paths = {
        "video": names["video"],
        "srt": names["predecessor_srt"],
        "ass": names["predecessor_ass"],
        "record": names["predecessor_record"],
        "publish": names["predecessor_publish"],
        "cover": names["cover"],
        "review_manifest": names["predecessor_manifest"],
    }
    return {
        "schema_version": FORMAL_RECORD_SCHEMA,
        "candidate_id": CID,
        "recording_date": RECORDING_DATE,
        "title": TITLE,
        "status": "PRIVATE_CANDIDATE_RENDERED_NO_ROOT_RECEIPT",
        "upload_allowed": False,
        "artifact_hashes": dict(artifact_hashes),
        "source_predecessor": {
            key: {
                "path": str(predecessor_paths[key]),
                "sha256": descriptor["sha256"],
            }
            for key, descriptor in predecessor.items()
        },
        "reviewed_baseline_recovery_chain": {
            "schema_version": "fastlane-c1-reviewed-baseline-recovery-chain.v1",
            "mode": "FIXED_C1_36_TO_26_PLUS_ONE_LIVE_INSERTION",
            "predecessor_reviewed_baseline": {
                key: {
                    "path": str(predecessor_paths[key]),
                    "sha256": predecessor[key]["sha256"],
                }
                for key in ("record", "publish", "review_manifest")
            },
            "successor_projection": {
                "grid": dict(authority["successor_grid"]),
                "successor_srt_path": str(names["successor_srt"]),
                "successor_ass_path": str(names["successor_ass"]),
            },
            "same_bv_only": True,
        },
        "subtitle_successor": {
            "grid": dict(authority["successor_grid"]),
            "successor_srt_path": str(names["successor_srt"]),
            "successor_ass_path": str(names["successor_ass"]),
            "materializer_receipt_path": str(names["materializer_receipt"]),
            "correction_authority_sha256": authority["correction_authority"]["sha256"],
        },
        "branding_intro": dict(authority["branding_intro"]),
        "public_identity_binding": dict(public_binding),
        "same_bv_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
        "publish_staging": {
            "title": TITLE,
            "video_path": str(names["burned_final"]),
            "subtitle_path": str(names["successor_srt"]),
            "subtitle_ass_path": str(names["successor_ass"]),
            "cover_path": str(names["cover"]),
            "upload_enabled": False,
            "target_bvid": authority["public_identity"]["bvid"],
        },
        "root_technical_receipt": {
            "status": ROOT_TECHNICAL_STATUS,
            "accepted": False,
            "receipt_path": None,
        },
    }


def _build_publish(
    *, authority: Mapping[str, object], artifact_hashes: Mapping[str, str], public_binding: Mapping[str, object]
) -> dict[str, object]:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    return {
        "schema_version": FORMAL_PUBLISH_SCHEMA,
        "candidate_id": CID,
        "recording_date": RECORDING_DATE,
        "title": TITLE,
        "upload_enabled": False,
        "video_path": str(names["burned_final"]),
        "subtitle_path": str(names["successor_srt"]),
        "subtitle_ass_path": str(names["successor_ass"]),
        "cover_path": str(names["cover"]),
        "artifact_hashes": dict(artifact_hashes),
        "public_identity_binding": dict(public_binding),
        "same_bv_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
        "root_technical_receipt": {
            "status": ROOT_TECHNICAL_STATUS,
            "accepted": False,
            "receipt_path": None,
        },
    }


def _build_manifest(*, authority: Mapping[str, object]) -> dict[str, object]:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    return {
        "schema_version": FORMAL_MANIFEST_SCHEMA,
        "status": ROOT_TECHNICAL_STATUS,
        "run_mode": "FASTLANE_C1_FORMAL_PRIVATE_REVIEW",
        "upload_allowed": False,
        "candidate_id": CID,
        "date": RECORDING_DATE,
        "title": TITLE,
        "subtitle_visual_contract": {"max_visual_lines": 2, "max_chars_per_line": 28},
        "same_bv_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
        "c1_formal_adapter": {
            "authority_path": FORMAL_AUTHORITY_RELATIVE.as_posix(),
            "authority_sha256": authority["authority_sha256"],
            "adapter_receipt": str(names["adapter_receipt"]),
            "technical_review_template": str(names["technical_template"]),
        },
        "package_artifacts": {
            key: str(value)
            for key, value in names.items()
        },
        "items": [
            {
                "id": CID,
                "candidate_id": CID,
                "kind": "talk",
                "title": TITLE,
                "video": str(names["burned_final"]),
                "subtitle_srt": str(names["successor_srt"]),
                "ass_path": str(names["successor_ass"]),
                "cover": str(names["cover"]),
                "record": str(names["record"]),
                "publish_json": str(names["publish"]),
                "title_txt": str(names["title"]),
            }
        ],
    }


def is_fastlane_c1_formal_manifest(manifest: Mapping[str, object]) -> bool:
    return manifest.get("schema_version") == FORMAL_MANIFEST_SCHEMA


def _validate_manifest_shape(manifest: Mapping[str, object], authority: Mapping[str, object]) -> None:
    expected = {
        "schema_version", "status", "run_mode", "upload_allowed", "candidate_id", "date",
        "title", "subtitle_visual_contract", "same_bv_delivery_constraint", "c1_formal_adapter",
        "package_artifacts", "items",
    }
    _require_exact_keys(manifest, expected, code="C1_FORMAL_MANIFEST_FIELDS_INVALID")
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    if (
        manifest.get("status") != ROOT_TECHNICAL_STATUS
        or manifest.get("run_mode") != "FASTLANE_C1_FORMAL_PRIVATE_REVIEW"
        or manifest.get("upload_allowed") is not False
        or manifest.get("candidate_id") != CID
        or manifest.get("date") != RECORDING_DATE
        or manifest.get("title") != TITLE
        or manifest.get("same_bv_delivery_constraint") != authority["same_bv_delivery_constraint"]
        or manifest.get("subtitle_visual_contract") != {"max_visual_lines": 2, "max_chars_per_line": 28}
        or manifest.get("package_artifacts") != {key: str(value) for key, value in names.items()}
    ):
        raise FastlaneC1FormalAdapterError("C1_FORMAL_MANIFEST_BINDING_DRIFT")
    adapter = manifest.get("c1_formal_adapter")
    expected_adapter = {
        "authority_path": FORMAL_AUTHORITY_RELATIVE.as_posix(),
        "authority_sha256": authority["authority_sha256"],
        "adapter_receipt": names["adapter_receipt"],
        "technical_review_template": names["technical_template"],
    }
    if adapter != expected_adapter:
        raise FastlaneC1FormalAdapterError("C1_FORMAL_MANIFEST_ADAPTER_DRIFT")
    items = manifest.get("items")
    expected_item = {
        "id": CID,
        "candidate_id": CID,
        "kind": "talk",
        "title": TITLE,
        "video": names["burned_final"],
        "subtitle_srt": names["successor_srt"],
        "ass_path": names["successor_ass"],
        "cover": names["cover"],
        "record": names["record"],
        "publish_json": names["publish"],
        "title_txt": names["title"],
    }
    if not isinstance(items, list) or items != [expected_item]:
        raise FastlaneC1FormalAdapterError("C1_FORMAL_MANIFEST_ITEM_DRIFT")


def _validate_root_receipt(value: object, *, label: str) -> None:
    if value != {
        "status": ROOT_TECHNICAL_STATUS,
        "accepted": False,
        "receipt_path": None,
    }:
        raise FastlaneC1FormalAdapterError(f"C1_{label}_ROOT_RECEIPT_INVALID")


def _validate_mirrors(
    *, root: Path, authority: Mapping[str, object], artifact_hashes: Mapping[str, str], public_binding: Mapping[str, object]
) -> None:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    record = _read_json(_contained(root, str(names["record"]), label="FORMAL_RECORD"), label="FORMAL_RECORD")
    publish = _read_json(_contained(root, str(names["publish"]), label="FORMAL_PUBLISH"), label="FORMAL_PUBLISH")
    if record != _build_record(authority=authority, artifact_hashes=artifact_hashes, public_binding=public_binding):
        raise FastlaneC1FormalAdapterError("C1_FORMAL_RECORD_MIRROR_DRIFT")
    if publish != _build_publish(authority=authority, artifact_hashes=artifact_hashes, public_binding=public_binding):
        raise FastlaneC1FormalAdapterError("C1_FORMAL_PUBLISH_MIRROR_DRIFT")


def _validate_ruling_seal(root: Path, authority: Mapping[str, object]) -> None:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    document = _contained(root, str(names["ruling_document"]), label="RULING_DOCUMENT")
    seal = _read_json(_contained(root, str(names["ruling_seal"]), label="RULING_SEAL"), label="RULING_SEAL")
    ruling = authority["operator_ruling"]
    assert isinstance(ruling, Mapping)
    expected = {
        "schema_version": RULING_SEAL_SCHEMA,
        "candidate_id": CID,
        "raw_line947_sha256": ruling["raw_line947_sha256"],
        "raw_line947_content_sha256": ruling["raw_line947_content_sha256"],
        "raw_line947": {
            "source_kind": "Claude JSONL user payload",
            "line_number": 947,
            "uuid": "555195ed-ec18-418d-a311-558f7e54291f",
            "timestamp": "2026-08-19T00:08:52.249Z",
        },
        "ruling_document": {
            "path": str(names["ruling_document"]),
            "sha256": sha256_file(document),
            "candidate_scope_ordinal": 1,
            "candidate_id": CID,
            "binding_mode": "RESEALED_AT_INTEGRATION",
        },
        "ivan_rereview_required": False,
    }
    if seal != expected:
        raise FastlaneC1FormalAdapterError("C1_RULING_SEAL_DRIFT")
    try:
        document_text = document.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise FastlaneC1FormalAdapterError("C1_RULING_DOCUMENT_CONTENT_INVALID") from exc
    if any(fragment not in document_text for fragment in ruling["ruling_document_required_fragments"]):
        raise FastlaneC1FormalAdapterError("C1_RULING_DOCUMENT_SCOPE_DRIFT")


def _validate_materializer_receipt(root: Path, authority: Mapping[str, object]) -> None:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    receipt = _read_json(
        _contained(root, str(names["materializer_receipt"]), label="MATERIALIZER_RECEIPT"),
        label="MATERIALIZER_RECEIPT",
    )
    expected_artifacts = {
        str(names[key]): sha256_file(
            _contained(root, str(names[key]), label=f"MATERIALIZER_{key.upper()}"),
            prefixed=False,
        )
        for key in ("video", "successor_srt", "successor_ass", "burned_final")
    }
    expected = {
        "schema_version": "fastlane-c1-private-successor.v1",
        "candidate_id": CID,
        "title": TITLE,
        "upload_allowed": False,
        "future_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
        "source_cue_count": 36,
        "release_cue_count": 26,
        "dropped_watched_video_cue_count": 11,
        "artifacts": expected_artifacts,
    }
    if receipt != expected:
        raise FastlaneC1FormalAdapterError("C1_MATERIALIZER_RECEIPT_DRIFT")


def _validate_technical_template(root: Path, authority: Mapping[str, object], manifest_path: Path) -> None:
    names = authority["output_names"]
    assert isinstance(names, Mapping)
    template = _read_json(
        _contained(root, str(names["technical_template"]), label="TECHNICAL_TEMPLATE"),
        label="TECHNICAL_TEMPLATE",
    )
    receipt = _contained(root, str(names["adapter_receipt"]), label="ADAPTER_RECEIPT")
    expected = {
        "schema_version": TECHNICAL_TEMPLATE_SCHEMA,
        "candidate_id": CID,
        "status": ROOT_TECHNICAL_STATUS,
        "purpose": "Delegated-root technical review template only; not acceptance, release, upload, or Bilibili mutation receipt.",
        "ivan_rereview_required": False,
        "root_technical_receipt": {
            "status": ROOT_TECHNICAL_STATUS,
            "accepted": False,
            "reviewed_by": None,
            "reviewed_at": None,
            "receipt_path": None,
        },
        "bindings": {
            "review_manifest": {"path": "review_manifest.json", "sha256": sha256_file(manifest_path)},
            "formal_adapter_receipt": {"path": str(names["adapter_receipt"]), "sha256": sha256_file(receipt)},
            "package_audit_path": "package_audit.json",
        },
        "six_named_points": [dict(point) for point in SIX_NAMED_POINTS],
        "required_checks": [
            "verify canonical package audit is current and passing",
            "inspect the six Ivan-named subtitle points on the canonical burned final",
            "confirm same-BV-only public identity and metadata preservation constraints",
            "record a root technical receipt separately if accepted",
        ],
    }
    if template != expected:
        raise FastlaneC1FormalAdapterError("C1_TECHNICAL_TEMPLATE_DRIFT")


def validate_formal_package(root: Path, *, repo_root: Path = ROOT) -> None:
    """Replay every C1-only binding from a portable private review package."""

    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise FastlaneC1FormalAdapterError("C1_PACKAGE_ROOT_INVALID")
    authority = load_formal_authority(repo_root=repo_root)
    manifest_path = _contained(root, "review_manifest.json", label="REVIEW_MANIFEST")
    manifest = _read_json(manifest_path, label="REVIEW_MANIFEST")
    _validate_manifest_shape(manifest, authority)
    names = authority["output_names"]
    predecessor = authority["predecessor"]
    assert isinstance(names, Mapping) and isinstance(predecessor, Mapping)

    title_path = _contained(root, str(names["title"]), label="TITLE")
    if title_path.read_text(encoding="utf-8") != TITLE + "\n":
        raise FastlaneC1FormalAdapterError("C1_TITLE_SURFACE_DRIFT")

    # All actual package paths are fixed by the candidate authority.  The
    # predecessor video is intentionally represented by the packaged source
    # video; the old SRT/ASS and control records are carried in predecessor/.
    source_paths = {
        "video": str(names["video"]),
        "srt": str(names["predecessor_srt"]),
        "ass": str(names["predecessor_ass"]),
        "record": str(names["predecessor_record"]),
        "publish": str(names["predecessor_publish"]),
        "cover": str(names["cover"]),
        "review_manifest": str(names["predecessor_manifest"]),
    }
    for key, relative in source_paths.items():
        descriptor = predecessor[key]
        assert isinstance(descriptor, Mapping)
        _verify_input(_contained(root, relative, label=f"PREDECESSOR_{key.upper()}"), descriptor["sha256"], label=f"PREDECESSOR_{key.upper()}")

    _successor_cue_contract(
        _contained(root, str(names["predecessor_srt"]), label="PREDECESSOR_SRT"),
        _contained(root, str(names["predecessor_ass"]), label="PREDECESSOR_ASS"),
        _contained(root, str(names["successor_srt"]), label="SUCCESSOR_SRT"),
        _contained(root, str(names["successor_ass"]), label="SUCCESSOR_ASS"),
    )
    _validate_materializer_receipt(root, authority)
    snapshot, public_binding = _public_identity_binding(
        _contained(root, str(names["public_identity"]), label="PUBLIC_IDENTITY"), authority
    )
    # Keep the complete live metadata map as an immutable packaged source; the
    # record and publish mirrors bind its canonical identity projection.
    if not isinstance(snapshot.get("identity"), dict):
        raise FastlaneC1FormalAdapterError("C1_PUBLIC_IDENTITY_CONTENT_DRIFT")

    artifact_names = {
        "video": str(names["video"]),
        "successor_srt": str(names["successor_srt"]),
        "successor_ass": str(names["successor_ass"]),
        "burned_final": str(names["burned_final"]),
        "cover": str(names["cover"]),
        "materializer_receipt": str(names["materializer_receipt"]),
        "ruling_seal": str(names["ruling_seal"]),
        "ruling_document": str(names["ruling_document"]),
        "public_identity": str(names["public_identity"]),
        "title": str(names["title"]),
    }
    artifact_hashes = _artifact_hashes(root, artifact_names)
    _validate_mirrors(
        root=root, authority=authority, artifact_hashes=artifact_hashes, public_binding=public_binding
    )
    _validate_ruling_seal(root, authority)

    receipt_path = _contained(root, str(names["adapter_receipt"]), label="ADAPTER_RECEIPT")
    receipt = _read_json(receipt_path, label="ADAPTER_RECEIPT")
    expected_receipt = {
        "schema_version": FORMAL_RECEIPT_SCHEMA,
        "candidate_id": CID,
        "recording_date": RECORDING_DATE,
        "title": TITLE,
        "status": "PRIVATE_PACKAGE_MATERIALIZED_NO_ROOT_RECEIPT",
        "upload_allowed": False,
        "authority_sha256": authority["authority_sha256"],
        "review_manifest": {"path": "review_manifest.json", "sha256": sha256_file(manifest_path)},
        "artifacts": dict(artifact_hashes),
        "same_bv_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
        "root_technical_receipt": {
            "status": ROOT_TECHNICAL_STATUS,
            "accepted": False,
            "receipt_path": None,
        },
    }
    if receipt != expected_receipt:
        raise FastlaneC1FormalAdapterError("C1_FORMAL_ADAPTER_RECEIPT_DRIFT")
    _validate_technical_template(root, authority, manifest_path)


def audit_fastlane_c1_formal_package(root: Path, *, repo_root: Path = ROOT) -> list[C1AuditIssue]:
    """Return one explicit fail-closed issue for the sealed C1 package."""

    try:
        validate_formal_package(root, repo_root=repo_root)
    except FastlaneC1FormalAdapterError as exc:
        return [C1AuditIssue(code=str(exc), path=Path(root) / "review_manifest.json")]
    return []


def audit_fastlane_c1_formal_manifest(
    root: Path, manifest: Mapping[str, object]
) -> list[dict[str, str]] | None:
    """Project the C1-only audit into the generic package-audit issue shape."""

    if not is_fastlane_c1_formal_manifest(manifest):
        return None
    return [
        {
            "code": issue.code,
            "severity": "BLOCK",
            **({"path": str(issue.path)} if issue.path is not None else {}),
            **({"detail": issue.detail} if issue.detail else {}),
        }
        for issue in audit_fastlane_c1_formal_package(root)
    ]


def materialize_formal_private_package(
    *,
    predecessor_dir: Path,
    predecessor_package: Path,
    branding_intro: Path,
    public_identity: Path,
    claude_jsonl: Path,
    ruling_document: Path,
    out: Path,
    repo_root: Path = ROOT,
) -> Path:
    """Create C1's one private formal package via the current canonical builder."""

    authority = load_formal_authority(repo_root=repo_root)
    names = authority["output_names"]
    predecessor = authority["predecessor"]
    assert isinstance(names, Mapping) and isinstance(predecessor, Mapping)
    predecessor_dir = predecessor_dir.resolve()
    predecessor_package = predecessor_package.resolve()
    out = out.resolve()
    if out.exists():
        raise FastlaneC1FormalAdapterError("C1_FORMAL_OUTPUT_ALREADY_EXISTS")
    if not predecessor_dir.is_dir() or predecessor_dir.is_symlink() or not predecessor_package.is_dir() or predecessor_package.is_symlink():
        raise FastlaneC1FormalAdapterError("C1_PREDECESSOR_INPUT_ROOT_INVALID")

    input_paths = {
        "video": predecessor_dir / str(predecessor["video"]["filename"]),
        "srt": predecessor_dir / str(predecessor["srt"]["filename"]),
        "ass": predecessor_dir / str(predecessor["ass"]["filename"]),
        "record": predecessor_package / str(predecessor["record"]["filename"]),
        "publish": predecessor_package / str(predecessor["publish"]["filename"]),
        "cover": predecessor_package / str(predecessor["cover"]["filename"]),
        "review_manifest": predecessor_package / str(predecessor["review_manifest"]["filename"]),
    }
    for key, path in input_paths.items():
        descriptor = predecessor[key]
        assert isinstance(descriptor, Mapping)
        _verify_input(path, descriptor["sha256"], label=f"PREDECESSOR_{key.upper()}")

    intro = authority["branding_intro"]
    assert isinstance(intro, Mapping)
    _verify_input(branding_intro.resolve(), intro["media_sha256"], label="BRANDING_INTRO")
    _snapshot, public_binding = _public_identity_binding(public_identity.resolve(), authority)
    ruling_seal, ruling_document_bytes = _validate_ruling_inputs(
        raw_line=_read_line_947(claude_jsonl.resolve()),
        ruling_document=ruling_document.resolve(),
        authority=authority,
    )

    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fastlane-c1-formal-", dir=out.parent) as raw_stage:
        stage = Path(raw_stage)
        successor_stage = stage / "successor"
        command = [
            sys.executable,
            str(repo_root / "scripts/build_fastlane_c1_private_successor.py"),
            "--predecessor-dir", str(predecessor_dir),
            "--out", str(successor_stage),
            "--burn-preview",
            "--branding-intro", str(branding_intro.resolve()),
        ]
        result = subprocess.run(command, cwd=repo_root, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise FastlaneC1FormalAdapterError("C1_CANONICAL_MATERIALIZER_FAILED")

        package = stage / "package"
        package.mkdir(mode=0o700)
        for key in ("video", "successor_srt", "successor_ass", "burned_final", "materializer_receipt"):
            name = str(names[key])
            source = successor_stage / name
            _require_regular(source, label=f"CANONICAL_{key.upper()}")
            (package / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, package / name)
        predecessor_output_names = {
            "srt": "predecessor_srt",
            "ass": "predecessor_ass",
            "record": "predecessor_record",
            "publish": "predecessor_publish",
            "review_manifest": "predecessor_manifest",
        }
        for key, output_name in predecessor_output_names.items():
            target = package / str(names[output_name])
            _copy_checked(input_paths[key], target, predecessor[key]["sha256"], label=f"PREDECESSOR_{key.upper()}")
        _copy_checked(input_paths["cover"], package / str(names["cover"]), predecessor["cover"]["sha256"], label="PREDECESSOR_COVER")
        _copy_checked(public_identity.resolve(), package / str(names["public_identity"]), authority["public_identity"]["snapshot_sha256"], label="PUBLIC_IDENTITY")
        ruling_target = package / str(names["ruling_document"])
        ruling_target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        ruling_target.write_bytes(ruling_document_bytes)
        _write_json(package / str(names["ruling_seal"]), ruling_seal)
        (package / str(names["title"])).write_text(TITLE + "\n", encoding="utf-8")

        artifact_names = {
            "video": str(names["video"]),
            "successor_srt": str(names["successor_srt"]),
            "successor_ass": str(names["successor_ass"]),
            "burned_final": str(names["burned_final"]),
            "cover": str(names["cover"]),
            "materializer_receipt": str(names["materializer_receipt"]),
            "ruling_seal": str(names["ruling_seal"]),
            "ruling_document": str(names["ruling_document"]),
            "public_identity": str(names["public_identity"]),
            "title": str(names["title"]),
        }
        artifact_hashes = _artifact_hashes(package, artifact_names)
        _write_json(
            package / str(names["record"]),
            _build_record(
                authority=authority, artifact_hashes=artifact_hashes, public_binding=public_binding
            ),
        )
        _write_json(
            package / str(names["publish"]),
            _build_publish(
                authority=authority, artifact_hashes=artifact_hashes, public_binding=public_binding
            ),
        )
        manifest = _build_manifest(authority=authority)
        _write_json(package / "review_manifest.json", manifest)
        manifest_path = package / "review_manifest.json"
        adapter_receipt = {
            "schema_version": FORMAL_RECEIPT_SCHEMA,
            "candidate_id": CID,
            "recording_date": RECORDING_DATE,
            "title": TITLE,
            "status": "PRIVATE_PACKAGE_MATERIALIZED_NO_ROOT_RECEIPT",
            "upload_allowed": False,
            "authority_sha256": authority["authority_sha256"],
            "review_manifest": {"path": "review_manifest.json", "sha256": sha256_file(manifest_path)},
            "artifacts": dict(artifact_hashes),
            "same_bv_delivery_constraint": dict(authority["same_bv_delivery_constraint"]),
            "root_technical_receipt": {
                "status": ROOT_TECHNICAL_STATUS,
                "accepted": False,
                "receipt_path": None,
            },
        }
        _write_json(package / str(names["adapter_receipt"]), adapter_receipt)
        technical_template = {
            "schema_version": TECHNICAL_TEMPLATE_SCHEMA,
            "candidate_id": CID,
            "status": ROOT_TECHNICAL_STATUS,
            "purpose": "Delegated-root technical review template only; not acceptance, release, upload, or Bilibili mutation receipt.",
            "ivan_rereview_required": False,
            "root_technical_receipt": {
                "status": ROOT_TECHNICAL_STATUS,
                "accepted": False,
                "reviewed_by": None,
                "reviewed_at": None,
                "receipt_path": None,
            },
            "bindings": {
                "review_manifest": {"path": "review_manifest.json", "sha256": sha256_file(manifest_path)},
                "formal_adapter_receipt": {
                    "path": str(names["adapter_receipt"]),
                    "sha256": sha256_file(package / str(names["adapter_receipt"])),
                },
                "package_audit_path": "package_audit.json",
            },
            "six_named_points": [dict(point) for point in SIX_NAMED_POINTS],
            "required_checks": [
                "verify canonical package audit is current and passing",
                "inspect the six Ivan-named subtitle points on the canonical burned final",
                "confirm same-BV-only public identity and metadata preservation constraints",
                "record a root technical receipt separately if accepted",
            ],
        }
        _write_json(package / str(names["technical_template"]), technical_template)
        validate_formal_package(package, repo_root=repo_root)
        try:
            os.rename(package, out)
        except FileExistsError as exc:
            raise FastlaneC1FormalAdapterError("C1_FORMAL_OUTPUT_ALREADY_EXISTS") from exc
    return out


def is_fastlane_formal_manifest(manifest: Mapping[str, object]) -> bool:
    from .fastlane_c2_formal_adapter import is_fastlane_c2_formal_manifest
    return is_fastlane_c1_formal_manifest(manifest) or is_fastlane_c2_formal_manifest(manifest)


def audit_fastlane_formal_package(root: Path, manifest: Mapping[str, object]) -> list[C1AuditIssue]:
    if is_fastlane_c1_formal_manifest(manifest):
        return audit_fastlane_c1_formal_package(root)
    from .fastlane_c2_formal_adapter import audit_fastlane_c2_formal_package
    return [C1AuditIssue(item["code"], root, item.get("detail", "")) for item in audit_fastlane_c2_formal_package(root)]
