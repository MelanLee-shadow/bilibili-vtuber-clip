#!/usr/bin/env python3
"""Replay all paired cases after source-provenance repair; no audio API calls.

This is a development-set recheck with fresh CPA, not a new cold ASR benchmark
and not an independent holdout. Original failed/baseline outputs remain intact.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_agy_replacement_paired as base
from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.local_asr_target_evidence import digest

OUT = base.OUT / "current-source-fix"


def rejudge(cid: str) -> None:
    origin = base.OUT / cid
    plan = base.load(origin / "plan.json")
    cues = parse_srt_cues((origin / "prepared.srt").read_text())
    initial = parse_srt_cues((origin / "text-first.srt").read_text())
    for arm in base.ARMS:
        out = OUT / cid / arm
        base.attempt(out, "frozen_audio_fresh_cpa_source_provenance_fix")
        start = time.monotonic()
        texts = {n: c.text for n, c in enumerate(initial, 1)}
        decisions = []
        for window in plan["windows"]:
            n = window["n"]
            p = origin / arm / str(n) / "adjudication.json"
            if not p.exists():
                decisions.append({"n": n, "status": "PRIOR_AUDIO_UNUSABLE_UNCHANGED"})
                continue
            old = base.load(p)
            request = dict(old["request"])
            request["current_cue"] = texts[n]
            request["whole_clip_current_srt"] = base.render(cues, texts)
            request.pop("request_sha256", None)
            request["request_sha256"] = digest(request)
            witness = dict(old["witness"])
            witness["request_sha256"] = request["request_sha256"]
            count = 0

            def cpa(prompt):
                nonlocal count
                count += 1
                return base.call_cpa(prompt, out / str(n) / f"cpa-{count}")

            applied, branch, audit = adjudicate_with_witness(
                check_request=request, witness=witness, llm_call=cpa
            )
            judge = audit.get("judge", {})
            if applied:
                chosen = judge.get("selected_candidate_text")
                if not isinstance(chosen, str) or not chosen.strip():
                    raise ValueError("UNBOUND_SELECTED_CANDIDATE")
                texts[n] = chosen
            base.save(
                out / str(n) / "adjudication.json",
                {
                    "request": request,
                    "witness": witness,
                    "applied": applied,
                    "branch": branch,
                    "audit": audit,
                    "frozen_audio_parent": str(p),
                    "frozen_audio_parent_sha256": base.sha(p.read_bytes()),
                },
            )
            decisions.append(
                {
                    "n": n,
                    "choice": judge.get("choice"),
                    "status": judge.get("status"),
                    "applied": applied,
                    "output": texts[n],
                    "reason": judge.get("reason"),
                }
            )
        result = base.render(cues, texts)
        (out / "evaluated-cpa-output.srt").write_text(result)
        base.save(
            out / "result.json",
            {
                "status": "EXPERIMENT_COMPLETE",
                "full_producer_complete": False,
                "package_or_release_approved": False,
                "candidate_id": cid,
                "arm": arm,
                "new_audio_calls": 0,
                "audio_frozen_from_v1": True,
                "fresh_cpa_replay_wall_seconds": time.monotonic() - start,
                "decisions": decisions,
                "unresolved_selected_count": sum(
                    d.get("choice") not in ("CURRENT", "PROPOSED") for d in decisions
                ),
                "deferred_count": len(plan["deferred"]),
                "output_sha256": base.sha(result.encode()),
                "judge_source_sha256": base.sha(
                    (ROOT / "src/autoslice/acoustic_witness_adjudication.py").read_bytes()
                ),
                "interpretation": "Frozen audio, fresh CPA; cannot substitute this wall time for cold end-to-end latency.",
            },
        )
        print("REJUDGE_COMPLETE", cid, arm, flush=True)


def score() -> None:
    spec = importlib.util.spec_from_file_location(
        "old_reference_scorer", base.HISTORY / "score_reference.py"
    )
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    rows = []
    for cid in base.IDS:
        for arm in base.ARMS:
            p = OUT / cid / arm / "evaluated-cpa-output.srt"
            if not p.exists():
                rows.append({"candidate_id": cid, "arm": arm, "status": "MISSING_OUTPUT"})
                continue
            result = scorer.score(cid, p)
            base.save(OUT / cid / f"score-{arm}.json", result)
            old = base.load(base.OUT / cid / f"score-{arm}.json")
            record = base.load(p.parent / "result.json")
            rows.append(
                {
                    "candidate_id": cid,
                    "arm": arm,
                    "dkeep_before": old["d_keep_distance"],
                    "dkeep_after": result["d_keep_distance"],
                    "reference_chars": result["reference_chars"],
                    "unresolved_selected_count": record["unresolved_selected_count"],
                    "fresh_cpa_replay_wall_seconds": record["fresh_cpa_replay_wall_seconds"],
                }
            )
    base.save(OUT / "summary.json", rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=["run", "score"])
    p.add_argument("--candidate", choices=base.IDS)
    args = p.parse_args()
    os.environ.pop("AUTOSLICE_BASE", None)
    if args.stage == "run":
        if args.candidate is None:
            p.error("--candidate required")
        rejudge(args.candidate)
    else:
        score()


if __name__ == "__main__":
    main()
