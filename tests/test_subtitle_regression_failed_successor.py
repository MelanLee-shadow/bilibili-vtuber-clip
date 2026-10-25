"""Latest failed evaluation admission, immutable ancestry, and single successor."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import subtitle_regression_eval as evaluator
from src.autoslice import subtitle_regression_recovery as recovery
from tests.test_subtitle_regression_interrupted import (
    specimen as specimen, inspect, freeze, write,
)


def failed_child(s):
    child = s.task / "failed-preparation"
    receipt = recovery.stage_successor(inspect(s), child)
    code = s.task / "code" / receipt["code_identity"]
    write(code / "scripts/subtitle_regression_eval.py", {"synthetic_new_code_binding": True})
    old = json.loads(s.terminal.read_bytes())
    replacements = {str(s.parent): str(child), str(s.code): str(code), s.code.name: code.name,
                    str(s.code / "scripts/subtitle_regression_eval.py"):
                    str(code / "scripts/subtitle_regression_eval.py")}
    command = [replacements.get(x, x) for x in old["command"]]
    command += ["--resume-interrupted", str(s.parent), "--interruption-evidence", str(s.freeze),
                "--interruption-evidence-sha256", receipt["freeze_sha256"]]
    runs = {"example": {"reviews": [], "error": "ValueError: PREPARED_BOUNDARY_SOURCE_BINDING_INVALID",
                        "would_release": None}}
    results = [evaluator.summarize_case(c, runs[c["package"]]) for c in s.cases]
    evaluation = {"schema_version": "subtitle-regression-evaluation.v1", "code_identity": code.name,
        "code_root": str(code), "fixture_sha256": evaluator.digest(s.fixture),
        "results": results, "summary": evaluator.aggregate(results), "runs": runs}
    write(child / "evaluation.json", evaluation)
    terminal = child / "attempt-terminal.json"
    write(terminal, {**old, "status": "COMPLETED", "returncode": 1,
        "head": code.name, "cwd": str(code), "output": str(child), "command": command,
        "parent_output": str(s.parent), "parent_freeze_sha256": receipt["freeze_sha256"],
        "evaluation_sha256": evaluator.digest(child / "evaluation.json"), "package_runs": {},
        "started_utc": "2026-10-19T06:00:00+00:00", "ended_utc": "2026-10-19T06:01:00+00:00"})
    outer = s.task / "failed-preparation.terminal.json"
    write(outer, {"status": "COMPLETED", "returncode": 1, "wall_limit_reached": False,
        "head": code.name, "cwd": str(code), "output": str(child),
        "worker_terminal_path": str(terminal), "worker_terminal_sha256": evaluator.digest(terminal)})
    # A stale progress file is historical input, not a live process assertion.
    write(child / "experiment-progress.json", {"status": "RUNNING", "completed_packages": ["example"]})
    # The failed old materialization remains an unproven independent copy.
    (child / "example/normal-off-source.mp4").write_bytes(Path(s.package["media_path"]).read_bytes())
    return freeze(SimpleNamespace(**{**vars(s), "parent": child, "folder": child / "example",
        "code": code, "terminal": terminal, "outer": outer, "freeze": s.task / "failed-freeze.json"}))


def test_latest_completed_all_error_without_package_run_has_one_legal_successor(specimen):
    s = specimen
    latest = failed_child(s)
    before = recovery.inventory(latest.parent)
    original_terminal = s.terminal.read_bytes()
    record = inspect(latest)
    assert record["failed_evaluation"]["classification"] == "COMPLETED_EVALUATION_FAILURE"
    assert record["failed_evaluation"]["missing_package_runs"] == ["example"]
    assert record["packages"]["example"]["parent_package_state"] == "FAILED_BEFORE_PACKAGE_RUN"
    assert record["lineage"][-1]["parent_output"] == str(s.parent)
    old_claim = Path(record["lineage"][-1]["consumed_claim_path"])
    old_claim_bytes = old_claim.read_bytes()
    successor = s.task / "successor"
    copied = recovery.stage_successor(record, successor)
    assert copied["complete_predecessor_archive"] is True
    assert not (successor / "example/normal-off-source.mp4").exists()
    assert not (successor / "example/run.json").exists()
    assert not (successor / "evaluation.json").exists()
    for name, bound in record["files"].items():
        assert evaluator.digest(successor / copied["copied_to"][name]) == bound["sha256"]
    with pytest.raises(FileExistsError):
        recovery.stage_successor(record, s.task / "duplicate-new-output")
    with pytest.raises(FileExistsError):
        recovery.stage_successor(inspect(s), s.task / "fork-old-parent")
    assert recovery.inventory(latest.parent) == before
    assert s.terminal.read_bytes() == original_terminal
    assert old_claim.read_bytes() == old_claim_bytes
    paid = successor / "runtime-state/state/gemini-paid-backup"
    assert paid.resolve() == s.paid.resolve()
    assert record["packages"]["example"]["accounting"]["unknown_refunded"] is False


@pytest.mark.parametrize("damage", ["terminal_rc", "terminal_status", "evaluation_sha", "fake_run",
    "case_missing", "case_outcome", "case_identity", "error_missing", "release", "summary",
    "claim_output", "claim_missing", "parent_terminal", "inherited_bytes", "missing_lineage"])
def test_refrozen_latest_failure_drift_cannot_reopen_ancestry(specimen, damage):
    latest = failed_child(specimen)
    evaluation = latest.parent / "evaluation.json"
    terminal = json.loads(latest.terminal.read_bytes())
    report = json.loads(evaluation.read_bytes())
    if damage == "terminal_rc":
        terminal["returncode"] = 0
    elif damage == "terminal_status":
        terminal["status"] = "RUNNING"
    elif damage == "evaluation_sha":
        terminal["evaluation_sha256"] = "0" * 64
    elif damage == "fake_run":
        terminal["package_runs"] = {"example": "0" * 64}
    elif damage == "case_missing":
        report["results"] = []
    elif damage == "case_outcome":
        report["results"][0]["outcome"] = "PASS_REPAIRED"
    elif damage == "case_identity":
        report["results"][0]["candidate_id"] = "different"
    elif damage == "error_missing":
        report["runs"]["example"]["error"] = None
    elif damage == "release":
        report["runs"]["example"]["would_release"] = True
        report["results"][0]["would_release"] = True
    elif damage == "summary":
        report["summary"]["pass"] = True
    elif damage in {"claim_output", "claim_missing"}:
        receipt = json.loads((latest.parent / "interrupted-resume.json").read_bytes())
        claim = Path(receipt["claim_path"])
        if damage == "claim_missing":
            claim.unlink()
        else:
            content = json.loads(claim.read_bytes())
            content["output"] = str(latest.task / "wrong-child")
            write(claim, content)
    elif damage == "parent_terminal":
        value = json.loads(specimen.terminal.read_bytes())
        value["status"] = "COMPLETED"
        write(specimen.terminal, value)
    elif damage == "inherited_bytes":
        receipt = json.loads((latest.parent / "interrupted-resume.json").read_bytes())
        rel = "example/original.srt"
        (latest.parent / receipt["copied_to"][rel]).write_bytes(b"altered frozen input")
    else:
        (latest.parent / "interrupted-resume.json").unlink()
    if damage in {"case_missing", "case_outcome", "case_identity", "error_missing", "release", "summary"}:
        write(evaluation, report)
        terminal["evaluation_sha256"] = evaluator.digest(evaluation)
    write(latest.terminal, terminal)
    outer = json.loads(latest.outer.read_bytes())
    outer['worker_terminal_sha256'] = evaluator.digest(latest.terminal)
    write(latest.outer, outer)
    freeze(latest)
    with pytest.raises((ValueError, OSError)):
        inspect(latest)


def test_latest_failed_evaluation_does_not_relax_complete_resume(specimen):
    latest = failed_child(specimen)
    before = recovery.inventory(latest.parent)
    with pytest.raises((ValueError, OSError)):
        evaluator.prepare_experiment_resume(
            latest.parent, latest.fixture, latest.cases, "c" * 40, latest.runtime)
    assert recovery.inventory(latest.parent) == before


def test_frozen_full_archive_survives_active_metadata_evolution(specimen):
    s = specimen
    record = inspect(s)
    out = s.task / "child"
    child = recovery.stage_successor(record, out)
    old = (out / child["copied_to"]["example/authority.json"]).read_bytes()
    write(out / "example/authority.json", {"synthetic": "active evolution; no quality claim"})
    assert (out / child["copied_to"]["example/authority.json"]).read_bytes() == old
    assert (s.folder / "authority.json").read_bytes() == old


@pytest.mark.parametrize("damage", ["outer_missing", "outer_worker_hash", "outer_worker_path", "outer_code", "lineage_code", "duplicate_case"])
def test_outer_and_claim_execution_identity_survive_refreezing(specimen, damage):
    latest = failed_child(specimen)
    outer = json.loads(latest.outer.read_bytes())
    if damage == "outer_missing":
        latest.outer.unlink()
        del latest.outer
    elif damage == "outer_worker_hash":
        outer["worker_terminal_sha256"] = "f" * 64
        write(latest.outer, outer)
    elif damage == "outer_worker_path":
        outer["worker_terminal_path"] = str(latest.task / "other-terminal.json")
        write(latest.outer, outer)
    elif damage == "outer_code":
        outer["head"] = "c" * 40
        write(latest.outer, outer)
    elif damage == "lineage_code":
        path = latest.parent / "interrupted-resume.json"
        receipt = json.loads(path.read_bytes())
        receipt["code_identity"] = "c" * 40
        write(path, receipt)
        claim_path = Path(receipt["claim_path"])
        claim = json.loads(claim_path.read_bytes())
        claim["code_identity"] = "c" * 40
        write(claim_path, claim)
    else:
        path = latest.parent / "evaluation.json"
        report = json.loads(path.read_bytes())
        report["results"].append(report["results"][0])
        write(path, report)
        terminal = json.loads(latest.terminal.read_bytes())
        terminal["evaluation_sha256"] = evaluator.digest(path)
        write(latest.terminal, terminal)
        outer["worker_terminal_sha256"] = evaluator.digest(latest.terminal)
        write(latest.outer, outer)
    freeze(latest)
    with pytest.raises((ValueError, OSError)):
        inspect(latest)
