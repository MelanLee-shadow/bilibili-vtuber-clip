"""Joint opt-in integration; all media/configuration/model boundaries synthetic."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from scripts import subtitle_regression_eval as cli
from src.autoslice import argon_regression_comparison as comparison
from src.autoslice.argon_escalation_contract import canonical, sha
from tests.test_argon_regression_comparison import grant

pytest_plugins = [
    "tests.test_argon_regression_comparison",
    "tests.test_producer_speaker_generation",
]


def _execution_args(plan_args, output):
    comparison.prepare(plan_args)
    _, path = grant(plan_args)
    args = deepcopy(plan_args)
    args.comparison_plan = args.output / "plan.json"
    args.comparison_grant = path
    args.output = output
    return args


def _argv(args):
    values = [
        "--comparison-plan",
        str(args.comparison_plan),
        "--comparison-grant",
        str(args.comparison_grant),
        "--output",
        str(args.output),
        "--code-identity",
        args.code_identity,
        "--runtime-root",
        args.runtime_root,
    ]
    for case_id in args.case or []:
        values.extend(["--case", case_id])
    if args.code_root is not None:
        values.extend(["--code-root", str(args.code_root)])
    if args.resume_comparison:
        values.append("--resume-comparison")
    return values


def _saved_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize(
    "changed", ["plan", "grant", "case", "code_label", "code_root", "runtime_root"]
)
@pytest.mark.parametrize("unknown", [False, True])
def test_public_cli_resume_identity_cannot_mutate_original(
    scene, plan_args, monkeypatch, changed, unknown
):
    args = _execution_args(plan_args, scene.root / "execution")
    calls = []

    def run(package, out, code, environment, **options):
        calls.append(options)
        out.mkdir(exist_ok=options["resume"])
        return json.loads((plan_args.prepare_argon_comparison / "sample/run.json").read_bytes())

    monkeypatch.setattr(cli, "run_package", run)
    assert cli.main(_argv(args)) == 1
    if unknown:
        identity = json.loads((args.output / "invocation.json").read_bytes())
        (args.output / "invocations/unknown.started.json").write_bytes(canonical(identity))
    before = _saved_bytes(args.output)
    args.resume_comparison = True
    if changed in {"plan", "case"}:
        plan = json.loads(args.comparison_plan.read_bytes())
        if changed == "plan":
            plan["created_at"] = "different-synthetic-freeze"
        else:
            plan["samples"][0]["case_id"] = "other-target"
            args.case = ["other-target"]
        plan.pop("plan_sha256")
        plan["plan_sha256"] = sha(canonical(plan))
        args.comparison_plan = scene.root / "changed-plan.json"
        args.comparison_plan.write_bytes(canonical(plan))
        auth = json.loads(args.comparison_grant.read_bytes())
        auth["plan_sha256"] = plan["plan_sha256"]
        if changed == "case":
            auth["authorized_case_ids"] = ["other-target"]
        args.comparison_grant = scene.root / "changed-synthetic-grant.json"
        args.comparison_grant.write_bytes(canonical(auth))
    elif changed == "grant":
        auth = json.loads(args.comparison_grant.read_bytes())
        auth["authority"] += "-changed"
        args.comparison_grant = scene.root / "other-synthetic-grant.json"
        args.comparison_grant.write_bytes(canonical(auth))
    elif changed == "code_label":
        args.code_identity = "different-claimed-code"
    elif changed == "code_root":
        args.code_root = scene.root / "wrong-code"
    else:
        args.runtime_root = str(scene.root / "wrong-runtime")
    with pytest.raises(ValueError):
        cli.main(_argv(args))
    assert _saved_bytes(args.output) == before
    assert len(calls) == 1 and not scene.provider.calls


@pytest.fixture
def joint(scene, case, monkeypatch):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice.clip_context import _canonical_json_sha256
    from tests.test_producer_text_pipeline_final_review import _adapters, _correction_pass

    scene.srt = (
        "1\n00:00:01,000 --> 00:00:02,000\n一百五十秒\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n她说第二句\n\n"
        "3\n00:00:04,300 --> 00:00:04,800\n她说第三句\n"
    )
    scene.context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "synthetic-consumer",
        "recording_date": "2026-10-16",
        "selection_hook": "synthetic unclear word with optional speaker evidence",
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": 6000,
                "source_media_sha256": "sha256:" + sha(scene.source.read_bytes()),
            }
        ],
        "whole_clip_draft_srt": scene.srt,
        "whole_clip_draft_srt_sha256": "sha256:" + sha(scene.srt.encode()),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": []},
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    scene.context["context_sha256"] = _canonical_json_sha256(scene.context)
    scene.ordinary_model = "Gemini 3.8 Flash (High)"
    pronoun_prompts, discovery_prompts = [], []

    def pronoun(prompt):
        pronoun_prompts.append(prompt)
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": f"cue-{i}-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "她",
                        "referent": "UNSPECIFIED",
                        "reason": "synthetic no pronoun mutation",
                    }
                    for i in (2, 3)
                ]
            }
        )

    def llm(prompt):
        if any(
            x in prompt for x in ("字幕选字裁决", "字幕文字闭集裁决", "疑似拼音", "## 新增 Argon")
        ):
            return scene.judge(prompt)
        discovery_prompts.append(prompt)
        return json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "context",
                        "suspect": "秒",
                        "replacement": "首",
                        "proposed_full_cue": "一百五十首",
                        "repair_class": "phonetic",
                        "content_risk": "meaning_reversal",
                        "risk_basis": "synthetic wrong unit changes meaning",
                        "why": "synthetic unclear unit requires ordinary listening",
                    }
                ]
            },
            ensure_ascii=False,
        )

    llm.cpa_cache_identity = scene.judge.cpa_cache_identity
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: llm)
    monkeypatch.setattr(pipeline, "_build_pronoun_audit_llm_call", lambda: pronoun)
    adapters = replace(_adapters(), profile_asset_file=lambda _: case["profile"])

    def build(*, enabled=True, argon=True, offset=0):
        spec = {
            "date": "2026-10-16",
            "source_piece_clock": {"padded_sha256": sha(scene.source.read_bytes())},
            "argon_escalation": {"enabled": argon},
            "speaker_attribution_generation": {
                "enabled": enabled,
                "speaker_python": str(case["runtime"]),
                "model_dir": str(case["model"]),
                "reference_dir": str(case["refs"]),
            },
        }
        context = pipeline._build_entity_verification_context(
            spec=spec,
            padded=scene.source,
            padded_dur=6000,
            host="localhost",
            text_override_path=None,
            cid="synthetic-consumer",
            out_root=scene.out,
            srt_text=scene.srt,
            authoritative_chat=[],
            adapters=adapters,
        )
        callback = pipeline._build_exact_final_review_callback(
            spec=spec,
            out_root=scene.out,
            cid="synthetic-consumer",
            padded=scene.source,
            adapters=adapters,
            authoritative_chat=(),
            clip_context=scene.context,
            final_review_audit=_correction_pass(),
            verify_confusable_entity=context.verify_confusable_entity,
            truth_ownership=None,
            screen_read_probe=None,
            text_recovery_only=True,
        )
        return callback, context.verify_confusable_entity

    return scene, case, build, pronoun_prompts, discovery_prompts


@pytest.mark.parametrize("offset", [0, 1000])
def test_joint_actual_speaker_cli_and_argon_recovery_reach_normal_consumers(joint, offset):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.unresolved_findings_gate import unresolved_findings_problem

    scene, case, build, pronoun, discovery = joint
    callback, verifier = build(offset=offset)
    first = callback(scene.srt, {}, offset, 6000)
    (scene.root / "joint-first.json").write_bytes(canonical(first))
    assert first["speaker_attribution_generation"]["status"] == "GENERATED", first
    assert (
        pronoun
        and "1. HOST（voiceprint_anchor）" in pronoun[-1]
        and "2. HOST（voiceprint_anchor）" in pronoun[-1]
    )
    assert "3. OTHER" not in pronoun[-1]
    rows = first.get("findings", []) + first.get("resolved_findings", [])
    native = [r.get("exact_release_adjudication", {}) for r in rows]
    # Admin ruling (Argon plan A): after the last round a batch this
    # run dispatched is read back (<=6 min, one read-only GET per 60 s, no new
    # inference); the run leaves PENDING instead of waiting for the next run.
    assert any(a.get("argon_escalation", {}).get("status") == "OBSERVED" for a in native), first
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET"]
    old_path = Path(first["speaker_attribution_generation"]["attribution_path"])
    old = old_path.read_bytes()
    # A fresh ordinary factory/callback with no supplied identity document must
    # recover the real on-disk S2 result and the same Argon job.
    callback, _ = build(offset=offset)
    second = callback(scene.srt, {}, offset, 6000)
    (scene.root / "joint-second.json").write_bytes(canonical(second))
    assert second["speaker_attribution_generation"]["status"] == "CACHE_HIT", second
    assert old_path.read_bytes() == old
    assert len(list((scene.out / "speaker-attribution").glob("*/execution.json"))) == 1
    output, repairs = _apply_exact_final_cpa_repairs(scene.srt, second, clip_context=scene.context)
    assert output != scene.srt and "一百五十首" in output and len(repairs) == 1, second
    assert repairs[0]["acoustic_witness_binding"]["witness"]["argon_escalation_evidence"]
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET"]
    assert any("## 新增 Argon 独立盲听证据" in p for p in scene.prompts)
    # A successful machine repair is still not whole-package release.
    assert first["release_gate"] == second["release_gate"] == "BLOCK"
    # The machine repair is a typed edit: it must be applied and the whole
    # text reviewed again (lifecycle EDIT_REVIEW), not a pending question.
    assert "FINAL_REVIEW_EDIT_REVIEW_REQUIRED" in first["reason_codes"]
    assert unresolved_findings_problem({"unresolved_findings_disclosed": first["findings"]})
    kept, vetoed = _apply_exact_final_cpa_repairs(
        scene.srt, second, clip_context=scene.context, protected_cue_indexes=frozenset({1})
    )
    assert kept == scene.srt and not vetoed
    binding = second["speaker_attribution"]["source_binding"]
    assert binding["timeline_offset_ms"] == offset
    assert binding["geometry"]["source_interval_ms"] == [offset, 6000]


@pytest.mark.parametrize(
    "mode", ["missing_model", "low_scores", "one_anchor", "corrupt_decoded", "old_nonhost"]
)
def test_s2_unknown_does_not_abort_argon_or_manufacture_identity(joint, mode):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs

    scene, case, build, pronoun, _ = joint
    if mode == "missing_model":
        (case["model"] / "weights").unlink()
    elif mode in {"low_scores", "one_anchor"}:
        script = case["runtime"].read_text()
        script = script.replace("'cue-0002.wav':.82", "'cue-0002.wav':.20")
        if mode == "low_scores":
            script = script.replace("'cue-0001.wav':.85", "'cue-0001.wav':.21")
        case["runtime"].write_text(script)
    callback, _ = build()
    first = callback(scene.srt, {}, 0, 6000)
    if mode in {"corrupt_decoded", "old_nonhost"}:
        directory = Path(first["speaker_attribution_generation"]["attribution_path"]).parent
        if mode == "corrupt_decoded":
            p = directory / "work/clip-16k-mono.wav"
            p.write_bytes(p.read_bytes() + b"changed")
        else:
            p = directory / "attribution.raw.json"
            doc = json.loads(p.read_bytes())
            doc["cues"][2]["speaker"] = "NON_HOST"
            p.write_bytes(canonical(doc))
    callback, _ = build()
    second = callback(scene.srt, {}, 0, 6000)
    assert {r["speaker"] for r in second["speaker_attribution"]["cues"]} == {"UNKNOWN"}
    assert "voiceprint_anchor" not in pronoun[-1]
    output, repairs = _apply_exact_final_cpa_repairs(scene.srt, second, clip_context=scene.context)
    assert output != scene.srt and repairs, second
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]
    assert second["release_gate"] == "BLOCK"


@pytest.mark.parametrize("mode", ["neither", "raw_drift", "contradiction", "wrong_model"])
def test_s2_host_context_cannot_promote_unresolved_or_invalid_argon(joint, mode):
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs

    scene, case, build, pronoun, _ = joint
    # Admin ruling (Argon plan A): after the last round a batch this
    # run dispatched is read back (<=6 min, one read-only GET per 60 s, no new
    # inference); the retrieved response is the one under test, so set it first.
    if mode == "neither":
        scene.after_choice = "NEITHER"
    elif mode == "contradiction":
        # Contradict the retrieved job (the GET), not the original submission.
        scene.provider.mutate = lambda response: response if (
            (response.get("metadata") or {}).get("state") == "BATCH_STATE_PENDING") else {
            **response,
            "error": {"code": 7, "message": "synthetic contradiction"},
        }
    elif mode == "wrong_model":
        original = scene.provider.payload
        scene.provider.payload = lambda: {**original(), "modelVersion": "wrong-model"}
    callback, _ = build()
    first = callback(scene.srt, {}, 0, 6000)
    assert first["speaker_attribution_generation"]["status"] == "GENERATED"
    callback, _ = build()
    second = callback(scene.srt, {}, 0, 6000)
    if mode == "raw_drift":
        row = next(
            f for f in second["findings"] if f.get("exact_release_adjudication", {}).get("repaired")
        )
        audit = row["exact_release_adjudication"]
        envelope = audit["request"]["argon_escalation_evidence"]
        task = Path(envelope["engine_root"]) / envelope["task_id"]
        result = json.loads((task / "blobs" / (envelope["result_sha256"] + ".bin")).read_bytes())
        raw = task / "blobs" / (result["response_sha256"] + ".bin")
        raw.write_bytes(raw.read_bytes() + b"changed")
    output, repairs = _apply_exact_final_cpa_repairs(scene.srt, second, clip_context=scene.context)
    assert output == scene.srt and not repairs
    assert second["release_gate"] == "BLOCK"
    assert "1. HOST（voiceprint_anchor）" in pronoun[-1]
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]
    assert len(list((scene.out / "speaker-attribution").glob("*/execution.json"))) == 1


@pytest.mark.parametrize("s2,argon", [(False, False), (True, False), (False, True)])
def test_joint_optins_are_independent_defaults(joint, s2, argon):
    scene, case, build, pronoun, _ = joint
    callback, _ = build(enabled=s2, argon=argon)
    result = callback(scene.srt, {}, 0, 6000)
    assert ("speaker_attribution_generation" in result) == s2
    assert bool(scene.provider.calls) == argon
    assert ("voiceprint_anchor" in pronoun[-1]) == s2
    assert result["release_gate"] == "BLOCK"


def test_comparison_cli_synthetic_grant_uses_unmocked_package_gate_and_cold_job(
    joint, plan_args, monkeypatch
):
    """No mocked run_package/reviewer/gate: only external model/asset boundaries."""
    import shutil
    from src.autoslice import producer_text_pipeline as pipeline
    from scripts import produce_slice_package as producer
    from tests.test_producer_text_pipeline_final_review import _correction_pass

    scene, case, build, pronoun, _ = joint
    # Prime an ordinary real manifest through the shared normal callback. Model
    # answers are fake; no Argon generation and no S2 generation are enabled.
    # This reusable fixture's builder is explicitly configured with the REST
    # label; make its returned fake identity match so cache compatibility is real.
    # Joint High/Low identity controls remain covered independently above.
    scene.ordinary_status = "OBSERVED"  # Eligible ordinary cache, still needs CPA/audio diagnosis.
    scene.ordinary_model = "gemini-3.8-flash"
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    _, verifier = build(enabled=False, argon=False)
    _, baseline_adjudication = adjudicate_context_finding(
        scene.srt,
        scene.finding,
        entity_verifier=verifier,
        clip_context=scene.context,
        source_media_timeline_offset_ms=0,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    assert baseline_adjudication["verdict"]["status"] == "OBSERVED"
    assert not scene.provider.calls
    docs = json.loads(plan_args.cases.read_bytes())
    package = docs["packages"]["sample"]
    context = Path(package["context_path"])
    context.write_bytes(canonical(scene.context))
    package["context_sha256"] = sha(context.read_bytes())
    correction = _correction_pass()
    record = Path(package["record_path"])
    r = json.loads(record.read_bytes())
    r["clip_context_payload_sha256"] = scene.context["context_sha256"]
    r["boundary_audit"] = {
        "boundary_semantic_review": {
            **correction["boundary_semantic_review"],
            "review_scope": "source_full_window",
        }
    }
    record.write_bytes(canonical(r))
    package["record_sha256"] = sha(record.read_bytes())
    authority = Path(package["authority_path"])
    authority.write_bytes(
        canonical(
            {"final_review_audit": {**correction, "correction_pass": correction}, "applied": []}
        )
    )
    package["authority_sha256"] = sha(authority.read_bytes())
    plan_args.cases.write_bytes(canonical(docs))
    baseline = plan_args.prepare_argon_comparison / "sample"
    raw = json.loads((baseline / "run.json").read_bytes())
    raw["input_hashes"] = {
        package[k + "_path"]: package[k + "_sha256"]
        for k in ("record", "srt", "authority", "media")
    }
    (baseline / "run.json").write_bytes(canonical(raw))
    shutil.copytree(scene.out / "entity_verdicts", baseline / "entity_verdicts")
    args = _execution_args(plan_args, scene.root / "full-cli")
    monkeypatch.setattr(producer, "profile_asset_file", lambda _: case["profile"])
    monkeypatch.setattr(producer, "_topic_graph_disabled", lambda: True)
    monkeypatch.setattr(producer, "_review_glossary", lambda: "")
    existing = pipeline._build_final_review_llm_call()

    def llm(prompt):
        if "本次不授权改字、删字或听音调度" in prompt:
            agenda = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps(
                {
                    "findings": [
                        {
                            "cue": row["cue"],
                            "content_risk": "meaning_reversal",
                            "risk_basis": "Synthetic additional cue remains unresolved; never a release shortcut",
                        }
                        for row in agenda
                    ]
                }
            )
        final = args.output / "sample/final.srt"
        if (
            not any(
                x in prompt
                for x in ("字幕选字裁决", "字幕文字闭集裁决", "疑似拼音", "## 新增 Argon")
            )
            and final.exists()
            and "一百五十首" in final.read_text()
        ):
            return '{"findings": []}'
        return existing(prompt)

    llm.cpa_cache_identity = existing.cpa_cache_identity
    monkeypatch.setattr(pipeline, "_build_final_review_llm_call", lambda: llm)
    rc = cli.main(_argv(args))
    run = json.loads((args.output / "sample/run.json").read_bytes())
    (scene.root / "unmocked-first.json").write_bytes(canonical(run))
    # Admin ruling (Argon plan A): after the last round a batch this
    # run dispatched is read back (<=6 min, one read-only GET per 60 s, no new
    # inference); the run leaves PENDING instead of waiting for the next run.
    assert [c["method"] for c in scene.provider.calls] == ["POST", "GET"], run
    assert scene.provider.calls[0]["method"] == "POST"
    assert run["would_release"] is False and rc == 1
    first_invocation = (args.output / "invocation.json").read_bytes()
    count = scene.ordinary_calls
    args.resume_comparison = True
    cli.main(_argv(args))
    done = json.loads((args.output / "sample/run.json").read_bytes())
    (scene.root / "unmocked-second.json").write_bytes(canonical(done))
    assert done["final_cues"][0]["text"] == "一百五十首", done
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]
    assert scene.ordinary_calls == count
    assert not (args.output / "sample/speaker-attribution").exists()
    assert (args.output / "invocation.json").read_bytes() == first_invocation
    assert any("## 新增 Argon 独立盲听证据" in p for p in scene.prompts)
    original_files = _saved_bytes(args.output)
    # Direct run_package is also a public callable; no alternate grant/root
    # may overwrite its per-package invocation before CLI-level validation.
    config = {
        "enabled": True,
        "comparison_plan": str(args.comparison_plan),
        "comparison_grant": str(args.comparison_grant),
        "comparison_case_ids": ["target"],
    }
    changed_grant = json.loads(args.comparison_grant.read_bytes())
    changed_grant["authority"] += "-different-synthetic-authority"
    changed_path = scene.root / "second-synthetic-grant.json"
    changed_path.write_bytes(canonical(changed_grant))
    config["comparison_grant"] = str(changed_path)
    with pytest.raises(ValueError, match="COMPARISON_PACKAGE_IDENTITY_DRIFT"):
        cli.run_package(
            package,
            args.output / "sample",
            Path.cwd(),
            {},
            comparison_config=config,
            witness_cache_root=Path(
                comparison.read_plan(args.comparison_plan)["baseline"]["sample"]["run_path"]
            ).parent,
            resume=True,
        )
    assert _saved_bytes(args.output) == original_files
    assert [row["method"] for row in scene.provider.calls] == ["POST", "GET"]
