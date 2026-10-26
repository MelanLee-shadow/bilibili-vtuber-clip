"""Same ordinary public entry in both arms; only model HTTP/config is fake."""

import json
import os
from src.autoslice.native_context_witness import build_independent_context_witnesses as real_wrapper

import pytest

from scripts import subtitle_regression_eval as cli
from src.autoslice import argon_regression_comparison as comparison
from src.autoslice import argon_comparison_policy as policy
from src.autoslice.argon_escalation_contract import canonical, sha
from tests.test_argon_regression_comparison import grant

from tests.test_common_regression_execution import (  # noqa: F401
    case, joint, offline, ordinary_case, plan_args, scene,
)


@pytest.fixture
def normal_pair(ordinary_case, monkeypatch):
    from src.autoslice import (
        native_context_witness as native,
        mai_transcription as mai,
        moss_transcription as moss,
    )

    scene, plan_args, _, runtime, paid, _, _ = ordinary_case
    for name, text in [
        (
            "mai.env",
            "AZURE_API_KEY=synthetic-key\nAZURE_ENDPOINT=https://synthetic.cognitiveservices.azure.com\n",
        ),
        ("moss.env", "MOSS_API_KEY=synthetic-key\n"),
    ]:
        p = runtime / name
        p.write_text(text)
        p.chmod(0o600)
    monkeypatch.setattr(native, "build_independent_context_witnesses", real_wrapper)
    calls = []

    def mai_http(audio, filename, mime, endpoint, key):
        calls.append(("mai", sha(audio)))
        return 200, canonical(
            {
                "durationMilliseconds": 500,
                "combinedPhrases": [{"text": "一百五十首"}],
                "phrases": [
                    {
                        "offsetMilliseconds": 0,
                        "durationMilliseconds": 500,
                        "text": "一百五十首",
                        "speaker": 0,
                    }
                ],
            }
        )

    def moss_http(path, audio, key):
        calls.append(("moss", sha(audio)))
        return 200, canonical(
            {"text": "一百五十手", "segments": [{"start": 0, "end": 0.5, "text": "一百五十手"}]}
        )

    monkeypatch.setattr(mai, "_request_once", mai_http)
    monkeypatch.setattr(moss, "_request_once", moss_http)
    doc = json.loads(plan_args.cases.read_bytes())
    package = doc["packages"]["sample"]
    prior = plan_args.prepare_argon_comparison / "sample/run.json"
    run = json.loads(prior.read_bytes())
    run["input_hashes"] = {
        package[k + "_path"]: package[k + "_sha256"]
        for k in ("record", "srt", "authority", "media")
    }
    prior.write_bytes(canonical(run))
    comparison.prepare(plan_args)
    plan, auth = grant(plan_args)
    path = scene.root / "scope-revision.json"
    revision = {
        "schema_version": policy.SCHEMA,
        "status": "AUTHORIZED",
        "authority": "synthetic direct-user scope revision",
        "plan_sha256": plan["plan_sha256"],
        "grant_sha256": sha(auth.read_bytes()),
        "case_ids": ["target"],
        "ordinary_audio_policy": "NORMAL_PRODUCTION_ROUTES",
        "ordinary_paid_policy": "EXISTING_THREE_ROUND_GATE",
        "argon_campaign_budget_unchanged": True,
        "s2_enabled": False,
        "argon_realtime_fallback": False,
    }
    path.write_bytes(canonical(revision))

    def argv(arm, *, resume=False):
        result = [
            "--comparison-plan",
            str(plan_args.output / "plan.json"),
            "--comparison-grant",
            str(auth),
            "--comparison-scope-revision",
            str(path),
            "--argon-arm",
            arm,
            "--case",
            "target",
            "--output",
            str(scene.root / arm),
            "--runtime-root",
            str(runtime),
            "--code-root",
            str(cli.ROOT),
            "--code-identity",
            "same-synthetic-code",
        ]
        if arm == "on":
            result.extend(["--paired-off-output", str(scene.root / "off")])
        if resume:
            result.append("--resume-comparison")
        return result

    return scene, plan_args, plan, auth, path, argv, calls, paid


def test_actual_cli_off_on_normal_routes_original_grant_preserved(normal_pair):
    scene, args, plan, auth, path, argv, calls, paid = normal_pair
    immutable = (
        auth.read_bytes(),
        (args.output / "plan.json").read_bytes(),
        (paid / "usage.json").read_bytes(),
    )
    assert cli.main(argv("off")) in (0, 1)
    off = json.loads((scene.root / "off/sample/run.json").read_bytes())
    assert off["ordinary_route_validity"]["status"] == "VALID_ROUTE_ATTEMPT_EVIDENCE", off
    assert {x[0] for x in calls} == {"mai", "moss"}
    assert not scene.provider.calls
    old_count = len(calls)
    assert cli.main(argv("on")) in (0, 1)
    on = json.loads((scene.root / "on/sample/run.json").read_bytes())
    assert on["ordinary_route_validity"]["status"] == "VALID_ROUTE_ATTEMPT_EVIDENCE", on
    assert len(calls) > old_count
    for run in (off, on):
        rows = run["ordinary_route_validity"]["samples"]
        assert len(rows) == 1 and rows[0]["independent_asr"]["routes_attempted"] == 2
        assert all(
            r["status"] == "OBSERVED" and r["response_sha256"]
            for r in rows[0]["real_or_cached_observation"]
        )
        assert run["would_release"] is False  # independent boundary/other doubts remain
    latest = sorted((scene.root / "on/comparisons").glob("*.json"))[-1]
    result = json.loads(latest.read_bytes())["results"][0]
    assert result["baseline_kind"] == "SAME_CODE_OFF"
    assert result["evaluation_validity"] == "VALID_ROUTE_ATTEMPT_EVIDENCE"
    assert (
        auth.read_bytes(),
        (args.output / "plan.json").read_bytes(),
        (paid / "usage.json").read_bytes(),
    ) == immutable
    assert not (scene.root / "off/sample/speaker-attribution").exists()
    assert (scene.root / "off/runtime-state/state/gemini-paid-backup").resolve() == paid.resolve()


@pytest.mark.parametrize(
    "field,value",
    [
        ("grant_sha256", "a" * 64),
        ("s2_enabled", True),
        ("argon_campaign_budget_unchanged", False),
        ("ordinary_audio_policy", "CACHE_ONLY"),
    ],
)
def test_scope_revision_cannot_widen_or_silently_modify_original(normal_pair, field, value):
    scene, args, plan, auth, path, argv, calls, _ = normal_pair
    d = json.loads(path.read_bytes())
    d[field] = value
    path.write_bytes(canonical(d))
    with pytest.raises(ValueError, match="COMPARISON_SCOPE_REVISION_INVALID"):
        cli.main(argv("off"))
    assert not calls and not scene.provider.calls and not (scene.root / "off").exists()


def test_normal_policy_retains_three_round_gate_and_original_ledger(normal_pair, monkeypatch):
    scene, args, _, _, _, _, _, paid = normal_pair
    from src.autoslice import gemini_backup_policy as backup

    base = scene.root / "policy-only/runtime-state"
    base.parent.mkdir(mode=0o700)
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "0")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    with comparison.comparison_environment(base, str(paid.parents[1]), normal_routes=True):
        assert backup.daily_cap() is None and not backup.dev_exception_active()
        assert (
            backup.paid_attempt_allowed("synthetic", prior_strikes=0, key_configured=True)[0]
            is False
        )
        assert (base / "state/gemini-paid-backup").resolve() == paid.resolve()
    assert os.environ["GEMINI_PAID_BACKUP_DAILY_CAP"] == "0"
    assert os.environ["GEMINI_PAID_BACKUP_DEV_EXCEPTION"] == "1"


def test_missing_native_route_stops_before_cpa_not_quality_failure(normal_pair, monkeypatch):
    scene, args, plan, auth, path, argv, calls, paid = normal_pair
    from src.autoslice import native_context_witness as native

    # Actual wrapper configured with no providers is the original missing-route
    # failure class; do not inject a fake routes_attempted count.
    real = native.build_independent_context_witnesses
    monkeypatch.setattr(
        native, "build_independent_context_witnesses", lambda **kw: real(**{**kw, "providers": ()})
    )
    old_prompts = len(scene.prompts)
    assert cli.main(argv("off")) == 1
    run = json.loads((scene.root / "off/sample/run.json").read_bytes())
    assert run["ordinary_route_validity"]["status"] == "INVALID_EVALUATION_CONFIGURATION"
    assert not calls and len(scene.prompts) == old_prompts
    report = json.loads(next((scene.root / "off/comparisons").glob("*.json")).read_bytes())
    assert report["results"][0]["change"] == "INVALID_EVALUATION_CONFIGURATION"


@pytest.mark.parametrize("drift", ["code", "text"])
def test_foreign_off_result_rejected_before_any_on_dispatch(normal_pair, drift):
    scene, args, plan, auth, path, argv, calls, _ = normal_pair
    assert cli.main(argv("off")) in (0, 1)
    if drift == "code":
        p = scene.root / "off/invocation.json"
        d = json.loads(p.read_bytes())
        d["code_identity"] = "different"
        p.write_bytes(canonical(d))
    else:
        p = scene.root / "off/sample/final.srt"
        p.write_text(p.read_text() + "\n")
    count = len(calls)
    native = len(scene.provider.calls)
    questions = len(scene.prompts)
    with pytest.raises(ValueError, match="COMPARISON_OFF_"):
        cli.main(argv("on"))
    assert (
        len(calls) == count
        and len(scene.provider.calls) == native
        and len(scene.prompts) == questions
    )
    assert not list((scene.root / "on/invocations").glob("*.started.json"))


def test_missing_native_config_not_unknown_dispatch(normal_pair):
    scene, args, plan, auth, path, argv, calls, paid = normal_pair
    (paid.parents[1] / "mai.env").unlink()
    questions = len(scene.prompts)
    with pytest.raises(ValueError, match="COMPARISON_ORDINARY_NATIVE_CONFIG_UNAVAILABLE"):
        cli.main(argv("off"))
    assert not calls and len(scene.prompts) == questions
    assert not list((scene.root / "off/invocations").glob("*.started.json"))


def test_request_failure_recorded_as_attempt_not_as_missing_route(normal_pair, monkeypatch):
    scene, args, plan, auth, path, argv, calls, _ = normal_pair
    from src.autoslice import moss_transcription as moss

    def failed(path, audio, key):
        calls.append(("moss_failed", sha(audio)))
        raise moss.MossTranscriptionError(
            "MOSS_HTTP_ERROR", "synthetic HTTP failure", metadata={"http_status": 503}
        )

    monkeypatch.setattr(moss, "_request_once", failed)
    cli.main(argv("off"))
    run = json.loads((scene.root / "off/sample/run.json").read_bytes())
    audit = run["ordinary_route_validity"]["samples"][0]["independent_asr"]
    assert audit["routes_attempted"] == 2
    assert any(
        r["provider"] == "moss" and r["status"] == "UNCERTAIN" for r in audit["observations"]
    )
    assert "moss_failed" in {p for p, _ in calls}
    assert run["ordinary_route_validity"]["status"] == "VALID_ROUTE_ATTEMPT_EVIDENCE"


def test_normal_cli_off_output_successor_preserves_original_and_unknown_history(normal_pair, monkeypatch):
    """Ordinary CLI/gates run unmocked; only existing fixture transports are fake."""
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder
    from tests.test_common_regression_execution import files
    scene,args,plan,auth,path,argv,calls,paid=normal_pair
    assert cli.main(argv('off')) in (0,1)
    parent=scene.root/'off'
    active,_=comparison.effective_invocation(parent)
    # Generate a real captured UNKNOWN at the mock transport boundary.
    monkeypatch.setenv('AUTOSLICE_BASE',str(parent/'runtime-state'))
    finding={'cue_index':1,'suspect':'秒','suggestion':'首','proposed_full_cue':'一百五十首',
             'repair_class':'phonetic','why':'synthetic unknown for copy-integrity control'}
    recorder=SemanticAttemptRecorder(srt_text=scene.srt,finding=finding,clip_context=scene.context,offset_ms=0)
    def interrupted(prompt):raise TimeoutError('synthetic lost transport reply')
    interrupted.cpa_cache_identity={'transport':'fixture','models':['known-offline-error-transport']}
    recorder.plan={'action':'reframe_question','question':'A distinct synthetic uncertainty capture',
                   'why_previous_failed':'This test exercises transport outcome retention, not model quality.',
                   'expected_gain':'Observe whether this mock transport returns.',
                   'verification':'Check actual STARTED/error capture without assuming non-dispatch.'}
    with pytest.raises(TimeoutError):recorder.wrap(interrupted)('synthetic unknown; no resubmit authorized')
    unknown=next(p for p in recorder.directory.glob('*.json') if json.loads(p.read_bytes())['status']=='OUTCOME_UNKNOWN')
    unknown_raw=unknown.read_bytes()
    reservation=args.output/'cost-reservations/target.json'
    reservation.parent.mkdir(exist_ok=True)
    reservation.write_bytes(b'{"status":"RESERVED_OR_OUTCOME_UNKNOWN","amount":"synthetic"}')
    budget_raw=reservation.read_bytes();paid_raw=(paid/'usage.json').read_bytes()
    before=files(parent)
    successor=scene.root/'off-repaired'
    command=argv('off',resume=True)
    command[command.index('--output')+1]=str(successor)
    command[command.index('--code-identity')+1]='synthetic-repaired-code'
    command += ['--comparison-code-successor',active['code_identity'],
                '--comparison-output-predecessor',str(parent),
                '--comparison-output-predecessor-sha256',sha(canonical(active))]
    assert cli.main(command) in (0,1)
    assert files(parent)==before
    assert (successor/unknown.relative_to(parent)).read_bytes()==unknown_raw
    assert reservation.read_bytes()==budget_raw and (paid/'usage.json').read_bytes()==paid_raw
    assert (successor/'runtime-state/state/gemini-paid-backup').resolve()==paid.resolve()
    assert (successor/'predecessor/replay-state.json').read_bytes()==before['replay-state.json']
    receipt=json.loads((successor/'off-output-successor.json').read_bytes())
    assert receipt['copied_retry_and_unknown_history'] and not receipt['quality_pass_inherited']
    assert not receipt['reservation_reset'] and not scene.provider.calls
    run=json.loads((successor/'sample/run.json').read_bytes())
    assert run['parent_run_sha256']==sha(before['sample/run.json'])
    assert run['ordinary_route_validity']['status']=='VALID_ROUTE_ATTEMPT_EVIDENCE'
    second_before=files(successor);parent2,_=comparison.effective_invocation(successor)
    final=scene.root/'off-next-code'
    again=list(command)
    for flag,value in [('--output',str(final)),('--code-identity','synthetic-next-code'),
        ('--comparison-code-successor',parent2['code_identity']),('--comparison-output-predecessor',str(successor)),
        ('--comparison-output-predecessor-sha256',sha(canonical(parent2)))]:
        again[again.index(flag)+1]=value
    assert cli.main(again) in (0,1)
    assert files(parent)==before and files(successor)==second_before
    assert (final/unknown.relative_to(parent)).read_bytes()==unknown_raw
    assert (final/'predecessor/replay-state.json').read_bytes()==before['replay-state.json']
    lineage=final/'predecessor'/sha(canonical(parent2))
    assert (lineage/'replay-state.json').read_bytes()==second_before['replay-state.json']
    assert (lineage/'off-output-successor.json').read_bytes()==second_before['off-output-successor.json']
    assert reservation.read_bytes()==budget_raw and (paid/'usage.json').read_bytes()==paid_raw


@pytest.mark.parametrize('damage',['unknown_invocation','wrong_identity','wrong_code','unexpected_link','native_task'])
def test_off_output_fork_rejects_before_writing_new_output(normal_pair, damage):
    from tests.test_common_regression_execution import files
    scene,args,plan,auth,path,argv,calls,paid=normal_pair
    assert cli.main(argv('off')) in (0,1)
    parent=scene.root/'off';active,_=comparison.effective_invocation(parent)
    identity_sha=sha(canonical(active));code=active['code_identity']
    if damage=='unknown_invocation':(parent/'invocations/unknown.started.json').write_bytes(canonical(active))
    elif damage=='wrong_identity':identity_sha='0'*64
    elif damage=='wrong_code':code='unrelated-code'
    elif damage=='unexpected_link':(parent/'bad-link').symlink_to(scene.root)
    else:
        p=parent/'runtime-state/cache/argon-consumer/target/batch/attempt-1/handle.json'
        p.parent.mkdir(parents=True);p.write_bytes(b'{}')
    before=files(parent);dest=scene.root/'blocked-successor'
    with pytest.raises(ValueError):
        comparison.prepare_off_output_successor(parent,dest,parent_identity_sha256=identity_sha,
            predecessor=code,plan=plan,grant_path=auth,scope_sha256=sha(path.read_bytes()),case_ids=['target'])
    assert not dest.exists() and files(parent)==before and not scene.provider.calls
