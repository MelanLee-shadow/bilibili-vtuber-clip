#!/usr/bin/env python3
"""Replay reported errors through the ordinary exact-final repair gate on OCI3.

The controller uses the caller's Python. The worker uses OCI3's production venv,
reads original package/media files and writes only a new private scratch folder.
Expected answers are used exclusively by summarize_case, never by the reviewer.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager, nullcontext
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import time
import unicodedata
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.autoslice.review_evidence_store import review_from_bytes
from src.autoslice.subtitle_regression_recovery import stable
DEFAULT_CASES = ROOT / "tests/fixtures/subtitle_regressions/cases.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value: object) -> None:
    from src.autoslice.final_review_carryover import _write_bytes_atomic
    _write_bytes_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def configure_replay_state(output: Path, runtime_root: Path, *, resume: bool = False) -> dict:
    """Fresh experiment history, with the real payment policy/account intact.

    Original-package replay is not a production continuation. Reusing the
    production semantic-attempt cache can block a changed generator before
    its first call and can contaminate the next production retry in turn.
    """
    base = output / "runtime-state"
    paid_ledger = runtime_root / "state" / "gemini-paid-backup"
    if resume:
        previous = json.loads((output / "replay-state.json").read_text())
        if (base.is_symlink() or not base.is_dir()
                or previous != {"experiment_state_root": str(base),
                    "production_runtime_root": str(runtime_root),
                    "production_retry_history_consumed": False,
                    "paid_backup_ledger": str(paid_ledger), "payment_policy_changed": False}
                or not (base / "state" / "gemini-paid-backup").is_symlink()
                or (base / "state" / "gemini-paid-backup").resolve() != paid_ledger.resolve()):
            raise ValueError("private replay state or payment ledger binding drift")
        # A code repair does not reset material novelty, UNKNOWN outcomes,
        # native budgets or the production paid ledger.
        os.environ["AUTOSLICE_BASE"] = str(base)
        return previous
    (base / "state").mkdir(parents=True, exist_ok=False)
    (base / "state" / "gemini-paid-backup").symlink_to(paid_ledger, target_is_directory=True)
    os.environ["AUTOSLICE_BASE"] = str(base)
    return {"experiment_state_root": str(base), "production_runtime_root": str(runtime_root),
            "production_retry_history_consumed": False,
            "paid_backup_ledger": str(paid_ledger), "payment_policy_changed": False}


def load_production_gemini_environment() -> dict:
    """Use the producer's allowlisted loader; only names/counts leave memory."""
    from scripts.session_autoslice import BILIVE_ENV, load_gemini_credentials
    from src.autoslice.agy_gemini_client import free_api_keys
    from src.autoslice.gemini_backup_policy import paid_backup_key

    credentials = load_gemini_credentials()
    if not credentials:
        raise RuntimeError("production Gemini credential file supplied no credentials")
    os.environ.update(credentials)
    return {"source": str(BILIVE_ENV), "loader": "scripts.session_autoslice.load_gemini_credentials",
            "loaded_variable_names": sorted(credentials),
            "configured_key_count": len(free_api_keys()),
            "configured_key_count_scope": "distinct free keys, matching the production ladder",
            "paid_backup_configured": bool(paid_backup_key()),
            "credential_values_recorded": False}


@contextmanager
def observe_gemini_http_attempts(events: list[dict]):
    """Observe the ordinary inline transport without changing routing/retries.

    The shared HTTP receipt lacks key tier. Record only the key's ordinal/tier,
    status and timing at the actual urlopen seam; never serialize headers/URLs.
    """
    from src.autoslice.agy_gemini_client import free_api_keys
    from src.autoslice.gemini_backup_policy import paid_backup_key

    free_keys, paid_key = free_api_keys(), paid_backup_key()
    original = urllib.request.urlopen

    def observed(request, *args, **kwargs):
        url = getattr(request, "full_url", "")
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname != "generativelanguage.googleapis.com" or not parsed.path.endswith(":generateContent"):
            return original(request, *args, **kwargs)
        key = next((v for k, v in request.header_items() if k.lower() == "x-goog-api-key"), None)
        ordinal = free_keys.index(key) + 1 if key in free_keys else (len(free_keys) + 1 if paid_key and key == paid_key else None)
        row = {"provider": "gemini_api", "key_tier": "free" if key in free_keys else ("paid_backup" if paid_key and key == paid_key else "unknown"),
               "key_ordinal": ordinal, "http_status": None, "error_type": None}
        events.append(row)
        started = time.monotonic()
        try:
            response = original(request, *args, **kwargs)
            row["http_status"] = getattr(response, "status", None)
            return response
        except Exception as exc:
            row.update(http_status=getattr(exc, "code", None), error_type=type(exc).__name__)
            raise
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 3)

    urllib.request.urlopen = observed
    try:
        yield
    finally:
        urllib.request.urlopen = original


def body_ms(finished_ms: int, intro_offset_ms: int) -> int:
    if type(finished_ms) is not int or type(intro_offset_ms) is not int:
        raise ValueError("timestamps must be integer milliseconds")
    if intro_offset_ms < 0 or finished_ms < intro_offset_ms:
        raise ValueError("timestamp is inside the intro or offset is invalid")
    return finished_ms - intro_offset_ms


def load_cases(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != "subtitle-regressions.v1":
        raise ValueError("unsupported fixture schema")
    seen = set()
    if not doc.get("cases"):
        raise ValueError("empty regression suite")
    for case in doc["cases"]:
        if case["id"] in seen:
            raise ValueError("duplicate case id")
        seen.add(case["id"])
        package = doc["packages"][case["package"]]
        if case["candidate_id"] != package["candidate_id"]:
            raise ValueError("candidate binding mismatch")
        if not case.get("source", {}).get("quote"):
            raise ValueError("source quotation missing")
        if case["status"] == "ready":
            window = case["body_window_ms"]
            if not case["cue_ids"] or not case["wrong_text"] or not (0 <= window[0] < window[1]):
                raise ValueError("ready case has no exact localization")
            if [body_ms(t, package["intro_offset_ms"]) for t in case["finished_window_ms"]] != window:
                raise ValueError("intro/body clock mismatch")
            if case["expected"]["required_severity"] != "critical":
                raise ValueError("reported semantic errors must remain critical")
        elif case["status"] != "pending_localization" or not case.get("missing_input"):
            raise ValueError("pending case must name missing human evidence")
    return doc


def normalized_text(text: str) -> str:
    # No homophone, pronoun, numeral, language or speaker normalization.
    return "".join(c for c in unicodedata.normalize("NFC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def finding_rows(audit: dict) -> list[dict]:
    return [row for key in ("findings", "resolved_findings", "unresolved_findings_disclosed")
            for row in audit.get(key, []) if isinstance(row, dict)]


def target_finding(case: dict, audit: dict, row: dict) -> bool:
    """Bind a finding to its own review input grid, including cue renumbering."""
    index = row.get("cue_index", row.get("cue"))
    cue = next((c for c in audit.get("evaluation_input_cues", []) if c["index"] == index), None)
    if cue is None:
        return False
    if row.get("base_text_sha256") and row["base_text_sha256"] != hashlib.sha256(cue["text"].encode()).hexdigest():
        return False
    return cue["start_ms"] < case["body_window_ms"][1] and cue["end_ms"] > case["body_window_ms"][0]


def validate_original_cases(cases: list[dict], cues: list[dict]) -> None:
    by_index = {c["index"]: c for c in cues}
    for case in cases:
        if case["status"] != "ready":
            continue
        selected = [by_index[i] for i in case["cue_ids"]]
        if ("\n".join(c["text"] for c in selected) != case["wrong_text"]
                or [selected[0]["start_ms"], selected[-1]["end_ms"]] != case["body_window_ms"]):
            raise ValueError("fixture does not locate its original erroneous text: " + case["id"])


def summarize_case(case: dict, run: dict) -> dict:
    """Score observations; the production gate, not this scorer, decides release.

    A different critical error, a provider outage, or an unrelated gate failure
    cannot count as successfully catching this case. Pending labels never pass.
    """
    result = {"case_id": case["id"], "candidate_id": case["candidate_id"],
              "detected": None, "severity": "UNKNOWN", "final_text": None,
              "text_match": "UNASSESSED", "would_release": run.get("would_release"),
              "outcome": "UNASSESSED", "reason": ""}
    if "gemini_environment" in run:
        result["configured_key_count"] = run["gemini_environment"]["configured_key_count"]
        result["provider_route_counts"] = run.get("provider_usage", {}).get("route_counts", {})
        result["provider_route_scope"] = "whole candidate replay; shared by cases of the same candidate"
    if case["status"] != "ready":
        return {**result, "outcome": "PENDING_HUMAN_EVIDENCE", "reason": case["missing_input"]}
    rows = [row for audit in run.get("reviews", []) for row in finding_rows(audit)
            if target_finding(case, audit, row)]
    risks = sorted({str(row.get("content_risk", "UNKNOWN")) for row in rows})
    texts = [cue["text"] for cue in run.get("final_cues", [])
             if cue["start_ms"] < case["body_window_ms"][1]
             and cue["end_ms"] > case["body_window_ms"][0]]
    final_text = "\n".join(texts)
    expected = case["expected"]
    correct = expected.get("correct_text") or expected.get("accepted_reference_text")
    match = "NO_HUMAN_VERBATIM_TRUTH"
    if correct:
        if normalized_text(final_text) == normalized_text(correct):
            match = "EXACT_NORMALIZED" if expected.get("correct_text") else "TASK_ACCEPTED_REFERENCE"
        elif any(normalized_text(final_text) == normalized_text(alt)
                 for alt in expected.get("equivalent_texts", [])):
            match = "CURATED_SEMANTIC_EQUIVALENT"
        elif normalized_text(final_text) == normalized_text(case["wrong_text"]):
            match = "INCORRECT_UNCHANGED"
        else:
            match = "UNREVIEWED_DIFFERENT_TEXT"
    final_audit = (run.get("reviews") or [{}])[-1]
    current_risks = sorted({str(row.get("content_risk", "UNKNOWN")) for row in finding_rows(final_audit)
                            if target_finding(case, final_audit, row)})
    critical = any(risk in {"meaning_reversal", "factual_error", "potential_harm"} for risk in current_risks)
    blocking_target = any(target_finding(case, final_audit, row)
                          and row.get("content_risk") in {"meaning_reversal", "factual_error", "potential_harm"}
                          for row in final_audit.get("findings", []))
    observed = any(a.get("discovery", {}).get("status") == "COMPLETE" for a in run.get("reviews", []))
    completed = final_audit.get("discovery", {}).get("status") == "COMPLETE"
    correct_or_equivalent = match in {"EXACT_NORMALIZED", "CURATED_SEMANTIC_EQUIVALENT", "TASK_ACCEPTED_REFERENCE"}
    result.update(detected=bool(rows) if observed else None,
                  severity="CRITICAL" if critical else ("NONCRITICAL" if current_risks == ["noncritical_uncertainty"] else ("RESOLVED" if correct_or_equivalent else "UNKNOWN")),
                  content_risks=current_risks, observed_content_risks=risks, final_text=final_text, text_match=match,
                  matches_correct_or_equivalent=True if correct_or_equivalent else (False if match == "INCORRECT_UNCHANGED" else None),
                  matching_findings=rows)
    if run.get("input_drift") or not completed:
        result.update(outcome="EVALUATION_ERROR", reason=run.get("error") or "input drift or discovery incomplete")
    elif correct_or_equivalent and run.get("would_release") is not None:
        result.update(outcome="PASS_REPAIRED", reason="ordinary typed repair matches the human reference or an explicitly curated equivalent")
    elif blocking_target and run.get("would_release") is False:
        result.update(outcome="PASS_BLOCKED_CRITICAL", reason="已拦未修：this target remains critical and blocked; repair acceptance has not passed")
    else:
        result.update(outcome="REGRESSION", reason="target was not repaired or retained as a blocking critical finding")
    return result


def aggregate(results: list[dict]) -> dict:
    counts = {key: sum(r["outcome"] == key for r in results) for key in
              ("PASS_REPAIRED", "PASS_BLOCKED_CRITICAL", "REGRESSION", "EVALUATION_ERROR", "PENDING_HUMAN_EVIDENCE")}
    return {"counts": counts, "pass": bool(results) and all(r["outcome"] == "PASS_REPAIRED" for r in results),
            "scored_count": sum(r["outcome"] != "PENDING_HUMAN_EVIDENCE" for r in results)}


def markdown_report(report: dict) -> str:
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    lines = ["# 字幕错误回归评测", "", f"代码：`{report['code_identity']}`。入口：普通 exact-final discovery / typed repair / release validator。",
             "终审放行只指字幕门；本评测不运行发布或修改线上包。人工答案只用于结果比较。", "",
             "|样例|发现|严重度|最终文字|与人工答案比较|字幕门放行|结果|", "|---|---|---|---|---|---|---|"]
    for r in report["results"]:
        lines.append("|" + "|".join(cell(r.get(k)) for k in
                     ("case_id", "detected", "severity", "final_text", "text_match", "would_release", "outcome")) + "|")
    lines += ["", "汇总：`" + json.dumps(report["summary"], ensure_ascii=False) + "`", "", "## 剩余项", ""]
    if "gemini_environment" in report:
        env = report["gemini_environment"]
        lines[4:4] = [f"Gemini configured_key_count（去重免费 key）={env['configured_key_count']}；付费备用已配置={env['paid_backup_configured']}。来源：`{env['source']}`。", ""]
        position = lines.index("## 剩余项")
        lines[position:position] = ["## 实际 provider 路线（按候选计，共用次数不重复相加）", "",
            "|候选|AGY 执行|免费 key HTTP|付费 key HTTP|", "|---|---:|---:|---:|"] + [
            "|" + "|".join(str(v) for v in (key, *[run.get("provider_usage", {}).get("route_counts", {}).get(k) for k in ("agy_executions", "gemini_free_http_attempts", "gemini_paid_http_attempts")])) + "|"
            for key, run in report.get("runs", {}).items()] + [""]
    pending = [r for r in report["results"] if r["outcome"] == "PENDING_HUMAN_EVIDENCE"]
    for r in pending:
        lines.append(f"- 外部阻塞：{r['reason']}｜需要 维护者 / 原听音研究 owner 提供｜补入精确样例并重跑。")
    if not pending:
        lines.append("无（评测执行范围；发现的产品回归由整合会话修复）。")
    return "\n".join(lines) + "\n"



def _is_private_experiment_directory(output: Path, runtime_root: Path) -> bool:
    """Allow the existing external layout or a real private-runs/task/run leaf."""
    if output.is_symlink() or not output.is_dir():
        return False
    try:
        physical = output.resolve(strict=True)
        runtime = runtime_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    lexical = output.absolute()
    if (not physical.is_relative_to(runtime)
            and not lexical.is_relative_to(runtime_root.absolute())):
        return True
    # This narrow runtime exception cannot be reached through current, a
    # symlink ancestor, a parent directory, or a similarly named prefix.
    if lexical != physical:
        return False
    try:
        relative = physical.relative_to(runtime / "private-runs")
    except ValueError:
        return False
    return len(relative.parts) == 2


def prepare_experiment_resume(output: Path, cases_path: Path, cases: list[dict],
                              code_identity: str, runtime_root: Path) -> dict:
    """Archive a terminal private experiment before normal-code continuation.

    No answer, candidate, severity or scoring function is supplied to the
    repair chain. Existing audio caches, budgets and exact request histories
    stay in their original paths; failed/unknown requests are not refunded.
    """
    if not _is_private_experiment_directory(output, runtime_root):
        raise ValueError("resume requires an existing private experiment")
    if (output / "invocation.json").exists() or any(output.glob("*/comparison-invocation.json")):
        raise ValueError("COMPARISON_REQUIRES_SAME_INVOCATION_RESUME")
    previous_path = output / "evaluation.json"
    if previous_path.is_symlink() or not previous_path.is_file():
        raise ValueError("prior evaluation is not terminal")
    previous = json.loads(previous_path.read_text())
    resume_status = output / "resume-status.json"
    if resume_status.exists() and json.loads(resume_status.read_text()).get("status") != "COMPLETED":
        raise ValueError("previous private continuation is incomplete; reconcile its outcome")
    if (previous.get("schema_version") != "subtitle-regression-evaluation.v1"
            or previous.get("fixture_sha256") != digest(cases_path)
            or previous.get("rescore_source")
            or not previous.get("code_identity")
            or previous["code_identity"] == code_identity
            or {row["case_id"] for row in previous.get("results", [])}
            != {row["id"] for row in cases}):
        raise ValueError("resume fixture, selection or source identity mismatch")
    for key in dict.fromkeys(row["package"] for row in cases if row["status"] == "ready"):
        folder = output / key
        run = json.loads((folder / "run.json").read_text())
        if (run.get("input_drift") or not run.get("final_srt_sha256")
                or digest(folder / "final.srt") != run["final_srt_sha256"]
                or not isinstance(run.get("input_hashes"), dict)
                or any(digest(Path(path)) != sha for path, sha in run["input_hashes"].items())
                or not (folder / "authority.json").is_file()):
            raise ValueError("resume input, actual text or source binding drift")
    # Validate the existing isolated state before copying or changing anything.
    configure_replay_state(output, runtime_root, resume=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = output.parent / "resume-history" / stamp
    archive.mkdir(parents=True, exist_ok=False)
    files = []
    allowed = {".json", ".jsonl", ".srt", ".txt", ".md", ".log", ".stdout", ".stderr"}
    for directory, dirs, names in os.walk(output, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or path.suffix not in allowed:
                continue
            rel = path.relative_to(output)
            target = archive / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("prior experiment changed during archive")
            target.write_bytes(data)
            files.append({"path": str(rel), "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
    record = {"schema_version": "subtitle-regression-resume.v1", "archive": str(archive),
              "parent_evaluation_sha256": digest(previous_path),
              "parent_code_identity": previous["code_identity"], "code_identity": code_identity,
              "fixture_sha256": digest(cases_path), "cases": [row["id"] for row in cases],
              "private_state_root": str(output / "runtime-state"), "history_reset": False,
              "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files}
    write_json(archive / "ARCHIVE.json", record)
    return record


def stage_normal_source_media(source: Path, out: Path, expected_sha256: str, *,
                              completed_record: dict, clip_context: dict,
                              candidate_id: str) -> Path:
    """Materialize the ordinary consumer through the shared source contract."""
    from src.autoslice.media_copy_provenance import materialize_media_copy
    return materialize_media_copy(
        source=source, target=out / ("normal-off-source" + source.suffix),
        expected_sha256=expected_sha256, completed_record=completed_record,
        clip_context=clip_context, candidate_id=candidate_id,
    )


def normal_off_samples(package: dict, cases: list[dict], cues: list[dict]) -> list[dict]:
    """Build blind probes from source identity/time only; answers stay in scoring."""
    samples = []
    for case in cases:
        start, end = case["body_window_ms"]
        if (type(start) is not int or type(end) is not int or not 0 <= start < end):
            raise ValueError("normal OFF target clock invalid")
        matches = [i for i, cue in enumerate(cues)
                   if cue["start_ms"] < end and cue["end_ms"] > start]
        if not matches:
            raise ValueError("normal OFF target missing")
        left, right = max(0, matches[0] - 2), min(len(cues) - 1, matches[-1] + 2)
        samples.append({
            "case_id": case["id"], "candidate_id": package["candidate_id"],
            "source_media_sha256": package["media_sha256"],
            "target_source_ms": [start, end],
            "context_source_ms": [min(start, cues[left]["start_ms"]),
                                  max(end, cues[right]["end_ms"])],
        })
    if not samples:
        raise ValueError("normal OFF requires selected ready targets")
    return samples


def run_package(package: dict, out: Path, code_root: Path, gemini_environment: dict, *,
                comparison_config: dict | None = None, witness_cache_root: Path | None = None,
                resume: bool = False, normal_samples: list[dict] | None = None,
                recovery: dict | None = None, checkpoint_identity: dict | None = None,
                preparation_only: bool = False) -> dict:
    """Only ordinary production functions below may judge or mutate subtitles."""
    if preparation_only and (normal_samples is None or comparison_config is not None):
        raise ValueError("preparation preflight requires ordinary normal OFF input")
    if recovery is not None and (resume or normal_samples is None):
        raise ValueError("interrupted package input requires the explicit normal OFF successor")
    if normal_samples is not None and comparison_config is not None:
        raise ValueError("normal OFF and Argon comparison identities cannot be mixed")
    normal_identity = None if normal_samples is None else {
        "argon_enabled": False, "speaker_attribution_enabled": False,
        "ordinary_audio_policy": "NORMAL_PRODUCTION_ROUTES",
        "scoring_answers_in_probes": False,
        "probe_samples_sha256": hashlib.sha256(json.dumps(
            normal_samples, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
    }
    if comparison_config is None and (out / "comparison-invocation.json").exists():
        raise ValueError("COMPARISON_PACKAGE_IDENTITY_REQUIRED")
    if comparison_config is not None:
        from src.autoslice.argon_regression_comparison import authorize, execution_binding, stable_bytes, validate_package_resume_identity
        from src.autoslice import argon_escalation_storage as comparison_store
        if not comparison_config.get("comparison_plan") or not comparison_config.get("comparison_grant"):
            raise ValueError("COMPARISON_COST_NOT_AUTHORIZED")
        comparison_plan, _ = authorize(Path(comparison_config["comparison_plan"]), Path(comparison_config["comparison_grant"]))
        from src.autoslice.argon_comparison_policy import load_revision, binding
        revision_path = comparison_config.get("comparison_scope_revision")
        revision = load_revision(Path(revision_path), comparison_plan, Path(comparison_config["comparison_grant"])) if revision_path else None
        arm = comparison_config.get("argon_arm", "on")
        policy_identity = binding(revision, arm)
        if revision is not None and comparison_config.get("enabled") is not (arm == "on"):
            raise ValueError("COMPARISON_ARM_INVALID")
        if comparison_config.get("allow_paid") or comparison_config.get("allow_realtime_fallback"):
            raise ValueError("COMPARISON_ROUTE_FORBIDDEN")
        comparison_identity = {
            "plan_sha256": comparison_plan["plan_sha256"],
            "grant_sha256": hashlib.sha256(stable_bytes(Path(comparison_config["comparison_grant"]))).hexdigest(),
            "case_ids": comparison_config.get("comparison_case_ids"),
            "package": package,
            "cache_root": str(witness_cache_root.resolve()) if witness_cache_root else None,
            "execution": execution_binding(code_root, Path(os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
                                          or os.environ.get("AUTOSLICE_BASE") or code_root)),
        }
        if revision is not None:
            comparison_identity.update(policy_identity)
        if resume:
            validate_package_resume_identity(out, comparison_identity)
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_text_pipeline import TextPipelineAdapters, build_prepared_exact_final_reviewer
    from src.autoslice.producer_package_finalization import CurrentTextReviewArtifacts, _run_exact_final_review_gate
    from src.autoslice.cpa_runtime import bind_observation
    from src.autoslice.clip_context import validate_clip_context
    from scripts import produce_slice_package as producer

    if not resume and recovery is None:
        out.mkdir(mode=0o700)
    local_srt, local_authority = out / "final.srt", out / "authority.json"
    previous_run_raw = None
    resume_parent_run = None
    if resume:
        from src.autoslice.argon_escalation_storage import immutable
        previous_run_raw = stable(out / "run.json")
        previous_run_sha256 = hashlib.sha256(previous_run_raw).hexdigest()
        archive = out / "run-history" / (previous_run_sha256 + ".json")
        # Preserve the only original before checkpoint() can replace run.json.
        # A failed archive write must leave that original untouched.
        immutable(archive, previous_run_raw)
        resume_parent_run = {
            "original_path": str(out / "run.json"), "archive_path": str(archive),
            "sha256": previous_run_sha256, "bytes": len(previous_run_raw),
            "validation": "PENDING",
        }
    started = time.monotonic()
    reviews = []
    run = {"candidate_id": package.get("candidate_id"), "would_release": None,
           "reviews": reviews, "error": None, "lifecycle": "PREPARING",
           "quality_outcome": "NOT_EVALUATED", "preparation_status": "STARTED",
           "input_hashes": {}, "declared_input_hashes": {
               str(package[k]): package.get(k.replace("_path", "_sha256"))
               for k in ("record_path", "srt_path", "authority_path", "media_path", "context_path")
               if k in package},
           "execution_identity": checkpoint_identity or {"code_root": str(code_root.resolve())}}
    if normal_identity is not None:
        run["normal_off"] = normal_identity
    if recovery is not None:
        run["interrupted_parent"] = recovery

    if resume_parent_run is not None:
        run["resume_parent_run"] = resume_parent_run

    authority_not_supplied = object()

    def checkpoint(phase, *, text=None, actual_authority=authority_not_supplied,
                   reject_invalid=True):
        run["phase"] = phase
        write_json(out / "run.json", run)
        if checkpoint_identity is None and normal_samples is None:
            return
        from src.autoslice.subtitle_regression_recovery import package_checkpoint
        raw_authority, invalid = None, None
        authority_value = actual_authority
        if actual_authority is authority_not_supplied:
            authority_value = None
            if local_authority.is_file():
                state = "READ_ERROR"
                try:
                    raw_authority = stable(local_authority)
                    state = "INVALID_JSON"
                    authority_value = review_from_bytes(
                        raw_authority, directory=local_authority.parent
                    )
                    state = "INVALID_TYPE"
                    if not isinstance(authority_value, dict):
                        raise ValueError("checkpoint authority must be a JSON object")
                except (OSError, ValueError, UnicodeError) as exc:
                    authority_value = None
                    invalid = {"state": state, "original_path": str(local_authority),
                               "raw_sha256": hashlib.sha256(raw_authority).hexdigest()
                                   if raw_authority is not None else None,
                               "raw_bytes": len(raw_authority) if raw_authority is not None else None,
                               "error": {"type": type(exc).__name__, "message": str(exc)}}
                    if raw_authority is not None:
                        from src.autoslice.media_copy_provenance import file_snapshot
                        try:
                            invalid["source_snapshot"] = file_snapshot(local_authority, invalid["raw_sha256"])
                        except (OSError, ValueError) as snapshot_error:
                            invalid["snapshot_error"] = {
                                "type": type(snapshot_error).__name__, "message": str(snapshot_error)}
                    run.setdefault("checkpoint_input_errors", []).append({"phase": phase, **invalid})
                    write_json(out / "run.json", run)
        package_checkpoint(
            out, phase=phase, run=copy.deepcopy(run),
            text=(local_srt.read_bytes() if local_srt.is_file() else None) if text is None else text,
            authority=authority_value,
            authority_raw=raw_authority if invalid is not None else None,
            authority_input=invalid)
        if invalid is not None and reject_invalid:
            raise ValueError("PACKAGE_CHECKPOINT_AUTHORITY_" + invalid["state"]
                             + ": " + invalid["error"]["message"])

    try:
        checkpoint("PACKAGE_PREPARATION_STARTED")
        iteration = len(list(out.glob("run-history-*.json"))) + 1
        tracked = {Path(package[key]): package[key.replace("_path", "_sha256")]
                   for key in ("record_path", "srt_path", "authority_path", "media_path")}
        for path, sha in tracked.items():
            if digest(path) != sha:
                raise ValueError(f"original input hash drift: {path}")
        record = json.loads(Path(package["record_path"]).read_text())
        if record["subtitle_audio_correspondence"]["intro_offset_ms"] != package["intro_offset_ms"]:
            raise ValueError("package intro offset drift")
        authority_path = Path(package["authority_path"])
        authority = review_from_bytes(
            stable(authority_path), directory=authority_path.parent
        )
        context_path = Path(package["context_path"])
        context_sha = digest(context_path)
        tracked[context_path] = context_sha
        context = json.loads(context_path.read_text())
        cid = package["candidate_id"]
        validate_clip_context(context, candidate_id=cid)
        if context["context_sha256"] != record["clip_context_payload_sha256"]:
            raise ValueError("original record/context payload binding drift")
        run["input_hashes"] = {str(p): h for p, h in tracked.items()}
        run["context_sha256"] = context_sha
        original = Path(package["srt_path"]).read_bytes()
        local_srt = out / "final.srt"
        local_authority = out / "authority.json"
        parent_run_sha256 = None
        if recovery is not None:
            saved = json.loads((out.parent / "interrupted-resume.json").read_text())
            if (saved["packages"].get(out.name) != recovery
                    or recovery["candidate_id"] != cid
                    or digest(local_srt) != recovery["current_srt_sha256"]
                    or digest(local_authority) != recovery["authority_sha256"]
                    or (out / "original.srt").read_bytes() != original
                    or recovery["normal_samples"] != normal_samples
                    or any(recovery["input_hashes"].get(str(p)) != h for p, h in tracked.items())):
                raise ValueError("interrupted staged package input drift")
            authority = review_from_bytes(
                stable(local_authority), directory=local_authority.parent
            )
            parent_run_sha256 = recovery["prior_run_sha256"]
        elif resume:
            prior_run = json.loads(previous_run_raw)
            if prior_run.get("normal_off") != normal_identity:
                raise ValueError("normal OFF scope or ordinary execution mode drift")
            if (prior_run.get("candidate_id") != cid
                    or prior_run.get("input_hashes") != {str(p): h for p, h in tracked.items()}
                    or prior_run.get("final_srt_sha256") != digest(local_srt)
                    or (out / "original.srt").read_bytes() != original):
                raise ValueError("resume package authority or current text drift")
            authority = review_from_bytes(
                stable(local_authority), directory=local_authority.parent
            )
            if not isinstance(authority, dict):
                raise ValueError("resume package authority or current text drift")
            parent_run_sha256 = hashlib.sha256(previous_run_raw).hexdigest()
            run["parent_run_sha256"] = parent_run_sha256
            resume_parent_run["validation"] = "VALIDATED"
        else:
            local_srt.write_bytes(original)
            (out / "original.srt").write_bytes(original)
            write_json(local_authority, authority)
        if comparison_config is not None:
            if not resume:
                comparison_store.put_json(out / "comparison-invocation.json", comparison_identity)
            if resume:
                for filename in ("run.json", "final.srt", "authority.json"):
                    saved = out / ("run-history-%03d-" % iteration + filename)
                    comparison_store.immutable(saved, previous_run_raw if filename == "run.json"
                                               else (out / filename).read_bytes())
        spec = {"candidate_id": cid, "date": context["recording_date"],
                "clip_context": context, "pieces": context["pieces"],
                "selection_hook": context.get("selection_hook", "")}
        if comparison_config is not None:
            # Evaluation policy is consumed by this ordinary entry, not silently
            # added to the native engine's deliberately narrow config contract.
            spec["argon_escalation"] = {k: v for k, v in comparison_config.items()
                                        if k not in {"comparison_scope_revision", "argon_arm"}}
            # Effect comparison isolates Argon. Joint opt-in is tested separately;
            # old S2 metadata must not introduce a second experimental change.
            spec["speaker_attribution_generation"] = {"enabled": False}
            spec["comparison_ordinary_cache_only"] = revision is None
            spec["comparison_witness_cache_root"] = str(witness_cache_root)
            record = {**record, "subtitle_path": str(out / "original.srt")}
        if normal_samples is not None:
            # Independent ordinary validation needs no old Argon plan/grant.
            # Neither the scorer's answers nor an ON escalation enter this spec.
            spec["argon_escalation"] = {"enabled": False}
            spec["speaker_attribution_generation"] = {"enabled": False}
        adapters = TextPipelineAdapters(
            build_aggregate_transcriber=producer._build_aggregate_asr_transcriber,
            build_agy_transcriber=producer._build_ssh_agy_transcribe_runner,
            load_term_boundary_surfaces=producer._load_term_boundary_surfaces,
            profile_asset_file=producer.profile_asset_file, review_glossary=producer._review_glossary,
            topic_graph_disabled=producer._topic_graph_disabled, topic_graph_path=producer._topic_graph_path,
            topic_graph_expected_sha256=producer._topic_graph_expected_sha256,
        )
        review_media = Path(package["media_path"])
        if normal_samples is not None:
            checkpoint("SOURCE_MATERIALIZATION_STARTED")
            review_media = stage_normal_source_media(
                review_media, out, package["media_sha256"], completed_record=record,
                clip_context=context, candidate_id=cid)
            run["source_media_input"] = {
                "path": str(review_media), "original_path": package["media_path"],
                "sha256": digest(review_media),
                "provenance_path": str(review_media.with_suffix(".provenance.json")),
                "provenance_sha256": digest(review_media.with_suffix(".provenance.json")),
                "materialization": "INDEPENDENT_BYTE_COPY"}
            checkpoint("SOURCE_MATERIALIZED")
        checkpoint("PREPARED_CONSTRUCTOR_STARTED")
        reviewer = build_prepared_exact_final_reviewer(
            spec=spec, out_root=out, cid=cid, padded=review_media, host="localhost",
            authority=copy.deepcopy(authority), completed_record=record, adapters=adapters, native_audio_state=None,
        )
        run["prepared_source_boundary_clock"] = getattr(reviewer, "source_boundary_clock", None)
        run["preparation_status"] = "PREPARED"
        checkpoint("PREPARED_CONSTRUCTOR_RETURNED")
        # In-flight review-input-N remains an immutable failed attempt too.
        review_offset = max((int(p.stem.split("-")[-1]) for p in
                             [*out.glob("review-*.json"), *out.glob("review-input-*.srt")]
                             if p.stem.split("-")[-1].isdigit()), default=0)

        def observed_review(*args):
            with (out / f"review-input-{review_offset + len(reviews) + 1:02d}.srt").open("x") as stream:
                stream.write(args[0])
                stream.flush()
                os.fsync(stream.fileno())
            checkpoint("REVIEW_STARTED", text=args[0].encode(), actual_authority=args[1])
            audit = reviewer(*args)
            observation = copy.deepcopy(audit)
            observation["evaluation_input_cues"] = [
                {"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                for i, c in enumerate(parse_srt_cues(args[0]), 1)]
            reviews.append(observation)
            write_json(out / f"review-{review_offset + len(reviews):02d}.json", observation)
            checkpoint("REVIEW_RETURNED", text=args[0].encode(), actual_authority=args[1])
            return audit

        run.update({"candidate_id": cid, "would_release": None, "reviews": reviews,
               "gemini_environment": gemini_environment,
               "input_hashes": {str(p): h for p, h in tracked.items()}, "context_sha256": context_sha,
               "intro_offset_ms": package["intro_offset_ms"], "error": None,
               "input_stage": "original published delivery SRT, original context/authority and unbranded source recut; fresh ordinary final review",
               "historical_release_gate": authority.get("final_review_audit", {}).get("release_gate")})
        if comparison_config is not None:
            run["comparison_execution"] = comparison_identity
        if normal_identity is not None:
            run["normal_off"] = normal_identity
            run["source_media_input"].update(path=str(review_media),
                original_path=package["media_path"], sha256=package["media_sha256"])
        if resume or recovery is not None:
            run.update(input_stage="ordinary continuation of prior private final SRT/authority; no reset of audio or retry history",
                       parent_run_sha256=parent_run_sha256, review_offset=review_offset)
        if recovery is not None:
            run["interrupted_parent"] = recovery
        if checkpoint_identity is not None:
            run["execution_identity"] = checkpoint_identity
        checkpoint("PACKAGE_READY")
        if preparation_only:
            if run["prepared_source_boundary_clock"] is None:
                raise ValueError("PREPARED_PREFLIGHT_CLOCK_RECEIPT_REQUIRED")
            run.update(lifecycle="COMPLETED", quality_outcome="NOT_EVALUATED",
                       final_srt_sha256=digest(local_srt), input_drift=[],
                       provider_usage={"new_provider_calls": 0, "cost_usd": None,
                                       "historical_accounting": (recovery or {}).get("accounting")},
                       provider_dispatch_disabled=True)
            checkpoint("PACKAGE_PREPARED_NO_PROVIDER")
            return run
        run["lifecycle"] = "RUNNING"
        trace = out / "provider-usage.jsonl"
        os.environ["AUTOSLICE_CPA_USAGE_FILE"] = str(trace)
        prior_gemini_attempts = []
        if resume:
            prior_gemini_attempts = prior_run.get("provider_usage", {}).get("gemini_http_attempts", [])
            if not isinstance(prior_gemini_attempts, list):
                raise ValueError("previous audio HTTP accounting is malformed")
        if recovery is not None:
            prior_http = out / "gemini-http-attempts.json"
            prior_gemini_attempts = json.loads(prior_http.read_text()) if prior_http.exists() else []
            run["parent_http_summary_available"] = prior_http.exists()
            if not isinstance(prior_gemini_attempts, list):
                raise ValueError("interrupted HTTP accounting malformed")
        trace_lines_before = len(trace.read_text().splitlines()) if trace.exists() else 0
        agy_receipts_before = set(out.rglob("agy.execution.json"))
        gemini_attempts = []
    except BaseException as exc:
        interrupted = not isinstance(exc, (Exception, SystemExit))
        if resume_parent_run is not None and resume_parent_run["validation"] == "PENDING":
            resume_parent_run["validation"] = "INTERRUPTED" if interrupted else "REJECTED"
        run.update(error=f"{type(exc).__name__}: {exc}",
                   failed_phase=run.get("phase"), preparation_status="FAILED",
                   lifecycle="INTERRUPTED" if interrupted else "FAILED",
                   quality_outcome="EVALUATION_ERROR", would_release=None,
                   elapsed_seconds=round(time.monotonic() - started, 3),
                   final_srt_sha256=digest(local_srt) if local_srt.is_file() else None,
                   provider_usage={"new_provider_calls": 0, "cost_usd": None,
                                   "historical_accounting": (recovery or {}).get("accounting")})
        if interrupted:
            run["interrupted"] = True
        checkpoint("PACKAGE_PREPARATION_INTERRUPTED" if interrupted else "PACKAGE_PREPARATION_FAILED",
                   reject_invalid=False)
        if interrupted:
            raise
        return run
    try:
        with bind_observation(trace, f"subtitle-regression:{out.parent.name}:{cid}"), (nullcontext() if comparison_config is not None and revision is None else observe_gemini_http_attempts(gemini_attempts)):
            if normal_samples is not None or (comparison_config is not None and revision is not None):
                from src.autoslice.argon_comparison_policy import route_probes
                targets = normal_samples if normal_samples is not None else [
                    s for s in comparison_plan["samples"] if s["candidate_id"] == cid
                    and s["case_id"] in comparison_config["comparison_case_ids"]]
                run["ordinary_route_validity"] = {"status": "INVALID_EVALUATION_CONFIGURATION", "samples": []}
                run["ordinary_route_validity"] = route_probes(reviewer, targets, local_srt.read_text(), out)
                checkpoint("ORDINARY_ROUTE_RETURNED")
                print(json.dumps({"package":cid,"route_validity":run["ordinary_route_validity"]["status"],
                                  "cases":[r["case_id"] for r in run["ordinary_route_validity"]["samples"]]}),flush=True)
            gate = _run_exact_final_review_gate(
                cid=cid, out_root=out, final_start=record["start_ms"], final_end=record["end_ms"],
                recut=CurrentTextReviewArtifacts(subtitle_path=local_srt),
                chat_authority_audit=authority, chat_authority_path=local_authority,
                run_exact_final_review=observed_review, clip_context=context,
            )
            run["would_release"] = gate.get("release_gate") == "PASS"
    except (Exception, SystemExit) as exc:
        run["error"] = f"{type(exc).__name__}: {exc}"
        if reviews and reviews[-1].get("release_gate") == "BLOCK":
            run["would_release"] = False
    except BaseException as exc:
        run["interrupted"] = True
        run["error"] = f"{type(exc).__name__}: {exc}"
        run["would_release"] = None
        raise
    finally:
        run["lifecycle"] = "INTERRUPTED" if run.get("interrupted") else "COMPLETED"
        run["quality_outcome"] = ("EVALUATION_ERROR" if run.get("error")
                                  else "PASS" if run.get("would_release") is True else "BLOCKED")
        run["elapsed_seconds"] = round(time.monotonic() - started, 3)
        run["input_drift"] = [str(p) for p, sha in tracked.items() if digest(p) != sha]
        if normal_samples is not None and digest(review_media) != package["media_sha256"]:
            run["input_drift"].append(str(review_media))
        run["final_cues"] = [{"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                             for i, c in enumerate(parse_srt_cues(local_srt.read_text()), 1)]
        run["final_srt_sha256"] = digest(local_srt)
        events = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        run["provider_usage"] = {"events": len(events),
            "http_attempt_events": sum(e.get("event_type") == "http_attempt" for e in events),
            "http_attempts_exact": None,
            "model_requests": sum(e.get("event_type") == "model_request" for e in events),
            "count_note": "Logical CPA calls and AGY execution receipts are counted separately; absent HTTP telemetry is not zero requests.",
            "cost_usd": None, "cost_note": "Runtime receipts do not expose billed dollars; no paid policy changed.",
            "trace": trace.name, "audio_receipts": "native-audio-budget.json and per-provider scratch receipts"}
        run["provider_usage"]["audio_http_attempts_observed"] = sum(e.get("event_type") == "audio_http_attempt" for e in events)
        run["provider_usage"]["agy_http_attempts"] = None
        run["provider_usage"]["audio_job_directories"] = [str(p.parent.relative_to(out)) for p in out.rglob("prompt.md")]
        run["provider_usage"]["agy_executions"] = [
            {"receipt": str(p.relative_to(out)), "sha256": digest(p),
             "returncode": json.loads(p.read_text()).get("returncode")}
            for p in out.rglob("agy.execution.json")]
        all_gemini_attempts = [*prior_gemini_attempts, *gemini_attempts]
        run["provider_usage"]["gemini_http_attempts"] = all_gemini_attempts
        if comparison_config is not None:
            from src.autoslice.argon_regression_comparison import native_accounting
            run["provider_usage"]["argon_native"] = native_accounting(run)
            run["provider_usage"]["legacy_gemini_counter_scope"] = "ordinary urllib only; Argon native HTTP accounted separately"
        run["provider_usage"]["route_counts"] = {
            "agy_executions": len(run["provider_usage"]["agy_executions"]),
            "gemini_free_http_attempts": sum(r["key_tier"] == "free" for r in all_gemini_attempts),
            "gemini_paid_http_attempts": sum(r["key_tier"] == "paid_backup" for r in all_gemini_attempts),
            "gemini_unknown_http_attempts": sum(r["key_tier"] == "unknown" for r in all_gemini_attempts)}
        if resume or recovery is not None:
            run["provider_usage"]["continuation_delta"] = {
                "model_requests": sum(e.get("event_type") == "model_request" for e in events[trace_lines_before:]),
                "http_attempt_events": sum(e.get("event_type") == "http_attempt" for e in events[trace_lines_before:]),
                "gemini_http_attempts": gemini_attempts,
                "agy_executions": len(set(out.rglob("agy.execution.json")) - agy_receipts_before),
                "cost_usd": None,
            }
            run["provider_usage"]["totals_scope"] = "cumulative within the original private experiment; do not add parent totals again"
        write_json(out / "gemini-http-attempts.json", all_gemini_attempts)
        write_json(out / "run.json", run)
        checkpoint("PACKAGE_INTERRUPTED" if run.get("interrupted") else "PACKAGE_RETURNED")
    return run



def _run_recovery_preflight(args, record: dict, doc: dict) -> int:
    """Exercise the actual package materializer and prepared constructor only."""
    from src.autoslice.argon_escalation_storage import put_json
    from src.autoslice.subtitle_regression_recovery import stage_successor, stable
    out = args.output.absolute()
    replay = getattr(args, "recovery_preflight_replay", False)
    if replay:
        saved = json.loads(stable(out / "interrupted-resume.json"))
        first = json.loads(stable(out / "preflight.json"))
        if (saved.get("preflight_only") is not True or saved.get("claim_path") is not None
                or saved.get("output") != str(out) or first.get("returncode") != 0
                or any(saved.get(k) != record.get(k) for k in (
                    "parent_output", "parent_terminal_sha256", "freeze_sha256",
                    "code_identity", "fixture_sha256", "cases", "packages", "files", "links"))):
            raise ValueError("preflight replay identity drift or incomplete initial preparation")
        staged = saved
        receipt_path = out / ("preflight-replay-%04d.json" % (
            len(list(out.glob("preflight-replay-*.json"))) + 1))
    else:
        staged = stage_successor(record, out, preflight_only=True)
        receipt_path = out / "preflight.json"
    completed = {}
    active = None
    status, returncode, error = "RUNNING", 1, None
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run_states = {}
    def progress():
        write_json(out / "experiment-progress.json", {
            "status": status, "returncode": returncode if status != "RUNNING" else None,
            "purpose": "PREPARATION_PREFLIGHT", "started_utc": started,
            "processed_packages": list(completed), "active_package": active,
            "remaining_packages": [key for key in record["packages"] if key not in completed],
            "not_started_packages": [key for key in record["packages"]
                                     if key not in completed and key != active],
            "package_states": run_states, "error": error,
            "code_identity": args.code_identity, "quality_outcome": "NOT_EVALUATED",
            "would_release": None, "release_authorized": False})
    def saved_state(key, run):
        return {"preparation_status": run.get("preparation_status"),
                "error": run.get("error"), "would_release": run.get("would_release"),
                "run_sha256": digest(out / key / "run.json"),
                "phase": run.get("phase"), "lifecycle": run.get("lifecycle"),
                "quality_outcome": run.get("quality_outcome")}
    try:
        progress()
        configure_replay_state(out, Path(args.runtime_root), resume=True)
        for key, package in record["packages"].items():
            active = key
            progress()
            run = run_package(doc["packages"][key], out / key, args.code_root.resolve(),
                {"credentials_loaded": False}, normal_samples=package["normal_samples"],
                recovery=staged["packages"][key], preparation_only=True,
                checkpoint_identity={"code_identity": args.code_identity,
                    "code_root": str(args.code_root.resolve()), "fixture_sha256": record["fixture_sha256"],
                    "parent_freeze_sha256": record["freeze_sha256"], "preflight_only": True})
            completed[key] = saved_state(key, run)
            run_states[key] = completed[key]
            active = None
            progress()
        audit = getattr(args, "_preflight_dispatch_audit", {})
        failed = any(row["error"] or row["preparation_status"] != "PREPARED"
                     for row in completed.values()) or bool(audit.get("blocked_dispatches"))
        status, returncode = "COMPLETED", 1 if failed else 0
    except BaseException as exc:
        status = "INTERRUPTED" if not isinstance(exc, (Exception, SystemExit)) else "FAILED"
        returncode = -getattr(exc, "signum", signal.SIGINT) if status == "INTERRUPTED" else 1
        error = {"type": type(exc).__name__, "message": str(exc)}
        if active is not None and (out / active / "run.json").is_file():
            actual = json.loads(stable(out / active / "run.json"))
            run_states[active] = saved_state(active, actual)
        raise
    finally:
        audit = getattr(args, "_preflight_dispatch_audit", {})
        report = {**record, "output": str(out), "status": status,
                  "returncode": returncode, "error": error, "preflight_only": True,
                  "preparation_runs": run_states, "returned_packages": list(completed),
                  "provider_calls": 0, "provider_dispatch_audit": audit,
                  "quality_outcome": "NOT_EVALUATED", "would_release": None,
                  "cold_replay": replay, "started_utc": started,
                  "created_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        put_json(receipt_path, report)
        progress()
        print(json.dumps({"status": status, "returncode": returncode,
                          "preparation_runs": run_states, "provider_calls": 0,
                          "quality_outcome": "NOT_EVALUATED"}), flush=True)
    return returncode


def _worker_body(args) -> int:
    if (args.output / "invocation.json").exists() or any(args.output.glob("*/comparison-invocation.json")):
        raise ValueError("COMPARISON_REQUIRES_SAME_INVOCATION_RESUME")
    code_root = args.code_root.resolve()
    sys.path.insert(0, str(code_root))
    os.chdir(code_root)
    os.environ["AUTOSLICE_BASE"] = args.runtime_root
    os.environ["AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"] = args.runtime_root
    os.environ.pop("AUTOSLICE_FINAL_REVIEW_SSH_HOST", None)
    # Production source and package inputs remain read-only; scratch owns all outputs.
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    # Preserve new command-transport prompts/responses before its temporary
    # bridge directory disappears. This never enables provider acquisition.
    os.environ["AUTOSLICE_CPA_ARCHIVE_TEXT_EXCHANGES"] = "1"
    recovery_parent = getattr(args, "resume_interrupted", None)
    # Interrupted admission is read-only and runs before credentials/providers.
    recovery_record = None
    if recovery_parent:
        from src.autoslice.subtitle_regression_recovery import inspect_parent, stage_successor
        preliminary_doc = load_cases(args.cases)
        preliminary_cases = [c for c in preliminary_doc["cases"] if not args.case or c["id"] in args.case]
        recovery_record = inspect_parent(
            parent=recovery_parent, freeze_path=args.interruption_evidence,
            freeze_sha256=args.interruption_evidence_sha256, cases_path=args.cases,
            cases=preliminary_cases, runtime_root=Path(args.runtime_root), code_identity=args.code_identity)
        if args.recovery_preflight:
            return _run_recovery_preflight(args, recovery_record, preliminary_doc)
        args._recovery_record = recovery_record
        recovery_record = stage_successor(recovery_record, args.output.absolute())
        args._recovery_record = recovery_record
        write_json(args.output / "attempt-start.json", args._attempt_start)
    gemini_environment = load_production_gemini_environment()
    if getattr(args, "normal_off", False):
        from src.autoslice.native_audio_runtime import native_audio_readiness
        from src.autoslice.agy_gemini_client import resolve_local_agy_binary
        readiness = native_audio_readiness(Path(args.runtime_root))
        if len(readiness) != 2 or not all(row.get("ready") is True for row in readiness):
            raise ValueError("normal OFF native production configuration unavailable")
        if not resolve_local_agy_binary(None):
            raise ValueError("normal OFF AGY production configuration unavailable")
    doc = load_cases(args.cases)
    cases = [c for c in doc["cases"] if not args.case or c["id"] in args.case]
    if args.case and set(args.case) - {c["id"] for c in cases}:
        raise ValueError("unknown case selection")
    # Preserve lexical directory identity for the resume path guard.
    out = args.output.absolute() if args.resume_existing else args.output.resolve()
    resume_record = None
    if args.resume_existing:
        resume_record = prepare_experiment_resume(
            out, args.cases, cases, args.code_identity, Path(args.runtime_root))
    elif recovery_record is None:
        out.mkdir(parents=True, exist_ok=False)
    replay_state = configure_replay_state(out, Path(args.runtime_root),
                                          resume=args.resume_existing or recovery_record is not None)
    write_json(out / "replay-state.json", replay_state)
    write_json(out / "gemini-environment.json", gemini_environment)
    runs = {}
    reused_package_runs = {}
    if resume_record is not None:
        write_json(out / "resume-status.json", {
            "status": "RUNNING", "worker_pid": os.getpid(), "code_identity": args.code_identity,
            "parent_evaluation_sha256": resume_record["parent_evaluation_sha256"],
            "archive": resume_record["archive"], "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    for key in dict.fromkeys(c["package"] for c in cases if c["status"] == "ready"):
        print(f"START {key}", flush=True)
        try:
            from src.autoslice.jingting_chunker import parse_srt_cues
            original_cues = [{"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                for i, c in enumerate(parse_srt_cues(Path(doc["packages"][key]["srt_path"]).read_text()), 1)]
            package_cases = [c for c in cases if c["package"] == key]
            validate_original_cases(package_cases, original_cues)
            # Normal OFF is a live regression of every selected package on
            # this code, including targets that an older version got right.
            if resume_record is not None and not getattr(args, "normal_off", False):
                prior_run_path = out / key / "run.json"
                prior_run = json.loads(prior_run_path.read_text())
                # Score only already completed real output. Human references
                # stay here in the evaluator and never enter a provider prompt.
                # A passing selected target does not mean its film can publish.
                if all(summarize_case(case, prior_run)["outcome"] == "PASS_REPAIRED"
                       for case in package_cases):
                    runs[key] = prior_run
                    reused_package_runs[key] = {
                        "source_code_identity": resume_record["parent_code_identity"],
                        "run_sha256": digest(prior_run_path),
                        "final_srt_sha256": digest(out / key / "final.srt"),
                        "new_provider_calls": 0,
                        "scope": "exact retained target-result replay; not new-code live execution or film acceptance",
                    }
                    print(f"REUSED_TARGET_RESULTS {key}: no new provider dispatch", flush=True)
                    continue
            options = {"resume": args.resume_existing}
            if getattr(args, "normal_off", False):
                options["normal_samples"] = normal_off_samples(
                    doc["packages"][key], package_cases, original_cues)
            if recovery_record is not None:
                options["recovery"] = recovery_record["packages"][key]
                options["checkpoint_identity"] = {
                    "code_identity": args.code_identity, "code_root": str(code_root),
                    "fixture_sha256": digest(args.cases), "parent_freeze_sha256": recovery_record["freeze_sha256"]}
            runs[key] = run_package(doc["packages"][key], out / key, code_root,
                                    gemini_environment, **options)
        except Exception as exc:
            runs[key] = {"reviews": [], "error": f"{type(exc).__name__}: {exc}", "would_release": None}
            write_json(out / f"{key}-error.json", runs[key])
        print(f"DONE {key}: {runs[key].get('error') or 'completed'}", flush=True)
        if recovery_record is not None:
            from src.autoslice.argon_escalation_storage import put_json
            scored = [summarize_case(c, runs[key]) for c in cases if c["package"] == key]
            put_json(out / "package-results" / (key + ".json"),
                     {"package": key, "code_identity": args.code_identity, "results": scored,
                      "run_sha256": digest(out / key / "run.json") if (out / key / "run.json").exists() else None})
            write_json(out / "experiment-progress.json",
                       {"status": "RUNNING", "completed_packages": list(runs),
                        "remaining_packages": [k for k in dict.fromkeys(c["package"] for c in cases)
                                               if k not in runs], "code_identity": args.code_identity,
                        "parent_freeze_sha256": recovery_record["freeze_sha256"]})
    results = [summarize_case(c, runs.get(c["package"], {})) for c in cases]
    report = {"schema_version": "subtitle-regression-evaluation.v1", "code_identity": args.code_identity,
              "fixture_sha256": digest(args.cases), "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "code_root": str(code_root), "worker_python": sys.executable,
              "gemini_environment": gemini_environment,
              "replay_state": replay_state,
              "scope": "subtitle final gate only; no render, upload, deployment or publication",
              "results": results, "summary": aggregate(results),
              "runs": {k: {n: v for n, v in r.items() if n not in {"reviews", "final_cues"}} for k, r in runs.items()}}
    if resume_record is not None:
        report["resume"] = {key: value for key, value in resume_record.items() if key != "files"}
        report["reused_package_runs"] = reused_package_runs
    if recovery_record is not None:
        report["interrupted_resume"] = {k: v for k, v in recovery_record.items()
                                       if k not in {"files", "copied_to", "packages"}}
    write_json(out / "evaluation.json", report)
    (out / "evaluation.md").write_text(markdown_report(report))
    if resume_record is not None:
        write_json(out / "resume-status.json", {
            "status": "COMPLETED", "worker_pid": os.getpid(), "code_identity": args.code_identity,
            "evaluation_sha256": digest(out / "evaluation.json"),
            "archive": resume_record["archive"], "ended_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
    return 0 if report["summary"]["pass"] else 1


def worker(args) -> int:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # A terminal output can be continued, but never by two simultaneous workers.
    with (output.parent / ("." + output.name + ".worker.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        parent = getattr(args, "resume_interrupted", None)
        if parent is not None:
            physical = Path(__file__).resolve().parents[1]
            if args.code_root.resolve() != physical:
                raise ValueError("interrupted worker imported code root drift")
            sys.path.insert(0, str(physical))
        if parent is None or getattr(args, "recovery_preflight", False):
            if parent is None:
                return _worker_body(args)
            from src.autoslice.subtitle_regression_recovery import (
                parent_lock, without_provider_dispatch, bounded_attempt)
            with parent_lock(parent), without_provider_dispatch() as audit, bounded_attempt(getattr(args, "maximum_wall_seconds", 14400)):
                args._preflight_dispatch_audit = audit
                return _worker_body(args)
        from src.autoslice.subtitle_regression_recovery import (
            bounded_attempt, parent_lock, EvaluationInterrupted)
        if output.exists():
            raise ValueError("interrupted successor must be a new sibling attempt")
        started = dt.datetime.now(dt.timezone.utc).isoformat()
        args._attempt_start = {
            "status": "RUNNING", "started_utc": started, "launcher_pid": os.getppid(),
            "worker_pid": os.getpid(), "cwd": str(args.code_root.resolve()),
            "command": list(sys.orig_argv), "head": args.code_identity, "output": str(output),
            "maximum_wall_seconds": args.maximum_wall_seconds,
            "parent_output": str(parent), "parent_freeze_sha256": args.interruption_evidence_sha256,
            "argon_enabled": False, "merged": False, "deployed": False}
        status, returncode, error_type = "FAILED", 1, None
        with parent_lock(parent):
            try:
                with bounded_attempt(args.maximum_wall_seconds):
                    returncode = _worker_body(args)
                status = "COMPLETED"
                return returncode
            except EvaluationInterrupted as exc:
                status, returncode, error_type = "INTERRUPTED", -exc.signum, type(exc).__name__
                return 128 + exc.signum
            except BaseException as exc:
                error_type = type(exc).__name__
                raise
            finally:
                if output.is_dir():
                    from src.autoslice.argon_escalation_storage import put_json
                    end = dt.datetime.now(dt.timezone.utc)
                    package_states = {}
                    for path in output.glob("*/run.json"):
                        saved = json.loads(path.read_text())
                        package_states[path.parent.name] = {
                            k: saved.get(k) for k in
                            ("lifecycle", "phase", "preparation_status", "error", "would_release")}
                    evaluation = json.loads((output / "evaluation.json").read_text()) if (
                        output / "evaluation.json").is_file() else None
                    write_json(output / "experiment-progress.json", {
                        "status": status, "returncode": returncode, "error_type": error_type,
                        "ended_utc": end.isoformat(), "code_identity": args.code_identity,
                        "parent_freeze_sha256": args.interruption_evidence_sha256,
                        "package_states": package_states,
                        "processed_packages": [p.stem for p in (output / "package-results").glob("*.json")],
                        "quality_outcome": ("EVALUATION_ERROR" if any(
                            p.get("error") for p in package_states.values()) else
                            "PASS" if evaluation and evaluation["summary"]["pass"] else "NOT_PASSED"),
                        "quality_pass": bool(evaluation and evaluation["summary"]["pass"]),
                        "would_release": ({k: p["would_release"] for k, p in package_states.items()}),
                        "release_authorized": False})
                    put_json(output / "attempt-terminal.json", {
                        **args._attempt_start, "status": status, "returncode": returncode,
                        "ended_utc": end.isoformat(), "error_type": error_type,
                        "elapsed_seconds": (end - dt.datetime.fromisoformat(started)).total_seconds(),
                        "evaluation_sha256": digest(output / "evaluation.json")
                            if status == "COMPLETED" and (output / "evaluation.json").exists() else None,
                        "package_runs": {p.parent.name: digest(p) for p in output.glob("*/run.json")},
                        "parent_failed_status_unchanged": (
                            digest(Path(args._recovery_record["parent_terminal_path"]))
                            == args._recovery_record["parent_terminal_sha256"])
                            if hasattr(args, "_recovery_record") else None})


def controller(args) -> int:
    load_cases(args.cases)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "cases.json").write_bytes(args.cases.read_bytes())
    remote = subprocess.check_output(["ssh", args.host, "mktemp -d /tmp/subtitle-regressions-XXXXXXXX"], text=True).strip()
    if not re.fullmatch(r"/tmp/subtitle-regressions-[A-Za-z0-9]+", remote):
        raise ValueError("invalid remote scratch path")
    identity = "deployed"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        if args.commit:
            identity = subprocess.check_output(["git", "rev-parse", f"{args.commit}^{{commit}}"], cwd=ROOT, text=True).strip()
            source = subprocess.check_output(["git", "archive", identity, "src", "scripts", "assets", "config", "profiles", "ops"], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(source)) as files:
                for member in files:
                    member.name = "code/" + member.name
                    tar.addfile(member, files.extractfile(member) if member.isfile() else None)
        tar.add(Path(__file__), arcname="evaluate.py")
        tar.add(args.cases, arcname="cases.json")
    subprocess.run(["ssh", args.host, f"tar -xf - -C {shlex.quote(remote)}"], input=archive.getvalue(), check=True)
    code_root = remote + "/code"
    if not args.commit:
        # Freeze the deployed tree too: a concurrent deployment must not mix code versions.
        snapshot = """import pathlib,shutil,hashlib,json
source=pathlib.Path(SOURCE); target=pathlib.Path(TARGET); target.mkdir()
def tree(root):
 h=hashlib.sha256()
 for directory in ('src','scripts','assets','config','profiles','ops'):
  for p in sorted((root/directory).rglob('*')):
   if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
    h.update(str(p.relative_to(root)).encode()+b'\\0'+hashlib.sha256(p.read_bytes()).digest())
 return h.hexdigest()
before=tree(source)
for name in ('src','scripts','assets','config','profiles','ops'):
 shutil.copytree(source/name,target/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
if tree(source)!=before or tree(target)!=before: raise RuntimeError('deployment changed during snapshot')
print('deployed:'+before)
""".replace("SOURCE", repr(args.runtime_root + "/repo")).replace("TARGET", repr(code_root))
        identity = subprocess.check_output(["ssh", args.host, "sudo python3 -c " + shlex.quote(snapshot)], text=True).strip()
    cmd = ["sudo", args.runtime_root + "/venv-main/bin/python", "-B", remote + "/evaluate.py", "--worker",
           "--cases", remote + "/cases.json", "--code-root", code_root, "--code-identity", identity,
           "--runtime-root", args.runtime_root, "--output", remote + "/results"]
    for case in args.case or []:
        cmd += ["--case", case]
    if getattr(args, "normal_off", False):
        cmd.append("--normal-off")
    write_json(output / "invocation.json", {"remote_scratch": remote, "code_identity": identity,
        "host": args.host, "controller_python": sys.executable, "command": cmd, "fixture_sha256": digest(args.cases)})
    with (output / "worker.log").open("w") as log:
        status = subprocess.run(["ssh", args.host, shlex.join(cmd)], stdout=log, stderr=subprocess.STDOUT).returncode
    # Read back text only; never bring media or provider audio caches into Git.
    pack = "import pathlib,tarfile,sys; root=pathlib.Path(" + repr(remote + "/results") + "); t=tarfile.open(fileobj=sys.stdout.buffer,mode='w|'); [(t.add(p,arcname=str(p.relative_to(root)))) for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink() and p.suffix in {'.json','.jsonl','.srt','.txt'} and p.stat().st_size < 16000000]; t.close()"
    packed = subprocess.check_output(["ssh", args.host, "sudo python3 -c " + shlex.quote(pack)])
    with tarfile.open(fileobj=io.BytesIO(packed)) as tar:
        for member in tar:
            target = output / member.name
            if not member.isfile() or not target.resolve().is_relative_to(output):
                raise ValueError("unsafe remote report member")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    report_path = output / "evaluation.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        (output / "evaluation.md").write_text(markdown_report(report))
        print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"Report: {output}")
    return status


def rescore(args) -> int:
    """Read saved native observations without provider calls or new verdicts."""
    doc = load_cases(args.cases)
    source = args.rescore.resolve()
    previous = json.loads((source / "evaluation.json").read_text())
    equivalence_update = None
    if previous["fixture_sha256"] != digest(args.cases):
        original_path = source / "cases.json"
        if not getattr(args, "allow_equivalence_update", False) or not original_path.exists():
            raise ValueError("fixture changed: run fresh evaluation or explicitly document equivalent strings")
        if digest(original_path) != previous["fixture_sha256"]:
            raise ValueError("original fixture hash mismatch")
        original = load_cases(original_path)
        old_stripped, new_stripped = copy.deepcopy(original), copy.deepcopy(doc)
        for data in (old_stripped, new_stripped):
            for case in data["cases"]:
                case["expected"].pop("equivalent_texts", None)
                case["expected"].pop("equivalence_notes", None)
        if old_stripped != new_stripped:
            raise ValueError("rescore cannot change source inputs, error labels, or canonical human text")
        for case in doc["cases"]:
            annotated = {note["text"] for note in case["expected"].get("equivalence_notes", [])
                         if note.get("basis") and note.get("assessor")}
            if set(case["expected"].get("equivalent_texts", [])) - annotated:
                raise ValueError("every equivalent string needs an explicit basis and assessor")
        equivalence_update = {"source_fixture_sha256": previous["fixture_sha256"],
                              "updated_fixture_sha256": digest(args.cases),
                              "scope": "explicit equivalent strings only; original observations and canonical human text unchanged"}
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "cases.json").write_bytes(args.cases.read_bytes())
    selected = {r["case_id"] for r in previous["results"]}
    cases = [c for c in doc["cases"] if c["id"] in selected]
    from src.autoslice.jingting_chunker import parse_srt_cues
    runs = {}
    for key in {c["package"] for c in cases if c["status"] == "ready"}:
        directory = source / key
        path = directory / "run.json"
        if not path.exists():
            runs[key] = {"error": "missing native run.json", "reviews": []}
            continue
        run = json.loads(path.read_text())
        srt_by_hash = {digest(p): p for p in directory.rglob("*.srt")}
        for index, review in enumerate(run.get("reviews", [])):
            sha = str(review.get("reviewed_srt_sha256", "")).removeprefix("sha256:")
            if sha in srt_by_hash:
                review["evaluation_input_cues"] = [
                    {"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                    for i, c in enumerate(parse_srt_cues(srt_by_hash[sha].read_text()), 1)]
            else:
                run.setdefault("unmapped_prior_review_inputs", []).append(sha)
                if index == len(run["reviews"]) - 1:
                    run["input_drift"] = ["missing exact final review input " + sha]
        runs[key] = run
    results = [summarize_case(c, runs.get(c["package"], {})) for c in cases]
    report = {**previous, "results": results, "summary": aggregate(results),
              "fixture_sha256": digest(args.cases), "equivalence_update": equivalence_update,
              "rescore_source": str(source), "rescore_source_sha256": digest(source / "evaluation.json"),
              "rescore_provider_calls": 0}
    write_json(output / "evaluation.json", report)
    (output / "evaluation.md").write_text(markdown_report(report))
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0 if report["summary"]["pass"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--commit", help="Exact local Git commit to run in isolated OCI3 scratch")
    source.add_argument("--deployed", action="store_true", help="Use the current OCI3 source")
    source.add_argument("--rescore", type=Path, help="Recompute summaries from saved exact observations; no provider calls")
    parser.add_argument("--host", default="oci3")
    parser.add_argument("--runtime-root", default="/opt/bilive/autoslice")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/subtitle-regressions" / dt.date.today().isoformat() / dt.datetime.now().strftime("%H%M%S"))
    parser.add_argument("--case", action="append", help="Optional case IDs; omitted means the full suite, including pending evidence")
    parser.add_argument("--allow-equivalence-update", action="store_true", help="With --rescore only: accept explicitly documented equivalent strings, retaining the original fixture")
    parser.add_argument("--normal-off", action="store_true",
                        help="New ordinary-route regression with bound blind probes; Argon and S2 disabled")
    parser.add_argument("--resume-existing", action="store_true",
                        help="Worker only: continue a terminal private experiment with repaired code, preserving all histories")
    parser.add_argument("--resume-interrupted", type=Path,
                        help="Normal OFF worker: create a single successor from a frozen interrupted parent")
    parser.add_argument("--interruption-evidence", type=Path, help="Immutable original interruption inventory")
    parser.add_argument("--interruption-evidence-sha256", help="Exact SHA256 of the interruption inventory")
    parser.add_argument("--recovery-preflight", action="store_true",
                        help="Validate the interrupted input and next state without credentials or providers")
    parser.add_argument("--recovery-preflight-replay", action="store_true",
                        help="Explicit cold replay of a successful same-identity preparation preflight")
    parser.add_argument("--maximum-wall-seconds", type=int, default=14400,
                        help="Single interrupted successor wall cap, 1..14400; no automatic restart")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--code-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--code-identity", help=argparse.SUPPRESS)
    parser.add_argument("--prepare-argon-comparison", type=Path, help="Freeze original inputs and stable completed baseline packages, without credentials/providers")
    parser.add_argument("--baseline-commit", default="ada052f06d0b57d2733c96168a15b7fec1a64068")
    parser.add_argument("--comparison-plan", type=Path, help="Frozen finite Argon comparison plan")
    parser.add_argument("--comparison-scope-revision", type=Path, help="Explicit revision enabling normal ordinary routes for paired OFF/ON")
    parser.add_argument("--argon-arm", choices=("off", "on"), default="on")
    parser.add_argument("--paired-off-output", type=Path, help="Same-code completed OFF output; required for revised ON arm")
    parser.add_argument("--comparison-grant", type=Path, help="Explicit cost authorization matching the frozen plan")
    parser.add_argument("--resume-comparison", action="store_true", help="Continue this same isolated comparison without resetting its state")
    parser.add_argument("--comparison-code-successor", help="Explicitly continue completed comparison with repaired code before any native task; value is exact previous code identity")
    parser.add_argument("--comparison-output-predecessor", type=Path,
                        help="Create a new OFF output from a completed OFF byte/history snapshot; original is not written")
    parser.add_argument("--comparison-output-predecessor-sha256",
                        help="Exact canonical effective invocation SHA for --comparison-output-predecessor")
    parser.add_argument("--attach-comparison-baseline", type=Path, help="Attach newly terminal baseline packages to an existing freeze, without providers or re-experiment")
    args = parser.parse_args(argv)
    if args.recovery_preflight_replay and not args.recovery_preflight:
        parser.error("preflight replay requires --recovery-preflight")
    if args.resume_interrupted or args.interruption_evidence or args.interruption_evidence_sha256 or args.recovery_preflight:
        if (not (args.worker and args.normal_off and args.resume_interrupted and args.interruption_evidence
                 and args.interruption_evidence_sha256 and args.case)
                or args.resume_existing or args.rescore or args.comparison_plan or args.comparison_grant
                or not 1 <= args.maximum_wall_seconds <= 14400):
            parser.error("interrupted recovery requires normal OFF worker, exact evidence SHA and cases; modes cannot mix")
    if args.comparison_output_predecessor or args.comparison_output_predecessor_sha256:
        if not (args.comparison_output_predecessor and args.comparison_output_predecessor_sha256
                and args.comparison_plan and args.resume_comparison and args.comparison_code_successor
                and args.argon_arm == "off"):
            parser.error("OFF output successor requires exact parent SHA, code successor and --resume-comparison")
    if args.comparison_code_successor and not (args.comparison_plan and args.resume_comparison):
        parser.error("--comparison-code-successor requires --comparison-plan and --resume-comparison")
    comparison_operation = any((args.prepare_argon_comparison, args.comparison_plan, args.attach_comparison_baseline))
    if args.normal_off and (comparison_operation or args.comparison_grant or args.resume_comparison or args.rescore):
        parser.error("--normal-off belongs only to the ordinary regression entry")
    if args.resume_existing and (not args.worker or args.rescore or comparison_operation):
        parser.error("--resume-existing requires only the ordinary --worker execution entry")
    if comparison_operation and (args.worker or args.rescore or args.deployed):
        parser.error("comparison and ordinary worker/rescore modes cannot be combined")
    if sum(bool(x) for x in (args.prepare_argon_comparison, args.comparison_plan, args.attach_comparison_baseline)) > 1:
        parser.error("choose one comparison operation")
    if args.attach_comparison_baseline:
        sys.path.insert(0, str(args.code_root or ROOT))
        from src.autoslice.argon_regression_comparison import attach_completed_baselines
        return attach_completed_baselines(args.attach_comparison_baseline)
    if args.prepare_argon_comparison:
        sys.path.insert(0, str(args.code_root or ROOT))
        from src.autoslice.argon_regression_comparison import prepare
        return prepare(args)
    if args.comparison_plan:
        sys.path.insert(0, str(args.code_root or ROOT))
        from src.autoslice.argon_regression_comparison import execute
        return execute(args)
    if args.comparison_grant or args.resume_comparison:
        parser.error("comparison grant/resume requires --comparison-plan")
    if args.rescore:
        sys.path.insert(0, str(ROOT))
        return rescore(args)
    return worker(args) if args.worker else controller(args)


if __name__ == "__main__":
    raise SystemExit(main())
