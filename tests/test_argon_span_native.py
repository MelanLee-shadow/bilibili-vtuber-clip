"""Actual normal Argon acquisition, read-only span state and cold consumers."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.autoslice.span_phoneme_resolution import observation_catalogue
from src.autoslice.native_context_witness import (
    build_independent_context_witnesses as REAL_NATIVE_WRAPPER,
)

from tests.test_argon_consumer_integration import offline, scene  # noqa: F401


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink()
    }


def test_public_native_completed_span_state_is_readonly_and_revalidates(scene):
    _, first = scene.run()
    assert first["argon_escalation"]["status"] == "PENDING"
    callback = getattr(scene.verifier, "span_termination_upgrade_status", None)
    assert callable(callback), "normal producer dropped the native span status seam"
    before = snapshot(scene.root)
    state = callback(first["request"])
    assert state["status"] != "COMPLETED" and state["enabled"] is True
    assert snapshot(scene.root) == before
    output, done = scene.run(scene.build())
    assert done["repaired"] and output != scene.srt
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]
    before = snapshot(scene.root)
    state = callback(done["request"])
    assert state["status"] == state["submission_outcome"] == "COMPLETED"
    assert state["observations"][0]["provider"] == "gemini_api"
    assert state["observations"][0]["model"] == "gemini-4-argon"
    catalogue, invalid = observation_catalogue(done["request"], done["verdict"])
    assert any(r["kind"] == "argon_native_transcript" for r in catalogue) and not invalid
    assert snapshot(scene.root) == before
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]


def test_public_producer_off_keeps_span_upgrade_unconfigured(scene):
    verifier = scene.build(False)
    assert not getattr(verifier, "span_termination_upgrade_enabled", False)
    assert getattr(verifier, "argon_escalation", None) is None


@pytest.fixture
def span_scene(scene, monkeypatch):
    """Real native composition with explicitly configured primary-only routes.

    This bounded fixture has no MAI/MOSS/range provider configured. A separate
    control below uses the producer's complete inventory and cannot terminate.
    """
    # scene's old fixture installs a wrapper bypass. Restore the actual shared
    # wrapper from the imported function captured before that fixture runs.
    from tests.test_span_phoneme_resolution import model_for
    from src.autoslice.clip_context import _canonical_json_sha256
    import hashlib

    scene.srt = scene.srt.replace("一百五十秒", "他说一百五十秒")
    scene.finding = {**scene.finding, "proposed_full_cue": "他说一百五十首"}
    scene.provider.argon_text = "她说一百五十首"
    scene.ordinary_status = "OBSERVED"
    scene.context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "synthetic-consumer",
        "recording_date": "2026-10-16",
        "selection_hook": "synthetic local replacement",
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": 6000,
                "source_media_sha256": "sha256:"
                + hashlib.sha256(scene.source.read_bytes()).hexdigest(),
            }
        ],
        "whole_clip_draft_srt": scene.srt,
        "whole_clip_draft_srt_sha256": "sha256:" + hashlib.sha256(scene.srt.encode()).hexdigest(),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": []},
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    scene.context["context_sha256"] = _canonical_json_sha256(scene.context)
    original = scene.judge
    scoped = model_for(target_quote="首", blind_quote="shou", outside=True)

    def judge(prompt):
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            raw = json.loads(scoped(prompt))
            raw["argon_escalation"] = {
                "reading_chat": True,
                "reason": "synthetic ordinary same-target diagnosis",
            }
            return json.dumps(raw, ensure_ascii=False)
        return original(prompt)

    judge.cpa_cache_identity = original.cpa_cache_identity
    scene.judge = judge
    scene.span_prompts = scoped.prompts

    def build():
        base = scene.build()
        wrapper = REAL_NATIVE_WRAPPER(
            fallback=base,
            source_media=scene.source,
            output_dir=scene.out / "primary-only",
            providers=(),
        )
        wrapper.argon_escalation = base.argon_escalation
        return wrapper

    scene.span_build = build
    return scene


def test_actual_native_span_termination_keeps_raw_needs_audio_and_cold_native_identity(span_scene):
    from src.autoslice.span_phoneme_resolution import validated_resolution
    from src.autoslice.producer_package_finalization import _apply_exact_final_cpa_repairs
    from src.autoslice.final_review_auditor import audit_correction_mutation_authority

    scene = span_scene
    before, pending = scene.run(scene.span_build())
    assert pending["argon_escalation"]["status"] == "PENDING", pending
    output, done = scene.run(scene.span_build())
    (scene.root / "SPAN-NATIVE-AUDIT.json").write_text(json.dumps(done, ensure_ascii=False))
    assert done["policy_branch"] == "CONTEXT_PHONEME_FIT", done
    proof = validated_resolution(done["span_context_resolution"], srt_text=scene.srt)
    assert proof["judge"]["needs_audio_requested"] is True
    assert proof["outside_findings"] and all(
        r["status"] == "UNRESOLVED" for r in proof["outside_findings"]
    )
    assert proof["route_state"]["upgrade"]["native_identity"]["model_version"] == "gemini-4-argon"
    finding = {
        **scene.finding,
        "base_text_sha256": done["request"]["base_text_sha256"],
        "exact_release_adjudication": done,
    }
    saved, repairs = _apply_exact_final_cpa_repairs(
        scene.srt, {"findings": [finding]}, clip_context=scene.context
    )
    assert saved == output and "他说一百五十首" in saved and len(repairs) == 1
    assert repairs[0]["span_context_resolution"] == proof
    mutation = {
        "status": "APPLIED",
        "applied_count": 1,
        "findings": [
            {
                **scene.finding,
                "routed": "context_audio_adjudicated_fix",
                "context_audio_adjudication": done,
            }
        ],
    }
    assert audit_correction_mutation_authority(mutation)["status"] == "PASS"
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]


def test_public_producer_gate_persists_native_span_and_new_process_retains_outside_obligation(
    span_scene,
):
    import hashlib
    import subprocess
    import sys
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import producer_package_finalization as finalizer
    from src.autoslice.span_finding_obligations import pending_findings
    from tests.test_producer_text_pipeline_final_review import (
        _adapters,
        _correction_pass,
        _literal_review_llm,
    )

    scene = span_scene
    final = scene.out / "clip.srt"
    final.write_text(scene.srt)
    authority = {}
    saved = scene.out / "authority.json"
    records = []

    def model(prompt):
        if "SPAN_REVIEW_INPUT" in prompt and "一百五十秒" not in final.read_text():
            return json.dumps(
                {
                    "choice": "NEITHER",
                    "needs_audio": True,
                    "reason": "synthetic independent prefix still unresolved",
                }
            )
        if "SPAN_REVIEW_INPUT" in prompt or any(
            w in prompt for w in ("字幕文字闭集裁决", "疑似拼音", "最终文字法官")
        ):
            return scene.judge(prompt)
        if "字幕缺失候选" in prompt or "字幕坏闭集" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "decision": "UNRESOLVED",
                    "needs_audio": True,
                    "proposed_cue": "",
                    "replacement_text": "",
                    "reason": "synthetic independent prefix still open",
                }
            )
        return json.dumps(
            {
                "findings": (
                    []
                    if "一百五十秒" not in final.read_text()
                    else [
                        {
                            "cue": 1,
                            "kind": "context",
                            "repair_class": "phonetic",
                            "suspect": "秒",
                            "suggestion": "首",
                            "proposed_full_cue": "他说一百五十首",
                            "why": "synthetic bound word doubt",
                            "content_risk": "meaning_reversal",
                            "risk_basis": "synthetic unit changes meaning",
                        }
                    ]
                )
            },
            ensure_ascii=False,
        )

    call = _literal_review_llm(model)
    call.cpa_cache_identity = scene.judge.cpa_cache_identity

    def pronoun(prompt):
        return json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": "cue-1-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "他",
                        "replacement_token": "他",
                        "referent": "UNSPECIFIED",
                        "reason": "synthetic unresolved speaker",
                    }
                ]
            }
        )

    def reviewer(text, prior, offset, end):
        result = pipeline._run_exact_final_release_review(
            srt_text=text,
            correction_audit=_correction_pass(),
            adapters=_adapters(),
            authoritative_chat=(),
            selection_hook="",
            clip_context=scene.context,
            verify_confusable_entity=scene.span_build(),
            verified_authority_audit=prior,
            timeline_offset_ms=offset,
            source_media_sha256=hashlib.sha256(scene.source.read_bytes()).hexdigest(),
            final_review_llm=call,
            pronoun_audit_llm=pronoun,
        )
        records.append(result)
        return result

    for _ in range(2):
        with pytest.raises(SystemExit, match="FINAL_REVIEW_RELEASE_BLOCKED"):
            finalizer._run_exact_final_review_gate(
                cid="synthetic-consumer",
                out_root=scene.out,
                final_start=1000,
                final_end=6000,
                recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=final),
                chat_authority_audit=authority,
                chat_authority_path=saved,
                run_exact_final_review=reviewer,
                clip_context=scene.context,
            )
    assert "他说一百五十首" in final.read_text(), [
        (r.get("reason_codes"), r.get("findings")) for r in records
    ]
    disk = json.loads(saved.read_bytes())
    questions = pending_findings(final.read_text(), disk)
    assert questions and any(r.get("origin_span_obligation") for r in questions)
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]
    (scene.root / "ORIGINAL.srt").write_text(scene.srt)
    proof = next(
        r["span_context_resolution"]
        for run in disk["exact_final_cpa_self_heal"]["passes"]
        for r in run["repairs"]
        if "span_context_resolution" in r
    )
    (scene.root / "COLD-RESOLUTION.json").write_text(json.dumps(proof, ensure_ascii=False))
    before = snapshot(scene.root)
    script = """
import json,sys,socket
from pathlib import Path
def no(*a,**k):raise AssertionError('no network in cold consumer')
socket.socket.connect=no
from src.autoslice.span_phoneme_resolution import validated_resolution
from src.autoslice.span_finding_obligations import pending_findings
from src.autoslice.final_review_contract import validate_final_review_release
validated_resolution(json.loads(Path(sys.argv[1]).read_bytes()))
text=Path(sys.argv[2]).read_text();a=json.loads(Path(sys.argv[3]).read_bytes())
p=pending_findings(text,a);assert p
try:validate_final_review_release(a['final_review_audit'])
except ValueError:print('NATIVE_COLD_VALIDATED_OUTSIDE_BLOCKED',len(p))
else:raise AssertionError('outside obligation released')
"""
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(scene.root / "COLD-RESOLUTION.json"),
            str(final),
            str(saved),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert "NATIVE_COLD_VALIDATED_OUTSIDE_BLOCKED" in run.stdout
    assert snapshot(scene.root) == before


@pytest.mark.parametrize(
    "damage", ["source", "clock", "raw", "old_raw", "identity", "operation", "state"]
)
def test_completed_native_span_cold_replay_rejects_drift_without_io(span_scene, damage):
    from src.autoslice.span_phoneme_resolution import validated_resolution
    from src.autoslice.argon_escalation_contract import canonical
    from src.autoslice import argon_escalation_storage as store

    scene = span_scene
    scene.run(scene.span_build())
    _, done = scene.run(scene.span_build())
    proof = deepcopy(done["span_context_resolution"])
    envelope = proof["request"]["argon_escalation_evidence"]
    task = Path(envelope["engine_root"]) / envelope["task_id"]
    if damage == "source":
        scene.source.write_bytes(scene.source.read_bytes() + b"drift")
    elif damage == "clock":
        proof["request"]["source_media_timeline_offset_ms"] += 1
    elif damage in ("raw", "identity", "operation"):
        result = store.get_json(task / "blobs" / (envelope["result_sha256"] + ".bin"))
        raw = task / "blobs" / (result["response_sha256"] + ".bin")
        if damage == "raw":
            raw.write_bytes(raw.read_bytes() + b"drift")
        else:
            data = json.loads(raw.read_bytes())
            if damage == "operation":
                data["error"] = {"code": 7, "message": "synthetic contradiction"}
            else:
                data["metadata"]["output"]["inlinedResponses"]["inlinedResponses"][0][
                    "response"
                ].pop("modelVersion")
            raw.write_bytes(canonical(data))
    elif damage == "old_raw":
        old = next((Path(envelope["consumer_root"]) / "ordinary-history").glob("*.json"))
        event = store.get_json(old)
        path = (
            Path(envelope["consumer_root"])
            / "blobs"
            / (event["ordinary"]["artifacts"]["response"] + ".bin")
        )
        path.write_bytes(path.read_bytes() + b"drift")
    else:
        proof["route_state"]["upgrade"]["submission_outcome"] = "OUTCOME_UNKNOWN"
    before = snapshot(scene.root)
    with pytest.raises((ValueError, OSError, RuntimeError)):
        validated_resolution(proof)
    assert snapshot(scene.root) == before
    assert [r["method"] for r in scene.provider.calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "kind", ["pending", "unknown", "contradiction", "different_phoneme", "neither"]
)
def test_native_nonterminal_or_disagreement_never_terminates_target(span_scene, kind):
    scene = span_scene
    if kind == "unknown":
        scene.provider.post_error = TimeoutError("synthetic submit outcome unknown")
        scene.provider.remote_state = "BATCH_STATE_RUNNING"
    if kind == "contradiction":
        scene.provider.mutate = lambda d: {
            **d,
            "error": {"code": 7, "message": "synthetic operation contradiction"},
        }
    scene.run(scene.span_build())
    if kind == "pending":
        scene.provider.remote_state = "BATCH_STATE_RUNNING"
    if kind == "different_phoneme":
        scene.provider.argon_text = "她说一百五十手表"
    if kind == "neither":
        previous = scene.judge

        def call(prompt):
            result = json.loads(previous(prompt))
            result["choice"] = "NEITHER"
            return json.dumps(result, ensure_ascii=False)

        call.cpa_cache_identity = previous.cpa_cache_identity
        scene.judge = call
    result, done = scene.run(scene.span_build())
    assert result == scene.srt and not done["repaired"]
    assert done.get("policy_branch") != "CONTEXT_PHONEME_FIT"
    assert sum(r["method"] == "POST" for r in scene.provider.calls) == 1


def test_full_normal_wrapper_inventory_is_forwarded_without_disabling_unfinished_routes(
    scene, monkeypatch
):
    from src.autoslice import native_context_witness as native

    monkeypatch.setattr(native, "build_independent_context_witnesses", REAL_NATIVE_WRAPPER)
    verifier = scene.build()
    assert verifier.span_termination_ordinary_routes == (
        "primary",
        "mai",
        "moss",
        "range:mai",
        "range:moss",
        "range:gemini",
    )
    assert verifier.span_termination_source_identity() == verifier.argon_escalation.source_sha
    assert verifier.span_termination_upgrade_enabled is True


@pytest.mark.parametrize(
    "field",
    ["status", "submission_outcome", "edit_binding_sha256", "native_identity", "observations"],
)
def test_native_upgrade_claim_itself_is_replayed_not_trusted(scene, field):
    from src.autoslice.argon_span_termination import validate_upgrade

    scene.run()
    _, done = scene.run(scene.build())
    state = scene.verifier.span_termination_upgrade_status(done["request"])
    before = snapshot(scene.root)
    state = deepcopy(state)
    if field == "native_identity":
        state[field]["model_version"] = "agy"
    elif field == "observations":
        state[field][0]["provider"] = "mai"
    else:
        state[field] = "forged"
    with pytest.raises(ValueError):
        validate_upgrade(done["request"], state)
    assert snapshot(scene.root) == before and [r["method"] for r in scene.provider.calls] == [
        "POST",
        "GET",
    ]


def test_unsubmitted_native_status_does_not_create_engine_or_ledger(scene):
    from src.autoslice.final_review_auditor import build_context_adjudication_request

    request = build_context_adjudication_request(
        scene.srt,
        scene.finding,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        require_complete_utterance_support=True,
    )
    before = snapshot(scene.root)
    status = scene.verifier.span_termination_upgrade_status(request)
    assert status["enabled"] is True and status["status"] == "AVAILABLE"
    assert not (scene.root / "runtime/cache/argon-consumer").exists()
    assert snapshot(scene.root) == before and not scene.provider.calls
