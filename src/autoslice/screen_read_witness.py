"""Screen-read witness: recover fast-spoken lines from on-screen text.

公开规则（424_522 BV1d73P6iErL 1:24 案）：她快速念屏幕上的
「战斗回合用尽，即将离开战场」，音频糊但**看一眼画面就知道全文**。

两个设计裁定（他亲自点的）：
- 何时看画面：不盲扫——声学证人自己承认失败（confidence 低 / 不确定位
 密集）时才升级视觉，一次 finding 至多采样两帧，成本只花在耳朵糊掉处。
- 如何找到她念的词：不定位屏幕区域——全帧 OCR 出文本池，用**拼音对齐**
 挑出与听写最相似的一段（她念的就是池里读音最像的那条），需边际优势，
 平票不选。

命中的屏幕文本以 ``verified_ocr`` 出处进入既有裁决引擎（正字法权威白名单
原生认这个 kind），judge 照常闭集裁定——视觉只供**候选与出处**，永不直改。
"""

from __future__ import annotations

from .review_errors import ContractRejection

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import json
import re
import hashlib
import subprocess
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any
from pathlib import Path

SCREEN_TEXT_QUESTION = (
    "Transcribe EVERY piece of legible on-screen text in this frame "
    "(game UI, dialog boxes, system messages, buttons, subtitles burned "
    "into the game, visible live chat/danmaku and Super Chat cards). "
    "Inspect the entire frame, including side panels and text beside the "
    "streamer avatar/webcam; exclude only the avatar/webcam image itself, "
    "never the adjacent chat. Verbatim, keep the "
    "original language, no translation, no guessing at blurry text. "
    'Reply with STRICT JSON only: {"texts": ["...", "..."]} — one entry '
    "per visually distinct text region, longest regions first."
)

_MIN_MATCH_RATIO = 0.55
_MIN_MATCH_MARGIN = 0.10
_MIN_TEXT_CHARS = 4
_SCREEN_TOKEN_ALIGNMENT_THRESHOLD = 0.80
WEAK_WITNESS_CONFIDENCE = 0.75
WEAK_WITNESS_UNCERTAIN_FRACTION = 1 / 3


def validate_screen_context_observation(
    srt_text: str,
    request: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Read immutable earlier-screen receipts as context, never transcript truth.

    The original SRT remains immutable. A legitimate text-only successor may
    reuse the observation when this cue's source clock is unchanged; the output
    binds the actual new SRT rather than relabelling an old CPA verdict.
    External source frames remain explicitly candidate context.
    """
    from src.autoslice.jingting_chunker import parse_srt_cues

    def checked_bytes(row: Mapping[str, Any]) -> bytes:
        data = Path(str(row["path"])).read_bytes()
        if hashlib.sha256(data).hexdigest() != str(row["sha256"]).removeprefix("sha256:"):
            raise ContractRejection("SCREEN_CONTEXT_FILE_DRIFT")
        return data

    try:
        index = observation.get("cue_index")
        if index not in request.get("cue_indexes", []):
            return None, "SCREEN_CONTEXT_OTHER_CUE"
        if (observation.get("schema_version") != "screen-context-observation.v1"
            or observation.get("authority") != "EVIDENCE_ONLY"
            or observation.get("mutation_authorized") is not False
            or observation.get("same_cue_direct_transcript_authority") is not False):
            raise ContractRejection("SCREEN_CONTEXT_AUTHORITY_INVALID")
        binding = request.get("clip_context_binding") or {}
        # G6: the candidate is the clip identity; the whole clip-context hash
        # (chat rows, speech memory, draft SRT) is representation, not part of
        # which screen frames were read (the frames and SRT bytes are checked below).
        if (not isinstance(observation.get("candidate_id"), str)
            or not observation.get("candidate_id")
            or observation.get("candidate_id") != binding.get("candidate_id")):
            raise ContractRejection("SCREEN_CONTEXT_CANDIDATE_CONTEXT_DRIFT")
        if (isinstance(index, bool) or not isinstance(index, int) or index < 1
            or observation.get("source_media_timeline_offset_ms")
            != request.get("source_media_timeline_offset_ms")):
            raise ContractRejection("SCREEN_CONTEXT_TIMELINE_DRIFT")
        old_text = checked_bytes(observation["original_srt"]).decode("utf-8")
        checked_bytes(observation["related_source_media"])
        old_cues = [c for c in parse_srt_cues(old_text) if c.text.strip()]
        current_cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
        old, current = old_cues[index - 1], current_cues[index - 1]
        if (old.start_ms, old.end_ms) != (current.start_ms, current.end_ms):
            raise ContractRejection("SCREEN_CONTEXT_CURRENT_CUE_GRID_DRIFT")
        if (request.get("matched_start_ms"), request.get("matched_end_ms")) != (
            current.start_ms, current.end_ms,
        ):
            raise ContractRejection("SCREEN_CONTEXT_REQUEST_CUE_GRID_DRIFT")
        offset = observation["source_media_timeline_offset_ms"]
        target = [current.start_ms + offset, current.end_ms + offset]
        if observation.get("target_source_interval_ms") != target:
            raise ContractRejection("SCREEN_CONTEXT_TARGET_SOURCE_DRIFT")
        span = observation["earlier_source_interval_ms"]
        if (not isinstance(span, list) or len(span) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in span)
            or not 0 <= span[0] < span[1] <= target[0]):
            raise ContractRejection("SCREEN_CONTEXT_NOT_EARLIER")
        frames = []
        for item in observation["observations"]:
            frame_ms = item["related_source_frame_ms"]
            if (isinstance(frame_ms, bool) or not isinstance(frame_ms, int)
                or not span[0] <= frame_ms < span[1]):
                raise ContractRejection("SCREEN_CONTEXT_FRAME_OUTSIDE_SPAN")
            result = json.loads(checked_bytes(item["receipt_result"]))
            receipt = result.get("receipt")
            image_sha256 = None
            if receipt is None:
                from src.autoslice.cpa_frame_witness import _extract_frame_jpeg

                probe = result["screen_read_witness"]
                receipt = probe["receipts"][item["receipt_index"]]
                media = observation["related_source_media"]["path"]
                if (probe.get("media_path") != media or receipt.get("media_path") != media
                    or receipt.get("frame_ms") != frame_ms
                    or item.get("frame_relation") != "SAME_MEDIA_EARLIER_FRAME"
                    or hashlib.sha256(_extract_frame_jpeg(Path(media), frame_ms)).hexdigest()
                    != receipt.get("frame_sha256")):
                    raise ContractRejection("SCREEN_CONTEXT_SOURCE_FRAME_DRIFT")
            else:
                checked_bytes(item["image"])
                image_sha256 = str(item["image"]["sha256"]).removeprefix("sha256:")
                if receipt.get("image_sha256") != image_sha256:
                    raise ContractRejection("SCREEN_CONTEXT_IMAGE_DRIFT")
            if (result.get("status") != "COMPLETED_EVIDENCE_ONLY"
                or result.get("same_cue_direct_transcript_authority") is not False
                or result.get("subtitle_mutation") is not False
                or not isinstance(receipt, Mapping)
                or receipt.get("schema_version") != "cpa-frame-witness.v1"
                or receipt.get("status") != "OBSERVED"
                or not _screen_answer_valid(str(receipt.get("answer") or ""))):
                raise ContractRejection("SCREEN_CONTEXT_RECEIPT_INVALID")
            evidence = item.get("source_evidence_artifacts")
            if not isinstance(evidence, list) or not evidence:
                raise ContractRejection("SCREEN_CONTEXT_SOURCE_EVIDENCE_MISSING")
            for artifact in evidence:
                checked_bytes(artifact)
            if item.get("frame_relation") not in {
                "SAME_MEDIA_EARLIER_FRAME", "EXTERNAL_SOURCE_FRAME_CONTEXT_CANDIDATE",
            }:
                raise ContractRejection("SCREEN_CONTEXT_FRAME_RELATION_INVALID")
            frames.append({
                "related_source_frame_ms": frame_ms,
                "frame_relation": item["frame_relation"],
                "receipt_result_sha256": item["receipt_result"]["sha256"],
                "image_sha256": image_sha256,
                "frame_sha256": receipt.get("frame_sha256"),
                "texts": parse_screen_text_pool([receipt]),
                "receipt": dict(receipt),
                "source_evidence_artifacts": list(evidence),
            })
        if not frames:
            raise ContractRejection("SCREEN_CONTEXT_FRAMES_MISSING")
        return {
            "schema_version": "validated-screen-context-observation.v1",
            "scope": "EARLIER_SOURCE_SCREEN_CONTEXT_ONLY",
            "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
            "same_cue_direct_transcript_authority": False,
            "candidate_id": observation["candidate_id"], "cue_index": index,
            "current_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
            "original_srt_sha256": observation["original_srt"]["sha256"],
            "related_source_media": dict(observation["related_source_media"]),
            "source_media_timeline_offset_ms": offset,
            "target_source_interval_ms": target,
            "earlier_source_interval_ms": span, "observations": frames,
        }, "SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"
    except (OSError, ValueError, KeyError, IndexError, TypeError, UnicodeError,
            AttributeError, subprocess.SubprocessError):
        return None, "SCREEN_CONTEXT_BINDING_INVALID"


def _range_screen_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    digest = value.removeprefix("sha256:").lower()
    return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None


def _range_screen_canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _range_screen_checked_bytes(row: Mapping[str, Any], label: str) -> tuple[bytes, str]:
    path_value, declared = row.get("path"), _range_screen_digest(row.get("sha256"))
    if not isinstance(path_value, str) or not path_value or declared is None:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_BINDING_INVALID")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_MISSING")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_MISSING") from exc
    actual = hashlib.sha256(data).hexdigest()
    if actual != declared:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_HASH_DRIFT")
    return data, actual


def _range_screen_checked_hash(row: Mapping[str, Any], label: str) -> str:
    path_value, declared = row.get("path"), _range_screen_digest(row.get("sha256"))
    if not isinstance(path_value, str) or not path_value or declared is None:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_BINDING_INVALID")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_MISSING")
    try:
        digest = _evidence_file_sha256(path)
    except OSError as exc:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_MISSING") from exc
    actual = digest
    if actual != declared:
        raise ContractRejection(f"RANGE_SCREEN_CONTEXT_{label}_HASH_DRIFT")
    return actual


def _range_screen_checked_observation(
    item: Mapping[str, Any], source_media_path: str,
) -> dict[str, Any]:
    frame_ms = item.get("related_source_frame_ms")
    relation = item.get("frame_relation") or "WHEN_CONTAINED_IN_RANGE"
    if type(frame_ms) is not int or not isinstance(relation, str):
        raise ContractRejection("RANGE_SCREEN_CONTEXT_FRAME_INVALID")
    if "EARLIER" in relation.upper():
        raise ContractRejection("RANGE_SCREEN_CONTEXT_EARLIER_ROLE")
    image = item.get("image")
    receipt_result = item.get("receipt_result")
    artifacts = item.get("source_evidence_artifacts")
    if not isinstance(image, Mapping) or not isinstance(receipt_result, Mapping):
        raise ContractRejection("RANGE_SCREEN_CONTEXT_OBSERVATION_ARTIFACT_MISSING")
    if not isinstance(artifacts, list) or not artifacts:
        raise ContractRejection("RANGE_SCREEN_CONTEXT_SOURCE_EVIDENCE_MISSING")
    _image_bytes, image_sha256 = _range_screen_checked_bytes(image, "IMAGE")
    result_bytes, result_sha256 = _range_screen_checked_bytes(receipt_result, "RECEIPT_RESULT")
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ContractRejection("RANGE_SCREEN_CONTEXT_RECEIPT_RESULT_JSON_INVALID") from exc
    receipt = result.get("receipt") if isinstance(result, Mapping) else None
    answer = receipt.get("answer") if isinstance(receipt, Mapping) else None
    if (
        not isinstance(result, Mapping)
        or result.get("status") != "COMPLETED_EVIDENCE_ONLY"
        or result.get("same_cue_direct_transcript_authority") is not False
        or result.get("subtitle_mutation") is not False
        or not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != "cpa-frame-witness.v1"
        or receipt.get("status") != "OBSERVED"
        or receipt.get("frame_ms") != frame_ms
        or _range_screen_digest(receipt.get("frame_sha256")) != image_sha256
        or receipt.get("media_path") != source_media_path
        or not isinstance(answer, str) or not answer.strip()
    ):
        raise ContractRejection("RANGE_SCREEN_CONTEXT_RECEIPT_INVALID")
    evidence_rows, evidence_hashes, evidence_bytes = [], [], []
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SOURCE_EVIDENCE_MISSING")
        artifact_bytes, artifact_sha = _range_screen_checked_bytes(artifact, "SOURCE_EVIDENCE")
        evidence_rows.append(dict(artifact))
        evidence_hashes.append(artifact_sha)
        evidence_bytes.append(artifact_bytes)
    for key in ("raw_frame_sha256", "raw_receipt_sha256", "raw_result_sha256"):
        declared = _range_screen_digest(result.get(key))
        if declared is None or declared not in {image_sha256, *evidence_hashes}:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RECEIPT_RESULT_DRIFT")
    raw_receipt_sha256 = _range_screen_digest(result.get("raw_receipt_sha256"))
    matching_raw_receipts = [
        data for data, digest in zip(evidence_bytes, evidence_hashes, strict=True)
        if digest == raw_receipt_sha256
    ]
    if not matching_raw_receipts:
        raise ContractRejection("RANGE_SCREEN_CONTEXT_RAW_RECEIPT_MISSING")
    try:
        raw_receipt = json.loads(matching_raw_receipts[0].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ContractRejection("RANGE_SCREEN_CONTEXT_RAW_RECEIPT_INVALID") from exc
    if raw_receipt != receipt:
        raise ContractRejection("RANGE_SCREEN_CONTEXT_RAW_RECEIPT_DRIFT")
    return {
        "related_source_frame_ms": frame_ms,
        "frame_relation": "WHEN_CONTAINED_IN_RANGE",
        "receipt_result_sha256": result_sha256,
        "image_sha256": image_sha256,
        "frame_sha256": image_sha256,
        "texts": parse_screen_text_pool([receipt]) or [answer],
        "receipt": dict(receipt),
        "source_evidence_artifacts": evidence_rows,
    }


def validate_range_screen_context_observation(
    srt_text: str,
    current_request: Mapping[str, Any],
    context_only_range_acoustic_observation: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Validate neutral range screen context against an existing range seal."""

    try:
        from src.autoslice.jingting_chunker import parse_srt_cues
        if not all(isinstance(value, Mapping) for value in (
            current_request, context_only_range_acoustic_observation, observation,
        )):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_INPUT_INVALID")
        if observation.get("schema_version") != "range-screen-context-observation.v1":
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SCHEMA_INVALID")
        if observation.get("authority") != "EVIDENCE_ONLY" or observation.get(
            "mutation_authorized"
        ) is not False or observation.get("same_cue_direct_transcript_authority") is not False:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_AUTHORITY_INVALID")
        cue_index = observation.get("cue_index")
        if type(cue_index) is not int or cue_index < 1:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_CUE_INVALID")
        if cue_index not in (current_request.get("cue_indexes") or []):
            return None, "RANGE_SCREEN_CONTEXT_OTHER_CUE"
        binding = current_request.get("clip_context_binding")
        candidate_id, context_sha256 = (
            binding.get("candidate_id"), binding.get("context_sha256")
        ) if isinstance(binding, Mapping) else (None, None)
        # G6: candidate identity, not the whole clip-context hash (same rule as
        # validate_screen_context_observation).
        if (
            not isinstance(candidate_id, str) or not candidate_id
            or observation.get("candidate_id") != candidate_id
        ):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_CANDIDATE_DRIFT")
        if current_request.get("whole_clip_current_srt") != srt_text:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SRT_REQUEST_DRIFT")
        current_srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
        offset = current_request.get("source_media_timeline_offset_ms")
        if type(offset) is not int or offset < 0:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_OFFSET_INVALID")
        if observation.get("source_media_timeline_offset_ms") != offset:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_OFFSET_DRIFT")
        cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
        if cue_index > len(cues):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_CURRENT_CUE_DRIFT")
        current_cue = cues[cue_index - 1]
        current_grid = (current_cue.start_ms, current_cue.end_ms)
        if (current_request.get("matched_start_ms"), current_request.get("matched_end_ms")) != current_grid:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_CURRENT_CUE_GRID_DRIFT")
        target = [current_cue.start_ms + offset, current_cue.end_ms + offset]
        if observation.get("target_source_interval_ms") != target:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_TARGET_SOURCE_DRIFT")

        original_srt, source_media = observation.get("original_srt"), observation.get("related_source_media")
        if not isinstance(original_srt, Mapping) or not isinstance(source_media, Mapping):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SOURCE_BINDING_MISSING")
        original_bytes, original_sha256 = _range_screen_checked_bytes(original_srt, "ORIGINAL_SRT")
        source_sha256 = _range_screen_checked_hash(source_media, "SOURCE_MEDIA")
        try:
            original_cues = [cue for cue in parse_srt_cues(original_bytes.decode("utf-8")) if cue.text.strip()]
        except UnicodeError as exc:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_ORIGINAL_SRT_INVALID") from exc
        if cue_index > len(original_cues) or (
            original_cues[cue_index - 1].start_ms, original_cues[cue_index - 1].end_ms
        ) != current_grid:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_CURRENT_CUE_GRID_DRIFT")

        range_row = context_only_range_acoustic_observation
        range_srt_sha256 = current_srt_sha256
        if range_row.get("schema_version") == "range-acoustic-context-carryover.v1":
            from src.autoslice.independent_context_adjudication import (
                adapt_range_acoustic_context_observation,
            )

            original_range = range_row.get("original_range_observation")
            if not isinstance(original_range, Mapping):
                raise ContractRejection("RANGE_SCREEN_CONTEXT_CARRYOVER_BINDING_INVALID")
            replayed, _reason = adapt_range_acoustic_context_observation(
                srt_text, current_request, original_range,
            )
            if replayed is None or replayed != range_row:
                raise ContractRejection("RANGE_SCREEN_CONTEXT_CARRYOVER_BINDING_INVALID")
            range_srt_sha256 = _range_screen_digest(
                replayed.get("original_reviewed_srt_sha256")
            )
            if (
                _range_screen_digest(replayed.get("current_srt_sha256")) != current_srt_sha256
                or range_srt_sha256 != original_sha256
            ):
                raise ContractRejection("RANGE_SCREEN_CONTEXT_CARRYOVER_SRT_DRIFT")
            # All original acoustic artifacts and current cue geometry were
            # replayed above. Keep their original SRT binding; the carryover
            # is evidence about this current text, never a new raw receipt.
            range_row = original_range
        elif range_row.get("text_revision") == "TEXT_ONLY_REVISION_AT_IDENTICAL_CUE_GRID":
            # A subtitle-text-free range survived a text-only revision at the
            # identical grid (41:1489-1492). Its receipt keeps the original SRT
            # binding; the row states which current SRT it was checked against.
            if _range_screen_digest(range_row.get("current_srt_sha256")) != current_srt_sha256:
                raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_SRT_DRIFT")
            range_srt_sha256 = _range_screen_digest(range_row.get("original_reviewed_srt_sha256"))
        if (
            range_row.get("schema_version") != "range-acoustic-context-observation.v1"
            or range_row.get("authority") != "EVIDENCE_ONLY"
            or range_row.get("mutation_authorized") is not False
            or range_row.get("candidate_id") != candidate_id
            or range_row.get("current_cue_index") != cue_index
        ):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_BINDING_INVALID")
        range_request, range_bindings, verdict = (
            range_row.get("witness_request"), range_row.get("artifact_bindings"), range_row.get("verdict")
        )
        if not all(isinstance(value, Mapping) for value in (range_request, range_bindings, verdict)):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_BINDING_INVALID")
        if range_request.get("source_media_timeline_offset_ms") != offset:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_OFFSET_DRIFT")
        if _range_screen_digest(range_bindings.get("source_media_sha256")) != source_sha256:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SOURCE_DRIFT")
        if range_bindings.get("source_media_path") != source_media.get("path"):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_SOURCE_PATH_DRIFT")
        if _range_screen_digest(range_bindings.get("reviewed_srt_sha256")) != range_srt_sha256:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_SRT_DRIFT")
        source_window = range_row.get("source_window")
        if isinstance(source_window, Mapping):
            span = [source_window.get("crop_start_ms"), source_window.get("crop_end_ms")]
            range_target = [source_window.get("target_start_ms"), source_window.get("target_end_ms")]
        else:
            start = range_request.get("matched_start_ms", range_request.get("context_start_ms"))
            end = range_request.get("matched_end_ms", range_request.get("context_end_ms"))
            span = [verdict.get("audio_start_ms"), verdict.get("audio_end_ms")]
            range_target = [start + offset, end + offset] if type(start) is int and type(end) is int else [-1, -1]
        if (
            [verdict.get("audio_start_ms"), verdict.get("audio_end_ms")] != span
            or observation.get("range_source_interval_ms") != span
            or any(type(value) is not int for value in (*span, *range_target))
            or not range_target[0] <= target[0] < target[1] <= range_target[1]
            or not span[0] <= target[0] < target[1] <= span[1]
        ):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_GEOMETRY_DRIFT")
        if not any(
            isinstance(row, Mapping) and row.get("cue_index") == cue_index
            and (row.get("start_ms"), row.get("end_ms")) == current_grid
            for row in range_row.get("cue_texts") or []
        ):
            raise ContractRejection("RANGE_SCREEN_CONTEXT_RANGE_CUE_GRID_DRIFT")

        checked = []
        for item in observation.get("observations") or []:
            if not isinstance(item, Mapping):
                raise ContractRejection("RANGE_SCREEN_CONTEXT_OBSERVATION_INVALID")
            frame_ms = item.get("related_source_frame_ms")
            if type(frame_ms) is not int or not span[0] <= frame_ms < span[1]:
                raise ContractRejection("RANGE_SCREEN_CONTEXT_FRAME_OUTSIDE_RANGE")
            if frame_ms < target[0]:
                raise ContractRejection("RANGE_SCREEN_CONTEXT_EARLIER_ROLE")
            checked.append(_range_screen_checked_observation(item, source_media["path"]))
        if not checked:
            raise ContractRejection("RANGE_SCREEN_CONTEXT_FRAMES_MISSING")
        return {
            "schema_version": "validated-range-screen-context-observation.v1",
            "scope": "RANGE_SOURCE_SCREEN_CONTEXT_ONLY",
            "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
            "same_cue_direct_transcript_authority": False,
            "candidate_id": candidate_id, "clip_context_sha256": observation["clip_context_sha256"],
            "cue_index": cue_index, "current_srt_sha256": current_srt_sha256,
            "original_srt": dict(original_srt), "original_srt_sha256": original_sha256,
            "related_source_media": dict(source_media), "source_media_sha256": source_sha256,
            "source_media_timeline_offset_ms": offset, "target_source_interval_ms": target,
            "range_source_interval_ms": span, "observations": checked,
            "original_observation_sha256": _range_screen_canonical_digest(observation),
        }, "RANGE_SCREEN_CONTEXT_VALIDATED_EVIDENCE_ONLY"
    except ValueError as exc:
        return None, str(exc)
    except (OSError, TypeError, KeyError, IndexError, UnicodeError, AttributeError):
        return None, "RANGE_SCREEN_CONTEXT_BINDING_INVALID"


def witness_is_weak(witness: Mapping[str, Any]) -> bool:
    """Whether the dictation itself admits it could not hear clearly."""

    if witness.get("status") != "OBSERVED" or witness.get(
        "target_audible"
    ) is not True:
        return False
    heard = str(witness.get("heard_pinyin") or "")
    tokens = heard.split()
    confidence = witness.get("confidence")
    if (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and float(confidence) < WEAK_WITNESS_CONFIDENCE
    ):
        return True
    uncertain = witness.get("uncertain_positions")
    return bool(
        tokens
        and isinstance(uncertain, list)
        and len(uncertain) >= max(2, len(tokens) * WEAK_WITNESS_UNCERTAIN_FRACTION)
    )


def _toneless_pinyin(text: str) -> list[str]:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return []
    return [
        re.sub(r"\s+", "", str(token).lower())
        for token in lazy_pinyin(text)
        if str(token).strip()
    ]


def _pinyin_ratio(heard_tokens: Sequence[str], candidate: str) -> float:
    """Best alignment of ``candidate`` against any subwindow of the dictation.

    她常把念的内容嵌在自己的句子里（「大家说的都是〈弹幕原文〉」）——
    整句比对会稀释相似度，按候选长度±1 开滑窗取最大比。
    """

    cand_tokens = _toneless_pinyin(candidate)
    if not heard_tokens or not cand_tokens:
        return 0.0
    cand_joined = " ".join(cand_tokens)
    best = 0.0
    for window in range(
        max(1, len(cand_tokens) - 2),
        min(len(heard_tokens), len(cand_tokens) + 2) + 1,
    ):
        for start in range(0, len(heard_tokens) - window + 1):
            ratio = SequenceMatcher(
                None,
                " ".join(heard_tokens[start : start + window]),
                cand_joined,
            ).ratio()
            if ratio > best:
                best = ratio
    return best


def parse_screen_text_pool(receipts: Sequence[Mapping[str, Any]]) -> list[str]:
    """Union the verbatim strings from frame-probe receipts (order-stable)."""

    pool: list[str] = []
    seen: set[str] = set()
    for receipt in receipts:
        answer = receipt.get("answer") if isinstance(receipt, Mapping) else None
        if not isinstance(answer, str):
            continue
        try:
            match = re.search(r"\{.*\}", answer, re.S)
            payload = json.loads(match.group(0)) if match else {}
        except (ValueError, AttributeError):
            continue
        for text in payload.get("texts") or []:
            cleaned = str(text).strip()
            if len(cleaned) < _MIN_TEXT_CHARS or cleaned in seen:
                continue
            seen.add(cleaned)
            pool.append(cleaned)
    return pool


def _screen_answer_valid(answer: str) -> bool:
    try:
        match = re.search(r"\{.*\}", answer, re.S)
        payload = json.loads(match.group(0)) if match else None
    except (ValueError, AttributeError, json.JSONDecodeError):
        return False
    return bool(isinstance(payload, dict) and isinstance(payload.get("texts"), list))


def match_screen_read(
    *,
    heard_pinyin: str,
    pool: Sequence[str],
) -> dict[str, Any] | None:
    """Pick the pool string she was reading, by pinyin alignment with margin."""

    heard_tokens = [t for t in str(heard_pinyin or "").lower().split() if t]
    if not heard_tokens or not pool:
        return None
    scored = sorted(
        (
            (_pinyin_ratio(heard_tokens, candidate), candidate)
            for candidate in pool
        ),
        reverse=True,
    )
    best_score, best_text = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score < _MIN_MATCH_RATIO:
        return None
    if best_score - runner_up < _MIN_MATCH_MARGIN and runner_up > 0.0:
        return None
    return {
        "text": best_text,
        "pinyin_ratio": round(best_score, 6),
        "runner_up_ratio": round(runner_up, 6),
    }


def screen_read_token_alignment(
    *,
    heard_pinyin: str,
    candidate: str,
) -> dict[str, Any]:
    """Measure ordered syllable-token support for a screen-read candidate.

    ``match_screen_read`` intentionally keeps its permissive character-level
    score for the existing structured-chat path.  A frame OCR match needs a
    second gate before it can become a spelling candidate: most candidate
    syllables must occur in order in the blind-hearing tokens.  This prevents a
    short OCR phrase from winning on a coincidental character resemblance while
    still allowing a screen phrase embedded in a longer spoken sentence.
    """

    heard_tokens = [
        token
        for token in (
            re.sub(r"[^a-z0-9]+", "", raw.lower())
            for raw in str(heard_pinyin or "").split()
        )
        if token
    ]
    candidate_tokens = [
        token
        for token in (
            re.sub(r"[^a-z0-9]+", "", raw.lower())
            for raw in _toneless_pinyin(candidate)
        )
        if token
    ]
    result: dict[str, Any] = {
        "schema_version": "screen-read-token-alignment.v1",
        "gate": "ordered_token_coverage",
        "threshold": _SCREEN_TOKEN_ALIGNMENT_THRESHOLD,
        "heard_token_count": len(heard_tokens),
        "candidate_token_count": len(candidate_tokens),
        "matched_token_count": 0,
        "token_coverage": 0.0,
        "order_preserved": False,
        "passes": False,
    }
    if not heard_tokens:
        result["reason_code"] = "NO_HEARD_PINYIN_TOKENS"
        return result
    if not candidate_tokens:
        result["reason_code"] = "NO_CANDIDATE_PINYIN_TOKENS"
        return result

    min_window = max(1, len(candidate_tokens) - 2)
    max_window = min(len(heard_tokens), len(candidate_tokens) + 2)
    if min_window > max_window:
        # A very short blind dictation cannot support a longer OCR phrase.
        # Keep this a conservative evidence-only result instead of letting an
        # empty search range reach the assertion below.
        result["reason_code"] = "HEARD_TOKEN_SEQUENCE_TOO_SHORT"
        return result

    best: tuple[float, int, int, int, bool] | None = None
    best_window: tuple[int, int] | None = None
    for window_size in range(min_window, max_window + 1):
        for start in range(0, len(heard_tokens) - window_size + 1):
            window = heard_tokens[start : start + window_size]
            matcher = SequenceMatcher(None, window, candidate_tokens, autojunk=False)
            blocks = matcher.get_matching_blocks()
            matched = sum(block.size for block in blocks)
            coverage = matched / len(candidate_tokens)
            pairs = [
                (window_index + offset, candidate_index + offset)
                for window_index, candidate_index, size in blocks
                if size
                for offset in range(size)
            ]
            ordered = bool(pairs) and all(
                left[0] < right[0] and left[1] < right[1]
                for left, right in zip(pairs, pairs[1:])
            )
            score = (
                coverage,
                matched,
                -abs(window_size - len(candidate_tokens)),
                -start,
                ordered,
            )
            if best is None or score > best:
                best = score
                best_window = (start, start + window_size)

    assert best is not None
    coverage, matched, _distance, _start, ordered = best
    result.update(
        {
            "matched_token_count": matched,
            "token_coverage": round(coverage, 6),
            "order_preserved": ordered,
            "passes": bool(
                ordered and coverage >= _SCREEN_TOKEN_ALIGNMENT_THRESHOLD
            ),
        }
    )
    if best_window is not None:
        result["heard_window"] = list(best_window)
    result["reason_code"] = (
        "ORDERED_TOKEN_COVERAGE_PASS"
        if result["passes"]
        else "ORDERED_TOKEN_COVERAGE_BELOW_THRESHOLD"
    )
    return result


def make_screen_read_probe(
    *,
    media_path: Any,
    api_base: str,
    api_key: str,
    frame_probe: Callable[..., Mapping[str, Any]] | None = None,
    prior_receipts: Sequence[Mapping[str, Any]] = (),
) -> Callable[[int, int], dict[str, Any]]:
    """Build a bounded two-frame screen-text prober for one media file."""

    receipt_pool = [deepcopy(dict(row)) for row in prior_receipts]

    def existing_receipt(frame_ms: int) -> dict[str, Any] | None:
        from src.autoslice.cpa_frame_witness import DEFAULT_FRAME_MODEL, _extract_frame_jpeg

        candidates = [row for row in receipt_pool if (
            row.get("schema_version") == "cpa-frame-witness.v1"
            and row.get("status") == "OBSERVED"
            and row.get("provider") == "cpa"
            and row.get("model") == DEFAULT_FRAME_MODEL
            and row.get("media_path") == str(media_path)
            and row.get("frame_ms") == frame_ms
            and row.get("question") == SCREEN_TEXT_QUESTION
            and not row.get("reference_image")
            and _screen_answer_valid(str(row.get("answer") or ""))
        )]
        if not candidates:
            return None
        try:
            frame_sha = hashlib.sha256(_extract_frame_jpeg(Path(media_path), frame_ms)).hexdigest()
        except (OSError, subprocess.SubprocessError):
            return None
        prompt_sha = hashlib.sha256(json.dumps({
            "question": SCREEN_TEXT_QUESTION,
            "frame_sha256": frame_sha,
            "model": DEFAULT_FRAME_MODEL,
        }, sort_keys=True).encode()).hexdigest()
        for row in candidates:
            if row.get("frame_sha256") == frame_sha and row.get("prompt_sha256") == prompt_sha:
                return deepcopy(row)
        return None

    def probe(span_start_ms: int, span_end_ms: int) -> dict[str, Any]:
        probe_kwargs: dict[str, Any] = {
            "api_base": api_base,
            "api_key": api_key,
        }
        if frame_probe is None:
            from src.autoslice.visual_witness import frame_vision_probe

            probe_kwargs["answer_validator"] = _screen_answer_valid
        else:
            frame_vision_probe = frame_probe  # type: ignore[assignment]
        duration = max(0, int(span_end_ms) - int(span_start_ms))
        sample_ms = sorted(
            {
                int(span_start_ms) + duration // 3,
                int(span_start_ms) + (2 * duration) // 3,
            }
        )
        receipts = []
        reused_frames = []
        for ms in sample_ms:
            receipt = existing_receipt(ms) if frame_probe is None else None
            if receipt is not None:
                reused_frames.append(ms)
            else:
                receipt = dict(frame_vision_probe(
                    media_path,
                    ms,
                    SCREEN_TEXT_QUESTION,
                    **probe_kwargs,
                ))
                receipt_pool.append(deepcopy(receipt))
            receipts.append(receipt)
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": str(media_path),
            "span_start_ms": int(span_start_ms),
            "span_end_ms": int(span_end_ms),
            "frame_ms": sample_ms,
            "receipts": [dict(row) for row in receipts],
            "reused_frame_ms": reused_frames,
            "pool": parse_screen_text_pool(receipts),
        }

    return probe


def locate_read_span(
    cue_text: str,
    candidate: str,
) -> tuple[int, int, float] | None:
    """Locate the character span of ``cue_text`` she used to read ``candidate``.

    嵌入式念读（「大家说的都是〈弹幕原文〉」）：在 cue 的逐字拼音序列上
    按候选长度±1 开滑窗，取拼音相似度最高的字符窗；命中返回
    (start_cp, end_cp, ratio)。CJK 一字一音节的近似在此场景成立；含
    拉丁段的 cue 由 lazy_pinyin 原样保留 token，窗口仍按 token 对齐。
    """

    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        return None
    chars = list(cue_text)
    if not chars:
        return None
    char_tokens = [
        re.sub(r"\s+", "", str(token).lower())
        for token in lazy_pinyin(cue_text)
    ]
    if len(char_tokens) != len(chars):
        # token↔字符不可逐位对齐（连写拉丁等）：退化为整 cue 替换判定
        ratio = _pinyin_ratio(
            [t for t in char_tokens if t], candidate
        )
        return (0, len(chars), ratio) if ratio > 0 else None
    cand_tokens = _toneless_pinyin(candidate)
    if not cand_tokens:
        return None
    cand_joined = " ".join(cand_tokens)
    best: tuple[int, int, float] | None = None
    for window in range(
        max(1, len(cand_tokens) - 2),
        min(len(chars), len(cand_tokens) + 2) + 1,
    ):
        for start in range(0, len(chars) - window + 1):
            ratio = SequenceMatcher(
                None,
                " ".join(char_tokens[start : start + window]),
                cand_joined,
            ).ratio()
            if best is None or ratio > best[2]:
                best = (start, start + window, ratio)
    return best


def danmaku_text_pool(
    clip_context: Mapping[str, Any] | None,
) -> list[str]:
    """结构化弹幕/SC 文本池（维护者 扩展：查证先弹幕后画面）。

    语境不通的 finding 里，她念的可能是弹幕原文——池按拼音对齐挑选，
    命中走 structured_chat_bound 出处。零外部调用。
    """

    if not isinstance(clip_context, Mapping):
        return []
    rows = clip_context.get("structured_chat")
    if not isinstance(rows, list):
        return []
    pool: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        text = str(row.get("text") or row.get("message") or "").strip()
        if len(text) < _MIN_TEXT_CHARS or text in seen:
            continue
        seen.add(text)
        pool.append(text)
    return pool


__all__ = [
    "SCREEN_TEXT_QUESTION",
    "danmaku_text_pool",
    "make_screen_read_probe",
    "match_screen_read",
    "parse_screen_text_pool",
    "screen_read_token_alignment",
    "validate_range_screen_context_observation",
    "witness_is_weak",
]


def build_env_screen_read_probe(media_path: Any, *, prior_observations: object = None):
    """CPA-primary 视觉读屏探针；AGY 仅作后备。"""

    import os
    import shutil
    from pathlib import Path

    from src.autoslice.llm_client import (
        LlmRuntimeEnvironmentError,
        runtime_cpa_command_environment,
    )
    from src.autoslice.producer_final_review_transport import _resolved_runtime_root

    agy_bin = os.environ.get(
        "AGY_BIN",
        str(Path.home() / ".local" / "bin" / "agy"),
    )
    api_base = os.environ.get("CPA_BASE_URL", "").strip()
    api_key = os.environ.get("CPA_API_KEY", "").strip()
    if not (api_base and api_key):
        try:
            runtime = _resolved_runtime_root(None)
            env = runtime_cpa_command_environment(runtime) if runtime is not None else {}
            api_base = env.get("CPA_BASE_URL", "").strip()
            api_key = env.get("CPA_API_KEY", "").strip()
        except (LlmRuntimeEnvironmentError, OSError, ValueError):
            api_base, api_key = "", ""
    agy_available = bool(Path(agy_bin).is_file() or shutil.which(agy_bin))
    if not Path(media_path).is_file() or not (
        (api_base and api_key) or agy_available
    ):
        return None
    prior_receipts = []

    def collect(value: object) -> None:
        if isinstance(value, Mapping):
            if value.get("schema_version") == "cpa-frame-witness.v1":
                prior_receipts.append(value)
            else:
                for child in value.values():
                    collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(prior_observations)
    return make_screen_read_probe(
        media_path=media_path,
        api_base=api_base,
        api_key=api_key,
        prior_receipts=prior_receipts,
    )
