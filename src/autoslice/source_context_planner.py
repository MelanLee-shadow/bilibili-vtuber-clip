from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

from src.autoslice.boundary_resolver import AnchorCandidate


SOURCE_CONTEXT_JINGTING_SCHEMA_VERSION = "source-context-jingting-job.v1"
DEFAULT_CONTEXT_PRE_MS = 90_000
DEFAULT_CONTEXT_POST_MS = 150_000
DEFAULT_PROVIDER = "agy"


@dataclass(frozen=True)
class JingtingJobProvenance:
    """Source-level provenance for a deterministic local jingting planning job."""

    recording_id: str
    source_sha256: str
    source_uri: str
    planner_version: str = "source-context-planner.v1"
    code_commit: str | None = None
    transcript_provider: str | None = None
    transcript_model: str | None = None
    prompt_sha256: str | None = None
    provider_request_id: str | None = None
    provider_fallback_used: bool | None = None

    def to_manifest(self) -> dict[str, object]:
        return _drop_none(asdict(self))


@dataclass(frozen=True)
class SourceContextJingtingJob:
    """A local manifest entry for agy/jingting on source context before recut."""

    job_id: str
    candidate_id: str
    anchor_start_ms: int
    anchor_end_ms: int
    context_start_ms: int
    context_end_ms: int
    source_duration_ms: int
    provenance: JingtingJobProvenance
    provider: str = DEFAULT_PROVIDER
    schema_version: str = SOURCE_CONTEXT_JINGTING_SCHEMA_VERSION
    local_only: bool = True

    @property
    def source_offset_ms(self) -> int:
        return self.context_start_ms

    @property
    def context_duration_ms(self) -> int:
        return self.context_end_ms - self.context_start_ms

    @property
    def context_anchor_start_ms(self) -> int:
        return self.anchor_start_ms - self.context_start_ms

    @property
    def context_anchor_end_ms(self) -> int:
        return self.anchor_end_ms - self.context_start_ms

    def to_manifest(self) -> dict[str, object]:
        """Return the JSON-serializable source-context jingting job manifest."""

        return {
            "schema_version": self.schema_version,
            "job_kind": "SOURCE_CONTEXT_JINGTING",
            "job_id": self.job_id,
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "local_only": self.local_only,
            "source_offset_ms": self.source_offset_ms,
            "provenance": self.provenance.to_manifest(),
            "timeline": {
                "source_duration_ms": self.source_duration_ms,
                "anchor_start_ms": self.anchor_start_ms,
                "anchor_end_ms": self.anchor_end_ms,
                "context_start_ms": self.context_start_ms,
                "context_end_ms": self.context_end_ms,
                "context_duration_ms": self.context_duration_ms,
                "context_anchor_start_ms": self.context_anchor_start_ms,
                "context_anchor_end_ms": self.context_anchor_end_ms,
            },
            "input": {
                "source_uri": self.provenance.source_uri,
                "source_sha256": self.provenance.source_sha256,
                "source_offset_ms": self.source_offset_ms,
                "duration_ms": self.context_duration_ms,
                "write_outputs": False,
            },
            "outputs": {
                "jingting_srt": None,
                "jingting_manifest": None,
                "review_required": None,
            },
        }


def plan_source_context_jingting_jobs(
    anchors: Sequence[AnchorCandidate],
    *,
    source_duration_ms: int,
    provenance: JingtingJobProvenance,
    pre_ms: int = DEFAULT_CONTEXT_PRE_MS,
    post_ms: int = DEFAULT_CONTEXT_POST_MS,
    provider: str = DEFAULT_PROVIDER,
) -> list[SourceContextJingtingJob]:
    """Build deterministic local source-context jingting job manifests.

    Anchors remain recall hints.  The returned jobs ask a later local executor to
    refine the bounded source window around each anchor before boundary
    resolution.  This planner never calls agy or writes outputs itself.
    """

    _validate_non_negative("source_duration_ms", source_duration_ms)
    _validate_non_negative("pre_ms", pre_ms)
    _validate_non_negative("post_ms", post_ms)

    jobs: list[SourceContextJingtingJob] = []
    for anchor in anchors:
        _validate_anchor(anchor, source_duration_ms)
        context_start_ms = max(0, anchor.anchor_start_ms - pre_ms)
        context_end_ms = min(source_duration_ms, anchor.anchor_end_ms + post_ms)
        job_id = _stable_job_id(
            {
                "schema_version": SOURCE_CONTEXT_JINGTING_SCHEMA_VERSION,
                "provider": provider,
                "candidate_id": anchor.candidate_id,
                "recording_id": provenance.recording_id,
                "source_sha256": provenance.source_sha256,
                "anchor_start_ms": anchor.anchor_start_ms,
                "anchor_end_ms": anchor.anchor_end_ms,
                "context_start_ms": context_start_ms,
                "context_end_ms": context_end_ms,
            }
        )
        jobs.append(
            SourceContextJingtingJob(
                job_id=job_id,
                candidate_id=anchor.candidate_id,
                anchor_start_ms=anchor.anchor_start_ms,
                anchor_end_ms=anchor.anchor_end_ms,
                context_start_ms=context_start_ms,
                context_end_ms=context_end_ms,
                source_duration_ms=source_duration_ms,
                provenance=provenance,
                provider=provider,
            )
        )

    return jobs


def _stable_job_id(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "scj_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _validate_non_negative(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_anchor(anchor: AnchorCandidate, source_duration_ms: int) -> None:
    if anchor.anchor_start_ms < 0:
        raise ValueError("anchor_start_ms must be non-negative")
    if anchor.anchor_end_ms < anchor.anchor_start_ms:
        raise ValueError("anchor_end_ms must be >= anchor_start_ms")
    if anchor.anchor_end_ms > source_duration_ms:
        raise ValueError("anchor_end_ms must be within source_duration_ms")


def _drop_none(data: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in data.items() if value is not None}
