"""Build the package-audit policy fingerprint from its sealed inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def build_policy_fingerprint(
    *,
    root: Path,
    entrypoint: Path,
    channel_profile: Any,
    policy_epoch: str,
) -> str:
    """Hash the exact source and policy assets consumed by package auditing."""

    sources = [
        entrypoint,
        root / "src/autoslice/review_package_policy_fingerprint.py",
        root / "src/autoslice/subtitle_validation.py",
        root / "src/autoslice/final_review_contract.py",
        root / "src/autoslice/boundary_semantic_review.py",
        root / "src/autoslice/boundary_endpoint_binding.py",
        root / "src/autoslice/redelivery_boundary_projection.py",
        root / "src/autoslice/redelivery_subtitle_baseline.py",
        root / "src/autoslice/producer_boundary.py",
        root / "src/autoslice/producer_boundary_owner_contract.py",
        root / "src/autoslice/producer_boundary_resolution.py",
        root / "src/autoslice/producer_boundary_review_stage.py",
        root / "src/autoslice/producer_source_boundary_review.py",
        root / "src/autoslice/reviewed_exact_source_interval.py",
        root / "src/autoslice/producer_package_finalization.py",
        root / "src/autoslice/source_subtitle_truth.py",
        root / "src/autoslice/producer_text_finalization.py",
        root / "src/autoslice/expected_value_canon_supersession.py",
        root / "src/autoslice/producer_text_pipeline.py",
        root / "src/autoslice/pronoun_consistency.py",
        root / "src/autoslice/review_package_boundary_contract.py",
        root / "src/autoslice/review_package_boundary_validators.py",
        root / "src/autoslice/review_package_owner_audit.py",
        root / "src/autoslice/review_package_owner_audit_c12_supersession.py",
        root / "src/autoslice/review_package_portable_evidence.py",
        root / "src/autoslice/candidate_entity_projection.py",
        root / "src/autoslice/candidate_public_text_surface_authority.py",
        root / "src/autoslice/reviewed_subtitle_baseline_registry.py",
        root / "src/autoslice/title_policy.py",
        root / "src/autoslice/selection_scorecard.py",
        root / "src/autoslice/addressee_attribution.py",
        root / "src/autoslice/manual_title_keep_authority.py",
        root / "src/autoslice/deterministic_text_surface_resolution.py",
        root / "src/autoslice/publication_title_exception.py",
        root / "src/autoslice/review_package_title_audit.py",
        root / "src/autoslice/fastlane_c1_formal_adapter.py",
        root / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.subtitle-correction.v1.json",
        root / "assets/lidousha/fastlane_c1_private/auto_173005_934_1166.formal-adapter.v1.json",
        root / "src/autoslice/review_package_source_fact_audit.py",
        root / "src/autoslice/source_fact_review.py",
        root / "src/autoslice/cover_only_audit_scope.py",
        root / "src/autoslice/cover_route_evidence.py",
        root / "src/autoslice/cover_punch_semantics.py",
        root / "src/autoslice/cover_text_pixel_evidence.py",
        root / "src/autoslice/cover_title_rendering.py",
        root / "src/autoslice/cover_font_paths.py",
        root / "src/autoslice/cover_generation.py",
        root / "src/autoslice/cover_screenshot_poster.py",
        channel_profile.asset_file("title_policy"),
        channel_profile.asset_file("selection_score_calibration"),
        channel_profile.asset_file("subtitle_truth_ledger"),
    ]
    digest = hashlib.sha256()
    digest.update(policy_epoch.encode("utf-8"))
    for path in sources:
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = str(path.resolve())
        digest.update(label.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"<unreadable:{type(exc).__name__}>".encode("utf-8"))
    return "sha256:" + digest.hexdigest()
