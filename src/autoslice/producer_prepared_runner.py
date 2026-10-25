"""Run one ordinary private prepared Talk recovery subprocess.

This module owns only the narrow runner boundary used by the normal Talk lane.
The subprocess is still the producer authority: this helper validates the
immutable recovery inputs, writes the real child output to a private retry log,
and projects the child summary without manufacturing a result or checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.producer_prepare_result import prepared_talk_result


_RECOVERY_SCHEMA = "producer-prepared-runner-recovery.v1"
_PROOF_SCHEMA = "producer-prepared-runner-recovery-proof.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA256_SEMANTIC = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANDIDATE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_STORE_ID = re.compile(r"[0-9a-f]{32}\Z")
_RECOVERY_KEYS = frozenset(
    {
        "schema_version",
        "spec_path",
        "spec_sha256",
        "prepared_path",
        "prepared_manifest_sha256",
        "worker_store_id",
    }
)
_ITEM_FIELDS = (
    "session_id",
    "start_ms",
    "end_ms",
    "hook",
    "confidence",
    "selection_scorecard",
    "session_relation_authority",
    "lane",
)


# The recovery module owns the canonical successor namespace.  Resolve it only
# when the subprocess is actually about to run: the Talk runner remains
# importable in a worker checkout while the wider recovery package is staged.
canonical_prepared_resume_output_root = None


def _fail(message: str) -> None:
    raise ValueError("PREPARED_RUNNER_" + message)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_hash_bound_file(path: Path, expected: str, *, label: str) -> bytes:
    if (
        not path.is_absolute()
        or path.resolve() != path
        or path.is_symlink()
        or not path.is_file()
    ):
        _fail(f"{label}_PATH_INVALID")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _fail(f"{label}_UNREADABLE:{exc}")
    if _sha256_bytes(payload) != expected:
        _fail(f"{label}_HASH_MISMATCH")
    return payload


def _canonical_base(runner: object) -> Path:
    raw = getattr(runner, "BASE", None)
    if not isinstance(raw, (str, os.PathLike)):
        _fail("RUNNER_BASE_INVALID")
    base = Path(raw)
    if not base.is_absolute() or base.resolve() != base:
        _fail("RUNNER_BASE_INVALID")
    return base


def _canonical_repo(runner: object) -> Path:
    raw = getattr(runner, "REPO_ROOT", None)
    if not isinstance(raw, (str, os.PathLike)):
        _fail("RUNNER_REPO_INVALID")
    repo = Path(raw)
    if not repo.is_absolute() or repo.resolve() != repo:
        _fail("RUNNER_REPO_INVALID")
    return repo


def _candidate_id(item: Mapping[str, object]) -> str:
    values = [
        value
        for value in (item.get("cid"), item.get("candidate_id"))
        if value is not None
    ]
    if not values or any(not isinstance(value, str) or not value for value in values):
        _fail("CANDIDATE_INVALID")
    if len(set(values)) != 1 or _CANDIDATE.fullmatch(values[0]) is None:
        _fail("CANDIDATE_INVALID")
    return values[0]


def _raw_hash(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(f"{label}_INVALID")
    return value


def _recovery_capsule(item: Mapping[str, object]) -> dict[str, str]:
    raw = item.get("prepared_recovery")
    if not isinstance(raw, Mapping) or set(raw) != _RECOVERY_KEYS:
        _fail("RECOVERY_SCHEMA_INVALID")
    if raw.get("schema_version") != _RECOVERY_SCHEMA:
        _fail("RECOVERY_SCHEMA_INVALID")
    fields = {}
    for key in ("spec_path", "prepared_path", "worker_store_id"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            _fail(f"RECOVERY_{key.upper()}_INVALID")
        fields[key] = value
    fields["spec_sha256"] = _raw_hash(raw.get("spec_sha256"), label="SPEC_SHA256")
    fields["prepared_manifest_sha256"] = _raw_hash(
        raw.get("prepared_manifest_sha256"), label="PREPARED_MANIFEST_SHA256"
    )
    if _STORE_ID.fullmatch(fields["worker_store_id"]) is None:
        _fail("RECOVERY_WORKER_STORE_ID_INVALID")
    return fields


def _canonical_input_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        _fail(f"{label}_PATH_INVALID")
    return path


def _path_identity(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        _fail(f"{label}_INVALID")
    path = Path(value)
    if not path.is_absolute():
        _fail(f"{label}_NOT_ABSOLUTE")
    # Source files are validated by the real producer.  Here we only bind
    # identity; resolving a not-yet-mounted path is intentionally allowed.
    return path.resolve()


def _piece_source_path(piece: Mapping[str, object], *, label: str) -> Path:
    for key in ("remote_media", "segment_path", "source_path"):
        value = piece.get(key)
        if value is not None:
            return _path_identity(value, label=f"{label}.{key}")
    _fail(f"{label}_SOURCE_PATH_MISSING")


def _validate_source_scope(spec: Mapping[str, object], item: Mapping[str, object]) -> None:
    allowed: set[Path] = set()
    segment = item.get("segment_path")
    if segment is not None:
        allowed.add(_path_identity(segment, label="ITEM_SEGMENT_PATH"))
    for collection_name in ("pieces", "source_pieces"):
        collection = item.get(collection_name)
        if collection is None:
            continue
        if not isinstance(collection, list):
            _fail("ITEM_SOURCE_PIECES_INVALID")
        for index, raw in enumerate(collection):
            if not isinstance(raw, Mapping):
                _fail(f"ITEM_SOURCE_PIECE_{index}_INVALID")
            allowed.add(_piece_source_path(raw, label=f"ITEM_{collection_name}_{index}"))
    if not allowed:
        _fail("ITEM_SOURCE_SCOPE_EMPTY")

    pieces = spec.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        _fail("SPEC_SOURCE_PIECES_INVALID")
    for index, raw in enumerate(pieces):
        if not isinstance(raw, Mapping):
            _fail(f"SPEC_SOURCE_PIECE_{index}_INVALID")
        source = _piece_source_path(raw, label=f"SPEC_PIECE_{index}")
        # Padding changes timing, not source identity.  Only the canonical
        # source path participates in this scope check.
        if source not in allowed:
            _fail("SOURCE_SCOPE_MISMATCH")


def _validate_spec(
    *, spec_bytes: bytes, spec_path: Path, base: Path, date: str, candidate_id: str,
    item: Mapping[str, object],
) -> dict[str, object]:
    try:
        spec = json.loads(spec_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"SPEC_INVALID:{exc}")
    if not isinstance(spec, dict):
        _fail("SPEC_INVALID")
    if spec.get("candidate_id") != candidate_id or spec.get("date") != date:
        _fail("SPEC_IDENTITY_MISMATCH")
    item_date = item.get("date")
    if item_date is not None and item_date != date:
        _fail("ITEM_DATE_MISMATCH")
    if not _DATE.fullmatch(date):
        _fail("DATE_INVALID")
    output_value = spec.get("output_root")
    if not isinstance(output_value, str) or not output_value:
        _fail("SPEC_OUTPUT_ROOT_INVALID")
    output_root = _canonical_input_path(output_value, label="SPEC_OUTPUT_ROOT")
    expected_output = base / "out" / date
    if output_root != expected_output:
        _fail("SPEC_OUTPUT_ROOT_MISMATCH")

    runtime_value = spec.get("runtime_root")
    if runtime_value is None:
        runtime_root = output_root.parents[1]
    else:
        if not isinstance(runtime_value, str) or not runtime_value:
            _fail("SPEC_RUNTIME_ROOT_INVALID")
        runtime_root = _canonical_input_path(runtime_value, label="SPEC_RUNTIME_ROOT")
    if runtime_root != base:
        _fail("SPEC_RUNTIME_ROOT_MISMATCH")
    _validate_source_scope(spec, item)
    return spec


def _canonical_resume_root(
    *, spec_path: Path, spec_sha256: str, prepared_path: Path,
    spec: Mapping[str, object], candidate_id: str,
) -> Path:
    helper = canonical_prepared_resume_output_root
    if helper is None:
        try:
            from src.autoslice.producer_prepared_recovery import (
                canonical_prepared_resume_output_root as helper,
            )
        except ImportError as exc:  # pragma: no cover - staged owner wiring
            _fail(f"RESUME_ROOT_HELPER_MISSING:{exc}")
    try:
        value = helper(spec_path, spec_sha256, prepared_path)
    except Exception as exc:
        _fail(f"RESUME_ROOT_INVALID:{exc}")
    if not isinstance(value, Path):
        value = Path(value)
    if not value.is_absolute() or value.resolve() != value:
        _fail("RESUME_ROOT_INVALID")
    original = Path(str(spec["output_root"]))
    expected_parent = original / candidate_id / ".prepared-resumes"
    if value.parent != expected_parent or _SHA256.fullmatch(value.name) is None:
        _fail("RESUME_ROOT_SCOPE_INVALID")
    return value


def _build_child_env(
    *, runner: object, date: str, child_env: Mapping[str, str] | None,
    usage_path: Path,
) -> dict[str, str]:
    if child_env is None:
        factory = getattr(runner, "child_env_for_date", None)
        if callable(factory):
            value = factory(date)
        else:
            value = os.environ.copy()
    else:
        value = child_env
    if not isinstance(value, Mapping):
        _fail("CHILD_ENV_INVALID")
    env = {str(key): str(val) for key, val in value.items()}
    env["AUTOSLICE_CPA_USAGE_FILE"] = str(usage_path)
    return env


def _pipeline_fingerprint(runner: object, candidate_id: str) -> object:
    fn = getattr(runner, "talk_pipeline_fingerprint", None)
    if callable(fn):
        return fn(candidate_id)
    fn = getattr(runner, "pipeline_fingerprint", None)
    if callable(fn):
        return fn()
    return None


def _base_result(*, item: Mapping[str, object], candidate_id: str, rc: int,
                 log_path: Path, pipeline_fingerprint: object) -> dict[str, object]:
    segment_value = item.get("segment_path")
    if not isinstance(segment_value, str) or not segment_value:
        _fail("ITEM_SEGMENT_PATH_INVALID")
    result: dict[str, object] = {
        "candidate_id": candidate_id,
        "segment": Path(segment_value).name,
        "segment_path": segment_value,
        "rc": rc,
        "log": str(log_path),
        "pipeline_fingerprint": pipeline_fingerprint,
    }
    for field in _ITEM_FIELDS:
        result[field] = item.get(field, "") if field in {"hook", "lane"} else item.get(field)
    return result


def _prepared_reference(
    summary: Mapping[str, object], *, base: Path, candidate_id: str
) -> Mapping[str, object]:
    reference = summary.get("prepared_delivery")
    if not isinstance(reference, Mapping):
        prepared_summary = summary.get("prepared_summary")
        if isinstance(prepared_summary, Mapping):
            reference = prepared_summary.get("prepared_delivery")
    if not isinstance(reference, Mapping) or set(reference) != {
        "manifest_path", "prepared_sha256", "upload_enabled"
    }:
        _fail("PREPARED_REFERENCE_INVALID")
    manifest = reference.get("manifest_path")
    if not isinstance(manifest, str) or not manifest:
        _fail("PREPARED_REFERENCE_PATH_INVALID")
    manifest_path = Path(manifest)
    if (
        not manifest_path.is_absolute()
        or manifest_path.resolve() != manifest_path
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
    ):
        _fail("PREPARED_REFERENCE_PATH_INVALID")
    private_root = base / ".prepared-deliveries" / "talk" / candidate_id
    try:
        relative = manifest_path.relative_to(private_root)
    except ValueError:
        _fail("PREPARED_REFERENCE_SCOPE_INVALID")
    if (
        len(relative.parts) != 2
        or _SHA256.fullmatch(relative.parts[0]) is None
        or relative.parts[1] != "prepared.json"
    ):
        _fail("PREPARED_REFERENCE_SCOPE_INVALID")
    prepared_sha = reference.get("prepared_sha256")
    if not isinstance(prepared_sha, str) or _SHA256_SEMANTIC.fullmatch(prepared_sha) is None:
        _fail("PREPARED_REFERENCE_HASH_INVALID")
    if reference.get("upload_enabled") is not False:
        _fail("PREPARED_REFERENCE_UPLOAD_ENABLED")
    if summary.get("candidate_id") != candidate_id:
        _fail("SUMMARY_CANDIDATE_MISMATCH")
    return reference


def _read_attempt_output(log_path: Path, offset: int) -> str:
    try:
        with log_path.open("rb") as source:
            source.seek(offset)
            return source.read().decode("utf-8", "replace")
    except OSError as exc:
        _fail(f"LOG_READ_FAILED:{exc}")


def produce_prepared_talk(
    *,
    runner: object,
    date: str,
    item: Mapping[str, object],
    prepare_only: bool,
    reuse_cover: bool,
    timeout_seconds: int = 7200,
    child_env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Run the real producer for one hash-bound prepared Talk recovery.

    The original SPEC and prepared manifest are immutable inputs.  The child
    owns the recovery result; this function only checks its stdout contract and
    applies the existing ``prepared_talk_result`` projection.
    """

    if not isinstance(item, Mapping):
        _fail("ITEM_INVALID")
    if prepare_only is not True or reuse_cover is not False:
        _fail("REQUIRES_PRIVATE_PREPARE_WITHOUT_COVER_REUSE")
    if not isinstance(date, str) or _DATE.fullmatch(date) is None:
        _fail("DATE_INVALID")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        _fail("TIMEOUT_INVALID")

    candidate_id = _candidate_id(item)
    capsule = _recovery_capsule(item)
    base = _canonical_base(runner)
    repo = _canonical_repo(runner)
    spec_path = _canonical_input_path(capsule["spec_path"], label="SPEC")
    prepared_path = _canonical_input_path(capsule["prepared_path"], label="PREPARED")
    spec_bytes = _read_hash_bound_file(spec_path, capsule["spec_sha256"], label="SPEC")
    _read_hash_bound_file(
        prepared_path, capsule["prepared_manifest_sha256"], label="PREPARED_MANIFEST"
    )
    spec = _validate_spec(
        spec_bytes=spec_bytes, spec_path=spec_path, base=base, date=date,
        candidate_id=candidate_id, item=item,
    )
    derived_root = _canonical_resume_root(
        spec_path=spec_path, spec_sha256=capsule["spec_sha256"],
        prepared_path=prepared_path, spec=spec, candidate_id=candidate_id,
    )
    candidate_root = derived_root / candidate_id
    for path, label in ((derived_root, "RESUME_ROOT"), (candidate_root, "RESUME_CANDIDATE_ROOT")):
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_dir():
                _fail(f"{label}_UNSAFE")
        else:
            # The ordinary production observer may already have created these
            # empty directories. It writes no checkpoint or child output.
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
    usage_path = candidate_root / f"{candidate_id}.cpa-usage.jsonl"
    env = _build_child_env(
        runner=runner, date=date, child_env=child_env, usage_path=usage_path,
    )
    cmd = [
        sys.executable,
        str(repo / "scripts" / "produce_slice_package.py"),
        "--spec",
        str(spec_path),
        "--resume-prepared",
        str(prepared_path),
        "--resume-spec-sha256",
        capsule["spec_sha256"],
        "--resume-output-root",
        str(derived_root),
        "--prepare-only",
        "--correct",
        "cpa",
        "--ssh-host",
        "localhost",
        "--speaker-mode",
        "uniform_host",
    ]
    # Result bundles are create-only: a later attempt must never append to a
    # log already exported by an earlier result. Keep the recovery namespace
    # stable, and allocate one exclusive log for this actual subprocess.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=derived_root,
        prefix="producer-", suffix=".log", delete=False,
    ) as sink:
        log_path = Path(sink.name)
        completed = subprocess.run(
            cmd,
            check=False,
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            cwd=str(repo),
            env=env,
        )
    attempt_output = _read_attempt_output(log_path, 0)

    # The subprocess is never allowed to mutate the retained inputs, even on
    # failure.  Re-read the exact bytes rather than trusting metadata alone.
    _read_hash_bound_file(spec_path, capsule["spec_sha256"], label="SPEC_AFTER")
    _read_hash_bound_file(
        prepared_path, capsule["prepared_manifest_sha256"], label="PREPARED_AFTER"
    )

    try:
        summary = getattr(runner, "last_json_block")(attempt_output)
    except Exception as exc:
        _fail(f"SUMMARY_PARSE_FAILED:{exc}")
    result = _base_result(
        item=item,
        candidate_id=candidate_id,
        rc=int(completed.returncode),
        log_path=log_path,
        pipeline_fingerprint=_pipeline_fingerprint(runner, candidate_id),
    )
    if completed.returncode != 0:
        if not isinstance(summary, Mapping):
            summary = {}
        result["status"] = "failed"
        result["summary"] = dict(summary)
        classifier = getattr(runner, "classify_talk_failure", None)
        if not callable(classifier):
            _fail("FAILURE_CLASSIFIER_MISSING")
        classified = classifier(attempt_output)
        if not isinstance(classified, Mapping):
            _fail("FAILURE_CLASSIFICATION_INVALID")
        result.update(dict(classified))
        return result

    if not isinstance(summary, Mapping):
        _fail("SUMMARY_INVALID")
    _prepared_reference(summary, base=base, candidate_id=candidate_id)
    result["summary"] = dict(summary)
    result["prepared_recovery_proof"] = {
        "schema_version": _PROOF_SCHEMA,
        "spec_sha256": capsule["spec_sha256"],
        "prepared_manifest_sha256": capsule["prepared_manifest_sha256"],
    }
    return prepared_talk_result(result)


__all__ = ["produce_prepared_talk", "canonical_prepared_resume_output_root"]
