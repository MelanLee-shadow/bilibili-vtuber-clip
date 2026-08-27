#!/usr/bin/env python3
"""Canonical no-upload replay of one or more reviewed subtitle baselines.

``plan`` reads authority only. ``full-dry-run`` validates private after-images;
normal ``apply`` commits candidate-rejected replay state-last. With explicit
published same-BV authority, the same modes instead build a create-only private
package with ``state_transition=none``; they never publish it.
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
import traceback
from pathlib import Path
from typing import Callable, Mapping

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
from src.autoslice.branding_intro import BrandingIntroError
from src.autoslice.llm_client import LLM_JSON_PARSE_REASON_CODES, LLM_TRANSPORT_REASON_CODES
from src.autoslice.reviewed_baseline_replay import (
    ReviewedBaselineReplayError, _canonical, _production_llm_call, _safe_directory, _sha,
    build_replay_plan, prepare_replay_after_image, rebind_replay_after_image_state, stage_replay,
    synthesize_replay_spec_and_finalize_private, regular_binding,
)
from src.autoslice.c6_exact_final_boundary_adapter import (
    CANDIDATE_ID as C6_EXACT_CANDIDATE_ID,
    RECORDING_DATE as C6_EXACT_RECORDING_DATE,
    build_c6_exact_final_reviewer,
)
from src.autoslice.published_recovery_package import (
    prepare_published_recovery_package, published_recovery_preflight,
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
_SAFE_EXCEPTION_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SAFE_EXCEPTION_TYPES = frozenset({
    "AssertionError", "AttributeError", "KeyError", "OSError", "RuntimeError", "SystemExit", "TypeError", "ValueError",
})
_MAX_REVIEW_FLAGS_BYTES = 512 * 1024
_MAX_REDELIVERY_BASELINE_FAILURE_ROWS = 32
_MAX_REDELIVERY_CUE_INDEX = 1_000_000
_MAX_REDELIVERY_TIME_MS = 1_000_000_000_000
_SPEAKER_RUNTIME_RELATIVE = Path("venv-diar/bin/python")
_SAFE_PROVIDER_CLASSES = frozenset({"quota", "service", "rejected", "unknown"})

# This private C2-only hook is deliberately narrower than the ordinary
# sanitized exception diagnostic.  Its caller is injected by the locked C2
# wrapper, never by argparse, and it can report only one of these static path
# roles.  It must not turn a path, a binding label, or a provider detail into a
# receipt-visible reason code.
_C2_PRIVATE_PATH_UNAVAILABLE_PREFIX = "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_"
_C2_PRIVATE_PATH_UNAVAILABLE_ROLES = frozenset({
    "SYNTHESIS_STAGE",
    "PRIVATE_RUNTIME_PARENT",
    "PRIVATE_ARTIFACT_PARENT",
    "DEPLOYED_AUTHORITY_RUNTIME",
    "EXACT_FINAL_RUNTIME",
    "PROVIDER_RUNTIME",
    "PADDED_PROVENANCE_PARENT",
    "PRIVATE_COVER_PACKAGE_ROOT",
    "DEPLOYED_ASSETS_RUNTIME",
    "C2_AUTHORITY_PRIVATE_RUNTIME",
    "REGULAR_STAGE_DOCUMENT_PARENT",
    "REGULAR_RECORD_PARENT",
    "REGULAR_PROVENANCE_PARENT",
    "REGULAR_PADDED_SOURCE_PARENT",
    "REGULAR_PADDED_PROVENANCE_PARENT",
    "REGULAR_RELEASE_TRUTH_PARENT",
    "REGULAR_CHAT_AUTHORITY_PARENT",
    "REGULAR_CLIP_CONTEXT_PARENT",
    "REGULAR_PORTABLE_RECORD_PARENT",
    "REGULAR_PORTABLE_CHAT_AUTHORITY_PARENT",
    "REGULAR_PORTABLE_CLIP_CONTEXT_PARENT",
    "REGULAR_PORTABLE_PUBLISH_PARENT",
    "REGULAR_DEPLOYED_MANIFEST_PARENT",
    "REGULAR_DELIVERY_PROJECTION_PARENT",
    "REGULAR_OLD_SPEAKER_MANIFEST_PARENT",
    "REGULAR_OPERATOR_DECISION_LEDGER_PARENT",
    "REGULAR_PIPELINE_DIAGNOSTIC_PARENT",
    "REGULAR_OPERATOR_TRUTH_DIFF_PARENT",
    "REGULAR_COVER_PARENT",
    "REGULAR_COVER_CARRY_PARENT",
    "REGULAR_PREPARED_HANDLE_PARENT",
    "REGULAR_BINDING_UNCLASSIFIED",
    "CALL_LOCUS_UNCLASSIFIED",
    "CLASSIFIER_INVALID",
})
_C2_PRIVATE_PATH_UNAVAILABLE_REASON_CODES = frozenset(
    _C2_PRIVATE_PATH_UNAVAILABLE_PREFIX + role
    for role in _C2_PRIVATE_PATH_UNAVAILABLE_ROLES
)

_SAFE_REDELIVERY_FAILURE_SCALAR_FIELDS = (
    "baseline_cue_index",
    "current_cue_index",
    "start_ms",
    "end_ms",
    "absolute_source_start_ms",
    "absolute_source_end_ms",
)
_SAFE_REDELIVERY_FAILURE_INDEX_ARRAY_FIELDS = (
    "baseline_cue_indexes",
    "current_cue_indexes",
)
_SAFE_REDELIVERY_FAILURE_ROW_FIELDS = frozenset({
    "reason_code",
    *_SAFE_REDELIVERY_FAILURE_SCALAR_FIELDS,
    *_SAFE_REDELIVERY_FAILURE_INDEX_ARRAY_FIELDS,
})
_SAFE_REDELIVERY_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SAFE_REDELIVERY_CANDIDATE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}\Z")

# Closed replay-adapter reason codes.  Keep this exact rather than accepting
# arbitrary suffixes: provider/path/prompt text must never become a predicate.
_REPLAY_TITLE_SURFACE_REASONS = {
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW": "SOURCE_FACT_REVIEW",
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_MISSING": "SOURCE_FACT_REVIEW",
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW": "SOURCE_FACT_REVIEW",
    **{
        f"REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_SOURCE_FACT_REVIEW_{code}": "SOURCE_FACT_REVIEW"
        for code in (
            "CPA_TEXT_REVIEW_INVALID",
            "CPA_TEXT_REVIEW_UNAVAILABLE",
            "CPA_TEXT_REVIEW_CALL_FAILED",
            "CPA_ENTITY_SURFACE_RESPONSE_INVALID",
            "CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED",
            "CPA_SOURCE_FACT_REPAIR_CYCLE",
            "CPA_SOURCE_FACT_REPAIR_EXHAUSTED",
            "SOURCE_FACT_DETERMINISTIC_TEXT_NARROWING",
            "SOURCE_FACT_ENTITY_CONTEXT_INVALID",
            "SOURCE_FACT_INPUT_ENTITY_SURFACE_INVALID",
            "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE",
            "SOURCE_FACT_SPEAKER_EVIDENCE_INVALID",
            "SOURCE_FACT_SUPPORTED_COMPRESSION_HEDGE_KEPT",
            "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED",
            "SOURCE_FACT_FINAL_REVIEWED_SRT_REQUIRED",
            "SOURCE_FACT_FINAL_REVIEWED_SRT_UNAVAILABLE",
        )
    },
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_STAGED_TITLE_MISMATCH": "FROZEN_TITLE_AUTHORITY",
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_STORY_RESOLVED_HOOK_MISMATCH": "FROZEN_TITLE_AUTHORITY",
    "REPLAY_FROZEN_TITLE_AUTHORITY_DRIFT_TITLE_AUTHORITY_ERROR": "FROZEN_TITLE_AUTHORITY",
}
# A finalizer's SystemExit routinely appends a candidate-private path or an
# adapter's diagnostic text.  Only these known *outer* families may cross the
# replay diagnostic boundary, and only as their all-caps prefix.
_SAFE_FINALIZER_EXIT_PREFIXES = (
    "FINAL_REVIEW_RELEASE_BLOCKED",
    "FINAL_DELIVERY_BOUNDARY_REVIEW_MISSING",
    "FINAL_REVIEW_EXACT_FINALIZER_MISSING",
    "FINAL_SUBTITLE_BURN_",
    "SPEAKER_",
    "CHAT_AUTHORITY_",
    "SOURCE_FACT_",
    "STORY_CONTRACT_",
    "TITLE_AUTHORITY_",
    "REDELIVERY_SUBTITLE_BASELINE_FAILED",
)


class _PrepareFailure(RuntimeError):
    def __init__(self, *, reason_code: str, provider_attempted: bool, stage_manifest_sha256: str | None = None,
                 prepared_manifest_sha256: str | None = None,
                 provider_receipt_sha256s: tuple[str, ...] = (),
                 predicate_failures: tuple[tuple[str, str], ...] = (),
                 provider_failure_summary: dict[str, object] | None = None,
                 exception_diagnostic: Mapping[str, object] | None = None,
                 redelivery_baseline_failure_summary: Mapping[str, object] | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.provider_attempted = provider_attempted
        self.stage_manifest_sha256 = stage_manifest_sha256
        self.prepared_manifest_sha256 = prepared_manifest_sha256
        self.provider_receipt_sha256s = provider_receipt_sha256s
        self.predicate_failures = predicate_failures
        self.provider_failure_summary = provider_failure_summary
        self.exception_diagnostic = _closed_exception_diagnostic(exception_diagnostic)
        self.redelivery_baseline_failure_summary = (
            _closed_redelivery_baseline_failure_summary(
                redelivery_baseline_failure_summary
            )
        )


def _safe_reason_code(exc: BaseException) -> str:
    """Expose only a stable typed reason, never provider or filesystem text."""

    if isinstance(exc, SystemExit):
        # Do not derive from ``str(exc)``: it may contain a private path,
        # prompt, provider stderr, or a numeric process exit status.
        raw = exc.code
        if isinstance(raw, str):
            prefix = raw.split(":", 1)[0]
            if (
                _SAFE_REASON_CODE.fullmatch(prefix)
                and any(prefix.startswith(allowed) for allowed in _SAFE_FINALIZER_EXIT_PREFIXES)
            ):
                return prefix
        return "REPLAY_PREPARE_SYSTEM_EXIT"
    if isinstance(exc, BrandingIntroError):
        return "REPLAY_BRANDING_AUTHORITY_BLOCKED"
    value = getattr(exc, "reason_code", str(exc))
    if isinstance(value, str) and _SAFE_REASON_CODE.fullmatch(value):
        return value
    return "REPLAY_PREPARE_EXCEPTION"


def _closed_exception_diagnostic(value: Mapping[str, object] | None) -> dict[str, object] | None:
    """Accept only a bounded, repository-local generic exception locator."""

    if not isinstance(value, Mapping) or set(value) - {"exception_type", "exception_locus"}:
        return None
    token = value.get("exception_type")
    if not isinstance(token, str) or token not in _SAFE_EXCEPTION_TYPES or not _SAFE_EXCEPTION_TYPE.fullmatch(token):
        return None
    result: dict[str, object] = {"exception_type": token}
    locus = value.get("exception_locus")
    if locus is None:
        return result
    if not isinstance(locus, Mapping) or set(locus) != {"module", "function", "line"}:
        return None
    module, function, line = locus.get("module"), locus.get("function"), locus.get("line")
    if (
        not isinstance(module, str) or not 1 <= len(module) <= 240
        or not all(part.isidentifier() for part in module.split("."))
        or not isinstance(function, str) or not 1 <= len(function) <= 128 or not function.isidentifier()
        or isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 10_000_000
    ):
        return None
    result["exception_locus"] = {"module": module, "function": function, "line": line}
    return result


def _repository_exception_locus(exc: BaseException) -> dict[str, object] | None:
    """Return only the innermost repository-local Python traceback locus."""

    repository = ROOT.resolve()
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        try:
            relative = Path(frame.filename).resolve().relative_to(repository)
        except (OSError, ValueError):
            continue
        if relative.suffix != ".py" or len(relative.parts) > 16:
            continue
        module_parts = (*relative.with_suffix("").parts,)
        if not module_parts or not all(part.isidentifier() for part in module_parts):
            continue
        if not isinstance(frame.name, str) or not frame.name.isidentifier() or not 1 <= frame.lineno <= 10_000_000:
            continue
        return {"module": ".".join(module_parts), "function": frame.name, "line": frame.lineno}
    return None


def _generic_exception_diagnostic(exc: BaseException) -> dict[str, object] | None:
    """Return the innermost safe in-repository locus for an untyped failure."""

    if (
        not isinstance(exc, Exception)
        or isinstance(exc, ReviewedBaselineReplayError)
        or _safe_reason_code(exc) != "REPLAY_PREPARE_EXCEPTION"
        or type(exc).__name__ not in _SAFE_EXCEPTION_TYPES
    ):
        return None
    diagnostic: dict[str, object] = {"exception_type": type(exc).__name__}
    locus = _repository_exception_locus(exc)
    if locus is not None:
        diagnostic["exception_locus"] = locus
    return _closed_exception_diagnostic(diagnostic)


def _system_exit_diagnostic(exc: BaseException) -> dict[str, object] | None:
    """Expose a SystemExit locus without reading its code or message."""

    if not isinstance(exc, SystemExit) or _safe_reason_code(exc) != "REPLAY_PREPARE_SYSTEM_EXIT":
        return None
    diagnostic: dict[str, object] = {"exception_type": "SystemExit"}
    locus = _repository_exception_locus(exc)
    if locus is not None:
        diagnostic["exception_locus"] = locus
    return _closed_exception_diagnostic(diagnostic)


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
    parser.add_argument("--published-recovery-bvid")
    parser.add_argument("--recovery-package-root", type=Path)
    parser.add_argument("--speaker-python", type=Path,
                        help="explicit interpreter for an isolated no-upload preflight")
    args = parser.parse_args(argv)
    if not args.readiness_graph and not args.candidate_id:
        parser.error("--candidate-id is required outside --readiness-graph")
    if len(args.candidate_id) != len(set(args.candidate_id)):
        parser.error("candidate ids must be unique")
    if len(args.candidate_id) > 5:
        parser.error("at most five candidates may be prepared together")
    if (args.full_dry_run or args.apply) and args.private_stage_parent is None:
        parser.error("full-dry-run/apply require --private-stage-parent")
    if args.published_recovery_bvid:
        if args.readiness_graph or len(args.candidate_id) != 1:
            parser.error("published recovery requires exactly one candidate outside readiness-graph")
        if args.apply and args.recovery_package_root is None:
            parser.error("published recovery apply requires --recovery-package-root")
        if not args.apply and args.recovery_package_root is not None:
            parser.error("--recovery-package-root is accepted only by published recovery apply")
    elif args.recovery_package_root is not None:
        parser.error("--recovery-package-root requires --published-recovery-bvid")
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


def _speaker_python_binding(requested: Path) -> dict[str, object]:
    """Seal the complete identity of one external venv launcher."""

    requested = Path(requested).absolute()
    try:
        requested_lstat = os.lstat(requested)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_UNAVAILABLE") from exc
    if stat.S_ISLNK(requested_lstat.st_mode):
        kind = "symlink"
        try:
            link_target: str | None = os.readlink(requested)
        except OSError as exc:
            raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_UNAVAILABLE") from exc
    elif stat.S_ISREG(requested_lstat.st_mode):
        kind = "regular"
        link_target = None
    else:
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_UNSAFE")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_UNAVAILABLE") from exc
    executable = regular_binding(resolved, label="SPEAKER_PYTHON")
    if not os.access(executable.path, os.X_OK):
        raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_UNSAFE")
    pyvenv = requested.parent.parent / "pyvenv.cfg"
    if pyvenv.exists() or pyvenv.is_symlink():
        pyvenv_binding = regular_binding(pyvenv, label="SPEAKER_PYVENV")
        pyvenv_document: dict[str, object] | None = {
            "path": str(pyvenv), "sha256": pyvenv_binding.sha256,
        }
    else:
        pyvenv_document = None
    return {
        "schema_version": "reviewed-baseline-speaker-python-binding.v1",
        "requested_path": str(requested),
        "requested_lstat": {
            "kind": kind,
            "mode": stat.S_IMODE(requested_lstat.st_mode),
            "readlink": link_target,
        },
        "resolved": {
            "path": str(executable.path), "sha256": executable.sha256,
            "mode": executable.mode,
        },
        "pyvenv_cfg": pyvenv_document,
    }


def _read_bound_json(path: Path, *, label: str) -> dict[str, object]:
    """Read a small stage control document only through a stable binding."""

    binding = regular_binding(path, label=label)
    if binding.size > 64 * 1024:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_TOO_LARGE")
    try:
        payload = binding.path.read_bytes()
    except OSError as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_UNAVAILABLE") from exc
    if hashlib.sha256(payload).hexdigest() != binding.sha256.removeprefix("sha256:"):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    if regular_binding(path, label=label) != binding:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_DRIFT")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise ReviewedBaselineReplayError(f"REPLAY_{label}_INVALID")
    return value


def _stage_speaker_python_revalidator(stage: Path, expected: Mapping[str, object]):
    """Return the finalizer-adjacent checker for a sealed private launcher."""

    expected_document = dict(expected)
    binding_path = stage / "speaker-python-binding.json"

    def revalidate() -> None:
        # This callback runs immediately before finalizer invocation.  Re-read
        # both create-only stage seals here, not merely when constructing the
        # callback, so a later stage-receipt swap cannot race the launcher
        # check.
        stage_document = _read_bound_json(stage / "stage.json", label="PRIVATE_STAGE")
        descriptor = stage_document.get("speaker_python_binding")
        binding = regular_binding(binding_path, label="SPEAKER_PYTHON_BINDING")
        if (
            not isinstance(descriptor, Mapping)
            or set(descriptor) != {"path", "sha256"}
            or descriptor.get("path") != binding_path.name
            or descriptor.get("sha256") != binding.sha256
            or _read_bound_json(binding_path, label="SPEAKER_PYTHON_BINDING") != expected_document
        ):
            raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_STAGE_BINDING_DRIFT")
        if _speaker_python_binding(Path(str(expected_document["requested_path"]))) != expected_document:
            raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_BINDING_DRIFT")

    return revalidate


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
    "RECORD_BOUND_CHAT_AUTHORITY", "RECORD_BOUND_CLIP_CONTEXT",
    "SOURCE_FACT_REVIEW", "EXACT_DELIVERY_BOUNDARY_REVIEW", "EXACT_FINAL_RELEASE_REVIEW",
    "RECORD_PUBLISH_CHAT_MIRRORS", "FROZEN_TITLE_AUTHORITY",
    "COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH", "PACKAGE_AUDIT", "STATE_AFTER_IMAGE_DIFF",
)


def _failure_predicate(reason_code: str) -> str:
    if reason_code.startswith("REDELIVERY_SUBTITLE_BASELINE_FAILED"):
        return "REDELIVERY_BASELINE_REPLAY"
    exact_replay_surface = _REPLAY_TITLE_SURFACE_REASONS.get(reason_code)
    if exact_replay_surface is not None:
        return exact_replay_surface
    if reason_code.startswith("REPLAY_FINALIZER_CHAT_AUTHORITY"):
        return "RECORD_BOUND_CHAT_AUTHORITY"
    if reason_code.startswith("REPLAY_FINALIZER_CLIP_CONTEXT"):
        return "RECORD_BOUND_CLIP_CONTEXT"
    if reason_code.startswith(("FINAL_REVIEW_", "FINAL_DELIVERY_BOUNDARY_")):
        return (
            "EXACT_DELIVERY_BOUNDARY_REVIEW"
            if reason_code.startswith("FINAL_DELIVERY_BOUNDARY_")
            else "EXACT_FINAL_RELEASE_REVIEW"
        )
    if reason_code.startswith(("SOURCE_FACT_", "STORY_CONTRACT_SOURCE_FACT_")):
        return "SOURCE_FACT_REVIEW"
    if reason_code.startswith("STORY_CONTRACT_COVER_"):
        return "COVER_ROUTE_PIXEL_HOST_PARTICIPANT_PUNCH"
    if reason_code.startswith(("TITLE_AUTHORITY_", "STORY_CONTRACT_TITLE_")):
        return "FROZEN_TITLE_AUTHORITY"
    if reason_code.startswith(("FINAL_SUBTITLE_BURN_", "SPEAKER_")):
        return "SPEAKER_ASS_BURN_REBUILD"
    if reason_code.startswith("CHAT_AUTHORITY_"):
        return "RECORD_BOUND_CHAT_AUTHORITY"
    return "PRIVATE_FINALIZATION"


def _read_bound_small_json(path: Path, *, label: str) -> dict[str, object] | None:
    """Read one bounded diagnostic control file through a stable no-follow fd."""

    try:
        binding = regular_binding(path, label=label)
    except ReviewedBaselineReplayError:
        return None
    if binding.size > _MAX_REVIEW_FLAGS_BYTES:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode), opened.st_size,
            opened.st_mtime_ns, opened.st_ctime_ns,
        ) != (
            binding.device, binding.inode, binding.mode, binding.size,
            binding.mtime_ns, binding.ctime_ns,
        ):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        if regular_binding(path, label=label) != binding:
            return None
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (ReviewedBaselineReplayError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_reason_codes(value: object) -> tuple[str, ...]:
    """Return only typed all-caps reason tokens from an exact list."""

    if not isinstance(value, list):
        return ()
    return tuple(sorted({row for row in value if isinstance(row, str) and _SAFE_REASON_CODE.fullmatch(row)}))


def _safe_redelivery_failure_number(value: object, *, index: bool) -> int | None:
    """Accept only bounded numeric cue coordinates from a failed audit row."""

    if type(value) is not int:
        return None
    if index:
        return value if 0 <= value <= _MAX_REDELIVERY_CUE_INDEX else None
    return value if -_MAX_REDELIVERY_TIME_MS <= value <= _MAX_REDELIVERY_TIME_MS else None


def _sanitize_redelivery_baseline_failure_row(value: object) -> dict[str, object] | None:
    """Project one finalizer audit failure onto its path/text-free public shape."""

    if not isinstance(value, Mapping):
        return None
    reason_code = value.get("reason_code")
    if (
        not isinstance(reason_code, str)
        or not _SAFE_REASON_CODE.fullmatch(reason_code)
        or not reason_code.startswith("REDELIVERY_")
    ):
        return None
    row: dict[str, object] = {"reason_code": reason_code}
    for field in _SAFE_REDELIVERY_FAILURE_SCALAR_FIELDS:
        number = _safe_redelivery_failure_number(
            value.get(field), index=field.endswith("cue_index"),
        )
        if number is not None:
            row[field] = number
    for field in _SAFE_REDELIVERY_FAILURE_INDEX_ARRAY_FIELDS:
        indexes = value.get(field)
        if (
            not isinstance(indexes, list)
            or len(indexes) > _MAX_REDELIVERY_BASELINE_FAILURE_ROWS
        ):
            continue
        sanitized_indexes = [
            number
            for item in indexes
            if (number := _safe_redelivery_failure_number(item, index=True)) is not None
        ]
        if len(sanitized_indexes) == len(indexes):
            row[field] = sorted(set(sanitized_indexes))
    return row


def _closed_redelivery_baseline_failure_summary(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Revalidate the complete closed shape before it reaches an output surface."""

    if (
        not isinstance(value, Mapping)
        or set(value) != {"reason_codes", "failure_count", "failure_rows"}
    ):
        return None
    reason_codes = value.get("reason_codes")
    failure_count = value.get("failure_count")
    failure_rows = value.get("failure_rows")
    if (
        not isinstance(reason_codes, list)
        or not isinstance(failure_rows, list)
        or type(failure_count) is not int
        or not 0 <= failure_count <= _MAX_REDELIVERY_BASELINE_FAILURE_ROWS
        or failure_count != len(failure_rows)
        or len(failure_rows) > _MAX_REDELIVERY_BASELINE_FAILURE_ROWS
        or any(
            not isinstance(code, str)
            or not _SAFE_REASON_CODE.fullmatch(code)
            or not code.startswith("REDELIVERY_")
            for code in reason_codes
        )
    ):
        return None
    rows: list[dict[str, object]] = []
    for raw_row in failure_rows:
        if (
            not isinstance(raw_row, Mapping)
            or set(raw_row) - _SAFE_REDELIVERY_FAILURE_ROW_FIELDS
            or "reason_code" not in raw_row
        ):
            return None
        row = _sanitize_redelivery_baseline_failure_row(raw_row)
        if row is None or row != dict(raw_row):
            return None
        rows.append(row)
    expected_codes = sorted({str(row["reason_code"]) for row in rows})
    if reason_codes != expected_codes:
        return None
    return {
        "reason_codes": expected_codes,
        "failure_count": len(rows),
        "failure_rows": rows,
    }


def _redelivery_baseline_failure_summary(*, stage: Path, plan) -> dict[str, object] | None:
    """Read only the private finalizer's failed redelivery audit before cleanup."""

    date = getattr(plan, "date", None)
    candidate_id = getattr(plan, "candidate_id", None)
    if (
        not isinstance(date, str)
        or not _SAFE_REDELIVERY_DATE.fullmatch(date)
        or not isinstance(candidate_id, str)
        or not _SAFE_REDELIVERY_CANDIDATE_ID.fullmatch(candidate_id)
    ):
        return None
    path = (
        Path(stage) / "finalizer-runtime" / date / candidate_id / "replacement_recuts"
        / f"{candidate_id}.redelivery-baseline.json"
    )
    document = _read_bound_small_json(path, label="REDELIVERY_BASELINE_AUDIT")
    if (
        not isinstance(document, dict)
        or document.get("schema_version")
        not in {
            "subtitle-redelivery-baseline-audit.v1",
            "subtitle-redelivery-baseline-audit.v2",
        }
        or document.get("status") != "FAILED"
        or not isinstance(document.get("failures"), list)
    ):
        return None
    rows: list[dict[str, object]] = []
    for failure in document["failures"]:
        row = _sanitize_redelivery_baseline_failure_row(failure)
        if row is not None:
            rows.append(row)
        if len(rows) >= _MAX_REDELIVERY_BASELINE_FAILURE_ROWS:
            break
    return _closed_redelivery_baseline_failure_summary({
        "reason_codes": sorted({str(row["reason_code"]) for row in rows}),
        "failure_count": len(rows),
        "failure_rows": rows,
    })


def _provider_failure_summary(discovery: object) -> dict[str, object] | None:
    """Return only the closed, non-text provider failure classification."""

    if not isinstance(discovery, dict):
        return None
    summary: dict[str, object] = {}
    parse_code = discovery.get("provider_error_code")
    if isinstance(parse_code, str) and parse_code in (LLM_JSON_PARSE_REASON_CODES | LLM_TRANSPORT_REASON_CODES):
        summary["provider_error_code"] = parse_code
    provider_class = discovery.get("provider_class")
    codes = discovery.get("provider_status_codes")
    if isinstance(provider_class, str) and provider_class in _SAFE_PROVIDER_CLASSES and isinstance(codes, list):
        if all(type(code) is int and 100 <= code <= 599 for code in codes):
            summary["provider_class"] = provider_class
            summary["provider_status_codes"] = sorted(set(codes))
    return summary or None


def _review_flags_diagnostics(*, stage: Path, plan) -> tuple[tuple[tuple[str, str], ...], dict[str, object] | None]:
    """Extract a tiny, path-free failure summary before private-stage cleanup.

    Review flags are optional diagnostics, never authority.  A malformed,
    over-sized, unsafe, or wrong-schema file is deliberately ignored so it
    cannot manufacture a more specific outcome than the finalizer itself.
    """

    path = (
        Path(stage) / "finalizer-runtime" / str(plan.date) / str(plan.candidate_id)
        / f"{plan.candidate_id}.review-flags.json"
    )
    document = _read_bound_small_json(path, label="REVIEW_FLAGS")
    if not isinstance(document, dict):
        return (), None
    if (
        document.get("schema_version") != "final-review-audit.v2"
        or not isinstance(document.get("status"), str)
        or not _SAFE_REASON_CODE.fullmatch(str(document["status"]))
        or document.get("release_gate") != "BLOCK"
    ):
        return (), None
    failures: dict[str, str] = {}
    boundary = document.get("boundary_semantic_review")
    if (
        isinstance(boundary, dict)
        and boundary.get("schema_version") == "talk-boundary-semantic-review.v1"
        and isinstance(boundary.get("status"), str)
        and _SAFE_REASON_CODE.fullmatch(str(boundary["status"]))
    ):
        boundary_codes = _safe_reason_codes(boundary.get("reason_codes"))
        if boundary_codes:
            failures["EXACT_DELIVERY_BOUNDARY_REVIEW"] = boundary_codes[0]
    final_codes = _safe_reason_codes(document.get("reason_codes"))
    if final_codes:
        failures["EXACT_FINAL_RELEASE_REVIEW"] = final_codes[0]
    return tuple(sorted(failures.items())), _provider_failure_summary(document.get("discovery"))


def _review_flags_predicate_failures(*, stage: Path, plan) -> tuple[tuple[str, str], ...]:
    """Compatibility wrapper for callers that require only gate rows."""

    return _review_flags_diagnostics(stage=stage, plan=plan)[0]


def _prepare_failure(*, exc: BaseException, stage: Path | None = None, plan=None) -> _PrepareFailure:
    """Build a sanitized per-candidate failure without preserving raw errors."""

    reason_code = _safe_reason_code(exc)
    flags_failures, provider_failure_summary = (
        _review_flags_diagnostics(stage=stage, plan=plan)
        if stage is not None and plan is not None
        else ((), None)
    )
    failures = flags_failures
    if not failures:
        failures = ((_failure_predicate(reason_code), reason_code),)
    exception_diagnostic = (
        _system_exit_diagnostic(exc)
        if reason_code == "REPLAY_PREPARE_SYSTEM_EXIT"
        else _generic_exception_diagnostic(exc)
    )
    redelivery_baseline_failure_summary = (
        _redelivery_baseline_failure_summary(stage=stage, plan=plan)
        if (
            reason_code.startswith("REDELIVERY_SUBTITLE_BASELINE_FAILED")
            and stage is not None
            and plan is not None
        )
        else None
    )
    return _PrepareFailure(
        reason_code=reason_code,
        provider_attempted=False,
        predicate_failures=failures,
        provider_failure_summary=provider_failure_summary,
        exception_diagnostic=exception_diagnostic,
        redelivery_baseline_failure_summary=redelivery_baseline_failure_summary,
    )


def _apply_c2_private_path_unavailable_diagnostic(
    failure: _PrepareFailure,
    *,
    exc: BaseException,
    diagnostic: Callable[[BaseException], object] | None,
) -> None:
    """Replace only C2's opaque missing-path code with a closed path role.

    Generic replay callers do not pass ``diagnostic`` and retain their exact
    historical behavior.  This deliberately accepts neither arbitrary reason
    text nor a generic prefix: the callback result must be one member of the
    fixed C2 vocabulary above.
    """

    if (
        diagnostic is None
        or not isinstance(exc, ReviewedBaselineReplayError)
        or str(exc) != "REPLAY_PATH_UNAVAILABLE"
    ):
        return
    fallback = _C2_PRIVATE_PATH_UNAVAILABLE_PREFIX + "CLASSIFIER_INVALID"
    try:
        candidate = diagnostic(exc)
    except BaseException:
        candidate = None
    reason_code = candidate if isinstance(candidate, str) else fallback
    if reason_code not in _C2_PRIVATE_PATH_UNAVAILABLE_REASON_CODES:
        reason_code = fallback
    failure.reason_code = reason_code
    # Do not let an unrelated staged review-flags document obscure the exact
    # missing-path locus we are diagnosing.  The role is a finalization-local
    # technical predicate, not a content or provider finding.
    failure.predicate_failures = ((_failure_predicate(reason_code), reason_code),)
    failure.exception_diagnostic = None


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


def _published_recovery_matrix(rows) -> list[dict[str, object]]:
    rendered = [dict(row) for row in rows if row.get("predicate") != "UPLOAD_ALLOWED"]
    rendered.extend((
        {"predicate": "PUBLISHED_STATE_SAME_BV_AUTHORITY", "status": "PASS"},
        {"predicate": "STATE_TRANSITION", "status": "PASS_NONE"},
        {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
    ))
    if len({str(row.get("predicate")) for row in rendered}) != len(rendered):
        raise ReviewedBaselineReplayError("REPLAY_PREDICATE_MATRIX_DUPLICATE")
    return rendered


def _canon(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sanitized_failure_receipt(*, runtime: Path, plan, matrix: list[dict[str, object]], provider_attempted: bool,
                                stage_manifest_sha256: str | None = None,
                                prepared_manifest_sha256: str | None = None,
                                provider_receipt_sha256s: tuple[str, ...] = (),
                                provider_failure_summary: dict[str, object] | None = None,
                                exception_diagnostic: Mapping[str, object] | None = None,
                                redelivery_baseline_failure_summary: Mapping[str, object] | None = None) -> str:
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
    # This is intentionally reconstructed through the same closed parser as
    # review-flags input.  A caller cannot use a diagnostic object to smuggle
    # provider text, request metadata, or filesystem paths into a receipt.
    summary = _provider_failure_summary(provider_failure_summary)
    if summary is not None:
        body["provider_failure_summary"] = summary
    generic = _closed_exception_diagnostic(exception_diagnostic)
    if generic is not None:
        body["exception_diagnostic"] = generic
    redelivery_summary = _closed_redelivery_baseline_failure_summary(
        redelivery_baseline_failure_summary
    )
    if redelivery_summary is not None:
        body["redelivery_baseline_failure_summary"] = redelivery_summary
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


def _prepare(
    plan, *, runtime: Path, stage_parent: Path, state_path: Path | None = None,
    record_authority_resolver=None, path_unavailable_diagnostic: Callable[[BaseException], object] | None = None,
    published_recovery=None, recovery_package_root: Path | None = None,
    persist_recovery: bool = False, speaker_python: Path | None = None,
):
             

    """Build one complete no-target-write after-image outside the commit lease."""
    speaker_binding = _speaker_python_binding(speaker_python) if speaker_python is not None else None
    expected_stage = stage_parent / (
        f"{plan.date}-{plan.candidate_id}-"
        f"{_sha(_canonical({'date': plan.date, 'candidate_id': plan.candidate_id, 'record': regular_binding(plan.record_path, label='RECORD').sha256, 'baseline': 'sha256:' + plan.baseline.config['sha256']})).removeprefix('sha256:')[:16]}"
    )
    try:
        staged = stage_replay(
            plan, stage_parent=stage_parent, runtime_authority_root=runtime,
            speaker_python_binding=speaker_binding,
        )
    except (Exception, SystemExit) as exc:
        if expected_stage.exists() and not expected_stage.is_symlink():
            _cleanup_private_stage(expected_stage, parent=stage_parent)
        raise _prepare_failure(exc=exc, plan=plan) from None
    stage = Path(str(staged.get("stage") or ""))
    if not stage.is_absolute() or stage.parent != stage_parent:
        if stage.is_absolute() and stage.parent == stage_parent and stage.exists() and not stage.is_symlink():
            _cleanup_private_stage(stage, parent=stage_parent)
        raise _PrepareFailure(reason_code="REPLAY_PRIVATE_STAGE_RETURN_INVALID", provider_attempted=False) from None
    # The synth entrypoint fail-closes unless the canonical entity verifier is
    # explicitly supplied.  This CLI does not invent one: a production caller
    # must use the dedicated exact-final builder once its fresh private spec is
    # available.  It is therefore impossible to misrepresent this as READY.
    provider_attempted = False
    exact_final_reviewer = None
    exact_final_production = True
    exact_final_text_adapters = None
    if plan.candidate_id == C6_EXACT_CANDIDATE_ID and plan.date == C6_EXACT_RECORDING_DATE:
        # The C6 closure is constructed only after stage_replay and its exact
        # stage manifest pin has been verified.  It is intentionally explicit:
        # synthesize must not select a provider-backed or adapter fallback.
        exact_final_production = False

    def mark_provider_attempt() -> None:
        nonlocal provider_attempted
        provider_attempted = True

    raw_source_fact_llm = _production_llm_call(runtime_root=runtime, effort="high")

    def source_fact_llm(prompt: str) -> str:
        mark_provider_attempt()
        return raw_source_fact_llm(prompt)

    try:
        selected_speaker_python = runtime / _SPEAKER_RUNTIME_RELATIVE
        speaker_python_revalidate = None
        if speaker_python is not None:
            # A preflight may use the installed interpreter without copying or
            # mutating its virtualenv.  Resolve and hash-bind the executable
            # target; accepting an unbound path would make the replay outcome
            # depend on a mutable ambient interpreter.  Invoke the requested
            # launcher, however: a conventional venv ``bin/python`` is a
            # symlink whose target is a base interpreter, and invoking that
            # target directly silently drops the venv's site-packages.
            if speaker_binding is None:
                raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_STAGE_BINDING_DRIFT")
            selected_speaker_python = Path(str(speaker_binding["requested_path"]))
            speaker_python_revalidate = _stage_speaker_python_revalidator(stage, speaker_binding)
        if plan.candidate_id == C6_EXACT_CANDIDATE_ID and plan.date == C6_EXACT_RECORDING_DATE:
            exact_final_reviewer = build_c6_exact_final_reviewer(plan=plan, stage=stage)
        else:
            exact_final_text_adapters = _text_adapters()
        finalization = synthesize_replay_spec_and_finalize_private(
            plan, stage=stage, runtime_authority_root=runtime,
            # The replay controller runs under the system interpreter, but the
            # speaker finalizer requires the pinned ModelScope/CAM++ runtime.
            # Binding this to the runtime root keeps full-dry/apply parity with
            # normal production instead of silently using ``sys.executable``.
            speaker_python=selected_speaker_python,
            source_fact_llm=source_fact_llm,
            adapters=_adapters(), exact_final_reviewer=exact_final_reviewer,
            use_production_exact_final_reviewer=exact_final_production,
            exact_final_text_adapters=exact_final_text_adapters,
            provider_invocation=mark_provider_attempt,
            record_authority_resolver=record_authority_resolver,
            **(
                {"recovery_publication_authority": published_recovery.publication_authority}
                if published_recovery is not None else {}
            ),
            speaker_python_revalidate=speaker_python_revalidate,
        )
    except (Exception, SystemExit) as exc:
        stage_sha = _stage_manifest_sha256(stage)
        provider_hashes = _provider_receipt_sha256s(stage)
        failure = _prepare_failure(exc=exc, stage=stage, plan=plan)
        _apply_c2_private_path_unavailable_diagnostic(
            failure, exc=exc, diagnostic=path_unavailable_diagnostic,
        )
        failure.provider_attempted = provider_attempted
        failure.stage_manifest_sha256 = stage_sha
        failure.provider_receipt_sha256s = provider_hashes
        _cleanup_private_stage(stage, parent=stage_parent)
        raise failure from None
    stage_sha = _stage_manifest_sha256(stage)
    provider_hashes = _provider_receipt_sha256s(finalization.private_runtime_root)
    try:
        if published_recovery is not None:
            if recovery_package_root is None:
                dry_parent = stage / "published-recovery-output"
                dry_parent.mkdir(mode=0o700)
                recovery_package_root = dry_parent / plan.candidate_id
            after = prepare_published_recovery_package(
                plan,
                finalization=finalization,
                runtime_root=runtime,
                preflight=published_recovery,
                package_outer_root=recovery_package_root,
                persist=persist_recovery,
            )
        else:
            if state_path is None:
                raise ReviewedBaselineReplayError("REPLAY_STATE_PATH_REQUIRED")
            after = prepare_replay_after_image(
                plan, runtime_root=runtime, state_path=state_path, finalization=finalization,
            )
    except (Exception, SystemExit) as exc:
        prepared_sha = _prepared_manifest_sha256(finalization)
        failure = _prepare_failure(exc=exc, stage=stage, plan=plan)
        failure.provider_attempted = provider_attempted
        failure.stage_manifest_sha256 = stage_sha
        failure.prepared_manifest_sha256 = prepared_sha
        failure.provider_receipt_sha256s = provider_hashes
        _cleanup_private_stage(stage, parent=stage_parent)
        raise failure from None
    return stage, finalization, after, stage_sha, provider_hashes, provider_attempted


def _c3_direct_pass0(runtime: Path, *, date: str, candidate_ids: list[str], stage_parent: Path, apply: bool) -> tuple[dict[str, object], int] | None:
    """Dispatch the sealed C3 v3 lane before generic replay/finalization."""
    from src.autoslice.fastlane_c3_v3_direct import (
        CANDIDATE_ID, C3V3ClosureError, V3_RELATIVE_ROOT, consume_c3_successor_pass0,
    )
    if date != "2026-08-13" or candidate_ids != [CANDIDATE_ID]:
        return None
    if not (runtime / V3_RELATIVE_ROOT).exists():
        return None
    if apply:
        return ({"schema_version": "c3-direct-pass0.v1", "candidate_id": CANDIDATE_ID,
                 "status": "BLOCKED", "reason_code": "C3_DIRECT_APPLY_STATE_AFTER_IMAGE_REQUIRED",
                 "upload_allowed": False, "provider_attempted": False}, 2)
    try:
        from scripts.audit_lidousha_review_package import audit_package
        result = consume_c3_successor_pass0(
            runtime_root=runtime, private_stage_parent=stage_parent,
            audit=audit_package,
        )
    except C3V3ClosureError as exc:
        return ({"schema_version": "c3-direct-pass0.v1", "candidate_id": CANDIDATE_ID,
                 "status": "BLOCKED", "reason_code": str(exc), "upload_allowed": False,
                 "provider_attempted": False}, 2)
    return ({"schema_version": "c3-direct-pass0.v1", "candidate_id": CANDIDATE_ID,
             "status": "PASS0", "upload_allowed": False, "provider_attempted": False,
             "prepared_delivery": result.prepared_delivery,
             "private_package_binding": dict(result.private_package_binding),
             "private_audit_binding": dict(result.private_audit_binding),
             "predicate_matrix": list(result.predicate_matrix)}, 0)


def main(
    argv: list[str] | None = None,
    *,
    _record_authority_resolver=None,
    _path_unavailable_diagnostic: Callable[[BaseException], object] | None = None,
) -> int:
    args = _args(argv)
    try:
        if args.speaker_python is not None and (
            not args.full_dry_run or args.private_stage_parent is None
        ):
            raise ReviewedBaselineReplayError("REPLAY_SPEAKER_PYTHON_PRIVATE_DRY_RUN_ONLY")
        runtime = _safe_directory(args.runtime_root)
        if args.readiness_graph:
            print(json.dumps(_readiness_graph(runtime=runtime, date=args.date,
                                               candidate_ids=args.candidate_id), ensure_ascii=False, sort_keys=True))
            return 0
        if args.full_dry_run or args.apply:
            _runtime_gate(runtime)
            direct = _c3_direct_pass0(
                runtime, date=args.date, candidate_ids=args.candidate_id,
                stage_parent=_private_parent(args.private_stage_parent), apply=args.apply,
            )
            if direct is not None:
                payload, status = direct
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return status
        plans = [build_replay_plan(repo_root=runtime / "repo", out_root=runtime / "out",
                                   date=args.date, candidate_id=cid)
                 for cid in args.candidate_id]
        recovery_by_id = {}
        if args.published_recovery_bvid:
            recovery = published_recovery_preflight(
                plans[0], runtime_root=runtime,
                expected_bvid=args.published_recovery_bvid,
            )
            recovery_by_id[plans[0].candidate_id] = recovery
        if args.plan or not (args.full_dry_run or args.apply):
            print(json.dumps({"schema_version": "reviewed-baseline-replay-plan.v2", "mode": "PLAN",
                              "upload_allowed": False,
                              "candidates": [{"candidate_id": p.candidate_id,
                                              "predicate_matrix": (
                                                  _published_recovery_matrix(p.matrix)
                                                  if p.candidate_id in recovery_by_id else list(p.matrix)
                                              )} for p in plans]},
                             ensure_ascii=False, sort_keys=True))
            return 0
        _runtime_gate(runtime)
        stage_parent = _private_parent(args.private_stage_parent)
        prepared, errors = {}, {}
        state_path = runtime / "state" / f"{args.date}.json"
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(plans)) as pool:
            prepare_kwargs = {
                "runtime": runtime,
                "stage_parent": stage_parent,
                "state_path": state_path,
            }
            if _record_authority_resolver is not None:
                prepare_kwargs["record_authority_resolver"] = _record_authority_resolver
            if _path_unavailable_diagnostic is not None:
                prepare_kwargs["path_unavailable_diagnostic"] = _path_unavailable_diagnostic
            if recovery_by_id:
                prepare_kwargs.update({
                    "published_recovery": recovery_by_id[plans[0].candidate_id],
                    "recovery_package_root": args.recovery_package_root,
                    "persist_recovery": bool(args.apply),
                })
            prepare_kwargs["speaker_python"] = args.speaker_python
            work = {
                pool.submit(_prepare, plan, **prepare_kwargs): plan
                for plan in plans
            }
            for future in concurrent.futures.as_completed(work):
                plan = work[future]
                try:
                    prepared[plan.candidate_id] = future.result()
                except (Exception, SystemExit) as exc:  # Never surface provider/path text.
                    errors[plan.candidate_id] = _PrepareFailure(
                        reason_code=_safe_reason_code(exc),
                        provider_attempted=bool(getattr(exc, "provider_attempted", False)),
                        stage_manifest_sha256=getattr(exc, "stage_manifest_sha256", None),
                        prepared_manifest_sha256=getattr(exc, "prepared_manifest_sha256", None),
                        provider_receipt_sha256s=tuple(getattr(exc, "provider_receipt_sha256s", ())),
                        predicate_failures=tuple(getattr(exc, "predicate_failures", ())),
                        provider_failure_summary=_provider_failure_summary(
                            getattr(exc, "provider_failure_summary", None),
                        ),
                        exception_diagnostic=_closed_exception_diagnostic(
                            getattr(exc, "exception_diagnostic", None),
                        ),
                        redelivery_baseline_failure_summary=(
                            _closed_redelivery_baseline_failure_summary(
                                getattr(
                                    exc,
                                    "redelivery_baseline_failure_summary",
                                    None,
                                )
                            )
                        ),
                    )
        result = {"schema_version": "reviewed-baseline-replay-run.v1",
                  "mode": "APPLY" if args.apply else "FULL_DRY_RUN",
                  "upload_allowed": False, "candidates": []}
        blocked = False
        for plan in plans:
            error = errors.get(plan.candidate_id)
            if error:
                blocked = True
                reason_code = error.reason_code
                provider_attempted = error.provider_attempted
                stage_sha = error.stage_manifest_sha256
                prepared_sha = error.prepared_manifest_sha256
                provider_hashes = error.provider_receipt_sha256s
                provider_summary = error.provider_failure_summary
                exception_diagnostic = _closed_exception_diagnostic(error.exception_diagnostic)
                redelivery_summary = _closed_redelivery_baseline_failure_summary(
                    error.redelivery_baseline_failure_summary
                )
                matrix = _matrix(plan, status="NOT_EVALUATED",
                                 failures=dict(error.predicate_failures) or {
                                     _failure_predicate(reason_code): reason_code,
                                 })
                receipt = _sanitized_failure_receipt(
                    runtime=runtime, plan=plan, matrix=matrix, provider_attempted=provider_attempted,
                    stage_manifest_sha256=stage_sha, prepared_manifest_sha256=prepared_sha,
                    provider_receipt_sha256s=provider_hashes,
                    provider_failure_summary=provider_summary,
                    exception_diagnostic=exception_diagnostic,
                    redelivery_baseline_failure_summary=redelivery_summary,
                )
                item = {"candidate_id": plan.candidate_id, "status": "BLOCKED",
                        "predicate_matrix": matrix, "diagnostic_receipt_sha256": receipt}
                if provider_summary is not None:
                    item["provider_failure_summary"] = provider_summary
                if exception_diagnostic is not None:
                    item["exception_diagnostic"] = exception_diagnostic
                if redelivery_summary is not None:
                    item["redelivery_baseline_failure_summary"] = redelivery_summary
                result["candidates"].append(item)
                continue
            stage, finalization, prepared_after, stage_sha, provider_hashes, provider_attempted = prepared[plan.candidate_id]
            if plan.candidate_id in recovery_by_id:
                recovery_receipt = dict(prepared_after.receipt)
                try:
                    _cleanup_private_stage(stage, parent=stage_parent)
                    item = {
                        "candidate_id": plan.candidate_id,
                        "status": (
                            "VERIFIED_PRIVATE_PACKAGE" if args.apply
                            else "READY_PRIVATE_PACKAGE"
                        ),
                        "predicate_matrix": _published_recovery_matrix(
                            _matrix(plan, status="PASS")
                        ),
                        "recovery_package": recovery_receipt,
                        "upload_allowed": False,
                    }
                except (Exception, SystemExit):
                    blocked = True
                    item = {
                        "candidate_id": plan.candidate_id,
                        "status": "PRIVATE_PACKAGE_CLEANUP_UNCONFIRMED",
                        "predicate_matrix": [
                            *_published_recovery_matrix(_matrix(plan, status="PASS"))[:-1],
                            {"predicate": "PRIVATE_STAGE_CLEANUP", "status": "FAIL"},
                            {"predicate": "UPLOAD_ALLOWED", "status": "PASS_FALSE"},
                        ],
                        "recovery_package": recovery_receipt,
                        "upload_allowed": False,
                    }
                result["candidates"].append(item)
                continue
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
                                runtime=runtime, plan=plan, matrix=matrix, provider_attempted=provider_attempted,
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
                                provider_attempted=provider_attempted, stage_manifest_sha256=stage_sha,
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
