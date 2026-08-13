"""Production host/guest speaker finalization for talk subtitles.

Contract:

1. input SRT is already text-final (ASR, terminology, pronouns, and any human
   corrections are complete);
2. CAM++ is primary; context only resolves margin-borderline cues; hash-bound
   human decisions apply last;
3. a clean text SRT remains the wording authority, while a review-labelled SRT,
   colour ASS, and evidence manifest are emitted before burn.

The ML imports are lazy so ordinary unit tests do not need the production venv.
"""

from __future__ import annotations
import argparse
import json
import statistics
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from scripts.apply_speaker_turn_overrides import (
    Cue,
    SPEAKER_SUBTITLE_STYLE_ID,
    apply_overrides,
    atomic_write_text,
    sha256_file,
    write_ass,
    write_srt,
)
from scripts.apply_subtitle_text_overrides import TextCue, parse_srt
from src.autoslice.campp_embed_once import (
    _build_embedding_similarity,
    _campp_embedding as _campp_embedding,
    _campp_runtime_fingerprint as _campp_runtime_fingerprint,
    _campp_similarity_score as _campp_similarity_score,
    _cosine_similarity as _cosine_similarity,
    _embedding_binding_sha256 as _embedding_binding_sha256,
    _load_cached_embedding as _load_cached_embedding,
    _validate_campp_embedding as _validate_campp_embedding,
    _write_cached_embedding as _write_cached_embedding,
)
from src.autoslice.host_vocal_proof import (
    _extract_checkpoint,
    _load_campplus_pipeline,
    _sha256_directory,
    _validate_profile,
)
from src.autoslice.speaker_common import (
    CAMPP_EMBEDDING_CACHE_SCHEMA as CAMPP_EMBEDDING_CACHE_SCHEMA,
    CAMPP_EMBEDDING_DIMENSION as CAMPP_EMBEDDING_DIMENSION,
    CHANNEL_PROFILE,
    FAST_FRESH_DERIVATION_SCHEMA,
    HOST_SPEAKER,
    MIXED_OVERLAP_EVIDENCE_SCHEMA as MIXED_OVERLAP_EVIDENCE_SCHEMA,
    PROFILE_ID,
    SOURCE_SESSION_ANCHOR_SCHEMA as SOURCE_SESSION_ANCHOR_SCHEMA,
    SPEAKERS,
    SPEAKER_FINALIZATION_SCHEMA,
    SpeakerFinalizationError,
    SpeakerIdentityIndeterminate,
    milliseconds as _ms,
    speaker_policy as _policy,
)
from src.autoslice.speaker_solo_prior import apply_portrait_solo_prior_from_path, identity_indeterminate_analysis
from src.autoslice.speaker_evidence import (
    validate_speaker_review_manifest_document,
    validate_mixed_overlap_evidence_document,
    _snapshot_bound_input,
    _validate_source_session_anchor_document,
    _validate_source_session_provenance,
)
from src.autoslice.speaker_context import (
    _two_means,
    resolve_ambiguous_labels,
    _singleton_nonlexical_dominant as _singleton_nonlexical_dominant,
    _resolve_singleton_outlier,
    _context_prompt as _context_prompt,
    _whole_clip_context_votes,
    _speaker_context_env as _speaker_context_env,
    _call_context_via_cpa,
)
from src.autoslice import speaker_guess
from src.autoslice.speaker_host_evidence import acoustic_hard_pass
from src.autoslice.speaker_overlap_evidence import subcue_mixed_overlap_disclosure
from src.autoslice.reviewed_speaker_baseline import (
    ReviewedSpeakerBaseline,
    build_fresh_automatic_labels,
    load_speaker_override_state,
    materialize_reviewed_automatic_labels,
    reviewed_machine_replay_evidence,
)

def _extract_cue_wavs(media_path: Path, cues: Sequence[TextCue], work_dir: Path) -> tuple[object, int, list[Path]]:
    try:
        import soundfile as sf  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - production ML runtime
        raise SpeakerFinalizationError(f"soundfile unavailable: {exc}") from exc
    wav_path = work_dir / "clip-16k-mono.wav"
    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(media_path),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if completed.returncode != 0 or not wav_path.is_file():
        raise SpeakerFinalizationError("failed to extract 16k mono audio: " + completed.stderr[-800:])
    audio, sample_rate = sf.read(str(wav_path))
    cue_dir = work_dir / "cue-wavs"
    cue_dir.mkdir(parents=True, exist_ok=True)
    cue_paths: list[Path] = []
    audio_end_ms = int(len(audio) * 1000 / sample_rate)
    for index, cue in enumerate(cues):
        start_ms, end_ms = _ms(cue.start), _ms(cue.end)
        lower = max(start_ms - 150, _ms(cues[index - 1].end) if index else 0)
        upper = min(end_ms + 150, _ms(cues[index + 1].start) if index + 1 < len(cues) else audio_end_ms)
        if upper <= lower:
            lower, upper = start_ms, end_ms
        cue_path = cue_dir / f"cue-{index + 1:04d}.wav"
        sf.write(
            str(cue_path),
            audio[int(lower * sample_rate / 1000): int(upper * sample_rate / 1000)],
            sample_rate,
        )
        cue_paths.append(cue_path)
    return audio, sample_rate, cue_paths

def _load_source_session_anchor_samples(
    manifest_path: Path,
    *,
    target_media_path: Path,
    profile_path: Path,
    references: Sequence[Mapping[str, object]],
    model_tree_sha256: str,
    host_seed_min: float,
    work_dir: Path,
    similarity: Callable[[Path, Path], float],
) -> tuple[list[Path], dict[str, object]]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpeakerFinalizationError(f"cannot read source-session anchor manifest: {exc}") from exc
    reference_hashes = {
        str(reference["id"]): str(reference["sha256"]) for reference in references
    }
    target_media_sha256 = sha256_file(target_media_path)
    document = _validate_source_session_anchor_document(
        raw,
        target_media_sha256=target_media_sha256,
        target_media_path=target_media_path,
        profile_sha256=sha256_file(profile_path),
        model_tree_sha256=model_tree_sha256,
        reference_hashes=reference_hashes,
        host_seed_min=host_seed_min,
    )
    provenance = _validate_source_session_provenance(
        document,
        target_media_path=target_media_path,
        target_media_sha256=target_media_sha256,
    )
    donor = document.get("donor")
    donor_media: Path | None = None
    donor_srt: Path | None = None
    donor_cues: list[TextCue] = []
    donor_wavs: list[Path] = []
    if isinstance(donor, Mapping):
        donor_media = Path(str(donor["media_path"])).resolve(strict=True)
        donor_srt = Path(str(donor["text_srt_path"])).resolve(strict=True)
        if sha256_file(donor_media) != donor["media_sha256"]:
            raise SpeakerFinalizationError("source-session donor media hash drift")
        if sha256_file(donor_srt) != donor["text_srt_sha256"]:
            raise SpeakerFinalizationError("source-session donor text SRT hash drift")
        donor_cues = parse_srt(donor_srt)
        donor_work_dir = work_dir / "source-session-donor"
        donor_work_dir.mkdir(parents=True, exist_ok=True)
        _audio, _sample_rate, donor_wavs = _extract_cue_wavs(
            donor_media, donor_cues, donor_work_dir
        )
    source_recording = Path(str(document["source_recording"])).resolve(strict=True)
    segment_work_dir = work_dir / "source-session-recording"
    segment_work_dir.mkdir(parents=True, exist_ok=True)
    anchor_paths: list[Path] = []
    evidence_rows: list[dict[str, object]] = []
    references_by_id = {str(reference["id"]): reference for reference in references}
    anchors = document["anchors"]
    assert isinstance(anchors, list)
    for position, raw_anchor in enumerate(anchors, start=1):
        assert isinstance(raw_anchor, Mapping)
        anchor_type = str(raw_anchor.get("anchor_type") or "donor_cue")
        if anchor_type == "donor_cue":
            cue_index = int(raw_anchor["source_cue"])
            if cue_index > len(donor_cues):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} source_cue exceeds donor SRT"
                )
            cue = donor_cues[cue_index - 1]
            expected_cue = (
                str(raw_anchor["start"]),
                str(raw_anchor["end"]),
                str(raw_anchor["text"]),
            )
            if (cue.start, cue.end, cue.text) != expected_cue:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} donor cue drift"
                )
            sample_path = donor_wavs[cue_index - 1]
            evidence_source: dict[str, object] = {
                "anchor_type": anchor_type,
                "source_cue": cue_index,
                "start": cue.start,
                "end": cue.end,
            }
        else:
            start_ms = int(raw_anchor["source_start_ms"])
            end_ms = int(raw_anchor["source_end_ms"])
            sample_path = segment_work_dir / f"anchor-{position:04d}.wav"
            try:
                _extract_checkpoint(
                    source_recording,
                    start_ms=start_ms,
                    expected_duration_ms=end_ms - start_ms,
                    output_path=sample_path,
                )
            except Exception as exc:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} source extraction failed: {exc}"
                ) from exc
            evidence_source = {
                "anchor_type": anchor_type,
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
            }
        sample_sha256 = sha256_file(sample_path)
        if sample_sha256 != raw_anchor["sample_sha256"]:
            raise SpeakerFinalizationError(f"source-session anchor {position} sample hash drift")
        actual_scores = {
            reference_id: float(
                similarity(Path(str(references_by_id[reference_id]["path"])), sample_path)
            )
            for reference_id in sorted(references_by_id)
        }
        expected_scores = raw_anchor["reference_scores"]
        assert isinstance(expected_scores, Mapping)
        for reference_id, actual_score in actual_scores.items():
            if abs(actual_score - float(expected_scores[reference_id])) > 1e-5:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} runtime score drift for {reference_id}"
                )
        enroll_median = float(statistics.median(actual_scores.values()))
        if enroll_median < host_seed_min:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} fails the unchanged host seed gate at runtime"
            )
        anchor_paths.append(sample_path)
        evidence_rows.append(
            {
                **evidence_source,
                "text": str(raw_anchor["text"]),
                "sample_sha256": sample_sha256,
                "reference_scores": actual_scores,
                "enroll_median_score": enroll_median,
            }
        )
    evidence: dict[str, object] = {
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "source_session_id": document["source_session_id"],
        "authority": document.get("authority"),
        "donor_candidate_id": donor["candidate_id"] if isinstance(donor, Mapping) else None,
        "donor_media_sha256": donor["media_sha256"] if isinstance(donor, Mapping) else None,
        "donor_text_srt_sha256": (
            donor["text_srt_sha256"] if isinstance(donor, Mapping) else None
        ),
        **provenance,
        "anchors": evidence_rows,
    }
    if isinstance(donor, Mapping):
        assert donor_media is not None and donor_srt is not None
        if sha256_file(donor_media) != donor["media_sha256"]:
            raise SpeakerFinalizationError("source-session donor media drifted during analysis")
        if sha256_file(donor_srt) != donor["text_srt_sha256"]:
            raise SpeakerFinalizationError("source-session donor text SRT drifted during analysis")
        if sha256_file(Path(str(provenance["donor_provenance"]))) != provenance[
            "donor_provenance_sha256"
        ]:
            raise SpeakerFinalizationError("source-session donor provenance drifted during analysis")
    if sha256_file(Path(str(provenance["canonical_target_media"]))) != provenance[
        "canonical_target_media_sha256"
    ]:
        raise SpeakerFinalizationError(
            "source-session canonical target media drifted during analysis"
        )
    if sha256_file(Path(str(provenance["target_provenance"]))) != provenance[
        "target_provenance_sha256"
    ]:
        raise SpeakerFinalizationError("source-session target provenance drifted during analysis")
    return anchor_paths, evidence

def _load_runtime(profile_path: Path, reference_dir: Path, model_dir: Path):
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    model_info, expected_references = _validate_profile(profile)
    actual_model_hash = _sha256_directory(model_dir)
    if actual_model_hash != model_info["tree_sha256"]:
        raise SpeakerFinalizationError("CAM++ model tree hash does not match profile")
    references = []
    for expected in expected_references:
        path = (reference_dir / expected["filename"]).resolve(strict=True)
        if sha256_file(path) != expected["sha256"]:
            raise SpeakerFinalizationError(f"voiceprint reference hash mismatch: {expected['id']}")
        references.append({**expected, "path": path})
    return profile, references, actual_model_hash, _load_campplus_pipeline(model_dir)

def _assert_runtime_assets_stable(
    *,
    model_dir: Path,
    model_tree_sha256: str,
    references: Sequence[Mapping[str, object]],
) -> None:
    if _sha256_directory(model_dir) != model_tree_sha256:
        raise SpeakerFinalizationError("CAM++ model tree drifted during speaker analysis")
    for reference in references:
        path = Path(str(reference["path"]))
        if sha256_file(path) != reference["sha256"]:
            raise SpeakerFinalizationError(
                f"voiceprint reference drifted during speaker analysis: {reference['id']}"
            )

@dataclass(frozen=True)
class _CampPlusAnchorState:
    policy: dict[str, object]
    references: list[dict[str, object]]
    model_hash: str
    cue_paths: list[Path]
    similarity: Callable[[Path, Path], float]
    seed_scores: list[float]
    clip_host_indices: list[int]
    host_indices: list[int]
    host_prints: list[Path]
    host_anchor_scope: str
    source_session_evidence: dict[str, object] | None
    reviewed_speaker_baseline: dict[str, object] | None
    host_bank_similarity: Callable[[int], float]

def _prepare_campplus_anchor_state(
    *,
    media_path: Path,
    cues: Sequence[TextCue],
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    source_session_anchor_path: Path | None,
    reviewed_anchor_labels: Mapping[int, str] | None = None,
    reviewed_speaker_baseline: Mapping[str, object] | None = None,
    guess_host_anchors: bool = False,
) -> _CampPlusAnchorState:
    """Load hash-bound runtime assets and construct the trusted host bank."""

    work_dir.mkdir(parents=True, exist_ok=True)
    profile, references, model_hash, verifier = _load_runtime(
        profile_path,
        reference_dir,
        model_dir,
    )
    policy = _policy(profile)
    _audio, _sample_rate, cue_paths = _extract_cue_wavs(media_path, cues, work_dir)
    similarity = _build_embedding_similarity(
        verifier=verifier,
        model_hash=model_hash,
        work_dir=work_dir,
    )
    seed_scores = [
        float(
            statistics.median(
                similarity(Path(reference["path"]), cue_path)
                for reference in references
            )
        )
        for cue_path in cue_paths
    ]
    anchor_count = int(policy["host_session_anchor_count"])
    clip_host_indices = [
        index
        for index in sorted(
            range(len(cues)),
            key=seed_scores.__getitem__,
            reverse=True,
        )
        if seed_scores[index] >= float(policy["host_session_seed_min"])
    ][:anchor_count]
    source_session_evidence: dict[str, object] | None = None
    if source_session_anchor_path is not None and reviewed_anchor_labels:
        raise SpeakerFinalizationError(
            "source-session anchors and reviewed speaker anchors are mutually exclusive"
        )
    if source_session_anchor_path is not None:
        host_prints, source_session_evidence = _load_source_session_anchor_samples(
            source_session_anchor_path.resolve(strict=True),
            target_media_path=media_path,
            profile_path=profile_path,
            references=references,
            model_tree_sha256=model_hash,
            host_seed_min=float(policy["host_session_seed_min"]),
            work_dir=work_dir,
            similarity=similarity,
        )
        host_indices: list[int] = []
        host_anchor_scope = "source_session"
        reviewed_baseline_evidence = None
    elif reviewed_anchor_labels:
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(cues)
            or speaker != HOST_SPEAKER
            for index, speaker in reviewed_anchor_labels.items()
        ):
            raise SpeakerFinalizationError(
                "reviewed speaker anchor labels are invalid"
            )
        host_indices = sorted(reviewed_anchor_labels)
        if len(host_indices) < 2:
            raise SpeakerFinalizationError(
                "reviewed speaker baseline supplied fewer than two host anchors"
            )
        host_prints = [cue_paths[index] for index in host_indices]
        host_anchor_scope = "reviewed_speaker_baseline"
        source_session_evidence = None
        reviewed_baseline_evidence = dict(reviewed_speaker_baseline or {})
    else:
        host_indices = clip_host_indices
        host_anchor_scope = "clip"
        if len(host_indices) < 2:
            # Ivan 2026-08-10「我要的就是正常分离两说话人，尽最大努力分开」：
            # 清过 host_session_seed_min 的 cue 不够两条时，**阈值一字不动**，
            # 改成明示降级——提名 seed 分最高的几条当锚点，下游整条分离链照跑。
            # 本体（含逐条"没清过阈值"的披露）在 src/autoslice/speaker_guess.py。
            nomination = (
                speaker_guess.nominate_host_anchors(
                    seed_scores,
                    anchor_count=anchor_count,
                    seed_min=float(policy["host_session_seed_min"]),
                )
                if guess_host_anchors
                else None
            )
            if nomination is None:
                raise SpeakerIdentityIndeterminate(
                    f"not enough {CHANNEL_PROFILE.prompt_name} clip anchors: {host_indices}"
                )
            host_indices = nomination[0]
            host_anchor_scope = speaker_guess.GUESSED_HOST_ANCHOR_SCOPE
        host_prints = [cue_paths[index] for index in host_indices]
        reviewed_baseline_evidence = None

    score_cache: dict[int, float] = {}

    def host_bank_similarity(index: int) -> float:
        if index in score_cache:
            return score_cache[index]
        values = [similarity(host, cue_paths[index]) for host in host_prints]
        score = (
            float(max(values))
            if host_anchor_scope == "source_session"
            else float(statistics.mean(values))
        )
        score_cache[index] = score
        return score

    return _CampPlusAnchorState(
        policy=dict(policy),
        references=references,
        model_hash=model_hash,
        cue_paths=cue_paths,
        similarity=similarity,
        seed_scores=seed_scores,
        clip_host_indices=clip_host_indices,
        host_indices=host_indices,
        host_prints=host_prints,
        host_anchor_scope=host_anchor_scope,
        source_session_evidence=source_session_evidence,
        reviewed_speaker_baseline=reviewed_baseline_evidence,
        host_bank_similarity=host_bank_similarity,
    )

def _run_campplus_analysis(
    *,
    media_path: Path,
    cues: Sequence[TextCue],
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    context_call: Callable[[str], str] | None,
    reviewed_context_votes: Mapping[int, str] | None = None,
    source_session_anchor_path: Path | None = None,
    reviewed_anchor_labels: Mapping[int, str] | None = None,
    reviewed_speaker_baseline: Mapping[str, object] | None = None,
    text_srt_path: Path | None = None,
    guess_host_anchors: bool = False,
) -> dict[str, object]:
    anchors = _prepare_campplus_anchor_state(
        media_path=media_path,
        cues=cues,
        profile_path=profile_path,
        reference_dir=reference_dir,
        model_dir=model_dir,
        work_dir=work_dir,
        source_session_anchor_path=source_session_anchor_path,
        reviewed_anchor_labels=reviewed_anchor_labels,
        reviewed_speaker_baseline=reviewed_speaker_baseline,
        guess_host_anchors=guess_host_anchors,
    )
    policy = anchors.policy
    references = anchors.references
    model_hash = anchors.model_hash
    cue_paths = anchors.cue_paths
    similarity = anchors.similarity
    seed_scores = anchors.seed_scores
    clip_host_indices = anchors.clip_host_indices
    host_indices = anchors.host_indices
    host_prints = anchors.host_prints
    host_anchor_scope = anchors.host_anchor_scope
    source_session_evidence = anchors.source_session_evidence
    reviewed_baseline_evidence = anchors.reviewed_speaker_baseline
    host_bank_similarity = anchors.host_bank_similarity

    guest_candidates: list[int] = []
    for index in sorted(range(len(cues)), key=seed_scores.__getitem__):
        if seed_scores[index] > float(policy["guest_seed_max"]) or index in host_indices:
            continue
        if _ms(cues[index].end) - _ms(cues[index].start) < int(policy["guest_min_duration_ms"]):
            continue
        session_similarity = host_bank_similarity(index)
        if session_similarity >= float(policy["guest_session_similarity_max"]):
            continue
        guest_candidates.append(index)
        if len(guest_candidates) >= 8:
            break

    if len(guest_candidates) < 2:
        median_seed = statistics.median(seed_scores)
        raw_long_low = [
            index for index, cue in enumerate(cues)
            if _ms(cue.end) - _ms(cue.start) >= int(policy["guest_min_duration_ms"])
            and seed_scores[index] < float(policy["guest_seed_max"])
        ]
        host_explained_low = (
            [
                index
                for index in raw_long_low
                if host_bank_similarity(index)
                >= float(policy["guest_session_similarity_max"])
            ]
            if host_anchor_scope == "source_session"
            else []
        )
        unexplained_long_low = [
            index for index in raw_long_low if index not in host_explained_low
        ]
        if len(guest_candidates) == 1 and unexplained_long_low == guest_candidates:
            singleton_index = guest_candidates[0]
            singleton = _resolve_singleton_outlier(
                cues=cues,
                singleton_index=singleton_index,
                seed_scores=seed_scores,
                host_bank_scores={
                    index: host_bank_similarity(index) for index in range(len(cues))
                },
                clip_host_indices=clip_host_indices,
                policy=policy,
                cue_audio_sha256=[sha256_file(path) for path in cue_paths],
                context_call=context_call,
                reviewed_context_votes=reviewed_context_votes,
            )
            _assert_runtime_assets_stable(
                model_dir=model_dir,
                model_tree_sha256=model_hash,
                references=references,
            )
            return {
                **singleton,
                "host_anchor_scope": host_anchor_scope,
                "source_session_anchor": source_session_evidence,
                "reviewed_speaker_baseline": reviewed_baseline_evidence,
                "policy": policy,
                "model_tree_sha256": model_hash,
                "reference_hashes": {
                    str(reference["id"]): str(reference["sha256"])
                    for reference in references
                },
                "host_anchor_cues": [index + 1 for index in host_indices],
                "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
                "host_explained_low_cues": [index + 1 for index in host_explained_low],
                "guest_anchor_groups": [],
                "threshold": None,
            }
        if median_seed < float(policy["single_host_median_seed_min"]) or unexplained_long_low:
            raise SpeakerIdentityIndeterminate(
                f"guest evidence exists but purified guest anchors are insufficient: {guest_candidates}"
            )
        _assert_runtime_assets_stable(
            model_dir=model_dir,
            model_tree_sha256=model_hash,
            references=references,
        )
        return {
            "mode": "single_host",
            "multi_speaker_detected": False,
            "host_anchor_scope": host_anchor_scope,
            "source_session_anchor": source_session_evidence,
            "reviewed_speaker_baseline": reviewed_baseline_evidence,
            "policy": policy,
            "model_tree_sha256": model_hash,
            "reference_hashes": {str(reference["id"]): str(reference["sha256"]) for reference in references},
            "host_anchor_cues": [index + 1 for index in host_indices],
            "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
            "host_explained_low_cues": [index + 1 for index in host_explained_low],
            "guest_anchor_groups": [],
            "threshold": None,
            "decisions": [
                {
                    "source_index": index + 1,
                    "speaker": HOST_SPEAKER,
                    "decision_source": "campp_single_host",
                    "seed_score": round(seed_scores[index], 8),
                    "margin": None,
                }
                for index in range(len(cues))
            ],
        }

    seed_guest = guest_candidates[0]
    group_a, group_b = [seed_guest], []
    for index in guest_candidates[1:]:
        target = group_a if similarity(cue_paths[seed_guest], cue_paths[index]) >= float(policy["guest_cluster_similarity_min"]) else group_b
        target.append(index)
    guest_groups = [group[:4] for group in (group_a, group_b) if group]

    host_scores: list[float] = []
    guest_scores: list[float] = []
    margins: list[float] = []
    for index, cue_path in enumerate(cue_paths):
        host_values = [similarity(path, cue_path) for path in host_prints if path != cue_path]
        host_score = statistics.mean(host_values or [1.0])
        guest_score = max(
            statistics.mean(
                [similarity(cue_paths[guest_index], cue_path) for guest_index in group if guest_index != index]
                or [0.0]
            )
            for group in guest_groups
        )
        host_scores.append(float(host_score))
        guest_scores.append(float(guest_score))
        margins.append(float(host_score - guest_score))
    low_center, high_center, threshold = _two_means(margins)
    band = float(policy["ambiguity_band"])
    labels: list[str | None] = []
    ambiguous: list[int] = []
    for index, margin in enumerate(margins):
        hard_speaker = acoustic_hard_pass(margin, threshold, band)
        if hard_speaker is None:
            labels.append(None)
            ambiguous.append(index)
        else:
            labels.append(hard_speaker)

    reviewed_votes = {
        index: speaker
        for index, speaker in (reviewed_context_votes or {}).items()
        if index in ambiguous and speaker in SPEAKERS
    }
    # Ivan 2026-08-07: the whole-clip judge is a corroborating signal, not an
    # independent one, so its votes must carry a real confidence value that
    # resolve_ambiguous_labels/speaker_host_evidence can gate on.
    context_vote_rows, context_attempts, context_errors = _whole_clip_context_votes(
        cues,
        labels,
        ambiguous,
        context_call,
        initial_speakers=reviewed_votes,
        require_confidence=True,
    )
    votes = {
        index: str(row["speaker"]) for index, row in context_vote_rows.items()
    }
    confidences = {
        index: float(row["confidence"])
        for index, row in context_vote_rows.items()
        if row.get("confidence") is not None
    }
    unresolved_context = [index for index in ambiguous if index not in votes]
    resolved, sources = resolve_ambiguous_labels(
        labels,
        margins,
        threshold,
        votes,
        band=band,
        policy=policy,
        context_confidences=confidences,
    )
    for index in reviewed_votes:
        if index in ambiguous:
            sources[index] = "accepted_context_baseline"

    # Ivan 2026-08-07: no neighbour-island smoothing toward HOST -- ambiguous
    # cues already default to GUEST (speaker_host_evidence), so there is
    # nothing left to smooth without manufacturing HOST from adjacency alone.

    _assert_runtime_assets_stable(
        model_dir=model_dir,
        model_tree_sha256=model_hash,
        references=references,
    )
    return {
        "mode": "multi_speaker",
        "multi_speaker_detected": True,
        "host_anchor_scope": host_anchor_scope,
        "source_session_anchor": source_session_evidence,
        "reviewed_speaker_baseline": reviewed_baseline_evidence,
        "policy": policy,
        "model_tree_sha256": model_hash,
        "reference_hashes": {str(reference["id"]): str(reference["sha256"]) for reference in references},
        "host_anchor_cues": [index + 1 for index in host_indices],
        "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
        "guest_anchor_groups": [[index + 1 for index in group] for group in guest_groups],
        "cluster_centers": {"guest": low_center, PROFILE_ID: high_center},
        "threshold": threshold,
        "context_attempts": context_attempts,
        "context_errors": context_errors,
        "context_votes": {str(index + 1): speaker for index, speaker in votes.items()},
        "reviewed_context_votes": {
            str(index + 1): speaker for index, speaker in reviewed_votes.items()
        },
        "context_required_cues": [index + 1 for index in ambiguous],
        "context_unresolved_cues": [index + 1 for index in unresolved_context],
        # F5 披露专用（Ivan 2026-08-09「证据只披露不改标签」）：产出 mixed gate 可
        # 消费的 sidecar，但本轮不喂回门、不改 decisions。见 speaker_overlap_evidence。
        "subcue_mixed_overlap": subcue_mixed_overlap_disclosure(cues=cues, cue_audio_paths=cue_paths, media_path=media_path, text_srt_path=text_srt_path, work_dir=work_dir, host_prints=host_prints, guest_groups=guest_groups, threshold=threshold, band=band, similarity=similarity) if text_srt_path is not None else None,
        "decisions": [
            {
                "source_index": index + 1,
                "speaker": resolved[index],
                "decision_source": sources[index],
                "seed_score": round(seed_scores[index], 8),
                "host_score": round(host_scores[index], 8),
                "guest_score": round(guest_scores[index], 8),
                "margin": round(margins[index], 8),
            }
            for index in range(len(cues))
        ],
    }

@dataclass(frozen=True)
class _BoundSpeakerInputs:
    media_path: Path
    text_srt_path: Path
    profile_path: Path
    profile_snapshot: Path
    profile_sha256: str
    cues: list[TextCue]
    source_session_anchor_original: Path | None
    source_session_anchor_snapshot: Path | None
    source_session_anchor_sha256: str | None
    mixed_overlap_evidence_original: Path | None
    mixed_overlap_evidence_snapshot: Path | None
    mixed_overlap_evidence_sha256: str | None

def _snapshot_speaker_inputs(
    *,
    media_path: Path,
    text_srt_path: Path,
    profile_path: Path,
    work_dir: Path,
    source_session_anchor_path: Path | None,
    mixed_overlap_evidence_path: Path | None,
) -> _BoundSpeakerInputs:
    """Resolve and freeze every mutable authority consumed by finalization."""

    media_path = media_path.resolve(strict=True)
    text_srt_path = text_srt_path.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile_snapshot, profile_sha256 = _snapshot_bound_input(
        profile_path,
        work_dir / "bound-inputs" / "voiceprint-profile.json",
    )
    source_original: Path | None = None
    source_snapshot: Path | None = None
    source_sha256: str | None = None
    if source_session_anchor_path is not None:
        source_original = source_session_anchor_path.resolve(strict=True)
        source_snapshot, source_sha256 = _snapshot_bound_input(
            source_original,
            work_dir / "bound-inputs" / "source-session-anchors.json",
        )
    mixed_original: Path | None = None
    mixed_snapshot: Path | None = None
    mixed_sha256: str | None = None
    if mixed_overlap_evidence_path is not None:
        mixed_original = mixed_overlap_evidence_path.resolve(strict=True)
        mixed_snapshot, mixed_sha256 = _snapshot_bound_input(
            mixed_original,
            work_dir / "bound-inputs" / "mixed-overlap-evidence.json",
        )
    return _BoundSpeakerInputs(
        media_path=media_path,
        text_srt_path=text_srt_path,
        profile_path=profile_path,
        profile_snapshot=profile_snapshot,
        profile_sha256=profile_sha256,
        cues=parse_srt(text_srt_path),
        source_session_anchor_original=source_original,
        source_session_anchor_snapshot=source_snapshot,
        source_session_anchor_sha256=source_sha256,
        mixed_overlap_evidence_original=mixed_original,
        mixed_overlap_evidence_snapshot=mixed_snapshot,
        mixed_overlap_evidence_sha256=mixed_sha256,
    )

@dataclass(frozen=True)
class _MixedOverlapGate:
    document: Mapping[str, object] | None
    review_manifest: dict[str, object] | None


def _evaluate_mixed_overlap_gate(
    *,
    bound: _BoundSpeakerInputs,
    override_document: Mapping[str, object] | None,
    override_path: Path | None,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
) -> _MixedOverlapGate:
    """Validate provider evidence and fail closed before acoustic analysis."""

    if bound.mixed_overlap_evidence_snapshot is None:
        return _MixedOverlapGate(None, None)
    try:
        document = json.loads(
            bound.mixed_overlap_evidence_snapshot.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise SpeakerFinalizationError(
            f"mixed/overlap evidence is invalid JSON: {exc}"
        ) from exc
    media_sha256 = sha256_file(bound.media_path)
    text_sha256 = sha256_file(bound.text_srt_path)
    mixed_rows = validate_mixed_overlap_evidence_document(
        document,
        expected_media_sha256=media_sha256,
        expected_text_sha256=text_sha256,
        cues=bound.cues,
        expected_audio_root=bound.mixed_overlap_evidence_original.parent,
    )
    override_sources = {
        int(item.get("source_cue", 0))
        for item in ((override_document or {}).get("overrides") or [])
        if isinstance(item, Mapping)
    }
    remaining_rows = [
        row for row in mixed_rows if int(row["source_cue"]) not in override_sources
    ]
    if not remaining_rows:
        return _MixedOverlapGate(document, None)
    if (
        sha256_file(bound.mixed_overlap_evidence_original)
        != bound.mixed_overlap_evidence_sha256
    ):
        raise SpeakerFinalizationError("mixed/overlap evidence drifted during validation")
    output_srt_path.unlink(missing_ok=True)
    output_ass_path.unlink(missing_ok=True)
    reason_codes = sorted(
        {
            str(reason)
            for row in remaining_rows
            for reason in row.get("reason_codes", [])
        }
    )
    unresolved = [int(row["source_cue"]) for row in remaining_rows]
    review_manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "SPEAKER_REVIEW_REQUIRED",
        "production_ready": False,
        "reason_code": "SPEAKER_REVIEW_REQUIRED",
        "reason": "mixed/overlap speaker evidence requires review: "
        + ",".join(reason_codes),
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(bound.media_path),
        "source_media_sha256": media_sha256,
        "text_final_srt": str(bound.text_srt_path),
        "text_final_srt_sha256": text_sha256,
        "profile": str(bound.profile_path.resolve()),
        "profile_sha256": bound.profile_sha256,
        "speaker_override": str(override_path.resolve()) if override_path is not None else None,
        "speaker_override_sha256": sha256_file(override_path)
        if override_path is not None
        else None,
        "source_session_anchor_manifest": (
            str(bound.source_session_anchor_original)
            if bound.source_session_anchor_original is not None
            else None
        ),
        "source_session_anchor_manifest_sha256": bound.source_session_anchor_sha256,
        "mixed_overlap_evidence": str(bound.mixed_overlap_evidence_original),
        "mixed_overlap_evidence_sha256": bound.mixed_overlap_evidence_sha256,
        "source_cue_count": len(bound.cues),
        "context_unresolved_cues": unresolved,
        "review_reason_codes": reason_codes,
        "review_required_cues": remaining_rows,
        "analysis": {
            "mode": "provider_mixed_overlap_gate",
            "provider": document.get("provider"),
        },
    }
    validate_speaker_review_manifest_document(
        review_manifest,
        expected_media_sha256=media_sha256,
        expected_text_sha256=text_sha256,
        cues=bound.cues,
    )
    atomic_write_text(
        output_manifest_path,
        json.dumps(review_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return _MixedOverlapGate(document, review_manifest)


def _run_bound_speaker_analysis(
    *,
    bound: _BoundSpeakerInputs,
    reference_dir: Path,
    model_dir: Path,
    work_dir: Path,
    analyzer: Callable[..., dict[str, object]],
    context_call: Callable[[str], str] | None,
    reviewed_votes: Mapping[int, str],
    reviewed_baseline: ReviewedSpeakerBaseline | None,
    mixed_overlap_document: Mapping[str, object] | None,
    guess_host_anchors: bool = False,
) -> dict[str, object]:
    """Run the analyzer, then prove every bound input stayed unchanged."""

    identity_error: SpeakerIdentityIndeterminate | None = None
    try:
        analysis = analyzer(
            media_path=bound.media_path,
            cues=bound.cues,
            profile_path=bound.profile_snapshot,
            reference_dir=reference_dir,
            model_dir=model_dir,
            work_dir=work_dir,
            context_call=context_call,
            reviewed_context_votes=reviewed_votes,
            source_session_anchor_path=bound.source_session_anchor_snapshot,
        reviewed_anchor_labels=(
            reviewed_baseline.anchor_labels if reviewed_baseline is not None else None
        ),
        reviewed_speaker_baseline=(
            reviewed_baseline.evidence if reviewed_baseline is not None else None
        ),
            text_srt_path=bound.text_srt_path,
            # 只在真的要猜时才传，既有 analyzer 假件（**_kwargs 之外的严格签名）
            # 在非猜路径上一个字节都感觉不到。
            **({"guess_host_anchors": True} if guess_host_anchors else {}),
        )
    except SpeakerIdentityIndeterminate as exc:
        identity_error = exc
        analysis = None
    if sha256_file(bound.profile_path) != bound.profile_sha256:
        raise SpeakerFinalizationError("voiceprint profile drifted during speaker analysis")
    if (
        bound.source_session_anchor_original is not None
        and sha256_file(bound.source_session_anchor_original)
        != bound.source_session_anchor_sha256
    ):
        raise SpeakerFinalizationError(
            "source-session anchor manifest drifted during speaker analysis"
        )
    if (
        bound.mixed_overlap_evidence_original is not None
        and sha256_file(bound.mixed_overlap_evidence_original)
        != bound.mixed_overlap_evidence_sha256
    ):
        raise SpeakerFinalizationError(
            "mixed/overlap evidence drifted during speaker analysis"
        )
    if mixed_overlap_document is not None:
        validate_mixed_overlap_evidence_document(
            mixed_overlap_document,
            expected_media_sha256=sha256_file(bound.media_path),
            expected_text_sha256=sha256_file(bound.text_srt_path),
            cues=bound.cues,
            expected_audio_root=bound.mixed_overlap_evidence_original.parent,
        )
    if identity_error is not None:
        raise identity_error
    assert analysis is not None
    return analysis


@dataclass(frozen=True)
class _SpeakerLabels:
    automatic: list[Cue]
    final: list[Cue]
    automatic_srt: Path
    fresh_automatic_srt: Path | None


def _materialize_speaker_labels(
    analysis: Mapping[str, object],
    *,
    cues: Sequence[TextCue],
    work_dir: Path,
    override_document: Mapping[str, object] | None,
    expected_automatic_sha256: str,
    reviewed_baseline: ReviewedSpeakerBaseline | None,
) -> _SpeakerLabels:
    """Turn analyzer decisions into the immutable automatic and final cue sets."""

    fresh_automatic = build_fresh_automatic_labels(analysis, cues)
    automatic, automatic_srt, fresh_automatic_srt = (
        materialize_reviewed_automatic_labels(
            fresh_automatic,
            work_dir=work_dir,
            baseline=reviewed_baseline,
        )
    )
    final_cues = automatic
    if override_document is not None:
        actual_automatic = sha256_file(automatic_srt)
        if (
            fresh_automatic_srt is None
            and expected_automatic_sha256
            and expected_automatic_sha256 != actual_automatic
        ):
            raise SpeakerFinalizationError(
                "speaker override source hash mismatch: "
                f"expected {expected_automatic_sha256!r}, got {actual_automatic!r}"
            )
        final_cues = apply_overrides(automatic, override_document)
    return _SpeakerLabels(
        automatic,
        final_cues,
        automatic_srt,
        fresh_automatic_srt,
    )


def _resolve_unresolved_speaker_gate(
    analysis: Mapping[str, object],
    *,
    bound: _BoundSpeakerInputs,
    override_document: Mapping[str, object] | None,
    override_path: Path | None,
    automatic_srt: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
) -> dict[str, object] | None:
    """Emit a validated review manifest or prove no unresolved cues remain."""

    unresolved_raw = analysis.get("context_unresolved_cues") or []
    if not isinstance(unresolved_raw, list) or any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in unresolved_raw
    ):
        raise SpeakerFinalizationError(
            "speaker analyzer returned invalid unresolved-context evidence"
        )
    override_sources = {
        int(item.get("source_cue", 0))
        for item in ((override_document or {}).get("overrides") or [])
        if isinstance(item, Mapping)
    }
    remaining_unresolved = sorted(set(unresolved_raw) - override_sources)
    if not remaining_unresolved:
        return None
    if analysis.get("review_required") is not True:
        raise SpeakerFinalizationError(
            "whole-clip context did not resolve ambiguous speaker cues: "
            + ",".join(str(value) for value in remaining_unresolved)
        )
    output_srt_path.unlink(missing_ok=True)
    output_ass_path.unlink(missing_ok=True)
    reason_codes_raw = analysis.get("review_reason_codes") or []
    if not isinstance(reason_codes_raw, list) or any(
        not isinstance(value, str) or not value.strip() for value in reason_codes_raw
    ):
        raise SpeakerFinalizationError("speaker analyzer returned invalid review reason codes")
    reason_codes = list(dict.fromkeys(reason_codes_raw))
    review_rows = analysis.get("review_required_cues")
    if review_rows is None:
        review_rows = analysis.get("singleton_evidence") or []
    media_sha256 = sha256_file(bound.media_path)
    text_sha256 = sha256_file(bound.text_srt_path)
    review_manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "SPEAKER_REVIEW_REQUIRED",
        "production_ready": False,
        "reason_code": "SPEAKER_REVIEW_REQUIRED",
        "reason": "speaker evidence requires review: "
        + ",".join(reason_codes or ["UNRESOLVED_SPEAKER_EVIDENCE"]),
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(bound.media_path),
        "source_media_sha256": media_sha256,
        "text_final_srt": str(bound.text_srt_path),
        "text_final_srt_sha256": text_sha256,
        "profile": str(bound.profile_path.resolve()),
        "profile_sha256": bound.profile_sha256,
        "automatic_labelled_srt": str(automatic_srt.resolve()),
        "automatic_labelled_srt_sha256": sha256_file(automatic_srt),
        "speaker_override": str(override_path.resolve()) if override_path is not None else None,
        "speaker_override_sha256": sha256_file(override_path)
        if override_path is not None
        else None,
        "source_session_anchor_manifest": (
            str(bound.source_session_anchor_original)
            if bound.source_session_anchor_original is not None
            else None
        ),
        "source_session_anchor_manifest_sha256": bound.source_session_anchor_sha256,
        "mixed_overlap_evidence": (
            str(bound.mixed_overlap_evidence_original)
            if bound.mixed_overlap_evidence_original is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": bound.mixed_overlap_evidence_sha256,
        "host_anchor_scope": analysis.get("host_anchor_scope", "clip"),
        "source_cue_count": len(bound.cues),
        "context_unresolved_cues": remaining_unresolved,
        "review_reason_codes": reason_codes,
        "review_required_cues": review_rows,
        "analysis": analysis,
    }
    validate_speaker_review_manifest_document(
        review_manifest,
        expected_media_sha256=media_sha256,
        expected_text_sha256=text_sha256,
        cues=bound.cues,
    )
    atomic_write_text(
        output_manifest_path,
        json.dumps(review_manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return review_manifest


def _write_ready_speaker_delivery(
    *,
    bound: _BoundSpeakerInputs,
    analysis: Mapping[str, object],
    final_cues: Sequence[Cue],
    automatic_srt: Path,
    override_path: Path | None,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    guess: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Atomically materialize the subtitle artifacts and their bindings.

    ``guess`` 不为空时产物照写、绑定照做，但状态是 ``SPEAKER_GUESS`` 且
    ``production_ready`` 恒 False——"READY = 证据充分"这条不变量一个字节都不稀释，
    下游（producer 门、包内文件名、运行时状态）全程按状态区分两种产物。
    """

    write_srt(final_cues, output_srt_path)
    write_ass(final_cues, output_ass_path, show_speaker_labels=False)
    manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": speaker_guess.SPEAKER_GUESS_STATUS if guess else "READY",
        "production_ready": guess is None,
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(bound.media_path),
        "source_media_sha256": sha256_file(bound.media_path),
        "text_final_srt": str(bound.text_srt_path),
        "text_final_srt_sha256": sha256_file(bound.text_srt_path),
        "profile": str(bound.profile_path.resolve()),
        "profile_sha256": bound.profile_sha256,
        "automatic_labelled_srt_sha256": sha256_file(automatic_srt),
        "speaker_override": str(override_path.resolve()) if override_path is not None else None,
        "speaker_override_sha256": sha256_file(override_path)
        if override_path is not None
        else None,
        "source_session_anchor_manifest": (
            str(bound.source_session_anchor_original)
            if bound.source_session_anchor_original is not None
            else None
        ),
        "source_session_anchor_manifest_sha256": bound.source_session_anchor_sha256,
        "mixed_overlap_evidence": (
            str(bound.mixed_overlap_evidence_original)
            if bound.mixed_overlap_evidence_original is not None
            else None
        ),
        "mixed_overlap_evidence_sha256": bound.mixed_overlap_evidence_sha256,
        "host_anchor_scope": analysis.get("host_anchor_scope", "clip"),
        "source_session_id": (
            (analysis.get("source_session_anchor") or {}).get("source_session_id")
            if isinstance(analysis.get("source_session_anchor"), Mapping)
            else None
        ),
        "output_review_srt": str(output_srt_path.resolve()),
        "output_review_srt_sha256": sha256_file(output_srt_path),
        "output_ass": str(output_ass_path.resolve()),
        "output_ass_sha256": sha256_file(output_ass_path),
        "visible_speaker_prefixes": False,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
        "speaker_taxonomy": "binary_visual_host_vs_guest",
        "host_identity_aliases": list(CHANNEL_PROFILE.speaker_identity_aliases),
        "source_cue_count": len(bound.cues),
        "output_cue_count": len(final_cues),
        "reviewed_output_cue_count": sum(
            cue.decision_source.startswith("reviewed_") for cue in final_cues
        ),
        "accepted_context_output_cue_count": sum(
            cue.decision_source == "accepted_context_baseline" for cue in final_cues
        ),
        "overlap_output_cue_count": sum(cue.placement == "above" for cue in final_cues),
        "solo_prior": analysis.get("solo_prior"),
        "solo_prior_receipt": analysis.get("solo_prior_receipt"),
        "speaker_guess": dict(guess) if guess else None,
        "analysis": analysis,
        "final_decisions": [asdict(cue) for cue in final_cues],
    }
    atomic_write_text(
        output_manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest
def finalize_speaker_subtitles(
    *,
    media_path: Path,
    text_srt_path: Path,
    profile_path: Path,
    reference_dir: Path,
    model_dir: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    work_dir: Path,
    candidate_id: str | None = None,
    override_path: Path | None = None,
    source_session_anchor_path: Path | None = None,
    mixed_overlap_evidence_path: Path | None = None,
    speaker_session_context_path: Path | None = None,
    analyzer: Callable[..., dict[str, object]] = _run_campplus_analysis,
    context_call: Callable[[str], str] | None = None,
    best_effort_guess: bool = False,
    expected_source_recording: Mapping[str, object] | None = None,
) -> dict[str, object]:
    bound = _snapshot_speaker_inputs(
        media_path=media_path,
        text_srt_path=text_srt_path,
        profile_path=profile_path,
        work_dir=work_dir,
        source_session_anchor_path=source_session_anchor_path,
        mixed_overlap_evidence_path=mixed_overlap_evidence_path,
    )
    override_state = load_speaker_override_state(
        override_path,
        candidate_id=candidate_id,
        media_path=bound.media_path,
        text_srt_path=bound.text_srt_path,
        cue_count=len(bound.cues),
        expected_source_recording=expected_source_recording,
    )
    override_document = override_state.document
    reviewed_votes = override_state.reviewed_votes
    reviewed_baseline = override_state.reviewed_baseline
    expected_automatic = override_state.expected_automatic_sha256
    mixed_gate = _evaluate_mixed_overlap_gate(
        bound=bound,
        override_document=override_document,
        override_path=override_path,
        output_srt_path=output_srt_path,
        output_ass_path=output_ass_path,
        output_manifest_path=output_manifest_path,
    )
    if mixed_gate.review_manifest is not None:
        return mixed_gate.review_manifest
    mixed_overlap_document = mixed_gate.document
    identity_error: SpeakerIdentityIndeterminate | None = None

    def analyze(guess_host_anchors: bool = False) -> dict[str, object]:
        return _run_bound_speaker_analysis(
            bound=bound,
            reference_dir=reference_dir,
            model_dir=model_dir,
            work_dir=work_dir,
            analyzer=analyzer,
            context_call=context_call,
            reviewed_votes=reviewed_votes,
            reviewed_baseline=reviewed_baseline,
            mixed_overlap_document=mixed_overlap_document,
            guess_host_anchors=guess_host_anchors,
        )

    try:
        analysis = analyze()
    except SpeakerIdentityIndeterminate as exc:
        identity_error = exc
        analysis = identity_indeterminate_analysis(cue_count=len(bound.cues), reason=str(exc))
    analysis = apply_portrait_solo_prior_from_path(
        analysis,
        context_path=(speaker_session_context_path if override_document is None else None),
        candidate_id=candidate_id,
    )
    ladder = speaker_guess.GuessLadder(best_effort_guess)
    if identity_error is not None and analysis.get("solo_prior") != "portrait":
        # portrait solo prior 优先级更高，上面已放行；它也救不回来时才轮到猜。
        analysis = ladder.escalate(
            identity_error, retry=analyze, cue_count=len(bound.cues)
        )
    labels = _materialize_speaker_labels(
        analysis,
        cues=bound.cues,
        work_dir=work_dir,
        override_document=override_document,
        expected_automatic_sha256=expected_automatic,
        reviewed_baseline=reviewed_baseline,
    )
    automatic_srt = labels.automatic_srt
    final_cues = labels.final
    if labels.fresh_automatic_srt is not None and reviewed_baseline is not None:
        analysis = dict(analysis)
        analysis["reviewed_machine_baseline_replay"] = reviewed_machine_replay_evidence(
            analysis=analysis,
            selected=labels.automatic,
            selected_path=automatic_srt,
            fresh_path=labels.fresh_automatic_srt,
            baseline=reviewed_baseline,
        )
    try:
        review_manifest = _resolve_unresolved_speaker_gate(
            analysis,
            bound=bound,
            override_document=override_document,
            override_path=override_path,
            automatic_srt=automatic_srt,
            output_srt_path=output_srt_path,
            output_ass_path=output_ass_path,
            output_manifest_path=output_manifest_path,
        )
    except SpeakerFinalizationError as exc:
        ladder.absorb_gate_stop(exc)  # 非"语境没定"的同类型异常在这里原样重抛
        review_manifest = None
    if review_manifest is not None and not best_effort_guess:
        return review_manifest
    # 梯子第二级：分离已经跑完，只是若干 cue 语境未定。原本这里删产物、交一份
    # SPEAKER_REVIEW_REQUIRED 就走人（Ivan 8/10：那样我什么也看不到）。
    ladder.absorb_gate_manifest(review_manifest)
    return _write_ready_speaker_delivery(
        bound=bound,
        analysis=analysis,
        final_cues=final_cues,
        automatic_srt=automatic_srt,
        override_path=override_path,
        output_srt_path=output_srt_path,
        output_ass_path=output_ass_path,
        output_manifest_path=output_manifest_path,
        guess=ladder.receipt(analysis, review_manifest),
    )

def finalize_fast_solo_subtitles(
    *,
    media_path: Path,
    text_srt_path: Path,
    output_srt_path: Path,
    output_ass_path: Path,
    output_manifest_path: Path,
    candidate_id: str,
    routing_claim_path: Path,
    verified_route: object,
    fresh_derivation: Mapping[str, object],
) -> dict[str, object]:
    """Render an all-host result from an already verified session authority.

    This function intentionally has no profile, reference, model, analyzer, or
    context arguments.  The opaque ``VerifiedFastSoloRoute`` value is produced
    only by the current-state verifier in ``speaker_session_router``.
    """

    from src.autoslice.speaker_session_router import VerifiedFastSoloRoute

    if not isinstance(verified_route, VerifiedFastSoloRoute):
        raise SpeakerFinalizationError("FAST_SOLO renderer requires a verified route")
    if verified_route.candidate_id != candidate_id:
        raise SpeakerFinalizationError("FAST_SOLO candidate authority mismatch")
    media_path = media_path.resolve(strict=True)
    text_srt_path = text_srt_path.resolve(strict=True)
    routing_claim_path = routing_claim_path.resolve(strict=True)
    source_media_sha256 = sha256_file(media_path)
    text_final_srt_sha256 = sha256_file(text_srt_path)
    expected_derivation_keys = {
        "schema_version",
        "method",
        "cache_reused",
        "source_path",
        "source_sha256",
        "absolute_source_start_ms",
        "absolute_source_end_ms",
        "expected_duration_ms",
        "actual_duration_ms",
        "output_path",
        "output_sha256",
    }
    if not isinstance(fresh_derivation, Mapping) or set(fresh_derivation) != expected_derivation_keys:
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation schema is incomplete")
    if (
        fresh_derivation.get("schema_version") != FAST_FRESH_DERIVATION_SCHEMA
        or fresh_derivation.get("method")
        != "canonical_accurate_recut_direct_from_claimed_segment"
        or fresh_derivation.get("cache_reused") is not False
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation method is invalid")
    derivation_source = Path(str(fresh_derivation.get("source_path") or "")).resolve(
        strict=True
    )
    derivation_start = fresh_derivation.get("absolute_source_start_ms")
    derivation_end = fresh_derivation.get("absolute_source_end_ms")
    expected_duration = fresh_derivation.get("expected_duration_ms")
    actual_duration = fresh_derivation.get("actual_duration_ms")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            derivation_start,
            derivation_end,
            expected_duration,
            actual_duration,
        )
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation timing is invalid")
    if (
        str(derivation_source) != verified_route.segment_path
        or fresh_derivation.get("source_sha256")
        != verified_route.segment_binding_sha256
        or sha256_file(derivation_source) != verified_route.segment_binding_sha256
        or not (
            verified_route.start_ms
            <= derivation_start
            < derivation_end
            <= verified_route.end_ms
        )
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation source binding mismatch")
    derived_duration = derivation_end - derivation_start
    if (
        expected_duration != derived_duration
        or actual_duration <= 0
        or str(media_path) != fresh_derivation.get("output_path")
        or source_media_sha256 != fresh_derivation.get("output_sha256")
    ):
        raise SpeakerFinalizationError("FAST_SOLO fresh derivation output binding mismatch")
    if sha256_file(routing_claim_path) != verified_route.claim_sha256:
        raise SpeakerFinalizationError("FAST_SOLO routing claim drifted before render")
    cues = parse_srt(text_srt_path)
    if not cues:
        raise SpeakerFinalizationError("FAST_SOLO text-final SRT has no cues")
    final_cues = [
        Cue(
            source_index=index,
            start=cue.start,
            end=cue.end,
            speaker=HOST_SPEAKER,
            text=cue.text,
            decision_source="verified_session_fast_solo",
        )
        for index, cue in enumerate(cues, start=1)
    ]
    output_srt_path.unlink(missing_ok=True)
    output_ass_path.unlink(missing_ok=True)
    write_srt(final_cues, output_srt_path)
    write_ass(final_cues, output_ass_path, show_speaker_labels=False)
    if (
        sha256_file(routing_claim_path) != verified_route.claim_sha256
        or sha256_file(media_path) != source_media_sha256
        or sha256_file(text_srt_path) != text_final_srt_sha256
        or sha256_file(derivation_source) != verified_route.segment_binding_sha256
    ):
        output_srt_path.unlink(missing_ok=True)
        output_ass_path.unlink(missing_ok=True)
        raise SpeakerFinalizationError("FAST_SOLO authority inputs drifted during render")
    manifest: dict[str, object] = {
        "schema_version": SPEAKER_FINALIZATION_SCHEMA,
        "status": "READY",
        "production_ready": True,
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(media_path),
        "source_media_sha256": source_media_sha256,
        "text_final_srt": str(text_srt_path),
        "text_final_srt_sha256": text_final_srt_sha256,
        "speaker_routing_claim": str(routing_claim_path),
        "speaker_routing_claim_sha256": verified_route.claim_sha256,
        "speaker_routing_request_sha256": verified_route.request_sha256,
        "speaker_routing_provider_evidence_sha256": (
            verified_route.provider_evidence_sha256
        ),
        "pipeline_fingerprint": verified_route.pipeline_fingerprint,
        "fresh_fast_derivation": dict(fresh_derivation),
        "output_review_srt": str(output_srt_path.resolve()),
        "output_review_srt_sha256": sha256_file(output_srt_path),
        "output_ass": str(output_ass_path.resolve()),
        "output_ass_sha256": sha256_file(output_ass_path),
        "visible_speaker_prefixes": False,
        "subtitle_style": SPEAKER_SUBTITLE_STYLE_ID,
        "speaker_taxonomy": "binary_visual_host_vs_guest",
        "host_identity_aliases": list(CHANNEL_PROFILE.speaker_identity_aliases),
        "host_anchor_scope": "verified_session_fast_solo",
        "source_cue_count": len(cues),
        "output_cue_count": len(final_cues),
        "reviewed_output_cue_count": 0,
        "accepted_context_output_cue_count": 0,
        "overlap_output_cue_count": 0,
        "analysis": {
            "mode": "speaker_session_fast_solo_v1",
            "campp_invoked": False,
            "context_invoked": False,
        },
        "final_decisions": [asdict(cue) for cue in final_cues],
    }
    atomic_write_text(
        output_manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--text-srt", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output-srt", type=Path, required=True)
    parser.add_argument("--output-ass", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--source-session-anchors", type=Path)
    parser.add_argument("--mixed-overlap-evidence", type=Path)
    parser.add_argument("--speaker-session-context", type=Path)
    parser.add_argument("--expected-source-recording-binding", type=json.loads)
    parser.add_argument("--no-context-judge", action="store_true")
    # 只有 producer 在 speaker_mode=auto 下才会传；required 永远不带这个开关。
    parser.add_argument("--best-effort-guess", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    context_call = None if args.no_context_judge else (
        lambda prompt: _call_context_via_cpa(prompt, repo_root=repo_root, work_dir=args.work_dir)
    )
    try:
        manifest = finalize_speaker_subtitles(
            media_path=args.media,
            text_srt_path=args.text_srt,
            profile_path=args.profile,
            reference_dir=args.reference_dir,
            model_dir=args.model_dir,
            output_srt_path=args.output_srt,
            output_ass_path=args.output_ass,
            output_manifest_path=args.output_manifest,
            work_dir=args.work_dir,
            candidate_id=args.candidate_id,
            override_path=args.overrides,
            source_session_anchor_path=args.source_session_anchors,
            mixed_overlap_evidence_path=args.mixed_overlap_evidence,
            speaker_session_context_path=args.speaker_session_context,
            context_call=context_call,
            best_effort_guess=args.best_effort_guess,
            expected_source_recording=args.expected_source_recording_binding,
        )
    except Exception as exc:
        blocked = {
            "schema_version": SPEAKER_FINALIZATION_SCHEMA,
            "status": "BLOCKED",
            "production_ready": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "text_final_srt": str(args.text_srt),
            "text_final_srt_sha256": sha256_file(args.text_srt) if args.text_srt.is_file() else None,
        }
        atomic_write_text(args.output_manifest, json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(blocked, ensure_ascii=False))
        return 3
    if manifest.get("status") == "SPEAKER_REVIEW_REQUIRED":
        print(json.dumps(manifest, ensure_ascii=False))
        return 4
    print(json.dumps({"status": manifest["status"], "manifest": str(args.output_manifest)}, ensure_ascii=False))
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
