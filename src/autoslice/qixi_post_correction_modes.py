"""Fixed operational modes for the sealed Qixi public-surface closure.

This is deliberately separate from the transaction module: PLAN and DIAGNOSE
are observational, FULL_DRY_RUN stages only private bytes, and APPLY remains
the existing lock-held journal transaction.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from src.autoslice.qixi_post_correction_after_image_gates import (
    AFTER_IMAGE_PREDICATE_IDS,
    build_context,
    gate_callables,
    validate_target_roles,
)
from src.autoslice.qixi_post_correction_diagnostics import (
    PredicateResult,
    PredicateStatus,
    canonical_sha256,
    collect_predicates,
    diagnostic_root,
    matrix_document,
    sha256_bytes,
    unavailable_predicates,
    write_failure_receipt,
)
from src.autoslice.qixi_post_correction_stage_gates import (
    STAGE_PREDICATE_IDS,
    StageBuildObservation,
)
from src.autoslice.repository_asset_authority import require_repository_asset_authority


class Mode(StrEnum):
    PLAN = "PLAN"
    DIAGNOSE = "DIAGNOSE"
    FULL_DRY_RUN = "FULL_DRY_RUN"
    APPLY = "APPLY"


class ProviderAttemptStatus(StrEnum):
    """The only diagnostic claim allowed about one provider lane."""

    ATTEMPTED = "ATTEMPTED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    UNKNOWN = "UNKNOWN"


MATRIX_PREDICATE_IDS = (
    "runtime_source_fact_preflight",
    "stage_build",
) + STAGE_PREDICATE_IDS + AFTER_IMAGE_PREDICATE_IDS + (
    "formal_prepare_preimages",
    "formal_after_image",
    "stage_cleanup",
)


@dataclass(frozen=True, slots=True)
class _MatrixInputs:
    results: list[PredicateResult]
    runtime: object | None


def _closure() -> object:
    return importlib.import_module("src.autoslice.qixi_post_correction_public_surface")


def _status_result(name: str, status: PredicateStatus, reason: str) -> PredicateResult:
    return PredicateResult(name=name, status=status, reason=reason)


def _formal_result(
    check: Callable[..., None],
    *,
    predicate_id: str,
    **kwargs: object,
) -> PredicateResult:
    """Record one formal call without exposing its exception payload."""

    try:
        check(**kwargs)
    except Exception as exc:
        error_type = type(exc).__name__
        return _status_result(
            predicate_id,
            PredicateStatus.FAIL,
            "PREDICATE_REJECTED" if error_type.endswith("PublicSurfaceError") else "UNEXPECTED_EXCEPTION",
        )
    return _status_result(predicate_id, PredicateStatus.PASS, "SATISFIED")


def _runtime_matrix(
    closure: object,
    *,
    authority: Mapping[str, object],
    repo_root: Path,
    runtime_root: Path,
) -> _MatrixInputs:
    try:
        runtime = closure.validate_runtime(authority, repo_root=repo_root, runtime_root=runtime_root)
    except closure.QixiPostCorrectionPublicSurfaceError:
        return _MatrixInputs(
            [_status_result("runtime_source_fact_preflight", PredicateStatus.FAIL, "PREDICATE_REJECTED")],
            None,
        )
    except Exception:
        return _MatrixInputs(
            [_status_result("runtime_source_fact_preflight", PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION")],
            None,
        )
    return _MatrixInputs(
        [_status_result("runtime_source_fact_preflight", PredicateStatus.PASS, "SATISFIED")],
        runtime,
    )


def _unavailable_after_matrix(*, provider_needed: bool) -> list[PredicateResult]:
    provider_ids = {
        "cover_generation_shape",
        "cover_route",
        "cover_rendered_text_pixels",
        "cover_final_host_identity",
        "cover_final_participant_identity",
        "cover_punch_semantics",
        "cover_materialized_hashes",
        "cover_projection",
    }
    return [
        _status_result(
            predicate_id,
            PredicateStatus.NEEDS_PROVIDER
            if provider_needed and predicate_id in provider_ids
            else PredicateStatus.NOT_EVALUATED,
            "PROVIDER_REQUIRED"
            if provider_needed and predicate_id in provider_ids
            else "DEPENDENT_INPUT_UNAVAILABLE",
        )
        for predicate_id in AFTER_IMAGE_PREDICATE_IDS
    ]


def collect_matrix(
    *,
    authority: Mapping[str, object],
    repo_root: Path,
    runtime_root: Path,
    before: Mapping[Path, bytes | None] | None = None,
    after: Mapping[Path, bytes] | None = None,
) -> tuple[dict[str, object], object | None]:
    """Collect all fixed observational predicates without changing admission."""

    closure = _closure()
    baseline = _runtime_matrix(
        closure, authority=authority, repo_root=repo_root, runtime_root=runtime_root
    )
    if baseline.runtime is None:
        return matrix_document(
            baseline.results
            + [
                _status_result("stage_build", PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE"),
            ]
            + unavailable_predicates(STAGE_PREDICATE_IDS)
            + _unavailable_after_matrix(provider_needed=False)
            + unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")),
        ), None
    if before is None or after is None:
        return matrix_document(
            baseline.results
            + [
                _status_result("stage_build", PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE"),
            ]
            + unavailable_predicates(STAGE_PREDICATE_IDS)
            + _unavailable_after_matrix(provider_needed=True)
            + unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")),
        ), baseline.runtime
    results = list(baseline.results) + [
        _status_result("stage_build", PredicateStatus.PASS, "SATISFIED"),
    ] + unavailable_predicates(STAGE_PREDICATE_IDS)
    try:
        validate_target_roles(closure, authority=authority, after=after)
    except closure.QixiPostCorrectionPublicSurfaceError:
        results.append(_status_result("after_image_target_roles", PredicateStatus.FAIL, "PREDICATE_REJECTED"))
        results.append(_status_result("after_image_json_documents", PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE"))
        results.extend(
            unavailable_predicates(AFTER_IMAGE_PREDICATE_IDS[2:], needs_provider=False)
        )
        results.extend(unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")))
        return matrix_document(results), baseline.runtime
    except Exception:
        results.append(_status_result("after_image_target_roles", PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION"))
        results.append(_status_result("after_image_json_documents", PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE"))
        results.extend(unavailable_predicates(AFTER_IMAGE_PREDICATE_IDS[2:], needs_provider=False))
        results.extend(unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")))
        return matrix_document(results), baseline.runtime
    results.append(_status_result("after_image_target_roles", PredicateStatus.PASS, "SATISFIED"))
    try:
        context = build_context(closure, authority=authority, before=before, after=after)
    except closure.QixiPostCorrectionPublicSurfaceError:
        results.append(_status_result("after_image_json_documents", PredicateStatus.FAIL, "PREDICATE_REJECTED"))
        results.extend(unavailable_predicates(AFTER_IMAGE_PREDICATE_IDS[2:], needs_provider=False))
        results.extend(unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")))
        return matrix_document(results), baseline.runtime
    except Exception:
        results.append(_status_result("after_image_json_documents", PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION"))
        results.extend(unavailable_predicates(AFTER_IMAGE_PREDICATE_IDS[2:], needs_provider=False))
        results.extend(unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")))
        return matrix_document(results), baseline.runtime
    results.append(_status_result("after_image_json_documents", PredicateStatus.PASS, "SATISFIED"))
    evaluated: dict[str, PredicateResult] = {}
    for gate in gate_callables(closure, context):
        if any(evaluated[dependency].status is not PredicateStatus.PASS for dependency in gate.dependencies):
            evaluated[gate.predicate_id] = _status_result(
                gate.predicate_id, PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE"
            )
            continue
        if gate.not_required is not None and gate.not_required():
            evaluated[gate.predicate_id] = _status_result(
                gate.predicate_id, PredicateStatus.PASS, "NOT_REQUIRED_BY_ROUTE"
            )
            continue
        evaluated[gate.predicate_id] = collect_predicates(
            [(gate.predicate_id, lambda gate=gate: _gate_passes(gate.check))],
            expected_errors=(closure.QixiPostCorrectionPublicSurfaceError,),
        )[0]
    results.extend(evaluated[predicate_id] for predicate_id in AFTER_IMAGE_PREDICATE_IDS[2:])
    results.extend(unavailable_predicates(("formal_prepare_preimages", "formal_after_image", "stage_cleanup")))
    return matrix_document(results), baseline.runtime


def _gate_passes(check: Callable[[], None]) -> bool:
    check()
    return True


def _safe_before(targets: Mapping[Path, bytes]) -> dict[Path, bytes | None]:
    before: dict[Path, bytes | None] = {}
    for path in targets:
        if not os.path.lexists(path):
            before[path] = None
            continue
        before[path] = _stable_regular_snapshot(path)
    return before


def _with_formal(
    matrix: Mapping[str, object], *, prepare: PredicateResult, after_image: PredicateResult
) -> dict[str, object]:
    document = dict(matrix)
    predicates = document.get("predicates")
    if not isinstance(predicates, list):
        raise RuntimeError("diagnostic matrix is malformed")
    replacement = {
        prepare.name: prepare.as_dict(),
        after_image.name: after_image.as_dict(),
    }
    document["predicates"] = [replacement.get(str(row.get("name")), row) if isinstance(row, Mapping) else row for row in predicates]
    return document


def _with_stage_build(
    matrix: Mapping[str, object], status: PredicateStatus, reason: str
) -> dict[str, object]:
    document = dict(matrix)
    predicates = document.get("predicates")
    if not isinstance(predicates, list):
        raise RuntimeError("diagnostic matrix is malformed")
    replacement = _status_result("stage_build", status, reason).as_dict()
    document["predicates"] = [
        replacement if isinstance(row, Mapping) and row.get("name") == "stage_build" else row
        for row in predicates
    ]
    return document


def _with_stage_cleanup(
    matrix: Mapping[str, object], status: PredicateStatus, reason: str
) -> dict[str, object]:
    """Record the private-stage cleanup outcome without changing formal gates."""

    document = dict(matrix)
    predicates = document.get("predicates")
    if not isinstance(predicates, list):
        raise RuntimeError("diagnostic matrix is malformed")
    replacement = _status_result("stage_cleanup", status, reason).as_dict()
    document["predicates"] = [
        replacement if isinstance(row, Mapping) and row.get("name") == "stage_cleanup" else row
        for row in predicates
    ]
    return document


def _with_stage_observation(
    matrix: Mapping[str, object], observation: StageBuildObservation
) -> dict[str, object]:
    document = dict(matrix)
    predicates = document.get("predicates")
    if not isinstance(predicates, list):
        raise RuntimeError("diagnostic matrix is malformed")
    replacement = {item.name: item.as_dict() for item in observation.unavailable()}
    document["predicates"] = [
        replacement.get(str(row.get("name")), row) if isinstance(row, Mapping) else row
        for row in predicates
    ]
    return document


def _stage_manifest(closure: object, runtime: object, targets: Mapping[Path, bytes]) -> list[dict[str, object]]:
    fixed = {
        runtime.artifact_paths["record"]: "record",
        runtime.artifact_paths["delivery_record"]: "delivery_record",
        runtime.artifact_paths["publish"]: "publish",
        runtime.state_path: "state",
    }
    package_root = runtime.artifact_paths["record"].parent
    manifest: list[dict[str, object]] = []
    for index, (path, payload) in enumerate(sorted(targets.items(), key=lambda item: str(item[0]))):
        role = fixed.get(path)
        row: dict[str, object]
        if role is None:
            try:
                relative = path.relative_to(package_root).as_posix()
            except ValueError as exc:
                raise RuntimeError("after-image artifact is outside the package") from exc
            role = f"public_artifact:{index}"
            row = {"relative_path_sha256": sha256_bytes(relative.encode("utf-8"))}
        else:
            row = {}
        row.update(
            {"relative_role": role, "sha256": closure._file_sha256_from_bytes(payload), "bytes": len(payload)}
        )
        manifest.append(row)
    return manifest


def _partial_stage_manifest(closure: object, stage_root: Path) -> list[dict[str, object]]:
    """Report only hashes/bytes/relative roles from a private failed stage."""

    manifest: list[dict[str, object]] = []
    for index, path in enumerate(sorted(stage_root.rglob("*"))):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError("private stage acquired a symlink")
        if not stat.S_ISREG(info.st_mode):
            continue
        payload = _stable_regular_snapshot(path)
        manifest.append(
            {
                "relative_role": f"private_stage_artifact:{index}",
                "relative_path_sha256": sha256_bytes(
                    path.relative_to(stage_root).as_posix().encode("utf-8")
                ),
                "sha256": closure._file_sha256_from_bytes(payload),
                "bytes": len(payload),
            }
        )
    return manifest


def _partial_stage_or_empty(
    closure: object, stage_root: Path
) -> tuple[list[dict[str, object]], str]:
    """Never let a private diagnostic inventory replace the real gate result."""

    try:
        return _partial_stage_manifest(closure, stage_root), "AVAILABLE"
    except Exception:
        return [], "UNAVAILABLE"


def _deployed_seal(
    closure: object,
    *,
    repo_root: Path,
    test_seal: Mapping[str, str] | None,
) -> Mapping[str, str]:
    """Bind diagnostics to the deployed manifest, never an ambient Git HEAD."""

    if test_seal is not None:
        if set(test_seal) != {
            "deployed_commit",
            "authority_file_sha256",
            "deployed_manifest_file_sha256",
            "deployed_manifest_sha256",
        }:
            raise RuntimeError("diagnostic test seal is malformed")
        return dict(test_seal)
    authority_path = repo_root / closure.RELATIVE_AUTHORITY_PATH
    try:
        raw = _stable_regular_snapshot(authority_path)
        seal = require_repository_asset_authority(
            repo_root=repo_root,
            relative_path=closure.RELATIVE_AUTHORITY_PATH,
            observed_bytes=raw,
        )
    except Exception as exc:
        raise RuntimeError("deployed repository authority seal is unavailable") from exc
    if seal.mode != "DEPLOYED_MANIFEST":
        raise RuntimeError("diagnostic receipt requires deployed authority manifest")
    try:
        manifest_raw = _stable_regular_snapshot(repo_root / "DEPLOYED_AUTHORITY_MANIFEST.json")
        manifest = json.loads(manifest_raw.decode("utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("manifest is not an object")
        unsigned = dict(manifest)
        declared = unsigned.pop("manifest_sha256", None)
        entries = unsigned.get("entries")
        relative = closure.RELATIVE_AUTHORITY_PATH.as_posix()
        entry = entries.get(relative) if isinstance(entries, Mapping) else None
        if (
            set(unsigned) != {"schema_version", "deployed_commit", "entries"}
            or unsigned.get("schema_version") != "deployed-authority-manifest.v1"
            or unsigned.get("deployed_commit") != seal.commit
            or not isinstance(declared, str)
            or declared != canonical_sha256(unsigned)
            or not isinstance(entry, Mapping)
            or set(entry) != {"bytes", "sha256"}
            or entry.get("sha256") != seal.file_sha256
            or entry.get("bytes") != len(raw)
        ):
            raise ValueError("manifest contract is invalid")
        manifest_file_sha256 = sha256_bytes(manifest_raw)
    except Exception as exc:
        raise RuntimeError("deployed authority manifest receipt binding is unavailable") from exc
    return {
        "deployed_commit": seal.commit,
        "authority_file_sha256": seal.file_sha256,
        "deployed_manifest_sha256": declared,
        "deployed_manifest_file_sha256": manifest_file_sha256,
    }


def _stable_regular_snapshot(path: Path) -> bytes:
    """Read one manifest exactly once without following/reusing a replaced path."""

    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise OSError("unsafe manifest")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RuntimeError("deployed authority manifest is unavailable") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("deployed authority manifest inode drifted")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        closed = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("deployed authority manifest path drifted") from exc
    expected = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    if expected != (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    ) or expected != (
        closed.st_dev,
        closed.st_ino,
        closed.st_mode,
        closed.st_size,
        closed.st_mtime_ns,
        closed.st_ctime_ns,
    ) or expected != (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeError("deployed authority manifest drifted during snapshot")
    return b"".join(chunks)


def _require_fixed_matrix(matrix: Mapping[str, object]) -> None:
    if matrix.get("schema_version") != "qixi-public-surface-predicate-matrix.v1":
        raise RuntimeError("diagnostic predicate matrix schema drifts")
    predicates = matrix.get("predicates")
    if not isinstance(predicates, list) or [
        row.get("name") if isinstance(row, Mapping) else None for row in predicates
    ] != list(MATRIX_PREDICATE_IDS):
        raise RuntimeError("diagnostic predicate matrix order drifts")


def _failure_receipt(
    closure: object,
    *,
    repo_root: Path,
    authority: Mapping[str, object],
    runtime: object | None,
    matrix: Mapping[str, object],
    stage_manifest: list[dict[str, object]],
    stage_manifest_status: str = "AVAILABLE",
    provider_evidence: Mapping[str, Mapping[str, object]],
    operation_mode: Mode,
    test_seal: Mapping[str, str] | None,
) -> Path:
    _require_fixed_matrix(matrix)
    _validate_provider_evidence(provider_evidence)
    if stage_manifest_status not in {"AVAILABLE", "UNAVAILABLE"}:
        raise RuntimeError("diagnostic stage manifest status drifts")
    if operation_mode not in {Mode.FULL_DRY_RUN, Mode.APPLY}:
        raise RuntimeError("diagnostic operation mode is invalid")
    seal = _deployed_seal(closure, repo_root=repo_root, test_seal=test_seal)
    if runtime is None:
        artifacts = authority["artifacts"]
        assert isinstance(artifacts, Mapping)
        candidate_root = Path(str(artifacts["record"]["path"])).parent.parent
    else:
        candidate_root = runtime.artifact_paths["record"].parent.parent
    authority_sha256 = str(authority["authority_sha256"])
    root = diagnostic_root(candidate_root=candidate_root, authority_sha256=authority_sha256)
    sealed_before = authority["sealed_before"]
    assert isinstance(sealed_before, Mapping)
    preimages = {
        role: {
            "sha256": value["sha256"],
            "bytes": value["bytes"],
            "mode": value["mode"],
        }
        for role, value in sealed_before.items()
        if isinstance(value, Mapping)
    }
    body = {
        "schema_version": "qixi-public-surface-diagnostic.v2",
        "operation_mode": operation_mode.value,
        "deployed_commit": seal["deployed_commit"],
        "authority_internal_sha256": authority_sha256,
        "authority_file_sha256": seal["authority_file_sha256"],
        "deployed_manifest_sha256": seal["deployed_manifest_sha256"],
        "deployed_manifest_file_sha256": seal["deployed_manifest_file_sha256"],
        "candidate_id": closure.CANDIDATE_ID,
        "recording_date": closure.RECORDING_DATE,
        "runtime_preimages": preimages,
        "matrix": matrix,
        "stage_manifest_status": stage_manifest_status,
        "stage_manifest": stage_manifest,
        "provider_evidence": {lane: dict(value) for lane, value in provider_evidence.items()},
    }
    unsigned_sha = canonical_sha256(body)[7:]
    return write_failure_receipt(root=root, filename=f"diagnostic-{unsigned_sha}.json", body=body)


def _provider_evidence(
    *,
    source_fact: ProviderAttemptStatus,
    cover: ProviderAttemptStatus,
    source_fact_receipt_sha256s: list[str] | None = None,
    cover_receipt_sha256s: list[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Return conservative provider evidence, never callback payload hashes.

    A lane is ATTEMPTED only when its supplied callback was entered.  Receipt
    hashes are emitted only when a persisted, canonical private-stage receipt
    is independently observed; this stage has no such observer yet, so callers
    deliberately pass an empty list.  Cover staging has no equivalent call
    observer and therefore remains UNKNOWN rather than claiming an attempt.
    """

    return {
        "source_fact": {
            "attempt_status": source_fact.value,
            "receipt_sha256s": list(source_fact_receipt_sha256s or []),
        },
        "cover": {
            "attempt_status": cover.value,
            "receipt_sha256s": list(cover_receipt_sha256s or []),
        },
    }


def _observed_source_fact_receipt_sha256s(
    closure: object, runtime: object, targets: Mapping[Path, bytes],
) -> list[str]:
    """Observe only the canonical receipt projected into a private after-image.

    Provider callbacks are untrusted transport boundaries.  In particular, do
    not hash their raw response (which can contain prompt, completion or
    credentials).  A diagnostic may name a receipt only after the canonical
    private after-image has materialized all three required mirrors and their
    values agree.  The emitted value is the canonical receipt digest alone.
    """

    try:
        record_payload = targets[runtime.artifact_paths["record"]]
        publish_payload = targets[runtime.artifact_paths["publish"]]
        record = json.loads(record_payload)
        publish = json.loads(publish_payload)
        if not isinstance(record, Mapping) or not isinstance(publish, Mapping):
            return []
        story = record.get("story_contract")
        staging = record.get("publish_staging")
        if not isinstance(story, Mapping) or not isinstance(staging, Mapping):
            return []
        receipt = staging.get("source_fact_review")
        if (
            not isinstance(receipt, Mapping)
            or story.get("source_fact_review") != receipt
            or publish.get("source_fact_review") != receipt
        ):
            return []
        claimed = receipt.get("receipt_sha256")
        body = dict(receipt)
        body.pop("receipt_sha256", None)
        digest = canonical_sha256(body)
        if (
            not isinstance(claimed, str)
            or not claimed.startswith("sha256:")
            or len(claimed) != 71
            or claimed != digest
        ):
            return []
        return [claimed]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        # No partial/malformed after-image is evidence of a provider receipt.
        return []


def _validate_provider_evidence(value: Mapping[str, Mapping[str, object]]) -> None:
    if set(value) != {"source_fact", "cover"}:
        raise RuntimeError("diagnostic provider evidence lanes drift")
    statuses = {status.value for status in ProviderAttemptStatus}
    for lane, item in value.items():
        if set(item) != {"attempt_status", "receipt_sha256s"}:
            raise RuntimeError("diagnostic provider evidence shape drifts")
        if item["attempt_status"] not in statuses:
            raise RuntimeError("diagnostic provider attempt status drifts")
        receipts = item["receipt_sha256s"]
        if not isinstance(receipts, list) or any(
            not isinstance(receipt, str)
            or not receipt.startswith("sha256:")
            or len(receipt) != 71
            for receipt in receipts
        ):
            raise RuntimeError(f"diagnostic provider receipt evidence drifts: {lane}")


def _run_apply(
    closure: object,
    *,
    repo_root: Path,
    runtime_root: Path,
    normalized: Mapping[str, object],
    source_fact_llm_call: Callable[[str], str] | None,
    stage_publish: Callable[..., dict[str, object] | None] | None,
    test_seal: Mapping[str, str] | None,
    reload_deployed_authority: bool = False,
) -> dict[str, object]:
        source_attempt = ProviderAttemptStatus.NOT_ATTEMPTED
        source_receipts: list[str] = []
        observation = StageBuildObservation()
        default_source_fact: Callable[[str], str] | None = None

        def tracked_source_fact(prompt: str) -> str:
            nonlocal default_source_fact, source_attempt
            delegate = source_fact_llm_call
            if delegate is None:
                default_source_fact = default_source_fact or closure._default_source_fact_llm(runtime_root)
                delegate = default_source_fact
            source_attempt = ProviderAttemptStatus.ATTEMPTED
            result = delegate(prompt)
            return result

        def prejournal_failure(
            inputs: object,
            stage_root: Path,
            error: BaseException,
            targets: Mapping[Path, bytes] | None,
            cleanup_failed: bool,
        ) -> None:
            del error
            try:
                if targets is None:
                    matrix, _ = collect_matrix(
                        authority=normalized, repo_root=repo_root, runtime_root=runtime_root
                    )
                    matrix = _with_stage_observation(
                        _with_stage_build(matrix, PredicateStatus.FAIL, "PREDICATE_REJECTED"),
                        observation,
                    )
                    stage_manifest, stage_manifest_status = _partial_stage_or_empty(
                        closure, stage_root
                    )
                else:
                    if source_attempt is ProviderAttemptStatus.ATTEMPTED:
                        source_receipts[:] = _observed_source_fact_receipt_sha256s(
                            closure, inputs, targets
                        )
                    before = _safe_before(targets)
                    matrix, _ = collect_matrix(
                        authority=normalized,
                        repo_root=repo_root,
                        runtime_root=runtime_root,
                        before=before,
                        after=targets,
                    )
                    matrix = _with_stage_observation(matrix, observation)
                    prepare = _formal_result(
                        closure._assert_prepare_preimages,
                        inputs=inputs,
                        targets=targets,
                        predicate_id="formal_prepare_preimages",
                    )
                    after_image = _status_result(
                        "formal_after_image",
                        PredicateStatus.NOT_EVALUATED,
                        "DEPENDENT_INPUT_UNAVAILABLE",
                    )
                    if prepare.status is PredicateStatus.PASS:
                        after_image = _formal_result(
                            closure._validate_after_image,
                            authority=normalized,
                            before=before,
                            after=targets,
                            predicate_id="formal_after_image",
                        )
                    matrix = _with_formal(matrix, prepare=prepare, after_image=after_image)
                    try:
                        stage_manifest = _stage_manifest(closure, inputs, targets)
                    except Exception:
                        stage_manifest, stage_manifest_status = [], "UNAVAILABLE"
                    else:
                        stage_manifest_status = "AVAILABLE"
                matrix = _with_stage_cleanup(
                    matrix,
                    PredicateStatus.FAIL if cleanup_failed else PredicateStatus.PASS,
                    "UNEXPECTED_EXCEPTION" if cleanup_failed else "SATISFIED",
                )
                _failure_receipt(
                    closure,
                    repo_root=repo_root,
                    authority=normalized,
                    runtime=inputs,
                    matrix=matrix,
                    stage_manifest=stage_manifest,
                    stage_manifest_status=stage_manifest_status,
                    provider_evidence=_provider_evidence(
                        source_fact=source_attempt,
                        cover=ProviderAttemptStatus.UNKNOWN,
                        source_fact_receipt_sha256s=source_receipts,
                    ),
                    operation_mode=Mode.APPLY,
                    test_seal=test_seal,
                )
            except RuntimeError:
                # Receipt publication requires an independently verified seal;
                # never hide the original transactional failure if it is absent.
                pass

        return closure._finalize_legacy(
            apply=True, repo_root=repo_root, runtime_root=runtime_root,
            source_fact_llm_call=tracked_source_fact, authority=normalized,
            _stage_publish=stage_publish or closure._stage_publish_draft,
            _stage_observation=observation, _prejournal_failure=prejournal_failure,
            _reload_deployed_authority=reload_deployed_authority)


def run(mode: Mode, *, repo_root: Path, runtime_root: Path,
        source_fact_llm_call: Callable[[str], str] | None = None,
        authority: Mapping[str, object] | None = None,
        stage_publish: Callable[..., dict[str, object] | None] | None = None,
        _test_deployed_seal: Mapping[str, str] | None = None) -> dict[str, object]:
    """Run one fixed operational mode without accepting mutable identity input."""

    closure = _closure()
    loaded = dict(authority) if authority is not None else closure.load_deployed_authority(repo_root)
    normalized = closure.validate_authority(loaded)
    if runtime_root != Path(str(normalized["runtime_root"])):
        raise closure.QixiPostCorrectionPublicSurfaceError("runtime root differs from the sealed authority")
    if mode is Mode.APPLY:
        return _run_apply(
            closure, repo_root=repo_root, runtime_root=runtime_root, normalized=normalized,
            source_fact_llm_call=source_fact_llm_call, stage_publish=stage_publish,
            test_seal=_test_deployed_seal, reload_deployed_authority=authority is None,
        )
    if mode is Mode.PLAN:
        closure.validate_runtime(normalized, repo_root=repo_root, runtime_root=runtime_root)
        return {"schema_version": "qixi-post-correction-public-surface-plan.v2", "status": "PLAN_PASS",
                "candidate_id": closure.CANDIDATE_ID, "recording_date": closure.RECORDING_DATE,
                "upload_enabled": False, "formal_validation": "NOT_RUN"}
    matrix, runtime = collect_matrix(
        authority=normalized, repo_root=repo_root, runtime_root=runtime_root
    )
    if mode is Mode.DIAGNOSE:
        return {"schema_version": "qixi-post-correction-public-surface-diagnose.v1",
                "status": "DIAGNOSE_COMPLETE", "candidate_id": closure.CANDIDATE_ID,
                "recording_date": closure.RECORDING_DATE, "upload_enabled": False,
                "matrix": matrix, "formal_validation": "NOT_RUN"}
    if mode is not Mode.FULL_DRY_RUN:
        raise ValueError("unsupported Qixi public-surface mode")
    if runtime is None:
        outcome = {
            "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
            "status": "FULL_DRY_RUN_BLOCKED",
            "candidate_id": closure.CANDIDATE_ID,
            "recording_date": closure.RECORDING_DATE,
            "upload_enabled": False,
            "formal_validation": "NOT_RUN",
            "matrix": matrix,
        }
        try:
            receipt = _failure_receipt(
                closure,
                repo_root=repo_root,
                authority=normalized,
                runtime=None,
                matrix=matrix,
                stage_manifest=[],
                provider_evidence=_provider_evidence(
                    source_fact=ProviderAttemptStatus.NOT_ATTEMPTED,
                    cover=ProviderAttemptStatus.NOT_ATTEMPTED,
                ),
                operation_mode=Mode.FULL_DRY_RUN,
                test_seal=_test_deployed_seal,
            )
        except Exception:
            outcome["diagnostic_receipt_status"] = "UNAVAILABLE"
        else:
            outcome["diagnostic_receipt"] = str(receipt)
        return outcome
    stage_root = Path(tempfile.mkdtemp(prefix=".qixi-public-surface-stage-", dir=runtime.artifact_paths["record"].parent))
    source_attempt = ProviderAttemptStatus.NOT_ATTEMPTED
    source_receipts: list[str] = []
    observation = StageBuildObservation()

    def tracked_source_fact(prompt: str) -> str:
        nonlocal source_attempt
        if source_fact_llm_call is None:
            raise closure.QixiPostCorrectionPublicSurfaceError("source-fact provider is unavailable")
        source_attempt = ProviderAttemptStatus.ATTEMPTED
        result = source_fact_llm_call(prompt)
        return result

    outcome: dict[str, object] | None = None
    stage_manifest: list[dict[str, object]] = []
    stage_manifest_status = "AVAILABLE"
    cleanup_failed = False
    try:
        targets, _metadata = closure._build_after_image(
            runtime,
            stage_root=stage_root,
            source_fact_llm_call=tracked_source_fact,
            stage_publish=stage_publish or closure._stage_publish_draft,
            observation=observation,
        )
        if source_attempt is ProviderAttemptStatus.ATTEMPTED:
            source_receipts[:] = _observed_source_fact_receipt_sha256s(
                closure, runtime, targets
            )
        before = _safe_before(targets)
        matrix, _ = collect_matrix(
            authority=normalized,
            repo_root=repo_root,
            runtime_root=runtime_root,
            before=before,
            after=targets,
        )
        matrix = _with_stage_observation(matrix, observation)
        formal_error: Exception | None = None
        prepare_result = _status_result("formal_prepare_preimages", PredicateStatus.PASS, "SATISFIED")
        try:
            closure._assert_prepare_preimages(runtime, targets)
        except closure.QixiPostCorrectionPublicSurfaceError:
            prepare_result = _status_result(
                "formal_prepare_preimages", PredicateStatus.FAIL, "PREDICATE_REJECTED"
            )
            formal_error = RuntimeError("formal prepare preimages rejected")
        except Exception:
            prepare_result = _status_result(
                "formal_prepare_preimages", PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION"
            )
            formal_error = RuntimeError("formal prepare preimages failed")
        after_result = _status_result("formal_after_image", PredicateStatus.NOT_EVALUATED, "DEPENDENT_INPUT_UNAVAILABLE")
        if formal_error is None:
            try:
                closure._validate_after_image(authority=normalized, before=before, after=targets)
                after_result = _status_result("formal_after_image", PredicateStatus.PASS, "SATISFIED")
            except closure.QixiPostCorrectionPublicSurfaceError:
                after_result = _status_result("formal_after_image", PredicateStatus.FAIL, "PREDICATE_REJECTED")
                formal_error = RuntimeError("formal after-image rejected")
            except Exception:
                after_result = _status_result("formal_after_image", PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION")
                formal_error = RuntimeError("formal after-image failed")
        matrix = _with_formal(matrix, prepare=prepare_result, after_image=after_result)
        try:
            stage_manifest = _stage_manifest(closure, runtime, targets)
        except Exception:
            stage_manifest, stage_manifest_status = [], "UNAVAILABLE"
        if formal_error is not None:
            outcome = {
                "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
                "status": "FULL_DRY_RUN_BLOCKED",
                "candidate_id": closure.CANDIDATE_ID,
                "recording_date": closure.RECORDING_DATE,
                "upload_enabled": False,
                "formal_validation": "FAIL",
                "matrix": matrix,
                "diagnostic_stage_manifest_status": stage_manifest_status,
            }
        elif stage_manifest_status == "UNAVAILABLE":
            outcome = {
                "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
                "status": "FULL_DRY_RUN_BLOCKED",
                "candidate_id": closure.CANDIDATE_ID,
                "recording_date": closure.RECORDING_DATE,
                "upload_enabled": False,
                "formal_validation": "PASS",
                "matrix": matrix,
                "diagnostic_stage_manifest_status": stage_manifest_status,
            }
        else:
            outcome = {
                "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
                "status": "FULL_DRY_RUN_PASS",
                "candidate_id": closure.CANDIDATE_ID,
                "recording_date": closure.RECORDING_DATE,
                "upload_enabled": False,
                "formal_validation": "PASS",
                "matrix": matrix,
                "stage_manifest": stage_manifest,
            }
    except closure.QixiPostCorrectionPublicSurfaceError:
        matrix, _ = collect_matrix(
            authority=normalized, repo_root=repo_root, runtime_root=runtime_root
        )
        matrix = _with_stage_observation(
            _with_stage_build(matrix, PredicateStatus.FAIL, "PREDICATE_REJECTED"), observation
        )
        stage_manifest, stage_manifest_status = _partial_stage_or_empty(closure, stage_root)
        outcome = {
            "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
            "status": "FULL_DRY_RUN_BLOCKED",
            "candidate_id": closure.CANDIDATE_ID,
            "recording_date": closure.RECORDING_DATE,
            "upload_enabled": False,
            "formal_validation": "NOT_RUN",
            "matrix": matrix,
            "diagnostic_stage_manifest_status": stage_manifest_status,
        }
    except Exception:
        matrix, _ = collect_matrix(
            authority=normalized, repo_root=repo_root, runtime_root=runtime_root
        )
        matrix = _with_stage_observation(
            _with_stage_build(matrix, PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION"), observation
        )
        stage_manifest, stage_manifest_status = _partial_stage_or_empty(closure, stage_root)
        outcome = {
            "schema_version": "qixi-post-correction-public-surface-full-dry-run.v1",
            "status": "FULL_DRY_RUN_BLOCKED",
            "candidate_id": closure.CANDIDATE_ID,
            "recording_date": closure.RECORDING_DATE,
            "upload_enabled": False,
            "formal_validation": "NOT_RUN",
            "matrix": matrix,
            "diagnostic_stage_manifest_status": stage_manifest_status,
        }
    finally:
        try:
            closure._remove_private_stage(stage_root)
        except Exception:
            cleanup_failed = True
    if outcome is None:
        raise RuntimeError("full dry-run produced no outcome")
    cleanup_status = PredicateStatus.FAIL if cleanup_failed else PredicateStatus.PASS
    cleanup_reason = "UNEXPECTED_EXCEPTION" if cleanup_failed else "SATISFIED"
    outcome["matrix"] = _with_stage_cleanup(outcome["matrix"], cleanup_status, cleanup_reason)
    if cleanup_failed and outcome["status"] == "FULL_DRY_RUN_PASS":
        outcome["status"] = "FULL_DRY_RUN_BLOCKED"
    if outcome["status"] == "FULL_DRY_RUN_BLOCKED":
        try:
            receipt = _failure_receipt(
                closure,
                repo_root=repo_root,
                authority=normalized,
                runtime=runtime,
                matrix=outcome["matrix"],
                stage_manifest=stage_manifest,
                stage_manifest_status=stage_manifest_status,
                provider_evidence=_provider_evidence(
                    source_fact=source_attempt,
                    cover=ProviderAttemptStatus.UNKNOWN,
                    source_fact_receipt_sha256s=source_receipts,
                ),
                operation_mode=Mode.FULL_DRY_RUN,
                test_seal=_test_deployed_seal,
            )
        except Exception:
            outcome["diagnostic_receipt_status"] = "UNAVAILABLE"
        else:
            outcome["diagnostic_receipt"] = str(receipt)
    return outcome
