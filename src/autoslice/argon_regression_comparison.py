"""Finite comparison preparation and scope checks for the existing regression CLI.

No provider or credential is needed to prepare. Scoring labels are a separate
artifact, and are never passed into producer spec or an audio request.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import base64
import os
from pathlib import Path
import subprocess
import time

from .argon_escalation_contract import (
    AudioSpec,
    Finding,
    canonical,
    file_sha,
    make_inner_request,
    prepare_audio,
    require,
    sha,
)
from . import argon_escalation_storage as store
from .jingting_chunker import parse_srt_cues

PLAN_SCHEMA = "argon-regression-comparison.v1"
GRANT_SCHEMA = "argon-regression-cost-grant.v1"
INPUT_LIMIT = 1_048_576
OUTPUT_LIMIT = 32_768
# Official public Batch rates, frozen to a dated quote rather than inferred
# from names such as FREE_KEY. No cache saving is assumed.
INPUT_RATE = Decimal("1")
OUTPUT_RATE = Decimal("5")
PER_REQUEST_USD = (INPUT_LIMIT * INPUT_RATE + OUTPUT_LIMIT * OUTPUT_RATE) / 1_000_000
PRICE_VALID_BEFORE = "2026-11-28T00:00:00+00:00"  # leave 48h before scheduled Dec1 increase


def stable_bytes(path: Path, expected: str | None = None) -> bytes:
    require(path.is_file() and not path.is_symlink(), "COMPARISON_INPUT_UNSAFE")
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    require(
        (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        == (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns),
        "COMPARISON_INPUT_CHANGED",
    )
    require(
        expected is None or sha(raw) == expected.removeprefix("sha256:"),
        "COMPARISON_INPUT_HASH_DRIFT",
    )
    return raw


def frozen_copy(source: Path, target: Path, bindings: list, expected=None):
    raw = stable_bytes(source, expected)
    store.immutable(target, raw)
    require(sha(stable_bytes(source)) == sha(raw), "COMPARISON_INPUT_CHANGED")
    bindings.append(
        {
            "source_path": str(source),
            "path": str(target),
            "bytes": len(raw),
            "sha256": sha(raw),
            "source_sha256_before": sha(raw),
            "source_sha256_after": sha(raw),
        }
    )
    return raw


def read_plan(path: Path) -> dict:
    plan = json.loads(stable_bytes(path))
    unsigned = dict(plan)
    digest = unsigned.pop("plan_sha256", None)
    require(
        plan.get("schema_version") == PLAN_SCHEMA and digest == sha(canonical(unsigned)),
        "COMPARISON_PLAN_INVALID",
    )
    for item in plan["bindings"]:
        if item.get("read_only_reference"):
            require(file_sha(Path(item["path"])) == item["sha256"], "COMPARISON_SOURCE_DRIFT")
        else:
            stable_bytes(Path(item["path"]), item["sha256"])
    return plan


def prepare(args) -> int:
    from scripts.subtitle_regression_eval import load_cases, summarize_case, validate_original_cases

    doc = load_cases(args.cases)
    selected = [c for c in doc["cases"] if not args.case or c["id"] in args.case]
    require(
        selected and (not args.case or set(args.case) == {c["id"] for c in selected}),
        "COMPARISON_CASE_SELECTION_INVALID",
    )
    require(all(c["status"] == "ready" for c in selected), "COMPARISON_CASE_NOT_READY")
    output = args.output.resolve()
    require(not output.exists(), "COMPARISON_OUTPUT_EXISTS")
    store.directory(output)
    scoring = {
        "schema_version": doc["schema_version"],
        "baseline_commit": doc["baseline_commit"],
        "clock_rule": doc["clock_rule"],
        "packages": {k: doc["packages"][k] for k in dict.fromkeys(c["package"] for c in selected)},
        "cases": selected,
    }
    store.put_json(output / "scoring-only.json", scoring)
    bindings, packages, samples, baselines = [], {}, [], {}
    for key, package in scoring["packages"].items():
        frozen = dict(package)
        documents = {}
        for kind in ("record", "srt", "authority", "context", "provenance"):
            source = Path(package[kind + "_path"])
            target = output / "inputs" / key / (kind + source.suffix)
            raw = frozen_copy(source, target, bindings, package.get(kind + "_sha256"))
            frozen[kind + "_path"] = str(target)
            frozen[kind + "_sha256"] = sha(raw)
            documents[kind] = raw
        record, context = json.loads(documents["record"]), json.loads(documents["context"])
        require(
            record["subtitle_audio_correspondence"]["intro_offset_ms"]
            == package["intro_offset_ms"],
            "COMPARISON_INTRO_CLOCK_DRIFT",
        )
        require(
            context["context_sha256"] == record["clip_context_payload_sha256"],
            "COMPARISON_CONTEXT_DRIFT",
        )
        cues = parse_srt_cues(documents["srt"].decode())
        cue_rows = [
            {"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
            for i, c in enumerate(cues, 1)
        ]
        these = [c for c in selected if c["package"] == key]
        validate_original_cases(these, cue_rows)
        media = Path(package["media_path"])
        media_sha = file_sha(media)
        require(media_sha == package["media_sha256"], "COMPARISON_SOURCE_DRIFT")
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(media),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        metadata = json.loads(probe.stdout)
        duration_ms = round(float(metadata["format"]["duration"]) * 1000)
        bindings.append(
            {
                "source_path": str(media),
                "path": str(media),
                "bytes": media.stat().st_size,
                "sha256": media_sha,
                "source_sha256_before": media_sha,
                "source_sha256_after": file_sha(media),
                "read_only_reference": True,
            }
        )
        require(bindings[-1]["source_sha256_after"] == media_sha, "COMPARISON_SOURCE_DRIFT")
        packages[key] = frozen
        # A terminal per-package run can be frozen while other packages remain
        # in progress. Never sample an unfinished package or the shared hot cache.
        original_run = args.prepare_argon_comparison / key / "run.json"
        baseline_root = output / "baseline" / key
        if original_run.exists():
            run_raw = stable_bytes(original_run)
            time.sleep(0.15)
            require(stable_bytes(original_run) == run_raw, "COMPARISON_BASELINE_UNSTABLE")
            run = json.loads(run_raw)
            require(
                run.get("candidate_id") == package["candidate_id"] and not run.get("input_drift"),
                "COMPARISON_BASELINE_INPUT_DRIFT",
            )
            require(
                all(
                    run.get("input_hashes", {}).get(package[k + "_path"]) == package[k + "_sha256"]
                    for k in ("record", "srt", "authority", "media")
                ),
                "COMPARISON_NOT_PAIRED_INPUT",
            )
            for source in sorted(original_run.parent.rglob("*")):
                if (
                    not source.is_file()
                    or source.is_symlink()
                    or source.suffix
                    not in {
                        ".json",
                        ".jsonl",
                        ".srt",
                        ".txt",
                        ".md",
                        ".mp4",
                        ".wav",
                        ".mp3",
                        ".m4a",
                        ".webm",
                    }
                ):
                    continue
                # All files belong to this completed package, not the shared
                # runtime and not another in-flight model run.
                frozen_copy(
                    source, baseline_root / source.relative_to(original_run.parent), bindings
                )
            require(stable_bytes(original_run) == run_raw, "COMPARISON_BASELINE_UNSTABLE")
            baselines[key] = {
                "status": "COMPLETED_STABLE",
                "run_path": str(baseline_root / "run.json"),
                "run_sha256": sha(run_raw),
                "source_path": str(original_run),
                "scoring": [summarize_case(c, run) for c in these],
            }
        else:
            run = None
            baselines[key] = {
                "status": "WAITING_BASELINE_TERMINAL",
                "source_path": str(original_run),
                "scoring": None,
            }
        for case in these:
            index = case["cue_ids"][0] - 1
            target = list(case["body_window_ms"])
            # This matches the ordinary context geometry on the unbranded recut
            # (delivery/source offset 0). It is not a transcript proposal.
            context_window = [
                max(0, cues[max(0, index - 1)].start_ms - 500),
                min(duration_ms, cues[min(len(cues) - 1, index + 1)].end_ms + 500),
            ]
            if context_window[1] - context_window[0] > 30_000:
                midpoint = sum(target) // 2
                context_window = [
                    max(0, midpoint - 15000),
                    min(duration_ms, max(0, midpoint - 15000) + 30000),
                ]
            findings = []
            if run:
                for ri, review in enumerate(run.get("reviews", [])):
                    grid = {row["index"]: row for row in review.get("evaluation_input_cues", [])}
                    for field in ("findings", "resolved_findings", "unresolved_findings_disclosed"):
                        for fi, row in enumerate(review.get(field, [])):
                            cue = grid.get(row.get("cue_index", row.get("cue")))
                            if cue and cue["start_ms"] < target[1] and cue["end_ms"] > target[0]:
                                findings.append(
                                    {
                                        "review_index": ri,
                                        "field": field,
                                        "row_index": fi,
                                        "finding": row,
                                    }
                                )
            geometry_id = sha(
                canonical({"source": media_sha, "target": target, "context": context_window})
            )
            binding, audio = prepare_audio(
                AudioSpec(
                    media, Finding(media_sha, tuple(target), geometry_id), tuple(context_window)
                )
            )
            inner = make_inner_request(binding, audio)
            sample_root = output / "samples" / case["id"]
            store.immutable(sample_root / "input.wav", audio)
            store.immutable(sample_root / "request-preview.json", inner)
            store.put_json(sample_root / "geometry.json", binding)
            store.put_json(sample_root / "original-findings.json", findings)
            for p in sample_root.iterdir():
                raw = p.read_bytes()
                bindings.append({"path": str(p), "bytes": len(raw), "sha256": sha(raw)})
            samples.append(
                {
                    "case_id": case["id"],
                    "package": key,
                    "candidate_id": case["candidate_id"],
                    "source_path": str(media),
                    "source_media_sha256": media_sha,
                    "target_source_ms": target,
                    "context_source_ms": context_window,
                    "source_media_timeline_offset_ms": 0,
                    "source_duration_ms": duration_ms,
                    "input_audio_sha256": sha(audio),
                    "request_preview_sha256": sha(inner),
                    "request_preview_scope": "GEOMETRY_ONLY_NOT_AN_AUTHORIZED_OR_TRIGGERED_TASK",
                    "findings_path": str(sample_root / "original-findings.json"),
                    "baseline_status": baselines[key]["status"],
                    "trigger_status": "NOT_ESTABLISHED_BY_PREPARATION",
                    "maximum_request_cost_usd": str(PER_REQUEST_USD),
                }
            )
    # No expected answers/quotations are present in the execution plan.
    plan = {
        "schema_version": PLAN_SCHEMA,
        "code_identity": args.code_identity or args.commit,
        "baseline_code_identity": args.baseline_commit,
        "fixture_sha256": sha(args.cases.read_bytes()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "packages": packages,
        "samples": samples,
        "bindings": bindings,
        "scoring_file": str(output / "scoring-only.json"),
        "scoring_sha256": sha((output / "scoring-only.json").read_bytes()),
        "baseline": {
            k: {n: v for n, v in b.items() if n != "scoring"} for k, b in baselines.items()
        },
        "real_provider_calls": 0,
        "new_cost_authorized_usd": "0",
        "input_token_ceiling": INPUT_LIMIT,
        "output_including_thinking_ceiling": OUTPUT_LIMIT,
        "total_submitted_audio_duration_ms": sum(
            s["context_source_ms"][1] - s["context_source_ms"][0] for s in samples
        ),
        "max_requests": len(samples),
        "max_usd": str(len(samples) * PER_REQUEST_USD),
        "price_valid_before": PRICE_VALID_BEFORE,
        "allow_paid_backup": False,
        "allow_realtime_fallback": False,
    }
    plan["plan_sha256"] = sha(canonical(plan))
    store.put_json(output / "plan.json", plan)
    store.put_json(output / "baseline-score.json", baselines)
    store.put_json(
        output / "grant-required.json",
        {
            "schema_version": GRANT_SCHEMA,
            "status": "NOT_AUTHORIZED",
            "plan_sha256": plan["plan_sha256"],
            "authorized_case_ids": [],
            "max_usd": "0",
            "existing_subscription_cpa_only": True,
            "ordinary_new_audio_calls": 0,
        },
    )
    read_plan(output / "plan.json")
    print(
        json.dumps(
            {
                "status": "PREPARED_COST_AUTHORIZATION_REQUIRED",
                "plan_sha256": plan["plan_sha256"],
                "cases": len(samples),
                "audio_ms": plan["total_submitted_audio_duration_ms"],
                "max_usd": plan["max_usd"],
                "baseline": {k: v["status"] for k, v in baselines.items()},
                "real_provider_calls": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


def authorize(plan_path: Path, grant_path: Path) -> tuple[dict, dict]:
    plan = read_plan(plan_path)
    grant = json.loads(stable_bytes(grant_path))
    ids = grant.get("authorized_case_ids", [])
    require(
        grant.get("schema_version") == GRANT_SCHEMA
        and grant.get("status") == "AUTHORIZED"
        and grant.get("plan_sha256") == plan["plan_sha256"]
        and isinstance(ids, list)
        and 1 <= len(ids) <= len(plan["samples"])
        and len(set(ids)) == len(ids)
        and set(ids) <= {r["case_id"] for r in plan["samples"]},
        "COMPARISON_COST_NOT_AUTHORIZED",
    )
    require(
        grant.get("existing_subscription_cpa_only") is True
        and grant.get("ordinary_new_audio_calls") == 0,
        "COMPARISON_OTHER_COST_NOT_AUTHORIZED",
    )
    require(
        isinstance(grant.get("authority"), str) and bool(grant["authority"].strip()),
        "COMPARISON_AUTHORITY_REQUIRED",
    )
    limit = Decimal(str(grant["max_usd"]))
    require(
        limit.is_finite() and limit >= len(ids) * PER_REQUEST_USD,
        "COMPARISON_COST_CEILING_INSUFFICIENT",
    )
    return plan, grant


def baseline_state(plan: dict, key: str) -> dict:
    original = plan["baseline"][key]
    if original["status"] == "COMPLETED_STABLE":
        return original
    pointer = Path(plan["scoring_file"]).parent / "baseline-completions" / (key + ".json")
    if not pointer.exists():
        return original
    receipt = json.loads(stable_bytes(pointer))
    unsigned = dict(receipt)
    seal = unsigned.pop("sha256", None)
    require(
        seal == sha(canonical(unsigned))
        and receipt["plan_sha256"] == plan["plan_sha256"]
        and receipt["source_path"] == original["source_path"],
        "COMPARISON_BASELINE_COMPLETION_DRIFT",
    )
    for binding in receipt["bindings"]:
        stable_bytes(Path(binding["path"]), binding["sha256"])
    validate_paired_baseline(
        plan, key, json.loads(stable_bytes(Path(receipt["run_path"]), receipt["run_sha256"]))
    )
    return receipt


def validate_paired_baseline(plan: dict, key: str, run: dict) -> None:
    package = plan["packages"][key]
    originals = {b["path"]: b.get("source_path", b["path"]) for b in plan["bindings"]}
    require(
        run.get("candidate_id") == package["candidate_id"]
        and not run.get("input_drift")
        and all(
            run.get("input_hashes", {}).get(originals[package[k + "_path"]])
            == package[k + "_sha256"]
            for k in ("record", "srt", "authority", "media")
        ),
        "COMPARISON_BASELINE_INPUT_DRIFT",
    )


def attach_completed_baselines(path: Path) -> int:
    plan = read_plan(path)
    root = Path(plan["scoring_file"]).parent
    added, waiting = [], []
    with store.locked(root / "baseline-completions"):
        for key, original in plan["baseline"].items():
            if baseline_state(plan, key)["status"] == "COMPLETED_STABLE":
                continue
            source = Path(original["source_path"])
            if not source.exists():
                waiting.append(key)
                continue
            raw = stable_bytes(source)
            time.sleep(0.15)
            require(stable_bytes(source) == raw, "COMPARISON_BASELINE_UNSTABLE")
            validate_paired_baseline(plan, key, json.loads(raw))
            bindings = []
            for item in sorted(source.parent.rglob("*")):
                if (
                    item.is_file()
                    and not item.is_symlink()
                    and item.suffix
                    in {
                        ".json",
                        ".jsonl",
                        ".srt",
                        ".txt",
                        ".md",
                        ".mp4",
                        ".wav",
                        ".mp3",
                        ".m4a",
                        ".webm",
                    }
                ):
                    frozen_copy(
                        item, root / "baseline" / key / item.relative_to(source.parent), bindings
                    )
            require(stable_bytes(source) == raw, "COMPARISON_BASELINE_UNSTABLE")
            receipt = {
                "status": "COMPLETED_STABLE",
                "plan_sha256": plan["plan_sha256"],
                "source_path": str(source),
                "run_path": str(root / "baseline" / key / "run.json"),
                "run_sha256": sha(raw),
                "bindings": bindings,
            }
            receipt["sha256"] = sha(canonical(receipt))
            store.put_json(root / "baseline-completions" / (key + ".json"), receipt)
            baseline_state(plan, key)
            added.append(key)
    print(
        json.dumps(
            {
                "added": added,
                "waiting": waiting,
                "plan_sha256": plan["plan_sha256"],
                "real_provider_calls": 0,
            }
        )
    )
    return 0


class SubmissionBudget:
    """One create attempt per frozen real target; UNKNOWN consumes reservation."""

    def __init__(self, plan_path: Path, grant_path: Path, allowed_case_ids=None):
        self.plan, self.grant = authorize(plan_path, grant_path)
        self.allowed_case_ids = (
            self.grant["authorized_case_ids"] if allowed_case_ids is None else allowed_case_ids
        )
        require(
            isinstance(self.allowed_case_ids, list)
            and self.allowed_case_ids
            and set(self.allowed_case_ids) <= set(self.grant["authorized_case_ids"]),
            "COMPARISON_SUBSCOPE_INVALID",
        )
        # The immutable plan fixes this root. Copying a plan or using another
        # output directory must not reset the same grant's reservations.
        self.root = Path(self.plan["scoring_file"]).parent / "cost-reservations"

    def reserve(self, spec: AudioSpec, task_id: str, inner: bytes) -> None:
        # A quote expiry stops NEW generation, not querying an existing job.
        require(
            datetime.now(timezone.utc) < datetime.fromisoformat(PRICE_VALID_BEFORE),
            "COMPARISON_REQUOTE_REQUIRED",
        )
        matches = [
            r
            for r in self.plan["samples"]
            if r["case_id"] in self.allowed_case_ids
            and r["source_media_sha256"] == spec.finding.source_media_sha256
            and r["target_source_ms"] == list(spec.finding.target_source_ms)
            and r["context_source_ms"] == list(spec.context_source_ms)
        ]
        require(len(matches) == 1, "COMPARISON_UNAUTHORIZED_TARGET")
        payload = json.loads(inner)
        require(
            payload.get("generationConfig", {}).get("maxOutputTokens") == OUTPUT_LIMIT
            and set(payload) == {"contents", "generationConfig"},
            "COMPARISON_REQUEST_COST_DRIFT",
        )
        require(not payload.get("tools"), "COMPARISON_TOOLS_NOT_AUTHORIZED")
        require(
            payload["generationConfig"]
            == {"maxOutputTokens": OUTPUT_LIMIT, "thinkingConfig": {"thinkingLevel": "HIGH"}},
            "COMPARISON_GENERATION_CONFIG_DRIFT",
        )
        parts = payload["contents"][0]["parts"]
        require(
            len(payload["contents"]) == 1
            and len(parts) == 2
            and parts[0]["inlineData"]["mimeType"] == "audio/wav"
            and sha(base64.b64decode(parts[0]["inlineData"]["data"], validate=True))
            == matches[0]["input_audio_sha256"],
            "COMPARISON_AUDIO_BYTES_DRIFT",
        )
        with store.locked(self.root):
            record = {
                "case_id": matches[0]["case_id"],
                "task_id": task_id,
                "request_sha256": sha(inner),
                "maximum_usd": str(PER_REQUEST_USD),
                "status": "RESERVED_OR_OUTCOME_UNKNOWN",
                "actual_cost_usd": None,
                "plan_sha256": self.plan["plan_sha256"],
            }
            path = self.root / (matches[0]["case_id"] + ".json")
            # Any reservation is spent opportunity, even if the process died
            # before POST. Ordinary engine recovery may query but never reserve again.
            require(not path.exists(), "COMPARISON_TARGET_ALREADY_RESERVED")
            reserved = sum(
                (Decimal(str(store.get_json(p)["maximum_usd"])) for p in self.root.glob("*.json")),
                Decimal(0),
            )
            require(
                reserved + PER_REQUEST_USD <= Decimal(str(self.grant["max_usd"])),
                "COMPARISON_BUDGET_EXHAUSTED",
            )
            store.put_json(path, record)


class CachedOrdinaryAudio:
    """Real ordinary cache consumer; fresh audio is not part of this experiment."""

    def __init__(self, delegate):
        self.delegate = delegate
        for name in (
            "span_termination_source_identity",
            "span_termination_ordinary_routes",
            "span_termination_upgrade_enabled",
            "span_termination_upgrade_status",
        ):
            if hasattr(delegate, name):
                setattr(self, name, getattr(delegate, name))

    def probe_witness_cache(self, request):
        probe = getattr(self.delegate, "probe_witness_cache", None)
        return probe(request) if callable(probe) else None

    def __call__(self, request):
        cached = self.probe_witness_cache(request)
        if cached is not None:
            return cached
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "request_sha256": request.get("request_sha256"),
            "status": "UNCERTAIN",
            "reason_code": "COMPARISON_ORDINARY_CACHE_MISS",
            "mutation_authorized": False,
        }

    def observe_independent_witnesses(self, request):
        probe = getattr(self.delegate, "observe_cached_independent_witnesses", None)
        return probe(request) if callable(probe) else []

    observe_cached_independent_witnesses = observe_independent_witnesses


@contextmanager
def comparison_environment(base: Path, runtime_root: str, *, normal_routes: bool = False):
    if normal_routes:
        from .argon_comparison_policy import normal_environment

        with normal_environment(base, runtime_root) as readiness:
            yield readiness
        return
    # Only this explicitly selected process scope changes; old budgets are not
    # symlinked, rewritten or cleared. Ordinary fresh audio is cache-only below.
    keys = {
        "AUTOSLICE_BASE": str(base),
        "GEMINI_PAID_BACKUP_DAILY_CAP": "0",
        "GEMINI_PAID_BACKUP_DEV_EXCEPTION": "0",
        "AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT": runtime_root,
    }
    previous = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(keys)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def native_accounting(run: dict) -> dict:
    """Separate the native HTTP journal from legacy urllib-only counters.

    Counts are cumulative within this isolated comparison, not newly billed
    calls and not summed again when the same task is present in two reviews.
    """
    tasks = {}

    def visit(value):
        if isinstance(value, dict):
            row = value.get("argon_escalation")
            if isinstance(row, dict) and row.get("engine_root") and row.get("task_id"):
                key = (row["engine_root"], row["task_id"])
                old = tasks.get(key)
                if old is None or (
                    row.get("accounting", {}).get("http_attempts", 0)
                    >= old.get("accounting", {}).get("http_attempts", 0)
                ):
                    tasks[key] = row
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(run.get("reviews", []))
    return {
        "tasks": list(tasks.values()),
        "scope": "cumulative_native_tasks_in_this_comparison",
        "http_journal_entries": sum(r["accounting"]["http_attempts"] for r in tasks.values()),
        "inference_reservations": sum(
            r["accounting"]["inference_reservations"] for r in tasks.values()
        ),
        "counter_is_confirmed_charge": False,
        "actual_charge_usd": None,
        "not_included_in_legacy_urllib_counters": True,
    }


def consumed_argon_ids(run: dict, target: list[int]) -> list[str]:
    """Count only native material in an actually completed normal CPA receipt."""
    from .argon_escalation_integration import validate_consumer_evidence

    found = set()

    def visit(value):
        if isinstance(value, dict):
            request = value.get("request")
            judge = (value.get("witness_judge") or {}).get("judge")
            if (
                isinstance(request, dict)
                and request.get("argon_escalation_evidence")
                and isinstance(judge, dict)
                and judge.get("status") == "JUDGED"
                and [request.get("matched_start_ms"), request.get("matched_end_ms")] == target
                and judge.get("check_request_sha256") == request.get("request_sha256")
            ):
                result = validate_consumer_evidence(request, request["argon_escalation_evidence"])
                found.add(result["inner_request_sha256"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(run.get("reviews", []))
    return sorted(found)


# Narrow source identity for the actual comparison/consumer path. This is not a
# claim to hash the whole repository, nor permission to retrieve restricted code.
EXECUTION_FILES = (
    "scripts/subtitle_regression_eval.py",
    "src/autoslice/argon_regression_comparison.py",
    "src/autoslice/argon_escalation_contract.py",
    "src/autoslice/argon_escalation_witness.py",
    "src/autoslice/argon_escalation_transport.py",
    "src/autoslice/argon_escalation_storage.py",
    "src/autoslice/argon_escalation_integration.py",
    "src/autoslice/argon_escalation_runtime.py",
    "src/autoslice/producer_text_pipeline.py",
    "src/autoslice/final_review_auditor.py",
    "src/autoslice/semantic_retry_progress.py",
    "src/autoslice/exact_final_witness_authority.py",
    "src/autoslice/producer_speaker_attribution.py",
    "src/autoslice/speaker_attribution.py",
    "src/autoslice/speaker_attribution_geometry.py",
    "src/autoslice/pronoun_consistency.py",
    "src/autoslice/independent_context_adjudication.py",
    "src/autoslice/llm_client.py",
    "src/autoslice/argon_comparison_policy.py",
    "src/autoslice/argon_span_termination.py",
    "src/autoslice/span_phoneme_resolution.py",
    "src/autoslice/span_finding_obligations.py",
    "src/autoslice/native_context_witness.py",
    "src/autoslice/read_aloud_llm_verifier.py",
    "src/autoslice/acoustic_witness_adjudication.py",
    "src/autoslice/final_review_witness_dispatch.py",
    "src/autoslice/final_review_contract.py",
    "src/autoslice/producer_recut_provenance.py",
    "src/autoslice/surface_canon.py",
    "profiles/lidousha/profile.json",
    "src/autoslice/producer_package_finalization.py",
    "src/autoslice/exact_final_cpa_history.py",
    "src/autoslice/span_judgment_history.py",
    "src/autoslice/missing_proposal_bootstrap.py",
    "src/autoslice/semantic_audio_recovery.py",
    "src/autoslice/retained_range_context.py",
)


def execution_binding(code_root: Path, runtime_root: Path) -> dict:
    """Pin the code actually imported, not merely a caller's --code-identity label."""
    from scripts import subtitle_regression_eval
    import sys

    physical = Path(__file__).resolve().parents[2]
    require(
        Path(code_root).resolve() == physical
        and Path(subtitle_regression_eval.__file__).resolve() == physical / EXECUTION_FILES[0],
        "COMPARISON_CODE_ROOT_DRIFT",
    )
    return {
        "code_root": str(physical),
        "provider_runtime_root": str(Path(runtime_root).resolve()),
        "python_executable": str(Path(sys.executable).absolute()),
        "source_sha256": {name: sha(stable_bytes(physical / name)) for name in EXECUTION_FILES},
        "source_scope": "explicit_comparison_and_shared_consumer_files_not_whole_repository",
        "speaker_attribution_generation": {"enabled": False},
    }


def _same_campaign(old: dict, new: dict) -> bool:
    # The explicit repair continuation may change code, never the experiment,
    # provider account namespace, interpreter, grant, targets or scope toggles.
    return {k: v for k, v in old.items() if k not in {"code_identity", "execution"}} == {
        k: v for k, v in new.items() if k not in {"code_identity", "execution"}
    } and {
        k: v for k, v in old["execution"].items() if k not in {"code_root", "source_sha256"}
    } == {k: v for k, v in new["execution"].items() if k not in {"code_root", "source_sha256"}}


def effective_invocation(out: Path) -> tuple[dict, list[dict]]:
    current = store.get_json(out / "invocation.json")
    records = []
    for path in sorted((out / "code-successions").glob("*.json")):
        row = store.get_json(path)
        unsigned = {k: v for k, v in row.items() if k != "sha256"}
        require(
            row.get("schema_version") == "argon-comparison-code-succession.v1"
            and row.get("sha256") == sha(canonical(unsigned))
            and row.get("parent_identity_sha256") == sha(canonical(current))
            and _same_campaign(current, row["next_identity"]),
            "COMPARISON_CODE_SUCCESSION_DRIFT",
        )
        current = row["next_identity"]
        records.append(row)
    return current, records


def accept_execution_identity(
    out: Path, identity: dict, *, predecessor: str | None, plan_path: Path
) -> None:
    if not (out / "invocation.json").exists():
        require(predecessor is None, "COMPARISON_SUCCESSOR_REQUIRES_EXISTING_EXECUTION")
        store.put_json(out / "invocation.json", identity)
        return
    current, records = effective_invocation(out)
    if current == identity:
        require(
            predecessor is None or records and records[-1]["parent_code_identity"] == predecessor,
            "COMPARISON_CODE_SUCCESSION_PARENT_MISMATCH",
        )
        return
    require(
        predecessor == current["code_identity"]
        and predecessor != identity["code_identity"]
        and _same_campaign(current, identity),
        "IMMUTABLE_ARTIFACT_CONFLICT",
    )
    # Only pre-native repair continuation is supported. Existing task handles,
    # unknown submits and reservations stay bound to their original engine.
    require(
        not list(
            (out / "runtime-state/cache/argon-consumer").glob("*/batch/attempt-*/handle.json")
        ),
        "COMPARISON_CODE_SUCCESSOR_HAS_NATIVE_TASK",
    )
    plan = read_plan(plan_path)
    reservations = Path(plan["scoring_file"]).parent / "cost-reservations"
    require(
        (current.get("argon_arm") == identity.get("argon_arm") == "off")
        or not any((reservations / (case + ".json")).exists() for case in identity["case_ids"]),
        "COMPARISON_CODE_SUCCESSOR_HAS_RESERVATION",
    )
    # OFF-to-OFF never executes that engine. Historical campaign reservations
    # remain at the same immutable namespace; this is not an ON transfer or reset.
    # Check the predecessor's actual code and current terminal outputs, before
    # appending a version edge. Old invocation/history/current text are kept.
    for name, digest in current["execution"]["source_sha256"].items():
        stable_bytes(Path(current["execution"]["code_root"]) / name, digest)
    for key in {r["package"] for r in plan["samples"] if r["case_id"] in identity["case_ids"]}:
        folder = out / key
        if not folder.exists():
            continue
        run = json.loads(stable_bytes(folder / "run.json"))
        require(
            not run.get("input_drift")
            and file_sha(folder / "final.srt") == run["final_srt_sha256"],
            "COMPARISON_SUCCESSOR_CURRENT_TEXT_DRIFT",
        )
        stable_bytes(folder / "authority.json")
    row = {
        "schema_version": "argon-comparison-code-succession.v1",
        "parent_identity_sha256": sha(canonical(current)),
        "parent_code_identity": predecessor,
        "next_identity": identity,
        "native_tasks_started": False,
        "reservations_transferred_or_reset": False,
        "reason": "explicit ordinary code repair before any native request; same campaign, history and current text",
    }
    row["sha256"] = sha(canonical(row))
    store.put_json(out / "code-successions" / f"{len(records) + 1:04d}.json", row)


def validate_package_resume_identity(out: Path, expected: dict) -> None:
    original = store.get_json(out / "comparison-invocation.json")
    if original == expected:
        if not (out.parent / "code-successions").exists():
            return
        active, records = effective_invocation(out.parent)
        require(
            not records or active["execution"] == expected["execution"],
            "COMPARISON_PACKAGE_IDENTITY_DRIFT",
        )
        return
    active, records = effective_invocation(out.parent)
    require(
        bool(records)
        and {k: v for k, v in original.items() if k != "execution"}
        == {k: v for k, v in expected.items() if k != "execution"}
        and all(
            active[k] == expected[k]
            for k in ("plan_sha256", "grant_sha256", "case_ids", "execution")
        ),
        "COMPARISON_PACKAGE_IDENTITY_DRIFT",
    )



def prepare_off_output_successor(parent: Path, out: Path, *, parent_identity_sha256: str,
                                 predecessor: str, plan: dict, grant_path: Path,
                                 scope_sha256: str, case_ids: list[str]) -> dict:
    """Create a terminal OFF-only byte successor, retaining every private history.

    This is the ordinary CLI's copy stage, not another experiment runner. The
    original remains immutable; ordinary code-succession and runtime/provider
    gates still execute afterwards. No paid ledger or native reservation is
    copied, cleared, re-keyed or treated as refunded.
    """
    import os
    import stat
    require(parent.is_absolute() and out.is_absolute() and parent != out
            and not parent.is_symlink() and not out.exists()
            and not out.is_relative_to(parent) and not parent.is_relative_to(out),
            "OFF_SUCCESSOR_PATH_INVALID")
    active, edges = effective_invocation(parent)
    require(sha(canonical(active)) == parent_identity_sha256
            and active.get('code_identity') == predecessor
            and active.get('argon_arm') == 'off'
            and active.get('plan_sha256') == plan['plan_sha256']
            and active.get('grant_sha256') == sha(stable_bytes(grant_path))
            and active.get('scope_revision_sha256') == scope_sha256
            and active.get('case_ids') == case_ids,
            'OFF_SUCCESSOR_PARENT_IDENTITY_DRIFT')
    known = [store.get_json(parent/'invocation.json'), *[r['next_identity'] for r in edges]]
    started = sorted((parent/'invocations').glob('*.started.json'))
    require(bool(started), 'OFF_SUCCESSOR_PARENT_NOT_TERMINAL')
    finished = []
    for item in started:
        require(store.get_json(item) in known, 'OFF_SUCCESSOR_START_IDENTITY_DRIFT')
        end = item.with_name(item.name.replace('.started.json', '.finished.json'))
        require(end.is_file() and not end.is_symlink(), 'OFF_SUCCESSOR_PARENT_NOT_TERMINAL')
        done = store.get_json(end)
        require(done.get('status') == 'RETURNED' and done.get('plan_sha256') == plan['plan_sha256'],
                'OFF_SUCCESSOR_PARENT_NOT_TERMINAL')
        finished.append(end)
    require(set((parent/'invocations').glob('*.finished.json')) == set(finished),
            'OFF_SUCCESSOR_INVOCATION_SET_DRIFT')
    # Old code is also a physical input, not just the parent label.
    for rel, wanted in active['execution']['source_sha256'].items():
        stable_bytes(Path(active['execution']['code_root'])/rel, wanted)
    require(not list((parent/'runtime-state/cache/argon-consumer').glob('*/batch/attempt-*/handle.json')),
            'OFF_SUCCESSOR_NATIVE_TASK_PRESENT')
    packages = {r['package'] for r in plan['samples'] if r['case_id'] in case_ids}
    for name in packages:
        run = json.loads(stable_bytes(parent/name/'run.json'))
        require(not run.get('input_drift') and run.get('comparison_execution', {}).get('argon_arm') == 'off'
                and file_sha(parent/name/'final.srt') == run['final_srt_sha256']
                and file_sha(parent/name/'original.srt') == plan['packages'][name]['srt_sha256'],
                'OFF_SUCCESSOR_PACKAGE_DRIFT')
    expected_link = str(Path(active['execution']['provider_runtime_root'])/'state/gemini-paid-backup')
    def inventory():
        files, links = [], []
        for directory, dirs, names in os.walk(parent, followlinks=False):
            for name in list(dirs)+names:
                path = Path(directory)/name
                mode = path.lstat().st_mode
                relative = str(path.relative_to(parent))
                if stat.S_ISLNK(mode):
                    target = os.readlink(path)
                    require(relative == 'runtime-state/state/gemini-paid-backup' and target == expected_link,
                            'OFF_SUCCESSOR_UNEXPECTED_LINK')
                    links.append({'path':relative,'target':target,'dereferenced':False})
                    if name in dirs:dirs.remove(name)
                elif stat.S_ISREG(mode):
                    require(not relative.startswith('runtime-state/state/'),
                            'OFF_SUCCESSOR_UNSUPPORTED_STATE_FILE')
                    files.append(relative)
                else:
                    require(stat.S_ISDIR(mode), 'OFF_SUCCESSOR_SPECIAL_FILE')
        require(len(links) == 1, 'OFF_SUCCESSOR_PAID_LEDGER_LINK_REQUIRED')
        return sorted(files), links
    files, links = inventory()
    records = [{'path':rel, 'bytes':len(stable_bytes(parent/rel)),
                'sha256':file_sha(parent/rel)} for rel in files]
    store.directory(out)
    for row in records:
        rel = row['path']
        # The regular runtime initializer makes a new locator and links the
        # same paid ledger. Keep its predecessor metadata as an exact original.
        destination = rel
        if rel == 'replay-state.json':
            destination = ('predecessor/replay-state.json' if 'predecessor/replay-state.json' not in files
                else 'predecessor/' + parent_identity_sha256 + '/replay-state.json')
        elif rel == 'off-output-successor.json':
            destination = 'predecessor/' + parent_identity_sha256 + '/off-output-successor.json'
        row['copied_to'] = destination
        store.immutable(out/destination, stable_bytes(parent/rel, row['sha256']))
    require(inventory() == (files, links), 'OFF_SUCCESSOR_PARENT_FILESET_CHANGED')
    for row in records:
        require(len(stable_bytes(parent/row['path'], row['sha256'])) == row['bytes'],
                'OFF_SUCCESSOR_PARENT_BYTES_CHANGED')
    receipt = {'schema_version':'comparison-off-output-successor.v1',
        'parent_output':str(parent), 'parent_identity_sha256':parent_identity_sha256,
        'parent_code_identity':predecessor, 'files':records, 'links':links,
        'current_input_kind':'PREDECESSOR_PRIVATE_FINAL_WITH_COMPLETE_TYPED_HISTORY',
        'copied_retry_and_unknown_history':True, 'copied_native_budgets':True,
        'payment_ledger_target':expected_link, 'reservation_reset':False,
        'provider_calls':0, 'quality_pass_inherited':False}
    receipt['sha256'] = sha(canonical(receipt))
    store.put_json(out/'off-output-successor.json', receipt)
    return receipt

def execute(args) -> int:
    """Use run_package's existing prepared reviewer and final gate, not a helper demo."""
    from scripts.subtitle_regression_eval import run_package, summarize_case

    require(args.comparison_grant is not None, "COMPARISON_COST_NOT_AUTHORIZED")
    # Must precede all provider configuration, credential loaders and runtime writes.
    plan, grant = authorize(args.comparison_plan, args.comparison_grant)
    from .argon_comparison_policy import load_revision, binding, paired_off_run

    revision_path = getattr(args, "comparison_scope_revision", None)
    revision = load_revision(revision_path, plan, args.comparison_grant) if revision_path else None
    arm = getattr(args, "argon_arm", "on")
    policy_identity = binding(revision, arm)
    off_root = getattr(args, "paired_off_output", None)
    require(
        (revision is None or arm == "off" or off_root is not None),
        "COMPARISON_OFF_BASELINE_REQUIRED",
    )
    selected = [
        s
        for s in plan["samples"]
        if s["case_id"] in grant["authorized_case_ids"]
        and (not args.case or s["case_id"] in args.case)
    ]
    require(
        selected and (not args.case or set(args.case) == {s["case_id"] for s in selected}),
        "COMPARISON_CASE_SELECTION_INVALID",
    )
    baselines = {key: baseline_state(plan, key) for key in plan["baseline"]}
    require(
        all(baselines[s["package"]]["status"] == "COMPLETED_STABLE" for s in selected),
        "COMPARISON_BASELINE_NOT_COMPLETE",
    )
    code = (args.code_root or Path(__file__).resolve().parents[2]).resolve()
    actual_execution = execution_binding(code, Path(args.runtime_root))
    out = args.output.resolve()
    output_parent = getattr(args, "comparison_output_predecessor", None)
    if output_parent is not None:
        require(revision is not None and arm == "off" and args.resume_comparison
                and getattr(args, "comparison_code_successor", None), "OFF_SUCCESSOR_MODE_INVALID")
        prepare_off_output_successor(Path(output_parent).absolute(), out,
            parent_identity_sha256=getattr(args, "comparison_output_predecessor_sha256", None),
            predecessor=args.comparison_code_successor, plan=plan, grant_path=args.comparison_grant,
            scope_sha256=revision['sha256'], case_ids=[s['case_id'] for s in selected])
    require(out.exists() is bool(args.resume_comparison), "COMPARISON_RESUME_IDENTITY_REQUIRED")
    if args.resume_comparison:
        require(
            (out / "invocation.json").is_file()
            and not (out / "evaluation.json").exists()
            and not (out / "resume-status.json").exists(),
            "COMPARISON_RESUME_IDENTITY_REQUIRED",
        )
    store.directory(out)
    with store.locked(out):
        attempts = out / "invocations"
        store.directory(attempts)
        incomplete = [
            p
            for p in attempts.glob("*.started.json")
            if not p.with_name(p.name.replace(".started.json", ".finished.json")).exists()
        ]
        require(not incomplete, "COMPARISON_PRIOR_INVOCATION_UNKNOWN")
        identity = {
            "plan_sha256": plan["plan_sha256"],
            "case_ids": [s["case_id"] for s in selected],
            "code_identity": args.code_identity or args.commit,
            "grant_sha256": sha(stable_bytes(args.comparison_grant)),
            "execution": actual_execution,
        }
        if revision is not None:
            identity.update(
                policy_identity, paired_off_output=str(off_root.resolve()) if off_root else None
            )
        if revision is not None and arm == "on":
            # Validate same-code OFF results BEFORE any provider/configuration
            # call, and freeze their exact bytes for the post-run comparison.
            for key in dict.fromkeys(s["package"] for s in selected):
                paired_off_run(off_root, key, identity)
            identity["paired_off_runs"] = {
                key: sha(stable_bytes(off_root / key / "run.json"))
                for key in dict.fromkeys(s["package"] for s in selected)
            }
        accept_execution_identity(
            out,
            identity,
            predecessor=getattr(args, "comparison_code_successor", None),
            plan_path=args.comparison_plan,
        )
        runtime = out / "runtime-state"
        store.directory(runtime)
        from .provider_slots import runtime_provider_slot

        results = {}
        attempt_id = str(time.time_ns())
        # Acquire the ORIGINAL normal provider pool as well as any nested runtime
        # admissions. Isolation must not create extra concurrency capacity.
        with comparison_environment(
            runtime, args.runtime_root, normal_routes=revision is not None
        ) as readiness:
            store.put_json(attempts / (attempt_id + ".started.json"), identity)
            for key in dict.fromkeys(s["package"] for s in selected):
                config = {
                    "enabled": arm == "on",
                    "allow_paid": False,
                    "allow_realtime_fallback": False,
                    "comparison_plan": str(args.comparison_plan.resolve()),
                    "comparison_grant": str(args.comparison_grant.resolve()),
                    "comparison_case_ids": [s["case_id"] for s in selected],
                }
                if revision is not None:
                    config.update(
                        comparison_scope_revision=str(revision_path.resolve()), argon_arm=arm
                    )
                # The ordinary entry sees only source/candidates, never scoring labels.
                with runtime_provider_slot(runtime_root=Path(args.runtime_root), timeout_seconds=1):
                    results[key] = run_package(
                        plan["packages"][key],
                        out / key,
                        code,
                        readiness["gemini"]
                        if revision is not None
                        else {
                            "source": "lazy canonical loader after authorization",
                            "configured_key_count": None,
                            "paid_backup_configured": False,
                        },
                        comparison_config=config,
                        witness_cache_root=Path(baselines[key]["run_path"]).parent,
                        resume=args.resume_comparison and (out / key).exists(),
                    )
                if (
                    revision is not None
                    and results[key].get("ordinary_route_validity", {}).get("status")
                    != "VALID_ROUTE_ATTEMPT_EVIDENCE"
                ):
                    break
        # Only after producer completion may the scoring file be opened.
        scoring = json.loads(stable_bytes(Path(plan["scoring_file"]), plan["scoring_sha256"]))
        cases = [c for c in scoring["cases"] if c["id"] in {s["case_id"] for s in selected}]
        comparisons = []
        for case in cases:
            baseline = json.loads(
                stable_bytes(
                    Path(baselines[case["package"]]["run_path"]),
                    baselines[case["package"]]["run_sha256"],
                )
            )
            current_run = results.get(case["package"], {})
            if revision is not None and arm == "on" and current_run:
                baseline = paired_off_run(off_root, case["package"], identity, current_run)
            before = summarize_case(case, baseline)
            after = summarize_case(case, current_run)
            if after["outcome"] == "EVALUATION_ERROR":
                change = "EVALUATION_ERROR"
            elif before["outcome"] == "PASS_REPAIRED":
                change = (
                    "PRESERVED_CORRECT"
                    if after["outcome"] == "PASS_REPAIRED"
                    else "CHANGED_CORRECT_TO_UNACCEPTED"
                )
            else:
                change = "REPAIRED" if after["outcome"] == "PASS_REPAIRED" else "UNREPAIRED"
            consumed = consumed_argon_ids(current_run, case["body_window_ms"])
            if change == "REPAIRED" and not consumed:
                change = "REPAIRED_WITHOUT_ARGON_EVIDENCE"
            route_valid = current_run.get("ordinary_route_validity", {}).get("status")
            if revision is not None and route_valid != "VALID_ROUTE_ATTEMPT_EVIDENCE":
                change = "INVALID_EVALUATION_CONFIGURATION"
            comparisons.append(
                {
                    "case_id": case["id"],
                    "argon_arm": arm if revision is not None else "legacy_on",
                    "evaluation_validity": route_valid
                    if revision is not None
                    else "LEGACY_CONFIGURATION_NOT_VALIDATED",
                    "baseline_kind": "SAME_CODE_OFF"
                    if revision is not None and arm == "on"
                    else "HISTORICAL_NOT_CAUSAL",
                    "baseline": before,
                    "with_argon": after,
                    "change": change,
                    "argon_acquisitions_consumed_by_cpa": consumed,
                }
            )
        iterations = out / "comparisons"
        store.directory(iterations)
        report = {
            "schema_version": "argon-paired-regression-result.v1",
            "code_identity": identity["code_identity"],
            "execution_identity_sha256": sha(canonical(identity)),
            "plan_sha256": plan["plan_sha256"],
            "results": comparisons,
            "release_authorized": False,
            "metric": "normal final subtitles against frozen scorer; not model agreement",
            "same_original_input": True,
        }
        store.put_json(iterations / (str(time.time_ns()) + ".json"), report)
        store.put_json(
            attempts / (attempt_id + ".finished.json"),
            {"status": "RETURNED", "plan_sha256": plan["plan_sha256"]},
        )
        print(
            json.dumps(
                {"results": [{k: r[k] for k in ("case_id", "change")} for r in comparisons]},
                ensure_ascii=False,
            )
        )
        return (
            0
            if all(
                r["with_argon"]["outcome"] == "PASS_REPAIRED"
                and r["change"] != "INVALID_EVALUATION_CONFIGURATION"
                for r in comparisons
            )
            else 1
        )
