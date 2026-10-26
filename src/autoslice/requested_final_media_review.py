"""Explicit whole-final-media requests, separate from anomaly and human review.

This admits a request to the existing sealed raw-AV protocol. It never attests
that an operator, agent, model, or tool has already watched or heard anything.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path

from src.autoslice.source_checkpoint_parent import binding, bound

SCHEMA = "requested-whole-final-media-review.v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_request(spec_path: Path) -> dict:
    from src.autoslice.first_publication_boundary import bind_published_recall_to_spec
    from src.autoslice.source_checkpoint_parent import validate_source_checkpoint_parent
    source_spec = binding(spec_path)
    spec = bound(source_spec)
    if not bind_published_recall_to_spec(spec):
        raise ValueError("FINAL_MEDIA_REVIEW_VERIFIED_PUBLICATION_REQUIRED")
    validate_source_checkpoint_parent(spec)
    authority = spec["recovery_publication_authority"]
    return {"schema_version": SCHEMA, "candidate_id": spec["candidate_id"],
            "bvid": authority["bvid"], "source_spec": source_spec,
            "publication_authority_sha256": authority["authority_sha256"],
            "scope": "EXACT_FINAL_MP4_AND_FULL_WAV_AND_SUBTITLES_0_TO_EOS",
            "content_review_status": "UNASSESSED", "release_authorized": False}


def validate_request(value: Mapping, *, candidate_id: str):
    if not isinstance(value, Mapping) or value.get("candidate_id") != candidate_id:
        raise ValueError("FINAL_MEDIA_REVIEW_REQUEST_IDENTITY_DRIFT")
    source = bound(value.get("source_spec"))
    if source.get("candidate_id") != candidate_id:
        raise ValueError("FINAL_MEDIA_REVIEW_REQUEST_IDENTITY_DRIFT")
    rebuilt = build_request(Path(value["source_spec"]["path"]))
    if dict(value) != rebuilt:
        raise ValueError("FINAL_MEDIA_REVIEW_REQUEST_BINDING_DRIFT")
    return source


def validate_request_record(request: Mapping, record: Mapping, *, candidate_id: str):
    """Tie the exact final record to the same normal publication authority."""
    source = validate_request(request, candidate_id=candidate_id)
    if (record.get("candidate_id") != candidate_id
            or record.get("recovery_publication_authority")
            != source.get("recovery_publication_authority")):
        raise ValueError("FINAL_MEDIA_REVIEW_REQUEST_RECORD_AUTHORITY_DRIFT")


def request_contract(request, final_duration_ms):
    if type(final_duration_ms) is not int or final_duration_ms <= 0:
        raise ValueError("FINAL_MEDIA_REVIEW_FINAL_DURATION_INVALID")
    points = [{"point_id": "whole-final-media-0-eos",
               "final_video_start_ms": 0, "final_video_end_ms": final_duration_ms,
               "expectation": "Review all final audio, burned/display subtitles and continuous picture from 0 to EOS; report omissions, truncation, wording or audiovisual anomalies."}]
    return _digest({"request": dict(request), "subtitle_review_points": points}), points


def inspect_runtime(runtime_root: Path) -> dict:
    from src.autoslice.final_media_review_raw_av import load_runtime_raw_av_capability
    from src.autoslice.final_media_review_model_capability_sentinel import load_sentinel_runtime
    capability = load_runtime_raw_av_capability(runtime_root)
    sentinel = load_sentinel_runtime(runtime_root)
    selected = capability or sentinel
    result = {"status": ("RAW_AV_CAPABILITY_READY" if capability is not None else
                         "SENTINEL_REQUIRED" if sentinel is not None else "SENTINEL_RUNTIME_ABSENT"),
              "provider_calls": 0, "runner_called": False,
              "content_review_status": "NOT_PERFORMED", "release_authorized": False}
    if selected is not None:
        result["identity"] = {key: selected[key] for key in ("provider", "model", "endpoint_family")}
    return result
