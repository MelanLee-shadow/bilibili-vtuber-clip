from __future__ import annotations

from dataclasses import asdict, dataclass


DEFAULT_CUT_ERROR_THRESHOLD_MS = 100


@dataclass(frozen=True)
class RenderRequest:
    """Requested source-timeline bounds for a rendered candidate."""

    candidate_id: str
    requested_start_ms: int
    requested_end_ms: int


@dataclass(frozen=True)
class RenderedTimelineMetadata:
    """Actual source-timeline bounds recovered from rendered output metadata."""

    actual_start_ms: int | None = None
    actual_end_ms: int | None = None


@dataclass(frozen=True)
class RenderPtsQaResult:
    candidate_id: str
    requested_start_ms: int
    requested_end_ms: int
    actual_start_ms: int | None
    actual_end_ms: int | None
    start_error_ms: int | None
    end_error_ms: int | None
    actual_cut_error_ms: int | None
    threshold_ms: int = DEFAULT_CUT_ERROR_THRESHOLD_MS

    @property
    def metadata_available(self) -> bool:
        return self.actual_start_ms is not None and self.actual_end_ms is not None

    @property
    def passed(self) -> bool:
        return self.actual_cut_error_ms is not None and self.actual_cut_error_ms <= self.threshold_ms

    @property
    def reason_codes(self) -> tuple[str, ...]:
        if self.actual_cut_error_ms is None:
            return ("ACTUAL_CUT_METADATA_MISSING",)
        if self.actual_cut_error_ms > self.threshold_ms:
            return ("ACTUAL_CUT_ERROR_HIGH",)
        return ()

    def to_manifest_check(self) -> dict[str, object]:
        code = "ACTUAL_CUT_ERROR"
        passed = self.passed
        if self.reason_codes:
            code = self.reason_codes[0]
        evidence = asdict(self)
        evidence.pop("candidate_id")
        return {
            "code": code,
            "pass": passed,
            "severity": "PASS" if passed else "AUTO_RECUT",
            "evidence": evidence,
        }


def evaluate_render_pts(
    request: RenderRequest,
    metadata: RenderedTimelineMetadata | None,
    *,
    threshold_ms: int = DEFAULT_CUT_ERROR_THRESHOLD_MS,
) -> RenderPtsQaResult:
    """Compare requested and actual render boundaries when metadata is available.

    The remote stream-copy slicer can round requested times and keyframe-shift the
    produced clip.  This local QA contract reports the maximum absolute start/end
    boundary error in milliseconds so auto-review can fail closed before upload.
    """

    actual_start_ms = metadata.actual_start_ms if metadata is not None else None
    actual_end_ms = metadata.actual_end_ms if metadata is not None else None

    start_error_ms = None
    if actual_start_ms is not None:
        start_error_ms = abs(actual_start_ms - request.requested_start_ms)

    end_error_ms = None
    if actual_end_ms is not None:
        end_error_ms = abs(actual_end_ms - request.requested_end_ms)

    actual_cut_error_ms = None
    if start_error_ms is not None and end_error_ms is not None:
        actual_cut_error_ms = max(start_error_ms, end_error_ms)

    return RenderPtsQaResult(
        candidate_id=request.candidate_id,
        requested_start_ms=request.requested_start_ms,
        requested_end_ms=request.requested_end_ms,
        actual_start_ms=actual_start_ms,
        actual_end_ms=actual_end_ms,
        start_error_ms=start_error_ms,
        end_error_ms=end_error_ms,
        actual_cut_error_ms=actual_cut_error_ms,
        threshold_ms=threshold_ms,
    )
