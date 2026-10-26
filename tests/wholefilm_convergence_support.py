"""Synthetic lower-provider inputs for the real ordinary finalizer.

No historical package, human label, credential, SSH, or external provider is
used. The producer callback, adjudicator, native verifier, disk caches,
SemanticAttemptRecorder, typed repairs and release validator stay real.
"""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from unittest.mock import patch
import wave


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def timestamp(ms):
    seconds, ms = divmod(ms, 1000)
    return f"00:{seconds // 60:02}:{seconds % 60:02},{ms:03}"


def make_srt(texts):
    return (
        "\n\n".join(
            f"{i}\n{timestamp((i - 1) * 3000)} --> {timestamp((i - 1) * 3000 + 2200)}\n{text}"
            for i, text in enumerate(texts, 1)
        )
        + "\n"
    )


def prepare(root, count=5):
    root.mkdir(parents=True, exist_ok=False)
    text = make_srt(["一百五十秒"] * count + ["今天讲到这里"])
    (root / "current.srt").write_text(text)
    (root / "original.srt").write_text(text)
    length = (count + 2) * 3000
    with wave.open(str(root / "source.wav"), "wb") as f:
        f.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        f.writeframes(b"".join(struct.pack("<h", i % 2000 - 1000) for i in range(length * 8)))
    write(
        root / "parameters.json",
        {"count": count, "source_sha256": sha((root / "source.wav").read_bytes())},
    )


def run(root, *, stop_after=None, mode="repair", discovery="first_wrong"):
    from src.autoslice import producer_text_pipeline as pipeline
    from src.autoslice import producer_package_finalization as finalizer
    from src.autoslice import entity_audio_verifier as ordinary
    from src.autoslice.clip_context import _canonical_json_sha256
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.final_review_contract import validate_final_review_release

    root = Path(root).resolve()
    parameters = json.loads((root / "parameters.json").read_bytes())
    count = parameters["count"]
    cid = "synthetic-wholefilm-convergence"
    source = root / "source.wav"
    text_path = root / "current.srt"
    authority_path = root / "authority.json"
    end = count * 3000 + 2200
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": cid,
        "recording_date": "2026-10-17",
        "selection_hook": "synthetic controlled sequence",
        "pieces": [
            {
                "start_ms": 0,
                "end_ms": end,
                "source_media_sha256": "sha256:" + parameters["source_sha256"],
            }
        ],
        "whole_clip_draft_srt": (root / "original.srt").read_text(),
        "whole_clip_draft_srt_sha256": "sha256:" + sha((root / "original.srt").read_bytes()),
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    events = []

    def event(kind, **data):
        row = {"kind": kind, **data}
        events.append(row)
        with (root / "calls.jsonl").open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def boundary_answer(prompt, *, source_stage=False):
        event("boundary_model", prompt_sha256=sha(prompt.encode()))
        return json.dumps(
            {
                "syntax_complete": True,
                "story_closed": True,
                "next_topic_separated": True,
                "recommended_end_cue_index": count + 1,
                "evidence_cue_indexes": [count, count + 1, *([count + 2] if source_stage else [])],
                "reason_codes": [],
                "summary": "synthetic closing sentence is complete",
            }
        )

    def model(prompt):
        event("text_model", prompt_sha256=sha(prompt.encode()))
        if any(
            x in prompt for x in ("字幕选字裁决", "字幕文字闭集裁决", "疑似拼音", "最终文字法官")
        ):
            if (
                stop_after is not None
                and text_path.read_text().count("一百五十秒") <= count - stop_after
            ):
                from src.autoslice.semantic_retry_progress import CallNotDispatched

                event("undispatched_capacity", prompt_sha256=sha(prompt.encode()))
                raise CallNotDispatched("SYNTHETIC_PROVIDER_NOT_ADMITTED")
            choice = (
                "NEITHER" if mode == "uncertain" else "CURRENT" if mode == "keep" else "PROPOSED"
            )
            return json.dumps(
                {
                    "choice": choice,
                    "needs_audio": mode in {"uncertain", "incompatible"}
                    or not any(x in prompt for x in ("yi bai wu shi shou", "yi bai wu shi miao")),
                    "reason": "synthetic independent judgment",
                    "current_utterance_supported": choice == "CURRENT",
                    "current_utterance_support_reason": "synthetic full original hearing"
                    if choice == "CURRENT"
                    else "not supported",
                }
            )
        if "本次不授权改字、删字或听音调度" in prompt:
            agenda = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps(
                {
                    "findings": [
                        {
                            "cue": r["cue"],
                            "content_risk": "meaning_reversal",
                            "risk_basis": "synthetic unit changes the meaning",
                        }
                        for r in agenda
                    ]
                }
            )
        # Boundary model schema and word/discovery protocols remain distinct.
        if "recommended_end_cue_index" in prompt and "syntax_complete" in prompt:
            return boundary_answer(prompt)
        texts = [c.text for c in parse_srt_cues(text_path.read_text())]
        wrong = [i for i, t in enumerate(texts, 1) if t == "一百五十秒"]
        if discovery == "empty":
            return '{"findings":[]}'
        targets = wrong[:1]
        if mode == "keep":
            targets = [1]
        event("discovery", cue_indexes=targets, input_srt_sha256=sha(text_path.read_bytes()))
        return json.dumps(
            {
                "findings": [
                    {
                        "cue": i,
                        "kind": "context",
                        "suspect": "秒",
                        "replacement": "首",
                        "proposed_full_cue": "一百五十首",
                        "repair_class": "phonetic",
                        "content_risk": "meaning_reversal",
                        "risk_basis": "synthetic incorrect unit",
                        "why": "synthetic source-bound new issue",
                    }
                    for i in targets
                ]
            },
            ensure_ascii=False,
        )

    model.cpa_cache_identity = {"models": ["synthetic-wholefilm-cpa"], "effort": "high"}

    def observe(**kw):
        event("audio_provider", request_sha256=kw["request"]["request_sha256"])
        job = kw["job_dir"]
        prompt, _, _ = ordinary._entity_observation_prompt(
            request=kw["request"],
            candidates=kw["candidates"],
            recording_date=kw["recording_date"],
            timely_context=kw["timely_context"],
        )
        pp = job / "prompt.md"
        pp.write_text(prompt)
        heard = (
            "yi bai wu shi miao"
            if mode == "keep"
            else "ling wai yi ju"
            if mode == "incompatible"
            else "yi bai wu shi shou"
        )
        raw = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "heard_pinyin": heard,
            "target_audible": True,
            "uncertain_positions": [],
            "syllable_count": len(heard.split()),
            "confidence": 0.9,
            "reason": "synthetic acoustic observation, not human speech",
        }
        rp = job / "verdict.raw.json"
        write(rp, raw)
        return ordinary._EntityProviderOutcome(
            raw, "agy", ordinary.ENTITY_AUDIO_MODEL, pp, rp, None, None, []
        )

    def unused(*a, **kw):
        raise AssertionError("UNEXPECTED_SOURCE_TRANSCRIPTION")

    adapters = pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=unused,
        build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused,
        profile_asset_file=lambda _: root / "absent-optional-asset",
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=lambda: None,
        topic_graph_expected_sha256=lambda: "",
    )
    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {"AUTOSLICE_BASE": str(root / "runtime"), "AUTOSLICE_PROVIDER_CONCURRENCY": "1"},
            )
        )
        stack.enter_context(patch.object(ordinary, "_observe_entity_audio", observe))
        stack.enter_context(patch.object(pipeline, "_build_final_review_llm_call", lambda: model))
        stack.enter_context(patch.object(pipeline, "_build_pronoun_audit_llm_call", lambda: model))
        verifier = ordinary.build_local_audio_entity_verifier(
            source_media=source,
            output_dir=root,
            recording_date="2026-10-17",
            source_duration_ms=(count + 2) * 3000,
            agy_bin="/usr/bin/true",
        )
        if authority_path.exists():
            authority = json.loads(authority_path.read_bytes())
            correction = authority["final_review_audit"]["correction_pass"]
        else:
            source_review = pipeline.review_final_boundary_semantics(
                cues=parse_srt_cues(
                    (root / "original.srt").read_text()
                    + "\n"
                    + f"{count + 2}\n{timestamp((count + 1) * 3000)} --> {timestamp((count + 1) * 3000 + 2200)}\n下个话题\n"
                ),
                boundary_target_ms=end,
                candidate_id=cid,
                selection_hook="synthetic complete ending",
                selection_scorecard={
                    "status": "VALID",
                    "dimensions": {"self_contained": 4, "comedic_payoff": 4},
                },
                structured_context="",
                candidate_context="",
                boundary_max_forward_ms=30000,
                llm_call=lambda prompt: boundary_answer(prompt, source_stage=True),
                extract_json=pipeline.extract_json_object,
            )
            from src.autoslice.boundary_endpoint_binding import bind_final_semantic_endpoint

            source_cues = parse_srt_cues(
                (root / "original.srt").read_text()
                + "\n"
                + f"{count + 2}\n{timestamp((count + 1) * 3000)} --> {timestamp((count + 1) * 3000 + 2200)}\n下个话题\n"
            )
            source_review, _ = bind_final_semantic_endpoint(
                semantic_review=source_review,
                cues=source_cues,
                closure_cue=source_cues[count],
                snapped_end_ms=end,
                final_start_ms=0,
                final_end_ms=end,
            )
            write(root / "source-boundary.json", source_review)
            correction = {
                "schema_version": "final-review-audit.v1",
                "status": "CLEAN",
                "applied_count": 0,
                "findings": [],
                "boundary_semantic_review": source_review,
            }
            authority = {"applied": [], "final_review_audit": correction}
            write(authority_path, authority)
        callback = pipeline._build_exact_final_review_callback(
            spec={
                "date": "2026-10-17",
                "selection_hook": "synthetic complete ending",
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
            verify_confusable_entity=verifier,
            truth_ownership=None,
            screen_read_probe=None,
            source_boundary_review=json.loads((root / "source-boundary.json").read_bytes()),
        )
        status = "UNKNOWN"
        error = None
        try:
            audit = finalizer._run_exact_final_review_gate(
                cid=cid,
                out_root=root,
                final_start=0,
                final_end=end,
                recut=finalizer.CurrentTextReviewArtifacts(subtitle_path=text_path),
                chat_authority_audit=authority,
                chat_authority_path=authority_path,
                run_exact_final_review=callback,
                clip_context=context,
            )
            validate_final_review_release(
                audit, expected_srt_sha256="sha256:" + sha(text_path.read_bytes())
            )
            status = "PASS"
        except (SystemExit, KeyboardInterrupt) as exc:
            status = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "BLOCK"
            error = str(exc)
        disk = json.loads(authority_path.read_bytes())
        last = disk.get("final_review_audit") or {}
        result = {
            "status": status,
            "error": error,
            "pid": os.getpid(),
            "text": text_path.read_text(),
            "new_audio_provider_calls": sum(e["kind"] == "audio_provider" for e in events),
            "new_text_model_calls": sum(e["kind"] == "text_model" for e in events),
            "review_pass_pressure": last.get("cpa_review_resource_pressure"),
            "self_heal": {
                "status": (disk.get("exact_final_cpa_self_heal") or {}).get("status"),
                "pass_count": len((disk.get("exact_final_cpa_self_heal") or {}).get("passes", [])),
            },
            "pending_findings": [
                {
                    "cue": r.get("cue_index"),
                    "status": (r.get("exact_release_adjudication") or {}).get("status"),
                    "policy_branch": (r.get("exact_release_adjudication") or {}).get(
                        "policy_branch"
                    ),
                }
                for r in last.get("findings", [])
            ],
            "real_provider_calls": 0,
        }
        write(root / f"result-{os.getpid()}.json", result)
        return result


def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("root", type=Path)
    p.add_argument("--prepare", action="store_true")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--stop-after", type=int)
    p.add_argument("--mode", default="repair")
    p.add_argument("--discovery", default="first_wrong")
    a = p.parse_args()
    violations = []

    def guard(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            violations.append(event)
            raise AssertionError("NO_EXTERNAL_PROVIDER")
        if (
            event == "open"
            and isinstance(args[0], (str, bytes, os.PathLike))
            and os.fsdecode(args[0]).endswith(".env")
        ):
            violations.append("credentials")
            raise AssertionError("NO_REAL_CREDENTIAL_READ")
        if event == "subprocess.Popen" and any(
            "ssh" == Path(str(x)).name for x in (args[1] if isinstance(args[1], list) else [])
        ):
            violations.append("ssh")
            raise AssertionError("NO_SSH")

    sys.addaudithook(guard)
    if a.prepare:
        prepare(a.root, a.count)
    result = run(a.root, stop_after=a.stop_after, mode=a.mode, discovery=a.discovery)
    result["forbidden_attempts"] = violations
    print(json.dumps(result, ensure_ascii=False))
    assert not violations


if __name__ == "__main__":
    main()
