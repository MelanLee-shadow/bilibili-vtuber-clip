"""Standard-library smoke for the installed production Python (no pytest needed).

Runs the *real* regression CLI, prepared reviewer, storage, package gate and
cold consumer. Only credential configuration, ASR response, native HTTP and
external text-CPA boundaries are synthetic. No real plan is ever authorized.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def seal(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(seal(value))


class NativeBatch:
    def __init__(self):
        self.calls = []
        self.task = None
        self.hearing = None

    def factory(self, resolver):
        owner = self

        class Transport:
            def request(self, method, path, body, *, route, timeout_s):
                assert resolver(route) == "fictional-key-fixture"
                return owner.request(method, path, body)

        return Transport()

    def request(self, method, path, body):
        from src.autoslice.argon_escalation_witness import HttpResponse

        self.calls.append(
            {
                "method": method,
                "path": path,
                "body_sha256": hashlib.sha256(body).hexdigest() if body else None,
            }
        )
        if method == "POST":
            item = json.loads(body)["batch"]["inputConfig"]["requests"]["requests"][0]
            self.task = item["metadata"]["key"]
            prompt = item["request"]["contents"][0]["parts"][1]["text"]
            assert "CURRENT" not in prompt and "PROPOSED" not in prompt
            marker = json.loads(prompt[prompt.rfind("{") :])
            self.hearing = {
                "schema_version": "argon-blind-hearing.v1",
                **marker,
                "status": "OBSERVED",
                "transcript": "一百五十首",
                "uncertainties": [],
                "speech_role": "unknown",
                "reason": None,
            }
        state = "BATCH_STATE_PENDING" if method == "POST" else "BATCH_STATE_SUCCEEDED"
        response = {
            "name": "batches/synthetic-common-smoke",
            "metadata": {
                "name": "batches/synthetic-common-smoke",
                "model": "models/gemini-4-argon",
                "displayName": "argon-" + self.task,
                "state": state,
            },
            "done": method != "POST",
        }
        if method != "POST":
            payload = {
                "modelVersion": "gemini-4-argon",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [{"text": json.dumps(self.hearing, ensure_ascii=False)}]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "totalTokenCount": 20},
            }
            response["metadata"]["output"] = {
                "inlinedResponses": {
                    "inlinedResponses": [{"metadata": {"key": self.task}, "response": payload}]
                }
            }
        return HttpResponse(200, seal(response))


def scenario(root: Path, mask: int, code_identity: str, *, exercise_successor=False) -> dict:
    from scripts import subtitle_regression_eval as cli, produce_slice_package as producer
    from src.autoslice import producer_text_pipeline as pipeline, entity_audio_verifier as ordinary
    from src.autoslice import (
        argon_escalation_runtime as runtime,
        argon_regression_comparison as comparison,
    )
    from src.autoslice.clip_context import _canonical_json_sha256
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    root.mkdir(mode=0o700)
    source = root / "source.wav"
    with wave.open(str(source), "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(b"".join(struct.pack("<h", i % 2000 - 1000) for i in range(48000)))
    text = (
        "1\n00:00:01,000 --> 00:00:02,000\n一百五十秒\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\n她说第二句\n\n"
        "3\n00:00:04,300 --> 00:00:04,800\n她说第三句\n"
    )
    finding = {
        "cue_index": 1,
        "suspect": "秒",
        "suggestion": "首",
        "proposed_full_cue": "一百五十首",
        "repair_class": "phonetic",
        "why": "synthetic unclear unit",
    }
    context = {
        "schema_version": "lidousha-clip-context.v1",
        "candidate_id": "synthetic-common-runtime",
        "recording_date": "2026-10-16",
        "selection_hook": "synthetic source-bound ambiguity",
        "pieces": [
            {"start_ms": 0, "end_ms": 6000, "source_media_sha256": "sha256:" + digest(source)}
        ],
        "whole_clip_draft_srt": text,
        "whole_clip_draft_srt_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "structured_chat": [],
        "topic_resolution": {},
        "session_topic_authorities": [],
        "speech_memory": {"entries": []},
        "retrieval_budget": {"whole_clip_transcript_truncated": False},
        "mutation_authorized": False,
    }
    context["context_sha256"] = _canonical_json_sha256(context)
    observed, prompts, pronoun_prompts = [], [], []
    native = NativeBatch()
    allow_upgrade = not exercise_successor
    output = root / "comparison"

    def judge(prompt):
        prompts.append(prompt)
        if "## 新增 Argon 独立盲听证据" in prompt:
            return json.dumps(
                {
                    "choice": "PROPOSED",
                    "candidate_id": "PROPOSAL",
                    "needs_audio": False,
                    "reason": "synthetic CPA chooses after consuming native evidence",
                    "current_utterance_supported": False,
                    "current_utterance_support_reason": "synthetic current unsupported",
                }
            )
        if any(x in prompt for x in ("字幕文字闭集裁决", "疑似拼音", "最终文字法官")):
            return json.dumps(
                {
                    "choice": "CURRENT",
                    "needs_audio": True,
                    "reason": "synthetic needs listening",
                    "current_utterance_supported": False,
                    "current_utterance_support_reason": "not established",
                    "argon_escalation": {
                        "reading_chat": allow_upgrade,
                        "reason": "synthetic diagnosis",
                    },
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
                            "risk_basis": "synthetic unresolved ordinary finding",
                        }
                        for r in agenda
                    ]
                }
            )
        final = output / "sample/final.srt"
        if final.exists() and "一百五十首" in final.read_text():
            return '{"findings":[]}'
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
                        "risk_basis": "synthetic wrong unit",
                        "why": "synthetic unclear unit",
                    }
                ]
            },
            ensure_ascii=False,
        )

    judge.cpa_cache_identity = {"models": ["synthetic-common-cpa"], "effort": "high"}

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
                        "reason": "synthetic neutral pronoun decision",
                    }
                    for i in (2, 3)
                ]
            }
        )

    def audio(**kwargs):
        observed.append(dict(kwargs["request"]))
        job = kwargs["job_dir"]
        prompt, _, _ = ordinary._entity_observation_prompt(
            request=kwargs["request"],
            candidates=kwargs["candidates"],
            recording_date=kwargs["recording_date"],
            timely_context=kwargs["timely_context"],
        )
        pp = job / "prompt.md"
        pp.write_text(prompt)
        raw = {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "heard_pinyin": "yi bai wu shi shou",
            "target_audible": True,
            "uncertain_positions": [],
            "syllable_count": 5,
            "confidence": 0.8,
            "reason": "synthetic original audio receipt",
        }
        rp = job / "verdict.raw.json"
        write(rp, raw)
        return ordinary._EntityProviderOutcome(
            raw, "agy", ordinary.ENTITY_AUDIO_MODEL, pp, rp, None, None, []
        )

    # The code's ordinary binary resolver gets an inert explicit fixture, while
    # the full verifier/caching/probe and independent-context wrapper stay real.
    original_builder = ordinary.build_local_audio_entity_verifier

    def builder(**kwargs):
        return original_builder(
            **{**kwargs, "agy_bin": "/usr/bin/true", "model": ordinary.ENTITY_AUDIO_MODEL}
        )

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "AUTOSLICE_BASE": str(root / "state"),
                    "AUTOSLICE_PROVIDER_CONCURRENCY": "1",
                    "GEMINI_PAID_BACKUP_DEV_EXCEPTION": "0",
                    "GEMINI_PAID_BACKUP_DAILY_CAP": "0",
                    "AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES": "0",
                },
            )
        )
        stack.enter_context(patch.object(ordinary, "_observe_entity_audio", audio))
        stack.enter_context(patch.object(ordinary, "build_local_audio_entity_verifier", builder))
        stack.enter_context(
            patch.object(
                runtime, "load_credentials", lambda: {"GEMINI_API_KEY": "fictional-key-fixture"}
            )
        )
        stack.enter_context(patch.object(runtime, "ArgonHttpTransport", native.factory))
        stack.enter_context(patch.object(pipeline, "_build_final_review_llm_call", lambda: judge))
        stack.enter_context(
            patch.object(pipeline, "_build_pronoun_audit_llm_call", lambda: pronoun)
        )
        stack.enter_context(
            patch.object(
                producer, "profile_asset_file", lambda _: root / "absent-optional-asset.json"
            )
        )
        stack.enter_context(patch.object(producer, "_topic_graph_disabled", lambda: True))
        stack.enter_context(patch.object(producer, "_review_glossary", lambda: ""))
        baseline = root / "baseline/sample"
        baseline.mkdir(parents=True)
        verifier = builder(
            source_media=source,
            output_dir=baseline,
            recording_date="2026-10-16",
            source_duration_ms=6000,
        )
        _, initial = adjudicate_context_finding(
            text,
            finding,
            entity_verifier=verifier,
            clip_context=context,
            source_media_timeline_offset_ms=0,
            judge_llm_call=judge,
            require_complete_utterance_support=True,
        )
        assert initial["verdict"]["status"] == "OBSERVED" and not native.calls
        inputs = root / "inputs"
        inputs.mkdir()
        source_review = {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "BLOCK",
            "review_scope": "source_full_window",
            "reason_codes": ["SYNTHETIC_BOUNDARY_NOT_ACCEPTED"],
        }
        correction = {
            "schema_version": "final-review-audit.v1",
            "status": "CLEAN",
            "applied_count": 0,
            "findings": [],
            "boundary_semantic_review": source_review,
        }
        docs = {
            "record": {
                "start_ms": 0,
                "end_ms": 6000,
                "subtitle_path": str(inputs / "srt.srt"),
                "clip_context_payload_sha256": context["context_sha256"],
                "subtitle_audio_correspondence": {"intro_offset_ms": 1000},
                "boundary_audit": {"boundary_semantic_review": source_review},
            },
            "authority": {
                "final_review_audit": {**correction, "correction_pass": correction},
                "applied": [],
            },
            "context": context,
            "provenance": {},
        }
        package = {
            "candidate_id": "synthetic-common-runtime",
            "intro_offset_ms": 1000,
            "media_path": str(source),
            "media_sha256": digest(source),
        }
        for kind, value in docs.items():
            p = inputs / (kind + ".json")
            write(p, value)
            package[kind + "_path"] = str(p)
            package[kind + "_sha256"] = digest(p)
        p = inputs / "srt.srt"
        p.write_text(text)
        package.update(srt_path=str(p), srt_sha256=digest(p))
        case = {
            "id": "synthetic-target",
            "package": "sample",
            "candidate_id": package["candidate_id"],
            "status": "ready",
            "cue_ids": [1],
            "body_window_ms": [1000, 2000],
            "finished_window_ms": [2000, 3000],
            "wrong_text": "一百五十秒",
            "source": {"quote": "synthetic-only scorer contract, no human annotation"},
            "expected": {"correct_text": "一百五十首", "required_severity": "critical"},
        }
        cases = root / "cases.json"
        write(
            cases,
            {
                "schema_version": "subtitle-regressions.v1",
                "baseline_commit": "synthetic-base",
                "clock_rule": "explicit",
                "packages": {"sample": package},
                "cases": [case],
            },
        )
        (baseline / "original.srt").write_text(text)
        write(
            baseline / "run.json",
            {
                "candidate_id": package["candidate_id"],
                "input_drift": [],
                "input_hashes": {
                    package[k + "_path"]: package[k + "_sha256"]
                    for k in ("record", "srt", "authority", "media")
                },
                "reviews": [
                    {
                        "discovery": {"status": "COMPLETE"},
                        "findings": [{"cue_index": 1, "content_risk": "meaning_reversal"}],
                        "evaluation_input_cues": [
                            {"index": 1, "start_ms": 1000, "end_ms": 2000, "text": "一百五十秒"}
                        ],
                    }
                ],
                "final_cues": [
                    {"index": 1, "start_ms": 1000, "end_ms": 2000, "text": "一百五十秒"}
                ],
                "would_release": False,
            },
        )
        frozen = root / "frozen"
        assert (
            cli.main(
                [
                    "--prepare-argon-comparison",
                    str(baseline.parent),
                    "--cases",
                    str(cases),
                    "--case",
                    "synthetic-target",
                    "--output",
                    str(frozen),
                    "--code-identity",
                    code_identity,
                ]
            )
            == 0
        )
        argv = [
            "--comparison-plan",
            str(frozen / "plan.json"),
            "--comparison-grant",
            str(frozen / "grant-required.json"),
            "--case",
            "synthetic-target",
            "--output",
            str(output),
            "--runtime-root",
            str(root / "provider-runtime"),
            "--code-root",
            str(ROOT),
            "--code-identity",
            code_identity,
        ]
        try:
            cli.main(argv)
        except ValueError as e:
            assert str(e) == "COMPARISON_COST_NOT_AUTHORIZED"
        else:
            raise AssertionError("zero grant not rejected")
        assert not output.exists() and not native.calls
        plan = comparison.read_plan(frozen / "plan.json")
        auth = root / "SYNTHETIC-ONLY-GRANT.json"
        write(
            auth,
            {
                "schema_version": comparison.GRANT_SCHEMA,
                "status": "AUTHORIZED",
                "authority": "SYNTHETIC_TEST_ONLY_NOT_USER_AUTHORIZATION",
                "plan_sha256": plan["plan_sha256"],
                "authorized_case_ids": ["synthetic-target"],
                "max_usd": str(comparison.PER_REQUEST_USD),
                "existing_subscription_cpa_only": True,
                "ordinary_new_audio_calls": 0,
            },
        )
        argv[argv.index("--comparison-grant") + 1] = str(auth)
        assert cli.main(argv) == 1
        if exercise_successor:
            assert not native.calls
            old_identity = (output / "invocation.json").read_bytes()
            old_package_identity = (output / "sample/comparison-invocation.json").read_bytes()
            old_run = (output / "sample/run.json").read_bytes()
            old_text = (output / "sample/final.srt").read_bytes()
            successor = [*argv, "--resume-comparison", "--comparison-code-successor", code_identity]
            successor[successor.index("--code-identity") + 1] = (
                code_identity + "-synthetic-code-revision"
            )
            assert cli.main(successor) == 1
            assert (output / "invocation.json").read_bytes() == old_identity
            assert (
                output / "sample/comparison-invocation.json"
            ).read_bytes() == old_package_identity
            assert (output / "sample/run-history-001-run.json").read_bytes() == old_run
            assert (output / "sample/final.srt").read_bytes() == old_text
            assert not native.calls and not (frozen / "cost-reservations").exists()
            active, chain = comparison.effective_invocation(output)
            assert (
                len(chain) == 1
                and active["code_identity"] == code_identity + "-synthetic-code-revision"
            )
            result = {
                "status": "PASS",
                "actual_cli_and_package_gate": True,
                "original_invocation_preserved": True,
                "original_package_identity_preserved": True,
                "real_provider_calls": 0,
                "code_only_revision_forced_new_generation": False,
                "effective_identity": active,
                "immutable_code_edge": chain[0],
            }
            write(root / "SUCCESSOR-RESULT.json", result)
            return result
        first = json.loads((output / "sample/run.json").read_bytes())
        write(root / "FIRST.json", first)
        assert [x["method"] for x in native.calls] == ["POST"], first
        before = (output / "invocation.json").read_bytes()
        assert cli.main(argv + ["--resume-comparison"]) in (0, 1)
        second = json.loads((output / "sample/run.json").read_bytes())
        write(root / "SECOND.json", second)
        assert second["final_cues"][0]["text"] == "一百五十首", second
        assert [x["method"] for x in native.calls] == ["POST", "GET"]
        assert (output / "invocation.json").read_bytes() == before
        assert not (output / "sample/speaker-attribution").exists()
        assert any("## 新增 Argon 独立盲听证据" in p for p in prompts)
        assert len(observed) == 1, "comparison must use saved ordinary audio only"
        # Explicit GWS new-code mode cannot take over this comparison output.
        try:
            cli.main(
                [
                    "--worker",
                    "--resume-existing",
                    "--code-root",
                    str(ROOT),
                    "--code-identity",
                    "synthetic-other-code",
                    "--cases",
                    str(cases),
                    "--runtime-root",
                    str(root / "provider-runtime"),
                    "--output",
                    str(output),
                ]
            )
        except ValueError as e:
            assert str(e) == "COMPARISON_REQUIRES_SAME_INVOCATION_RESUME"
        else:
            raise AssertionError("comparison switched execution mode")
        write(root / "CPA-PROMPTS.json", prompts)
        write(root / "HTTP-RECEIPTS.json", native.calls)
        result = {
            "status": "PASS",
            "umask": f"{mask:03o}",
            "ordinary_gate_unmocked": True,
            "new_normal_audio_fake_calls": 1,
            "native_fake_methods": [x["method"] for x in native.calls],
            "s2_effect_comparison": False,
            "actual_final_text": second["final_cues"][0]["text"],
            "would_release": second["would_release"],
            "first_reason": first["error"],
            "second_reason": second["error"],
            "actual_invocation": json.loads(before),
            "real_provider_calls": 0,
            "real_credentials_read": False,
            "actual_bill_usd": None,
        }
        write(root / "RESULT.json", result)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-identity", required=True)
    parser.add_argument("--frozen-plan", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    violations = []

    def guard(event, params):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            violations.append(event)
            raise RuntimeError("NO_EXTERNAL_PROVIDER")
        if event == "open" and isinstance(params[0], (str, bytes, os.PathLike)):
            p = os.fsdecode(params[0])
            if p.endswith(".env") and not Path(p).absolute().is_relative_to(output):
                violations.append("real-env")
                raise RuntimeError("NO_REAL_CREDENTIALS")

    sys.addaudithook(guard)
    from scripts import subtitle_regression_eval as cli
    from src.autoslice import producer_speaker_attribution  # noqa: F401

    actual_denial = None
    if args.frozen_plan:
        try:
            cli.main(
                [
                    "--comparison-plan",
                    str(args.frozen_plan),
                    "--comparison-grant",
                    str(args.frozen_plan.parent / "grant-required.json"),
                    "--output",
                    str(output / "real-MUST-NOT-EXIST"),
                    "--code-root",
                    str(ROOT),
                    "--code-identity",
                    args.code_identity,
                ]
            )
        except ValueError as e:
            assert str(e) == "COMPARISON_COST_NOT_AUTHORIZED"
            actual_denial = str(e)
        else:
            raise AssertionError("real unfunded plan not rejected")
        assert not (output / "real-MUST-NOT-EXIST").exists()
    results = []
    for mask in (0o022, 0o077):
        previous = os.umask(mask)
        try:
            results.append(scenario(output / f"umask-{mask:03o}", mask, args.code_identity))
        finally:
            os.umask(previous)
    assert not violations
    result = {
        "status": "PASS",
        "code_identity": args.code_identity,
        "code_root": str(ROOT),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "actual_plan_denial": actual_denial,
        "scenarios": results,
        "external_or_credential_attempts": violations,
        "real_provider_calls": 0,
        "paid_calls": 0,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    write(output / "RESULT.json", result)
    print(
        json.dumps(
            {
                "status": "PASS",
                "umasks": ["022", "077"],
                "real_provider_calls": 0,
                "code_root": str(ROOT),
            }
        )
    )


if __name__ == "__main__":
    main()
