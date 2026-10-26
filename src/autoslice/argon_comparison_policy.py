"""Explicit A/B scope revision for the existing comparison CLI.

The old plan/grant and Argon reservation namespace are immutable. This narrow
adapter changes ordinary-route permission only after a separate authorization
record and binds both arms to identical source/code/configuration contracts.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path

from .argon_escalation_contract import canonical, require, sha
from . import argon_escalation_storage as store

SCHEMA = "argon-comparison-scope-revision.v1"


def load_revision(path: Path, plan: dict, grant_path: Path) -> dict:
    from .argon_regression_comparison import stable_bytes

    raw = stable_bytes(path)
    revision = json.loads(raw)
    require(
        revision.get("schema_version") == SCHEMA
        and revision.get("status") == "AUTHORIZED"
        and revision.get("plan_sha256") == plan["plan_sha256"]
        and revision.get("grant_sha256") == sha(stable_bytes(grant_path))
        and set(revision.get("case_ids", [])) == {r["case_id"] for r in plan["samples"]}
        and revision.get("ordinary_audio_policy") == "NORMAL_PRODUCTION_ROUTES"
        and revision.get("ordinary_paid_policy") == "EXISTING_THREE_ROUND_GATE"
        and revision.get("argon_campaign_budget_unchanged") is True
        and revision.get("s2_enabled") is False
        and revision.get("argon_realtime_fallback") is False
        and isinstance(revision.get("authority"), str)
        and bool(revision["authority"].strip()),
        "COMPARISON_SCOPE_REVISION_INVALID",
    )
    return {"document": revision, "sha256": sha(raw)}


def binding(revision: dict | None, arm: str) -> dict:
    if revision is None:
        require(arm == "on", "COMPARISON_SCOPE_REVISION_REQUIRED")
        return {}
    require(arm in {"on", "off"}, "COMPARISON_ARM_INVALID")
    return {
        "scope_revision_sha256": revision["sha256"],
        "argon_arm": arm,
        "ordinary_audio_policy": "NORMAL_PRODUCTION_ROUTES",
    }


@contextmanager
def normal_environment(base: Path, runtime_root: str):
    """Reuse normal private history and original paid ledger, not an empty ladder."""
    from scripts.subtitle_regression_eval import (
        configure_replay_state,
        load_production_gemini_environment,
    )
    from .native_audio_runtime import native_audio_readiness
    from .agy_gemini_client import resolve_local_agy_binary

    names = (
        "AUTOSLICE_BASE",
        "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT",
        "GEMINI_PAID_BACKUP_DEV_EXCEPTION",
        "GEMINI_PAID_BACKUP_DAILY_CAP",
        "AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
    )
    before = {k: os.environ.get(k) for k in names}
    try:
        os.environ["AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"] = runtime_root
        os.environ["GEMINI_PAID_BACKUP_DEV_EXCEPTION"] = "0"
        # The A revision restores the normal uncapped production policy; the
        # Argon-specific plan reservation cap and allow_paid=False are separate.
        os.environ.pop("GEMINI_PAID_BACKUP_DAILY_CAP", None)
        os.environ["AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES"] = "1"
        metadata = base.parent / "replay-state.json"
        state = configure_replay_state(base.parent, Path(runtime_root), resume=metadata.exists())
        store.put_json(metadata, state)
        gemini = load_production_gemini_environment()
        native = native_audio_readiness(Path(runtime_root))
        require(
            all(r.get("ready") is True for r in native) and len(native) == 2,
            "COMPARISON_ORDINARY_NATIVE_CONFIG_UNAVAILABLE",
        )
        require(bool(resolve_local_agy_binary(None)), "COMPARISON_AGY_NOT_CONFIGURED")
        yield {
            "gemini": gemini,
            "native_configuration": native,
            "configuration_is_not_attempt_evidence": True,
        }
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def route_probes(reviewer, samples: list[dict], source_text: str, out: Path) -> dict:
    """Probe via the SAME prepared reviewer's ordinary native seam before CPA.

    Only actual audio geometry/current surface enters the request; no scoring
    label or proposed answer. Generated native envelopes stay in the reviewer's
    normal cache and are subsequently consumed by that same ordinary chain.
    """
    from .independent_context_adjudication import independent_asr_candidates
    from .jingting_chunker import parse_srt_cues

    require(bool(samples), "COMPARISON_PROBE_TARGET_MISSING")
    cues = parse_srt_cues(source_text)
    results = []
    for sample in samples:
        start, end = sample["target_source_ms"]
        matching = [(i, c) for i, c in enumerate(cues, 1) if c.start_ms < end and c.end_ms > start]
        require(bool(matching), "COMPARISON_PROBE_TARGET_MISSING")
        request = {
            "evidence_id": "comparison-route:" + sample["case_id"],
            "cue_indexes": [i for i, _ in matching],
            "matched_start_ms": start,
            "matched_end_ms": end,
            "context_start_ms": sample["context_source_ms"][0],
            "context_end_ms": sample["context_source_ms"][1],
            "source_media_timeline_offset_ms": 0,
            "current_cue": "".join(c.text for _, c in matching),
            "proposed_cue": "",
            "whole_clip_current_srt": source_text,
        }
        request["request_sha256"] = sha(canonical(request))
        _, _, audit = independent_asr_candidates(request, reviewer)
        observed = audit.get("observations") or []
        attempted = audit.get("routes_attempted")
        aligned = [
            r
            for r in observed
            if isinstance(r, dict)
            and r.get("source_media_sha256", "").removeprefix("sha256:")
            == sample["source_media_sha256"]
            and [r.get("audio_start_ms"), r.get("audio_end_ms")] == [start, end]
        ]
        valid = type(attempted) is int and attempted > 0 and bool(aligned)
        row = {
            "case_id": sample["case_id"],
            "request": request,
            "independent_asr": audit,
            "validity": "VALID_ROUTE_ATTEMPT_EVIDENCE"
            if valid
            else "INVALID_EVALUATION_CONFIGURATION",
            "source_media_sha256": sample["source_media_sha256"],
            "target_source_ms": [start, end],
            "role": "actual normal independent route result, not a config declaration",
            "real_or_cached_observation": [
                {
                    k: r.get(k)
                    for k in (
                        "provider",
                        "model",
                        "status",
                        "reason_code",
                        "served_from_cache",
                        "request_sha256",
                        "source_media_sha256",
                        "audio_clip_sha256",
                        "response_sha256",
                    )
                }
                for r in aligned
            ],
        }
        directory = out / "route-validity" / sample["case_id"]
        store.put_json(directory / (sha(canonical(row)) + ".json"), row)
        results.append(row)
        require(valid, "INVALID_EVALUATION_CONFIGURATION")
    return {"status": "VALID_ROUTE_ATTEMPT_EVIDENCE", "samples": results}


def paired_off_run(
    off_root: Path, package: str, identity: dict, current: dict | None = None
) -> dict:
    """No comparison to a different-code historical baseline masquerading as OFF."""
    from .argon_regression_comparison import effective_invocation, stable_bytes

    old, _ = effective_invocation(off_root)
    old_common = {
        k: v
        for k, v in old.items()
        if k not in {"argon_arm", "paired_off_output", "paired_off_runs"}
    }
    new_common = {
        k: v
        for k, v in identity.items()
        if k not in {"argon_arm", "paired_off_output", "paired_off_runs"}
    }
    require(
        old.get("argon_arm") == "off" and old_common == new_common, "COMPARISON_OFF_IDENTITY_DRIFT"
    )
    starts = list((off_root / "invocations").glob("*.started.json"))
    require(
        bool(starts)
        and all(
            p.with_name(p.name.replace(".started.json", ".finished.json")).is_file() for p in starts
        ),
        "COMPARISON_OFF_NOT_TERMINAL",
    )
    raw = stable_bytes(off_root / package / "run.json")
    run = json.loads(raw)
    require(
        identity.get("paired_off_runs", {}).get(package, sha(raw)) == sha(raw),
        "COMPARISON_OFF_RESULT_DRIFT",
    )
    require(
        (current is None or run.get("input_hashes") == current.get("input_hashes"))
        and run.get("ordinary_route_validity", {}).get("status") == "VALID_ROUTE_ATTEMPT_EVIDENCE",
        "COMPARISON_OFF_INPUT_OR_ROUTE_DRIFT",
    )
    require(
        sha(stable_bytes(off_root / package / "final.srt")) == run.get("final_srt_sha256"),
        "COMPARISON_OFF_RESULT_DRIFT",
    )
    return run
