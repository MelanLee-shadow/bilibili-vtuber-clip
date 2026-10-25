"""Keep prior CPA repair evidence when a later native review starts a new run."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json


HISTORY_KEY = "retained_previous_runs"
RUN_SCHEMA = "retained-exact-final-cpa-self-heal-run.v1"
CAPTION_SUCCESSORS_KEY = "caption_display_successors"
SOURCE_EXTENSION_SUCCESSORS_KEY = "source_extension_successors"
NESTED_CAPTION_SUCCESSORS_KEY = "nested_caption_display_successors"


OVERLAP_TIMING_SUCCESSORS_KEY = "overlap_timing_successors"
RELEASE_GRADE_MERGE_SUCCESSORS_KEY = "release_grade_merge_successors"
RELEASE_GRADE_MERGE_SCHEMA = "exact-final-release-grade-merge-successor.v1"
JAPANESE_DISPLAY_SUCCESSORS_KEY = "japanese_display_successors"

# These reasons describe different failed contracts, not recovery instructions.
FINAL_IDENTITY_MISMATCH = "EXACT_FINAL_CPA_SELF_HEAL_FINAL_HASH_MISMATCH"
CHAIN_TERMINAL_MISMATCH = "EXACT_FINAL_CPA_SELF_HEAL_CHAIN_TERMINAL_MISMATCH"
RECOVERY_MATERIAL_MISSING = "EXACT_FINAL_SOURCE_EXTENSION_RECOVERY_MATERIAL_MISSING"
RECOVERY_TYPE_INAPPLICABLE = "EXACT_FINAL_SOURCE_EXTENSION_RECOVERY_TYPE_INAPPLICABLE"
RECOVERY_PREFIX_INVALID = "EXACT_FINAL_SOURCE_EXTENSION_PREFIX_INVALID"


def _text_sha(text: str) -> str:
    from src.autoslice.evidence_identity import sha256_ref

    return sha256_ref(hashlib.sha256(text.encode("utf-8")).hexdigest())


def _merge_cue_mapping(before: str, after: str) -> list[dict]:
    """Identify originals by their actual windows, never a recycled ordinal.

    The merge owner has already produced and authenticated the output bytes.
    Each surviving cue contains the full windows of its contributing originals.
    No merging, punctuation or gap rule is defined here.
    """
    from src.autoslice.jingting_chunker import parse_srt_cues
    old, new = parse_srt_cues(before), parse_srt_cues(after)
    result = []
    for cue in old:
        matches = [c for c in new if c.start_ms <= cue.start_ms < cue.end_ms <= c.end_ms]
        if len(matches) != 1:
            raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
        mapped = matches[0]
        result.append({
            "input_cue_index": cue.index, "output_cue_index": mapped.index,
            "input_window_ms": [cue.start_ms, cue.end_ms],
            "output_window_ms": [mapped.start_ms, mapped.end_ms],
            "input_text": cue.text, "output_text": mapped.text,
        })
    if not result or {r["output_cue_index"] for r in result} != {c.index for c in new}:
        raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
    return result


def release_grade_merge_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    """Replay the release validator's own merger against the literal SRT bytes."""
    from src.autoslice.cue_split_hygiene import merge_release_grade_cues
    rows, passes = receipt.get(RELEASE_GRADE_MERGE_SUCCESSORS_KEY, []), receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
    result: dict[int, list[dict]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
        index = row.get("after_pass_index")
        before, after = row.get("input_srt_text"), row.get("output_srt_text")
        if (type(index) is not int or not 1 <= index <= len(passes)
                or row.get("schema_version") != RELEASE_GRADE_MERGE_SCHEMA
                or row.get("status") != "DRAFT_REQUIRES_COMPLETE_REVIEW"
                or row.get("release_gate") != "BLOCK"
                or row.get("publication_authority") is not False
                or not isinstance(before, str) or not isinstance(after, str) or before == after):
            raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
        expected, report = merge_release_grade_cues(before)
        if (expected != after or report != row.get("merge_report")
                or row.get("input_srt_sha256") != _text_sha(before)
                or row.get("output_srt_sha256") != _text_sha(after)
                or row.get("cue_mapping") != _merge_cue_mapping(before, expected)):
            raise ValueError("EXACT_FINAL_RELEASE_GRADE_MERGE_SUCCESSOR_INVALID")
        result.setdefault(index, []).append(dict(row))
    return result


def with_release_grade_merge_successor(previous: Mapping[str, object], *,
                                       before: str, after: str, merge_report: list) -> dict:
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": previous},
        expected_srt_sha256=_text_sha(before), allow_pending=True,
    )
    result = deepcopy(previous)
    result["status"] = "REVIEW_PENDING"
    result.pop("final_srt_sha256", None)
    result.setdefault(RELEASE_GRADE_MERGE_SUCCESSORS_KEY, []).append({
        "schema_version": RELEASE_GRADE_MERGE_SCHEMA,
        "status": "DRAFT_REQUIRES_COMPLETE_REVIEW", "release_gate": "BLOCK",
        "publication_authority": False, "after_pass_index": len(previous["passes"]),
        "input_srt_text": before, "output_srt_text": after,
        "input_srt_sha256": _text_sha(before), "output_srt_sha256": _text_sha(after),
        "merge_report": deepcopy(merge_report), "cue_mapping": _merge_cue_mapping(before, after),
    })
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": result},
        expected_srt_sha256=_text_sha(after), allow_pending=True,
    )
    return result


def overlap_timing_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    """Replay only end clamps at the next cue's unchanged start, never retiming speech."""
    from src.autoslice.jingting_chunker import parse_srt_cues, _format_srt_time

    rows, passes = receipt.get(OVERLAP_TIMING_SUCCESSORS_KEY, []), receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
    result: dict[int, list[dict]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
        index = row.get("after_pass_index")
        before, after = row.get("input_srt_text"), row.get("output_srt_text")
        if (type(index) is not int or not 1 <= index <= len(passes)
                or row.get("schema_version") != "exact-final-overlap-timing-successor.v1"
                or row.get("status") != "DRAFT_REQUIRES_COMPLETE_REVIEW"
                or row.get("release_gate") != "BLOCK"
                or row.get("publication_authority") is not False
                or not isinstance(before, str) or not isinstance(after, str) or before == after):
            raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
        old, new = parse_srt_cues(before), parse_srt_cues(after)
        if not old or len(old) != len(new):
            raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
        changed = []
        expected_blocks = []
        for i, (a, b) in enumerate(zip(old, new)):
            end = min(a.end_ms, old[i + 1].start_ms) if i + 1 < len(old) else a.end_ms
            if (end <= a.start_ms or (a.index, a.start_ms, end, a.text)
                    != (b.index, b.start_ms, b.end_ms, b.text)):
                raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
            expected_blocks.append(f"{a.index}\n{_format_srt_time(a.start_ms)} --> {_format_srt_time(end)}\n{a.text}")
            if a.end_ms != end:
                changed.append({"cue_index": a.index, "start_ms": a.start_ms,
                                "before_end_ms": a.end_ms, "after_end_ms": end})
        expected_text = "\n\n".join(expected_blocks) + "\n"
        if (not changed or after != expected_text or row.get("end_clamps") != changed
                or row.get("input_srt_sha256") != "sha256:" + hashlib.sha256(before.encode()).hexdigest()
                or row.get("output_srt_sha256") != "sha256:" + hashlib.sha256(after.encode()).hexdigest()):
            raise ValueError("EXACT_FINAL_OVERLAP_TIMING_SUCCESSOR_INVALID")
        result.setdefault(index, []).append(dict(row))
    return result


def with_overlap_timing_successor(previous: Mapping[str, object], before: str, after: str) -> dict:
    """Preserve actual typed repairs and reopen complete review after an overlap clamp."""
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    from src.autoslice.jingting_chunker import parse_srt_cues

    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": previous},
        expected_srt_sha256="sha256:" + hashlib.sha256(before.encode()).hexdigest(), allow_pending=True,
    )
    old, new = parse_srt_cues(before), parse_srt_cues(after)
    result = deepcopy(previous)
    result["status"] = "REVIEW_PENDING"
    result.pop("final_srt_sha256", None)
    result.setdefault(OVERLAP_TIMING_SUCCESSORS_KEY, []).append({
        "schema_version": "exact-final-overlap-timing-successor.v1",
        "status": "DRAFT_REQUIRES_COMPLETE_REVIEW", "release_gate": "BLOCK",
        "publication_authority": False, "after_pass_index": len(previous["passes"]),
        "input_srt_text": before, "output_srt_text": after,
        "input_srt_sha256": "sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        "output_srt_sha256": "sha256:" + hashlib.sha256(after.encode()).hexdigest(),
        "end_clamps": [{"cue_index": a.index, "start_ms": a.start_ms,
                        "before_end_ms": a.end_ms, "after_end_ms": b.end_ms}
                       for a, b in zip(old, new) if a.end_ms != b.end_ms],
    })
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": result},
        expected_srt_sha256="sha256:" + hashlib.sha256(after.encode()).hexdigest(), allow_pending=True,
    )
    return result


def nested_caption_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    """Replay only the normal, source/frame-bound whole-caption display filter."""
    from pathlib import Path
    from src.autoslice.nested_media_caption_dedup import apply_bound_display_policy

    rows, passes = receipt.get(NESTED_CAPTION_SUCCESSORS_KEY, []), receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID")
    result: dict[int, list[dict]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID")
        index, before = row.get("after_pass_index"), row.get("input_srt_text")
        interval = row.get("source_interval_ms")
        if (type(index) is not int or not 1 <= index <= len(passes)
                or not isinstance(before, str)
                or row.get("schema_version") != "exact-final-nested-caption-display-successor.v1"
                or row.get("status") != "DRAFT_REQUIRES_COMPLETE_REVIEW"
                or row.get("release_gate") != "BLOCK"
                or row.get("publication_authority") is not False
                or not isinstance(interval, list) or len(interval) != 2
                or any(type(value) is not int for value in interval)
                or not 0 <= interval[0] < interval[1]
                or not isinstance(row.get("source_media"), str)
                or not isinstance(row.get("candidate_id"), str)):
            raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID")
        try:
            after, audit = apply_bound_display_policy(
                config=row.get("caption_config"), candidate_id=row["candidate_id"],
                source_media=Path(row["source_media"]), transcript=before,
                source_start_ms=interval[0], source_end_ms=interval[1],
                evidence_root=Path(row["evidence_root"]),
            )
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID") from exc
        if (before == after or audit != row.get("dedup_audit")
                or row.get("input_srt_sha256") != "sha256:" + hashlib.sha256(before.encode()).hexdigest()
                or row.get("output_srt_sha256") != "sha256:" + hashlib.sha256(after.encode()).hexdigest()):
            raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID")
        result.setdefault(index, []).append({**row, "output_srt_text": after})
    return result


def with_nested_caption_successor(
    previous: Mapping[str, object], *, before: str, after: str,
    config: Mapping[str, object], candidate_id: str, source_media,
    source_start_ms: int, source_end_ms: int, evidence_root,
) -> dict:
    """Retain typed repairs when independent source captions filter the display."""
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    from src.autoslice.nested_media_caption_dedup import apply_bound_display_policy

    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": previous},
        expected_srt_sha256="sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        allow_pending=True,
    )
    expected, audit = apply_bound_display_policy(
        config=config, candidate_id=candidate_id, source_media=source_media,
        transcript=before, source_start_ms=source_start_ms, source_end_ms=source_end_ms,
        evidence_root=evidence_root,
    )
    if before == after or after != expected:
        raise ValueError("EXACT_FINAL_NESTED_CAPTION_SUCCESSOR_INVALID")
    result = deepcopy(previous)
    result["status"] = "REVIEW_PENDING"
    result.pop("final_srt_sha256", None)
    result.setdefault(NESTED_CAPTION_SUCCESSORS_KEY, []).append({
        "schema_version": "exact-final-nested-caption-display-successor.v1",
        "status": "DRAFT_REQUIRES_COMPLETE_REVIEW", "release_gate": "BLOCK",
        "publication_authority": False, "after_pass_index": len(previous["passes"]),
        "input_srt_text": before,
        "input_srt_sha256": "sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        "output_srt_sha256": "sha256:" + hashlib.sha256(after.encode()).hexdigest(),
        "candidate_id": candidate_id, "source_media": str(source_media),
        "source_interval_ms": [source_start_ms, source_end_ms],
        "evidence_root": str(evidence_root), "caption_config": dict(config),
        "dedup_audit": audit,
    })
    _validate_exact_final_cpa_self_heal(
        {"exact_final_cpa_self_heal": result},
        expected_srt_sha256="sha256:" + hashlib.sha256(after.encode()).hexdigest(),
        allow_pending=True,
    )
    return result


def source_extension_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    """Replay draft-only source additions without changing prior CPA repairs."""
    from dataclasses import replace
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_boundary_resolution import _current_delivery_source_extension

    rows = receipt.get(SOURCE_EXTENSION_SUCCESSORS_KEY, [])
    passes = receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
    result: dict[int, list[dict]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        index = row.get("after_pass_index")
        before, after, source = (row.get(k) for k in
                                 ("input_srt_text", "output_srt_text", "source_srt_text"))
        boundary = row.get("boundary_audit")
        if (type(index) is not int or not 1 <= index <= len(passes)
                or row.get("schema_version") != "exact-final-source-extension-successor.v1"
                or row.get("status") != "DRAFT_REQUIRES_COMPLETE_REVIEW"
                or row.get("release_gate") != "BLOCK"
                or row.get("mutation_authorized") is not False
                or not all(isinstance(t, str) for t in (before, after, source))
                or before == after or not isinstance(boundary, Mapping)):
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        for text, key in ((before, "input_srt_sha256"), (after, "output_srt_sha256"),
                          (source, "source_srt_sha256")):
            if row.get(key) != "sha256:" + hashlib.sha256(text.encode()).hexdigest():
                raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        old, new, source_cues = map(parse_srt_cues, (before, after, source))
        start, end = boundary.get("final_start_ms"), boundary.get("final_end_ms")
        projection = boundary.get("current_delivery_source_projection", {})
        if (type(start) is not int or type(end) is not int or end <= start
                or projection.get("offset_ms") != start or not old or not source_cues):
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        projected = [replace(c, start_ms=c.start_ms + start, end_ms=c.end_ms + start) for c in old]
        closures = [c for c in source_cues if c.end_ms == boundary.get("snapped_sentence_end_ms")]
        if len(closures) != 1:
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        try:
            combined, binding = _current_delivery_source_extension(
                source_cues=source_cues, current_delivery_cues=projected,
                current_delivery_projection=projection,
                semantic_review=boundary.get("boundary_semantic_review"),
                closure_cue=closures[0], snapped_end_ms=closures[0].end_ms,
                final_start_ms=start, final_end_ms=end,
                manual_end_mode=boundary.get("manual_end_mode", "semantic_lower_bound"),
            )
        except (SystemExit, ValueError, TypeError, KeyError) as exc:
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID") from exc
        expected = [(str(i), c.start_ms - start, c.end_ms - start, c.text)
                    for i, c in enumerate(combined or [], 1)]
        actual = [(c.index, c.start_ms, c.end_ms, c.text) for c in new]
        # The normal timing stage can lengthen a new flash cue to 1 second.
        # Preserve every old cue exactly; accept this deterministic tail-only
        # change only with the actual timing action, never arbitrary retiming.
        timing = row.get("timing_qa", {})
        if (isinstance(timing, Mapping)
                and timing.get("schema_version") == "subtitle-timing-qa.v3"
                and timing.get("window") == {"start_ms": start, "end_ms": end}
                and timing.get("policy", {}).get("min_readable_ms") == 1000):
            for i in range(len(old), len(expected)):
                cue = combined[i]
                limit = min(end, combined[i + 1].start_ms) if i + 1 < len(combined) else end
                target = min(cue.start_ms + 1000, limit)
                actions = [a for a in timing.get("actions", []) if isinstance(a, Mapping)
                           and a.get("action") == "retime" and a.get("reasons") == ["flash_extended"]
                           and a.get("text") == cue.text
                           and a.get("before") == {"start_ms": cue.start_ms, "end_ms": cue.end_ms}
                           and a.get("after") == {"start_ms": cue.start_ms, "end_ms": cue.start_ms + 1000}]
                if 0 < cue.end_ms - cue.start_ms < 1000 and len(actions) == 1:
                    expected[i] = (str(i + 1), cue.start_ms - start, target - start, cue.text)
        if (binding is None or binding != boundary.get("current_delivery_closure_binding")
                or expected != actual or len(new) <= len(old)):
            raise ValueError("EXACT_FINAL_SOURCE_EXTENSION_INVALID")
        result.setdefault(index, []).append(dict(row))
    return result


def caption_successors(receipt: Mapping[str, object]) -> dict[int, dict]:
    """Replay registered display changes between actual CPA repair passes."""
    from src.autoslice.surface_canon import normalize_caption_display_surfaces
    from src.autoslice.jingting_chunker import parse_srt_cues

    rows = receipt.get(CAPTION_SUCCESSORS_KEY, [])
    passes = receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_CAPTION_SUCCESSOR_INVALID")
    result = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_CAPTION_SUCCESSOR_INVALID")
        index, before = row.get("after_pass_index"), row.get("input_srt_text")
        if (type(index) is not int or not 1 <= index <= len(passes)
                or index in result or not isinstance(before, str)
                or row.get("schema_version") != "exact-final-caption-display-successor.v1"):
            raise ValueError("EXACT_FINAL_CAPTION_SUCCESSOR_INVALID")
        after, audit = normalize_caption_display_surfaces(before)
        old_cues, new_cues = parse_srt_cues(before), parse_srt_cues(after)
        def grid(cues):
            return [(c.index, c.start_ms, c.end_ms) for c in cues]
        if (before == after or grid(old_cues) != grid(new_cues)
                or row.get("normalization_audit") != audit
                or row.get("input_srt_sha256") != "sha256:" + hashlib.sha256(before.encode()).hexdigest()
                or row.get("output_srt_sha256") != "sha256:" + hashlib.sha256(after.encode()).hexdigest()):
            raise ValueError("EXACT_FINAL_CAPTION_SUCCESSOR_INVALID")
        result[index] = {**row, "output_srt_text": after}
    return result


def with_caption_successor(previous: Mapping[str, object], before: str, after: str) -> dict:
    """Keep applied CPA receipts and reopen review for a deterministic display change."""
    from src.autoslice.surface_canon import normalize_caption_display_surfaces

    expected, audit = normalize_caption_display_surfaces(before)
    if expected != after or before == after:
        raise ValueError("EXACT_FINAL_CAPTION_SUCCESSOR_INVALID")
    result = deepcopy(previous)
    result["status"] = "REVIEW_PENDING"
    result.pop("final_srt_sha256", None)
    result.setdefault(CAPTION_SUCCESSORS_KEY, []).append({
        "schema_version": "exact-final-caption-display-successor.v1",
        "after_pass_index": len(previous["passes"]),
        "input_srt_text": before,
        "input_srt_sha256": "sha256:" + hashlib.sha256(before.encode()).hexdigest(),
        "output_srt_sha256": "sha256:" + hashlib.sha256(after.encode()).hexdigest(),
        "normalization_audit": audit,
    })
    caption_successors(result)
    return result


def japanese_display_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    from src.autoslice.surface_canon import normalize_japanese_native_script_surfaces
    rows, passes = receipt.get(JAPANESE_DISPLAY_SUCCESSORS_KEY, []), receipt.get("passes", [])
    if not isinstance(rows, list) or not isinstance(passes, list):
        raise ValueError("EXACT_FINAL_JAPANESE_DISPLAY_SUCCESSOR_INVALID")
    result: dict[int, list[dict]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("EXACT_FINAL_JAPANESE_DISPLAY_SUCCESSOR_INVALID")
        index, before, after = (row.get(k) for k in ("after_pass_index", "input_srt_text", "output_srt_text"))
        if (type(index) is not int or not 1 <= index <= len(passes)
                or row.get("schema_version") != "exact-final-japanese-display-successor.v1"
                or row.get("status") != "DRAFT_REQUIRES_COMPLETE_REVIEW"
                or row.get("release_gate") != "BLOCK" or row.get("publication_authority") is not False
                or not isinstance(before, str) or not isinstance(after, str) or before == after):
            raise ValueError("EXACT_FINAL_JAPANESE_DISPLAY_SUCCESSOR_INVALID")
        expected, report = normalize_japanese_native_script_surfaces(before)
        if (after != expected or row.get("normalization_audit") != report
                or row.get("input_srt_sha256") != _text_sha(before)
                or row.get("output_srt_sha256") != _text_sha(after)):
            raise ValueError("EXACT_FINAL_JAPANESE_DISPLAY_SUCCESSOR_INVALID")
        result.setdefault(index, []).append(dict(row))
    return result


def with_japanese_display_successor(previous: Mapping[str, object], before: str, after: str) -> dict:
    from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
    from src.autoslice.surface_canon import normalize_japanese_native_script_surfaces
    _validate_exact_final_cpa_self_heal({"exact_final_cpa_self_heal": previous},
        expected_srt_sha256=_text_sha(before), allow_pending=True)
    _, report = normalize_japanese_native_script_surfaces(before)
    result = deepcopy(previous)
    result["status"] = "REVIEW_PENDING"
    result.pop("final_srt_sha256", None)
    result.setdefault(JAPANESE_DISPLAY_SUCCESSORS_KEY, []).append({
        "schema_version": "exact-final-japanese-display-successor.v1",
        "status": "DRAFT_REQUIRES_COMPLETE_REVIEW", "release_gate": "BLOCK", "publication_authority": False,
        "after_pass_index": len(previous["passes"]), "input_srt_text": before, "output_srt_text": after,
        "input_srt_sha256": _text_sha(before), "output_srt_sha256": _text_sha(after),
        "normalization_audit": report,
    })
    _validate_exact_final_cpa_self_heal({"exact_final_cpa_self_heal": result},
        expected_srt_sha256=_text_sha(after), allow_pending=True)
    return result


# One registry owns the complete type set for writers and cold consumers.
SUCCESSOR_READERS = {
    CAPTION_SUCCESSORS_KEY: caption_successors,
    SOURCE_EXTENSION_SUCCESSORS_KEY: source_extension_successors,
    NESTED_CAPTION_SUCCESSORS_KEY: nested_caption_successors,
    OVERLAP_TIMING_SUCCESSORS_KEY: overlap_timing_successors,
    RELEASE_GRADE_MERGE_SUCCESSORS_KEY: release_grade_merge_successors,
    JAPANESE_DISPLAY_SUCCESSORS_KEY: japanese_display_successors,
}


def successor_payload(receipt: Mapping[str, object]) -> dict[str, list]:
    """Copy registered fields without teaching each writer the type set."""
    result = {}
    for key in SUCCESSOR_READERS:
        rows = receipt.get(key, [])
        if not isinstance(rows, list):
            raise ValueError("EXACT_FINAL_SUCCESSOR_COLLECTION_INVALID")
        if rows:
            result[key] = deepcopy(rows)
    return result


def read_successors(receipt: Mapping[str, object]) -> dict[int, list[dict]]:
    """Return independently replayed typed edges grouped by their owning pass."""
    result: dict[int, list[dict]] = {}
    for reader in SUCCESSOR_READERS.values():
        for index, rows in reader(receipt).items():
            result.setdefault(index, []).extend(rows if isinstance(rows, list) else [rows])
    return result


def ordered_successors(rows: list[dict], input_sha256: str) -> list[dict]:
    """Consume every edge exactly once; reject gaps, forks and duplicate edges."""
    pending, result = list(rows), []
    while pending:
        matches = [r for r in pending if r["input_srt_sha256"] == input_sha256]
        if len(matches) != 1:
            raise ValueError("EXACT_FINAL_DISPLAY_SUCCESSOR_BINDING_MISMATCH")
        row = matches[0]
        pending.remove(row)
        result.append(row)
        input_sha256 = row["output_srt_sha256"]
    return result


def _sha(value: object) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def retained_runs(previous: object) -> list[dict[str, object]]:
    """Flatten only completed runs; their final SRT remains historical."""
    if not isinstance(previous, Mapping):
        return []
    retained = previous.get(HISTORY_KEY, [])
    if not isinstance(retained, list):
        raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
    result = deepcopy(retained)
    if previous.get("status") == "PASS":
        body = {str(k): deepcopy(v) for k, v in previous.items() if k != HISTORY_KEY}
        entry = {"schema_version": RUN_SCHEMA, "audit": body, "audit_sha256": _sha(body)}
        if not any(row == entry for row in result):
            result.append(entry)
    # Validate through the same reader used by package acceptance.
    repair_history({"passes": [], HISTORY_KEY: result})
    return result


def repair_history(current: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return actual receipts; reject a malformed or rehashed partial history."""
    runs: list[Mapping[str, object]] = [current]
    history = current.get(HISTORY_KEY, [])
    if not isinstance(history, list):
        raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
    seen: set[str] = set()
    for entry in history:
        if not isinstance(entry, Mapping) or set(entry) != {"schema_version", "audit", "audit_sha256"}:
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        audit = entry["audit"]
        digest = entry["audit_sha256"]
        if not (
            entry["schema_version"] == RUN_SCHEMA
            and isinstance(audit, Mapping)
            and audit.get("schema_version") == "exact-final-cpa-self-heal-audit.v1"
            and audit.get("status") == "PASS"
            and isinstance(audit.get("final_srt_sha256"), str)
            and len(audit["final_srt_sha256"]) == 71
            and HISTORY_KEY not in audit
            and digest == _sha(audit)
            and digest not in seen
        ):
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        seen.add(str(digest))
        runs.append(audit)
    receipts: list[Mapping[str, object]] = []
    for run in runs:
        passes = run.get("passes")
        if not isinstance(passes, list):
            raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
        for row in passes:
            repairs = row.get("repairs") if isinstance(row, Mapping) else None
            if not isinstance(repairs, list) or any(not isinstance(r, Mapping) for r in repairs):
                raise ValueError("EXACT_FINAL_CPA_HISTORY_INVALID")
            receipts.extend(repairs)
    return receipts


def review_input_of_pending_run(receipt: object) -> str | None:
    """Bytes reviewed by the review that produced a pending run's terminal pass.

    The ordinary exact-final writer (``_run_exact_final_review_gate_body``)
    commits one pass as a unit: the pass receipt whose input is the reviewed
    bytes, the new active bytes, and the unchanged review that produced the
    pass.  Reviewing the new bytes is the next ordinary step, so an interrupted
    run legitimately persists ``final_review_audit.reviewed_srt_sha256`` equal
    to this value while its active bytes are the run's terminal output (plus
    registered successors).  This is the single definition of that writer
    state; the cold continuation reader accepts exactly it, and only as review
    input (the chain itself is validated separately against the active bytes).
    """
    if not isinstance(receipt, Mapping) or receipt.get("status") != "REVIEW_PENDING":
        return None
    passes = receipt.get("passes")
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
        return None
    value = passes[-1].get("input_srt_sha256")
    return value if isinstance(value, str) and len(value) == 71 else None


def build_run_audit(
    passes: list, previous_runs: list, *, final_srt_sha256: str | None = None,
    successors: Mapping[str, object] | None = None, **successor_fields,
) -> dict:
    """Build current-run state while preserving independently completed runs."""
    if set(successor_fields) - SUCCESSOR_READERS.keys():
        raise TypeError("Unknown exact-final successor type")
    fields = {**(successors or {}), **{k: v for k, v in successor_fields.items() if v is not None}}
    return {
        "schema_version": "exact-final-cpa-self-heal-audit.v1",
        "status": "PASS" if final_srt_sha256 is not None else "REVIEW_PENDING",
        "passes": passes,
        **({"final_srt_sha256": final_srt_sha256} if final_srt_sha256 is not None else {}),
        **({HISTORY_KEY: previous_runs} if previous_runs else {}),
        **successor_payload(fields),
    }
