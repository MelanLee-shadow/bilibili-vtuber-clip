"""Shared canonical-stage predicates for Qixi diagnostic observation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from src.autoslice.qixi_post_correction_diagnostics import (
    PredicateResult,
    PredicateStatus,
)


STAGE_PREDICATE_IDS = (
    "stage_return_shape",
    "stage_publish_json_shape",
    "stage_manual_title_consumed",
    "stage_upload_disabled",
    "stage_source_fact_pass",
    "stage_media_locator",
    "stage_raw_source_fact_mirrors",
    "stage_raw_story_source_fact_mirrors",
    "stage_raw_cover_generation_mirrors",
    "stage_projected_source_fact_mirrors",
    "stage_projected_cover_generation_mirrors",
    "stage_locator_projection",
    "stage_materialized_locator_manifest",
    "stage_state_projection",
    "stage_materialized_target_manifest",
)

STAGE_DEPENDENCIES = {
    "stage_return_shape": (),
    "stage_publish_json_shape": (),
    "stage_manual_title_consumed": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_upload_disabled": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_source_fact_pass": ("stage_return_shape",),
    "stage_media_locator": ("stage_publish_json_shape",),
    "stage_raw_source_fact_mirrors": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_raw_story_source_fact_mirrors": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_raw_cover_generation_mirrors": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_projected_source_fact_mirrors": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_projected_cover_generation_mirrors": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_locator_projection": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_materialized_locator_manifest": ("stage_locator_projection",),
    "stage_state_projection": ("stage_return_shape", "stage_publish_json_shape"),
    "stage_materialized_target_manifest": ("stage_locator_projection", "stage_state_projection"),
}


@dataclass(slots=True)
class StageBuildObservation:
    """Non-authoritative outcomes captured before formal stage admission."""

    results: dict[str, PredicateResult] = field(default_factory=dict)

    def evaluate(self, predicate_id: str, check: Callable[[], None], *, error_type: type[Exception]) -> None:
        if predicate_id not in STAGE_PREDICATE_IDS:
            raise ValueError("unknown Qixi stage predicate")
        if any(
            self.results.get(dependency, PredicateResult(dependency, PredicateStatus.NOT_EVALUATED, "")).status
            is not PredicateStatus.PASS
            for dependency in STAGE_DEPENDENCIES[predicate_id]
        ):
            self.results[predicate_id] = PredicateResult(
                predicate_id,
                PredicateStatus.NOT_EVALUATED,
                "DEPENDENT_INPUT_UNAVAILABLE",
            )
            return
        try:
            check()
        except error_type:
            self.results[predicate_id] = PredicateResult(
                predicate_id, PredicateStatus.FAIL, "PREDICATE_REJECTED"
            )
        except Exception:
            self.results[predicate_id] = PredicateResult(
                predicate_id, PredicateStatus.FAIL, "UNEXPECTED_EXCEPTION"
            )
        else:
            self.results[predicate_id] = PredicateResult(
                predicate_id, PredicateStatus.PASS, "SATISFIED"
            )

    def unavailable(self) -> list[PredicateResult]:
        return [
            self.results.get(
                predicate_id,
                PredicateResult(
                    predicate_id,
                    PredicateStatus.NOT_EVALUATED,
                    "DEPENDENT_INPUT_UNAVAILABLE",
                ),
            )
            for predicate_id in STAGE_PREDICATE_IDS
        ]


def formal_gate(
    observation: StageBuildObservation | None,
    predicate_id: str,
    check: Callable[[], None],
    *,
    error_type: type[Exception],
) -> None:
    """Observe one shared callable, then preserve formal first-error behavior."""

    if observation is not None:
        observation.evaluate(predicate_id, check, error_type=error_type)
    check()
