"""Speaker routing and finalization transactions for talk-slice production."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_media import (
    FastMediaRollbackError,
    _begin_fast_media_transaction,
    _commit_fast_media_transaction,
    _derive_fresh_fast_media,
    _resolved_optional_path,
    _rollback_fast_media_transaction,
    _sha256,
    _validate_fast_transaction_outputs,
    _write_json_atomic,
    run,
)
from src.autoslice.producer_text_finalization import _format_srt_timestamp
from src.autoslice.speaker_finalizer import (
    SpeakerFinalizationError,
    finalize_fast_solo_subtitles,
    validate_speaker_review_manifest_document,
)
from src.autoslice.speaker_session_router import (
    FAST_SOLO,
    SpeakerRoutingError,
    verify_speaker_routing_claim,
    verify_speaker_routing_claim_for_candidate,
)

ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)


def profile_asset_file(key: str) -> Path:
    return CHANNEL_PROFILE.asset_file(key, repo_root=ROOT)


def profile_voiceprint_reference_dir() -> Path:
    return (
        Path("/opt/bilive/autoslice/voiceprints")
        / CHANNEL_PROFILE.voiceprint_reference_subdirectory
    )
def _write_route_mixed_overlap_evidence(
    *,
    media_path: Path,
    text_srt_path: Path,
    work_dir: Path,
    claim: dict,
    verified_route: object,
) -> Path:
    """Turn provider-wide mixed/overlap evidence into a bound terminal review.

    The acoustic provider currently reports at candidate-window granularity.
    Therefore every final cue is conservatively affected.  Each row includes a
    real extracted cue-audio digest, and the downstream finalizer emits no SRT
    or ASS unless exact speaker overrides cover all affected cues.
    """

    candidate_result = getattr(verified_route, "candidate_result")
    evidence = candidate_result["evidence"]
    reasons = []
    if evidence.get("mixed_speaker_within_unit_detected"):
        reasons.append("CUE_MIXED_SPEAKER")
    if evidence.get("overlap_detected"):
        reasons.append("CUE_OVERLAPPING_SPEECH")
    if not reasons:
        raise SpeakerRoutingError("provider route has no mixed/overlap evidence")
    cues = [
        cue
        for cue in parse_srt_cues(text_srt_path.read_text(encoding="utf-8"))
        if cue.text.strip()
    ]
    if not cues:
        raise SpeakerRoutingError("mixed/overlap review has no final subtitle cues")
    work_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for source_cue, cue in enumerate(cues, start=1):
        audio_path = work_dir / f"mixed-review-cue-{source_cue:04d}.wav"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{cue.start_ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-t",
                f"{max(1, cue.end_ms - cue.start_ms) / 1000:.3f}",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio_path),
            ],
            timeout=300,
        )
        rows.append(
            {
                "source_cue": source_cue,
                "zero_based_index": source_cue - 1,
                "start": _format_srt_timestamp(cue.start_ms),
                "end": _format_srt_timestamp(cue.end_ms),
                "text": cue.text,
                "audio_sha256": _sha256(audio_path),
                "reason_codes": reasons,
                "provider_details": {
                    "cluster_count": max(
                        2, int(evidence.get("speaker_count_lower_bound") or 1)
                    ),
                    "overlap_detected": bool(evidence.get("overlap_detected")),
                    "audio_path": str(audio_path.resolve()),
                    "provider_audio_window_sha256": evidence["audio_window_sha256"],
                    "provider_acoustic_observation_sha256": evidence[
                        "acoustic_observation_sha256"
                    ],
                    "routing_claim_sha256": getattr(verified_route, "claim_sha256"),
                    "provider_evidence_sha256": getattr(
                        verified_route, "provider_evidence_sha256"
                    ),
                },
            }
        )
    provider = claim.get("provider") or {}
    config_sha = (((provider.get("artifacts") or {}).get("config") or {}).get("sha256"))
    output = work_dir / "provider-mixed-overlap-evidence.json"
    _write_json_atomic(
        output,
        {
            "schema_version": (
                f"{CHANNEL_PROFILE.profile_id}-speaker-mixed-overlap-evidence.v1"
            ),
            "status": "REVIEW_REQUIRED",
            "source_media_sha256": _sha256(media_path),
            "text_final_srt_sha256": _sha256(text_srt_path),
            "provider": {
                "name": str(provider.get("name") or "sealed-acoustic-provider"),
                "config_sha256": str(config_sha or ""),
            },
            "review_required_cues": rows,
        },
    )
    return output

def _rebase_remote_speaker_manifest(
    manifest: dict,
    *,
    host: str,
    media_path: Path,
    text_srt_path: Path,
    override_path: Path | None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    output_srt_path: Path,
    output_ass_path: Path,
) -> dict:
    """Replace deleted /tmp paths while retaining their execution provenance."""

    rebased = dict(manifest)
    path_keys = (
        "source_media",
        "text_final_srt",
        "speaker_override",
        "source_session_anchor_manifest",
        "mixed_overlap_evidence",
        "output_review_srt",
        "output_ass",
    )
    rebased["runtime_host"] = host
    rebased["ephemeral_runtime_paths"] = {key: manifest.get(key) for key in path_keys}
    rebased.update(
        {
            "source_media": str(media_path.resolve()),
            "text_final_srt": str(text_srt_path.resolve()),
            "speaker_override": str(override_path.resolve()) if override_path is not None else None,
            "source_session_anchor_manifest": (
                str(source_session_anchor_path.resolve())
                if source_session_anchor_path is not None
                else None
            ),
            "mixed_overlap_evidence": (
                str(mixed_overlap_evidence_path.resolve())
                if mixed_overlap_evidence_path is not None
                else None
            ),
            "output_review_srt": str(output_srt_path.resolve()),
            "output_ass": str(output_ass_path.resolve()),
        }
    )
    return rebased

def run_speaker_finalizer(
    *,
    host: str,
    candidate_id: str,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    override_path: Path | None = None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    speaker_python: Path = Path("/opt/bilive/autoslice/venv-diar/bin/python"),
    reference_dir: Path | None = None,
    profile_path: Path | None = None,
    model_dir: Path = Path("/opt/bilive/autoslice/models/campp"),
) -> dict:
    """Run the pinned speaker runtime locally on free or through a remote temp.

    The command consumes the already-final text SRT.  Outputs are accepted only
    when the manifest says READY and every returned artifact hash matches.
    """

    safe_cid = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate_id)[:80]
    local_host = host in {"localhost", "127.0.0.1", "::1"}
    profile = profile_path or profile_asset_file("voiceprint_profile")
    reference_dir = reference_dir or profile_voiceprint_reference_dir()
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale results before reading any runtime prerequisite.  A missing
    # profile used to raise above this cleanup and leave an old speaker-final
    # sidecar that looked newer than the current text attempt.
    for stale in (output_srt_path, output_ass_path, output_manifest_path):
        stale.unlink(missing_ok=True)
    frozen_inputs = {
        "source_media_sha256": _sha256(media_path),
        "text_final_srt_sha256": _sha256(text_srt_path),
        "profile_sha256": _sha256(profile),
        "speaker_override_sha256": _sha256(override_path) if override_path is not None else None,
        "source_session_anchor_manifest_sha256": (
            _sha256(source_session_anchor_path)
            if source_session_anchor_path is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": (
            _sha256(mixed_overlap_evidence_path)
            if mixed_overlap_evidence_path is not None
            else None
        ),
    }

    def valid_review_manifest(document: object) -> bool:
        try:
            validate_speaker_review_manifest_document(
                document,
                expected_media_sha256=str(frozen_inputs["source_media_sha256"]),
                expected_text_sha256=str(frozen_inputs["text_final_srt_sha256"]),
            )
        except SpeakerFinalizationError:
            return False
        return True

    if local_host:
        command = [
            str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
            "--candidate-id", candidate_id,
            "--media", str(media_path), "--text-srt", str(text_srt_path),
            "--profile", str(profile), "--reference-dir", str(reference_dir),
            "--model-dir", str(model_dir), "--output-srt", str(output_srt_path),
            "--output-ass", str(output_ass_path), "--output-manifest", str(output_manifest_path),
            "--work-dir", str(work_dir),
        ]
        if override_path is not None:
            command.extend(["--overrides", str(override_path)])
        if source_session_anchor_path is not None:
            command.extend(["--source-session-anchors", str(source_session_anchor_path)])
        if mixed_overlap_evidence_path is not None:
            command.extend(["--mixed-overlap-evidence", str(mixed_overlap_evidence_path)])
        completed = subprocess.run(
            command, cwd=str(ROOT), check=False, capture_output=True, text=True, timeout=1800
        )
    else:
        remote_dir = f"/tmp/autoslice-speaker-{safe_cid}-{os.getpid()}"
        remote_media = f"{remote_dir}/media.mp4"
        remote_srt = f"{remote_dir}/text-final.srt"
        remote_output_srt = f"{remote_dir}/speaker-final.srt"
        remote_output_ass = f"{remote_dir}/speaker-final.ass"
        remote_manifest = f"{remote_dir}/speaker-final.json"
        remote_override = f"{remote_dir}/overrides.json"
        remote_session_anchors = f"{remote_dir}/source-session-anchors.json"
        remote_mixed_overlap = f"{remote_dir}/mixed-overlap-evidence.json"
        run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)}/work"], timeout=120)
        try:
            run(["scp", "-q", str(media_path), str(text_srt_path), f"{host}:{remote_dir}/"], timeout=1800)
            # scp preserves local basenames, normalize to fixed remote names.
            remote_setup = (
                f"mv {shlex.quote(remote_dir + '/' + media_path.name)} {shlex.quote(remote_media)}; "
                f"mv {shlex.quote(remote_dir + '/' + text_srt_path.name)} {shlex.quote(remote_srt)}"
            )
            run(["ssh", host, remote_setup], timeout=120)
            if override_path is not None:
                run(["scp", "-q", str(override_path), f"{host}:{remote_override}"], timeout=120)
            if source_session_anchor_path is not None:
                run(
                    ["scp", "-q", str(source_session_anchor_path), f"{host}:{remote_session_anchors}"],
                    timeout=120,
                )
            if mixed_overlap_evidence_path is not None:
                run(
                    ["scp", "-q", str(mixed_overlap_evidence_path), f"{host}:{remote_mixed_overlap}"],
                    timeout=120,
                )
            try:
                remote_profile = (
                    Path("/opt/bilive/autoslice/repo")
                    / profile.resolve().relative_to(ROOT.resolve())
                )
            except ValueError as exc:
                raise RuntimeError(
                    "remote speaker finalization requires the selected voiceprint profile "
                    "to live inside the repository"
                ) from exc
            remote_command = [
                str(speaker_python), "-m", "src.autoslice.speaker_finalizer",
                "--candidate-id", candidate_id,
                "--media", remote_media, "--text-srt", remote_srt,
                "--profile", str(remote_profile),
                "--reference-dir", str(reference_dir), "--model-dir", str(model_dir),
                "--output-srt", remote_output_srt, "--output-ass", remote_output_ass,
                "--output-manifest", remote_manifest, "--work-dir", f"{remote_dir}/work",
            ]
            if override_path is not None:
                remote_command.extend(["--overrides", remote_override])
            if source_session_anchor_path is not None:
                remote_command.extend(["--source-session-anchors", remote_session_anchors])
            if mixed_overlap_evidence_path is not None:
                remote_command.extend(["--mixed-overlap-evidence", remote_mixed_overlap])
            shell_command = "cd /opt/bilive/autoslice/repo && " + " ".join(
                shlex.quote(part) for part in remote_command
            )
            completed = subprocess.run(
                ["ssh", host, shell_command], check=False, capture_output=True, text=True, timeout=1800
            )
            if completed.returncode == 0:
                for remote_source, local_target in (
                    (remote_output_srt, output_srt_path),
                    (remote_output_ass, output_ass_path),
                    (remote_manifest, output_manifest_path),
                ):
                    run(["scp", "-q", f"{host}:{remote_source}", str(local_target)], timeout=600)
            else:
                # The CLI writes a compact BLOCKED manifest before returning
                # non-zero.  Preserve it so callers see the real fail-closed
                # reason instead of an unrelated tail of ModelScope warnings.
                subprocess.run(
                    ["scp", "-q", f"{host}:{remote_manifest}", str(output_manifest_path)],
                    check=False,
                    timeout=120,
                )
        finally:
            subprocess.run(["ssh", host, f"rm -rf {shlex.quote(remote_dir)}"], check=False, timeout=120)
    if completed.returncode != 0:
        if output_manifest_path.is_file():
            try:
                blocked_manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                blocked_manifest = {}
            if (
                blocked_manifest.get("status") == "SPEAKER_REVIEW_REQUIRED"
                and blocked_manifest.get("reason")
                and valid_review_manifest(blocked_manifest)
            ):
                raise RuntimeError(
                    f"SPEAKER_REVIEW_REQUIRED: {blocked_manifest['reason']}"
                )
            if blocked_manifest.get("status") == "BLOCKED" and blocked_manifest.get("reason"):
                raise RuntimeError(
                    f"SPEAKER_FINALIZATION_BLOCKED: {blocked_manifest['reason']}"
                )
        raise RuntimeError(
            "SPEAKER_FINALIZATION_FAILED: " + (completed.stderr or completed.stdout)[-1200:]
        )
    if not output_manifest_path.is_file():
        raise RuntimeError("SPEAKER_FINALIZATION_FAILED: READY process omitted its manifest")
    manifest = json.loads(output_manifest_path.read_text(encoding="utf-8"))
    if not local_host:
        manifest = _rebase_remote_speaker_manifest(
            manifest,
            host=host,
            media_path=media_path,
            text_srt_path=text_srt_path,
            override_path=override_path,
            source_session_anchor_path=source_session_anchor_path,
            mixed_overlap_evidence_path=mixed_overlap_evidence_path,
            output_srt_path=output_srt_path,
            output_ass_path=output_ass_path,
        )
        output_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if manifest.get("status") == "SPEAKER_REVIEW_REQUIRED":
        if valid_review_manifest(manifest):
            raise RuntimeError(f"SPEAKER_REVIEW_REQUIRED: {manifest.get('reason')}")
        raise RuntimeError("SPEAKER_FINALIZATION_BLOCKED: invalid speaker review evidence")
    if manifest.get("status") != "READY" or manifest.get("production_ready") is not True:
        raise RuntimeError(f"SPEAKER_FINALIZATION_BLOCKED: {manifest.get('reason')}")
    expected = {
        output_srt_path: manifest.get("output_review_srt_sha256"),
        output_ass_path: manifest.get("output_ass_sha256"),
    }
    for path, digest in expected.items():
        if not path.is_file() or digest != _sha256(path):
            raise RuntimeError(f"SPEAKER_FINALIZATION_HASH_MISMATCH: {path}")
    if manifest.get("source_media_sha256") != frozen_inputs["source_media_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_MEDIA_BINDING_MISMATCH")
    if manifest.get("text_final_srt_sha256") != frozen_inputs["text_final_srt_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_TEXT_BINDING_MISMATCH")
    if manifest.get("profile_sha256") != frozen_inputs["profile_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_PROFILE_BINDING_MISMATCH")
    if manifest.get("speaker_override_sha256") != frozen_inputs["speaker_override_sha256"]:
        raise RuntimeError("SPEAKER_FINALIZATION_OVERRIDE_BINDING_MISMATCH")
    if (
        manifest.get("source_session_anchor_manifest_sha256")
        != frozen_inputs["source_session_anchor_manifest_sha256"]
    ):
        raise RuntimeError("SPEAKER_FINALIZATION_SOURCE_SESSION_BINDING_MISMATCH")
    if (
        manifest.get("mixed_overlap_evidence_sha256")
        != frozen_inputs["mixed_overlap_evidence_sha256"]
    ):
        raise RuntimeError("SPEAKER_FINALIZATION_MIXED_OVERLAP_BINDING_MISMATCH")
    current_inputs = {
        "source_media_sha256": _sha256(media_path),
        "text_final_srt_sha256": _sha256(text_srt_path),
        "profile_sha256": _sha256(profile),
        "speaker_override_sha256": _sha256(override_path) if override_path is not None else None,
        "source_session_anchor_manifest_sha256": (
            _sha256(source_session_anchor_path)
            if source_session_anchor_path is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": (
            _sha256(mixed_overlap_evidence_path)
            if mixed_overlap_evidence_path is not None
            else None
        ),
    }
    if current_inputs != frozen_inputs:
        raise RuntimeError("SPEAKER_FINALIZATION_INPUT_DRIFT")
    return manifest

def _default_speaker_mode() -> str:
    """Resolve the effective default speaker mode from the environment.

    argparse does not validate defaults against ``choices``, so an unknown
    env value must fail toward the standing uniform_host policy instead of
    silently reaching the finalizer dispatch.
    """

    mode = os.environ.get("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    return mode if mode in ("uniform_host", "required", "auto") else "uniform_host"


@dataclass(frozen=True)
class SpeakerFinalizationAdapters:
    """Patchable runtime seams kept explicit at the producer boundary."""

    verify_route: Callable[..., object] = verify_speaker_routing_claim
    verify_candidate_route: Callable[..., object] = verify_speaker_routing_claim_for_candidate
    write_mixed_overlap_evidence: Callable[..., Path] = _write_route_mixed_overlap_evidence
    begin_transaction: Callable[..., object] = _begin_fast_media_transaction
    derive_fresh_media: Callable[..., dict[str, object]] = _derive_fresh_fast_media
    finalize_fast: Callable[..., dict] = finalize_fast_solo_subtitles
    validate_transaction: Callable[..., None] = _validate_fast_transaction_outputs
    commit_transaction: Callable[..., None] = _commit_fast_media_transaction
    rollback_transaction: Callable[..., None] = _rollback_fast_media_transaction
    run_binary_finalizer: Callable[..., dict] = run_speaker_finalizer


def run_producer_speaker_finalization(
    *,
    speaker_mode: str,
    host: str,
    candidate_id: str,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    spec: dict,
    spec_parent: Path,
    override_path: Path | None,
    source_session_anchor_path: Path | None,
    mixed_overlap_evidence_path: Path | None,
    speaker_python: Path,
    final_source_start_ms: int | None,
    final_source_end_ms: int | None,
    adapters: SpeakerFinalizationAdapters | None = None,
) -> dict:
    """Dispatch a verified FAST route or the existing binary finalizer.

    Any missing, malformed, stale, or uncertain routing authority falls back
    to the full finalizer.  The FAST branch is unavailable when candidate-local
    override/session/mixed evidence exists, so it cannot bypass stronger
    authority or a review gate.
    """

    adapters = adapters or SpeakerFinalizationAdapters()

    if speaker_mode == "uniform_host":
        raise ValueError(
            "uniform_host mode must never dispatch speaker finalization; "
            "the caller skips this step entirely"
        )
    fallback_reason = "SPEAKER_MODE_REQUIRED"
    if speaker_mode == "auto":
        fallback_reason = "ROUTING_CLAIM_MISSING"
        claim_path = _resolved_optional_path(
            spec.get("speaker_routing_claim"), relative_to=spec_parent
        )
        expected_claim_sha256 = str(
            spec.get("speaker_routing_claim_sha256") or ""
        ).removeprefix("sha256:")
        candidate = spec.get("speaker_routing_candidate")
        if claim_path is not None and expected_claim_sha256:
            try:
                if _sha256(claim_path) != expected_claim_sha256:
                    raise SpeakerRoutingError("routing claim bytes drifted")
                claim = json.loads(claim_path.read_text(encoding="utf-8"))
                if not isinstance(claim, dict):
                    raise SpeakerRoutingError("routing claim must be an object")
                if claim.get("decision") != FAST_SOLO:
                    reasons = claim.get("reason_codes")
                    fallback_reason = (
                        "ROUTER_REQUIRED:"
                        + ",".join(str(value) for value in reasons)
                        if isinstance(reasons, list) and reasons
                        else "ROUTER_REQUIRED"
                    )
                    if isinstance(candidate, dict):
                        verified_binary = adapters.verify_candidate_route(
                            claim,
                            claim_path=claim_path,
                            expected_claim_sha256=expected_claim_sha256,
                            expected_date=str(spec.get("date") or ""),
                            expected_candidate_id=candidate_id,
                            expected_segment_binding_sha256=str(
                                candidate.get("segment_binding_sha256") or ""
                            ),
                            expected_start_ms=int(candidate["start_ms"]),
                            expected_end_ms=int(candidate["end_ms"]),
                            expected_pipeline_fingerprint=str(
                                candidate.get("pipeline_fingerprint") or ""
                            ),
                            expected_segment_path=Path(str(candidate["segment_path"])),
                            expected_segment_stat_signature=candidate[
                                "segment_stat_signature"
                            ],
                            expected_bcut_srt_path=Path(
                                str(candidate["bcut_srt_path"])
                            ),
                            expected_bcut_srt_sha256=str(
                                candidate.get("bcut_srt_sha256") or ""
                            ),
                        )
                        result = verified_binary.candidate_result
                        if (
                            result.get("mixed_or_overlap_detected") is True
                            and mixed_overlap_evidence_path is None
                        ):
                            mixed_overlap_evidence_path = (
                                adapters.write_mixed_overlap_evidence(
                                    media_path=media_path,
                                    text_srt_path=text_srt_path,
                                    work_dir=work_dir,
                                    claim=claim,
                                    verified_route=verified_binary,
                                )
                            )
                            fallback_reason = "ROUTER_MIXED_OVERLAP_REVIEW_REQUIRED"
                elif any(
                    path is not None
                    for path in (
                        override_path,
                        source_session_anchor_path,
                        mixed_overlap_evidence_path,
                    )
                ):
                    fallback_reason = "CANDIDATE_SPEAKER_AUTHORITY_REQUIRES_BINARY"
                elif not isinstance(candidate, dict):
                    fallback_reason = "ROUTING_CANDIDATE_BINDING_MISSING"
                elif (
                    final_source_start_ms is None
                    or final_source_end_ms is None
                    or not int(candidate["start_ms"])
                    <= final_source_start_ms
                    < final_source_end_ms
                    <= int(candidate["end_ms"])
                ):
                    fallback_reason = "FINAL_RECUT_OUTSIDE_ROUTING_COVERAGE"
                else:
                    verified = adapters.verify_route(
                        claim,
                        claim_path=claim_path,
                        expected_claim_sha256=expected_claim_sha256,
                        expected_date=str(spec.get("date") or ""),
                        expected_candidate_id=candidate_id,
                        expected_segment_binding_sha256=str(
                            candidate.get("segment_binding_sha256") or ""
                        ),
                        expected_start_ms=int(candidate["start_ms"]),
                        expected_end_ms=int(candidate["end_ms"]),
                        expected_pipeline_fingerprint=str(
                            candidate.get("pipeline_fingerprint") or ""
                        ),
                        expected_segment_path=Path(str(candidate["segment_path"])),
                        expected_segment_stat_signature=candidate["segment_stat_signature"],
                        expected_bcut_srt_path=Path(str(candidate["bcut_srt_path"])),
                        expected_bcut_srt_sha256=str(
                            candidate.get("bcut_srt_sha256") or ""
                        ),
                    )
                    transaction = adapters.begin_transaction(media_path)
                    try:
                        fresh_derivation = adapters.derive_fresh_media(
                            host=host,
                            media_path=media_path,
                            claimed_segment_path=Path(str(candidate["segment_path"])),
                            expected_segment_sha256=str(
                                candidate.get("segment_binding_sha256") or ""
                            ),
                            final_source_start_ms=final_source_start_ms,
                            final_source_end_ms=final_source_end_ms,
                        )
                        fast_manifest = adapters.finalize_fast(
                            media_path=media_path,
                            text_srt_path=text_srt_path,
                            output_srt_path=output_srt_path,
                            output_ass_path=output_ass_path,
                            output_manifest_path=output_manifest_path,
                            candidate_id=candidate_id,
                            routing_claim_path=claim_path,
                            verified_route=verified,
                            fresh_derivation=fresh_derivation,
                        )
                        adapters.validate_transaction(
                            manifest=fast_manifest,
                            media_path=media_path,
                            output_srt_path=output_srt_path,
                            output_ass_path=output_ass_path,
                            output_manifest_path=output_manifest_path,
                            fresh_derivation=fresh_derivation,
                        )
                        adapters.commit_transaction(transaction)
                    except Exception as fast_exc:
                        try:
                            adapters.rollback_transaction(
                                transaction,
                                output_srt_path=output_srt_path,
                                output_ass_path=output_ass_path,
                                output_manifest_path=output_manifest_path,
                            )
                        except Exception as rollback_exc:
                            raise FastMediaRollbackError(
                                "FAST failed and original media rollback could not be verified"
                            ) from rollback_exc
                        raise SpeakerRoutingError(
                            f"FAST transaction rolled back: {type(fast_exc).__name__}: {fast_exc}"
                        ) from fast_exc
                    return fast_manifest
            except (
                KeyError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                SpeakerFinalizationError,
                SpeakerRoutingError,
            ) as exc:
                fallback_reason = f"ROUTING_UNCERTAIN:{type(exc).__name__}:{exc}"

    manifest = adapters.run_binary_finalizer(
        host=host,
        candidate_id=candidate_id,
        media_path=media_path,
        text_srt_path=text_srt_path,
        output_srt_path=output_srt_path,
        output_ass_path=output_ass_path,
        output_manifest_path=output_manifest_path,
        work_dir=work_dir,
        override_path=override_path,
        source_session_anchor_path=source_session_anchor_path,
        mixed_overlap_evidence_path=mixed_overlap_evidence_path,
        speaker_python=speaker_python,
    )
    manifest["speaker_routing"] = {
        "requested_mode": speaker_mode,
        "decision": "RUN_BINARY_FINALIZER",
        "reason": fallback_reason,
    }
    output_manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
