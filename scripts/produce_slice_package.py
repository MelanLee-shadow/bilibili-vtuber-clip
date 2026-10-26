#!/usr/bin/env python3
# brief-contract: writes <spec.output_root>/<candidate_id>/ <profile.delivery_root>/<spec.date>/ <delivery_runtime>/.prepared-deliveries/talk/<candidate_id>/ <native_audio_state.cache_root>/ <native_audio_state.budget_receipt.parent>/ <AUTOSLICE_BASE-or-/opt/bilive/autoslice>/reports/ <cpa_usage_log.parent>/ <external_audio_cache_root>/ local:<tempdir>/ <ssh-host>:/tmp/ <ssh-host>:<LOCAL_AGY_JOB_ROOT>/ <ssh-host>:/opt/bilive/jingting_jobs/ <python.pycache_root>/ ; reads: <spec-path> <runtime-code-lease> ; bindings: native_audio_state=fresh-output-or-runtime-.native-audio-resume,cpa_usage_log=AUTOSLICE_CPA_USAGE_FILE-or-output-default,external_audio_cache_root=profile_external_audio_cache_root(),python.pycache_root=PYTHONPYCACHEPREFIX-or-imported-module-__pycache__ ; modes: bytecode=unless--B-or-PYTHONDONTWRITEBYTECODE,other-paths=selected-mode-only ; logs: <spec.output_root>/<candidate_id>/<candidate_id>.time-ledger.json <cpa_usage_log> stderr ; expects_change: native-audio-budget.revision=nondecreasing
"""Produce a finished channel talk-slice package from explicit window specs.

This is the standard "圈中候选→出成品" driver (维护者). Its core
contract is the TOPIC-CLOSURE boundary rule: a clip must end where the topic
lands, on a COMPLETE sentence — never mid-sentence, never mid-story.

Flow: remote accurate piece cuts (supports cross-segment stitching) → local
concat → fresh whole-window transcription (glossary+danmaku+screen text) →
sentence-snap the final end to a transcription cue boundary near the semantic
target (fail closed if none) → VAD boundary audit + deterministic red flags →
SELF-REPAIR loop (维护者: flags move the cut to the next verifiably
clean sentence end / pull the opening onto the straddled sentence; an
unrepairable boundary fails closed — no quarantine state) → final accurate cut
→ VAD-sanitized text-final subtitles → speaker finalization → colour ASS burn
→ title/cover staging → flat delivery copy to the profile output directory.

Spec JSON:
{
  "candidate_id": "...",
  "date": "",
  "output_root": "reports/.../finals",
  "delivery_name": "买弹幕梗当场拆台",
  "selection_hook": "弹幕让主播表演上下摇……", # selected main event; automatic title facts come only from final SRT
  "selection_scorecard": {...},             # selector 评分卡；边界门的 selector_story_witness 从它
                                            # 派生——手写 spec 也必须如实填一张（null 必被
                                            # WITNESS_INSUFFICIENT 拦），结构见 selection_scorecard.py
  "given_title": null,                      # 维护者-given title is verbatim-final
  "lead_pad_ms": 300,
  "pieces": [                                # concatenated in order
    {"remote_media": "<abs path on recording host>", "start_ms": ..., "end_ms": ...,
     "danmaku_xml_local": "<local path>"}
    # A boundary-review widened-context retry (talk_lane.py) may append one
    # trailing piece tagged {"piece_role": "boundary_witness_reserve"} drawn
    # from a wall-clock-continuous next recording segment. It supplies extra
    # source context only — see src/autoslice/piece_roles.py for how every
    # semantic_end_ms/prior_piece_duration_ms consumer excludes it from the
    # "last piece" anchor while still counting its duration toward available
    # local source context.
  ],
  "semantic_end_ms": <absolute ms in the LAST CONTENT piece's segment timeline>
}
"""

from __future__ import annotations

import datetime as dt
from functools import wraps
import os
import subprocess  # compatibility seam: speaker tests and callers patch this module object
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__" and (ROOT / "DEPLOYED_COMMIT").is_file():
    # Load the stable stdlib bootstrap before importing mutable repository code.
    import runpy as _lease_runpy
    _lease_bootstrap = ROOT.parent / "runtime-code-lease.py"
    _lease_runpy.run_path(str(_lease_bootstrap))["ensure_cli_code_lease"](ROOT, Path(__file__))

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
from src.autoslice.channel_profile import load_channel_profile, profile_binding_from_environment
from src.autoslice.speaker_finalizer import (
    finalize_fast_solo_subtitles,
)
from src.autoslice.speaker_session_router import (
    verify_speaker_routing_claim,
    verify_speaker_routing_claim_for_candidate,
)
from src.autoslice.topic_entity_graph import load_topic_entity_graph
from src.autoslice.producer_nested_caption_observer import build_runtime_caption_observer
from src.autoslice.producer_nested_acoustic import (
    build_runtime_nested_acoustic_observer,
    prepare_final_scene_acoustic_spec,
)
from src.autoslice.producer_nested_source_locator import build_runtime_nested_source_locator
from src.autoslice.runtime_code_lease import repository_runtime_root

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


def profile_external_audio_cache_root() -> Path:
    """Return the standard runtime cache root for official button recordings."""

    runtime_value = (
        os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT")
        or os.environ.get("AUTOSLICE_BASE")
    )
    runtime_root = Path(runtime_value).expanduser() if runtime_value else repository_runtime_root(ROOT)
    return runtime_root / "cache" / "nested-external-audio"


def profile_external_audio_source_map() -> Path | None:
    """Resolve the optional public map only when the profile registered it."""

    if "external_audio_sources" not in CHANNEL_PROFILE.asset_files:
        return None
    return profile_asset_file("external_audio_sources")


def prepare_profile_nested_acoustic_spec(**kwargs) -> tuple[dict, dict]:
    kwargs.setdefault(
        "external_audio_source_map", profile_external_audio_source_map()
    )
    kwargs.setdefault("external_audio_cache_root", profile_external_audio_cache_root())
    return prepare_final_scene_acoustic_spec(
        **kwargs, profile_path=profile_asset_file("voiceprint_profile"),
        reference_dir=profile_voiceprint_reference_dir(),
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
    ISLAND_CONTINUES_FLAG_MS,
    adaptive_tail_cut,
    boundary_audit,
    boundary_red_flags,
    needs_tail_refinement,
    next_clean_closure,
    repair_start_for_straddler,
    snap_end_to_sentence,
    snap_start_to_sentence,
    syntactic_tail_audit,
    tail_requires_forward_extension,
)
from src.autoslice.producer_boundary_resolution import (
    BoundaryResolutionAdapters,
    resolve_producer_boundary,
)
from src.autoslice.producer_boundary_owner_contract import (
    structured_chat_payoff_scope_ms_from_frozen_contract,
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
    _rollback_fast_media_transaction,
    _valid_cached_provenance,
    _validate_fast_transaction_outputs,
    _validated_burned_artifact,
    ffprobe_duration_ms,
    run,
    _derive_fresh_fast_media as _derive_fresh_fast_media_impl,
)
from src.autoslice.producer_package_finalization import (
    ProducerFinalizationAdapters,
    finalize_producer_package,
)
from src.autoslice.producer_finalization_options import finalization_options_from_args
from src.autoslice.existing_final_recut import select_accurate_recut_command
from src.autoslice.existing_subtitle_surface import (
    select_nonaggregate_transcriber_builder,
)
from src.autoslice.producer_request import (
    load_producer_request,
    parse_producer_args,
)
from src.autoslice.producer_source_media import prepare_source_media
from src.autoslice.producer_duration import enforce_minimum_effective_duration
from src.autoslice.producer_stage_log import producer_stage, staged
from src.autoslice.run_time_ledger import producer_run_ledger


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
from src.autoslice.talk_filler import write_final_filler_audit


def _load_term_boundary_surfaces(spec: dict) -> list[str]:
    """Known-proper-noun surfaces for cross-cue boundary unification.

    Starts with shared permanent glossary/referent surfaces, then reuses the
    already-gated timely-terms snapshot and
    topic_entity_graph paths session_autoslice.py substitutes per
    AUTOSLICE_BLIND_TIMELY_TERMS / AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH before
    invoking this script — the same env vars ``approved_timely_terms`` and
    the topic-resolution block below already trust.  No reviewed asset path
    is read directly here.
    """

    # Permanent host names/aliases must not vanish when the timely or topic
    # snapshot is empty. Spelling protection is not acoustic authority.
    from src.autoslice.term_authority import protected_terms

    surfaces: list[str] = sorted(protected_terms())
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


def _restore_usage_environment(call):
    from src.autoslice.cpa_runtime import USAGE_ENV
    @wraps(call)
    def invoke(argv=None):
        previous = os.environ.get(USAGE_ENV)
        try:
            return call(argv)
        finally:
            if previous is None:
                os.environ.pop(USAGE_ENV, None)
            else:
                os.environ[USAGE_ENV] = previous
    return invoke


@_restore_usage_environment
def main(argv: list[str] | None = None) -> int:
    run_started_utc = dt.datetime.now(dt.timezone.utc).isoformat()
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
        selected_channel_profile=CHANNEL_PROFILE,
        trusted_parent_profile_binding=profile_binding_from_environment(),
    )
    with producer_run_ledger(out_root=request.out_root, cid=request.cid, started_utc=run_started_utc):
        recovery = None
        if getattr(args, "resume_prepared", None) is not None:
            from src.autoslice.producer_prepared_recovery import load_prepared_producer_recovery
            recovery = load_prepared_producer_recovery(args=args, request=request)
        if getattr(args, "resume_cover_failure", None) and args.resume_cover_prepare_qc:
            from src.autoslice.producer_prepared_cover_recovery import resume_prepared_cover
            return resume_prepared_cover(args=args, request=request, recovery=recovery,
                                         delivery_root=profile_delivery_root())
        from src.autoslice.production_attempts import observed_production_attempt
        with observed_production_attempt(runtime_root=Path(os.environ.get('AUTOSLICE_BASE','/opt/bilive/autoslice')),
                date=str(request.spec['date']),candidate_id=request.cid,lane='talk',
                trace=Path(os.environ.get('AUTOSLICE_CPA_USAGE_FILE') or request.out_root/f'{request.cid}.cpa-usage.jsonl'),
                inherit_process_attempt=True):
            if getattr(args, "resume_cover_failure", None):
                from src.autoslice.producer_prepared_cover_recovery import resume_prepared_cover
                return resume_prepared_cover(args=args, request=request, recovery=recovery,
                                             delivery_root=profile_delivery_root())
            if recovery is not None:
                return _produce_prepared_request(args, request, recovery)
            return _produce_loaded_request(args,request)


def _produce_prepared_request(args, request, recovery) -> int:
    from src.autoslice.producer_text_pipeline import build_prepared_exact_final_reviewer
    text_adapters = TextPipelineAdapters(
        build_aggregate_transcriber=_build_aggregate_asr_transcriber,
        build_agy_transcriber=_build_ssh_agy_transcribe_runner,
        load_term_boundary_surfaces=_load_term_boundary_surfaces,
        profile_asset_file=profile_asset_file, review_glossary=_review_glossary,
        topic_graph_disabled=_topic_graph_disabled, topic_graph_path=_topic_graph_path,
        topic_graph_expected_sha256=_topic_graph_expected_sha256,
    )
    reviewer = build_prepared_exact_final_reviewer(
        spec=request.spec, out_root=request.out_root, cid=request.cid,
        padded=recovery.padded, host=request.host, authority=recovery.authority,
        completed_record=recovery.completed.record, adapters=text_adapters,
        native_audio_state=recovery.native_audio_state,
    )
    with producer_stage("finalization"):
        return finalize_producer_package(
            options=finalization_options_from_args(args), profile_id=CHANNEL_PROFILE.profile_id,
            speaker_subtitle_style_id=SPEAKER_SUBTITLE_STYLE_ID,
            spec=request.spec, cid=request.cid, out_root=request.out_root, host=request.host,
            padded=recovery.padded, padded_provenance_path=recovery.padded_provenance_path,
            piece_provenance_rows=recovery.piece_rows, final_start=recovery.final_start,
            final_end=recovery.final_end, sanitized=[], timing_qa=recovery.completed.record["subtitle_timing_qa"],
            audit=recovery.completed.record["boundary_audit"], text_override_path=None,
            subtitle_regression_path=request.subtitle_regression_path,
            chat_authority_audit=recovery.authority, chat_authority_path=recovery.authority_path,
            branding_intro=request.branding_intro, talk_filler_audit_path=recovery.filler_path,
            completed=recovery.completed,
            adapters=ProducerFinalizationAdapters(
                accurate_recut_command=_accurate_reencode_recut_command, run_command=run,
                write_source_range_srt=_write_source_range_srt,
                apply_text_override_document=apply_text_override_document,
                run_speaker_finalization=staged("speaker_finalization", run_producer_speaker_finalization),
                burn_preview_subtitles=staged("burn", _burn_preview_subtitles),
                stage_publish_draft=staged("title_cover", _stage_publish_draft),
                generate_upload_tags=staged("upload_tags", generate_upload_tags),
                delivery_root=profile_delivery_root,
                run_exact_final_review=reviewer,
                observe_nested_captions=build_runtime_caption_observer(repository_runtime_root(ROOT)),
                observe_nested_acoustics=build_runtime_nested_acoustic_observer(repository_runtime_root(ROOT)),
                prepare_nested_acoustic_spec=prepare_profile_nested_acoustic_spec,
                locate_nested_source=build_runtime_nested_source_locator(
                    repository_runtime_root(ROOT), repo_root=ROOT,
                    community_config_path=CHANNEL_PROFILE.asset_file("community_name_sources", repo_root=ROOT)),
            ),
        )


def _produce_loaded_request(args,request) -> int:
    spec = request.spec
    boundary_repair_extend_cap_ms = request.boundary_repair_extend_cap_ms
    branding_intro = request.branding_intro
    cid = request.cid
    out_root = request.out_root
    host = request.host
    text_override_path = request.text_override_path
    subtitle_regression_path = request.subtitle_regression_path

    if args.prepare_only:
        from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
        from src.autoslice.producer_delivery_transaction import deployment_authority_binding

        # A private output directory is not the deployed runtime. Fail before
        # cutting media or invoking text providers; finalization rechecks it.
        deployment_authority_binding(resolve_talk_delivery_runtime_root(spec))

    # 1. Remote accurate piece cuts (production encode params), pull local.
    with producer_stage("source_media_pieces_and_padded"):
        source_media = prepare_source_media(
            spec=spec,
            cid=cid,
            out_root=out_root,
            host=host,
            spec_parent=args.spec.parent,
        )
    durations = source_media.durations
    padded = source_media.padded
    padded_dur = source_media.padded_duration_ms
    padded_provenance_path = source_media.padded_provenance_path
    piece_provenance_rows = source_media.piece_provenance_rows
    # 2. Danmaku + on-screen SUPER_CHATs merged onto the concat timeline.
    with producer_stage("text_pipeline"):
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
                # The text owner chooses checkpoint/retained ingress before
                # loading a fresh padded-local transcription substrate.
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
    surface_receipt = spec.get("existing_subtitle_surface_receipt")
    if isinstance(surface_receipt, dict):
        chat_authority_audit["transcription_substrate_receipt"] = dict(surface_receipt)
    # The same immutable context digest must reach subtitle adjudication,
    # StoryContract, title, and cover.  Keep it on the in-memory spec only;
    # the full artifact is already persisted beside the producer evidence.
    spec["clip_context"] = text_result.clip_context
    spec["clip_context_path"] = str(text_result.clip_context_path)

    # The text pipeline already classified and froze structured payoff identity.
    # Media production consumes that exact value; it must not rescan mutable rows.
    required_tail_end_ms = structured_chat_payoff_scope_ms_from_frozen_contract(
        chat_authority_audit.get("frozen_boundary_owner_contract")
    )
    final_review_audit = chat_authority_audit.get("final_review_audit") or {}
    if isinstance(final_review_audit, dict):
        boundary_semantic_review = final_review_audit.get(
            "boundary_semantic_review"
        )
        if isinstance(boundary_semantic_review, dict):
            spec["boundary_semantic_review"] = boundary_semantic_review
    boundary_context_cues = text_result.boundary_context_cues
    with producer_stage("boundary"):
        boundary = resolve_producer_boundary(
            spec=spec,
            durations=durations,
            padded=padded,
            padded_dur=padded_dur,
            cid=cid,
            out_root=out_root,
            transcriber=transcriber,
            cues=boundary_context_cues if boundary_context_cues is not None else cues,
            spans=spans,
            boundary_repair_extend_cap_ms=boundary_repair_extend_cap_ms,
            required_tail_end_ms=required_tail_end_ms,
            adapters=BoundaryResolutionAdapters(
                accurate_recut_command=_accurate_reencode_recut_command,
                run_command=run,
            ),
            chat_authority_audit=chat_authority_audit,
            current_delivery_cues=cues if boundary_context_cues is not None else None,
        )
    resolved_boundary_semantic = boundary.audit.get(
        "boundary_semantic_review"
    )
    correction_review = chat_authority_audit.get("final_review_audit")
    if (
        isinstance(correction_review, dict)
        and isinstance(resolved_boundary_semantic, dict)
    ):
        correction_review["boundary_semantic_review"] = (
            resolved_boundary_semantic
        )
        spec["boundary_semantic_review"] = resolved_boundary_semantic
    enforce_minimum_effective_duration(
        spec,
        final_start_ms=boundary.final_start,
        final_end_ms=boundary.final_end,
    )
    talk_filler_audit_path = write_final_filler_audit(
        spec=spec,
        durations=durations,
        final_start_ms=boundary.final_start,
        final_end_ms=boundary.final_end,
        piece_provenance_rows=piece_provenance_rows,
        branding_intro=branding_intro,
        output_path=out_root / f"{cid}.filler-audit.json",
    )
    # 5. Final accurate cut + VAD-sanitized subtitles rebased to the cut.
    finalization_options = finalization_options_from_args(args)
    with producer_stage("finalization"):
        return finalize_producer_package(
            options=finalization_options,
            profile_id=CHANNEL_PROFILE.profile_id,
            speaker_subtitle_style_id=SPEAKER_SUBTITLE_STYLE_ID,
            spec=spec,
            cid=cid,
            out_root=out_root,
            host=host,
            padded=padded,
            padded_provenance_path=padded_provenance_path,
            piece_provenance_rows=piece_provenance_rows,
            final_start=boundary.final_start,
            final_end=boundary.final_end,
            sanitized=boundary.sanitized_cues,
            timing_qa=boundary.timing_qa,
            audit=boundary.audit,
            text_override_path=text_override_path,
            subtitle_regression_path=subtitle_regression_path,
            chat_authority_audit=chat_authority_audit,
            chat_authority_path=chat_authority_path,
            branding_intro=branding_intro,
            talk_filler_audit_path=talk_filler_audit_path,
            adapters=ProducerFinalizationAdapters(
                accurate_recut_command=select_accurate_recut_command(
                    spec=spec, padded=padded, chat_authority_audit=chat_authority_audit,
                    legacy_command=_accurate_reencode_recut_command,
                ),
                run_command=run,
                write_source_range_srt=_write_source_range_srt,
                apply_text_override_document=apply_text_override_document,
                run_speaker_finalization=staged("speaker_finalization", run_producer_speaker_finalization),
                burn_preview_subtitles=staged("burn", _burn_preview_subtitles),
                stage_publish_draft=staged("title_cover", _stage_publish_draft),
                generate_upload_tags=staged("upload_tags", generate_upload_tags),
                delivery_root=profile_delivery_root,
                run_exact_final_review=text_result.review_exact_final_srt,
                observe_nested_captions=(build_runtime_caption_observer(
                    repository_runtime_root(ROOT) if (repository_runtime_root(ROOT) / "cpa.env").exists() else None)
                    if spec.get("selection_scorecard") is not None else None),
                observe_nested_acoustics=build_runtime_nested_acoustic_observer(
                    repository_runtime_root(ROOT) if (repository_runtime_root(ROOT) / "cpa.env").exists() else None),
                prepare_nested_acoustic_spec=prepare_profile_nested_acoustic_spec,
                locate_nested_source=(build_runtime_nested_source_locator(
                    repository_runtime_root(ROOT) if (repository_runtime_root(ROOT) / "cpa.env").exists() else None,
                    repo_root=ROOT,
                    community_config_path=CHANNEL_PROFILE.asset_file(
                        "community_name_sources", repo_root=ROOT
                    ),
                ) if spec.get("selection_scorecard") is not None else None),
            ),
        )


if __name__ == "__main__":
    raise SystemExit(main())
