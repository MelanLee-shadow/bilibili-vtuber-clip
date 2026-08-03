"""Talk lane: recall parsing, speaker evidence, failure policy, and production.

Extracted from scripts/session_autoslice.py. Runner-owned paths, policy
constants, and patchable helpers resolve at call time through a lazy proxy. This
preserves the runner's existing monkeypatch seam while remaining safe when cron
executes the runner as __main__.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.boundary_semantic_review import (
    SEMANTIC_TAIL_TRIM_MAX_MS,
    boundary_search_scope_is_valid,
    required_source_context_end_ms,
)
from src.autoslice.cross_segment_witness import (
    discover_cross_segment_witness_reserve as
    _discover_cross_segment_witness_reserve,
)
from src.autoslice.foreign_source_failure_evidence import (
    foreign_source_gate_violation as _foreign_source_gate_violation,
    foreign_source_provider_transient as _foreign_source_provider_transient,
)
from src.autoslice.producer_boundary_owner_contract import (
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.recovery_title_authority import (
    RecoveryTitleAuthorityError,
    validate_recovery_publication_authority,
)
from src.autoslice.selection_scorecard import selection_scorecard_is_valid
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    validate_speaker_review_manifest_document,
)
from src.autoslice.story_contract import (
    canonicalize_relation_summary,
    canonicalize_story_scorecard,
)
from src.autoslice.talk_filler import (
    build_piece_specs,
    build_talk_filler_plan,
    verify_automatic_filler_plan,
)


_runner = RunnerProxy()

def _bound_boundary_retry_source_end_ms(
    *,
    out_root: Path,
    candidate_id: str,
    repair_cap_ms: int,
) -> int | None:
    """Read the failed review's bound scope and retain ceiling witnesses."""

    review_path = (
        out_root
        / candidate_id
        / f"{candidate_id}.review-flags.json"
    )
    document, _digest = _read_final_review_surface(review_path)
    if not isinstance(document, dict):
        return None
    review = document.get("boundary_semantic_review")
    if not isinstance(review, dict):
        return None
    scope = review.get("boundary_search_scope")
    if not boundary_search_scope_is_valid(scope):
        return None
    try:
        return required_source_context_end_ms(
            scope,
            repair_cap_ms=repair_cap_ms,
        )
    except (TypeError, ValueError):
        return None


def _load_boundary_retry_owner_contract(
    *,
    out_root: Path,
    candidate_id: str,
) -> dict[str, object]:
    """Load the first attempt's hash-bound owner set before widening context."""

    chat_path = (
        out_root
        / candidate_id
        / f"{candidate_id}.chat-authority.json"
    )
    document, _digest = _read_final_review_surface(chat_path)
    if not isinstance(document, dict):
        raise RuntimeError("BOUNDARY_RETRY_OWNER_SET_DRIFT")
    return validate_frozen_boundary_owner_contract(
        document.get("frozen_boundary_owner_contract")
    )


def danmaku_hints(xml_path: Path | None) -> str | None:
    if xml_path is None:
        return None
    try:
        from src.autoslice.danmaku_evidence import find_danmaku_bursts, load_danmaku_xml

        items = load_danmaku_xml(xml_path)
        bursts = find_danmaku_bursts(items)
    except Exception:  # noqa: BLE001 — hints are optional enrichment
        return None
    lines = []
    for burst in bursts[:6]:
        sample = " / ".join(burst.sample_texts[:3])
        lines.append(f"{burst.start_ms // 60000:02d}:{burst.start_ms // 1000 % 60:02d} x{burst.count}: {sample}")
    return "弹幕突发时段(观众密集反应，强候选提示):\n" + "\n".join(lines) if lines else None


def danmaku_count_in(xml_path_str: str | None, start_ms: int, end_ms: int) -> int:
    if not xml_path_str:
        return 0
    try:
        from src.autoslice.danmaku_evidence import load_danmaku_xml

        items = load_danmaku_xml(Path(xml_path_str))
    except Exception:  # noqa: BLE001
        return 0
    return sum(1 for item in items if start_ms <= item.offset_ms < end_ms)


def slice_srt(src_srt: Path, start_ms: int, end_ms: int, dest: Path) -> int:
    """Cut [start_ms, end_ms) out of an SRT and rebase timestamps to 0."""
    from scripts.run_auto_review_shadow_pipeline import _parse_srt

    def ts(ms: int) -> str:
        ms = max(0, ms)
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    blocks = []
    for cue in _parse_srt(src_srt):
        if cue.source_end_ms <= start_ms or cue.source_start_ms >= end_ms:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{ts(cue.source_start_ms - start_ms)} --> {ts(cue.source_end_ms - start_ms)}\n{cue.text}"
        )
    dest.write_text("\n\n".join(blocks) + "\n" if blocks else "", encoding="utf-8")
    return len(blocks)


def safe_name(text: str, fallback: str) -> str:
    """Human-readable delivery filename from the recall hook."""
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "", (text or "").strip())
    truncated = cleaned[:18]
    # 硬截会把孤立的开引号/连接标点挂在文件名尾（shell 里全角引号极难处理）；
    # 截断点回退到内容字符为止。
    truncated = truncated.rstrip("“”「」『』《》〈〉（）(),，、。：:;；—-·…‘’'\"")
    return truncated if truncated else (cleaned[:18] or fallback)


def last_json_block(text: str) -> dict:
    """Parse the LAST balanced top-level JSON object in text (produce logs end
    with a summary object that contains nested objects — a non-greedy regex
    can't match it; this walks braces from the last closing brace backwards)."""
    end = text.rfind("}")
    while end != -1:
        depth = 0
        for start in range(end, -1, -1):
            ch = text[start]
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : end + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        break
                    break
        end = text.rfind("}", 0, max(0, end))
    return {}


def recall_candidates(srt_path: Path, hints: str | None) -> tuple[list, str, dict]:
    """(candidates, lane, extras) — semantic lane first, deterministic fallback.

    extras maps candidate_id → hook/confidence plus the deterministic
    selection_scorecard.  Confidence remains a recall signal; content value is
    ranked by hard Tier then the fixed scorecard arithmetic.
    """
    from scripts.run_auto_review_shadow_pipeline import _parse_srt
    from src.autoslice.full_session_candidate_selector import (
        select_fallback_session_candidates,
        select_full_session_candidates,
    )
    from src.autoslice.llm_client import LlmCallError, LlmConfig, build_llm_call
    from src.autoslice.semantic_candidate_selector import (
        select_semantic_session_candidates_covered,
    )

    cues = _parse_srt(srt_path)
    if not cues:
        return [], "empty", {}
    llm = build_llm_call(
        LlmConfig(
            transport="command",
            # Talk semantic recall = deep open-ended lane → gpt-5.6-sol medium.
            command_template=f"bash {_runner.REPO_ROOT}/scripts/llm_via_cpa.sh {{prompt_file}} {{completion_file}} 'gpt-5.6-sol gpt-5.5 gpt-5.4' medium",
            timeout_seconds=600.0,
        )
    )
    try:
        candidates, diag = select_semantic_session_candidates_covered(
            cues, llm_call=llm, max_candidates=_runner.PER_SEGMENT_CANDIDATES, danmaku_hints=hints
        )
        hooks = diag.get("hooks") or {}
        scorecards = diag.get("scorecards") or {}
        filler_proposals = diag.get("filler_proposals") or {}
        merge_gap_plans = diag.get("merge_gaps") or {}
        source_srt_sha256 = "sha256:" + hashlib.sha256(
            srt_path.read_bytes()
        ).hexdigest()
        extras = {}
        for cand in candidates:
            cid = cand.anchor.candidate_id
            extras[cid] = {
                "hook": str(hooks.get(cid) or ""),
                "confidence": round(float(getattr(cand.boundary, "start_boundary_score", 0.5) or 0.5), 2),
                "selection_scorecard": (
                    dict(scorecards[cid])
                    if isinstance(scorecards.get(cid), dict)
                    else None
                ),
                "filler_proposals": list(filler_proposals.get(cid) or []),
                "filler_proposal_srt_sha256": source_srt_sha256,
                # 同主题合并跳切缝隙（event_key 确定性合并，维护者）。
                "merge_gap_removals": list(merge_gap_plans.get(cid) or []),
            }
        # Deterministic song supplement: recall's candidate cap squeezes songs
        # out on song-heavy streams (first real run: 4+ songs sung, 1 caught).
        try:
            supplement = select_fallback_session_candidates(cues, max_candidates=8)
        except Exception:  # noqa: BLE001
            supplement = []
        for cand in supplement:
            if getattr(cand, "content_type_hint", "talk") != "song":
                continue
            if any(
                getattr(c, "content_type_hint", "talk") == "song"
                and not (cand.anchor.anchor_end_ms <= c.anchor.anchor_start_ms or cand.anchor.anchor_start_ms >= c.anchor.anchor_end_ms)
                for c in candidates
            ):
                continue  # overlaps a recall song → duplicate
            candidates.append(cand)
            extras[cand.anchor.candidate_id] = {
                "hook": "确定性歌检测补充(演唱段)",
                "confidence": 0.5,
                "selection_scorecard": None,
            }
        lane = (
            "semantic_recall_sharded"
            if diag.get("mode") == "sharded"
            else "semantic_recall"
        )
        return candidates, lane, extras
    except LlmCallError as exc:
        _runner.log(f"semantic recall failed ({exc}); falling back to deterministic lanes")
        primary = select_full_session_candidates(cues, max_candidates=_runner.PER_SEGMENT_CANDIDATES)
        fallback = select_fallback_session_candidates(cues, max_candidates=8)
        for candidate in fallback:
            if any(
                min(candidate.anchor.anchor_end_ms, existing.anchor.anchor_end_ms)
                > max(candidate.anchor.anchor_start_ms, existing.anchor.anchor_start_ms)
                for existing in primary
            ):
                continue
            primary.append(candidate)
        return primary, "deterministic_fallback", {}


def read_publish_meta(work_dir: Path) -> dict:
    for publish in sorted(work_dir.glob("replacement_recuts/*.publish.json")):
        try:
            d = json.loads(publish.read_text(encoding="utf-8"))
            hashes = d.get("artifact_hashes") if isinstance(d.get("artifact_hashes"), dict) else {}
            return {
                "title": d.get("title"),
                "title_source": d.get("title_source"),
                "title_authority_status": d.get("title_authority_status"),
                "title_authority_error": d.get("title_authority_error"),
                "cover_status": d.get("cover_status"),
                "cover_path": d.get("cover_path"),
                "cover_sha256": hashes.get("cover_sha256"),
                "cover_generation": d.get("cover_generation"),
                "video_sha256": hashes.get("burned_video_sha256") or hashes.get("video_sha256"),
            }
        except (OSError, ValueError):
            continue
    return {}


def _talk_cover_delivery_ready(date: str, result: dict) -> bool:
    """Prove the copied talk package has a route-bound cover, not just video."""

    if result.get("cover_status") != "AI_COVER_READY":
        return False
    paths = _runner.delivered_paths(date, result)
    if paths is None:
        return False
    mp4, cover = paths
    return bool(
        cover.is_file()
        and _runner._initial_cover_proof_valid(date, result, mp4, cover)
    )


def _speaker_review_manifest_state(work_dir: Path) -> dict[str, tuple[int, int, int, int]]:
    state: dict[str, tuple[int, int, int, int]] = {}
    for path in work_dir.glob("replacement_recuts/*.speaker-final.json"):
        try:
            metadata = path.stat()
        except OSError:
            continue
        state[str(path)] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_size,
        )
    return state


def read_speaker_review_meta(
    work_dir: Path,
    *,
    previous_state: dict[str, tuple[int, int, int, int]],
) -> dict:
    for manifest_path in sorted(work_dir.glob("replacement_recuts/*.speaker-final.json")):
        try:
            metadata = manifest_path.stat()
            current_state = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mtime_ns,
                metadata.st_size,
            )
            if previous_state.get(str(manifest_path)) == current_state:
                continue
            payload = manifest_path.read_bytes()
            after_read = manifest_path.stat()
            if current_state != (
                after_read.st_dev,
                after_read.st_ino,
                after_read.st_mtime_ns,
                after_read.st_size,
            ):
                continue
            document = json.loads(payload)
            rows = validate_speaker_review_manifest_document(document)
        except (OSError, ValueError, SpeakerFinalizationError):
            continue
        return {
            "speaker_review_manifest": str(manifest_path),
            "speaker_review_manifest_sha256": "sha256:"
            + hashlib.sha256(payload).hexdigest(),
            "speaker_review_source_media_sha256": document["source_media_sha256"],
            "speaker_review_text_final_srt_sha256": document["text_final_srt_sha256"],
            "speaker_review_context_unresolved_cues": document["context_unresolved_cues"],
            "speaker_review_reason": document["reason"],
            "speaker_review_required_cues": rows,
        }
    return {}


def _speaker_evidence_insufficient_failure(attempt_output: str) -> bool:
    """Recognize only deterministic identity-evidence shortage from finalizer.

    Other ``SPEAKER_FINALIZATION_BLOCKED`` errors include missing/drifted
    runtime assets and malformed manifests.  Those must remain retryable
    producer failures rather than being hidden as a rejected content pick.
    """

    return (
        "SPEAKER_FINALIZATION_BLOCKED" in attempt_output
        and "SpeakerFinalizationError: not enough Li Dousha clip anchors:"
        in attempt_output
    )


_FINAL_REVIEW_ARTIFACT_RX = re.compile(
    r"(/[^\r\n'\"<>]*?(?:\.chat-authority|\.review-flags)\.json)"
)
_FINAL_REVIEW_ARTIFACT_MAX_BYTES = 8 * 1024 * 1024


def _read_final_review_surface(path: Path) -> tuple[dict | None, str | None]:
    try:
        if (
            not path.is_file()
            or path.stat().st_size > _FINAL_REVIEW_ARTIFACT_MAX_BYTES
        ):
            return None, None
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, ValueError):
        return None, None
    if not isinstance(document, dict):
        return None, None
    return document, "sha256:" + hashlib.sha256(payload).hexdigest()


def _final_review_candidate_id(path: Path) -> str:
    for suffix in (".chat-authority.json", ".review-flags.json"):
        if path.name.endswith(suffix):
            return path.name[: -len(suffix)]
    return path.stem


def _compact_final_review_finding(row: object) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    summary = {
        key: row.get(key)
        for key in (
            "cue_index",
            "kind",
            "repair_class",
            "suspect",
            "suggestion",
            "proposed_full_cue",
            "suggestion_rejected_reason",
            "why",
            "force_acoustic",
            "correlated_text_witness",
            "base_text_sha256",
            "candidate_memory_id",
            "evidence_cue_ids",
            "reported_scope_warnings",
            "span_start_codepoint",
            "span_end_codepoint",
        )
        if row.get(key) is not None
    }
    provenance = row.get("candidate_provenance")
    if isinstance(provenance, dict):
        summary["candidate_provenance"] = {
            key: provenance.get(key)
            for key in (
                "kind",
                "surface",
                "memory_id",
                "mutation_authorized",
                "ledger_sha256",
                "nearest_cue_distance",
            )
            if provenance.get(key) is not None
        }
    return summary


def _compact_boundary_semantic_review(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {"status": "MISSING"}
    return {
        key: value.get(key)
        for key in (
            "schema_version",
            "status",
            "reason_codes",
            "target_ms",
            "recommended_end_ms",
            "syntax_complete",
            "story_closed",
            "content_anchor_covered",
            "next_topic_separated",
            "summary",
            "request_sha256",
        )
        if value.get(key) is not None
    }


def _final_review_contract_reason_code(attempt_output: str) -> str | None:
    match = re.search(
        r"FINAL_REVIEW_RELEASE_BLOCKED:\s*([A-Z0-9_]+)",
        attempt_output,
    )
    return match.group(1) if match else None


def _compact_correction_pass(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"status": "MISSING"}
    reason_codes = value.get("reason_codes")
    if isinstance(reason_codes, str):
        normalized_reason_codes = [reason_codes] if reason_codes else []
    elif isinstance(reason_codes, list):
        normalized_reason_codes = [
            str(code) for code in reason_codes if str(code)
        ]
    else:
        normalized_reason_codes = []
    discovery = value.get("discovery")
    return {
        key: item
        for key, item in {
            "schema_version": value.get("schema_version"),
            "status": value.get("status"),
            "reason_codes": normalized_reason_codes,
            "discovery": (
                {
                    field: discovery.get(field)
                    for field in ("status", "detail")
                    if discovery.get(field) is not None
                }
                if isinstance(discovery, Mapping)
                else {"status": "MISSING"}
            ),
            "error_type": value.get("error_type"),
            "applied_count": value.get("applied_count"),
        }.items()
        if item is not None
    }


def _compact_correction_mutation_authority(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {"status": "MISSING", "failures": []}
    compact_failures: list[dict[str, object]] = []
    for row in value.get("failures") or []:
        if not isinstance(row, Mapping):
            continue
        compact_failures.append(
            {
                key: row.get(key)
                for key in (
                    "reason_code",
                    "upstream_reason_codes",
                    "cue_index",
                    "routed",
                )
                if row.get(key) is not None
            }
        )
    return {
        key: item
        for key, item in {
            "schema_version": value.get("schema_version"),
            "status": value.get("status"),
            "applied_count": value.get("applied_count"),
            "validated_mutation_count": value.get(
                "validated_mutation_count"
            ),
            "failures": compact_failures,
        }.items()
        if item is not None
    }


def _final_review_failure_evidence(attempt_output: str) -> dict[str, object]:
    matches = _FINAL_REVIEW_ARTIFACT_RX.findall(attempt_output)
    artifact_path = Path(matches[-1].strip()) if matches else None
    evidence: dict[str, object] = {
        "schema_version": "talk-final-review-failure-evidence.v1",
        "candidate_id": (
            _final_review_candidate_id(artifact_path)
            if artifact_path is not None
            else None
        ),
        "contract_reason_code": _final_review_contract_reason_code(attempt_output),
        "audit_loaded": False,
    }
    if artifact_path is None:
        return evidence

    evidence["artifact_file"] = artifact_path.name
    if artifact_path.name.endswith(".chat-authority.json"):
        candidate_name = artifact_path.name[: -len(".chat-authority.json")]
        review_path = artifact_path.with_name(candidate_name + ".review-flags.json")
        chat_path = artifact_path
    else:
        candidate_name = artifact_path.name[: -len(".review-flags.json")]
        review_path = artifact_path
        chat_path = artifact_path.with_name(candidate_name + ".chat-authority.json")

    review_document, review_sha = _read_final_review_surface(review_path)
    chat_document, chat_sha = _read_final_review_surface(chat_path)
    surface_files = []
    if review_sha is not None:
        surface_files.append(
            {"role": "review_flags", "file": review_path.name, "sha256": review_sha}
        )
    if chat_sha is not None:
        surface_files.append(
            {
                "role": "chat_authority",
                "file": chat_path.name,
                "sha256": chat_sha,
            }
        )
    evidence["surface_files"] = surface_files

    direct_audit = (
        review_document
        if isinstance(review_document, dict)
        and review_document.get("schema_version") == "final-review-audit.v2"
        else None
    )
    nested = (
        chat_document.get("final_review_audit")
        if isinstance(chat_document, dict)
        else None
    )
    nested_audit = (
        nested
        if isinstance(nested, dict)
        and nested.get("schema_version") == "final-review-audit.v2"
        else None
    )
    if direct_audit is not None and nested_audit is not None:
        # The exact-final pass persists carryover metadata into chat authority
        # after review-flags has already been written.  That one additive
        # receipt is not a contradictory audit surface; every shared field
        # must still match byte-for-value and no other field may diverge.
        direct_only = set(direct_audit) - set(nested_audit)
        nested_only = set(nested_audit) - set(direct_audit)
        shared_mismatches = [
            key
            for key in set(direct_audit) & set(nested_audit)
            if direct_audit[key] != nested_audit[key]
        ]
        allowed_nested_only = {"carryover_persisted_count"}
        evidence["audit_surfaces_consistent"] = (
            not direct_only
            and not shared_mismatches
            and nested_only <= allowed_nested_only
        )
        if nested_only:
            evidence["audit_nested_additive_fields"] = sorted(nested_only)
    audit = direct_audit or nested_audit
    if audit is None:
        return evidence

    raw_findings = audit.get("findings")
    nested_carryover_count = (
        nested_audit.get("carryover_persisted_count")
        if isinstance(nested_audit, Mapping)
        else None
    )
    carryover_file = artifact_path.with_name(
        candidate_name + ".final-review-carryover.json"
    )
    carryover_document: object = None
    carryover_sha256: str | None = None
    try:
        carryover_payload = carryover_file.read_bytes()
        carryover_sha256 = (
            "sha256:" + hashlib.sha256(carryover_payload).hexdigest()
        )
        carryover_document = json.loads(carryover_payload)
    except (OSError, ValueError):
        pass
    carryover_rows = (
        carryover_document.get("findings")
        if isinstance(carryover_document, Mapping)
        and carryover_document.get("schema_version")
        == "final-review-carryover.v1"
        else None
    )
    expected_carryover_rows = {
        (
            row.get("cue_index") or row.get("cue"),
            str(row.get("suspect") or ""),
            str(row.get("proposed_full_cue") or ""),
        )
        for row in (
            raw_findings if isinstance(raw_findings, list) else []
        )
        if isinstance(row, Mapping)
        and isinstance(row.get("exact_release_adjudication"), Mapping)
        and row["exact_release_adjudication"].get("repaired") is True
    }
    observed_carryover_rows = {
        (
            row.get("cue"),
            str(row.get("suspect") or ""),
            str(row.get("proposed_full_cue") or ""),
        )
        for row in (
            carryover_rows if isinstance(carryover_rows, list) else []
        )
        if isinstance(row, Mapping)
    }
    carryover_retry_ready = bool(
        isinstance(nested_carryover_count, int)
        and not isinstance(nested_carryover_count, bool)
        and nested_carryover_count > 0
        and isinstance(carryover_rows, list)
        and len(carryover_rows) == nested_carryover_count
        and len(observed_carryover_rows) == nested_carryover_count
        and observed_carryover_rows == expected_carryover_rows
    )
    if (
        nested_carryover_count is not None
        or carryover_document is not None
    ):
        evidence["carryover"] = {
            "file": carryover_file.name,
            "sha256": carryover_sha256,
            "declared_count": nested_carryover_count,
            "observed_count": (
                len(carryover_rows)
                if isinstance(carryover_rows, list)
                else None
            ),
            "retry_ready": carryover_retry_ready,
        }
    raw_reason_codes = audit.get("reason_codes")
    if isinstance(raw_reason_codes, str):
        normalized_reason_codes = [raw_reason_codes] if raw_reason_codes else []
    elif isinstance(raw_reason_codes, list):
        normalized_reason_codes = [
            str(code) for code in raw_reason_codes if str(code)
        ]
    else:
        normalized_reason_codes = []
    compact_findings = [
        summary
        for row in (raw_findings if isinstance(raw_findings, list) else [])
        if (summary := _compact_final_review_finding(row)) is not None
    ]
    discovery = audit.get("discovery")
    evidence.update(
        {
            "audit_loaded": True,
            "audit_schema_version": audit.get("schema_version"),
            "status": audit.get("status"),
            "release_gate": audit.get("release_gate"),
            "reason_codes": normalized_reason_codes,
            "reviewed_srt_sha256": audit.get("reviewed_srt_sha256"),
            "discovery": (
                dict(discovery)
                if isinstance(discovery, dict)
                else {"status": "MISSING"}
            ),
            "findings_contract_valid": isinstance(raw_findings, list),
            "validated_finding_count": audit.get("validated_finding_count"),
            "findings": compact_findings[:20],
            "findings_truncated": max(0, len(compact_findings) - 20),
            "boundary_semantic_review": _compact_boundary_semantic_review(
                audit.get("boundary_semantic_review")
            ),
            "correction_pass": _compact_correction_pass(
                audit.get("correction_pass")
            ),
            "correction_mutation_authority": (
                _compact_correction_mutation_authority(
                    audit.get("correction_mutation_authority")
                )
            ),
        }
    )
    return evidence


def _classify_final_review_release(
    attempt_output: str,
) -> tuple[str, str, bool, dict[str, object]]:
    evidence = _final_review_failure_evidence(attempt_output)
    contract_reason = str(evidence.get("contract_reason_code") or "")
    reason_codes = {
        str(code) for code in (evidence.get("reason_codes") or []) if str(code)
    }
    discovery = evidence.get("discovery")
    discovery_status = (
        str(discovery.get("status") or "")
        if isinstance(discovery, dict)
        else ""
    )
    if evidence.get("audit_surfaces_consistent") is False:
        return "final_review_contract", "final_review_contract", False, evidence
    if contract_reason in {
        "FINAL_REVIEW_AUDIT_MISSING_OR_INVALID",
        "FINAL_REVIEW_AUDIT_SCHEMA_INVALID",
        "FINAL_REVIEW_SRT_BINDING_INVALID",
        "FINAL_REVIEW_SRT_BINDING_MISMATCH",
        "FINAL_REVIEW_FINDINGS_CONTRACT_INVALID",
    }:
        return "final_review_contract", "final_review_contract", False, evidence
    if (
        contract_reason == "FINAL_REVIEW_DISCOVERY_INCOMPLETE"
        and evidence.get("audit_loaded") is True
        and evidence.get("status") == "AUDITOR_UNAVAILABLE"
        and discovery_status == "AUDITOR_UNAVAILABLE"
    ):
        return "provider_transient", "final_review_discovery", True, evidence
    if discovery_status != "COMPLETE":
        return "final_review_contract", "final_review_contract", False, evidence
    if evidence.get("audit_loaded") is not True:
        return "final_review_contract", "final_review_contract", False, evidence
    correction_pass = evidence.get("correction_pass")
    correction_status = (
        str(correction_pass.get("status") or "")
        if isinstance(correction_pass, Mapping)
        else ""
    )
    correction_mutations = evidence.get("correction_mutation_authority")
    correction_failures = (
        correction_mutations.get("failures")
        if isinstance(correction_mutations, Mapping)
        else []
    )
    correction_failure_codes = {
        str(row.get("reason_code") or "")
        for row in (
            correction_failures
            if isinstance(correction_failures, list)
            else []
        )
        if isinstance(row, Mapping)
    }
    if (
        correction_status == "AUDITOR_UNAVAILABLE"
        or "CORRECTION_DISCOVERY_INCOMPLETE"
        in correction_failure_codes
    ):
        return (
            "provider_transient",
            "final_review_correction_discovery",
            True,
            evidence,
        )
    boundary = evidence.get("boundary_semantic_review")
    boundary_status = (
        str(boundary.get("status") or "")
        if isinstance(boundary, dict)
        else ""
    )
    if (
        boundary_status == "BLOCK"
        or "FINAL_REVIEW_BOUNDARY_SEMANTIC_BLOCKED" in reason_codes
    ):
        return (
            "content_boundary",
            "final_review_boundary_semantic",
            False,
            evidence,
        )
    if boundary_status != "PASS":
        return "final_review_contract", "final_review_contract", False, evidence
    finding_count = evidence.get("validated_finding_count")
    if (
        isinstance(finding_count, int)
        and not isinstance(finding_count, bool)
        and finding_count > 0
    ) or "FINAL_REVIEW_UNRESOLVED_FINDINGS" in reason_codes:
        carryover = evidence.get("carryover")
        if (
            isinstance(carryover, Mapping)
            and carryover.get("retry_ready") is True
        ):
            return (
                "subtitle_authority",
                "final_review_carryover",
                True,
                evidence,
            )
        return "subtitle_authority", "final_review_findings", False, evidence
    return "final_review_contract", "final_review_contract", False, evidence


def _boundary_context_failure_evidence(attempt_output: str) -> dict[str, object]:
    lines = [
        line.strip()
        for line in attempt_output.splitlines()
        if "BOUNDARY_CONTEXT_EXHAUSTED" in line
    ]
    marker = lines[-1] if lines else "BOUNDARY_CONTEXT_EXHAUSTED"
    retry_scope = re.search(r"\bretry_scope=([a-z_]+)", marker)
    legacy_reason_codes = re.search(
        r"\breason_codes=([A-Z0-9_,]+)", marker
    )
    json_reason_codes: list[str] = []
    json_match = re.search(
        r"BOUNDARY_CONTEXT_EXHAUSTED:\s*(\[[^\r\n]*?\])"
        r"(?=\s+(?:max_forward_ms|retry_scope)=|$)",
        marker,
    )
    if json_match is not None:
        try:
            parsed = json.loads(json_match.group(1))
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            json_reason_codes = [
                str(code) for code in parsed if str(code)
            ]
    return {
        "schema_version": "talk-boundary-context-failure-evidence.v1",
        "gate": "BOUNDARY_CONTEXT_EXHAUSTED",
        "retry_scope": retry_scope.group(1) if retry_scope else None,
        "reason_codes": (
            json_reason_codes
            or (
                [
                    code
                    for code in legacy_reason_codes.group(1).split(",")
                    if code
                ]
                if legacy_reason_codes
                else []
            )
        ),
        "marker": re.sub(r"/[^\s:'\"]+", "<path>", marker)[:1200],
    }


def _normalize_failure_fingerprint_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _normalize_failure_fingerprint_value(nested)
            for key, nested in value.items()
            if key != "surface_files"
        }
    if isinstance(value, list):
        return [_normalize_failure_fingerprint_value(item) for item in value]
    if isinstance(value, str):
        normalized = re.sub(r"/[^\s:'\"]+", "<path>", value)
        return re.sub(r"\b\d{8,}\b", "<n>", normalized)
    return value


def classify_talk_failure(attempt_output: str) -> dict:
    """Persist a stable failure identity instead of a bare generic status."""

    tail = attempt_output[-8000:]
    nonempty = [line.strip() for line in tail.splitlines() if line.strip()]
    message = nonempty[-1][:1200] if nonempty else "producer exited without diagnostic"
    failure_evidence: dict[str, object] | None = None
    if "BOUNDARY_RETRY_OWNER_SET_DRIFT" in tail:
        kind, stage, recoverable = (
            "pipeline_contract",
            "boundary_retry_owner_contract",
            False,
        )
    elif "TALK_EFFECTIVE_DURATION_NOT_OVER_45S_AFTER_BOUNDARY" in tail:
        kind, stage, recoverable = "content_duration", "boundary_resolution", False
    elif "BOUNDARY_CONTEXT_EXHAUSTED" in tail:
        kind, stage, recoverable = (
            "content_boundary",
            "boundary_semantic_review",
            False,
        )
        failure_evidence = _boundary_context_failure_evidence(tail)
    elif "BOUNDARY_UNREPAIRABLE" in tail:
        kind, stage, recoverable = "content_boundary", "boundary_resolution", False
    elif "voiceprint_profile.v1.json" in tail and (
        "FileNotFoundError" in tail or "No such file" in tail
    ):
        kind, stage, recoverable = "runtime_prerequisite", "speaker_preflight", True
    elif "SOURCE_RECORDING_ROOT_UNAVAILABLE" in tail:
        # 录制 mount 掉了——看门狗恢复后同一候选照常可产出，属基础设施等待。
        kind, stage, recoverable = (
            "runtime_prerequisite",
            "source_media_binding",
            True,
        )
    elif any(
        marker in tail
        for marker in (
            "Transport endpoint is not connected",
            "State not recoverable",
        )
    ):
        # CloudFS/FUSE can disconnect after the date-level preflight but while
        # a producer is reading media.  This is the same recoverable mount
        # outage, not an unknown producer defect with a one-retry lifetime.
        kind, stage, recoverable = (
            "runtime_prerequisite",
            "source_media_binding",
            True,
        )
    elif "SOURCE_MEDIA_MISSING" in tail:
        # 选片后源录像消失（mount 仍健康）：字节已不可得，重试永远失败。
        # 标成可恢复会让 runner 误判为外部故障并中断当批，饿死健康场次的候选。
        kind, stage, recoverable = "source_media", "source_media_binding", False
    elif "SPEAKER_REVIEW_REQUIRED" in tail:
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif _runner._speaker_evidence_insufficient_failure(tail):
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED" in tail:
        failure_evidence = _foreign_source_gate_violation(tail)
        if _foreign_source_provider_transient(failure_evidence):
            kind, stage, recoverable = (
                "provider_transient",
                "foreign_source_audio_witness",
                True,
            )
        else:
            kind, stage, recoverable = (
                "subtitle_authority",
                "foreign_source_transcription",
                False,
            )
    elif "CHAT_AUTHORITY_FINALIZATION_FAILED" in tail:
        kind, stage, recoverable = (
            "subtitle_authority",
            "chat_authority_finalization",
            False,
        )
    elif "CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED" in tail:
        # 打包终验的 chat-authority 最终面校验失败：确定性内容缺陷。
        # 此前落 unknown/recoverable=True 兜底，每个 tick 空转重试
        # ；修复靠 truth/代码波
        # 改变 fingerprint 唤醒，不是无限基础设施重试。
        kind, stage, recoverable = (
            "subtitle_authority",
            "chat_authority_final_artifact",
            False,
        )
    elif "REDELIVERY_SUBTITLE_BASELINE_FAILED" in tail:
        # Reviewed-redelivery text/timing equivalence is a deterministic
        # authority gate.  Classify it explicitly instead of laundering a
        # concrete baseline defect into producer_error/unknown and retrying the
        # identical fingerprint forever.
        kind, stage, recoverable = (
            "subtitle_authority",
            "redelivery_subtitle_baseline",
            False,
        )
    elif "STORY_CONTRACT_INPUT_INVALID" in tail:
        kind, stage, recoverable = (
            "story_contract",
            "subtitle_entity_consistency",
            False,
        )
    elif "STORY_CONTRACT_TITLE_FAILED" in tail:
        kind, stage, recoverable = (
            "story_contract",
            "title_fact_consistency",
            False,
        )
    elif "SOURCE_FACT_REPAIR_EXHAUSTED" in tail:
        kind, stage, recoverable = (
            "story_contract",
            "source_fact_repair",
            False,
        )
    elif "SOURCE_FACT_REVIEW_INFRA_UNRESOLVED" in tail:
        kind, stage, recoverable = (
            "provider_transient",
            "source_fact_review",
            True,
        )
    elif "FINAL_REVIEW_RELEASE_BLOCKED" in tail:
        kind, stage, recoverable, failure_evidence = (
            _classify_final_review_release(tail)
        )
    elif "FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED" in tail:
        # 审片员修复提案因 provider 失败未决——文本本身可修，等 provider
        # AGY 恢复或成功缓存可用后重试即可，不是内容缺陷。
        kind, stage, recoverable = "provider_transient", "final_review_adjudication", True
    elif any(
        marker in tail.upper()
        for marker in ("TOO MANY REQUESTS", "INDIVIDUAL QUOTA REACHED", "TIMED OUT", "TIMEOUT")
    ):
        kind, stage, recoverable = "provider_transient", "external_provider", True
    else:
        kind, stage, recoverable = "producer_error", "unknown", True
    if failure_evidence is not None:
        # Full chat-authority documents can embed run-root paths (for example
        # ledger_path).  Keep their hashes as diagnostics, but derive failure
        # identity only from the normalized substantive audit.
        fingerprint_evidence = _normalize_failure_fingerprint_value(
            failure_evidence
        )
        fingerprint_basis = json.dumps(
            fingerprint_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        fingerprint_basis = re.sub(r"/[^\s:'\"]+", "<path>", message)
        fingerprint_basis = re.sub(r"\b\d{8,}\b", "<n>", fingerprint_basis)
    fingerprint = hashlib.sha256(
        f"{kind}\0{stage}\0{fingerprint_basis}".encode("utf-8")
    ).hexdigest()
    result = {
        "failure_kind": kind,
        "failure_stage": stage,
        "failure_message": message,
        "failure_fingerprint": "sha256:" + fingerprint,
        "failure_recoverable": recoverable,
    }
    if failure_evidence is not None:
        result["failure_evidence"] = failure_evidence
    if "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED" in tail:
        violation = _foreign_source_gate_violation(tail)
        if violation is not None:
            result["gate_violation"] = violation
    return result


def _prepare_talk_filler_plan(item: dict) -> dict[str, object]:
    source_srt_path = (
        Path(str(item["bcut_srt_path"]))
        if item.get("bcut_srt_path")
        else None
    )
    filler_proposals = [
        row
        for row in (item.get("filler_proposals") or [])
        if isinstance(row, dict)
    ]
    reviewed_removals = [
        row
        for row in (item.get("reviewed_filler_removals") or [])
        if isinstance(row, dict)
    ]
    cues = []
    if source_srt_path is not None and source_srt_path.is_file():
        from scripts.run_auto_review_shadow_pipeline import _parse_srt

        cues = _parse_srt(source_srt_path)
    filler_plan = build_talk_filler_plan(
        start_ms=int(item["start_ms"]),
        end_ms=int(item["end_ms"]),
        cues=cues,
        proposals=filler_proposals,
        proposal_srt_sha256=(
            str(item.get("filler_proposal_srt_sha256") or "") or None
        ),
        source_srt_path=source_srt_path,
        reviewed_removals=reviewed_removals,
        merge_gap_removals=[
            row
            for row in (item.get("merge_gap_removals") or [])
            if isinstance(row, dict)
        ],
    )
    automatic_removals = [
        row
        for row in (filler_plan.get("removals") or [])
        if isinstance(row, dict)
        and row.get("authorization_kind") == "automatic"
    ]
    if not automatic_removals:
        return filler_plan

    from src.autoslice.llm_client import LlmConfig, build_llm_call

    verifier = build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                f"bash {_runner.REPO_ROOT}/scripts/llm_via_cpa.sh "
                "{prompt_file} {completion_file} "
                "'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
            ),
            timeout_seconds=600.0,
        )
    )
    global_verification = verify_automatic_filler_plan(
        cues=cues,
        plan=filler_plan,
        llm_call=verifier,
    )
    if global_verification.get("status") == "PASS":
        filler_plan["global_verification"] = global_verification
        return filler_plan

    rejected_plan = filler_plan
    filler_plan = build_talk_filler_plan(
        start_ms=int(item["start_ms"]),
        end_ms=int(item["end_ms"]),
        cues=cues,
        source_srt_path=source_srt_path,
        reviewed_removals=reviewed_removals,
    )
    filler_plan["status"] = (
        "active_reviewed_only_after_global_semantic_veto"
        if filler_plan.get("removals")
        else "contiguous_fallback_global_semantic_veto"
    )
    filler_plan["rejected_proposals"] = [
        *(rejected_plan.get("rejected_proposals") or []),
        *[
            {
                "proposal_id": row.get("proposal_id"),
                "reason_code": "GLOBAL_SEMANTIC_VERIFIER_VETO",
            }
            for row in automatic_removals
        ],
    ]
    filler_plan["global_verification"] = global_verification
    return filler_plan


def _copy_cover_regeneration_receipt(
    item: dict,
    result: dict[str, object],
) -> None:
    for field in (
        "cover_route_regeneration_fingerprint",
        "cover_route_regeneration_attempts",
    ):
        if item.get(field) is not None:
            result[field] = item[field]


def _selection_scorecard_rejection(item: dict) -> dict[str, object] | None:
    lane = str(item.get("lane") or "")
    from src.autoslice.selection_scorecard import (
        selection_calibration_violations,
    )

    calibration_violations = selection_calibration_violations(
        str(item.get("cid") or item.get("candidate_id") or ""),
        item.get("selection_scorecard"),
    )
    if lane not in {"semantic_recall", "semantic_recall_sharded"} or (
        selection_scorecard_is_valid(item.get("selection_scorecard"))
        and not calibration_violations
    ):
        return None
    cid = str(item["cid"])
    result: dict[str, object] = {
        "candidate_id": cid,
        "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"],
        "end_ms": item["end_ms"],
        "hook": item.get("hook", ""),
        "confidence": item.get("confidence"),
        "selection_scorecard": item.get("selection_scorecard"),
        "session_relation_authority": item.get("session_relation_authority"),
        "lane": lane,
        "rc": 0,
        "status": "candidate_rejected",
        "failure_stage": "selection_scorecard_gate",
        "rejection_reason": (
            "selection_scorecard_calibration_failed"
            if calibration_violations
            else "selection_scorecard_missing_or_invalid"
        ),
        "reason_codes": calibration_violations
        or ["SELECTION_SCORECARD_MISSING_OR_INVALID"],
        "failure_evidence": {
            "schema_version": "candidate-gate-violation.v1",
            "gate": "SELECTION_SCORECARD_REQUIRED",
            "lane": lane,
            "reason_code": (
                calibration_violations[0]
                if calibration_violations
                else "SELECTION_SCORECARD_MISSING_OR_INVALID"
            ),
        },
        "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(cid),
    }
    _copy_cover_regeneration_receipt(item, result)
    return result


def _apply_optional_talk_spec_fields(
    spec: dict[str, object],
    item: dict,
) -> None:
    slot = item.get("cover_diversity_slot")
    if (
        isinstance(slot, int)
        and not isinstance(slot, bool)
        and slot >= 0
    ):
        spec["cover_diversity_slot"] = slot
    song_names = item.get("song_name_candidates")
    if song_names:
        # Screen songlist + 点歌 + known-songs evidence for deterministic pin.
        spec["song_name_candidates"] = list(song_names)


def _apply_recovery_authorities_to_talk_spec(
    spec: dict[str, object],
    item: dict,
    *,
    candidate_id: str,
) -> None:
    if item.get("given_end_ms") is not None:
        given_end_ms = item["given_end_ms"]
        if isinstance(given_end_ms, bool) or not isinstance(given_end_ms, int):
            raise ValueError("given_end_ms must be an integer source timestamp")
        spec["given_end_ms"] = given_end_ms
        spec["given_end_authority"] = str(
            item.get("given_end_authority") or ""
        ).strip()
        spec["given_end_mode"] = "semantic_lower_bound"
    authority = item.get("recovery_publication_authority")
    if item.get("given_title") is None and authority is None:
        return
    given_title = item.get("given_title")
    if not isinstance(given_title, str) or not given_title:
        raise ValueError(
            "recovery publication authority requires an exact given_title"
        )
    try:
        authority = validate_recovery_publication_authority(
            authority,
            candidate_id=candidate_id,
            expected_final_title=given_title,
        )
    except RecoveryTitleAuthorityError as exc:
        raise ValueError(
            f"given_title requires verified publication authority: {exc}"
        ) from exc
    if (
        spec.get("given_end_ms") != authority["required_given_end_ms"]
    ):
        raise ValueError(
            "recovery publication authority given_end_ms mismatch"
        )
    spec["given_end_mode"] = authority["boundary_end_mode"]
    spec["given_title"] = given_title
    spec["recovery_publication_authority"] = authority


def _talk_filler_rejection(
    item: dict,
    *,
    candidate_id: str,
    filler_plan: dict,
) -> dict[str, object] | None:
    """Fail closed when a filler plan cannot preserve the selected talk."""

    requested_merge_gaps = [
        row
        for row in (item.get("merge_gap_removals") or [])
        if isinstance(row, dict)
    ]
    if requested_merge_gaps:
        planned_merge_gaps = [
            row
            for row in (filler_plan.get("removals") or [])
            if isinstance(row, dict)
            and row.get("authorization_kind") == "merge_gap"
        ]
        if len(planned_merge_gaps) != len(requested_merge_gaps):
            return {
                "candidate_id": candidate_id,
                "segment": Path(item["segment_path"]).name,
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "hook": item.get("hook", ""),
                "confidence": item.get("confidence"),
                "selection_scorecard": item.get("selection_scorecard"),
                "session_relation_authority": item.get(
                    "session_relation_authority"
                ),
                "lane": item.get("lane", ""),
                "talk_filler_plan": filler_plan,
                "rc": 0,
                "status": "candidate_rejected",
                "rejection_reason": "merge_gap_plan_rejected",
                "reason_codes": ["SAME_TOPIC_MERGE_GAP_PLAN_REJECTED"],
                "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(
                    candidate_id
                ),
            }
    effective_duration_ms = int(filler_plan["effective_duration_ms"])
    if effective_duration_ms > _runner.MIN_TALK_EFFECTIVE_DURATION_MS:
        return None
    return {
        "candidate_id": candidate_id,
        "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"],
        "end_ms": item["end_ms"],
        "effective_duration_ms": effective_duration_ms,
        "hook": item.get("hook", ""),
        "confidence": item.get("confidence"),
        "selection_scorecard": item.get("selection_scorecard"),
        "session_relation_authority": item.get(
            "session_relation_authority"
        ),
        "lane": item.get("lane", ""),
        "talk_filler_plan": filler_plan,
        "rc": 0,
        "status": "candidate_rejected",
        "rejection_reason": "talk_effective_duration_too_short",
        "reason_codes": ["TALK_EFFECTIVE_DURATION_NOT_OVER_45S"],
        "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(
            candidate_id
        ),
    }


def _run_talk_producer_with_boundary_context_retry(
    *,
    date: str,
    item: dict,
    candidate_id: str,
    spec: dict,
    out_root: Path,
    spec_path: Path,
    log_path: Path,
    cmd: list[str],
):
    boundary_context_retries = 0
    retry_owner_contract_sha256: str | None = None

    def run_producer():
        speaker_review_state = _runner._speaker_review_manifest_state(
            out_root / candidate_id
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        attempt_offset = log_path.stat().st_size if log_path.is_file() else 0
        with open(log_path, "a", encoding="utf-8") as sink:
            completed = subprocess.run(
                cmd,
                check=False,
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=5400,
                cwd=str(_runner.REPO_ROOT),
                env=_runner.child_env_for_date(date),
            )
        with open(log_path, "rb") as source:
            source.seek(attempt_offset)
            attempt_output = source.read().decode("utf-8", "replace")
        return completed, attempt_output, speaker_review_state

    completed, attempt_output, speaker_review_state = run_producer()
    first_tail = attempt_output[-4000:]
    failure_evidence = _boundary_context_failure_evidence(first_tail)
    retry_scope = failure_evidence.get("retry_scope")
    should_retry = bool(
        completed.returncode != 0
        and (
            "BOUNDARY_UNREPAIRABLE" in first_tail
            or "BOUNDARY_CONTEXT_EXHAUSTED" in first_tail
        )
        and retry_scope
        in {
            "same_topic_continues",
            "source_witness_reserve",
        }
    )
    if not should_retry:
        return (
            completed,
            attempt_output,
            speaker_review_state,
            boundary_context_retries,
            retry_owner_contract_sha256,
        )

    piece = spec["pieces"][-1]
    scoped_retry_end = _bound_boundary_retry_source_end_ms(
        out_root=out_root,
        candidate_id=candidate_id,
        repair_cap_ms=_runner.BOUNDARY_REPAIR_RETRY_CAP_MS,
    )
    retry_end = scoped_retry_end or piece["end_ms"]
    current_cap = int(spec["boundary_repair_extend_cap_ms"])
    source_context_already_sufficient = bool(
        scoped_retry_end is not None and piece["end_ms"] >= scoped_retry_end
    )
    source_context_reachable = bool(
        scoped_retry_end is not None
        and (
            not item["seg_dur_ms"]
            or scoped_retry_end <= item["seg_dur_ms"]
        )
    )
    # The witness-reserve requirement can spill past this segment's own
    # recording file near a segment-rotation boundary. Rather than fail
    # closed immediately, look for a wall-clock-continuous next segment: if
    # one is proven, widen this piece to its own segment ceiling and append a
    # reserve piece from the next segment's head to cover the remainder.
    cross_segment_reserve: dict[str, object] | None = None
    if (
        scoped_retry_end is not None
        and not source_context_reachable
        and isinstance(item.get("seg_dur_ms"), int)
        and not isinstance(item.get("seg_dur_ms"), bool)
        and item["seg_dur_ms"] > 0
        and scoped_retry_end > item["seg_dur_ms"]
    ):
        cross_segment_reserve = _discover_cross_segment_witness_reserve(
            date=date,
            item=item,
            deficit_ms=scoped_retry_end - item["seg_dur_ms"],
        )
        if cross_segment_reserve is not None:
            source_context_reachable = True
            retry_end = item["seg_dur_ms"]
    retry_allowed = bool(
        source_context_reachable
        and _runner.BOUNDARY_REPAIR_RETRY_CAP_MS > current_cap
        and (
            retry_end > piece["end_ms"]
            or source_context_already_sufficient
            or cross_segment_reserve is not None
        )
    )
    if not retry_allowed:
        return (
            completed,
            attempt_output,
            speaker_review_state,
            boundary_context_retries,
            retry_owner_contract_sha256,
        )
    try:
        frozen_owner_contract = _load_boundary_retry_owner_contract(
            out_root=out_root,
            candidate_id=candidate_id,
        )
    except RuntimeError:
        drift_marker = (
            "BOUNDARY_RETRY_OWNER_SET_DRIFT: missing or invalid "
            "first-attempt frozen owner contract; refusing widened "
            "context retry"
        )
        with open(log_path, "a", encoding="utf-8") as sink:
            sink.write("\n" + drift_marker + "\n")
        attempt_output += "\n" + drift_marker + "\n"
    else:
        retry_owner_contract_sha256 = str(
            frozen_owner_contract["contract_sha256"]
        )
        spec["boundary_retry_frozen_owner_contract"] = frozen_owner_contract
        boundary_context_retries = 1
        piece["end_ms"] = max(piece["end_ms"], retry_end)
        reserve_log_note = ""
        if cross_segment_reserve is not None:
            spec["pieces"].append(cross_segment_reserve["piece"])
            spec["reserve_chat_binding_absent"] = bool(
                cross_segment_reserve["chat_binding_absent"]
            )
            reserve_log_note = (
                " reserve_next_segment="
                f"{cross_segment_reserve['next_segment_name']} "
                f"reserve_gap_ms={cross_segment_reserve['gap_ms']} "
                "reserve_piece_end_ms="
                f"{cross_segment_reserve['piece']['end_ms']} "
                "wallclock_continuity_delta_ms="
                f"{cross_segment_reserve['continuity_delta_ms']} "
                "reserve_chat_binding_absent="
                f"{cross_segment_reserve['chat_binding_absent']}"
            )
        spec["boundary_repair_extend_cap_ms"] = (
            _runner.BOUNDARY_REPAIR_RETRY_CAP_MS
        )
        spec_path.write_text(
            json.dumps(spec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with open(log_path, "a", encoding="utf-8") as sink:
            sink.write(
                "\nBOUNDARY_CONTEXT_RETRY: retaining/widening source "
                f"post-context to {piece['end_ms']}ms "
                "with hash-bound frozen owner set "
                f"{retry_owner_contract_sha256} "
                "and absolute repair cap to "
                f"{_runner.BOUNDARY_REPAIR_RETRY_CAP_MS}ms "
                f"(semantic end remains {item['end_ms']}ms; "
                f"bound_scope_required_end={scoped_retry_end}; "
                f"retry_scope={retry_scope})"
                f"{reserve_log_note}\n"
            )
        completed, attempt_output, speaker_review_state = run_producer()
    return (
        completed,
        attempt_output,
        speaker_review_state,
        boundary_context_retries,
        retry_owner_contract_sha256,
    )


def produce_talk(date: str, item: dict, *, reuse_cover: bool = False) -> dict:
    """Run produce_slice_package for one pending talk item (plain-dict spec).

    Returns the result record; deterministic speaker uncertainty is preserved
    as ``speaker_review_required`` instead of a generic retryable failure.
    A title_failed pick is cleaned up (no delivery with a cid title/cover) and
    retried on a later resume.  ``reuse_cover`` keeps the existing delivered
    cover (subtitle-only re-run) and skips the ~90s AI cover step.
    """
    item = dict(item)
    item["hook"] = canonicalize_relation_summary(
        str(item.get("hook") or ""),
        session_relation_authority=item.get("session_relation_authority"),
    )
    item["selection_scorecard"] = canonicalize_story_scorecard(
        item.get("selection_scorecard"),
        session_relation_authority=item.get("session_relation_authority"),
    )
    cid = item["cid"]
    scorecard_rejection = _selection_scorecard_rejection(item)
    if scorecard_rejection is not None:
        return scorecard_rejection
    filler_plan = _prepare_talk_filler_plan(item)
    # 合并候选的缝隙移除若被 plan 拒绝，绝不回退成整窗连续交付——那会把
    # 缝里的 8 分钟无关内容一起端出去（fail-closed，候选拒绝留审计）。
    filler_rejection = _talk_filler_rejection(
        item,
        candidate_id=cid,
        filler_plan=filler_plan,
    )
    if filler_rejection is not None:
        return filler_rejection
    effective_duration_ms = int(filler_plan["effective_duration_ms"])
    out_root = _runner.BASE / "out" / date
    delivery_name = _runner.safe_name(item.get("hook", ""), cid)
    # A manual lower bound beyond the semantic end shifts the boundary search
    # origin by the same amount, so the first materialized window must carry
    # that extra tail too or every such candidate needlessly burns a widened
    # full re-transcription retry.
    given_end_ms = item.get("given_end_ms")
    manual_tail_delta_ms = (
        max(0, int(given_end_ms) - int(item["end_ms"]))
        if isinstance(given_end_ms, int)
        and not isinstance(given_end_ms, bool)
        else 0
    )
    pieces = build_piece_specs(
        item=item,
        plan=filler_plan,
        pre_ms=_runner.PIECE_PRE_MS,
        post_ms=_runner.PIECE_POST_MS + manual_tail_delta_ms,
    )
    spec = {
        "candidate_id": cid,
        "date": date,
        "human_truth_mode": _runner.human_truth_mode(),
        "output_root": str(out_root),
        "delivery_name": delivery_name,
        "selection_hook": item.get("hook", ""),
        "selection_scorecard": item.get("selection_scorecard"),
        "session_relation_authority": item.get("session_relation_authority"),
        "lead_pad_ms": 400,
        "semantic_start_ms": item["start_ms"],
        "semantic_end_ms": item["end_ms"],
        "minimum_effective_duration_ms": _runner.MIN_TALK_EFFECTIVE_DURATION_MS,
        "boundary_repair_extend_cap_ms": _runner.BOUNDARY_REPAIR_INITIAL_CAP_MS,
        "semantic_tail_trim_cap_ms": SEMANTIC_TAIL_TRIM_MAX_MS,
        "pieces": pieces,
        "talk_filler_plan": filler_plan,
    }
    _apply_recovery_authorities_to_talk_spec(
        spec, item, candidate_id=cid
    )
    _apply_optional_talk_spec_fields(spec, item)
    candidate_text_override = _runner.candidate_text_override_path(cid)
    if candidate_text_override is not None:
        spec["subtitle_text_overrides"] = str(candidate_text_override)
    candidate_subtitle_regression = _runner.candidate_subtitle_regression_path(cid)
    if candidate_subtitle_regression is not None:
        spec["subtitle_regression"] = str(candidate_subtitle_regression)
    if (reviewed := _runner.candidate_reviewed_subtitle_baseline(cid)) is not None:
        spec["subtitle_redelivery_baseline"] = reviewed.config
    candidate_speaker_override = _runner.candidate_speaker_override_path(cid)
    if candidate_speaker_override is not None:
        spec["speaker_overrides"] = str(candidate_speaker_override)
    for routing_key in (
        "speaker_routing_claim",
        "speaker_routing_claim_sha256",
        "speaker_routing_candidate",
    ):
        if item.get(routing_key) is not None:
            spec[routing_key] = item[routing_key]
    out_root.mkdir(parents=True, exist_ok=True)
    spec_path = out_root / f"spec_{cid}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    filler_plan_path = out_root / f"{cid}.filler-plan.json"
    filler_plan_path.write_text(
        json.dumps(filler_plan, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    log_path = _runner.BASE / "logs" / f"{date}_{cid}.log"
    _runner.log(
        f"producing {cid} ({effective_duration_ms // 1000}s effective, "
        f"{len(pieces)} piece(s)) from {Path(item['segment_path']).name}"
    )
    cmd = [sys.executable, str(_runner.REPO_ROOT / "scripts" / "produce_slice_package.py"),
           "--spec", str(spec_path), "--ssh-host", "localhost", "--speaker-mode", _runner.SPEAKER_MODE]
    if reuse_cover:
        cmd.append("--reuse-cover")
    (
        completed,
        attempt_output,
        previous_speaker_review_state,
        boundary_context_retries,
        boundary_retry_owner_contract_sha256,
    ) = _run_talk_producer_with_boundary_context_retry(
        date=date,
        item=item,
        candidate_id=cid,
        spec=spec,
        out_root=out_root,
        spec_path=spec_path,
        log_path=log_path,
        cmd=cmd,
    )
    result = {
        "candidate_id": cid, "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"], "end_ms": item["end_ms"],
        "hook": item.get("hook", ""), "confidence": item.get("confidence"),
        "selection_scorecard": item.get("selection_scorecard"),
        "session_relation_authority": item.get("session_relation_authority"),
        "lane": item.get("lane", ""), "rc": completed.returncode, "log": str(log_path),
        "boundary_context_retries": boundary_context_retries,
        "boundary_retry_owner_contract_sha256": (
            boundary_retry_owner_contract_sha256
        ),
        "talk_repair_retry_count": int(item.get("talk_repair_retry_count") or 0),
        "talk_transient_retry_count": int(item.get("talk_transient_retry_count") or 0),
        "selected_repair": bool(item.get("selected_repair")),
        "retry_reason": item.get("retry_reason"),
        # 跳切/微剪计划随 result 持久化：requeue 重建 item 时必须原样恢复，
        # 否则合并候选重试会退化成整窗 sweep（fail-closed 守卫会拒绝，但
        # 那等于白丢一次重试）。
        "filler_proposals": list(item.get("filler_proposals") or []),
        "filler_proposal_srt_sha256": item.get("filler_proposal_srt_sha256"),
        "merge_gap_removals": list(item.get("merge_gap_removals") or []),
        "effective_duration_ms": effective_duration_ms,
        "talk_filler_plan_status": filler_plan.get("status"),
        "talk_filler_removal_count": len(filler_plan.get("removals") or []),
        "talk_filler_plan_path": str(filler_plan_path),
        "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(cid),
    }
    _copy_cover_regeneration_receipt(item, result)
    if item.get("given_end_ms") is not None:
        result["given_end_ms"] = item["given_end_ms"]
        result["given_end_authority"] = item.get("given_end_authority")
    if item.get("given_title") is not None:
        result["given_title"] = item["given_title"]
        result["recovery_publication_authority"] = item.get("recovery_publication_authority")
    if "cover_diversity_slot" in item:
        result["cover_diversity_slot"] = item["cover_diversity_slot"]
    # 复活审计块贯穿 requeue item → 新 pick，缺一环即断链
    if item.get("revivals"):
        result["revivals"] = list(item["revivals"])
    for field, value_type in (("sanctioned_revival_retry", dict), ("final_review_carryover_consumed_fingerprints", list)):
        if isinstance(item.get(field), value_type):
            result[field] = value_type(item[field])
    # Classify only bytes written by this subprocess attempt.  The log is
    # append-only; a stale boundary marker followed by a transient CPA error
    # must not make the new attempt terminal again.
    tail = attempt_output[-4000:]
    result["summary"] = _runner.last_json_block(tail)
    result.update(_runner.read_publish_meta(out_root / cid))
    if completed.returncode != 0:
        result.update(_runner.classify_talk_failure(attempt_output))
        result["failure_recovery_fingerprint"] = _runner.talk_failure_recovery_fingerprint(str(result["failure_kind"]), cid)
        if result["failure_recoverable"]:
            retry_epoch = int(time.time()) + _runner.SONG_INFRA_RETRY_BASE_SECONDS
            result["next_retry_at_epoch"] = retry_epoch
            result["next_retry_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry_epoch)
            )
        if (
            "TITLE_AUTHORITY_UNRESOLVED" in tail
            and str(result.get("title_authority_status") or "").startswith("UNRESOLVED")
        ):
            # The producer now fails before delivery.  Keep cleanup for old
            # partial/stale attempts, then classify this deterministic lane so
            # the bounded title retry policy can act on it.
            delivered = _runner.profile_delivery_root() / date
            for f in delivered.glob(f"{delivery_name}.*"):
                f.unlink(missing_ok=True)
            recuts = out_root / cid / "replacement_recuts"
            if recuts.is_dir():
                import shutil

                shutil.rmtree(recuts, ignore_errors=True)
            result["status"] = "title_failed"
            return result
        if "SPEAKER_REVIEW_REQUIRED" in attempt_output:
            review_meta = _runner.read_speaker_review_meta(
                out_root / cid,
                previous_state=previous_speaker_review_state,
            )
            if review_meta:
                result.update(review_meta)
                result["status"] = "speaker_review_required"
                return result
        if _runner._speaker_evidence_insufficient_failure(tail):
            result["status"] = "speaker_evidence_insufficient"
            return result
        if "TALK_EFFECTIVE_DURATION_NOT_OVER_45S_AFTER_BOUNDARY" in tail:
            result["status"] = "candidate_rejected"
            result["rejection_reason"] = "talk_effective_duration_too_short"
            result["reason_codes"] = [
                "TALK_EFFECTIVE_DURATION_NOT_OVER_45S_AFTER_BOUNDARY"
            ]
            return result
        if "SOURCE_MEDIA_MISSING" in tail:
            # 终态且不复跑：源字节没了，`failed` 会被 requeue 成永久空转。
            result["status"] = "candidate_rejected"
            result["rejection_reason"] = "source_media_missing"
            result["reason_codes"] = ["SOURCE_MEDIA_MISSING"]
            return result
        # Boundary exhaustion is deterministic for this pipeline generation;
        # a later fingerprint change can earn a bounded retry.
        result["status"] = (
            "boundary_unrepairable"
            if (
                "BOUNDARY_UNREPAIRABLE" in tail
                or "BOUNDARY_CONTEXT_EXHAUSTED" in tail
            )
            else "failed"
        )
        return result
    if str(result.get("title_authority_status") or "").startswith("UNRESOLVED"):
        # No delivery with a cid title / cid-text cover — clean and retry later.
        delivered = _runner.profile_delivery_root() / date
        for f in delivered.glob(f"{delivery_name}.*"):
            f.unlink(missing_ok=True)
        recuts = out_root / cid / "replacement_recuts"
        if recuts.is_dir():
            import shutil

            shutil.rmtree(recuts, ignore_errors=True)
        result["status"] = "title_failed"
        return result
    # Boundary self-repair (维护者) replaced quarantine: a delivered
    # clip is clean by construction — red flags either got repaired (trail in
    # boundary_repairs) or the produce exited non-zero above (no delivery).
    summary = result.get("summary") or {}
    result["red_flags"] = list(summary.get("red_flags") or [])
    result["boundary_repairs"] = list(summary.get("boundary_repairs") or [])
    if not _talk_cover_delivery_ready(date, result):
        result["status"] = _runner.TALK_COVER_PENDING_STATUS
        result["cover_integrity_status"] = "INVALID_OR_MISSING_INITIAL_COVER"
        result["cover_pending_reason_codes"] = [
            "TALK_DELIVERY_COVER_PROOF_REQUIRED"
        ]
        return result
    result["status"] = "review_ready"
    return result
