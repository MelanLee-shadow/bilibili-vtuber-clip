"""截图路线→CPA 重绘的显式降级契约：有回执才放行，静默改道必拒。"""

from src.autoslice.cover_route_evidence import (
    build_cover_route_decision,
    record_cover_route_execution,
    validate_cover_route_decision,
)


def _generation(selected: str) -> dict:
    route = build_cover_route_decision(
        selected_treatment=selected,
        selected_rationale="test rationale for routing",
        story_contract=None,
        reference_authority=None,
        decision_inputs={},
    )
    return {"route_decision": route}


def _demote_to_redraw(gen: dict, *, with_receipt: bool = True, detail: str | None = "demoted from screenshot_polish: SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED") -> dict:
    if with_receipt:
        gen["screenshot_direct"] = {
            "status": "BLOCKED",
            "reason_code": "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
            "detail": "ValueError: SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED",
        }
    record_cover_route_execution(
        gen,
        actual_treatment="cpa_redraw",
        execution_status="READY_DEGRADED",
        image_generation_attempted=True,
        image_generation_used=True,
        detail=detail,
    )
    gen["method"] = "images.edit"
    gen["cover_origin"] = "AI_REDRAW"
    return gen


def test_explicit_demotion_with_blocked_receipt_validates() -> None:
    gen = _demote_to_redraw(_generation("screenshot_polish"))
    assert validate_cover_route_decision(gen)


def test_demotion_from_screenshot_direct_also_validates() -> None:
    gen = _demote_to_redraw(_generation("screenshot_direct"))
    assert validate_cover_route_decision(gen)


def test_silent_reroute_without_blocked_receipt_is_rejected() -> None:
    gen = _demote_to_redraw(_generation("screenshot_polish"), with_receipt=False)
    assert not validate_cover_route_decision(gen)


def test_demotion_requires_degraded_status_not_plain_ready() -> None:
    gen = _generation("screenshot_polish")
    gen["screenshot_direct"] = {
        "status": "BLOCKED",
        "reason_code": "SCREENSHOT_ROUTE_MATERIALIZATION_FAILED",
        "detail": "x",
    }
    record_cover_route_execution(
        gen,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
        detail="demoted",
    )
    gen["method"] = "images.edit"
    gen["cover_origin"] = "AI_REDRAW"
    assert not validate_cover_route_decision(gen)


def test_demotion_requires_execution_detail() -> None:
    gen = _demote_to_redraw(_generation("screenshot_polish"), detail=None)
    assert not validate_cover_route_decision(gen)


def test_undemoted_selected_redraw_still_validates_plain_ready() -> None:
    gen = _generation("cpa_redraw")
    record_cover_route_execution(
        gen,
        actual_treatment="cpa_redraw",
        execution_status="READY",
        image_generation_attempted=True,
        image_generation_used=True,
    )
    gen["method"] = "images.edit"
    gen["cover_origin"] = "AI_REDRAW"
    assert validate_cover_route_decision(gen)
