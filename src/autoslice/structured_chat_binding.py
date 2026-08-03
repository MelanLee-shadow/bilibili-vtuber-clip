"""Hash-bound structured-chat sidecar resolution for recording aliases."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from src.autoslice.chat_authority import recording_start_epoch_ms
from src.autoslice.source_subtitle_truth import _load_source_aliases


class StructuredChatBindingError(RuntimeError):
    """A committed structured-chat authority could not be bound safely."""


_RECORDING_DATE_RX = re.compile(r"_(?P<date>\d{8})-")
_SHA256_VALUE_RX = re.compile(r"(?:sha256:)?(?P<digest>[0-9a-f]{64})")


def _sha256_bound_file(path: Path, *, reason_code: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise StructuredChatBindingError(f"{reason_code}_NOT_REGULAR")
    try:
        if path.stat().st_size <= 0:
            raise StructuredChatBindingError(f"{reason_code}_EMPTY")
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise StructuredChatBindingError(f"{reason_code}_UNREADABLE") from exc
    return "sha256:" + hasher.hexdigest()


def _recording_date_directory(recording_basename: str) -> str | None:
    match = _RECORDING_DATE_RX.search(Path(recording_basename).stem)
    if match is None:
        return None
    raw = match.group("date")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _canonical_chat_candidates(
    segment: Path,
    *,
    canonical_recording_basename: str,
    canonical_rec_root: Path,
) -> tuple[Path, ...]:
    jsonl_name = Path(canonical_recording_basename).with_suffix(".jsonl").name
    candidates = [
        segment.parent / jsonl_name,
        segment.parent / "sources" / jsonl_name,
    ]
    date_directory = _recording_date_directory(canonical_recording_basename)
    if date_directory:
        candidates.extend(
            (
                canonical_rec_root / date_directory / jsonl_name,
                canonical_rec_root / date_directory / "sources" / jsonl_name,
            )
        )
    return tuple(dict.fromkeys(candidates))


def _load_aliases(ledger_path: Path) -> tuple[dict, ...]:
    try:
        document = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise StructuredChatBindingError(
            "STRUCTURED_CHAT_ALIAS_LEDGER_INVALID"
        ) from exc
    if not isinstance(document, dict):
        raise StructuredChatBindingError("STRUCTURED_CHAT_ALIAS_LEDGER_INVALID")
    try:
        aliases = _load_source_aliases(document)
    except RuntimeError as exc:
        raise StructuredChatBindingError(str(exc)) from exc
    return tuple(dict(alias) for alias in aliases)


def _normalized_sha256(value: object) -> str | None:
    match = _SHA256_VALUE_RX.fullmatch(str(value or "").strip().lower())
    if match is None:
        return None
    return "sha256:" + match.group("digest")


def _bound_chat_metadata(
    jsonl_path: Path,
    *,
    binding_status: str,
    timeline_offset_ms: int,
    binding_authority: str,
    source_alias_id: str | None = None,
    canonical_recording_basename: str | None = None,
) -> dict[str, object]:
    jsonl_sha256 = _sha256_bound_file(
        jsonl_path,
        reason_code="STRUCTURED_CHAT_JSONL",
    )
    origin_epoch_ms = recording_start_epoch_ms(jsonl_path)
    if (
        isinstance(origin_epoch_ms, bool)
        or not isinstance(origin_epoch_ms, int)
        or origin_epoch_ms <= 0
    ):
        raise StructuredChatBindingError(
            "STRUCTURED_CHAT_RECORDING_ORIGIN_UNAVAILABLE"
        )
    result: dict[str, object] = {
        "chat_jsonl": str(jsonl_path),
        "chat_jsonl_sha256": jsonl_sha256,
        "chat_origin_epoch_ms": origin_epoch_ms,
        "chat_timeline_offset_ms": timeline_offset_ms,
        "structured_chat_required": True,
        "chat_binding_status": binding_status,
        "chat_binding_authority": binding_authority,
    }
    if source_alias_id:
        result["chat_source_alias_id"] = source_alias_id
    if canonical_recording_basename:
        result["chat_canonical_recording_basename"] = (
            canonical_recording_basename
        )
    return result


def _existing_canonical_jsonl(
    segment: Path,
    *,
    canonical_recording_basename: str,
    canonical_rec_root: Path,
) -> Path | None:
    for candidate in _canonical_chat_candidates(
        segment,
        canonical_recording_basename=canonical_recording_basename,
        canonical_rec_root=canonical_rec_root,
    ):
        try:
            if (
                not candidate.is_symlink()
                and candidate.is_file()
                and candidate.stat().st_size > 0
            ):
                return candidate
        except OSError:
            continue
    return None


def resolve_structured_chat_binding(
    segment: Path,
    *,
    alias_ledger_path: Path,
    canonical_rec_root: Path,
    direct_jsonl: Path | None,
    source_sha256: str | None = None,
) -> dict[str, object]:
    """Resolve direct or alias chat without fuzzy timestamp matching."""

    aliases = [
        alias
        for alias in _load_aliases(alias_ledger_path)
        if alias.get("alias_recording_basename") == segment.name
    ]
    if not aliases:
        if direct_jsonl is not None:
            return _bound_chat_metadata(
                direct_jsonl,
                binding_status="BOUND_DIRECT",
                timeline_offset_ms=0,
                binding_authority="recording-sidecar-exact-basename.v1",
                canonical_recording_basename=segment.name,
            )
        return {
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        }

    normalized_source_sha256 = _normalized_sha256(source_sha256)
    if source_sha256 is not None and normalized_source_sha256 is None:
        raise StructuredChatBindingError(
            "STRUCTURED_CHAT_ALIAS_SOURCE_SHA256_INVALID"
        )
    if normalized_source_sha256 is None:
        normalized_source_sha256 = _sha256_bound_file(
            segment,
            reason_code="STRUCTURED_CHAT_ALIAS_SOURCE",
        )
    matches = [
        alias
        for alias in aliases
        if alias.get("alias_source_sha256") == normalized_source_sha256
    ]
    if len(matches) != 1:
        raise StructuredChatBindingError(
            "STRUCTURED_CHAT_ALIAS_SOURCE_SHA256_MISMATCH"
        )
    alias = matches[0]
    canonical_basename = str(alias["canonical_recording_basename"])
    jsonl_path = _existing_canonical_jsonl(
        segment,
        canonical_recording_basename=canonical_basename,
        canonical_rec_root=canonical_rec_root,
    )
    if jsonl_path is None:
        raise StructuredChatBindingError(
            "STRUCTURED_CHAT_ALIAS_CANONICAL_JSONL_MISSING"
        )
    return _bound_chat_metadata(
        jsonl_path,
        binding_status="BOUND_SOURCE_ALIAS",
        timeline_offset_ms=int(alias["alias_timeline_offset_ms"]),
        binding_authority=str(alias["authority"]),
        source_alias_id=str(alias["alias_id"]),
        canonical_recording_basename=canonical_basename,
    )
