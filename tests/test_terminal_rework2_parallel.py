"""Terminal-funnel parallel execution keeps the serial decision contract."""

from copy import deepcopy
import json
import os
import threading
import time

import pytest


SRT = "\n\n".join(
    f"{index}\n00:00:{2 * index:02d},000 --> 00:00:{2 * index + 1:02d},000\n第{index}句"
    for index in range(1, 21)
) + "\n"

# The scheduler seams replace content/material work, but admission still needs
# the same physical-source context as the real request/group constructor.
CLIP_CONTEXT = {"candidate_id": "offline-parallel", "pieces": [{
    "start_ms": 0, "end_ms": 41_000, "source_media_sha256": "a" * 64}]}


def _agenda(count=20, *, choices=None):
    choices = list(choices or ["CURRENT"] * count)
    return [
        {
            "cue_index": index,
            "suspect": f"第{index}句",
            "suggestion": f"第{index}字",
            "repair_class": "phonetic",
            "why": f"question-{index}",
            "_test_choice": choices[index - 1],
        }
        for index in range(1, count + 1)
    ]


def _install_terminal_seams(monkeypatch, *, delays=None, recorder_audit=None):
    from src.autoslice import exact_final_terminal_funnel as funnel
    from src.autoslice import final_review_contract as contract
    from src.autoslice import review_evidence_store
    from src.autoslice import span_judgment_history
    from src.autoslice import targeted_relisten
    from src.autoslice import semantic_retry_progress

    delays = dict(delays or {})
    seen_context = []

    def check(srt_text, finding, **kwargs):
        seen_context.append((finding["cue_index"], span_judgment_history._active.get(),
                             review_evidence_store._cold_active.get(),
                             tuple(row.get("_history_marker") for row in
                                   kwargs.get("historical_findings", ()))))
        return {"cue_index": finding["cue_index"], "current_cue": finding["suspect"]}

    def proof(**kwargs):
        cue = kwargs["finding"]["cue_index"]
        return {"proof_sha256": f"proof-{cue}", "stop_reason_code": "NO_GAIN"}

    active = 0
    peak = 0
    total_calls = 0
    guard = threading.Lock()

    def close(*, finding, **kwargs):
        nonlocal active, peak, total_calls
        cue = finding["cue_index"]
        with guard:
            active += 1
            total_calls += 1
            peak = max(peak, active)
        try:
            time.sleep(delays.get(cue, 0.0))
            history = span_judgment_history._active.get()
            if isinstance(history, dict):
                execution_id = f"synthetic-execution-{cue}"
                history.setdefault("expected", {})[execution_id] = {"cue_index": cue}
                history.setdefault("outcomes", {})[execution_id] = {"choice": finding["_test_choice"]}
            audit = {
                "schema_version": "subtitle-span-adjudication.v1",
                "status": "OBSERVED",
                "repaired": False,
                "choice": finding["_test_choice"],
                "policy_branch": "TEST_TERMINAL_CHOICE",
                "semantic_retry_progress": recorder_audit or {
                    "schema_version": "semantic-retry-progress.v1",
                    "events": [], "status": "RECORDED", "mutation_authorized": False,
                },
            }
            return audit
        finally:
            with guard:
                active -= 1

    original_predicate = contract.is_release_resolved_keep
    monkeypatch.setattr(funnel, "terminal_check", check)
    monkeypatch.setattr(targeted_relisten, "compile_loop_stop_no_gain", proof)
    monkeypatch.setattr(targeted_relisten, "close_with_existing_editorial", close)
    # The scheduler fixture supplies a minimal synthetic check.  Production D
    # owns the real target-material admission; this seam keeps that contract
    # explicit while testing only G6 execution and ordered publication.
    monkeypatch.setattr(targeted_relisten, "complete_target_materials",
                        lambda *, check, finding, adapters: check)
    monkeypatch.setattr(contract, "is_release_resolved_keep",
                        lambda row: (row.get("exact_release_adjudication") or {}).get("choice")
                        != "UNRESOLVABLE")

    class FixedRecorder:
        def __init__(self, **kwargs):
            self.events = [{"status": "COMPLETED", "provider_calls": 1}]
            self.blocked = None

        def wrap(self, call):
            return call

        def audit(self):
            return deepcopy(recorder_audit or {
                "schema_version": "semantic-retry-progress.v1",
                "events": self.events, "status": "RECORDED",
                "mutation_authorized": False,
            })

    # The production function creates one recorder per independent terminal
    # owner.  This deterministic recorder exercises the same audit attachment
    # branch without making a provider call or writing a test cache.
    monkeypatch.setattr(semantic_retry_progress, "SemanticAttemptRecorder", FixedRecorder)
    return (funnel.terminalize_review, seen_context, lambda: peak, original_predicate,
            lambda: total_calls)


def _run(monkeypatch, *, workers, choices, delays=None, recorder_audit=None,
         active_state=None, agenda=None):
    terminalize, seen_context, peak, _, usage = _install_terminal_seams(
        monkeypatch, delays=delays, recorder_audit=recorder_audit)
    monkeypatch.setenv("AUTOSLICE_EXACT_REVIEW_PARALLELISM", str(workers))
    token = None
    from src.autoslice import span_judgment_history
    if active_state is not None:
        token = span_judgment_history._active.set(active_state)
    try:
        findings = _agenda(20, choices=choices) if agenda is None else deepcopy(agenda)
        result = terminalize(SRT, {"findings": findings,
                                  "unresolved_findings_disclosed": [],
                                  "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]},
                             pass_receipts=[{"pass_index": 1}], adapters={
                                 "clip_context": CLIP_CONTEXT,
                                 "judge_llm_call": lambda prompt: "{}",
                                 "terminal_usage": usage,
                                 "historical_findings": [{"_history_marker": "closed-history"}],
                             })
    finally:
        if token is not None:
            span_judgment_history._active.reset(token)
    return result, seen_context, peak()


def test_terminal_g6_group_has_one_owner_call_and_ordered_member_projection(monkeypatch):
    agenda = _agenda(3, choices=["CURRENT", "CURRENT", "PROPOSED"])
    # The first two findings deliberately bind to one physical/audio question;
    # their different suggestions remain a G6-shared terminal question.
    agenda[1].update(cue_index=agenda[0]["cue_index"], suspect=agenda[0]["suspect"],
                     suggestion="另一个候选", why="different editorial wording")
    terminalize, _, _, _, usage = _install_terminal_seams(monkeypatch)
    monkeypatch.setenv("AUTOSLICE_EXACT_REVIEW_PARALLELISM", "8")
    result = terminalize(SRT, {"findings": agenda,
                               "unresolved_findings_disclosed": [],
                               "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]},
                         pass_receipts=[{"pass_index": 1}], adapters={
                             "clip_context": CLIP_CONTEXT,
                             "judge_llm_call": lambda prompt: "{}",
                             "terminal_usage": usage,
                         })
    rows = result["unresolved_findings_disclosed"]
    assert [row["cue_index"] for row in rows] == [1, 1, 3]
    assert rows[1]["terminal_question_owner"] == 0
    assert rows[0]["exact_release_adjudication"]["terminal_question_members"] == rows[1]["exact_release_adjudication"]["terminal_question_members"]
    assert rows[2]["exact_release_adjudication"]["terminal_question_members"] != rows[0]["exact_release_adjudication"]["terminal_question_members"]
    # The shared question is adjudicated once; the second member is only a
    # deterministic projection of the owner result.
    assert [attempt.get("shared_question_owner") for attempt in result["terminal_funnel"]["attempts"]] == [None, 0, None]
    assert result["cpa_adjudication_usage"]["provider_adjudication_count"] == 2


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED", "CONTEXT_FIT", "UNRESOLVABLE"])
def test_terminal_serial_parallel_each_choice_and_audits_are_equal(monkeypatch, choice):
    recorder_audit = {
        "schema_version": "semantic-retry-progress.v1",
        "events": [{"status": "COMPLETED", "request_sha256": "request-fixed",
                    "provider_calls": 1}],
        "status": "RECORDED", "mutation_authorized": False,
    }
    active_state = {"expected": {}, "outcomes": {}}
    serial, serial_context, serial_peak = _run(
        monkeypatch, workers=1, choices=[choice] * 20,
        recorder_audit=recorder_audit, active_state=active_state,
        delays={index: 0.002 for index in range(1, 21)})
    parallel, parallel_context, parallel_peak = _run(
        monkeypatch, workers=8, choices=[choice] * 20,
        recorder_audit=recorder_audit, active_state=active_state,
        delays={index: 0.002 for index in range(1, 21)})
    assert json.dumps(parallel, ensure_ascii=False, sort_keys=True) == json.dumps(
        serial, ensure_ascii=False, sort_keys=True)
    assert sorted(row[0] for row in parallel_context) == list(range(1, 21))
    assert all(cold is True for _, _, cold, _ in parallel_context)
    assert all(history == active_state for _, history, _, _ in parallel_context)
    assert all(history is not active_state for _, history, _, _ in parallel_context)
    assert all(history[0] == "closed-history" and all(marker is None for marker in history[1:])
               for *_, history in parallel_context)
    assert serial_peak == 1
    assert parallel_peak > 1
    assert all(
        row["exact_release_adjudication"]["semantic_retry_progress"] == recorder_audit
        for row in parallel["unresolved_findings_disclosed"] + parallel["findings"]
    )


def test_terminal_span_history_is_absorbed_in_agenda_order(monkeypatch):
    serial_state = {"expected": {}, "outcomes": {}}
    parallel_state = {"expected": {}, "outcomes": {}}
    _run(monkeypatch, workers=1, choices=["CURRENT"] * 20,
         active_state=serial_state,
         delays={index: (21 - index) * 0.001 for index in range(1, 21)})
    _run(monkeypatch, workers=8, choices=["CURRENT"] * 20,
         active_state=parallel_state,
         delays={index: (21 - index) * 0.001 for index in range(1, 21)})
    expected_ids = [f"synthetic-execution-{index}" for index in range(1, 21)]
    assert list(serial_state["expected"]) == expected_ids
    assert list(parallel_state["expected"]) == expected_ids
    assert parallel_state == serial_state


def _run_with_context_budget(monkeypatch, *, workers, fail_prepare_at=None):
    from src.autoslice import exact_final_terminal_funnel as funnel
    from src.autoslice import final_review_contract as contract
    from src.autoslice import targeted_relisten
    from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    monkeypatch.setenv("AUTOSLICE_EXACT_REVIEW_PARALLELISM", str(workers))
    delays = {index: (21 - index) * 0.001 for index in range(1, 21)}

    def check(srt_text, finding, **kwargs):
        return {"cue_index": finding["cue_index"], "current_cue": finding["suspect"]}

    def proof(**kwargs):
        return {"proof_sha256": f"budget-proof-{kwargs['finding']['cue_index']}",
                "stop_reason_code": "NO_GAIN"}

    monkeypatch.setattr(funnel, "terminal_check", check)
    monkeypatch.setattr(targeted_relisten, "compile_loop_stop_no_gain", proof)
    monkeypatch.setattr(targeted_relisten, "complete_target_materials",
                        lambda *, check, finding, adapters: check)
    monkeypatch.setattr(contract, "is_release_resolved_keep",
                        lambda row: (row.get("exact_release_adjudication") or {}).get("choice")
                        != "UNRESOLVABLE")

    def adjudicate_context(srt_text, finding, **kwargs):
        time.sleep(delays[finding["cue_index"]])
        return srt_text, {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "OBSERVED", "repaired": False,
            "choice": finding["_test_choice"],
            "policy_branch": "TEST_CONTEXT_BUDGET",
        }

    budget = ContextAdjudicationBudget(
        limit=100, entity_verifier=None, clip_context=CLIP_CONTEXT,
        source_media_timeline_offset_ms=0, judge_llm_call=lambda prompt: "{}",
        screen_read_probe=None, adjudicate_context=adjudicate_context,
        provider_calls_allowed=True,
    )

    def terminal_prepare(srt_text, row, recovery, prior):
        if row["cue_index"] == fail_prepare_at:
            from src.autoslice.review_errors import ContractRejection
            raise ContractRejection("PREPARE_FAIL")
        return budget.prepare(
            srt_text, {**row, "_audio_gain_termination": recovery["no_gain_proof"]},
            adjudicate_context=adjudicate_context,
        )

    def terminal_owner(srt_text, row, recovery, prior):
        return terminal_owner.prepare(srt_text, row, recovery, prior)()

    terminal_owner.prepare = terminal_prepare
    result = funnel.terminalize_review(
        SRT,
        {"findings": _agenda(20), "unresolved_findings_disclosed": [],
         "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"]},
        pass_receipts=[{"pass_index": 1}],
        adapters={"clip_context": CLIP_CONTEXT, "judge_llm_call": lambda prompt: "{}",
                  "terminal_owner": terminal_owner,
                  "terminal_usage": lambda: budget.provider_adjudication_count},
    )
    rows = result["unresolved_findings_disclosed"]
    pressure = [row["exact_release_adjudication"]["cpa_resource_pressure"]
                ["provider_adjudication_count"] for row in rows]
    return result, pressure


def test_terminal_context_budget_admission_is_serial_equivalent(monkeypatch):
    serial, serial_pressure = _run_with_context_budget(monkeypatch, workers=1)
    parallel, parallel_pressure = _run_with_context_budget(monkeypatch, workers=8)
    assert serial_pressure == list(range(1, 21))
    assert parallel_pressure == serial_pressure
    assert json.dumps(parallel, ensure_ascii=False, sort_keys=True) == json.dumps(
        serial, ensure_ascii=False, sort_keys=True)


def test_terminal_prepare_failure_closes_native_admission_for_later_groups(monkeypatch):
    result, pressure = _run_with_context_budget(
        monkeypatch, workers=8, fail_prepare_at=1)
    assert result["findings"][0]["terminal_funnel_error"]["reason_code"] == "PREPARE_FAIL"
    assert [row["cue_index"] for row in result["unresolved_findings_disclosed"]] == list(range(2, 21))
    assert pressure == list(range(1, 20))


def test_terminal_production_shaped_twenty_groups_speed_up_and_keep_order(monkeypatch):
    # The default keeps CI quick while preserving the production 5..60 second
    # shape.  A real acceptance run sets the scale to 1 and records the same
    # terminal-funnel path with 20 independent G6 groups.
    scale = float(os.environ.get("AUTOSLICE_TERMINAL_PARALLEL_BENCHMARK_SCALE", "0.001"))
    assert scale > 0
    durations = {index: (5 + (index * 11) % 56) * scale for index in range(1, 21)}
    started = time.monotonic()
    serial, _, serial_peak = _run(monkeypatch, workers=1,
                                  choices=["CURRENT"] * 20, delays=durations)
    serial_elapsed = time.monotonic() - started
    started = time.monotonic()
    parallel, _, parallel_peak = _run(monkeypatch, workers=8,
                                      choices=["CURRENT"] * 20, delays=durations)
    parallel_elapsed = time.monotonic() - started
    serial_rows = serial["unresolved_findings_disclosed"]
    parallel_rows = parallel["unresolved_findings_disclosed"]
    assert [row["cue_index"] for row in parallel_rows] == list(range(1, 21))
    assert json.dumps(parallel, ensure_ascii=False, sort_keys=True) == json.dumps(
        serial, ensure_ascii=False, sort_keys=True)
    assert serial_peak == 1 and parallel_peak > 1
    assert serial_elapsed > parallel_elapsed * 2
    print("TERMINAL_PARALLEL_BENCHMARK=" + json.dumps({
        "groups": 20, "durations_seconds": sorted(durations.values()),
        "scale": scale, "serial_s": serial_elapsed,
        "parallel_8_s": parallel_elapsed,
        "speedup": serial_elapsed / parallel_elapsed,
        "serial_peak": serial_peak, "parallel_peak": parallel_peak,
    }, ensure_ascii=False, sort_keys=True))
    assert len(serial_rows) == len(parallel_rows) == 20
