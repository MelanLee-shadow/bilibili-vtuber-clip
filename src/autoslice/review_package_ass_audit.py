"""Portable speaker-SRT/ASS release audit for review packages.

The burn subtitle is a derived artifact. Hash agreement alone does not prove
that it still renders every reviewed speaker cue at the reviewed time. This
module replays the production layout contract from the package-internal speaker
SRT and compares every expected event with the packaged ASS.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.subtitle_rendering import _layout_cue_for_display


ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(ROOT)
HOST_SPEAKER = CHANNEL_PROFILE.host_speaker_label
GUEST_SPEAKER = CHANNEL_PROFILE.guest_speaker_label

_SHA256_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")
_SRT_TIME_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\Z"
)
_SRT_TIMING_RE = re.compile(r"(.+?)\s+-->\s+(.+?)\Z")
_ASS_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{2})\Z")
_SPEAKER_RE = re.compile(
    r"^\[("
    + "|".join(re.escape(value) for value in (HOST_SPEAKER, GUEST_SPEAKER))
    + r")\]\s+(.*)\Z",
    re.S,
)


@dataclass(frozen=True)
class ReviewPackageAssIssue:
    code: str
    path: Path
    detail: str = ""


@dataclass(frozen=True)
class ReviewPackageAssAuditResult:
    ass_path: Path | None
    speaker_srt_path: Path | None
    issues: tuple[ReviewPackageAssIssue, ...]


@dataclass(frozen=True)
class _SpeakerCue:
    start_ms: int
    end_ms: int
    speaker: str
    text: str


@dataclass(frozen=True)
class _AssEvent:
    start_ms: int
    end_ms: int
    style: str
    text: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_matches(path: Path | None, expected: object) -> bool:
    return bool(
        path is not None
        and path.is_file()
        and isinstance(expected, str)
        and _SHA256_RE.fullmatch(expected)
        and _sha256(path) == expected.removeprefix("sha256:")
    )


def _portable_regular_file(
    root: Path,
    value: object,
    *,
    portable_required: bool,
) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    raw = Path(value)
    if portable_required and raw.is_absolute():
        return None
    candidate = raw if raw.is_absolute() else root / raw
    if not raw.is_absolute():
        cursor = root
        for part in raw.parts:
            cursor /= part
            if cursor.is_symlink():
                return None
    elif candidate.is_symlink():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if portable_required:
        try:
            resolved.relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return None
    if not resolved.is_file():
        return None
    return resolved


def _parse_srt_time(value: str) -> int | None:
    match = _SRT_TIME_RE.fullmatch(value.strip())
    if match is None:
        return None
    hours, minutes, seconds, millis = (
        int(part) for part in match.groups()
    )
    if minutes > 59 or seconds > 59:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _speaker_cues(
    path: Path,
) -> tuple[list[_SpeakerCue], list[ReviewPackageAssIssue]]:
    issues: list[ReviewPackageAssIssue] = []
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        return [], [
            ReviewPackageAssIssue(
                "SUBTITLE_SPEAKER_SRT_INVALID",
                path,
                f"{type(exc).__name__}: {exc}",
            )
        ]
    blocks = [
        block
        for block in re.split(
            r"\n[ \t]*\n",
            raw.replace("\r\n", "\n").replace("\r", "\n").strip(),
        )
        if block.strip()
    ]
    if not blocks:
        return [], [
            ReviewPackageAssIssue(
                "SUBTITLE_SPEAKER_SRT_INVALID", path, "zero cues"
            )
        ]

    cues: list[_SpeakerCue] = []
    previous_start_ms = -1
    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_INVALID",
                    path,
                    f"block {block_number} is too short",
                )
            )
            continue
        try:
            cue_index = int(lines[0].strip())
        except ValueError:
            cue_index = -1
        if cue_index != block_number:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_INVALID",
                    path,
                    f"block {block_number} has cue index {lines[0]!r}",
                )
            )
        timing = _SRT_TIMING_RE.fullmatch(lines[1].strip())
        if timing is None:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_INVALID",
                    path,
                    f"block {block_number} has invalid timing",
                )
            )
            continue
        start_ms = _parse_srt_time(timing.group(1))
        end_ms = _parse_srt_time(timing.group(2))
        if (
            start_ms is None
            or end_ms is None
            or end_ms <= start_ms
            or start_ms < previous_start_ms
        ):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_INVALID",
                    path,
                    f"block {block_number} has invalid timestamp order",
                )
            )
            continue
        previous_start_ms = start_ms
        body = "\n".join(lines[2:]).strip()
        speaker_match = _SPEAKER_RE.fullmatch(body)
        if speaker_match is None or not speaker_match.group(2).strip():
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_INVALID",
                    path,
                    f"block {block_number} lacks a canonical speaker label/text",
                )
            )
            continue
        cues.append(
            _SpeakerCue(
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=speaker_match.group(1),
                text=speaker_match.group(2).strip(),
            )
        )
    return cues, issues


def _parse_ass_time(value: str) -> int | None:
    match = _ASS_TIME_RE.fullmatch(value.strip())
    if match is None:
        return None
    hours, minutes, seconds, centis = (
        int(part) for part in match.groups()
    )
    if minutes > 59 or seconds > 59:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + centis * 10


def _ass_events(
    path: Path,
) -> tuple[list[_AssEvent], list[ReviewPackageAssIssue]]:
    issues: list[ReviewPackageAssIssue] = []
    events: list[_AssEvent] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return [], [
            ReviewPackageAssIssue(
                "SUBTITLE_ASS_DIALOGUE_INVALID",
                path,
                f"{type(exc).__name__}: {exc}",
            )
        ]
    for line_number, line in enumerate(lines, start=1):
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) != 10:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_DIALOGUE_INVALID",
                    path,
                    f"line {line_number} has {len(fields)} fields",
                )
            )
            continue
        start_ms = _parse_ass_time(fields[1])
        end_ms = _parse_ass_time(fields[2])
        if start_ms is None or end_ms is None or end_ms <= start_ms:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_DIALOGUE_INVALID",
                    path,
                    f"line {line_number} has invalid timestamps",
                )
            )
            continue
        events.append(
            _AssEvent(
                start_ms=start_ms,
                end_ms=end_ms,
                style=fields[3],
                text=fields[9],
            )
        )
    if not events:
        issues.append(
            ReviewPackageAssIssue(
                "SUBTITLE_ASS_DIALOGUE_MISSING", path
            )
        )
    return events, issues


def _speaker_ass_escape(value: str) -> str:
    line_break = "\u0000ASS_LINE_BREAK\u0000"
    return (
        value.replace(r"\N", line_break)
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", line_break)
        .replace(line_break, r"\N")
    )


def _expected_events(cues: list[_SpeakerCue]) -> list[_AssEvent]:
    events: list[_AssEvent] = []
    for cue in cues:
        style = "LDS" if cue.speaker == HOST_SPEAKER else "GUEST"
        for start_ms, end_ms, display_text in _layout_cue_for_display(
            cue.start_ms, cue.end_ms, cue.text
        ):
            events.append(
                _AssEvent(
                    start_ms=((start_ms + 5) // 10) * 10,
                    end_ms=((end_ms + 5) // 10) * 10,
                    style=style,
                    text=_speaker_ass_escape(display_text),
                )
            )
    return events


def _visual_issues(
    path: Path,
    events: list[_AssEvent],
    *,
    max_visual_lines: int,
    max_visual_line_chars: int,
) -> list[ReviewPackageAssIssue]:
    issues: list[ReviewPackageAssIssue] = []
    for index, event in enumerate(events, start=1):
        visual_lines = [
            part for part in re.split(r"\\+N", event.text) if part
        ]
        if len(visual_lines) > max_visual_lines:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_TOO_MANY_VISUAL_LINES",
                    path,
                    f"dialogue {index} has {len(visual_lines)} visual lines",
                )
            )
        for line in visual_lines:
            length = sum(1 for char in line if not char.isspace())
            if length > max_visual_line_chars:
                issues.append(
                    ReviewPackageAssIssue(
                        "SUBTITLE_ASS_LINE_TOO_LONG",
                        path,
                        f"dialogue {index} line length {length}: {line}",
                    )
                )
    return issues


def _pair_issues(
    ass_path: Path,
    speaker_srt_path: Path,
    *,
    events: list[_AssEvent],
    ass_is_valid: bool,
) -> list[ReviewPackageAssIssue]:
    cues, srt_issues = _speaker_cues(speaker_srt_path)
    issues = [*srt_issues]
    if srt_issues or not ass_is_valid:
        return issues
    expected = _expected_events(cues)
    if len(events) != len(expected):
        issues.append(
            ReviewPackageAssIssue(
                "SUBTITLE_ASS_SPEAKER_SRT_EVENT_COUNT_MISMATCH",
                ass_path,
                f"expected={len(expected)} actual={len(events)}",
            )
        )
    for index, (actual, wanted) in enumerate(
        zip(events, expected, strict=False), start=1
    ):
        if (actual.start_ms, actual.end_ms) != (
            wanted.start_ms,
            wanted.end_ms,
        ):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_SPEAKER_SRT_TIMELINE_MISMATCH",
                    ass_path,
                    f"event {index}: expected={wanted} actual={actual}",
                )
            )
            break
    for index, (actual, wanted) in enumerate(
        zip(events, expected, strict=False), start=1
    ):
        if actual.text != wanted.text:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_SPEAKER_SRT_TEXT_MISMATCH",
                    ass_path,
                    f"event {index}: expected={wanted.text!r} actual={actual.text!r}",
                )
            )
            break
    for index, (actual, wanted) in enumerate(
        zip(events, expected, strict=False), start=1
    ):
        if actual.style != wanted.style:
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_SPEAKER_SRT_STYLE_MISMATCH",
                    ass_path,
                    f"event {index}: expected={wanted.style} actual={actual.style}",
                )
            )
            break
    return issues


def audit_review_package_ass(
    *,
    root: Path,
    item: Mapping[str, Any],
    portable_required: bool,
    max_visual_lines: int,
    max_visual_line_chars: int,
    record: Mapping[str, Any] | None = None,
    chat_authority: Mapping[str, Any] | None = None,
) -> ReviewPackageAssAuditResult:
    """Audit portable paths, hashes, layout, and full speaker event parity."""

    issues: list[ReviewPackageAssIssue] = []
    ass_value = (
        item.get("ass_path")
        if portable_required
        else item.get("ass_path") or item.get("ass")
    )
    srt_value = item.get("speaker_srt")
    ass_path = _portable_regular_file(
        root, ass_value, portable_required=portable_required
    )
    speaker_srt_path = _portable_regular_file(
        root, srt_value, portable_required=portable_required
    )

    def require_path(
        value: object, path: Path | None, code: str, label: str
    ) -> None:
        if value is None and not portable_required:
            return
        if path is None:
            declared = Path(str(value)) if value is not None else root / "review_manifest.json"
            issues.append(
                ReviewPackageAssIssue(
                    code,
                    declared if declared.is_absolute() else root / declared,
                    f"{label} must be the exact package-relative regular file",
                )
            )

    require_path(
        ass_value,
        ass_path,
        "SUBTITLE_ASS_PATH_MISSING_OR_NONPORTABLE",
        "ASS",
    )
    require_path(
        srt_value,
        speaker_srt_path,
        "SUBTITLE_SPEAKER_SRT_PATH_MISSING_OR_NONPORTABLE",
        "speaker SRT",
    )

    manifest_ass_sha256 = item.get("ass_sha256")
    manifest_srt_sha256 = item.get("speaker_srt_sha256")
    if portable_required or manifest_ass_sha256 is not None:
        if not isinstance(manifest_ass_sha256, str) or not _SHA256_RE.fullmatch(
            manifest_ass_sha256
        ):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_HASH_MISSING_OR_INVALID",
                    ass_path or root / "review_manifest.json",
                )
            )
        elif not _hash_matches(ass_path, manifest_ass_sha256):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_HASH_MISMATCH",
                    ass_path or root / "review_manifest.json",
                    f"manifest={manifest_ass_sha256}",
                )
            )
    if portable_required or manifest_srt_sha256 is not None:
        if not isinstance(manifest_srt_sha256, str) or not _SHA256_RE.fullmatch(
            manifest_srt_sha256
        ):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_HASH_MISSING_OR_INVALID",
                    speaker_srt_path or root / "review_manifest.json",
                )
            )
        elif not _hash_matches(speaker_srt_path, manifest_srt_sha256):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_HASH_MISMATCH",
                    speaker_srt_path or root / "review_manifest.json",
                    f"manifest={manifest_srt_sha256}",
                )
            )

    events: list[_AssEvent] = []
    event_issues: list[ReviewPackageAssIssue] = []
    if ass_path is not None:
        events, event_issues = _ass_events(ass_path)
        issues.extend(event_issues)
        if not event_issues:
            issues.extend(
                _visual_issues(
                    ass_path,
                    events,
                    max_visual_lines=max_visual_lines,
                    max_visual_line_chars=max_visual_line_chars,
                )
            )

    if portable_required and ass_path is not None:
        artifact_hashes = (
            record.get("artifact_hashes")
            if isinstance(record, Mapping)
            else None
        )
        record_ass_sha256 = (
            artifact_hashes.get("ass_sha256")
            if isinstance(artifact_hashes, Mapping)
            else None
        )
        if not _hash_matches(ass_path, record_ass_sha256):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_RECORD_HASH_MISMATCH",
                    ass_path,
                    f"record={record_ass_sha256!r}",
                )
            )
        chat_ass_sha256 = (
            chat_authority.get("speaker_ass_sha256")
            if isinstance(chat_authority, Mapping)
            else None
        )
        if not _hash_matches(ass_path, chat_ass_sha256):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_ASS_CHAT_HASH_MISMATCH",
                    ass_path,
                    f"chat_authority={chat_ass_sha256!r}",
                )
            )
    if portable_required and speaker_srt_path is not None:
        chat_srt_sha256 = (
            chat_authority.get("final_speaker_srt_sha256")
            if isinstance(chat_authority, Mapping)
            else None
        )
        if not _hash_matches(speaker_srt_path, chat_srt_sha256):
            issues.append(
                ReviewPackageAssIssue(
                    "SUBTITLE_SPEAKER_SRT_CHAT_HASH_MISMATCH",
                    speaker_srt_path,
                    f"chat_authority={chat_srt_sha256!r}",
                )
            )

    if ass_path is not None and speaker_srt_path is not None:
        issues.extend(
            _pair_issues(
                ass_path,
                speaker_srt_path,
                events=events,
                ass_is_valid=not event_issues,
            )
        )

    return ReviewPackageAssAuditResult(
        ass_path=ass_path,
        speaker_srt_path=speaker_srt_path,
        issues=tuple(issues),
    )
