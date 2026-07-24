from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice.subtitle_rendering import (  # noqa: E402
    ASS_MAX_CHARS_PER_LINE,
    ASS_MAX_VISUAL_LINES,
)
from src.autoslice.cover_generation import COVER_MIN_TALK_FONT_SIZE  # noqa: E402
from src.autoslice.cover_route_evidence import (  # noqa: E402
    validate_cover_route_decision,
    validate_rendered_text_pixel_evidence,
)
from src.autoslice.cover_text_pixel_evidence import (  # noqa: E402
    verify_pre_overlay_route_background,
    verify_rendered_text_pixel_artifacts,
)
from src.autoslice.cover_font_paths import (  # noqa: E402
    resolve_trusted_cover_font,
)
from src.autoslice.clip_context import (  # noqa: E402
    ClipContextError,
    clip_context_prompt_text,
    validate_clip_context,
)
from src.autoslice.jingting_chunker import parse_srt_cues  # noqa: E402
from src.autoslice.final_review_contract import (  # noqa: E402
    FinalReviewContractError,
    validate_final_review_release,
)
from src.autoslice.selection_scorecard import (  # noqa: E402
    SelectionCalibrationPolicyError,
    load_selected_selection_calibration_policy,
    selection_calibration_violations,
    selection_scorecard_is_valid,
)
from src.autoslice.subtitle_validation import validate_srt_file  # noqa: E402
from src.autoslice.review_package_ass_audit import (  # noqa: E402
    audit_review_package_ass,
)
from src.autoslice.review_package_boundary_contract import (  # noqa: E402
    audit_boundary_contract,
)
from src.autoslice.review_package_owner_audit import (  # noqa: E402
    audit_source_truth_owner_attestations,
)
from src.autoslice.review_package_title_audit import (  # noqa: E402
    audit_recovery_publication_surfaces,
    recovery_publication_authority_contract,
)
from src.autoslice.title_policy import (  # noqa: E402
    CHANNEL_PROFILE,
    publish_title_policy_violations,
)
from src.autoslice.story_contract import (  # noqa: E402
    SCHEMA_VERSION as STORY_CONTRACT_SCHEMA,
    audit_story_artifact,
)


DEFAULT_MAX_VISUAL_LINES = 2
DEFAULT_MAX_VISUAL_LINE_CHARS = 18
LONG_STATIC_CUE_SECONDS = 10.0
STORY_CONTRACT_ENFORCED_FROM_DATE = "2026-07-22"
AUDIT_SCHEMA_VERSION = "lidousha-review-package-audit.v2"
AUDIT_POLICY_EPOCH = "2026-07-23.final-artifact-gates.v3"
_DYNAMIC_ATTESTATION_SUFFIXES = (
    ".upload_manifest.json",
    ".uploaded.json",
    ".public_verify.json",
    ".season_verify.json",
)
_PORTABLE_ARTIFACT_SUFFIXES = (
    ".mp4",
    ".flv",
    ".mkv",
    ".mov",
    ".cover.png",
    ".cover.pre-overlay.png",
    ".cover.route-background.png",
    ".cover.title-mask.png",
    ".srt",
    ".ass",
    ".record.json",
    ".clip-context.json",
    ".subtitle-regression.json",
    ".chat-authority.json",
    ".redelivery-baseline.json",
    ".publish.json",
    ".title.txt",
    ".cover-generation.json",
    ".cover-route-evidence.json",
    ".selection-scorecard.json",
    ".story-contract.json",
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_dynamic_attestation(path: Path) -> bool:
    name = path.name
    return (
        "package-audit" in name
        or "package_audit" in name
        or any(name.endswith(suffix) for suffix in _DYNAMIC_ATTESTATION_SUFFIXES)
    )


def _manifest_referenced_inputs(root: Path) -> set[Path]:
    """Return existing in-package files explicitly named by the review manifest."""

    manifest_path = root / "review_manifest.json"
    referenced: set[Path] = set()
    if not manifest_path.is_file():
        return referenced

    try:
        payload: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return referenced

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, str) or not value.strip():
            return

        candidate = Path(value)
        candidates: list[Path] = []
        if candidate.is_absolute():
            # A portable package may retain the producer's absolute path.  Only
            # bind it when the corresponding basename is actually packaged.
            candidates.extend(root.rglob(candidate.name))
        else:
            candidates.append(root / candidate)

        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            if resolved.is_file() and not resolved.is_symlink():
                referenced.add(resolved)

    visit(payload)
    return referenced


def _audited_inputs(root: Path) -> list[dict[str, Any]]:
    """Hash the complete portable delivery contract, not operational sidecars.

    Upload manifests, ledgers, locks and uploader binaries are intentionally
    outside the review package.  Binding them would make the attestation drift
    merely because an upload was attempted.  Conversely, every manifest-
    referenced artifact and every standard deliverable sidecar is included so
    title/subtitle/cover/baseline mutations invalidate the audit.
    """

    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    resolved_root = root.resolve()
    referenced = _manifest_referenced_inputs(root)
    package_files = {
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and (
            path.name == "review_manifest.json"
            or any(path.name.endswith(suffix) for suffix in _PORTABLE_ARTIFACT_SUFFIXES)
        )
    }
    for path in sorted(referenced | package_files):
        if _is_dynamic_attestation(path):
            continue
        rows.append(
            {
                "path": path.relative_to(resolved_root).as_posix(),
                "sha256": _sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    return rows


def _audit_policy_fingerprint() -> str:
    sources = [
        Path(__file__),
        ROOT / "src/autoslice/subtitle_validation.py",
        ROOT / "src/autoslice/final_review_contract.py",
        ROOT / "src/autoslice/boundary_semantic_review.py",
        ROOT / "src/autoslice/boundary_endpoint_binding.py",
        ROOT / "src/autoslice/producer_boundary.py",
        ROOT / "src/autoslice/producer_boundary_owner_contract.py",
        ROOT / "src/autoslice/producer_boundary_resolution.py",
        ROOT / "src/autoslice/producer_boundary_review_stage.py",
        ROOT / "src/autoslice/producer_package_finalization.py",
        ROOT / "src/autoslice/source_subtitle_truth.py",
        ROOT / "src/autoslice/producer_text_finalization.py",
        ROOT / "src/autoslice/producer_text_pipeline.py",
        ROOT / "src/autoslice/review_package_boundary_contract.py",
        ROOT / "src/autoslice/review_package_owner_audit.py",
        ROOT / "src/autoslice/title_policy.py",
        ROOT / "src/autoslice/selection_scorecard.py",
        ROOT / "src/autoslice/cover_route_evidence.py",
        ROOT / "src/autoslice/cover_text_pixel_evidence.py",
        ROOT / "src/autoslice/cover_title_rendering.py",
        ROOT / "src/autoslice/cover_font_paths.py",
        ROOT / "src/autoslice/cover_generation.py",
        ROOT / "src/autoslice/cover_screenshot_poster.py",
        CHANNEL_PROFILE.asset_file("title_policy"),
        CHANNEL_PROFILE.asset_file("selection_score_calibration"),
        ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json",
    ]
    digest = hashlib.sha256()
    digest.update(AUDIT_POLICY_EPOCH.encode("utf-8"))
    for path in sources:
        try:
            label = path.relative_to(ROOT).as_posix()
        except ValueError:
            label = str(path.resolve())
        digest.update(label.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(
                f"<unreadable:{type(exc).__name__}>".encode("utf-8")
            )
    return "sha256:" + digest.hexdigest()


def _audit_result(root: Path, issues: list[dict[str, Any]]) -> dict[str, Any]:
    blocking = [issue for issue in issues if issue.get("severity") != "INFO"]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "policy_epoch": AUDIT_POLICY_EPOCH,
        "policy_fingerprint": _audit_policy_fingerprint(),
        "auditor_source_sha256": "sha256:" + _sha256_file(Path(__file__)),
        "passed": not blocking,
        "root": str(root.resolve()),
        "audited_inputs": _audited_inputs(root),
        "issues": issues,
        "issue_count": len(issues),
        "blocking_issue_count": len(blocking),
    }


def _resolve(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        if path.exists():
            return path
        # Remote/container paths in manifests are not locally reachable. Fall through to basename lookup.
        candidates = list(root.rglob(path.name))
        return candidates[0] if candidates else path
    return root / path


def _text_len(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def _parse_srt_time(value: str) -> float:
    hms, ms = value.split(",", 1)
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    text = path.read_bytes().decode("utf-8")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        start_raw, end_raw = [part.strip() for part in lines[1].split("-->", 1)]
        try:
            start = _parse_srt_time(start_raw)
            end = _parse_srt_time(end_raw)
        except Exception:
            continue
        cues.append({"index": lines[0], "start": start, "end": end, "timing": lines[1], "lines": lines[2:]})
    return cues


def _read_title_txt(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("cover_text:"):
            return stripped
    return ""


def _read_publish_title(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    data = _load_json(path)
    title = data.get("title")
    return title.strip() if isinstance(title, str) else ""


def _story_transcript(path: Path | None) -> str:
    """Use the same SRT parser/normalization as package finalization."""

    if path is None or not path.exists():
        return ""
    try:
        cues = parse_srt_cues(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return ""
    return "\n".join(cue.text.strip() for cue in cues if cue.text.strip())


def _story_contract_is_required(manifest: dict[str, Any]) -> bool:
    """Policy changes apply by package date, not by an optional producer flag."""

    if manifest.get("story_contract_required") is True:
        return True
    package_date = manifest.get("date") or manifest.get("session_date")
    return bool(
        isinstance(package_date, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", package_date)
        and package_date >= STORY_CONTRACT_ENFORCED_FROM_DATE
    )


def _compact_cover_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _audit_story_bound_cover(
    *,
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    record: dict[str, Any],
    story_contract: dict[str, Any],
    required: bool,
) -> None:
    publish_staging = record.get("publish_staging")
    if not isinstance(publish_staging, dict):
        publish_staging = {}
    generation = record.get("cover_generation")
    if not isinstance(generation, dict):
        generation = publish_staging.get("cover_generation")
    if not isinstance(generation, dict):
        generation = {}

    staged_text = publish_staging.get("cover_text")
    generation_text = generation.get("cover_text")
    rendered_lines = generation.get("rendered_lines")
    staged_text = staged_text if isinstance(staged_text, str) else ""
    generation_text = generation_text if isinstance(generation_text, str) else ""
    rendered_text = (
        "".join(str(value) for value in rendered_lines)
        if isinstance(rendered_lines, list) and rendered_lines
        else ""
    )

    if required and not staged_text:
        _add_issue(
            issues,
            "COVER_STORY_TEXT_MISSING",
            stem=stem,
            path=record_path,
            detail="publish_staging.cover_text is absent",
        )
    if required and not rendered_text:
        _add_issue(
            issues,
            "COVER_RENDERED_TEXT_EVIDENCE_MISSING",
            stem=stem,
            path=record_path,
            detail="cover_generation.rendered_lines is absent",
        )
    if staged_text and generation_text and _compact_cover_text(staged_text) != _compact_cover_text(
        generation_text
    ):
        _add_issue(
            issues,
            "COVER_TEXT_BINDING_DRIFT",
            stem=stem,
            path=record_path,
            detail=f"staged={staged_text!r}; generation={generation_text!r}",
        )
    expected_text = staged_text or generation_text
    if (
        generation.get("cover_text_mode") != "punch"
        and expected_text
        and rendered_text
        and _compact_cover_text(expected_text) != _compact_cover_text(rendered_text)
    ):
        _add_issue(
            issues,
            "COVER_RENDERED_TEXT_DRIFT",
            stem=stem,
            path=record_path,
            detail=f"expected={expected_text!r}; rendered={rendered_text!r}",
        )

    binding = generation.get("story_contract")
    expected_binding = {
        "schema_version": story_contract.get("schema_version"),
        "relation_state": story_contract.get("relation_state"),
        "participants": story_contract.get("participants"),
        "cover_counterpart_reference_available": story_contract.get(
            "cover_counterpart_reference_available"
        ),
        "cover_reference_authority": story_contract.get(
            "cover_reference_authority"
        ),
        "source_media_sha256s": story_contract.get("source_media_sha256s"),
        "clip_context_binding": story_contract.get("clip_context_binding"),
        "boundary_semantic_review": story_contract.get(
            "boundary_semantic_review"
        ),
        "human_boundary_authority": story_contract.get(
            "human_boundary_authority"
        ),
        "cover_fallback_mode": story_contract.get("cover_fallback_mode"),
    }
    if required and (
        not isinstance(binding, dict)
        or any(binding.get(key) != value for key, value in expected_binding.items())
    ):
        _add_issue(
            issues,
            "COVER_STORY_CONTRACT_BINDING_MISSING_OR_STALE",
            stem=stem,
            path=record_path,
            detail=json.dumps(
                {"expected": expected_binding, "actual": binding},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    if required and (
        not validate_cover_route_decision(
            generation, allow_legacy_v1=False
        )
    ):
        _add_issue(
            issues,
            "COVER_ROUTE_DECISION_MISSING_OR_INVALID",
            stem=stem,
            path=record_path,
        )
    if required and not validate_rendered_text_pixel_evidence(generation):
        _add_issue(
            issues,
            "COVER_RENDERED_TEXT_PIXELS_MISSING_OR_INVALID",
            stem=stem,
            path=record_path,
            detail=(
                "current packages require renderer-produced glyph bounds "
                "bound to the final cover hash"
            ),
        )

    for surface_name, surface in (
        ("cover_text", expected_text),
        ("cover_rendered_text", rendered_text),
    ):
        if not surface:
            continue
        story_audit = audit_story_artifact(
            surface,
            story_contract=story_contract,
            artifact_kind=surface_name,
        )
        for violation in story_audit.get("violations") or []:
            if isinstance(violation, dict):
                _add_issue(
                    issues,
                    str(violation.get("reason_code") or "STORY_CONTRACT_COVER_FAILED"),
                    stem=stem,
                    path=record_path,
                    detail=json.dumps(violation, ensure_ascii=False, sort_keys=True),
                )


def _contains_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ヶ]", text))


def _looks_song_like(item: dict[str, Any], evidence: dict[str, Any]) -> bool:
    if str(item.get("classification") or "").lower() == "song":
        return True
    if str(item.get("title") or "").startswith(CHANNEL_PROFILE.song_title_prefix):
        return True
    joined = " ".join(
        str(value)
        for value in [
            item.get("title", ""),
            evidence.get("title", ""),
            evidence.get("summary", ""),
            evidence.get("transcript_excerpt", ""),
            evidence.get("hook", ""),
        ]
    )
    song_markers = [
        "合唱",
        "唱歌",
        "唱着",
        "歌词",
        "词曲",
        "一首歌",
        "最后一首",
        "伴奏",
        "KTV",
        "song",
        "lyrics",
        "fly me",
    ]
    return any(marker.lower() in joined.lower() for marker in song_markers) or _contains_japanese(joined)


def _has_alignment_evidence(root: Path, item: dict[str, Any]) -> bool:
    keys = [
        "lyrics_alignment_report",
        "lyrics_alignment",
        "alignment_report",
        "lyrics_source",
        "clip_first_lyric_time",
        "clip_last_lyric_time",
        "tail_delta",
    ]
    if any(key in item for key in keys):
        return True
    stem = str(item.get("stem") or "")
    if not stem:
        return bool(list((root / "lyrics_alignment").glob("*.json"))) if (root / "lyrics_alignment").exists() else False
    candidates = list(root.glob(f"lyrics_alignment/**/{stem}*.json")) + list(root.glob(f"**/{stem}*alignment*.json"))
    return bool(candidates)


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value)
    )


def _cover_artifact_path(
    root: Path,
    item: dict[str, Any],
    generation: dict[str, Any],
    generation_key: str,
    *item_keys: str,
    fallback_when_generation_unavailable: bool = False,
) -> Path | None:
    value = generation.get(generation_key)
    generation_path = _resolve(root, value)
    if generation_path is not None and (
        generation_path.is_file()
        or not fallback_when_generation_unavailable
    ):
        return generation_path

    item_value = next(
        (item.get(key) for key in item_keys if item.get(key)), None
    )
    item_path = _resolve(root, item_value)
    return item_path if item_path is not None else generation_path


def _portable_item_artifact_path(
    root: Path,
    item: Mapping[str, Any],
    key: str,
) -> Path | None:
    """Resolve a current-package artifact without trusting host paths."""

    value = item.get(key)
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if raw.is_absolute():
        return None
    resolved_root = root.resolve()
    unresolved = root / raw
    # ``Path.resolve()`` follows the terminal symlink, after which
    # ``candidate.is_symlink()`` can no longer detect that the manifest named
    # a link.  Current review packages must carry their own regular bytes, not
    # aliases to host state.  Reject every symlink component before resolving
    # containment, including a final link whose target happens to stay inside
    # the package.
    cursor = root
    for part in raw.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _artifact_matches_sha256(path: Path | None, expected: object) -> bool:
    if path is None or not path.is_file() or not _is_sha256(expected):
        return False
    expected_hex = str(expected).removeprefix("sha256:")
    return hashlib.sha256(path.read_bytes()).hexdigest() == expected_hex


def _audit_finished_cover_evidence(
    *,
    root: Path,
    item: dict[str, Any],
    generation: dict[str, Any] | None,
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    require_rendered_pixel_artifacts: bool,
) -> None:
    """Audit the materialized route instead of treating model defaults as proof.

    Screenshot covers and CPA redraws are both finished routes.  A route label,
    ``model`` default, or ``ai_cover_generated`` boolean cannot prove that the
    selected route actually produced its required artifacts.
    """

    if not isinstance(generation, dict):
        _add_issue(
            issues,
            "AI_COVER_EVIDENCE_MISSING",
            stem=stem,
            path=record_path,
            detail="No route-aware cover generation evidence found",
        )
        return
    route_decision = generation.get("route_decision")
    treatment = (
        str(route_decision.get("selected_treatment") or "")
        if isinstance(route_decision, dict)
        else ""
    )
    final_path = _cover_artifact_path(
        root,
        item,
        generation,
        "final_cover",
        "cover",
        "cover_path",
        fallback_when_generation_unavailable=True,
    )
    final_hash = generation.get("final_cover_sha256") or item.get("cover_sha256")
    rendered_lines = generation.get("rendered_lines")
    rendered_text_ready = (
        isinstance(rendered_lines, list)
        and bool("".join(str(value) for value in rendered_lines).strip())
    )
    current_final_path: Path | None = None
    route_background_path: Path | None = None
    if require_rendered_pixel_artifacts:
        pixel_evidence = generation.get("rendered_text_pixels")
        current_final_path = _portable_item_artifact_path(
            root, item, "cover"
        )
        mask_path = _portable_item_artifact_path(
            root, item, "cover_title_mask"
        )
        pre_overlay_path = _portable_item_artifact_path(
            root, item, "cover_pre_overlay"
        )
        route_background_path = _portable_item_artifact_path(
            root, item, "cover_route_background"
        )
        trusted_font = None
        if isinstance(pixel_evidence, Mapping):
            try:
                trusted_font = resolve_trusted_cover_font(
                    file_name=str(
                        pixel_evidence.get("font_file_name") or ""
                    ),
                    expected_sha256=str(
                        pixel_evidence.get("font_file_sha256") or ""
                    ),
                    channel_profile=CHANNEL_PROFILE,
                    root=ROOT,
                )
            except RuntimeError:
                trusted_font = None
        if not (
            isinstance(pixel_evidence, Mapping)
            and current_final_path is not None
            and pre_overlay_path is not None
            and route_background_path is not None
            and mask_path is not None
            and trusted_font is not None
            and generation.get("pre_overlay_sha256")
            == pixel_evidence.get("pre_overlay_sha256")
            and verify_rendered_text_pixel_artifacts(
                pixel_evidence,
                final_cover_path=current_final_path,
                pre_overlay_path=pre_overlay_path,
                mask_path=mask_path,
                font_path=trusted_font,
                expected_pre_overlay_sha256=generation.get(
                    "pre_overlay_sha256"
                ),
            )
            and verify_pre_overlay_route_background(
                route_background_path=route_background_path,
                pre_overlay_path=pre_overlay_path,
                expected_route_background_sha256=generation.get(
                    "ai_background_sha256"
                ),
                text_backing=generation.get("text_backing"),
                scrim=generation.get("scrim"),
            )
        ):
            _add_issue(
                issues,
                "COVER_RENDERED_TEXT_PIXEL_ARTIFACT_MISMATCH",
                stem=stem,
                path=record_path,
                detail=(
                    "package-internal final cover/pre-overlay/mask and the "
                    "committed font cannot independently replay the exact "
                    "title layer and final composition"
                ),
            )
        # Every later route check in a current package must use the same
        # package-internal bytes.  Existing staging paths are provenance
        # strings, not portable audit authority.
        final_path = current_final_path

    if treatment in {"screenshot_direct", "screenshot_polish"}:
        method = str(generation.get("method") or "")
        degraded_polish = (
            treatment == "screenshot_polish"
            and method == "screenshot_direct"
            and isinstance(generation.get("screenshot_polish"), dict)
            and generation["screenshot_polish"].get("status")
            == "DEGRADED_TO_DIRECT"
        )
        valid_method = method == treatment or degraded_polish
        screenshot_frame = generation.get("screenshot_frame")
        reference_selection = generation.get("reference_selection")
        frame_ms = (
            screenshot_frame.get("frame_ms")
            if isinstance(screenshot_frame, Mapping)
            else None
        )
        best_ms = (
            reference_selection.get("best_ms")
            if isinstance(reference_selection, Mapping)
            else None
        )
        frame_binding_valid = bool(
            isinstance(frame_ms, int)
            and not isinstance(frame_ms, bool)
            and frame_ms >= 0
            and isinstance(best_ms, int)
            and not isinstance(best_ms, bool)
            and best_ms >= 0
            and frame_ms == best_ms
        )
        reference_ready = bool(generation.get("reference_image")) and _is_sha256(
            generation.get("reference_sha256")
        )
        route_ready = (
            validate_cover_route_decision(
                generation, allow_legacy_v1=True
            )
        )
        if not (
            valid_method
            and frame_binding_valid
            and route_ready
            and reference_ready
            and rendered_text_ready
            and _artifact_matches_sha256(final_path, final_hash)
        ):
            _add_issue(
                issues,
                "SCREENSHOT_COVER_EVIDENCE_MISSING",
                stem=stem,
                path=record_path,
                detail=(
                    "screenshot route requires route decision, materialized "
                    "method, exact reference/frame timing, reference/final "
                    "hashes, and rendered title text"
                ),
            )
        return

    ai_background_path = (
        route_background_path
        if require_rendered_pixel_artifacts
        else _cover_artifact_path(
            root,
            item,
            generation,
            "ai_background",
            "source_ai_background",
            "ai_background",
            "ai_cover",
        )
    )
    ai_background_hash = generation.get("ai_background_sha256") or item.get(
        "ai_background_sha256"
    )
    attempted_models = generation.get("attempted_models")
    model = str(generation.get("model") or "")
    actual_ai_ready = (
        _artifact_matches_sha256(ai_background_path, ai_background_hash)
        and _artifact_matches_sha256(final_path, final_hash)
        and isinstance(attempted_models, list)
        and bool(attempted_models)
        and model not in {"", "none"}
        and model in {str(value) for value in attempted_models}
    )
    if treatment == "cpa_redraw":
        route_ready = (
            validate_cover_route_decision(
                generation, allow_legacy_v1=True
            )
        )
        if not (route_ready and rendered_text_ready and actual_ai_ready):
            _add_issue(
                issues,
                "CPA_COVER_EVIDENCE_MISSING",
                stem=stem,
                path=record_path,
                detail=(
                    "CPA route requires route decision, actual hashed AI/final "
                    "artifacts, attempted/selected model evidence, and rendered text"
                ),
            )
        return

    # Legacy, non-story-contract packages have no route decision.  Keep them
    # auditable only when real hashed AI bytes and model-attempt evidence exist;
    # method/model defaults or a boolean flag alone are deliberately insufficient.
    if not actual_ai_ready:
        _add_issue(
            issues,
            "AI_COVER_EVIDENCE_MISSING",
            stem=stem,
            path=record_path,
            detail="No materialized hashed AI background/final cover and model-attempt evidence",
        )


def _add_issue(issues: list[dict[str, Any]], code: str, *, stem: str = "", path: Path | None = None, detail: str = "", severity: str = "BLOCK") -> None:
    issue: dict[str, Any] = {"code": code, "severity": severity}
    if stem:
        issue["stem"] = stem
    if path is not None:
        issue["path"] = str(path)
    if detail:
        issue["detail"] = detail
    issues.append(issue)


def _subtitle_visual_contract(
    manifest: dict[str, Any],
    issues: list[dict[str, Any]],
    manifest_path: Path,
) -> tuple[int, int]:
    """Resolve a package-bound display contract without permitting a looser
    limit than the canonical Sapphire renderer can actually guarantee.

    Historical/manual packages keep the stricter 18-character default.  The
    autoslice Sapphire72 lane renders at the current 28-character contract and
    must declare that profile explicitly in its review manifest; otherwise a
    perfectly valid burn is falsely rejected by the older package default.
    """

    raw = manifest.get("subtitle_visual_contract")
    if raw is None:
        return DEFAULT_MAX_VISUAL_LINES, DEFAULT_MAX_VISUAL_LINE_CHARS
    if not isinstance(raw, dict):
        _add_issue(
            issues,
            "SUBTITLE_VISUAL_CONTRACT_INVALID",
            path=manifest_path,
            detail="subtitle_visual_contract must be an object",
        )
        return DEFAULT_MAX_VISUAL_LINES, DEFAULT_MAX_VISUAL_LINE_CHARS
    max_lines = raw.get("max_visual_lines")
    max_chars = raw.get("max_chars_per_line")
    if (
        isinstance(max_lines, bool)
        or not isinstance(max_lines, int)
        or not 1 <= max_lines <= ASS_MAX_VISUAL_LINES
        or isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= ASS_MAX_CHARS_PER_LINE
    ):
        _add_issue(
            issues,
            "SUBTITLE_VISUAL_CONTRACT_INVALID",
            path=manifest_path,
            detail=(
                f"requested lines/chars={max_lines!r}/{max_chars!r}; "
                f"supported maxima are {ASS_MAX_VISUAL_LINES}/{ASS_MAX_CHARS_PER_LINE}"
            ),
        )
        return DEFAULT_MAX_VISUAL_LINES, DEFAULT_MAX_VISUAL_LINE_CHARS
    return max_lines, max_chars


def _audit_item_story_contract(
    *,
    root: Path,
    manifest: dict[str, Any],
    item: dict[str, Any],
    issues: list[dict[str, Any]],
    stem: str,
    subtitle_path: Path | None,
    chat_authority: dict[str, Any],
    publish_path: Path | None,
    title_txt_path: Path | None,
    publish_title: str,
    title_txt: str,
    record_path: Path | None,
    record: dict[str, Any],
    story_contract: object,
    story_contract_required: bool,
    is_song: bool,
) -> None:
    """Audit all story/context/boundary bindings for one manifest item."""

    if story_contract_required and not record:
        _add_issue(
            issues,
            "STORY_CONTRACT_RECORD_MISSING",
            stem=stem,
            path=record_path,
        )
    if story_contract_required and not isinstance(story_contract, dict):
        _add_issue(
            issues,
            "STORY_CONTRACT_MISSING",
            stem=stem,
            path=record_path,
        )
    if not isinstance(story_contract, dict):
        return
    if story_contract.get("schema_version") != STORY_CONTRACT_SCHEMA:
        _add_issue(
            issues,
            "STORY_CONTRACT_SCHEMA_STALE",
            stem=stem,
            path=record_path,
            detail=str(story_contract.get("schema_version")),
        )
    clip_binding = story_contract.get("clip_context_binding")
    clip_context_path = _resolve(
        root,
        item.get("clip_context_json")
        or item.get("clip_context")
        or record.get("clip_context_path"),
    )
    if story_contract_required and not isinstance(clip_binding, dict):
        _add_issue(
            issues,
            "CLIP_CONTEXT_BINDING_MISSING",
            stem=stem,
            path=record_path,
        )
    elif isinstance(clip_binding, dict):
        if clip_context_path is None or not clip_context_path.is_file():
            _add_issue(
                issues,
                "CLIP_CONTEXT_FILE_MISSING",
                stem=stem,
                path=clip_context_path,
            )
        else:
            clip_context = _load_json(clip_context_path)
            try:
                validate_clip_context(
                    clip_context,
                    candidate_id=str(story_contract.get("candidate_id") or ""),
                    recording_date=str(manifest.get("date") or ""),
                    source_media_sha256s=[
                        str(value)
                        for value in (
                            story_contract.get("source_media_sha256s") or []
                        )
                    ],
                )
            except ClipContextError as exc:
                _add_issue(
                    issues,
                    str(exc),
                    stem=stem,
                    path=clip_context_path,
                )
            else:
                expected_prompt = clip_context_prompt_text(clip_context)
                if (
                    story_contract.get("clip_context_prompt")
                    != expected_prompt
                ):
                    _add_issue(
                        issues,
                        "CLIP_CONTEXT_PROMPT_BINDING_DRIFT",
                        stem=stem,
                        path=clip_context_path,
                    )
            file_sha256 = "sha256:" + hashlib.sha256(
                clip_context_path.read_bytes()
            ).hexdigest()
            artifact_hashes = record.get("artifact_hashes") or {}
            if clip_context.get("context_sha256") != clip_binding.get(
                "context_sha256"
            ) or record.get("clip_context_payload_sha256") != clip_binding.get(
                "context_sha256"
            ):
                _add_issue(
                    issues,
                    "CLIP_CONTEXT_PAYLOAD_BINDING_DRIFT",
                    stem=stem,
                    path=clip_context_path,
                )
            if (
                not isinstance(artifact_hashes, dict)
                or artifact_hashes.get("clip_context_file_sha256")
                != file_sha256
            ):
                _add_issue(
                    issues,
                    "CLIP_CONTEXT_FILE_HASH_DRIFT",
                    stem=stem,
                    path=clip_context_path,
                )
    transcript = _story_transcript(subtitle_path)
    subtitle_story_audit = audit_story_artifact(
        transcript,
        story_contract=story_contract,
        artifact_kind="subtitle",
    )
    if transcript and subtitle_story_audit.get("text_sha256") != story_contract.get(
        "transcript_sha256"
    ):
        _add_issue(
            issues,
            "STORY_CONTRACT_SUBTITLE_HASH_DRIFT",
            stem=stem,
            path=subtitle_path,
            detail=(
                f"contract={story_contract.get('transcript_sha256')}; "
                f"current={subtitle_story_audit.get('text_sha256')}"
            ),
        )
    for violation in subtitle_story_audit.get("violations") or []:
        if isinstance(violation, dict):
            _add_issue(
                issues,
                str(violation.get("reason_code") or "STORY_CONTRACT_SUBTITLE_FAILED"),
                stem=stem,
                path=subtitle_path,
                detail=json.dumps(violation, ensure_ascii=False, sort_keys=True),
            )
    artifact_title = publish_title or title_txt or str(item.get("title") or "")
    title_story_audit = audit_story_artifact(
        artifact_title,
        story_contract=story_contract,
        artifact_kind="title",
    )
    for violation in title_story_audit.get("violations") or []:
        if isinstance(violation, dict):
            _add_issue(
                issues,
                str(violation.get("reason_code") or "STORY_CONTRACT_TITLE_FAILED"),
                stem=stem,
                path=publish_path or title_txt_path,
                detail=json.dumps(violation, ensure_ascii=False, sort_keys=True),
            )
    _audit_story_bound_cover(
        issues=issues,
        stem=stem,
        record_path=record_path,
        record=record,
        story_contract=story_contract,
        required=story_contract_required,
    )
    audit_boundary_contract(
        issue_adder=_add_issue,
        issues=issues,
        stem=stem,
        record_path=record_path,
        subtitle_path=subtitle_path,
        exact_final_review=chat_authority.get("final_review_audit"),
        record=record,
        story_contract=story_contract,
        required=story_contract_required,
        is_song=is_song,
    )
    scorecard = story_contract.get("selection_scorecard")
    if story_contract_required and not is_song and (
        not selection_scorecard_is_valid(scorecard)
    ):
        _add_issue(
            issues,
            "SELECTION_SCORECARD_MISSING_OR_STALE",
            stem=stem,
            path=record_path,
        )
    if story_contract_required and not is_song:
        for code in selection_calibration_violations(
            str(story_contract.get("candidate_id") or ""),
            scorecard,
        ):
            _add_issue(
                issues,
                code,
                stem=stem,
                path=record_path,
                detail=f"effective_score={(scorecard or {}).get('effective_score') if isinstance(scorecard, dict) else None}",
            )


def _audit_source_truth_owner_attestations(
    *,
    issues: list[dict[str, Any]],
    stem: str,
    chat_authority_path: Path | None,
    chat_authority: dict[str, Any],
    record_path: Path | None,
    record: dict[str, Any],
) -> None:
    audit_source_truth_owner_attestations(
        issue_adder=_add_issue,
        issues=issues,
        stem=stem,
        chat_authority_path=chat_authority_path,
        chat_authority=chat_authority,
        record_path=record_path,
        record=record,
    )

def _audit_final_review_attestation(
    *,
    issues: list[dict[str, Any]],
    stem: str,
    subtitle_path: Path | None,
    chat_authority_path: Path | None,
    chat_authority: dict[str, Any],
    record_path: Path | None,
    record: dict[str, Any],
) -> None:
    if (
        subtitle_path is None
        or not subtitle_path.is_file()
        or chat_authority_path is None
        or not chat_authority_path.is_file()
        or not chat_authority
    ):
        return
    actual_chat_sha256 = "sha256:" + _sha256_file(chat_authority_path)
    artifact_hashes = record.get("artifact_hashes")
    declared_chat_sha256 = (
        artifact_hashes.get("chat_authority_audit_sha256")
        if isinstance(artifact_hashes, dict)
        else None
    )
    if declared_chat_sha256 != actual_chat_sha256:
        _add_issue(
            issues,
            "CHAT_AUTHORITY_RECORD_HASH_MISMATCH",
            stem=stem,
            path=record_path or chat_authority_path,
            detail=(
                f"record={declared_chat_sha256!r}; "
                f"actual={actual_chat_sha256}"
            ),
        )
    subtitle_bytes = subtitle_path.read_bytes()
    expected_srt_sha256 = (
        "sha256:" + hashlib.sha256(subtitle_bytes).hexdigest()
    )
    declared_srt_sha256 = (
        artifact_hashes.get("subtitle_sha256")
        if isinstance(artifact_hashes, dict)
        else None
    )
    if not _is_sha256(declared_srt_sha256):
        _add_issue(
            issues,
            "SUBTITLE_RECORD_HASH_MISSING_OR_INVALID",
            stem=stem,
            path=record_path or subtitle_path,
        )
    elif declared_srt_sha256 != expected_srt_sha256:
        _add_issue(
            issues,
            "SUBTITLE_RECORD_HASH_MISMATCH",
            stem=stem,
            path=record_path or subtitle_path,
            detail=(
                f"record={declared_srt_sha256!r}; "
                f"actual={expected_srt_sha256}"
            ),
        )
    try:
        validate_final_review_release(
            chat_authority.get("final_review_audit"),
            expected_srt_sha256=expected_srt_sha256,
        )
    except FinalReviewContractError as exc:
        _add_issue(
            issues,
            exc.reason_code,
            stem=stem,
            path=chat_authority_path,
        )


def _audit_finished_item_cover(
    *,
    root: Path,
    item: dict[str, Any],
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    record: dict[str, Any],
    publish_staging: dict[str, Any],
    cover_generation: object,
    is_song: bool,
    story_contract_required: bool,
    story_candidate_id: str,
    item_candidate_id: str,
    attestations_by_candidate: dict[str, dict[str, Any]],
    manifest_path: Path,
) -> None:
    record_generation = record.get("cover_generation")
    if not isinstance(record_generation, dict):
        record_generation = publish_staging.get("cover_generation")
    finished_generation = (
        record_generation
        if isinstance(record_generation, dict)
        else cover_generation
    )
    if not is_song and isinstance(finished_generation, dict):
        font_size = finished_generation.get("font_size")
        if (
            isinstance(font_size, bool)
            or (
                isinstance(font_size, (int, float))
                and font_size < COVER_MIN_TALK_FONT_SIZE
            )
            or (
                story_contract_required
                and not isinstance(font_size, (int, float))
            )
        ):
            _add_issue(
                issues,
                "COVER_TITLE_TOO_SMALL",
                stem=stem,
                path=record_path,
                detail=(
                    f"talk cover emphasis is {font_size}px; minimum is "
                    f"{COVER_MIN_TALK_FONT_SIZE}px"
                ),
            )
    cover_generation_text = (
        json.dumps(finished_generation, ensure_ascii=False)
        if isinstance(finished_generation, dict)
        else str(cover_generation or "")
    )
    if isinstance(finished_generation, dict):
        fallback_cover = (
            bool(item.get("cover_regenerated_from_burn_frame"))
            or finished_generation.get("fallback_used") is True
        )
        attestation_candidate_id = story_candidate_id or item_candidate_id
        if story_contract_required and attestation_candidate_id:
            attestation = attestations_by_candidate.get(
                attestation_candidate_id
            )
            if not isinstance(attestation, dict):
                _add_issue(
                    issues,
                    "MANIFEST_COVER_ATTESTATION_MISSING",
                    stem=stem,
                    path=manifest_path,
                    detail=f"candidate_id={attestation_candidate_id}",
                )
            else:
                expected_attestation = {
                    "reference_sha256": finished_generation.get(
                        "reference_sha256"
                    ),
                    "final_cover_sha256": finished_generation.get(
                        "final_cover_sha256"
                    ),
                    "method": finished_generation.get("method"),
                    "route_decision": finished_generation.get(
                        "route_decision"
                    ),
                    "reference_authority": finished_generation.get(
                        "reference_authority"
                    ),
                }
                for key, expected in expected_attestation.items():
                    if attestation.get(key) != expected:
                        _add_issue(
                            issues,
                            "MANIFEST_COVER_ATTESTATION_DRIFT",
                            stem=stem,
                            path=manifest_path,
                            detail=(
                                f"candidate_id={attestation_candidate_id}; "
                                f"field={key}"
                            ),
                        )
    else:
        fallback_cover = bool(
            item.get("cover_regenerated_from_burn_frame")
        ) or any(
            marker in cover_generation_text.lower()
            for marker in [
                "deterministic",
                "burn-frame",
                "burned-frame",
                "fallback",
                "frame cover",
            ]
        )
    if fallback_cover:
        _add_issue(
            issues,
            "COVER_FALLBACK_NOT_FINISHED",
            stem=stem,
            detail=cover_generation_text,
        )
    _audit_finished_cover_evidence(
        root=root,
        item=item,
        generation=(
            finished_generation
            if isinstance(finished_generation, dict)
            else None
        ),
        issues=issues,
        stem=stem,
        record_path=record_path,
        require_rendered_pixel_artifacts=story_contract_required,
    )


def _cover_attestations_by_candidate(
    *,
    manifest: dict[str, Any],
    items: list[Any],
    recovery_publication_authorities: dict[str, Any],
    publication_contract_required: bool,
    story_contract_required: bool,
    manifest_path: Path,
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    cover_attestations = manifest.get("cover_route_attestations")
    indexed: dict[str, dict[str, Any]] = {}
    if isinstance(cover_attestations, list):
        for attestation in cover_attestations:
            if not isinstance(attestation, dict):
                _add_issue(
                    issues,
                    "MANIFEST_COVER_ATTESTATION_ID_INVALID",
                    path=manifest_path,
                    detail="attestation is not an object",
                )
                continue
            candidate_id = str(attestation.get("candidate_id") or "")
            if not candidate_id or candidate_id in indexed:
                _add_issue(
                    issues,
                    "MANIFEST_COVER_ATTESTATION_ID_INVALID",
                    path=manifest_path,
                    detail=f"candidate_id={candidate_id!r}",
                )
                continue
            indexed[candidate_id] = attestation

    if not (story_contract_required or publication_contract_required):
        return indexed
    expected = (
        set(recovery_publication_authorities)
        if publication_contract_required
        else {
            str(item.get("candidate_id") or "")
            for item in items
            if isinstance(item, dict)
            and str(item.get("candidate_id") or "")
        }
    )
    if not isinstance(cover_attestations, list):
        _add_issue(
            issues,
            "MANIFEST_COVER_ATTESTATIONS_MISSING",
            path=manifest_path,
        )
    if set(indexed) != expected:
        _add_issue(
            issues,
            "MANIFEST_COVER_ATTESTATION_SET_MISMATCH",
            path=manifest_path,
            detail=(
                f"attestations={sorted(indexed)}; "
                f"expected={sorted(expected)}"
            ),
        )
    return indexed


def audit_package(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    manifest_path = root / "review_manifest.json"
    manifest = _load_json(manifest_path)
    issues: list[dict[str, Any]] = []

    try:
        load_selected_selection_calibration_policy()
    except SelectionCalibrationPolicyError as exc:
        _add_issue(
            issues,
            "SELECTION_CALIBRATION_POLICY_INVALID",
            path=exc.path,
            detail=f"{exc.reason_code}: {exc.detail}",
        )
        if not manifest:
            _add_issue(issues, "MANIFEST_MISSING_OR_INVALID", path=manifest_path)
        return _audit_result(root, issues)

    if not manifest:
        _add_issue(issues, "MANIFEST_MISSING_OR_INVALID", path=manifest_path)
        return _audit_result(root, issues)

    max_visual_lines, max_visual_line_chars = _subtitle_visual_contract(
        manifest, issues, manifest_path
    )

    status = str(manifest.get("status") or "")
    if status.startswith("invalid_review_draft"):
        _add_issue(issues, "PACKAGE_MARKED_INVALID_REVIEW_DRAFT", detail=f"manifest.status is {status}")

    invalid_marker = root / "INVALID_REDO_REQUIRED.json"
    if invalid_marker.exists():
        marker = _load_json(invalid_marker)
        _add_issue(
            issues,
            "PACKAGE_MARKED_INVALID_REDO_REQUIRED",
            path=invalid_marker,
            detail=str(marker.get("reason") or marker.get("status") or "package explicitly invalidated"),
        )

    items = manifest.get("items")
    if not isinstance(items, list):
        _add_issue(issues, "MANIFEST_ITEMS_MISSING", path=manifest_path)
        items = []
    (
        recovery_publication_authorities,
        publication_contract_required,
        publication_contract_issues,
    ) = recovery_publication_authority_contract(
        manifest,
        items,
    )
    for authority_issue in publication_contract_issues:
        _add_issue(issues, authority_issue.code, path=manifest_path, detail=authority_issue.detail)

    story_contract_required = _story_contract_is_required(manifest)
    if story_contract_required:
        if manifest.get("upload_allowed") is not False:
            _add_issue(
                issues,
                "REVIEW_PACKAGE_UPLOAD_POLICY_INVALID",
                path=manifest_path,
                detail="story-contract review packages must bind upload_allowed=false",
            )
        if str(manifest.get("run_mode") or "") not in {
            "RECOVERY_REVIEW",
            "PRODUCTION_REVIEW",
        }:
            _add_issue(
                issues,
                "REVIEW_PACKAGE_RUN_MODE_INVALID",
                path=manifest_path,
                detail=f"run_mode={manifest.get('run_mode')!r}",
            )

    attestations_by_candidate = _cover_attestations_by_candidate(
        manifest=manifest,
        items=items,
        recovery_publication_authorities=(
            recovery_publication_authorities
        ),
        publication_contract_required=publication_contract_required,
        story_contract_required=story_contract_required,
        manifest_path=manifest_path,
        issues=issues,
    )

    for item in items:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or item.get("id") or item.get("title") or "")
        subtitle_path = _resolve(root, item.get("subtitle_srt") or item.get("subtitle"))
        evidence_path = _resolve(root, item.get("evidence_json") or item.get("evidence"))
        publish_path = _resolve(root, item.get("publish_json") or item.get("publish"))
        title_txt_path = _resolve(root, item.get("title_txt") or item.get("title_path"))
        evidence = _load_json(evidence_path) if evidence_path else {}

        is_song = _looks_song_like(item, evidence)
        if is_song:
            source_srt = str(item.get("source_srt") or "")
            if source_srt.endswith(".jingting.srt"):
                _add_issue(issues, "SONG_USES_JINGTING_SRT", stem=stem, detail=source_srt)
            if not _has_alignment_evidence(root, item):
                _add_issue(issues, "SONG_LYRIC_SOURCE_MISSING", stem=stem, detail="No external timed lyric source evidence found")
                _add_issue(issues, "SONG_ALIGNMENT_REPORT_MISSING", stem=stem, detail="No first/last lyric anchor offset/tail report found")
        if subtitle_path and subtitle_path.exists():
            if story_contract_required:
                srt_release = validate_srt_file(subtitle_path)
                for error in srt_release.get("errors") or []:
                    if not isinstance(error, dict):
                        continue
                    _add_issue(
                        issues,
                        str(error.get("code") or "SRT_RELEASE_VALIDATION_FAILED"),
                        stem=stem,
                        path=subtitle_path,
                        detail=json.dumps(error, ensure_ascii=False, sort_keys=True),
                    )
            for cue in _parse_srt(subtitle_path):
                duration = cue["end"] - cue["start"]
                lines = [line.strip() for line in cue["lines"] if line.strip()]
                if len(lines) > max_visual_lines:
                    _add_issue(issues, "SUBTITLE_CUE_TOO_MANY_LINES", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} has {len(lines)} lines")
                for line in lines:
                    n = _text_len(line)
                    if n > max_visual_line_chars:
                        _add_issue(issues, "SUBTITLE_LINE_TOO_LONG", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} line length {n}: {line}")
                if duration >= LONG_STATIC_CUE_SECONDS and lines:
                    _add_issue(issues, "SUBTITLE_LONG_STATIC_CUE", stem=stem, path=subtitle_path, detail=f"cue {cue['index']} lasts {duration:.2f}s")

        title_txt = _read_title_txt(title_txt_path)
        publish_title = _read_publish_title(publish_path)
        if title_txt and publish_title and title_txt != publish_title:
            _add_issue(issues, "PUBLISH_TITLE_TXT_MISMATCH", stem=stem, detail=f"title_txt={title_txt!r}; publish.title={publish_title!r}")

        cover_generation = item.get("cover_generation")
        record_path = _resolve(root, item.get("record") or item.get("record_json"))
        record = _load_json(record_path) if record_path else {}
        chat_authority_path = _resolve(root, item.get("chat_authority"))
        chat_authority = (
            _load_json(chat_authority_path) if chat_authority_path else {}
        )
        ass_audit = audit_review_package_ass(
            root=root,
            item=item,
            portable_required=story_contract_required and not is_song,
            max_visual_lines=max_visual_lines,
            max_visual_line_chars=max_visual_line_chars,
            record=record,
            chat_authority=chat_authority,
        )
        for ass_issue in ass_audit.issues:
            _add_issue(
                issues,
                ass_issue.code,
                stem=stem,
                path=ass_issue.path,
                detail=ass_issue.detail,
            )
        if story_contract_required and (
            chat_authority_path is None
            or not chat_authority_path.is_file()
            or not chat_authority
        ):
            _add_issue(
                issues,
                "CHAT_AUTHORITY_AUDIT_MISSING_OR_INVALID",
                stem=stem,
                path=chat_authority_path or manifest_path,
            )
        story_contract = record.get("story_contract")
        item_title = str(item.get("title") or "")
        final_publish_title = publish_title or title_txt or item_title
        if story_contract_required or is_song:
            for code in publish_title_policy_violations(
                final_publish_title,
                lane="song" if is_song else "talk",
            ):
                _add_issue(
                    issues,
                    code.upper(),
                    stem=stem,
                    path=publish_path or title_txt_path or manifest_path,
                    detail=final_publish_title,
                )
        publish_staging = (
            record.get("publish_staging")
            if isinstance(record.get("publish_staging"), dict)
            else {}
        )
        record_title = str(publish_staging.get("title") or "")
        if item_title and record_title and item_title != record_title:
            _add_issue(
                issues,
                "MANIFEST_ITEM_TITLE_DRIFT",
                stem=stem,
                path=manifest_path,
                detail=f"manifest={item_title!r}; record={record_title!r}",
            )
        if story_contract_required and not is_song:
            _audit_source_truth_owner_attestations(
                issues=issues,
                stem=stem,
                chat_authority_path=chat_authority_path,
                chat_authority=chat_authority,
                record_path=record_path,
                record=record,
            )
            _audit_final_review_attestation(
                issues=issues,
                stem=stem,
                subtitle_path=subtitle_path,
                chat_authority_path=chat_authority_path,
                chat_authority=chat_authority,
                record_path=record_path,
                record=record,
            )
        story_candidate_id = (
            str(story_contract.get("candidate_id") or "")
            if isinstance(story_contract, dict)
            else ""
        )
        item_candidate_id = str(item.get("candidate_id") or "")
        for authority_issue in audit_recovery_publication_surfaces(
            item=item,
            item_candidate_id=item_candidate_id,
            item_title=item_title,
            publish_path=publish_path,
            record_path=record_path,
            record=record,
            publish_staging=publish_staging,
            expected_authority=recovery_publication_authorities.get(
                item_candidate_id
            ),
            required=publication_contract_required,
        ):
            _add_issue(
                issues,
                authority_issue.code,
                stem=stem,
                path=authority_issue.path,
                detail=authority_issue.detail,
            )
        if (
            item_candidate_id
            and story_candidate_id
            and item_candidate_id != story_candidate_id
        ):
            _add_issue(
                issues,
                "MANIFEST_ITEM_CANDIDATE_ID_DRIFT",
                stem=stem,
                path=manifest_path,
                detail=(
                    f"manifest={item_candidate_id!r}; "
                    f"record_story_contract={story_candidate_id!r}"
                ),
            )
        _audit_item_story_contract(
            root=root,
            manifest=manifest,
            item=item,
            issues=issues,
            stem=stem,
            subtitle_path=subtitle_path,
            chat_authority=chat_authority,
            publish_path=publish_path,
            title_txt_path=title_txt_path,
            publish_title=publish_title,
            title_txt=title_txt,
            record_path=record_path,
            record=record,
            story_contract=story_contract,
            story_contract_required=story_contract_required,
            is_song=is_song,
        )
        _audit_finished_item_cover(
            root=root,
            item=item,
            issues=issues,
            stem=stem,
            record_path=record_path,
            record=record,
            publish_staging=publish_staging,
            cover_generation=cover_generation,
            is_song=is_song,
            story_contract_required=story_contract_required,
            story_candidate_id=story_candidate_id,
            item_candidate_id=item_candidate_id,
            attestations_by_candidate=attestations_by_candidate,
            manifest_path=manifest_path,
        )

    return _audit_result(root, issues)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a Li Dousha finished/review package for subtitle, title, and cover gates.")
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = audit_package(args.package_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"passed={result['passed']} blocking={result['blocking_issue_count']} issues={result['issue_count']}")
        for issue in result["issues"]:
            print(f"{issue.get('severity','BLOCK')} {issue['code']} {issue.get('stem','')} {issue.get('detail','')}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
