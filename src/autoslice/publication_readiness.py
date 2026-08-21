"""Read-only, fail-closed readiness graph for current Li Dousha candidates."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from scripts import authorized_upload
from src.autoslice.publication_registry import load_publication_registry

READY_TO_PREPARE = "READY_TO_PREPARE"
NEEDS_PROVIDER = "NEEDS_PROVIDER"
NEEDS_IVAN_TRUTH = "NEEDS_IVAN_TRUTH"
STATE_DRIFT = "STATE_DRIFT"
CODE_DEFECT = "CODE_DEFECT"
READY_FOR_SERIAL_UPLOAD = "READY_FOR_SERIAL_UPLOAD"

_PRECEDENCE = (NEEDS_IVAN_TRUTH, STATE_DRIFT, CODE_DEFECT, NEEDS_PROVIDER, READY_FOR_SERIAL_UPLOAD, READY_TO_PREPARE)
_CODE_FAILURE_STAGES = frozenset({"invariant_violation", "runtime_code_defect"})
_PROVIDER_FAILURE_STAGES = frozenset({"final_review_provider_budget_ledger", "source_fact_provider", "cover_provider"})


def _reason(code: str, role: str) -> dict[str, str]:
    return {"code": code, "evidence_role": role}


def _safe_path(path: Path, root: Path | None = None) -> Path:
    """Return a lexical, no-symlink regular-file path below its trusted root."""
    absolute = path if path.is_absolute() else (root / path if root else path.absolute())
    absolute = absolute.absolute()
    trusted_root = (root or absolute.parent).absolute()
    try:
        relative = absolute.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("path escapes trusted root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("unsafe relative path")
    cursor = Path(absolute.anchor)
    for part in trusted_root.parts[1:] + relative.parts:
        cursor /= part
        entry = cursor.lstat()
        if stat.S_ISLNK(entry.st_mode):
            raise ValueError("symlink in trusted path")
        if cursor != absolute and not stat.S_ISDIR(entry.st_mode):
            raise ValueError("non-directory parent")
    if not stat.S_ISREG(absolute.lstat().st_mode):
        raise ValueError("not a regular file")
    return absolute


def _safe_directory(path: Path) -> bool:
    cursor = Path(path.absolute().anchor)
    try:
        for part in path.absolute().parts[1:]:
            cursor /= part
            entry = cursor.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                return False
    except OSError:
        return False
    return True


def _snapshot_regular(path: Path, root: Path | None = None) -> bytes:
    """Read a regular file through an O_NOFOLLOW fd and verify a stable inode."""
    absolute = _safe_path(path, root)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ValueError("file changed during read")
    # Re-walk the parent chain after the fd read; a directory swap is also drift.
    _safe_path(absolute, root)
    final = absolute.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns
    ):
        raise ValueError("path changed during read")
    return b"".join(chunks)


def _sha256(path: Path, root: Path | None = None) -> str:
    """Hash a stable regular, non-symlink file inside ``root`` when supplied."""
    digest = hashlib.sha256()
    digest.update(_snapshot_regular(path, root))
    return "sha256:" + digest.hexdigest()


def _load_json(path: Path, root: Path | None = None) -> Mapping[str, object]:
    document = json.loads(_snapshot_regular(path, root).decode("utf-8"))
    if not isinstance(document, Mapping):
        raise ValueError("JSON document is not an object")
    return document


def _state_rows(runtime_root: Path) -> tuple[list[tuple[Path, Mapping[str, object]]], list[dict[str, str]]]:
    rows: list[tuple[Path, Mapping[str, object]]] = []
    problems: list[dict[str, str]] = []
    for path in sorted((runtime_root / "state").glob("????-??-??.json")):
        try:
            document = _load_json(path, runtime_root / "state")
        except (OSError, ValueError, json.JSONDecodeError):
            problems.append(_reason("STATE_FILE_INVALID", str(path)))
            continue
        for lane in ("picks", "songs"):
            collection = document.get(lane, [])
            if not isinstance(collection, list):
                problems.append(_reason("STATE_COLLECTION_INVALID", str(path)))
                continue
            for pick in collection:
                if isinstance(pick, Mapping) and str(pick.get("candidate_id") or "").strip():
                    rows.append((path, pick))
    return rows, problems


def _package_root(runtime_root: Path, date: str, candidate_id: str, pick: Mapping[str, object]) -> Path | None:
    del pick
    conventional = runtime_root / "out" / date / candidate_id
    return conventional if _safe_directory(conventional) else None


def _one_file(root: Path, patterns: tuple[str, ...]) -> Path | None:
    found: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            try:
                _safe_path(path, root)
            except (OSError, ValueError):
                continue
            found.add(path)
    return next(iter(found)) if len(found) == 1 else None


def _field_path(document: Mapping[str, object], *keys: str) -> Path | None:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    return None


def _valid_joint_qc(
    receipt: Mapping[str, object], *, candidate_id: str, title: object, cover: Path | None, cover_sha256: object, root: Path
) -> bool:
    """Local pre-manifest replay of the actual CPA joint-QC receipt shape."""
    verdict = receipt.get("verdict")
    witness = receipt.get("witness")
    if not isinstance(verdict, Mapping) or not isinstance(witness, Mapping) or cover is None:
        return False
    expected_cover_path = str(_safe_path(cover, root))
    expected_cover_raw = str(cover_sha256 or "").removeprefix("sha256:")
    expected_verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "pass": True,
    }
    if (
        receipt.get("schema_version") != "lidousha-title-cover-joint-qc.v1"
        or receipt.get("candidate_id") != candidate_id
        or receipt.get("status") != "PASS"
        or receipt.get("pass") is not True
        or receipt.get("selected_provider") != "cpa"
        or receipt.get("preferred_provider") not in (None, "cpa")
        or witness.get("schema_version") != "cpa-frame-witness.v1"
        or witness.get("provider") != "cpa"
        or witness.get("status") != "OBSERVED"
        or not str(witness.get("model") or "").strip()
        or receipt.get("title") != title
        or receipt.get("title_sha256") != "sha256:" + hashlib.sha256(str(title).encode()).hexdigest()
        or str(receipt.get("cover_sha256") or "").removeprefix("sha256:") != expected_cover_raw
        or str(witness.get("image_sha256") or "").removeprefix("sha256:") != expected_cover_raw
        or receipt.get("cover_path") != expected_cover_path
        or witness.get("image_path") != expected_cover_path
        or verdict.get("physical_text_line_count") not in (1, 2)
        or verdict.get("unrelated_or_misleading_elements") != []
    ):
        return False
    return all(isinstance(verdict.get(key), bool) and verdict[key] is value for key, value in expected_verdict.items())


def _inspect_package(root: Path | None, candidate_id: str, date: str) -> tuple[set[str], dict[str, str], Path | None]:
    """Replay the minimum local descriptor closure required for readiness."""
    reasons: set[str] = set()
    dependencies: dict[str, str] = {}
    if root is None:
        return {"PACKAGE_ROOT_MISSING"}, dependencies, None
    record_path = _one_file(root, (f"{candidate_id}.record.json", "*.record.json"))
    if record_path is None:
        return {"PACKAGE_RECORD_MISSING_OR_AMBIGUOUS"}, dependencies, None
    dependencies["record"] = str(record_path)
    try:
        record = _load_json(record_path, root)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"PACKAGE_RECORD_INVALID"}, dependencies, None
    if record.get("candidate_id") not in (None, candidate_id) or record.get("recording_date") not in (None, date):
        reasons.add("PACKAGE_RECORD_IDENTITY_DRIFT")
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping):
        return reasons | {"PACKAGE_ARTIFACT_HASHES_MISSING"}, dependencies, None
    preview = record.get("burned_preview")
    nested_burn = _field_path(preview, "burned_path", "path") if isinstance(preview, Mapping) else None
    required = ((("media_path", "main_path"), "video_sha256", "media"), (("subtitle_path",), "subtitle_sha256", "subtitle"), (("subtitle_ass_path",), "ass_sha256", "subtitle_ass"), (("burned_video_path",), "burned_video_sha256", "burned_video"))
    for fields, hash_key, role in required:
        path = nested_burn if role == "burned_video" and nested_burn is not None else _field_path(record, *fields)
        if path is None:
            reasons.add("PACKAGE_ARTIFACT_LOCATOR_MISSING")
            continue
        try:
            actual = _sha256(path, root)
        except (OSError, ValueError):
            reasons.add("PACKAGE_ARTIFACT_FILE_INVALID")
        else:
            dependencies[role] = str(path)
            if hashes.get(hash_key) != actual:
                reasons.add("PACKAGE_ARTIFACT_HASH_DRIFT")
            if role == "burned_video" and isinstance(preview, Mapping) and nested_burn is not None:
                if preview.get("burned_sha256") != actual:
                    reasons.add("PACKAGE_ARTIFACT_HASH_DRIFT")
    staging = record.get("publish_staging")
    publish_path = _field_path(staging, "publish_json_path") if isinstance(staging, Mapping) else None
    publish_path = publish_path or _one_file(root, (f"{candidate_id}.publish.json", "*.publish.json"))
    if publish_path is None:
        return reasons | {"PACKAGE_PUBLISH_MISSING_OR_AMBIGUOUS"}, dependencies, None
    try:
        publish = _load_json(publish_path, root)
    except (OSError, ValueError, json.JSONDecodeError):
        return reasons | {"PACKAGE_PUBLISH_INVALID"}, dependencies, None
    dependencies["publish"] = str(publish_path)
    if publish.get("candidate_id") not in (None, candidate_id):
        reasons.add("PACKAGE_PUBLISH_IDENTITY_DRIFT")
    publish_hashes = publish.get("artifact_hashes")
    if not isinstance(publish_hashes, Mapping) or any(publish_hashes.get(key) != hashes.get(key) for key in ("video_sha256", "subtitle_sha256", "ass_sha256", "burned_video_sha256")):
        reasons.add("PACKAGE_RECORD_PUBLISH_HASH_MIRROR_DRIFT")
    generation = publish.get("cover_generation")
    cover: Path | None = None
    actual_cover: str | None = None
    if not isinstance(generation, Mapping):
        reasons.add("COVER_QC_MISSING")
    else:
        cover = _field_path(generation, "final_cover") or _field_path(publish, "cover_path")
        if cover is None:
            reasons.add("COVER_QC_MISSING")
        else:
            try:
                actual_cover = _sha256(cover, root)
            except (OSError, ValueError):
                reasons.add("PACKAGE_ARTIFACT_FILE_INVALID")
            else:
                dependencies["cover"] = str(cover)
                if generation.get("final_cover_sha256") != actual_cover or (hashes.get("cover_sha256") is not None and hashes.get("cover_sha256") != actual_cover):
                    reasons.add("PACKAGE_ARTIFACT_HASH_DRIFT")
    source_fact = record.get("source_fact_review")
    if not isinstance(source_fact, Mapping):
        contract = record.get("story_contract")
        source_fact = contract.get("source_fact_review") if isinstance(contract, Mapping) else None
    if not isinstance(source_fact, Mapping) or source_fact.get("status") != "PASS":
        reasons.add("SOURCE_FACT_PROVIDER_MISSING")
    qc = _one_file(root, ("*title*cover*qc*.json", "*joint*qc*.json"))
    if qc is None:
        reasons.add("COVER_QC_MISSING")
    else:
        dependencies["title_cover_qc"] = str(qc)
        try:
            qc_doc = _load_json(qc, root)
        except (OSError, ValueError, json.JSONDecodeError):
            reasons.add("COVER_QC_INVALID")
        else:
            if not _valid_joint_qc(
                qc_doc,
                candidate_id=candidate_id,
                title=publish.get("title"),
                cover=cover,
                cover_sha256=actual_cover,
                root=root,
            ):
                reasons.add("COVER_QC_MISSING")
    for role, patterns in (
        ("review_manifest", ("*review*manifest*.json",)),
        ("package_audit", ("*package*audit*.json",)),
    ):
        document = _one_file(root, patterns)
        if document is not None:
            dependencies[role] = str(document)
    return reasons, dependencies, record_path


def _manifest_path(root: Path | None) -> Path | None:
    return _one_file(root, ("*.upload_manifest.json", "*upload*manifest*.json")) if root else None


def _category(reasons: set[str], *, serial: bool) -> str:
    if serial:
        return READY_FOR_SERIAL_UPLOAD
    groups = {
        NEEDS_IVAN_TRUTH: {"HOLD_PENDING_REVIEW", "HUMAN_TRUTH_MISSING", "TITLE_AUTHORITY_REQUIRED"},
        STATE_DRIFT: {code for code in reasons if code.startswith(("STATE_", "PACKAGE_", "UPLOAD_"))},
        CODE_DEFECT: {"TYPED_RUNTIME_FAILURE"},
        NEEDS_PROVIDER: {"SOURCE_FACT_PROVIDER_MISSING", "COVER_QC_MISSING", "COVER_QC_INVALID"},
    }
    for category in _PRECEDENCE:
        if reasons & groups.get(category, set()):
            return category
    return READY_TO_PREPARE


def build_readiness_graph(*, repository_root: Path, runtime_root: Path, registry_loader: Callable[..., Mapping[str, object]] = load_publication_registry, manifest_loader: Callable[..., tuple[dict | None, list[str]]] = authorized_upload.load_and_verify, ledger_reader: Callable[[Path], tuple[list[dict], list[str]]] = authorized_upload.read_ledger, ledger_checker: Callable[[Path, str], tuple[str | None, dict | None, list[str]]] = authorized_upload.ledger_guard) -> dict[str, object]:
    """Return a read-only graph. No result is a release or upload grant."""
    registry_path = repository_root / "assets/lidousha/publication_registry.v1.json"
    graph_problems: list[dict[str, str]] = []
    try:
        registry = registry_loader(registry_path, runtime_path=runtime_root / "state/publication_registry.runtime.v1.json")
        registry_rows = registry.get("entries", []) if isinstance(registry, Mapping) else []
        registry_valid = True
    except Exception:
        registry_rows = []
        registry_valid = False
        graph_problems.append(_reason("PUBLICATION_REGISTRY_INVALID", "publication_registry"))
    indexed = {(str(row.get("candidate_id") or ""), str(row.get("recording_date") or "")): row for row in registry_rows if isinstance(row, Mapping)}
    candidates: dict[tuple[str, str], tuple[Path | None, Mapping[str, object]]] = {}
    state_rows, state_problems = _state_rows(runtime_root)
    graph_problems.extend(state_problems)
    for state_path, pick in state_rows:
        key = (str(pick["candidate_id"]), str(pick.get("recording_date") or state_path.stem))
        if key in candidates:
            graph_problems.append(_reason("STATE_CANDIDATE_DUPLICATE", str(state_path)))
        else:
            candidates[key] = (state_path, pick)
    excluded = [{"candidate_id": key[0], "recording_date": key[1], "reason_code": "PUBLISHED_EXCLUDED"} for key, row in indexed.items() if row.get("status") == "published"]
    for key, row in indexed.items():
        if row.get("status") == "hold_pending_review" and key not in candidates:
            candidates[key] = (None, row)
    ledger = runtime_root / "reports/upload_ledger.jsonl"
    try:
        _, ledger_problems = ledger_reader(ledger)
    except Exception:
        ledger_problems = ["ledger reader failed"]
    if ledger_problems:
        graph_problems.extend(_reason("UPLOAD_LEDGER_INVALID", "upload_ledger") for _ in ledger_problems)
    rows: list[dict[str, object]] = []
    for (candidate_id, date), (state_path, pick) in sorted(candidates.items()):
        registry_row = indexed.get((candidate_id, date))
        if registry_row and registry_row.get("status") == "published":
            continue
        reasons: set[str] = set()
        if registry_row and registry_row.get("status") == "hold_pending_review":
            reasons.add("HOLD_PENDING_REVIEW")
        if state_path is None:
            reasons.add("HUMAN_TRUTH_MISSING")
        else:
            if pick.get("status") != "review_ready" or pick.get("rc") != 0:
                reasons.add("STATE_ROW_NOT_REVIEW_READY")
            if "bundle_lifecycle" in pick and pick.get("bundle_lifecycle") != "CURRENT":
                reasons.add("STATE_NOT_CURRENT")
            if "bundle_compliance" in pick and pick.get("bundle_compliance") != "COMPLIANT":
                reasons.add("STATE_NOT_COMPLIANT")
        failure_stage, rejection = str(pick.get("failure_stage") or ""), str(pick.get("rejection_reason") or "")
        if (
            str(pick.get("title_authority_error") or "")
            or "human" in rejection
            or failure_stage in {"final_review_findings", "final_review_unresolved"}
        ):
            reasons.add("HUMAN_TRUTH_MISSING")
        if failure_stage in _PROVIDER_FAILURE_STAGES:
            reasons.add("SOURCE_FACT_PROVIDER_MISSING")
        if failure_stage in _CODE_FAILURE_STAGES:
            reasons.add("TYPED_RUNTIME_FAILURE")
        root = _package_root(runtime_root, date, candidate_id, pick)
        try:
            package_reasons, dependencies, record_path = _inspect_package(root, candidate_id, date)
        except Exception:
            package_reasons, dependencies, record_path = {"PACKAGE_INSPECTION_EXCEPTION"}, {}, None
        reasons.update(package_reasons)
        if ledger_problems:
            reasons.add("LEDGER_SERIAL_BLOCKED")
        serial = False
        try:
            manifest = _manifest_path(root)
            serial_inputs = {"review_manifest", "package_audit", "title_cover_qc"}
            if not package_reasons and manifest is not None and serial_inputs <= dependencies.keys() and registry_valid and not (registry_row and registry_row.get("status") == "hold_pending_review"):
                parsed, manifest_problems = manifest_loader(manifest)
                if manifest_problems or parsed is None:
                    reasons.add("UPLOAD_MANIFEST_INVALID")
                else:
                    attestation = parsed.get("package_attestation") if isinstance(parsed, Mapping) else None
                    attested_record = attestation.get("record") if isinstance(attestation, Mapping) else None
                    if not isinstance(attested_record, Mapping) or str(attested_record.get("path") or "") != str(record_path):
                        reasons.add("UPLOAD_MANIFEST_IDENTITY_DRIFT")
                    if parsed.get("candidate_id") not in (None, candidate_id) or parsed.get("recording_date") not in (None, date):
                        reasons.add("UPLOAD_MANIFEST_IDENTITY_DRIFT")
                    video = parsed.get("video", {}) if isinstance(parsed, Mapping) else {}
                    status, _entry, guard_problems = ledger_checker(ledger, str(video.get("sha256") if isinstance(video, Mapping) else ""))
                    if status == "uploaded":
                        reasons.add("UPLOAD_STATE_REGISTRY_DRIFT")
                    elif ledger_problems or guard_problems or status == "unresolved":
                        reasons.add("LEDGER_SERIAL_BLOCKED")
                    else:
                        serial = not reasons
            elif not package_reasons and manifest is not None and not serial_inputs <= dependencies.keys():
                reasons.add("PACKAGE_AUDIT_OR_REVIEW_MANIFEST_MISSING")
            elif not package_reasons and manifest is not None and not registry_valid:
                reasons.add("REGISTRY_SERIAL_BLOCKED")
        except Exception:
            reasons.add("PACKAGE_MANIFEST_OR_LEDGER_EXCEPTION")
        rows.append({"candidate_id": candidate_id, "recording_date": date, "category": _category(reasons, serial=serial), "reason_codes": sorted(reasons) or ["LOCAL_PREPARATION_AVAILABLE"], "dependencies": [str(state_path)] if state_path else ["publication_registry"], "package_dependencies": dependencies, "observational_only": True})
    return {"schema_version": "publication-readiness-graph.v1", "observational_only": True, "precedence": list(_PRECEDENCE), "graph_blockers": graph_problems, "rows": rows, "excluded_published": excluded}
