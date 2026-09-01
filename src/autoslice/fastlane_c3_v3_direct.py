"""C3 v3 private exact-byte closure and provider-free PASS0 lane.

The v2 successor is immutable input.  This module derives a new v3 closure by
copying the fifteen sealed v2 roles and exactly five explicitly allow-listed
auxiliary preimages.  It never searches for inputs, rewrites v2, calls a
provider, or writes an authoritative target.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

CANDIDATE_ID = "auto_220021_561_670"
RECORDING_DATE = "2026-08-13"
V3_RELATIVE_ROOT = Path("out") / RECORDING_DATE / CANDIDATE_ID / "c3-successor-authority-v3"
V2_SCHEMA = "fastlane-c3-successor-authority-manifest.v2"
V3_SCHEMA = "fastlane-c3-successor-authority-manifest.v3"
_SCOPE = "private_exact_accepted_byte_carry_only"
_FORBIDDEN = ["provider", "fresh_review", "state", "deploy", "upload"]
_SHA = "sha256:"
_APPROVED_V2_RAW_SHA = "sha256:a8a59579942ae9d55b8db0d4c3f8295b4ab3c178041c4b9dcb170021b1f35676"
_APPROVED_V2_SELF_SEAL = "sha256:9d880c5ab92bb6217d88f62a6ca8a2a23519f88cdb04b92263bbc2e71c2cc1d4"
_APPROVED_V3_RAW_SHA = "sha256:ecc19afdfa30bc3c978d1c29ef5a407f387251c04912b02ebcdbf37bc0de9f75"
_APPROVED_V3_SELF_SEAL = "sha256:0a5465acbb7d6457894638f9d62cbda59320474209ef8374c9b91213f9c15fa2"
_APPROVED_V3_TREE_SHA = "sha256:7afbc9e4489f523890538402d497d2f9075a77387bbcf2c5ee3654547b7577f2"
_APPROVED_V2_SOURCE_AUTHORITIES = {
    "final_grid_authority_sha256": "sha256:6800bbb1bb12267abdfc6484148bd531ab7173b8187a144fc46a53ca6df860a6",
    "final_grid_file_sha256": "sha256:217feaddc1993a1b7ba8e7ae78228697faa06daa6fba4bc8bbdbe6752f1d18fd",
    "r7e_final_grid_record_sha256": "sha256:444785698c67fec556e161448d76d135cf1cae956d053ddf2b6e50266f5817db",
    "record_chat_rebind_authority_sha256": "sha256:edabdbd3212758b24e01f8bebfe8a01fb10ded1fcb068c5163872f434745e5a6",
    "record_chat_rebind_file_sha256": "sha256:a19084d53aba632730d25acf9f4abf221b493994ce4a3e01b2373447dd05c064",
}
_APPROVED_V2_ENDPOINT = {
    "final_closure_cue_index": 34,
    "final_snapped_end_ms": 108940,
    "source_final_end_ms": 118730,
    "source_final_start_ms": 9690,
}
_APPROVED_BASELINE_FILE_SHA = "sha256:fa1fee5f2ec29556d8713414963df32f3bdf2a0f2b6cdd56efc71937509adf30"
_APPROVED_DECISION_LEDGER_SHA = "54157e30df77040a4cf141c592f4508cfdc96113ed98aaed9d009c5c956c8472"
_APPROVED_TRUTH_DIFF_SHA = "473d0eafdde366db7a26ce35f8e1d0dcea3427f6ad3cf3b6ef41fa015612ae45"
_APPROVED_LINE947_AUTHORITY = "维护者/Claude line947 exhaustive C3 ruling"

# These are the only v2 roles admitted to the v3 closure.  Values are the
# sealed live-v2 bytes, not a mutable runtime observation.
V2_ROLES: Mapping[str, tuple[str, int, str]] = {
    "ass": ("auto_220021_561_670.recut.burned-successor-v2.speaker.ass", 1644, "sha256:d3160f325648915db0ebb6f4f05e48cc6e6cf25caccf726759c654d02829838a"),
    "burned_video": ("auto_220021_561_670.recut.burned-successor-v2.mp4", 36101691, "sha256:c5d49bdff6842298f9a9cc1907044faa20dc8cae3d7717e99e80bd51aa2f9faf"),
    "chat": ("auto_220021_561_670.recut.burned-successor-v2.chat-authority.json", 346728, "sha256:88728f603043603c59c77b0f57364309f8215640c9baa5a47d201df627e3981c"),
    "clip_context": ("auto_220021_561_670.recut.burned-successor-v2.clip-context.json", 37245, "sha256:fb2d67110858fe3fcc348278856005e45f1ab790785a7d3dd0615103eb423987"),
    "cover": ("auto_220021_561_670.recut.burned-successor-v2.cover.png", 2423123, "sha256:7e77ab5d0141879caa7d24ae264f9d3ab94ea9fbdc80d7a4633e9a0a07758df3"),
    "delivery_binding": ("auto_220021_561_670.recut.burned-successor-v2.operator-reviewed-delivery-binding.json", 1060, "sha256:9f116b3ff0b9d2601c0ef3405cc62cbe11c730d3b593ac039a5d2779a18a918f"),
    "delivery_video": ("auto_220021_561_670.recut.mp4", 29930967, "sha256:2e94ba7ae18e64903baca4cadb647b9545915778b19082e3cd0582d32e20c84e"),
    "plain_srt": ("auto_220021_561_670.recut.burned-successor-v2.reviewed-baseline.srt", 828, "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc"),
    "publish": ("auto_220021_561_670.recut.burned-successor-v2.publish.json", 51802, "sha256:f711b5db6522a7064570a548db48b413b5bf1a5c94614b36da82e544c493b09d"),
    "r7e_record": ("c3-final-grid-record.json", 79902, "sha256:444785698c67fec556e161448d76d135cf1cae956d053ddf2b6e50266f5817db"),
    "record": ("auto_220021_561_670.recut.burned-successor-v2.record.json", 111526, "sha256:76966e096593d1b132a1f9155d30093866b5b5f28df2141adb3794ce640c8a21"),
    "source_fact": ("auto_220021_561_670.recut.burned-successor-v2.terminal-source-fact-preservation.json", 6392, "sha256:72ba250c3c270016251fb25bb88550650b0f64c570f1df456bd2857c949960bd"),
    "speaker_manifest": ("auto_220021_561_670.recut.burned-successor-v2.speaker-finalization.json", 8967, "sha256:f6423e4477f8db100d26be453791598ea6a4af73a36a320c254d544bf598ee41"),
    "speaker_srt": ("auto_220021_561_670.recut.burned-successor-v2.speaker.srt", 960, "sha256:51eef37bf38a2e1a58e4412115c5f2a9699904df80a70f6d8a406d6dde206153"),
    "speaker_truth": ("auto_220021_561_670.recut.burned-successor-v2.operator-reviewed-speaker-truth.json", 2985, "sha256:feec7231174ae969f472f6c06f81f9b59f5c4d6379a53a07176b37161a98eb1d"),
}

# Source package is untrusted as a package.  These are five independent raw
# preimages whose hashes are already bound by the v2 parent role pointers.
AUXILIARY_ROLES: Mapping[str, tuple[str, str, int, str, str]] = {
    "cover_reference": ("auto_220021_561_670.recut.burned-successor-v2.cover.cover-ref.png", "cover_reference", 940018, "sha256:8c8c6b547ef03d41372a055adacd25368df0adc33dabad8c3262633a96ff0db8", "record.publish_staging.cover_generation.reference_image"),
    "cover_pre_overlay": ("auto_220021_561_670.recut.burned-successor-v2.cover.pre-overlay.png", "cover_pre_overlay", 2471867, "sha256:7d83e83bc8d1961929020c387ec28b995c82c7189a461153975e3851132d6be2", "record.publish_staging.cover_generation.pre_overlay_path"),
    "cover_background": ("auto_220021_561_670.recut.burned-successor-v2.cover.ai-bg.png", "cover_background", 2471867, "sha256:7d83e83bc8d1961929020c387ec28b995c82c7189a461153975e3851132d6be2", "record.publish_staging.cover_generation.ai_background"),
    "cover_title_mask": ("auto_220021_561_670.recut.burned-successor-v2.cover.title-mask.png", "cover_title_mask", 24466, "sha256:7300cd4932d7050fe82463d20bb48581c0cbb215680538ed9b2cfc725d9f0741", "record.publish_staging.cover_generation.rendered_text_pixels.mask_path"),
    "speaker_override": ("auto_220021_561_670.recut.burned-successor-v2.speaker-override.json", "speaker_override", 7475, "sha256:58daf1496d253b6e2e66a7997bbd42969fcea4325004bf5fc9c3ecf2e78e8581", "speaker_manifest.speaker_override"),
}
_AUX_POINTER_BASENAMES = {
    "cover_reference": "auto_220021_561_670.recut.burned-successor-v2.cover.cover-ref.png",
    "cover_pre_overlay": "auto_220021_561_670.cover.pre-overlay.png",
    "cover_background": "auto_220021_561_670.recut.burned-successor-v2.cover.ai-bg.png",
    "cover_title_mask": "auto_220021_561_670.cover.title-mask.png",
    "speaker_override": "auto_220021_561_670.recut.burned-successor-v2.speaker-override.json",
}
_AUX_PARENT_ROLES = {
    "cover_reference": ("record", "reference_sha256"),
    "cover_pre_overlay": ("record", "pre_overlay_sha256"),
    "cover_background": ("record", "ai_background_sha256"),
    "cover_title_mask": ("record", "mask_sha256"),
    "speaker_override": ("speaker_manifest", "speaker_override_sha256"),
}

class C3V3ClosureError(ValueError):
    """A v3 closure cannot be proven without guessing or mutation."""

@dataclass(frozen=True, slots=True)
class C3V3Authority:
    root: Path
    manifest_path: Path
    manifest_raw_sha256: str
    manifest_self_seal: str
    root_tree_sha256: str
    roles: Mapping[str, Path]

@dataclass(frozen=True, slots=True)
class C3DirectPass0Result:
    prepared_delivery: object
    private_package_binding: Mapping[str, object]
    private_audit_binding: Mapping[str, object]
    predicate_matrix: tuple[Mapping[str, object], ...]
    upload_allowed: bool = False
    provider_attempted: bool = False


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()

def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()

def _safe_dir(path: Path, *, label: str) -> Path:
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try: row = os.lstat(current)
        except OSError as exc: raise C3V3ClosureError(f"C3_V3_{label}_UNAVAILABLE") from exc
        if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode):
            raise C3V3ClosureError(f"C3_V3_{label}_UNSAFE")
    return path

def _open_regular(path: Path, *, label: str, require_private_mode: bool = True) -> tuple[int, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        row = os.fstat(fd)
    except OSError as exc:
        raise C3V3ClosureError(f"C3_V3_{label}_UNAVAILABLE") from exc
    if not stat.S_ISREG(row.st_mode) or row.st_nlink != 1:
        os.close(fd)
        raise C3V3ClosureError(f"C3_V3_{label}_NOT_REGULAR")
    if require_private_mode and stat.S_IMODE(row.st_mode) != 0o600:
        os.close(fd)
        raise C3V3ClosureError(f"C3_V3_{label}_MODE_DRIFT")
    return fd, row


def _read_regular(path: Path, *, label: str, require_private_mode: bool = True) -> bytes:
    fd, row = _open_regular(path, label=label, require_private_mode=require_private_mode)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (row.st_dev, row.st_ino, row.st_size, row.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_nlink):
            raise C3V3ClosureError(f"C3_V3_{label}_DRIFT")
        data = b"".join(chunks)
        if len(data) != row.st_size:
            raise C3V3ClosureError(f"C3_V3_{label}_DRIFT")
        return data
    finally:
        os.close(fd)

def _write_exclusive(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0: raise C3V3ClosureError("C3_V3_WRITE_FAILED")
            view = view[count:]
        os.fsync(fd)
    finally: os.close(fd)
    if stat.S_IMODE(os.lstat(path).st_mode) != 0o600: raise C3V3ClosureError("C3_V3_ROLE_MODE_DRIFT")

def _copy_exact(source: Path, target: Path, *, expected_size: int, expected_sha: str, label: str, source_private_mode: bool = True) -> None:
    """Copy from one opened source inode while hashing the bytes being copied."""
    _safe_dir(source.parent, label=f"{label}_SOURCE_PARENT")
    if target.exists() or target.is_symlink():
        raise C3V3ClosureError(f"C3_V3_{label}_COLLISION")
    source_fd, before = _open_regular(source, label=label, require_private_mode=source_private_mode)
    target_fd = -1
    digest = hashlib.sha256()
    copied = 0
    try:
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        while True:
            chunk = os.read(source_fd, 1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                count = os.write(target_fd, view)
                if count <= 0:
                    raise C3V3ClosureError("C3_V3_WRITE_FAILED")
                view = view[count:]
        after = os.fstat(source_fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_nlink):
            raise C3V3ClosureError(f"C3_V3_{label}_DRIFT")
        if copied != expected_size or _SHA + digest.hexdigest() != expected_sha:
            raise C3V3ClosureError(f"C3_V3_{label}_HASH_DRIFT")
        os.fsync(target_fd)
    except OSError as exc:
        raise C3V3ClosureError(f"C3_V3_{label}_COPY_FAILED") from exc
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        os.close(source_fd)
    if stat.S_IMODE(os.lstat(target).st_mode) != 0o600:
        raise C3V3ClosureError("C3_V3_ROLE_MODE_DRIFT")

def _tree_sha256(entries: Mapping[str, Mapping[str, object]]) -> str:
    return _sha(_canonical({key: entries[key] for key in sorted(entries)}))

def _v2_manifest(v2_root: Path) -> tuple[dict, bytes, str]:
    manifest_path = v2_root / "manifest.json"
    raw = _read_regular(manifest_path, label="V2_MANIFEST")
    try: doc = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise C3V3ClosureError("C3_V3_V2_MANIFEST_INVALID") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != V2_SCHEMA: raise C3V3ClosureError("C3_V3_V2_SCHEMA_DRIFT")
    if (doc.get("candidate_id"), doc.get("recording_date"), doc.get("authority_scope")) != (CANDIDATE_ID, RECORDING_DATE, _SCOPE): raise C3V3ClosureError("C3_V3_V2_IDENTITY_DRIFT")
    if doc.get("forbidden_operations") != _FORBIDDEN: raise C3V3ClosureError("C3_V3_V2_FORBIDDEN_OPERATIONS_DRIFT")
    declared = doc.get("manifest_sha256")
    unsigned = dict(doc); unsigned.pop("manifest_sha256", None)
    if not isinstance(declared, str) or declared != _sha(_canonical(unsigned)): raise C3V3ClosureError("C3_V3_V2_SELF_SEAL_DRIFT")
    raw_sha = _sha(raw)
    if raw_sha != _APPROVED_V2_RAW_SHA or declared != _APPROVED_V2_SELF_SEAL:
        raise C3V3ClosureError("C3_V3_V2_APPROVED_MANIFEST_DRIFT")
    if doc.get("source_authorities") != _APPROVED_V2_SOURCE_AUTHORITIES:
        raise C3V3ClosureError("C3_V3_V2_SOURCE_AUTHORITIES_DRIFT")
    if doc.get("endpoint") != _APPROVED_V2_ENDPOINT:
        raise C3V3ClosureError("C3_V3_V2_ENDPOINT_DRIFT")
    if set(doc.get("roles", {})) != set(V2_ROLES): raise C3V3ClosureError("C3_V3_V2_ROLE_SET_DRIFT")
    for role, (name, size, digest) in V2_ROLES.items():
        entry = doc["roles"].get(role)
        if entry != {"locator": name, "mode": "0600", "sha256": digest, "size": size}: raise C3V3ClosureError("C3_V3_V2_ROLE_DESCRIPTOR_DRIFT")
        data = _read_regular(v2_root / name, label=f"V2_{role.upper()}")
        if len(data) != size or _sha(data) != digest: raise C3V3ClosureError("C3_V3_V2_ROLE_HASH_DRIFT")
    return doc, raw, _sha(raw)

def _validate_baseline_binding(repo_root: Path) -> None:
    base = _safe_dir(repo_root / "assets/lidousha/reviewed_subtitle_baselines", label="BASELINE_PARENT")
    baseline = base / f"{CANDIDATE_ID}.subtitle-baseline.v1.json"
    data = _read_regular(baseline, label="BASELINE", require_private_mode=False)
    if _sha(data) != _APPROVED_BASELINE_FILE_SHA:
        raise C3V3ClosureError("C3_V3_BASELINE_FILE_DRIFT")
    try:
        doc = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C3V3ClosureError("C3_V3_BASELINE_INVALID") from exc
    ownership = doc.get("operator_text_full_ownership", {})
    if (doc.get("candidate_id"), doc.get("schema_version"), doc.get("exact_interval_replay"), doc.get("authority")) != (CANDIDATE_ID, "subtitle-redelivery-baseline.v2", True, _APPROVED_LINE947_AUTHORITY):
        raise C3V3ClosureError("C3_V3_BASELINE_BINDING_DRIFT")
    if doc.get("sha256") != "d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc" or ownership.get("speaker_authority") != "REVIEWER_LINE947_EXHAUSTIVE":
        raise C3V3ClosureError("C3_V3_BASELINE_HASH_DRIFT")
    for name, expected in (("operator-decisions.v3.json", _APPROVED_DECISION_LEDGER_SHA), ("operator-truth-diff.v2.json", _APPROVED_TRUTH_DIFF_SHA)):
        if _sha(_read_regular(base / f"{CANDIDATE_ID}.{name}", label=f"BASELINE_{name.upper()}", require_private_mode=False)) != _SHA + expected:
            raise C3V3ClosureError("C3_V3_BASELINE_AUXILIARY_HASH_DRIFT")
    lanes = doc.get("operator_truth_lanes", {})
    if not isinstance(lanes, dict) or lanes.get("decision_ledger", {}).get("sha256") != _APPROVED_DECISION_LEDGER_SHA or lanes.get("diff_receipt", {}).get("sha256") != _APPROVED_TRUTH_DIFF_SHA:
        raise C3V3ClosureError("C3_V3_BASELINE_AUXILIARY_BINDING_DRIFT")

def _json_pointer(document: object, pointer: str) -> object:
    current = document
    for part in pointer.split(".")[1:]:
        if not isinstance(current, dict) or part not in current:
            raise C3V3ClosureError("C3_V3_AUXILIARY_POINTER_MISSING")
        current = current[part]
    return current


def _validate_auxiliary_documents(parents: Mapping[str, Mapping[str, object]]) -> None:
    for role, (name, _kind, size, digest, pointer) in AUXILIARY_ROLES.items():
        parent_role, hash_field = _AUX_PARENT_ROLES[role]
        value = _json_pointer(parents[parent_role], pointer)
        if not isinstance(value, str) or Path(value).name != _AUX_POINTER_BASENAMES[role]:
            raise C3V3ClosureError("C3_V3_AUXILIARY_POINTER_DRIFT")
        parent = parents[parent_role]
        # Hash fields live in the same cover_generation object as their path;
        # the speaker manifest keeps the override hash beside its locator.
        hash_value = None
        if role == "cover_title_mask":
            hash_value = _json_pointer(parent, "record.publish_staging.cover_generation.rendered_text_pixels." + hash_field)
        elif role.startswith("cover_"):
            hash_value = _json_pointer(parent, "record.publish_staging.cover_generation." + hash_field)
        else:
            hash_value = parent.get(hash_field)
        if isinstance(hash_value, str) and not hash_value.startswith(_SHA):
            hash_value = _SHA + hash_value
        if hash_value != digest:
            raise C3V3ClosureError("C3_V3_AUXILIARY_POINTER_HASH_DRIFT")


def _validate_auxiliary_preimages(v2_root: Path, *, v2_raw_sha: str, v2_doc: Mapping[str, object]) -> None:
    parents: dict[str, dict] = {}
    for role in ("record", "speaker_manifest"):
        try:
            value = json.loads(_read_regular(v2_root / V2_ROLES[role][0], label=f"V2_{role.upper()}_PARENT").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise C3V3ClosureError("C3_V3_AUXILIARY_PARENT_INVALID") from exc
        if not isinstance(value, dict):
            raise C3V3ClosureError("C3_V3_AUXILIARY_PARENT_INVALID")
        parents[role] = value
    _validate_auxiliary_documents(parents)


def _publish_create_only(staging: Path, destination: Path) -> None:
    """Publish a directory without replacing a concurrently-created target."""
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex = getattr(libc, "renamex_np", None)
        if renamex is None:
            raise C3V3ClosureError("C3_V3_CREATE_ONLY_UNAVAILABLE")
        renamex.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex.restype = ctypes.c_int
        if renamex(os.fsencode(staging), os.fsencode(destination), 0x00000004) != 0:
            error = ctypes.get_errno()
            if error == 17:
                raise C3V3ClosureError("C3_V3_DESTINATION_EXISTS")
            raise C3V3ClosureError("C3_V3_PUBLISH_FAILED")
        return
    # Linux's renameat2 is the only portable create-only directory primitive.
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise C3V3ClosureError("C3_V3_CREATE_ONLY_UNAVAILABLE")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise C3V3ClosureError("C3_V3_DESTINATION_EXISTS")
        raise C3V3ClosureError("C3_V3_PUBLISH_FAILED")


def build_c3_v3_closure(*, v2_root: Path, auxiliary_root: Path, destination: Path, repo_root: Path) -> C3V3Authority:
    """Derive one new create-only v3 root from verified byte preimages."""
    v2_root = _safe_dir(v2_root, label="V2_ROOT")
    auxiliary_root = _safe_dir(auxiliary_root, label="AUXILIARY_ROOT")
    destination = Path(destination).absolute()
    _safe_dir(destination.parent, label="DESTINATION_PARENT")
    if destination.exists() or destination.is_symlink(): raise C3V3ClosureError("C3_V3_DESTINATION_EXISTS")
    v2_doc, _v2_raw, v2_raw_sha = _v2_manifest(v2_root)
    _validate_auxiliary_preimages(v2_root, v2_raw_sha=v2_raw_sha, v2_doc=v2_doc)
    _validate_baseline_binding(repo_root)
    staging = destination.parent / f".{destination.name}.build-{os.getpid()}"
    if staging.exists() or staging.is_symlink(): raise C3V3ClosureError("C3_V3_TEMP_EXISTS")
    os.mkdir(staging, 0o700)
    try:
        if os.stat(staging).st_dev != os.stat(destination.parent).st_dev: raise C3V3ClosureError("C3_V3_CROSS_FILESYSTEM")
        roles: dict[str, dict[str, object]] = {}
        for role, (name, size, digest) in V2_ROLES.items():
            _copy_exact(v2_root / name, staging / name, expected_size=size, expected_sha=digest, label=f"V2_{role.upper()}")
            roles[role] = {"locator": name, "mode": "0600", "size": size, "sha256": digest, "origin": {"kind": "v2_role", "v2_manifest_sha256": v2_raw_sha, "v2_manifest_self_seal": v2_doc["manifest_sha256"], "v2_pointer": f"roles.{role}"}}
        for role, (name, _kind, size, digest, pointer) in AUXILIARY_ROLES.items():
            _copy_exact(auxiliary_root / name, staging / name, expected_size=size, expected_sha=digest, label=f"AUX_{role.upper()}", source_private_mode=False)
            parent_role, _hash_field = _AUX_PARENT_ROLES[role]
            roles[role] = {"locator": name, "mode": "0600", "size": size, "sha256": digest, "origin": {"kind": "allowlisted_auxiliary_preimage", "auxiliary_source": "explicit_recovery_store", "sealed_parent_role": parent_role, "v2_manifest_sha256": v2_raw_sha, "v2_manifest_self_seal": v2_doc["manifest_sha256"], "v2_pointer": pointer, "raw_hash": digest}}
        tree = {role: {k: value for k, value in row.items() if k != "origin"} for role, row in roles.items()}
        manifest: dict[str, object] = {"schema_version": V3_SCHEMA, "candidate_id": CANDIDATE_ID, "recording_date": RECORDING_DATE, "authority_scope": _SCOPE, "forbidden_operations": list(_FORBIDDEN), "derived_from": {"schema_version": V2_SCHEMA, "manifest_raw_sha256": v2_raw_sha, "manifest_self_seal": v2_doc["manifest_sha256"]}, "roles": roles, "required_auxiliary_roles": sorted(AUXILIARY_ROLES), "root_tree_sha256": _tree_sha256(tree), "line947_binding": {"uuid": "555195ed-ec18-418d-a311-558f7e54291f", "raw_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa", "content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b", "baseline_sha256": "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc"}}
        manifest["manifest_sha256"] = _sha(_canonical(manifest))
        _write_exclusive(staging / "manifest.json", _canonical(manifest))
        fd = os.open(staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)); os.fsync(fd); os.close(fd)
        _publish_create_only(staging, destination)
        fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)); os.fsync(fd); os.close(fd)
    except BaseException:
        if staging.exists() and not staging.is_symlink():
            for child in sorted(staging.iterdir(), reverse=True):
                if child.is_file(): child.unlink()
            staging.rmdir()
        raise
    return load_c3_v3_authority(root=destination)

def load_c3_v3_authority(*, root: Path) -> C3V3Authority:
    root = _safe_dir(root, label="ROOT")
    if stat.S_IMODE(os.lstat(root).st_mode) != 0o700: raise C3V3ClosureError("C3_V3_ROOT_MODE_DRIFT")
    names = {p.name for p in root.iterdir()}
    raw = _read_regular(root / "manifest.json", label="MANIFEST")
    raw_sha = _sha(raw)
    try: doc = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise C3V3ClosureError("C3_V3_MANIFEST_INVALID") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != V3_SCHEMA or doc.get("candidate_id") != CANDIDATE_ID or doc.get("recording_date") != RECORDING_DATE or doc.get("authority_scope") != _SCOPE or doc.get("forbidden_operations") != _FORBIDDEN: raise C3V3ClosureError("C3_V3_IDENTITY_DRIFT")
    unsigned = dict(doc); declared = unsigned.pop("manifest_sha256", None)
    if not isinstance(declared, str) or declared != _sha(_canonical(unsigned)): raise C3V3ClosureError("C3_V3_SELF_SEAL_DRIFT")
    if raw_sha != _APPROVED_V3_RAW_SHA or declared != _APPROVED_V3_SELF_SEAL:
        raise C3V3ClosureError("C3_V3_APPROVED_MANIFEST_DRIFT")
    entries = doc.get("roles")
    if not isinstance(entries, dict) or set(entries) != set(V2_ROLES) | set(AUXILIARY_ROLES): raise C3V3ClosureError("C3_V3_ROLE_SET_DRIFT")
    expected_names = {str(entry.get("locator")) for entry in entries.values() if isinstance(entry, dict)} | {"manifest.json"}
    if names != expected_names: raise C3V3ClosureError("C3_V3_EXTRA_OR_MISSING_FILE")
    roles: dict[str, Path] = {}
    tree: dict[str, dict[str, object]] = {}
    for role, entry in entries.items():
        if not isinstance(entry, dict) or entry.get("mode") != "0600": raise C3V3ClosureError("C3_V3_ROLE_DESCRIPTOR_DRIFT")
        expected = V2_ROLES.get(role) or AUXILIARY_ROLES.get(role)
        if expected is None or entry.get("locator") != expected[0] or entry.get("size") != expected[2 if role in AUXILIARY_ROLES else 1] or entry.get("sha256") != expected[3 if role in AUXILIARY_ROLES else 2]:
            raise C3V3ClosureError("C3_V3_ROLE_DESCRIPTOR_DRIFT")
        path = root / str(entry.get("locator")); data = _read_regular(path, label=f"ROLE_{role.upper()}")
        if len(data) != entry.get("size") or _sha(data) != entry.get("sha256"): raise C3V3ClosureError("C3_V3_ROLE_HASH_DRIFT")
        if role in V2_ROLES:
            origin = entry.get("origin")
            if origin != {"kind": "v2_role", "v2_manifest_sha256": _APPROVED_V2_RAW_SHA, "v2_manifest_self_seal": _APPROVED_V2_SELF_SEAL, "v2_pointer": f"roles.{role}"}:
                raise C3V3ClosureError("C3_V3_V2_ROLE_ORIGIN_DRIFT")
        else:
            origin = entry.get("origin")
            parent_role, _hash_field = _AUX_PARENT_ROLES[role]
            if origin != {"kind": "allowlisted_auxiliary_preimage", "auxiliary_source": "explicit_recovery_store", "sealed_parent_role": parent_role, "v2_manifest_sha256": _APPROVED_V2_RAW_SHA, "v2_manifest_self_seal": _APPROVED_V2_SELF_SEAL, "v2_pointer": AUXILIARY_ROLES[role][4], "raw_hash": AUXILIARY_ROLES[role][3]}:
                raise C3V3ClosureError("C3_V3_AUXILIARY_ANCESTRY_DRIFT")
        roles[role] = path; tree[role] = {k: entry[k] for k in ("locator", "mode", "size", "sha256")}
    if doc.get("root_tree_sha256") != _tree_sha256(tree): raise C3V3ClosureError("C3_V3_TREE_SEAL_DRIFT")
    if doc.get("root_tree_sha256") != _APPROVED_V3_TREE_SHA:
        raise C3V3ClosureError("C3_V3_APPROVED_TREE_DRIFT")
    parent_docs: dict[str, dict] = {}
    for parent_role in ("record", "speaker_manifest"):
        try:
            parent = json.loads(_read_regular(roles[parent_role], label=f"ROLE_{parent_role.upper()}_PARENT").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise C3V3ClosureError("C3_V3_AUXILIARY_PARENT_INVALID") from exc
        if not isinstance(parent, dict):
            raise C3V3ClosureError("C3_V3_AUXILIARY_PARENT_INVALID")
        parent_docs[parent_role] = parent
    _validate_auxiliary_documents(parent_docs)
    derived = doc.get("derived_from")
    if not isinstance(derived, dict) or derived.get("schema_version") != V2_SCHEMA or not isinstance(derived.get("manifest_raw_sha256"), str) or not isinstance(derived.get("manifest_self_seal"), str): raise C3V3ClosureError("C3_V3_V2_ANCESTRY_MISSING")
    if doc.get("required_auxiliary_roles") != sorted(AUXILIARY_ROLES): raise C3V3ClosureError("C3_V3_AUXILIARY_SET_DRIFT")
    if derived != {"schema_version": V2_SCHEMA, "manifest_raw_sha256": _APPROVED_V2_RAW_SHA, "manifest_self_seal": _APPROVED_V2_SELF_SEAL}:
        raise C3V3ClosureError("C3_V3_V2_ANCESTRY_DRIFT")
    line = doc.get("line947_binding")
    if line != {"uuid": "555195ed-ec18-418d-a311-558f7e54291f", "raw_sha256": "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa", "content_sha256": "sha256:0e0e69e54fc06c88296536c6dfbca947181170873529c5de508a2af39aa93f6b", "baseline_sha256": "sha256:d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc"}: raise C3V3ClosureError("C3_V3_LINE947_BINDING_DRIFT")
    return C3V3Authority(root, root / "manifest.json", raw_sha, declared, str(doc["root_tree_sha256"]), roles)

def _remove_private_tree(root: Path) -> None:
    """Remove only a freshly-created private stage after its evidence is sealed."""
    if root.is_symlink() or not root.is_dir():
        raise C3V3ClosureError("C3_V3_PRIVATE_STAGE_CLEANUP_UNSAFE")
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        for name in files:
            item = current / name
            if item.is_symlink() or not item.is_file():
                raise C3V3ClosureError("C3_V3_PRIVATE_STAGE_CLEANUP_UNSAFE")
            item.unlink()
        for name in dirs:
            item = current / name
            if item.is_symlink() or not item.is_dir():
                raise C3V3ClosureError("C3_V3_PRIVATE_STAGE_CLEANUP_UNSAFE")
            item.rmdir()
    root.rmdir()


def consume_c3_successor_pass0(*, runtime_root: Path, private_stage_parent: Path, audit: Callable[[Path], Mapping[str, object]] | None = None) -> C3DirectPass0Result:
    """Consume v3 only after complete closure checks; never calls a provider."""
    authority = load_c3_v3_authority(root=Path(runtime_root) / V3_RELATIVE_ROOT)
    parent = _safe_dir(private_stage_parent, label="PRIVATE_PARENT")
    stage = parent / f"{RECORDING_DATE}-{CANDIDATE_ID}-pass0"
    if stage.exists() or stage.is_symlink(): raise C3V3ClosureError("C3_V3_PRIVATE_STAGE_EXISTS")
    os.mkdir(stage, 0o700)
    package = stage / "package"; os.mkdir(package, 0o700)
    try:
        for role, source in authority.roles.items():
            expected = V2_ROLES.get(role) or AUXILIARY_ROLES.get(role)
            expected_size = expected[2 if role in AUXILIARY_ROLES else 1]
            expected_sha = expected[3 if role in AUXILIARY_ROLES else 2]
            _copy_exact(source, package / source.name, expected_size=expected_size, expected_sha=expected_sha, label=f"PRIVATE_{role.upper()}")
        package_binding = {"root_tree_sha256": authority.root_tree_sha256, "manifest_sha256": authority.manifest_raw_sha256, "artifact_count": len(authority.roles)}
        if audit is None: raise C3V3ClosureError("C3_V3_CANONICAL_AUDIT_CALLBACK_REQUIRED")
        try:
            result = dict(audit(package))
        except Exception as exc:
            raise C3V3ClosureError("C3_V3_CANONICAL_AUDIT_UNAVAILABLE") from exc
        if result.get("passed") is not True or result.get("issue_count") != 0 or result.get("blocking_issue_count") != 0:
            codes = tuple(sorted({str(item.get("code")) for item in result.get("issues", ()) if isinstance(item, Mapping) and item.get("code")}))
            suffix = ",".join(codes) if codes else "UNKNOWN"
            raise C3V3ClosureError(f"C3_V3_CANONICAL_AUDIT_BLOCKED:{suffix}")
        if not isinstance(result.get("auditor_source_sha256"), str) or not isinstance(result.get("policy_fingerprint"), str):
            raise C3V3ClosureError("C3_V3_AUDITOR_BINDING_MISSING")
        prepared = {
            "schema_version": "c3-direct-canonical-prepared-transaction-binding.v1",
            "lane": "talk", "candidate_id": CANDIDATE_ID,
            "upload_enabled": False, "provider_attempted": False,
            "authority_manifest_sha256": authority.manifest_raw_sha256,
            "package_root_tree_sha256": authority.root_tree_sha256,
            "review_manifest_sha256": result.get("review_manifest_sha256"),
        }
        audit_binding = {"schema_version": "c3-direct-pass0-audit-binding.v1", "passed": True, "audit_sha256": _sha(_canonical(result)), "auditor_source_sha256": result["auditor_source_sha256"], "policy_fingerprint": result["policy_fingerprint"]}
        matrix = tuple({"predicate": name, "status": "PASS"} for name in ("V3_DESCRIPTOR", "V2_BYTE_CARRY", "AUXILIARY_CLOSURE", "LINE947_BASELINE_BINDING", "CANONICAL_PACKAGE_LAYOUT", "CANONICAL_PACKAGE_AUDIT", "AUDITOR_SOURCE_BINDING", "AUDITOR_POLICY_BINDING")) + ({"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"}, {"predicate": "PROVIDER_ATTEMPTED", "status": "PASS_FALSE"})
        return C3DirectPass0Result(prepared_delivery=prepared, private_package_binding=package_binding, private_audit_binding=audit_binding, predicate_matrix=matrix)
    finally:
        _remove_private_tree(stage)
