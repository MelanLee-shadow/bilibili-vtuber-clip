"""Strict v6 text-occlusion admission plus bounded pixel-guided title reflow.

Pixel quietness only proposes a zone.  The existing cover renderer remains the
authority for art direction, copy, fonts, line limits, rotation and glyph proof.
"""

from __future__ import annotations

import copy
import json
import math
import re
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageFilter

from src.autoslice.cover_title_rendering import FEED_SAFE_X0, FEED_SAFE_X1, CoverTitleRenderError, sha256_file


SCHEMA_VERSION = "lidousha-cover-title-reflow-proposal.v1"
V6_HOST_IDENTITY_SCHEMA = "lidousha-cover-final-host-identity-verification.v6"
V6_HOST_IDENTITY_AUTHORITY = (
    "CPA_PRIMARY_HASH_BOUND_SOURCE_FINAL_IDENTITY_PROMINENCE_AND_HOST_ONLY_FEATURE_COMPARISON"
)
TEXT_OCCLUSION_REASON_CODE = "COVER_TITLE_REFLOW_TEXT_OCCLUSION_ONLY"
REQUIRED_REASON_CODE = "FINAL_COVER_SUBJECT_PROMINENCE_FAILED"
FRAME_WITNESS_SCHEMA = "cpa-frame-witness.v1"
DEFAULT_SAFE_ZONE = (FEED_SAFE_X0, 0, FEED_SAFE_X1, 1080)
DEFAULT_QUIET_SCORE_MAX = 0.46
DEFAULT_GRID_STEP = 64
DEFAULT_QUIET_MARGIN = 24
_FEATURE_CATEGORIES = ("clothing", "head_appearance", "accessories")
_PROVIDER_HASH = re.compile(r"^[0-9a-f]{64}$")


class CoverTitleReflowError(ValueError):
    """Fail-closed error with a stable recovery reason code."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = str(reason_code)
        self.detail = str(detail or reason_code)
        super().__init__(f"{self.reason_code}: {self.detail}")


def _rejection(
    reason_code: str,
    detail: str,
    *,
    evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "REJECTED",
        "admitted": False,
        "reason_code": reason_code,
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def _unwrap_verification(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    direct = value.get("final_host_identity_verification")
    if isinstance(direct, Mapping):
        return direct
    generation = value.get("generation")
    if isinstance(generation, Mapping):
        nested = generation.get("final_host_identity_verification")
        if isinstance(nested, Mapping):
            return nested
    if "schema_version" in value and "verdict" in value:
        return value
    return None


def _text_occlusion_description(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.casefold()
    # A mixed failure is not evidence that the background can be retained.
    # Ambiguous wording stays on the ordinary review/retry path.
    if any(term in text for term in (
        "主体太小", "人物太小", "脸太小", "模糊", "出框", "裁切", "裁掉",
        "too small", "cropped", "blurry", "blurred", "wrong identity",
    )):
        return False
    title = any(
        token in text
        for token in (
            "标题", "文字", "字层", "字卡", "title", "text", "glyph",
            "caption", "rendered",
        )
    )
    occlusion = any(
        token in text
        for token in (
            "遮", "盖", "覆盖", "压住", "挡住", "挡", "occlu", "covers", "covered", "covering",
            "overlap", "obstruct", "blocked",
        )
    )
    return title and occlusion


def _provider_route_observed(verification: Mapping[str, object]) -> bool:
    witness = verification.get("witness")
    if not isinstance(witness, Mapping):
        return False
    if witness.get("status") != "OBSERVED" or witness.get("schema_version") != FRAME_WITNESS_SCHEMA:
        return False
    provider = witness.get("provider")
    routing = witness.get("routing")
    image_sha = witness.get("image_sha256")
    comparison_sha = verification.get("comparison_sha256")
    bound = (
        isinstance(image_sha, str)
        and _PROVIDER_HASH.fullmatch(image_sha) is not None
        and isinstance(comparison_sha, str)
        and comparison_sha == "sha256:" + image_sha
    )
    if provider == "cpa":
        return bound and (
            not isinstance(routing, Mapping)
            or routing.get("preferred_provider") in (None, "cpa")
        ) and (
            not isinstance(routing, Mapping)
            or routing.get("selected_provider") in (None, "cpa")
        )
    if provider != "agy" or not isinstance(routing, Mapping):
        return False
    primary = routing.get("primary_receipt")
    return bool(
        routing.get("preferred_provider") == "cpa"
        and routing.get("fallback_used") is True
        and isinstance(primary, Mapping)
        and primary.get("provider") == "cpa"
        and bound
    )


def classify_text_occlusion_admission(value: object) -> dict[str, object]:
    """Return a strict admission record for the v6 text-occlusion lane.

    The caller owns receipt, asset and old-final hash verification.  This
    classifier binds the precise witness answer and rejects identity,
    participant, background, small-host and unknown-provider failures.
    """

    verification = _unwrap_verification(value)
    if verification is None:
        return _rejection(
            "COVER_TITLE_REFLOW_VERIFICATION_MISSING",
            "a v6 final_host_identity_verification row is required",
        )
    evidence = {
        "schema_version": verification.get("schema_version"),
        "status": verification.get("status"),
        "reason_code": verification.get("reason_code"),
    }
    checks = (
        (verification.get("schema_version") == V6_HOST_IDENTITY_SCHEMA, "COVER_TITLE_REFLOW_V6_WITNESS_REQUIRED", "only the v6 final witness is eligible"),
        (verification.get("authority") == V6_HOST_IDENTITY_AUTHORITY, "COVER_TITLE_REFLOW_WITNESS_AUTHORITY_INVALID", "the v6 authority is required"),
        (verification.get("status") == "FAIL", "COVER_TITLE_REFLOW_WITNESS_NOT_FAILED", "reflow requires an observed failed final cover"),
        (verification.get("host_only_required") is True, "COVER_TITLE_REFLOW_PARTICIPANT_SCOPE_MISSING", "host-only scope is required"),
        (verification.get("reason_code") == REQUIRED_REASON_CODE, "COVER_TITLE_REFLOW_REASON_CODE_NOT_PROMINENCE", "the failure is outside the prominence lane"),
        (_provider_route_observed(verification), "COVER_TITLE_REFLOW_PROVIDER_UNAVAILABLE", "a bound observed CPA route is required"),
    )
    for passed, reason, detail in checks:
        if not passed:
            return _rejection(reason, detail, evidence=evidence)

    verdict = verification.get("verdict")
    witness = verification.get("witness")
    if not isinstance(verdict, Mapping):
        return _rejection("COVER_TITLE_REFLOW_VERDICT_MISSING", "the precise verdict is required", evidence=evidence)
    if not isinstance(witness, Mapping) or not isinstance(witness.get("answer"), str):
        return _rejection("COVER_TITLE_REFLOW_RAW_VERDICT_MISSING", "the raw witness answer is required", evidence=evidence)
    try:
        raw_verdict = json.loads(witness["answer"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _rejection("COVER_TITLE_REFLOW_RAW_VERDICT_UNPARSEABLE", f"the raw witness answer is not JSON: {exc}", evidence=evidence)
    if not isinstance(raw_verdict, Mapping) or raw_verdict != verdict:
        return _rejection("COVER_TITLE_REFLOW_VERDICT_BINDING_MISMATCH", "raw witness answer JSON differs from verification.verdict", evidence=evidence)
    # Reuse the production v6 shape validator so an otherwise plausible
    # hand-written verdict cannot pass with omitted/empty feature evidence.
    from src.autoslice.cover_host_identity_gate import _identity_answer_valid

    if not _identity_answer_valid(
        witness["answer"],
        scene_kind="talk",
        host_only_required=True,
        feature_scope_required=True,
    ):
        return _rejection(
            "COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED",
            "the production host-only v6 feature shape validator rejected the witness",
            evidence=evidence,
        )

    required = {
        "source_lidousha_located": True,
        "primary_subject_is_lidousha": True,
        "primary_subject_matches_other_source_participant": False,
        "primary_subject_is_visually_dominant": True,
        "primary_subject_carries_story_reaction": True,
        "excessive_dead_space": False,
        "meaningless_dominant_decoration": False,
        "thumbnail_has_clear_click_hook": True,
        "primary_subject_face_is_large_and_clear": False,
    }
    for field, expected in required.items():
        if verdict.get(field) is not expected:
            reason = (
                "COVER_TITLE_REFLOW_FACE_FAILURE_REQUIRED"
                if field == "primary_subject_face_is_large_and_clear"
                else "COVER_TITLE_REFLOW_SEMANTIC_AXIS_FAILED"
            )
            return _rejection(reason, f"verdict.{field} must be {expected!r}", evidence={**evidence, "field": field, "actual": verdict.get(field)})
    if verdict.get("identity_conflicts") != [] or verdict.get("final_feature_conflicts") != []:
        return _rejection("COVER_TITLE_REFLOW_IDENTITY_CONFLICT", "identity conflicts must be empty", evidence=evidence)
    if (
        verdict.get("other_recognizable_people_or_avatars_visible") is not False
        or verdict.get("other_recognizable_people_or_avatars") != []
    ):
        return _rejection("COVER_TITLE_REFLOW_OTHER_PARTICIPANT_VISIBLE", "no other recognizable participant may be visible", evidence=evidence)

    matches = verdict.get("final_feature_matches")
    observations = verdict.get("source_visible_feature_observations")
    comparisons = verdict.get("final_feature_comparisons")
    if (
        not isinstance(matches, Mapping)
        or set(matches) != set(_FEATURE_CATEGORIES)
        or any(matches.get(category) is not True for category in _FEATURE_CATEGORIES)
        or not isinstance(observations, Mapping)
        or set(observations) != set(_FEATURE_CATEGORIES)
        or any(not isinstance(observations.get(category), list) for category in _FEATURE_CATEGORIES)
        or not isinstance(comparisons, Mapping)
        or set(comparisons) != set(_FEATURE_CATEGORIES)
    ):
        return _rejection("COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED", "all v6 feature categories must compare cleanly", evidence=evidence)
    for category in _FEATURE_CATEGORIES:
        rows = comparisons.get(category)
        if not isinstance(rows, list):
            return _rejection("COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED", f"comparison rows missing for {category}", evidence=evidence)
        for row in rows:
            if not isinstance(row, Mapping) or row.get("status") not in {"MATCH", "OCCLUDED"}:
                return _rejection("COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED", f"comparison row failed for {category}", evidence=evidence)
            if row.get("status") == "OCCLUDED" and not _text_occlusion_description(row.get("final_observation")):
                return _rejection("COVER_TITLE_REFLOW_FEATURE_COMPARISON_FAILED", f"occlusion for {category} is not title-specific", evidence=evidence)
    conflicts = verdict.get("composition_conflicts")
    if (
        not isinstance(conflicts, list)
        or not conflicts
        or any(not _text_occlusion_description(item) for item in conflicts)
    ):
        return _rejection("COVER_TITLE_REFLOW_NON_TEXT_COMPOSITION_FAILURE", "composition conflicts must explicitly cite title occlusion", evidence=evidence)
    if not _text_occlusion_description(verdict.get("reason")):
        return _rejection("COVER_TITLE_REFLOW_NON_TEXT_COMPOSITION_FAILURE", "the verdict reason must explicitly cite title occlusion", evidence=evidence)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ADMITTED",
        "admitted": True,
        "reason_code": TEXT_OCCLUSION_REASON_CODE,
        "detail": "v6 identity and story axes pass; the sole face failure is rendered-title occlusion",
        "evidence": {
            **evidence,
            "semantic_axes": {
                "identity": "PASS",
                "participants": "PASS",
                "story_reaction": "PASS",
                "dominance": "PASS",
                "face_large_clear": "FAIL",
            },
            "text_occlusion_conflicts": copy.deepcopy(conflicts),
            "identity_semantics": "WITNESS_ONLY; PIXEL_SEARCH_DOES_NOT_CERTIFY_IDENTITY",
        },
    }


def require_text_occlusion_admission(value: object) -> dict[str, object]:
    result = classify_text_occlusion_admission(value)
    if result.get("admitted") is not True:
        raise CoverTitleReflowError(
            str(result.get("reason_code") or "COVER_TITLE_REFLOW_ADMISSION_REJECTED"),
            str(result.get("detail") or "text-occlusion admission rejected"),
        )
    return result


admit_text_occlusion_reflow = classify_text_occlusion_admission


def title_only_occlusion(verification: object) -> bool:
    """Boolean seam for callers that already verify the old bound assets."""

    return classify_text_occlusion_admission(verification).get("admitted") is True


class _TexturePixels:
    """Integral-image texture and border-colour measurements from real pixels."""

    def __init__(self, image: Image.Image) -> None:
        scale = 8
        self.scale = scale
        resample = getattr(getattr(Image, "Resampling", None), "BILINEAR", Image.BILINEAR)
        sample = image.resize(
            (max(1, image.width // scale), max(1, image.height // scale)),
            resample=resample,
        ).convert("RGB")
        edges = image.filter(ImageFilter.FIND_EDGES).convert("L").resize(sample.size, resample=resample)
        self.width, self.height = sample.size
        luma: list[float] = []
        chroma: list[float] = []
        edge_values: list[float] = []
        for rgb, edge in zip(sample.getdata(), edges.getdata()):
            luma.append(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2])
            chroma.append(float(max(rgb) - min(rgb)))
            edge_values.append(float(edge))
        self._luma = self._integral(luma)
        self._luma_sq = self._integral([value * value for value in luma])
        self._chroma = self._integral(chroma)
        self._edges = self._integral(edge_values)
        border_width = max(1, self.width // 10)
        border = [
            index
            for y in range(self.height)
            for x in range(self.width)
            if x < border_width
            or x >= self.width - border_width
            or y < border_width
            or y >= self.height - border_width
            for index in [y * self.width + x]
        ]
        self.border_luma = sum(luma[i] for i in border) / max(1, len(border))
        self.border_chroma = sum(chroma[i] for i in border) / max(1, len(border))

    def _integral(self, values: Sequence[float]) -> list[float]:
        stride = self.width + 1
        result = [0.0] * ((self.height + 1) * stride)
        for y in range(self.height):
            running = 0.0
            row = (y + 1) * stride
            previous = y * stride
            for x in range(self.width):
                running += values[y * self.width + x]
                result[row + x + 1] = result[previous + x + 1] + running
        return result

    def _mean(self, integral: Sequence[float], box: tuple[int, int, int, int]) -> float:
        x0, y0, x1, y1 = box
        x0 = max(0, min(self.width - 1, x0 // self.scale))
        y0 = max(0, min(self.height - 1, y0 // self.scale))
        x1 = max(x0 + 1, min(self.width, math.ceil(x1 / self.scale)))
        y1 = max(y0 + 1, min(self.height, math.ceil(y1 / self.scale)))
        stride = self.width + 1
        total = (
            integral[y1 * stride + x1]
            - integral[y0 * stride + x1]
            - integral[y1 * stride + x0]
            + integral[y0 * stride + x0]
        )
        return total / max(1, (x1 - x0) * (y1 - y0))

    def measure(self, box: tuple[int, int, int, int]) -> dict[str, float]:
        mean = self._mean(self._luma, box)
        variance = max(0.0, self._mean(self._luma_sq, box) - mean * mean)
        std = math.sqrt(variance)
        chroma = self._mean(self._chroma, box)
        edge = self._mean(self._edges, box)
        colour_distance = (
            abs(mean - self.border_luma) / 255.0
            + abs(chroma - self.border_chroma) / 255.0
        ) / 2.0
        score = min(
            1.0,
            edge / 255.0 + std / 255.0 + chroma / 1020.0 + 0.20 * colour_distance,
        )
        return {
            "texture_score": float(score),
            "edge_mean": float(edge),
            "luma_mean": float(mean),
            "luma_std": float(std),
            "chroma_mean": float(chroma),
            "border_colour_distance": float(colour_distance),
        }


def _candidate_zones(
    safe: tuple[int, int, int, int],
    *,
    step: int = DEFAULT_GRID_STEP,
    margin: int = DEFAULT_QUIET_MARGIN,
) -> list[tuple[int, int, int, int]]:
    x0, y0, x1, y1 = safe
    widths = (720, 800, 960, 1120, 1280)
    heights = (300, 360, 440, 560)
    zones: list[tuple[int, int, int, int]] = []
    for width in widths:
        for height in heights:
            if width + 2 * margin > x1 - x0 or height + 2 * margin > y1 - y0:
                continue
            max_x, max_y = x1 - width, y1 - height
            xs = list(range(x0 + margin, max_x - margin + 1, max(1, step)))
            ys = list(range(y0 + margin, max_y - margin + 1, max(1, step)))
            if max_x - margin not in xs:
                xs.append(max_x - margin)
            if max_y - margin not in ys:
                ys.append(max_y - margin)
            zones.extend((x, y, x + width, y + height) for y in ys for x in xs)
    return zones


def _render_candidate(
    background: Path,
    output: Path,
    *,
    cover_text: str,
    art_direction: object,
    full_text_cover_contract: object,
    zone: tuple[int, int, int, int],
) -> dict[str, object]:
    # The import and call below are the only title renderer in this module.
    from src.autoslice.cover_generation import _overlay_cover_title

    return _overlay_cover_title(
        background,
        output,
        cover_text=cover_text,
        art_direction=art_direction,
        full_text_cover_contract=full_text_cover_contract,
        source_title_zone=zone,
    )


def render_title_reflow(
    background: Path,
    final: Path,
    *,
    cover_text: str,
    art_direction: object,
    full_text_cover_contract: object = None,
) -> dict[str, object]:
    """Render a title-only successor into a measured quiet background zone.

    ``art_direction`` is passed by identity to the existing renderer.  The
    caller must run :func:`title_only_occlusion` and hash-bind the old assets;
    this function does not manufacture an admission.
    """

    background = Path(background)
    final = Path(final)
    if not background.is_file():
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_BACKGROUND_UNAVAILABLE",
            f"background file is missing: {background}",
        )
    if final.resolve() == background.resolve():
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_SOURCE_OVERWRITE_FORBIDDEN",
            "final cannot overwrite the source background",
        )
    if not isinstance(cover_text, str) or not cover_text.strip():
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_TEXT_CONTRACT_INVALID",
            "cover_text is required",
        )
    if not hasattr(art_direction, "layout") or not hasattr(art_direction, "cover_punch"):
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_ART_DIRECTION_INVALID",
            "the reviewed art direction object is required",
        )
    try:
        with Image.open(background) as opened:
            opened.load()
            if opened.size != (1920, 1080):
                raise CoverTitleReflowError(
                    "COVER_TITLE_REFLOW_BACKGROUND_CANVAS_INVALID",
                    f"expected 1920x1080, got {opened.size}",
                )
            image = opened.convert("RGB").copy()
    except CoverTitleReflowError:
        raise
    except (OSError, Image.DecompressionBombError) as exc:
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_BACKGROUND_UNAVAILABLE",
            str(exc),
        ) from exc

    source_sha = sha256_file(background)
    pixels = _TexturePixels(image)
    zones = _candidate_zones(DEFAULT_SAFE_ZONE)
    measured = []
    for zone in zones:
        metrics = pixels.measure(zone)
        if metrics["texture_score"] <= DEFAULT_QUIET_SCORE_MAX:
            measured.append({"zone": list(zone), "metrics": metrics})
    measured.sort(
        key=lambda row: (
            float(row["metrics"]["texture_score"]),
            row["zone"][1],
            row["zone"][0],
            -((row["zone"][2] - row["zone"][0]) * (row["zone"][3] - row["zone"][1])),
        )
    )
    if not measured:
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_NO_SAFE_FITTING_ZONE",
            "no pixel-quiet measured region is available",
        )

    final.parent.mkdir(parents=True, exist_ok=True)
    accepted: list[tuple[dict[str, object], dict[str, object]]] = []
    attempts: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="cover-title-reflow-") as scratch_name:
        scratch = Path(scratch_name)
        for index, candidate in enumerate(measured[:16]):
            zone = tuple(int(value) for value in candidate["zone"])
            path = scratch / f"candidate-{index}.png"
            try:
                rendered = _render_candidate(
                    background,
                    path,
                    cover_text=cover_text,
                    art_direction=art_direction,
                    full_text_cover_contract=full_text_cover_contract,
                    zone=zone,
                )
                font_size = rendered.get("font_size")
                if not isinstance(font_size, int) or (
                    not getattr(art_direction, "is_song", False) and font_size < 120
                ):
                    raise CoverTitleReflowError(
                        "COVER_TITLE_TOO_SMALL",
                        "ordinary renderer did not meet the talk minimum font gate",
                    )
                pixels_evidence = rendered.get("rendered_text_pixels")
                if not isinstance(pixels_evidence, Mapping) or pixels_evidence.get("status") != "PASS":
                    raise CoverTitleReflowError(
                        "COVER_TITLE_REFLOW_RENDER_EVIDENCE_FAILED",
                        "ordinary renderer did not emit PASS glyph evidence",
                    )
                attempts.append(
                    {
                        **copy.deepcopy(candidate),
                        "status": "ACCEPTED",
                        "font_size": font_size,
                        "rendered_lines": copy.deepcopy(rendered.get("rendered_lines")),
                    }
                )
                accepted.append((candidate, rendered))
                break
            except (CoverTitleReflowError, CoverTitleRenderError, OSError, RuntimeError, TypeError, ValueError) as exc:
                attempts.append(
                    {
                        **copy.deepcopy(candidate),
                        "status": "REJECTED",
                        "reason_code": getattr(exc, "reason_code", type(exc).__name__),
                        "detail": str(exc)[:240],
                    }
                )
    if not accepted:
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_NO_SAFE_FITTING_ZONE",
            "measured quiet regions could not satisfy the ordinary renderer gates",
        )

    selected, _probe = accepted[0]
    selected_zone = tuple(int(value) for value in selected["zone"])
    rendered = _render_candidate(
        background,
        final,
        cover_text=cover_text,
        art_direction=art_direction,
        full_text_cover_contract=full_text_cover_contract,
        zone=selected_zone,
    )
    if sha256_file(background) != source_sha:
        raise CoverTitleReflowError(
            "COVER_TITLE_REFLOW_BACKGROUND_PIXELS_CHANGED",
            "source background changed during rendering",
        )
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "status": "PROPOSED",
        "measured_zone": list(selected_zone),
        "selected_zone": list(selected_zone),
        "safe_zone": list(DEFAULT_SAFE_ZONE),
        "selected_metrics": copy.deepcopy(selected["metrics"]),
        "candidate_evidence": attempts,
        "candidate_count": len(zones),
        "measured_candidate_count": len(measured),
        "accepted_candidate_count": len(accepted),
        "background_sha256": source_sha,
        "pre_overlay_sha256": rendered.get("pre_overlay_sha256"),
        "background_bytes_preserved": sha256_file(background) == source_sha,
        "identity_semantics": "PIXEL_TEXTURE_AND_BORDER_COLOUR_ONLY; DOES_NOT_CERTIFY_FACE_OR_IDENTITY",
        "art_direction_preserved_by_identity": True,
        "full_text_cover_contract_forwarded": full_text_cover_contract is not None,
        "font_and_rotation_authority": "src.autoslice.cover_generation._overlay_cover_title",
    }
    rendered["title_reflow_proposal"] = proposal
    return rendered


__all__ = [
    "SCHEMA_VERSION",
    "TEXT_OCCLUSION_REASON_CODE",
    "CoverTitleReflowError",
    "classify_text_occlusion_admission",
    "require_text_occlusion_admission",
    "admit_text_occlusion_reflow",
    "title_only_occlusion",
    "render_title_reflow",
]
