"""Ordinary final-review producer/gate with exact-target synthetic provider adapters.

No old film, human annotation or denied readback is used. Only exact primary,
MAI/MOSS and text-model boundary replies are controlled; ordinary receipts,
adjudicator, persistence, typed repair, retry recorder and final gate execute.
"""

from contextlib import ExitStack
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from tests import wholefilm_convergence_support as fixture
from tests.test_wholefilm_three_gaps import evidence_model
from tests.test_native_context_witness import _receipt, native_digest


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    import socket

    def no(*a, **kw):
        raise AssertionError("No actual provider in wholefilm test")

    monkeypatch.setattr(socket.socket, "connect", no)
    monkeypatch.setattr(socket, "create_connection", no)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))


@pytest.mark.parametrize(
    "outside,current_supported", [(False, False), (True, False), (False, True)]
)
def test_real_producer_plurality_persistence_and_fresh_process_gate(
    tmp_path, monkeypatch, outside, current_supported, initial_unlocatable=False
):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import producer_package_finalization as finalizer
    from src.autoslice import entity_audio_verifier as ordinary
    from src.autoslice import native_context_witness as native
    from src.autoslice.clip_context import _canonical_json_sha256
    from src.autoslice.final_review_contract import validate_final_review_release
    from src.autoslice.span_finding_obligations import pending_findings

    root = tmp_path / "case"
    fixture.prepare(root, 1)
    original = fixture.make_srt(["说能发挥吧", "今天讲到这里"])
    for name in ("current.srt", "original.srt"):
        (root / name).write_text(original)
    source = root / "source.wav"
    source_sha = fixture.sha(source.read_bytes())
    final = root / "current.srt"
    path = root / "authority.json"
    end = 5200
    cid = "synthetic-three-gap-producer"
    calls = []
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": cid,
        "recording_date": "2026-10-17",
        "selection_hook": "synthetic exact replacement",
        "pieces": [{"start_ms": 0, "end_ms": end, "source_media_sha256": "sha256:" + source_sha}],
        "whole_clip_draft_srt": original,
        "whole_clip_draft_srt_sha256": "sha256:" + fixture.sha(original.encode()),
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    scoped = evidence_model()

    def model(prompt):
        calls.append(("text", fixture.sha(prompt.encode())))
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            if initial_unlocatable:
                data = json.loads(prompt.split("\nSPAN_REVIEW_INPUT\n",1)[1].split("\nEND_SPAN_REVIEW_INPUT\n",1)[0])
                if data["observations"] and all(o["kind"] == "blind_pinyin" for o in data["observations"]):
                    return json.dumps({"choice":"PROPOSED","candidate_id":"PROPOSAL","needs_audio":True,
                        "reason":"Synthetic primary cannot locate target; already acquired independent evidence must be consumed.",
                        "span_review":{"binding_sha256":data["binding"]["binding_sha256"],
                            "request_sha256":data["request_sha256"],"status":"UNLOCATABLE",
                            "unlocatable_reason":"Primary observation alone lacks reliable localization.",
                            "context_basis":"Synthetic contextual test, not a truth stamp.",
                            "all_audio_questions_located":False,"target_needs_audio":True,
                            "alignments":[],"audio_questions":[]}})
            return scoped(prompt)
        if any(
            x in prompt for x in ("字幕选字裁决", "字幕文字闭集裁决", "疑似拼音", "最终文字法官")
        ):
            return json.dumps(
                {
                    "choice": "PROPOSED",
                    "candidate_id": "PROPOSAL",
                    "needs_audio": True,
                    "current_utterance_supported": False,
                    "current_utterance_support_reason": "synthetic old span unsupported",
                    "reason": "synthetic audio needed",
                }
            )
        if "本次不授权改字、删字或听音调度" in prompt:
            data = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps(
                {
                    "findings": [
                        {
                            "cue": r["cue"],
                            "content_risk": "meaning_reversal",
                            "risk_basis": "synthetic unsupported words",
                        }
                        for r in data
                    ]
                }
            )
        if "recommended_end_cue_index" in prompt and "syntax_complete" in prompt:
            return boundary(prompt)
        # The model may not erase an independent external obligation by simply
        # omitting it once the original target was repaired.
        findings = (
            []
            if "能发挥" not in final.read_text()
            else [
                {
                    "cue": 1,
                    "kind": "context",
                    "suspect": "能发挥",
                    "replacement": "你的坏话",
                    "proposed_full_cue": "说你的坏话吧",
                    "repair_class": "phonetic",
                    "content_risk": "meaning_reversal",
                    "risk_basis": "synthetic unsupported phrase",
                    "why": "synthetic user-diagnosis-shaped case",
                }
            ]
        )
        return json.dumps({"findings": findings}, ensure_ascii=False)

    model.cpa_cache_identity = {"models": ["synthetic-three-gap-model"], "effort": "high"}

    def boundary(prompt, source_stage=False):
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": 2,
                "evidence_cue_indexes": [1, 2, *([3] if source_stage else [])],
                "reason_codes": [],
                "summary": "synthetic completed closing sentence",
            }
        )

    def audio(**kw):
        calls.append(("primary", kw["request"]["request_sha256"]))
        prompt, _, _ = ordinary._entity_observation_prompt(
            request=kw["request"],
            candidates=kw["candidates"],
            recording_date=kw["recording_date"],
            timely_context=kw["timely_context"],
        )
        pp = kw["job_dir"] / "prompt.md"
        pp.write_text(prompt)
        raw = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "shuo ni de huai hua ba",
            "syllable_count": 6,
            "uncertain_positions": [],
            "confidence": 0.9,
            "reason": "synthetic hearing",
        }
        rp = kw["job_dir"] / "verdict.raw.json"
        fixture.write(rp, raw)
        return ordinary._EntityProviderOutcome(
            raw, "agy", ordinary.ENTITY_AUDIO_MODEL, pp, rp, None, None, []
        )

    def native_provider(**kwargs):
        provider = kwargs["provider"]

        def observe(*, start_ms, end_ms):
            calls.append((provider, (start_ms, end_ms)))
            text = (
                "说能发挥吧"
                if current_supported and provider == "moss"
                else "说人坏话吧"
                if provider == "moss"
                else "说你的坏话吧"
            )
            if outside and provider == "moss":
                text = "有人说人坏话吧"
            r = _receipt(
                source_sha256=source_sha,
                start_ms=start_ms,
                end_ms=end_ms,
                provider=provider,
                transcript=text,
            )
            r["model"] = native.NATIVE_MODELS[provider]
            # Match the actual exact-crop writer's declared observation scope.
            # _receipt alone is intentionally incomplete for final mutation.
            r["observation_scope"] = "complete_exact_cue_no_padding"
            r["receipt_sha256"] = native_digest(
                {k: v for k, v in r.items() if k != "receipt_sha256"}
            )
            return r

        return observe

    adapters = pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=None,
        build_agy_transcriber=None,
        load_term_boundary_surfaces=None,
        profile_asset_file=lambda _: root / "absent",
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: None,
        topic_graph_expected_sha256=lambda: "",
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(ordinary, "_observe_entity_audio", audio))
        stack.enter_context(patch.object(native, "build_native_foreign_witness", native_provider))
        stack.enter_context(patch.object(pipeline, "_build_final_review_llm_call", lambda: model))
        stack.enter_context(patch.object(pipeline, "_build_pronoun_audit_llm_call", lambda: model))
        direct = ordinary.build_local_audio_entity_verifier(
            source_media=source,
            output_dir=root,
            recording_date="2026-10-17",
            source_duration_ms=9000,
            agy_bin="/usr/bin/true",
        )
        secondary = native.build_independent_context_witnesses(
            fallback=direct,
            source_media=source,
            output_dir=root / "independent",
            providers=("mai", "moss"),
        )
        # This bounded fixture configures exact-target routes, no range model.
        # Their original receipt constructors and disk/cache readers are real.
        primary = direct

        def direct(request):
            return primary(request)

        direct.probe_witness_cache = primary.probe_witness_cache
        direct.observe_independent_witnesses = secondary.observe_independent_witnesses
        direct.observe_cached_independent_witnesses = secondary.observe_cached_independent_witnesses
        direct.span_termination_ordinary_routes = ("primary", "mai", "moss")
        direct.span_termination_source_identity = lambda: source_sha
        if initial_unlocatable:
            from src.autoslice.final_review_auditor import build_context_adjudication_request
            from src.autoslice.acoustic_witness_adjudication import build_witness_request
            request = build_context_adjudication_request(original,{
                "cue_index":1,"suspect":"能发挥","suggestion":"你的坏话",
                "proposed_full_cue":"说你的坏话吧","repair_class":"phonetic"},clip_context=context)
            # Actual native disk/cache writer, only transport fixture is fake.
            secondary.observe_independent_witnesses(build_witness_request(request))
        source_srt = original + "\n3\n00:00:06,000 --> 00:00:08,200\n下个话题\n"
        source_cues = pipeline.parse_srt_cues(source_srt)
        source_review = pipeline.review_final_boundary_semantics(
            cues=source_cues,
            boundary_target_ms=end,
            candidate_id=cid,
            selection_hook="synthetic closing",
            selection_scorecard={
                "status": "VALID",
                "dimensions": {"self_contained": 4, "comedic_payoff": 4},
            },
            structured_context="",
            candidate_context="",
            boundary_max_forward_ms=30000,
            llm_call=lambda prompt: boundary(prompt, True),
            extract_json=pipeline.extract_json_object,
        )
        from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint

        source_review, _ = bind_final_semantic_endpoint(
            semantic_review=source_review,
            cues=source_cues,
            closure_cue=source_cues[1],
            snapped_end_ms=end,
            final_start_ms=0,
            final_end_ms=end,
        )
        correction = {
            "schema_version": "final-review-audit.v1",
            "status": "CLEAN",
            "applied_count": 0,
            "findings": [],
            "boundary_semantic_review": source_review,
        }
        authority = {"applied": [], "final_review_audit": correction}
        fixture.write(path, authority)
        callback = pipeline._build_exact_final_review_callback(
            spec={
                "date": "2026-10-17",
                "selection_hook": "synthetic closing",
                "selection_scorecard": {
                    "status": "VALID",
                    "dimensions": {"self_contained": 4, "comedic_payoff": 4},
                },
            },
            out_root=root,
            cid=cid,
            padded=source,
            adapters=adapters,
            authoritative_chat=[],
            clip_context=context,
            final_review_audit=correction,
            verify_confusable_entity=direct,
            truth_ownership=None,
            screen_read_probe=None,
            source_boundary_review=source_review,
        )
        kwargs = dict(
            cid=cid,
            out_root=root,
            final_start=0,
            final_end=end,
            recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=final),
            chat_authority_audit=authority,
            chat_authority_path=path,
            run_exact_final_review=callback,
            clip_context=context,
        )
        if outside or current_supported:
            with pytest.raises(SystemExit):
                finalizer._run_exact_final_review_gate(**kwargs)
        else:
            gate = finalizer._run_exact_final_review_gate(**kwargs)
            validate_final_review_release(
                gate, expected_srt_sha256="sha256:" + fixture.sha(final.read_bytes())
            )
    disk = json.loads(path.read_bytes())
    text = final.read_text()
    if current_supported:
        assert "说能发挥吧" in text
    else:
        assert "说你的坏话吧" in text
    assert "今天讲到这里" in text
    if outside:
        assert pending_findings(text, disk)
    fixture.write(root / "OBSERVED-CALLS.json", calls)
    # Fresh interpreter uses the real release/typed persistence consumers and
    # cannot silently repeat the network or mutate the stored final evidence.
    child_script = """import json,sys,socket
from pathlib import Path
from src.autoslice.final_review_contract import validate_final_review_release
from src.autoslice.span_finding_obligations import repair_chain, validate_span_repair, pending_findings
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden'))
root=Path(sys.argv[1]);a=json.loads((root/'authority.json').read_bytes());text=(root/'current.srt').read_text()
repairs=repair_chain(a)
for r in repairs: validate_span_repair(r)
blocked=sys.argv[2]=='yes'
try:validate_final_review_release(a['final_review_audit'])
except ValueError:
 assert blocked;print('COLD_BLOCK_PRESERVED')
else:
 assert not blocked;print('COLD_CLEAN_VALIDATED')
"""
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            child_script,
            str(root),
            "yes" if outside or current_supported else "no",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert all(p.read_bytes() == b for p, b in before.items())
    fixture.write(
        root / "COLD-PROCESS.json",
        {
            "stdout": run.stdout,
            "returncode": run.returncode,
            "consumer": "ordinary typed history/release validator",
            "real_provider_calls": 0,
        },
    )
