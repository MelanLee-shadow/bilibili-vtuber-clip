"""Interruption recovery contracts. Synthetic bytes; no network or media provider."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
from types import SimpleNamespace

import pytest

from scripts import subtitle_regression_eval as evaluator
from src.autoslice import subtitle_regression_recovery as recovery
from src.autoslice.clip_context import SCHEMA_VERSION, _canonical_json_sha256
from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder, RetryNeedsPlan
from src.autoslice.supplement_audio_budget import start_budget

SRT = "1\n00:00:01,000 --> 00:00:03,000\n合成原始文字\n"
EXPECTED = "SYNTHETIC_SCORER_ONLY_NEVER_A_PROMPT"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n")


@pytest.fixture(autouse=True)
def no_providers(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("interruption tests must not connect to any provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.delenv("AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES", raising=False)


def freeze(s):
    files, links = recovery.inventory(s.parent)
    external = {}
    for name in ("record", "srt", "authority", "media", "context"):
        p = Path(s.package[name + "_path"])
        external[str(p)] = {"sha256": evaluator.digest(p), "bytes": p.stat().st_size}
    status = json.loads(s.terminal.read_text())
    code = Path(status["cwd"])
    doc = {"schema_version": "subtitle-interruption-evidence-freeze.v1",
           "parent_output": str(s.parent), "code_root": str(code),
           "parent_files": files, "parent_links": links, "external_inputs": external,
           "external_files": {str(p): {"sha256": evaluator.digest(p), "bytes": p.stat().st_size}
                              for p in (s.terminal, s.fixture, *([s.outer] if hasattr(s, 'outer') else []))},
           "parent_code_files": {str(p.relative_to(code)):
                                {"sha256": evaluator.digest(p), "bytes": p.stat().st_size}
                                for p in code.rglob("*") if p.is_file()}}
    write(s.freeze, doc)
    return s


@pytest.fixture
def specimen(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    paid = runtime / "state/gemini-paid-backup"
    write(paid / "usage.json", {"old_charge": 17, "unknown_reserved": 3})
    task = runtime / "private-runs/task"
    parent = task / "normal-01"
    state = evaluator.configure_replay_state(parent, runtime)
    write(parent / "replay-state.json", state)
    source = tmp_path / "original"
    source.mkdir()
    (source / "source.mp4").write_bytes(b"synthetic media, no extraction")
    (source / "source.srt").write_text(SRT)
    context = {"schema_version": SCHEMA_VERSION, "candidate_id": "synthetic",
               "recording_date": "2026-10-19", "mutation_authorized": False,
               "whole_clip_draft_srt": SRT,
               "whole_clip_draft_srt_sha256": "sha256:" + recovery.sha(SRT.encode()),
               "retrieval_budget": {"whole_clip_transcript_truncated": False},
               "pieces": [{"source_media_sha256": "sha256:" + evaluator.digest(source / "source.mp4"),
                           "start_ms": 0, "end_ms": 4000}]}
    context["context_sha256"] = _canonical_json_sha256(context)
    write(source / "context.json", context)
    write(source / "record.json", {"start_ms": 0, "end_ms": 4000,
          "clip_context_payload_sha256": context["context_sha256"],
          "subtitle_audio_correspondence": {"intro_offset_ms": 1000}})
    write(source / "authority.json", {"final_review_audit": {}})
    package = {"candidate_id": "synthetic", "intro_offset_ms": 1000,
               "context_path": str(source / "context.json")}
    for key, name in [("record", "record.json"), ("srt", "source.srt"),
                      ("authority", "authority.json"), ("media", "source.mp4")]:
        package[key + "_path"] = str(source / name)
        package[key + "_sha256"] = evaluator.digest(source / name)
    cases = [{"id": "synthetic-cue1", "candidate_id": "synthetic", "package": "example",
              "status": "ready", "source": {"quote": "Synthetic fixture only"},
              "cue_ids": [1], "body_window_ms": [1000, 3000], "finished_window_ms": [2000, 4000],
              "wrong_text": "合成原始文字",
              "expected": {"required_severity": "critical", "correct_text": EXPECTED}}]
    fixture = task / "cases.json"
    write(fixture, {"schema_version": "subtitle-regressions.v1",
                    "packages": {"example": package}, "cases": cases})
    folder = parent / "example"
    folder.mkdir()
    (folder / "original.srt").write_text(SRT)
    (folder / "final.srt").write_text(SRT)
    (folder / "review-input-01.srt").write_text(SRT)
    write(folder / "authority.json", {"final_review_audit": {"findings": [{"cue": 1}]},
                                     "pending_obligations": ["synthetic-outside-obligation"]})
    write(folder / "review-01.json", {"reviewed_srt_sha256": "sha256:" + recovery.sha(SRT.encode()),
                                      "findings": [{"cue": 1}]})
    budget = start_budget(source / "source.mp4")
    budget.consume("moss", "synthetic-model", 1000, 2000)
    persist_native_audio_budget_receipt(source_media=source / "source.mp4",
        source_media_sha256=evaluator.digest(source / "source.mp4"),
        receipt_path=folder / "native-audio-budget.json")
    recorder = SemanticAttemptRecorder(srt_text=SRT, finding={"cue_index": 1},
                                      clip_context=context, offset_ms=0)
    def success(prompt):
        return '{"status":"UNRESOLVED","needs_audio":true}'
    success.cpa_cache_identity = {"model": "synthetic"}
    recorder.wrap(success)("synthetic successful transport")
    def unknown(prompt):
        raise TimeoutError("synthetic timeout")
    unknown.cpa_cache_identity = success.cpa_cache_identity
    with pytest.raises(TimeoutError):
        recorder.wrap(unknown)("synthetic unknown transport")
    cpa = folder / "cpa-text-exchanges/request-synthetic"
    prompt, completion = b"synthetic prompt", b'{"status":"UNRESOLVED"}'
    write(cpa / "request.json", {"schema_version": "private-cpa-text-exchange.v1",
          "status": "STARTED", "prompt_sha256": recovery.sha(prompt)})
    (cpa / "prompt.txt").write_bytes(prompt)
    (cpa / "completion.txt").write_bytes(completion)
    write(cpa / "terminal.json", {"status": "COMPLETED", "completion_sha256": recovery.sha(completion)})
    code = task / "code" / ("a" * 40)
    write(code / "scripts/subtitle_regression_eval.py", {"synthetic_code_binding": True})
    command = ["python", str(code / "scripts/subtitle_regression_eval.py"), "--worker", "--normal-off",
               "--output", str(parent), "--code-root", str(code), "--code-identity", code.name,
               "--runtime-root", str(runtime), "--cases", str(fixture), "--case", cases[0]["id"]]
    terminal = task / "RUN-STATUS-01.json"
    write(terminal, {"status": "FAILED", "returncode": -15, "launcher_pid": 999999998,
          "worker_pid": 999999999, "started_utc": "2026-10-19T01:00:00+00:00",
          "ended_utc": "2026-10-19T05:00:00+00:00", "head": code.name, "cwd": str(code),
          "maximum_wall_seconds": 14400, "output": str(parent), "command": command})
    s = SimpleNamespace(parent=parent, runtime=runtime, paid=paid, task=task, folder=folder,
                        package=package, cases=cases, fixture=fixture, terminal=terminal,
                        freeze=task / "freeze.json", code=code, context=context, recorder=recorder)
    return freeze(s)


def inspect(s):
    return recovery.inspect_parent(parent=s.parent, freeze_path=s.freeze,
        freeze_sha256=evaluator.digest(s.freeze), cases_path=s.fixture, cases=s.cases,
        runtime_root=s.runtime, code_identity="b" * 40)


def test_interrupted_input_has_no_fake_evaluation_or_quality_inheritance(specimen):
    s = specimen
    before = recovery.inventory(s.parent), s.terminal.read_bytes()
    result = inspect(s)
    p = result["packages"]["example"]
    assert p["parent_package_state"] == "INTERRUPTED"
    assert p["next_state"] == "ORDINARY_EXACT_FINAL_REVIEW_REQUIRED"
    assert p["authority_findings"] == 1 and p["quality_inherited"] is False
    assert EXPECTED not in json.dumps(result)
    assert not (s.parent / "evaluation.json").exists() and not (s.folder / "run.json").exists()
    assert (recovery.inventory(s.parent), s.terminal.read_bytes()) == before


@pytest.mark.parametrize("damage", ["current", "authority", "source", "fixture", "code", "request", "completion"])
def test_frozen_drift_refused_before_output_or_provider(specimen, damage):
    s = specimen
    p = {"current": s.folder / "final.srt", "authority": s.folder / "authority.json",
         "source": Path(s.package["media_path"]), "fixture": s.fixture,
         "code": s.code / "scripts/subtitle_regression_eval.py",
         "request": s.folder / "cpa-text-exchanges/request-synthetic/prompt.txt",
         "completion": s.folder / "cpa-text-exchanges/request-synthetic/completion.txt"}[damage]
    p.write_bytes(p.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        inspect(s)
    assert not (s.task / "interrupted-recovery-claims").exists()


@pytest.mark.parametrize("defect", ["missing_reply", "unknown_status", "source_clock",
                                   "changed_without_typed", "review_grid", "live_pid",
                                   "bad_terminal_clock", "same_case_new_mode"])
def test_frozen_invalid_evidence_still_rejected_not_just_inventory_checked(specimen, defect):
    s = specimen
    if defect == "missing_reply":
        (s.folder / "cpa-text-exchanges/request-synthetic/completion.txt").unlink()
    elif defect == "unknown_status":
        p = s.folder / "cpa-text-exchanges/request-synthetic/terminal.json"
        d = json.loads(p.read_text())
        d["status"] = "PASS"
        write(p, d)
    elif defect == "source_clock":
        d = json.loads(Path(s.package["record_path"]).read_text())
        d["subtitle_audio_correspondence"]["intro_offset_ms"] = 999
        write(Path(s.package["record_path"]), d)
    elif defect == "changed_without_typed":
        (s.folder / "final.srt").write_text(SRT.replace("原始", "改变"))
    elif defect == "review_grid":
        (s.folder / "review-input-01.srt").write_text(SRT.replace("01,000", "02,000"))
    else:
        d = json.loads(s.terminal.read_text())
        if defect == "live_pid":
            d["worker_pid"] = os.getpid()
        if defect == "bad_terminal_clock":
            d["ended_utc"] = "2026-10-18T01:00:00+00:00"
        if defect == "same_case_new_mode":
            d["command"].remove("--normal-off")
        write(s.terminal, d)
    freeze(s)
    with pytest.raises((ValueError, FileNotFoundError)):
        inspect(s)
    assert not (s.task / "interrupted-recovery-claims").exists()


def test_successor_keeps_all_bytes_unknown_and_single_paid_account(specimen):
    s = specimen
    before = recovery.inventory(s.parent)
    paid = (s.paid / "usage.json").read_bytes()
    result = inspect(s)
    child = s.task / "recovered-01"
    receipt = recovery.stage_successor(result, child)
    for source, target in receipt["copied_to"].items():
        assert (s.parent / source).read_bytes() == (child / target).read_bytes()
    assert recovery.inventory(s.parent) == before
    assert (s.paid / "usage.json").read_bytes() == paid
    assert (child / "runtime-state/state/gemini-paid-backup").resolve() == s.paid.resolve()
    account = result["packages"]["example"]["accounting"]
    assert account["native"]["total_audio_ms"] == 1000
    assert account["native"]["status_counts"] == {"DISPATCHED": 1}
    assert account["native"]["pending_attempt_count"] == 1
    assert account["semantic_outcomes"] == {"COMPLETED": 1, "OUTCOME_UNKNOWN": 1}
    assert account["unknown_refunded"] is False and account["cost_usd"] is None
    assert not (child / "example/run.json").exists()
    with pytest.raises(FileExistsError):
        recovery.stage_successor(result, s.task / "different-attempt")
    assert not (s.task / "different-attempt").exists()


def test_parent_lock_and_atomic_claim_refuse_two_starters(specimen):
    s = specimen
    with recovery.parent_lock(s.parent):
        with pytest.raises(BlockingIOError):
            with recovery.parent_lock(s.parent):
                pass
    result = inspect(s)
    def start(n):
        try:
            recovery.stage_successor(result, s.task / ("child-" + str(n)))
            return "STARTED"
        except FileExistsError:
            return "REFUSED"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(start, [1, 2])) == ["REFUSED", "STARTED"]


def test_existing_semantic_replay_success_and_unknown_do_not_rebuy(specimen, monkeypatch):
    s = specimen
    receipt = recovery.stage_successor(inspect(s), s.task / "child")
    monkeypatch.setenv("AUTOSLICE_BASE", str(s.task / "child/runtime-state"))
    recorder = SemanticAttemptRecorder(srt_text=SRT, finding={"cue_index": 1},
                                      clip_context=s.context, offset_ms=0)
    calls = []
    def forbidden(prompt):
        calls.append(prompt)
        raise AssertionError("same transport must not be resent")
    forbidden.cpa_cache_identity = {"model": "synthetic"}
    answer = recorder.wrap(forbidden)("synthetic successful transport")
    assert answer == '{"status":"UNRESOLVED","needs_audio":true}'
    assert recorder.events[-1]["status"] == "EXACT_RAW_REPLAY"
    with pytest.raises(RetryNeedsPlan, match="PRIOR_CALL_OUTCOME_UNKNOWN"):
        recorder.wrap(forbidden)("synthetic unknown transport")
    assert calls == []
    assert receipt["history_reset"] is False


def test_stage_tuple_survives_failed_pointer_and_second_interruption(tmp_path, monkeypatch):
    out = tmp_path / "package"
    out.mkdir()
    authority = {"outside": [{"cue": 9, "status": "UNRESOLVED"}]}
    first = recovery.package_checkpoint(out, phase="REVIEW_STARTED",
        run={"would_release": None}, text=SRT.encode(), authority=authority)
    original = first.read_bytes()
    atomic = recovery._write_bytes_atomic
    def fail(*a, **kw):
        raise OSError("synthetic pointer interruption")
    monkeypatch.setattr(recovery, "_write_bytes_atomic", fail)
    with pytest.raises(OSError):
        recovery.package_checkpoint(out, phase="REVIEW_RETURNED",
            run={"would_release": None}, text=SRT.encode(), authority=authority)
    second = out / "regression-checkpoints/stage-0002.json"
    assert second.exists()
    assert json.loads(second.read_bytes())["parent_stage_sha256"] == recovery.sha(original)
    monkeypatch.setattr(recovery, "_write_bytes_atomic", atomic)
    third = recovery.package_checkpoint(out, phase="PACKAGE_INTERRUPTED",
        run={"interrupted": True, "would_release": None}, text=SRT.encode(), authority=authority)
    d = json.loads(third.read_bytes())
    assert d["parent_stage_sha256"] == evaluator.digest(second)
    assert d["release_authorized"] is False
    assert first.read_bytes() == original
    blob = out / "regression-checkpoints/blobs" / (d["authority_sha256"] + ".json")
    assert json.loads(blob.read_bytes()) == authority


def test_signal_unwinds_with_bounded_limit():
    with pytest.raises(recovery.EvaluationInterrupted) as caught:
        with recovery.bounded_attempt(1):
            os.kill(os.getpid(), signal.SIGTERM)
    assert caught.value.signum == signal.SIGTERM
    for seconds in (0, 14401):
        with pytest.raises(ValueError):
            with recovery.bounded_attempt(seconds):
                pass


def test_public_preflight_never_loads_credentials(specimen, monkeypatch):
    s = specimen
    def forbidden():
        pytest.fail("preflight must precede credential loader")
    monkeypatch.setattr(evaluator, "load_production_gemini_environment", forbidden)
    monkeypatch.chdir(evaluator.ROOT)
    out = s.task / "preflight"
    args = SimpleNamespace(output=out, code_root=evaluator.ROOT, code_identity="b" * 40,
        runtime_root=str(s.runtime), cases=s.fixture, case=[s.cases[0]["id"]], normal_off=True,
        resume_existing=False, resume_interrupted=s.parent, interruption_evidence=s.freeze,
        interruption_evidence_sha256=evaluator.digest(s.freeze), recovery_preflight=True)
    assert evaluator.worker(args) == 0
    d = json.loads((out / "preflight.json").read_bytes())
    assert d["provider_calls"] == 0 and d["quality_inherited"] is False
    assert not (s.task / "interrupted-recovery-claims").exists()


def test_old_complete_resume_still_refuses_this_interrupted_parent(specimen):
    s = specimen
    with pytest.raises(ValueError, match="prior evaluation is not terminal"):
        evaluator.prepare_experiment_resume(s.parent, s.fixture, s.cases, "b" * 40, s.runtime)


def test_parser_requires_exact_interruption_identity_and_normal_off(tmp_path):
    with pytest.raises(SystemExit) as e:
        evaluator.main(["--worker", "--resume-interrupted", str(tmp_path),
                        "--output", str(tmp_path / "out")])
    assert e.value.code == 2


@pytest.mark.parametrize("interrupt", [False, True])
def test_actual_ordinary_gate_retains_inputs_and_persists_interrupted_package(specimen, monkeypatch, interrupt):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice.final_review_meaning_policy import POLICY_VERSION
    from src.autoslice import argon_comparison_policy
    # This tests the actual text gate and persistence, not acoustic quality.
    monkeypatch.setattr(argon_comparison_policy, "route_probes",
                        lambda *a: {"status": "SYNTHETIC_ROUTE_NOT_EVALUATED", "samples": []})
    s = specimen
    record = recovery.stage_successor(inspect(s), s.task / "gate-child")
    package = record["packages"]["example"]
    folder = s.task / "gate-child/example"
    (folder / "review-input-02.srt").write_text(SRT)
    before_input = (folder / "review-input-02.srt").read_bytes()
    observed = []
    def build(**kwargs):
        assert EXPECTED not in json.dumps(kwargs["spec"])
        assert kwargs["authority"]["pending_obligations"] == ["synthetic-outside-obligation"]
        def reviewer(text, authority, start, end):
            observed.append((text, json.loads(json.dumps(authority))))
            if interrupt:
                raise recovery.EvaluationInterrupted(signal.SIGTERM)
            return {"schema_version": "final-review-audit.v2", "meaning_policy": POLICY_VERSION,
                    "reviewed_srt_sha256": "sha256:" + recovery.sha(text.encode()),
                    "status": "FLAGGED", "release_gate": "BLOCK", "validated_finding_count": 0,
                    "findings": [], "reason_codes": ["SYNTHETIC_REVIEW_NOT_RELEASE"]}
        return reviewer
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", build)
    monkeypatch.setenv("AUTOSLICE_BASE", str(s.task / "gate-child/runtime-state"))
    kwargs = dict(normal_samples=package["normal_samples"], recovery=package,
                  checkpoint_identity={"code_identity": "b" * 40, "fixture_sha256": evaluator.digest(s.fixture)})
    if interrupt:
        with pytest.raises(recovery.EvaluationInterrupted):
            evaluator.run_package(s.package, folder, evaluator.ROOT, {}, **kwargs)
    else:
        evaluator.run_package(s.package, folder, evaluator.ROOT, {}, **kwargs)
    run = json.loads((folder / "run.json").read_bytes())
    assert observed and observed[0][0] == SRT
    assert observed[0][1]["pending_obligations"] == ["synthetic-outside-obligation"]
    assert (folder / "review-input-02.srt").read_bytes() == before_input
    assert (folder / "review-input-03.srt").read_text() == SRT
    assert run["would_release"] is (None if interrupt else False)
    assert run.get("interrupted", False) is interrupt
    assert run["provider_usage"]["cost_usd"] is None
    assert run["provider_usage"]["continuation_delta"]["model_requests"] == 0
    stages = recovery.validate_stage_history(folder, (folder / "final.srt").read_bytes(),
                                             json.loads((folder / "authority.json").read_bytes()))
    assert stages["current_matches_last_stage"]
    assert stages["last_phase"] == ("PACKAGE_INTERRUPTED" if interrupt else "PACKAGE_RETURNED")
    assert not (s.parent / "evaluation.json").exists()


def test_stage_blob_tampering_refused_even_with_refrozen_inventory(tmp_path):
    out = tmp_path / "package"
    out.mkdir()
    stage = recovery.package_checkpoint(out, phase="PACKAGE_INTERRUPTED",
        run={"interrupted": True}, text=SRT.encode(), authority={"pending": [9]})
    row = json.loads(stage.read_bytes())
    blob = out / "regression-checkpoints/blobs" / (row["text_sha256"] + ".srt")
    blob.write_bytes(b"changed")
    with pytest.raises(ValueError, match="file hash drift"):
        recovery.validate_stage_history(out, SRT.encode(), {"pending": [9]})


def test_second_interruption_keeps_parent_chain_and_completed_package_receipts(specimen):
    s = specimen
    child = s.task / "child-one"
    record = recovery.stage_successor(inspect(s), child)
    write(child / "package-results/example.json", {"synthetic": "previous returned stage; not a pass"})
    write(child / "example/run.json", {"candidate_id": s.package["candidate_id"], "input_drift": [],
        "input_hashes": record["packages"]["example"]["input_hashes"],
        "final_srt_sha256": evaluator.digest(child / "example/final.srt"),
        "normal_off": {"probe_samples_sha256": recovery.sha(recovery.canonical(
            record["packages"]["example"]["normal_samples"]))},
        "interrupted": True, "would_release": None})
    old_status = json.loads(s.terminal.read_text())
    code = s.task / "code" / record["code_identity"]
    write(code / "scripts/subtitle_regression_eval.py", {"synthetic_child_code": True})
    new_status = {**old_status, "status": "INTERRUPTED", "output": str(child),
                  "head": code.name, "cwd": str(code),
                  "parent_output": str(s.parent), "parent_freeze_sha256": record["freeze_sha256"]}
    replacements = {str(s.parent): str(child), str(s.code): str(code), s.code.name: code.name,
                    str(s.code / "scripts/subtitle_regression_eval.py"):
                    str(code / "scripts/subtitle_regression_eval.py")}
    new_status["command"] = [replacements.get(item, item) for item in old_status["command"]]
    terminal = child / "attempt-terminal.json"
    write(terminal, new_status)
    again = SimpleNamespace(**{**vars(s), "parent": child, "folder": child / "example",
                               "terminal": terminal, "freeze": s.task / "freeze-second.json"})
    freeze(again)
    child_bytes = recovery.inventory(child)
    second = inspect(again)
    assert second["packages"]["example"]["parent_package_state"] == "INTERRUPTED"
    descendant = s.task / "child-two"
    receipt = recovery.stage_successor(second, descendant)
    assert not (descendant / "package-results/example.json").exists()
    assert not (descendant / "example/run.json").exists()
    for relative in ("attempt-terminal.json", "interrupted-resume.json", "package-results/example.json"):
        target = descendant / receipt["copied_to"][relative]
        assert target.read_bytes() == (child / relative).read_bytes()
    assert recovery.inventory(child) == child_bytes
    assert json.loads((s.terminal).read_bytes())["status"] == "FAILED"
