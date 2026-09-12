#!/usr/bin/env python3
"""Frozen native-ASR / fresh CPA replay with exact whole-cue scope.

This does not call MOSS/MAI, transfer credentials, edit production state, or
claim a new cold audio benchmark. All old failures and out-of-scope crops stay
in the denominator. Reference text is opened only by the separate score stage.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_astra_native_audio as transport
from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
from src.autoslice.foreign_span_witness import _transcript_witness
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.local_asr_target_evidence import exact_target_evidence, digest
from src.autoslice.subtitle_draft_preparation import _asr_ts

INPUT = Path(
    "/Users/op/Project/.worktrees/vtuber-slice-agy-replacement-paired-20260909/reports/astra-native-audio-20260909"
)
OUT = ROOT / "reports/native-target-scope-20260909/frozen-audio-replay"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def render(cues, texts):
    return (
        "\n\n".join(
            f"{n}\n{_asr_ts(c.start_ms)} --> {_asr_ts(c.end_ms)}\n{texts[n]}"
            for n, c in enumerate(cues, 1)
        )
        + "\n"
    )


def prepare():
    inputs = load(INPUT / "inputs.json")
    save(
        OUT / "intent.json",
        {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_head": os.popen("git rev-parse HEAD").read().strip(),
            "input_root": str(INPUT),
            "candidate_ids": list(inputs),
            "requested_cpa_model": "gpt-6-astra",
            "effort": "medium",
            "new_audio_calls": 0,
            "maximum_new_cpa_calls": 36,
            "maximum_audio_targets_per_source": 3,
            "comparison": "same frozen first-low full outputs; same frozen native audio observations; actual shared closed-set CPA",
            "scope_rule": "exact target must contain exactly one complete BCUT/current cue; no tolerance or character slicing",
            "reference_in_generation": False,
            "full_producer_or_package_claimed": False,
            "source_code_sha256": {
                name: sha((ROOT / name).read_bytes())
                for name in (
                    "src/autoslice/foreign_span_witness.py",
                    "src/autoslice/acoustic_witness_adjudication.py",
                    "src/autoslice/local_asr_target_evidence.py",
                    "scripts/evaluate_native_target_replay.py",
                )
            },
        },
    )
    print("FROZEN_REPLAY_INTENT_LOCKED", flush=True)


def run(cid, provider):
    intent = load(OUT / "intent.json")
    for name, digest0 in intent["source_code_sha256"].items():
        if sha((ROOT / name).read_bytes()) != digest0:
            raise RuntimeError("REPLAY_SOURCE_CHANGED")
    folder = OUT / cid / provider
    save(
        folder / "attempt.json",
        {"stage": "frozen_native_fresh_cpa", "provider": provider, "candidate_id": cid},
    )
    data = load(INPUT / "inputs.json")[cid]
    original = (INPUT / cid / "prepared.srt").read_text()
    common = (INPUT / cid / "first-low/output.srt").read_text()
    raw = parse_srt_cues(original)
    cues = parse_srt_cues(common)
    assert [(c.start_ms, c.end_ms) for c in raw] == [(c.start_ms, c.end_ms) for c in cues]
    texts = {n: c.text for n, c in enumerate(cues, 1)}
    decisions = []
    for target in data["targets"]:
        tid = target["target_id"]
        start = target["start_ms"]
        end = target["end_ms"]
        selected = [n for n, c in enumerate(cues, 1) if max(c.start_ms, start) < min(c.end_ms, end)]
        base = {"target_id": tid, "start_ms": start, "end_ms": end, "input_cue_indexes": selected}
        if (
            len(selected) != 1
            or not start <= cues[selected[0] - 1].start_ms < cues[selected[0] - 1].end_ms <= end
        ):
            decisions.append({**base, "status": "SCOPE_NOT_EQUAL_WHOLE_CUE", "new_cpa_calls": 0})
            continue
        n = selected[0]
        source = INPUT / cid / tid / "exact" / provider
        receipt = load(source / "receipt.json")
        response_path = source / "response.json"
        response = load(response_path)
        if response.get("status") != "OK":
            decisions.append(
                {
                    **base,
                    "status": "PRIOR_PROVIDER_FAILED",
                    "reason": response.get("reason_code"),
                    "new_cpa_calls": 0,
                }
            )
            continue
        assert receipt["response_file_sha256"] == sha(response_path.read_bytes())
        crop = load(source.parent / "crop.json")
        audio = (source.parent / "audio.mp3").read_bytes()
        assert sha(audio) == crop["audio_sha256"] == receipt["audio_sha256"]
        assert (crop["crop_start_ms"], crop["crop_end_ms"]) == (start, end)
        assert crop["full_decode"] == "PASS"
        meta = response["metadata"]
        assert meta["provider"] == provider
        try:
            evidence = exact_target_evidence(
                meta,
                audio=audio,
                source_sha256=data["item"]["audio_sha256"],
                start_ms=start,
                end_ms=end,
            )
            native = evidence["native_segments"]
            assert not any(a["end_ms"] > b["start_ms"] for a, b in zip(native, native[1:]))
        except (ValueError, AssertionError) as exc:
            decisions.append(
                {
                    **base,
                    "status": "NATIVE_CONTRACT_UNUSABLE",
                    "exception_type": type(exc).__name__,
                    "new_cpa_calls": 0,
                }
            )
            continue
        if evidence["status"] != "OBSERVED" or not evidence["transcript"].strip():
            decisions.append(
                {**base, "status": "NO_SPEECH_NOT_DELETION_AUTHORITY", "new_cpa_calls": 0}
            )
            continue
        cell = folder / tid
        save(
            cell / "frozen-observation.json",
            {
                "evidence": evidence,
                "source_response_file": str(response_path),
                "source_response_file_sha256": sha(response_path.read_bytes()),
                "source_receipt_sha256": sha((source / "receipt.json").read_bytes()),
                "cache_replay_not_new_provider_observation": True,
            },
        )
        witness = _transcript_witness(
            {
                "provider": provider,
                "audio_sha256": sha(audio),
                "response_sha256": meta["response_sha256"],
                "start_ms": start,
                "end_ms": end,
                "witness_native_evidence": evidence,
            },
            transcript=evidence["transcript"],
        )
        request = {
            "current_cue": texts[n],
            "proposed_cue": evidence["transcript"],
            "proposed_candidates": [
                {
                    "text": raw[n - 1].text,
                    "source": {
                        "kind": "source_asr_draft",
                        "provider": "bcut",
                        "source_srt_sha256": sha(original.encode()),
                        "start_ms": raw[n - 1].start_ms,
                        "end_ms": raw[n - 1].end_ms,
                    },
                }
            ],
            "candidate_provenance": {
                "kind": "bounded_candidate_blind_audio_transcript",
                "provider": provider,
                "model": meta["model"],
                "response_sha256": meta["response_sha256"],
                "audio_sha256": sha(audio),
                "start_ms": start,
                "end_ms": end,
            },
            "repair_class": "spoken_unit",
            "whole_clip_current_srt": render(cues, texts),
            "reason": "Frozen exact-target native transcript is a fallible alternative, never independent pinyin. No target-outside words may be added.",
        }
        request["request_sha256"] = digest(request)
        count = 0

        def cpa(prompt):
            nonlocal count
            count += 1
            return transport.call_cpa(prompt, "medium", cell / f"cpa-{count}")

        applied, branch, audit = adjudicate_with_witness(
            check_request=request, witness=witness, llm_call=cpa
        )
        judge = audit.get("judge") or {}
        if applied:
            chosen = judge.get("selected_candidate_text")
            assert isinstance(chosen, str) and chosen.strip()
            texts[n] = chosen
        save(
            cell / "adjudication.json",
            {
                "request": request,
                "witness": witness,
                "audit": audit,
                "branch": branch,
                "applied": applied,
            },
        )
        decisions.append(
            {
                **base,
                "status": judge.get("status"),
                "choice": judge.get("choice"),
                "applied": applied,
                "before": cues[n - 1].text,
                "after": texts[n],
                "native": evidence["transcript"],
                "new_cpa_calls": count,
            }
        )
    output = render(cues, texts)
    (folder / "output.srt").write_text(output)
    save(
        folder / "result.json",
        {
            "candidate_id": cid,
            "provider": provider,
            "decisions": decisions,
            "new_audio_calls": 0,
            "new_cpa_calls": sum(d["new_cpa_calls"] for d in decisions),
            "output_sha256": sha(output.encode()),
            "input_srt_sha256": sha(common.encode()),
            "whole_timegrid_preserved": True,
            "no_full_producer_claim": True,
        },
    )
    print(
        "REPLAY_COMPLETE",
        cid,
        provider,
        [(d["target_id"], d["status"], d.get("choice")) for d in decisions],
        flush=True,
    )


def score():
    model = transport.scorer()
    rows = []
    for cid in load(INPUT / "inputs.json"):
        base = model.score(cid, INPUT / cid / "first-low/output.srt")
        for provider in ("mai", "moss"):
            folder = OUT / cid / provider
            if not (folder / "result.json").exists():
                rows.append({"candidate_id": cid, "provider": provider, "status": "NOT_COMPLETED"})
                continue
            scored = model.score(cid, folder / "output.srt")
            result = load(folder / "result.json")
            save(folder / "score.json", scored)
            rows.append(
                {
                    "candidate_id": cid,
                    "provider": provider,
                    "baseline_dkeep": base["d_keep_distance"],
                    "dkeep": scored["d_keep_distance"],
                    "reference_chars": scored["reference_chars"],
                    "new_cpa_calls": result["new_cpa_calls"],
                    "applied": sum(d.get("applied", False) for d in result["decisions"]),
                    "scope_exclusions": sum(
                        d["status"] == "SCOPE_NOT_EQUAL_WHOLE_CUE" for d in result["decisions"]
                    ),
                    "unresolved_or_unusable": sum(
                        d.get("choice") not in ("CURRENT", "PROPOSED") for d in result["decisions"]
                    ),
                }
            )
    save(OUT / "summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "run", "score"])
    parser.add_argument("--candidate")
    parser.add_argument("--provider", choices=["mai", "moss"])
    args = parser.parse_args()
    transport.OUT = OUT
    os.environ.pop(
        "AUTOSLICE_BASE", None
    )  # These are fresh CPA decisions, not global cache timings.
    if args.stage == "prepare":
        prepare()
    elif args.stage == "score":
        score()
    else:
        if args.candidate not in load(INPUT / "inputs.json") or args.provider is None:
            parser.error("candidate/provider required")
        run(args.candidate, args.provider)


if __name__ == "__main__":
    main()
