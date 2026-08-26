"""Build the lane-owned after-image for a private reviewed replay."""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.autoslice.reviewed_baseline_replay_projection import exact_candidate_sidecar_target


def build_replay_after_image(
    plan: object,
    *,
    runtime_root: Path,
    state_path: Path,
    finalization: object,
    package: object,
    projection: object,
):
    """Seal the lane-owned live target map for a fully audited private replay.

    This deliberately has no ``target`` or state JSON caller input.  The
    canonical producer handle declares delivery roles; the deployed profile
    declares delivery root; and the current record package declares the
    candidate package root.  The transaction receives only their resulting
    exact map plus one state-last after image.
    """

    from src.autoslice.reviewed_baseline_replay import (
        ReviewedBaselineReplayError,
        _load_json,
        _prepared_package_names,
        _replay_package_root,
        _safe_directory,
        project_replay_state_after,
        regular_binding,
    )
    from src.autoslice.producer_delivery_transaction import deployment_authority_binding
    from src.autoslice.reviewed_baseline_replay_transaction import (
        ReplayAfterImage,
        _remove_owned_stage,
        stage_lane_after_image_artifacts,
    )

    runtime = _safe_directory(runtime_root)
    # All fallible authority/state reads precede private transaction staging.
    # If any of these drift, no transaction-owned media namespace is created.
    deployed = deployment_authority_binding(runtime)
    try:
        private_deployed = deployment_authority_binding(finalization.private_runtime_root)
    except Exception as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_DEPLOYED_AUTHORITY_INVALID") from exc
    if private_deployed != deployed:
        # The private finalizer copied its seal before potentially long
        # provider work.  Never let an old-code package inherit a newer live
        # seal merely because the deployment changed while it was preparing.
        raise ReviewedBaselineReplayError("REPLAY_DEPLOYED_AUTHORITY_DRIFT")
    record_before = regular_binding(plan.record_path, label="RECORD").sha256
    stage_binding = regular_binding(package.package_audit.path, label="PACKAGE_AUDIT")
    state = project_replay_state_after(
        plan, runtime_root=runtime, state_path=state_path,
        finalization=finalization, projection=projection, package=package,
    )
    delivery = state.delivered
    sources: dict[str, Path] = {}
    targets: dict[str, Path] = {}

    projected_sources = {
        "record": projection.record.path,
        "publish": projection.publish.path,
        "chat_authority": projection.chat.path,
        "speaker_manifest": projection.speaker_manifest.path,
    }
    for role, row in delivery.items():
        source = projected_sources.get(role, Path(row["source"]))
        sources[f"delivery-{role}"] = source
        targets[f"delivery-{role}"] = Path(row["target"])

    # The candidate package contains the finalizer's ordinary package files
    # plus the manual manifest/audit.  Replace the four private JSON sources
    # with the projected bytes before they become a live consumer surface.
    names = _prepared_package_names(plan, finalization=finalization)
    if not {"record", "publish", "chat_authority", "speaker_manifest", "clip_context"}.issubset(names):
        raise ReviewedBaselineReplayError("REPLAY_PREPARED_ARTIFACT_INCOMPLETE")
    package_overrides = {
        names["record"]: projection.record.path,
        names["publish"]: projection.publish.path,
        names["chat_authority"]: projection.chat.path,
        names["speaker_manifest"]: projection.speaker_manifest.path,
    }
    package_files = sorted(
        path for path in package.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    if not package_files:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_EMPTY")
    package_relatives: set[str] = set()
    for index, source in enumerate(package_files):
        binding = regular_binding(source, label="PRIVATE_PACKAGE_ARTIFACT")
        relative = source.relative_to(package.root)
        relative_key = relative.as_posix()
        package_relatives.add(relative_key)
        role = f"package-{index:03d}-{hashlib.sha256(relative_key.encode()).hexdigest()[:16]}"
        if role in sources:
            raise ReviewedBaselineReplayError("REPLAY_AFTER_IMAGE_ROLE_COLLISION")
        sources[role] = package_overrides.get(relative_key, binding.path)
        targets[role] = _replay_package_root(plan) / relative
    # A private package which omitted one projected authority document cannot
    # be audited as the exact live after-image.
    if not set(package_overrides).issubset(package_relatives):
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_PACKAGE_INCOMPLETE")
    # These two record locators live beside the candidate package, not in the
    # flattened ``replacement_recuts`` directory.  Install their exact
    # projected copies as first-class targets rather than leaving a record to
    # point at a stale/private sidecar.
    record_document = _load_json(projection.record, label="PROJECTED_RECORD")
    chat_target = record_document.get("chat_authority_audit_path")
    clip_target = record_document.get("clip_context_path")
    if not isinstance(chat_target, str) or not isinstance(clip_target, str):
        raise ReviewedBaselineReplayError("REPLAY_RECORD_MIRROR_BINDING_INVALID")
    for role, source, target in (
        ("candidate-chat-authority", projection.chat.path, Path(chat_target)),
        ("candidate-clip-context", Path(delivery["clip_context"]["source"]), Path(clip_target)),
    ):
        exact_candidate_sidecar_target(
            plan.candidate_id, plan.package_root, role=role, target=target,
            error=ReviewedBaselineReplayError,
        )
        if target in targets.values():
            raise ReviewedBaselineReplayError("REPLAY_AFTER_IMAGE_TARGET_COLLISION")
        sources[role] = source
        targets[role] = target
    # The projected document may reference only already-existing deployed repo
    # authority or an exact target in this after-image.  A private finalizer
    # path hidden in a forgotten sidecar would otherwise survive until after
    # the private workspace is cleaned.
    from src.autoslice.package_relocation_contract import (
        CHAT_PATH_POINTERS,
        PUBLISH_PATH_POINTERS,
        RECORD_PATH_POINTERS,
        SPEAKER_PATH_POINTERS,
        get_value,
    )
    target_values = {str(path) for path in targets.values()}
    for document, pointers in (
        (record_document, RECORD_PATH_POINTERS),
        (_load_json(projection.publish, label="PROJECTED_PUBLISH"), PUBLISH_PATH_POINTERS),
        (_load_json(projection.chat, label="PROJECTED_CHAT"), CHAT_PATH_POINTERS),
        (_load_json(projection.speaker_manifest, label="PROJECTED_SPEAKER"), SPEAKER_PATH_POINTERS),
    ):
        for pointer in pointers:
            value = get_value(document, pointer)
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            path = Path(value)
            if path.is_relative_to(_replay_package_root(plan)) or path.is_relative_to(plan.package_root):
                if str(path) not in target_values:
                    raise ReviewedBaselineReplayError("REPLAY_PROJECTED_LOCATOR_TARGET_MISSING")
    artifacts = stage_lane_after_image_artifacts(
        runtime_root=runtime, date=plan.date, candidate_id=plan.candidate_id,
        sources=sources, targets=targets,
    )
    try:
        return ReplayAfterImage(
            date=plan.date, candidate_id=plan.candidate_id,
            deployed=deployed, state_path=Path(state_path),
            state_before=state.before, state_after=state.after,
            record_before_sha256=record_before, stage_sha256=stage_binding.sha256,
            artifacts=artifacts, upload_allowed=False,
        )
    except BaseException:
        # The transaction stage has no journal until the commit lease.  Do not
        # leave a copied media namespace behind if a late lane-owned assembly
        # check/constructor fails after the copy primitive returned.
        _remove_owned_stage(artifacts[0].staged.path.parent, runtime_root=runtime)
        raise
