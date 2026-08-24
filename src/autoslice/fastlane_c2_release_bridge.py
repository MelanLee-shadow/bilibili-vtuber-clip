"""C2-only projection from the sealed formal review package to a v3 package.

This deliberately does not teach the ordinary uploader about an alternative
record shape.  It produces the same-stem record/review/publish closure that
the ordinary v3 uploader already consumes, while the package auditor has a
narrow C2 validator for the provenance which cannot be represented by a
normal producer record.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.suggest_upload_tags import generate_upload_tags

from .fastlane_c2_formal_adapter import (
    CID,
    NAMES,
    TITLE,
    audit_fastlane_c2_formal_package,
    sha256,
)

SCHEMA = "fastlane-c2-release-package-manifest.v1"
AUTH_SCHEMA = "fastlane-c2-direct-upload-authorization.v1"
TAG_RECEIPT_SCHEMA = "fastlane-c2-tag-generation-receipt.v1"
DATE = "2026-08-13"
VIDEO_NAME = f"{CID}.recut.burned-final-speaker.mp4"
SRT_NAME = f"{CID}.recut.burned-final-speaker.srt"
COVER_NAME = f"{CID}.recut.burned-final-speaker.cover.png"
RECORD_NAME = f"{CID}.recut.burned-final-speaker.record.json"
PUBLISH_NAME = f"{CID}.recut.burned-final-speaker.publish.json"
FORMAL_DIR = "formal"
AUTH_NAME = "c2.release-authorization.v1.json"
ROOT_RECEIPT_NAME = "c2.root-technical-receipt.v1.json"
TAG_RECEIPT_NAME = "c2.tag-generation-receipt.v1.json"
DIRECT_IVAN_LINES = {
    947: (
        "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa",
        "以上我说的所有内容修复后都可以走快车道上传，优先级顺序是我说时效性强的优先上传（=七夕），然后按顺序走快车道上传",
    ),
    1643: (
        "sha256:2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609",
        "不等人工节点，直接上传，七夕优先，查看 CI",
    ),
    1745: (
        "sha256:7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329",
        "七夕优先，其余随后",
    ),
}


class C2ReleaseBridgeError(ValueError):
    pass


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise C2ReleaseBridgeError(f"{label} unreadable") from exc
    if not isinstance(value, dict):
        raise C2ReleaseBridgeError(f"{label} is not an object")
    return value


def _sha_entry(path: Path) -> dict[str, object]:
    return {"path": path.name, "sha256": "sha256:" + sha256(path), "bytes": path.stat().st_size}


def _regular(path: Path, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise C2ReleaseBridgeError(f"{label} is missing or unsafe")


def _copy_regular(source: Path, destination: Path) -> None:
    _regular(source, str(source))
    if destination.exists() or destination.is_symlink():
        raise C2ReleaseBridgeError(f"create-only target already exists: {destination}")
    shutil.copy2(source, destination)
    if destination.is_symlink() or sha256(source) != sha256(destination):
        raise C2ReleaseBridgeError(f"copy hash drift: {source.name}")


def _copy_formal_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise C2ReleaseBridgeError("formal target already exists")
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise C2ReleaseBridgeError(f"formal source symlink: {relative}")
        target = destination / relative
        if path.is_dir():
            target.mkdir(mode=0o700, parents=True, exist_ok=False)
        elif path.is_file():
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_regular(path, target)
        else:
            raise C2ReleaseBridgeError(f"formal source special file: {relative}")


def is_fastlane_c2_release_manifest(manifest: Mapping[str, object]) -> bool:
    return (
        manifest.get("schema_version") == SCHEMA
        and manifest.get("candidate_id") == CID
        and manifest.get("recording_date") == DATE
    )


def _validate_authorization(payload: Mapping[str, object]) -> None:
    if (
        payload.get("schema_version") != AUTH_SCHEMA
        or payload.get("candidate_id") != CID
        or payload.get("recording_date") != DATE
        or payload.get("title") != TITLE
    ):
        raise C2ReleaseBridgeError("C2 direct-upload authorization identity drift")
    lines = payload.get("direct_ivan_lines")
    if not isinstance(lines, list) or [row.get("line") for row in lines if isinstance(row, Mapping)] != list(DIRECT_IVAN_LINES):
        raise C2ReleaseBridgeError("C2 direct-upload authorization line set drift")
    for row in lines:
        expected = DIRECT_IVAN_LINES.get(row.get("line")) if isinstance(row, Mapping) else None
        if not isinstance(row, Mapping) or expected is None or row.get("raw_line_sha256") != expected[0] or row.get("quote") != expected[1]:
            raise C2ReleaseBridgeError("C2 direct-upload authorization evidence invalid")


def _validate_formal_audit(formal: Path) -> None:
    """Replay the source formal package before accepting it as a bridge input."""
    from scripts.audit_lidousha_review_package import audit_package

    saved = _read_object(formal / "package_audit.json", "C2 formal package audit")
    current = audit_package(formal)
    if current != saved or current.get("passed") is not True or current.get("blocking_issue_count") != 0:
        raise C2ReleaseBridgeError("C2 formal package audit is not a current passing replay")


def _root_receipt_input_interface(receipt: Mapping[str, object], formal: Path) -> None:
    """Check only the bridge's input binding surface.

    The C2 receipt builder and its authoritative schema validator are owned by
    the C2 package worker.  This is intentionally not a second receipt
    validator: it merely refuses to consume a receipt that cannot bind the
    exact formal bytes this bridge projects.
    """
    if (
        receipt.get("candidate_id") != CID
        or receipt.get("title") != TITLE
        or receipt.get("accepted") is not True
        or receipt.get("reviewer") != "Codex root"
        or not str(receipt.get("reviewed_at") or "").strip()
    ):
        raise C2ReleaseBridgeError("C2 root technical receipt input is not accepted for this formal package")
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping):
        raise C2ReleaseBridgeError("C2 root technical receipt input has no bindings")
    expected = {
        "review_manifest_sha256": "sha256:" + sha256(formal / "review_manifest.json"),
        "package_audit_sha256": "sha256:" + sha256(formal / "package_audit.json"),
    }
    if any(bindings.get(key) != value for key, value in expected.items()):
        raise C2ReleaseBridgeError("C2 root technical receipt input formal binding drift")
    artifacts = bindings.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise C2ReleaseBridgeError("C2 root technical receipt input lacks artifact bindings")
    for role, name in NAMES.items():
        entry = artifacts.get(role)
        if not isinstance(entry, Mapping) or str(entry.get("sha256") or "").removeprefix("sha256:") != sha256(formal / name):
            raise C2ReleaseBridgeError(f"C2 root technical receipt input {role} drift")


def _valid_tags(result: Mapping[str, object]) -> list[str]:
    if result.get("status") not in {"OK", "OK_NO_LLM"}:
        raise C2ReleaseBridgeError("C2 tag generation failed")
    tags = result.get("final_tags")
    if not isinstance(tags, list) or not tags or len(tags) > 12:
        raise C2ReleaseBridgeError("C2 tag generation produced no valid tag line")
    clean = [str(tag).strip() for tag in tags]
    if any(not tag or len(tag) > 20 or any(c in tag for c in ",，\n\t") for tag in clean):
        raise C2ReleaseBridgeError("C2 tag generation produced invalid tag")
    if len({tag.casefold() for tag in clean}) != len(clean):
        raise C2ReleaseBridgeError("C2 tag generation produced duplicate tags")
    return clean


def _release_item() -> dict[str, object]:
    return {
        "candidate_id": CID,
        "recording_date": DATE,
        "title": TITLE,
        "video": VIDEO_NAME,
        "media": VIDEO_NAME,
        "cover": COVER_NAME,
        "record": RECORD_NAME,
        "record_json": RECORD_NAME,
        "subtitle_srt": SRT_NAME,
        "subtitle": SRT_NAME,
        "publish_json": PUBLISH_NAME,
        "publish": PUBLISH_NAME,
    }


def build_release_package(
    *,
    formal_package: Path,
    root_receipt: Path,
    authorization: Path,
    out: Path,
    tag_generator: Callable[..., dict] = generate_upload_tags,
    receipt_validator: Callable[[Path], None] | None = None,
) -> Path:
    """Create one private C2 projection.  It never calls upload or CPA image QC."""
    formal_package, root_receipt, authorization, out = (
        formal_package.resolve(), root_receipt.resolve(), authorization.resolve(), out.resolve()
    )
    if out.exists() or out.is_symlink():
        raise C2ReleaseBridgeError("C2 release package output already exists")
    if audit_fastlane_c2_formal_package(formal_package):
        raise C2ReleaseBridgeError("C2 source formal package is not current/passing")
    _validate_formal_audit(formal_package)
    _regular(root_receipt, "root receipt")
    _regular(authorization, "authorization")
    if receipt_validator is not None:
        receipt_validator(root_receipt)
    _root_receipt_input_interface(_read_object(root_receipt, "root receipt"), formal_package)
    _validate_authorization(_read_object(authorization, "authorization"))

    out.mkdir(mode=0o700, parents=True)
    try:
        _copy_formal_tree(formal_package, out / FORMAL_DIR)
        formal = out / FORMAL_DIR
        _copy_regular(root_receipt, out / ROOT_RECEIPT_NAME)
        _copy_regular(authorization, out / AUTH_NAME)
        _copy_regular(formal / NAMES["video"], out / VIDEO_NAME)
        _copy_regular(formal / NAMES["cover"], out / COVER_NAME)
        _copy_regular(formal / NAMES["srt"], out / SRT_NAME)
        result = tag_generator(TITLE, out / SRT_NAME)
        tags = _valid_tags(result)
        tag_receipt = {
            "schema_version": TAG_RECEIPT_SCHEMA,
            "candidate_id": CID,
            "title": TITLE,
            "title_sha256": "sha256:" + hashlib.sha256(TITLE.encode()).hexdigest(),
            "subtitle_sha256": "sha256:" + sha256(out / SRT_NAME),
            "engine_output": result,
            "final_tags": tags,
        }
        (out / TAG_RECEIPT_NAME).write_text(json.dumps(tag_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record = {
            "schema_version": "lidousha-c2-release-record.v1",
            "candidate_id": CID,
            "recording_date": DATE,
            "upload_allowed": False,
            "artifact_hashes": {
                "burned_video_sha256": "sha256:" + sha256(out / VIDEO_NAME),
                "cover_sha256": "sha256:" + sha256(out / COVER_NAME),
                "delivery_subtitle_sha256": "sha256:" + sha256(out / SRT_NAME),
                "ass_sha256": "sha256:" + sha256(formal / NAMES["ass"]),
            },
            "publish_staging": {"title": TITLE},
            "story_contract": {
                "schema_version": "lidousha-c2-release-story-projection.v1",
                "candidate_id": CID,
                "transcript_sha256": "sha256:" + sha256(out / SRT_NAME),
                "formal_authority": {
                    "formal_review_manifest_sha256": "sha256:" + sha256(formal / "review_manifest.json"),
                    "correction_authority_sha256": "sha256:" + sha256(out / AUTH_NAME),
                    "root_receipt_sha256": "sha256:" + sha256(out / ROOT_RECEIPT_NAME),
                },
            },
            "upload_tags": result,
            "c2_tag_generation_receipt": _sha_entry(out / TAG_RECEIPT_NAME),
        }
        publish = {
            "schema_version": "lidousha-c2-release-publish.v1",
            "candidate_id": CID,
            "title": TITLE,
            "cover_generation": {
                "final_cover": COVER_NAME,
                "final_cover_sha256": sha256(out / COVER_NAME),
                "formal_cover_reprojection_sha256": "sha256:" + sha256(formal / NAMES["cover_meta"]),
            },
        }
        (out / RECORD_NAME).write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (out / PUBLISH_NAME).write_text(json.dumps(publish, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": SCHEMA,
            "candidate_id": CID,
            "recording_date": DATE,
            "title": TITLE,
            "scope": "C2_NAMED_FASTLANE_NEW_BV_ONLY",
            "items": [_release_item()],
            "formal_source": {
                "review_manifest": _sha_entry(formal / "review_manifest.json"),
                "package_audit": _sha_entry(formal / "package_audit.json"),
                "correction_authority": _sha_entry(out / AUTH_NAME),
                "root_receipt": _sha_entry(out / ROOT_RECEIPT_NAME),
            },
            "tag_generation": _sha_entry(out / TAG_RECEIPT_NAME),
        }
        (out / "review_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        problems = audit_fastlane_c2_release_package(out)
        if problems:
            raise C2ReleaseBridgeError("C2 release package validation failed: " + ",".join(row["code"] for row in problems))
    except Exception:
        # The public API is create-only even if preparation fails; retaining the
        # private failed evidence is safer than silently reusing its pathname.
        raise
    return out


def audit_fastlane_c2_release_package(root: Path) -> list[dict[str, str]]:
    def issue(code: str, detail: str = "") -> list[dict[str, str]]:
        return [{"code": code, "severity": "BLOCK", "detail": detail}]
    try:
        manifest = (
            _read_object(root / "review_manifest.json", "C2 release manifest")
            if (root / "review_manifest.json").is_file()
            else {}
        )
    except C2ReleaseBridgeError as exc:
        return issue("C2_RELEASE_MANIFEST_INVALID", str(exc))
    if not is_fastlane_c2_release_manifest(manifest) or manifest.get("title") != TITLE or manifest.get("scope") != "C2_NAMED_FASTLANE_NEW_BV_ONLY":
        return issue("C2_RELEASE_MANIFEST_INVALID")
    items = manifest.get("items")
    if items != [_release_item()]:
        return issue("C2_RELEASE_ITEM_DRIFT")
    formal = root / FORMAL_DIR
    if audit_fastlane_c2_formal_package(formal):
        return issue("C2_RELEASE_FORMAL_SOURCE_INVALID")
    try:
        auth_path, receipt_path = root / AUTH_NAME, root / ROOT_RECEIPT_NAME
        _regular(auth_path, "authorization")
        _regular(receipt_path, "root receipt")
        _validate_formal_audit(formal)
        _validate_authorization(_read_object(auth_path, "authorization"))
        _root_receipt_input_interface(_read_object(receipt_path, "root receipt"), formal)
        for name, formal_name in ((VIDEO_NAME, NAMES["video"]), (COVER_NAME, NAMES["cover"]), (SRT_NAME, NAMES["srt"])):
            _regular(root / name, name)
            if sha256(root / name) != sha256(formal / formal_name):
                raise C2ReleaseBridgeError(f"C2 release artifact drift: {name}")
        record = _read_object(root / RECORD_NAME, "record")
        publish = _read_object(root / PUBLISH_NAME, "publish")
        tags_receipt_path = root / TAG_RECEIPT_NAME
        tag_receipt = _read_object(tags_receipt_path, "tag receipt")
        if record.get("candidate_id") != CID or record.get("recording_date") != DATE or record.get("upload_allowed") is not False:
            raise C2ReleaseBridgeError("C2 release record identity drift")
        story = record.get("story_contract")
        if not isinstance(story, Mapping) or story.get("candidate_id") != CID or story.get("transcript_sha256") != "sha256:" + sha256(root / SRT_NAME):
            raise C2ReleaseBridgeError("C2 release StoryContract drift")
        if record.get("publish_staging") != {"title": TITLE}:
            raise C2ReleaseBridgeError("C2 release publish title drift")
        if publish.get("candidate_id") != CID or publish.get("title") != TITLE or (publish.get("cover_generation") or {}).get("final_cover_sha256") != sha256(root / COVER_NAME):
            raise C2ReleaseBridgeError("C2 release publish/cover drift")
        if tag_receipt.get("schema_version") != TAG_RECEIPT_SCHEMA or tag_receipt.get("candidate_id") != CID or tag_receipt.get("subtitle_sha256") != "sha256:" + sha256(root / SRT_NAME):
            raise C2ReleaseBridgeError("C2 tag receipt drift")
        tags = _valid_tags(record.get("upload_tags") if isinstance(record.get("upload_tags"), Mapping) else {})
        if tag_receipt.get("final_tags") != tags:
            raise C2ReleaseBridgeError("C2 record tags differ from generation receipt")
        expected_source = {
            "review_manifest": formal / "review_manifest.json",
            "package_audit": formal / "package_audit.json",
            "correction_authority": auth_path,
            "root_receipt": receipt_path,
        }
        if set(manifest.get("formal_source") or {}) != set(expected_source):
            raise C2ReleaseBridgeError("C2 formal source entry set drift")
        for key, source_path in expected_source.items():
            entry = (manifest.get("formal_source") or {}).get(key)
            if not isinstance(entry, Mapping) or entry != _sha_entry(source_path):
                raise C2ReleaseBridgeError(f"C2 formal source entry drift: {key}")
        if manifest.get("tag_generation") != _sha_entry(tags_receipt_path):
            raise C2ReleaseBridgeError("C2 tag generation binding drift")
    except C2ReleaseBridgeError as exc:
        return issue("C2_RELEASE_CLOSURE_DRIFT", str(exc))
    return []
