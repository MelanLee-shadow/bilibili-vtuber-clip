#!/usr/bin/env python3
"""Offline cold-continuation authority preflight for in-flight slices.

Runs the ordinary producer CLI (``scripts/produce_slice_package.py`` of the
given code root) on one slice's existing output/checkpoints and stops it at the
first provider-facing boundary.  It records:

* every sub-check of ``_continuation_validate_existing_authority`` per call
  (PASS/FAIL/SKIP, reason code, seconds, bytes read).  A code root that has
  ``continuation_subchecks`` reports the producer's own check list; an older
  root gets the legacy decomposition below plus its unmodified first failure;
* call counts, seconds and outcome of every cold-read validator in the
  continuation function family (inventory coverage);
* large files opened more than once (repeated whole-file hashing);
* the outcome: ``REACHED_<boundary>`` (continuation authority accepted, the
  next ordinary step is that boundary), ``CONTINUATION_BLOCKED`` (the
  engineering code the runner classifies as continuation_authority) or an
  unexpected error.

It must run inside ``continuation_authority_preflight_sandbox.sh`` (private
mount + network namespace, runtime root behind a disposable overlay,
credentials masked).  It never calls a provider: network, remote processes,
the exact-final review gate and the boundary review are tripwires.
"""
from __future__ import annotations

import argparse
import builtins
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

BOUNDARY_PROCESSES = {"ssh", "scp", "sftp", "rsync", "curl", "wget", "agy", "codex", "claude", "gemini"}
FAMILY = {
    "src.autoslice.producer_text_pipeline": re.compile(
        r"^(_continuation_|_automatic_current_|_validate_current_|_validate_retained_|_retained_|"
        r"_source_checkpoint_|_checkpoint_current_|_current_checkpoint_|_source_piece_container_clock|"
        r"_validate_source_stage_|_replay_|_resume_pending_|_load_pending_|_pending_)"),
    "src.autoslice.producer_text_checkpoint": re.compile(r"^(validate_|load_|_frozen_binding|write_current_text_checkpoint)"),
    "src.autoslice.retained_reviewed_draft": re.compile(r"^(load_|validate_|_validate)"),
    "src.autoslice.retained_preparation_checkpoint": re.compile(r"^(load_|validate_|replayed_input_present|retained_attempt_present)"),
    "src.autoslice.retained_source_input": re.compile(r"^load_"),
    "src.autoslice.source_checkpoint_parent": re.compile(r"^validate_"),
    "src.autoslice.final_review_contract": re.compile(r"^(validate_final_review_release|_validate_)"),
    "src.autoslice.final_source_language_owner": re.compile(r"^(validate_final_cpa_owner|discovery_generations)$"),
    "src.autoslice.continuation_review_state": re.compile(r"^(unavailable_review_stage|reviewed_reference_cues)$"),
    "src.autoslice.native_audio_budget_receipt": re.compile(r"^(_validate_existing|_require_budget_history_extension)$"),
}


class PreflightBoundary(BaseException):
    """The first provider-facing step was reached; nothing past it runs."""


class PreflightTimeBudget(BaseException):
    """The cold validation exceeded its declared time budget (still in progress)."""


def _rchar() -> int | None:
    try:
        for line in Path("/proc/self/io").read_text().splitlines():
            if line.startswith("rchar:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def isolation_problems() -> list[str]:
    problems = []
    if os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        problems.append("NETWORK_NAMESPACE_NOT_PRIVATE")
    if os.readlink("/proc/self/ns/mnt") == os.readlink("/proc/1/ns/mnt"):
        problems.append("MOUNT_NAMESPACE_NOT_PRIVATE")
    for secret in (Path("/opt/bilive/.env"), Path("/opt/bilive/autoslice/cpa.env")):
        try:
            if secret.exists() and secret.stat().st_size:
                problems.append("CREDENTIAL_FILE_NOT_MASKED:" + str(secret))
        except OSError:
            pass
    if os.environ.get("CONTINUATION_PREFLIGHT_SANDBOX") != "1":
        problems.append("SANDBOX_LAUNCHER_REQUIRED")
    return problems


def install_guards(state: dict) -> None:
    def refuse_network(*_args, **_kwargs):
        raise PreflightBoundary("NETWORK")

    socket.socket.connect = refuse_network
    socket.socket.connect_ex = refuse_network
    socket.create_connection = refuse_network
    socket.getaddrinfo = refuse_network
    original_popen = subprocess.Popen.__init__

    def guarded_popen(self, args, *rest, **kwargs):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        head = os.path.basename(os.fsdecode(argv[0]).split()[0]) if argv else ""
        if head in BOUNDARY_PROCESSES:
            raise PreflightBoundary("REMOTE_PROCESS:" + head)
        return original_popen(self, args, *rest, **kwargs)

    subprocess.Popen.__init__ = guarded_popen
    opens: dict[str, list[int]] = state["large_file_opens"]
    original_open = io.open

    def counting_open(file, mode="r", *args, **kwargs):
        handle = original_open(file, mode, *args, **kwargs)
        if isinstance(mode, str) and "r" in mode and "+" not in mode and isinstance(file, (str, os.PathLike)):
            try:
                size = os.fstat(handle.fileno()).st_size
            except (OSError, ValueError, AttributeError):
                size = 0
            if size >= 1 << 20:
                row = opens.setdefault(os.path.abspath(os.fsdecode(file)), [size, 0, {}])
                row[1] += 1
                # Which validators re-read it: the innermost repository frames.
                frames = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                          for frame in traceback.extract_stack(limit=14)[:-1]
                          if "/src/autoslice/" in frame.filename][-3:]
                key = " < ".join(reversed(frames))
                row[2][key] = row[2].get(key, 0) + 1
        return handle

    io.open = counting_open
    builtins.open = counting_open


def wrap_family(state: dict) -> None:
    calls: dict[str, dict] = state["validators"]
    originals = {}
    for module_name, pattern in FAMILY.items():
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
        for name, value in list(vars(module).items()):
            if not callable(value) or not pattern.search(name) or getattr(value, "__module__", None) != module_name:
                continue
            key = module_name.rsplit(".", 1)[1] + "." + name
            row = calls.setdefault(key, {"calls": 0, "fail": 0, "seconds": 0.0, "last_failure": None})

            def wrapper(*args, __f=value, __row=row, **kwargs):
                __row["calls"] += 1
                started = time.perf_counter()
                try:
                    return __f(*args, **kwargs)
                except Exception as exc:
                    __row["fail"] += 1
                    __row["last_failure"] = (type(exc).__name__ + ":" + str(getattr(exc, "reason_code", exc)))[:200]
                    raise
                finally:
                    __row["seconds"] += time.perf_counter() - started

            wrapper.__wrapped__ = value
            originals[id(value)] = wrapper
    # Rebind every module-level reference (``from x import f`` copies too).
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith(("src.autoslice", "produce_slice_package")):
            continue
        for name, value in list(vars(module).items()):
            if callable(value) and id(value) in originals:
                setattr(module, name, originals[id(value)])


def legacy_rows(ptp, chat, kwargs) -> list[dict]:
    """Same item names, evaluated independently with an older code root."""
    from src.autoslice import final_review_contract as frc

    rows: list[dict] = []
    srt_text, srt_sha256 = kwargs["srt_text"], kwargs["srt_sha256"]
    current = "sha256:" + srt_sha256

    def item(name, check):
        started, before = time.perf_counter(), _rchar()
        row = {"validator": "_continuation_validate_existing_authority(legacy)", "check": name}
        try:
            check()
            row["status"] = "PASS"
        except Exception as exc:
            row.update(status="FAIL", error_type=type(exc).__name__,
                       reason_code=str(getattr(exc, "reason_code", None) or exc)[:300])
        after = _rchar()
        row["elapsed_s"] = round(time.perf_counter() - started, 4)
        row["rchar_bytes"] = (after - before) if after is not None and before is not None else None
        rows.append(row)

    def require(condition, code):
        if not condition:
            raise ptp.CurrentTextContinuationError(code)

    item("chat_authority.schema", lambda: require(chat.get("schema_version") == "chat-authority-audit.v2",
                                                   "CURRENT_CHAT_AUTHORITY_SCHEMA_INVALID"))

    def final_srt():
        declared = chat.get("final_output_srt_sha256") or chat.get("final_text_srt_sha256")
        if ptp._continuation_normalize_sha(declared) == srt_sha256:
            return
        receipt = chat.get("exact_final_cpa_self_heal")
        terminal = (ptp.caption_successors(receipt).get(len(receipt.get("passes", [])))
                    if isinstance(receipt, dict) and receipt.get("status") == "REVIEW_PENDING" else None)
        require(terminal is not None and terminal["output_srt_sha256"] == current
                and terminal["output_srt_text"] == srt_text, "CURRENT_FINAL_SRT_DRIFT")
    item("final_srt.binding", final_srt)
    item("chat_authority.mutation_flag", lambda: require(chat.get("mutation_authorized") is not True,
                                                          "CURRENT_CHAT_AUTHORITY_MUTATION_INVALID"))
    review = chat.get("final_review_audit")
    item("final_review.present", lambda: require(isinstance(review, dict), "CURRENT_FINAL_REVIEW_AUDIT_MISSING"))
    neutral = {"_validate_exact_final_cpa_self_heal": lambda *a, **k: None,
               "_continuation_validate_exact_final_owner": lambda *a, **k: None,
               "_continuation_validate_final_language_owner": lambda *a, **k: None,
               "_continuation_validate_existing_repair": lambda *a, **k: None}
    saved = {name: getattr(ptp, name) for name in neutral}

    def release_or_pending():
        # The unmodified validator with only the chain and row owners neutral:
        # its review classification + integrity + geometry, alone.
        for name, value in neutral.items():
            setattr(ptp, name, value)
        try:
            ptp._LEGACY_ORIGINAL_VALIDATOR(chat, **kwargs)
        finally:
            for name, value in saved.items():
                setattr(ptp, name, value)
    item("final_review.release_or_pending+integrity_geometry", release_or_pending)

    def chain():
        try:
            frc._validate_exact_final_cpa_self_heal(
                chat, expected_srt_sha256=current,
                allow_pending=isinstance(review, dict) and review.get("release_gate") == "BLOCK")
        except frc.FinalReviewContractError as exc:
            raise ptp.CurrentTextContinuationError(exc.reason_code) from exc
    item("self_heal.chain", chain)
    for index, row in enumerate(chat.get("entity_repairs") or []):
        def owner(row=row, index=index):
            judge = row.get("judge")
            if (row.get("mode") == "exact_final_cpa_self_heal" and isinstance(judge, dict)
                    and judge.get("schema_version") in {"exact-final-cpa-cycle-judge.v1",
                                                         "exact-final-cpa-convergence-judge.v1",
                                                         "editorial-subtitle-judge.v1"}):
                saved["_continuation_validate_exact_final_owner"](
                    row, row_index=index, chat_authority=chat,
                    source_clock_review=kwargs.get("source_clock_review"))
            elif row.get("mode") in {"final_foreign_script_cpa_adjudication",
                                     "final_source_language_cpa_adjudication"}:
                saved["_continuation_validate_final_language_owner"](
                    row, row_index=index, chat_authority=chat, srt_text=srt_text,
                    final_review=review if isinstance(review, dict) else {})
            else:
                saved["_continuation_validate_existing_repair"](
                    row, srt_text=srt_text, clip_context=kwargs["clip_context"])
        item(f"entity_repairs[{index}]:{row.get('mode') if isinstance(row, dict) else None}", owner)
    return rows


def run_one(args) -> dict:
    state = {"large_file_opens": {}, "validators": {}, "groups": [], "subcheck_rows": []}
    report = {"schema_version": "continuation-authority-preflight.v1", "spec": str(args.spec),
              "code_root": str(args.code_root), "isolation_problems": isolation_problems()}
    if report["isolation_problems"]:
        report["outcome"] = "REFUSED_NOT_ISOLATED"
        return report
    sys.path.insert(0, str(args.code_root))
    install_guards(state)
    spec_doc = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    report["candidate_id"] = spec_doc.get("candidate_id")
    module_spec = importlib.util.spec_from_file_location(
        "produce_slice_package", args.code_root / "scripts/produce_slice_package.py")
    producer = importlib.util.module_from_spec(module_spec)
    sys.modules["produce_slice_package"] = producer
    module_spec.loader.exec_module(producer)
    from src.autoslice import producer_package_finalization as ppf
    from src.autoslice import producer_text_pipeline as ptp

    def boundary(name):
        def stop(*_args, **_kwargs):
            raise PreflightBoundary(name)
        return stop

    ppf._run_exact_final_review_gate = boundary("EXACT_FINAL_REVIEW_GATE")
    ptp.review_source_boundary = boundary("SOURCE_BOUNDARY_REVIEW")
    # Building a provider transport reads credentials: that is a provider step.
    from src.autoslice import llm_client
    credentials = llm_client.runtime_cpa_command_environment
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("src.autoslice"):
            for name, value in list(vars(module).items()):
                if value is credentials:
                    setattr(module, name, boundary("PROVIDER_CREDENTIALS"))
    try:
        from src.autoslice import continuation_subchecks
    except ImportError:
        continuation_subchecks = None
        original = ptp._continuation_validate_existing_authority
        ptp._LEGACY_ORIGINAL_VALIDATOR = original

        def legacy_validator(chat_authority, **kwargs):
            state["groups"].append(legacy_rows(ptp, chat_authority, kwargs))
            return original(chat_authority, **kwargs)

        ptp._continuation_validate_existing_authority = legacy_validator
        ppf_ref = getattr(ppf, "_continuation_validate_existing_authority", None)
        if ppf_ref is original:
            ppf._continuation_validate_existing_authority = legacy_validator
    wrap_family(state)
    argv = ["--spec", str(args.spec), "--ssh-host", "localhost", "--substrate", args.substrate,
            "--correct", "cpa", "--speaker-mode", "uniform_host", "--reuse-cover", "--prepare-only"]
    if args.time_budget_s:
        import signal

        def expire(_signum, frame):
            state["budget_stack"] = traceback.format_stack(frame)[-12:]
            raise PreflightTimeBudget(args.time_budget_s)

        signal.signal(signal.SIGALRM, expire)
        signal.alarm(args.time_budget_s)
    started, rchar = time.perf_counter(), _rchar()
    audit_context = continuation_subchecks.audit() if continuation_subchecks else None
    rows = []
    try:
        if audit_context is not None:
            with audit_context as rows:
                producer.main(argv)
        else:
            producer.main(argv)
        report["outcome"] = "PRODUCER_RETURNED"
    except PreflightTimeBudget:
        report.update(outcome="TIME_BUDGET_EXCEEDED", budget_stack=state.get("budget_stack"))
    except PreflightBoundary as reached:
        report["outcome"] = "REACHED_" + str(reached.args[0])
        report["boundary_stack"] = traceback.format_exception(reached)[-6:]
    except ptp.CurrentTextContinuationError as exc:
        report.update(outcome="CONTINUATION_BLOCKED", reason_code=str(exc),
                      traceback_tail=traceback.format_exception(exc)[-4:])
    except SystemExit as exc:
        report.update(outcome="SYSTEM_EXIT", reason_code=str(exc)[:500])
    except Exception as exc:
        report.update(outcome="ERROR", error_type=type(exc).__name__, reason_code=str(exc)[:500],
                      traceback_tail=traceback.format_exception(exc)[-6:])
    after = _rchar()
    report["elapsed_s"] = round(time.perf_counter() - started, 3)
    report["rchar_bytes"] = (after - rchar) if after is not None and rchar is not None else None
    report["subchecks"] = list(rows) + [row for group in state["groups"] for row in group]
    report["subcheck_source"] = "producer:continuation_subchecks" if continuation_subchecks else "legacy-decomposition"
    report["validators"] = state["validators"]
    report["repeated_large_file_opens"] = sorted(
        ({"path": path, "bytes": size, "opens": count,
          "call_sites": sorted(sites.items(), key=lambda item: -item[1])[:6]}
         for path, (size, count, sites) in state["large_file_opens"].items() if count > 1),
        key=lambda row: -row["bytes"] * row["opens"])
    return report


def seal_version(repo: Path, version: str, like: Path) -> None:
    """Seal a candidate tree as a versioned deployment (sandbox overlay only).

    Same authority paths as the deployed manifest ``like``; hashes, bytes and
    the commit identity are the candidate's, so the candidate is read exactly
    as a newly deployed version would be.
    """
    sys.path.insert(0, str(repo))
    from src.autoslice.repository_asset_authority import build_deployed_authority_manifest

    entries = json.loads(like.read_text(encoding="utf-8"))["entries"]
    manifest = build_deployed_authority_manifest(
        repo_root=repo, deployed_commit=version, relative_paths=[Path(path) for path in sorted(entries)])
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_bytes((json.dumps(
        manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode())
    (repo / "DEPLOYED_COMMIT").write_text(version + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--seal-version":
        seal_version(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--code-root", type=Path, required=True)
    parser.add_argument("--substrate", required=True, choices=("existing_srt", "aggregate_asr"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--time-budget-s", type=int, default=0)
    args = parser.parse_args()
    args.code_root = args.code_root.resolve()
    report = run_one(args)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_id": report.get("candidate_id"), "outcome": report.get("outcome"),
                      "reason_code": report.get("reason_code"), "elapsed_s": report.get("elapsed_s")},
                     ensure_ascii=False))
    return 0 if str(report.get("outcome", "")).startswith("REACHED_") else 1


if __name__ == "__main__":
    raise SystemExit(main())
