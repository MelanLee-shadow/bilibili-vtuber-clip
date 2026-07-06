from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_manifest(self) -> dict[str, int]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "duration_ms": self.duration_ms}


@dataclass(frozen=True)
class MediaSegmentObservation:
    path: str
    start_ms: int
    end_ms: int
    duration_ms: int
    size_bytes: int
    probed_ok: bool = True

    @property
    def range(self) -> TimeRange:
        return TimeRange(self.start_ms, self.end_ms)

    def to_manifest(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "size_bytes": self.size_bytes,
            "probed_ok": self.probed_ok,
        }


@dataclass(frozen=True)
class SourceIntegrityIssue:
    code: str
    message: str
    range: TimeRange | None = None
    segment_path: str | None = None
    severity: str = "BLOCK"

    def to_manifest(self) -> dict[str, object]:
        data: dict[str, object] = {"code": self.code, "message": self.message, "severity": self.severity}
        if self.range is not None:
            data["range"] = self.range.to_manifest()
        if self.segment_path is not None:
            data["segment_path"] = self.segment_path
        return data


@dataclass(frozen=True)
class SourceRangeLedger:
    room_id: str
    session_date: str
    expected_range: TimeRange
    observed_ranges: tuple[TimeRange, ...]
    missing_ranges: tuple[TimeRange, ...]
    issues: tuple[SourceIntegrityIssue, ...]
    can_use_local_source: bool
    compensation_required: bool
    replay_probe_required: bool
    danmaku_latest_ms: int | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "source-range-ledger.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "expected_range": self.expected_range.to_manifest(),
            "observed_ranges": [item.to_manifest() for item in self.observed_ranges],
            "missing_ranges": [item.to_manifest() for item in self.missing_ranges],
            "issues": [issue.to_manifest() for issue in self.issues],
            "can_use_local_source": self.can_use_local_source,
            "compensation_required": self.compensation_required,
            "replay_probe_required": self.replay_probe_required,
            "danmaku_latest_ms": self.danmaku_latest_ms,
        }

    def write_manifest(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class BilibiliReplayCompensationPlan:
    room_id: str
    session_date: str
    status: str
    download_ranges: tuple[TimeRange, ...]
    reason_codes: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "bilibili-replay-compensation-plan.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "status": self.status,
            "download_ranges": [item.to_manifest() for item in self.download_ranges],
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class BilibiliReplayDownloadCommandPlan:
    room_id: str
    session_date: str
    status: str
    commands: tuple[tuple[str, ...], ...]
    reason_codes: tuple[str, ...] = ()

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "bilibili-replay-download-command-plan.v1",
            "room_id": self.room_id,
            "session_date": self.session_date,
            "status": self.status,
            "commands": [_redact_command(command) for command in self.commands],
            "reason_codes": list(self.reason_codes),
        }


_MIN_SEGMENT_DURATION_MS = 10_000
_MIN_SEGMENT_SIZE_BYTES = 64 * 1024


def build_source_range_ledger(
    *,
    room_id: str,
    session_date: str,
    segments: Sequence[MediaSegmentObservation],
    expected_start_ms: int,
    expected_end_ms: int,
    danmaku_latest_ms: int | None = None,
    active_media_size_growth_bytes: int | None = None,
    max_gap_ms: int = 2_000,
    min_segment_duration_ms: int = _MIN_SEGMENT_DURATION_MS,
    min_segment_size_bytes: int = _MIN_SEGMENT_SIZE_BYTES,
) -> SourceRangeLedger:
    expected_range = TimeRange(expected_start_ms, expected_end_ms)
    ordered = sorted(segments, key=lambda item: (item.start_ms, item.end_ms, item.path))
    issues: list[SourceIntegrityIssue] = []

    if not ordered:
        issues.append(
            SourceIntegrityIssue(
                code="SOURCE_MEDIA_MISSING",
                message="No local media segment observations are available for the expected source range.",
                range=expected_range,
            )
        )

    observed_ranges: list[TimeRange] = []
    for segment in ordered:
        observed = TimeRange(max(expected_start_ms, segment.start_ms), min(expected_end_ms, segment.end_ms))
        if observed.duration_ms > 0:
            observed_ranges.append(observed)

    merged_ranges = _merge_ranges(observed_ranges, max_gap_ms=max_gap_ms)
    missing_ranges = _missing_ranges(expected_range, merged_ranges, max_gap_ms=max_gap_ms)
    coverage_complete = not missing_ranges

    for segment in ordered:
        observed = TimeRange(max(expected_start_ms, segment.start_ms), min(expected_end_ms, segment.end_ms))
        if not segment.probed_ok:
            # An unprobeable file (e.g. raw fmp4 .m4s sidecar) is harmless only when a
            # probed sibling segment records the same interval; otherwise it may hold
            # unique content and must block.
            has_probed_coverage = _has_redundant_probed_coverage(segment, ordered, max_gap_ms=max_gap_ms)
            degraded_severity = "WARN" if has_probed_coverage else "BLOCK"
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_PROBE_FAILED",
                    message="Media segment could not be probed successfully."
                    + (" Probed sibling coverage exists; treated as a redundant sidecar." if has_probed_coverage else ""),
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
        else:
            # A verified-but-tiny segment (recorder-restart stub) is only evidence of
            # loss when the session coverage is actually incomplete.
            degraded_severity = "WARN" if coverage_complete else "BLOCK"
        if segment.duration_ms < min_segment_duration_ms:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_SEGMENT_TOO_SHORT",
                    message="Media segment duration is too short to cover a live recording interval reliably.",
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
        if segment.size_bytes < min_segment_size_bytes:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_SEGMENT_TOO_SMALL",
                    message="Media segment is suspiciously small for a live recording interval.",
                    range=observed if observed.duration_ms else segment.range,
                    segment_path=segment.path,
                    severity=degraded_severity,
                )
            )
    for missing in missing_ranges:
        issues.append(
            SourceIntegrityIssue(
                code="MEDIA_COVERAGE_GAP",
                message="Expected source timeline has no local media coverage for this range.",
                range=missing,
            )
        )

    coverage_end_ms = max((item.end_ms for item in merged_ranges), default=expected_start_ms)
    if danmaku_latest_ms is not None and danmaku_latest_ms > coverage_end_ms + max_gap_ms:
        outrun_range = TimeRange(coverage_end_ms, min(expected_end_ms, danmaku_latest_ms))
        issues.append(
            SourceIntegrityIssue(
                code="DANMAKU_OUTRUNS_MEDIA",
                message="Danmaku/event timeline advances beyond available local media coverage.",
                range=outrun_range,
            )
        )
        if active_media_size_growth_bytes == 0:
            issues.append(
                SourceIntegrityIssue(
                    code="MEDIA_STALLED_WHILE_DANMAKU_ADVANCES",
                    message="Local media did not grow while danmaku/events continued advancing.",
                    range=outrun_range,
                )
            )

    blocking_issues = [issue for issue in issues if issue.severity != "WARN"]
    compensation_required = bool(missing_ranges or blocking_issues)
    can_use_local_source = not compensation_required
    replay_probe_required = compensation_required
    return SourceRangeLedger(
        room_id=room_id,
        session_date=session_date,
        expected_range=expected_range,
        observed_ranges=tuple(merged_ranges),
        missing_ranges=tuple(missing_ranges),
        issues=tuple(_dedupe_issues(issues)),
        can_use_local_source=can_use_local_source,
        compensation_required=compensation_required,
        replay_probe_required=replay_probe_required,
        danmaku_latest_ms=danmaku_latest_ms,
    )


def plan_bilibili_replay_compensation(
    ledger: SourceRangeLedger,
    *,
    auth_available: bool,
    replay_available: bool | None,
) -> BilibiliReplayCompensationPlan:
    if not ledger.compensation_required:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="NOT_REQUIRED",
            download_ranges=(),
        )

    if not auth_available:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="BLOCKED",
            download_ranges=(),
            reason_codes=("BILIBILI_REPLAY_AUTH_REQUIRED",),
        )

    if replay_available is None:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="PROBE_REQUIRED",
            download_ranges=ledger.missing_ranges,
            reason_codes=("BILIBILI_REPLAY_AVAILABILITY_UNKNOWN",),
        )

    if not replay_available:
        return BilibiliReplayCompensationPlan(
            room_id=ledger.room_id,
            session_date=ledger.session_date,
            status="RETRY_INFRA",
            download_ranges=(),
            reason_codes=("BILIBILI_REPLAY_UNAVAILABLE",),
        )

    return BilibiliReplayCompensationPlan(
        room_id=ledger.room_id,
        session_date=ledger.session_date,
        status="READY_TO_DOWNLOAD",
        download_ranges=ledger.missing_ranges or (ledger.expected_range,),
    )


def plan_bilibili_replay_download_commands(
    compensation: BilibiliReplayCompensationPlan,
    *,
    replay_url: str | None,
    output_dir: Path,
    cookie_file: Path | None,
    tool_path: str | Path | None,
) -> BilibiliReplayDownloadCommandPlan:
    if compensation.status != "READY_TO_DOWNLOAD":
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status=compensation.status,
            commands=(),
            reason_codes=compensation.reason_codes,
        )
    if not replay_url:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="BLOCKED",
            commands=(),
            reason_codes=("BILIBILI_REPLAY_URL_REQUIRED",),
        )
    if cookie_file is None:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="BLOCKED",
            commands=(),
            reason_codes=("BILIBILI_REPLAY_AUTH_REQUIRED",),
        )
    if tool_path is None:
        return BilibiliReplayDownloadCommandPlan(
            room_id=compensation.room_id,
            session_date=compensation.session_date,
            status="RETRY_INFRA",
            commands=(),
            reason_codes=("BILIBILI_DOWNLOAD_TOOL_MISSING",),
        )

    commands: list[tuple[str, ...]] = []
    for item in compensation.download_ranges:
        output_template = output_dir / f"{compensation.room_id}_{compensation.session_date}_{item.start_ms}_{item.end_ms}.%(ext)s"
        commands.append(
            (
                str(tool_path),
                replay_url,
                "--download-sections",
                f"*{_format_ms(item.start_ms)}-{_format_ms(item.end_ms)}",
                "--cookies",
                str(cookie_file),
                "-o",
                str(output_template),
            )
        )
    return BilibiliReplayDownloadCommandPlan(
        room_id=compensation.room_id,
        session_date=compensation.session_date,
        status="READY",
        commands=tuple(commands),
    )


def _has_redundant_probed_coverage(
    segment: MediaSegmentObservation,
    segments: Sequence[MediaSegmentObservation],
    *,
    max_gap_ms: int,
) -> bool:
    probed_ranges: list[TimeRange] = []
    for other in segments:
        if other is segment or not other.probed_ok or other.duration_ms <= 0:
            continue
        if segment.duration_ms <= 0:
            if abs(other.start_ms - segment.start_ms) <= max_gap_ms:
                return True
            continue
        probed_ranges.append(other.range)

    if segment.duration_ms <= 0:
        return False
    merged = _merge_ranges(probed_ranges, max_gap_ms=max_gap_ms)
    return not _missing_ranges(segment.range, merged, max_gap_ms=max_gap_ms)


def _format_ms(value: int) -> str:
    millis = max(0, int(value))
    total_seconds, ms = divmod(millis, 1000)
    minutes_total, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def _redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for part in command:
        if hide_next:
            redacted.append("[REDACTED_COOKIE_FILE]")
            hide_next = False
            continue
        redacted.append(part)
        if part == "--cookies":
            hide_next = True
    return redacted


def _merge_ranges(ranges: Sequence[TimeRange], *, max_gap_ms: int) -> list[TimeRange]:
    merged: list[TimeRange] = []
    for item in sorted((r for r in ranges if r.duration_ms > 0), key=lambda r: (r.start_ms, r.end_ms)):
        if not merged or item.start_ms > merged[-1].end_ms + max_gap_ms:
            merged.append(item)
        else:
            previous = merged[-1]
            merged[-1] = TimeRange(previous.start_ms, max(previous.end_ms, item.end_ms))
    return merged


def _missing_ranges(expected: TimeRange, observed: Sequence[TimeRange], *, max_gap_ms: int) -> list[TimeRange]:
    missing: list[TimeRange] = []
    cursor = expected.start_ms
    for item in observed:
        if item.end_ms <= expected.start_ms or item.start_ms >= expected.end_ms:
            continue
        start = max(expected.start_ms, item.start_ms)
        end = min(expected.end_ms, item.end_ms)
        if start > cursor + max_gap_ms:
            missing.append(TimeRange(cursor, start))
        cursor = max(cursor, end)
    if expected.end_ms > cursor + max_gap_ms:
        missing.append(TimeRange(cursor, expected.end_ms))
    return missing


def _dedupe_issues(issues: Sequence[SourceIntegrityIssue]) -> list[SourceIntegrityIssue]:
    seen: set[tuple[object, ...]] = set()
    result: list[SourceIntegrityIssue] = []
    for issue in issues:
        key = (
            issue.code,
            issue.segment_path,
            issue.range.start_ms if issue.range else None,
            issue.range.end_ms if issue.range else None,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(issue)
    return result
