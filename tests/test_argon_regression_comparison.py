"""Normal regression CLI: immutable preparation, cost guard, and persistent opt-in."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from scripts import subtitle_regression_eval as evaluator
from src.autoslice import argon_regression_comparison as comparison
from src.autoslice.argon_escalation_contract import canonical, sha

# These fixtures already exercise the actual ordinary producer factory, native
# manifest, public context adjudicator, raw response and normal typed consumers.
pytest_plugins = ["tests.test_argon_consumer_integration"]


@pytest.fixture
def plan_args(scene):
    root = scene.root
    context = deepcopy(scene.context)
    context.update(recording_date="2026-10-16", schema_version="fixture-context")
    package = {"candidate_id": "synthetic-consumer", "intro_offset_ms": 1000}
    docs = {
        "srt": scene.srt,
        "context": json.dumps(context),
        "record": json.dumps(
            {
                "subtitle_audio_correspondence": {"intro_offset_ms": 1000},
                "clip_context_payload_sha256": context["context_sha256"],
                "start_ms": 0,
                "end_ms": 6000,
                "subtitle_path": str(root / "srt.srt"),
            }
        ),
        "authority": "{}",
        "provenance": "{}",
    }
    for kind, data in docs.items():
        path = root / (kind + (".srt" if kind == "srt" else ".json"))
        path.write_text(data)
        package[kind + "_path"] = str(path)
        package[kind + "_sha256"] = sha(path.read_bytes())
    package.update(media_path=str(scene.source), media_sha256=sha(scene.source.read_bytes()))
    case = {
        "id": "target",
        "package": "sample",
        "candidate_id": "synthetic-consumer",
        "status": "ready",
        "cue_ids": [1],
        "body_window_ms": [1000, 2000],
        "finished_window_ms": [2000, 3000],
        "wrong_text": "一百五十秒",
        "source": {"quote": "scorer-only-human-quotation"},
        "expected": {"correct_text": "scorer-only-correct-answer", "required_severity": "critical"},
    }
    doc = {
        "schema_version": "subtitle-regressions.v1",
        "baseline_commit": "synthetic-baseline",
        "clock_rule": "explicit",
        "packages": {"sample": package},
        "cases": [case],
    }
    cases = root / "cases.json"
    cases.write_bytes(canonical(doc))
    baseline = root / "baseline"
    (baseline / "sample").mkdir(parents=True)
    run = {
        "candidate_id": "synthetic-consumer",
        "would_release": False,
        "error": None,
        "input_drift": [],
        "input_hashes": {
            package[k + "_path"]: package[k + "_sha256"]
            for k in ("record", "srt", "authority", "media")
        },
        "reviews": [
            {
                "discovery": {"status": "COMPLETE"},
                "evaluation_input_cues": [
                    {"index": 1, "start_ms": 1000, "end_ms": 2000, "text": "一百五十秒"}
                ],
                "findings": [{"cue_index": 1, "content_risk": "meaning_reversal"}],
            }
        ],
        "final_cues": [{"index": 1, "start_ms": 1000, "end_ms": 2000, "text": "一百五十秒"}],
    }
    (baseline / "sample/run.json").write_bytes(canonical(run))
    (baseline / "sample/original.srt").write_text(scene.srt)
    return SimpleNamespace(
        cases=cases,
        case=["target"],
        prepare_argon_comparison=baseline,
        baseline_commit="synthetic-baseline",
        code_identity="synthetic-current",
        commit=None,
        output=root / "frozen",
        code_root=None,
        runtime_root=str(root / "runtime"),
        resume_comparison=False,
        comparison_plan=None,
        comparison_grant=None,
    )


def grant(plan_args, case_ids=None):
    plan = comparison.read_plan(plan_args.output / "plan.json")
    payload = {
        "schema_version": comparison.GRANT_SCHEMA,
        "status": "AUTHORIZED",
        "plan_sha256": plan["plan_sha256"],
        "authorized_case_ids": case_ids or ["target"],
        "max_usd": str(comparison.PER_REQUEST_USD),
        "existing_subscription_cpa_only": True,
        "ordinary_new_audio_calls": 0,
        "authority": "SYNTHETIC_TEST_ONLY_NOT_USER_AUTHORIZATION",
    }
    path = plan_args.output / "fictional-grant.json"
    path.write_bytes(canonical(payload))
    return plan, path


def test_cli_prepares_real_audio_and_separates_scoring_answers(scene, plan_args):
    args = plan_args
    original = (args.prepare_argon_comparison / "sample/run.json").read_bytes()
    assert (
        evaluator.main(
            [
                "--prepare-argon-comparison",
                str(args.prepare_argon_comparison),
                "--cases",
                str(args.cases),
                "--case",
                "target",
                "--code-identity",
                args.code_identity,
                "--output",
                str(args.output),
            ]
        )
        == 0
    )
    plan = comparison.read_plan(args.output / "plan.json")
    assert plan["new_cost_authorized_usd"] == "0"
    assert plan["samples"][0]["trigger_status"] == "NOT_ESTABLISHED_BY_PREPARATION"
    assert plan["samples"][0]["context_source_ms"] == [500, 4500]
    assert plan["total_submitted_audio_duration_ms"] == 4000
    for p in [args.output / "plan.json", args.output / "samples/target/request-preview.json"]:
        assert b"scorer-only" not in p.read_bytes()
    assert b"scorer-only" in (args.output / "scoring-only.json").read_bytes()
    assert (args.prepare_argon_comparison / "sample/run.json").read_bytes() == original
    assert scene.loader_calls == 0 and not scene.provider.calls and not scene.prompts
    with pytest.raises(ValueError, match="OUTPUT_EXISTS"):
        comparison.prepare(args)


def test_missing_baseline_is_waiting_not_failed_or_forced_trigger(scene, plan_args):
    (plan_args.prepare_argon_comparison / "sample/run.json").unlink()
    comparison.prepare(plan_args)
    plan = comparison.read_plan(plan_args.output / "plan.json")
    assert plan["baseline"]["sample"]["status"] == "WAITING_BASELINE_TERMINAL"
    assert (
        json.loads((plan_args.output / "baseline-score.json").read_bytes())["sample"]["scoring"]
        is None
    )
    _, path = grant(plan_args)
    args = deepcopy(plan_args)
    args.comparison_plan = args.output / "plan.json"
    args.comparison_grant = path
    args.output = scene.root / "run"
    with pytest.raises(ValueError, match="BASELINE_NOT_COMPLETE"):
        comparison.execute(args)
    assert not args.output.exists() and not scene.provider.calls


def test_zero_dollar_plan_refuses_cli_before_loaders_or_runtime_writes(scene, plan_args):
    comparison.prepare(plan_args)
    with pytest.raises(ValueError, match="COST_NOT_AUTHORIZED"):
        evaluator.main(
            [
                "--comparison-plan",
                str(plan_args.output / "plan.json"),
                "--comparison-grant",
                str(plan_args.output / "grant-required.json"),
                "--output",
                str(scene.root / "must-not-exist"),
            ]
        )
    assert not (scene.root / "must-not-exist").exists()
    assert scene.loader_calls == 0 and not scene.provider.calls


@pytest.mark.parametrize("drift", ["source", "target_file", "cap", "model_plan"])
def test_frozen_plan_or_grant_drift_cannot_submit(scene, plan_args, drift):
    comparison.prepare(plan_args)
    plan, path = grant(plan_args)
    if drift == "source":
        scene.source.write_bytes(scene.source.read_bytes() + b"drift")
    elif drift == "target_file":
        (plan_args.output / "samples/target/geometry.json").write_text("{}")
    elif drift == "cap":
        value = json.loads(path.read_bytes())
        value["max_usd"] = "0"
        path.write_bytes(canonical(value))
    else:
        value = json.loads(path.read_bytes())
        value["plan_sha256"] = "f" * 64
        path.write_bytes(canonical(value))
    with pytest.raises(ValueError):
        comparison.authorize(plan_args.output / "plan.json", path)
    assert scene.loader_calls == 0 and not scene.provider.calls


@pytest.mark.parametrize("label", ["Gemini 3.8 Flash (High)", "Gemini 3.8 Flash (Low)"])
def test_actual_agy_display_identity_is_consumed_without_relabelling(scene, label):
    scene.ordinary_model = label
    output, pending = scene.run()
    assert output == scene.srt and pending["argon_escalation"]["status"] == "PENDING", pending
    output, done = scene.run(scene.build())
    assert done["repaired"], done
    roots = list((scene.root / "runtime/cache/argon-consumer").glob("*/ordinary-history/*.json"))
    assert roots
    for root in roots:
        event = json.loads(root.read_bytes())
        raw = root.parent.parent / "blobs" / (event["ordinary"]["artifacts"]["manifest"] + ".bin")
        assert json.loads(raw.read_bytes())["model"] == label


def test_cache_only_comparison_never_turns_missing_audio_into_new_provider(scene):
    class Ordinary:
        def __call__(self, request):
            raise AssertionError("no new model")

    wrapper = comparison.CachedOrdinaryAudio(Ordinary())
    request = {"request_sha256": "a" * 64}
    assert wrapper(request)["reason_code"] == "COMPARISON_ORDINARY_CACHE_MISS"
    assert wrapper.observe_independent_witnesses(request) == []
    assert not hasattr(wrapper, "observe_range_context")
    assert not scene.provider.calls


def test_comparison_budget_reserves_unknown_once_on_normal_public_entry(scene, plan_args):
    from src.autoslice.argon_escalation_integration import build_argon_integration

    comparison.prepare(plan_args)
    plan, path = grant(plan_args)
    # Match the public entry's real +1000 source offset, not the fixture scorer's
    # body clock. Reprepare this synthetic plan explicitly as a separate fixture.
    sample = plan["samples"][0]
    sample["target_source_ms"] = [2000, 3000]
    sample["context_source_ms"] = [1500, 5500]
    from src.autoslice.argon_escalation_contract import AudioSpec, Finding, prepare_audio

    _, audio = prepare_audio(
        AudioSpec(
            scene.source,
            Finding(sample["source_media_sha256"], (2000, 3000), "synthetic-target"),
            (1500, 5500),
        )
    )
    sample["input_audio_sha256"] = sha(audio)
    plan.pop("plan_sha256")
    plan["plan_sha256"] = sha(canonical(plan))
    pp = plan_args.output / "shifted-test-plan.json"
    pp.write_bytes(canonical(plan))
    g = json.loads(path.read_bytes())
    g["plan_sha256"] = plan["plan_sha256"]
    path.write_bytes(canonical(g))
    scene.verifier.argon_escalation = build_argon_integration(
        config={"enabled": True, "comparison_plan": str(pp), "comparison_grant": str(path)},
        source_media=scene.source,
        source_duration_ms=6000,
        ordinary_output_dir=scene.out,
        runtime_root=scene.root / "runtime",
        credential_loader=lambda: scene.credentials,
        transport_factory=scene.provider.factory,
    )
    scene.provider.post_error = TimeoutError("synthetic submission unknown")
    _, one = scene.run()
    assert one["argon_escalation"]["status"] == "SUBMIT_UNKNOWN", one
    _, two = scene.run()
    assert two["repaired"], two
    assert len([c for c in scene.provider.calls if c["method"] == "POST"]) == 1
    reservations = list((plan_args.output / "cost-reservations").glob("*.json"))
    assert len(reservations) == 1
    assert json.loads(reservations[0].read_bytes())["actual_cost_usd"] is None


def test_existing_regression_run_package_passes_opt_in_and_retains_pending_across_resume(
    scene, plan_args, monkeypatch
):
    """The actual run_package -> prepared factory -> public judge chain executes.

    Only the outer whole-package gate is a finite synthetic harness here: its
    supplied prepared reviewer still calls the real producer entity factory and
    public context adjudicator, native FakeProvider and actual CPA prompt code.
    """
    from src.autoslice import (
        producer_text_pipeline as pipeline,
        producer_package_finalization as finalizer,
    )
    from src.autoslice import clip_context as clip_module
    from src.autoslice.final_review_auditor import adjudicate_context_finding
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs

    doc = json.loads(plan_args.cases.read_bytes())
    package = doc["packages"]["sample"]
    # A completed ordinary hearing is retained before the new arm. It is not
    # manufactured by a trigger status and never becomes another paid request.
    scene.ordinary_status = "OBSERVED"
    scene.diagnosis = None
    adjudicate_context_finding(
        scene.srt,
        scene.finding,
        entity_verifier=scene.build(enabled=False),
        clip_context=scene.context,
        source_media_timeline_offset_ms=0,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    scene.diagnosis = "reading_chat"
    compare_out = scene.root / "comparison-run"
    captured = []
    monkeypatch.setattr(clip_module, "validate_clip_context", lambda *a, **k: None)

    def callback(**kwargs):
        captured.append(kwargs)

        def review(text, authority, start, end):
            output, audit = adjudicate_context_finding(
                text,
                scene.finding,
                entity_verifier=kwargs["verify_confusable_entity"],
                clip_context=scene.context,
                source_media_timeline_offset_ms=0,
                judge_llm_call=scene.judge,
                require_complete_utterance_support=True,
            )
            return {
                "discovery": {"status": "COMPLETE"},
                "release_gate": "BLOCK",
                "status": "FLAGGED",
                "findings": [
                    {
                        **scene.finding,
                        "base_text_sha256": sha("一百五十秒".encode()),
                        "context_audio_adjudication": audit,
                    }
                ],
                "applied_count": int(audit["repaired"]),
            }

        return review

    monkeypatch.setattr(pipeline, "_build_exact_final_review_callback", callback)
    monkeypatch.setattr(pipeline, "_continuation_chat_from_context", lambda _: [])
    record_path = Path(package["record_path"])
    record = json.loads(record_path.read_bytes())
    record["boundary_audit"] = {"boundary_semantic_review": {}}
    record_path.write_bytes(canonical(record))
    package["record_sha256"] = sha(record_path.read_bytes())
    authority_path = Path(package["authority_path"])
    authority_path.write_bytes(canonical({"final_review_audit": {}}))
    package["authority_sha256"] = sha(authority_path.read_bytes())

    def gate(**kwargs):
        srt_path = kwargs["recut"].subtitle_path
        review = kwargs["run_exact_final_review"](
            srt_path.read_text(), kwargs["chat_authority_audit"], 0, 6000
        )
        changed, repairs = _apply_exact_final_cpa_repairs(
            srt_path.read_text(),
            review,
            clip_context=scene.context,
            adjudication_stage="context_audio_adjudication",
        )
        if repairs:
            srt_path.write_text(changed)
        return {"release_gate": "BLOCK"}

    monkeypatch.setattr(finalizer, "_run_exact_final_review_gate", gate)
    doc["packages"]["sample"] = package
    plan_args.cases.write_bytes(canonical(doc))
    previous = plan_args.prepare_argon_comparison / "sample/run.json"
    baseline = json.loads(previous.read_bytes())
    baseline["input_hashes"] = {
        package[k + "_path"]: package[k + "_sha256"]
        for k in ("record", "srt", "authority", "media")
    }
    previous.write_bytes(canonical(baseline))
    comparison.prepare(plan_args)
    _, grant_path = grant(plan_args)
    config = {
        "enabled": True,
        "comparison_plan": str(plan_args.output / "plan.json"),
        "comparison_grant": str(grant_path),
    }
    first = evaluator.run_package(
        package, compare_out, Path.cwd(), {}, comparison_config=config, witness_cache_root=scene.out
    )
    assert first["reviews"], first
    assert captured[0]["spec"]["comparison_ordinary_cache_only"] is True
    assert captured[0]["screen_read_probe"] is None
    assert (
        first["reviews"][0]["findings"][0]["context_audio_adjudication"]["argon_escalation"][
            "status"
        ]
        == "PENDING"
    ), first
    before = (compare_out / "review-01.json").read_bytes()
    old_run = (compare_out / "run.json").read_bytes()
    old_inputs = {name: (compare_out / name).read_bytes()
                  for name in ("run.json", "final.srt", "authority.json")}
    preparing = []
    write_json = evaluator.write_json
    def capture_preparing(path, value):
        write_json(path, value)
        if path == compare_out / "run.json" and value.get("phase") == "PACKAGE_PREPARATION_STARTED":
            preparing.append(path.read_bytes())
    monkeypatch.setattr(evaluator, "write_json", capture_preparing)
    count = scene.ordinary_calls
    second = evaluator.run_package(
        package,
        compare_out,
        Path.cwd(),
        {},
        comparison_config=config,
        witness_cache_root=scene.out,
        resume=True,
    )
    assert second["final_cues"][0]["text"] == "一百五十首", second
    assert (compare_out / "review-01.json").read_bytes() == before
    assert second["parent_run_sha256"] == sha(old_run)
    assert preparing and second["parent_run_sha256"] != sha(preparing[0])
    assert second["resume_parent_run"]["validation"] == "VALIDATED"
    assert Path(second["resume_parent_run"]["archive_path"]).read_bytes() == old_run
    for name, raw in old_inputs.items():
        assert (compare_out / ("run-history-001-" + name)).read_bytes() == raw
    assert scene.ordinary_calls == count
    assert [x["method"] for x in scene.provider.calls] == ["POST", "GET"]
    assert first["provider_usage"]["argon_native"]["http_journal_entries"] == 1
    assert second["provider_usage"]["argon_native"]["http_journal_entries"] == 2
    assert second["provider_usage"]["argon_native"]["inference_reservations"] == 1
    assert second["provider_usage"]["argon_native"]["actual_charge_usd"] is None
    assert "## 新增 Argon 独立盲听证据" in scene.prompts[-1]
    assert all("scorer-only" not in prompt for prompt in scene.prompts)


def test_same_grant_does_not_reset_by_new_output_or_copied_plan(scene, plan_args):
    from src.autoslice.argon_escalation_contract import (
        AudioSpec,
        Finding,
        prepare_audio,
        make_inner_request,
    )

    comparison.prepare(plan_args)
    plan, path = grant(plan_args)
    spec = AudioSpec(
        scene.source,
        Finding(sha(scene.source.read_bytes()), (1000, 2000), "synthetic-one"),
        (500, 4500),
    )
    binding, audio = prepare_audio(spec)
    inner = make_inner_request(binding, audio)
    budget = comparison.SubmissionBudget(plan_args.output / "plan.json", path)
    budget.reserve(spec, "task-one", inner)
    copied = scene.root / "new-output-plan.json"
    copied.write_bytes((plan_args.output / "plan.json").read_bytes())
    other = comparison.SubmissionBudget(copied, path)
    assert other.root == budget.root
    with pytest.raises(ValueError, match="ALREADY_RESERVED"):
        other.reserve(spec, "different-task", inner)
    assert not scene.provider.calls


def test_cli_execute_and_resume_keep_same_state_and_leave_unknown_iteration_closed(
    scene, plan_args, monkeypatch
):
    comparison.prepare(plan_args)
    plan, path = grant(plan_args)
    args = deepcopy(plan_args)
    args.comparison_plan = args.output / "plan.json"
    args.comparison_grant = path
    args.output = scene.root / "execution"
    calls = []

    def run(package, out, code, environment, **options):
        calls.append(options)
        assert package == plan["packages"]["sample"]
        assert options["comparison_config"]["allow_paid"] is False
        assert options["comparison_config"]["allow_realtime_fallback"] is False
        out.mkdir(exist_ok=options["resume"])
        return json.loads((plan_args.prepare_argon_comparison / "sample/run.json").read_bytes())

    monkeypatch.setattr(evaluator, "run_package", run)
    assert comparison.execute(args) == 1
    assert not calls[0]["resume"]
    args.resume_comparison = True
    assert comparison.execute(args) == 1 and calls[1]["resume"]
    state = json.loads((args.output / "invocation.json").read_bytes())
    (args.output / "invocations/interrupted.started.json").write_bytes(canonical(state))
    with pytest.raises(ValueError, match="PRIOR_INVOCATION_UNKNOWN"):
        comparison.execute(args)
    assert len(calls) == 2
    assert not scene.provider.calls


def test_direct_regression_entry_cannot_bypass_zero_cost_gate(scene, plan_args):
    package = json.loads(plan_args.cases.read_bytes())["packages"]["sample"]
    with pytest.raises(ValueError, match="COST_NOT_AUTHORIZED"):
        evaluator.run_package(
            package, scene.root / "forbidden", Path.cwd(), {}, comparison_config={"enabled": True}
        )
    assert scene.loader_calls == 0 and not scene.provider.calls
    assert not (scene.root / "forbidden").exists()


def test_late_baseline_is_attached_without_changing_frozen_plan_or_restarting_it(scene, plan_args):
    path = plan_args.prepare_argon_comparison / "sample/run.json"
    raw = path.read_bytes()
    path.unlink()
    comparison.prepare(plan_args)
    before = (plan_args.output / "plan.json").read_bytes()
    assert comparison.attach_completed_baselines(plan_args.output / "plan.json") == 0
    path.write_bytes(raw)
    assert (
        evaluator.main(["--attach-comparison-baseline", str(plan_args.output / "plan.json")]) == 0
    )
    plan = comparison.read_plan(plan_args.output / "plan.json")
    assert comparison.baseline_state(plan, "sample")["status"] == "COMPLETED_STABLE"
    assert (plan_args.output / "plan.json").read_bytes() == before
    assert path.read_bytes() == raw and not scene.provider.calls and not scene.prompts


def test_smoke_case_subscope_cannot_spend_another_granted_target(scene, plan_args):
    comparison.prepare(plan_args)
    plan, path = grant(plan_args)
    with pytest.raises(ValueError, match="SUBSCOPE_INVALID"):
        comparison.SubmissionBudget(
            plan_args.output / "plan.json", path, allowed_case_ids=["not-granted"]
        )
    assert not (plan_args.output / "cost-reservations").exists()


def test_expired_price_cannot_reserve_new_generation_but_keeps_readback_authority(
    scene, plan_args, monkeypatch
):
    from src.autoslice.argon_escalation_contract import (
        AudioSpec,
        Finding,
        prepare_audio,
        make_inner_request,
    )

    comparison.prepare(plan_args)
    _, path = grant(plan_args)
    spec = AudioSpec(
        scene.source,
        Finding(sha(scene.source.read_bytes()), (1000, 2000), "synthetic-target"),
        (500, 4500),
    )
    binding, audio = prepare_audio(spec)
    monkeypatch.setattr(comparison, "PRICE_VALID_BEFORE", "2000-01-01T00:00:00+00:00")
    comparison.authorize(
        plan_args.output / "plan.json", path
    )  # Original job queries remain possible.
    budget = comparison.SubmissionBudget(plan_args.output / "plan.json", path)
    with pytest.raises(ValueError, match="REQUOTE_REQUIRED"):
        budget.reserve(spec, "not-dispatched", make_inner_request(binding, audio))
    assert not budget.root.exists() and not scene.provider.calls
