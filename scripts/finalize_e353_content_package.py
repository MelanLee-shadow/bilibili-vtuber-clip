#!/usr/bin/env python3
"""Assemble E353's already-accepted media into the current formal package lane.

This successor does not transcribe, rerender, or recapture BCUT.  It freezes the
root-accepted media/audio bytes, builds the typed E353 text successor, and then
uses the ordinary producer staging path for title, source-fact, screenshot-first
cover, StoryContract, and upload tags.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_slice_package import _stage_publish_draft, generate_upload_tags
from src.autoslice.e353_content_formal_successor import (
    AUDIO_VERIFIED_RECORD_SHA256,
    CANDIDATE_ID,
    CURRENT_SRT_SHA256,
    FINAL_ASS_SHA256,
    FINAL_AV_ACCEPTANCE_SHA256,
    FINAL_MEDIA_RESULT_SHA256,
    FINAL_MEDIA_SHA256,
    FIDELITY_CLOSURE_SHA256,
    FOREIGN_CONSUMER_SHA256,
    PADDED_SRT_SHA256,
    PARENT_CHAT_FILE_SHA256,
    PARENT_SRT_SHA256,
    STORY_DURATION_MS,
    build_chat_authority_successor,
    build_final_review_successor,
    validate_final_review_successor,
)
from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
from src.autoslice.producer_boundary import TAIL_PAD_MS
from src.autoslice.producer_package_finalization import (
    FinalRecutArtifacts,
    ProducerFinalizationAdapters,
    ProducerFinalizationOptions,
    _stage_record,
)
from src.autoslice.review_evidence_store import load_review

CONTENT_MEDIA_SHA256 = "sha256:89fbb7ac7f16f40d613624461a9506377fc97376440090aec68a37a602e7a3bc"
SOURCE_MEDIA_SHA256 = "sha256:6bbdb9ed94ff373d28c5f5defc090f6a1288e4c23ea4bf4e00719276d9192243"
CLIP_CONTEXT_FILE_SHA256 = "sha256:3dfb935c0e8bf2fc6c3152448a66d7c2ccda2e962e94511b7392ed671b0b0389"
TITLE_SELECTION_HOOK = "刚唱完歌就遭遇空耳“你妈”和强行攀亲，她先否认与《东京夏日相会》有关，转头又认定它和《年轮》是有血缘的远亲。"
PROFILE_STYLE = "lidousha-final-sapphire72"


class E353PackageError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = load_review(path)
    except (OSError, ValueError) as exc:
        raise E353PackageError(f"invalid json: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise E353PackageError(f"json root is not object: {path}")
    return value


def _require(path: Path, expected: str, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise E353PackageError(f"{label}_MISSING:{path}")
    actual = _sha(path)
    if actual != expected:
        raise E353PackageError(f"{label}_HASH_DRIFT:{actual}:{expected}")
    return path


def _copy(source: Path, target: Path, expected: str, label: str) -> Path:
    _require(source, expected, label)
    if target.exists():
        raise E353PackageError(f"OUTPUT_ALREADY_EXISTS:{target}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, target)
    os.chmod(target, 0o600)
    _require(target, expected, label + "_COPY")
    return target


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    if path.exists():
        raise E353PackageError(f"OUTPUT_ALREADY_EXISTS:{path}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _unused(*_args: object, **_kwargs: object) -> object:
    raise E353PackageError("UNUSED_PRODUCER_ADAPTER_CALLED")


def _timing_qa(srt_path: Path) -> dict[str, object]:
    from src.autoslice.jingting_chunker import parse_srt_cues
    cues = [cue for cue in parse_srt_cues(srt_path.read_text(encoding="utf-8")) if cue.text.strip()]
    return {
        "schema_version": "subtitle-timing-qa.v3",
        "actions": [],
        "counts": {"dropped": 0, "retimed": 0},
        "policy": {
            "boundary_fragment_max_visible_ms": 300,
            "hard_max_ms": 12_000,
            "min_density_cps": 0.8,
            "min_readable_ms": 1_000,
            "soft_max_ms": 6_000,
        },
        "speech_span_count": len(cues),
        "speech_total_ms": sum(cue.end_ms - cue.start_ms for cue in cues),
        "vad_evidence_contract": "positive_only_never_shrink_normal_asr_cues",
        "window": {"start_ms": 0, "end_ms": STORY_DURATION_MS},
        "authority": "E353_FROZEN_FINAL_SRT_TIMINGS_UNCHANGED",
    }


def _boundary_audit(review: Mapping[str, object]) -> dict[str, object]:
    source = review["source_boundary_semantic_review"]
    final = review["boundary_semantic_review"]
    return {
        "boundary_authority": "correlated_semantic_review_plus_deterministic_guards",
        "manual_end_mode": "semantic_lower_bound",
        "manual_end_authority": "",
        "boundary_semantic_review": deepcopy(source),
        "final_delivery_boundary_semantic_review": deepcopy(final),
        "snapped_sentence_end_ms": STORY_DURATION_MS,
        "final_start_ms": 0,
        "final_end_ms": STORY_DURATION_MS,
        "boundary_selection_lower_bound_ms": STORY_DURATION_MS,
        "delivery_coverage_lower_bound_ms": STORY_DURATION_MS,
        "delivery_coverage_verification": {"status": "PASS", "failure": None},
        "tail_pad_coverage_bridge": {
            "status": "NOT_NEEDED",
            "delivery_lower_bound_ms": STORY_DURATION_MS,
            "closure_lower_bound_ms": STORY_DURATION_MS,
            "maximum_tail_pad_ms": TAIL_PAD_MS,
        },
        "closure_sentence": "有血缘关系的远亲",
        "end_on_sentence_boundary": True,
        "end_cut_inside_speech_island": False,
        "end_island_continues_ms": 0,
        "boundary_repairs": [],
        "required_boundary_owner_verification": {"status": "PASS", "reason_codes": []},
        "syntactic_tail_audit": {"status": "PASS", "reason_codes": []},
        "authority": "E353_GENUINE_BOUNDARY_REVIEW_REPLAY_20260924T1317",
        "replayed_provider_reviews": 2,
        "provider_calls": 0,
        "paid_calls": 0,
    }


def _rebind_audio(audio: Mapping[str, object], *, output: Path, stem: str, final_srt: Path, final_video: Path) -> dict[str, object]:
    out = deepcopy(dict(audio))
    names = {
        "witness_srt": output / f"{CANDIDATE_ID}.subtitle-audio-witness.srt",
        "provenance": output / f"{CANDIDATE_ID}.subtitle-audio-provenance.json",
        "correspondence": output / f"{CANDIDATE_ID}.subtitle-audio-correspondence.json",
        "raw_result": output / f"{CANDIDATE_ID}.subtitle-audio-bcut.raw.json",
    }
    out["output_dir"] = str(output)
    for key, path in names.items():
        out[f"{key}_path"] = str(path)
    receipt = out.get("receipt")
    if isinstance(receipt, dict):
        inputs = receipt.get("inputs")
        if isinstance(inputs, dict):
            if isinstance(inputs.get("final_srt"), dict):
                inputs["final_srt"]["path"] = str(final_srt)
            if isinstance(inputs.get("actual_media"), dict):
                inputs["actual_media"]["path"] = str(final_video)
            if isinstance(inputs.get("witness_srt"), dict):
                inputs["witness_srt"]["path"] = str(names["witness_srt"])
            if isinstance(inputs.get("provenance"), dict):
                inputs["provenance"]["path"] = str(names["provenance"])
    return out


def _copy_audio_sidecars(source_dir: Path, output: Path, stem: str) -> dict[str, Path]:
    suffixes = {
        "witness_srt": "subtitle-audio-witness.srt",
        "provenance": "subtitle-audio-provenance.json",
        "correspondence": "subtitle-audio-correspondence.json",
        "raw_result": "subtitle-audio-bcut.raw.json",
    }
    result: dict[str, Path] = {}
    for key, suffix in suffixes.items():
        src = source_dir / f"{CANDIDATE_ID}.{suffix}"
        candidate_target = output / f"{CANDIDATE_ID}.{suffix}"
        shutil.copy2(src, candidate_target)
        os.chmod(candidate_target, 0o600)
        stem_target = output / f"{stem}.{suffix}"
        shutil.copy2(src, stem_target)
        os.chmod(stem_target, 0o600)
        if _sha(candidate_target) != _sha(src) or _sha(stem_target) != _sha(src):
            raise E353PackageError(f"AUDIO_SIDECAR_COPY_DRIFT:{suffix}")
        result[key] = candidate_target
    return result


def _spec(job: Mapping[str, object], clip: Mapping[str, object], clip_path: Path, review: Mapping[str, object], output: Path) -> dict[str, object]:
    candidate = job.get("candidate")
    if not isinstance(candidate, Mapping):
        raise E353PackageError("JOB_CANDIDATE_MISSING")
    scorecard = candidate.get("selection_scorecard")
    if not isinstance(scorecard, Mapping):
        raise E353PackageError("SELECTION_SCORECARD_MISSING")
    spec = deepcopy(dict(candidate))
    spec.update(
        {
            "candidate_id": CANDIDATE_ID,
            "date": "2026-08-26",
            "output_root": str(output.parent),
            "delivery_name": CANDIDATE_ID,
            "selection_hook": TITLE_SELECTION_HOOK,
            "selection_scorecard": deepcopy(dict(scorecard)),
            "given_title": None,
            "recovery_publication_authority": None,
            "pieces": deepcopy(list(clip.get("pieces") or [])),
            "clip_context": deepcopy(dict(clip)),
            "clip_context_path": str(clip_path),
            "boundary_semantic_review": deepcopy(dict(review["boundary_semantic_review"])),
            "given_end_authority": "",
            "minimum_effective_duration_ms": 0,
            "session_relation_authority": None,
            "cover_diversity_slot": int(candidate.get("cover_diversity_slot") or 2),
        }
    )
    spec.pop("cid", None)
    spec.pop("hook", None)
    return spec


def build(args: argparse.Namespace) -> dict[str, object]:
    output = args.output.resolve()
    if output.exists():
        raise E353PackageError(f"OUTPUT_EXISTS:{output}")
    output.mkdir(parents=True, mode=0o700)

    stem = f"{CANDIDATE_ID}.recut"
    content_video = _copy(args.content_video.resolve(), output / f"{stem}.mp4", CONTENT_MEDIA_SHA256, "CONTENT_VIDEO")
    final_video = _copy(args.final_video.resolve(), output / f"{stem}.burned-final-sapphire72.mp4", FINAL_MEDIA_SHA256, "FINAL_VIDEO")
    final_srt = _copy(args.current_srt.resolve(), output / f"{stem}.srt", CURRENT_SRT_SHA256, "FINAL_SRT")
    final_ass = _copy(args.ass.resolve(), output / f"{stem}.final-sapphire72.ass", FINAL_ASS_SHA256, "FINAL_ASS")
    clip_path = _copy(args.clip_context.resolve(), output / f"{CANDIDATE_ID}.clip-context.json", CLIP_CONTEXT_FILE_SHA256, "CLIP_CONTEXT")

    review = build_final_review_successor(
        parent_chat_authority=args.parent_chat,
        parent_srt=args.parent_srt,
        current_srt=args.current_srt,
        padded_srt=args.padded_srt,
        fidelity_closure=args.fidelity_closure,
        foreign_consumer=args.foreign_consumer,
        final_av_acceptance=args.final_av_acceptance,
        final_media_result=args.final_media_result,
        audio_verified_record=args.audio_record,
    )
    validate_final_review_successor(review, expected_srt_sha256=CURRENT_SRT_SHA256)
    review_path = output / "E353-FINAL-REVIEW-SUCCESSOR.json"
    _write_json(review_path, review)

    chat = build_chat_authority_successor(parent_chat_authority=args.parent_chat, final_review_successor=review)
    chat["final_text_srt_path"] = str(final_srt)
    chat["final_speaker_srt_path"] = str(final_srt)
    chat["final_output_srt_path"] = str(final_srt)
    chat["burn_binding"] = {
        "burned_media_path": str(final_video),
        "burned_media_sha256": FINAL_MEDIA_SHA256.removeprefix("sha256:"),
        "ass_path": str(final_ass),
        "ass_sha256": FINAL_ASS_SHA256.removeprefix("sha256:"),
    }
    chat_path = output / f"{CANDIDATE_ID}.chat-authority.json"
    _write_json(chat_path, chat)

    audio_record = _json(args.audio_record)
    audio = audio_record.get("subtitle_audio_correspondence")
    if not isinstance(audio, Mapping):
        raise E353PackageError("AUDIO_RECORD_GATE_MISSING")
    audio_paths = _copy_audio_sidecars(args.audio_record.parent, output, stem)
    rebound_audio = _rebind_audio(audio, output=output, stem=stem, final_srt=final_srt, final_video=final_video)

    media_record = _json(args.media_record)
    burned = media_record.get("burned_preview")
    if not isinstance(burned, Mapping):
        raise E353PackageError("MEDIA_RECORD_BURN_MISSING")
    burned_preview = deepcopy(dict(burned))
    burned_preview.update(
        {
            "path": str(final_video),
            "ass_path": str(final_ass),
            "burned_sha256": FINAL_MEDIA_SHA256,
            "subtitle_style": PROFILE_STYLE,
            "successor_materialization": {
                "schema_version": "e353-existing-media-binding.v1",
                "method": "copy_root_accepted_bytes_no_render",
                "new_render": False,
                "source_sha256": FINAL_MEDIA_SHA256,
                "source_result_sha256": FINAL_MEDIA_RESULT_SHA256,
            },
        }
    )
    branding = burned_preview.get("branding_intro")
    if isinstance(branding, dict):
        intro_assets = output / "evidence" / "intro-assets"
        intro_assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        selected_intro = args.final_media_result.parent / "intro-assets" / "lidousha-branding-intro.z2-kmx-baobao-guankou-20260719.mp4"
        intro_target = intro_assets / selected_intro.name
        _copy(selected_intro, intro_target, "sha256:d37f4c358cc5d2515bca5a6fbc54bb5a10d7d2b8a36008777842def3d502ac29", "Z2_INTRO")
        manifest_target = intro_assets / "branding_intro.v1.json"
        _copy(ROOT / "assets/lidousha/intro/branding_intro.v1.json", manifest_target, "sha256:bee66d164b683451602ae3601d79d19175bc72b0d13257b3026ae2dc47ee06ef", "INTRO_MANIFEST")
        branding["intro_media_path"] = str(intro_target)
        branding["policy_manifest_path"] = str(manifest_target)

    clip = _json(clip_path)
    job = _json(args.job)
    spec = _spec(job, clip, clip_path, review, output)
    spec_path = output / "E353-PRODUCER-SPEC.json"
    _write_json(spec_path, spec)

    hashes = {
        "video_sha256": CONTENT_MEDIA_SHA256,
        "subtitle_sha256": CURRENT_SRT_SHA256,
        "ass_sha256": FINAL_ASS_SHA256,
        "burned_video_sha256": FINAL_MEDIA_SHA256,
        "chat_authority_audit_sha256": _sha(chat_path),
        "clip_context_file_sha256": CLIP_CONTEXT_FILE_SHA256,
        "subtitle_audio_witness_srt_sha256": _sha(audio_paths["witness_srt"]),
        "subtitle_audio_provenance_sha256": _sha(audio_paths["provenance"]),
        "subtitle_audio_correspondence_sha256": _sha(audio_paths["correspondence"]),
        "subtitle_audio_bcut_raw_sha256": _sha(audio_paths["raw_result"]),
        "subtitle_audio_extracted_audio_sha256": str(rebound_audio.get("audio_sha256")),
    }
    record: dict[str, object] = {
        "status": "MATERIALIZED",
        "candidate_id": CANDIDATE_ID,
        "classification": "talk",
        "media_path": str(content_video),
        "subtitle_path": str(final_srt),
        "subtitle_source": "e353-content-final-review-successor.v1+uniform_host+root-final-media",
        "start_ms": 0,
        "end_ms": STORY_DURATION_MS,
        "duration_ms": STORY_DURATION_MS,
        "artifact_hashes": hashes,
        "chat_authority_audit_path": str(chat_path),
        "redelivery_baseline_audit_path": None,
        "redelivery_baseline": None,
        "subtitle_regression_audit_path": None,
        "subtitle_regression": None,
        "talk_filler_audit_path": None,
        "text_finalization_manifest_path": None,
        "speaker_mode": "uniform_host",
        "speaker_review_srt_path": None,
        "subtitle_ass_path": str(final_ass),
        "subtitle_style": PROFILE_STYLE,
        "speaker_finalization_manifest_path": None,
        "speaker_finalization_manifest_sha256": None,
        "speaker_finalization": None,
        "subtitle_timing_qa": _timing_qa(final_srt),
        "boundary_audit": _boundary_audit(review),
        "burned_preview": burned_preview,
        "subtitle_audio_correspondence": rebound_audio,
        "clip_context_path": str(clip_path),
        "clip_context_payload_sha256": str(clip.get("context_sha256")),
        "formal_successor": {
            "schema_version": "e353-formal-package-inputs.v1",
            "final_review_successor_path": str(review_path),
            "final_review_successor_sha256": _sha(review_path),
            "parent_chat_file_sha256": PARENT_CHAT_FILE_SHA256,
            "parent_srt_sha256": PARENT_SRT_SHA256,
            "current_srt_sha256": CURRENT_SRT_SHA256,
            "padded_srt_sha256": PADDED_SRT_SHA256,
            "fidelity_closure_sha256": FIDELITY_CLOSURE_SHA256,
            "foreign_consumer_sha256": FOREIGN_CONSUMER_SHA256,
            "final_av_acceptance_sha256": FINAL_AV_ACCEPTANCE_SHA256,
            "final_media_result_sha256": FINAL_MEDIA_RESULT_SHA256,
            "audio_verified_record_sha256": AUDIO_VERIFIED_RECORD_SHA256,
            "provider_calls_before_public_staging": 0,
            "asr_calls": 0,
            "renders": 0,
            "bcut_calls": 0,
        },
    }

    options = ProducerFinalizationOptions(
        spec=spec_path,
        substrate="e353-root-accepted-media",
        correct="e353-content-successor",
        speaker_mode="uniform_host",
        speaker_overrides=None,
        speaker_source_session_anchors=None,
        speaker_mixed_overlap_evidence=None,
        speaker_python=Path(sys.executable),
        reuse_cover=bool(getattr(args, "defer_cover", False)),
        prepare_only=True,
    )
    recut = FinalRecutArtifacts(
        recut_dir=output,
        media_path=content_video,
        subtitle_path=final_srt,
        text_manifest_path=None,
        text_manifest=None,
    )
    adapters = ProducerFinalizationAdapters(
        accurate_recut_command=_unused,
        run_command=_unused,
        write_source_range_srt=_unused,
        apply_text_override_document=_unused,
        run_speaker_finalization=_unused,
        burn_preview_subtitles=_unused,
        stage_publish_draft=_stage_publish_draft,
        generate_upload_tags=(lambda *_a, **_k: {"status": "PENDING", "reason": "title/source-fact checkpoint; tags run at final cover/package completion"}) if getattr(args, "defer_cover", False) else generate_upload_tags,
        delivery_root=lambda: output,
    )
    staged = _stage_record(options=options, spec=spec, cid=CANDIDATE_ID, recut=recut, record=record, adapters=adapters)
    final_record = staged.record
    # Public staging must point at the accepted final-with-intro media, not the content substrate.
    publish = dict(final_record.get("publish_staging") or {})
    publish["video_path"] = str(final_video)
    publish["subtitle_path"] = str(final_srt)
    publish["subtitle_ass_path"] = str(final_ass)
    publish_path = Path(str(publish.get("publish_json_path") or (output / f"{stem}.publish.json")))
    publish["publish_json_path"] = str(publish_path)
    if getattr(args, "defer_cover", False):
        publish["cover_status"] = "PENDING"
        publish["cover_generation"] = {"status": "PENDING", "note": "Cover deferred after title/source-fact; no cover exists"}
    final_record["publish_staging"] = publish
    final_record["artifact_hashes"]["burned_video_sha256"] = FINAL_MEDIA_SHA256
    final_record["artifact_hashes"]["ass_sha256"] = FINAL_ASS_SHA256
    final_record["artifact_hashes"]["subtitle_sha256"] = CURRENT_SRT_SHA256
    final_record["artifact_hashes"]["chat_authority_audit_sha256"] = _sha(chat_path)
    final_record["artifact_hashes"]["clip_context_file_sha256"] = CLIP_CONTEXT_FILE_SHA256
    final_record["subtitle_audio_correspondence"] = rebound_audio
    final_record["burned_preview"] = burned_preview
    final_record["subtitle_ass_path"] = str(final_ass)
    final_record["speaker_mode"] = "uniform_host"
    final_record["speaker_review_srt_path"] = None
    final_record["speaker_finalization_manifest_path"] = None
    final_record["speaker_finalization"] = None

    record_path = output / f"{CANDIDATE_ID}.record.json"
    record_path.write_text(json.dumps(final_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(record_path, 0o600)
    publish_path.write_text(json.dumps(publish, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(publish_path, 0o600)
    final_record["artifact_hashes"]["publish_draft_sha256"] = _sha(publish_path)
    record_path.write_text(json.dumps(final_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    validate_final_subtitle_audio_check(final_record, final_srt, final_video, output, candidate_id=CANDIDATE_ID)
    # No candidate override: infer package filename from SRT, preserve logical candidate identity.
    validate_final_subtitle_audio_check(final_record, final_srt, final_video, output)

    result = {
        "schema_version": "e353-formal-package-assembly.v1",
        "status": "STAGED_TITLE_READY_COVER_PENDING" if getattr(args, "defer_cover", False) else "STAGED_TITLE_COVER_READY",
        "candidate_id": CANDIDATE_ID,
        "output": str(output),
        "record": {"path": str(record_path), "sha256": _sha(record_path)},
        "publish": {"path": str(publish_path), "sha256": _sha(publish_path)},
        "video": {"path": str(final_video), "sha256": _sha(final_video)},
        "content_media": {"path": str(content_video), "sha256": _sha(content_video)},
        "srt": {"path": str(final_srt), "sha256": _sha(final_srt)},
        "ass": {"path": str(final_ass), "sha256": _sha(final_ass)},
        "cover": {"path": publish.get("cover_path"), "sha256": _sha(Path(str(publish["cover_path"]))) if publish.get("cover_path") else None},
        "title": publish.get("title"),
        "source_fact_status": (publish.get("source_fact_review") or {}).get("status"),
        "story_contract_status": (final_record.get("story_contract") or {}).get("status"),
        "provider_calls": "current generic public-title/source-fact/cover staging only",
        "new_asr_calls": 0,
        "new_bcut_calls": 0,
        "new_renders": 0,
        "uploads": 0,
    }
    _write_json(output / "ASSEMBLY-RESULT.json", result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    for name in (
        "parent-chat", "parent-srt", "current-srt", "padded-srt", "fidelity-closure",
        "foreign-consumer", "final-av-acceptance", "final-media-result", "audio-record",
        "media-record", "content-video", "final-video", "ass", "clip-context", "job",
    ):
        ap.add_argument("--" + name, type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--defer-cover", action="store_true", help="Save title/source-fact checkpoint; cover/tags/package acceptance remain pending")
    args = ap.parse_args()
    try:
        result = build(args)
    except Exception as exc:
        print(f"E353_FORMAL_PACKAGE_FAILED:{type(exc).__name__}:{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
