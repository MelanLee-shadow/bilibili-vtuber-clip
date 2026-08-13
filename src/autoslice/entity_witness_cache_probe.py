"""Provider-free replay probe for sealed candidate-blind audio witnesses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from scripts.gemini_slice_jingting import strip_markdown_fence
from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL
from src.autoslice.llm_client import extract_json_object


def _artifact_by_sha256(
    directory: Path,
    *,
    patterns: tuple[str, ...],
    expected_sha256: str,
    sha256_file: Any,
) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return None
    paths = sorted(
        {
            path
            for pattern in patterns
            for path in directory.glob(pattern)
            if path.is_file() and not path.is_symlink()
        }
    )
    for path in paths:
        try:
            if sha256_file(path) == expected_sha256:
                return path
        except OSError:
            continue
    return None


def _load_manifests(output_dir: Path) -> list[tuple[Path, Mapping[str, Any]]]:
    loaded: list[tuple[Path, Mapping[str, Any]]] = []
    for path in sorted((output_dir / "entity_verdicts").glob("*/verdict.manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(manifest, Mapping):
            loaded.append((path, manifest))
    return sorted(
        loaded,
        key=lambda item: (
            0 if item[1].get("provider") == "agy" else 1,
            str(item[0]),
        ),
    )


def probe_cached_local_audio_witness(
    *, verifier: Any, request: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Rebind a physically identical witness without crop, writes, or provider I/O.

    Candidate/base/proposed drift is intentionally outside the blind witness
    and remains bound by the CPA judge cache.  This probe binds source bytes,
    physical source geometry, audio, full provider prompt, response, provider,
    and model before rebuilding a verdict for the current witness request.
    """

    # Lazy import avoids a module cycle: the verifier class owns this probe,
    # while this focused module reuses its validators and receipt builders.
    from src.autoslice import entity_audio_verifier as audio

    if request.get("schema_version") != audio.WITNESS_REQUEST_SCHEMA:
        return None
    request_sha = str(request.get("request_sha256") or "")
    payload = dict(request)
    payload.pop("request_sha256", None)
    forbidden = {
        "candidate_entities",
        "current_cue",
        "proposed_cue",
        "matched_audio_text",
        "context_before",
        "context_after",
        "suspect",
        "replacement",
    }
    hint = request.get("syllable_count_hint")
    offset = request.get("source_media_timeline_offset_ms")
    if not (
        re.fullmatch(r"[0-9a-f]{64}", request_sha)
        and audio._json_sha256(payload) == request_sha
        and request.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and not any(key in request for key in forbidden)
        and (hint is None or (not isinstance(hint, bool) and isinstance(hint, int) and hint > 0))
        and not isinstance(offset, bool)
        and isinstance(offset, int)
        and offset >= 0
    ):
        return None
    try:
        # The verifier established source_sha256 from bytes between two equal
        # stat samples at construction.  Rechecking that compact binding here
        # detects later replacement/drift without rereading a multi-GB FUSE
        # recording once per final-review finding.
        if audio._source_stat_binding(verifier.source_media) != verifier.source_stat_binding:
            return None
    except (OSError, RuntimeError):
        return None
    span = audio._prepare_audio_span(
        request=request,
        context_mode=True,
        source_media_timeline_offset_ms=offset,
        source_duration_ms=verifier.source_duration_ms,
        witness_mode=True,
    )
    if span is None:
        return None
    geometry = span.timeline_binding.get("source_media")
    if not isinstance(geometry, Mapping):
        return None

    def prompt(delivery_mode: str) -> str:
        return audio._witness_prompt(
            recording_date=verifier.recording_date,
            delivery_mode=delivery_mode,
            syllable_count_hint=span.observed_request.get("syllable_count_hint"),
            target_audio_start_ms=span.observed_request.get("target_audio_start_ms"),
            target_audio_end_ms=span.observed_request.get("target_audio_end_ms"),
        )

    contracts = {
        "agy": (verifier.model, prompt("agy")),
        "gemini_api": (audio._entity_api_model(), prompt("gemini_api")),
    }
    for manifest_path, cached in _load_manifests(verifier.output_dir):
        provider = str(cached.get("provider") or "")
        contract = contracts.get(provider)
        if contract is None:
            continue
        expected_model, expected_prompt = contract
        prompt_sha = hashlib.sha256(expected_prompt.encode()).hexdigest()
        timeline = cached.get("timeline_binding")
        cached_geometry = timeline.get("source_media") if isinstance(timeline, Mapping) else None
        verdict = cached.get("verdict")
        audio_sha = str(cached.get("audio_clip_sha256") or "")
        response_sha = str(cached.get("response_sha256") or "")
        if not (
            cached.get("schema_version") == "entity-audio-verdict-manifest.v1"
            and cached.get("source_media_sha256") == verifier.source_sha256
            and cached.get("witness_prompt_contract") == audio.WITNESS_PROMPT_CONTRACT
            and cached.get("model") == expected_model
            and cached.get("prompt_sha256") == prompt_sha
            and cached_geometry == geometry
            and isinstance(verdict, Mapping)
            and verdict.get("schema_version") == audio.WITNESS_SCHEMA
            and verdict.get("status") == "OBSERVED"
            and verdict.get("source_media_sha256") == verifier.source_sha256
            and verdict.get("audio_clip_sha256") == audio_sha
            and verdict.get("prompt_sha256") == prompt_sha
            and verdict.get("response_sha256") == response_sha
            and verdict.get("provider") == provider
            and verdict.get("model") == expected_model
            and verdict.get("timeline_binding") == timeline
        ):
            continue
        clip = manifest_path.parent / "input.mp4"
        if not clip.is_file() or clip.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", audio_sha):
            continue
        try:
            if audio._sha256(clip) != audio_sha:
                continue
        except OSError:
            continue
        prompt_path = _artifact_by_sha256(
            manifest_path.parent,
            patterns=("prompt*",),
            expected_sha256=prompt_sha,
            sha256_file=audio._sha256,
        )
        response_path = _artifact_by_sha256(
            manifest_path.parent,
            patterns=("verdict*.json", "response*.json"),
            expected_sha256=response_sha,
            sha256_file=audio._sha256,
        )
        if prompt_path is None or response_path is None:
            continue
        try:
            observed = extract_json_object(
                strip_markdown_fence(response_path.read_text(encoding="utf-8"))
            )
        except (OSError, TypeError, ValueError):
            continue
        if not isinstance(observed, Mapping):
            continue
        outcome = audio._EntityProviderOutcome(
            observed=observed,
            provider=provider,
            model=expected_model,
            prompt_path=prompt_path,
            response_path=response_path,
            accepted_key_tier=(str(cached.get("key_tier")) if cached.get("key_tier") else None),
            paid_policy_stamp=(
                dict(cached["paid_backup_policy"])
                if isinstance(cached.get("paid_backup_policy"), Mapping)
                else None
            ),
            provider_failures=[],
            served_from_cache=True,
        )
        rebound = audio._subtitle_acoustic_witness_verdict(
            request=request,
            request_sha=request_sha,
            observed=observed,
            outcome=outcome,
            source_sha256=verifier.source_sha256,
            audio_path=clip,
            start_ms=span.crop_start_ms,
            end_ms=span.crop_end_ms,
            timeline_binding=span.timeline_binding,
        )
        if rebound.get("status") != "OBSERVED":
            continue
        return {
            **rebound,
            "served_from_cache": True,
            "witness_cache_replay": {
                "schema_version": "witness-metadata-cache-replay.v1",
                "status": "PASS",
                "provider_call_count": 0,
                "source_media_sha256": verifier.source_sha256,
                "audio_clip_sha256": audio_sha,
                "prompt_sha256": prompt_sha,
                "response_sha256": response_sha,
                "provider": provider,
                "model": expected_model,
                "source_media_geometry": dict(geometry),
                "replayed_manifest_request_sha256": str(cached.get("request_sha256") or ""),
                "current_witness_request_sha256": request_sha,
            },
        }
    return None
