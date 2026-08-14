"""Local artifact/cache boundary for exact-source transcript observations."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from scripts.gemini_slice_jingting import strip_markdown_fence
from src.autoslice.exact_source_transcript_contract import (
    PROVIDER_REPORT_SCHEMA,
    seal_exact_source_transcript_observation,
    valid_exact_source_transcript_observation,
)
from src.autoslice.exact_source_transcript_provider_policy import (
    rebind_cached_gemini_route,
)
from src.autoslice.llm_client import extract_json_object


MANIFEST_SCHEMA = "exact-source-transcript-manifest.v2"


def provider_manifest_path(job_dir: Path, *, provider: str, model: str) -> Path:
    """Provider/model-isolated cache path; never a shared routing shortcut."""

    if provider not in {"agy", "gemini_api"} or not model:
        raise ValueError("EXACT_SOURCE_TRANSCRIPT_PROVIDER_IDENTITY_INVALID")
    model_id = hashlib.sha256(model.encode("utf-8")).hexdigest()
    return job_dir / f"verdict.{provider}.{model_id}.manifest.json"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _source_stat_binding(path: Path) -> tuple[int, int, int, int, int, int, int]:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("source media is not a regular file")
    return (
        int(info.st_dev), int(info.st_ino), int(info.st_mode), int(info.st_uid),
        int(info.st_size), int(info.st_mtime_ns), int(info.st_ctime_ns),
    )


def seal_provider_observation(
    *,
    request: Mapping[str, Any],
    observed: Mapping[str, Any],
    source_media_sha256: str,
    audio_path: Path,
    provider: str,
    model: str,
    prompt_path: Path,
    response_path: Path,
    timeline_binding: Mapping[str, Any],
    key_tier: str | None,
    provider_route: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate the raw provider report and seal an evidence-only observation."""

    report_keys = {
        "schema_version", "status", "target_audible", "audible_language",
        "exact_transcript", "reason",
    }
    if not (
        set(observed) == report_keys
        and observed.get("schema_version") == PROVIDER_REPORT_SCHEMA
        and observed.get("status") == "OBSERVED"
        and observed.get("target_audible") is True
        and isinstance(observed.get("exact_transcript"), str)
        and isinstance(observed.get("reason"), str)
    ):
        return None
    try:
        verdict = seal_exact_source_transcript_observation(
            request=request,
            exact_transcript=observed["exact_transcript"],
            audible_language=str(observed.get("audible_language") or ""),
            source_media_sha256=source_media_sha256,
            audio_clip_sha256=_file_sha256(audio_path),
            provider=provider,
            model=model,
            response_sha256=_file_sha256(response_path),
            timeline_binding=timeline_binding,
            key_tier=key_tier,
            provider_route=provider_route,
        )
        if verdict["prompt_sha256"] != _file_sha256(prompt_path):
            return None
        return verdict
    except (OSError, TypeError, ValueError):
        return None


def store_manifest(
    *,
    manifest_path: Path,
    request: Mapping[str, Any],
    source_media_sha256: str,
    source_stat_binding: tuple[int, ...],
    audio_path: Path,
    prompt_path: Path,
    response_path: Path,
    observation: Mapping[str, Any],
) -> None:
    payload = {
        "schema_version": MANIFEST_SCHEMA,
        "request_sha256": request["request_sha256"],
        "request_payload_sha256": _json_sha256(request),
        "source_media_sha256": source_media_sha256,
        "source_stat_binding": list(source_stat_binding),
        "audio_clip_sha256": _file_sha256(audio_path),
        "prompt_artifact": prompt_path.name,
        "response_artifact": response_path.name,
        "prompt_sha256": _file_sha256(prompt_path),
        "response_sha256": _file_sha256(response_path),
        "provider": observation["provider"],
        "model": observation["model"],
        "algorithm_id": observation["algorithm_id"],
        "timeline_binding": observation["timeline_binding"],
        "provider_route": observation["provider_route"],
        "observation": dict(observation),
        "observation_sha256": _json_sha256(observation),
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def serve_manifest(
    *,
    manifest_path: Path,
    audio_path: Path,
    request: Mapping[str, Any],
    source_media: Path,
    source_media_sha256: str,
    source_stat_binding: tuple[int, ...],
    expected_models: Mapping[str, str],
    served_from_cache: bool,
    current_provider_failures: list[Mapping[str, Any]] | None = None,
) -> Mapping[str, Any] | None:
    """Replay against the verifier's already-hashed, stat-stable source."""

    try:
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        observation = cached.get("observation")
        prompt_name = str(cached.get("prompt_artifact") or "")
        response_name = str(cached.get("response_artifact") or "")
        prompt_path = manifest_path.parent / prompt_name
        response_path = manifest_path.parent / response_name
        provider = str(cached.get("provider") or "")
        model = str(cached.get("model") or "")
        if not (
            cached.get("schema_version") == MANIFEST_SCHEMA
            and cached.get("request_sha256") == request.get("request_sha256")
            and cached.get("request_payload_sha256") == _json_sha256(request)
            and cached.get("source_media_sha256") == source_media_sha256
            and cached.get("source_stat_binding") == list(source_stat_binding)
            and _source_stat_binding(source_media) == tuple(source_stat_binding)
            and cached.get("audio_clip_sha256") == _file_sha256(audio_path)
            and Path(prompt_name).name == prompt_name and prompt_path.is_file()
            and Path(response_name).name == response_name and response_path.is_file()
            and cached.get("prompt_sha256") == _file_sha256(prompt_path)
            and cached.get("response_sha256") == _file_sha256(response_path)
            and expected_models.get(provider) == model
            and isinstance(observation, Mapping)
            and cached.get("provider_route") == observation.get("provider_route")
            and cached.get("observation_sha256") == _json_sha256(observation)
            and valid_exact_source_transcript_observation(observation, request=request)
        ):
            return None
        raw = extract_json_object(
            strip_markdown_fence(response_path.read_text(encoding="utf-8"))
        )
        provider_route = observation.get("provider_route")
        if current_provider_failures is not None:
            provider_route = rebind_cached_gemini_route(
                provider_route,
                audio_clip_sha256=str(observation.get("audio_clip_sha256") or ""),
                current_provider_failures=current_provider_failures,
            )
        rebuilt = seal_provider_observation(
            request=request, observed=raw,
            source_media_sha256=source_media_sha256, audio_path=audio_path,
            provider=provider, model=model, prompt_path=prompt_path,
            response_path=response_path,
            timeline_binding=observation.get("timeline_binding") or {},
            key_tier=observation.get("key_tier") or None,
            provider_route=provider_route,
        )
        if rebuilt is None:
            return None
        if current_provider_failures is None:
            if rebuilt != dict(observation):
                return None
        else:
            original = dict(observation)
            rebuilt_base = dict(rebuilt)
            for value in (original, rebuilt_base):
                value.pop("provider_route", None)
                value.pop("observation_sha256", None)
            if original != rebuilt_base:
                return None
        if served_from_cache:
            rebuilt["served_from_cache"] = True
            rebuilt.pop("observation_sha256", None)
            rebuilt["observation_sha256"] = _json_sha256(rebuilt)
        return rebuilt
    except (OSError, TypeError, ValueError):
        return None
