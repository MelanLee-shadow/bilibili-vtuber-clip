from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.review_evidence import SourceCue
from src.autoslice.term_lexicon import load_discovered_term_lexicon, normalize_text


@dataclass(frozen=True)
class AgyChunkAttestation:
    chunk_index: int
    media_start_ms: int
    media_end_ms: int
    media_sha256: str
    draft_srt_sha256: str
    refined_srt_sha256: str
    executed_provider: str
    timing_validated: bool
    audio_input_attested: bool


@dataclass(frozen=True)
class AgyExecutionResult:
    provider: str = "agy"
    model: str | None = None
    agy_rc: int | None = None
    provider_fallback_used: bool | None = None
    provider_request_id: str | None = None
    requested_provider: str | None = None
    executed_provider: str | None = None
    source_media_sha256: str | None = None
    draft_srt_sha256: str | None = None
    refined_srt_sha256: str | None = None
    timing_validated: bool | None = None
    audio_input_attested: bool | None = None
    chunk_count: int | None = None
    agy_chunk_count: int | None = None
    api_fallback_chunk_count: int | None = None
    chunk_attestations: tuple[AgyChunkAttestation, ...] = ()


class AgyRunnerError(RuntimeError):
    """Agy runner failure with a machine-distinguishable reason code.

    ``AGY_EMPTY_OUTPUT`` (rc=0 but no output written — the documented
    antigravity print-mode failure) must not be conflated with ``AGY_TIMEOUT``
    or ``AGY_FAILED_RC``: they need different fixes and the review evidence
    has to say which one actually happened.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class SourceContextExecutionResult:
    decision: str
    reason_codes: tuple[str, ...]
    context_media_path: str | None
    context_draft_srt_path: str | None
    context_refined_srt_path: str | None
    jingting_manifest_path: str | None
    review_required_path: str
    source_cues_path: str | None
    jingting_done: bool = False


AgyRunner = Callable[[Path, Path, Path], AgyExecutionResult]


def build_ffmpeg_context_clip_command(
    *,
    source_video_path: Path,
    output_media_path: Path,
    context_start_ms: int,
    context_duration_ms: int,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{context_start_ms / 1000:.3f}",
        "-i",
        str(source_video_path),
        "-t",
        f"{context_duration_ms / 1000:.3f}",
        "-c",
        "copy",
        str(output_media_path),
    ]


def execute_source_context_job(
    job_manifest: Mapping[str, object],
    *,
    source_video_path: Path,
    output_dir: Path,
    full_source_srt_path: Path | None = None,
    refined_srt_path: Path | None = None,
    agy_result: AgyExecutionResult | None = None,
    agy_runner: AgyRunner | None = None,
    refinement_required: bool = True,
    run_ffmpeg: bool = True,
) -> SourceContextExecutionResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    job_id = str(job_manifest.get("job_id") or "source-context")
    timeline = _mapping(job_manifest.get("timeline"))
    context_start_ms = _int(timeline.get("context_start_ms"), 0)
    context_duration_ms = _int(timeline.get("context_duration_ms"), 0)

    review_required_path = output_dir / f"{job_id}.jingting.review-required.json"
    jingting_manifest_path = output_dir / f"{job_id}.jingting.manifest.json"
    context_media_path = output_dir / f"{job_id}.context.mp4"
    context_draft_srt_path = output_dir / f"{job_id}.context.draft.srt"
    context_refined_srt_path = output_dir / f"{job_id}.context.refined.srt"
    source_cues_path = output_dir / f"{job_id}.source-cues.json"

    if not source_video_path.is_file():
        return _blocked(
            decision="RETRY",
            reason_codes=("SOURCE_VIDEO_MISSING",),
            review_required_path=review_required_path,
        )

    source_sha256 = _sha256_file(source_video_path)
    expected_source_sha256 = _source_sha_from_manifest(job_manifest)
    if expected_source_sha256 is not None:
        if not _is_hex_sha256(expected_source_sha256):
            # A declared-but-unusable hash must block, not silently skip the
            # integrity check: it usually means the planner or manifest is broken.
            return _blocked(
                decision="RETRY_INFRA",
                reason_codes=("SOURCE_SHA256_MALFORMED",),
                review_required_path=review_required_path,
            )
        if expected_source_sha256 != source_sha256:
            return _blocked(
                decision="RETRY",
                reason_codes=("SOURCE_SHA256_MISMATCH",),
                review_required_path=review_required_path,
            )

    if run_ffmpeg:
        cmd = build_ffmpeg_context_clip_command(
            source_video_path=source_video_path,
            output_media_path=context_media_path,
            context_start_ms=context_start_ms,
            context_duration_ms=context_duration_ms,
        )
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if completed.returncode != 0:
            _write_review_required(
                review_required_path,
                release_ready=False,
                findings=("FFMPEG_CONTEXT_CLIP_FAILED",),
                metadata={"stderr_tail": completed.stderr[-1000:]},
            )
            return SourceContextExecutionResult(
                decision="RETRY_INFRA",
                reason_codes=("FFMPEG_CONTEXT_CLIP_FAILED",),
                context_media_path=str(context_media_path),
                context_draft_srt_path=None,
                context_refined_srt_path=None,
                jingting_manifest_path=None,
                review_required_path=str(review_required_path),
                source_cues_path=None,
                jingting_done=False,
            )
    else:
        context_media_path.write_bytes(b"dry-run context media placeholder\n")

    if full_source_srt_path is None or not full_source_srt_path.is_file():
        return _blocked(
            decision="RETRY",
            reason_codes=("DRAFT_SRT_MISSING",),
            review_required_path=review_required_path,
            context_media_path=context_media_path,
        )

    cues = _parse_srt(full_source_srt_path)
    selected = _select_context_cues(cues, context_start_ms, context_start_ms + context_duration_ms)
    source_cues_path.write_text(
        json.dumps([cue.to_manifest() for cue in selected], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_relative_srt(selected, context_start_ms, context_draft_srt_path)

    if not refinement_required:
        # Song lyrics are authorized later by independently fetched LRC plus
        # audio alignment and host-vocal proof.  A talk-style multimodal
        # rewrite here is both redundant and a provider-failure dependency.
        # Keep the fresh source ASR only as proof context; it is never promoted
        # to final lyric authority.
        shutil.copy2(context_draft_srt_path, context_refined_srt_path)
        refined_srt_path = context_refined_srt_path
        agy_result = AgyExecutionResult(
            provider="source_draft_context",
            model=None,
            agy_rc=0,
            provider_fallback_used=False,
            provider_request_id="BYPASSED_NOT_AUTHORITATIVE_FOR_SONG_LRC",
        )
    elif refined_srt_path is None and agy_runner is not None:
        try:
            agy_result = agy_runner(context_media_path, context_draft_srt_path, context_refined_srt_path)
            refined_srt_path = context_refined_srt_path
        except Exception as exc:
            findings: tuple[str, ...] = ("AGY_SOURCE_CONTEXT_RUNNER_FAILED",)
            specific = getattr(exc, "reason_code", None)
            if isinstance(specific, str) and specific:
                findings = findings + (specific,)
            retry_after_seconds = getattr(exc, "retry_after_seconds", None)
            metadata: dict[str, object] = {"error": f"{type(exc).__name__}: {exc}"}
            if isinstance(retry_after_seconds, int) and retry_after_seconds > 0:
                metadata["retry_after_seconds"] = retry_after_seconds
            _write_review_required(
                review_required_path,
                release_ready=False,
                findings=findings,
                metadata=metadata,
            )
            return SourceContextExecutionResult(
                decision="RETRY_INFRA",
                reason_codes=findings,
                context_media_path=str(context_media_path),
                context_draft_srt_path=str(context_draft_srt_path),
                context_refined_srt_path=None,
                jingting_manifest_path=None,
                review_required_path=str(review_required_path),
                source_cues_path=str(source_cues_path),
                jingting_done=False,
            )

    agy_result = agy_result or AgyExecutionResult(provider="agy", model=None, agy_rc=None, provider_fallback_used=None)
    output_sha256 = None
    if refined_srt_path is not None and refined_srt_path.is_file():
        if refined_srt_path.resolve() != context_refined_srt_path.resolve():
            shutil.copy2(refined_srt_path, context_refined_srt_path)
        output_sha256 = _sha256_file(context_refined_srt_path)

    prompt_payload = {
        "job_id": job_id,
        "source_sha256": source_sha256,
        "context_start_ms": context_start_ms,
        "context_duration_ms": context_duration_ms,
        "draft_srt_sha256": _sha256_file(context_draft_srt_path),
    }
    manifest = {
        "schema_version": "jingting-source-context-result.v1",
        "job_id": job_id,
        "provider": agy_result.provider,
        "model": agy_result.model,
        "agy_rc": agy_result.agy_rc,
        "provider_fallback_used": agy_result.provider_fallback_used,
        "provider_request_id": agy_result.provider_request_id,
        "requested_provider": agy_result.requested_provider,
        "executed_provider": agy_result.executed_provider,
        "declared_source_media_sha256": agy_result.source_media_sha256,
        "declared_draft_srt_sha256": agy_result.draft_srt_sha256,
        "declared_refined_srt_sha256": agy_result.refined_srt_sha256,
        "timing_validated": agy_result.timing_validated,
        "audio_input_attested": agy_result.audio_input_attested,
        "chunk_count": agy_result.chunk_count,
        "agy_chunk_count": agy_result.agy_chunk_count,
        "api_fallback_chunk_count": agy_result.api_fallback_chunk_count,
        "chunk_attestations": [
            {
                "chunk_index": row.chunk_index,
                "media_start_ms": row.media_start_ms,
                "media_end_ms": row.media_end_ms,
                "media_sha256": row.media_sha256,
                "draft_srt_sha256": row.draft_srt_sha256,
                "refined_srt_sha256": row.refined_srt_sha256,
                "executed_provider": row.executed_provider,
                "timing_validated": row.timing_validated,
                "audio_input_attested": row.audio_input_attested,
            }
            for row in agy_result.chunk_attestations
        ],
        "refinement_required": refinement_required,
        "subtitle_authority_scope": (
            "proof_context_only_external_lrc_required"
            if not refinement_required
            else "talk_source_context_refinement"
        ),
        "source_offset_ms": context_start_ms,
        "source_sha256": f"sha256:{source_sha256}",
        "input_sha256": f"sha256:{_sha256_file(context_draft_srt_path)}",
        "output_sha256": f"sha256:{output_sha256}" if output_sha256 else None,
        "prompt_sha256": f"sha256:{_sha256_bytes(json.dumps(prompt_payload, sort_keys=True).encode('utf-8'))}",
    }
    jingting_manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    reason_codes = _agy_reason_codes(agy_result, refinement_required=refinement_required)
    if reason_codes:
        _write_review_required(review_required_path, release_ready=False, findings=reason_codes)
        return SourceContextExecutionResult(
            decision="RETRY_INFRA",
            reason_codes=reason_codes,
            context_media_path=str(context_media_path),
            context_draft_srt_path=str(context_draft_srt_path),
            context_refined_srt_path=str(context_refined_srt_path) if context_refined_srt_path.exists() else None,
            jingting_manifest_path=str(jingting_manifest_path),
            review_required_path=str(review_required_path),
            source_cues_path=str(source_cues_path),
            jingting_done=False,
        )

    if not context_refined_srt_path.exists():
        _write_review_required(review_required_path, release_ready=False, findings=("REFINED_SRT_MISSING",))
        return SourceContextExecutionResult(
            decision="RETRY",
            reason_codes=("REFINED_SRT_MISSING",),
            context_media_path=str(context_media_path),
            context_draft_srt_path=str(context_draft_srt_path),
            context_refined_srt_path=None,
            jingting_manifest_path=str(jingting_manifest_path),
            review_required_path=str(review_required_path),
            source_cues_path=str(source_cues_path),
            jingting_done=False,
        )

    if review_required_path.exists():
        review_required_path.unlink()
    (output_dir / f"{job_id}.jingting.done").write_text("{}\n", encoding="utf-8")
    return SourceContextExecutionResult(
        decision="READY",
        reason_codes=(),
        context_media_path=str(context_media_path),
        context_draft_srt_path=str(context_draft_srt_path),
        context_refined_srt_path=str(context_refined_srt_path),
        jingting_manifest_path=str(jingting_manifest_path),
        review_required_path=str(review_required_path),
        source_cues_path=str(source_cues_path),
        jingting_done=True,
    )


def _blocked(
    *,
    decision: str,
    reason_codes: Sequence[str],
    review_required_path: Path,
    context_media_path: Path | None = None,
) -> SourceContextExecutionResult:
    _write_review_required(review_required_path, release_ready=False, findings=reason_codes)
    return SourceContextExecutionResult(
        decision=decision,
        reason_codes=tuple(reason_codes),
        context_media_path=str(context_media_path) if context_media_path else None,
        context_draft_srt_path=None,
        context_refined_srt_path=None,
        jingting_manifest_path=None,
        review_required_path=str(review_required_path),
        source_cues_path=None,
        jingting_done=False,
    )


def _write_review_required(path: Path, *, release_ready: bool, findings: Sequence[str], metadata: Mapping[str, object] | None = None) -> None:
    path.write_text(
        json.dumps(
            {"schema_version": "jingting-review-required.v1", "release_ready": release_ready, "findings": list(findings), "metadata": dict(metadata or {})},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _parse_srt(path: Path) -> list[SourceCue]:
    raw = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    lexicon = load_discovered_term_lexicon(path)
    cues: list[SourceCue] = []
    for block_index, block in enumerate(raw.split("\n\n"), start=1):
        lines = [line for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            timing_line = lines[0]
            text_lines = lines[1:]
            cue_id = f"u_{block_index:06d}"
        else:
            timing_line = lines[1]
            text_lines = lines[2:]
            cue_id = lines[0].strip() or f"u_{block_index:06d}"
        if "-->" not in timing_line:
            continue
        start, end = [part.strip() for part in timing_line.split("-->", 1)]
        cues.append(
            SourceCue(
                cue_id=str(cue_id),
                source_start_ms=_parse_srt_time_ms(start),
                source_end_ms=_parse_srt_time_ms(end),
                text=normalize_text("\n".join(text_lines).strip(), lexicon=lexicon),
                language="zh",
                kind="speech",
                confidence=1.0,
            )
        )
    return cues


def _select_context_cues(cues: Sequence[SourceCue], context_start_ms: int, context_end_ms: int) -> list[SourceCue]:
    return [cue for cue in cues if cue.source_end_ms > context_start_ms and cue.source_start_ms < context_end_ms]


def _write_relative_srt(cues: Sequence[SourceCue], context_start_ms: int, path: Path) -> None:
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        start_ms = max(0, cue.source_start_ms - context_start_ms)
        end_ms = max(start_ms + 1, cue.source_end_ms - context_start_ms)
        blocks.append(f"{index}\n{_format_srt_time(start_ms)} --> {_format_srt_time(end_ms)}\n{cue.text}")
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def _parse_srt_time_ms(value: str) -> int:
    hhmmss, millis = value.replace(",", ".").split(".", 1)
    hours, minutes, seconds = [int(part) for part in hhmmss.split(":")]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(millis[:3].ljust(3, "0"))


def _format_srt_time(ms: int) -> str:
    seconds, millis = divmod(max(0, ms), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{sec:02d},{millis:03d}"


def _agy_reason_codes(
    result: AgyExecutionResult,
    *,
    refinement_required: bool = True,
) -> tuple[str, ...]:
    if not refinement_required and result.provider == "source_draft_context":
        return ()
    reasons: list[str] = []
    accepted_gemini_fallback = (
        result.provider == "gemini_api"
        and result.agy_rc is None
        and result.provider_fallback_used is True
    )
    if result.provider not in {"agy", "gemini_api"}:
        reasons.append("JINGTING_PROVIDER_NOT_AGY")
    if result.provider == "agy" and result.agy_rc != 0:
        reasons.append("AGY_FAILED")
    if result.provider_fallback_used is True and not accepted_gemini_fallback:
        reasons.append("JINGTING_PROVIDER_FALLBACK_USED")
    elif result.provider_fallback_used is None:
        reasons.append("JINGTING_PROVIDER_FALLBACK_UNKNOWN")
    if not result.model:
        reasons.append("JINGTING_MODEL_MISSING")
    return tuple(dict.fromkeys(reasons))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_sha_from_manifest(manifest: Mapping[str, object]) -> str | None:
    inputs = _mapping(manifest.get("input"))
    value = inputs.get("source_sha256") or manifest.get("source_sha256")
    if not isinstance(value, str):
        return None
    return value.removeprefix("sha256:")


def _is_hex_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in value)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
