"""Talk lane: recall parsing, speaker evidence, failure policy, and production.

Extracted from scripts/free_session_autoslice.py. Runner-owned paths, policy
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
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.selection_scorecard import selection_scorecard_is_valid
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    validate_speaker_review_manifest_document,
)
from src.autoslice.talk_filler import (
    build_piece_specs,
    build_talk_filler_plan,
    verify_automatic_filler_plan,
)


_runner = RunnerProxy()


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
    return cleaned[:18] if cleaned else fallback


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
                # 同主题合并跳切缝隙（event_key 确定性合并，Ivan 2026-07-19）。
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


def _foreign_source_gate_violation(attempt_output: str) -> dict[str, object] | None:
    """Recover the exact cue/token witness that caused a foreign-text gate.

    Older state stored only the final ``FOREIGN_SOURCE...`` exception.  The
    chat-authority artifact already contains the useful evidence, so bind it
    into the terminal candidate record while that immutable attempt still
    exists.  Reporting can then explain a rejection without log archaeology.
    """

    matches = re.findall(
        r"FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED(?:_AFTER_REDELIVERY)?:\s*([^\s]+\.chat-authority\.json)",
        attempt_output,
    )
    if not matches:
        return None
    path = Path(matches[-1])
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    audit = document.get("foreign_script_consistency_audit")
    if not isinstance(audit, dict):
        return None
    findings = audit.get("mixed_cjk_latin_cues")
    if not isinstance(findings, list) or not findings:
        return None
    valid_findings = [row for row in findings if isinstance(row, dict)]
    if not valid_findings:
        return None
    all_witness_rows = [
        row for row in (audit.get("audio_witness_rows") or []) if isinstance(row, dict)
    ]

    def witnesses_for(finding: dict[str, object]) -> list[dict[str, object]]:
        return [
            row
            for row in all_witness_rows
            if row.get("cue_index") == finding.get("cue_index")
        ]

    unresolved = [
        finding
        for finding in valid_findings
        if not any(row.get("witnessed") is True for row in witnesses_for(finding))
    ]
    finding = unresolved[0] if unresolved else valid_findings[0]
    cue_index = finding.get("cue_index")
    witness_rows = witnesses_for(finding)
    witnessed = any(row.get("witnessed") is True for row in witness_rows)
    hashes = {
        "chat_authority_sha256": "sha256:"
        + hashlib.sha256(path.read_bytes()).hexdigest(),
        "input_srt_sha256": document.get("input_srt_sha256"),
        "output_srt_sha256": document.get("output_srt_sha256"),
        "audio_sha256": next(
            (
                "sha256:" + str(row["audio_sha256"]).removeprefix("sha256:")
                for row in witness_rows
                if row.get("audio_sha256")
            ),
            None,
        ),
    }
    return {
        "schema_version": "candidate-gate-violation.v1",
        "gate": "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED",
        "token_class": "MIXED_CJK_MULTIWORD_LATIN",
        "token": " ".join(str(word) for word in finding.get("latin_words") or []),
        "cue_index": cue_index,
        "start_ms": finding.get("start_ms"),
        "end_ms": finding.get("end_ms"),
        "text": finding.get("text"),
        "available_witnesses": ["bounded_audio"] if witness_rows else [],
        "missing_witnesses": [] if witnessed else ["positive_source_audio_transcription"],
        "witness_rows": witness_rows,
        "unresolved_findings": [
            {
                "token": " ".join(
                    str(word) for word in unresolved_finding.get("latin_words") or []
                ),
                "cue_index": unresolved_finding.get("cue_index"),
                "start_ms": unresolved_finding.get("start_ms"),
                "end_ms": unresolved_finding.get("end_ms"),
                "text": unresolved_finding.get("text"),
            }
            for unresolved_finding in unresolved
        ],
        "artifact_hashes": {key: value for key, value in hashes.items() if value},
    }


def classify_talk_failure(attempt_output: str) -> dict:
    """Persist a stable failure identity instead of a bare generic status."""

    tail = attempt_output[-8000:]
    nonempty = [line.strip() for line in tail.splitlines() if line.strip()]
    message = nonempty[-1][:1200] if nonempty else "producer exited without diagnostic"
    if "TALK_EFFECTIVE_DURATION_NOT_OVER_45S_AFTER_BOUNDARY" in tail:
        kind, stage, recoverable = "content_duration", "boundary_resolution", False
    elif "BOUNDARY_UNREPAIRABLE" in tail:
        kind, stage, recoverable = "content_boundary", "boundary_resolution", False
    elif "voiceprint_profile.v1.json" in tail and (
        "FileNotFoundError" in tail or "No such file" in tail
    ):
        kind, stage, recoverable = "runtime_prerequisite", "speaker_preflight", True
    elif "SPEAKER_REVIEW_REQUIRED" in tail:
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif _runner._speaker_evidence_insufficient_failure(tail):
        kind, stage, recoverable = "speaker_evidence", "speaker_finalization", False
    elif "FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED" in tail:
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
    elif "FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED" in tail:
        # 审片员修复提案因 provider 失败未决——文本本身可修，等 provider
        # 恢复（或付费兜底额度）后重试即可，不是内容缺陷。
        kind, stage, recoverable = "provider_transient", "final_review_adjudication", True
    elif any(
        marker in tail.upper()
        for marker in ("TOO MANY REQUESTS", "INDIVIDUAL QUOTA REACHED", "TIMED OUT", "TIMEOUT")
    ):
        kind, stage, recoverable = "provider_transient", "external_provider", True
    else:
        kind, stage, recoverable = "producer_error", "unknown", True
    normalized = re.sub(r"/[^\s:'\"]+", "<path>", message)
    normalized = re.sub(r"\b\d{8,}\b", "<n>", normalized)
    fingerprint = hashlib.sha256(f"{kind}\0{stage}\0{normalized}".encode("utf-8")).hexdigest()
    result = {
        "failure_kind": kind,
        "failure_stage": stage,
        "failure_message": message,
        "failure_fingerprint": "sha256:" + fingerprint,
        "failure_recoverable": recoverable,
    }
    if kind == "subtitle_authority":
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


def _selection_scorecard_rejection(item: dict) -> dict[str, object] | None:
    lane = str(item.get("lane") or "")
    if lane not in {"semantic_recall", "semantic_recall_sharded"} or (
        selection_scorecard_is_valid(item.get("selection_scorecard"))
    ):
        return None
    cid = str(item["cid"])
    return {
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
        "rejection_reason": "selection_scorecard_missing_or_invalid",
        "reason_codes": ["SELECTION_SCORECARD_MISSING_OR_INVALID"],
        "failure_evidence": {
            "schema_version": "candidate-gate-violation.v1",
            "gate": "SELECTION_SCORECARD_REQUIRED",
            "lane": lane,
            "reason_code": "SELECTION_SCORECARD_MISSING_OR_INVALID",
        },
        "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(cid),
    }


def produce_talk(date: str, item: dict, *, reuse_cover: bool = False) -> dict:
    """Run produce_slice_package for one pending talk item (plain-dict spec).

    Returns the result record; deterministic speaker uncertainty is preserved
    as ``speaker_review_required`` instead of a generic retryable failure.
    A title_failed pick is cleaned up (no delivery with a cid title/cover) and
    retried on a later resume.  ``reuse_cover`` keeps the existing delivered
    cover (subtitle-only re-run) and skips the ~90s AI cover step.
    """
    cid = item["cid"]
    scorecard_rejection = _selection_scorecard_rejection(item)
    if scorecard_rejection is not None:
        return scorecard_rejection
    filler_plan = _prepare_talk_filler_plan(item)
    # 合并候选的缝隙移除若被 plan 拒绝，绝不回退成整窗连续交付——那会把
    # 缝里的 8 分钟无关内容一起端出去（fail-closed，候选拒绝留审计）。
    requested_merge_gaps = [
        row for row in (item.get("merge_gap_removals") or []) if isinstance(row, dict)
    ]
    if requested_merge_gaps:
        planned_merge_gaps = [
            row
            for row in (filler_plan.get("removals") or [])
            if isinstance(row, dict) and row.get("authorization_kind") == "merge_gap"
        ]
        if len(planned_merge_gaps) != len(requested_merge_gaps):
            return {
                "candidate_id": cid,
                "segment": Path(item["segment_path"]).name,
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
                "hook": item.get("hook", ""),
                "confidence": item.get("confidence"),
                "selection_scorecard": item.get("selection_scorecard"),
                "session_relation_authority": item.get("session_relation_authority"),
                "lane": item.get("lane", ""),
                "talk_filler_plan": filler_plan,
                "rc": 0,
                "status": "candidate_rejected",
                "rejection_reason": "merge_gap_plan_rejected",
                "reason_codes": ["SAME_TOPIC_MERGE_GAP_PLAN_REJECTED"],
                "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(cid),
            }
    effective_duration_ms = int(filler_plan["effective_duration_ms"])
    if effective_duration_ms <= _runner.MIN_TALK_EFFECTIVE_DURATION_MS:
        return {
            "candidate_id": cid,
            "segment": Path(item["segment_path"]).name,
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "effective_duration_ms": effective_duration_ms,
            "hook": item.get("hook", ""),
            "confidence": item.get("confidence"),
            "selection_scorecard": item.get("selection_scorecard"),
            "session_relation_authority": item.get("session_relation_authority"),
            "lane": item.get("lane", ""),
            "talk_filler_plan": filler_plan,
            "rc": 0,
            "status": "candidate_rejected",
            "rejection_reason": "talk_effective_duration_too_short",
            "reason_codes": ["TALK_EFFECTIVE_DURATION_NOT_OVER_45S"],
            "pipeline_fingerprint": _runner.talk_pipeline_fingerprint(cid),
        }
    out_root = _runner.BASE / "out" / date
    delivery_name = _runner.safe_name(item.get("hook", ""), cid)
    pieces = build_piece_specs(
        item=item,
        plan=filler_plan,
        pre_ms=_runner.PIECE_PRE_MS,
        post_ms=_runner.PIECE_POST_MS,
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
        "given_title": None,
        "lead_pad_ms": 400,
        "semantic_start_ms": item["start_ms"],
        "semantic_end_ms": item["end_ms"],
        "minimum_effective_duration_ms": _runner.MIN_TALK_EFFECTIVE_DURATION_MS,
        "boundary_repair_extend_cap_ms": _runner.BOUNDARY_REPAIR_INITIAL_CAP_MS,
        "pieces": pieces,
        "talk_filler_plan": filler_plan,
    }
    if item.get("given_end_ms") is not None:
        given_end_ms = item["given_end_ms"]
        if isinstance(given_end_ms, bool) or not isinstance(given_end_ms, int):
            raise ValueError("given_end_ms must be an integer source timestamp")
        spec["given_end_ms"] = given_end_ms
        spec["given_end_authority"] = str(
            item.get("given_end_authority") or ""
        ).strip()
    if (
        isinstance(item.get("cover_diversity_slot"), int)
        and not isinstance(item.get("cover_diversity_slot"), bool)
        and int(item["cover_diversity_slot"]) >= 0
    ):
        spec["cover_diversity_slot"] = int(item["cover_diversity_slot"])
    song_name_candidates = item.get("song_name_candidates")
    if song_name_candidates:
        # Machine-evidence song-name pool (screen songlist + 点歌 + known-songs)
        # for the deterministic pin — see src/autoslice/song_name_pin.py.
        spec["song_name_candidates"] = list(song_name_candidates)
    candidate_text_override = _runner.candidate_text_override_path(cid)
    if candidate_text_override is not None:
        spec["subtitle_text_overrides"] = str(candidate_text_override)
    candidate_subtitle_regression = _runner.candidate_subtitle_regression_path(cid)
    if candidate_subtitle_regression is not None:
        spec["subtitle_regression"] = str(candidate_subtitle_regression)
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
    boundary_context_retries = 0

    def run_producer():
        speaker_review_state = _runner._speaker_review_manifest_state(out_root / cid)
        attempt_offset = log_path.stat().st_size if log_path.is_file() else 0
        with open(log_path, "a", encoding="utf-8") as sink:
            completed = subprocess.run(
                cmd, check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=5400,
                cwd=str(_runner.REPO_ROOT), env=_runner.child_env_for_date(date),
            )
        with open(log_path, "rb") as source:
            source.seek(attempt_offset)
            attempt_output = source.read().decode("utf-8", "replace")
        return completed, attempt_output, speaker_review_state

    completed, attempt_output, previous_speaker_review_state = run_producer()
    first_tail = attempt_output[-4000:]
    if (
        completed.returncode != 0
        and "BOUNDARY_UNREPAIRABLE" in first_tail
        and "retry_scope=same_topic_continues" in first_tail
    ):
        piece = spec["pieces"][-1]
        retry_end = (
            min(item["seg_dur_ms"], item["end_ms"] + _runner.BOUNDARY_CONTEXT_RETRY_POST_MS)
            if item["seg_dur_ms"]
            else item["end_ms"] + _runner.BOUNDARY_CONTEXT_RETRY_POST_MS
        )
        current_cap = int(spec["boundary_repair_extend_cap_ms"])
        if retry_end > piece["end_ms"] and _runner.BOUNDARY_REPAIR_RETRY_CAP_MS > current_cap:
            boundary_context_retries = 1
            piece["end_ms"] = retry_end
            spec["boundary_repair_extend_cap_ms"] = _runner.BOUNDARY_REPAIR_RETRY_CAP_MS
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
            with open(log_path, "a", encoding="utf-8") as sink:
                sink.write(
                    f"\nBOUNDARY_CONTEXT_RETRY: widening source post-context to {retry_end}ms "
                    f"and absolute repair cap to {_runner.BOUNDARY_REPAIR_RETRY_CAP_MS}ms "
                    f"(semantic end remains {item['end_ms']}ms)\n"
                )
            completed, attempt_output, previous_speaker_review_state = run_producer()
    result = {
        "candidate_id": cid, "segment": Path(item["segment_path"]).name,
        "start_ms": item["start_ms"], "end_ms": item["end_ms"],
        "hook": item.get("hook", ""), "confidence": item.get("confidence"),
        "selection_scorecard": item.get("selection_scorecard"),
        "session_relation_authority": item.get("session_relation_authority"),
        "lane": item.get("lane", ""), "rc": completed.returncode, "log": str(log_path),
        "boundary_context_retries": boundary_context_retries,
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
    if item.get("given_end_ms") is not None:
        result["given_end_ms"] = item["given_end_ms"]
        result["given_end_authority"] = item.get("given_end_authority")
    if "cover_diversity_slot" in item:
        result["cover_diversity_slot"] = item["cover_diversity_slot"]
    # Classify only bytes written by this subprocess attempt.  The log is
    # append-only; a stale boundary marker followed by a transient CPA error
    # must not make the new attempt terminal again.
    tail = attempt_output[-4000:]
    result["summary"] = _runner.last_json_block(tail)
    result.update(_runner.read_publish_meta(out_root / cid))
    if completed.returncode != 0:
        result.update(_runner.classify_talk_failure(attempt_output))
        result["failure_recovery_fingerprint"] = _runner.talk_failure_recovery_fingerprint(
            str(result["failure_kind"]), cid
        )
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
        # BOUNDARY_UNREPAIRABLE is deterministic for this pipeline generation;
        # a later fingerprint change can earn a bounded retry.
        result["status"] = (
            "boundary_unrepairable" if "BOUNDARY_UNREPAIRABLE" in tail else "failed"
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
    # Boundary self-repair (Ivan 2026-07-10) replaced quarantine: a delivered
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
