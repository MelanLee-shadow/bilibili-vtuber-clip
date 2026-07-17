"""Text-final SRT rendering and authority-surface verification."""

from __future__ import annotations

from .chat_authority import (
    _fragment_spoken_in,
    _strip_interjections_once,
    canonicalize_hard_meme_surfaces,
    normalize_chat_text,
    normalize_srt_payload_window,
)


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
        ("sc_sender", row, str(row.get("after") or ""))
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
            str(row.get("structured_exact_text") or "")
            or "".join(str(value) for value in row.get("after") or []),
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
    required_rows: list[dict] = []
    for kind, row, expected_text in decision_rows:
        matched_start = int(row["matched_start_ms"])
        matched_end = int(row["matched_end_ms"])
        row["final_verification_kind"] = kind
        if matched_end <= delivery_start_ms or matched_start >= delivery_end_ms:
            row["final_verification_scope"] = "OUTSIDE_DELIVERY"
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
        text_check = _strip_interjections_once(text_window, interjections)
        speaker_check = _strip_interjections_once(speaker_window, interjections)
        row["survived_final_text_srt"] = bool(span_expected and span_expected in text_check) and dropped_ok
        row["survived_final_speaker_srt"] = bool(span_expected and span_expected in speaker_check) and dropped_ok
        required_rows.append(row)
    audit["final_required_decision_count"] = len(required_rows)
    audit["final_outside_delivery_count"] = len(decision_rows) - len(required_rows)
    return all(
        row.get("survived_final_text_srt") and row.get("survived_final_speaker_srt")
        for row in required_rows
    )
