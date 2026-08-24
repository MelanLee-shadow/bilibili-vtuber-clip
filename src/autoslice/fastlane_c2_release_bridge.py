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
import os
import stat
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
LEGACY_PROPOSAL_NAME = "c2.legacy-recovery.proposal.v1.json"
LEGACY_CONTRACT_NAME = "c2.legacy-execution-contract.v1.json"
TAG_RECEIPT_NAME = "c2.tag-generation-receipt.v1.json"
DIRECT_IVAN_LINES = {
    947: (
        "555195ed-ec18-418d-a311-558f7e54291f",
        "2026-08-19T00:08:52.249Z",
        "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa",
        "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b",
        "以上我说的所有内容修复后都可以走快车道上传，优先级顺序是我说时效性强的优先上传，然后按顺序走快车道上传",
    ),
    1643: (
        "b95d4356-7ad2-4481-b4a7-0b7afa3c35b9",
        "2026-08-19T04:06:54.376Z",
        "sha256:2269c653fa6be7fb0c20df98a7348d5f5c57176e3e41fe80eb13b85c39307609",
        "sha256:61e0ee6e0811fce540efc959d7468354bbd1633c271b140b8eed8ac44e8d010a",
        "我要睡觉了，你不要再等人工节点了，今天晚上把我授权的快车道全部上传，不过优先七夕。另外，github CI run fail了你看下",
    ),
    1745: (
        "a79d6670-88b1-43c3-a688-3c9615c1da51",
        "2026-08-19T04:46:25.889Z",
        "sha256:7f97b7f8b6a7ca9cad7f54e02836a185b41c5ea158d70cb31f0867a174f13329",
        "sha256:f5d60aee9cc02d100ec6f2b660ade76f951e7b113fe0ae95397e1ca6d2cbbc69",
        "继续任务，七夕优先上传，其余快车道随后",
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
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise C2ReleaseBridgeError(f"{label} is missing or unsafe") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise C2ReleaseBridgeError(f"{label} is missing or unsafe")


def _safe_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise C2ReleaseBridgeError(f"{label} is missing or unsafe") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise C2ReleaseBridgeError(f"{label} is missing or unsafe")


def _safe_input(path: Path, label: str, *, directory: bool = False) -> Path:
    """Make an absolute path without resolving through any symlink."""
    absolute = path.absolute()
    chain = [absolute, *absolute.parents]
    for component in chain:
        try:
            mode = component.lstat().st_mode
        except OSError as exc:
            raise C2ReleaseBridgeError(f"{label} parent is missing or unsafe") from exc
        if stat.S_ISLNK(mode):
            raise C2ReleaseBridgeError(f"{label} symlink is forbidden")
    if directory:
        _safe_directory(absolute, label)
    else:
        _regular(absolute, label)
    return absolute


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_create_only(path: Path, label: str) -> None:
    _safe_directory(path.parent, f"{label} parent")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise C2ReleaseBridgeError(f"create-only directory already exists: {path}") from exc
    _safe_directory(path, label)
    _fsync_directory(path.parent)


def _copy_regular(source: Path, destination: Path) -> None:
    _regular(source, str(source))
    _safe_directory(destination.parent, "copy target parent")
    source_flags = os.O_RDONLY
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    try:
        destination_fd = os.open(destination, destination_flags, 0o600)
    except FileExistsError as exc:
        os.close(source_fd)
        raise C2ReleaseBridgeError(f"create-only target already exists: {destination}") from exc
    try:
        with os.fdopen(source_fd, "rb", closefd=True) as input_file, os.fdopen(destination_fd, "wb", closefd=True) as output_file:
            while chunk := input_file.read(1024 * 1024):
                offset = 0
                while offset < len(chunk):
                    written = output_file.write(chunk[offset:])
                    if not isinstance(written, int) or written <= 0:
                        raise C2ReleaseBridgeError("copy short write")
                    offset += written
            output_file.flush()
            os.fsync(output_file.fileno())
    finally:
        # fdopen owns successful descriptors; this only handles partial opens.
        for fd in (source_fd, destination_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    _fsync_directory(destination.parent)
    if destination.is_symlink() or sha256(source) != sha256(destination):
        raise C2ReleaseBridgeError(f"copy hash drift: {source.name}")


def _write_create_only_json(path: Path, value: Mapping[str, object]) -> None:
    _safe_directory(path.parent, "JSON target parent")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise C2ReleaseBridgeError(f"create-only target already exists: {path}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _copy_formal_tree(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise C2ReleaseBridgeError("formal target already exists")
    _mkdir_create_only(destination, "formal target directory")
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_symlink():
            raise C2ReleaseBridgeError(f"formal source symlink: {relative}")
        target = destination / relative
        if path.is_dir():
            _mkdir_create_only(target, "formal target directory")
        elif path.is_file():
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
    required = {"schema_version", "candidate_id", "recording_date", "title", "direct_ivan_lines", "remaining_machine_gates", "self_seal"}
    if set(payload) != required or any(payload.get(key) != value for key, value in {"schema_version": AUTH_SCHEMA, "candidate_id": CID, "recording_date": DATE, "title": TITLE}.items()):
        raise C2ReleaseBridgeError("C2 direct-upload authorization identity drift")
    lines = payload.get("direct_ivan_lines")
    if not isinstance(lines, list) or [row.get("line") for row in lines if isinstance(row, Mapping)] != list(DIRECT_IVAN_LINES):
        raise C2ReleaseBridgeError("C2 direct-upload authorization line set drift")
    for row in lines:
        expected = DIRECT_IVAN_LINES.get(row.get("line")) if isinstance(row, Mapping) else None
        if not isinstance(row, Mapping) or set(row) != {"line", "uuid", "timestamp", "raw_line_sha256", "content_sha256", "quote"} or expected is None or tuple(row.get(key) for key in ("uuid", "timestamp", "raw_line_sha256", "content_sha256", "quote")) != expected:
            raise C2ReleaseBridgeError("C2 direct-upload authorization evidence invalid")
    gates = [
        "accepted C2 root technical receipt bound to current formal audit and artifacts",
        "current C2 release-package audit",
        "CPA title-cover joint QC for exact final title and cover",
        "authorized-upload manifest verify",
        "single serialized upload and public Creator section reconciliation",
    ]
    if payload.get("remaining_machine_gates") != gates:
        raise C2ReleaseBridgeError("C2 direct-upload authorization gate set drift")
    seal = payload.get("self_seal")
    unsigned = dict(payload)
    unsigned.pop("self_seal", None)
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if seal != {"canonical_json_without_self_seal_sha256": "sha256:" + hashlib.sha256(canonical).hexdigest()}:
        raise C2ReleaseBridgeError("C2 direct-upload authorization self seal drift")


def _validate_formal_audit(formal: Path) -> None:
    """Replay the source formal package before accepting it as a bridge input."""
    from scripts.audit_lidousha_review_package import audit_package

    saved = _read_object(formal / "package_audit.json", "C2 formal package audit")
    current = audit_package(formal)
    if current != saved or current.get("passed") is not True or current.get("blocking_issue_count") != 0:
        raise C2ReleaseBridgeError("C2 formal package audit is not a current passing replay")


def _validate_root_receipt(formal: Path, proposal: Path, receipt: Path) -> None:
    """Mandatory C2 receipt replay; no local compatibility substitute exists."""
    from .fastlane_c2_technical_receipt import validate_accepted_receipt

    _regular(proposal, "C2 ready proposal")
    _regular(receipt, "C2 root receipt")
    try:
        validate_accepted_receipt(formal, proposal, _read_object(receipt, "C2 root receipt"))
    except ValueError as exc:
        raise C2ReleaseBridgeError("C2 accepted root receipt rejected") from exc


def _receipt_bound_proposal(formal: Path, supplied: Path, receipt: Path) -> Path:
    """Resolve only the exact proposal self-bound by the accepted receipt."""
    _regular(receipt, "C2 root receipt")
    receipt_data = _read_object(receipt, "C2 root receipt")
    binding = receipt_data.get("proposal")
    if not isinstance(binding, Mapping) or set(binding) != {"path", "bytes", "sha256"}:
        raise C2ReleaseBridgeError("C2 receipt proposal binding invalid")
    name, byte_count, digest = binding["path"], binding["bytes"], binding["sha256"]
    if not isinstance(name, str) or Path(name).name != name or not isinstance(byte_count, int) or not isinstance(digest, str):
        raise C2ReleaseBridgeError("C2 receipt proposal binding unsafe")
    bound = formal / name
    _regular(bound, "C2 receipt-bound proposal")
    if bound.stat().st_size != byte_count or digest != "sha256:" + sha256(bound):
        raise C2ReleaseBridgeError("C2 receipt proposal binding drift")
    if supplied.resolve() != bound.resolve():
        raise C2ReleaseBridgeError("C2 supplied proposal is not receipt-bound proposal")
    return bound


def _valid_tags(result: Mapping[str, object]) -> list[str]:
    if result.get("status") not in {"OK", "OK_NO_LLM"}:
        raise C2ReleaseBridgeError("C2 tag generation failed")
    tags = result.get("final_tags")
    if not isinstance(tags, list) or not tags or len(tags) > 10:
        raise C2ReleaseBridgeError("C2 tag generation produced no valid tag line")
    clean = [str(tag).strip() for tag in tags]
    if any(not tag or len(tag) > 20 or any(c in tag for c in ",，\n\t") for tag in clean):
        raise C2ReleaseBridgeError("C2 tag generation produced invalid tag")
    if len({tag.casefold() for tag in clean}) != len(clean):
        raise C2ReleaseBridgeError("C2 tag generation produced duplicate tags")
    return clean


def _validate_legacy_execution_contract(formal: Path, proposal: Path, contract: Path, authorization: Path, receipt: Path) -> None:
    from .fastlane_c2_legacy_recovery import validate_accepted_execution_contract, validate_proposal
    _regular(proposal, "C2 legacy proposal")
    _regular(contract, "C2 legacy execution contract")
    if proposal.name != LEGACY_PROPOSAL_NAME or contract.name != LEGACY_CONTRACT_NAME:
        raise C2ReleaseBridgeError("C2 legacy input basename drift")
    try:
        validate_proposal(_read_object(proposal, "C2 legacy proposal"), formal=formal, authorization=authorization, receipt=receipt)
        validate_accepted_execution_contract(_read_object(contract, "C2 legacy execution contract"), proposal=proposal)
    except ValueError as exc:
        raise C2ReleaseBridgeError("C2 legacy execution contract rejected") from exc


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
    ready_proposal: Path,
    root_receipt: Path,
    authorization: Path,
    legacy_proposal: Path,
    legacy_execution_contract: Path,
    out: Path,
    tag_generator: Callable[..., dict] = generate_upload_tags,
) -> Path:
    """Create one private C2 projection.  It never calls upload or CPA image QC."""
    formal_package = _safe_input(formal_package, "formal package", directory=True)
    ready_proposal = _safe_input(ready_proposal, "ready proposal")
    root_receipt = _safe_input(root_receipt, "root receipt")
    authorization = _safe_input(authorization, "authorization")
    legacy_proposal = _safe_input(legacy_proposal, "legacy proposal")
    legacy_execution_contract = _safe_input(legacy_execution_contract, "legacy execution contract")
    out = out.absolute()
    for parent in out.parents:
        _safe_directory(parent, "C2 release output parent")
    if out.exists() or out.is_symlink():
        raise C2ReleaseBridgeError("C2 release package output already exists")
    if audit_fastlane_c2_formal_package(formal_package):
        raise C2ReleaseBridgeError("C2 source formal package is not current/passing")
    _validate_formal_audit(formal_package)
    _regular(authorization, "authorization")
    ready_proposal = _receipt_bound_proposal(formal_package, ready_proposal, root_receipt)
    _validate_root_receipt(formal_package, ready_proposal, root_receipt)
    _validate_authorization(_read_object(authorization, "authorization"))
    _validate_legacy_execution_contract(formal_package, legacy_proposal, legacy_execution_contract, authorization, root_receipt)

    _mkdir_create_only(out, "C2 release package output")
    try:
        _copy_formal_tree(formal_package, out / FORMAL_DIR)
        formal = out / FORMAL_DIR
        proposal_name = ready_proposal.name
        _copy_regular(ready_proposal, out / proposal_name)
        _copy_regular(root_receipt, out / ROOT_RECEIPT_NAME)
        _copy_regular(authorization, out / AUTH_NAME)
        _copy_regular(legacy_proposal, out / LEGACY_PROPOSAL_NAME)
        _copy_regular(legacy_execution_contract, out / LEGACY_CONTRACT_NAME)
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
        _write_create_only_json(out / TAG_RECEIPT_NAME, tag_receipt)
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
            "legacy_execution_contract": _sha_entry(out / LEGACY_CONTRACT_NAME),
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
        _write_create_only_json(out / RECORD_NAME, record)
        _write_create_only_json(out / PUBLISH_NAME, publish)
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
                "ready_proposal": _sha_entry(out / proposal_name),
                "legacy_proposal": _sha_entry(out / LEGACY_PROPOSAL_NAME),
                "legacy_execution_contract": _sha_entry(out / LEGACY_CONTRACT_NAME),
            },
            "tag_generation": _sha_entry(out / TAG_RECEIPT_NAME),
        }
        _write_create_only_json(out / "review_manifest.json", manifest)
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
        auth_path = root / AUTH_NAME
        receipt_path = root / ROOT_RECEIPT_NAME
        _regular(auth_path, "authorization")
        _regular(receipt_path, "root receipt")
        _validate_formal_audit(formal)
        _validate_authorization(_read_object(auth_path, "authorization"))
        receipt_data = _read_object(receipt_path, "root receipt")
        binding = receipt_data.get("proposal")
        candidate_name = binding.get("path") if isinstance(binding, Mapping) else "__invalid__"
        proposal_path = _receipt_bound_proposal(formal, formal / candidate_name, receipt_path)
        _validate_root_receipt(formal, proposal_path, receipt_path)
        legacy_proposal, legacy_contract = root / LEGACY_PROPOSAL_NAME, root / LEGACY_CONTRACT_NAME
        _validate_legacy_execution_contract(formal, legacy_proposal, legacy_contract, auth_path, receipt_path)
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
        if record.get("legacy_execution_contract") != _sha_entry(legacy_contract) or "story_contract" in record:
            raise C2ReleaseBridgeError("C2 legacy execution contract drift")
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
            "ready_proposal": proposal_path,
            "legacy_proposal": legacy_proposal,
            "legacy_execution_contract": legacy_contract,
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
