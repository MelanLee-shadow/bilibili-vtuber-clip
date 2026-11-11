"""Replay outside-span questions from actual typed repair history.

This is not a second review system: questions return to the ordinary finding
agenda, and only ordinary typed repairs or the shared release-resolved KEEP
contract close them.
Omitting a question in a fresh CLEAN response never resolves it.
"""

from __future__ import annotations

from .review_evidence_store import cold_input, persisted_upgrade, persisted_view
from collections.abc import Mapping
from copy import deepcopy

from src.autoslice.span_phoneme_resolution import (
    FIELD,
    POLICY,
    digest,
    text_sha,
    validated_resolution,
)

UNAPPLIED_FIELD = "span_unapplied_questions"
REVIEW_FIELD = "span_obligation_review"
ORIGIN_FIELD = "origin_span_obligation"
# The historical writer's closed row shape. A future writer field must get
# its own upgrade; historical replay must not silently inherit it.
_PRE_CLOCK_STATE_FIELDS = frozenset({
    "obligation_id", "cue_index", "current_cue", "base_text_sha256",
    "codepoint_span", "cue_window_ms", "current_text", "status", "origin", "resolution",
})


@cold_input
def repair_chain(authority: Mapping) -> list[dict]:
    receipt = authority.get("exact_final_cpa_self_heal")
    if receipt is None:
        receipt = (authority.get("final_review_audit") or {}).get("exact_final_cpa_self_heal")
    if not isinstance(receipt, Mapping):
        return []
    from src.autoslice.exact_final_cpa_history import repair_history

    repair_history(receipt)  # Validate retained-run seals with their owner.
    runs = [entry["audit"] for entry in receipt.get("retained_previous_runs", [])] + [receipt]
    return [
        deepcopy(dict(r))
        for run in runs
        for p in run.get("passes", [])
        for r in p.get("repairs", [])
    ]


@cold_input
def validate_span_repair(repair: Mapping) -> dict | None:
    marked = (
        FIELD in repair
        or repair.get("policy_branch") == POLICY
        or (repair.get("mutation_authority") or {}).get("basis") == POLICY
    )
    if not marked:
        return None
    value = validated_resolution(
        repair.get(FIELD),
        current=repair.get("before"),
        proposed=repair.get("after"),
        window=(repair.get("matched_start_ms"), repair.get("matched_end_ms")),
    )
    if (
        repair.get("policy_branch") != POLICY
        or repair.get("judge") != value["judge"]
        or repair.get("before_sha256") != "sha256:" + text_sha(value["request"]["current_cue"])
        or repair.get("after_sha256") != "sha256:" + text_sha(value["request"]["proposed_cue"])
        or repair.get("mutation_authority")
        != {
            "schema_version": "subtitle-correction-mutation-authority.v1",
            "status": "PASS",
            "basis": POLICY,
        }
    ):
        raise ValueError("SPAN_REPAIR_RECEIPT_INVALID")
    return value


def _difference(before: str, after: str) -> tuple[int, int, int]:
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    suffix = 0
    while (
        suffix < min(len(before), len(after)) - start and before[-1 - suffix] == after[-1 - suffix]
    ):
        suffix += 1
    return start, len(before) - suffix, len(after) - len(before)


def _closed_set_current_has_full_utterance_support(adjudication) -> bool:
    """Require CPA's complete, request-bound CURRENT support receipt."""
    from src.autoslice.acoustic_witness_adjudication import valid_current_utterance_support

    if not isinstance(adjudication, Mapping):
        return False
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    if not isinstance(request, Mapping) or not isinstance(judge, Mapping):
        return False
    try:
        return valid_current_utterance_support(
            check_request=request,
            judge=judge,
            witness_judge=witness_judge,
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _valid_closing_adjudication(adjudication, *, before, after, window, keep=False,
                                finding=None, reviewed_srt_sha256=None):
    if not isinstance(adjudication, Mapping):
        return False
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = witness_judge.get("judge") if isinstance(witness_judge, Mapping) else None
    if not isinstance(request, Mapping):
        return False
    try:
        request_sha = digest({k: v for k, v in request.items() if k != "request_sha256"})
    except (TypeError, ValueError):
        return False
    if (
        adjudication.get("status") != "OBSERVED"
        or adjudication.get("timing_immutable") is not True
        or adjudication.get("decision_authority") != "CPA_JUDGE"
        or request.get("current_cue") != before
        or request.get("base_text_sha256") != text_sha(before)
        or [request.get("matched_start_ms"), request.get("matched_end_ms")] != list(window)
        or request_sha != request.get("request_sha256")
    ):
        return False
    if keep:
        from src.autoslice.final_review_contract import is_release_resolved_keep, SUB18_KEEP_BRANCH

        # C needs the original row's typed meaning/risk proof; do not infer it
        # from an adjudication label or manufacture an audio witness for it.
        row = dict(finding) if isinstance(finding, Mapping) else {
            "base_text_sha256": text_sha(before),
        }
        row["exact_release_adjudication"] = adjudication
        if row.get("base_text_sha256") != text_sha(before) or not is_release_resolved_keep(row):
            return False
        risk = row.get("current_content_risk")
        if (adjudication.get("policy_branch") == SUB18_KEEP_BRANCH
                and reviewed_srt_sha256 is not None and isinstance(risk, Mapping)
                and str(risk.get("reviewed_srt_sha256")).removeprefix("sha256:")
                != str(reviewed_srt_sha256).removeprefix("sha256:")):
            return False
        if adjudication.get('terminal_review'):
            return True
        from src.autoslice.final_review_contract import is_sub18_context_meaning_equivalent_keep
        if is_sub18_context_meaning_equivalent_keep(row):
            return True
        from src.autoslice.span_phoneme_resolution import observation_catalogue

        if not _closed_set_current_has_full_utterance_support(adjudication):
            return False
        verdict = adjudication.get("verdict")
        if not isinstance(verdict, Mapping):
            return False
        try:
            rows, invalid = observation_catalogue(request, verdict)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            return False
        return (
            adjudication.get("repaired") is False
            and judge.get("choice") == "CURRENT"
            and bool(rows)
            and not invalid
        )
    if (not isinstance(judge, Mapping) or judge.get("status") != "JUDGED"
            or judge.get("check_request_sha256") != request.get("request_sha256")):
        return False
    if (
        adjudication.get("repaired") is not True
        or request.get("proposed_cue") != after
        or judge.get("choice") != "PROPOSED"
    ):
        return False
    from src.autoslice.span_phoneme_resolution import observation_catalogue

    rows, invalid = observation_catalogue(request, adjudication.get("verdict") or {})
    if not rows or invalid:
        return False
    from src.autoslice.final_review_auditor import audit_correction_mutation_authority

    receipt = {
        "status": "APPLIED",
        "applied_count": 1,
        "findings": [
            {"routed": "context_audio_adjudicated_fix", "context_audio_adjudication": adjudication}
        ],
    }
    return audit_correction_mutation_authority(receipt)["status"] == "PASS"


def _born_questions(value: Mapping, after: str, *, applied: bool) -> dict:
    pending = {}
    binding = value["binding"]
    target_start, target_end = binding["codepoint_span"]
    delta = len(binding["replacement"]) - (target_end - target_start) if applied else 0
    grouped = []
    for question in sorted(value["outside_findings"], key=lambda q: q["codepoint_span"]):
        a, b = question["codepoint_span"]
        if grouped and a < grouped[-1][0][1]:
            grouped[-1][0][1] = max(b, grouped[-1][0][1])
            grouped[-1][1].append(question)
        else:
            grouped.append(([a, b], [question]))
    for interval, questions in grouped:
        oid = digest({"parent_scope_sha256": value["receipt_sha256"], "codepoint_span": interval})
        left, right = interval
        projected = [left + delta, right + delta] if left >= target_end else [left, right]
        pending[oid] = {
            "obligation_id": oid,
            "cue_index": binding["cue_index"],
            "current_cue": after,
            "base_text_sha256": text_sha(after),
            "codepoint_span": projected,
            "cue_window_ms": binding["cue_window_ms"],
            "current_text": after[projected[0] : projected[1]],
            "status": "UNRESOLVED",
            "origin": {
                "parent_scope_sha256": value["receipt_sha256"],
                "original_cue_sha256": binding["current_cue_sha256"],
                "original_codepoint_span": list(interval),
                "questions": deepcopy(questions),
            },
            "resolution": None,
        }
        if binding["source_offset_ms"]:
            pending[oid]["source_offset_ms"] = binding["source_offset_ms"]
    return pending


def _physical_window(row):
    offset = row.get("source_offset_ms", 0)
    return [point + offset for point in row["cue_window_ms"]]


def _repair_source_offset(repair, span_value):
    """Rebase only from the typed writer's validated clock, never text search."""
    if span_value is not None:
        return span_value["binding"]["source_offset_ms"]
    if repair.get("editorial_authority") is not None:
        from .editorial_subtitle import validate_repair
        validate_repair(repair)
        return repair["editorial_authority"]["request"]["source_media_timeline_offset_ms"]
    for request in (
        (repair.get("origin_span_adjudication") or {}).get("request"),
        ((repair.get("judge") or {}).get("span_compilation") or {}).get("request"),
    ):
        if (isinstance(request, Mapping) and request.get("current_cue") == repair.get("before")
                and [request.get("matched_start_ms"), request.get("matched_end_ms")]
                == [repair.get("matched_start_ms"), repair.get("matched_end_ms")]
                and type(request.get("source_media_timeline_offset_ms")) is int
                and digest({k: v for k, v in request.items() if k != "request_sha256"})
                == request.get("request_sha256")):
            return request["source_media_timeline_offset_ms"]
    return None


def derive_obligations(
    repairs: list[Mapping], closures: list[Mapping] = (), unapplied: list[Mapping] = (),
    *, reviewed_srt_sha256: str | None = None,
) -> list[dict]:
    return _derive_obligations(repairs, closures, unapplied,
                               reviewed_srt_sha256=reviewed_srt_sha256)


def _derive_obligations(repairs, closures=(), unapplied=(), *, legacy=False,
                        reviewed_srt_sha256=None):
    """Replay current meaning or the exact pre-clock/pre-reanchor v1 form.

    Legacy replay is only a recognizer for sealed historical rows. It is never
    used to decide release, create findings, or write a new review.
    """
    from src.autoslice.span_phoneme_resolution import validate_question_packet

    def born(value, after, *, applied):
        rows = _born_questions(value, after, applied=applied)
        if legacy:
            rows = {oid: {key: value for key, value in row.items()
                          if key in _PRE_CLOCK_STATE_FIELDS} for oid, row in rows.items()}
        return rows

    births = {}
    for record in unapplied:
        count = record.get("after_repair_count")
        if (
            type(count) is not int
            or not 0 <= count <= len(repairs)
            or digest(repairs[:count]) != record.get("repair_prefix_sha256")
        ):
            raise ValueError("SPAN_PENDING_REPAIR_HISTORY_DRIFT")
        value = validate_question_packet(record.get("packet"))
        births.setdefault(count, []).append(value)
    pending = {}
    for number in range(len(repairs) + 1):
        for value in births.get(number, []):
            pending.update(born(value, value["request"]["current_cue"], applied=False))
        if number == len(repairs):
            break
        repair = repairs[number]
        before, after = repair.get("before"), repair.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise ValueError("SPAN_OBLIGATION_REPAIR_HISTORY_INVALID")
        index = repair.get("cue_index")
        window = [repair.get("matched_start_ms"), repair.get("matched_end_ms")]
        value = validate_span_repair(repair)
        offset = None if legacy else _repair_source_offset(repair, value)
        change_start, change_end, delta = _difference(before, after)
        for oid, row in pending.items():
            if row["status"] == "RESOLVED":
                continue
            physical_match = (offset is not None
                and _physical_window(row) == [point + offset for point in window])
            if offset is not None and not physical_match:
                continue
            if row["cue_index"] != index and not physical_match:
                continue
            left, right = row["codepoint_span"]
            if before != row["current_cue"] or not (
                physical_match or (offset is None and window == row["cue_window_ms"])
            ):
                row["status"] = "LOCATION_STALE"
                continue
            row["cue_index"] = index
            row["cue_window_ms"] = window
            if offset is not None and (offset or "source_offset_ms" in row):
                row["source_offset_ms"] = offset
            origin = repair.get(ORIGIN_FIELD) or {}
            if (
                oid in [origin.get("obligation_id"), *(origin.get("obligation_ids") or [])]
                and left <= change_start <= change_end <= right
                and before != after
                and _valid_closing_adjudication(
                    repair.get("origin_span_adjudication"),
                    before=before,
                    after=after,
                    window=window,
                )
            ):
                row["status"] = "RESOLVED"
                row["resolution"] = {
                    "kind": "ORDINARY_TYPED_REPAIR",
                    "repair_sha256": digest(repair),
                }
                continue
            if change_end <= left:
                row["codepoint_span"] = [left + delta, right + delta]
            elif change_start >= right:
                pass
            else:
                # An independent valid edit is not a ruling on this question.
                # Its sealed before/after and exact physical window permit a
                # fresh whole-current-cue question, not guessed word alignment
                # or a release. Retain the original question and repair edge.
                if legacy or not after.strip() or row["status"] == "LOCATION_STALE":
                    row["status"] = "LOCATION_STALE"
                    continue
                row["codepoint_span"] = [0, len(after)]
                row.setdefault("reanchor_chain", []).append({
                    "repair_sha256": digest(repair),
                    "before_sha256": text_sha(before), "after_sha256": text_sha(after),
                    "physical_cue_window_ms": _physical_window(row),
                    "action": "REVIEW_CURRENT_CUE_REQUIRED",
                })
            row["current_cue"] = after
            row["base_text_sha256"] = text_sha(after)
            if not legacy:
                a, b = row["codepoint_span"]
                row["current_text"] = after[a:b]
        if value is None:
            continue
        pending.update(born(value, after, applied=True))
    for closure in closures:
        oid = closure.get("obligation_id")
        if oid not in pending:
            raise ValueError("SPAN_ORPHAN_CLOSURE")
        row = pending[oid]
        adjudication = closure.get("adjudication")
        request = adjudication.get("request") if isinstance(adjudication, Mapping) else None
        from src.autoslice.final_review_contract import SUB18_KEEP_BRANCH
        finding = closure.get("finding")
        risk = finding.get("current_content_risk") if isinstance(finding, Mapping) else None
        historical = (not legacy and reviewed_srt_sha256 is not None
                      and isinstance(adjudication, Mapping)
                      and adjudication.get("policy_branch") == SUB18_KEEP_BRANCH
                      and isinstance(risk, Mapping)
                      and str(risk.get("reviewed_srt_sha256")).removeprefix("sha256:")
                      != str(reviewed_srt_sha256).removeprefix("sha256:"))
        if historical:
            # Verify the original ruling on its own terms before retaining it
            # as history. It cannot close the question on a different SRT.
            span = closure.get("codepoint_span")
            before = request.get("current_cue") if isinstance(request, Mapping) else None
            window = ([request.get("matched_start_ms"), request.get("matched_end_ms")]
                      if isinstance(request, Mapping) else [])
            offset = request.get("source_media_timeline_offset_ms", 0) if isinstance(request, Mapping) else None
            if (not isinstance(before, str) or not isinstance(span, list) or len(span) != 2
                    or any(type(p) is not int for p in span)
                    or not 0 <= span[0] <= span[1] <= len(before)
                    or type(offset) is not int or any(type(p) is not int for p in window)
                    or [p + offset for p in window] != _physical_window(row)
                    or not _valid_closing_adjudication(
                        adjudication, before=before, after=before, window=window,
                        keep=True, finding=finding)):
                raise ValueError("SPAN_KEEP_CLOSURE_INVALID")
            continue
        window = row["cue_window_ms"]
        if not legacy and isinstance(request, Mapping):
            candidate_window = [request.get("matched_start_ms"), request.get("matched_end_ms")]
            offset = request.get("source_media_timeline_offset_ms")
            if (type(offset) is int and all(type(p) is int for p in candidate_window)
                    and [p + offset for p in candidate_window] == _physical_window(row)):
                window = candidate_window
        if (
            row["status"] != "UNRESOLVED"
            or closure.get("codepoint_span") != row["codepoint_span"]
            or not _valid_closing_adjudication(
                adjudication,
                before=row["current_cue"],
                after=row["current_cue"],
                window=window,
                keep=True,
                finding=closure.get("finding"),
            )
        ):
            raise ValueError("SPAN_KEEP_CLOSURE_INVALID")
        row["status"] = "RESOLVED"
        row["resolution"] = {
            "kind": ("ORDINARY_RELEASE_RESOLVED_KEEP"
                     if adjudication.get("sub18_context_meaning_equivalent_keep")
                     else "ORDINARY_AUDIO_KEEP_CURRENT"),
            "closure_sha256": digest(closure),
        }
    return list(pending.values())


def _finding(row: Mapping) -> dict:
    left, right = row["codepoint_span"]
    origin = {
        "obligation_id": row["obligation_id"],
        "codepoint_span": [left, right],
        "base_text_sha256": row["base_text_sha256"],
        "cue_window_ms": row["cue_window_ms"],
        "obligation_ids": row.get("obligation_ids", [row["obligation_id"]]),
        "source": row["origin"],
    }
    if row.get("reanchor_chain"):
        origin["reanchor_chain"] = deepcopy(row["reanchor_chain"])
    if "source_offset_ms" in row:
        origin["source_offset_ms"] = row["source_offset_ms"]
        origin["physical_cue_window_ms"] = _physical_window(row)
    finding = {
        "cue": row["cue_index"],
        "cue_index": row["cue_index"],
        "base_text_sha256": row["base_text_sha256"],
        "kind": "context",
        "repair_class": "phonetic",
        "suspect": row["current_text"],
        "span_start_codepoint": left,
        "span_end_codepoint": right,
        "suggestion": None,
        "proposed_full_cue": None,
        "why": "Separate source-bound audible question outside the selected edit span: "
        + " / ".join(q["reason"] for q in row["origin"]["questions"]),
        # Risk is classified by the ordinary reviewer, not invented from an
        # audio disagreement. The unresolved obligation itself blocks release.
        "force_acoustic": True,
        "timing_immutable": True,
        ORIGIN_FIELD: origin,
        "span_obligation_status": row["status"],
        "mutation_authorized": False,
    }
    from .finding_lifecycle import annotate
    return annotate(finding)


def _reviewed_adjudication(row):
    return (row.get("exact_release_adjudication") or row.get("context_audio_adjudication")
            or (row.get("span_obligation_recovery") or {}).get("latest_adjudication"))


def _recovery_view(row):
    saved = row.get("span_obligation_recovery")
    if not isinstance(saved, Mapping):
        return False
    actual = row.get("exact_release_adjudication") or row.get("context_audio_adjudication")
    return not actual or saved.get("latest_adjudication_sha256") == digest(actual)


def _reviewed_rows(authority):
    for review in (authority.get("final_review_audit") or {}, authority):
        for regenerated in (True, False):
            for row in [*(review.get("resolved_findings") or []),
                        *(review.get("unresolved_findings_disclosed") or []),
                        *(review.get("findings") or [])]:
                if (isinstance(row, Mapping) and _reviewed_adjudication(row)
                        and _recovery_view(row) == regenerated):
                    yield row


def _retain_review(pending, reviewed, *, reviewed_srt_sha256):
    """Keep the latest real answer, without moving its authority to new text."""
    from .finding_lifecycle import annotate, classify

    origin = pending[ORIGIN_FIELD]
    ids = set(origin["obligation_ids"])
    generated = pending
    for prior in reviewed:
        old = prior.get(ORIGIN_FIELD) or {}
        if not ids.intersection({old.get("obligation_id"), *(old.get("obligation_ids") or [])}):
            continue
        adj = _reviewed_adjudication(prior)
        if not (prior.get("exact_release_adjudication") or prior.get("context_audio_adjudication")):
            # A reanchored row intentionally has no current adjudication. Its
            # retained answer is still history on the next cold resume.
            pending = dict(generated)
            pending.pop("lifecycle", None)
            pending["span_obligation_recovery"] = deepcopy(prior["span_obligation_recovery"])
            pending = annotate(pending)
            continue
        request = adj.get("request") if isinstance(adj, Mapping) else None
        if not isinstance(request, Mapping):
            bound = False
        else:
            bound = (request.get("base_text_sha256") == pending["base_text_sha256"]
                     and [request.get("matched_start_ms"), request.get("matched_end_ms")]
                     == origin["cue_window_ms"]
                     and request.get("source_media_timeline_offset_ms", 0)
                     == origin.get("source_offset_ms", 0)
                     and request.get("request_sha256") == digest(
                         {k: v for k, v in request.items() if k != "request_sha256"})
                     and pending["span_obligation_status"] != "LOCATION_STALE")
        risk = prior.get("current_content_risk")
        srt_drift = (isinstance(risk, Mapping)
                     and str(risk.get("reviewed_srt_sha256")).removeprefix("sha256:")
                     != reviewed_srt_sha256)
        if srt_drift:
            bound = False
        history = {
            "latest_finding_sha256": ((prior.get("span_obligation_recovery") or {})
                                       .get("latest_finding_sha256") if _recovery_view(prior)
                                       else digest(prior)),
            "latest_adjudication_sha256": digest(adj),
            "latest_adjudication": deepcopy(adj),
            "reason_code": ("SPAN_OBLIGATION_CURRENT_SRT_DRIFT" if srt_drift else
                            "SPAN_RELEASE_RESOLVED_KEEP_REJECTED" if bound else
                            "SPAN_OBLIGATION_ADJUDICATION_BINDING_MISMATCH"),
            "owner": "exact_final_terminal_funnel.terminal_check",
        }
        previous_view = classify({k: v for k, v in prior.items()
                                  if k not in {"lifecycle", "span_obligation_recovery"}})
        if bound:
            detail = previous_view.get("detail") or {}
            history["reason_code"] = (detail.get("validation_error")
                                      or detail.get("reason_code")
                                      or "SPAN_RELEASE_RESOLVED_KEEP_REJECTED")
            if previous_view["state"] == "EDIT_REVIEW":
                history.update(reason_code="SPAN_TYPED_REPAIR_NOT_APPLIED",
                               owner="producer_package_finalization._apply_exact_final_cpa_repairs")
        if bound:
            # Retain risk/meaning/candidate evidence with the actual answer.
            # The regenerated location and unresolved status remain decisive.
            pending = {**prior, **generated, "exact_release_adjudication": deepcopy(adj)}
        else:
            pending = dict(generated)
        pending.pop("lifecycle", None)
        pending["span_obligation_recovery"] = history
        pending = annotate(pending)
    return pending


def _resolved_generated_finding(finding, resolved):
    """Retract only an old derived agenda row whose bound questions closed."""
    if (finding.get("span_obligation_status") != "UNRESOLVED"
            or finding.get("mutation_authorized") is not False
            or finding.get("content_risk") is not None
            or finding.get("proposed_full_cue")
            or finding.get("suggestion")
            or finding.get("exact_release_adjudication")
            or finding.get("context_audio_adjudication")):
        return False
    origin = finding.get(ORIGIN_FIELD) or {}
    ids = set(origin.get("obligation_ids") or [origin.get("obligation_id")])
    window = origin.get("cue_window_ms")
    span = origin.get("codepoint_span")
    if (not ids or not isinstance(window, list) or len(window) != 2
            or not isinstance(span, list) or len(span) != 2
            or [finding.get("span_start_codepoint"), finding.get("span_end_codepoint")] != span):
        return False
    return all(oid in resolved
        and finding.get("base_text_sha256") == origin.get("base_text_sha256")
            == resolved[oid]["base_text_sha256"]
        # Older generated rows omitted the source offset. Their unchanged
        # delivery window inherits its clock from this validated birth ID;
        # an explicitly supplied offset must still match the physical window.
        and (window == resolved[oid]["cue_window_ms"] if "source_offset_ms" not in origin
             else [p + origin["source_offset_ms"] for p in window] == _physical_window(resolved[oid]))
        and span[0] <= resolved[oid]["codepoint_span"][0]
            <= resolved[oid]["codepoint_span"][1] <= span[1]
        for oid in ids)


@cold_input
def pending_findings(srt_text: str, authority: Mapping, *, timeline_offset_ms=0) -> list[dict]:
    from src.autoslice.jingting_chunker import parse_srt_cues

    repairs = repair_chain(authority)
    unapplied = authority.get(UNAPPLIED_FIELD, [])
    if not any(FIELD in r for r in repairs) and not unapplied:
        return []
    closures = authority.get("span_obligation_closures", [])
    rows = derive_obligations(repairs, closures, unapplied,
                              reviewed_srt_sha256=text_sha(srt_text))
    grouped = []
    for row in sorted(
        (r for r in rows if r["status"] != "RESOLVED"),
        key=lambda r: (r["cue_index"], r["codepoint_span"]),
    ):
        if (
            grouped
            and row["cue_index"] == grouped[-1]["cue_index"]
            and row["current_cue"] == grouped[-1]["current_cue"]
            and row["codepoint_span"][0] < grouped[-1]["codepoint_span"][1]
        ):
            previous = grouped[-1]
            previous["codepoint_span"][1] = max(
                previous["codepoint_span"][1], row["codepoint_span"][1]
            )
            a, b = previous["codepoint_span"]
            previous["current_text"] = previous["current_cue"][a:b]
            previous["obligation_ids"].append(row["obligation_id"])
            previous["origin"]["questions"].extend(row["origin"]["questions"])
            if row["status"] != "UNRESOLVED":
                previous["status"] = row["status"]
        else:
            row = deepcopy(row)
            row["obligation_ids"] = [row["obligation_id"]]
            grouped.append(row)
    rows = grouped
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    result = []
    for row in rows:
        if row["status"] == "RESOLVED":
            continue
        matches = [(i, cue) for i, cue in enumerate(cues, 1)
            if cue.text == row["current_cue"]
            and [cue.start_ms + timeline_offset_ms, cue.end_ms + timeline_offset_ms]
            == _physical_window(row)]
        if len(matches) != 1:
            row["status"] = "LOCATION_STALE"
        else:
            index, cue = matches[0]
            row["cue_index"] = index
            row["cue_window_ms"] = [cue.start_ms, cue.end_ms]
            if timeline_offset_ms or "source_offset_ms" in row:
                row["source_offset_ms"] = timeline_offset_ms
        result.append(_retain_review(_finding(row), _reviewed_rows(authority),
                                     reviewed_srt_sha256=text_sha(srt_text)))
    return result


def merge_pending(srt_text: str, authority: Mapping, findings: list, *, timeline_offset_ms=0) -> list:
    result = list(findings)
    for pending in pending_findings(srt_text, authority, timeline_offset_ms=timeline_offset_ms):
        origin = pending[ORIGIN_FIELD]
        matched = None
        pending_ids = set(origin["obligation_ids"])
        for number, row in enumerate(result):
            if not isinstance(row, Mapping):
                continue
            previous = row.get(ORIGIN_FIELD) or {}
            previous_ids = {previous.get("obligation_id"), *(previous.get("obligation_ids") or [])}
            if (pending["span_obligation_status"] != "LOCATION_STALE"
                    and pending_ids.intersection(previous_ids)
                    and (row.get("cue_index", row.get("cue")) != pending["cue_index"]
                         or row.get("base_text_sha256") != pending["base_text_sha256"]
                         or previous.get("cue_window_ms") != origin["cue_window_ms"])):
                # Same obligation, new bound representation. Keep the old
                # finding identifiable in the immutable authority; never
                # transplant its old decision onto the current text.
                matched = {**pending, "prior_span_location": {
                    "finding_sha256": digest(row),
                    "cue_index": row.get("cue_index", row.get("cue")),
                    "base_text_sha256": row.get("base_text_sha256"),
                    "cue_window_ms": previous.get("cue_window_ms"),
                }}
                result[number] = matched
                break
            if (
                row.get("cue_index", row.get("cue")) != pending["cue_index"]
            ):
                continue
            if row.get("base_text_sha256") not in {None, pending["base_text_sha256"]}:
                continue
            exact = (
                row.get("suspect") == pending["suspect"]
                and row.get("span_start_codepoint", pending["span_start_codepoint"])
                == pending["span_start_codepoint"]
                and row.get("span_end_codepoint", pending["span_end_codepoint"])
                == pending["span_end_codepoint"]
            )
            left, right = row.get("span_start_codepoint"), row.get("span_end_codepoint")
            scoped = (
                type(left) is int
                and type(right) is int
                and pending["span_start_codepoint"]
                <= left
                <= right
                <= pending["span_end_codepoint"]
                and isinstance(row.get("proposed_full_cue"), str)
            )
            if exact or scoped:
                matched = row
                break
        if matched is not None and isinstance(matched, dict):
            if pending.get("span_obligation_recovery") and _recovery_view(matched):
                # This is a previously regenerated agenda row, not a newer
                # ruling. Replace its old recovery view with the latest one.
                matched.update(pending)
            elif pending.get("span_obligation_recovery") and not (
                matched.get("exact_release_adjudication") or matched.get("context_audio_adjudication")
            ):
                matched.update({**pending, **matched,
                                "span_obligation_recovery": pending["span_obligation_recovery"],
                                "lifecycle": pending["lifecycle"]})
            elif (pending.get("span_obligation_recovery")
                    and pending["span_obligation_recovery"]["latest_adjudication_sha256"]
                    == digest(_reviewed_adjudication(matched))):
                # A fresh real answer may inherit the previous recovery tag.
                # Update its derived view without replacing that new answer.
                from .finding_lifecycle import annotate
                matched["span_obligation_recovery"] = deepcopy(pending["span_obligation_recovery"])
                matched.update(annotate(matched))
            matched[ORIGIN_FIELD] = deepcopy(origin)
            matched.setdefault("span_start_codepoint", pending["span_start_codepoint"])
            matched.setdefault("span_end_codepoint", pending["span_end_codepoint"])
            if pending["why"] not in str(matched.get("why") or ""):
                matched["why"] = (
                    str(matched.get("why") or "")
                    + "\nOutstanding source-bound question: "
                    + pending["why"]
                )
        else:
            result.append(pending)
    return result


@cold_input
def enforce_review(srt_text: str, authority: Mapping, audit: dict, *, timeline_offset_ms=0) -> dict:
    """Never let a new empty model response erase already born obligations."""
    repairs = repair_chain(authority)
    from src.autoslice.span_phoneme_resolution import validate_question_packet

    unapplied = deepcopy(authority.get(UNAPPLIED_FIELD, []))
    seen = {r["packet"]["receipt_sha256"] for r in unapplied}
    for finding in audit.get("findings") or []:
        adj = (
            finding.get("exact_release_adjudication")
            or finding.get("context_audio_adjudication")
            or {}
        )
        packet = adj.get("span_pending_questions")
        if isinstance(packet, Mapping) and packet.get("receipt_sha256") not in seen:
            validate_question_packet(packet)
            unapplied.append(
                {
                    "after_repair_count": len(repairs),
                    "repair_prefix_sha256": digest(repairs),
                    "packet": deepcopy(packet),
                }
            )
            seen.add(packet["receipt_sha256"])
    if not any(FIELD in r for r in repairs) and not unapplied:
        return audit
    # Append to a private list; existing closure evidence is immutable and is
    # content-addressed by the common document publisher.
    closures = list(authority.get("span_obligation_closures", []))
    unclosed = {
        r["obligation_id"]: r
        for r in derive_obligations(repairs, closures, unapplied,
                                    reviewed_srt_sha256=text_sha(srt_text))
        if r["status"] != "RESOLVED"
    }
    for finding in [
        *(audit.get("resolved_findings") or []),
        *(audit.get("unresolved_findings_disclosed") or []),
    ]:
        origin = finding.get(ORIGIN_FIELD) or {}
        for oid in dict.fromkeys(
            [origin.get("obligation_id"), *(origin.get("obligation_ids") or [])]
        ):
            if oid not in unclosed:
                continue
            row = unclosed[oid]
            adj = finding.get("exact_release_adjudication") or finding.get(
                "context_audio_adjudication"
            )
            if _valid_closing_adjudication(
                adj,
                before=row["current_cue"],
                after=row["current_cue"],
                window=[p - timeline_offset_ms for p in _physical_window(row)],
                keep=True,
                finding=finding,
                reviewed_srt_sha256=text_sha(srt_text),
            ):
                closures.append(
                    {
                        "obligation_id": oid,
                        "codepoint_span": row["codepoint_span"],
                        "adjudication": adj,
                        "finding": deepcopy(finding),
                    }
                )
                unclosed.pop(oid)
    effective = {
        **dict(authority),
        "span_obligation_closures": closures,
        UNAPPLIED_FIELD: unapplied,
        "final_review_audit": {"findings": [*_reviewed_rows(authority), *_reviewed_rows(audit)]},
    }
    audit["findings"] = merge_pending(srt_text, effective, list(audit.get("findings") or []),
                                     timeline_offset_ms=timeline_offset_ms)
    states = derive_obligations(repairs, closures, unapplied,
                                reviewed_srt_sha256=text_sha(srt_text))
    resolved = {row["obligation_id"]: row for row in states if row["status"] == "RESOLVED"}
    prior_count = len(audit["findings"])
    audit["findings"] = [row for row in audit["findings"]
                         if not _resolved_generated_finding(row, resolved)]
    retracted = prior_count != len(audit["findings"])
    proof = {
        "schema_version": "subtitle-span-obligation-review.v1",
        "srt_sha256": text_sha(srt_text),
        "repairs": repairs,
        "closures": closures,
        "states": states,
        UNAPPLIED_FIELD: unapplied,
    }
    proof["receipt_sha256"] = digest(proof)
    audit[REVIEW_FIELD] = proof
    audit[UNAPPLIED_FIELD] = deepcopy(unapplied)
    audit["validated_finding_count"] = len(audit["findings"])
    if any(r["status"] != "RESOLVED" for r in states):
        from .finding_lifecycle import REVIEW_PENDING
        audit.update(status="FLAGGED", release_gate="BLOCK")
        audit["reason_codes"] = list(
            dict.fromkeys([*(audit.get("reason_codes") or []), REVIEW_PENDING])
        )
    if retracted:
        from .finding_lifecycle import review_outcome
        audit.update(review_outcome(audit, terminal=bool(audit.get("terminal_funnel"))))
    return audit


@persisted_upgrade("span-obligation-states", "subtitle-span-obligation-review.v1")
def _upgrade_state_clock(stored, proof):
    """Read pre-clock states using their own sealed birth packet's clock.

    The proof's original bytes and receipt remain authoritative. Only an
    absent offset has a legacy representation; explicit offsets and every
    other state field must still equal the ordinary replay result.
    """
    from src.autoslice.span_phoneme_resolution import validate_question_packet

    clocks = {}
    for repair in proof["repairs"]:
        value = validate_span_repair(repair)
        if value is not None:
            clocks[value["receipt_sha256"]] = value["binding"]["source_offset_ms"]
    for record in proof.get(UNAPPLIED_FIELD, []):
        value = validate_question_packet(record.get("packet"))
        clocks[value["receipt_sha256"]] = value["binding"]["source_offset_ms"]
    if not isinstance(stored, list):
        return stored
    normalized = []
    for row in stored:
        if isinstance(row, Mapping) and "source_offset_ms" not in row:
            origin = row.get("origin")
            parent = origin.get("parent_scope_sha256") if isinstance(origin, Mapping) else None
            offset = clocks.get(parent) if isinstance(parent, str) else None
            if offset:
                row = {**row, "source_offset_ms": offset}
        normalized.append(row)
    return normalized


@persisted_upgrade("span-obligation-states", "subtitle-span-obligation-review.v1")
def _upgrade_pre_reanchor_states(stored, proof):
    """Recognize the entire old projection before replaying current meaning.

    No field is ignored: an explicit mismatch, extra field, missing birth
    material or tampered legacy status cannot qualify. Original questions and
    seals survive, and current release/pending checks still run afterwards.
    """
    if not isinstance(stored, list):
        return stored
    try:
        old = _derive_obligations(proof["repairs"], proof["closures"],
                                  proof.get(UNAPPLIED_FIELD, []), legacy=True)
    except ValueError:
        return stored
    if stored != _upgrade_state_clock(old, proof):
        return stored
    current = _derive_obligations(proof["repairs"], proof["closures"],
                                  proof.get(UNAPPLIED_FIELD, []),
                                  reviewed_srt_sha256=proof.get("srt_sha256"))
    fields = _PRE_CLOCK_STATE_FIELDS | {"source_offset_ms", "reanchor_chain"}
    return [{key: value for key, value in row.items() if key in fields} for row in current]


@cold_input
def validate_release_obligations(audit: Mapping) -> None:
    from .final_review_contract import SUB18_KEEP_BRANCH
    repairs = repair_chain(audit)
    proof = audit.get(REVIEW_FIELD)
    if proof is None:
        if any(FIELD in r for r in repairs) or audit.get(UNAPPLIED_FIELD):
            raise ValueError("SPAN_OBLIGATION_REVIEW_MISSING")
        return
    if (
        not isinstance(proof, Mapping)
        or proof.get("schema_version") != "subtitle-span-obligation-review.v1"
        or proof.get("srt_sha256")
        != str(audit.get("reviewed_srt_sha256") or "").removeprefix("sha256:")
        or digest({k: v for k, v in proof.items() if k != "receipt_sha256"})
        != proof.get("receipt_sha256")
        or (repairs and repairs != proof.get("repairs"))
    ):
        raise ValueError("SPAN_OBLIGATION_REVIEW_DRIFT")
    states = derive_obligations(proof["repairs"], proof["closures"],
                                proof.get(UNAPPLIED_FIELD, []),
                                reviewed_srt_sha256=proof["srt_sha256"])
    stored = proof.get("states")
    stale_closure_hashes = set()
    for closure in proof["closures"]:
        finding = closure.get("finding") or {}
        risk = finding.get("current_content_risk") if isinstance(finding, Mapping) else None
        if ((closure.get("adjudication") or {}).get("policy_branch") == SUB18_KEEP_BRANCH
                and isinstance(risk, Mapping)
                and str(risk.get("reviewed_srt_sha256")).removeprefix("sha256:")
                != proof["srt_sha256"]):
            stale_closure_hashes.add(digest(closure))
    if isinstance(stored, list) and any(
        isinstance(row, Mapping) and row.get("status") == "RESOLVED"
        and isinstance(row.get("resolution"), Mapping)
        and row["resolution"].get("closure_sha256") in stale_closure_hashes
        for row in stored
    ):
        raise ValueError("SPAN_KEEP_CLOSURE_CURRENT_SRT_DRIFT")
    if states != stored and states != persisted_view("span-obligation-states", proof, stored):
        raise ValueError("SPAN_OBLIGATION_STATES_DRIFT")
    if any(r["status"] != "RESOLVED" for r in states):
        from .finding_lifecycle import REVIEW_PENDING
        raise ValueError(REVIEW_PENDING)
