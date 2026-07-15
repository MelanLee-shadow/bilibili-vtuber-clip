#!/usr/bin/env python3
"""Produce a finished channel talk-slice package from explicit window specs.

This is the standard "圈中候选→出成品" driver (Ivan 2026-07-04). Its core
contract is the TOPIC-CLOSURE boundary rule: a clip must end where the topic
lands, on a COMPLETE sentence — never mid-sentence, never mid-story.

Flow: remote accurate piece cuts (supports cross-segment stitching) → local
concat → fresh whole-window transcription (glossary+danmaku+screen text) →
sentence-snap the final end to a transcription cue boundary near the semantic
target (fail closed if none) → VAD boundary audit + deterministic red flags →
SELF-REPAIR loop (Ivan 2026-07-10: flags move the cut to the next verifiably
clean sentence end / pull the opening onto the straddled sentence; an
unrepairable boundary fails closed — no quarantine state) → final accurate cut
→ VAD-sanitized text-final subtitles → speaker finalization → colour ASS burn
→ title/cover staging → flat delivery copy to the profile output directory.

Spec JSON:
{
  "candidate_id": "...",
  "date": "2026-07-02",
  "output_root": "reports/.../finals",
  "delivery_name": "买弹幕梗当场拆台",
  "selection_hook": "弹幕让李豆沙表演上下摇……", # selected main event; auto-title must retain it
  "given_title": null,                      # Ivan-given title is verbatim-final
  "lead_pad_ms": 300,
  "pieces": [                                # concatenated in order
    {"remote_media": "<abs path on free>", "start_ms": ..., "end_ms": ...,
     "danmaku_xml_local": "<local path>"}
  ],
  "semantic_end_ms": <absolute ms in the LAST piece's segment timeline>
}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess  # compatibility seam: speaker tests and callers patch this module object
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_auto_review_shadow_pipeline import (
    _accurate_reencode_recut_command,
    _burn_preview_subtitles,
    _sha256,
    _stage_publish_draft,
    _write_source_range_srt,
)
from scripts.run_full_session_selector_cpa_shadow import (
    _build_aggregate_asr_transcriber,
    _build_ssh_agy_transcribe_runner,
)
from scripts.apply_subtitle_text_overrides import apply_document as apply_text_override_document
from scripts.apply_speaker_turn_overrides import SPEAKER_SUBTITLE_STYLE_ID
from scripts.gemini_slice_jingting import (
    approved_timely_terms,
    glossary as _review_glossary,
)
from scripts.suggest_upload_tags import generate_upload_tags
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.chat_authority import (
    reconcile_pending_text_overrides,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.review_evidence import SourceCue
from src.autoslice.speaker_finalizer import (
    finalize_fast_solo_subtitles,
)
from src.autoslice.speaker_session_router import (
    verify_speaker_routing_claim,
    verify_speaker_routing_claim_for_candidate,
)
from src.autoslice.subtitle_timing_qa import sanitize_cue_timing
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces
from src.autoslice.topic_entity_graph import load_topic_entity_graph

CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_file(key: str) -> Path:
    """Resolve a selected-profile asset against this producer's repository."""

    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


def profile_delivery_root() -> Path:
    """Return the selected profile's local delivery root."""

    return CHANNEL_PROFILE.delivery_root_for(ROOT)


def profile_voiceprint_reference_dir() -> Path:
    """Return the selected profile's production voiceprint enrollment root."""

    return (
        Path("/opt/bilive/autoslice/voiceprints")
        / CHANNEL_PROFILE.voiceprint_reference_subdirectory
    )


def _topic_graph_disabled() -> bool:
    return (
        os.environ.get("AUTOSLICE_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
        or os.environ.get("LIDOUSHA_DISABLE_TOPIC_ENTITY_GRAPH") == "1"
    )


def _topic_graph_path() -> Path:
    configured = (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH")
    )
    return Path(configured) if configured else profile_asset_file("topic_entity_graph")


def _topic_graph_expected_sha256() -> str:
    return (
        os.environ.get("AUTOSLICE_TOPIC_ENTITY_GRAPH_SHA256")
        or os.environ.get("LIDOUSHA_TOPIC_ENTITY_GRAPH_SHA256")
        or ""
    )

from src.autoslice.producer_boundary import (
    BOUNDARY_REPAIR_EXTEND_CAP_MAX_MS,
    BOUNDARY_REPAIR_EXTEND_CAP_MS,
    ISLAND_CONTINUES_FLAG_MS,
    LEAD_AIR_MS,
    MAX_BOUNDARY_REPAIRS,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
    tail_requires_forward_extension,
)
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    resolve_producer_boundary,
)


from src.autoslice.producer_chat_input import (
    _load_independent_chat_support_srts,
    _load_superchats,
    _piece_chat_evidence,
)


                             # SC BACKLOG in batches, reading SCs minutes after they
                             # appeared (《想要成为真正的拉拉》SC was read ~4min later),
                             # so "recent" is not enough — content-match picks the right
                             # one out of the backlog, irrelevant ones are ignored.
                               # minutes; giftName is unmasked structured evidence
                               # (see chat_authority._apply_gift_name_repairs), the
                               # masked sender name is not repaired here.


# 7/10 incident: the first plausible closure ended at +25,030ms and the old
# 25,000ms hard edge excluded it before VAD/next-cue cleanliness could even be
# evaluated.  Keep the normal repair bounded to 30s.  A runner retry may raise
# that absolute-from-semantic-target ceiling once to 60s, but only alongside
# more source context and only for deterministic continuation evidence.


from src.autoslice.producer_media import (
    FAST_FRESH_DERIVATION_SCHEMA,
    RECUT_PROVENANCE_SCHEMA,
    _begin_fast_media_transaction,
    _commit_fast_media_transaction,
    _resolved_optional_path,
    _rollback_fast_media_transaction,
    _source_media_sha256,
    _valid_cached_provenance,
    _validate_fast_transaction_outputs,
    _validated_burned_artifact,
    _write_json_atomic,
    ffprobe_duration_ms,
    run,
    _derive_fresh_fast_media as _derive_fresh_fast_media_impl,
)
from src.autoslice.producer_request import (
    load_producer_request,
    parse_producer_args,
)
from src.autoslice.producer_source_media import prepare_source_media


def _derive_fresh_fast_media(
    *,
    host: str,
    media_path: Path,
    claimed_segment_path: str,
    expected_segment_sha256: str,
    final_source_start_ms: int,
    final_source_end_ms: int,
) -> dict[str, object]:
    """Compatibility seam for patched media command adapters."""

    return _derive_fresh_fast_media_impl(
        host=host,
        media_path=media_path,
        claimed_segment_path=claimed_segment_path,
        expected_segment_sha256=expected_segment_sha256,
        final_source_start_ms=final_source_start_ms,
        final_source_end_ms=final_source_end_ms,
        accurate_command_builder=_accurate_reencode_recut_command,
        run_command=run,
        duration_probe=ffprobe_duration_ms,
    )


from src.autoslice.producer_text_finalization import (
    verify_chat_authority_final_surfaces,
)
from src.autoslice.producer_text_pipeline import (
    TextPipelineAdapters,
    run_text_pipeline,
)


def _load_term_boundary_surfaces(spec: dict) -> list[str]:
    """Known-proper-noun surfaces for cross-cue boundary unification.

    Reuses only already-gated loader outputs — the timely-terms snapshot and
    topic_entity_graph paths free_session_autoslice.py substitutes per
    AUTOSLICE_BLIND_TIMELY_TERMS / AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH before
    invoking this script — the same env vars ``approved_timely_terms`` and
    the topic-resolution block below already trust.  No reviewed asset path
    is read directly here.
    """

    surfaces: list[str] = []
    for record in approved_timely_terms():
        surfaces.append(str(record.get("canonical") or ""))
        surfaces.extend(str(value) for value in record.get("readings") or [])
        surfaces.extend(str(value) for value in record.get("aliases") or [])
    if not _topic_graph_disabled():
        graph_path = _topic_graph_path()
        if graph_path.is_file() and not graph_path.is_symlink():
            try:
                graph, _graph_sha = load_topic_entity_graph(
                    graph_path,
                    expected_sha256=_topic_graph_expected_sha256(),
                )
                if dt.datetime.now(dt.timezone.utc) <= dt.datetime.fromisoformat(graph["expires_at"]):
                    # Full graph, not topic-resolved: resolution below scopes
                    # entities using this very transcript as evidence, so it
                    # cannot run before the boundary fix that repairs it.
                    for entity in graph.get("entities") or []:
                        surfaces.append(str(entity.get("canonical_zh") or ""))
                        surfaces.extend(str(value) for value in entity.get("native_names") or [])
                        surfaces.extend(str(value) for value in entity.get("aliases") or [])
            except (OSError, ValueError):
                pass
    return surfaces


from src.autoslice.producer_speaker import (
    SpeakerFinalizationAdapters,
    _default_speaker_mode,
    _rebase_remote_speaker_manifest,
    _write_route_mixed_overlap_evidence,
    run_producer_speaker_finalization as _run_producer_speaker_finalization,
    run_speaker_finalizer,
)


def run_producer_speaker_finalization(**kwargs: object) -> dict:
    """Compatibility boundary preserving the producer's patchable adapters."""

    return _run_producer_speaker_finalization(
        **kwargs,
        adapters=SpeakerFinalizationAdapters(
            verify_route=verify_speaker_routing_claim,
            verify_candidate_route=verify_speaker_routing_claim_for_candidate,
            write_mixed_overlap_evidence=_write_route_mixed_overlap_evidence,
            begin_transaction=_begin_fast_media_transaction,
            derive_fresh_media=_derive_fresh_fast_media,
            finalize_fast=finalize_fast_solo_subtitles,
            validate_transaction=_validate_fast_transaction_outputs,
            commit_transaction=_commit_fast_media_transaction,
            rollback_transaction=_rollback_fast_media_transaction,
            run_binary_finalizer=run_speaker_finalizer,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_producer_args(
        argv,
        description=__doc__ or "",
        speaker_display_name=CHANNEL_PROFILE.display_name,
        default_speaker_mode=_default_speaker_mode(),
    )
    request = load_producer_request(
        args,
        repo_root=ROOT,
        profile_asset_file=profile_asset_file,
    )
    spec = request.spec
    boundary_repair_extend_cap_ms = request.boundary_repair_extend_cap_ms
    branding_intro = request.branding_intro
    cid = request.cid
    out_root = request.out_root
    host = request.host
    text_override_path = request.text_override_path
    subtitle_regression_path = request.subtitle_regression_path

    # 1. Remote accurate piece cuts (production encode params), pull local.
    source_media = prepare_source_media(
        spec=spec,
        cid=cid,
        out_root=out_root,
        host=host,
    )
    durations = source_media.durations
    padded = source_media.padded
    padded_dur = source_media.padded_duration_ms
    padded_provenance_path = source_media.padded_provenance_path
    piece_provenance_rows = source_media.piece_provenance_rows

    # 2. Danmaku + on-screen SUPER_CHATs merged onto the concat timeline.
    text_result = run_text_pipeline(
        spec=spec,
        durations=durations,
        padded=padded,
        padded_dur=padded_dur,
        host=host,
        text_override_path=text_override_path,
        cid=cid,
        out_root=out_root,
        substrate=args.substrate,
        correct=args.correct,
        screen_text=args.screen_text,
        adapters=TextPipelineAdapters(
            build_aggregate_transcriber=_build_aggregate_asr_transcriber,
            build_agy_transcriber=_build_ssh_agy_transcribe_runner,
            load_term_boundary_surfaces=_load_term_boundary_surfaces,
            profile_asset_file=profile_asset_file,
            review_glossary=_review_glossary,
            topic_graph_disabled=_topic_graph_disabled,
            topic_graph_path=_topic_graph_path,
            topic_graph_expected_sha256=_topic_graph_expected_sha256,
        ),
    )
    transcriber = text_result.transcriber
    spans = text_result.spans
    cues = text_result.cues
    chat_authority_audit = text_result.chat_authority_audit
    chat_authority_path = text_result.chat_authority_path

    # 4a. Sentence-snap the START (the clip must open on a sentence).
    boundary = resolve_producer_boundary(
        spec=spec,
        durations=durations,
        padded=padded,
        padded_dur=padded_dur,
        cid=cid,
        out_root=out_root,
        transcriber=transcriber,
        cues=cues,
        spans=spans,
        boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
        adapters=BoundaryResolutionAdapters(
            accurate_recut_command=_accurate_reencode_recut_command,
            run_command=run,
        ),
    )
    final_start = boundary.final_start
    final_end = boundary.final_end
    audit = boundary.audit
    sanitized = boundary.sanitized_cues
    timing_qa = boundary.timing_qa

    # 5. Final accurate cut + VAD-sanitized subtitles rebased to the cut.
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    media_path = recut_dir / f"{cid}.recut.mp4"
    run(_accurate_reencode_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
    recut_provenance_path = media_path.with_suffix(".provenance.json")
    _write_json_atomic(
        recut_provenance_path,
        {
            "schema_version": RECUT_PROVENANCE_SCHEMA,
            "source_piece": piece_provenance_rows[0] if len(piece_provenance_rows) == 1 else None,
            "padded": json.loads(padded_provenance_path.read_text(encoding="utf-8")),
            "final_recut": {
                "source_path": str(padded.resolve()),
                "source_sha256": _sha256(padded),
                "start_ms": final_start,
                "end_ms": final_end,
                "absolute_source_start_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_start
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "absolute_source_end_ms": (
                    int(spec["pieces"][0]["start_ms"]) + final_end
                    if len(spec["pieces"]) == 1
                    else None
                ),
                "output_path": str(media_path.resolve()),
                "output_sha256": _sha256(media_path),
            },
        },
    )
    subtitle_path = media_path.with_suffix(".srt")
    text_manifest_path: Path | None = None
    text_manifest: dict | None = None
    if text_override_path is not None:
        automatic_text_path = media_path.with_suffix(".automatic-text.srt")
        _write_source_range_srt(sanitized, final_start, final_end, automatic_text_path)
        text_manifest_path = media_path.with_suffix(".text-finalization.json")
        text_manifest = apply_text_override_document(
            automatic_text_path, text_override_path, subtitle_path, text_manifest_path
        )
    else:
        _write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    (recut_dir / f"{cid}.recut.timing_qa.json").write_text(
        json.dumps(timing_qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    speaker_manifest: dict | None = None
    speaker_review_srt: Path | None = None
    speaker_ass: Path | None = None
    speaker_manifest_path: Path | None = None
    if args.speaker_mode in {"required", "auto"}:
        speaker_review_srt = media_path.with_suffix(".speaker-final.srt")
        speaker_ass = media_path.with_suffix(".speaker-final.ass")
        speaker_manifest_path = media_path.with_suffix(".speaker-final.json")
        speaker_override_path = args.speaker_overrides or _resolved_optional_path(
            spec.get("speaker_overrides"), relative_to=args.spec.parent
        )
        source_session_anchor_path = (
            args.speaker_source_session_anchors
            or _resolved_optional_path(
                spec.get("speaker_source_session_anchors"), relative_to=args.spec.parent
            )
        )
        mixed_overlap_evidence_path = (
            args.speaker_mixed_overlap_evidence
            or _resolved_optional_path(
                spec.get("speaker_mixed_overlap_evidence"),
                relative_to=args.spec.parent,
            )
        )
        speaker_manifest = run_producer_speaker_finalization(
            speaker_mode=args.speaker_mode,
            host=host,
            candidate_id=cid,
            media_path=media_path,
            text_srt_path=subtitle_path,
            output_srt_path=speaker_review_srt,
            output_ass_path=speaker_ass,
            output_manifest_path=speaker_manifest_path,
            work_dir=recut_dir / f"{cid}.speaker-work",
            override_path=speaker_override_path,
            source_session_anchor_path=source_session_anchor_path,
            mixed_overlap_evidence_path=mixed_overlap_evidence_path,
            speaker_python=args.speaker_python,
            spec=spec,
            spec_parent=args.spec.parent,
            final_source_start_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_start
                if len(spec.get("pieces") or []) == 1
                else None
            ),
            final_source_end_ms=(
                int(spec["pieces"][0]["start_ms"]) + final_end
                if len(spec.get("pieces") or []) == 1
                else None
            ),
        )

    # Generative correction, a human override, timing sanitation, or speaker
    # rendering must never silently undo a structured-source lock.  Recheck the
    # exact text against both final subtitle surfaces and bind their hashes into
    # the append-only authority audit before the burn.
    final_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
    final_speaker_text = (
        speaker_review_srt.read_text(encoding="utf-8", errors="replace")
        if speaker_review_srt is not None and speaker_review_srt.is_file()
        else final_text
    )
    pending_override_ok = reconcile_pending_text_overrides(
        chat_authority_audit,
        text_manifest,
        delivery_start_ms=final_start,
    )
    final_authority_ok = pending_override_ok and verify_chat_authority_final_surfaces(
        chat_authority_audit,
        final_text_srt=final_text,
        final_speaker_srt=final_speaker_text,
        delivery_start_ms=final_start,
        delivery_end_ms=final_end,
    )
    chat_authority_audit.update(
        {
            "final_status": "FINAL_ARTIFACTS_VERIFIED" if final_authority_ok else "FINAL_ARTIFACTS_FAILED",
            "final_text_srt_path": str(subtitle_path),
            "final_text_srt_sha256": hashlib.sha256(final_text.encode("utf-8")).hexdigest(),
            "final_speaker_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
            "final_speaker_srt_sha256": hashlib.sha256(final_speaker_text.encode("utf-8")).hexdigest(),
            "speaker_ass_path": str(speaker_ass) if speaker_ass is not None else None,
            "speaker_ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
            "speaker_manifest_sha256": _sha256(speaker_manifest_path) if speaker_manifest_path is not None else None,
        }
    )
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not final_authority_ok:
        raise SystemExit(f"CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED: {chat_authority_path}")

    subtitle_regression_audit_path: Path | None = None
    subtitle_regression_audit: dict | None = None
    if subtitle_regression_path is not None:
        subtitle_regression_audit = verify_subtitle_regression_surfaces(
            subtitle_regression_path,
            candidate_id=cid,
            final_text_srt=final_text,
            final_speaker_srt=final_speaker_text,
        )
        subtitle_regression_audit_path = recut_dir / f"{cid}.subtitle-regression.json"
        subtitle_regression_audit_path.write_text(
            json.dumps(subtitle_regression_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if subtitle_regression_audit["status"] != "PASS":
            raise SystemExit(
                f"SUBTITLE_REGRESSION_FAILED: {subtitle_regression_audit_path}"
            )

    record: dict = {
        "status": "MATERIALIZED",
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": f"{args.substrate}+{args.correct}+pronoun+text_final",
        "start_ms": 0,
        "end_ms": final_end - final_start,
        "duration_ms": final_end - final_start,
        "artifact_hashes": {
            "video_sha256": "sha256:" + _sha256(media_path),
            "subtitle_sha256": "sha256:" + _sha256(subtitle_path),
            **({"ass_sha256": "sha256:" + _sha256(speaker_ass)} if speaker_ass is not None else {}),
            **({"speaker_review_srt_sha256": "sha256:" + _sha256(speaker_review_srt)} if speaker_review_srt is not None else {}),
            "chat_authority_audit_sha256": "sha256:" + _sha256(chat_authority_path),
            **(
                {
                    "subtitle_regression_audit_sha256": "sha256:"
                    + _sha256(subtitle_regression_audit_path)
                }
                if subtitle_regression_audit_path is not None
                else {}
            ),
        },
        "chat_authority_audit_path": str(chat_authority_path),
        "subtitle_regression_audit_path": (
            str(subtitle_regression_audit_path)
            if subtitle_regression_audit_path is not None
            else None
        ),
        "subtitle_regression": subtitle_regression_audit,
        "text_finalization_manifest_path": str(text_manifest_path) if text_manifest_path is not None else None,
        "speaker_mode": args.speaker_mode,
        "speaker_review_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
        "subtitle_ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "subtitle_style": (
            SPEAKER_SUBTITLE_STYLE_ID
            if speaker_ass is not None
            else f"{CHANNEL_PROFILE.profile_id}-final-sapphire72"
        ),
        "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest_path is not None else None,
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest_path is not None else None,
        "speaker_finalization": speaker_manifest,
        "subtitle_timing_qa": timing_qa,
        "boundary_audit": audit,
    }
    record = _burn_preview_subtitles(record, run_ffmpeg=True, branding_intro=branding_intro)
    if not isinstance(record.get("burned_preview"), dict) or record["burned_preview"].get("status") != "BURNED":
        raise SystemExit(f"FINAL_SUBTITLE_BURN_FAILED: {record.get('burned_preview')}")
    burned = _validated_burned_artifact(record)
    chat_authority_audit["burn_binding"] = {
        "burned_media_path": str(burned),
        "burned_media_sha256": _sha256(burned),
        "ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "ass_sha256": _sha256(speaker_ass) if speaker_ass is not None else None,
    }
    chat_authority_path.write_text(
        json.dumps(chat_authority_audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    record["artifact_hashes"]["chat_authority_audit_sha256"] = "sha256:" + _sha256(chat_authority_path)

    # 6. Title (Ivan-given verbatim, else style-asset LLM) + cover + delivery.
    given_title = spec.get("given_title")
    # Title LLM only when no manual title (iron rule: manual titles pass through
    # untouched). Cover art-direction LLM ALWAYS runs (Ivan 2026-07-04): even with
    # a hand-given title the cover still benefits from persona-fit expression /
    # layout / background; it is fail-open, so it never blocks.
    # Per-stage CPA chains (2026-07-10, Ivan): title is a single brand-critical
    # short call → gpt-5.6-sol at high effort; art direction is a structured
    # pick with a known good shape, deterministic fallback and judge guardrails
    # → gpt-5.6-luna at medium (the doc-exact luna lane).  Both fall back
    # 5.5 → 5.4.
    title_llm = None
    if not given_title:
        title_llm = build_llm_call(
            LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-sol gpt-5.5 gpt-5.4' high", timeout_seconds=180.0)
        )
    art_direction_llm = None if args.reuse_cover else build_llm_call(
        LlmConfig(transport="command", command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium", timeout_seconds=180.0)
    )
    final_title_cues = [
        SourceCue(
            f"text_final_{index:04d}", cue.start_ms, cue.end_ms, cue.text.strip(),
            "zh", "speech", 1.0,
        )
        for index, cue in enumerate(parse_srt_cues(subtitle_path.read_text(encoding="utf-8")), start=1)
        if cue.text.strip()
    ]
    record = _stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        skip_cover=args.reuse_cover,
        selection_hook=str(spec.get("selection_hook") or ""),
    )
    staging = record.get("publish_staging") or {}
    # 7. Upload tags (Ivan 2026-07-13): generated at package time against the
    # FINAL title + FINAL delivered subtitles (tag 必须按成品字幕出), frozen
    # into the record so make-manifest picks them up without re-running any
    # model. Fail-safe by contract: generate_upload_tags never raises; a tag
    # failure records status=FAILED and the uploader falls back to base tags.
    if not str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        record["upload_tags"] = generate_upload_tags(
            str(staging.get("title") or given_title or cid), subtitle_path, timeout=180.0
        )
    record_path = recut_dir / f"{cid}.record.json"
    with record_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    if str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        # The standalone producer is also a supported entry point.  Never
        # materialize delivery bytes and rely on the unattended runner to
        # notice and delete them afterward.  The publish/record evidence above
        # remains available for classification and bounded retry.
        raise SystemExit(
            f"TITLE_AUTHORITY_UNRESOLVED: {staging.get('title_authority_error') or 'unknown'}"
        )

    delivery = profile_delivery_root() / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    run(["cp", str(burned), str(delivery / f"{name}.mp4")])
    run(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
    for source, suffix in (
        (speaker_review_srt, ".speaker.srt"),
        (speaker_ass, ".speaker.ass"),
        (speaker_manifest_path, ".speaker.json"),
        (chat_authority_path, ".chat-authority.json"),
        (subtitle_regression_audit_path, ".subtitle-regression.json"),
        (text_manifest_path, ".text-finalization.json"),
        (record_path, ".record.json"),
    ):
        if source is not None and source.is_file():
            run(["cp", str(source), str(delivery / f"{name}{suffix}")])
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        run(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    print(json.dumps(
        {
            "candidate_id": cid,
            "final_end_ms": final_end,
            "closure_sentence": closure_cue.text,
            "boundary_verdict": audit["verdict"],
            "red_flags": red_flags,
            "boundary_repairs": boundary_repairs,
            "timing_qa": timing_qa.get("counts"),
            "cover_status": staging.get("cover_status"),
            "title": staging.get("title"),
            "delivery": str(delivery / f"{name}.mp4"),
            "subtitle": str(delivery / f"{name}.srt"),
            "speaker_subtitle": str(delivery / f"{name}.speaker.srt") if speaker_review_srt else None,
            "speaker_ass": str(delivery / f"{name}.speaker.ass") if speaker_ass else None,
            "speaker_status": speaker_manifest.get("status") if speaker_manifest else "OFF",
            "subtitle_regression_status": (
                subtitle_regression_audit.get("status")
                if subtitle_regression_audit is not None
                else "NOT_CONFIGURED"
            ),
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
