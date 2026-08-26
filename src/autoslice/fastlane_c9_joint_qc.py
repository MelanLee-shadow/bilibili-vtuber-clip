"""Repository-sealed, provider-free C9 title/cover joint-QC derivation.

The only permitted input is the exact C9 direct Ivan authority plus its two
already observed predecessor receipts.  This module never calls a provider and
never maps generic title/cover evidence into C9.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

CID = "auto_143025_1112_1285"
DATE = "2026-08-15"
BASE = Path("assets/lidousha/fastlane_c9_private")
AUTHORITY_NAME = f"{CID}.ivan-direct-title-cover-authority.v1.json"
JOINT_QC_NAME = f"{CID}.derived-joint-title-cover-qc.v1.json"
HOOK_SHA = "sha256:241e2fb0046dd3d692f06133030ce5f9093f94e6b8ee6fd041c4f9db076c38cd"
TITLE_SHA = "sha256:334bb710f4099657c04beb2fa7ec9dc98c18b9817882a65eb23fa501222cd736"
COVER_SHA = "sha256:4222118c60e02b449aa75abf53ce36fef43a2b4a8f7ea34fb7b83428edec8d19"
AUTHORITY_SCHEMA = "fastlane-c9-ivan-direct-title-cover-authority.v1"
JOINT_SCHEMA = "fastlane-c9-derived-joint-title-cover-qc.v1"


class FastlaneC9JointQCError(ValueError):
    """The exact C9 authority or derived receipt cannot be replayed."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha_object(value: object) -> str:
    return _sha_bytes(_canonical(value))


def _safe_parts(path: Path) -> None:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        info = os.lstat(cursor)
        if stat.S_ISLNK(info.st_mode):
            raise FastlaneC9JointQCError("C9_JOINT_QC_SYMLINK_UNSAFE")


def _opened_bytes(path: Path, *, label: str) -> bytes:
    path = Path(path).absolute()
    _safe_parts(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise FastlaneC9JointQCError(f"C9_JOINT_QC_{label}_UNREADABLE") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise FastlaneC9JointQCError("C9_JOINT_QC_HARDLINK_UNSAFE")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink
        ):
            raise FastlaneC9JointQCError("C9_JOINT_QC_OPENED_BYTES_DRIFT")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise FastlaneC9JointQCError("C9_JOINT_QC_OPENED_BYTES_DRIFT")
        return data
    finally:
        os.close(fd)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_opened_bytes(path, label=label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastlaneC9JointQCError(f"C9_JOINT_QC_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise FastlaneC9JointQCError(f"C9_JOINT_QC_{label}_INVALID")
    return value


def _verify_self(document: Mapping[str, Any], *, code: str) -> None:
    declared = document.get("self_sha256")
    unsigned = dict(document)
    unsigned.pop("self_sha256", None)
    if declared != _sha_object(unsigned):
        raise FastlaneC9JointQCError(code)


def _verify_predecessor(name: str, value: object, *, authority: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSOR_INVALID")
    _verify_self(value, code="C9_JOINT_QC_PREDECESSOR_SELF_HASH_DRIFT")
    if value.get("candidate_id") != CID or value.get("recording_date") != DATE:
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSOR_SCOPE_INVALID")
    if value.get("selection_hook_sha256") != HOOK_SHA or value.get("title_sha256") != TITLE_SHA:
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSOR_TUPLE_DRIFT")
    if value.get("status") != "PASS" or value.get("provider") != "cpa" or value.get("model") != "gpt-5.6-sol" or value.get("provider_status") != "OBSERVED":
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSOR_PROVIDER_INVALID")
    if name == "cpa_redraw":
        if value.get("source_path") != "/opt/bilive/autoslice/out/2026-08-15/auto_143025_1112_1285/replacement_recuts/cover_refs/auto_143025_1112_1285.cover-ref.png" or value.get("source_bytes_sha256") != "sha256:1138aa08326fd2f7df5e4a871e5c4cc96eddc12570a563856785bc4d1526f958" or value.get("receipt_path") != "/opt/bilive/autoslice/out/2026-08-15/auto_143025_1112_1285/replacement_recuts/evidence/auto_143025_1112_1285.cover-source-composition-verification.json" or value.get("receipt_sha256") != "sha256:bf41946b6dbd5a73f3eeed028708edd4bc56d37f5249a3875e6c3308899d2c21" or value.get("witness_receipt_sha256") != "sha256:98a707b1cc77732375ce7696cfcc16c6517528006115be0e558cf01cb6628b44":
            raise FastlaneC9JointQCError("C9_JOINT_QC_REDRAW_PREDECESSOR_BINDING_INVALID")
        verdict = value.get("verdict")
        if value.get("selected_treatment") != "cpa_redraw" or not isinstance(verdict, Mapping) or verdict.get("cpa_redraw_recommended") is not True:
            raise FastlaneC9JointQCError("C9_JOINT_QC_REDRAW_PREDECESSOR_INVALID")
    elif name == "host_identity":
        if value.get("final_cover_path") != "/opt/bilive/autoslice/out/2026-08-15/auto_143025_1112_1285/replacement_recuts/covers/auto_143025_1112_1285.ai-title.cover.png" or value.get("final_cover_bytes_sha256") != COVER_SHA or value.get("comparison_path") != "/opt/bilive/autoslice/out/2026-08-15/auto_143025_1112_1285/replacement_recuts/covers/auto_143025_1112_1285.ai-title.cover.host-identity-witness.png" or value.get("comparison_bytes_sha256") != "sha256:0923ac83fe752bedbff921e44fdc4e1476e690b2e3a2e71d7a520a40e9e306ea" or value.get("witness_response_sha256") != "sha256:1bedd1956ddc8780e4186fd64326e14db438fa174c9f256ad4e0d40caf22f4ea":
            raise FastlaneC9JointQCError("C9_JOINT_QC_HOST_PREDECESSOR_BINDING_INVALID")
        if value.get("final_cover_bytes_sha256") != COVER_SHA:
            raise FastlaneC9JointQCError("C9_JOINT_QC_HOST_COVER_TUPLE_DRIFT")
        verdict = value.get("verdict")
        if not isinstance(verdict, Mapping) or any(verdict.get(key) is not True for key in (
            "source_lidousha_located", "primary_subject_is_lidousha", "primary_subject_is_visually_dominant",
            "primary_subject_face_is_large_and_clear", "primary_subject_carries_story_reaction", "thumbnail_has_clear_click_hook",
        )) or verdict.get("primary_subject_matches_other_source_participant") is not False or verdict.get("identity_conflicts") != [] or verdict.get("composition_conflicts") != []:
            raise FastlaneC9JointQCError("C9_JOINT_QC_HOST_PREDECESSOR_INVALID")
    else:
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSOR_NAME_INVALID")
    return dict(value)


def validate_c9_direct_authority(repo_root: Path) -> dict[str, Any]:
    """Validate the exact repository authority and both predecessor PASS receipts."""
    repo_root = Path(repo_root).absolute()
    authority_path = repo_root / BASE / AUTHORITY_NAME
    authority_raw = _opened_bytes(authority_path, label="AUTHORITY")
    try:
        authority = json.loads(authority_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastlaneC9JointQCError("C9_JOINT_QC_AUTHORITY_INVALID") from exc
    if not isinstance(authority, dict):
        raise FastlaneC9JointQCError("C9_JOINT_QC_AUTHORITY_INVALID")
    _verify_self(authority, code="C9_JOINT_QC_AUTHORITY_SELF_HASH_DRIFT")
    expected_authority_keys = {
        "schema_version", "candidate_id", "recording_date", "authorization_label",
        "selection_hook_sha256", "title", "title_sha256", "cover_path", "cover_sha256",
        "provider_attempted", "direct_authority", "predecessors", "constraints", "self_sha256",
    }
    if set(authority) != expected_authority_keys or authority.get("schema_version") != AUTHORITY_SCHEMA or authority.get("candidate_id") != CID or authority.get("recording_date") != DATE or authority.get("authorization_label") != "授权专属 derived joint-QC（Recommended）":
        raise FastlaneC9JointQCError("C9_JOINT_QC_AUTHORITY_SCOPE_INVALID")
    if authority.get("selection_hook_sha256") != HOOK_SHA or authority.get("title_sha256") != TITLE_SHA or authority.get("cover_sha256") != COVER_SHA:
        raise FastlaneC9JointQCError("C9_JOINT_QC_AUTHORITY_TUPLE_INVALID")
    title = authority.get("title")
    if not isinstance(title, str) or _sha_bytes(title.encode("utf-8")) != TITLE_SHA:
        raise FastlaneC9JointQCError("C9_JOINT_QC_TITLE_BYTES_INVALID")
    if authority.get("provider_attempted") is not False:
        raise FastlaneC9JointQCError("C9_JOINT_QC_PROVIDER_ATTEMPTED")
    constraints = authority.get("constraints")
    expected_constraints = {
        "exact_c9_only": True, "title_cover_bytes_frozen": True, "generic_mapping_forbidden": True,
        "other_candidate_date_hash_forbidden": True, "provider_result_relabel_forbidden": True,
        "canonical_delivery_allowed": False, "upload_allowed": False, "state_write_allowed": False, "remote_allowed": False,
    }
    if constraints != expected_constraints:
        raise FastlaneC9JointQCError("C9_JOINT_QC_CONSTRAINTS_INVALID")
    direct = authority.get("direct_authority")
    expected_direct = {
        "tool_call": "call_qBbVkqwbu1TfEsh1JKX7p8Xg|fc_0b3cd3bcdc4f5bfb016a8edacc2a6887d1a1bc868d5e96b950",
        "assistant_message_id": "eda3f113-96a5-4926-a277-ba7a1b7c3678",
        "tool_result_user_message_id": "59201f5f-35ec-483a-9c63-a349f6e1d454",
        "sequence": 535090,
        "step": 371,
        "turn": 14,
        "epoch_ms": 1787747929999,
        "answer_sha256": "sha256:6678e47d95a4a67fa834dcdcf667a44d8f8b8f16685baf1e17e8560268b9194f",
    }
    if direct != expected_direct:
        raise FastlaneC9JointQCError("C9_JOINT_QC_DIRECT_AUTHORITY_INVALID")
    cover_raw = authority.get("cover_path")
    if cover_raw != str((BASE / f"{CID}.cover.png").as_posix()):
        raise FastlaneC9JointQCError("C9_JOINT_QC_COVER_PATH_INVALID")
    cover = repo_root / Path(cover_raw)
    if _sha_bytes(_opened_bytes(cover, label="COVER")) != COVER_SHA:
        raise FastlaneC9JointQCError("C9_JOINT_QC_COVER_BYTES_DRIFT")
    predecessors = authority.get("predecessors")
    if not isinstance(predecessors, Mapping) or set(predecessors) != {"cpa_redraw", "host_identity"}:
        raise FastlaneC9JointQCError("C9_JOINT_QC_PREDECESSORS_MISSING")
    redraw = _verify_predecessor("cpa_redraw", predecessors.get("cpa_redraw"), authority=authority)
    host = _verify_predecessor("host_identity", predecessors.get("host_identity"), authority=authority)
    return {"authority": authority, "authority_sha256": _sha_bytes(authority_raw), "cover_sha256": COVER_SHA, "title": title, "cpa_redraw": redraw, "host_identity": host}


def derive_c9_joint_qc(repo_root: Path) -> dict[str, Any]:
    """Derive a PASS receipt without invoking or relabelling any provider result."""
    checked = validate_c9_direct_authority(repo_root)
    receipt: dict[str, Any] = {
        "schema_version": JOINT_SCHEMA,
        "candidate_id": CID,
        "recording_date": DATE,
        "selection_hook_sha256": HOOK_SHA,
        "title": checked["title"],
        "title_sha256": TITLE_SHA,
        "cover_path": str((BASE / f"{CID}.cover.png").as_posix()),
        "cover_sha256": COVER_SHA,
        "status": "PASS",
        "provider_attempted": False,
        "provider_result": False,
        "derivation": "C9-specific derived joint-QC from exact predecessor CPA redraw PASS and host-identity PASS; no new provider result.",
        "authority_sha256": checked["authority_sha256"],
        "predecessor_receipts": {
            "cpa_redraw": checked["cpa_redraw"],
            "host_identity": checked["host_identity"],
        },
        "constraints": {
            "exact_c9_only": True,
            "title_cover_bytes_frozen": True,
            "generic_mapping_forbidden": True,
            "provider_result_relabel_forbidden": True,
            "canonical_delivery_allowed": False,
            "upload_allowed": False,
            "state_write_allowed": False,
        },
    }
    receipt["self_sha256"] = _sha_object(receipt)
    return receipt


def validate_c9_joint_qc(receipt: Mapping[str, Any], repo_root: Path) -> bool:
    """Replay a derived receipt and its authority/cover binding."""
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != JOINT_SCHEMA:
        return False
    try:
        _verify_self(receipt, code="C9_JOINT_QC_RECEIPT_SELF_HASH_DRIFT")
        checked = validate_c9_direct_authority(repo_root)
        if receipt.get("candidate_id") != CID or receipt.get("recording_date") != DATE or receipt.get("selection_hook_sha256") != HOOK_SHA or receipt.get("title_sha256") != TITLE_SHA or receipt.get("cover_sha256") != COVER_SHA or receipt.get("cover_path") != str((BASE / f"{CID}.cover.png").as_posix()) or receipt.get("title") != checked["title"] or receipt.get("status") != "PASS" or receipt.get("provider_attempted") is not False or receipt.get("provider_result") is not False or receipt.get("authority_sha256") != checked["authority_sha256"]:
            return False
        if receipt.get("predecessor_receipts") != {"cpa_redraw": checked["cpa_redraw"], "host_identity": checked["host_identity"]}:
            return False
        return receipt.get("constraints") == {
            "exact_c9_only": True, "title_cover_bytes_frozen": True, "generic_mapping_forbidden": True,
            "provider_result_relabel_forbidden": True, "canonical_delivery_allowed": False,
            "upload_allowed": False, "state_write_allowed": False,
        }
    except FastlaneC9JointQCError:
        return False


def materialize_c9_joint_qc(*, repo_root: Path, output_path: Path) -> dict[str, Any]:
    """Create one repository-sealed derived receipt, refusing overwrite/symlink."""
    output_path = Path(output_path).absolute()
    if output_path.exists() or output_path.is_symlink():
        raise FastlaneC9JointQCError("C9_JOINT_QC_OUTPUT_NOT_CREATE_ONLY")
    receipt = derive_c9_joint_qc(repo_root)
    _safe_parts(output_path.parent)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.parent.is_symlink():
        raise FastlaneC9JointQCError("C9_JOINT_QC_OUTPUT_PARENT_UNSAFE")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(output_path, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise FastlaneC9JointQCError("C9_JOINT_QC_OUTPUT_NOT_CREATE_ONLY") from exc
    return receipt
