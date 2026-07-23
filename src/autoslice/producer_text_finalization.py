"""Text-final SRT rendering and authority-surface verification."""

from __future__ import annotations

from .chat_authority import (
    _fragment_spoken_in,
    _strip_interjections_once,
    canonicalize_hard_meme_surfaces,
    normalize_chat_text,
    normalize_srt_payload_window,
    parse_srt_cues,
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


def _source_truth_pinned_intervals(
    audit: dict, final_text_srt: str
) -> list[tuple[int, int]]:
    """已应用/已满足的 ledger 钉子在交付时间轴上的 cue 区间。

    钉子是最高文本权威且最后落刀（2026-07-20 kmx r2 案：十麻乃钉子加了
    「的」，把更早的 sender 修复面的子串校验打破）——被钉子辖区覆盖的
    早期决策不再作为终稿存活要求。"""

    truth = audit.get("source_subtitle_truth_audit") or {}
    intervals: list[tuple[int, int]] = []
    indexes: set[int] = set()
    for key in ("applied", "satisfied"):
        for row in truth.get(key) or []:
            if row.get("final_owner_verified") is not True:
                continue
            windows = row.get("local_windows") or []
            if windows:
                # 首选：ledger 落刀时记录的交付时间轴辖区（layout 重排后
                # cue 序号会漂，时间不会——2026-07-20 kmx r3 案）。
                for window in windows:
                    intervals.append(
                        (int(window["start_ms"]), int(window["end_ms"]))
                    )
                continue
            for index in row.get("cue_indexes") or []:
                indexes.add(int(index))
    if indexes:
        cues = [cue for cue in parse_srt_cues(final_text_srt)]
        for index in sorted(indexes):
            if 1 <= index <= len(cues):
                intervals.append((cues[index - 1].start_ms, cues[index - 1].end_ms))
    return intervals


def _redelivery_baseline_intervals(audit: dict) -> list[tuple[int, int]]:
    baseline = audit.get("redelivery_subtitle_baseline_audit") or {}
    if baseline.get("status") not in {"APPLIED", "ALREADY_SATISFIED"}:
        return []
    return [
        (int(row["start_ms"]), int(row["end_ms"]))
        for row in baseline.get("mappings") or []
        if row.get("final_owner_verified") is True
    ]


def _window_payload(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    strip_speaker_labels: bool = False,
) -> str:
    return normalize_chat_text(
        normalize_srt_payload_window(
            srt_text,
            start_ms=max(0, start_ms),
            end_ms=max(start_ms + 1, end_ms),
            strip_speaker_labels=strip_speaker_labels,
        )
    )


def _verify_source_truth_owners(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
) -> tuple[bool, int]:
    truth = audit.get("source_subtitle_truth_audit") or {}
    rows: list[dict] = [
        row
        for key in ("applied", "satisfied")
        for row in truth.get(key) or []
        if isinstance(row, dict)
    ]
    required_count = 0
    failures: list[dict] = []
    for row in rows:
        contract = row.get("declared_output_contract") or {}
        action = str(contract.get("action") or row.get("action") or "")
        canonical_texts = contract.get("canonical_texts")
        if not isinstance(canonical_texts, list):
            canonical_texts = row.get("after") or []
        expected_exact = normalize_chat_text(
            "".join(str(value) for value in canonical_texts)
        )
        required_text = normalize_chat_text(
            str(contract.get("required_text") or "")
        )
        windows = row.get("local_windows") or []
        if not windows:
            row["final_owner_verified"] = False
            failures.append(
                {
                    "truth_id": row.get("truth_id"),
                    "reason_code": "SOURCE_TRUTH_FINAL_WINDOW_MISSING",
                }
            )
            continue
        window_results: list[dict] = []
        for window in windows:
            relative_start = int(window["start_ms"]) - delivery_start_ms
            relative_end = int(window["end_ms"]) - delivery_start_ms
            text_payload = _window_payload(
                final_text_srt,
                start_ms=relative_start,
                end_ms=relative_end,
            )
            speaker_payload = _window_payload(
                final_speaker_srt,
                start_ms=relative_start,
                end_ms=relative_end,
                strip_speaker_labels=True,
            )
            if action == "drop_cue":
                text_ok = not text_payload
                speaker_ok = not speaker_payload
            elif action == "replace_cue":
                text_ok = bool(expected_exact) and text_payload == expected_exact
                speaker_ok = (
                    bool(expected_exact) and speaker_payload == expected_exact
                )
            elif action == "replace_substring":
                expected = required_text or expected_exact
                text_ok = bool(expected) and expected in text_payload
                speaker_ok = bool(expected) and expected in speaker_payload
            else:
                text_ok = speaker_ok = False
            required_count += 1
            result = {
                "start_ms": relative_start,
                "end_ms": relative_end,
                "action": action,
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
        row["final_owner_windows"] = window_results
        row["final_owner_verified"] = bool(window_results) and all(
            item["text_ok"] and item["speaker_ok"] for item in window_results
        )
    audit["final_source_truth_owner_verification"] = {
        "status": "FAIL" if failures else "PASS",
        "required_window_count": required_count,
        "failures": failures,
    }
    return not failures, required_count


def _verify_redelivery_baseline_owners(
    audit: dict,
    *,
    final_text_srt: str,
    final_speaker_srt: str,
    source_truth_intervals: list[tuple[int, int]],
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
        if any(
            min(end_ms, truth_end) - max(start_ms, truth_start) >= 200
            for truth_start, truth_end in source_truth_intervals
        ):
            row["final_owner_scope"] = "SUPERSEDED_BY_SOURCE_TRUTH_OWNER"
            row["final_owner_verified"] = False
            continue
        expected = normalize_chat_text(
            str(row.get("text") or row.get("after") or "")
        )
        text_payload = _window_payload(
            final_text_srt, start_ms=start_ms, end_ms=end_ms
        )
        speaker_payload = _window_payload(
            final_speaker_srt,
            start_ms=start_ms,
            end_ms=end_ms,
            strip_speaker_labels=True,
        )
        text_ok = bool(expected) and text_payload == expected
        speaker_ok = bool(expected) and speaker_payload == expected
        required_count += 1
        row.update(
            {
                "final_owner_scope": "DELIVERY",
                "final_owner_expected": expected,
                "final_owner_text_payload": text_payload,
                "final_owner_speaker_payload": speaker_payload,
                "final_owner_verified": text_ok and speaker_ok,
            }
        )
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
    )
    if not source_owner_ok:
        audit["final_verification_failure"] = (
            "SOURCE_TRUTH_FINAL_OWNER_NOT_VERIFIED"
        )
        return False
    pinned_intervals = _source_truth_pinned_intervals(audit, final_text_srt)
    baseline_owner_ok, baseline_owner_count = (
        _verify_redelivery_baseline_owners(
            audit,
            final_text_srt=final_text_srt,
            final_speaker_srt=final_speaker_srt,
            source_truth_intervals=[
                (start - delivery_start_ms, end - delivery_start_ms)
                for start, end in pinned_intervals
            ],
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
        if any(
            min(matched_end, pin_end) - max(matched_start, pin_start) >= 200
            for pin_start, pin_end in pinned_intervals
        ):
            row["final_verification_scope"] = "SUPERSEDED_BY_SOURCE_TRUTH"
            superseded_by_truth += 1
            continue
        relative_matched_start = matched_start - delivery_start_ms
        relative_matched_end = matched_end - delivery_start_ms
        if not row.get("boundary_required") and any(
            min(relative_matched_end, pin_end)
            - max(relative_matched_start, pin_start)
            >= 200
            for pin_start, pin_end in redelivery_intervals
        ):
            row["final_verification_scope"] = (
                "SUPERSEDED_BY_REDELIVERY_BASELINE"
            )
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
        boundary_sliver = (
            overlap_ms < FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_MS
            and overlap_ratio < FINAL_AUTHORITY_BOUNDARY_SLIVER_MAX_RATIO
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
