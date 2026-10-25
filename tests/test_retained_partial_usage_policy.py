"""Partial transport observations never become invented logical CPA admissions."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.autoslice.retained_partial_evidence import freeze_partial_evidence, load_partial_evidence
from src.autoslice.semantic_request_history import (
    with_partial_usage_guard, preserve_partial_usage, partial_proof_from_audit,
)
from src.autoslice.semantic_retry_progress import CallNotDispatched
from tests.test_prepared_native_audio_recovery import _seed
from tests.test_retained_semantic_history import _context, _exchange, _ref


def _partial(tmp_path, monkeypatch):
    parent, source, _ = _seed(tmp_path)
    context = parent/"context.json"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    context.write_text(json.dumps(_context(source_sha)))
    authority = parent/"parent-authority.json"
    authority.write_text(json.dumps({"schema_version":"chat-authority-audit.v2",
        "mutation_authorized":False,"final_review_audit":{"release_gate":"BLOCK"},
        "cpa_adjudication_usage":{"provider_adjudication_count":108,"policy":"SOFT","soft_limit":12}}))
    native = parent/"original-native-budget.json"
    native.write_bytes((parent/"native-audio-budget.json").read_bytes())
    _exchange(parent,"original unscoped complete question",status="COMPLETED")
    _exchange(parent,"original unscoped unfinished question",name="request-unknown",status="STARTED")
    usage = parent/"candidate.cpa-usage.jsonl"
    usage.write_text(json.dumps({"production_attempt_id":"original-attempt","kind":"text","status":"STARTED"})+"\n")
    ref = freeze_partial_evidence(output_root=parent,candidate_id="candidate",source=source,
        source_context=_ref(context),parent_authority=_ref(authority),parent_native_budget=_ref(native),
        manifest_path=tmp_path/"partial.json")
    monkeypatch.setenv("AUTOSLICE_BASE",str(tmp_path/"runtime"))
    loaded = load_partial_evidence(reference=ref,candidate_id="candidate",source=source,
        parent_authority_sha256=_ref(authority)["sha256"],runtime_root=tmp_path/"runtime")
    audit = {"retained_partial_evidence":loaded["proof"],"cpa_adjudication_usage":{
        "provider_adjudication_count":108,"policy":"SOFT","soft_limit":12}}
    return source,parent,audit


def _soft(call,audit,source):
    return with_partial_usage_guard(call,audit,usage_dependency="SOFT_PRESSURE",
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),candidate_id="candidate")


def test_verified_unknown_partial_allows_soft_consumer_and_preserves_unknown_amount(tmp_path,monkeypatch):
    source,parent,audit = _partial(tmp_path,monkeypatch)
    before = {str(p):p.read_bytes() for p in parent.rglob("*") if p.is_file()}
    calls = []
    def call(prompt):
        calls.append(prompt)
        return "{}"
    assert _soft(call,audit,source)("new independent comparison") == "{}"
    assert calls == ["new independent comparison"]
    usage = preserve_partial_usage({"provider_adjudication_count":110,"policy":"SOFT"},audit)
    assert usage["known_provider_adjudication_lower_bound"] == 110
    assert usage["exact_provider_adjudication_count"] is None
    assert usage["partial_usage_lineage"]["partial_logical_adjudication_count"] is None
    assert usage["partial_usage_lineage"]["original_exchange_counts"]["OUTCOME_UNKNOWN"] == 1
    assert usage["partial_usage_lineage"]["original_exchange_counts"]["COMPLETED"] == 1
    assert {str(p):p.read_bytes() for p in parent.rglob("*") if p.is_file()} == before
    assert audit["cpa_adjudication_usage"]["provider_adjudication_count"] == 108
    assert not list(parent.glob("*.source-text-checkpoint.json"))


def test_same_unknown_still_blocks_exact_count_dependency_and_offline_provider_policy(tmp_path,monkeypatch):
    source,parent,audit = _partial(tmp_path,monkeypatch)
    def forbidden(prompt):
        pytest.fail("no provider admission")
    with pytest.raises(CallNotDispatched,match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        with_partial_usage_guard(forbidden,audit)("requires exact cumulative allowance")
    from src.autoslice.final_review_provider_budget import ContextAdjudicationBudget
    budget = ContextAdjudicationBudget(limit=12,entity_verifier=None,clip_context={},
        source_media_timeline_offset_ms=0,judge_llm_call=_soft(forbidden,audit,source),
        screen_read_probe=None,adjudicate_context=forbidden,provider_calls_allowed=False,
        initial_provider_adjudication_count=108)
    result = budget.adjudicate("1\n00:00:00,010 --> 00:00:00,030\nword\n",{"cue_index":1,"suspect":"word"})
    assert result["status"] == "SKIPPED_PROVIDER_DISABLED"
    assert not result["repaired"]
    assert result["cpa_resource_pressure"]["provider_calls_this_decision"] == 0
    assert result["cpa_resource_pressure"]["exact_provider_adjudication_count"] is None


@pytest.mark.parametrize("damage",["fake_soft_flag","source","candidate","manifest","parent","usage","terminal","unknown_disposition"])
def test_unverified_partial_cannot_authorize_new_call(tmp_path,monkeypatch,damage):
    source,parent,audit = _partial(tmp_path,monkeypatch)
    proof = audit["retained_partial_evidence"]
    expected_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate = "candidate"
    if damage == "fake_soft_flag":
        proof.clear()
        proof.update(partial_logical_usage_status="UNKNOWN_PRESERVED_IN_ORIGINAL_EVENTS",
                     verified=True,policy="SOFT")
    elif damage == "source":
        expected_sha = "0"*64
    elif damage == "candidate":
        candidate = "another"
    elif damage == "manifest":
        Path(proof["manifest"]["path"]).write_text("{}")
    elif damage == "parent":
        Path(proof["parent_authority"]["path"]).write_text("{}")
    elif damage == "usage":
        Path(proof["raw_cpa_usage"]["path"]).write_text("{}\n")
    elif damage == "terminal":
        (parent/"cpa-text-exchanges/request-unknown/terminal.json").write_text('{"status":"COMPLETED"}')
    else:
        proof["partial_logical_usage_status"] = "NOT_DISPATCHED"
    calls = []
    guard = with_partial_usage_guard(lambda prompt:calls.append(prompt),audit,
        usage_dependency="SOFT_PRESSURE",source_media_sha256=expected_sha,candidate_id=candidate)
    with pytest.raises(CallNotDispatched,match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        guard("cannot turn historical uncertainty into fresh allowance")
    assert calls == []


def test_unknown_lineage_roundtrips_and_revalidates_in_fresh_process(tmp_path,monkeypatch):
    source,parent,audit = _partial(tmp_path,monkeypatch)
    usage = preserve_partial_usage({"provider_adjudication_count":108,"policy":"SOFT"},audit)
    saved = {"cpa_adjudication_usage":usage}
    assert partial_proof_from_audit(saved) == audit["retained_partial_evidence"]
    path = tmp_path/"saved-usage.json"
    path.write_text(json.dumps(saved))
    code = """import json,sys
from pathlib import Path
from src.autoslice.semantic_request_history import with_partial_usage_guard,preserve_partial_usage
a=json.loads(Path(sys.argv[1]).read_bytes())
n=[]
def guard(event,args):
 if event in {'socket.connect','socket.getaddrinfo','socket.sendto','subprocess.Popen'}: raise AssertionError(event)
sys.addaudithook(guard)
call=with_partial_usage_guard(lambda p:n.append(p),a,usage_dependency='SOFT_PRESSURE',source_media_sha256=sys.argv[2],candidate_id='candidate')
call('fresh consumer with original unknown preserved')
print(json.dumps({'sentinels':len(n),'usage':preserve_partial_usage(a['cpa_adjudication_usage'],a)}))
"""
    child = subprocess.run([sys.executable,"-B","-c",code,str(path),hashlib.sha256(source.read_bytes()).hexdigest()],
        cwd=Path(__file__).parents[1],env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"},capture_output=True,text=True)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["sentinels"] == 1 and result["usage"] == usage
    missing = copy.deepcopy(saved)
    del missing["cpa_adjudication_usage"]["partial_usage_lineage"]
    with pytest.raises(ValueError,match="RETAINED_PARTIAL_LINEAGE_CONFLICT"):
        partial_proof_from_audit(missing)


@pytest.mark.parametrize("damage", ["replace", "parent", "manifest", "remove"])
def test_verified_guard_rejects_later_lineage_drift(tmp_path, monkeypatch, damage):
    source, _, audit = _partial(tmp_path, monkeypatch)
    calls = []
    guarded = _soft(lambda prompt:calls.append(prompt), audit, source)
    guarded("first independent question")
    if damage == "replace":
        audit["retained_partial_evidence"] = {"manifest":{"path":"/other","sha256":"0"*64}}
    elif damage == "remove":
        audit.pop("retained_partial_evidence")
    else:
        audit["retained_partial_evidence"][damage if damage=="manifest" else "parent_authority"]["sha256"] = "sha256:"+"0"*64
    with pytest.raises(CallNotDispatched,match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        guarded("cannot reuse old validation after lineage changes")
    assert calls == ["first independent question"]


def test_lineage_drift_during_first_validation_is_not_admitted(tmp_path, monkeypatch):
    from src.autoslice import semantic_request_history as history
    source, _, audit = _partial(tmp_path, monkeypatch)
    original = copy.deepcopy(audit["retained_partial_evidence"])
    validate = history._validated_partial_usage
    calls = []
    def mutate_after_validation(proof, **kwargs):
        checked = validate(proof, **kwargs)
        audit["retained_partial_evidence"]["parent_authority"]["sha256"] = "sha256:"+"0"*64
        assert proof == original  # The validator receives a detached immutable input snapshot.
        return checked
    monkeypatch.setattr(history, "_validated_partial_usage", mutate_after_validation)
    guarded = _soft(lambda prompt:calls.append(prompt), audit, source)
    with pytest.raises(CallNotDispatched,match="RETAINED_PARTIAL_CPA_USAGE_UNKNOWN"):
        guarded("must remain undispatched")
    assert calls == []


def test_same_lineage_allows_counter_growth_without_rescanning_originals(tmp_path, monkeypatch):
    from src.autoslice import semantic_request_history as history
    source, _, audit = _partial(tmp_path, monkeypatch)
    validate = history._validated_partial_usage
    validations, calls = [], []
    def counted(proof, **kwargs):
        validations.append(1)
        return validate(proof, **kwargs)
    monkeypatch.setattr(history, "_validated_partial_usage", counted)
    guarded = _soft(lambda prompt:calls.append(prompt), audit, source)
    guarded("new question one")
    audit["retained_partial_evidence"] = json.loads(json.dumps(audit["retained_partial_evidence"],sort_keys=True))
    audit["cpa_adjudication_usage"]["provider_adjudication_count"] = 109
    guarded("new question two")
    assert len(calls) == 2 and len(validations) == 1
    audit["cpa_adjudication_usage"]["provider_adjudication_count"] = 107
    with pytest.raises(CallNotDispatched):
        guarded("a lower bound cannot regress")
    assert len(calls) == 2


@pytest.mark.parametrize("status", ["COMPLETED", "STARTED", "ERROR", "OUTCOME_UNKNOWN"])
def test_soft_partial_admission_never_reopens_old_target_material(tmp_path, monkeypatch, status):
    from src.autoslice.final_review_auditor import build_context_adjudication_request
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan
    from src.autoslice.semantic_request_history import HISTORY_KEY, EXCHANGES_SCHEMA
    from tests.test_retained_semantic_history import _srt, _finding, _prompt, _observation
    from src.autoslice.supplement_audio_budget import get_budget
    source, _, audit = _partial(tmp_path, monkeypatch)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    context = _context(source_sha)
    root = tmp_path/"original-target-history"
    root.mkdir()
    context_path = root/"context.json"
    context_path.write_text(json.dumps(context))
    request = build_context_adjudication_request(_srt(), _finding(), clip_context=context)
    observation = {**_observation(),"source_media_sha256":source_sha}
    item = _exchange(root, _prompt(request,[observation]),status=status)
    manifest = root/"archive.json"
    manifest.write_text(json.dumps({"schema_version":EXCHANGES_SCHEMA,"source_context":_ref(context_path),
        "source_media_sha256":source_sha,"production_attempt_id":"original-attempt","exchanges":[item]}))
    reference = {"kind":"exchanges",**_ref(manifest)}
    original = {str(p):p.read_bytes() for p in root.rglob("*") if p.is_file()}
    calls = []
    def sentinel(prompt):
        calls.append(prompt)
        return '{"choice":"UNRESOLVED","needs_audio":true}'
    sentinel.cpa_cache_identity = {"model":"fixture"}
    guarded = _soft(sentinel,audit,source)
    finding = {**_finding(),HISTORY_KEY:[reference]}
    recorder = SemanticAttemptRecorder(srt_text=_srt(),finding=finding,clip_context=context,offset_ms=0)
    with pytest.raises(RetryNeedsPlan,match="PRIOR_SOURCE_QUESTION_ALREADY_OFFERED|PRIOR_CALL_OUTCOME_UNKNOWN"):
        recorder.wrap(guarded)(_prompt(request,[observation],wrapper="new delivery wrapper"))
    assert calls == [] and recorder._entries() == []
    # The same material is legal first use for a genuinely different physical target.
    other = {"cue_index":2,"kind":"context","repair_class":"phonetic","suspect":"邻",
             "suggestion":"临","proposed_full_cue":"临句",
             "base_text_sha256":hashlib.sha256("邻句".encode()).hexdigest(),HISTORY_KEY:[reference]}
    other_request = build_context_adjudication_request(_srt(),other,clip_context=context)
    other_recorder = SemanticAttemptRecorder(srt_text=_srt(),finding=other,clip_context=context,offset_ms=0)
    other_recorder.wrap(guarded)(_prompt(other_request,[observation]))
    assert len(calls) == 1 and len(other_recorder._entries()) == 1
    assert get_budget(source).snapshot()["attempt_count"] == 2
    assert {str(p):p.read_bytes() for p in root.rglob("*") if p.is_file()} == original
