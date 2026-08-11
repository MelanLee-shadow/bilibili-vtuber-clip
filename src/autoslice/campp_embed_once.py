"""Content-addressed CAM++ embed-once scoring, shared by the talk and song lanes.

ModelScope's speaker-verification pipeline re-embeds *every* input on *every*
call (``forward`` loops over the input list with no memo), and its pairwise
score is nothing but the cosine of the two per-clip embeddings, rounded to five
decimals in ``postprocess``.  Asking it for N pairs therefore pays 2N forward
passes to learn what N+M embeddings already determine.

Embedding each wav exactly once and scoring pairs through the pipeline's own
``compute_cos_similarity`` is *bit-identical* to the pairwise path - same
float32 vectors, same torch cosine, same ``round(score, 5)`` - so this is a
pure cost change with no effect on any threshold or decision.

The cache is bound to (audio bytes, model tree hash, runtime fingerprint,
embedding dimension) and re-derives its own ``binding_sha256`` on read: a
corrupted, foreign, or stale entry is ignored and the embedding is recomputed.
The cache therefore only ever skips a *repeated forward pass*; it never skips a
validation, a hash check, or an extraction.

This module deliberately imports nothing beyond the standard library and
``speaker_common`` so that both the talk finalizer and the fail-closed
host-vocal prover can depend on it without an import cycle and without dragging
an ML runtime into ordinary verification processes.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice.speaker_common import (
    CAMPP_COSINE_EPSILON,
    CAMPP_EMBEDDING_CACHE_SCHEMA,
    CAMPP_EMBEDDING_DIMENSION,
    CAMPP_MIN_EMBEDDING_NORM,
    CAMPP_SCORE_ROUNDING_TOLERANCE,
    SpeakerFinalizationError,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace one cache entry atomically without exposing a truncated file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Dependency-free equivalent of Torch cosine for test/runtime fallbacks.

    ``torch.nn.CosineSimilarity`` clamps each vector norm independently, not
    their product. Production still delegates to the loaded ModelScope
    pipeline's own scorer so float32 rounding stays identical to the original
    pair path.
    """

    if not left or not right:
        raise SpeakerFinalizationError("CAM++ embedding must not be empty")
    if len(left) != len(right):
        raise SpeakerFinalizationError("CAM++ embedding dimensions do not match")
    dot = left_norm_sq = right_norm_sq = 0.0
    for raw_a, raw_b in zip(left, right, strict=True):
        a, b = float(raw_a), float(raw_b)
        if not math.isfinite(a) or not math.isfinite(b):
            raise SpeakerFinalizationError("CAM++ embedding values must be finite")
        dot += a * b
        left_norm_sq += a * a
        right_norm_sq += b * b
    if not all(math.isfinite(value) for value in (dot, left_norm_sq, right_norm_sq)):
        raise SpeakerFinalizationError("CAM++ embedding arithmetic must be finite")
    denominator = max(math.sqrt(left_norm_sq), CAMPP_COSINE_EPSILON) * max(
        math.sqrt(right_norm_sq), CAMPP_COSINE_EPSILON
    )
    score = dot / denominator
    if not math.isfinite(score):
        raise SpeakerFinalizationError("CAM++ similarity must be finite")
    if not -1.0 - CAMPP_SCORE_ROUNDING_TOLERANCE <= score <= 1.0 + CAMPP_SCORE_ROUNDING_TOLERANCE:
        raise SpeakerFinalizationError("CAM++ similarity must be finite and within [-1, 1]")
    return min(1.0, max(-1.0, score))


def _validate_campp_embedding(values: Sequence[float]) -> list[float]:
    vector = [float(value) for value in values]
    if len(vector) != CAMPP_EMBEDDING_DIMENSION:
        raise SpeakerFinalizationError(
            f"CAM++ embedding must have {CAMPP_EMBEDDING_DIMENSION} values"
        )
    if any(not math.isfinite(value) for value in vector):
        raise SpeakerFinalizationError("CAM++ embedding values must be finite")
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm < CAMPP_MIN_EMBEDDING_NORM:
        raise SpeakerFinalizationError("CAM++ embedding norm is degenerate")
    return vector


def _campp_runtime_fingerprint(verifier: Callable[..., object]) -> str:
    components: dict[str, str] = {
        "pipeline_class": (
            f"{verifier.__class__.__module__}.{verifier.__class__.__qualname__}"
        )
    }
    for package in ("modelscope", "torch", "numpy"):
        try:
            components[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            components[package] = "unavailable"
    payload = json.dumps(components, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _embedding_binding_sha256(
    *,
    vector: Sequence[float],
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "schema_version": CAMPP_EMBEDDING_CACHE_SCHEMA,
            "model_sha256": model_hash,
            "runtime_fingerprint": runtime_fingerprint,
            "audio_sha256": audio_sha256,
            "dimension": CAMPP_EMBEDDING_DIMENSION,
            "embedding": list(vector),
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_cached_embedding(
    path: Path,
    *,
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> list[float] | None:
    """Return a fully bound cache entry, or require a trusted re-embedding."""

    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            return None
        if document.get("schema_version") != CAMPP_EMBEDDING_CACHE_SCHEMA:
            return None
        if document.get("model_sha256") != model_hash:
            return None
        if document.get("runtime_fingerprint") != runtime_fingerprint:
            return None
        if document.get("audio_sha256") != audio_sha256:
            return None
        if document.get("dimension") != CAMPP_EMBEDDING_DIMENSION:
            return None
        raw_vector = document.get("embedding")
        if not isinstance(raw_vector, list):
            return None
        vector = _validate_campp_embedding(raw_vector)
        if document.get("binding_sha256") != _embedding_binding_sha256(
            vector=vector,
            model_hash=model_hash,
            runtime_fingerprint=runtime_fingerprint,
            audio_sha256=audio_sha256,
        ):
            return None
        return vector
    except (OSError, TypeError, ValueError, SpeakerFinalizationError):
        return None


def _write_cached_embedding(
    path: Path,
    *,
    vector: Sequence[float],
    model_hash: str,
    runtime_fingerprint: str,
    audio_sha256: str,
) -> None:
    validated = _validate_campp_embedding(vector)
    document = {
        "schema_version": CAMPP_EMBEDDING_CACHE_SCHEMA,
        "model_sha256": model_hash,
        "runtime_fingerprint": runtime_fingerprint,
        "audio_sha256": audio_sha256,
        "dimension": CAMPP_EMBEDDING_DIMENSION,
        "embedding": validated,
    }
    document["binding_sha256"] = _embedding_binding_sha256(
        vector=validated,
        model_hash=model_hash,
        runtime_fingerprint=runtime_fingerprint,
        audio_sha256=audio_sha256,
    )
    _atomic_write_text(
        path,
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _campp_similarity_score(
    verifier: Callable[..., object],
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """Score cached embeddings through ModelScope's exact float32 code path."""

    # Validate structure and model-specific magnitude before handing data to
    # Torch; do not pre-compute cosine in Python because its float64 rounding
    # is not the production authority.
    validated_left = _validate_campp_embedding(left)
    validated_right = _validate_campp_embedding(right)
    compute = getattr(verifier, "compute_cos_similarity", None)
    if not callable(compute):
        raise SpeakerFinalizationError("CAM++ runtime has no compute_cos_similarity method")
    try:
        try:
            import torch  # type: ignore[import-not-found]
        except ImportError:
            # Lightweight unit-test fakes can accept validated Python lists;
            # the production speaker venv always has Torch.
            raw_score = compute(validated_left, validated_right)
        else:  # pragma: no cover - exercised by the production ML runtime
            raw_score = compute(
                torch.tensor(validated_left, dtype=torch.float32),
                torch.tensor(validated_right, dtype=torch.float32),
            )
        score = float(raw_score)
    except SpeakerFinalizationError:
        raise
    except Exception as exc:
        raise SpeakerFinalizationError(
            f"CAM++ cached-embedding similarity failed: {type(exc).__name__}: {exc}"
        ) from exc
    if not math.isfinite(score):
        raise SpeakerFinalizationError("CAM++ similarity must be finite")
    if not -1.0 - CAMPP_SCORE_ROUNDING_TOLERANCE <= score <= 1.0 + CAMPP_SCORE_ROUNDING_TOLERANCE:
        raise SpeakerFinalizationError("CAM++ similarity must be finite and within [-1, 1]")
    return min(1.0, max(-1.0, score))


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
    return _validate_campp_embedding(values)


def _build_embedding_similarity(
    *, verifier: Callable[..., object], model_hash: str, work_dir: Path
) -> Callable[[Path, Path], float]:
    """Embed each cue once, then score pairs by ModelScope cosine.

    The previous implementation asked the pipeline for every (cue, anchor) pair
    and re-embedded both wavs each time, so a talk clip paid O(cues x anchors)
    CAM++ inferences and long clips blew past the finalizer timeout.  A CAM++
    pair score is the cosine of the two per-clip embeddings, so embedding each
    wav a single time and caching the vector is exactly score-preserving while
    collapsing the cost to O(cues) inferences.

    The ``round(score, 5)`` below is not a tolerance: it reproduces the
    rounding ModelScope's own ``postprocess`` applies to a pairwise score, so a
    cached-embedding score equals the pairwise score bit for bit.
    """
    cache_dir = work_dir / "embedding-cache-v3"
    embeddings: dict[str, list[float]] = {}
    runtime_fingerprint = _campp_runtime_fingerprint(verifier)
    fingerprints: dict[Path, str] = {}

    def fingerprint(path: Path) -> str:
        resolved = path.resolve()
        if resolved not in fingerprints:
            fingerprints[resolved] = _sha256_file(resolved)
        return fingerprints[resolved]

    def embedding(path: Path) -> list[float]:
        # Cue basenames are reused after text/timing corrections. Content-bound
        # keys keep a persistent work dir from serving stale voice vectors.
        audio_sha256 = fingerprint(path)
        key = model_hash + "|" + runtime_fingerprint + "|" + audio_sha256
        vector = embeddings.get(key)
        if vector is None:
            cache_name = hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
            cache_path = cache_dir / cache_name
            vector = _load_cached_embedding(
                cache_path,
                model_hash=model_hash,
                runtime_fingerprint=runtime_fingerprint,
                audio_sha256=audio_sha256,
            )
        if vector is None:
            vector = _campp_embedding(verifier, path)
            _write_cached_embedding(
                cache_path,
                vector=vector,
                model_hash=model_hash,
                runtime_fingerprint=runtime_fingerprint,
                audio_sha256=audio_sha256,
            )
            embeddings[key] = vector
        else:
            embeddings[key] = vector
        return vector

    def similarity(left: Path, right: Path) -> float:
        return round(
            _campp_similarity_score(verifier, embedding(left), embedding(right)),
            5,
        )

    return similarity
