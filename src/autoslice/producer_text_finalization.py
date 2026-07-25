"""Text-final SRT rendering and authority-surface verification."""

from __future__ import annotations

from .chat_authority import (
    _fragment_spoken_in,
    _strip_interjections_once,
    canonicalize_hard_meme_surfaces,
    normalize_chat_text,
    normalize_srt_payload_window,
)
from .chat_evidence import normalize_srt_owner_payload_window
from .redelivery_subtitle_baseline import MIN_ALIGNMENT_OVERLAP_MS
from .source_subtitle_truth import (
    MIN_CUE_OVERLAP_MS,
    source_truth_owner_windows,
    validated_source_truth_projection,
)

FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_MS = 250
FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_RATIO = 0.1


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
    before_payload = normalize_chat_text(
        "".join(str(row.get("before") or "") for row in touching)
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


def _window_payload(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    min_overlap_ms: int,
    strip_speaker_labels: bool = False,
) -> str:
    return normalize_chat_text(
        normalize_srt_owner_payload_window(
            srt_text,
            start_ms=max(0, start_ms),
            end_ms=max(start_ms + 1, end_ms),
            min_overlap_ms=min_overlap_ms,
            strip_speaker_labels=strip_speaker_labels,
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
        window_results: list[dict] = []
        for owned in inside_windows:
            relative_start = int(owned["start_ms"]) - delivery_start_ms
            relative_end = int(owned["end_ms"]) - delivery_start_ms
            text_payload = _window_payload(
                final_text_srt,
                start_ms=relative_start,
                end_ms=relative_end,
                min_overlap_ms=MIN_CUE_OVERLAP_MS,
            )
            speaker_payload = _window_payload(
                final_speaker_srt,
                start_ms=relative_start,
                end_ms=relative_end,
                min_overlap_ms=MIN_CUE_OVERLAP_MS,
                strip_speaker_labels=True,
            )
            expected_after_raw = owned["expected_after"]
            if expected_after_raw is None:
                text_ok = speaker_ok = True
            else:
                expected_after = normalize_chat_text(str(expected_after_raw))
                text_ok = text_payload == expected_after
                speaker_ok = speaker_payload == expected_after
            required_count += 1
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
            window_results.append(result)
            if not (text_ok and speaker_ok):
                failures.append(
                    {
                        "truth_id": row.get("truth_id"),
                        "reason_code": "SOURCE_TRUTH_FINAL_OWNER_MISMATCH",
                        **result,
                    }
                )
        row["final_owner_scope"] = "FINAL_DELIVERY"
        aggregate_text = "".join(str(item["text_payload"]) for item in window_results)
        aggregate_speaker = "".join(str(item["speaker_payload"]) for item in window_results)
        if action == "drop_cue":
            contract_text_ok = not aggregate_text
            contract_speaker_ok = not aggregate_speaker
        elif action == "replace_cue":
            contract_text_ok = bool(expected_exact) and aggregate_text == expected_exact
            contract_speaker_ok = bool(expected_exact) and aggregate_speaker == expected_exact
        elif action == "replace_substring":
            expected = required_text or expected_exact
            contract_text_ok = bool(expected) and expected in aggregate_text
            contract_speaker_ok = bool(expected) and expected in aggregate_speaker
        else:
            contract_text_ok = contract_speaker_ok = False
        if not (contract_text_ok and contract_speaker_ok):
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": ("SOURCE_TRUTH_FINAL_CONTRACT_MISMATCH"),
                    "action": action,
                    "expected_exact": expected_exact,
                    "required_text": required_text,
                    "text_payload": aggregate_text,
                    "speaker_payload": aggregate_speaker,
                    "text_ok": contract_text_ok,
                    "speaker_ok": contract_speaker_ok,
                }
            )
        row["final_owner_contract"] = {
            "action": action,
            "expected_exact": expected_exact,
            "required_text": required_text,
            "text_payload": aggregate_text,
            "speaker_payload": aggregate_speaker,
            "text_ok": contract_text_ok,
            "speaker_ok": contract_speaker_ok,
        }
        row["final_owner_windows"] = window_results
        row["final_owner_verified"] = (
            bool(window_results)
            and all(item["text_ok"] and item["speaker_ok"] for item in window_results)
            and contract_text_ok
            and contract_speaker_ok
        )
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
        if not (text_ok and speaker_ok):
            failures.append(
                {
                    "baseline_cue_index": row.get("baseline_cue_index"),
                    "reason_code": "REDELIVERY_BASELINE_FINAL_OWNER_MISMATCH",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "expected": expected,
                    "text_payload": text_payload,
                    "speaker_payload": speaker_payload,
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
        "failures": failures,
    }
    return not failures, required_count


def verify_chat_authority_final_surfaces(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
    delivery_end_ms: int,
) -> bool:
    """Verify every in-delivery authority decision at its original time span."""

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
    required_rows: list[dict] = []
    for kind, row, expected_text in decision_rows:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
        row["final_verification_kind"] = kind
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
        # ≤ 而非 <：250ms 正是片头 pad 常数，行 matched_end 恰好落在首 cue
        # 起点时 overlap 精确等于 250（1863 sender 案），刀刃值必须算 sliver。
        boundary_sliver = (
            overlap_ms <= FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_MS
            and overlap_ratio <= FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_RATIO
        )
        if overlap_ms == 0 or boundary_sliver:
            if row.get("boundary_required") is True:
                row["final_verification_scope"] = (
                    "BOUNDARY_REQUIRED_OWNER_EXCLUDED"
                )
                row["survived_final_text_srt"] = False
                row["survived_final_speaker_srt"] = False
                required_rows.append(row)
            else:
                row["final_verification_scope"] = "OUTSIDE_DELIVERY"
                if boundary_sliver:
                    row["final_verification_scope_reason"] = (
                        "BOUNDARY_SLIVER_BELOW_MEANINGFUL_AUDIO_THRESHOLD"
                    )
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
        text_window = normalize_srt_payload_window(
            final_text_srt, start_ms=relative_start, end_ms=relative_end
        )
        speaker_window = normalize_srt_payload_window(
            final_speaker_srt,
            start_ms=relative_start,
            end_ms=relative_end,
            strip_speaker_labels=True,
        )
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
        row["survived_final_text_srt"] = bool(span_expected and span_expected in text_check) and dropped_ok
        row["survived_final_speaker_srt"] = bool(span_expected and span_expected in speaker_check) and dropped_ok
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
    audit["final_outside_delivery_count"] = (
        len(decision_rows)
        - len(required_rows)
        - superseded_by_truth
        - superseded_by_redelivery
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
