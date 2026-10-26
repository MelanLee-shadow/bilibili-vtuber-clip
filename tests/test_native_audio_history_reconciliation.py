"""Forked native accounts retain original identities, bytes and unknown reservations."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src.autoslice.native_audio_budget_receipt import (
    BudgetReceiptPersistenceError, persist_native_audio_budget_receipt,
    resume_native_audio_budget, receipt_sha256, _require_budget_history_extension,
)
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.supplement_audio_budget import get_budget, start_budget
from src.autoslice.subtitle_audio_evidence import CONTRACT, _digest
from tests.test_prepared_native_audio_recovery import _seed


def _ref(path):
    return {"path":str(path),"sha256":"sha256:"+hashlib.sha256(path.read_bytes()).hexdigest()}


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value,sort_keys=True)+"\n")
    return _ref(path)


def _observe(root, source, *, start, status="OBSERVED", tag="branch"):
    budget = get_budget(source)
    token = budget.consume("mai","MAI-Transcribe-2",start,start+1000)
    if status != "DISPATCHED":
        budget.finish_attempt(token,status=status)
    if status == "OBSERVED":
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        audio = root/"native-exact/mai"/f"{sha[:20]}-{start}-{start+1000}"/"input.mp3"
        audio.parent.mkdir(parents=True,exist_ok=True)
        audio.write_bytes(b"same source window")
        binding = {"contract":CONTRACT,"provider":"mai","model":"MAI-Transcribe-2",
            "input_audio_sha256":hashlib.sha256(audio.read_bytes()).hexdigest(),
            "duration_ms":1000,"candidate_exposure":"none"}
        evidence = {**binding,"status":"OBSERVED","raw_response":{"text":tag}}
        audio.with_suffix(".audio-evidence.mai.json").write_text(json.dumps({
            "binding":binding,"evidence":evidence,"evidence_sha256":_digest(evidence)}))
    persist_native_audio_budget_receipt(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=root/"native-audio-budget.json")


def _fork(tmp_path, monkeypatch, *, budget_config=None):
    ancestor, source, _ = _seed(tmp_path)
    budget_config = budget_config or {}
    if budget_config:
        receipt = json.loads((ancestor/"native-audio-budget.json").read_bytes())
        budget = receipt["budget"]
        budget.update(budget_config)
        budget["remaining_windows"] = budget["max_windows"] - budget["distinct_window_count"]
        budget["remaining_audio_ms"] = budget["max_audio_ms"] - budget["total_audio_ms"]
        receipt["receipt_sha256"] = receipt_sha256(receipt)
        _save(ancestor/"native-audio-budget.json", receipt)
        start_budget(source, **budget_config)
    runtime = tmp_path/"runtime"
    monkeypatch.setenv("AUTOSLICE_BASE",str(runtime))
    canonical = inherit_prepared_native_audio(cid="candidate",parent=ancestor,
        source=source,runtime_root=runtime)
    _observe(canonical.cache_root,source,start=5000,tag="canonical original")
    frozen = tmp_path/"plan-inputs/canonical.json"
    frozen.parent.mkdir()
    shutil.copy2(canonical.budget_receipt,frozen)
    incoming = tmp_path/"incoming"
    shutil.copytree(ancestor,incoming)
    start_budget(source, **budget_config)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    resume_native_audio_budget(source_media=source,source_media_sha256=sha,
        receipt_path=incoming/"native-audio-budget.json")
    _observe(incoming,source,start=7000,tag="incoming distinct first reservation")
    _observe(incoming,source,start=5000,tag="incoming distinct response")
    _observe(incoming,source,start=9000,status="DISPATCHED")
    start_budget(source, **budget_config)
    ancestor_ref = _ref(ancestor/"native-audio-budget.json")
    plan = {"schema_version":"native-audio-history-reconciliation-plan.v1",
        "candidate_id":"candidate","source_media_sha256":sha,
        "canonical":_ref(frozen),"incoming":_ref(incoming/"native-audio-budget.json"),
        "ancestor":ancestor_ref,"mutation_authorized":False,"release_authorized":False}
    for key in ("canonical","incoming","ancestor"):
        plan[key]["receipt_sha256"] = json.loads(Path(plan[key]["path"]).read_bytes())["receipt_sha256"]
    reference = _save(tmp_path/"plan.json",plan)
    return source,runtime,canonical,incoming,reference


def _inherit(state):
    source,runtime,canonical,incoming,reference = state
    return inherit_prepared_native_audio(cid="candidate",parent=incoming,
        source=source,runtime_root=runtime,reconciliation=reference)


def _auto(state):
    source,runtime,canonical,incoming,_ = state
    return inherit_prepared_native_audio(cid="candidate",parent=incoming,
        source=source,runtime_root=runtime)


def test_populated_fork_reconciles_from_registered_ancestry_and_preserves_branch_ids_unknown_and_raw(tmp_path,monkeypatch):
    # Phase-2 task book section 2 (K08 change requirement): the ordinary
    # entry must not need a hand-written SPEC plan. It derives the plan from
    # the account's own registered parent manifest and archive.
    state = _fork(tmp_path,monkeypatch)
    source,runtime,canonical,incoming,reference = state
    before = canonical.budget_receipt.read_bytes()
    raw = {str(p):p.read_bytes() for p in incoming.rglob("*") if p.is_file()}
    canonical_audio = next((canonical.cache_root/"native-exact/mai").glob("*-5000-6000/*.json"))
    canonical_raw = canonical_audio.read_bytes()
    result = _auto(state)
    receipt = json.loads(canonical.budget_receipt.read_bytes())
    plan_reference = receipt["budget"]["account_history"]["plan_reference"]
    plan = json.loads(Path(plan_reference["path"]).read_bytes())
    assert Path(plan_reference["path"]).parent == canonical.cache_root/"history-plans"
    assert plan["derivation"]["method"] == "REGISTERED_PARENT_ARCHIVE_COMMON_SEALED_ANCESTOR"
    assert plan["mutation_authorized"] is False and plan["release_authorized"] is False
    # The occupied canonical original is archived byte-for-byte, not edited.
    assert Path(plan["canonical"]["path"]).read_bytes() == before
    assert plan["ancestor"]["sha256"] == json.loads(Path(reference["path"]).read_bytes())["ancestor"]["sha256"]
    snapshot = get_budget(source).snapshot()
    assert snapshot["attempt_count"] == 6
    assert snapshot["pending_attempt_count"] == 1
    assert snapshot["total_audio_ms"] == 7000
    h = snapshot["account_history"]
    assert h["canonical"]["budget"]["attempt_count"] == 3
    assert h["incoming"]["budget"]["attempt_count"] == 5
    assert h["ancestor"]["budget"]["attempt_count"] == 2
    assert [r["attempt_id"] for r in snapshot["attempts"]] == [1,2,3,3,4,5]
    assert len({(r["history_branch_sha256"],r["attempt_id"]) for r in snapshot["attempts"]}) == 6
    assert canonical_audio.read_bytes() == canonical_raw
    fork_file = next((result.history_cache_roots[0]/"native-exact/mai").glob("*-5000-6000/*.json"))
    assert fork_file.read_bytes() != canonical_raw
    assert {str(p):p.read_bytes() for p in incoming.rglob("*") if p.is_file()} == raw
    # Active branch IDs continue within their original namespace.
    token = get_budget(source).consume("moss","moss-transcribe-diarize-pro",15000,16000)
    assert token == 4
    get_budget(source).finish_attempt(token,status="FAILED",reason_code="TEST_OFFLINE")
    persist_native_audio_budget_receipt(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=canonical.budget_receipt)
    after = get_budget(source).snapshot()
    assert after["attempt_count"] == 7 and after["pending_attempt_count"] == 1
    assert after["account_history"]["incoming"] == h["incoming"]


def test_reconciliation_repeats_and_fresh_process_cold_without_writes_or_dispatch(tmp_path,monkeypatch):
    state = _fork(tmp_path,monkeypatch)
    result = _inherit(state)
    source,runtime,canonical,incoming,reference = state
    before = {str(p):(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns)
              for p in canonical.cache_root.rglob("*") if p.is_file()}
    start_budget(source)
    assert _inherit(state) == result
    assert {str(p):(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns)
            for p in canonical.cache_root.rglob("*") if p.is_file()} == before
    code = """import json,sys
from pathlib import Path
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.supplement_audio_budget import get_budget
def guard(event,args):
 if event in {'socket.connect','socket.getaddrinfo','socket.sendto','subprocess.Popen'}: raise AssertionError(event)
sys.addaudithook(guard)
s=Path(sys.argv[1])
inherit_prepared_native_audio(cid='candidate',parent=Path(sys.argv[2]),source=s,runtime_root=Path(sys.argv[3]),reconciliation=json.loads(sys.argv[4]))
print(json.dumps(get_budget(s).snapshot()))
"""
    child = subprocess.run([sys.executable,"-B","-c",code,str(source),str(incoming),str(runtime),json.dumps(reference)],
        cwd=Path(__file__).parents[1],env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"},capture_output=True,text=True)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == get_budget(source).snapshot()
    assert {str(p):(p.read_bytes(),p.stat().st_ino,p.stat().st_mtime_ns)
            for p in canonical.cache_root.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("damage",["source","candidate","incoming_identity","canonical_identity",
    "ancestor_identity","missing_ancestry","raw_observation","plan_bytes","release_authority"])
def test_unproven_or_damaged_fork_never_changes_occupied_account(tmp_path,monkeypatch,damage):
    state = _fork(tmp_path,monkeypatch)
    source,runtime,canonical,incoming,reference = state
    plan = json.loads(Path(reference["path"]).read_bytes())
    if damage in {"source","candidate"}:
        plan["source_media_sha256" if damage=="source" else "candidate_id"] = "different"
    elif damage.endswith("_identity"):
        key = damage.removesuffix("_identity")
        path = Path(plan[key]["path"])
        data = json.loads(path.read_bytes())
        data["budget"]["attempts"][0]["model"] = "tampered"
        data["receipt_sha256"] = receipt_sha256(data)
        plan[key] = {**_save(path,data),"receipt_sha256":data["receipt_sha256"]}
    elif damage == "missing_ancestry":
        (canonical.cache_root/"parents"/(plan["ancestor"]["sha256"][7:]+".json")).unlink()
    elif damage == "raw_observation":
        path = next((incoming/"native-exact/mai").glob("*-1000-2500/*.json"))
        path.write_text("{}")
    elif damage == "release_authority":
        plan["release_authorized"] = True  # A plan can never supply release authority.
    if damage != "plan_bytes":
        reference = _save(Path(reference["path"]),plan)
        state = (*state[:-1],reference)
    else:
        Path(reference["path"]).write_text("{}")
    before = {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()}
    with pytest.raises((ValueError,OSError,KeyError)):
        _inherit(state)
    assert {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()} == before
    assert get_budget(source).snapshot()["attempt_count"] == 0


def test_v2_prefix_stays_strict_and_v3_cannot_drop_or_edit_branch(tmp_path,monkeypatch):
    state = _fork(tmp_path,monkeypatch)
    _inherit(state)
    receipt = json.loads(state[2].budget_receipt.read_bytes())
    for component in ("canonical","incoming","ancestor"):
        _require_budget_history_extension(receipt["budget"]["account_history"][component],receipt)
    with pytest.raises(BudgetReceiptPersistenceError):
        _require_budget_history_extension(receipt,receipt["budget"]["account_history"]["incoming"])
    changed = copy.deepcopy(receipt)
    changed["budget"]["account_history"]["incoming"]["budget"]["attempts"][-1]["start_ms"] += 1
    with pytest.raises((BudgetReceiptPersistenceError,ValueError)):
        _require_budget_history_extension(receipt,changed)


def test_account_lock_excludes_concurrent_reconciliation_and_later_retry_is_idempotent(tmp_path,monkeypatch):
    from src.autoslice.native_audio_budget_receipt import exclusive_native_audio_budget
    state = _fork(tmp_path,monkeypatch)
    source,runtime,canonical,incoming,reference = state
    before = {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()}
    code = """import json,sys
from pathlib import Path
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
try:
 inherit_prepared_native_audio(cid='candidate',parent=Path(sys.argv[2]),source=Path(sys.argv[1]),runtime_root=Path(sys.argv[3]),reconciliation=json.loads(sys.argv[4]))
except Exception as e:
 print(type(e).__name__)
 raise SystemExit(2)
raise SystemExit(0)
"""
    with exclusive_native_audio_budget(canonical.budget_receipt):
        child = subprocess.run([sys.executable,"-B","-c",code,str(source),str(incoming),str(runtime),json.dumps(reference)],
            cwd=Path(__file__).parents[1],capture_output=True,text=True)
    assert child.returncode == 2 and "BudgetReceiptPersistenceError" in child.stdout
    assert {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()} == before
    _inherit(state)
    first = get_budget(source).snapshot()
    _inherit(state)
    assert get_budget(source).snapshot() == first


def test_interrupted_migration_preserves_original_canonical_and_can_finish_once(tmp_path,monkeypatch):
    from src.autoslice import prepared_native_audio_recovery as recovery
    state = _fork(tmp_path,monkeypatch)
    canonical = state[2]
    before = canonical.budget_receipt.read_bytes()
    with monkeypatch.context() as scoped:
        def failed_commit(*args,**kwargs):
            raise OSError("simulated durable commit failure")
        scoped.setattr(recovery,"_write_atomic",failed_commit)
        with pytest.raises(OSError,match="simulated"):
            _inherit(state)
    assert canonical.budget_receipt.read_bytes() == before
    _inherit(state)
    assert get_budget(state[0]).snapshot()["attempt_count"] == 6
    manifest = json.loads((canonical.cache_root/"parent.json").read_bytes())
    assert len(manifest["parents"]) == 2
    first = canonical.budget_receipt.read_bytes()
    _inherit(state)
    assert canonical.budget_receipt.read_bytes() == first


@pytest.mark.parametrize("cap", ["total", "windows", "attempts"])
def test_enforced_limits_include_both_original_branches(tmp_path, monkeypatch, cap):
    from src.autoslice.supplement_audio_budget import (
        BudgetExceeded, TOTAL_AUDIO_CAP, WINDOW_CAP, ATTEMPT_CAP,
    )
    config = {"enforce_limits":True, "max_windows":4 if cap=="windows" else 6,
              "max_audio_ms":7500 if cap=="total" else 20000}
    state = _fork(tmp_path, monkeypatch, budget_config=config)
    _inherit(state)
    budget = get_budget(state[0])
    start = {"total":7000, "windows":11000, "attempts":5000}[cap]
    expected = {"total":TOTAL_AUDIO_CAP, "windows":WINDOW_CAP, "attempts":ATTEMPT_CAP}[cap]
    with pytest.raises(BudgetExceeded):
        budget.consume("mai", "MAI-Transcribe-2", start, start+1000,
                       max_attempts_per_window=2 if cap=="attempts" else None)
    snapshot = budget.snapshot()
    assert snapshot["attempt_count"] == 6 and snapshot["pending_attempt_count"] == 1
    assert snapshot["refusals"][-1]["reason_code"] == expected
    assert snapshot["account_history"]["incoming"]["budget"]["attempt_count"] == 5


def test_stale_v2_writer_cannot_overwrite_reconciled_account_even_with_larger_revision(tmp_path, monkeypatch):
    state = _fork(tmp_path, monkeypatch)
    _inherit(state)
    source, _, canonical, _, reference = state
    before = canonical.budget_receipt.read_bytes()
    plan = json.loads(Path(reference["path"]).read_bytes())
    start_budget(source)
    resume_native_audio_budget(source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=Path(plan["canonical"]["path"]))
    budget = get_budget(source)
    for start in (20000,22000,24000):
        token = budget.consume("mai","MAI-Transcribe-2",start,start+1000)
        budget.finish_attempt(token,status="FAILED",reason_code="OFFLINE_STALE_WRITER")
    assert budget.snapshot()["revision"] > json.loads(before)["revision"]
    with pytest.raises(BudgetReceiptPersistenceError,match="branch history was removed"):
        persist_native_audio_budget_receipt(source_media=source,
            source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            receipt_path=canonical.budget_receipt)
    assert canonical.budget_receipt.read_bytes() == before


def test_cold_restore_revalidates_plan_bytes_without_erasing_account(tmp_path, monkeypatch):
    state = _fork(tmp_path, monkeypatch)
    _inherit(state)
    before = state[2].budget_receipt.read_bytes()
    Path(state[-1]["path"]).write_text("{}")
    start_budget(state[0])
    with pytest.raises(BudgetReceiptPersistenceError,match="reference bytes changed"):
        _inherit(state)
    assert state[2].budget_receipt.read_bytes() == before
    assert get_budget(state[0]).snapshot()["attempt_count"] == 0

def test_auto_reconciliation_is_idempotent_and_cold_without_a_plan_input(tmp_path,monkeypatch):
    state = _fork(tmp_path,monkeypatch)
    source,runtime,canonical,incoming,_ = state
    result = _auto(state)
    first = get_budget(source).snapshot()
    files = {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()}
    start_budget(source)
    assert _auto(state) == result
    assert get_budget(source).snapshot() == first
    assert {str(p):p.read_bytes() for p in canonical.cache_root.rglob("*") if p.is_file()} == files
    code = """import json,sys
from pathlib import Path
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.supplement_audio_budget import get_budget
s=Path(sys.argv[1])
inherit_prepared_native_audio(cid='candidate',parent=Path(sys.argv[2]),source=s,runtime_root=Path(sys.argv[3]))
print(json.dumps(get_budget(s).snapshot()))
"""
    child = subprocess.run([sys.executable,"-B","-c",code,str(source),str(incoming),str(runtime)],
        cwd=Path(__file__).parents[1],env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"},capture_output=True,text=True)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout) == first


@pytest.mark.parametrize("damage",["unregistered_ancestor","missing_archive","pending_ancestor","ancestor_parent_drift"])
def test_unprovable_registered_ancestry_keeps_original_rejection_and_account(tmp_path,monkeypatch,damage):
    state = _fork(tmp_path,monkeypatch)
    source,runtime,canonical,incoming,reference = state
    account = canonical.cache_root
    manifest_path = account/"parent.json"
    manifest = json.loads(manifest_path.read_bytes())
    ancestor_sha = manifest["parents"][0]["budget_sha256"]
    if damage == "unregistered_ancestor":
        manifest["parents"] = []
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "missing_archive":
        (account/"parents"/(ancestor_sha[7:]+".json")).unlink()
    elif damage == "pending_ancestor":
        archive = account/"parents"/(ancestor_sha[7:]+".json")
        data = json.loads(archive.read_bytes())
        data["budget"]["pending_attempt_count"] = 1
        archive.write_text(json.dumps(data))
        manifest["parents"][0]["budget_sha256"] = "sha256:"+hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.rename(account/"parents"/(manifest["parents"][0]["budget_sha256"][7:]+".json"))
        manifest_path.write_text(json.dumps(manifest))
    elif damage == "ancestor_parent_drift":
        ancestor_dir = Path(manifest["parents"][0]["path"])
        path = next((ancestor_dir/"native-exact/mai").glob("*/*.json"))
        path.write_text("{}")
    before = {str(p):p.read_bytes() for p in account.rglob("*") if p.is_file()}
    start_budget(source)
    with pytest.raises(BudgetReceiptPersistenceError,match="prior attempt identity changed"):
        _auto(state)
    assert {str(p):p.read_bytes() for p in account.rglob("*") if p.is_file()} == before
    assert not (account/"history-plans").exists() or not any((account/"history-plans").iterdir())
