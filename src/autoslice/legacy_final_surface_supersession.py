"""Receipt-bound final-surface supersession helpers.

The older exact-final helper in this module writes the historical
``reconciliation`` field.  The normal display-history path below uses a
separate field because the generic authority-row reader treats
``reconciliation`` as an instruction to skip a row.  A display successor is
replayed on every verification, so an old successful verification cannot
survive changed source/frame/final-subtitle bytes.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace

from .jingting_chunker import parse_srt_cues


NORMAL_HISTORY_DISPLAY_RECONCILIATION_SCHEMA = (
    "normal-history-display-owner-supersession.v1"
)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_repair_sha256(repair: Mapping[str, object]) -> str:
    return _sha256_text(
        json.dumps(
            dict(repair),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _normal_history_display_candidate(
    row: Mapping[str, object], *, expected_text: str,
) -> bool:
    """Recognize only the typed exact-final owner rows this path can retire."""

    digest = row.get("exact_final_repair_sha256")
    return bool(
        row.get("mode") == "exact_final_cpa_self_heal"
        and row.get("boundary_required") is False
        and row.get("decision_authority") == "CPA_JUDGE"
        and row.get("timing_immutable") is True
        and isinstance(expected_text, str)
        and bool(expected_text)
        and isinstance(digest, str)
        and len(digest) == 71
        and digest.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in digest[7:])
    )


def _normal_history_display_replays(
    audit: Mapping[str, object], *, final_text_srt: str,
) -> dict[str, object] | None:
    """Revalidate the complete current self-heal/display chain."""

    receipt = audit.get("exact_final_cpa_self_heal")
    if not isinstance(receipt, Mapping) or receipt.get("status") != "PASS":
        return None
    final_sha256 = _sha256_text(final_text_srt)
    try:
        from src.autoslice.exact_final_cpa_history import (
            read_successors, repair_history,
        )
        from src.autoslice.final_review_contract import (
            _validate_exact_final_cpa_self_heal,
        )

        _validate_exact_final_cpa_self_heal(
            audit,
            expected_srt_sha256=final_sha256,
            allow_pending=False,
        )
        repair_history(receipt)
        # A completed native review can start a new run at the previous
        # terminal SRT. Its retained typed/display receipts still own earlier
        # cues. Replay those original runs too; current CLEAN alone cannot
        # retire an owner from a retained run.
        runs = [entry["audit"] for entry in receipt.get("retained_previous_runs", [])]
        runs.append(receipt)
        passes: list[Mapping[str, object]] = []
        pass_inputs: dict[str, Mapping[str, object]] = {}
        origins: dict[int, dict[str, object]] = {}
        successors: list[dict] = []
        for run_index, run in enumerate(runs):
            _validate_exact_final_cpa_self_heal(
                {"exact_final_cpa_self_heal": run},
                expected_srt_sha256=run["final_srt_sha256"],
                allow_pending=False,
            )
            for pass_row in run["passes"]:
                input_hash = pass_row["input_srt_sha256"]
                prior = pass_inputs.get(input_hash)
                if prior is not None:
                    if prior != pass_row:
                        return None
                    continue
                pass_inputs[input_hash] = pass_row
                passes.append(pass_row)
                origins[len(passes)] = {
                    "run_final_srt_sha256": run["final_srt_sha256"],
                    "run_pass_index": pass_row["pass_index"],
                }
            for rows in read_successors(run).values():
                for value in rows:
                    if value not in successors:
                        successors.append(value)
    except (KeyError, OSError, TypeError, ValueError):
        return None

    review = audit.get("final_review_audit")
    if (
        not isinstance(review, Mapping)
        or review.get("status") != "CLEAN"
        or review.get("release_gate") != "PASS"
        or review.get("reviewed_srt_sha256") != final_sha256
    ):
        return None
    return {
        "receipt": receipt,
        "final_sha256": final_sha256,
        "successors": successors,
        "passes": passes,
        "pass_origins": origins,
    }


def _srt_cue_at(
    srt_text: str, *, start_ms: int, end_ms: int, text: str | None = None,
) -> list[object]:
    return [
        cue
        for cue in parse_srt_cues(srt_text)
        if cue.start_ms == start_ms
        and cue.end_ms == end_ms
        and (text is None or cue.text == text)
    ]


def _srt_cue_overlaps(srt_text: str, *, start_ms: int, end_ms: int) -> bool:
    return any(
        max(0, min(cue.end_ms, end_ms) - max(cue.start_ms, start_ms)) > 0
        for cue in parse_srt_cues(srt_text)
    )


def _speaker_owner_payload(srt_text: str, *, start_ms: int, end_ms: int) -> str:
    from src.autoslice.chat_evidence import normalize_srt_owner_payload_window

    return normalize_srt_owner_payload_window(
        srt_text,
        start_ms=start_ms,
        end_ms=end_ms,
        min_overlap_ms=1,
        strip_speaker_labels=True,
    )


def _display_successor_rows(
    replays: Mapping[str, object],
) -> list[dict[str, object]]:
    if "successors" in replays:
        return list(replays["successors"])
    # Older in-process callers supplied grouped replay results. Adapt that
    # representation by shape; the type set still comes only from the reader.
    rows = []
    for collection in replays.values():
        if isinstance(collection, Mapping):
            for group in collection.values():
                for row in group if isinstance(group, list) else [group]:
                    if isinstance(row, dict) and {"input_srt_text", "output_srt_text"} <= row.keys():
                        rows.append(row)
    return rows


def _render_srt_cues(cues: list[object]) -> str:
    blocks = []
    for cue in cues:
        hours, remainder = divmod(cue.start_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        start = f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
        hours, remainder = divmod(cue.end_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1_000)
        end = f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
        blocks.append(f"{cue.index}\n{start} --> {end}\n{cue.text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _replay_typed_pass(
    text: str, pass_row: Mapping[str, object], *, reverse: bool,
) -> str | None:
    """Replay one typed CPA pass in either direction and verify its hash."""

    input_hash = pass_row.get("input_srt_sha256")
    output_hash = pass_row.get("output_srt_sha256")
    if not isinstance(input_hash, str) or not isinstance(output_hash, str):
        return None
    expected_hash = input_hash if reverse else output_hash
    if _sha256_text(text) != (output_hash if reverse else input_hash):
        return None
    repairs = pass_row.get("repairs")
    if not isinstance(repairs, list) or not repairs:
        return None
    ordered = list(reversed(repairs)) if reverse else repairs
    cues = parse_srt_cues(text)
    for repair in ordered:
        if not isinstance(repair, Mapping):
            return None
        cue_index = repair.get("cue_index")
        if isinstance(cue_index, bool):
            return None
        try:
            cue_index = int(cue_index)
            start_ms = int(repair["matched_start_ms"])
            end_ms = int(repair["matched_end_ms"])
        except (KeyError, TypeError, ValueError):
            return None
        old_text = repair.get("after" if reverse else "before")
        new_text = repair.get("before" if reverse else "after")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return None
        matches = [
            index for index, cue in enumerate(cues)
            if str(cue.index) == str(cue_index)
            and cue.start_ms == start_ms
            and cue.end_ms == end_ms
            and cue.text == old_text
        ]
        if len(matches) != 1:
            return None
        index = matches[0]
        cues[index] = replace(cues[index], text=new_text)
    rendered = _render_srt_cues(cues)
    if _sha256_text(rendered) != expected_hash:
        return None
    return rendered


def _normal_history_display_retires_decision_row(
    row: dict,
    *,
    expected_text: str,
    audit: Mapping[str, object],
    final_text_srt: str,
    final_speaker_srt: str,
    delivery_start_ms: int,
    replays: Mapping[str, object],
) -> bool:
    """Retire one exact-final owner only through a replayed display chain."""

    # A prior successful verification is never evidence for this invocation.
    row.pop("normal_history_display_reconciliation", None)
    if not _normal_history_display_candidate(row, expected_text=expected_text):
        return False
    receipt = replays.get("receipt")
    if not isinstance(receipt, Mapping):
        return False
    try:
        owner_start = int(row["matched_start_ms"])
        owner_end = int(row["matched_end_ms"])
        local_start = owner_start - delivery_start_ms
        local_end = owner_end - delivery_start_ms
    except (KeyError, TypeError, ValueError):
        return False
    if not 0 <= local_start < local_end:
        return False

    passes = replays.get("passes", receipt.get("passes"))
    if not isinstance(passes, list):
        return False
    matching: list[tuple[int, Mapping[str, object], Mapping[str, object]]] = []
    for pass_index, pass_row in enumerate(passes, start=1):
        if not isinstance(pass_row, Mapping):
            return False
        repairs = pass_row.get("repairs")
        if not isinstance(repairs, list):
            return False
        for repair in repairs:
            if not isinstance(repair, Mapping):
                return False
            if _canonical_repair_sha256(repair) == row.get("exact_final_repair_sha256"):
                matching.append((pass_index, pass_row, repair))
    if len(matching) != 1:
        return False
    pass_index, pass_row, repair = matching[0]
    before = repair.get("before")
    after = repair.get("after")
    if (
        not isinstance(before, str)
        or not before
        or not isinstance(after, str)
        or not after
        or expected_text != after
        or row.get("before") != [before]
        or row.get("after") != [after]
        or row.get("structured_exact_text") != after
        or row.get("request_sha256") != repair.get("request_sha256")
        or row.get("cue_indexes") != [repair.get("cue_index")]
        or row.get("matched_start_ms")
        != delivery_start_ms + repair.get("matched_start_ms", -1)
        or row.get("matched_end_ms")
        != delivery_start_ms + repair.get("matched_end_ms", -1)
        or repair.get("decision_authority") != "CPA_JUDGE"
        or repair.get("timing_immutable") is not True
    ):
        return False
    mutation = repair.get("mutation_authority")
    if (
        not isinstance(mutation, Mapping)
        or mutation.get("schema_version")
        != "subtitle-correction-mutation-authority.v1"
        or mutation.get("status") != "PASS"
    ):
        return False
    local_repair_start = repair.get("matched_start_ms")
    local_repair_end = repair.get("matched_end_ms")
    if (
        not isinstance(local_repair_start, int)
        or isinstance(local_repair_start, bool)
        or not isinstance(local_repair_end, int)
        or isinstance(local_repair_end, bool)
        or local_repair_start != local_start
        or local_repair_end != local_end
    ):
        return False

    output_hash = pass_row.get("output_srt_sha256")
    if not isinstance(output_hash, str):
        return False
    pass_edges: dict[str, tuple[int, Mapping[str, object]]] = {}
    pass_outputs: dict[str, tuple[int, Mapping[str, object]]] = {}
    for later_index, later_pass in enumerate(passes, start=1):
        if not isinstance(later_pass, Mapping):
            return False
        input_hash = later_pass.get("input_srt_sha256")
        later_output_hash = later_pass.get("output_srt_sha256")
        if (
            not isinstance(input_hash, str)
            or not isinstance(later_output_hash, str)
            or input_hash in pass_edges
            or later_output_hash in pass_outputs
        ):
            return False
        pass_edges[input_hash] = (later_index, later_pass)
        pass_outputs[later_output_hash] = (later_index, later_pass)
    successors = _display_successor_rows(replays)
    by_input: dict[str, list[dict[str, object]]] = {}
    by_output: dict[str, list[dict[str, object]]] = {}
    for successor in successors:
        input_hash = successor.get("input_srt_sha256")
        successor_output_hash = successor.get("output_srt_sha256")
        if not isinstance(input_hash, str) or not isinstance(successor_output_hash, str):
            return False
        by_input.setdefault(input_hash, []).append(successor)
        by_output.setdefault(successor_output_hash, []).append(successor)

    final_hash = replays.get("final_sha256")
    if not isinstance(final_hash, str) or _sha256_text(final_text_srt) != final_hash:
        return False

    # The receipt stores pass hashes but not their full SRT bytes.  Reconstruct
    # the text at the owner pass output by walking backwards from the current
    # final bytes: successor rows provide both texts, while typed CPA passes
    # are replayed in reverse.  This makes a later unrelated CPA edit part of
    # the proof instead of comparing its changed full SRT to a stale snapshot.
    owner_output_text = final_text_srt
    backward_hash = final_hash
    backward_seen: set[str] = set()
    while backward_hash != output_hash:
        if backward_hash in backward_seen:
            return False
        backward_seen.add(backward_hash)
        successor_candidates = by_output.get(backward_hash, [])
        pass_candidate = pass_outputs.get(backward_hash)
        edge_count = len(successor_candidates) + (1 if pass_candidate else 0)
        if edge_count != 1:
            return False
        if successor_candidates:
            successor = successor_candidates[0]
            output_text = successor.get("output_srt_text")
            input_text = successor.get("input_srt_text")
            input_hash = successor.get("input_srt_sha256")
            if (
                not isinstance(output_text, str)
                or not isinstance(input_text, str)
                or not isinstance(input_hash, str)
                or output_text != owner_output_text
                or _sha256_text(output_text) != backward_hash
                or _sha256_text(input_text) != input_hash
            ):
                return False
            owner_output_text = input_text
            backward_hash = input_hash
            continue
        later_index, later_pass = pass_candidate
        if later_index <= pass_index:
            return False
        reconstructed = _replay_typed_pass(
            owner_output_text, later_pass, reverse=True,
        )
        if reconstructed is None:
            return False
        input_hash = later_pass.get("input_srt_sha256")
        if not isinstance(input_hash, str):
            return False
        owner_output_text = reconstructed
        backward_hash = input_hash

    owner_input_text = _replay_typed_pass(
        owner_output_text, pass_row, reverse=True,
    )
    if owner_input_text is None:
        return False
    owner_before_cues = _srt_cue_at(
        owner_input_text,
        start_ms=local_start,
        end_ms=local_end,
        text=before,
    )
    owner_after_cues = _srt_cue_at(
        owner_output_text,
        start_ms=local_start,
        end_ms=local_end,
        text=after,
    )
    if len(owner_before_cues) != 1 or len(owner_after_cues) != 1:
        return False

    # Now follow the same graph forwards.  Every intermediate CPA edge is
    # replayed on the current full SRT before the next display row is checked.
    current_hash = output_hash
    current_text = owner_output_text
    current_owner_text = after
    current_owner_index = owner_after_cues[0].index
    display_path: str | None = None
    successor_receipt: dict[str, object] | None = None
    typed_owner_successors: list[dict[str, object]] = []
    for _ in range(len(successors) + len(passes) + 1):
        if current_hash == final_hash:
            break
        next_rows = by_input.get(current_hash, [])
        if len(next_rows) > 1:
            return False
        next_pass = pass_edges.get(current_hash)
        if next_rows and next_pass is not None:
            return False
        if not next_rows:
            if next_pass is None:
                return False
            next_pass_index, next_pass_row = next_pass
            if next_pass_index <= pass_index:
                return False
            # Canonical display normalization may precede another real typed
            # repair of this owner. Its complete before/after text and immutable
            # span are authority; the whole pass is still replayed below. A
            # recycled ordinal after full removal never identifies this owner.
            for later_repair in next_pass_row.get("repairs", []):
                if not isinstance(later_repair, Mapping):
                    return False
                same_span = (
                    later_repair.get("matched_start_ms") == local_start
                    and later_repair.get("matched_end_ms") == local_end
                )
                same_ordinal = str(later_repair.get("cue_index")) == str(current_owner_index)
                if not same_span and (display_path == "FULL_VISUAL_DEDUP" or not same_ordinal):
                    continue
                mutation = later_repair.get("mutation_authority")
                after_text = later_repair.get("after")
                if (
                    display_path not in {"CANONICAL_NORMALIZATION", "OVERLAP_TIMING_ONLY", "RELEASE_GRADE_MERGE"}
                    or not same_span
                    or later_repair.get("before") != current_owner_text
                    or not isinstance(after_text, str)
                    or not after_text
                    or later_repair.get("decision_authority") != "CPA_JUDGE"
                    or later_repair.get("timing_immutable") is not True
                    or not isinstance(mutation, Mapping)
                    or mutation.get("schema_version") != "subtitle-correction-mutation-authority.v1"
                    or mutation.get("status") != "PASS"
                    or later_repair.get("before_sha256") != _sha256_text(current_owner_text)
                    or later_repair.get("after_sha256") != _sha256_text(after_text)
                ):
                    return False
                current_owner_text = after_text
                typed_owner_successors.append({
                    "repair_sha256": _canonical_repair_sha256(later_repair),
                    "request_sha256": later_repair.get("request_sha256"),
                    "input_srt_sha256": next_pass_row.get("input_srt_sha256"),
                    "output_srt_sha256": next_pass_row.get("output_srt_sha256"),
                })
            next_output = next_pass_row.get("output_srt_sha256")
            if not isinstance(next_output, str):
                return False
            current_text = _replay_typed_pass(
                current_text, next_pass_row, reverse=False,
            )
            if current_text is None:
                return False
            current_hash = next_output
            continue
        successor = next_rows[0]
        output_text = successor.get("output_srt_text")
        input_text = successor.get("input_srt_text")
        if not isinstance(input_text, str) or not isinstance(output_text, str):
            return False
        if (
            _sha256_text(input_text) != current_hash
            or input_text != current_text
        ):
            return False
        next_hash = successor.get("output_srt_sha256")
        if not isinstance(next_hash, str) or _sha256_text(output_text) != next_hash:
            return False
        owner_cues = _srt_cue_at(
            input_text,
            start_ms=local_start,
            end_ms=local_end,
            text=current_owner_text,
        )
        if len(owner_cues) > 1:
            return False
        from src.autoslice.exact_final_cpa_history import RELEASE_GRADE_MERGE_SCHEMA
        if successor.get("schema_version") == RELEASE_GRADE_MERGE_SCHEMA:
            if len(owner_cues) != 1:
                return False
            mappings = [m for m in successor["cue_mapping"]
                        if m["input_cue_index"] == owner_cues[0].index
                        and m["input_window_ms"] == [local_start, local_end]
                        and m["input_text"] == current_owner_text]
            if len(mappings) != 1:
                return False
            mapped = mappings[0]
            local_start, local_end = mapped["output_window_ms"]
            current_owner_text = mapped["output_text"]
            current_owner_index = mapped["output_cue_index"]
            display_path = "RELEASE_GRADE_MERGE"
            successor_receipt = successor
        if successor.get("schema_version") == "exact-final-overlap-timing-successor.v1":
            output_cues = [cue for cue in parse_srt_cues(output_text)
                           if owner_cues and cue.index == owner_cues[0].index]
            if len(owner_cues) != 1 or len(output_cues) != 1:
                return False
            mapped = output_cues[0]
            if mapped.start_ms != local_start or mapped.text != current_owner_text:
                return False
            local_end = mapped.end_ms
            display_path = "OVERLAP_TIMING_ONLY"
            successor_receipt = successor
        nested_audit = successor.get("dedup_audit")
        if owner_cues and isinstance(nested_audit, Mapping):
            dropped = nested_audit.get("dropped_input_cue_indexes")
            matched = nested_audit.get("matched_observations")
            owner_index = int(owner_cues[0].index)
            if (
                nested_audit.get("status") == "APPLIED_DISPLAY_DEDUP"
                and isinstance(dropped, list)
                and owner_index in dropped
                and isinstance(matched, Mapping)
                and bool(matched.get(str(owner_index)))
                and nested_audit.get("publication_authority") is False
                and nested_audit.get("speaker_identity_proven") is False
            ):
                display_path = "FULL_VISUAL_DEDUP"
                successor_receipt = successor
        normalization = successor.get("normalization_audit")
        if owner_cues and isinstance(normalization, Mapping):
            if normalization.get("status") != "APPLIED":
                return False
            output_cues = _srt_cue_at(
                output_text, start_ms=local_start, end_ms=local_end,
            )
            if len(output_cues) != 1:
                return False
            if output_cues[0].start_ms != local_start or output_cues[0].end_ms != local_end:
                return False
            if output_cues[0].text != current_owner_text:
                display_path = "CANONICAL_NORMALIZATION"
                current_owner_text = output_cues[0].text
                successor_receipt = successor
        surviving = _srt_cue_at(output_text, start_ms=local_start, end_ms=local_end,
                               text=current_owner_text)
        if len(surviving) == 1:
            current_owner_index = surviving[0].index
        current_text = output_text
        current_hash = next_hash
    else:
        return False
    if display_path is None or successor_receipt is None:
        return False
    if current_hash != replays.get("final_sha256"):
        return False

    final_display_cues = _srt_cue_at(
        final_text_srt, start_ms=local_start, end_ms=local_end,
    )
    speaker_payload = _speaker_owner_payload(
        final_speaker_srt, start_ms=local_start, end_ms=local_end,
    )
    from src.autoslice.chat_authority import normalize_chat_text

    if display_path == "FULL_VISUAL_DEDUP":
        # Visual dedup does not prove speaker identity.  The ordinary host
        # track may retain the source cue or may have no subtitle at the same
        # span; both are valid while the independent visual evidence remains
        # fully replayable.
        if speaker_payload not in {"", normalize_chat_text(current_owner_text)}:
            return False
    elif speaker_payload != normalize_chat_text(current_owner_text):
        return False
    if display_path == "FULL_VISUAL_DEDUP":
        if _srt_cue_overlaps(
            final_text_srt, start_ms=local_start, end_ms=local_end,
        ):
            return False
    else:
        if len(final_display_cues) != 1 or final_display_cues[0].text != current_owner_text:
            return False
        if (
            final_display_cues[0].start_ms != local_start
            or final_display_cues[0].end_ms != local_end
        ):
            return False
    successor_receipt = dict(successor_receipt)
    row["normal_history_display_reconciliation"] = {
        "schema_version": NORMAL_HISTORY_DISPLAY_RECONCILIATION_SCHEMA,
        "status": "SUPERSEDED_BY_NORMAL_DISPLAY_HISTORY",
        "display_path": display_path,
        "exact_final_repair_sha256": row["exact_final_repair_sha256"],
        "repair_pass_index": pass_index,
        **dict(replays.get("pass_origins", {}).get(pass_index, {})),
        "cue_index": repair.get("cue_index"),
        "matched_start_ms": owner_start,
        "matched_end_ms": owner_end,
        "before_sha256": repair.get("before_sha256"),
        "after_sha256": repair.get("after_sha256"),
        "request_sha256": repair.get("request_sha256"),
        "successor": {
            "schema_version": successor_receipt.get("schema_version"),
            "after_pass_index": successor_receipt.get("after_pass_index"),
            "input_srt_sha256": successor_receipt.get("input_srt_sha256"),
            "output_srt_sha256": successor_receipt.get("output_srt_sha256"),
        },
        "typed_owner_successors": typed_owner_successors,
        "final_text_srt_sha256": replays.get("final_sha256"),
        "final_speaker_srt_sha256": _sha256_text(final_speaker_srt),
        "timing_immutable": (local_start, local_end) == (
            owner_start - delivery_start_ms, owner_end - delivery_start_ms,
        ),
        "final_owner_window_ms": [local_start, local_end],
    }
    return True


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
