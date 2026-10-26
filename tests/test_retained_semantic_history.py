"""Target-local original requests and stopped retained input; no provider fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding, build_context_adjudication_request,
)
from src.autoslice.semantic_retry_progress import (
    RetryNeedsPlan, SemanticAttemptRecorder, with_novel_range_plan,
)
from src.autoslice.semantic_request_history import HISTORY_KEY, EXCHANGES_SCHEMA, digest


def _ref(path):
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


def _srt(offset=0, neighbor="邻句"):
    def stamp(ms):
        return f"00:00:{ms // 1000:02},{ms % 1000:03}"
    return (f"1\n{stamp(1000-offset)} --> {stamp(2000-offset)}\n甲方发言。\n\n"
            f"2\n{stamp(3000-offset)} --> {stamp(4000-offset)}\n{neighbor}\n")


def _context(source="a" * 64):
    return {"candidate_id": "candidate", "context_sha256": "b" * 64,
            "pieces": [{"source_media_sha256": "sha256:" + source, "start_ms": 0, "end_ms": 5000}]}


def _finding(index=1):
    return {"cue_index": index, "kind": "context", "repair_class": "phonetic",
            "suspect": "甲", "suggestion": "乙", "proposed_full_cue": "乙方发言。",
            "why": "Synthetic comparison question",
            "base_text_sha256": hashlib.sha256("甲方发言。".encode()).hexdigest()}


def _range(raw="c" * 64):
    return {"schema_version": "range-acoustic-context-observation.v1",
            "provider": "agy", "model": "range-fixture", "verdict": {},
            "artifact_bindings": {"source_media_sha256": "a" * 64,
                "audio_clip_sha256": "d" * 64, "raw_response_sha256": raw}}


def _observation():
    return {"provider": "agy", "model": "fixture", "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "e" * 64, "response_sha256": "f" * 64,
            "physical_audio_window_ms": [500, 4500], "kind": "blind_pinyin", "text": "jia"}


def _prompt(request, observations=(), ranges=(), wrapper="original"):
    block = {"binding": request["edit_span_binding"], "request_sha256": request["request_sha256"],
             "observations": list(observations), "invalid_observations": []}
    return ("# 字幕选字裁决\n" + wrapper + "\nSPAN_REVIEW_INPUT\n"
            + json.dumps(block, ensure_ascii=False, sort_keys=True)
            + "\nEND_SPAN_REVIEW_INPUT\n" + json.dumps(list(ranges), sort_keys=True))


def _exchange(root, prompt, *, name="request-original", status="COMPLETED"):
    folder = root / "cpa-text-exchanges" / name
    folder.mkdir(parents=True)
    (folder / "prompt.txt").write_text(prompt)
    (folder / "request.json").write_text(json.dumps({
        "schema_version": "private-cpa-text-exchange.v1", "status": "STARTED",
        "mutation_authorized": False, "production_attempt_id": "original-attempt",
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "model_identity": {"model": "fixture"}, "started_utc": "2026-10-19T00:00:00+00:00"}))
    if status != "STARTED":
        terminal = {"status": status}
        if status == "COMPLETED":
            raw = '{"choice":"UNRESOLVED","needs_audio":true}'
            (folder / "completion.txt").write_text(raw)
            terminal["completion_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
        (folder / "terminal.json").write_text(json.dumps(terminal))
    return {"request": _ref(folder / "request.json"), "prompt": _ref(folder / "prompt.txt"),
            "terminal": _ref(folder / "terminal.json") if (folder / "terminal.json").exists() else None,
            "completion": _ref(folder / "completion.txt") if (folder / "completion.txt").exists() else None}


def _archive(tmp_path, *, status="COMPLETED", ranges=(), observations=()):
    context = _context()
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context))
    request = build_context_adjudication_request(_srt(), _finding(), clip_context=context)
    prompt = _prompt(request, observations, ranges)
    item = _exchange(tmp_path, prompt, status=status)
    manifest = {"schema_version": EXCHANGES_SCHEMA, "source_context": _ref(context_path),
                "source_media_sha256": "a" * 64, "production_attempt_id": "original-attempt",
                "exchanges": [item]}
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(manifest))
    return {"kind": "exchanges", **_ref(path)}, request, prompt, item


def _three_uncertain(srt, finding, context):
    def call(prompt):
        return '{"choice":"UNRESOLVED","needs_audio":true}'
    call.cpa_cache_identity = {"model": "fixture"}
    recorder = SemanticAttemptRecorder(srt_text=srt, finding=finding, clip_context=context, offset_ms=0)
    for n in range(3):
        recorder.wrap(call)("synthetic earlier question " + str(n))


@pytest.mark.parametrize("status", ["COMPLETED", "STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_range_consumption_survives_source_delivery_clock_and_unknown(tmp_path, monkeypatch, status):
    reference, _, _, _ = _archive(tmp_path, status=status, ranges=[_range()])
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    _three_uncertain(_srt(), _finding(), _context())
    finding = {**_finding(), HISTORY_KEY: [reference], "_prior_range_acoustic_observations": [_range()]}
    current = _srt(offset=500)
    planned = with_novel_range_plan(srt_text=current, finding=finding, clip_context=_context(), offset_ms=500)
    assert "continuation_plan" not in planned
    recorder = SemanticAttemptRecorder(srt_text=current, finding=finding, clip_context=_context(), offset_ms=500)
    assert recorder.submitted_history()[0]["status"] == ("OUTCOME_UNKNOWN" if status == "STARTED" else status)
    assert len(recorder._entries()) == 3  # No synthetic import into the original attempt schema.


def test_other_target_first_use_not_consumed_by_neighbor_or_range_overlap(tmp_path, monkeypatch):
    reference, _, _, _ = _archive(tmp_path, ranges=[_range()])
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    other = {"cue_index": 2, "why": "A genuinely different physical target"}
    _three_uncertain(_srt(), other, _context())
    finding = {**other, HISTORY_KEY: [reference], "_prior_range_acoustic_observations": [_range()]}
    plan = with_novel_range_plan(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    assert plan["continuation_plan"]["action"] == "expand_context"


def test_wrapper_response_and_other_subtitle_changes_do_not_refresh_material(tmp_path, monkeypatch):
    reference, _, _, _ = _archive(tmp_path, ranges=[_range()])
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    _three_uncertain(_srt(), _finding(), _context())
    finding = {**_finding(), HISTORY_KEY: [reference], "_prior_range_acoustic_observations": [_range(raw="9" * 64)]}
    plan = with_novel_range_plan(srt_text=_srt(neighbor="其他字幕刚修好"), finding=finding,
                                clip_context=_context(), offset_ms=0)
    assert "continuation_plan" not in plan


@pytest.mark.parametrize("status", ["COMPLETED", "STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_original_question_is_consumed_without_copying_old_answer(tmp_path, monkeypatch, status):
    observation = _observation()
    reference, request, _, _ = _archive(tmp_path, status=status, observations=[observation])
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    finding = {**_finding(), HISTORY_KEY: [reference]}
    recorder = SemanticAttemptRecorder(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    calls = []
    def no_provider(prompt):
        calls.append(prompt)
        pytest.fail("same physical question and old materials must not be dispatched")
    no_provider.cpa_cache_identity = {"model": "fixture"}
    with pytest.raises(RetryNeedsPlan, match="PRIOR_SOURCE_QUESTION_ALREADY_OFFERED|PRIOR_CALL_OUTCOME_UNKNOWN"):
        recorder.wrap(no_provider)(_prompt(request, [observation], wrapper="new wrapper"))
    assert calls == [] and recorder._entries() == []
    assert recorder.audit()["events"][-1]["provider_calls"] == 0


@pytest.mark.parametrize("change", ["proposal", "audio"])
def test_actual_changed_comparison_or_acquisition_still_reaches_recording(tmp_path, monkeypatch, change):
    observation = _observation()
    reference, request, _, _ = _archive(tmp_path, observations=[observation])
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    finding = {**_finding(), HISTORY_KEY: [reference]}
    if change == "proposal":
        finding.update(suggestion="丙", proposed_full_cue="丙方发言。")
        request = build_context_adjudication_request(_srt(), finding, clip_context=_context())
    else:
        observation["audio_clip_sha256"] = "1" * 64
    calls = []
    def call(prompt):
        calls.append(prompt)
        return '{"choice":"UNRESOLVED","needs_audio":true}'
    call.cpa_cache_identity = {"model": "fixture"}
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    rec.wrap(call)(_prompt(request, [observation]))
    assert len(calls) == 1 and len(rec._entries()) == 1


@pytest.mark.parametrize("role", ["request", "prompt", "terminal", "completion"])
def test_original_exchange_byte_drift_blocks_before_dispatch(tmp_path, monkeypatch, role):
    reference, request, prompt, item = _archive(tmp_path, observations=[_observation()])
    path = Path(item[role]["path"])
    path.write_bytes(path.read_bytes() + b" ")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    calls = []
    def call(prompt):
        calls.append(prompt)
        return "{}"
    call.cpa_cache_identity = {"model": "fixture"}
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding={**_finding(), HISTORY_KEY: [reference]},
                                  clip_context=_context(), offset_ms=0)
    with pytest.raises(RetryNeedsPlan, match="SEMANTIC_RETAINED_HISTORY_INVALID"):
        rec.wrap(call)(prompt)
    assert not calls


def test_normal_argon_none_source_records_unknown_before_cold_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    calls = []
    def failed_judge(prompt):
        calls.append(prompt)
        raise RuntimeError("synthetic uncertain transport")
    failed_judge.cpa_cache_identity = {"model": "fixture"}
    kwargs = dict(entity_verifier=lambda request: {"status": "OUTCOME_UNKNOWN"},
                  judge_llm_call=failed_judge, clip_context=_context())
    original = _srt()
    output, first = adjudicate_context_finding(original, _finding(), **kwargs)
    assert output == original and calls
    count = len(calls)
    output, second = adjudicate_context_finding(original, _finding(), **kwargs)
    assert output == original and len(calls) == count
    assert second["semantic_retry_progress"]["status"] == "PRIOR_CALL_OUTCOME_UNKNOWN"
    assert not second.get("repaired")
    assert all(json.loads(p.read_bytes())["status"] == "OUTCOME_UNKNOWN"
               for p in (tmp_path / "runtime/cache/semantic-cpa-attempts").rglob("*.json"))



def _seed_agy_acquisition(parent, source, monkeypatch):
    """Original complete-block acquisition; no current-target range was offered."""
    from types import SimpleNamespace
    from tests.test_gemini_range_context import _check_request, _range_request, _verifier
    from src.autoslice import gemini_range_context as gemini
    srt = "1\n00:00:01,000 --> 00:00:03,000\nspoken words\n\n"
    check = _check_request(source, srt, candidate_id="candidate")
    def extract(source_path, output_path, start_ms, end_ms):
        output_path.write_bytes(b"original native mp3")
    def crop(**kwargs):
        kwargs["audio_path"].write_bytes(b"original native black-frame mp4")
        return True, ""
    def observe(**kwargs):
        kwargs["on_provider_dispatch"]("agy", "test-agy-model")
        root = kwargs["job_dir"]
        prompt = root / "prompt.md"
        prompt.write_text(gemini.range_transcript_prompt(kwargs["request"]))
        duration = kwargs["request"]["target_audio_end_ms"]
        raw = {"status": "OBSERVED", "target_audible": True,
            "full_transcript": "heard complete block",
            "segments": [{"segment": "complete_block", "start_ms": 0,
                          "end_ms": duration, "transcript": "heard complete block"}]}
        response = root / "verdict.raw.json"
        response.write_text(json.dumps(raw))
        return SimpleNamespace(observed=raw, provider="agy", model="test-agy-model",
            prompt_path=prompt, response_path=response, provider_failures=[])
    with monkeypatch.context() as scoped:
        scoped.setattr(gemini, "_extract_exact_mp3", extract)
        scoped.setattr(gemini, "_crop_black_frame_audio", crop)
        scoped.setattr(gemini, "_observe_entity_audio", observe)
        rows = gemini.observe_gemini_range_context(
            verifier=_verifier(source, parent), check_request=check,
            range_witness_request=_range_request(check),
            budget_receipt_path=parent / "native-audio-budget.json")
    assert rows[0]["status"] == "OBSERVED"
    return next((parent / "gemini-range-acquisition").rglob("acquisition.json"))


@pytest.mark.parametrize("with_agy", [False, True])
def test_stopped_partial_restores_raw_unknown_and_same_native_account(tmp_path, monkeypatch, with_agy):
    from tests.test_prepared_native_audio_recovery import _seed
    from src.autoslice.retained_partial_evidence import freeze_partial_evidence, load_partial_evidence
    from src.autoslice.supplement_audio_budget import start_budget, get_budget
    parent, source, original = _seed(tmp_path)
    context_path = parent / "context.json"
    context_path.write_text(json.dumps(_context(hashlib.sha256(source.read_bytes()).hexdigest())))
    authority = parent / "parent-authority.json"
    authority.write_text(json.dumps({"final_review_audit": {"release_gate": "BLOCK"},
                                    "cpa_adjudication_usage": {"provider_adjudication_count": 108}}))
    frozen_budget = parent / "original-native-budget.json"
    frozen_budget.write_bytes((parent / "native-audio-budget.json").read_bytes())
    if with_agy:
        _seed_agy_acquisition(parent, source, monkeypatch)
    _exchange(parent, "original synthetic source question", status="COMPLETED")
    _exchange(parent, "original pending source question", name="request-unknown", status="STARTED")
    usage = parent / "candidate.cpa-usage.jsonl"
    usage.write_text(json.dumps({"production_attempt_id": "original-attempt", "kind": "text", "status": "STARTED"}) + "\n")
    manifest = freeze_partial_evidence(output_root=parent, candidate_id="candidate", source=source,
        source_context=_ref(context_path), parent_authority=_ref(authority),
        parent_native_budget=_ref(frozen_budget), manifest_path=tmp_path / "partial.json")
    before = {str(p): p.read_bytes() for p in parent.rglob("*") if p.is_file()}
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    kwargs = dict(reference=manifest, candidate_id="candidate", source=source,
                  parent_authority_sha256=_ref(authority)["sha256"], runtime_root=tmp_path / "runtime")
    first = load_partial_evidence(**kwargs)
    start_budget(source)
    second = load_partial_evidence(**kwargs)
    assert first["native"] == second["native"]
    assert get_budget(source).snapshot()["attempt_count"] == (3 if with_agy else 2)
    assert json.loads(Path(manifest["path"]).read_bytes())["range_observations"] == []
    assert first["proof"]["original_exchange_counts"] == {
        "COMPLETED": 1, "OUTCOME_UNKNOWN": 1, "ERROR": 0, "STARTED": 0}
    assert first["proof"]["partial_logical_adjudication_count"] is None
    assert first["proof"]["release_authorized"] is False
    assert {str(p): p.read_bytes() for p in parent.rglob("*") if p.is_file()} == before
    assert not (parent / "candidate.source-text-checkpoint.json").exists()
    assert not (parent / "candidate.current-text-checkpoint.json").exists()
    assert not (parent / "candidate.chat-authority.json").exists()


@pytest.mark.parametrize("status", ["STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_native_attempt_unknown_material_does_not_reopen_range_plan(tmp_path, monkeypatch, status):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    _three_uncertain(_srt(), _finding(), _context())
    finding = {**_finding(), "_prior_range_acoustic_observations": [_range()]}
    planned = with_novel_range_plan(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    assert planned["continuation_plan"]["action"] == "expand_context"
    def call(prompt):
        return '{"choice":"UNRESOLVED","needs_audio":true}'
    call.cpa_cache_identity = {"model": "fixture"}
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding=planned, clip_context=_context(), offset_ms=0)
    rec.wrap(call)("original fourth request with range")
    row = next(r for r in rec._entries() if r["request"]["prompt"] == "original fourth request with range")
    row["status"] = status  # Stored legacy/crash fixture; never changed to completed by recovery.
    path = rec.directory / (row["request_sha256"] + ".json")
    path.write_text(json.dumps(row))
    before = path.read_bytes()
    finding["_prior_range_acoustic_observations"] = [_range(raw="9" * 64)]
    reopened = with_novel_range_plan(srt_text=_srt(neighbor="邻句已改变"), finding=finding,
                                    clip_context=_context(), offset_ms=0)
    assert "continuation_plan" not in reopened
    assert path.read_bytes() == before


def test_parent_archive_keeps_actual_malformed_reply_without_judged_authority(tmp_path, monkeypatch):
    from src.autoslice.semantic_request_history import freeze_parent_exchanges
    reference, request, prompt, item = _archive(tmp_path, observations=[_observation()])
    raw = "original malformed model response"
    Path(item["completion"]["path"]).write_text(raw)
    terminal = Path(item["terminal"]["path"])
    terminal.write_text(json.dumps({"status": "COMPLETED", "completion_sha256": hashlib.sha256(raw.encode()).hexdigest()}))
    frozen = freeze_parent_exchanges(output_root=tmp_path, source_contexts=[_ref(tmp_path / "context.json")],
                                     source_media_sha256="a" * 64)
    path = tmp_path / "parent-exchanges.json"
    path.write_bytes(frozen)
    reference = {"kind": "exchanges", **_ref(path)}
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding={**_finding(), HISTORY_KEY: [reference]},
                                  clip_context=_context(), offset_ms=0)
    def forbidden(prompt):
        pytest.fail("A malformed captured reply does not undo its actual dispatch")
    forbidden.cpa_cache_identity = {"model": "different-route"}
    with pytest.raises(RetryNeedsPlan, match="PRIOR_SOURCE_QUESTION_ALREADY_OFFERED"):
        rec.wrap(forbidden)(prompt)
    assert not rec._entries()


def test_frozen_unknown_cannot_silently_gain_a_terminal(tmp_path, monkeypatch):
    reference, request, prompt, item = _archive(tmp_path, status="STARTED", observations=[_observation()])
    Path(item["request"]["path"]).with_name("terminal.json").write_text('{"status":"ERROR"}')
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding={**_finding(), HISTORY_KEY: [reference]},
                                  clip_context=_context(), offset_ms=0)
    with pytest.raises(RetryNeedsPlan, match="SEMANTIC_RETAINED_HISTORY_INVALID"):
        rec.submitted_history()


def test_unknown_partial_usage_proves_not_dispatched_without_consuming_fresh_material(tmp_path, monkeypatch):
    from src.autoslice.semantic_request_history import with_partial_usage_guard
    from src.autoslice.semantic_retry_progress import CallNotDispatched
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    _three_uncertain(_srt(), _finding(), _context())
    finding = {**_finding(), "_prior_range_acoustic_observations": [_range()]}
    planned = with_novel_range_plan(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    calls = []
    def call(prompt):
        calls.append(prompt)
        return "{}"
    call.cpa_cache_identity = {"model": "fixture"}
    guard = with_partial_usage_guard(call, {"retained_partial_evidence": {
        "partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS"}})
    rec = SemanticAttemptRecorder(srt_text=_srt(), finding=planned, clip_context=_context(), offset_ms=0)
    with pytest.raises(CallNotDispatched, match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        rec.wrap(guard)("new material, no paid admission")
    assert not calls
    row = rec._entries()[-1]
    assert row["status"] == "NOT_DISPATCHED" and row["dispatch_outcome"] == "NOT_DISPATCHED"
    reopened = with_novel_range_plan(srt_text=_srt(), finding=finding, clip_context=_context(), offset_ms=0)
    assert reopened["continuation_plan"]["action"] == "expand_context"
    # No inherited unknown usage means this guard changes no unrelated budget.
    assert with_partial_usage_guard(call, {})( "independent known accounting") == "{}"
    assert calls == ["independent known accounting"]


def test_unknown_partial_usage_allows_complete_audio_cold_reuse(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    from tests.test_noncritical_routing import BoundWitness, _complete_judge, _finding as typed_finding, srt, _audit
    from src.autoslice.final_review_auditor import adjudicate_exact_release_findings
    from src.autoslice.final_review_contract import is_audio_decided_keep_current, validate_final_review_release
    from src.autoslice.semantic_request_history import with_partial_usage_guard
    source = srt("我把杯子放这儿")
    witness, judge = BoundWitness("wo ba bei zi fang zhe er"), _complete_judge("CURRENT")
    pending, resolved = adjudicate_exact_release_findings(source, [typed_finding(source)],
        entity_verifier=witness, judge_llm_call=judge, initial_provider_adjudication_count=62)
    assert not pending and len(resolved) == 1
    counts = (len(witness.calls), len(judge.calls))
    guard = with_partial_usage_guard(judge, {"retained_partial_evidence": {
        "partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS"}})
    pending, cold = adjudicate_exact_release_findings(source, json.loads(json.dumps(resolved)),
        entity_verifier=witness, judge_llm_call=guard, initial_provider_adjudication_count=63)
    assert not pending and len(cold) == 1 and is_audio_decided_keep_current(cold[0])
    assert counts == (len(witness.calls), len(judge.calls))
    assert cold[0]["exact_release_adjudication"]["provider_budget_replay"]["provider_call_count"] == 0
    assert validate_final_review_release(_audit(source, cold, disclosed=True))["release_gate"] == "PASS"


def test_serialized_partial_lineage_reaches_source_and_final_consumers(tmp_path):
    from src.autoslice.semantic_request_history import partial_proof_from_audit, sources_from_audit, with_partial_usage_guard
    from src.autoslice.retained_partial_evidence import partial_ranges_from_audit
    from src.autoslice.semantic_retry_progress import CallNotDispatched
    path = tmp_path / "frozen-partial.json"
    path.write_text(json.dumps({"range_observations": [_range()]}))
    proof = {"manifest": _ref(path), "partial_logical_usage_status": "UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS",
             "semantic_history_sources": [{"kind": "exchanges", **_ref(path)}]}
    saved = json.loads(json.dumps({"final_review_audit": {"retained_partial_evidence": proof}}))
    assert partial_proof_from_audit(saved) == proof
    assert sources_from_audit(saved) == proof["semantic_history_sources"]
    assert partial_ranges_from_audit(saved) == (_range(),)
    with pytest.raises(CallNotDispatched, match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        with_partial_usage_guard(lambda prompt: pytest.fail("must not dispatch"), saved)("new question")


def test_normal_retained_loader_preserves_unscoped_parent_request_without_invented_target(tmp_path):
    from tests.test_retained_reviewed_draft import _fixture, _load
    from src.autoslice.semantic_request_history import sources_from_audit, submitted_history, validate_exchange_manifest
    spec, output, padded, data = _fixture(tmp_path)
    parent = Path(spec["retained_current_text"]["parent_authority_path"]).parent
    item = _exchange(parent, "original non-scoped source discovery", status="STARTED")
    original = {k: Path(v["path"]).read_bytes() for k, v in item.items() if v}
    first = _load(spec, output, padded)
    second = _load(spec, output, padded)
    refs = sources_from_audit({"retained_reviewed_draft": second["proof"]})
    archive_ref = next(r for r in refs if r["kind"] == "exchanges")
    archive = json.loads(Path(archive_ref["path"]).read_bytes())
    assert validate_exchange_manifest(archive)["OUTCOME_UNKNOWN"] == 1
    assert first["proof"] == second["proof"]
    assert {k: Path(v["path"]).read_bytes() for k, v in item.items() if v} == original
    # An unscoped original discovery consumes no inferred target.
    context = second["parent_clip_context"]
    assert submitted_history(finding={HISTORY_KEY: [archive_ref]}, context=context, target=(10, 30)) == []


@pytest.mark.parametrize("damage", ["audio", "model", "source_window", "missing_metadata"])
def test_original_agy_acquisition_drift_never_refreshes_allowance(tmp_path, monkeypatch, damage):
    from tests.test_prepared_native_audio_recovery import _seed
    from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
    from src.autoslice.gemini_range_context import _digest_json
    from src.autoslice.supplement_audio_budget import get_budget
    parent, source, _ = _seed(tmp_path)
    acquisition = _seed_agy_acquisition(parent, source, monkeypatch)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    args = dict(cid="candidate", parent=parent, source=source, runtime_root=tmp_path)
    state = inherit_prepared_native_audio(**args)
    original_budget = (parent / "native-audio-budget.json").read_bytes()
    before = get_budget(source).snapshot()
    metadata = json.loads(acquisition.read_bytes())
    if damage == "audio":
        Path(metadata["paths"]["actual_provider_audio"]["path"]).write_bytes(b"drift")
    elif damage == "missing_metadata":
        acquisition.unlink()
    else:
        if damage == "model":
            metadata["model"] = "different-model"
        else:
            metadata["source_target_ms"][0] += 100
        metadata.pop("metadata_sha256")
        metadata["metadata_sha256"] = _digest_json(metadata)
        acquisition.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="PREPARED_NATIVE_RANGE_ACQUISITION"):
        inherit_prepared_native_audio(**args)
    assert get_budget(source).snapshot() == before
    assert before["attempt_count"] == 3
    assert (parent / "native-audio-budget.json").read_bytes() == original_budget
    assert state.budget_receipt.read_bytes() == original_budget


def test_bound_argon_none_public_source_records_each_actual_judge_once(tmp_path, monkeypatch):
    from tests.test_noncritical_routing import BoundWitness, _complete_judge, _finding as typed_finding, srt
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    source = srt("我把杯子放这儿")
    finding = typed_finding(source)
    context = _context()
    witness = BoundWitness("wo ba bei zi fang zhe er")
    judge = _complete_judge("CURRENT")
    assert getattr(witness, "argon_escalation", None) is None
    kwargs = dict(entity_verifier=witness, judge_llm_call=judge, clip_context=context,
                  require_complete_utterance_support=True)
    output, decision = adjudicate_context_finding(source, finding, **kwargs)
    assert output == source and decision["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    recorder = SemanticAttemptRecorder(srt_text=source, finding=finding,
                                      clip_context=context, offset_ms=0)
    rows = recorder._entries()
    assert rows and len(rows) == len(judge.calls)
    assert all(row["status"] == "COMPLETED" for row in rows)
    assert {row["request"]["prompt"] for row in rows} == set(judge.calls)
    assert len({row["request_sha256"] for row in rows}) == len(rows)
    before = {p.name: p.read_bytes() for p in recorder.directory.glob("*.json")}
    calls = len(judge.calls)
    reopened, cold = adjudicate_context_finding(source, finding, **kwargs)
    assert reopened == source and cold["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert len(judge.calls) == calls
    assert {p.name: p.read_bytes() for p in recorder.directory.glob("*.json")} == before
