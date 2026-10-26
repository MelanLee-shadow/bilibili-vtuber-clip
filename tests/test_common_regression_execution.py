"""Shared ordinary CLI: explicit GWS revision vs immutable Argon invocation."""

from copy import deepcopy
import fcntl
import json
from pathlib import Path

import pytest

from scripts import subtitle_regression_eval as cli
from src.autoslice import argon_regression_comparison as comparison
from src.autoslice.argon_escalation_contract import canonical, sha
from tests.test_argon_regression_comparison import grant

from tests.test_argon_s2_joint_runtime import case, joint, offline, plan_args, scene  # noqa: F401


def files(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def ordinary_case(joint, plan_args, monkeypatch):
    from scripts import produce_slice_package as producer
    from src.autoslice import producer_text_pipeline as pipeline
    from tests.test_producer_text_pipeline_final_review import _correction_pass

    scene, speaker, _, _, _ = joint
    # Reuse the same actual media/current context. No precomputed identity or
    # fake intermediate gate result is supplied to the ordinary reviewer.
    doc = json.loads(plan_args.cases.read_bytes())
    package = doc["packages"]["sample"]
    context = Path(package["context_path"])
    context.write_bytes(canonical(scene.context))
    package["context_sha256"] = sha(context.read_bytes())
    correction = _correction_pass()
    record = Path(package["record_path"])
    row = json.loads(record.read_bytes())
    row["clip_context_payload_sha256"] = scene.context["context_sha256"]
    row["boundary_audit"] = {
        "boundary_semantic_review": {
            **correction["boundary_semantic_review"],
            "review_scope": "source_full_window",
        }
    }
    record.write_bytes(canonical(row))
    package["record_sha256"] = sha(record.read_bytes())
    authority = Path(package["authority_path"])
    authority.write_bytes(
        canonical(
            {"final_review_audit": {**correction, "correction_pass": correction}, "applied": []}
        )
    )
    package["authority_sha256"] = sha(authority.read_bytes())
    plan_args.cases.write_bytes(canonical(doc))
    monkeypatch.setattr(producer, "profile_asset_file", lambda _: speaker["profile"])
    monkeypatch.setattr(producer, "_topic_graph_disabled", lambda: True)
    monkeypatch.setattr(producer, "_review_glossary", lambda: "")
    llm = pipeline._build_final_review_llm_call()

    def answers(prompt):
        if "本次不授权改字、删字或听音调度" in prompt:
            agenda = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps(
                {
                    "findings": [
                        {
                            "cue": r["cue"],
                            "content_risk": "meaning_reversal",
                            "risk_basis": "synthetic unresolved review, not a release",
                        }
                        for r in agenda
                    ]
                }
            )
        return llm(prompt)

    answers.cpa_cache_identity = llm.cpa_cache_identity
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: answers)
    loaded = []
    monkeypatch.setattr(
        cli,
        "load_production_gemini_environment",
        lambda: (
            loaded.append(True)
            or {
                "source": "synthetic-loader-only",
                "configured_key_count": 0,
                "paid_backup_configured": False,
            }
        ),
    )
    runtime = scene.root / "ordinary-runtime"
    paid = runtime / "state/gemini-paid-backup"
    paid.mkdir(parents=True)
    (paid / "usage.json").write_bytes(b'{"synthetic_paid_history":7}\n')
    output = scene.root / "ordinary-result"
    argv = [
        "--worker",
        "--code-root",
        str(cli.ROOT),
        "--code-identity",
        "synthetic-old-code",
        "--runtime-root",
        str(runtime),
        "--cases",
        str(plan_args.cases),
        "--case",
        "target",
        "--output",
        str(output),
    ]
    return scene, plan_args, output, runtime, paid, argv, loaded


def test_gws_real_cli_new_code_resume_preserves_unknown_paid_and_current_text(
    ordinary_case, monkeypatch
):
    from src.autoslice import llm_client
    from src.autoslice.cpa_runtime import bind_observation

    scene, args, output, runtime, paid, argv, loaded = ordinary_case
    assert cli.main(argv) == 1
    first = json.loads((output / "sample/run.json").read_bytes())
    assert first["reviews"] and first["would_release"] is False
    current = (output / "sample/final.srt").read_bytes()
    paid_before = (paid / "usage.json").read_bytes()

    # Produce a genuine UNKNOWN capture at the existing low-level dispatch
    # boundary, instead of inventing a successful model or an authority stamp.
    def interrupted(*a):
        raise TimeoutError("synthetic unknown dispatch")

    monkeypatch.setattr(llm_client, "_call_direct", interrupted)
    call = llm_client.build_llm_call(
        llm_client.LlmConfig(transport="direct", model="synthetic-only", runtime_root=str(runtime))
    )
    with (
        bind_observation(output / "sample/provider-usage.jsonl", "synthetic-unknown"),
        pytest.raises(TimeoutError),
    ):
        call("synthetic prior operation with unknown response")
    captured = next((output / "sample/cpa-text-exchanges").glob("*/terminal.json"))
    unknown = captured.read_bytes()
    assert json.loads(unknown)["status"] == "OUTCOME_UNKNOWN"
    original_evaluation = (output / "evaluation.json").read_bytes()
    resume = argv + ["--resume-existing"]
    resume[resume.index("--code-identity") + 1] = "synthetic-new-code"
    # Real worker lock must reject before even the injected loader is called.
    count = len(loaded)
    with (output.parent / ("." + output.name + ".worker.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cli.main(resume)
    assert len(loaded) == count
    assert cli.main(resume) == 1
    final = json.loads((output / "evaluation.json").read_bytes())
    archive = Path(final["resume"]["archive"])
    assert (archive / "evaluation.json").read_bytes() == original_evaluation
    assert (archive / captured.relative_to(output)).read_bytes() == unknown == captured.read_bytes()
    assert (paid / "usage.json").read_bytes() == paid_before
    assert (output / "runtime-state/state/gemini-paid-backup").resolve() == paid.resolve()
    assert (output / "sample/final.srt").read_bytes() == current
    assert final["resume"]["parent_code_identity"] == "synthetic-old-code"
    assert final["code_identity"] == "synthetic-new-code"
    assert json.loads((output / "resume-status.json").read_bytes())["status"] == "COMPLETED"
    assert not scene.provider.calls
    (scene.root / "COMMON-CLI-RESULT.json").write_bytes(
        canonical(
            {
                "normal_gate_unmocked": True,
                "unknown_path": str(captured),
                "unknown_sha256": sha(unknown),
                "archive": str(archive),
                "parent_code": "synthetic-old-code",
                "new_code": "synthetic-new-code",
                "current_srt_sha256": sha(current),
                "real_provider_calls": 0,
            }
        )
    )


@pytest.mark.parametrize("mode", ["comparison-marker", "running-ordinary"])
def test_ordinary_worker_cannot_reenter_foreign_or_unknown_output(ordinary_case, mode):
    scene, args, output, runtime, paid, argv, loaded = ordinary_case
    assert cli.main(argv) == 1
    if mode == "comparison-marker":
        (output / "invocation.json").write_bytes(b'{"synthetic_foreign_identity":true}')
    else:
        (output / "resume-status.json").write_bytes(b'{"status":"RUNNING"}')
    before = files(output)
    resume = argv + ["--resume-existing"]
    resume[resume.index("--code-identity") + 1] = "synthetic-revised-code"
    with pytest.raises(ValueError):
        cli.main(resume)
    assert files(output) == before
    assert not (output.parent / "resume-history").exists()
    assert not scene.provider.calls


def test_comparison_cannot_adopt_ordinary_output_without_original_invocation(scene, plan_args):
    comparison.prepare(plan_args)
    _, auth = grant(plan_args)
    args = deepcopy(plan_args)
    args.comparison_plan = args.output / "plan.json"
    args.comparison_grant = auth
    args.resume_comparison = True
    args.output = scene.root / "foreign-output"
    args.output.mkdir(mode=0o700)
    (args.output / "evaluation.json").write_bytes(b'{"synthetic":"ordinary terminal"}')
    before = files(args.output)
    with pytest.raises(ValueError, match="COMPARISON_RESUME_IDENTITY_REQUIRED"):
        comparison.execute(args)
    assert files(args.output) == before and not scene.provider.calls


@pytest.mark.parametrize(
    "args",
    [
        ["--worker", "--resume-existing", "--comparison-plan", "not-opened"],
        ["--worker", "--comparison-plan", "not-opened"],
        ["--rescore", "not-opened", "--comparison-plan", "not-opened"],
    ],
)
def test_mixed_cli_modes_rejected_before_any_input_read(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
