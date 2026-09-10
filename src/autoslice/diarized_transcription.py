"""One draft-ASR call shape; anonymous clusters never establish identity."""

from pathlib import Path
from typing import Any, Callable


class DiarizedTranscriptionError(RuntimeError):
    def __init__(self, reason_code: str, message: str, metadata: Any = None):
        super().__init__(message)
        self.reason_code = reason_code
        self.metadata = metadata


def transcribe_diarized(audio_path: bytes | Path, *, provider: str, duration_ms=None):
    """Return (draft SRT, metadata) with cue-aligned millisecond ``segments``.

    Every segment has start_ms/end_ms/text/speaker; speaker is anonymous or
    None. Native segments and the original response remain separate evidence.
    Provider selection is explicit, with no retry or hidden model fallback.
    """
    if provider == "moss":
        from src.autoslice.moss_transcription import MossTranscriptionError, transcribe_moss
        call, error_type = transcribe_moss, MossTranscriptionError
    elif provider == "mai":
        from src.autoslice.mai_transcription import MaiTranscriptionError, transcribe_mai
        call, error_type = transcribe_mai, MaiTranscriptionError
    else:
        raise ValueError(f"unsupported diarized provider: {provider}")
    try:
        return call(audio_path, duration_ms=duration_ms)
    except error_type as exc:
        raise DiarizedTranscriptionError(
            exc.reason_code, str(exc), getattr(exc, "metadata", None)
        ) from None


def transcribe_evidence(
    audio_path: bytes | Path,
    provider: str,
    duration_ms: int | None = None,
    before_request: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Return provider-native ASR evidence without forcing a publishable SRT."""

    if provider == "moss":
        from src.autoslice.moss_transcription import MossTranscriptionError, transcribe_moss_evidence

        call, error_type = transcribe_moss_evidence, MossTranscriptionError
    elif provider == "mai":
        from src.autoslice.mai_transcription import MaiTranscriptionError, transcribe_mai_evidence

        call, error_type = transcribe_mai_evidence, MaiTranscriptionError
    else:
        raise ValueError(f"unsupported diarized provider: {provider}")
    try:
        kwargs: dict[str, Any] = {"duration_ms": duration_ms}
        if before_request is not None:
            kwargs["before_request"] = before_request
        return call(audio_path, **kwargs)
    except error_type as exc:
        raise DiarizedTranscriptionError(
            exc.reason_code, str(exc), getattr(exc, "metadata", None)
        ) from None
