"""Current-package mechanical acceptance without inventing human playback.

Current operating rule: a validated burn pipeline plus its actual current
input/output checks does not need another full viewing of every product.
Semantic, source, boundary, cover and publication authority stay with the
existing canonical gates. This receipt is an alternative to a new human claim,
not an upload permission or a way to relabel failed upstream evidence.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from typing import Mapping

from scripts.audit_review_package import (
    AUDIT_POLICY_EPOCH,
    AUDIT_SCHEMA_VERSION,
    audit_package,
)
from src.autoslice import final_human_review as review
from src.autoslice.package_audit_binding import audit_binding
from src.autoslice.final_media_review_bootstrap import (
    FinalMediaReviewBootstrapError,
    bootstrap_final_media_review_job,
)
from src.autoslice.final_media_review_capability_autobootstrap import (
    CAPABILITY_BOOTSTRAP_BLOCKING_STATUSES,
    FinalMediaReviewCapabilityAutobootstrapError,
    ensure_runtime_raw_av_capability,
)
from src.autoslice.final_media_review_materialization import (
    FinalMediaReviewMaterializationError,
    resolve_or_materialize_review_job,
)
from src.autoslice.final_media_review_raw_av import (
    FinalMediaReviewRawAvError,
    bind_review_job_to_runtime_capability,
)
from src.autoslice.producer_media import _validated_burned_artifact, _validated_burned_ass_artifact
from src.autoslice.review_package_portable_evidence import contained_package_artifact

SCHEMA_VERSION = "lidousha-mechanical-delivery-review.v1"
STATUS = "VERIFIED_MECHANICAL_DELIVERY"
_FIELDS = frozenset({
    "schema_version", "status", "scope", "checked_at", "checked_by",
    "fresh_human_full_playback_claimed", "new_upload_authorized", "bindings",
})


def _need(condition: bool, detail: str) -> None:
    if not condition:
        raise ValueError("mechanical delivery review: " + detail)


def _binding(root: Path, relative: str) -> dict[str, object]:
    path = review._regular_package_file(root, relative)
    return {"path": relative, "sha256": review._sha256(path), "bytes": path.stat().st_size}


def _bindings(package_root: Path, audit_path: Path) -> dict[str, object]:
    root = review._package_directory(package_root)
    audit_relative = str(audit_path.absolute().relative_to(root))
    safe_audit = review._regular_package_file(root, audit_relative)
    manifest_path = review._regular_package_file(root, "review_manifest.json")
    saved = review._json_object(safe_audit, source="package audit")
    current = audit_package(root)
    _need(
        current.get("schema_version") == AUDIT_SCHEMA_VERSION
        and current.get("policy_epoch") == AUDIT_POLICY_EPOCH
        and current.get("passed") is True
        and current.get("blocking_issue_count") == 0,
        "current canonical audit rejected the package",
    )
    _need(audit_binding(saved) == audit_binding(current), "canonical audit is stale")
    manifest = review._json_object(manifest_path, source="review manifest")
    # Reuse the exact current candidate/title/target/cover/intro identity checks;
    # do not reuse or fabricate a human observer's eight textual observations.
    order, closures = review._manifest_items(manifest, package_root=root)
    items = []
    for candidate_id in order:
        closure = closures[candidate_id]
        record_path = str(closure["record_path"])
        record = review._json_object(
            review._regular_package_file(root, record_path), source="record",
        )
        preview = record.get("burned_preview")
        _need(isinstance(preview, dict), "burn record is missing")
        video = contained_package_artifact(root, preview.get("path"), label="burned video")
        ass = contained_package_artifact(root, preview.get("ass_path"), label="burned ASS")
        _need(video == review._regular_package_file(root, closure["artifacts"]["video"]),
              "burned video differs from the manifest video")
        # Portable paths are projected only in memory. Reuse the producer's own
        # status and dual video/ASS hash checks, rather than trust a PASS label.
        portable_record = {**record, "burned_preview": {
            **preview, "path": str(video), "ass_path": str(ass),
        }}
        try:
            _validated_burned_artifact(portable_record)
            _validated_burned_ass_artifact(portable_record)
        except RuntimeError as exc:
            raise ValueError("mechanical delivery review: burn output binding invalid") from exc
        item = {
            "candidate_id": candidate_id,
            "burned_ass": _binding(root, ass.relative_to(root).as_posix()),
            "title": closure["title"],
            "publication_target": closure["publication_target"],
            "publication_authority": record["recovery_publication_authority"],
            "final_duration_ms": closure["final_duration_ms"],
            "record": _binding(root, record_path),
            "artifacts": {
                kind: _binding(root, str(relative))
                for kind, relative in closure["artifacts"].items()
            },
        }
        # A normal autonomous perceptual consumer may bind its durable state
        # into the package.  Mechanical review must not silently ignore a
        # present, content-required BLOCK/WAIT/FAILED state; doing so would turn
        # input integrity into a false content PASS.  Historical packages with
        # no such state retain the established mechanical route.
        review_job_relative = (
            f"verification/{candidate_id}.final-media-review-job.json"
        )
        review_state_relative = (
            f"verification/{candidate_id}.final-media-review-state.json"
        )
        bootstrap_relative = (
            f"verification/{candidate_id}.final-media-review-bootstrap.json"
        )
        verification_dir = root / "verification"
        review_job_path = root / review_job_relative
        review_state_path = root / review_state_relative
        bootstrap_path = root / bootstrap_relative
        if (
            not review_job_path.exists()
            and not review_job_path.is_symlink()
            and closure.get("duration_witness_mismatch") is True
        ):
            try:
                contract_sha, contracts = review._review_contracts()  # noqa: SLF001
                bootstrap_final_media_review_job(
                    package_root=root,
                    candidate_id=candidate_id,
                    closure=closure,
                    review_contract_sha256=contract_sha,
                    review_points=contracts.get(candidate_id),
                )
            except (
                FinalMediaReviewBootstrapError,
                review.FinalHumanReviewError,
            ) as exc:
                raise ValueError(
                    "mechanical delivery review: final media bootstrap invalid"
                ) from exc
        has_review_sidecar = (
            review_job_path.exists()
            or review_job_path.is_symlink()
            or review_state_path.exists()
            or review_state_path.is_symlink()
        )
        if has_review_sidecar:
            _need(
                verification_dir.is_dir()
                and not verification_dir.is_symlink()
                and verification_dir.resolve() == root / "verification",
                "verification directory is not contained",
            )
        if review_job_path.exists() or review_job_path.is_symlink():
            from src.autoslice.final_media_review_inputs import (
                FinalMediaReviewInputError,
                consume_review_job,
            )
            try:
                # The normal consumer resolves a deterministic local successor
                # before touching CPA. This closes exact-audio gaps without an
                # operator command; raw-AV capability and content PASS remain
                # separate, fail-closed states.
                safe_review_job = review._regular_package_file(
                    root, review_job_relative
                )
                resolved = resolve_or_materialize_review_job(
                    safe_review_job, allowed_root=root
                )
                active_job_path = review._regular_package_file(
                    root,
                    Path(str(resolved["active_job_path"]))
                    .relative_to(root)
                    .as_posix(),
                )
                selected_runtime_root = (
                    os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
                    or os.environ.get("AUTOSLICE_BASE")
                    or None
                )
                capability_binding: dict[str, object] | None = None
                if selected_runtime_root is not None:
                    capability_bootstrap = ensure_runtime_raw_av_capability(
                        selected_runtime_root
                    )
                    if (
                        capability_bootstrap.get("status")
                        in CAPABILITY_BOOTSTRAP_BLOCKING_STATUSES
                    ):
                        raise ValueError(
                            "final-media capability bootstrap is not complete: "
                            f"{capability_bootstrap.get('status')}"
                        )
                    capability_binding = bind_review_job_to_runtime_capability(
                        active_job_path,
                        allowed_root=root,
                        runtime_root=selected_runtime_root,
                    )
                    active_job_path = review._regular_package_file(
                        root,
                        Path(str(capability_binding["active_job_path"]))
                        .relative_to(root)
                        .as_posix(),
                    )
                consume_review_job(
                    active_job_path,
                    review_state_path,
                    runtime_root=selected_runtime_root,
                    allowed_root=root,
                )
            except (
                FinalMediaReviewCapabilityAutobootstrapError,
                FinalMediaReviewInputError,
                FinalMediaReviewMaterializationError,
                FinalMediaReviewRawAvError,
                KeyError,
                ValueError,
            ) as exc:
                raise ValueError(
                    "mechanical delivery review: final media review job invalid"
                ) from exc
            item["final_media_review_job"] = _binding(
                root, review_job_relative
            )
            if bootstrap_path.exists() or bootstrap_path.is_symlink():
                item["final_media_review_bootstrap"] = _binding(
                    root, bootstrap_relative
                )
            active_relative = active_job_path.relative_to(root).as_posix()
            if active_relative != review_job_relative:
                item["final_media_review_active_job"] = _binding(
                    root, active_relative
                )
            materialization_receipt = resolved.get("receipt_path")
            if materialization_receipt is not None:
                receipt_relative = Path(
                    str(materialization_receipt)
                ).relative_to(root).as_posix()
                item["final_media_review_materialization"] = _binding(
                    root, receipt_relative
                )
            if capability_binding is not None:
                binding_path = capability_binding.get("binding_path")
                if binding_path is not None:
                    binding_relative = Path(str(binding_path)).relative_to(
                        root
                    ).as_posix()
                    item["final_media_review_transport_capability"] = _binding(
                        root, binding_relative
                    )
                capability_receipt = capability_binding.get("receipt_path")
                if capability_receipt is not None:
                    capability_receipt_relative = Path(
                        str(capability_receipt)
                    ).relative_to(root).as_posix()
                    item["final_media_review_transport_binding"] = _binding(
                        root, capability_receipt_relative
                    )
        if review_state_path.exists() or review_state_path.is_symlink():
            from src.autoslice.final_media_review_inputs import (
                FinalMediaReviewInputError,
                consumer_state_release_passes,
                load_consumer_state,
            )
            try:
                safe_review_state = review._regular_package_file(
                    root, review_state_relative
                )
                review_state = load_consumer_state(
                    safe_review_state,
                    candidate_id=candidate_id,
                    source_video_sha256=review._sha256(video),
                )
            except FinalMediaReviewInputError as exc:
                raise ValueError(
                    "mechanical delivery review: final media review state invalid"
                ) from exc
            if (
                review_state.get("content_review_required") is True
                and not consumer_state_release_passes(review_state)
            ):
                raise ValueError(
                    "mechanical delivery review: final media perceptual review unresolved"
                )
            item["final_media_review_state"] = _binding(
                root, review_state_relative
            )
        items.append(item)
    return {
        "review_manifest": _binding(root, "review_manifest.json"),
        "package_audit": _binding(root, audit_relative),
        "canonical_audit": audit_binding(current),
        "items": items,
    }


def _validate_receipt_envelope(value: Mapping[str, object]) -> None:
    _need(
        isinstance(value, Mapping) and set(value) == _FIELDS,
        "invalid receipt fields",
    )
    _need(
        value["schema_version"] == SCHEMA_VERSION
        and value["status"] == STATUS
        and value["scope"] == "same_bv_validated_pipeline_current_package"
        and value["checked_by"] == "canonical-package-auditor"
        and value["fresh_human_full_playback_claimed"] is False
        and value["new_upload_authorized"] is False,
        "receipt overclaims review or authorization",
    )
    _need(isinstance(value["checked_at"], str), "invalid timestamp")
    at = datetime.fromisoformat(value["checked_at"].replace("Z", "+00:00"))
    _need(at.utcoffset() is not None, "timestamp lacks timezone")


def _receipt_binding(
    root: Path,
    raw: object,
    *,
    label: str,
    expected_relative: str | None = None,
) -> tuple[dict[str, object], Path]:
    _need(isinstance(raw, Mapping), f"{label} binding missing")
    _need(
        set(raw) == {"path", "sha256", "bytes"},
        f"{label} binding fields invalid",
    )
    relative = raw.get("path")
    _need(isinstance(relative, str) and relative, f"{label} path invalid")
    if expected_relative is not None:
        _need(relative == expected_relative, f"{label} path differs")
    path = review._regular_package_file(root, relative)
    expected = _binding(root, relative)
    _need(dict(raw) == expected, f"{label} binding drift")
    return expected, path


def _read_only_final_media_bindings(
    root: Path,
    candidate_id: str,
    *,
    video: Path,
    item: Mapping[str, object],
) -> dict[str, object]:
    from src.autoslice.final_media_review_inputs import (
        FinalMediaReviewInputError,
        consumer_state_release_passes,
        load_consumer_state,
    )

    verification = root / "verification"
    job_relative = f"verification/{candidate_id}.final-media-review-job.json"
    state_relative = f"verification/{candidate_id}.final-media-review-state.json"
    bootstrap_relative = (
        f"verification/{candidate_id}.final-media-review-bootstrap.json"
    )
    job_path = root / job_relative
    state_path = root / state_relative
    bootstrap_path = root / bootstrap_relative
    has_job = job_path.exists() or job_path.is_symlink()
    has_state = state_path.exists() or state_path.is_symlink()
    has_bootstrap = bootstrap_path.exists() or bootstrap_path.is_symlink()
    if not (has_job or has_state or has_bootstrap):
        return {}
    _need(
        verification.is_dir()
        and not verification.is_symlink()
        and verification.resolve() == root / "verification",
        "verification directory is not contained",
    )
    _need(has_job and has_state, "final media review sidecars are incomplete")
    expected: dict[str, object] = {
        "final_media_review_job": _binding(root, job_relative),
        "final_media_review_state": _binding(root, state_relative),
    }
    if has_bootstrap:
        expected["final_media_review_bootstrap"] = _binding(
            root, bootstrap_relative
        )
    try:
        state = load_consumer_state(
            review._regular_package_file(root, state_relative),
            candidate_id=candidate_id,
            source_video_sha256=review._sha256(video),
        )
    except FinalMediaReviewInputError as exc:
        raise ValueError(
            "mechanical delivery review: final media review state invalid"
        ) from exc
    _need(
        state.get("content_review_required") is not True
        or consumer_state_release_passes(state),
        "final media perceptual review unresolved",
    )
    for key in (
        "final_media_review_active_job",
        "final_media_review_materialization",
    ):
        raw = item.get(key)
        if raw is None:
            continue
        binding, path = _receipt_binding(root, raw, label=key)
        _need(
            path.is_relative_to(root / "verification"),
            f"{key} escapes verification",
        )
        expected[key] = binding
    return expected


def _read_only_mechanical_item(
    root: Path,
    *,
    candidate_id: str,
    closure: Mapping[str, object],
    raw_item: object,
) -> dict[str, object]:
    _need(isinstance(raw_item, Mapping), "mechanical item is invalid")
    record_path = str(closure["record_path"])
    record_file = review._regular_package_file(root, record_path)
    record = review._json_object(record_file, source="record")
    preview = record.get("burned_preview")
    _need(isinstance(preview, dict), "burn record is missing")
    video = contained_package_artifact(
        root, preview.get("path"), label="burned video"
    )
    ass = contained_package_artifact(root, preview.get("ass_path"), label="burned ASS")
    _need(
        video == review._regular_package_file(
            root, closure["artifacts"]["video"]
        ),
        "burned video differs from the manifest video",
    )
    portable_record = {
        **record,
        "burned_preview": {**preview, "path": str(video), "ass_path": str(ass)},
    }
    try:
        _validated_burned_artifact(portable_record)
        _validated_burned_ass_artifact(portable_record)
    except RuntimeError as exc:
        raise ValueError(
            "mechanical delivery review: burn output binding invalid"
        ) from exc
    expected: dict[str, object] = {
        "candidate_id": candidate_id,
        "burned_ass": _binding(root, ass.relative_to(root).as_posix()),
        "title": closure["title"],
        "publication_target": closure["publication_target"],
        "publication_authority": record["recovery_publication_authority"],
        "final_duration_ms": closure["final_duration_ms"],
        "record": _binding(root, record_path),
        "artifacts": {
            kind: _binding(root, str(relative))
            for kind, relative in closure["artifacts"].items()
        },
    }
    final_media = _read_only_final_media_bindings(
        root, candidate_id, video=video, item=raw_item
    )
    expected.update(final_media)
    allowed = set(expected)
    _need(set(raw_item) == allowed, "mechanical item fields drift")
    _need(dict(raw_item) == expected, "mechanical item binding drift")
    return expected


def validate_saved_mechanical_receipt(
    value: Mapping[str, object],
    package_root: Path,
) -> dict[str, object]:
    """Replay a saved receipt against current bytes without running the auditor.

    This read-only path is for publication-readiness discovery.  It validates
    the saved canonical audit, manifest closure, burn identity, artifact bytes,
    and any present final-media state.  It never creates sidecars, invokes a
    provider, or substitutes for :func:`validate_mechanical_receipt` at upload.
    """

    _validate_receipt_envelope(value)
    root = review._package_directory(package_root)
    bindings = value.get("bindings")
    _need(
        isinstance(bindings, Mapping)
        and set(bindings)
        == {"review_manifest", "package_audit", "canonical_audit", "items"},
        "mechanical bindings are invalid",
    )
    _receipt_binding(
        root,
        bindings["review_manifest"],
        label="review manifest",
        expected_relative="review_manifest.json",
    )
    audit_binding_row, audit_path = _receipt_binding(
        root, bindings["package_audit"], label="package audit"
    )
    audit_relative = str(audit_binding_row["path"])
    _need(
        audit_relative == "package_audit.json"
        or audit_relative == "verification/package-audit.json",
        "package audit is not current-instance canonical",
    )
    saved = review._json_object(audit_path, source="package audit")
    _need(
        saved.get("schema_version") == AUDIT_SCHEMA_VERSION
        and saved.get("policy_epoch") == AUDIT_POLICY_EPOCH
        and saved.get("passed") is True
        and saved.get("blocking_issue_count") == 0
        and saved.get("root") == str(root),
        "saved canonical audit rejected the package",
    )
    _need(
        bindings["canonical_audit"] == audit_binding(saved),
        "saved canonical audit binding drift",
    )
    manifest = review._json_object(
        root / "review_manifest.json", source="review manifest"
    )
    order, closures = review._manifest_items(manifest, package_root=root)
    raw_items = bindings.get("items")
    _need(
        isinstance(raw_items, list) and len(raw_items) == len(order),
        "mechanical item count drift",
    )
    expected_items = [
        _read_only_mechanical_item(
            root,
            candidate_id=candidate_id,
            closure=closures[candidate_id],
            raw_item=raw_items[index],
        )
        for index, candidate_id in enumerate(order)
    ]
    _need(raw_items == expected_items, "mechanical item order drift")
    return dict(value)


def build_mechanical_receipt(package_root: Path, audit_path: Path) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS,
        "scope": "same_bv_validated_pipeline_current_package",
        "checked_at": datetime.now().astimezone().isoformat(),
        "checked_by": "canonical-package-auditor",
        "fresh_human_full_playback_claimed": False,
        "new_upload_authorized": False,
        "bindings": _bindings(package_root, audit_path),
    }


def validate_mechanical_receipt(
    value: Mapping[str, object], package_root: Path, audit_path: Path,
    *, publication_authority: object = None,
) -> dict[str, object]:
    _validate_receipt_envelope(value)
    expected = _bindings(package_root, audit_path)
    _need(value["bindings"] == expected, "current package binding drift")
    if publication_authority is not None:
        _need(
            isinstance(publication_authority, Mapping)
            and sum(item["publication_authority"] == publication_authority
                    for item in expected["items"]) == 1,
            "publication target does not match the audited package",
        )
    return dict(value)
