"""Shared ordinary-hearing manifest projections for writer and cold consumers."""
from collections.abc import Mapping
from dataclasses import dataclass

from .argon_escalation_contract import canonical, sha


@dataclass(frozen=True)
class NativeWitnessArtifactContract:
    request: Mapping
    manifest: Mapping
    artifact_hashes = {"prompt": "prompt_sha256", "response": "response_sha256", "audio": "audio_clip_sha256"}

    @property
    def request_identity(self):
        return {"request_sha256": self.request["request_sha256"],
                "request_payload_sha256": sha(canonical(dict(self.request)))}

    def manifest_binding(self, *, source, source_sha, audio, audio_sha):
        return {"schema_version": "entity-audio-verdict-manifest.v1", **self.request_identity,
                "source_media": str(source), "source_media_sha256": source_sha,
                "audio_clip": str(audio), "audio_clip_sha256": audio_sha}

    def request_matches(self):
        return all(self.manifest.get(k) == value for k, value in self.request_identity.items())

    def bytes_match(self, artifacts):
        return all(sha(artifacts[k]) == self.manifest[field] for k, field in self.artifact_hashes.items())

    @staticmethod
    def uncertain_valid(raw, verdict):
        return (raw.get("schema_version") == "subtitle-span-acoustic-witness.v1"
                and isinstance(raw.get("reason"), str) and bool(raw["reason"].strip())
                and verdict.get("status") == "UNCERTAIN")

    @property
    def physical_target(self):
        timeline = self.manifest.get("timeline_binding")
        timeline = timeline if isinstance(timeline, Mapping) else {}
        physical = timeline.get("source_media")
        physical = physical if isinstance(physical, Mapping) else {}
        return [physical.get("target_start_ms"), physical.get("target_end_ms")]

    @property
    def projected_target(self):
        timeline = self.manifest.get("timeline_binding")
        timeline = timeline if isinstance(timeline, Mapping) else {}
        local = timeline.get("delivery_local")
        local = local if isinstance(local, Mapping) else {}
        offset = timeline.get("source_media_timeline_offset_ms")
        if type(offset) is int and offset >= 0 and all(type(local.get(k)) is int for k in ("target_start_ms", "target_end_ms")):
            return [local["target_start_ms"] + offset, local["target_end_ms"] + offset]
        return None


def native_witness_artifact_contract(request, manifest=None):
    return NativeWitnessArtifactContract(request, manifest if manifest is not None else {})
