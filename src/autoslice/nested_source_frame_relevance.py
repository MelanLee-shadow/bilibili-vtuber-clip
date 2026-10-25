"""Evidence-only visual relevance for a caption-discovered source candidate.

This module is deliberately narrower than source identity.  It compares one
hash-bound frame from the current watched player with a few deterministic
frames from the independently cached candidate media.  The result can be used
as a guard for an explicit voice-reference handoff, but it never upgrades the
candidate cache, proves uniqueness, establishes timing, or authorizes a
subtitle/display mutation.
"""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import copy
import hashlib
import json
import math
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from src.autoslice.cpa_model_ladder import TOP_CPA_MODEL
from src.autoslice.cpa_frame_witness import _extract_frame_jpeg
from src.autoslice.package_import import atomic_write_bytes, atomic_write_json, exclusive_lock


SCHEMA_VERSION = "nested-source-frame-relevance.v1"
DEFAULT_MODEL = TOP_CPA_MODEL
DEFAULT_SOURCE_FRAME_COUNT = 3
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,180}\Z")
_DECISIONS = frozenset({"MATCH", "NO_MATCH", "UNKNOWN"})
_RELATIONS = frozenset({"SCENE_OR_PROGRAM", "APPEARANCE_ONLY", "UNKNOWN"})
_NO_PROVIDER_CALL_REASONS = frozenset(
    {
        "CPA_CREDENTIALS_MISSING",
        "BATCH_IMAGE_COUNT_INVALID",
        "BATCH_QUESTION_INVALID",
        "IMAGE_READ_FAILED",
    }
)


def _bare_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:").lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return digest


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("FILE_UNAVAILABLE")
    digest = _evidence_file_sha256(path)
    return digest


def _canonical_sha(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha_bytes(payload)


def _safe_segment(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(code)
    return value


def _same_path(left: Path, right: Path) -> bool:
    try:
        if left.resolve() == right.resolve():
            return True
        return left.is_file() and right.is_file() and os.path.samefile(left, right)
    except OSError:
        return False


def _mapping_path(raw: object, evidence_root: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("EVIDENCE_PATH_REQUIRED")
    path = Path(raw)
    return path if path.is_absolute() else evidence_root / path


def _declared_path_hash(raw_path: object, raw_sha: object, *, label: str) -> tuple[Path, str]:
    path = Path(str(raw_path))
    actual = _sha_file(path)
    declared = _bare_sha(raw_sha)
    if declared is None or declared != actual:
        raise ValueError(f"{label}_HASH_MISMATCH")
    return path.resolve(), actual


def _outer_identities(descriptor: Mapping[str, object]) -> set[tuple[str, str]]:
    """Read only explicitly named outer provenance fields, never candidate fields."""

    fields = (
        "nested_media_outer_context",
        "outer_context",
        "outer_provenance",
        "outer_media_identity",
        "current_outer_context",
    )
    identities: set[tuple[str, str]] = set()

    def visit(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        bvid = value.get("bvid")
        cid = value.get("cid")
        if isinstance(bvid, str) and isinstance(cid, (int, str)) and not isinstance(cid, bool):
            identities.add((bvid, str(cid)))
        for nested in value.values():
            if isinstance(nested, Mapping):
                visit(nested)

    for field in fields:
        visit(descriptor.get(field))
    return identities


def _find_current_evidence(
    descriptor: Mapping[str, object], evidence_root: Path,
) -> tuple[Path, str, Mapping[str, object]]:
    candidates: list[object] = []
    for field in (
        "current_evidence",
        "caption_evidence",
        "nested_media_current_evidence",
        "nested_media_caption_dedup",
    ):
        if field in descriptor:
            candidates.append(descriptor[field])
    for field in ("evidence", "acoustic_only_timing_scope"):
        if field in descriptor:
            candidates.append(descriptor[field])
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            raw_path = candidate.get("evidence_path", candidate.get("path"))
            raw_sha = candidate.get("evidence_sha256", candidate.get("sha256"))
        elif isinstance(candidate, str):
            raw_path, raw_sha = candidate, None
        else:
            continue
        if raw_path is None:
            continue
        path = _mapping_path(raw_path, evidence_root)
        actual = _sha_file(path)
        if _bare_sha(raw_sha) != actual:
            raise ValueError("CURRENT_EVIDENCE_HASH_MISMATCH")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("CURRENT_EVIDENCE_INVALID") from exc
        if not isinstance(document, Mapping):
            raise ValueError("CURRENT_EVIDENCE_INVALID")
        return path.resolve(), actual, document
    raise ValueError("CURRENT_EVIDENCE_REQUIRED")


def _validate_current_binding(
    *,
    candidate_id: str,
    descriptor: Mapping[str, object],
    source_media: Path,
    subtitle_path: Path,
    evidence_root: Path,
) -> dict[str, object]:
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("CANDIDATE_ID_REQUIRED")
    outer_path = source_media.resolve()
    outer_sha = _sha_file(outer_path)
    srt_path = subtitle_path.resolve()
    srt_sha = _sha_file(srt_path)
    if _bare_sha(descriptor.get("outer_media_sha256")) != outer_sha:
        raise ValueError("OUTER_MEDIA_HASH_MISMATCH")
    if _bare_sha(descriptor.get("final_srt_sha256")) != srt_sha:
        raise ValueError("FINAL_SRT_HASH_MISMATCH")

    frame_raw = descriptor.get("caption_frame_path")
    frame_ms = descriptor.get("caption_frame_ms")
    if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or frame_ms < 0:
        raise ValueError("CURRENT_FRAME_TIME_INVALID")
    frame_path, frame_sha = _declared_path_hash(
        frame_raw,
        descriptor.get("caption_frame_sha256"),
        label="CURRENT_FRAME",
    )
    interval = descriptor.get("interval_ms")
    target_interval = descriptor.get("target_interval_ms")
    target_cues = descriptor.get("target_cue_indexes")
    if (
        not isinstance(interval, list)
        or len(interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in interval)
        or interval[0] < 0
        or interval[1] <= interval[0]
        or not isinstance(target_interval, list)
        or len(target_interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in target_interval)
        or target_interval[0] < interval[0]
        or target_interval[1] > interval[1]
        or target_interval[1] <= target_interval[0]
        or not isinstance(target_cues, list)
        or len(target_cues) != 1
        or isinstance(target_cues[0], bool)
        or not isinstance(target_cues[0], int)
        or not target_interval[0] <= frame_ms < target_interval[1]
    ):
        raise ValueError("CURRENT_TARGET_BINDING_INVALID")

    visual = descriptor.get("visual_observation")
    if isinstance(visual, Mapping):
        if visual.get("frame_ms") != frame_ms:
            raise ValueError("CURRENT_VISUAL_FRAME_MISMATCH")
        if _bare_sha(visual.get("frame_sha256")) != frame_sha:
            raise ValueError("CURRENT_VISUAL_FRAME_HASH_MISMATCH")
        try:
            if Path(str(visual.get("frame_path"))).resolve() != frame_path:
                raise ValueError("CURRENT_VISUAL_FRAME_PATH_MISMATCH")
        except (OSError, RuntimeError) as exc:
            raise ValueError("CURRENT_VISUAL_FRAME_PATH_MISMATCH") from exc
        if visual.get("pipeline_burned_subtitle") not in (None, False):
            raise ValueError("CURRENT_VISUAL_BURNED_SUBTITLE")
        if visual.get("role") not in (
            None,
            "WATCHED_MEDIA_CAPTION",
            "NO_EMBEDDED_CAPTION_OBSERVED",
            "WATCHED_MEDIA",
        ):
            raise ValueError("CURRENT_VISUAL_ROLE_INVALID")

    evidence_path, evidence_sha, evidence = _find_current_evidence(descriptor, evidence_root)
    if evidence.get("candidate_id") not in (None, candidate_id):
        raise ValueError("CURRENT_EVIDENCE_CANDIDATE_MISMATCH")
    for key in ("source_media_sha256", "outer_media_sha256"):
        if key in evidence and _bare_sha(evidence.get(key)) != outer_sha:
            raise ValueError("CURRENT_EVIDENCE_OUTER_HASH_MISMATCH")
    for key in ("input_transcript_sha256", "final_srt_sha256"):
        if key in evidence and _bare_sha(evidence.get(key)) != srt_sha:
            raise ValueError("CURRENT_EVIDENCE_SRT_HASH_MISMATCH")
    if evidence.get("source_role") not in (None, "WATCHED_MEDIA"):
        raise ValueError("CURRENT_EVIDENCE_ROLE_INVALID")
    rows = evidence.get("observations")
    if not isinstance(rows, list):
        rows = evidence.get("scene_observations")
    if not isinstance(rows, list):
        raise ValueError("CURRENT_EVIDENCE_OBSERVATIONS_REQUIRED")
    matching_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("frame_ms") != frame_ms or _bare_sha(row.get("frame_sha256")) != frame_sha:
            continue
        if row.get("role") in ("WATCHED_MEDIA_CAPTION", "NO_EMBEDDED_CAPTION_OBSERVED"):
            matching_rows.append(row)
        elif row.get("scene") == "WATCHED_MEDIA":
            matching_rows.append(row)
    if len(matching_rows) != 1:
        raise ValueError("CURRENT_EVIDENCE_FRAME_BINDING_MISMATCH")

    return {
        "candidate_id": candidate_id,
        "outer_media": {"path": str(outer_path), "sha256": outer_sha},
        "final_srt": {"path": str(srt_path), "sha256": srt_sha},
        "current_frame": {
            "path": str(frame_path),
            "sha256": frame_sha,
            "frame_ms": frame_ms,
            "source_role": "WATCHED_MEDIA",
        },
        "context_interval_ms": list(interval),
        "target_interval_ms": list(target_interval),
        "target_cue_indexes": list(target_cues),
        "evidence": {"path": str(evidence_path), "sha256": evidence_sha},
    }


def _validate_cached_source(
    *, cached_source: Mapping[str, object], candidate_id: str, outer: Mapping[str, object],
    descriptor: Mapping[str, object],
) -> tuple[Path, str, dict[str, object]]:
    if (
        cached_source.get("status") != "CANDIDATE_CACHE_AVAILABLE"
        or cached_source.get("action") != "KEEP"
        or cached_source.get("mutation_authorized") is not False
        or cached_source.get("source_role_authority") is not False
        or cached_source.get("uniqueness_claim") is not False
    ):
        raise ValueError("SOURCE_CANDIDATE_CACHE_REQUIRED")
    consumer_id = cached_source.get("consumer_candidate_id")
    if consumer_id not in (None, candidate_id):
        raise ValueError("SOURCE_CANDIDATE_CONSUMER_MISMATCH")
    raw_path = cached_source.get("independent_source_media_path")
    path = Path(str(raw_path)).resolve()
    actual_sha = _sha_file(path)
    declared_sha = _bare_sha(cached_source.get("independent_source_media_sha256"))
    if declared_sha is None or declared_sha != actual_sha:
        raise ValueError("INDEPENDENT_SOURCE_HASH_MISMATCH")
    outer_path = Path(str(outer["path"])).resolve()
    outer_sha = str(outer["sha256"])
    if _same_path(path, outer_path) or actual_sha == outer_sha:
        raise ValueError("INDEPENDENT_SOURCE_OUTER_MEDIA_COLLISION")
    binding = cached_source.get("source_cache_binding")
    if not isinstance(binding, Mapping):
        raise ValueError("SOURCE_CACHE_BINDING_REQUIRED")
    bvid, cid = binding.get("bvid"), binding.get("cid")
    if not isinstance(bvid, str) or not isinstance(cid, (int, str)) or isinstance(cid, bool):
        raise ValueError("SOURCE_CACHE_BINDING_IDENTITY_INVALID")
    if (bvid, str(cid)) in _outer_identities(descriptor):
        raise ValueError("INDEPENDENT_SOURCE_OUTER_IDENTITY_COLLISION")
    source_binding = {
        "bvid": bvid,
        "cid": int(cid) if isinstance(cid, int) else cid,
        "webpage_url": binding.get("webpage_url"),
        "cache_entry_path": binding.get("cache_entry_path"),
        "cache_entry_sha256": binding.get("cache_entry_sha256"),
        "source_role_authority": False,
    }
    return path, actual_sha, source_binding


def _probe_duration_ms(path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    duration = float(completed.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("SOURCE_DURATION_INVALID")
    duration_ms = int(math.floor(duration * 1000.0))
    if duration_ms <= 0:
        raise ValueError("SOURCE_DURATION_INVALID")
    return duration_ms


def _existing_source_frames(
    cached_source: Mapping[str, object], source_path: Path, source_sha: str,
) -> list[dict[str, object]] | None:
    raw = cached_source.get("source_frames")
    if raw is None:
        raw = cached_source.get("independent_source_frames")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("SOURCE_FRAMES_INVALID")
    rows: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("SOURCE_FRAME_INVALID")
        frame_ms = item.get("frame_ms")
        if isinstance(frame_ms, bool) or not isinstance(frame_ms, int) or frame_ms < 0:
            raise ValueError("SOURCE_FRAME_TIME_INVALID")
        frame_path, frame_sha = _declared_path_hash(
            item.get("frame_path", item.get("path")),
            item.get("frame_sha256", item.get("sha256")),
            label="SOURCE_FRAME",
        )
        declared_source_sha = item.get("source_media_sha256")
        if declared_source_sha is None or _bare_sha(declared_source_sha) != source_sha:
            raise ValueError("SOURCE_FRAME_MEDIA_HASH_MISMATCH")
        declared_source_path = item.get("source_media_path")
        if declared_source_path is not None and Path(str(declared_source_path)).resolve() != source_path:
            raise ValueError("SOURCE_FRAME_MEDIA_PATH_MISMATCH")
        rows.append(
            {
                "path": str(frame_path),
                "sha256": frame_sha,
                "frame_ms": frame_ms,
            }
        )
    rows.sort(key=lambda row: (int(row["frame_ms"]), str(row["path"])))
    return rows[:DEFAULT_SOURCE_FRAME_COUNT]


def _source_frame_samples(
    *, cached_source: Mapping[str, object], source_path: Path, source_sha: str,
) -> list[dict[str, object]]:
    existing = _existing_source_frames(cached_source, source_path, source_sha)
    if existing is not None:
        return existing
    duration_ms = _probe_duration_ms(source_path)
    samples: list[dict[str, object]] = []
    seen_times: set[int] = set()
    for index in range(DEFAULT_SOURCE_FRAME_COUNT):
        frame_ms = max(0, min(duration_ms - 1, (duration_ms * (index + 1)) // (DEFAULT_SOURCE_FRAME_COUNT + 1)))
        if frame_ms in seen_times:
            continue
        frame_jpeg = _extract_frame_jpeg(source_path, frame_ms)
        if not frame_jpeg:
            raise ValueError("SOURCE_FRAME_EMPTY")
        samples.append({"frame_ms": frame_ms, "bytes": frame_jpeg, "sha256": _sha_bytes(frame_jpeg)})
        seen_times.add(frame_ms)
    if not samples:
        raise ValueError("SOURCE_FRAME_SAMPLES_EMPTY")
    return samples


def _question() -> str:
    return (
        "Compare the ordered images as visual evidence. Image 1 is one exact frame "
        "from the outermost watched player; the remaining images are exact frames "
        "from an independently cached candidate video. Assess only concrete visible "
        "program or scene content. A shared person, title, logo, style, or caption "
        "alone is appearance evidence and is insufficient for a scene/program match. "
        "If the evidence is incomplete or contradictory, use UNKNOWN. Do not infer "
        "voice, audio, timing, subtitle identity, uniqueness, source-role authority, "
        "or any edit/drop action. Return one JSON object with exactly these semantic "
        "fields (no markdown): {\"decision\":\"MATCH|NO_MATCH|UNKNOWN\","
        "\"relation\":\"SCENE_OR_PROGRAM|APPEARANCE_ONLY|UNKNOWN\","
        "\"visible_evidence\":\"brief concrete observation or uncertainty\"}."
    )


def _structured_decision(answer: object) -> tuple[str, str | None, dict[str, object] | None]:
    if isinstance(answer, Mapping):
        payload = dict(answer)
    elif isinstance(answer, str):
        text = answer.strip()
        if text.startswith("```") and text.endswith("```"):
            text = text[3:-3].strip()
            if text.lower().startswith("json"):
                text = text[4:].lstrip()
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return "UNKNOWN", "CPA_DECISION_SCHEMA_INVALID", None
        if not isinstance(decoded, Mapping):
            return "UNKNOWN", "CPA_DECISION_SCHEMA_INVALID", None
        payload = dict(decoded)
    else:
        return "UNKNOWN", "CPA_DECISION_SCHEMA_INVALID", None
    decision = payload.get("decision")
    relation = payload.get("relation")
    visible = payload.get("visible_evidence")
    if (
        not isinstance(decision, str)
        or not isinstance(relation, str)
        or not isinstance(visible, str)
        or not visible.strip()
        or decision.upper() not in _DECISIONS
        or relation.upper() not in _RELATIONS
    ):
        return "UNKNOWN", "CPA_DECISION_SCHEMA_INVALID", None
    normalized = {
        "decision": decision.upper(),
        "relation": relation.upper(),
        "visible_evidence": visible.strip(),
    }
    if normalized["decision"] == "MATCH" and normalized["relation"] != "SCENE_OR_PROGRAM":
        return "UNKNOWN", "CPA_MATCH_RELATION_INSUFFICIENT", normalized
    return normalized["decision"], None, normalized


def _vision_binding_reason(
    receipt: Mapping[str, object], *, question: str, model: str,
    image_hashes: list[str],
) -> str | None:
    response_images = receipt.get("images")
    if (
        receipt.get("model") != model
        or receipt.get("question") != question
        or not isinstance(response_images, list)
        or len(response_images) != len(image_hashes)
        or [
            _bare_sha(row.get("image_sha256"))
            for row in response_images
            if isinstance(row, Mapping)
        ]
        != image_hashes
    ):
        return "VISION_PROBE_RESPONSE_BINDING_MISMATCH"
    prompt_binding = {
        "question": question,
        "image_sha256s": image_hashes,
        "model": model,
    }
    if receipt.get("prompt_sha256") != _sha_bytes(
        json.dumps(prompt_binding, sort_keys=True).encode("utf-8")
    ):
        return "VISION_PROBE_RESPONSE_BINDING_MISMATCH"
    return None


def _provider_call_count(receipt: Mapping[str, object]) -> int | None:
    """Classify only what the batch receipt can actually establish."""

    if receipt.get("status") == "OBSERVED":
        return 1
    reason = receipt.get("reason_code")
    if isinstance(reason, str) and reason in _NO_PROVIDER_CALL_REASONS:
        return 0
    # HTTP/provider failure and slot timeout do not expose whether the remote
    # request was accepted.  Keep the count explicitly unknown.
    return None


def _validate_artifacts(saved_request: Mapping[str, object]) -> bool:
    artifacts = saved_request.get("image_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            return False
        path = Path(str(artifact.get("path")))
        try:
            if _sha_file(path) != _bare_sha(artifact.get("sha256")):
                return False
        except (OSError, ValueError):
            return False
    return True


def _base_result(candidate_id: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "status": "UNAVAILABLE",
        "authority": "EVIDENCE_ONLY",
        "decision": "UNKNOWN",
        "action": "KEEP",
        "mutation_authorized": False,
        "source_cache_verified": False,
        "source_role_authority": False,
        "uniqueness_claim": False,
        "time_alignment_performed": False,
        "source_subtraction_authorized": False,
        "caption_dedup_authority": False,
        "drop_authority": False,
        "probe_invocations": 0,
        "provider_calls_this_invocation": 0,
        "provider_calls": 0,
        "cpa_calls": 0,
    }


def observe_candidate_source_frame_relevance(
    *,
    candidate_id: str,
    descriptor: Mapping[str, object],
    cached_source: Mapping[str, object],
    source_media: Path,
    subtitle_path: Path,
    evidence_root: Path,
    vision_probe: Callable[[list[Path], str], Mapping[str, object]] | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, object]:
    """Compare the current outer frame with exact frames from cached media.

    ``vision_probe`` is normally a closure around
    :func:`cpa_frame_witness.batch_jpeg_vision_probe`.  The callback receives
    one outer image first and then up to three independent-source images.  A
    missing callback means no visual call is made.  Every failure is returned
    as ``UNKNOWN``/``KEEP`` so this helper cannot open a higher-authority
    route by itself.
    """

    result = _base_result(candidate_id)
    try:
        if not isinstance(descriptor, Mapping) or not isinstance(cached_source, Mapping):
            raise ValueError("RELEVANCE_INPUT_INVALID")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("VISION_MODEL_REQUIRED")
        outer = _validate_current_binding(
            candidate_id=candidate_id,
            descriptor=descriptor,
            source_media=Path(source_media),
            subtitle_path=Path(subtitle_path),
            evidence_root=Path(evidence_root),
        )
        source_path, source_sha, source_binding = _validate_cached_source(
            cached_source=cached_source,
            candidate_id=candidate_id,
            outer=outer["outer_media"],
            descriptor=descriptor,
        )
        result.update(
            {
                "outer_media": dict(outer["outer_media"]),
                "final_srt": dict(outer["final_srt"]),
                "current_frame": dict(outer["current_frame"]),
                "target_interval_ms": list(outer["target_interval_ms"]),
                "target_cue_indexes": list(outer["target_cue_indexes"]),
                "current_evidence": dict(outer["evidence"]),
                "source_media": {"path": str(source_path), "sha256": source_sha},
                "source_cache_binding": source_binding,
            }
        )
        if vision_probe is None:
            result["reason_code"] = "VISION_PROBE_REQUIRED"
            return result

        source_samples = _source_frame_samples(
            cached_source=cached_source, source_path=source_path, source_sha=source_sha,
        )
        current_bytes = Path(str(outer["current_frame"]["path"])).read_bytes()
        if not current_bytes:
            raise ValueError("CURRENT_FRAME_EMPTY")
        image_inputs: list[dict[str, object]] = [
            {
                "role": "OUTER_WATCHED_PLAYER",
                "origin_path": str(outer["current_frame"]["path"]),
                "frame_ms": outer["current_frame"]["frame_ms"],
                "sha256": outer["current_frame"]["sha256"],
            }
        ]
        for sample in source_samples:
            image_inputs.append(
                {
                    "role": "INDEPENDENT_CACHED_SOURCE",
                    "origin_path": str(sample.get("path", source_path)),
                    "frame_ms": sample.get("frame_ms"),
                    "sha256": sample["sha256"],
                }
            )
        question = _question()
        request_binding: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "model": model,
            "question": question,
            "outer_media": dict(outer["outer_media"]),
            "final_srt": dict(outer["final_srt"]),
            "current_frame": dict(outer["current_frame"]),
            "current_evidence": dict(outer["evidence"]),
            "context_interval_ms": list(outer["context_interval_ms"]),
            "target_interval_ms": list(outer["target_interval_ms"]),
            "target_cue_indexes": list(outer["target_cue_indexes"]),
            "source_media": {"path": str(source_path), "sha256": source_sha},
            "source_cache_binding": source_binding,
            "images": image_inputs,
        }
        request_sha = _canonical_sha(request_binding)
        base = Path(evidence_root).resolve() / _safe_segment(
            candidate_id, code="CANDIDATE_ID_INVALID"
        )
        request_dir = base / request_sha
        request_path = request_dir / "REQUEST.json"
        response_path = request_dir / "RESPONSE.json"
        result.update(
            {
                "request_sha256": request_sha,
                "request_path": str(request_path),
                "response_path": str(response_path),
            }
        )
        request_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(base / "relevance.lock", label="source frame relevance"):
            if request_path.exists() or response_path.exists():
                if not request_path.is_file() or not response_path.is_file():
                    result["reason_code"] = "RELEVANCE_EVIDENCE_INCOMPLETE"
                    return result
                try:
                    saved_request = json.loads(request_path.read_text(encoding="utf-8"))
                    saved_response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    result["reason_code"] = "RELEVANCE_EVIDENCE_INVALID"
                    return result
                if (
                    not isinstance(saved_request, Mapping)
                    or saved_request.get("request_sha256") != request_sha
                    or saved_request.get("binding") != request_binding
                    or not isinstance(saved_response, Mapping)
                    or saved_response.get("request_sha256") != request_sha
                    or not _validate_artifacts(saved_request)
                ):
                    result["reason_code"] = "RELEVANCE_EVIDENCE_BINDING_MISMATCH"
                    return result
                saved_receipt = saved_response.get("vision_receipt")
                if (
                    isinstance(saved_receipt, Mapping)
                    and saved_receipt.get("status") == "OBSERVED"
                ):
                    binding_reason = _vision_binding_reason(
                        saved_receipt,
                        question=question,
                        model=model,
                        image_hashes=[str(row["sha256"]) for row in image_inputs],
                    )
                    if binding_reason is not None:
                        result["reason_code"] = binding_reason
                        return result
                    decision, _reason, structured = _structured_decision(saved_receipt.get("answer"))
                    if (saved_response.get("decision") != decision
                            or saved_response.get("relevance_observation") != structured):
                        result["reason_code"] = "RELEVANCE_DECISION_CACHE_MISMATCH"
                        return result
                elif saved_response.get("decision") != "UNKNOWN":
                    result["reason_code"] = "RELEVANCE_DECISION_CACHE_MISMATCH"
                    return result
                fixed = _base_result(candidate_id)
                if any(saved_response.get(key) != fixed[key] for key in (
                    "authority", "action", "mutation_authorized", "source_cache_verified",
                    "source_role_authority", "uniqueness_claim", "time_alignment_performed",
                    "source_subtraction_authorized", "caption_dedup_authority", "drop_authority",
                )):
                    result["reason_code"] = "RELEVANCE_AUTHORITY_CACHE_MISMATCH"
                    return result
                result.update(copy.deepcopy(dict(saved_response)))
                result["status"] = "REPLAYED"
                result["probe_invocations"] = 0
                result["provider_calls_this_invocation"] = 0
                return result

            artifact_paths = [request_dir / "outer-watched-player.jpg"]
            artifact_bytes = [current_bytes]
            for index, sample in enumerate(source_samples, start=1):
                artifact_paths.append(request_dir / f"independent-source-{index:02d}.jpg")
                sample_bytes = sample.get("bytes")
                if not isinstance(sample_bytes, bytes):
                    sample_bytes = Path(str(sample["path"])).read_bytes()
                artifact_bytes.append(sample_bytes)
            for path, payload in zip(artifact_paths, artifact_bytes):
                atomic_write_bytes(path, payload)
            request_document = {
                "schema_version": SCHEMA_VERSION,
                "request_sha256": request_sha,
                "binding": request_binding,
                "image_artifacts": [
                    {"path": str(path), "sha256": _sha_bytes(payload)}
                    for path, payload in zip(artifact_paths, artifact_bytes)
                ],
            }
            atomic_write_json(request_path, request_document)
            try:
                vision_receipt = vision_probe(list(artifact_paths), question)
            except Exception as exc:
                vision_receipt = {
                    "status": "UNAVAILABLE",
                    "reason_code": "VISION_PROBE_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            result["probe_invocations"] = 1
            if not isinstance(vision_receipt, Mapping):
                vision_receipt = {
                    "status": "UNAVAILABLE",
                    "reason_code": "VISION_PROBE_RESPONSE_INVALID",
                }
            else:
                vision_receipt = copy.deepcopy(dict(vision_receipt))
            result["vision_receipt"] = vision_receipt
            result["provider_calls"] = _provider_call_count(vision_receipt)
            result["provider_calls_this_invocation"] = result["provider_calls"]
            result["cpa_calls"] = result["provider_calls"]
            if vision_receipt.get("status") != "OBSERVED":
                result["reason_code"] = str(
                    vision_receipt.get("reason_code") or "VISION_PROBE_UNAVAILABLE"
                )
            else:
                binding_reason = _vision_binding_reason(
                    vision_receipt,
                    question=question,
                    model=model,
                    image_hashes=[str(row["sha256"]) for row in image_inputs],
                )
                if binding_reason is not None:
                    result["reason_code"] = binding_reason
                else:
                    answer = vision_receipt.get("answer")
                    if not isinstance(answer, (str, Mapping)) or (
                        isinstance(answer, str) and not answer.strip()
                    ):
                        result["reason_code"] = "VISION_PROBE_ANSWER_MISSING"
                    else:
                        decision, reason, structured = _structured_decision(answer)
                        result["decision"] = decision
                        if structured is not None:
                            result["relevance_observation"] = structured
                        if reason is not None:
                            result["reason_code"] = reason
                        else:
                            result.pop("reason_code", None)
            result["status"] = "OBSERVED" if vision_receipt.get("status") == "OBSERVED" else "UNAVAILABLE"
            response_document = copy.deepcopy(result)
            atomic_write_json(response_path, response_document)
            return result
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        result["reason_code"] = str(exc) or type(exc).__name__
        return result


__all__ = [
    "DEFAULT_MODEL",
    "SCHEMA_VERSION",
    "observe_candidate_source_frame_relevance",
]
