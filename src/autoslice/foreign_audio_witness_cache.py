"""Content-addressed cache for successful AGY foreign-audio observations."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


CACHE_SCHEMA = "foreign-audio-witness-success-cache.v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def witness_identity(
    *,
    audio_path: Path,
    prompt: str,
    model: str,
    algorithm_id: str,
) -> dict[str, str]:
    """Bind a cache entry to exact audio, prompt, model, and adapter contract."""

    identity = {
        "schema_version": CACHE_SCHEMA,
        "audio_sha256": _sha256_bytes(audio_path.read_bytes()),
        "prompt_sha256": _sha256_bytes(prompt.encode("utf-8")),
        "model": str(model),
        "algorithm_id": str(algorithm_id),
    }
    identity["cache_key_sha256"] = _sha256_bytes(_canonical_json(identity))
    return identity


def _cache_path(identity: Mapping[str, str]) -> Path | None:
    base = os.environ.get("AUTOSLICE_BASE")
    if not base:
        return None
    key = identity["cache_key_sha256"]
    return Path(base) / "cache" / "foreign-audio-witnesses" / key[:2] / f"{key}.json"


def _observation_sha256(observation: Mapping[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(observation))


def cache_audit(
    identity: Mapping[str, str],
    *,
    observation: Mapping[str, Any] | None = None,
    served_from_cache: bool,
    persisted: bool | None = None,
) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "witness_cache_key_sha256": identity["cache_key_sha256"],
        "witness_prompt_sha256": identity["prompt_sha256"],
        "witness_model": identity["model"],
        "witness_algorithm_id": identity["algorithm_id"],
        "served_from_cache": served_from_cache,
    }
    if observation is not None:
        audit["witness_observation_sha256"] = _observation_sha256(observation)
    if persisted is not None:
        audit["witness_cache_persisted"] = persisted
    return audit


def load_successful_observation(
    *,
    identity: Mapping[str, str],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return only a hash-valid success entry for the exact identity."""

    path = _cache_path(identity)
    if path is None:
        return None, cache_audit(identity, served_from_cache=False)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entry, dict):
            raise ValueError("cache entry is not an object")
        observation = entry.get("observation")
        if (
            entry.get("schema_version") != CACHE_SCHEMA
            or entry.get("status") != "SUCCESS"
            or not isinstance(entry.get("identity"), dict)
            or entry["identity"] != dict(identity)
            or not isinstance(observation, dict)
            or entry.get("observation_sha256")
            != _observation_sha256(observation)
        ):
            raise ValueError("cache contract mismatch")
    except (OSError, TypeError, ValueError):
        return None, cache_audit(identity, served_from_cache=False)
    return dict(observation), cache_audit(
        identity,
        observation=observation,
        served_from_cache=True,
        persisted=True,
    )


def store_successful_observation(
    *,
    identity: Mapping[str, str],
    observation: Mapping[str, Any],
    source_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically persist one accepted success; IO failure stays non-fatal."""

    path = _cache_path(identity)
    if path is None:
        return cache_audit(
            identity,
            observation=observation,
            served_from_cache=False,
            persisted=False,
        )
    payload = {
        "schema_version": CACHE_SCHEMA,
        "status": "SUCCESS",
        "identity": dict(identity),
        "observation": dict(observation),
        "observation_sha256": _observation_sha256(observation),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_provenance": dict(source_provenance or {}),
    }
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return cache_audit(
            identity,
            observation=observation,
            served_from_cache=False,
            persisted=False,
        )
    return cache_audit(
        identity,
        observation=observation,
        served_from_cache=False,
        persisted=True,
    )
