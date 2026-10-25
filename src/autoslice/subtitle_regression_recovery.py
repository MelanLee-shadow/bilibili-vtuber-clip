"""Interrupted normal regression inputs; no scoring or provider authorization here.

Reuse the evaluator's private layout, native typed current-text validator,
carryover loader, semantic recorder and immutable artifact writer. This is the
ordinary worker's input preparation, not an alternate subtitle pipeline.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat

from src.autoslice.argon_escalation_storage import directory as private_directory, immutable, put_json
from src.autoslice.final_review_carryover import (
    _write_bytes_atomic, carryover_path, load_replayable_final_review_carryover,
)
from src.autoslice.final_review_contract import _validate_exact_final_cpa_self_heal
from src.autoslice.review_evidence_store import review_from_bytes

SCHEMA = "subtitle-interrupted-resume.v1"


_PROVIDER_AUDIT = None
_PROVIDER_AUDIT_INSTALLED = False


@contextmanager
def without_provider_dispatch():
    """Bound a preparation-only worker; importing a reviewer grants no I/O."""
    import sys
    global _PROVIDER_AUDIT, _PROVIDER_AUDIT_INSTALLED
    if not _PROVIDER_AUDIT_INSTALLED:
        def audit(event, _args):
            if _PROVIDER_AUDIT is not None and event in {
                "socket.connect", "socket.sendto", "socket.getaddrinfo",
                "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork",
            }:
                _PROVIDER_AUDIT["blocked_dispatches"].append(event)
                raise RuntimeError("PREFLIGHT_PROVIDER_DISPATCH_FORBIDDEN: " + event)
        sys.addaudithook(audit)
        _PROVIDER_AUDIT_INSTALLED = True
    previous = _PROVIDER_AUDIT
    activity = {"blocked_dispatches": [], "provider_calls": 0}
    _PROVIDER_AUDIT = activity
    try:
        yield activity
    finally:
        _PROVIDER_AUDIT = previous


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def no_links(path: Path) -> None:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("interrupted recovery path contains a symlink")


def stable(path: Path, expected: str | None = None) -> bytes:
    no_links(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("interrupted recovery needs a regular file")
    raw = path.read_bytes()
    after = path.stat()
    def identity(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after) or len(raw) != after.st_size:
        raise ValueError("interrupted recovery file changed during read")
    if expected is not None and sha(raw) != expected:
        raise ValueError("interrupted recovery file hash drift: " + str(path))
    return raw


def inventory(root: Path) -> tuple[dict, dict]:
    files, links = {}, {}
    no_links(root)
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs) + names:
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                links[relative] = os.readlink(path)
                if name in dirs:
                    dirs.remove(name)
            elif stat.S_ISREG(mode):
                raw = stable(path)
                files[relative] = {"bytes": len(raw), "sha256": sha(raw)}
            elif not stat.S_ISDIR(mode):
                raise ValueError("interrupted recovery special file")
    return files, links


def _file_rows_match(actual: dict, expected: dict) -> bool:
    return set(actual) == set(expected) and all(
        actual[k] == {n: expected[k][n] for n in ("bytes", "sha256")} for k in actual
    )


def _assert_dead(start: dict, parent: Path) -> None:
    # A terminal file or an unlocked file alone cannot prove process exit.
    for key in ("launcher_pid", "worker_pid"):
        pid = start.get(key)
        if type(pid) is not int or pid <= 0:
            raise ValueError("interrupted parent lacks process identity")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise ValueError("interrupted parent process is still alive")
    proc = Path("/proc")
    if proc.is_dir():
        for path in proc.iterdir():
            if not path.name.isdigit() or int(path.name) == os.getpid():
                continue
            try:
                args = (path / "cmdline").read_bytes().split(b"\0")
                matches = any(args[i] == b"--output" and args[i + 1].decode() == str(parent)
                              for i in range(len(args) - 1))
                if matches:
                    raise ValueError("interrupted parent still has an output writer")
            except (OSError, UnicodeDecodeError):
                continue


def reconcile_requests(folder: Path, runtime: Path) -> dict:
    """Validate original request/response pairs, without sealing or refunding."""
    from src.autoslice.native_audio_budget_receipt import _validate_existing, _validate_budget_history
    from src.autoslice.semantic_retry_progress import SemanticAttemptRecorder

    native = folder / "native-audio-budget.json"
    native_summary = {"present": False}
    if native.exists():
        receipt = json.loads(stable(native))
        _validate_existing(receipt, source_media_sha256=receipt["source_media_sha256"])
        _validate_budget_history(receipt["budget"])
        budget = receipt["budget"]
        native_summary = {"present": True, "receipt_sha256": sha(stable(native)),
                          "source_media_sha256": receipt["source_media_sha256"],
                          "attempt_count": budget["attempt_count"],
                          "pending_attempt_count": budget["pending_attempt_count"],
                          "total_audio_ms": budget["total_audio_ms"],
                          "revision": budget["revision"],
                          "status_counts": dict(Counter(r["status"] for r in budget["attempts"]))}
    counts, unknown = Counter(), []
    for directory in sorted((folder / "cpa-text-exchanges").glob("request-*")):
        request_path = directory / "request.json"
        # A pre-dispatch archive interrupted before request.json is not success.
        if not request_path.exists():
            unknown.append(str(directory / "request.json"))
            continue
        request = json.loads(stable(request_path))
        if request.get("schema_version") != "private-cpa-text-exchange.v1" or request.get("status") != "STARTED":
            raise ValueError("CPA request schema or state drift")
        stable(directory / "prompt.txt", request["prompt_sha256"])
        terminal_path = directory / "terminal.json"
        if not terminal_path.exists():
            unknown.append(str(request_path))
            continue
        terminal = json.loads(stable(terminal_path))
        if terminal.get("status") == "COMPLETED":
            stable(directory / "completion.txt", terminal["completion_sha256"])
        elif terminal.get("status") != "OUTCOME_UNKNOWN":
            raise ValueError("CPA terminal is not a recognized outcome")
        else:
            unknown.append(str(request_path))
        counts[terminal["status"]] += 1
    semantic_counts = Counter()
    for directory in sorted((runtime / "cache/semantic-cpa-attempts").glob("*")):
        if not directory.is_dir():
            raise ValueError("semantic history directory malformed")
        # The existing reader checks schema, issue, request and completion SHA.
        recorder = SemanticAttemptRecorder.__new__(SemanticAttemptRecorder)
        recorder.directory, recorder.issue, recorder.blocked = directory, directory.name, None
        for row in recorder._entries():
            semantic_counts[row["status"]] += 1
    return {"native": native_summary, "cpa_text_outcomes": dict(counts),
            "cpa_unknown": unknown, "semantic_outcomes": dict(semantic_counts),
            "unknown_refunded": False, "cost_usd": None,
            "counters_overlap": True}


def validate_stage_history(
    folder: Path,
    current: bytes,
    authority: dict,
    *,
    authority_sha256: str | None = None,
) -> dict:
    """Read immutable stage tuples; a stale pointer cannot create a verdict.

    ``authority_sha256`` is the hash of the sealed compact authority object;
    callers that expand review children must pass it so representation changes
    do not weaken the stage identity check.
    """
    paths = sorted((folder / "regression-checkpoints").glob("stage-*.json"))
    previous, last = None, None
    for sequence, path in enumerate(paths, 1):
        raw = stable(path)
        row = json.loads(raw)
        if (path.name != "stage-%04d.json" % sequence
                or row.get("schema_version") not in {"subtitle-regression-stage.v1", "subtitle-regression-stage.v2"}
                or row.get("sequence") != sequence or row.get("parent_stage_sha256") != previous
                or row.get("release_authorized") is not False):
            raise ValueError("interrupted stage history drift")
        blobs = path.parent / "blobs"
        if row["schema_version"] == "subtitle-regression-stage.v2":
            presence = row.get("input_presence")
            if presence != {"text": row.get("text_sha256") is not None,
                            "authority": row.get("authority_sha256") is not None}:
                raise ValueError("interrupted preparation input presence drift")
        elif row.get("text_sha256") is None or row.get("authority_sha256") is None:
            raise ValueError("interrupted legacy stage lacks inputs")
        if row.get("text_sha256") is not None:
            stable(blobs / (row["text_sha256"] + ".srt"), row["text_sha256"])
        if row.get("authority_sha256") is not None:
            saved_raw = stable(blobs / (row["authority_sha256"] + ".json"))
            # The stage hash names the sealed representation.  Verify it
            # before expanding any content-addressed review children.
            saved_compact = json.loads(saved_raw)
            if sha(canonical(saved_compact)) != row["authority_sha256"]:
                raise ValueError("interrupted stage authority blob drift")
            saved = review_from_bytes(saved_raw, directory=folder)
            if not isinstance(saved, dict):
                raise ValueError("interrupted stage authority blob type drift")
        invalid = row.get("authority_input")
        if invalid is not None:
            if (not isinstance(invalid, dict) or row.get("authority_sha256") is not None
                    or invalid.get("state") not in {"INVALID_JSON", "INVALID_TYPE", "READ_ERROR"}
                    or not isinstance(invalid.get("error"), dict)):
                raise ValueError("interrupted preparation invalid authority state drift")
            raw_sha = invalid.get("raw_sha256")
            if raw_sha is not None:
                saved_raw = stable(blobs / (raw_sha + ".authority.raw"), raw_sha)
                if len(saved_raw) != invalid.get("raw_bytes"):
                    raise ValueError("interrupted preparation raw authority binding drift")
                snapshot = invalid.get("source_snapshot")
                if snapshot is not None and snapshot.get("sha256") != raw_sha:
                    raise ValueError("interrupted preparation raw authority snapshot drift")
            elif invalid["state"] != "READ_ERROR":
                raise ValueError("interrupted preparation invalid authority lacks raw bytes")
        previous, last = sha(raw), row
    authority_digest = (
        str(authority_sha256).removeprefix("sha256:")
        if authority_sha256 is not None
        else sha(canonical(authority))
    )
    return {"stages": len(paths), "last_stage_sha256": previous,
            "last_phase": last["phase"] if last else None,
            "current_matches_last_stage": bool(last and last["text_sha256"] == sha(current)
                                               and last["authority_sha256"] == authority_digest)}



def _failed_outer_binding(*, parent: Path, status: dict, status_path: Path,
                          code_root: Path, freeze: dict) -> dict:
    """Bind a completed failed worker to its actual bounded supervisor."""
    candidates = []
    for name in freeze["external_files"]:
        path = Path(name)
        if not path.name.endswith(".terminal.json"):
            continue
        payload = json.loads(stable(path))
        if "worker_terminal_path" in payload:
            candidates.append((path, payload))
    if len(candidates) != 1:
        raise ValueError("failed evaluation bounded outer terminal missing or ambiguous")
    path, outer = candidates[0]
    if (outer.get("worker_terminal_path") != str(status_path)
            or outer.get("worker_terminal_sha256") != sha(stable(status_path))
            or outer.get("status") != "COMPLETED"
            or outer.get("returncode") != status["returncode"]
            or outer.get("output") != str(parent)
            or outer.get("cwd") != str(code_root) or outer.get("head") != code_root.name
            or outer.get("wall_limit_reached") is not False):
        raise ValueError("failed evaluation bounded outer identity drift")
    return {"path": str(path), "sha256": sha(stable(path)),
            "worker_terminal_sha256": outer["worker_terminal_sha256"]}


def _failed_evaluation(parent: Path, status: dict, code_root: Path, cases_path: Path,
                       cases: list[dict], *, status_path: Path, freeze: dict) -> dict | None:
    """A failed evaluation is terminal evidence, never complete package input."""
    from scripts.subtitle_regression_eval import aggregate
    evaluation_path = parent / "evaluation.json"
    if not evaluation_path.exists():
        if status.get("status") == "COMPLETED":
            raise ValueError("completed parent lacks its bound evaluation")
        return None
    raw = stable(evaluation_path)
    evaluation = json.loads(raw)
    package_keys = {c["package"] for c in cases}
    results, runs = evaluation.get("results"), evaluation.get("runs")
    actual_runs = {p.parent.name: sha(stable(p)) for p in parent.glob("*/run.json")}
    if (status.get("status") != "COMPLETED" or status.get("returncode") != 1
            or status.get("evaluation_sha256") != sha(raw)
            or status.get("package_runs") != actual_runs
            or evaluation.get("schema_version") != "subtitle-regression-evaluation.v1"
            or evaluation.get("code_identity") != code_root.name
            or evaluation.get("code_root") != str(code_root)
            or evaluation.get("fixture_sha256") != sha(stable(cases_path))
            or not isinstance(results, list) or len(results) != len(cases)
            or not isinstance(runs, dict) or set(runs) != package_keys
            or set(actual_runs) - package_keys):
        raise ValueError("complete evaluation must use the complete-resume contract; failed evaluation identity invalid")
    by_id = {row.get("case_id"): row for row in results if isinstance(row, dict)}
    if len(by_id) != len(cases):
        raise ValueError("failed evaluation case coverage drift")
    for case in cases:
        row, run = by_id.get(case["id"]), runs[case["package"]]
        if (not isinstance(row, dict) or not isinstance(run, dict)
                or row.get("candidate_id") != case["candidate_id"]
                or row.get("outcome") != "EVALUATION_ERROR"
                or not isinstance(run.get("error"), str) or not run["error"]
                or row.get("reason") != run["error"]
                or row.get("would_release") != run.get("would_release")
                or run.get("would_release") is True):
            raise ValueError("complete evaluation must use the complete-resume contract; not an all-error failed attempt")
        run_path = parent / case["package"] / "run.json"
        if run_path.exists():
            actual = json.loads(stable(run_path))
            if (actual.get("error") != run["error"]
                    or actual.get("would_release") != run.get("would_release")):
                raise ValueError("failed evaluation package outcome drift")
    if evaluation.get("summary") != aggregate(results):
        raise ValueError("failed evaluation summary drift")
    outer = _failed_outer_binding(parent=parent, status=status, status_path=status_path,
                                  code_root=code_root, freeze=freeze)
    return {"classification": "COMPLETED_EVALUATION_FAILURE", "evaluation_sha256": sha(raw),
            "bounded_outer": outer,
            "missing_package_runs": sorted(package_keys - set(actual_runs)),
            "quality_inherited": False}


def _parent_lineage(parent: Path, status: dict, *, cases_path: Path, cases: list[dict],
                    runtime_root: Path, code_identity: str, seen: tuple[str, ...]) -> list[dict]:
    """Replay consumed claims and immutable parent bytes without changing them."""
    receipt_path = parent / "interrupted-resume.json"
    if not receipt_path.exists():
        if status.get("parent_output") or status.get("parent_freeze_sha256"):
            raise ValueError("interrupted parent lineage receipt missing")
        return []
    receipt_raw = stable(receipt_path)
    receipt = json.loads(receipt_raw)
    previous = Path(receipt["parent_output"]).absolute()
    if (receipt.get("schema_version") != SCHEMA or receipt.get("output") != str(parent)
            or receipt.get("preflight_only") is True or previous.parent != parent.parent
            or previous == parent or receipt.get("history_reset") is not False
            or receipt.get("quality_inherited") is not False
            or status.get("parent_output") != str(previous)
            or status.get("parent_freeze_sha256") != receipt.get("freeze_sha256")
            or status.get("head") != receipt.get("code_identity")):
        raise ValueError("interrupted parent lineage identity drift")
    original = inspect_parent(
        parent=previous, freeze_path=Path(receipt["freeze_path"]),
        freeze_sha256=receipt["freeze_sha256"], cases_path=cases_path, cases=cases,
        runtime_root=runtime_root, code_identity=receipt["code_identity"], _seen=seen)
    for key in ("parent_output", "parent_terminal_path", "parent_terminal_sha256",
                "parent_code_identity", "freeze_sha256", "fixture_sha256", "cases",
                "files", "links", "payment_ledger_target"):
        if receipt.get(key) != original.get(key):
            raise ValueError("interrupted inherited parent binding drift: " + key)
    claim_key = sha(canonical({"parent": str(previous), "terminal": receipt["parent_terminal_sha256"]}))
    claim_path = parent.parent / "interrupted-recovery-claims" / (claim_key + ".json")
    if receipt.get("claim_path") != str(claim_path):
        raise ValueError("interrupted consumed claim path drift")
    claim_raw = stable(claim_path)
    claim = json.loads(claim_raw)
    if claim != {"output": str(parent), "parent_terminal_sha256": receipt["parent_terminal_sha256"],
                 "freeze_sha256": receipt["freeze_sha256"], "code_identity": receipt["code_identity"]}:
        raise ValueError("interrupted consumed claim identity drift")
    copied = receipt.get("copied_to")
    if not isinstance(copied, dict) or set(copied) != set(original["files"]):
        raise ValueError("interrupted inherited original inventory missing")
    for relative, bound in original["files"].items():
        destination = Path(copied[relative])
        if destination.is_absolute() or ".." in destination.parts:
            raise ValueError("interrupted inherited copy escaped parent")
        raw = stable(parent / destination, bound["sha256"])
        if len(raw) != bound["bytes"]:
            raise ValueError("interrupted inherited original size drift")
    return [*original.get("lineage", []), {
        "parent_output": str(previous), "successor_output": str(parent),
        "parent_terminal_sha256": receipt["parent_terminal_sha256"],
        "parent_freeze_sha256": receipt["freeze_sha256"],
        "consumed_claim_path": str(claim_path), "consumed_claim_sha256": sha(claim_raw),
        "inheritance_receipt_sha256": sha(receipt_raw), "preserved_files": len(copied)}]

def inspect_parent(*, parent: Path, freeze_path: Path, freeze_sha256: str,
                   cases_path: Path, cases: list[dict], runtime_root: Path,
                   code_identity: str, _seen: tuple[str, ...] = ()) -> dict:
    from scripts.subtitle_regression_eval import (
        _is_private_experiment_directory, configure_replay_state, digest,
        normal_off_samples, validate_original_cases,
    )
    from src.autoslice.clip_context import validate_clip_context
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_recut_provenance import prepared_boundary_clock_binding

    parent = parent.absolute()
    if str(parent) in _seen or len(_seen) >= 32:
        raise ValueError("interrupted parent lineage cycle or depth invalid")
    seen = (*_seen, str(parent))
    no_links(parent)
    if not _is_private_experiment_directory(parent, runtime_root):
        raise ValueError("interrupted resume requires an existing private experiment")
    if (parent / "invocation.json").exists() or any(parent.glob("*/comparison-invocation.json")):
        raise ValueError("COMPARISON_REQUIRES_SAME_INVOCATION_RESUME")
    freeze = json.loads(stable(freeze_path, freeze_sha256))
    if freeze.get("schema_version") != "subtitle-interruption-evidence-freeze.v1" or freeze.get("parent_output") != str(parent):
        raise ValueError("interrupted freeze parent identity drift")
    files, links = inventory(parent)
    if not _file_rows_match(files, freeze["parent_files"]) or links != freeze["parent_links"]:
        raise ValueError("interrupted parent inventory drift")
    paid = str(runtime_root / "state/gemini-paid-backup")
    if links != {"runtime-state/state/gemini-paid-backup": paid}:
        raise ValueError("interrupted paid ledger binding drift")
    for section in ("external_inputs", "external_files"):
        for name, expected in freeze[section].items():
            raw = stable(Path(name), expected["sha256"])
            if len(raw) != expected["bytes"]:
                raise ValueError("interrupted external input size drift")
    code_root = Path(freeze["code_root"])
    code_files, code_links = inventory(code_root)
    if code_links or not _file_rows_match(code_files, freeze["parent_code_files"]):
        raise ValueError("interrupted parent code inventory drift")
    status_paths = [Path(p) for p in freeze["external_files"]
                    if Path(p).name.startswith("RUN-STATUS-") or Path(p).name == "attempt-terminal.json"]
    if len(status_paths) != 1:
        raise ValueError("interrupted parent terminal binding missing")
    status_path = status_paths[0]
    status = json.loads(stable(status_path))
    if (status.get("status") not in {"FAILED", "INTERRUPTED", "COMPLETED"}
            or type(status.get("returncode")) is not int or status["returncode"] == 0
            or status.get("argon_enabled", False) is not False):
        raise ValueError("parent process has no failed or interrupted terminal")
    if (status.get("output") != str(parent) or status.get("head") != code_root.name
            or status.get("cwd") != str(code_root)):
        raise ValueError("interrupted parent execution identity drift")
    start = status.get("started_utc")
    end = status.get("ended_utc")
    if (not start or not end or dt.datetime.fromisoformat(end) < dt.datetime.fromisoformat(start)
            or status.get("maximum_wall_seconds") not in range(1, 14401)):
        raise ValueError("interrupted parent clock or wall budget invalid")
    _assert_dead(status, parent)
    command = status.get("command", [])
    def option(key):
        if command.count(key) != 1:
            raise ValueError("parent command binding missing: " + key)
        return command[command.index(key) + 1]
    selected = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "--case"]
    if ("--worker" not in command or "--normal-off" not in command
            or option("--output") != str(parent) or option("--code-root") != str(code_root)
            or option("--code-identity") != code_root.name or option("--runtime-root") != str(runtime_root)
            or set(selected) != {c["id"] for c in cases} or len(selected) != len(cases)
            or Path(option("--cases")) != cases_path):
        raise ValueError("interrupted parent mode, cases or code binding drift")
    fixture_row = freeze["external_files"].get(str(cases_path))
    if not fixture_row or digest(cases_path) != fixture_row["sha256"]:
        raise ValueError("interrupted fixture is not frozen")
    failed_evaluation = _failed_evaluation(parent, status, code_root, cases_path, cases,
                                         status_path=status_path, freeze=freeze)
    lineage = _parent_lineage(parent, status, cases_path=cases_path, cases=cases,
                              runtime_root=runtime_root, code_identity=code_identity, seen=seen)
    doc = json.loads(stable(cases_path))
    # Validate the original shared account; do not reinitialize it.
    before_base = os.environ.get("AUTOSLICE_BASE")
    try:
        configure_replay_state(parent, runtime_root, resume=True)
    finally:
        if before_base is None:
            os.environ.pop("AUTOSLICE_BASE", None)
        else:
            os.environ["AUTOSLICE_BASE"] = before_base
    packages = {}
    for key in dict.fromkeys(c["package"] for c in cases if c["status"] == "ready"):
        package, folder = doc["packages"][key], parent / key
        original = stable(Path(package["srt_path"]), package["srt_sha256"])
        if stable(folder / "original.srt") != original:
            raise ValueError("interrupted original subtitle drift")
        tracked = {}
        for field in ("record", "srt", "authority", "media", "context", "provenance"):
            if field + "_path" not in package:
                continue
            path = Path(package[field + "_path"])
            bound = freeze["external_inputs"].get(str(path))
            if bound is None:
                raise ValueError("interrupted source input is not frozen")
            raw = stable(path, bound["sha256"])
            if package.get(field + "_sha256", sha(raw)) != sha(raw):
                raise ValueError("interrupted original source binding drift")
            tracked[str(path)] = sha(raw)
        record = json.loads(stable(Path(package["record_path"])))
        context = json.loads(stable(Path(package["context_path"])))
        validate_clip_context(context, candidate_id=package["candidate_id"])
        if (record["subtitle_audio_correspondence"]["intro_offset_ms"] != package["intro_offset_ms"]
                or record["clip_context_payload_sha256"] != context["context_sha256"]):
            raise ValueError("interrupted source clock/context drift")
        clock = prepared_boundary_clock_binding(
            media_path=Path(package["media_path"]), completed_record=record,
            clip_context=context, candidate_id=package["candidate_id"])
        original_cues = [{"index": i, "start_ms": c.start_ms, "end_ms": c.end_ms, "text": c.text}
                         for i, c in enumerate(parse_srt_cues(original.decode()), 1)]
        package_cases = [c for c in cases if c["package"] == key]
        validate_original_cases(package_cases, original_cues)
        samples = normal_off_samples(package, package_cases, original_cues)
        current = stable(folder / "final.srt")
        authority_raw = stable(folder / "authority.json")
        authority_compact = json.loads(authority_raw)
        authority = review_from_bytes(authority_raw, directory=folder)
        if current != original and not authority.get("exact_final_cpa_self_heal"):
            raise ValueError("changed current subtitle has no native typed checkpoint")
        _validate_exact_final_cpa_self_heal(
            authority, expected_srt_sha256="sha256:" + sha(current), allow_pending=True)
        stages = validate_stage_history(
            folder, current, authority,
            authority_sha256=sha(canonical(authority_compact)),
        )
        run_path = folder / "run.json"
        prior_run = json.loads(stable(run_path)) if run_path.exists() else None
        if prior_run is not None:
            if (prior_run.get("input_drift") or prior_run.get("candidate_id") != package["candidate_id"]
                    or prior_run.get("final_srt_sha256") != sha(current)
                    or any(tracked.get(p) != h for p, h in prior_run.get("input_hashes", {}).items())
                    or prior_run.get("normal_off", {}).get("probe_samples_sha256") != sha(canonical(samples))):
                raise ValueError("interrupted completed package binding drift")
        elif current != original and authority["exact_final_cpa_self_heal"].get("status") != "REVIEW_PENDING":
            raise ValueError("interrupted current text lacks a pending native checkpoint")
        # An in-flight input is evidence of the next review's exact bytes, not
        # a successful review. Existing completed reviews retain their own grid.
        for review in folder.glob("review-*.json"):
            number = review.stem.removeprefix("review-")
            inp = folder / ("review-input-" + number + ".srt")
            audit = review_from_bytes(stable(review), directory=folder)
            if audit.get("reviewed_srt_sha256") != "sha256:" + sha(stable(inp)):
                raise ValueError("interrupted review input binding drift")
        if prior_run is None:
            inputs = sorted(folder.glob("review-input-*.srt"))
            if (not stages["current_matches_last_stage"]
                    and (not inputs or stable(inputs[-1]) != current)):
                raise ValueError("interrupted next review has no exact current input or atomic stage")
        carry = carryover_path(folder, package["candidate_id"])
        for path in (carry, carry.with_suffix(".checkpoint.json")):
            if path.exists():
                document = json.loads(stable(path))
                rows = document.get("findings")
                integrity = document.get("integrity", {})
                if (not isinstance(rows, list) or integrity.get("finding_count") != len(rows)
                        or integrity.get("findings_sha256") != sha(canonical(rows))):
                    raise ValueError("interrupted carryover integrity drift")
        rows = load_replayable_final_review_carryover(carry)
        accounting = reconcile_requests(folder, parent / "runtime-state")
        if accounting["native"].get("source_media_sha256", package["media_sha256"]) != package["media_sha256"]:
            raise ValueError("interrupted native budget source drift")
        packages[key] = {"candidate_id": package["candidate_id"], "parent_package": str(folder),
                         "current_srt_sha256": sha(current), "authority_sha256": sha(stable(folder / "authority.json")),
                         "original_srt_sha256": sha(original), "input_hashes": tracked,
                         "normal_samples": samples, "source_clock": clock, "stage_history": stages,
                         "prior_run_sha256": sha(stable(run_path)) if prior_run else None,
                         "parent_package_state": ("FAILED_BEFORE_PACKAGE_RUN" if failed_evaluation and prior_run is None
                                                  else "FAILED" if prior_run and prior_run.get("error")
                                                  else "RETURNED" if prior_run and not prior_run.get("interrupted")
                                                  else "INTERRUPTED"),
                         "next_state": "ORDINARY_EXACT_FINAL_REVIEW_REQUIRED",
                         "carryover_cues": [r.get("cue") for r in rows],
                         "authority_findings": len(authority.get("final_review_audit", {}).get("findings", [])),
                         "accounting": accounting, "quality_inherited": False}
    if inventory(parent) != (files, links):
        raise ValueError("interrupted parent changed during validation")
    return {"schema_version": SCHEMA, "parent_output": str(parent),
            "freeze_path": str(freeze_path), "freeze_sha256": freeze_sha256,
            "parent_terminal_path": str(status_path), "parent_terminal_sha256": sha(stable(status_path)),
            "parent_code_identity": code_root.name, "code_identity": code_identity,
            "fixture_sha256": sha(stable(cases_path)), "cases": [c["id"] for c in cases],
            "packages": packages, "files": files, "links": links, "payment_ledger_target": paid,
            "failed_evaluation": failed_evaluation, "lineage": lineage,
            "history_reset": False, "quality_inherited": False, "provider_calls": 0}


def stage_successor(record: dict, out: Path, *, preflight_only: bool = False) -> dict:
    """Create one child; a failed preparation consumes the claim, never a retry."""
    parent = Path(record["parent_output"])
    no_links(out)
    if out.parent != parent.parent or out == parent or out.exists():
        raise ValueError("interrupted successor must be a new sibling attempt")
    claim_key = sha(canonical({"parent": str(parent), "terminal": record["parent_terminal_sha256"]}))
    claim_dir = parent.parent / "interrupted-recovery-claims"
    claim = claim_dir / (claim_key + ".json")
    if not preflight_only:
        claim_dir.mkdir(mode=0o700, exist_ok=True)
        no_links(claim_dir)
        # O_EXCL, not a resettable lease. Same parent cannot fork across output IDs.
        with claim.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical({"output": str(out), "parent_terminal_sha256": record["parent_terminal_sha256"],
                                    "freeze_sha256": record["freeze_sha256"], "code_identity": record["code_identity"]}))
            stream.flush()
            os.fsync(stream.fileno())
    out.mkdir(mode=0o700)
    mapping, active_mapping = {}, {}
    for relative, bound in record["files"].items():
        path = Path(relative)
        archive = str(Path("predecessor") / record["freeze_sha256"] / relative)
        active = not (relative in {"replay-state.json", "evaluation.json", "evaluation.md", "resume-status.json",
                                  "interrupted-resume.json", "attempt-start.json", "attempt-terminal.json",
                                  "experiment-progress.json"}
                      or path.name in {"run.json", "regression-checkpoint.json"}
                      or path.parts[0] == "package-results"
                      or (len(path.parts) == 2 and path.parts[0] in record["packages"]
                          and path.name.startswith("normal-off-source.")))
        # A complete immutable predecessor archive survives mutations of active
        # current text, carryover, budget and cache pointers in a later attempt.
        data = stable(parent / relative, bound["sha256"])
        for destination in ([archive, relative] if active else [archive]):
            target = out / destination
            private_directory(target.parent)
            with target.open("xb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        mapping[relative] = archive
        if active:
            active_mapping[relative] = relative
    link = out / "runtime-state/state/gemini-paid-backup"
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    link.symlink_to(record["payment_ledger_target"], target_is_directory=True)
    replay = {"experiment_state_root": str(out / "runtime-state"),
              "production_runtime_root": str(Path(record["payment_ledger_target"]).parents[1]),
              "production_retry_history_consumed": False,
              "paid_backup_ledger": record["payment_ledger_target"], "payment_policy_changed": False}
    put_json(out / "replay-state.json", replay)
    copied = {**record, "output": str(out), "claim_path": None if preflight_only else str(claim),
              "preflight_only": preflight_only, "copied_to": mapping,
              "active_copied_to": active_mapping, "complete_predecessor_archive": True}
    put_json(out / "interrupted-resume.json", copied)
    if inventory(parent) != (record["files"], record["links"]):
        raise ValueError("interrupted parent changed during staging")
    return copied


def package_checkpoint(out: Path, *, phase: str, run: dict,
                       text: bytes | None, authority: dict | None,
                       authority_raw: bytes | None = None,
                       authority_input: dict | None = None) -> Path:
    """Publish actual input presence and state before advancing the pointer."""
    root = out / "regression-checkpoints"
    root.mkdir(mode=0o700, exist_ok=True)
    sequence = len(list(root.glob("stage-*.json"))) + 1
    text_sha = sha(text) if text is not None else None
    authority_sha = sha(canonical(authority)) if authority is not None else None
    if text is not None:
        immutable(root / "blobs" / (text_sha + ".srt"), text)
    if authority is not None:
        put_json(root / "blobs" / (authority_sha + ".json"), authority)
    if authority_raw is not None:
        if authority_input is None or authority_input.get("raw_sha256") != sha(authority_raw):
            raise ValueError("interrupted preparation raw authority binding drift")
        immutable(root / "blobs" / (sha(authority_raw) + ".authority.raw"), authority_raw)
    previous = sorted(root.glob("stage-*.json"))
    record = {"schema_version": "subtitle-regression-stage.v2", "phase": phase,
              "sequence": sequence, "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
              "parent_stage_sha256": sha(stable(previous[-1])) if previous else None,
              "text_sha256": text_sha, "authority_sha256": authority_sha,
              "input_presence": {"text": text is not None, "authority": authority is not None},
              "run": run, "release_authorized": False}
    if authority_input is not None:
        record["authority_input"] = authority_input
    path = root / ("stage-%04d.json" % sequence)
    put_json(path, record)
    _write_bytes_atomic(out / "regression-checkpoint.json", canonical(
        {"path": str(path), "sha256": sha(stable(path)), "phase": phase, "release_authorized": False}))
    return path


class EvaluationInterrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__("ordinary regression interrupted")


@contextmanager
def bounded_attempt(seconds: int):
    if type(seconds) is not int or not 1 <= seconds <= 14400:
        raise ValueError("normal recovery wall limit must be 1..14400 seconds")
    def interrupted(signum, _frame):
        raise EvaluationInterrupted(signum)
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGALRM)}
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def parent_lock(parent: Path):
    path = parent.parent / ("." + parent.name + ".worker.lock")
    no_links(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise ValueError("interrupted parent lock unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
