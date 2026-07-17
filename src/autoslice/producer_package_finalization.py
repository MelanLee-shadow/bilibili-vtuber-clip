"""Final recut, authority binding, burn, staging, and local delivery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice.chat_authority import (
    reconcile_pending_text_overrides,
    reconcile_reviewed_text_override_conflicts,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.producer_media import (
    RECUT_PROVENANCE_SCHEMA,
    _resolved_optional_path,
    _validated_burned_artifact,
    _write_json_atomic,
)
from src.autoslice.producer_text_finalization import verify_chat_authority_final_surfaces
from src.autoslice.review_evidence import SourceCue
from src.autoslice.shadow_review import _sha256
from src.autoslice.subtitle_regression import verify_subtitle_regression_surfaces


@dataclass(frozen=True)
class ProducerFinalizationOptions:
    spec: Path
    substrate: str
    correct: str
    speaker_mode: str
    speaker_overrides: Path | None
    speaker_source_session_anchors: Path | None
    speaker_mixed_overlap_evidence: Path | None
    speaker_python: Path
    reuse_cover: bool


@dataclass(frozen=True)
class ProducerFinalizationAdapters:
    accurate_recut_command: Callable[..., list[str]]
    run_command: Callable[..., None]
    write_source_range_srt: Callable[..., None]
    apply_text_override_document: Callable[..., dict]
    run_speaker_finalization: Callable[..., dict]
    burn_preview_subtitles: Callable[..., dict]
    stage_publish_draft: Callable[..., dict]
    generate_upload_tags: Callable[..., dict]
    delivery_root: Callable[[], Path]


@dataclass(frozen=True)
class FinalRecutArtifacts:
    recut_dir: Path
    media_path: Path
    subtitle_path: Path
    text_manifest_path: Path | None
    text_manifest: dict | None


@dataclass(frozen=True)
class SpeakerArtifacts:
    manifest: dict | None
    review_srt: Path | None
    ass: Path | None
    manifest_path: Path | None


@dataclass(frozen=True)
class AuthorityArtifacts:
    subtitle_regression_audit_path: Path | None
    subtitle_regression_audit: dict | None


@dataclass(frozen=True)
class StagedRecord:
    record: dict
    staging: dict
    record_path: Path


def _materialize_final_recut(
    *,
    spec: dict,
    cid: str,
    out_root: Path,
    padded: Path,
    padded_provenance_path: Path,
    piece_provenance_rows: list[dict],
    final_start: int,
    final_end: int,
    sanitized: list[SourceCue],
    timing_qa: dict,
    text_override_path: Path | None,
    adapters: ProducerFinalizationAdapters,
) -> FinalRecutArtifacts:
    recut_dir = out_root / "replacement_recuts"
    recut_dir.mkdir(exist_ok=True)
    media_path = recut_dir / f"{cid}.recut.mp4"
    adapters.run_command(adapters.accurate_recut_command(source_video=padded, output_media=media_path, start_ms=final_start, duration_ms=final_end - final_start))
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
        adapters.write_source_range_srt(sanitized, final_start, final_end, automatic_text_path)
        text_manifest_path = media_path.with_suffix(".text-finalization.json")
        text_manifest = adapters.apply_text_override_document(
            automatic_text_path, text_override_path, subtitle_path, text_manifest_path
        )
    else:
        adapters.write_source_range_srt(sanitized, final_start, final_end, subtitle_path)
    (recut_dir / f"{cid}.recut.timing_qa.json").write_text(
        json.dumps(timing_qa, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return FinalRecutArtifacts(
        recut_dir=recut_dir,
        media_path=media_path,
        subtitle_path=subtitle_path,
        text_manifest_path=text_manifest_path,
        text_manifest=text_manifest,
    )

def _finalize_speaker(
    *,
    options: ProducerFinalizationOptions,
    spec: dict,
    host: str,
    cid: str,
    recut: FinalRecutArtifacts,
    final_start: int,
    final_end: int,
    adapters: ProducerFinalizationAdapters,
) -> SpeakerArtifacts:
    recut_dir = recut.recut_dir
    media_path = recut.media_path
    subtitle_path = recut.subtitle_path
    speaker_manifest: dict | None = None
    speaker_review_srt: Path | None = None
    speaker_ass: Path | None = None
    speaker_manifest_path: Path | None = None
    if options.speaker_mode in {"required", "auto"}:
        speaker_review_srt = media_path.with_suffix(".speaker-final.srt")
        speaker_ass = media_path.with_suffix(".speaker-final.ass")
        speaker_manifest_path = media_path.with_suffix(".speaker-final.json")
        speaker_override_path = options.speaker_overrides or _resolved_optional_path(
            spec.get("speaker_overrides"), relative_to=options.spec.parent
        )
        source_session_anchor_path = (
            options.speaker_source_session_anchors
            or _resolved_optional_path(
                spec.get("speaker_source_session_anchors"), relative_to=options.spec.parent
            )
        )
        mixed_overlap_evidence_path = (
            options.speaker_mixed_overlap_evidence
            or _resolved_optional_path(
                spec.get("speaker_mixed_overlap_evidence"),
                relative_to=options.spec.parent,
            )
        )
        speaker_manifest = adapters.run_speaker_finalization(
            speaker_mode=options.speaker_mode,
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
            speaker_python=options.speaker_python,
            spec=spec,
            spec_parent=options.spec.parent,
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
    return SpeakerArtifacts(
        manifest=speaker_manifest,
        review_srt=speaker_review_srt,
        ass=speaker_ass,
        manifest_path=speaker_manifest_path,
    )

def _verify_final_authority(
    *,
    cid: str,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    subtitle_regression_path: Path | None,
) -> AuthorityArtifacts:
    subtitle_path = recut.subtitle_path
    text_manifest = recut.text_manifest
    recut_dir = recut.recut_dir
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
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
    reconcile_reviewed_text_override_conflicts(
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
    return AuthorityArtifacts(
        subtitle_regression_audit_path=subtitle_regression_audit_path,
        subtitle_regression_audit=subtitle_regression_audit,
    )

def _build_and_burn_record(
    *,
    options: ProducerFinalizationOptions,
    profile_id: str,
    speaker_subtitle_style_id: str,
    final_start: int,
    final_end: int,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    authority: AuthorityArtifacts,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    timing_qa: dict,
    audit: dict,
    branding_intro: dict[str, object] | None,
    adapters: ProducerFinalizationAdapters,
) -> dict:
    media_path = recut.media_path
    subtitle_path = recut.subtitle_path
    text_manifest_path = recut.text_manifest_path
    speaker_manifest = speaker.manifest
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
    subtitle_regression_audit_path = authority.subtitle_regression_audit_path
    subtitle_regression_audit = authority.subtitle_regression_audit
    record: dict = {
        "status": "MATERIALIZED",
        "media_path": str(media_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_source": f"{options.substrate}+{options.correct}+pronoun+text_final",
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
        "speaker_mode": options.speaker_mode,
        "speaker_review_srt_path": str(speaker_review_srt) if speaker_review_srt is not None else None,
        "subtitle_ass_path": str(speaker_ass) if speaker_ass is not None else None,
        "subtitle_style": (
            speaker_subtitle_style_id
            if speaker_ass is not None
            else f"{profile_id}-final-sapphire72"
        ),
        "speaker_finalization_manifest_path": str(speaker_manifest_path) if speaker_manifest_path is not None else None,
        "speaker_finalization_manifest_sha256": ("sha256:" + _sha256(speaker_manifest_path)) if speaker_manifest_path is not None else None,
        "speaker_finalization": speaker_manifest,
        "subtitle_timing_qa": timing_qa,
        "boundary_audit": audit,
    }
    record = adapters.burn_preview_subtitles(record, run_ffmpeg=True, branding_intro=branding_intro)
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
    return record

def _stage_record(
    *,
    options: ProducerFinalizationOptions,
    spec: dict,
    cid: str,
    recut: FinalRecutArtifacts,
    record: dict,
    adapters: ProducerFinalizationAdapters,
) -> StagedRecord:
    recut_dir = recut.recut_dir
    subtitle_path = recut.subtitle_path
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
    art_direction_llm = None if options.reuse_cover else build_llm_call(
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
    record = adapters.stage_publish_draft(
        record,
        candidate_id=cid,
        title=given_title or cid,
        cues=final_title_cues,
        run_ffmpeg=True,
        title_llm_call=title_llm,
        art_direction_llm_call=art_direction_llm,
        skip_cover=options.reuse_cover,
        selection_hook=str(spec.get("selection_hook") or ""),
    )
    staging = record.get("publish_staging") or {}
    # 7. Upload tags (Ivan 2026-07-13): generated at package time against the
    # FINAL title + FINAL delivered subtitles (tag 必须按成品字幕出), frozen
    # into the record so make-manifest picks them up without re-running any
    # model. Fail-safe by contract: generate_upload_tags never raises; a tag
    # failure records status=FAILED and the uploader falls back to base tags.
    if not str(staging.get("title_authority_status") or "").startswith("UNRESOLVED"):
        record["upload_tags"] = adapters.generate_upload_tags(
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
    return StagedRecord(record=record, staging=staging, record_path=record_path)

def _deliver_staged_record(
    *,
    spec: dict,
    cid: str,
    final_end: int,
    audit: dict,
    timing_qa: dict,
    recut: FinalRecutArtifacts,
    speaker: SpeakerArtifacts,
    authority: AuthorityArtifacts,
    chat_authority_path: Path,
    staged: StagedRecord,
    adapters: ProducerFinalizationAdapters,
) -> int:
    record = staged.record
    staging = staged.staging
    record_path = staged.record_path
    subtitle_path = recut.subtitle_path
    text_manifest_path = recut.text_manifest_path
    speaker_manifest = speaker.manifest
    speaker_review_srt = speaker.review_srt
    speaker_ass = speaker.ass
    speaker_manifest_path = speaker.manifest_path
    subtitle_regression_audit_path = authority.subtitle_regression_audit_path
    subtitle_regression_audit = authority.subtitle_regression_audit
    delivery = adapters.delivery_root() / spec["date"]
    delivery.mkdir(parents=True, exist_ok=True)
    name = spec.get("delivery_name") or cid
    # Old sapphire renders may coexist in replacement_recuts; copy only the
    # exact hash-bound speaker burn made by this run.
    burned = _validated_burned_artifact(record)
    adapters.run_command(["cp", str(burned), str(delivery / f"{name}.mp4")])
    adapters.run_command(["cp", str(subtitle_path), str(delivery / f"{name}.srt")])
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
            adapters.run_command(["cp", str(source), str(delivery / f"{name}{suffix}")])
    cover = staging.get("cover_path")
    if cover and Path(cover).is_file():
        adapters.run_command(["cp", str(cover), str(delivery / f"{name}.cover.png")])

    print(json.dumps(
        {
            "candidate_id": cid,
            "final_end_ms": final_end,
            "closure_sentence": audit["closure_sentence"],
            "boundary_verdict": audit["verdict"],
            "red_flags": audit.get("red_flags", []),
            "boundary_repairs": audit.get("boundary_repairs", []),
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
def finalize_producer_package(
    *,
    options: ProducerFinalizationOptions,
    profile_id: str,
    speaker_subtitle_style_id: str,
    spec: dict,
    cid: str,
    out_root: Path,
    host: str,
    padded: Path,
    padded_provenance_path: Path,
    piece_provenance_rows: list[dict],
    final_start: int,
    final_end: int,
    sanitized: list[SourceCue],
    timing_qa: dict,
    audit: dict,
    text_override_path: Path | None,
    subtitle_regression_path: Path | None,
    chat_authority_audit: dict,
    chat_authority_path: Path,
    branding_intro: dict[str, object] | None,
    adapters: ProducerFinalizationAdapters,
) -> int:
    recut = _materialize_final_recut(
        spec=spec,
        cid=cid,
        out_root=out_root,
        padded=padded,
        padded_provenance_path=padded_provenance_path,
        piece_provenance_rows=piece_provenance_rows,
        final_start=final_start,
        final_end=final_end,
        sanitized=sanitized,
        timing_qa=timing_qa,
        text_override_path=text_override_path,
        adapters=adapters,
    )
    speaker = _finalize_speaker(
        options=options,
        spec=spec,
        host=host,
        cid=cid,
        recut=recut,
        final_start=final_start,
        final_end=final_end,
        adapters=adapters,
    )
    authority = _verify_final_authority(
        cid=cid,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        speaker=speaker,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        subtitle_regression_path=subtitle_regression_path,
    )
    record = _build_and_burn_record(
        options=options,
        profile_id=profile_id,
        speaker_subtitle_style_id=speaker_subtitle_style_id,
        final_start=final_start,
        final_end=final_end,
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_audit=chat_authority_audit,
        chat_authority_path=chat_authority_path,
        timing_qa=timing_qa,
        audit=audit,
        branding_intro=branding_intro,
        adapters=adapters,
    )
    staged = _stage_record(
        options=options,
        spec=spec,
        cid=cid,
        recut=recut,
        record=record,
        adapters=adapters,
    )
    return _deliver_staged_record(
        spec=spec,
        cid=cid,
        final_end=final_end,
        audit=audit,
        timing_qa=timing_qa,
        recut=recut,
        speaker=speaker,
        authority=authority,
        chat_authority_path=chat_authority_path,
        staged=staged,
        adapters=adapters,
    )
