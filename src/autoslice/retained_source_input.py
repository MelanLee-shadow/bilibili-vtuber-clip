"""Reuse source bytes and cumulative accounts without inheriting a verdict."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.autoslice.native_audio_budget_receipt import _no_links
from src.autoslice.prepared_native_audio_recovery import (
    PreparedNativeAudioState,
    inherit_prepared_native_audio,
)
from src.autoslice.shadow_review import _sha256


@dataclass(frozen=True)
class RetainedSourceInput:
    native: PreparedNativeAudioState
    cpa_usage: dict
    proof: dict


def _bound_json(
    path_value: object,
    digest: object,
    *,
    review: bool = False,
) -> tuple[Path, dict]:
    path = Path(str(path_value or ""))
    _no_links(path)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("RETAINED_SOURCE_INPUT_PARENT_DRIFT")
    raw = path.read_bytes()
    expected = "sha256:" + str(digest).removeprefix("sha256:")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("RETAINED_SOURCE_INPUT_PARENT_DRIFT")
    if review:
        from src.autoslice.review_evidence_store import review_from_bytes

        value = review_from_bytes(raw, directory=path.parent)
    else:
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("RETAINED_SOURCE_INPUT_PARENT_INVALID")
    return path, value


def load_retained_source_input(
    *, spec: Mapping, cid: str, padded: Path, out_root: Path,
) -> RetainedSourceInput | None:
    config = spec.get("retained_source_input")
    if config is None:
        return None
    from src.autoslice.source_checkpoint_parent import SCHEMA, consume_source_checkpoint_parent
    if isinstance(config, Mapping) and config.get("schema_version") == SCHEMA:
        return consume_source_checkpoint_parent(spec=spec, cid=cid, padded=padded, out_root=out_root)
    if (not isinstance(config, Mapping)
            or config.get("schema_version") != "retained-source-input.v1"
            or config.get("candidate_id") != cid
            or spec.get("retained_current_text") is not None):
        raise ValueError("RETAINED_SOURCE_INPUT_INVALID")
    _no_links(padded)
    digest = "sha256:" + _sha256(padded)
    if (str(config.get("padded_sha256")).removeprefix("sha256:") != digest[7:]
            or str(config.get("parent_materialization_sha256")).removeprefix("sha256:") != digest[7:]
            or config.get("parent_materialization_path") != str(padded)):
        raise ValueError("RETAINED_SOURCE_INPUT_MEDIA_DRIFT")
    surface = spec.get("existing_subtitle_surface")
    if (not isinstance(surface, Mapping) or surface.get("candidate_id") != cid
            or str(surface.get("source_media_sha256")).removeprefix("sha256:") != digest[7:]):
        raise ValueError("RETAINED_SOURCE_INPUT_EXISTING_SRT_REQUIRED")
    binding = config.get("usage_parent")
    if not isinstance(binding, Mapping):
        raise ValueError("RETAINED_SOURCE_INPUT_USAGE_PARENT_REQUIRED")
    record_path, record = _bound_json(binding.get("path"), binding.get("sha256"))
    if record.get("candidate_id") != cid:
        raise ValueError("RETAINED_SOURCE_INPUT_CANDIDATE_DRIFT")
    hashes = record.get("artifact_hashes") or {}
    authority_path, authority = _bound_json(
        record.get("chat_authority_audit_path"), hashes.get("chat_authority_audit_sha256"),
        review=True,
    )
    _, context = _bound_json(record.get("clip_context_path"), hashes.get("clip_context_file_sha256"))
    if (authority.get("schema_version") != "chat-authority-audit.v2"
            or authority.get("mutation_authorized") is True
            or context.get("schema_version") != "lidousha-clip-context.v1"
            or context.get("candidate_id") != cid):
        raise ValueError("RETAINED_SOURCE_INPUT_AUTHORITY_SCOPE_DRIFT")
    actual_pieces, parent_pieces = spec.get("pieces"), context.get("pieces")
    if (not isinstance(actual_pieces, list) or not actual_pieces
            or not isinstance(parent_pieces, list) or len(actual_pieces) != len(parent_pieces)):
        raise ValueError("RETAINED_SOURCE_INPUT_SOURCE_DRIFT")
    for actual, parent in zip(actual_pieces, parent_pieces):
        if (any(actual.get(key) != parent.get(key) for key in ("start_ms", "end_ms"))
                or Path(str(actual.get("remote_media"))).name != parent.get("recording_basename")
                or not actual.get("source_media_sha256")
                or str(actual["source_media_sha256"]).removeprefix("sha256:")
                    != str(parent.get("source_media_sha256")).removeprefix("sha256:")):
            raise ValueError("RETAINED_SOURCE_INPUT_SOURCE_DRIFT")
    # Only cumulative usage crosses this boundary. The old release, typed
    # decisions, and final-text projection remain immutable parent history.
    from src.autoslice.producer_text_pipeline import _retained_parent_cpa_usage_seed
    usage = _retained_parent_cpa_usage_seed(authority)
    native_parent = authority_path.parent
    _bound_json(native_parent / "native-audio-budget.json", binding.get("native_budget_sha256"))
    from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
    runtime = resolve_talk_delivery_runtime_root({**spec, "output_root": str(out_root)})
    state = inherit_prepared_native_audio(cid=cid, parent=native_parent, source=padded, runtime_root=runtime)
    return RetainedSourceInput(state, usage, {
        "schema_version": "retained-source-input-consumption.v1",
        "candidate_id": cid,
        "parent_record": {"path": str(record_path), "sha256": "sha256:" + _sha256(record_path)},
        "padded_sha256": digest,
        "native_budget_receipt": str(state.budget_receipt),
        "native_cache_root": str(state.cache_root),
        "inherited_cpa_adjudication_count": usage["provider_adjudication_count"],
        "release_authorized": False,
    })
