"""Production Li-Dousha/guest speaker finalization for talk subtitles.

Contract:

1. input SRT is already text-final (ASR, terminology, pronouns, and any human
   corrections are complete);
2. CAM++ supplies acoustic evidence, whole-conversation context resolves short
   or boundary-band cues, and optional hash-bound human decisions are applied;
3. a clean text SRT remains the wording authority, while a review-labelled SRT,
   colour ASS, and evidence manifest are emitted before burn.

The ML imports are lazy so ordinary unit tests do not need the production venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import statistics
import stat
import subprocess
import tempfile
from dataclasses import asdict
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
from src.autoslice.host_vocal_proof import (
    _extract_checkpoint,
    _load_campplus_pipeline,
    _sha256_directory,
    _validate_profile,
)
from src.autoslice.llm_client import extract_json_object


class SpeakerFinalizationError(RuntimeError):
    pass


SOURCE_SESSION_ANCHOR_SCHEMA = "lidousha-speaker-source-session-anchors.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


DEFAULT_POLICY: dict[str, float | int] = {
    "host_session_seed_min": 0.68,
    "host_session_anchor_count": 4,
    "guest_seed_max": 0.42,
    "guest_min_duration_ms": 1_800,
    "guest_session_similarity_max": 0.45,
    "guest_cluster_similarity_min": 0.45,
    "ambiguity_band": 0.10,
    "short_cue_ms": 1_500,
    "single_host_median_seed_min": 0.55,
}


def _require_sha256(value: object, *, field: str) -> str:
    digest = str(value or "")
    if not SHA256_RE.fullmatch(digest):
        raise SpeakerFinalizationError(f"{field} must be a SHA-256 digest")
    return digest


def _snapshot_bound_input(source: Path, target: Path) -> tuple[Path, str]:
    """Freeze one small control-plane input as the exact bytes we hash/use."""

    resolved = source.resolve(strict=True)
    payload = resolved.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target.resolve(), hashlib.sha256(payload).hexdigest()


def _validate_source_session_anchor_document(
    document: object,
    *,
    target_media_sha256: str,
    target_media_path: Path | None = None,
    profile_sha256: str,
    model_tree_sha256: str,
    reference_hashes: Mapping[str, str],
    host_seed_min: float,
) -> dict[str, object]:
    """Validate the immutable contract before any donor media is decoded.

    A source-session anchor is not a relaxed threshold.  Every donor cue must
    already pass the same static enrollment gate used for clip-local anchors,
    and the manifest explicitly allowlists the exact target recut hash.  This
    lets a short clip reuse trusted speech from the same source recording while
    keeping cross-session or drifted anchors fail-closed.
    """

    if not isinstance(document, dict) or document.get("schema_version") != SOURCE_SESSION_ANCHOR_SCHEMA:
        raise SpeakerFinalizationError(
            f"source-session anchor schema must be {SOURCE_SESSION_ANCHOR_SCHEMA}"
        )
    if document.get("status") != "READY" or document.get("subject") != "李豆沙":
        raise SpeakerFinalizationError("source-session anchor manifest is not READY for 李豆沙")
    session_id = str(document.get("source_session_id") or "").strip()
    if not session_id:
        raise SpeakerFinalizationError("source-session anchor manifest is missing source_session_id")
    if _require_sha256(document.get("profile_sha256"), field="source-session profile_sha256") != profile_sha256:
        raise SpeakerFinalizationError("source-session anchor profile hash drift")
    if _require_sha256(document.get("model_tree_sha256"), field="source-session model_tree_sha256") != model_tree_sha256:
        raise SpeakerFinalizationError("source-session anchor model hash drift")
    declared_references = document.get("reference_hashes")
    if not isinstance(declared_references, Mapping) or dict(declared_references) != dict(reference_hashes):
        raise SpeakerFinalizationError("source-session anchor reference hashes drift")
    source_recording = str(document.get("source_recording") or "").strip()
    if not source_recording or not Path(source_recording).is_absolute():
        raise SpeakerFinalizationError(
            "source-session anchor source_recording must be an absolute path"
        )
    allowed_targets = document.get("allowed_targets")
    if not isinstance(allowed_targets, list) or not allowed_targets:
        raise SpeakerFinalizationError("source-session anchor target allowlist is missing")
    normalized_target_hashes: list[str] = []
    normalized_target_ids: list[str] = []
    matching_target: Mapping[str, object] | None = None
    for position, target in enumerate(allowed_targets, start=1):
        if not isinstance(target, Mapping):
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} must be an object"
            )
        candidate_id = str(target.get("candidate_id") or "").strip()
        media_path = str(target.get("media_path") or "").strip()
        provenance_path = str(target.get("provenance_path") or "").strip()
        if not candidate_id or not media_path or not provenance_path:
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} is missing provenance fields"
            )
        if not Path(media_path).is_absolute() or not Path(provenance_path).is_absolute():
            raise SpeakerFinalizationError(
                f"source-session allowed target {position} paths must be absolute"
            )
        media_digest = _require_sha256(
            target.get("media_sha256"),
            field=f"source-session allowed target {position} media_sha256",
        )
        _require_sha256(
            target.get("provenance_sha256"),
            field=f"source-session allowed target {position} provenance_sha256",
        )
        normalized_target_hashes.append(media_digest)
        normalized_target_ids.append(candidate_id)
        if media_digest == target_media_sha256:
            matching_target = target
    if len(set(normalized_target_hashes)) != len(normalized_target_hashes):
        raise SpeakerFinalizationError("source-session anchor target allowlist contains duplicates")
    if len(set(normalized_target_ids)) != len(normalized_target_ids):
        raise SpeakerFinalizationError("source-session anchor target candidate IDs contain duplicates")
    if matching_target is None:
        raise SpeakerFinalizationError("target media is not allowlisted for source-session anchors")
    # The runtime may consume a byte-identical temporary copy (the remote
    # wrapper deliberately scps to /tmp).  Identity is therefore the frozen
    # media SHA; the canonical path remains provenance, not execution state.
    if target_media_path is not None:
        target_media_path.resolve(strict=True)

    anchors = document.get("anchors")
    if not isinstance(anchors, list) or len(anchors) < 2:
        raise SpeakerFinalizationError("source-session anchor manifest requires at least two anchors")
    reference_ids = set(reference_hashes)
    seen_sources: set[tuple[object, ...]] = set()
    needs_cue_donor = False
    for position, anchor in enumerate(anchors, start=1):
        if not isinstance(anchor, Mapping):
            raise SpeakerFinalizationError(f"source-session anchor {position} must be an object")
        anchor_type = str(anchor.get("anchor_type") or "donor_cue")
        if anchor_type == "donor_cue":
            needs_cue_donor = True
            cue_index = anchor.get("source_cue")
            if not isinstance(cue_index, int) or isinstance(cue_index, bool) or cue_index < 1:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid source_cue"
                )
            source_key = (anchor_type, cue_index)
            if not str(anchor.get("start") or "") or not str(anchor.get("end") or ""):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} is missing timing"
                )
        elif anchor_type == "source_recording_segment":
            start_ms = anchor.get("source_start_ms")
            end_ms = anchor.get("source_end_ms")
            if (
                not isinstance(start_ms, int)
                or isinstance(start_ms, bool)
                or not isinstance(end_ms, int)
                or isinstance(end_ms, bool)
                or start_ms < 0
                or end_ms - start_ms < 1_000
                or end_ms - start_ms > 30_000
            ):
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid source recording segment"
                )
            source_key = (anchor_type, start_ms, end_ms)
        else:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} has invalid anchor_type"
            )
        if source_key in seen_sources:
            raise SpeakerFinalizationError("source-session anchor sources must be unique")
        seen_sources.add(source_key)
        if not isinstance(anchor.get("text"), str):
            raise SpeakerFinalizationError(f"source-session anchor {position} is missing text")
        _require_sha256(anchor.get("sample_sha256"), field=f"source-session anchor {position} sample")
        scores = anchor.get("reference_scores")
        if not isinstance(scores, Mapping) or set(scores) != reference_ids:
            raise SpeakerFinalizationError(f"source-session anchor {position} reference scores drift")
        normalized_scores: list[float] = []
        for reference_id in sorted(reference_ids):
            score = scores.get(reference_id)
            if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= float(score) <= 1.0:
                raise SpeakerFinalizationError(
                    f"source-session anchor {position} has invalid score for {reference_id}"
                )
            normalized_scores.append(float(score))
        declared_median = anchor.get("enroll_median_score")
        if not isinstance(declared_median, (int, float)) or isinstance(declared_median, bool):
            raise SpeakerFinalizationError(f"source-session anchor {position} median is missing")
        actual_declared_median = float(statistics.median(normalized_scores))
        if abs(float(declared_median) - actual_declared_median) > 1e-6:
            raise SpeakerFinalizationError(f"source-session anchor {position} median drift")
        if actual_declared_median < host_seed_min:
            raise SpeakerFinalizationError(
                f"source-session anchor {position} does not pass the unchanged host seed gate"
            )
    donor = document.get("donor")
    if needs_cue_donor and not isinstance(donor, Mapping):
        raise SpeakerFinalizationError("source-session cue anchors require a donor")
    if donor is not None:
        if not isinstance(donor, Mapping):
            raise SpeakerFinalizationError("source-session anchor donor must be an object")
        if not str(donor.get("candidate_id") or "").strip():
            raise SpeakerFinalizationError("source-session anchor donor candidate_id is missing")
        for key in ("media_path", "text_srt_path", "provenance_path"):
            if not str(donor.get(key) or "").strip():
                raise SpeakerFinalizationError(f"source-session anchor donor {key} is missing")
            if not Path(str(donor[key])).is_absolute():
                raise SpeakerFinalizationError(
                    f"source-session anchor donor {key} must be absolute"
                )
        _require_sha256(donor.get("media_sha256"), field="source-session donor media_sha256")
        _require_sha256(
            donor.get("text_srt_sha256"), field="source-session donor text_srt_sha256"
        )
        _require_sha256(
            donor.get("provenance_sha256"), field="source-session donor provenance_sha256"
        )
    return dict(document)


def _validate_source_session_provenance(
    document: Mapping[str, object],
    *,
    target_media_path: Path,
    target_media_sha256: str,
) -> dict[str, object]:
    """Verify hash-bound donor/target specs name the same source recording."""

    source_recording = str(document["source_recording"])
    donor = document.get("donor")
    targets = document["allowed_targets"]
    assert isinstance(targets, list)
    target = next(
        (
            item
            for item in targets
            if isinstance(item, Mapping) and item.get("media_sha256") == target_media_sha256
        ),
        None,
    )
    if not isinstance(target, Mapping):
        raise SpeakerFinalizationError("source-session target provenance is missing")

    def load_spec(entry: Mapping[str, object], *, role: str) -> tuple[Path, dict[str, object]]:
        spec_path = Path(str(entry["provenance_path"])).resolve(strict=True)
        if sha256_file(spec_path) != entry["provenance_sha256"]:
            raise SpeakerFinalizationError(f"source-session {role} provenance hash drift")
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SpeakerFinalizationError(
                f"cannot read source-session {role} provenance: {exc}"
            ) from exc
        if not isinstance(spec, dict) or spec.get("candidate_id") != entry["candidate_id"]:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance candidate drift"
            )
        pieces = spec.get("pieces")
        if not isinstance(pieces, list) or not pieces:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance has no source pieces"
            )
        remote_media = {
            str(piece.get("remote_media") or "")
            for piece in pieces
            if isinstance(piece, Mapping)
        }
        if remote_media != {source_recording}:
            raise SpeakerFinalizationError(
                f"source-session {role} provenance names a different source recording"
            )
        return spec_path, spec

    donor_spec_path: Path | None = None
    if isinstance(donor, Mapping):
        donor_spec_path, _donor_spec = load_spec(donor, role="donor")
    target_spec_path, _target_spec = load_spec(target, role="target")
    canonical_target_media = Path(str(target["media_path"])).resolve(strict=True)
    if sha256_file(canonical_target_media) != target_media_sha256:
        raise SpeakerFinalizationError("source-session canonical target media hash drift")
    if sha256_file(target_media_path) != target_media_sha256:
        raise SpeakerFinalizationError("source-session target media hash drift")
    return {
        "source_recording": source_recording,
        "donor_provenance": str(donor_spec_path) if donor_spec_path is not None else None,
        "donor_provenance_sha256": (
            donor["provenance_sha256"] if isinstance(donor, Mapping) else None
        ),
        "target_candidate_id": target["candidate_id"],
        "canonical_target_media": str(canonical_target_media),
        "canonical_target_media_sha256": target_media_sha256,
        "target_provenance": str(target_spec_path),
        "target_provenance_sha256": target["provenance_sha256"],
    }


def _ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000 + int(millis)


def _policy(profile: Mapping[str, object]) -> dict[str, float | int]:
    result = dict(DEFAULT_POLICY)
    configured = profile.get("talk_speaker_policy")
    if isinstance(configured, Mapping):
        for key in result:
            value = configured.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
    return result


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    dot = left_norm_sq = right_norm_sq = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm_sq += a * a
        right_norm_sq += b * b
    if left_norm_sq <= 0.0 or right_norm_sq <= 0.0:
        return 0.0
    return dot / math.sqrt(left_norm_sq * right_norm_sq)


def _campp_embedding(verifier: Callable[..., object], path: Path) -> list[float]:
    """Return the CAM++ speaker embedding for one wav.

    ModelScope's speaker-verification pipeline embeds every input inside
    ``forward`` and only derives a pairwise score when exactly two inputs are
    passed (``postprocess`` returns no score otherwise), so a single-input
    ``output_emb`` call yields that clip's embedding with one inference.
    """
    result = verifier([str(path)], output_emb=True)
    embeddings = result["embs"] if isinstance(result, Mapping) and "embs" in result else result
    row = embeddings[0]
    values = row.tolist() if hasattr(row, "tolist") else list(row)
    return [float(value) for value in values]


def _build_embedding_similarity(
    *, verifier: Callable[..., object], model_hash: str, work_dir: Path
) -> Callable[[Path, Path], float]:
    """Embed each cue once, then score pairs by cosine.

    The previous implementation asked the pipeline for every (cue, anchor) pair
    and re-embedded both wavs each time, so a talk clip paid O(cues x anchors)
    CAM++ inferences and long clips blew past the finalizer timeout.  A CAM++
    pair score is the cosine of the two per-clip embeddings, so embedding each
    wav a single time and caching the vector is exactly score-preserving while
    collapsing the cost to O(cues) inferences.
    """
    cache_path = work_dir / "embedding-cache.json"
    try:
        raw_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    except (OSError, ValueError):
        raw_cache = {}
    embeddings: dict[str, list[float]] = {
        key: [float(value) for value in vector]
        for key, vector in raw_cache.items()
        if isinstance(vector, list)
    }
    fingerprints: dict[Path, str] = {}

    def fingerprint(path: Path) -> str:
        resolved = path.resolve()
        if resolved not in fingerprints:
            fingerprints[resolved] = sha256_file(resolved)
        return fingerprints[resolved]

    def embedding(path: Path) -> list[float]:
        # Cue basenames are reused after text/timing corrections. Content-bound
        # keys keep a persistent work dir from serving stale voice vectors.
        key = model_hash + "|" + fingerprint(path)
        vector = embeddings.get(key)
        if vector is None:
            vector = _campp_embedding(verifier, path)
            embeddings[key] = vector
            atomic_write_text(cache_path, json.dumps(embeddings, sort_keys=True))
        return vector

    def similarity(left: Path, right: Path) -> float:
        return round(_cosine_similarity(embedding(left), embedding(right)), 5)

    return similarity


def _two_means(values: Sequence[float]) -> tuple[float, float, float]:
    if len(values) < 2:
        raise SpeakerFinalizationError("not enough cue margins for two-speaker clustering")
    low, high = min(values), max(values)
    if low == high:
        raise SpeakerFinalizationError("speaker margin distribution has no separation")
    for _ in range(40):
        high_side = [value for value in values if abs(value - high) < abs(value - low)]
        low_side = [value for value in values if abs(value - high) >= abs(value - low)]
        if not high_side or not low_side:
            break
        next_low = statistics.mean(low_side)
        next_high = statistics.mean(high_side)
        if abs(next_low - low) < 1e-8 and abs(next_high - high) < 1e-8:
            low, high = next_low, next_high
            break
        low, high = next_low, next_high
    if low > high:
        low, high = high, low
    return float(low), float(high), float((low + high) / 2)


def resolve_ambiguous_labels(
    labels: Sequence[str | None],
    margins: Sequence[float],
    threshold: float,
    context_votes: Mapping[int, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Resolve ambiguous cue labels and record the evidence source.

    ``context_votes`` uses zero-based cue indices.  Missing votes fall back to
    matching neighbours when possible, then to the acoustic side of the
    threshold.  This is the best-effort policy for short interjections; it
    never silently claims that fallback evidence was a confident voiceprint.
    """

    if len(labels) != len(margins):
        raise ValueError("labels and margins must have equal length")
    result = list(labels)
    sources = ["campp_audio" if label is not None else "unresolved" for label in labels]
    votes = context_votes or {}
    for index, vote in votes.items():
        if 0 <= index < len(result) and result[index] is None and vote in {"李豆沙", "连线"}:
            result[index] = vote
            sources[index] = "whole_clip_context"
    for index, label in enumerate(result):
        if label is not None:
            continue
        previous = next((result[j] for j in range(index - 1, -1, -1) if result[j]), None)
        following = next((result[j] for j in range(index + 1, len(result)) if result[j]), None)
        if previous is not None and previous == following:
            result[index] = previous
            sources[index] = "neighbour_context_fallback"
        else:
            result[index] = "李豆沙" if margins[index] >= threshold else "连线"
            sources[index] = "acoustic_threshold_fallback"
    return [str(label) for label in result], sources


def _reviewed_context_votes(
    override_document: Mapping[str, object],
    *,
    cue_count: int,
) -> dict[int, str]:
    """Load hash-bound, human-accepted context votes from an override asset.

    The JSON uses one-based cue numbers; the analyzer uses zero-based indices.
    These votes stabilize only an already reviewed clip.  New clips continue to
    use the normal whole-clip context judge.
    """

    raw = override_document.get("reviewed_context_votes")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpeakerFinalizationError("reviewed_context_votes must be an object")
    labels = raw.get("labels")
    if not isinstance(labels, Mapping) or not labels:
        raise SpeakerFinalizationError("reviewed_context_votes.labels must be a non-empty object")
    if not str(raw.get("authority") or "").strip():
        raise SpeakerFinalizationError("reviewed_context_votes.authority must be non-empty")
    expected_source = str(override_document.get("source_srt_sha256") or "")
    bound_source = str(raw.get("source_automatic_srt_sha256") or "")
    if not expected_source or not bound_source:
        raise SpeakerFinalizationError(
            "reviewed context votes require source_srt_sha256 and source_automatic_srt_sha256"
        )
    if expected_source != bound_source:
        raise SpeakerFinalizationError("reviewed context vote source hash does not match source_srt_sha256")

    votes: dict[int, str] = {}
    for cue_number_raw, speaker_raw in labels.items():
        try:
            cue_number = int(str(cue_number_raw))
        except ValueError as exc:
            raise SpeakerFinalizationError(
                f"reviewed context cue number is invalid: {cue_number_raw!r}"
            ) from exc
        speaker = str(speaker_raw)
        if not 1 <= cue_number <= cue_count:
            raise SpeakerFinalizationError(f"reviewed context cue is out of range: {cue_number}")
        if speaker not in {"李豆沙", "连线"}:
            raise SpeakerFinalizationError(
                f"reviewed context speaker is invalid for cue {cue_number}: {speaker!r}"
            )
        votes[cue_number - 1] = speaker
    return votes


def _context_prompt(cues: Sequence[TextCue], labels: Sequence[str | None], ambiguous: Sequence[int]) -> str:
    rows = [
        f"{index}. [{label or '待定'}] {cue.text}"
        for index, (cue, label) in enumerate(zip(cues, labels, strict=True), start=1)
    ]
    return (
        "这是李豆沙（直播间主人）与连线主播的完整切片字幕，文本、专名和代词已经最终定稿。"
        "大部分行已经由声纹标为[李豆沙]/[连线]；只有[待定]行因太短或处于声纹分界带，需要根据整段问答、称呼方向和上下文判断。\n"
        "规则：别人评价李豆沙后，她的反问/自辩通常是李豆沙；对李豆沙使用第三人称评价的通常是连线；"
        "对话中作为名字出现的精确词 shadow 是李豆沙的自称之一，不是第四位说话人或连线嘉宾；"
        "不要修改文字，不要把相邻两个人的连续短句合成同一说话人。\n"
        f"待定行号（1-based）：{[index + 1 for index in ambiguous]}\n\n"
        + "\n".join(rows)
        + '\n\n只输出 JSON：{"labels":[{"n":1,"speaker":"李豆沙"}]}，且只列待定行。'
    )


def _speaker_context_env() -> dict[str, str]:
    """Load the fixed runtime CPA env without echoing or shell-evaluating it."""

    env = dict(os.environ)
    if env.get("CPA_BASE_URL") and env.get("CPA_API_KEY"):
        return env
    env_path = Path(os.environ.get("AUTOSLICE_CPA_ENV", "/opt/bilive/autoslice/cpa.env"))
    if not env_path.is_file():
        return env
    mode = stat.S_IMODE(env_path.stat().st_mode)
    if mode & 0o077:
        raise SpeakerFinalizationError(f"CPA env permissions are too broad: {oct(mode)}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"CPA_[A-Z0-9_]+", key):
            continue
        values = shlex.split(raw_value, comments=True, posix=True)
        if len(values) != 1:
            raise SpeakerFinalizationError(f"invalid {key} entry in CPA env")
        env.setdefault(key, values[0])
    return env


def _call_context_via_cpa(prompt: str, *, repo_root: Path, work_dir: Path) -> str:
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", dir=work_dir, delete=False) as handle:
        prompt_path = Path(handle.name)
        handle.write(prompt)
    output_path = prompt_path.with_suffix(".out")
    try:
        completed = subprocess.run(
            [
                "bash",
                str(repo_root / "scripts" / "llm_via_cpa.sh"),
                str(prompt_path),
                str(output_path),
                "gpt-5.6-sol gpt-5.5 gpt-5.4",
                "medium",
            ],
            cwd=str(repo_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=_speaker_context_env(),
        )
        if completed.returncode != 0 or not output_path.is_file():
            raise SpeakerFinalizationError(
                "speaker context judge failed: " + (completed.stderr or completed.stdout)[-800:]
            )
        return output_path.read_text(encoding="utf-8")
    finally:
        prompt_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


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
) -> dict[str, object]:
    work_dir.mkdir(parents=True, exist_ok=True)
    profile, references, model_hash, verifier = _load_runtime(profile_path, reference_dir, model_dir)
    policy = _policy(profile)
    _audio, _sample_rate, cue_paths = _extract_cue_wavs(media_path, cues, work_dir)
    similarity = _build_embedding_similarity(
        verifier=verifier, model_hash=model_hash, work_dir=work_dir
    )

    seed_scores = [
        float(statistics.median(similarity(Path(reference["path"]), cue_path) for reference in references))
        for cue_path in cue_paths
    ]
    anchor_count = int(policy["host_session_anchor_count"])
    clip_host_indices = [
        index for index in sorted(range(len(cues)), key=seed_scores.__getitem__, reverse=True)
        if seed_scores[index] >= float(policy["host_session_seed_min"])
    ][:anchor_count]
    source_session_evidence: dict[str, object] | None = None
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
    else:
        host_indices = clip_host_indices
        if len(host_indices) < 2:
            raise SpeakerFinalizationError(f"not enough Li Dousha clip anchors: {host_indices}")
        host_prints = [cue_paths[index] for index in host_indices]
        host_anchor_scope = "clip"

    def host_bank_similarity(index: int) -> float:
        # A trusted host bank contains complementary speaking styles.  A cue
        # that strongly matches any unchanged-high-gate host anchor is host-
        # explained; averaging would dilute the one matching style and create
        # false guest evidence (notably excited/farewell delivery).
        values = [similarity(host, cue_paths[index]) for host in host_prints]
        if host_anchor_scope == "source_session":
            return float(max(values))
        # Preserve the established clip-local classifier exactly.  The
        # any-anchor veto is authorized only by an explicit, hash-bound source
        # session bank; it must not silently relax every historical clip.
        return float(statistics.mean(values))

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
        if median_seed < float(policy["single_host_median_seed_min"]) or unexplained_long_low:
            raise SpeakerFinalizationError(
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
                    "speaker": "李豆沙",
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
    short_ms = int(policy["short_cue_ms"])
    labels: list[str | None] = []
    ambiguous: list[int] = []
    for index, (cue, margin) in enumerate(zip(cues, margins, strict=True)):
        short = _ms(cue.end) - _ms(cue.start) < short_ms
        if short or abs(margin - threshold) < band:
            labels.append(None)
            ambiguous.append(index)
        else:
            labels.append("李豆沙" if margin >= threshold else "连线")

    reviewed_votes = {
        index: speaker
        for index, speaker in (reviewed_context_votes or {}).items()
        if index in ambiguous and speaker in {"李豆沙", "连线"}
    }
    votes: dict[int, str] = dict(reviewed_votes)
    context_errors: list[str] = []
    context_attempts = 0
    if any(index not in votes for index in ambiguous) and context_call is not None:
        # Retry incomplete answers as well as transport failures. Production
        # readiness still requires every ambiguous cue to have either a context
        # vote or a hash-bound human override.
        for _attempt in range(3):
            context_attempts += 1
            try:
                pending = [index for index in ambiguous if index not in votes]
                payload = extract_json_object(context_call(_context_prompt(cues, labels, pending)))
                rows = payload.get("labels", [])
                if not isinstance(rows, list):
                    raise ValueError("labels must be a list")
                for row in rows:
                    cue_index = int(row["n"]) - 1
                    speaker = str(row["speaker"])
                    # A context reply may include rows that were not requested.
                    # Never let it overwrite a frozen reviewed vote.
                    if cue_index in pending and speaker in {"李豆沙", "连线"}:
                        votes[cue_index] = speaker
                if all(index in votes for index in ambiguous):
                    break
            except Exception as exc:
                context_errors.append(f"{type(exc).__name__}: {exc}")
    unresolved_context = [index for index in ambiguous if index not in votes]
    resolved, sources = resolve_ambiguous_labels(labels, margins, threshold, votes)
    for index in reviewed_votes:
        if index in ambiguous:
            sources[index] = "accepted_context_baseline"

    # Smooth only acoustically ambiguous one-cue islands; never override a
    # whole-clip context judgement or confident audio label.
    for index in range(1, len(resolved) - 1):
        if (
            resolved[index - 1] == resolved[index + 1] != resolved[index]
            and sources[index] in {"neighbour_context_fallback", "acoustic_threshold_fallback"}
            and abs(margins[index] - threshold) < band
        ):
            resolved[index] = resolved[index - 1]
            sources[index] = "ambiguous_island_smoothing"

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
        "policy": policy,
        "model_tree_sha256": model_hash,
        "reference_hashes": {str(reference["id"]): str(reference["sha256"]) for reference in references},
        "host_anchor_cues": [index + 1 for index in host_indices],
        "clip_host_anchor_candidates": [index + 1 for index in clip_host_indices],
        "guest_anchor_groups": [[index + 1 for index in group] for group in guest_groups],
        "cluster_centers": {"guest": low_center, "lidousha": high_center},
        "threshold": threshold,
        "context_attempts": context_attempts,
        "context_errors": context_errors,
        "context_votes": {str(index + 1): speaker for index, speaker in votes.items()},
        "reviewed_context_votes": {
            str(index + 1): speaker for index, speaker in reviewed_votes.items()
        },
        "context_required_cues": [index + 1 for index in ambiguous],
        "context_unresolved_cues": [index + 1 for index in unresolved_context],
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
    override_path: Path | None = None,
    source_session_anchor_path: Path | None = None,
    analyzer: Callable[..., dict[str, object]] = _run_campplus_analysis,
    context_call: Callable[[str], str] | None = None,
) -> dict[str, object]:
    media_path = media_path.resolve(strict=True)
    text_srt_path = text_srt_path.resolve(strict=True)
    profile_path = profile_path.resolve(strict=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    profile_snapshot, profile_sha256 = _snapshot_bound_input(
        profile_path, work_dir / "bound-inputs" / "voiceprint-profile.json"
    )
    source_session_anchor_original: Path | None = None
    source_session_anchor_snapshot: Path | None = None
    source_session_anchor_sha256: str | None = None
    if source_session_anchor_path is not None:
        source_session_anchor_original = source_session_anchor_path.resolve(strict=True)
        source_session_anchor_snapshot, source_session_anchor_sha256 = _snapshot_bound_input(
            source_session_anchor_original,
            work_dir / "bound-inputs" / "source-session-anchors.json",
        )
    cues = parse_srt(text_srt_path)
    override_document: dict[str, object] | None = None
    reviewed_votes: dict[int, str] = {}
    expected_automatic = ""
    if override_path is not None:
        loaded = json.loads(override_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise SpeakerFinalizationError("speaker override document must be an object")
        override_document = loaded
        expected_text = str(override_document.get("text_final_srt_sha256") or "")
        expected_automatic = str(override_document.get("source_srt_sha256") or "")
        expected_media = str(override_document.get("source_media_sha256") or "")
        actual_text = sha256_file(text_srt_path)
        actual_media = sha256_file(media_path)
        if not expected_media:
            raise SpeakerFinalizationError("speaker override is missing source_media_sha256")
        if expected_media != actual_media:
            raise SpeakerFinalizationError(
                f"speaker override media hash mismatch: expected {expected_media!r}, got {actual_media!r}"
            )
        if expected_text and expected_text != actual_text:
            raise SpeakerFinalizationError(
                f"speaker override text-final hash mismatch: expected {expected_text!r}, got {actual_text!r}"
            )
        reviewed_votes = _reviewed_context_votes(override_document, cue_count=len(cues))
    analysis = analyzer(
        media_path=media_path,
        cues=cues,
        profile_path=profile_snapshot,
        reference_dir=reference_dir,
        model_dir=model_dir,
        work_dir=work_dir,
        context_call=context_call,
        reviewed_context_votes=reviewed_votes,
        source_session_anchor_path=source_session_anchor_snapshot,
    )
    if sha256_file(profile_path) != profile_sha256:
        raise SpeakerFinalizationError("voiceprint profile drifted during speaker analysis")
    if (
        source_session_anchor_original is not None
        and sha256_file(source_session_anchor_original) != source_session_anchor_sha256
    ):
        raise SpeakerFinalizationError(
            "source-session anchor manifest drifted during speaker analysis"
        )
    decisions = analysis.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != len(cues):
        raise SpeakerFinalizationError("speaker analyzer returned incomplete decisions")
    automatic: list[Cue] = []
    for index, (text_cue, decision) in enumerate(zip(cues, decisions, strict=True), start=1):
        if not isinstance(decision, Mapping) or decision.get("speaker") not in {"李豆沙", "连线"}:
            raise SpeakerFinalizationError(f"speaker decision {index} is invalid")
        automatic.append(
            Cue(
                source_index=index,
                start=text_cue.start,
                end=text_cue.end,
                speaker=str(decision["speaker"]),
                text=text_cue.text,
                decision_source=str(decision.get("decision_source") or "campp_audio"),
                note=(f"margin={decision.get('margin')}" if decision.get("margin") is not None else None),
            )
        )
    automatic_srt = work_dir / "automatic-labelled.srt"
    write_srt(automatic, automatic_srt)
    final_cues = automatic
    if override_document is not None:
        actual_automatic = sha256_file(automatic_srt)
        if expected_automatic and expected_automatic != actual_automatic:
            raise SpeakerFinalizationError(
                f"speaker override source hash mismatch: expected {expected_automatic!r}, got {actual_automatic!r}"
            )
        final_cues = apply_overrides(automatic, override_document)
    unresolved_raw = analysis.get("context_unresolved_cues") or []
    try:
        unresolved = {int(value) for value in unresolved_raw}
    except (TypeError, ValueError) as exc:
        raise SpeakerFinalizationError(
            "speaker analyzer returned invalid unresolved-context evidence"
        ) from exc
    override_sources = {
        int(item.get("source_cue", 0))
        for item in ((override_document or {}).get("overrides") or [])
        if isinstance(item, Mapping)
    }
    remaining_unresolved = sorted(unresolved - override_sources)
    if remaining_unresolved:
        raise SpeakerFinalizationError(
            "whole-clip context did not resolve ambiguous speaker cues: "
            + ",".join(str(value) for value in remaining_unresolved)
        )
    write_srt(final_cues, output_srt_path)
    write_ass(final_cues, output_ass_path, show_speaker_labels=False)
    manifest: dict[str, object] = {
        "schema_version": "lidousha-speaker-finalization.v1",
        "status": "READY",
        "production_ready": True,
        "stage_order": "text_final_then_speaker_then_ass_then_burn",
        "source_media": str(media_path),
        "source_media_sha256": sha256_file(media_path),
        "text_final_srt": str(text_srt_path),
        "text_final_srt_sha256": sha256_file(text_srt_path),
        "profile": str(profile_path.resolve()),
        "profile_sha256": profile_sha256,
        "automatic_labelled_srt_sha256": sha256_file(automatic_srt),
        "speaker_override": str(override_path.resolve()) if override_path is not None else None,
        "speaker_override_sha256": sha256_file(override_path) if override_path is not None else None,
        "source_session_anchor_manifest": (
            str(source_session_anchor_original)
            if source_session_anchor_original is not None
            else None
        ),
        "source_session_anchor_manifest_sha256": source_session_anchor_sha256,
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
        "host_identity_aliases": ["李豆沙", "shadow"],
        "source_cue_count": len(cues),
        "output_cue_count": len(final_cues),
        "reviewed_output_cue_count": sum(cue.decision_source.startswith("reviewed_") for cue in final_cues),
        "accepted_context_output_cue_count": sum(
            cue.decision_source == "accepted_context_baseline" for cue in final_cues
        ),
        "overlap_output_cue_count": sum(cue.placement == "above" for cue in final_cues),
        "analysis": analysis,
        "final_decisions": [asdict(cue) for cue in final_cues],
    }
    atomic_write_text(output_manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
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
    parser.add_argument("--overrides", type=Path)
    parser.add_argument("--source-session-anchors", type=Path)
    parser.add_argument("--no-context-judge", action="store_true")
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
            override_path=args.overrides,
            source_session_anchor_path=args.source_session_anchors,
            context_call=context_call,
        )
    except Exception as exc:
        blocked = {
            "schema_version": "lidousha-speaker-finalization.v1",
            "status": "BLOCKED",
            "production_ready": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "text_final_srt": str(args.text_srt),
            "text_final_srt_sha256": sha256_file(args.text_srt) if args.text_srt.is_file() else None,
        }
        atomic_write_text(args.output_manifest, json.dumps(blocked, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(blocked, ensure_ascii=False))
        return 3
    print(json.dumps({"status": manifest["status"], "manifest": str(args.output_manifest)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
