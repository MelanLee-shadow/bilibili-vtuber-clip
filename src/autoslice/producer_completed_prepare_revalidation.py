"""Provider-free checks before re-preparing an equivalent deployed package."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path

from src.autoslice.producer_delivery_transaction import (
    PreparedDelivery,
    ProducerDeliveryTransactionError,
    _read_document,
    _read_regular_bytes,
)
from src.autoslice.review_evidence_store import review_from_bytes


def validate_completed_prepare_artifacts(handle: PreparedDelivery, result: dict) -> None:
    """Replay current native consumers on the exact original package bytes.

    This does not renew an operator approval or create a quality verdict.
    Missing/currently invalid receipts block; no provider fallback is offered.
    The transaction caller checks every source/staged inode and hash first.
    """
    document = _read_document(handle)
    paths = {row["role"]: Path(row["source_path"]) for row in document["artifacts"]}

    def read(role: str) -> dict:
        if role not in paths:
            raise ProducerDeliveryTransactionError("completed prepare lacks native " + role)
        raw = _read_regular_bytes(paths[role])
        value = (
            review_from_bytes(raw, directory=paths[role].parent)
            if role == "chat_authority"
            else json.loads(raw.decode("utf-8"))
        )
        if not isinstance(value, dict):
            raise ProducerDeliveryTransactionError("completed prepare has invalid " + role)
        return value

    if handle.lane == "song":
        from src.autoslice.song_completion import song_completion_evidence
        from src.autoslice.song_delivery import (
            _validated_song_upload_tags,
            record_is_song,
            song_delivery_artifacts,
        )
        from src.autoslice.song_lane import song_delivery_ok
        from src.autoslice.verified_io import _document_video_hash, _matches_sha256

        summary_path = Path(str(result.get("selector_summary_path") or ""))
        summary_raw = _read_regular_bytes(summary_path)
        if "sha256:" + hashlib.sha256(summary_raw).hexdigest() != result.get(
            "selector_summary_sha256"
        ):
            raise ProducerDeliveryTransactionError("completed Song selector summary bytes drift")
        summary = json.loads(summary_raw)
        records = [
            row
            for row in summary.get("records", [])
            if isinstance(row, dict)
            and row.get("candidate_id") == result.get("selector_record_candidate_id")
        ]
        if len(records) != 1:
            raise ProducerDeliveryTransactionError("completed Song selector record is ambiguous")
        record = records[0]
        active, publish = read("active_record"), read("publish")
        completion = song_completion_evidence(record)
        if not song_delivery_ok(
            int(result.get("rc") or 0),
            record_is_song(record),
            record.get("reason_codes"),
            completion,
        ):
            raise ProducerDeliveryTransactionError(
                "completed Song proof is not currently delivery-ready"
            )
        artifacts = song_delivery_artifacts(record)
        required = {
            "video": (artifacts.get("video_path"), artifacts.get("video_sha256")),
            "subtitle": (artifacts.get("subtitle_path"), artifacts.get("subtitle_sha256")),
            "lyrics_alignment_report": (
                completion.get("alignment_report_path"), completion.get("alignment_report_sha256"),
            ),
            "host_vocal_proof": (
                completion.get("host_vocal_proof_path"), completion.get("host_vocal_proof_sha256"),
            ),
            "recut_manifest": (
                artifacts.get("recut_manifest_path"), artifacts.get("recut_manifest_sha256"),
            ),
        }
        if "cover" in paths:
            required["cover"] = (artifacts.get("cover_path"), artifacts.get("cover_sha256"))
        for role, (original_path, digest) in required.items():
            if (
                role not in paths
                or not isinstance(original_path, str)
                or not isinstance(digest, str)
                or not _matches_sha256(Path(original_path), digest)
                or not _matches_sha256(paths[role], digest)
            ):
                raise ProducerDeliveryTransactionError(
                    "completed Song native proof is not bound to sealed " + role
                )
        staging = active.get("publish_staging")
        video_sha = "sha256:" + hashlib.sha256(_read_regular_bytes(paths["video"])).hexdigest()
        if (
            not isinstance(staging, dict)
            or active.get("delivery_candidate_id") != handle.candidate_id
            or active.get("source_candidate_id") != record.get("candidate_id")
            or publish.get("candidate_id") != record.get("candidate_id")
            or publish.get("schema_version") != "shadow-publish-draft.v1"
            or publish.get("title") != staging.get("title")
            or staging.get("upload_enabled") is not False
            or publish.get("upload_enabled") is not False
            or _document_video_hash(active) != video_sha
            or _document_video_hash(publish) != video_sha
        ):
            raise ProducerDeliveryTransactionError("completed Song active publish binding drifts")
        _validated_song_upload_tags(active.get("upload_tags"))
        # Repeat the exact projection checks used by the ordinary commit.
        from src.autoslice.producer_batch_projection import project_materialized_song
        from copy import deepcopy

        project_materialized_song(
            deepcopy(result),
            runtime_root=handle.runtime_root,
            candidate_id=handle.candidate_id,
            song_status=lambda *_args: "validated",
        )
        return

    from src.autoslice.addressee_attribution import build_addressee_evidence
    from src.autoslice.final_review_contract import validate_final_review_release
    from src.autoslice.final_subtitle_audio_gate import validate_final_subtitle_audio_check
    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.producer_media import (
        _validated_burned_artifact,
        _validated_burned_ass_artifact,
    )
    from src.autoslice.producer_delivery_prepare import _recut_provenance
    from src.autoslice.producer_package_finalization import _audit_story_bound_cover
    from src.autoslice.publish_staging_paths import (
        validate_package_editorial_binding,
        validate_staged_editorial_files,
    )
    from src.autoslice.review_package_source_fact_audit import _source_fact_receipt_valid
    from src.autoslice.shadow_review import _sha256
    from src.autoslice.subtitle_validation import validate_srt_file
    from src.autoslice.publication_title_exception import candidate_title_policy_violations

    record = read("record")
    if (
        record.get("candidate_id") != handle.candidate_id
        or "subtitle" not in paths
        or "video" not in paths
    ):
        raise ProducerDeliveryTransactionError("completed Talk identity is incomplete")
    srt, video = paths["subtitle"], paths["video"]
    hashes = record.get("artifact_hashes") or {}
    chat_path = paths.get("chat_authority")
    if chat_path is None:
        raise ProducerDeliveryTransactionError("completed Talk identity is incomplete")
    chat_raw = _read_regular_bytes(chat_path)
    if hashes.get("subtitle_sha256") != "sha256:" + _sha256(srt) or hashes.get(
        "chat_authority_audit_sha256"
    ) != "sha256:" + hashlib.sha256(chat_raw).hexdigest():
        raise ProducerDeliveryTransactionError("completed Talk text proof bytes drift")
    # The raw chat wire identity is checked before review references are
    # expanded for validation.
    chat = review_from_bytes(chat_raw, directory=chat_path.parent)
    publish = read("publish")
    validate_final_review_release(
        chat.get("final_review_audit"), expected_srt_sha256="sha256:" + _sha256(srt)
    )
    if validate_srt_file(srt).get("errors"):
        raise ProducerDeliveryTransactionError("completed Talk subtitle fails current validation")
    if _sha256(_validated_burned_artifact(record)) != _sha256(video):
        raise ProducerDeliveryTransactionError("completed Talk burn identity drifts")
    if "source_provenance" in paths:
        provenance = _recut_provenance(record)
        if provenance is None or _sha256(provenance) != _sha256(paths["source_provenance"]):
            raise ProducerDeliveryTransactionError("completed Talk recut source provenance drifts")
    ass = _validated_burned_ass_artifact(record)
    if not any(
        _sha256(ass) == _sha256(path)
        for role in ("uniform_host_ass", "speaker_ass")
        if (path := paths.get(role)) is not None
    ):
        raise ProducerDeliveryTransactionError("completed Talk burn ASS is not sealed")
    validate_final_subtitle_audio_check(record, srt, video, candidate_id=handle.candidate_id)
    staging = record.get("publish_staging")
    story = record.get("story_contract")
    if (
        not isinstance(staging, dict)
        or not isinstance(story, dict)
        or publish.get("title") != staging.get("title")
    ):
        raise ProducerDeliveryTransactionError("completed Talk title/story binding is incomplete")
    if publish.get("candidate_id") != handle.candidate_id:
        raise ProducerDeliveryTransactionError("completed Talk publish candidate drifts")
    if "cover" in paths:
        validate_package_editorial_binding(
            record,
            publish,
            candidate_id=handle.candidate_id,
            subtitle_path=srt,
            cover_path=paths["cover"],
            request_path=paths.get("cover_request"),
            response_path=paths.get("cover_response"),
            choice_path=paths.get("cover_choice_0000"),
            speaker_srt_path=paths.get("speaker_srt"),
        )
    else:
        validate_staged_editorial_files(
            record, staging, candidate_id=handle.candidate_id, subtitle_path=srt
        )
    title = str(staging.get("title") or "")
    if staging.get("cover_status") == "AI_COVER_READY" and "cover" not in paths:
        raise ProducerDeliveryTransactionError("completed Talk ready cover is not sealed")
    if candidate_title_policy_violations(
        candidate_id=handle.candidate_id,
        title=title,
        lane="talk",
        source_fact_review=staging.get("source_fact_review"),
    ):
        raise ProducerDeliveryTransactionError("completed Talk title fails current policy")
    transcript, speaker = build_addressee_evidence(
        record, parse_srt_cues(srt.read_text(encoding="utf-8"))
    )
    if not _source_fact_receipt_valid(
        staging.get("source_fact_review"),
        record=record,
        story_contract=story,
        artifact_title=title,
        final_transcript=transcript,
        subtitle_path=srt,
        rebuilt_speaker_evidence=speaker.speaker_evidence,
        qixi_repo_root=None,
    ):
        raise ProducerDeliveryTransactionError(
            "completed Talk source-fact/operator authority is not currently valid"
        )
    if "cover" in paths:
        generation = staging.get("cover_generation") or {}
        if generation.get("final_cover_sha256") != "sha256:" + _sha256(paths["cover"]):
            raise ProducerDeliveryTransactionError("completed Talk cover bytes drift")
        codes, _ = _audit_story_bound_cover(staging, story)
        if codes:
            raise ProducerDeliveryTransactionError(
                "completed Talk cover fails current checks: " + ",".join(codes)
            )
