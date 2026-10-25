"""Recover original native range artifacts from a saved review, as context only."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json

from src.autoslice.independent_context_adjudication import (
    validate_range_acoustic_observation, adapt_range_acoustic_context_observation,
)
from src.autoslice.acoustic_witness_adjudication import build_witness_request


def persisted_native_range_artifacts(authority: Mapping | None) -> tuple[dict, ...]:
    """Revalidate original consumption before exposing its immutable artifacts.

    The returned references deliberately have no single-cue witness identity.
    The ordinary range validator must independently check the next target and
    any text-only SRT carryover. Original requests and observations stay intact.
    No output/cache directory is searched and no provider is called.
    """
    found: dict[str, dict] = {}
    attempted = set()

    def walk(value):
        if isinstance(value, Mapping):
            text = value.get("whole_clip_current_srt")
            observations = value.get("context_only_range_acoustic_observations")
            if isinstance(text, str) and isinstance(observations, list):
                payload = dict(value)
                declared = str(payload.pop("request_sha256", "")).removeprefix("sha256:")
                actual = hashlib.sha256(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                if declared == actual:
                    for observation in observations:
                        if not isinstance(observation, Mapping):
                            continue
                        bindings = observation.get("artifact_bindings")
                        if not isinstance(bindings, Mapping) or not bindings.get("native_envelope_path"):
                            continue
                        key = (declared, json.dumps(dict(bindings), sort_keys=True))
                        if key in attempted:
                            continue
                        attempted.add(key)
                        try:
                            narrow = build_witness_request(value)
                            targets = value['cue_indexes']
                            if not isinstance(targets, list) or len(targets) != 1:
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                        if (observation.get('single_cue_witness_request_sha256', narrow['request_sha256'])
                                != narrow['request_sha256']
                                or observation.get('current_cue_index', targets[0]) != targets[0]):
                            continue
                        validated, reason = validate_range_acoustic_observation(text, value, observation)
                        if validated is None and reason in {
                            "RANGE_ACOUSTIC_SRT_FILE_DRIFT", "NATIVE_RANGE_SOURCE_OR_SRT_DRIFT",
                        }:
                            validated, _ = adapt_range_acoustic_context_observation(text, value, observation)
                        if validated is None:
                            continue
                        artifacts = deepcopy(validated["artifact_bindings"])
                        identity = json.dumps(artifacts, sort_keys=True, ensure_ascii=False)
                        found.setdefault(identity, {
                            "schema_version": "native-range-artifact-reference.v1",
                            "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
                            "artifact_bindings": artifacts,
                            "origin_context_request_sha256": declared,
                            "origin_target_cue_indexes": deepcopy(value.get("cue_indexes")),
                        })
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(authority)
    return tuple(found.values())
