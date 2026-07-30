"""Text-final SRT rendering and authority-surface verification."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .chat_authority import (
    _fragment_spoken_in,
    _strip_interjections_once,
    canonicalize_hard_meme_surfaces,
    normalize_chat_text,
    normalize_srt_payload_window,
)
from .chat_evidence import normalize_srt_owner_payload_window
from .jingting_chunker import parse_srt_cues
from .redelivery_subtitle_baseline import MIN_ALIGNMENT_OVERLAP_MS
from .source_subtitle_truth import (
    MIN_CUE_OVERLAP_MS,
    source_truth_owner_windows,
    validated_source_truth_projection,
)

FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_MS = 250
FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_RATIO = 0.1
FINAL_AUTHORITY_SCOPE_REJECTED_STRADDLER_MAX_MS = 500
FINAL_AUTHORITY_SCOPE_REJECTED_STRADDLER_MAX_RATIO = 0.15
FINAL_RELEASE_GRADE_MERGE_MAX_GAP_MS = 150


def _outside_delivery_edge_fragment_reason(
    row: Mapping[str, object],
    *,
    matched_start_ms: int,
    matched_end_ms: int,
    delivery_start_ms: int,
    delivery_end_ms: int,
    overlap_ms: int,
    overlap_ratio: float,
) -> str | None:
    """Classify a non-owner correction fragment at a delivery edge."""

    crosses_delivery_edge = (
        matched_start_ms < delivery_start_ms < matched_end_ms
        or matched_start_ms < delivery_end_ms < matched_end_ms
    )
    boundary_sliver = (
        overlap_ms <= FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_MS
        and (
            crosses_delivery_edge
            or overlap_ratio <= FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_RATIO
        )
    )
    if boundary_sliver:
        return "BOUNDARY_SLIVER_BELOW_MEANINGFUL_AUDIO_THRESHOLD"

    # A padded-context correction can be rejected as a boundary owner while
    # still leaving a small geometric fragment in the final interval
    # (2026-07-22 1863: 340/2840ms).  The typed rejection plus two caps keeps
    # this narrower than the generic sliver rule; materially retained content
    # and every story owner still need final-surface survival.
    if (
        crosses_delivery_edge
        and row.get("boundary_required") is False
        and row.get("boundary_owner_rejection")
        == "STRADDLES_IMMUTABLE_STORY_SCOPE"
        and overlap_ms
        <= FINAL_AUTHORITY_SCOPE_REJECTED_STRADDLER_MAX_MS
        and overlap_ratio
        <= FINAL_AUTHORITY_SCOPE_REJECTED_STRADDLER_MAX_RATIO
    ):
        return "SCOPE_REJECTED_EDGE_FRAGMENT_OUTSIDE_OWNER"
    return None


def _minimal_changed_surface(before: str, after: str) -> str:
    """Return the repaired span plus one stable character of local context."""

    if not before or not after or before == after:
        return after
    prefix = 0
    prefix_cap = min(len(before), len(after))
    while prefix < prefix_cap and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    suffix_cap = min(len(before) - prefix, len(after) - prefix)
    while (
        suffix < suffix_cap
        and before[len(before) - suffix - 1] == after[len(after) - suffix - 1]
    ):
        suffix += 1
    changed_end = len(after) - suffix if suffix else len(after)
    context_start = max(0, prefix - 1)
    context_end = min(len(after), changed_end + 1)
    return after[context_start:context_end] or after


def _entity_repair_final_surface(row: dict) -> str:
    """Return only the text surface actually owned by an entity repair."""

    if row.get("mode") == "chat_scaffold_plus_human_entity_override":
        return str(row.get("structured_exact_text") or "")
    expected_entity = str(
        row.get("expected_entity") or row.get("resolved_canonical") or ""
    )
    if expected_entity:
        return expected_entity
    before = [str(value) for value in row.get("before") or []]
    after = [str(value) for value in row.get("after") or []]
    if (
        row.get("mode") == "final_review_context_adjudication"
        and len(before) == len(after) == 1
    ):
        return _minimal_changed_surface(before[0], after[0])
    return str(row.get("structured_exact_text") or "") or "".join(after)


def _authorized_final_review_drop_cue(row: dict) -> bool:
    """Recognize a typed CPA-authorized whole-cue deletion receipt."""

    before = [str(value) for value in row.get("before") or []]
    after = [str(value) for value in row.get("after") or []]
    mutation_authority = row.get("mutation_authority") or {}
    return bool(
        row.get("mode") == "final_review_context_adjudication"
        and row.get("repair_class") == "acoustic_drop_cue"
        and row.get("decision_authority") == "CPA_JUDGE"
        and str(row.get("policy_branch") or "").startswith(
            ("CPA_JUDGE_APPLY_", "WITNESS_JUDGE_APPLY_")
        )
        and mutation_authority.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation_authority.get("status") == "PASS"
        and len(before) == len(after) == 1
        and bool(before[0])
        and after[0] == ""
        and row.get("structured_exact_text") == ""
    )


def _record_entity_repair_window_survival(
    row: dict,
    *,
    span_expected: str,
    text_window: str,
    speaker_window: str,
    owner_text_window: str,
    owner_speaker_window: str,
    text_check: str,
    speaker_check: str,
    dropped_ok: bool,
) -> None:
    if _authorized_final_review_drop_cue(row):
        # Empty is the owned result for a whole-cue deletion.  It is only
        # accepted with the typed CPA mutation receipt above, and the exact
        # adjudicated window must be subtitle-empty on both final surfaces.
        row["final_drop_cue_empty_text_window"] = not bool(
            owner_text_window
        )
        row["final_drop_cue_empty_speaker_window"] = not bool(
            owner_speaker_window
        )
        row["survived_final_text_srt"] = not bool(owner_text_window)
        row["survived_final_speaker_srt"] = not bool(owner_speaker_window)
        return
    row["survived_final_text_srt"] = bool(
        span_expected and span_expected in text_check
    ) and dropped_ok
    row["survived_final_speaker_srt"] = bool(
        span_expected and span_expected in speaker_check
    ) and dropped_ok


def _entity_repair_windows(
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    start_ms: int,
    end_ms: int,
) -> tuple[str, str, str, str]:
    return (
        normalize_srt_payload_window(
            final_text_srt, start_ms=start_ms, end_ms=end_ms
        ),
        normalize_srt_payload_window(
            final_speaker_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            strip_speaker_labels=True,
        ),
        normalize_srt_owner_payload_window(
            final_text_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            min_overlap_ms=1,
        ),
        normalize_srt_owner_payload_window(
            final_speaker_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            min_overlap_ms=1,
            strip_speaker_labels=True,
        ),
    )


def _sc_sender_final_surface(row: dict) -> str:
    """Return only the narrow sender slot owned by an SC sender repair."""

    spoken_sender = str(row.get("spoken_sender") or "")
    if not spoken_sender:
        return str(row.get("after") or "")
    if row.get("alignment_basis") == (
        "matched-superchat-body-plus-action-anchor.v1"
    ):
        return spoken_sender + "SC"
    return spoken_sender


def _format_srt_timestamp(ms: int) -> str:
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render_cues_to_srt(cues) -> str:
    """Render parsed cues back to SRT text, preserving index/timestamps."""

    blocks = [
        f"{cue.index}\n{_format_srt_timestamp(cue.start_ms)} --> {_format_srt_timestamp(cue.end_ms)}\n{cue.text}"
        for cue in cues
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _verified_source_truth_rows(audit: dict) -> list[dict]:
    truth = audit.get("source_subtitle_truth_audit") or {}
    return [
        row
        for key in ("applied", "satisfied")
        for row in truth.get(key) or []
        if isinstance(row, dict) and row.get("final_owner_verified") is True
    ]


def _rebase_text_through_source_truth_substrings(
    text: str,
    *,
    start_ms: int,
    end_ms: int,
    audit: dict,
    timeline_offset_ms: int = 0,
) -> tuple[str, list[dict[str, str]]]:
    """Replay only typed substring changes that materially own this span."""

    output = text
    applied: list[dict[str, str]] = []
    for row in _verified_source_truth_rows(audit):
        if row.get("action") != "replace_substring":
            continue
        projection = validated_source_truth_projection(row)
        if projection is None or projection["status"] != "RESOLVED":
            continue
        projection_by_index = {
            int(cue["cue_index"]): cue for cue in projection["cues"]
        }
        for replacement in row.get("replacements") or []:
            if not isinstance(replacement, dict):
                continue
            cue_index = replacement.get("cue_index")
            if isinstance(cue_index, bool) or not isinstance(cue_index, int):
                continue
            owner = projection_by_index.get(cue_index)
            if owner is None:
                continue
            owner_start = int(owner["start_ms"]) - timeline_offset_ms
            owner_end = int(owner["end_ms"]) - timeline_offset_ms
            if (
                min(end_ms, owner_end) - max(start_ms, owner_start)
                < MIN_CUE_OVERLAP_MS
            ):
                continue
            surface = str(replacement.get("surface") or "")
            canonical = str(replacement.get("canonical") or "")
            if not surface or not canonical or surface not in output:
                continue
            output = output.replace(surface, canonical)
            applied.append(
                {
                    "truth_id": str(row.get("truth_id") or ""),
                    "surface": surface,
                    "canonical": canonical,
                }
            )
    return output, applied


def _full_source_truth_projection_match(
    *,
    expected_before: str,
    final_text_payload: str,
    final_speaker_payload: str,
    start_ms: int,
    end_ms: int,
    audit: dict,
    timeline_offset_ms: int,
) -> dict[str, object] | None:
    """Prove one baseline cue was deterministically transformed by truth."""

    expected_norm = normalize_chat_text(expected_before)
    for row in _verified_source_truth_rows(audit):
        if row.get("action") not in {"replace_cue", "drop_cue"}:
            continue
        projection = validated_source_truth_projection(row)
        if projection is None or projection["status"] != "RESOLVED":
            continue
        selected = [
            cue
            for cue in projection["cues"]
            if min(
                end_ms,
                int(cue["end_ms"]) - timeline_offset_ms,
            )
            - max(
                start_ms,
                int(cue["start_ms"]) - timeline_offset_ms,
            )
            >= MIN_CUE_OVERLAP_MS
        ]
        if not selected:
            continue
        before_payload = normalize_chat_text(
            "".join(str(cue["before_text"]) for cue in selected)
        )
        after_payload = normalize_chat_text(
            "".join(str(cue["after_text"]) for cue in selected)
        )
        if (
            before_payload == expected_norm
            and after_payload == final_text_payload
            and after_payload == final_speaker_payload
        ):
            return {
                "truth_id": str(row.get("truth_id") or ""),
                "action": str(row.get("action") or ""),
                "before_payload": before_payload,
                "after_payload": after_payload,
            }
    return None


def _full_source_truth_supersedes_decision(
    *,
    expected_text: str,
    start_ms: int,
    end_ms: int,
    audit: dict,
) -> dict[str, str] | None:
    """Require a causal before/after proof before retiring an older surface."""

    expected = normalize_chat_text(expected_text)
    if not expected:
        return None
    for row in _verified_source_truth_rows(audit):
        if row.get("action") not in {"replace_cue", "drop_cue"}:
            continue
        projection = validated_source_truth_projection(row)
        if projection is None or projection["status"] != "RESOLVED":
            continue
        selected = [
            cue
            for cue in projection["cues"]
            if min(end_ms, int(cue["end_ms"]))
            - max(start_ms, int(cue["start_ms"]))
            >= MIN_CUE_OVERLAP_MS
        ]
        if not selected:
            continue
        before_payload = normalize_chat_text(
            "".join(str(cue["before_text"]) for cue in selected)
        )
        after_payload = normalize_chat_text(
            "".join(str(cue["after_text"]) for cue in selected)
        )
        if expected in before_payload and expected not in after_payload:
            return {
                "truth_id": str(row.get("truth_id") or ""),
                "action": str(row.get("action") or ""),
                "before_payload": before_payload,
                "after_payload": after_payload,
                "superseded_surface": expected,
            }
    return None


def _redelivery_baseline_intervals(audit: dict) -> list[tuple[int, int]]:
    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    if baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        return []
    return [
        (int(row["start_ms"]), int(row["end_ms"]))
        for row in baseline.get("mappings") or []
        if row.get("final_owner_verified") is True
    ]


def _baseline_replay_reverted_row(
    expected_text: str,
    *,
    start_ms: int,
    end_ms: int,
    audit: dict,
) -> dict[str, object] | None:
    """Return causal proof that baseline replay reverted this row's text.

    豁免边界 owner 行需要因果证据，不是几何巧合：replay audit 的 mapping
    必须记录「行的修复文本曾在字幕里（before），被已验证的 baseline 文本
    （after/final_owner_verified）有意替换且替换后不再包含它」。仅有窗口
    重叠、没有 before→after 替换记录的行不豁免——baseline 不得静默压制
    从未见证过的有据修复。"""

    expected_norm = normalize_chat_text(expected_text)
    if not expected_norm:
        return None
    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    if baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        return None
    touching = sorted(
        (
            row
            for row in baseline.get("mappings") or []
            if isinstance(row, dict)
            and row.get("final_owner_verified") is True
            and min(end_ms, int(row.get("end_ms") or 0))
            - max(start_ms, int(row.get("start_ms") or 0))
            >= 200
        ),
        key=lambda row: int(row.get("start_ms") or 0),
    )
    if not touching:
        return None
    pre_replay_cues: dict[int, str] = {}
    for mapping in touching:
        for cue in mapping.get("pre_replay_cues") or []:
            if not isinstance(cue, Mapping):
                continue
            cue_index = cue.get("current_cue_index")
            if (
                isinstance(cue_index, int)
                and not isinstance(cue_index, bool)
                and isinstance(cue.get("text"), str)
            ):
                pre_replay_cues.setdefault(cue_index, str(cue["text"]))
    before_payload = normalize_chat_text(
        (
            "".join(pre_replay_cues[index] for index in sorted(pre_replay_cues))
            if pre_replay_cues
            else "".join(str(row.get("before") or "") for row in touching)
        )
    )
    after_payload = normalize_chat_text(
        "".join(
            str(row.get("text") or row.get("after") or "") for row in touching
        )
    )
    if (
        before_payload
        and before_payload != after_payload
        and expected_norm in before_payload
        and expected_norm not in after_payload
    ):
        return {
            "baseline_cue_indexes": [
                int(row.get("baseline_cue_index") or 0) for row in touching
            ],
            "before_payload": before_payload,
            "after_payload": after_payload,
        }
    return None


def _baseline_retires_decision_row(
    row: dict,
    *,
    expected_text: str,
    start_ms: int,
    end_ms: int,
    audit: dict,
    baseline_overlap: bool,
) -> bool:
    """两类有因果证据的 baseline 退位（无证据的有据修复保持 fail-closed）。

    1) 边界 owner 行被 replay 的 before→after 记录证明"曾在字幕、被已验证
       baseline 有意替换"（1863 SC 案）。
    2) correction pass 的声学裁决修正被同窗逐字验证的 baseline 文本收回
       （672 礼墨/看外案）——修正输给 Ivan 已审 baseline 是既定层级，提案
       与 verdict 留在审计里，修订 baseline 的唯一正道是 ledger 真值。"""

    if not baseline_overlap:
        return False
    if row.get("boundary_required"):
        replay_revert = _baseline_replay_reverted_row(
            expected_text, start_ms=start_ms, end_ms=end_ms, audit=audit
        )
        if replay_revert is not None:
            row["final_verification_scope"] = "SUPERSEDED_BY_REDELIVERY_BASELINE"
            row["final_verification_scope_reason"] = (
                "BOUNDARY_OWNER_REVERTED_BY_VERIFIED_BASELINE_REPLAY"
            )
            row["final_redelivery_baseline_revert"] = replay_revert
            return True
    if row.get("mode") == "final_review_context_adjudication" and (
        _adjudication_pinned_by_baseline(
            expected_text, start_ms=start_ms, end_ms=end_ms, audit=audit
        )
    ):
        row["final_verification_scope"] = "SUPERSEDED_BY_REDELIVERY_BASELINE"
        row["final_verification_scope_reason"] = (
            "ADJUDICATION_PINNED_BY_REVIEWED_BASELINE"
        )
        return True
    return False


def _adjudication_pinned_by_baseline(
    expected_text: str,
    *,
    start_ms: int,
    end_ms: int,
    audit: dict,
) -> bool:
    """correction 修正表面是否已被同窗验证过的 baseline 文本收回。"""

    expected_norm = normalize_chat_text(expected_text)
    if not expected_norm:
        return False
    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    if baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        return False
    for row in baseline.get("mappings") or []:
        if not isinstance(row, dict) or row.get("final_owner_verified") is not True:
            continue
        if (
            min(end_ms, int(row.get("end_ms") or 0))
            - max(start_ms, int(row.get("start_ms") or 0))
            < 200
        ):
            continue
        owner_norm = normalize_chat_text(
            str(row.get("final_owner_expected") or row.get("text") or row.get("after") or "")
        )
        if owner_norm and expected_norm not in owner_norm:
            return True
    return False


def _owner_window_payloads(
    action: str,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    start_ms: int,
    end_ms: int,
) -> tuple[str, str]:
    """majority 门（1573 r11 案）：真值窗坐标来自落值轮网格，本轮句尾早移时
    窗尾会以 ≥80ms 绝对门擦进下一句——replace_cue 的邻句须过半重叠才算
    owner payload，有界抖动由门吸收。drop_cue 语义相反（任何 cue 侵入删除
    区间都是真信号），维持绝对门。"""

    majority = 0.5 if action != "drop_cue" else None
    text_payload = _window_payload(
        final_text_srt,
        start_ms=start_ms,
        end_ms=end_ms,
        min_overlap_ms=MIN_CUE_OVERLAP_MS,
        majority_ratio=majority,
    )
    speaker_payload = _window_payload(
        final_speaker_srt,
        start_ms=start_ms,
        end_ms=end_ms,
        min_overlap_ms=MIN_CUE_OVERLAP_MS,
        strip_speaker_labels=True,
        majority_ratio=majority,
    )
    return text_payload, speaker_payload


def _contiguous_owner_window_union(
    windows: list[dict],
) -> tuple[int, int] | None:
    """Return the union of a multi-cue owner only when it has no time gap.

    Source-truth projection is bound to the cue grid on which the truth was
    applied.  Later release hygiene may legally merge adjacent projected cues
    without changing a single spoken character.  In that case, probing every
    old sub-window returns the same merged cue twice and falsely duplicates the
    final payload.  Only a contiguous multi-window set is safe to coalesce:
    non-contiguous mentions retain the strict per-window checks below.
    """

    if len(windows) < 2:
        return None
    ordered = sorted(
        (
            (int(window["start_ms"]), int(window["end_ms"]))
            for window in windows
        ),
        key=lambda value: (value[0], value[1]),
    )
    union_start, union_end = ordered[0]
    for start_ms, end_ms in ordered[1:]:
        if start_ms > union_end:
            return None
        union_end = max(union_end, end_ms)
    return union_start, union_end


def _window_payload(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    min_overlap_ms: int,
    strip_speaker_labels: bool = False,
    majority_ratio: float | None = None,
) -> str:
    return normalize_chat_text(
        normalize_srt_owner_payload_window(
            srt_text,
            start_ms=max(0, start_ms),
            end_ms=max(start_ms + 1, end_ms),
            min_overlap_ms=min_overlap_ms,
            strip_speaker_labels=strip_speaker_labels,
            majority_ratio=majority_ratio,
        )
    )


def _classify_source_truth_delivery_windows(
    owned_rows: list[dict],
    *,
    delivery_start_ms: int,
    delivery_end_ms: int,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    classified: list[dict] = []
    for owned in owned_rows:
        owner_start = int(owned["start_ms"])
        owner_end = int(owned["end_ms"])
        if owner_end <= delivery_start_ms:
            relation = "OUTSIDE_BEFORE_FINAL_DELIVERY"
        elif owner_start >= delivery_end_ms:
            relation = "OUTSIDE_AFTER_FINAL_DELIVERY"
        elif owner_start < delivery_start_ms and owner_end > delivery_end_ms:
            relation = "STRADDLES_BOTH_FINAL_DELIVERY_ENDPOINTS"
        elif owner_start < delivery_start_ms:
            relation = "STRADDLES_FINAL_DELIVERY_START"
        elif owner_end > delivery_end_ms:
            relation = "STRADDLES_FINAL_DELIVERY_END"
        else:
            relation = "INSIDE_FINAL_DELIVERY"
        classified.append(
            {
                **owned,
                "source_timeline_start_ms": owner_start,
                "source_timeline_end_ms": owner_end,
                "delivery_relation": relation,
            }
        )
    straddlers = [
        row
        for row in classified
        if str(row["delivery_relation"]).startswith("STRADDLES_")
    ]
    inside = [
        row
        for row in classified
        if row["delivery_relation"] == "INSIDE_FINAL_DELIVERY"
    ]
    outside = [
        row
        for row in classified
        if str(row["delivery_relation"]).startswith("OUTSIDE_")
    ]
    return classified, straddlers, inside, outside


def _coalesced_source_truth_owner(
    *,
    action: str,
    inside_windows: list[dict],
    expected_exact: str,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
) -> dict | None:
    projection_bound = bool(inside_windows) and all(
        window.get("expected_after") is not None
        and isinstance(window.get("cue_index"), int)
        and not isinstance(window.get("cue_index"), bool)
        for window in inside_windows
    )
    contiguous_union = (
        _contiguous_owner_window_union(inside_windows)
        if action == "replace_cue" and projection_bound
        else None
    )
    if contiguous_union is None:
        return None
    union_start, union_end = contiguous_union
    relative_start = union_start - delivery_start_ms
    relative_end = union_end - delivery_start_ms
    text_payload, speaker_payload = _owner_window_payloads(
        action,
        final_text_srt=final_text_srt,
        final_speaker_srt=final_speaker_srt,
        start_ms=relative_start,
        end_ms=relative_end,
    )
    text_ok = bool(expected_exact) and text_payload == expected_exact
    speaker_ok = (
        bool(expected_exact) and speaker_payload == expected_exact
    )
    return {
        "schema_version": "source-truth-final-recue-coalescence.v1",
        "status": "PASS" if text_ok and speaker_ok else "FAIL",
        "start_ms": relative_start,
        "end_ms": relative_end,
        "projected_window_count": len(inside_windows),
        "expected_exact": expected_exact,
        "text_payload": text_payload,
        "speaker_payload": speaker_payload,
        "text_ok": text_ok,
        "speaker_ok": speaker_ok,
    }


def _verify_inside_source_truth_owner(
    row: dict,
    *,
    action: str,
    expected_exact: str,
    required_text: str,
    inside_windows: list[dict],
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
) -> tuple[list[dict], int]:
    failures: list[dict] = []
    window_results: list[dict] = []
    coalesced_owner = _coalesced_source_truth_owner(
        action=action,
        inside_windows=inside_windows,
        expected_exact=expected_exact,
        final_text_srt=final_text_srt,
        final_speaker_srt=final_speaker_srt,
        delivery_start_ms=delivery_start_ms,
    )
    coalesced_owner_ok = bool(
        coalesced_owner and coalesced_owner["status"] == "PASS"
    )
    for owned in inside_windows:
        relative_start = int(owned["start_ms"]) - delivery_start_ms
        relative_end = int(owned["end_ms"]) - delivery_start_ms
        text_payload, speaker_payload = _owner_window_payloads(
            action,
            final_text_srt=final_text_srt,
            final_speaker_srt=final_speaker_srt,
            start_ms=relative_start,
            end_ms=relative_end,
        )
        expected_after_raw = owned["expected_after"]
        if expected_after_raw is None:
            text_ok = speaker_ok = True
        else:
            expected_after = normalize_chat_text(
                str(expected_after_raw)
            )
            text_ok = text_payload == expected_after
            speaker_ok = speaker_payload == expected_after
        projected_text_ok = text_ok
        projected_speaker_ok = speaker_ok
        if coalesced_owner_ok:
            text_ok = True
            speaker_ok = True
        result = {
            "start_ms": relative_start,
            "end_ms": relative_end,
            "cue_index": owned["cue_index"],
            "action": action,
            "expected_after": expected_after_raw,
            "expected_exact": expected_exact,
            "required_text": required_text,
            "text_payload": text_payload,
            "speaker_payload": speaker_payload,
            "text_ok": text_ok,
            "speaker_ok": speaker_ok,
        }
        if coalesced_owner is not None:
            result.update(
                {
                    "verification_mode": (
                        "CONTIGUOUS_RECUED_OWNER_UNION"
                    ),
                    "projected_text_ok": projected_text_ok,
                    "projected_speaker_ok": projected_speaker_ok,
                }
            )
        window_results.append(result)
        if not (text_ok and speaker_ok):
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": (
                        "SOURCE_TRUTH_FINAL_OWNER_MISMATCH"
                    ),
                    **result,
                }
            )

    aggregate_text = (
        str(coalesced_owner["text_payload"])
        if coalesced_owner is not None
        else "".join(
            str(item["text_payload"]) for item in window_results
        )
    )
    aggregate_speaker = (
        str(coalesced_owner["speaker_payload"])
        if coalesced_owner is not None
        else "".join(
            str(item["speaker_payload"]) for item in window_results
        )
    )
    if action == "drop_cue":
        contract_text_ok = not aggregate_text
        contract_speaker_ok = not aggregate_speaker
    elif action == "replace_cue":
        contract_text_ok = (
            bool(expected_exact) and aggregate_text == expected_exact
        )
        contract_speaker_ok = (
            bool(expected_exact) and aggregate_speaker == expected_exact
        )
    elif action == "replace_substring":
        expected = required_text or expected_exact
        contract_text_ok = bool(expected) and expected in aggregate_text
        contract_speaker_ok = (
            bool(expected) and expected in aggregate_speaker
        )
    else:
        contract_text_ok = contract_speaker_ok = False
    if not (contract_text_ok and contract_speaker_ok):
        failures.append(
            {
                "truth_id": row.get("truth_id"),
                "reason_code": "SOURCE_TRUTH_FINAL_CONTRACT_MISMATCH",
                "action": action,
                "expected_exact": expected_exact,
                "required_text": required_text,
                "text_payload": aggregate_text,
                "speaker_payload": aggregate_speaker,
                "text_ok": contract_text_ok,
                "speaker_ok": contract_speaker_ok,
            }
        )
    row["final_owner_scope"] = "FINAL_DELIVERY"
    row["final_owner_contract"] = {
        "action": action,
        "expected_exact": expected_exact,
        "required_text": required_text,
        "text_payload": aggregate_text,
        "speaker_payload": aggregate_speaker,
        "text_ok": contract_text_ok,
        "speaker_ok": contract_speaker_ok,
    }
    if coalesced_owner is not None:
        row["final_owner_contract"]["recue_coalescence"] = (
            coalesced_owner
        )
    row["final_owner_windows"] = window_results
    row["final_owner_verified"] = (
        bool(window_results)
        and all(
            item["text_ok"] and item["speaker_ok"]
            for item in window_results
        )
        and contract_text_ok
        and contract_speaker_ok
    )
    return failures, len(window_results)


def _verify_source_truth_owners(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
    delivery_end_ms: int,
) -> tuple[bool, int]:
    truth = audit.get("source_subtitle_truth_audit") or {}
    all_rows: list[dict] = [
        row
        for key in ("applied", "satisfied")
        for row in truth.get(key) or []
        if isinstance(row, dict)
    ]
    optional_rows = [row for row in all_rows if row.get("required") is False]
    rows = [row for row in all_rows if row.get("required") is not False]
    required_count = 0
    failures: list[dict] = []
    required_rows: list[dict] = []
    context_only_rows: list[dict] = []
    straddling_rows: list[dict] = []
    if (
        isinstance(delivery_start_ms, bool)
        or not isinstance(delivery_start_ms, int)
        or isinstance(delivery_end_ms, bool)
        or not isinstance(delivery_end_ms, int)
        or delivery_start_ms < 0
        or delivery_end_ms <= delivery_start_ms
    ):
        failures.append(
            {
                "reason_code": "SOURCE_TRUTH_FINAL_DELIVERY_INTERVAL_INVALID",
                "delivery_start_ms": delivery_start_ms,
                "delivery_end_ms": delivery_end_ms,
            }
        )
        audit["final_source_truth_owner_verification"] = {
            "status": "FAIL",
            "final_delivery_interval": {
                "timeline": "padded_source_local_ms",
                "interval_semantics": "half_open",
                "start_ms": delivery_start_ms,
                "end_ms": delivery_end_ms,
            },
            "required_truth_row_count": len(rows),
            "required_truth_ids": [
                str(row.get("truth_id") or "") for row in rows
            ],
            "context_only_truth_row_count": 0,
            "context_only_truth_ids": [],
            "context_only_truth_evidence": [],
            "optional_truth_row_count": len(optional_rows),
            "straddling_truth_row_count": 0,
            "required_window_count": 0,
            "failures": failures,
        }
        return False, 0

    for row in rows:
        for stale_key in (
            "final_owner_scope",
            "final_owner_scope_reason",
            "final_owner_contract",
            "final_owner_windows",
            "final_owner_verified",
        ):
            row.pop(stale_key, None)
        contract = row.get("declared_output_contract") or {}
        action = str(contract.get("action") or row.get("action") or "")
        canonical_texts = contract.get("canonical_texts")
        if not isinstance(canonical_texts, list):
            canonical_texts = row.get("after") or []
        expected_exact = normalize_chat_text("".join(str(value) for value in canonical_texts))
        required_text = normalize_chat_text(str(contract.get("required_text") or ""))
        projection_present = "resolved_target_projection" in row
        projection = validated_source_truth_projection(row)
        if projection_present and projection is None:
            required_rows.append(row)
            row["final_owner_scope"] = "INVALID_PROJECTION"
            row["final_owner_verified"] = False
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": ("SOURCE_TRUTH_FINAL_PROJECTION_INVALID"),
                }
            )
            continue
        if projection is not None and projection["status"] == "RESOLVED":
            owned_rows = [
                {
                    "start_ms": int(cue["start_ms"]),
                    "end_ms": int(cue["end_ms"]),
                    "expected_after": str(cue["after_text"]),
                    "cue_index": int(cue["cue_index"]),
                }
                for cue in projection["cues"]
            ]
        else:
            owned_rows = [
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "expected_after": None,
                    "cue_index": None,
                }
                for start_ms, end_ms in source_truth_owner_windows(row)
            ]
        if not owned_rows:
            required_rows.append(row)
            row["final_owner_scope"] = "MISSING_OWNER_WINDOW"
            row["final_owner_verified"] = False
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": "SOURCE_TRUTH_FINAL_WINDOW_MISSING",
                }
            )
            continue

        (
            classified_windows,
            straddlers,
            inside_windows,
            outside_windows,
        ) = _classify_source_truth_delivery_windows(
            owned_rows,
            delivery_start_ms=delivery_start_ms,
            delivery_end_ms=delivery_end_ms,
        )
        if straddlers:
            required_rows.append(row)
            straddling_rows.append(row)
            row["final_owner_scope"] = "STRADDLES_FINAL_DELIVERY"
            row["final_owner_windows"] = classified_windows
            row["final_owner_verified"] = False
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": ("SOURCE_TRUTH_FINAL_WINDOW_STRADDLES_DELIVERY"),
                    "delivery_start_ms": delivery_start_ms,
                    "delivery_end_ms": delivery_end_ms,
                    "windows": classified_windows,
                }
            )
            continue
        if inside_windows and outside_windows:
            required_rows.append(row)
            row["final_owner_scope"] = "MIXED_FINAL_DELIVERY_AND_CONTEXT_WINDOWS"
            row["final_owner_windows"] = classified_windows
            row["final_owner_verified"] = False
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": ("SOURCE_TRUTH_FINAL_WINDOW_SET_CROSSES_DELIVERY_SCOPE"),
                    "delivery_start_ms": delivery_start_ms,
                    "delivery_end_ms": delivery_end_ms,
                    "windows": classified_windows,
                }
            )
            continue
        if outside_windows and not inside_windows:
            context_only_rows.append(row)
            row["final_owner_scope"] = "CONTEXT_ONLY_OUTSIDE_FINAL_DELIVERY"
            row["final_owner_scope_reason"] = (
                "ALL_EFFECTIVE_OWNER_WINDOWS_OUTSIDE_HALF_OPEN_FINAL_INTERVAL"
            )
            row["final_owner_windows"] = classified_windows
            row["final_owner_verified"] = False
            continue

        required_rows.append(row)
        owner_failures, owner_window_count = (
            _verify_inside_source_truth_owner(
                row,
                action=action,
                expected_exact=expected_exact,
                required_text=required_text,
                inside_windows=inside_windows,
                final_text_srt=final_text_srt,
                final_speaker_srt=final_speaker_srt,
                delivery_start_ms=delivery_start_ms,
            )
        )
        failures.extend(owner_failures)
        required_count += owner_window_count
    audit["final_source_truth_owner_verification"] = {
        "status": "FAIL" if failures else "PASS",
        "final_delivery_interval": {
            "timeline": "padded_source_local_ms",
            "interval_semantics": "half_open",
            "start_ms": delivery_start_ms,
            "end_ms": delivery_end_ms,
        },
        "required_truth_row_count": len(required_rows),
        "required_truth_ids": [
            str(row.get("truth_id") or "") for row in required_rows
        ],
        "context_only_truth_row_count": len(context_only_rows),
        "context_only_truth_ids": [
            str(row.get("truth_id") or "") for row in context_only_rows
        ],
        "context_only_truth_evidence": [
            {
                "truth_id": str(row.get("truth_id") or ""),
                "boundary_role": str(row.get("boundary_role") or ""),
                "final_owner_scope": row.get("final_owner_scope"),
                "final_owner_windows": row.get("final_owner_windows"),
            }
            for row in context_only_rows
        ],
        "optional_truth_row_count": len(optional_rows),
        "straddling_truth_row_count": len(straddling_rows),
        "required_window_count": required_count,
        "failures": failures,
    }
    return not failures, required_count


def _merge_receipts_can_collapse_group(
    expected_parts: list[str],
    receipts: list[dict],
) -> tuple[bool, list[dict]]:
    """Replay typed release-grade receipts over one adjacent baseline group."""

    states: dict[tuple[str, ...], list[dict]] = {
        tuple(expected_parts): []
    }
    for receipt in receipts:
        action = str(receipt.get("action") or "")
        receipt_text = normalize_chat_text(str(receipt.get("text") or ""))
        if action not in {"MERGED_INTO_NEXT", "MERGED_INTO_PREV"}:
            continue
        next_states = dict(states)
        for state, applied in states.items():
            for position, part in enumerate(state):
                if part != receipt_text:
                    continue
                if action == "MERGED_INTO_NEXT" and position + 1 < len(state):
                    collapsed = (
                        state[:position]
                        + (state[position] + state[position + 1],)
                        + state[position + 2 :]
                    )
                elif action == "MERGED_INTO_PREV" and position > 0:
                    collapsed = (
                        state[: position - 1]
                        + (state[position - 1] + state[position],)
                        + state[position + 1 :]
                    )
                else:
                    continue
                next_states.setdefault(collapsed, applied + [receipt])
        states = next_states
    final_state = ("".join(expected_parts),)
    applied = states.get(final_state)
    return applied is not None, applied or []


def _verify_release_grade_merge_groups(
    audit: dict,
    *,
    mapping_results: list[dict],
    final_text_srt: str,
) -> list[dict]:
    """Accept only hash-bound, exactly replayable cue-layout merges.

    Redelivery owns the reviewed words, while the later release-grade hygiene
    layer may remove an invalid sliver cue without changing those words.  The
    old owner check compared every baseline cue in isolation, so a legitimate
    ``哦`` + ``这样吗`` -> ``哦，这样吗`` merge looked like text mutation.
    This verifier groups mappings by the one final cue that owns them, proves
    their normalized concatenation is exact, and replays the typed merge
    receipts.  Missing/tampered receipts, extra words, non-adjacent windows, or
    a stale final hash remain fail-closed.
    """

    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    baseline_receipts = baseline.get("final_release_grade_cue_merges") or []
    audit_receipts = audit.get("final_release_grade_cue_merges") or []
    final_hash = hashlib.sha256(final_text_srt.encode("utf-8")).hexdigest()
    if (
        not isinstance(baseline_receipts, list)
        or not baseline_receipts
        or audit_receipts != baseline_receipts
        or baseline.get("post_release_grade_output_sha256") != final_hash
        or audit.get("final_output_srt_sha256") != final_hash
    ):
        return []
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("block"), int)
        or int(row["block"]) <= 0
        or row.get("action")
        not in {"MERGED_INTO_NEXT", "MERGED_INTO_PREV"}
        or not normalize_chat_text(str(row.get("text") or ""))
        for row in baseline_receipts
    ):
        return []

    final_cues = parse_srt_cues(final_text_srt)
    groups: dict[int, list[dict]] = {}
    for result in mapping_results:
        if result["verified"]:
            continue
        owner_cues = [
            cue
            for cue in final_cues
            if min(result["end_ms"], cue.end_ms)
            - max(result["start_ms"], cue.start_ms)
            >= MIN_ALIGNMENT_OVERLAP_MS
        ]
        if len(owner_cues) != 1:
            continue
        groups.setdefault(owner_cues[0].index, []).append(result)

    verified_groups: list[dict] = []
    cues_by_index = {cue.index: cue for cue in final_cues}
    for cue_index, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda result: (result["start_ms"], result["end_ms"]))
        cue = cues_by_index[cue_index]
        if (
            cue.start_ms != group[0]["start_ms"]
            or cue.end_ms != group[-1]["end_ms"]
        ):
            continue
        if any(
            current["start_ms"] < previous["end_ms"]
            or current["start_ms"] - previous["end_ms"]
            > FINAL_RELEASE_GRADE_MERGE_MAX_GAP_MS
            for previous, current in zip(group, group[1:])
        ):
            continue
        expected_parts = [result["expected"] for result in group]
        combined = "".join(expected_parts)
        final_payload = normalize_chat_text(cue.text)
        if (
            not all(expected_parts)
            or combined != final_payload
            or any(
                result["text_payload"] != final_payload
                or result["speaker_payload"] != final_payload
                for result in group
            )
        ):
            continue
        receipt_ok, applied_receipts = _merge_receipts_can_collapse_group(
            expected_parts,
            baseline_receipts,
        )
        if not receipt_ok:
            continue
        verified_groups.append(
            {
                "final_cue_index": cue_index,
                "mapping_results": group,
                "final_payload": final_payload,
                "receipt_actions": applied_receipts,
            }
        )
    return verified_groups


def _verify_redelivery_baseline_owners(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
) -> tuple[bool, int]:
    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    if baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        audit["final_redelivery_baseline_owner_verification"] = {
            "status": "NOT_APPLICABLE",
            "required_mapping_count": 0,
            "failures": [],
        }
        return True, 0
    mappings = [
        row for row in baseline.get("mappings") or [] if isinstance(row, dict)
    ]
    failures: list[dict] = []
    mapping_results: list[dict] = []
    required_count = 0
    for row in mappings:
        start_ms = int(row.get("start_ms") or 0)
        end_ms = int(row.get("end_ms") or 0)
        expected_raw = str(row.get("text") or row.get("after") or "")
        expected_rebased, substring_replacements = (
            _rebase_text_through_source_truth_substrings(
                expected_raw,
                start_ms=start_ms,
                end_ms=end_ms,
                audit=audit,
                timeline_offset_ms=delivery_start_ms,
            )
        )
        expected = normalize_chat_text(expected_rebased)
        text_payload = _window_payload(
            final_text_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            min_overlap_ms=MIN_ALIGNMENT_OVERLAP_MS,
        )
        speaker_payload = _window_payload(
            final_speaker_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            min_overlap_ms=MIN_ALIGNMENT_OVERLAP_MS,
            strip_speaker_labels=True,
        )
        text_ok = bool(expected) and text_payload == expected
        speaker_ok = bool(expected) and speaker_payload == expected
        full_projection = None
        if not (text_ok and speaker_ok):
            full_projection = _full_source_truth_projection_match(
                expected_before=expected_raw,
                final_text_payload=text_payload,
                final_speaker_payload=speaker_payload,
                start_ms=start_ms,
                end_ms=end_ms,
                audit=audit,
                timeline_offset_ms=delivery_start_ms,
            )
            if full_projection is not None:
                text_ok = speaker_ok = True
        required_count += 1
        row.update(
            {
                "final_owner_scope": (
                    "TRANSFORMED_BY_SOURCE_TRUTH_OWNER"
                    if full_projection is not None
                    or substring_replacements
                    else "DELIVERY"
                ),
                "final_owner_expected": expected,
                "final_owner_text_payload": text_payload,
                "final_owner_speaker_payload": speaker_payload,
                "final_owner_verified": text_ok and speaker_ok,
            }
        )
        if substring_replacements:
            row["final_owner_source_truth_substring_replacements"] = (
                substring_replacements
            )
        if full_projection is not None:
            row["final_owner_source_truth_projection"] = full_projection
        mapping_results.append(
            {
                "row": row,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "expected": expected,
                "text_payload": text_payload,
                "speaker_payload": speaker_payload,
                "verified": text_ok and speaker_ok,
            }
        )
    release_merge_groups = _verify_release_grade_merge_groups(
        audit,
        mapping_results=mapping_results,
        final_text_srt=final_text_srt,
    )
    release_merge_mapping_ids = {
        id(result["row"])
        for group in release_merge_groups
        for result in group["mapping_results"]
    }
    for result in mapping_results:
        row = result["row"]
        if id(row) in release_merge_mapping_ids:
            result["verified"] = True
            row.update(
                {
                    "final_owner_scope": (
                        "DETERMINISTIC_RELEASE_GRADE_CUE_MERGE"
                    ),
                    "final_owner_verified": True,
                }
            )
            continue
        if result["verified"]:
            continue
        failures.append(
            {
                "baseline_cue_index": row.get("baseline_cue_index"),
                "reason_code": "REDELIVERY_BASELINE_FINAL_OWNER_MISMATCH",
                "start_ms": result["start_ms"],
                "end_ms": result["end_ms"],
                "expected": result["expected"],
                "text_payload": result["text_payload"],
                "speaker_payload": result["speaker_payload"],
            }
        )
    if not mappings:
        failures.append(
            {
                "reason_code": "REDELIVERY_BASELINE_FINAL_MAPPINGS_MISSING",
            }
        )
    audit["final_redelivery_baseline_owner_verification"] = {
        "status": "FAIL" if failures else "PASS",
        "required_mapping_count": required_count,
        "release_grade_merge_group_count": len(release_merge_groups),
        "release_grade_merge_groups": [
            {
                "final_cue_index": group["final_cue_index"],
                "baseline_cue_indexes": [
                    result["row"].get("baseline_cue_index")
                    for result in group["mapping_results"]
                ],
                "final_payload": group["final_payload"],
                "receipt_actions": group["receipt_actions"],
            }
            for group in release_merge_groups
        ],
        "failures": failures,
    }
    return not failures, required_count


def _exact_final_cpa_retires_decision_row(
    row: dict,
    *,
    expected_text: str,
    audit: Mapping[str, object],
    final_text_srt: str,
    delivery_start_ms: int,
) -> bool:
    """Retire an older correction only through an exact CPA receipt chain.

    Older packages predate explicit exact-final surface-owner rows. Their
    self-heal receipts are still sufficient when the prior owner's text hash,
    unchanged cue geometry, ordered before->after chain, and final SRT hash all
    agree. This is intentionally narrower than generic overlap supersession.
    """

    self_heal = audit.get("exact_final_cpa_self_heal")
    if (
        not isinstance(self_heal, Mapping)
        or self_heal.get("schema_version")
        != "exact-final-cpa-self-heal-audit.v1"
        or self_heal.get("status") != "PASS"
        or self_heal.get("final_srt_sha256")
        != "sha256:" + hashlib.sha256(final_text_srt.encode("utf-8")).hexdigest()
        or not expected_text
    ):
        return False
    try:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    local_start = matched_start - delivery_start_ms
    local_end = matched_end - delivery_start_ms
    cue_matches = [
        cue
        for cue in parse_srt_cues(final_text_srt)
        if cue.start_ms == local_start and cue.end_ms == local_end
    ]
    if len(cue_matches) != 1:
        return False
    cue = cue_matches[0]
    chain_hash = "sha256:" + hashlib.sha256(
        expected_text.encode("utf-8")
    ).hexdigest()
    final_hash = "sha256:" + hashlib.sha256(
        cue.text.encode("utf-8")
    ).hexdigest()
    consumed: list[dict[str, object]] = []
    passes = self_heal.get("passes")
    if not isinstance(passes, list):
        return False
    for pass_row in passes:
        if not isinstance(pass_row, Mapping):
            return False
        repairs = pass_row.get("repairs")
        if not isinstance(repairs, list):
            return False
        for repair in repairs:
            if not isinstance(repair, Mapping):
                return False
            mutation = repair.get("mutation_authority")
            if (
                repair.get("schema_version")
                != "exact-final-cpa-self-heal.v1"
                or str(repair.get("cue_index")) != str(cue.index)
                or repair.get("decision_authority") != "CPA_JUDGE"
                or repair.get("timing_immutable") is not True
                or not isinstance(mutation, Mapping)
                or mutation.get("schema_version")
                != "subtitle-correction-mutation-authority.v1"
                or mutation.get("status") != "PASS"
            ):
                continue
            before_hash = repair.get("before_sha256")
            after_hash = repair.get("after_sha256")
            if (
                before_hash == chain_hash
                and isinstance(after_hash, str)
                and len(after_hash) == 71
                and after_hash.startswith("sha256:")
            ):
                chain_hash = after_hash
                consumed.append(
                    {
                        "pass_index": pass_row.get("pass_index"),
                        "finding_sha256": repair.get("finding_sha256"),
                        "request_sha256": repair.get("request_sha256"),
                        "before_sha256": before_hash,
                        "after_sha256": after_hash,
                    }
                )
    if not consumed or chain_hash != final_hash:
        return False
    row["reconciliation"] = {
        "schema_version": "exact-final-cpa-receipt-chain-supersession.v1",
        "status": "SUPERSEDED_BY_EXACT_FINAL_CPA",
        "cue_index": cue.index,
        "matched_start_ms": matched_start,
        "matched_end_ms": matched_end,
        "final_cue_sha256": final_hash,
        "receipt_chain": consumed,
        "timing_immutable": True,
    }
    return True


def verify_chat_authority_final_surfaces(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
    delivery_end_ms: int,
) -> bool:
    """Verify every in-delivery authority decision at its original time span."""

    # Recovery retries reuse the prior chat-authority audit.  A successful
    # verification must not retain the previous attempt's terminal reason
    # (2026-07-22 1863 otherwise returned True while still serializing
    # REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED).
    audit.pop("final_verification_failure", None)

    hard_meme_failures = {}
    for surface_name, srt_text in (
        ("final_text_srt", final_text_srt),
        ("final_speaker_srt", final_speaker_srt),
    ):
        _normalized, replacements = canonicalize_hard_meme_surfaces(srt_text)
        if replacements:
            hard_meme_failures[surface_name] = replacements
    audit["final_hard_meme_surface_verification"] = {
        "status": "FAIL" if hard_meme_failures else "PASS",
        "failures": hard_meme_failures,
    }
    if hard_meme_failures:
        audit["final_verification_failure"] = (
            "UNBYPASSABLE_HARD_MEME_SURFACE_PRESENT"
        )
        return False

    source_owner_ok, source_owner_count = _verify_source_truth_owners(
        audit,
        final_text_srt=final_text_srt,
        final_speaker_srt=final_speaker_srt,
        delivery_start_ms=delivery_start_ms,
        delivery_end_ms=delivery_end_ms,
    )
    if not source_owner_ok:
        audit["final_verification_failure"] = (
            "SOURCE_TRUTH_FINAL_OWNER_NOT_VERIFIED"
        )
        return False
    baseline_owner_ok, baseline_owner_count = (
        _verify_redelivery_baseline_owners(
            audit,
            final_text_srt=final_text_srt,
            final_speaker_srt=final_speaker_srt,
            delivery_start_ms=delivery_start_ms,
        )
    )
    if not baseline_owner_ok:
        audit["final_verification_failure"] = (
            "REDELIVERY_BASELINE_FINAL_OWNER_NOT_VERIFIED"
        )
        return False

    decision_rows: list[tuple[str, dict, str]] = []
    decision_rows.extend(
        ("exact_read", row, str(row.get("exact_text") or ""))
        for row in audit.get("applied") or []
        # A later hash-bound reviewed text decision owns a corrected homophone
        # in the same cue.  The superseded chat proposal remains in the audit,
        # but its old spelling is no longer a final-surface requirement.
        if not row.get("reconciliation")
    )
    decision_rows.extend(
        ("sc_sender", row, _sc_sender_final_surface(row))
        for row in audit.get("sender_repairs") or []
    )
    decision_rows.extend(
        ("gift_name", row, str(row.get("after") or ""))
        for row in audit.get("gift_repairs") or []
    )
    decision_rows.extend(
        ("reply_coreference", row, str(row.get("after") or ""))
        for row in audit.get("coreference_repairs") or []
    )
    decision_rows.extend(
        (
            "entity_repair",
            row,
            _entity_repair_final_surface(row),
        )
        for row in audit.get("entity_repairs") or []
        # 已被和解回退的行（矛盾裁定/未注册实体）不再要求其结果存活于终稿。
        if not row.get("reconciliation")
    )
    if any(
        row.get("reconciliation_status") != "APPLIED_AND_HASH_VERIFIED"
        for row in audit.get("pending_text_overrides") or []
    ):
        audit["final_verification_failure"] = "PENDING_TEXT_OVERRIDE_NOT_RECONCILED"
        return False
    redelivery_intervals = _redelivery_baseline_intervals(audit)
    superseded_by_truth = 0
    superseded_by_redelivery = 0
    superseded_by_exact_final_cpa = 0
    required_rows: list[dict] = []
    for kind, row, expected_text in decision_rows:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
        row["final_verification_kind"] = kind
        if (
            kind == "entity_repair"
            and row.get("mode") == "final_review_context_adjudication"
            and _exact_final_cpa_retires_decision_row(
                row,
                expected_text=expected_text,
                audit=audit,
                final_text_srt=final_text_srt,
                delivery_start_ms=delivery_start_ms,
            )
        ):
            row["final_verification_scope"] = (
                "SUPERSEDED_BY_EXACT_FINAL_CPA"
            )
            superseded_by_exact_final_cpa += 1
            continue
        # 同轴直比（2026-07-20 kmx r4 案）：决策行 matched_* 与 ledger 的
        # local_windows 都锚在产线 spec（padded）时间轴上；换算到交付轴再比
        # 会差 recut 头（9770ms 级），豁免只剩巧合交叠。
        full_truth_supersession = _full_source_truth_supersedes_decision(
            expected_text=expected_text,
            start_ms=matched_start,
            end_ms=matched_end,
            audit=audit,
        )
        if full_truth_supersession is not None:
            row["final_verification_scope"] = "SUPERSEDED_BY_SOURCE_TRUTH"
            row["final_source_truth_supersession"] = (
                full_truth_supersession
            )
            superseded_by_truth += 1
            continue
        expected_text, source_truth_replacements = (
            _rebase_text_through_source_truth_substrings(
                expected_text,
                start_ms=matched_start,
                end_ms=matched_end,
                audit=audit,
            )
        )
        if source_truth_replacements:
            row["final_source_truth_substring_replacements"] = (
                source_truth_replacements
            )
        relative_matched_start = matched_start - delivery_start_ms
        relative_matched_end = matched_end - delivery_start_ms
        baseline_overlap = any(
            min(relative_matched_end, pin_end)
            - max(relative_matched_start, pin_start)
            >= 200
            for pin_start, pin_end in redelivery_intervals
        )
        if baseline_overlap and not row.get("boundary_required"):
            row["final_verification_scope"] = (
                "SUPERSEDED_BY_REDELIVERY_BASELINE"
            )
            superseded_by_redelivery += 1
            continue
        # 边界 owner 行只有拿到因果证据才可退位给 baseline：replay audit
        # 的 before→after 记录证明修复文本曾在字幕里、被已验证的 Ivan 已审
        # baseline 有意替换（672 礼墨/1863 SC 案），此时行文本不再是终稿
        # 要求，否则 repair-vs-replay 永久死锁。无替换记录的有据修复维持
        # fail-closed（baseline 不得静默压制）。边界几何仍由 frozen owner
        # contract 的 local_windows 约束，与文本存活无关。
        if _baseline_retires_decision_row(
            row,
            expected_text=expected_text,
            start_ms=relative_matched_start,
            end_ms=relative_matched_end,
            audit=audit,
            baseline_overlap=baseline_overlap,
        ):
            superseded_by_redelivery += 1
            continue
        overlap_ms = max(
            0,
            min(matched_end, delivery_end_ms)
            - max(matched_start, delivery_start_ms),
        )
        matched_duration_ms = max(1, matched_end - matched_start)
        overlap_ratio = overlap_ms / matched_duration_ms
        row["final_delivery_overlap_ms"] = overlap_ms
        row["final_delivery_overlap_ratio"] = round(overlap_ratio, 6)
        outside_fragment_reason = _outside_delivery_edge_fragment_reason(
            row,
            matched_start_ms=matched_start,
            matched_end_ms=matched_end,
            delivery_start_ms=delivery_start_ms,
            delivery_end_ms=delivery_end_ms,
            overlap_ms=overlap_ms,
            overlap_ratio=overlap_ratio,
        )
        if overlap_ms == 0 or outside_fragment_reason:
            if row.get("boundary_required") is True:
                row["final_verification_scope"] = (
                    "BOUNDARY_REQUIRED_OWNER_EXCLUDED"
                )
                row["survived_final_text_srt"] = False
                row["survived_final_speaker_srt"] = False
                required_rows.append(row)
            else:
                row["final_verification_scope"] = "OUTSIDE_DELIVERY"
                if outside_fragment_reason:
                    row["final_verification_scope_reason"] = outside_fragment_reason
            continue
        row["final_verification_scope"] = "DELIVERY"
        relative_start = max(0, matched_start - delivery_start_ms)
        relative_end = min(delivery_end_ms - delivery_start_ms, matched_end - delivery_start_ms)
        expected_norm = normalize_chat_text(expected_text)
        # 对齐拼接（2026-07-13）：exact_read 允许声明「authority 头/尾她已在相邻
        # 句说过，不重复注入」。跨度窗口只验剩余部分；被弃置的头/尾必须在
        # ±邻近上下文窗口里真实存在，否则该声明不成立、行判失败——契约仍是
        # "authority 全文都在最终字幕里"，只是允许分布在跨度+相邻上下文。
        alignment = row.get("span_alignment") or {} if kind == "exact_read" else {}
        dropped_parts = [
            normalize_chat_text(alignment.get(key) or "")
            for key in ("dropped_duplicate_authority_head", "dropped_duplicate_authority_tail")
        ]
        span_expected = expected_norm
        head_norm, tail_norm = dropped_parts
        if head_norm and span_expected.startswith(head_norm):
            span_expected = span_expected[len(head_norm):]
        if tail_norm and span_expected.endswith(tail_norm):
            span_expected = span_expected[: len(span_expected) - len(tail_norm)]
        repair_windows = _entity_repair_windows(
            final_text_srt=final_text_srt,
            final_speaker_srt=final_speaker_srt,
            start_ms=relative_start,
            end_ms=relative_end,
        )
        text_window, speaker_window, owner_text_window, owner_speaker_window = repair_windows
        dropped_ok = True
        if any(dropped_parts):
            context_start = max(0, relative_start - 30_000)
            context_end = min(delivery_end_ms - delivery_start_ms, relative_end + 30_000)
            text_context = normalize_srt_payload_window(
                final_text_srt, start_ms=context_start, end_ms=context_end
            )
            speaker_context = normalize_srt_payload_window(
                final_speaker_srt,
                start_ms=context_start,
                end_ms=context_end,
                strip_speaker_labels=True,
            )
            # 同一把尺（2026-07-14 生日结婚案）：apply 侧用 _fragment_spoken_in
            # coverage≥0.8 证明弃置头/尾已被说过（「抱抱李~」vs 口播「抱抱」），
            # 外层复证必须用同一谓词——子串全包含会把合法 0.85 覆盖误杀。
            dropped_ok = all(
                (not part)
                or (
                    _fragment_spoken_in(part, text_context)
                    and _fragment_spoken_in(part, speaker_context)
                )
                for part in dropped_parts
            )
            row["dropped_duplicate_context_verified"] = dropped_ok
        row["final_relative_start_ms"] = relative_start
        row["final_relative_end_ms"] = relative_end
        interjections = alignment.get("preserved_span_interjections") or ()
        text_check = _strip_interjections_once(
            text_window,
            interjections,
            required_substring=span_expected,
        )
        speaker_check = _strip_interjections_once(
            speaker_window,
            interjections,
            required_substring=span_expected,
        )
        _record_entity_repair_window_survival(
            row, span_expected=span_expected,
            text_window=text_window,
            speaker_window=speaker_window,
            owner_text_window=owner_text_window,
            owner_speaker_window=owner_speaker_window,
            text_check=text_check,
            speaker_check=speaker_check,
            dropped_ok=dropped_ok,
        )
        required_rows.append(row)
    audit["final_required_legacy_decision_count"] = len(required_rows)
    audit["final_required_source_truth_owner_count"] = source_owner_count
    audit["final_required_redelivery_baseline_owner_count"] = (
        baseline_owner_count
    )
    audit["final_required_decision_count"] = (
        len(required_rows) + source_owner_count + baseline_owner_count
    )
    audit["final_superseded_by_source_truth_count"] = superseded_by_truth
    audit["final_superseded_by_redelivery_baseline_count"] = (
        superseded_by_redelivery
    )
    audit["final_superseded_by_exact_final_cpa_count"] = (
        superseded_by_exact_final_cpa
    )
    audit["final_outside_delivery_count"] = (
        len(decision_rows)
        - len(required_rows)
        - superseded_by_truth
        - superseded_by_redelivery
        - superseded_by_exact_final_cpa
    )
    audit["final_boundary_required_exclusion_count"] = sum(
        row.get("final_verification_scope")
        == "BOUNDARY_REQUIRED_OWNER_EXCLUDED"
        for row in required_rows
    )
    return all(
        row.get("survived_final_text_srt") and row.get("survived_final_speaker_srt")
        for row in required_rows
    )
