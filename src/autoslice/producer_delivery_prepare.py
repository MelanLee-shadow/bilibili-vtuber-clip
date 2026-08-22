"""Lane-specific private preparation for Talk delivery packages.

This keeps producer finalization focused on media/title/cover proof.  These
functions only enumerate those already-proven files and hand them to the
candidate-private transaction store; they never create a public delivery path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    PreparedDelivery,
    deployment_authority_binding,
    prepare_delivery,
)
from src.autoslice.producer_media import (
    _validated_burned_artifact,
    _validated_burned_ass_artifact,
)
from src.autoslice.shadow_review import _sha256


def _optional(path: Path | None, *, role: str, destination: Path) -> DeliveryArtifact | None:
    if path is None or not path.is_file():
        return None
    return DeliveryArtifact(role, path, destination, "sha256:" + _sha256(path))


def prepare_talk_delivery(
    *,
    spec: Mapping[str, object],
    candidate_id: str,
    record: Mapping[str, object],
    staging: Mapping[str, object],
    record_path: Path,
    subtitle_path: Path,
    speaker_review_srt: Path | None,
    speaker_ass: Path | None,
    speaker_manifest_path: Path | None,
    redelivery_baseline_audit_path: Path | None,
    subtitle_regression_audit_path: Path | None,
    chat_authority_path: Path,
    talk_filler_audit_path: Path | None,
    text_manifest_path: Path | None,
    delivery_root: Path,
) -> PreparedDelivery:
    """Create one private Talk handle from finalization's exact artifacts."""

    output_root = Path(str(spec["output_root"])).resolve(strict=True)
    runtime_root = output_root.parents[1]
    date = str(spec["date"])
    basename = str(spec.get("delivery_name") or candidate_id)
    delivery = delivery_root / date
    generation = staging.get("cover_generation")
    generation = generation if isinstance(generation, Mapping) else {}
    rendered = generation.get("rendered_text_pixels")
    rendered = rendered if isinstance(rendered, Mapping) else {}
    clip_context = Path(str(record["clip_context_path"])) if record.get("clip_context_path") else None
    sources: list[tuple[str, Path | None, str]] = [
        ("video", _validated_burned_artifact(dict(record)), ".mp4"),
        ("subtitle", subtitle_path, ".srt"),
        ("uniform_host_ass", _validated_burned_ass_artifact(dict(record)) if speaker_ass is None else None, ".final-sapphire72.ass"),
        ("speaker_srt", speaker_review_srt, ".speaker.srt"),
        ("speaker_ass", speaker_ass, ".speaker.ass"),
        ("speaker_manifest", speaker_manifest_path, ".speaker.json"),
        ("chat_authority", chat_authority_path, ".chat-authority.json"),
        ("redelivery_baseline", redelivery_baseline_audit_path, ".redelivery-baseline.json"),
        ("subtitle_regression", subtitle_regression_audit_path, ".subtitle-regression.json"),
        ("filler_audit", talk_filler_audit_path, ".filler-audit.json"),
        ("text_finalization", text_manifest_path, ".text-finalization.json"),
        ("clip_context", clip_context, ".clip-context.json"),
        ("cover_title_mask", Path(str(rendered["mask_path"])) if rendered.get("mask_path") else None, ".cover.title-mask.png"),
        ("cover_pre_overlay", Path(str(generation["pre_overlay_path"])) if generation.get("pre_overlay_path") else None, ".cover.pre-overlay.png"),
        ("cover_route_background", Path(str(generation["ai_background"])) if generation.get("ai_background") else None, ".cover.route-background.png"),
        ("publish", Path(str(staging["publish_json_path"])) if staging.get("publish_json_path") else None, ".publish.json"),
        ("record", record_path, ".record.json"),
        ("cover", Path(str(staging["cover_path"])) if staging.get("cover_path") else None, ".cover.png"),
    ]
    artifacts = [
        prepared for role, source, suffix in sources
        if (prepared := _optional(source, role=role, destination=delivery / f"{basename}{suffix}")) is not None
    ]
    return prepare_delivery(
        runtime_root=runtime_root,
        lane="talk",
        candidate_id=candidate_id,
        artifacts=artifacts,
        deployed_authority=deployment_authority_binding(runtime_root),
    )


def talk_delivery_summary(
    *,
    candidate_id: str,
    final_end: int,
    record: Mapping[str, object],
    audit: Mapping[str, object],
    timing_qa: Mapping[str, object],
    staging: Mapping[str, object],
    delivery: Path,
    speaker_review_srt: Path | None,
    speaker_ass: Path | None,
    speaker_manifest: Mapping[str, object] | None,
    speaker_guess: object,
    subtitle_regression_audit: Mapping[str, object] | None,
    redelivery_baseline_audit: Mapping[str, object] | None,
    talk_filler_audit_path: Path | None,
    prepared: PreparedDelivery | None = None,
) -> dict[str, object]:
    """One output projection shared by direct and prepare-only finalization."""

    result: dict[str, object] = {
        "candidate_id": candidate_id,
        "final_end_ms": final_end,
        "duration_ms": int(record["duration_ms"]),
        "closure_sentence": audit["closure_sentence"],
        "boundary_verdict": audit["verdict"],
        "red_flags": list(audit.get("red_flags", [])),
        "boundary_repairs": list(audit.get("boundary_repairs", [])),
        "timing_qa": timing_qa.get("counts"),
        "cover_status": staging.get("cover_status"),
        "title": staging.get("title"),
        "delivery": str(Path(str(delivery) + ".mp4")),
        "subtitle": str(Path(str(delivery) + ".srt")),
        "speaker_subtitle": str(Path(str(delivery) + ".speaker.srt")) if speaker_review_srt else None,
        "speaker_ass": str(Path(str(delivery) + ".speaker.ass")) if speaker_ass else None,
        "speaker_status": speaker_manifest.get("status") if speaker_manifest else "OFF",
        "speaker_guess": getattr(speaker_guess, "summary_digest")(speaker_manifest),
        "subtitle_regression_status": subtitle_regression_audit.get("status") if subtitle_regression_audit is not None else "NOT_CONFIGURED",
        "redelivery_baseline_status": redelivery_baseline_audit.get("status") if redelivery_baseline_audit is not None else "NOT_CONFIGURED",
        "talk_filler_audit": str(Path(str(delivery) + ".filler-audit.json")) if talk_filler_audit_path is not None else None,
    }
    if prepared is not None:
        result["prepared_delivery"] = {
            "manifest_path": str(prepared.manifest_path),
            "prepared_sha256": f"sha256:{prepared.prepared_sha256}",
            "upload_enabled": False,
        }
    return result


def emit_talk_delivery_summary(summary: Mapping[str, object]) -> None:
    print(json.dumps(dict(summary), ensure_ascii=False, indent=2))


def prepare_and_emit_talk_delivery(
    *,
    spec: Mapping[str, object], candidate_id: str, final_end: int,
    audit: Mapping[str, object], timing_qa: Mapping[str, object],
    record: Mapping[str, object], staging: Mapping[str, object], record_path: Path,
    subtitle_path: Path, speaker_review_srt: Path | None, speaker_ass: Path | None,
    speaker_manifest: Mapping[str, object] | None, speaker_manifest_path: Path | None,
    speaker_guess: object, redelivery_baseline_audit_path: Path | None,
    redelivery_baseline_audit: Mapping[str, object] | None,
    subtitle_regression_audit_path: Path | None,
    subtitle_regression_audit: Mapping[str, object] | None,
    chat_authority_path: Path, talk_filler_audit_path: Path | None,
    text_manifest_path: Path | None, delivery_root: Path,
) -> None:
    """Prepare a Talk package and emit the direct-path-equivalent summary."""

    prepared = prepare_talk_delivery(
        spec=spec, candidate_id=candidate_id, record=record, staging=staging,
        record_path=record_path, subtitle_path=subtitle_path,
        speaker_review_srt=speaker_review_srt, speaker_ass=speaker_ass,
        speaker_manifest_path=speaker_manifest_path,
        redelivery_baseline_audit_path=redelivery_baseline_audit_path,
        subtitle_regression_audit_path=subtitle_regression_audit_path,
        chat_authority_path=chat_authority_path,
        talk_filler_audit_path=talk_filler_audit_path,
        text_manifest_path=text_manifest_path, delivery_root=delivery_root,
    )
    delivery = delivery_root / str(spec["date"]) / str(spec.get("delivery_name") or candidate_id)
    emit_talk_delivery_summary(talk_delivery_summary(
        candidate_id=candidate_id, final_end=final_end, record=record,
        audit=audit, timing_qa=timing_qa, staging=staging, delivery=delivery,
        speaker_review_srt=speaker_review_srt, speaker_ass=speaker_ass,
        speaker_manifest=speaker_manifest, speaker_guess=speaker_guess,
        subtitle_regression_audit=subtitle_regression_audit,
        redelivery_baseline_audit=redelivery_baseline_audit,
        talk_filler_audit_path=talk_filler_audit_path, prepared=prepared,
    ))


def prepare_and_emit_talk_delivery_from_finalization(**values: object) -> int:
    """Thin compatibility bridge so finalization need not grow another lane."""

    spec = values["spec"]
    recut = values["recut"]
    speaker = values["speaker"]
    authority = values["authority"]
    staged = values["staged"]
    adapters = values["adapters"]
    prepare_and_emit_talk_delivery(
        spec=spec, candidate_id=str(values["cid"]), final_end=int(values["final_end"]),
        audit=values["audit"], timing_qa=values["timing_qa"],
        record=staged.record, staging=staged.staging, record_path=staged.record_path,
        subtitle_path=recut.subtitle_path, speaker_review_srt=speaker.review_srt,
        speaker_ass=speaker.ass, speaker_manifest=speaker.manifest,
        speaker_manifest_path=speaker.manifest_path, speaker_guess=_speaker_guess_module(),
        redelivery_baseline_audit_path=recut.redelivery_baseline_audit_path,
        redelivery_baseline_audit=recut.redelivery_baseline_audit,
        subtitle_regression_audit_path=authority.subtitle_regression_audit_path,
        subtitle_regression_audit=authority.subtitle_regression_audit,
        chat_authority_path=values["chat_authority_path"],
        talk_filler_audit_path=values.get("talk_filler_audit_path"),
        text_manifest_path=recut.text_manifest_path,
        delivery_root=adapters.delivery_root(),
    )
    return 0


def _speaker_guess_module() -> object:
    # Import at call time: producer finalization already owns the concrete
    # speaker implementation and tests patch that seam.
    from src.autoslice import speaker_guess

    return speaker_guess
