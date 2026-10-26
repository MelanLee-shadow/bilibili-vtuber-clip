"""The public preflight and preparation failures use the actual package entry."""
import json
import signal
from types import SimpleNamespace

import pytest

from scripts import subtitle_regression_eval as evaluator
from src.autoslice import subtitle_regression_recovery as recovery
from src.autoslice import producer_text_pipeline as pipeline
from tests.test_subtitle_regression_interrupted import specimen as specimen, inspect


def args_for(s, output, **changes):
    values = dict(output=output, code_root=evaluator.ROOT, code_identity="b" * 40,
        runtime_root=str(s.runtime), cases=s.fixture, case=[s.cases[0]["id"]], normal_off=True,
        resume_existing=False, resume_interrupted=s.parent, interruption_evidence=s.freeze,
        interruption_evidence_sha256=evaluator.digest(s.freeze), recovery_preflight=True,
        maximum_wall_seconds=60, recovery_preflight_replay=False)
    return SimpleNamespace(**{**values, **changes})


def test_public_preflight_constructs_actual_materialized_consumer_and_cold_replays(specimen, monkeypatch):
    s = specimen
    def forbidden(*a, **kw):
        pytest.fail("preflight must not load credentials or call a provider")
    monkeypatch.setattr(evaluator, "load_production_gemini_environment", forbidden)
    original = pipeline.build_prepared_exact_final_reviewer
    calls = []
    def observed(**kw):
        calls.append(str(kw["padded"]))
        return original(**kw)
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", observed)
    before = recovery.inventory(s.parent)
    out = s.task / "actual-preflight"
    assert evaluator.worker(args_for(s, out)) == 0
    media = out / "example/normal-off-source.mp4"
    assert calls == [str(media)]
    proof = media.with_suffix(".provenance.json").read_bytes()
    info = media.stat()
    run = json.loads((out / "example/run.json").read_bytes())
    assert run["phase"] == "PACKAGE_PREPARED_NO_PROVIDER"
    assert run["would_release"] is None and run["quality_outcome"] == "NOT_EVALUATED"
    assert run["prepared_source_boundary_clock"]["media_path"] == str(media)
    assert run["provider_usage"]["new_provider_calls"] == 0
    assert run["provider_usage"]["cost_usd"] is None
    assert not (out / "evaluation.json").exists()
    assert not (s.task / "interrupted-recovery-claims").exists()
    assert evaluator.worker(args_for(s, out, recovery_preflight_replay=True)) == 0
    assert calls == [str(media), str(media)]
    assert media.stat().st_ino == info.st_ino and media.stat().st_ctime_ns == info.st_ctime_ns
    assert media.with_suffix(".provenance.json").read_bytes() == proof
    assert recovery.inventory(s.parent) == before
    cold = json.loads((out / "preflight-replay-0001.json").read_bytes())
    assert cold["cold_replay"] and cold["provider_dispatch_audit"]["blocked_dispatches"] == []


@pytest.mark.parametrize("where", ["materialization", "constructor", "constructor_interrupt", "input_hash"])
def test_preparation_failure_has_truthful_run_and_stage_chain(specimen, monkeypatch, where):
    s = specimen
    out = s.task / "child"
    record = recovery.stage_successor(inspect(s), out)
    folder = out / "example"
    kwargs = dict(normal_samples=record["packages"]["example"]["normal_samples"],
                  recovery=record["packages"]["example"],
                  checkpoint_identity={"code_identity": "b" * 40})
    if where == "input_hash":
        package = {**s.package, "media_sha256": "0" * 64}
    else:
        package = s.package
        def broken(*a, **kw):
            if where == "constructor_interrupt":
                raise recovery.EvaluationInterrupted(signal.SIGTERM)
            raise ValueError("SYNTHETIC_" + where.upper() + "_FAILURE")
        if where == "materialization":
            monkeypatch.setattr(evaluator, "stage_normal_source_media", broken)
        else:
            monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", broken)
    if where == "constructor_interrupt":
        with pytest.raises(recovery.EvaluationInterrupted):
            evaluator.run_package(package, folder, evaluator.ROOT, {}, **kwargs)
    else:
        run = evaluator.run_package(package, folder, evaluator.ROOT, {}, **kwargs)
        assert run["error"] and run["lifecycle"] == "FAILED"
    saved = json.loads((folder / "run.json").read_bytes())
    assert saved["error"] and saved["would_release"] is None
    assert saved["quality_outcome"] == "EVALUATION_ERROR"
    assert saved["provider_usage"]["new_provider_calls"] == 0
    assert saved["provider_usage"]["cost_usd"] is None
    stages = recovery.validate_stage_history(folder, (folder / "final.srt").read_bytes(),
                                             json.loads((folder / "authority.json").read_bytes()))
    assert stages["stages"] >= 2
    assert stages["last_phase"] == ("PACKAGE_PREPARATION_INTERRUPTED"
                                    if where == "constructor_interrupt" else "PACKAGE_PREPARATION_FAILED")


def test_missing_preparation_inputs_are_explicit_not_successful_blobs(tmp_path):
    out = tmp_path / "package"
    out.mkdir()
    path = recovery.package_checkpoint(out, phase="PACKAGE_PREPARATION_STARTED",
        text=None, authority=None, run={"lifecycle": "PREPARING", "would_release": None})
    record = json.loads(path.read_bytes())
    assert record["input_presence"] == {"text": False, "authority": False}
    assert record["text_sha256"] is None and record["authority_sha256"] is None
    assert not (path.parent / "blobs").exists()
    assert not recovery.validate_stage_history(out, b"", {})["current_matches_last_stage"]


def test_actual_preflight_provider_dispatch_is_blocked_before_execution(specimen, monkeypatch):
    import socket
    s = specimen
    def invalid_constructor(**kw):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
        pytest.fail("network dispatch must not return")
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", invalid_constructor)
    out = s.task / "forbidden-preflight"
    assert evaluator.worker(args_for(s, out)) == 1
    saved = json.loads((out / "preflight.json").read_bytes())
    assert saved["provider_calls"] == 0
    assert saved["provider_dispatch_audit"]["blocked_dispatches"]
    assert "PREFLIGHT_PROVIDER_DISPATCH_FORBIDDEN" in saved["preparation_runs"]["example"]["error"]
    assert json.loads((out / "experiment-progress.json").read_bytes())["status"] == "COMPLETED"
    assert not (out / "evaluation.json").exists()


@pytest.mark.parametrize("bad,state", [(b"{broken", "INVALID_JSON"), (b"[]", "INVALID_TYPE")])
def test_invalid_staged_authority_is_preserved_before_failure(specimen, monkeypatch, bad, state):
    s = specimen
    out = s.task / "bad-authority-child"
    record = recovery.stage_successor(inspect(s), out)
    folder = out / "example"
    authority_path = folder / "authority.json"
    authority_path.write_bytes(bad)
    def forbidden(**kw):
        pytest.fail("invalid authority cannot reach constructor")
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", forbidden)
    run = evaluator.run_package(s.package, folder, evaluator.ROOT, {},
        normal_samples=record["packages"]["example"]["normal_samples"],
        recovery=record["packages"]["example"], checkpoint_identity={"code_identity": "b" * 40})
    assert run["lifecycle"] == "FAILED" and run["would_release"] is None
    assert run["provider_usage"]["new_provider_calls"] == 0
    assert "PACKAGE_CHECKPOINT_AUTHORITY_" + state in run["error"]
    stages = sorted((folder / "regression-checkpoints").glob("stage-*.json"))
    assert len(stages) == 2
    last = json.loads(stages[-1].read_bytes())
    invalid = last["authority_input"]
    assert invalid["state"] == state and last["input_presence"]["authority"] is False
    blob = stages[-1].parent / "blobs" / (invalid["raw_sha256"] + ".authority.raw")
    assert blob.read_bytes() == bad == authority_path.read_bytes()
    assert invalid["source_snapshot"]["stat"]["st_ino"] == authority_path.stat().st_ino
    history = recovery.validate_stage_history(folder, (folder / "final.srt").read_bytes(), {})
    assert history["last_phase"] == "PACKAGE_PREPARATION_FAILED"
    assert not history["current_matches_last_stage"]
    blob.unlink()
    with pytest.raises((OSError, ValueError)):
        recovery.validate_stage_history(folder, (folder / "final.srt").read_bytes(), {})


def test_constructor_error_survives_secondary_invalid_authority(specimen, monkeypatch):
    s = specimen
    out = s.task / "secondary-error-child"
    record = recovery.stage_successor(inspect(s), out)
    folder = out / "example"
    def broken(**kw):
        (folder / "authority.json").write_bytes(b"{actual failed write")
        raise ValueError("ORIGINAL_CONSTRUCTOR_FAILURE")
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", broken)
    run = evaluator.run_package(s.package, folder, evaluator.ROOT, {},
        normal_samples=record["packages"]["example"]["normal_samples"],
        recovery=record["packages"]["example"], checkpoint_identity={"code_identity": "b" * 40})
    assert run["error"] == "ValueError: ORIGINAL_CONSTRUCTOR_FAILURE"
    assert run["failed_phase"] == "PREPARED_CONSTRUCTOR_STARTED"
    assert run["checkpoint_input_errors"][-1]["state"] == "INVALID_JSON"
    assert run["phase"] == "PACKAGE_PREPARATION_FAILED" and run["would_release"] is None


def test_public_preflight_interruption_persists_actual_package_and_terminal(specimen, monkeypatch):
    s = specimen
    def interrupted(**kw):
        raise recovery.EvaluationInterrupted(signal.SIGTERM)
    monkeypatch.setattr(pipeline, "build_prepared_exact_final_reviewer", interrupted)
    out = s.task / "interrupted-preflight"
    with pytest.raises(recovery.EvaluationInterrupted):
        evaluator.worker(args_for(s, out))
    terminal = json.loads((out / "preflight.json").read_bytes())
    progress = json.loads((out / "experiment-progress.json").read_bytes())
    assert terminal["status"] == progress["status"] == "INTERRUPTED"
    assert terminal["returncode"] == -signal.SIGTERM
    assert terminal["returned_packages"] == [] and progress["processed_packages"] == []
    assert terminal["preparation_runs"]["example"]["phase"] == "PACKAGE_PREPARATION_INTERRUPTED"
    assert progress["remaining_packages"] == ["example"]
    assert progress["not_started_packages"] == []
    assert terminal["provider_calls"] == 0 and terminal["would_release"] is None
    with pytest.raises(ValueError, match="preflight replay identity drift"):
        evaluator.worker(args_for(s, out, recovery_preflight_replay=True))
