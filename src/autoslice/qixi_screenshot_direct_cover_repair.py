"""Sealed, screenshot-only cover repair inputs for one Qixi candidate.

This module deliberately owns no renderer or runtime mutation.  It validates
the immutable predecessor evidence that a fixed wrapper must replay before it
can invoke the normal screenshot composition path.  Keeping this boundary
small prevents a cover repair from becoming a subtitle/title/source-fact lane.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import base64
import re
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.qixi_transaction_core import (
    InstallCallbacks,
    QixiTransactionCoreError,
    create_staged_inode,
    exclusive_runner_lock,
    install_checkpointed_inode,
    journal_before_snapshot,
    restore_owned_inode,
    safe_parent,
    stable_regular_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_screenshot_direct_cover_repair/"
    "auto_123655_771_844.v1.json"
)
CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
PUNCH_CANDIDATES = ("有女友感吗？", "宿敌有点亲密")
PREFLIGHT_SCHEMA = "qixi-screenshot-direct-cover-repair-preflight.v1"
PREFLIGHT_RECEIPT_SCHEMA = "qixi-screenshot-direct-cover-repair-preflight-receipt.v1"
JOURNAL_SCHEMA = "qixi-screenshot-direct-cover-repair-journal.v1"


class QixiScreenshotDirectCoverRepairError(ValueError):
    """The fixed screenshot-only repair cannot establish its sealed inputs."""


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_authority(repo_root: Path = ROOT) -> dict[str, object]:
    """Load only a committed/deployed fixed authority asset."""

    path = repo_root / AUTHORITY_PATH
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_PATH, observed_bytes=raw
        )
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_UNSEALED") from exc
    if not isinstance(value, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_INVALID")
    normalized = validate_authority(value)
    terminal = normalized["terminal_refresh_authority"]
    assert isinstance(terminal, Mapping)
    try:
        terminal_path = Path(str(terminal["relative_path"]))
        if terminal_path.is_absolute() or ".." in terminal_path.parts:
            raise ValueError("terminal authority path escapes")
        terminal_raw = (repo_root / terminal_path).read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=terminal_path, observed_bytes=terminal_raw
        )
        terminal_document = json.loads(terminal_raw)
        if (
            not isinstance(terminal_document, Mapping)
            or terminal_document.get("authority_sha256") != terminal["authority_sha256"]
        ):
            raise ValueError("terminal authority does not bind")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TERMINAL_SUCCESSOR_DRIFT") from exc
    return normalized


def validate_authority(value: Mapping[str, object]) -> dict[str, object]:
    """Validate shape and the narrow scope before any runtime/provider work."""

    authority = dict(value)
    claimed = authority.pop("authority_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "runtime_root",
        "upload_enabled", "title", "punch_candidates", "terminal_refresh_authority",
        "legacy_cover", "immutable_media", "title_projection_sha256",
        "allowed_mutations",
    }
    if (
        set(authority) != required
        or authority.get("schema_version")
        != "qixi-screenshot-direct-cover-repair-authority.v1"
        or authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("recording_date") != RECORDING_DATE
        or authority.get("upload_enabled") is not False
        or authority.get("punch_candidates") != list(PUNCH_CANDIDATES)
        or not isinstance(claimed, str)
        or canonical_sha256(authority) != claimed
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_INVALID")
    legacy = authority.get("legacy_cover")
    if not isinstance(legacy, Mapping) or set(legacy) != {
        "final_cover", "failed_joint_qc", "generation_sha256", "subtree_sha256"
    }:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_SCOPE_INVALID")
    for role in ("final_cover", "failed_joint_qc"):
        descriptor = legacy.get(role)
        if (
            not isinstance(descriptor, Mapping)
            or set(descriptor) != {"path", "sha256", "bytes"}
            or not isinstance(descriptor.get("path"), str)
            or not isinstance(descriptor.get("sha256"), str)
            or isinstance(descriptor.get("bytes"), bool)
            or not isinstance(descriptor.get("bytes"), int)
        ):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_DESCRIPTOR_INVALID")
    if not isinstance(legacy.get("subtree_sha256"), Mapping) or not all(
        isinstance(key, str) and isinstance(digest, str) and digest.startswith("sha256:")
        for key, digest in legacy["subtree_sha256"].items()
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_SUBTREE_INVALID")
    title = authority.get("title")
    terminal = authority.get("terminal_refresh_authority")
    allowed = authority.get("allowed_mutations")
    if (
        not isinstance(title, Mapping)
        or set(title) != {"value", "sha256"}
        or not isinstance(title.get("value"), str)
        or title.get("sha256") != _sha256_bytes(title["value"].encode("utf-8"))
        or not isinstance(terminal, Mapping)
        or set(terminal) != {"relative_path", "authority_sha256"}
        or not isinstance(terminal.get("relative_path"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(terminal.get("authority_sha256")))
        or not isinstance(allowed, Mapping)
        or set(allowed) != {"record", "delivery_record", "publish", "state"}
        or any(
            not isinstance(roots, list)
            or len(roots) != len(set(roots))
            or any(not isinstance(root, str) or not root.startswith("/") for root in roots)
            for roots in allowed.values()
        )
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_SCOPE_INVALID")
    authority["authority_sha256"] = claimed
    return authority


def validate_legacy_cover_inputs(
    authority: Mapping[str, object], *, generation: Mapping[str, object],
    old_cover: bytes, old_qc: bytes,
) -> None:
    """Replay every stable old-cover fact without pinning a mutable record."""

    normalized = validate_authority(authority)
    legacy = normalized["legacy_cover"]
    assert isinstance(legacy, Mapping)
    if (
        _sha256_bytes(old_cover) != legacy["final_cover"]["sha256"]
        or len(old_cover) != legacy["final_cover"]["bytes"]
        or _sha256_bytes(old_qc) != legacy["failed_joint_qc"]["sha256"]
        or len(old_qc) != legacy["failed_joint_qc"]["bytes"]
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_BYTES_DRIFT")
    if generation.get("method") != "screenshot_direct" or generation.get("image_generation_used") is not False:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROUTE_DRIFT")
    subtrees = legacy["subtree_sha256"]
    assert isinstance(subtrees, Mapping)
    for key, expected in subtrees.items():
        if canonical_sha256(generation.get(key)) != expected:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_GENERATION_SUBTREE_DRIFT")
    if canonical_sha256(generation) != legacy["generation_sha256"]:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_GENERATION_DRIFT")


def require_repaired_punch(value: object) -> tuple[str, ...]:
    """Prevent a caller from turning a bounded candidate pool into free text."""

    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= 2):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_INVALID")
    lines = tuple(value)
    if any(not isinstance(line, str) or line not in PUNCH_CANDIDATES for line in lines):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_OUTSIDE_POOL")
    return lines


def build_preflight_manifest(
    *, authority: Mapping[str, object], cover_bytes: bytes, qc_bytes: bytes,
    generation: Mapping[str, object], logical_cover_path: str,
    logical_qc_path: str,
) -> dict[str, object]:
    """Freeze a successful private-stage result without writing any target.

    The caller owns staging/provider execution.  This small pure primitive
    deliberately accepts only byte images plus their intended final logical
    paths, so APPLY can consume the exact preflight rather than rerunning a
    provider or silently choosing a new route.
    """
    normalized = validate_authority(authority)
    if (
        generation.get("method") != "screenshot_direct"
        or generation.get("image_generation_used") is not False
        or not isinstance(logical_cover_path, str)
        or not logical_cover_path
        or not isinstance(logical_qc_path, str)
        or not logical_qc_path
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_INVALID")
    if _sha256_bytes(cover_bytes) != generation.get("final_cover_sha256"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_COVER_DRIFT")
    try:
        qc = json.loads(qc_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_QC_INVALID") from exc
    if (
        not isinstance(qc, Mapping)
        or qc.get("schema_version") != "lidousha-title-cover-joint-qc.v1"
        or qc.get("status") != "PASS"
        or qc.get("pass") is not True
        or qc.get("cover_path") != logical_cover_path
        or qc.get("cover_sha256") != _sha256_bytes(cover_bytes)
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_QC_INVALID")
    manifest: dict[str, object] = {
        "schema_version": PREFLIGHT_SCHEMA,
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "upload_enabled": False,
        "authority_sha256": normalized["authority_sha256"],
        "route": "screenshot_direct",
        "image_generation_used": False,
        "cover": {"logical_path": logical_cover_path, "sha256": _sha256_bytes(cover_bytes), "bytes": len(cover_bytes)},
        "joint_qc": {"logical_path": logical_qc_path, "sha256": _sha256_bytes(qc_bytes), "bytes": len(qc_bytes)},
        "generation_sha256": canonical_sha256(generation),
    }
    manifest["preflight_sha256"] = canonical_sha256(manifest)
    return manifest


def validate_preflight_manifest(value: Mapping[str, object], *, authority: Mapping[str, object]) -> dict[str, object]:
    """Reject hand-edited or cross-authority preflight data before APPLY."""
    manifest = dict(value)
    claimed = manifest.pop("preflight_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "upload_enabled",
        "authority_sha256", "route", "image_generation_used", "cover", "joint_qc",
        "generation_sha256",
    }
    normalized = validate_authority(authority)
    if (
        set(manifest) != required
        or claimed != canonical_sha256(manifest)
        or manifest.get("schema_version") != PREFLIGHT_SCHEMA
        or manifest.get("candidate_id") != CANDIDATE_ID
        or manifest.get("recording_date") != RECORDING_DATE
        or manifest.get("upload_enabled") is not False
        or manifest.get("authority_sha256") != normalized["authority_sha256"]
        or manifest.get("route") != "screenshot_direct"
        or manifest.get("image_generation_used") is not False
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    for role in ("cover", "joint_qc"):
        row = manifest.get(role)
        if not isinstance(row, Mapping) or set(row) != {"logical_path", "sha256", "bytes"}:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    manifest["preflight_sha256"] = claimed
    return manifest


def run_private_preflight(
    *, authority: Mapping[str, object], stage_root_parent: Path,
    stage: object,
) -> dict[str, object]:
    """Run one injected canonical stage wholly below a private directory.

    The stage callable returns ``generation``, ``cover_path``, ``qc_bytes`` and
    ``logical_cover_path``/``logical_qc_path``.  This core owns only private
    filesystem hygiene and manifest freezing; it deliberately has no access
    to official record/state/journal paths.
    """
    normalized = validate_authority(authority)
    if not callable(stage):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_UNAVAILABLE")
    _require_private_parent(stage_root_parent)
    root = Path(tempfile.mkdtemp(prefix=".qixi-screenshot-cover-stage-", dir=stage_root_parent))
    root_identity = os.lstat(root)
    try:
        os.chmod(root, 0o700)
        output = stage(root)
        if not isinstance(output, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INVALID")
        generation = output.get("generation")
        cover_path = output.get("cover_path")
        qc_bytes = output.get("qc_bytes")
        logical_cover = output.get("logical_cover_path")
        logical_qc = output.get("logical_qc_path")
        if not isinstance(generation, Mapping) or not isinstance(cover_path, Path) or not isinstance(qc_bytes, bytes):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INVALID")
        resolved_root = root.resolve(strict=True)
        resolved_cover = cover_path.resolve(strict=True)
        if not resolved_cover.is_relative_to(resolved_root):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE")
        try:
            cover_snapshot = stable_regular_snapshot(resolved_cover, label="cover private stage")
        except QixiTransactionCoreError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_COVER_INVALID") from exc
        if cover_snapshot is None:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_COVER_INVALID")
        cover_bytes = cover_snapshot.payload
        manifest = build_preflight_manifest(
            authority=normalized, cover_bytes=cover_bytes, qc_bytes=qc_bytes,
            generation=generation, logical_cover_path=logical_cover,
            logical_qc_path=logical_qc,
        )
        store = _preflight_store_root(stage_root_parent, normalized)
        digest = str(manifest["preflight_sha256"])[7:]
        destination = store / digest
        if os.path.lexists(destination):
            reusable = _load_preflight_store(destination, authority=normalized)
            if reusable["manifest"] != manifest:
                raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_COLLISION")
            return {
                "status": "FULL_DRY_RUN_REUSED", "upload_enabled": False,
                "manifest": manifest, "preflight_store": str(destination), "target_writes": 0,
            }
        try:
            destination.mkdir(mode=0o700)
        except OSError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
        files = {
            "cover.png": cover_bytes,
            "joint-qc.json": qc_bytes,
            "generation.json": json.dumps(generation, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        }
        for name, payload in files.items():
            _write_private_new(destination / name, payload)
        receipt = _preflight_receipt(manifest, files)
        _write_private_new(destination / "receipt.json", _json_bytes(receipt))
        _fsync_directory(destination)
        _load_preflight_store(destination, authority=normalized)
        return {
            "status": "FULL_DRY_RUN_PASS", "upload_enabled": False,
            "manifest": manifest, "preflight_store": str(destination), "target_writes": 0,
        }
    finally:
        # Successful preflight returns only sealed bytes/hashes, never a
        # provider raw response or private artifact locator.
        _remove_owned_private_stage(root, root_identity)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_private_parent(parent: Path) -> None:
    try:
        safe_parent(parent / ".qixi-private-probe")
        info = os.lstat(parent)
    except (OSError, QixiTransactionCoreError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE")


def _private_directory(path: Path) -> None:
    _require_private_parent(path.parent)
    if not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE")


def _preflight_store_root(parent: Path, authority: Mapping[str, object]) -> Path:
    namespace = parent / "qixi_screenshot_direct_cover_preflights"
    _private_directory(namespace)
    root = namespace / str(authority["authority_sha256"])[7:23]
    _private_directory(root)
    return root


def _write_private_new(path: Path, payload: bytes) -> None:
    try:
        snapshot = create_staged_inode(path, payload=payload, mode=0o600, label="cover preflight store")
    except QixiTransactionCoreError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    if snapshot.payload != payload:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")


def _preflight_receipt(manifest: Mapping[str, object], files: Mapping[str, bytes]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": PREFLIGHT_RECEIPT_SCHEMA,
        "status": "STORED",
        "authority_sha256": manifest["authority_sha256"],
        "preflight_sha256": manifest["preflight_sha256"],
        "files": {
            name: {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for name, payload in files.items()
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _load_preflight_store(destination: Path, *, authority: Mapping[str, object]) -> dict[str, object]:
    _private_directory(destination)
    expected_names = {"cover.png", "joint-qc.json", "generation.json", "manifest.json", "receipt.json"}
    if {child.name for child in destination.iterdir()} != expected_names:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    payloads: dict[str, bytes] = {}
    for name in expected_names:
        try:
            snapshot = stable_regular_snapshot(destination / name, label="cover preflight store")
        except QixiTransactionCoreError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT") from exc
        if snapshot is None or snapshot.mode != 0o600:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
        payloads[name] = snapshot.payload
    try:
        manifest = json.loads(payloads["manifest.json"])
        receipt = json.loads(payloads["receipt.json"])
        generation = json.loads(payloads["generation.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT") from exc
    manifest = validate_preflight_manifest(manifest, authority=authority)
    files = {key: value for key, value in payloads.items() if key != "receipt.json"}
    if (
        not isinstance(receipt, Mapping)
        or receipt != _preflight_receipt(manifest, files)
        or canonical_sha256(generation) != manifest["generation_sha256"]
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    return {"manifest": manifest, "cover": payloads["cover.png"], "joint_qc": payloads["joint-qc.json"], "generation": generation}


def _remove_owned_private_stage(root: Path, owner: os.stat_result) -> None:
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (owner.st_dev, owner.st_ino):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED")
    for child in root.rglob("*"):
        if stat.S_ISLNK(os.lstat(child).st_mode):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED")
    shutil.rmtree(root)


def apply_preflight_targets(
    *, authority: Mapping[str, object], stage_root_parent: Path,
    preflight_sha256: str, targets: Mapping[Path, bytes], apply: bool,
) -> dict[str, object]:
    """Install an already-stored cover projection; it never invokes a provider.

    The caller must derive ``targets`` from the sealed candidate projection.
    This boundary freezes target bytes into a durable CAS journal before the
    first rename, and is deliberately useful to the fixed CLI and test seams
    without accepting arbitrary provider data at apply time.
    """

    normalized = validate_authority(authority)
    if not isinstance(preflight_sha256, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", preflight_sha256):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    if not targets or any(not isinstance(path, Path) or not isinstance(payload, bytes) for path, payload in targets.items()):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PROJECTION_INVALID")
    if not apply:
        return {"status": "PLAN_PASS", "target_writes": 0, "upload_enabled": False}
    with exclusive_runner_lock(Path(str(normalized["runtime_root"]))):
        root = _transaction_root(stage_root_parent, normalized, preflight_sha256)
        journal_path = root / "journal.json"
        if os.path.lexists(journal_path):
            journal = _read_journal(journal_path, normalized, preflight_sha256)
            if journal["status"] == "COMMITTED":
                _verify_committed(journal)
                _write_transaction_receipt(root, journal)
                return {"status": "ALREADY_COMMITTED", "target_writes": 0, "upload_enabled": False}
        else:
            entries: list[dict[str, object]] = []
            for index, (target, payload) in enumerate(sorted(targets.items(), key=lambda item: str(item[0]))):
                try:
                    snapshot = stable_regular_snapshot(target, label="cover repair preimage")
                except QixiTransactionCoreError as exc:
                    raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TARGET_UNSAFE") from exc
                if snapshot is None:
                    raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TARGET_MISSING")
                entries.append({
                    "target": str(target), "before_bytes_b64": base64.b64encode(snapshot.payload).decode(),
                    "before_sha256": snapshot.sha256, "before_mode": snapshot.mode,
                    "before_device": snapshot.device, "before_inode": snapshot.inode,
                    "after_bytes_b64": base64.b64encode(payload).decode(),
                    "after_sha256": _sha256_bytes(payload), "after_mode": snapshot.mode,
                    "staged_name": f".{target.name}.qixi-cover-{index}.tmp",
                    "staged_device": None, "staged_inode": None,
                    "installed_device": None, "installed_inode": None, "phase": "PREPARED",
                })
            journal = {"schema_version": JOURNAL_SCHEMA, "status": "PREPARED", "authority_sha256": normalized["authority_sha256"], "preflight_sha256": preflight_sha256, "entries": entries}
            _write_journal_new(journal_path, journal)
        try:
            _commit_journal(root, journal)
        except BaseException:
            try:
                _rollback_journal(root, journal)
            except BaseException:
                journal["status"] = "ROLLBACK_REQUIRED"
                _write_journal(journal_path, journal)
            raise
        _write_transaction_receipt(root, journal)
        return {"status": "COMMITTED", "target_writes": len(targets), "upload_enabled": False}


def _transaction_root(parent: Path, authority: Mapping[str, object], preflight_sha256: str) -> Path:
    root = parent / "qixi_screenshot_direct_cover_transactions" / str(authority["authority_sha256"])[7:23] / preflight_sha256[7:23]
    for directory in (root.parent.parent, root.parent, root):
        _private_directory(directory)
    return root


def _journal_digest(journal: Mapping[str, object]) -> str:
    return canonical_sha256({key: value for key, value in journal.items() if key != "journal_sha256"})


def _write_journal_new(path: Path, journal: dict[str, object]) -> None:
    journal["journal_sha256"] = _journal_digest(journal)
    _write_private_new(path, _json_bytes(journal))


def _write_journal(path: Path, journal: dict[str, object]) -> None:
    journal["journal_sha256"] = _journal_digest(journal)
    old = stable_regular_snapshot(path, label="cover repair journal")
    if old is None:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    staged = create_staged_inode(path.with_name(".journal.qixi-cover.tmp"), payload=_json_bytes(journal), mode=0o600, label="cover repair journal")
    install_checkpointed_inode(staged, target=path, expected_before=old, callbacks=InstallCallbacks(checkpoint_installed=lambda _snap: None, verify_installed=lambda _snap: None), label="cover repair journal")


def _read_journal(path: Path, authority: Mapping[str, object], preflight_sha256: str) -> dict[str, object]:
    snapshot = stable_regular_snapshot(path, label="cover repair journal")
    if snapshot is None:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    try:
        journal = json.loads(snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != JOURNAL_SCHEMA or journal.get("authority_sha256") != authority["authority_sha256"] or journal.get("preflight_sha256") != preflight_sha256 or journal.get("journal_sha256") != _journal_digest(journal):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    return journal


def _commit_journal(root: Path, journal: dict[str, object]) -> None:
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
        target = Path(str(entry["target"]))
        payload = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        if entry["phase"] == "INSTALLED":
            continue
        staged = create_staged_inode(target.with_name(str(entry["staged_name"])), payload=payload, mode=int(entry["after_mode"]), label="cover repair stage")
        entry.update({"phase": "INSTALLING", "staged_device": staged.device, "staged_inode": staged.inode})
        _write_journal(root / "journal.json", journal)
        before = journal_before_snapshot(entry, target=target)
        installed = install_checkpointed_inode(staged, target=target, expected_before=before, callbacks=InstallCallbacks(checkpoint_installed=lambda snap: (entry.update({"phase": "INSTALLED", "installed_device": snap.device, "installed_inode": snap.inode}), _write_journal(root / "journal.json", journal)), verify_installed=lambda _snap: None), label="cover repair install")
        entry.update({"phase": "INSTALLED", "installed_device": installed.device, "installed_inode": installed.inode})
        _write_journal(root / "journal.json", journal)
    journal["status"] = "COMMITTED"
    _write_journal(root / "journal.json", journal)
    _verify_committed(journal)


def _rollback_journal(root: Path, journal: dict[str, object]) -> None:
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("phase") != "INSTALLED":
            continue
        target = Path(str(entry["target"]))
        after = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        current = stable_regular_snapshot(target, label="cover repair rollback")
        if current is None or current.payload != after or (current.device, current.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROLLBACK_OWNERSHIP_DRIFT")
        restored = restore_owned_inode(current, before=journal_before_snapshot(entry, target=target), label="cover repair rollback")
        if restored is None or restored.payload != base64.b64decode(str(entry["before_bytes_b64"]), validate=True):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROLLBACK_DRIFT")
        entry["phase"] = "PREPARED"
    journal["status"] = "ROLLED_BACK"
    _write_journal(root / "journal.json", journal)


def _verify_committed(journal: Mapping[str, object]) -> None:
    if journal.get("status") != "COMMITTED" or not isinstance(journal.get("entries"), list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMIT_MISSING")
    for entry in journal["entries"]:
        if not isinstance(entry, Mapping) or entry.get("phase") != "INSTALLED":
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMITTED_DRIFT")
        snap = stable_regular_snapshot(Path(str(entry["target"])), label="cover repair committed target")
        if snap is None or snap.payload != base64.b64decode(str(entry["after_bytes_b64"]), validate=True) or (snap.device, snap.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMITTED_DRIFT")


def _write_transaction_receipt(root: Path, journal: Mapping[str, object]) -> None:
    receipt = {"schema_version": "qixi-screenshot-direct-cover-repair-receipt.v1", "status": "COMMITTED", "authority_sha256": journal["authority_sha256"], "journal_sha256": journal["journal_sha256"]}
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    path = root / "receipt.json"
    payload = _json_bytes(receipt)
    if os.path.lexists(path):
        existing = stable_regular_snapshot(path, label="cover repair receipt")
        if existing is None or existing.payload != payload:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RECEIPT_DRIFT")
    else:
        _write_private_new(path, payload)
