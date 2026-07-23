from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

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
)
from src.autoslice.clip_context import (  # noqa: E402
    ClipContextError,
    validate_clip_context,
)
from src.autoslice.jingting_chunker import parse_srt_cues  # noqa: E402
from src.autoslice.selection_scorecard import (  # noqa: E402
    selection_scorecard_is_valid,
)
from src.autoslice.story_contract import (  # noqa: E402
    SCHEMA_VERSION as STORY_CONTRACT_SCHEMA,
    audit_story_artifact,
)


DEFAULT_MAX_VISUAL_LINES = 2
DEFAULT_MAX_VISUAL_LINE_CHARS = 18
LONG_STATIC_CUE_SECONDS = 10.0
STORY_CONTRACT_ENFORCED_FROM_DATE = "2026-07-22"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


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
    text = path.read_text(encoding="utf-8")
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


def _ass_dialogue_texts(path: Path) -> list[str]:
    if not path.exists():
        return []
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) == 10:
            texts.append(parts[9])
    return texts


def _visual_lines(ass_text: str) -> list[str]:
    # ASS manual line breaks are \N. Some test fixtures may contain an escaped double backslash.
    return [part for part in re.split(r"\\+N", ass_text) if part != ""]


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
        cues = parse_srt_cues(path.read_text(encoding="utf-8"))
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
            generation, allow_legacy_v1=True
        )
    ):
        _add_issue(
            issues,
            "COVER_ROUTE_DECISION_MISSING_OR_INVALID",
            stem=stem,
            path=record_path,
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


def _audit_boundary_contract(
    *,
    issues: list[dict[str, Any]],
    stem: str,
    record_path: Path | None,
    record: dict[str, Any],
    story_contract: dict[str, Any],
    required: bool,
    is_song: bool,
) -> None:
    if not required or is_song:
        return
    audit = record.get("boundary_audit")
    if not isinstance(audit, dict):
        _add_issue(issues, "BOUNDARY_AUDIT_MISSING", stem=stem, path=record_path)
        return
    human_authority = str(story_contract.get("human_boundary_authority") or "").strip()
    if human_authority:
        if (
            audit.get("boundary_authority") != "human_source_reviewed_end"
            or str(audit.get("manual_end_authority") or "").strip()
            != human_authority
        ):
            _add_issue(
                issues,
                "HUMAN_BOUNDARY_AUTHORITY_DRIFT",
                stem=stem,
                path=record_path,
            )
        return
    review = story_contract.get("boundary_semantic_review")
    audit_review = audit.get("boundary_semantic_review")
    if not isinstance(review, dict) or review.get("status") != "PASS":
        _add_issue(
            issues,
            "BOUNDARY_SEMANTIC_REVIEW_NOT_PASS",
            stem=stem,
            path=record_path,
        )
        return
    if audit.get("boundary_authority") != "multi_witness_semantic_review":
        _add_issue(
            issues,
            "BOUNDARY_MULTI_WITNESS_AUTHORITY_MISSING",
            stem=stem,
            path=record_path,
        )
    if audit_review != review:
        _add_issue(
            issues,
            "BOUNDARY_SEMANTIC_REVIEW_BINDING_DRIFT",
            stem=stem,
            path=record_path,
        )
    if review.get("recommended_end_ms") != audit.get("snapped_sentence_end_ms"):
        _add_issue(
            issues,
            "BOUNDARY_RECOMMENDED_END_NOT_MATERIALIZED",
            stem=stem,
            path=record_path,
        )


def _contains_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ヶ]", text))


def _looks_song_like(item: dict[str, Any], evidence: dict[str, Any]) -> bool:
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
                    "method, reference/final hashes, and rendered title text"
                ),
            )
        return

    ai_background_path = _cover_artifact_path(
        root,
        item,
        generation,
        "ai_background",
        "source_ai_background",
        "ai_background",
        "ai_cover",
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
    _audit_boundary_contract(
        issues=issues,
        stem=stem,
        record_path=record_path,
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


def audit_package(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    manifest_path = root / "review_manifest.json"
    manifest = _load_json(manifest_path)
    issues: list[dict[str, Any]] = []

    if not manifest:
        _add_issue(issues, "MANIFEST_MISSING_OR_INVALID", path=manifest_path)
        return {"passed": False, "root": str(root), "issues": issues, "issue_count": len(issues)}

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

    cover_attestations = manifest.get("cover_route_attestations")
    attestations_by_candidate: dict[str, dict[str, Any]] = {}
    if isinstance(cover_attestations, list):
        for attestation in cover_attestations:
            if not isinstance(attestation, dict):
                continue
            candidate_id = str(attestation.get("candidate_id") or "")
            if not candidate_id or candidate_id in attestations_by_candidate:
                _add_issue(
                    issues,
                    "MANIFEST_COVER_ATTESTATION_ID_INVALID",
                    path=manifest_path,
                    detail=f"candidate_id={candidate_id!r}",
                )
                continue
            attestations_by_candidate[candidate_id] = attestation

    for item in items:
        if not isinstance(item, dict):
            continue
        stem = str(item.get("stem") or item.get("id") or item.get("title") or "")
        subtitle_path = _resolve(root, item.get("subtitle_srt") or item.get("subtitle"))
        ass_path = _resolve(root, item.get("ass_path") or item.get("ass"))
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
            title = str(item.get("title") or "")
            if not (title.startswith("【李豆沙】豆沙歌，") and "《" in title and "》" in title):
                _add_issue(issues, "SONG_TITLE_FORMAT_INVALID", stem=stem, detail=title)

        if subtitle_path and subtitle_path.exists():
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

        if ass_path and ass_path.exists():
            for idx, text in enumerate(_ass_dialogue_texts(ass_path), start=1):
                visual_lines = _visual_lines(text)
                if len(visual_lines) > max_visual_lines:
                    _add_issue(issues, "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES", stem=stem, path=ass_path, detail=f"dialogue {idx} has {len(visual_lines)} visual lines")
                for line in visual_lines:
                    n = _text_len(line)
                    if n > max_visual_line_chars:
                        _add_issue(issues, "SUBTITLE_ASS_LINE_TOO_LONG", stem=stem, path=ass_path, detail=f"dialogue {idx} line length {n}: {line}")

        title_txt = _read_title_txt(title_txt_path)
        publish_title = _read_publish_title(publish_path)
        if title_txt and publish_title and title_txt != publish_title:
            _add_issue(issues, "PUBLISH_TITLE_TXT_MISMATCH", stem=stem, detail=f"title_txt={title_txt!r}; publish.title={publish_title!r}")

        cover_generation = item.get("cover_generation")
        record_path = _resolve(root, item.get("record") or item.get("record_json"))
        record = _load_json(record_path) if record_path else {}
        story_contract = record.get("story_contract")
        item_title = str(item.get("title") or "")
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
        story_candidate_id = (
            str(story_contract.get("candidate_id") or "")
            if isinstance(story_contract, dict)
            else ""
        )
        item_candidate_id = str(item.get("candidate_id") or "")
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
        record_generation = record.get("cover_generation")
        if not isinstance(record_generation, dict):
            record_generation = (
                publish_staging.get("cover_generation")
                if isinstance(publish_staging, dict)
                else None
            )
        finished_generation = (
            record_generation
            if isinstance(record_generation, dict)
            else cover_generation
        )
        if not is_song and isinstance(finished_generation, dict):
            font_size = finished_generation.get("font_size")
            if (
                isinstance(font_size, bool)
                or (isinstance(font_size, (int, float)) and font_size < COVER_MIN_TALK_FONT_SIZE)
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
            if attestations_by_candidate and attestation_candidate_id:
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
            fallback_cover = bool(item.get("cover_regenerated_from_burn_frame")) or any(
                marker in cover_generation_text.lower()
                for marker in ["deterministic", "burn-frame", "burned-frame", "fallback", "frame cover"]
            )
        if fallback_cover:
            _add_issue(issues, "COVER_FALLBACK_NOT_FINISHED", stem=stem, detail=cover_generation_text)
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
        )

    blocking = [issue for issue in issues if issue.get("severity") != "INFO"]
    return {"passed": not blocking, "root": str(root), "issues": issues, "issue_count": len(issues), "blocking_issue_count": len(blocking)}


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
