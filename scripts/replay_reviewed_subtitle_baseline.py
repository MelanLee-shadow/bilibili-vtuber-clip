#!/usr/bin/env python3
"""Canonical no-upload replay of one or more reviewed subtitle baselines.

``plan`` reads authority only.  ``full-dry-run`` creates candidate-private
stages and validates complete sealed after-images; ``apply`` prepares in
parallel and commits each valid candidate state-last under its own short lease.
There are deliberately no caller-supplied target or state-after switches.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_subtitle_text_overrides import apply_document
from scripts.produce_slice_package import (
    _accurate_reencode_recut_command, _burn_preview_subtitles,
    _build_aggregate_asr_transcriber, _build_ssh_agy_transcribe_runner,
    _load_term_boundary_surfaces, _review_glossary, _topic_graph_disabled,
    _topic_graph_expected_sha256, _topic_graph_path, profile_asset_file,
    _stage_publish_draft, _write_source_range_srt, profile_delivery_root,
    run, run_producer_speaker_finalization,
)
from scripts.suggest_upload_tags import generate_upload_tags
from src.autoslice.producer_package_finalization import ProducerFinalizationAdapters
from src.autoslice.producer_text_pipeline import TextPipelineAdapters
from src.autoslice.reviewed_baseline_replay import (
    ReviewedBaselineReplayError, _canonical, _production_llm_call, _safe_directory, _sha,
    build_replay_plan, prepare_replay_after_image, rebind_replay_after_image_state, stage_replay,
    synthesize_replay_spec_and_finalize_private, regular_binding,
)
from src.autoslice.reviewed_baseline_replay_transaction import (
    cleanup_prepared_after_image_stage, commit_prepared_after_image, stream_binding,
)

_TALK_DISCOVERY_LANES = frozenset({"talk", "semantic_recall", "semantic_recall_sharded"})
_REPLAY_OWNED_STALE_REASONS = frozenset({
    "STATE_ROW_NOT_REVIEW_READY", "PACKAGE_ARTIFACT_HASH_DRIFT", "COVER_QC_MISSING",
    "SELECTION_SUPPORT_TERMINAL_BLOCKED",
})
_REPLAY_NORMALIZABLE_CATEGORIES = frozenset({"STATE_DRIFT", "NEEDS_IVAN_TRUTH"})
_SAFE_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,159}\Z")


class _PrepareFailure(RuntimeError):
    def __init__(self, *, reason_code: str, provider_attempted: bool, stage_manifest_sha256: str | None = None,
                 prepared_manifest_sha256: str | None = None,
                 provider_receipt_sha256s: tuple[str, ...] = ()) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.provider_attempted = provider_attempted
        self.stage_manifest_sha256 = stage_manifest_sha256
        self.prepared_manifest_sha256 = prepared_manifest_sha256
        self.provider_receipt_sha256s = provider_receipt_sha256s


def _safe_reason_code(exc: BaseException) -> str:
    """Expose only a stable typed reason, never provider or filesystem text."""

    value = getattr(exc, "reason_code", str(exc))
    if isinstance(value, str) and _SAFE_REASON_CODE.fullmatch(value):
        return value
    if isinstance(exc, SystemExit):
        return "REPLAY_PREPARE_SYSTEM_EXIT"
    return "REPLAY_PREPARE_EXCEPTION"


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--full-dry-run", action="store_true")
    modes.add_argument("--apply", action="store_true")
    modes.add_argument("--readiness-graph", action="store_true")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--candidate-id", action="append", default=[])
    parser.add_argument("--private-stage-parent", type=Path)
    args = parser.parse_args(argv)
    if not args.readiness_graph and not args.candidate_id:
        parser.error("--candidate-id is required outside --readiness-graph")
    if len(args.candidate_id) != len(set(args.candidate_id)):
        parser.error("candidate ids must be unique")
    if len(args.candidate_id) > 5:
        parser.error("at most five candidates may be prepared together")
    if (args.full_dry_run or args.apply) and args.private_stage_parent is None:
        parser.error("full-dry-run/apply require --private-stage-parent")
    return args


def _readiness_graph(*, runtime: Path, date: str, candidate_ids: list[str]) -> dict[str, object]:
    """Return the generic graph plus the sealed replay lane's read-only view."""

    from src.autoslice.publication_readiness import build_readiness_graph

    graph = build_readiness_graph(
        repository_root=runtime / "repo", runtime_root=runtime,
        recording_dates=frozenset({date}),
        candidate_ids=frozenset(candidate_ids) if candidate_ids else None,
    )
    rows = graph.get("rows")
    if not isinstance(rows, list):
        raise ReviewedBaselineReplayError("REPLAY_READINESS_GRAPH_INVALID")
    wanted = set(candidate_ids)
    scoped = [
        row for row in rows
        if isinstance(row, dict) and str(row.get("recording_date") or "") == date
        and (not wanted or str(row.get("candidate_id") or "") in wanted)
    ]
    # ``candidate_rejected`` is intentionally not upload-ready in the generic
    # graph.  It can nevertheless be legally schedulable for this separate
    # no-upload replay lane when all sealed baseline/old-record authority
    # still builds an exact plan.  Discovery labels ``semantic_recall`` and
    # ``semantic_recall_sharded`` are Talk lanes, not Song lanes.  This narrow
    # normalization accepts only stale package observations owned by replay;
    # it never hides a human, hold, ledger, or upload conflict.
    from src.autoslice.runner_state_writeback import read_exact_state_preimage
    raw = read_exact_state_preimage(runtime / "state" / f"{date}.json", runtime_root=runtime)
    try:
        state = json.loads((raw or b"").decode("utf-8"))
        picks = state.get("picks") if isinstance(state, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        picks = None
    pick_by_cid = {
        str(row.get("cid") or row.get("candidate_id") or ""): row
        for row in (picks if isinstance(picks, list) else [])
        if isinstance(row, dict)
    }
    normalized: list[dict[str, object]] = []
    for original in scoped:
        row = dict(original)
        candidate_id = str(row.get("candidate_id") or "")
        pick = pick_by_cid.get(candidate_id)
        reason_codes = original.get("reason_codes")
        stale_reasons = (
            isinstance(reason_codes, list)
            and bool(reason_codes)
            and all(isinstance(reason, str) for reason in reason_codes)
            and set(reason_codes).issubset(_REPLAY_OWNED_STALE_REASONS)
        )
        if (
            row.get("category") in _REPLAY_NORMALIZABLE_CATEGORIES
            and isinstance(pick, dict)
            and pick.get("status") == "candidate_rejected"
            and str(pick.get("lane") or "talk").lower() in _TALK_DISCOVERY_LANES
            and stale_reasons
        ):
            try:
                plan = build_replay_plan(
                    repo_root=runtime / "repo", out_root=runtime / "out",
                    date=date, candidate_id=candidate_id,
                )
            except ReviewedBaselineReplayError as exc:
                code = str(exc)
                if "BASELINE" in code or "PIN" in code or "TRUTH" in code:
                    row["category"] = "NEEDS_IVAN_TRUTH"
                elif "STATE" in code or "RECORD" in code or "PREIMAGE" in code:
                    row["category"] = "STATE_DRIFT"
                else:
                    row["category"] = "CODE_DEFECT"
                row["replay_readiness"] = {"predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY",
                                           "status": "FAIL", "reason_code": code}
            else:
                row["category"] = "READY_TO_PREPARE"
                row["replay_readiness"] = {
                    "predicate": "SEALED_REVIEWED_BASELINE_REPLAY_READY",
                    "status": "PASS", "baseline_sha256": plan.baseline.config.get("sha256"),
                }
                row["generic_observation"] = {
                    "category": original.get("category"),
                    "reason_codes": original.get("reason_codes", []),
                }
        normalized.append(row)
    # The graph is itself the predicate/reason evidence; do not synthesize an
    # upload grant from state status in this CLI.
    return {"schema_version": "reviewed-baseline-replay-readiness.v1", "mode": "READINESS_GRAPH",
            "upload_allowed": False, "rows": normalized,
            "graph_problems": graph.get("graph_blockers", [])}


def _private_parent(path: Path) -> Path:
    path = _safe_directory(Path(path))
    try:
        row = os.lstat(path)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_PARENT_MISSING") from exc
    if stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode) or stat.S_IMODE(row.st_mode) != 0o700:
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_PARENT_UNSAFE")
    return path


def _adapters() -> ProducerFinalizationAdapters:
    return ProducerFinalizationAdapters(
        accurate_recut_command=_accurate_reencode_recut_command, run_command=run,
        write_source_range_srt=_write_source_range_srt,
        apply_text_override_document=apply_document,
        run_speaker_finalization=run_producer_speaker_finalization,
        burn_preview_subtitles=_burn_preview_subtitles,
        stage_publish_draft=_stage_publish_draft,
        generate_upload_tags=generate_upload_tags, delivery_root=profile_delivery_root,
    )


def _text_adapters() -> TextPipelineAdapters:
    """Normal producer authority/asset construction for exact-final review."""

    return TextPipelineAdapters(
        build_aggregate_transcriber=_build_aggregate_asr_transcriber,
        build_agy_transcriber=_build_ssh_agy_transcribe_runner,
        load_term_boundary_surfaces=_load_term_boundary_surfaces,
        profile_asset_file=profile_asset_file, review_glossary=_review_glossary,
        topic_graph_disabled=_topic_graph_disabled, topic_graph_path=_topic_graph_path,
        topic_graph_expected_sha256=_topic_graph_expected_sha256,
    )


def _runtime_gate(runtime: Path) -> None:
    if not (runtime / "DISABLED").is_file() or (runtime / "DISABLED").is_symlink():
        raise ReviewedBaselineReplayError("REPLAY_DISABLED_REQUIRED")
    if (runtime / "AUTO_UPLOAD").exists() or (runtime / "AUTO_UPLOAD").is_symlink():
        raise ReviewedBaselineReplayError("REPLAY_AUTO_UPLOAD_FORBIDDEN")


_AFTER_IMAGE_PREDICATES = (
    "SOURCE_FACT_REVIEW", "EXACT_DELIVERY_BOUNDARY_REVIEW", "EXACT_FINAL_RELEASE_REVIEW",
    "RECORD_PUBLISH_CHAT_MIRRORS", "FROZEN_TITLE_AUTHORITY",
    "COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH", "PACKAGE_AUDIT", "STATE_AFTER_IMAGE_DIFF",
)


def _matrix(
    plan, *, status: str, failures: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Render one truthful row per predicate without blanket failure claims."""

    terminal = status == "PASS"
    rows = []
    for source in plan.matrix:
        if source.get("predicate") == "UPLOAD_ALLOWED":
            continue
        row = dict(source)
        if row.get("status") in {"PENDING", "PENDING_STAGE", "BLOCKED_BY_SOURCE_FACT"}:
            row["status"] = "PASS" if terminal else "NOT_EVALUATED"
            row.pop("reason_code", None)
        rows.append(row)
    existing = {str(row.get("predicate")) for row in rows}
    for predicate in _AFTER_IMAGE_PREDICATES:
        if predicate in existing:
            continue
        row: dict[str, object] = {
            "predicate": predicate, "status": "PASS" if terminal else "NOT_EVALUATED",
        }
        rows.append(row)
    rows.append({"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"})
    indexed = {str(row["predicate"]): row for row in rows}
    for predicate, reason_code in sorted((failures or {}).items()):
        row = indexed.get(predicate)
        if row is None:
            row = {"predicate": predicate}
            rows.append(row)
            indexed[predicate] = row
        row.update({"status": "FAIL", "reason_code": reason_code})
    if len({str(row.get("predicate")) for row in rows}) != len(rows):
        raise ReviewedBaselineReplayError("REPLAY_PREDICATE_MATRIX_DUPLICATE")
    return rows


def _canon(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sanitized_failure_receipt(*, runtime: Path, plan, matrix: list[dict[str, object]], provider_attempted: bool,
                                stage_manifest_sha256: str | None = None,
                                prepared_manifest_sha256: str | None = None,
                                provider_receipt_sha256s: tuple[str, ...] = ()) -> str:
    """Create one sealed diagnostic receipt without a path, prompt, or error text."""

    deployed = (runtime / "repo" / "DEPLOYED_COMMIT").read_text(encoding="utf-8").strip()
    body = {
        "schema_version": "reviewed-baseline-replay-diagnostic.v1",
        "candidate_id": plan.candidate_id, "recording_date": plan.date,
        "upload_allowed": False, "provider_attempted": provider_attempted,
        "deployed_commit": deployed,
        "deployed_authority_manifest_sha256": regular_binding(
            runtime / "repo" / "DEPLOYED_AUTHORITY_MANIFEST.json", label="DEPLOYED_MANIFEST"
        ).sha256,
        "predicate_matrix": matrix,
        "baseline_sha256": str(plan.baseline.config.get("sha256") or ""),
        "expected_recut_sha256": plan.expected_video_sha256,
        "stage_manifest_sha256": stage_manifest_sha256,
        "prepared_manifest_sha256": prepared_manifest_sha256,
        "provider_receipt_sha256s": list(provider_receipt_sha256s),
    }
    body["receipt_sha256"] = "sha256:" + hashlib.sha256(_canon(body)).hexdigest()
    root = runtime
    for index, component in enumerate(("reports", "reviewed-baseline-replay-diagnostics", plan.date, plan.candidate_id)):
        root /= component
        try:
            row = os.lstat(root)
        except FileNotFoundError:
            os.mkdir(root, 0o700)
            row = os.lstat(root)
        if (stat.S_ISLNK(row.st_mode) or not stat.S_ISDIR(row.st_mode)
                or (index and stat.S_IMODE(row.st_mode) != 0o700)):
            raise ReviewedBaselineReplayError("REPLAY_DIAGNOSTIC_NAMESPACE_UNSAFE")
    target = root / (body["receipt_sha256"].removeprefix("sha256:") + ".json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError:
        existing = regular_binding(target, label="DIAGNOSTIC")
        if existing is None or _read_exact_receipt(target, expected=existing) != _canon(body):
            raise ReviewedBaselineReplayError("REPLAY_DIAGNOSTIC_COLLISION")
        return body["receipt_sha256"]
    try:
        payload = _canon(body)
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise ReviewedBaselineReplayError("REPLAY_DIAGNOSTIC_SHORT_WRITE")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return body["receipt_sha256"]


def _read_exact_receipt(path: Path, *, expected) -> bytes:
    """Read an existing collision candidate through its bound no-follow inode."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        identity = (expected.device, expected.inode, expected.mode, expected.size,
                    expected.mtime_ns, expected.ctime_ns)
        if (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
                opened.st_mtime_ns, opened.st_ctime_ns) != identity:
            raise ReviewedBaselineReplayError("REPLAY_DIAGNOSTIC_COLLISION")
        chunks = []
        while chunk := os.read(fd, 64 * 1024):
            chunks.append(chunk)
    finally:
        os.close(fd)
    if regular_binding(path, label="DIAGNOSTIC") != expected:
        raise ReviewedBaselineReplayError("REPLAY_DIAGNOSTIC_COLLISION")
    return b"".join(chunks)


def _cleanup_private_stage(stage: Path, *, parent: Path) -> None:
    """Remove only this candidate-private owned directory; never follow links."""

    stage = Path(stage).absolute()
    parent = Path(parent).absolute()
    if stage.parent != parent or stage.is_symlink() or not stage.is_dir():
        raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_CLEANUP_UNSAFE")
    for root, dirs, files in os.walk(stage, topdown=False, followlinks=False):
        current = Path(root)
        if current.is_symlink():
            raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_CLEANUP_UNSAFE")
        for name in [*files, *dirs]:
            item = current / name
            row = os.lstat(item)
            if stat.S_ISLNK(row.st_mode):
                raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_CLEANUP_UNSAFE")
            if stat.S_ISREG(row.st_mode):
                os.unlink(item)
            elif stat.S_ISDIR(row.st_mode):
                os.rmdir(item)
            else:
                raise ReviewedBaselineReplayError("REPLAY_PRIVATE_STAGE_CLEANUP_UNSAFE")
    os.rmdir(stage)


def _stage_manifest_sha256(stage: Path) -> str | None:
    path = Path(stage) / "stage.json"
    binding = regular_binding(path, label="PRIVATE_STAGE")
    return None if binding is None else binding.sha256


def _prepared_manifest_sha256(finalization: object) -> str | None:
    """Return the actual prepared-manifest file digest, never its inner seal."""

    path = getattr(finalization, "prepared_manifest", None)
    if not isinstance(path, Path):
        return None
    try:
        return regular_binding(path, label="PREPARED_MANIFEST").sha256
    except ReviewedBaselineReplayError:
        return None


def _provider_receipt_sha256s(root: Path) -> tuple[str, ...]:
    """Expose only canonical hashes of self-sealed provider receipts, never paths."""

    values: set[str] = set()
    for directory, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            if not name.endswith(".json") or "receipt" not in name:
                continue
            path = Path(directory) / name
            try:
                binding = regular_binding(path, label="PRIVATE_RECEIPT")
                if binding is None or binding.size > 4 * 1024 * 1024:
                    continue
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    data = os.read(fd, binding.size + 1)
                finally:
                    os.close(fd)
                document = json.loads(data.decode("utf-8"))
                if isinstance(document, dict) and isinstance(document.get("receipt_sha256"), str):
                    values.add(binding.sha256)
            except (OSError, UnicodeDecodeError, ValueError, ReviewedBaselineReplayError):
                continue
    return tuple(sorted(values))


def _prepare(plan, *, runtime: Path, stage_parent: Path, state_path: Path | None = None):
    """Build one complete no-target-write after-image outside the commit lease."""
    expected_stage = stage_parent / (
        f"{plan.date}-{plan.candidate_id}-"
        f"{_sha(_canonical({'date': plan.date, 'candidate_id': plan.candidate_id, 'record': regular_binding(plan.record_path, label='RECORD').sha256, 'baseline': 'sha256:' + plan.baseline.config['sha256']})).removeprefix('sha256:')[:16]}"
    )
    try:
        staged = stage_replay(plan, stage_parent=stage_parent)
    except (Exception, SystemExit) as exc:
        if expected_stage.exists() and not expected_stage.is_symlink():
            _cleanup_private_stage(expected_stage, parent=stage_parent)
        raise _PrepareFailure(reason_code=_safe_reason_code(exc), provider_attempted=False) from None
    stage = Path(str(staged.get("stage") or ""))
    if not stage.is_absolute() or stage.parent != stage_parent:
        if stage.is_absolute() and stage.parent == stage_parent and stage.exists() and not stage.is_symlink():
            _cleanup_private_stage(stage, parent=stage_parent)
        raise _PrepareFailure(reason_code="REPLAY_PRIVATE_STAGE_RETURN_INVALID", provider_attempted=False) from None
    # The synth entrypoint fail-closes unless the canonical entity verifier is
    # explicitly supplied.  This CLI does not invent one: a production caller
    # must use the dedicated exact-final builder once its fresh private spec is
    # available.  It is therefore impossible to misrepresent this as READY.
    try:
        finalization = synthesize_replay_spec_and_finalize_private(
            plan, stage=stage, runtime_authority_root=runtime,
            speaker_python=Path(sys.executable),
            source_fact_llm=_production_llm_call(runtime_root=runtime, effort="high"),
            adapters=_adapters(), use_production_exact_final_reviewer=True,
            exact_final_text_adapters=_text_adapters(),
        )
    except (Exception, SystemExit) as exc:
        stage_sha = _stage_manifest_sha256(stage)
        provider_hashes = _provider_receipt_sha256s(stage)
        _cleanup_private_stage(stage, parent=stage_parent)
        raise _PrepareFailure(reason_code=_safe_reason_code(exc), provider_attempted=True, stage_manifest_sha256=stage_sha,
                              provider_receipt_sha256s=provider_hashes) from None
    stage_sha = _stage_manifest_sha256(stage)
    provider_hashes = _provider_receipt_sha256s(finalization.private_runtime_root)
    try:
        if state_path is None:
            raise ReviewedBaselineReplayError("REPLAY_STATE_PATH_REQUIRED")
        after = prepare_replay_after_image(
            plan, runtime_root=runtime, state_path=state_path, finalization=finalization,
        )
    except (Exception, SystemExit) as exc:
        prepared_sha = _prepared_manifest_sha256(finalization)
        _cleanup_private_stage(stage, parent=stage_parent)
        raise _PrepareFailure(reason_code=_safe_reason_code(exc), provider_attempted=True, stage_manifest_sha256=stage_sha,
                              prepared_manifest_sha256=prepared_sha,
                              provider_receipt_sha256s=provider_hashes) from None
    return stage, finalization, after, stage_sha, provider_hashes


def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    try:
        runtime = _safe_directory(args.runtime_root)
        if args.readiness_graph:
            print(json.dumps(_readiness_graph(runtime=runtime, date=args.date,
                                               candidate_ids=args.candidate_id), ensure_ascii=False, sort_keys=True))
            return 0
        plans = [build_replay_plan(repo_root=runtime / "repo", out_root=runtime / "out",
                                   date=args.date, candidate_id=cid)
                 for cid in args.candidate_id]
        if args.plan or not (args.full_dry_run or args.apply):
            print(json.dumps({"schema_version": "reviewed-baseline-replay-plan.v2", "mode": "PLAN",
                              "upload_allowed": False,
                              "candidates": [{"candidate_id": p.candidate_id,
                                              "predicate_matrix": list(p.matrix)} for p in plans]},
                             ensure_ascii=False, sort_keys=True))
            return 0
        _runtime_gate(runtime)
        stage_parent = _private_parent(args.private_stage_parent)
        prepared, errors = {}, {}
        state_path = runtime / "state" / f"{args.date}.json"
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
            work = {
                pool.submit(_prepare, plan, runtime=runtime, stage_parent=stage_parent,
                            state_path=state_path): plan
                for plan in plans
            }
            for future in concurrent.futures.as_completed(work):
                plan = work[future]
                try:
                    prepared[plan.candidate_id] = future.result()
                except (Exception, SystemExit) as exc:  # Never surface provider/path text.
                    errors[plan.candidate_id] = (
                        _safe_reason_code(exc), bool(getattr(exc, "provider_attempted", True)),
                        getattr(exc, "stage_manifest_sha256", None),
                        getattr(exc, "prepared_manifest_sha256", None),
                        tuple(getattr(exc, "provider_receipt_sha256s", ())),
                    )
        result = {"schema_version": "reviewed-baseline-replay-run.v1",
                  "mode": "APPLY" if args.apply else "FULL_DRY_RUN",
                  "upload_allowed": False, "candidates": []}
        blocked = False
        for plan in plans:
            error = errors.get(plan.candidate_id)
            if error:
                blocked = True
                reason_code, provider_attempted, stage_sha, prepared_sha, provider_hashes = error
                matrix = _matrix(plan, status="NOT_EVALUATED",
                                 failures={"PRIVATE_FINALIZATION": reason_code})
                receipt = _sanitized_failure_receipt(
                    runtime=runtime, plan=plan, matrix=matrix, provider_attempted=provider_attempted,
                    stage_manifest_sha256=stage_sha, prepared_manifest_sha256=prepared_sha,
                    provider_receipt_sha256s=provider_hashes,
                )
                result["candidates"].append({"candidate_id": plan.candidate_id,
                                             "status": "BLOCKED", "predicate_matrix": matrix,
                                             "diagnostic_receipt_sha256": receipt})
                continue
            stage, finalization, prepared_after, stage_sha, provider_hashes = prepared[plan.candidate_id]
            after = prepared_after.after
            committed_journal = None
            commit_attempted = False
            try:
                if args.apply:
                    # Every future prepared against the original state bytes.
                    # Commits are intentionally serial, so refresh only this
                    # candidate's state pick against the latest durable state;
                    # its candidate-private artifact bindings stay untouched.
                    after = rebind_replay_after_image_state(
                        plan, runtime_root=runtime, state_path=state_path,
                        finalization=finalization, after=after,
                        projection=prepared_after.projection,
                    )
                    # A failure above this boundary has no formal journal, so
                    # its transaction-owned private media must be removed.
                    # Once commit is attempted, recovery owns any PREPARED or
                    # INSTALLING journal and this path must not touch it.
                    commit_attempted = True
                    journal = commit_prepared_after_image(runtime_root=runtime, after=after)
                    committed_journal = journal
                    cleanup_prepared_after_image_stage(
                        runtime_root=runtime, after=after, committed_journal=journal,
                    )
                    item = {"candidate_id": plan.candidate_id, "status": "COMMITTED",
                            "journal_sha256": stream_binding(journal, label="JOURNAL").sha256,
                            "upload_allowed": False}
                else:
                    cleanup_prepared_after_image_stage(runtime_root=runtime, after=after)
                    item = {"candidate_id": plan.candidate_id, "status": "READY_TO_COMMIT",
                            "predicate_matrix": _matrix(plan, status="PASS"),
                            "upload_allowed": False}
            except (Exception, SystemExit) as exc:
                blocked = True
                if committed_journal is not None:
                    cleanup_matrix = [
                        *_matrix(plan, status="PASS"),
                        {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"},
                    ]
                    item = {"candidate_id": plan.candidate_id,
                            "status": "COMMITTED_CLEANUP_UNCONFIRMED",
                            "journal_sha256": stream_binding(committed_journal, label="JOURNAL").sha256,
                            "predicate_matrix": cleanup_matrix,
                            "upload_allowed": False}
                else:
                    matrix = _matrix(plan, status="NOT_EVALUATED",
                                     failures={"AFTER_IMAGE": type(exc).__name__})
                    if args.apply and not commit_attempted:
                        try:
                            cleanup_prepared_after_image_stage(runtime_root=runtime, after=after)
                        except (Exception, SystemExit):
                            matrix.append({"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"})
                    item = {"candidate_id": plan.candidate_id, "status": "BLOCKED", "predicate_matrix": matrix,
                            "diagnostic_receipt_sha256": _sanitized_failure_receipt(
                                runtime=runtime, plan=plan, matrix=matrix, provider_attempted=True,
                                stage_manifest_sha256=stage_sha,
                                prepared_manifest_sha256=_prepared_manifest_sha256(finalization),
                                provider_receipt_sha256s=provider_hashes)}
            try:
                _cleanup_private_stage(stage, parent=stage_parent)
            except (Exception, SystemExit):
                blocked = True
                if committed_journal is not None:
                    cleanup_matrix = [
                        *_matrix(plan, status="PASS"),
                        {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"},
                    ]
                    item = {"candidate_id": plan.candidate_id,
                            "status": "COMMITTED_CLEANUP_UNCONFIRMED",
                            "journal_sha256": stream_binding(committed_journal, label="JOURNAL").sha256,
                            "predicate_matrix": cleanup_matrix,
                            "upload_allowed": False}
                else:
                    item = {"candidate_id": plan.candidate_id, "status": "BLOCKED",
                            "predicate_matrix": [*_matrix(plan, status="NOT_EVALUATED"), {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"}],
                            "diagnostic_receipt_sha256": _sanitized_failure_receipt(
                                runtime=runtime, plan=plan,
                                matrix=[*_matrix(plan, status="NOT_EVALUATED"), {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"}],
                                provider_attempted=True, stage_manifest_sha256=stage_sha,
                                prepared_manifest_sha256=_prepared_manifest_sha256(finalization),
                                provider_receipt_sha256s=provider_hashes)}
            result["candidates"].append(item)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if blocked else 0
    except ReviewedBaselineReplayError as exc:
        print(json.dumps({"status": "REFUSED", "reason_code": str(exc), "upload_allowed": False},
                         ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
