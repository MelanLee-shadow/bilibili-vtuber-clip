"""Regression tests for the relisten acquisition gap (sister 165d111 real run).

Rules: 41 targeted relisten §1/§4 (an original CPA needs_audio answer enters the
audio adapter; K04 itself is not relaxed), 41 same-source identity (finding IDs are
not audio identity), 41:1540-1541 / 40:57 (no re-buying of a stopped run's real
observations), 41 Argon production enablement (SUB-04/SUB-20).
No provider, network or truth injection: fixtures only return protocol-shaped bytes.
"""
from __future__ import annotations

import json
import socket

import pytest

from src.autoslice.final_review_auditor import (
    _candidate_blind_missing_proposal_scaffold, _derive_single_span_edit,
    build_context_adjudication_request,
)
from tests.test_span_phoneme_resolution import Observers, SOURCE_SHA, srt


@pytest.fixture(autouse=True)
def offline(_hermetic_autoslice_base, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def no(*args, **kwargs):
        raise AssertionError("External provider/network forbidden")
    monkeypatch.setattr(socket.socket, "connect", no)
    monkeypatch.setattr(socket, "create_connection", no)


CONTEXT = {"candidate_id": "synthetic-k04", "pieces": [{"source_media_sha256": SOURCE_SHA}]}
TEXT = srt("什么")
FINDING = {"cue_index": 1, "suspect": "什么", "proposed_full_cue": None, "repair_class": "phonetic",
           "why": "Synthetic dispute; the original CPA answers asked for audio."}


def _request():
    scaffold = _candidate_blind_missing_proposal_scaffold(TEXT, FINDING)
    return build_context_adjudication_request(TEXT, scaffold, clip_context=CONTEXT)


def _model(calls, *, needs_audio=True):
    def response(prompt):
        calls.append(prompt)
        return json.dumps({"status": "UNRESOLVED", "decision": "UNRESOLVED", "needs_audio": needs_audio,
                           "reason": "Synthetic conflict remains."})
    response.cpa_cache_identity = {"transport": "fixture", "models": ["k04-gap"], "effort": "high"}
    return response


def _history(calls, *, needs_audio=True, prompt_prefix="## 声音证据的判断次序 prior "):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    model = _model(calls, needs_audio=needs_audio)
    for i in range(3):
        recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
        recorder.wrap(model)(prompt_prefix + str(i))
    return model


class FreshOnly(Observers):
    """Witness cache misses; the normal observer returns a never-offered hearing."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fresh = 0

    def probe_witness_cache(self, q):
        return None

    def __call__(self, q):
        self.fresh += 1
        return Observers.__call__(self, q)


def _prepare(model, verifier):
    from src.autoslice.missing_proposal_bootstrap import prepare_missing_proposal_candidate
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
    return prepare_missing_proposal_candidate(
        srt_text=TEXT, finding=FINDING, structured_chat="", clip_context=CONTEXT,
        llm_call=recorder.wrap(model), derive_single_span_edit=_derive_single_span_edit,
        check_request=_request(), entity_verifier=verifier)


# ---------------------------------------------------------------- A1 proposal path

def test_k04_stop_on_original_audio_request_acquires_and_asks_once_with_a_plan():
    calls = []
    model = _history(calls)
    verifier = FreshOnly(heard="shen me")
    _, audit, failure = _prepare(model, verifier)
    assert verifier.fresh == 1  # the ordinary adapter was actually entered
    record = audit["k04_material_acquisition"]
    assert record["plan_opened"] is True and len(record["original_audio_request_completion_sha256s"]) == 3
    assert audit["prior_text_proposal"]["reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert len(calls) > 3  # the proposal was re-asked once with real audio
    assert "shen me" in calls[3]
    # The same acquired hearing is now offered material: no second plan, K04 stays.
    again = FreshOnly(heard="shen me")
    before = len(calls)
    _, audit2, _ = _prepare(model, again)
    assert audit2.get("k04_material_acquisition", {}).get("plan_opened") is False
    assert len(calls) == before


def test_k04_stop_without_original_audio_request_does_not_acquire():
    calls = []
    model = _history(calls, needs_audio=False)

    class Forbidden(FreshOnly):
        def __call__(self, q):
            raise AssertionError("no CPA answer asked for audio; no acquisition")
    _, audit, failure = _prepare(model, Forbidden())
    assert failure is not None and failure["reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert "k04_material_acquisition" not in (audit or {})
    assert len(calls) == 3


def test_recorder_reads_only_completed_original_audio_requests(tmp_path):
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    calls = []
    _history(calls)
    recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
    assert len(recorder.original_audio_requests()) == 3
    other = SemanticAttemptRecorder(srt_text=srt("别的"), finding={**FINDING, "cue_index": 1},
                                    clip_context={**CONTEXT, "candidate_id": "x",
                                                  "pieces": [{"source_media_sha256": "b" * 64}]},
                                    offset_ms=0)
    assert other.original_audio_requests() == []


# ------------------------------------------------------------- A2 judge-path plan

def test_requested_audio_plan_opens_only_for_unoffered_hearing():
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan, SemanticAttemptRecorder
    calls = []
    model = _history(calls)
    request = _request()
    witness = Observers(heard="shen me")(__import__(
        "src.autoslice.acoustic_witness_adjudication", fromlist=["x"]).build_witness_request(request))
    recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
    with pytest.raises(RetryNeedsPlan):
        recorder.wrap(model)("## 声音证据的判断次序 no new material")
    recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
    recorder.plan_requested_audio_review(check_request=request, witness=witness)
    assert recorder.plan is not None
    assert recorder.material["requested_audio_review_basis"]["kind"] == "ORIGINAL_NEEDS_AUDIO_ANSWERED_BY_UNOFFERED_HEARING"
    recorder.sync_judge_evidence(check_request=request, witness=witness)
    recorder.wrap(model)("## 声音证据的判断次序 with new hearing")
    assert len(calls) == 4
    # Offered now: the same hearing cannot open another plan.
    later = SemanticAttemptRecorder(srt_text=TEXT, finding=FINDING, clip_context=CONTEXT, offset_ms=0)
    later.plan_requested_audio_review(check_request=request, witness=witness)
    assert later.plan is None


def test_judge_retry_stop_uses_the_normal_observer_only_with_an_original_audio_request():
    import inspect
    from src.autoslice import final_review_auditor as auditor
    body = inspect.getsource(auditor)
    assert "deferred_acquisition = bool(" in body
    assert "or (retry_value_deferred and not deferred_acquisition)" in body
    assert "plan_requested_audio_review" in body


# ------------------------------------------------------------ A4 / A5 contracts

def _assistance():
    from src.autoslice import context_assisted_audio as assisted
    return assisted


def test_assisted_reply_with_target_excerpt_and_block_transcript_is_one_heard_block(monkeypatch):
    assisted = _assistance()
    monkeypatch.setattr(assisted, "validate_assistance", lambda value, *a, **k: value)
    assistance = {"sources": {"cue:35": {}}, "assistance_sha256": "c" * 64}
    raw = {"schema_version": "context-assisted-audio-response.v1", "status": "OBSERVED",
           "target_audible": True, "heard": "能能别聊",
           "inferred": [{"text": "但", "source_refs": ["cue:35"]}, {"text": "不", "source_refs": ["cue:35"]}],
           "uncertain": [{"question": "q", "audio_basis": "b"}],
           "full_transcript": "开播可以，能能别聊老男人啊。",
           "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": 5740,
                         "transcript": "开播可以，能能别聊老男人啊。"}], "audio_observations": []}
    out = assisted.validate_response(raw, assistance)
    assert out["heard"] == "开播可以，能能别聊老男人啊。" and out["target_heard_excerpt"] == "能能别聊"
    for bad in ({"heard": "听不到的"}, {"segments": [{"transcript": "别的"}]}, {"full_transcript": ""}):
        with pytest.raises(ValueError, match="SCHEMA_INVALID|INFERENCE_IN_TRANSCRIPT"):
            assisted.validate_response({**raw, **bad}, assistance)
    same = {**raw, "heard": raw["full_transcript"]}
    assert "target_heard_excerpt" not in assisted.validate_response(same, assistance)


def test_saved_witness_is_projected_against_its_own_request_not_the_new_evidence_id():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request, retry_judge_evidence
    request = _request()
    old_request = dict(request, evidence_id="old-finding-identity")
    old_request.pop("request_sha256", None)
    bound = build_witness_request(old_request)
    target = build_witness_request(request)
    assert bound["request_sha256"] != target["request_sha256"]
    row = Observers(heard="shen me")(bound)
    with pytest.raises(ValueError, match="witness binding"):
        retry_judge_evidence(request, row)
    material = retry_judge_evidence(request, row, bound_request=bound)
    assert material and material[0]["response_sha256"]
    moved = dict(bound, matched_start_ms=bound["matched_start_ms"] + 10)
    with pytest.raises(ValueError, match="witness binding"):
        retry_judge_evidence(request, row, bound_request=moved)


# ------------------------------------------------------------------ A3 Argon on

def test_runner_and_derived_production_specs_enable_argon_but_keep_explicit_values(monkeypatch, tmp_path):
    from src.autoslice.argon_escalation_defaults import (
        apply_production_argon_default, is_production_runtime_spec,
    )
    spec = {"candidate_id": "x"}
    # COST-01 (task): the production default also opens the paid
    # backup and the same-key realtime method; "enabled" is unchanged.
    assert apply_production_argon_default(spec) and spec["argon_escalation"]["enabled"] is True
    off = {"argon_escalation": {"enabled": False}}
    assert not apply_production_argon_default(off) and off["argon_escalation"] == {"enabled": False}
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "base"))
    assert is_production_runtime_spec({"runtime_root": str(tmp_path / "base")})
    assert not is_production_runtime_spec({"runtime_root": str(tmp_path / "other")})
    assert not is_production_runtime_spec({})
    import inspect
    from src.autoslice import producer_request, talk_lane
    assert "apply_production_argon_default(spec)" in inspect.getsource(talk_lane)
    assert "is_production_runtime_spec(spec)" in inspect.getsource(producer_request.load_producer_request)


def test_argon_targeted_admission_enters_at_three_effective_observations(tmp_path):
    from src.autoslice.argon_escalation_integration import FindingSession as ArgonSession
    entered = []

    class Probe(ArgonSession):
        def __init__(self):
            self.envelope, self.advanced, self.audit = None, False, None

        def advance(self):
            entered.append(True)
            self.advanced = True
    session = Probe()
    assert session.consider_targeted_relisten(check={}, original_request={}, prior={}, recovery={},
                                              projection={"effective_distinct_observations": 2}) is False
    assert entered == []


# ------------------------------------------- B interrupted run's own native account

def _own_interrupted(tmp_path, monkeypatch, *, damage=None):
    import hashlib
    import shutil
    from src.autoslice.native_audio_budget_receipt import resume_native_audio_budget
    from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
    from src.autoslice.supplement_audio_budget import start_budget
    from tests.test_native_audio_history_reconciliation import _observe
    from tests.test_prepared_native_audio_recovery import _seed
    parent, source, _ = _seed(tmp_path)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    own = tmp_path / "rerun-output" / "candidate"
    if damage == "prefix":
        own.mkdir(parents=True)
        start_budget(source)
        _observe(own, source, start=7000, tag="unrelated history")
    else:
        shutil.copytree(parent, own, ignore=shutil.ignore_patterns("padded.mp4"))
        start_budget(source)
        resume_native_audio_budget(source_media=source, source_media_sha256=sha,
                                   receipt_path=own / "native-audio-budget.json")
        _observe(own, source, start=7000, tag="interrupted run real hearing")
        if damage == "original":
            next((own / "native-exact" / "mai").glob("*-7000-8000/input.audio-evidence.mai.json")).unlink()
    start_budget(source)
    canonical = inherit_prepared_native_audio(cid="candidate", parent=parent, source=source, runtime_root=runtime)
    return source, runtime, canonical, own


def _attempt_windows(receipt):
    budget = json.loads(receipt.read_bytes())["budget"]
    return [(a["provider"], a["start_ms"], a["end_ms"], a["status"]) for a in budget["attempts"]]


def test_interrupted_run_account_with_complete_originals_extends_the_inherited_ledger(tmp_path, monkeypatch):
    from src.autoslice.prepared_native_audio_recovery import join_own_interrupted_account
    source, runtime, canonical, own = _own_interrupted(tmp_path, monkeypatch)
    parent_windows = _attempt_windows(canonical.budget_receipt)
    own_before = (own / "native-audio-budget.json").read_bytes()
    joined, record = join_own_interrupted_account(
        canonical, cid="candidate", out_root=own, source=source, runtime_root=runtime)
    assert record["status"] == "JOINED"
    windows = _attempt_windows(joined.budget_receipt)
    assert windows[:len(parent_windows)] == parent_windows  # exact prefix, no reset
    assert ("mai", 7000, 8000, "OBSERVED") in windows[len(parent_windows):]
    assert list(joined.cache_root.glob("native-exact/mai/*-7000-8000/input.audio-evidence.mai.json"))
    assert (own / "native-audio-budget.json").read_bytes() == own_before  # original account untouched
    again, record2 = join_own_interrupted_account(
        joined, cid="candidate", out_root=own, source=source, runtime_root=runtime)
    assert record2["status"] == "JOINED" and _attempt_windows(again.budget_receipt) == windows


@pytest.mark.parametrize("damage", ["original", "prefix"])
def test_interrupted_run_account_without_proof_is_not_joined(tmp_path, monkeypatch, damage):
    from src.autoslice.prepared_native_audio_recovery import join_own_interrupted_account
    source, runtime, canonical, own = _own_interrupted(tmp_path, monkeypatch, damage=damage)
    before = canonical.budget_receipt.read_bytes()
    state, record = join_own_interrupted_account(
        canonical, cid="candidate", out_root=own, source=source, runtime_root=runtime)
    assert record["status"] == "NOT_JOINED" and state is canonical
    assert canonical.budget_receipt.read_bytes() == before


def test_retained_parent_loader_joins_the_interrupted_runs_own_account():
    import inspect
    from src.autoslice import retained_reviewed_draft
    body = inspect.getsource(retained_reviewed_draft.load_retained_reviewed_draft)
    assert "join_own_interrupted_account(" in body and 'result["retained_native_own_attempt"]' in body


# ------------------------------------------------------------ guimi 165d111 codes

def test_retained_history_reader_failure_reason_is_persisted():
    from src.autoslice.semantic_request_history import HISTORY_KEY
    from src.autoslice.semantic_retry_progress import RetryNeedsPlan, SemanticAttemptRecorder
    finding = {**FINDING, HISTORY_KEY: [{"kind": "authority", "path": "/nonexistent/authority.json",
                                         "sha256": "sha256:" + "0" * 64}]}
    recorder = SemanticAttemptRecorder(srt_text=TEXT, finding=finding, clip_context=CONTEXT, offset_ms=0)
    with pytest.raises(RetryNeedsPlan, match="SEMANTIC_RETAINED_HISTORY_INVALID"):
        recorder.submitted_history()
    event = recorder.audit()["events"][-1]
    assert event["status"] == "SEMANTIC_RETAINED_HISTORY_INVALID"
    assert event["error_type"] == "ValueError" and event["reason_detail"] == "RETAINED_CPA_FILE_MISSING"


def test_nested_independent_rows_keep_their_own_pre_candidate_binding():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request, retry_judge_evidence
    request = _request()
    earlier = dict(request, evidence_id="independent-source-request")
    earlier.pop("request_sha256", None)
    nested = Observers(heard="shen me")(build_witness_request(earlier))
    nested = {**nested, "response_sha256": "d" * 64, "audio_clip_sha256": "e" * 64}
    witness = Observers(heard="shen me")(build_witness_request(request))
    material = retry_judge_evidence(request, {**witness, "independent_observations": [nested]})
    assert {row["response_sha256"] for row in material} >= {"d" * 64}
    with pytest.raises(ValueError, match="witness binding"):
        retry_judge_evidence(request, nested)  # a top-level foreign binding still fails
    moved = {**nested, "audio_start_ms": nested["audio_start_ms"] + 500}
    with pytest.raises(ValueError, match="witness coverage"):
        retry_judge_evidence(request, {**witness, "independent_observations": [moved]})


def test_assisted_textual_hearing_notes_are_kept_and_never_sound_events(monkeypatch):
    from src.autoslice import context_assisted_audio as assisted
    from src.autoslice.independent_context_adjudication import validated_audio_observations
    monkeypatch.setattr(assisted, "validate_assistance", lambda value, *a, **k: value)
    assistance = {"sources": {"cue:28": {}}, "assistance_sha256": "c" * 64}
    raw = {"schema_version": "context-assisted-audio-response.v1", "status": "OBSERVED", "target_audible": True,
           "heard": "我们要把我们俩演一个", "inferred": [], "uncertain": [],
           "full_transcript": "我们要把我们俩演一个",
           "segments": [{"segment": "complete_block", "start_ms": 0, "end_ms": 7150, "transcript": "我们要把我们俩演一个"}],
           "audio_observations": ["‘要’后无独立可听的‘然’音节", "‘把’为低元音[a]"]}
    assert validated_audio_observations(raw, 7150) is None  # the old reading of strings
    shaped, notes = assisted.split_text_notes(raw)
    assert validated_audio_observations(shaped, 7150) == [] and len(notes) == 2
    out = assisted.validate_response(raw, assistance)
    assert out["assisted_audio_notes"] == notes and out["independent_witness"] is False
    mixed = {**raw, "audio_observations": ["note", {"kind": "environment_sound"}]}
    assert assisted.split_text_notes(mixed) == (mixed, [])


def test_retained_range_replay_compares_cue_grid_and_anchor_not_whole_srt_bytes():
    import inspect
    from src.autoslice import targeted_relisten
    body = inspect.getsource(targeted_relisten.replay_retained_range_observations)
    # admin round 2 (G2): the grid is compared on the source clock.
    assert "same_physical_cue_grid(previous[\"whole_clip_current_srt\"]," in body
    assert '("whole_clip_current_srt", "current_cue", "clip_context_binding")' not in body
    assert "validate_range_acoustic_observation(check[\"whole_clip_current_srt\"], check, row)" in body


def test_argon_ordinary_reads_acoustic_cache_hit_copies(tmp_path):
    import inspect
    from src.autoslice import argon_escalation_integration as integration
    body = inspect.getsource(integration)
    assert 'acoustic_hit = (manifest.get("acoustic_cache") or {}).get("hit") is True' in body
    assert '"response.acoustic-cache*"' in body


def test_unprovable_native_narrow_row_is_not_counted_and_does_not_block_assisted_audio(tmp_path, monkeypatch):
    from copy import deepcopy
    from rejection_reachability_registry import blind_seed
    from src.autoslice import context_assisted_audio as assisted
    row = blind_seed(tmp_path, monkeypatch)
    check = row['check']
    original = assisted.validate_prior_blind(check)
    foreign = deepcopy(check['prior_acoustic_observations'][0])
    foreign['verdict'].update(witness_protocol='candidate_blind_transcript',
        exact_transcript='unproven native row', candidate_exposure='none', source_media_sha256='0'*64)
    for key in ('heard_pinyin','confidence','syllable_count','uncertain_positions'):
        foreign['verdict'].pop(key,None)
    check['prior_acoustic_observations'].append(foreign)
    assert assisted.validate_prior_blind(check) == original
    check['clip_context_binding']['source_media_sha256s'] = ['f'*64]
    with pytest.raises(ValueError, match='ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND'):
        assisted.validate_prior_blind(check)


def test_native_narrow_row_on_producer_media_uses_the_validated_range_ancestry():
    import inspect
    from src.autoslice import targeted_relisten
    body = inspect.getsource(targeted_relisten.evidence_projection)
    assert "deferred_native.append(row)" in body and "recording_identities(media_binding)" in body
    assert "if not proven or not proven.issubset(sources):" in body


def test_k04_stop_does_not_ask_an_owner_without_its_cache_first_record():
    """40:57 / 41:1541: a provider that cannot serve its own completed record first
    is not asked under a continuation stop, even when old answers wanted audio."""
    calls = []
    model = _history(calls)

    def bare(_request):
        raise AssertionError("an owner without a cache-first record must not be asked")
    _, audit, failure = _prepare(model, bare)
    assert failure is not None and failure["reason_code"] == "CONTINUATION_VALUE_PLAN_REQUIRED"
    assert "k04_material_acquisition" not in (audit or {}) and len(calls) == 3
