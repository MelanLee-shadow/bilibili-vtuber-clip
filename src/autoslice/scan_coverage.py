"""Immutable discovery coverage, bound to cue bytes and local dependencies.

This ledger records completed scans, never nomination counts or release rights.
The caller supplies C(P) from finding_context; discovery does not invent another
dependency graph. A receipt for an earlier version cannot cover changed bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Protocol


@dataclass(frozen=True)
class CueVersion:
    cue_index: int
    start_ms: int
    end_ms: int
    text_sha256: str

    def __post_init__(self) -> None:
        from src.autoslice.discovery_channels import DiscoveryContractError
        if (any(type(v) is not int for v in (self.cue_index, self.start_ms, self.end_ms))
            or self.cue_index < 1 or not 0 <= self.start_ms < self.end_ms):
            raise DiscoveryContractError("SCAN_CUE_GEOMETRY_INVALID")


@dataclass(frozen=True)
class ScanReceipt:
    channel: str
    scan_id: str
    cue: CueVersion
    dependencies: tuple[CueVersion, ...]


@dataclass(frozen=True)
class CoverageGap:
    cue_index: int
    channel: str


class CoverageReader(Protocol):
    def gaps(self, channels: tuple[str, ...]) -> tuple[CoverageGap, ...]: ...


@dataclass(frozen=True)
class ScanCoverage:
    source_id: str
    cues: tuple[CueVersion, ...]
    receipts: tuple[ScanReceipt, ...] = ()

    def __post_init__(self) -> None:
        from src.autoslice.discovery_channels import DiscoveryContractError
        if not self.source_id or len({c.cue_index for c in self.cues}) != len(self.cues):
            raise DiscoveryContractError("SCAN_SOURCE_OR_CUE_ID_INVALID")

    def record(
        self, *, channel: str, scan_id: str, scope_cues: frozenset[int],
        dependencies: tuple[tuple[int, tuple[int, ...]], ...] = (),
    ) -> ScanCoverage:
        from src.autoslice.discovery_channels import DiscoveryContractError
        by_index = {cue.cue_index: cue for cue in self.cues}
        if (not channel or not scan_id or any(type(i) is not int for i in scope_cues)
            or not scope_cues <= by_index.keys()):
            raise DiscoveryContractError("SCAN_SCOPE_INVALID")
        contexts = dict(dependencies)
        added: list[ScanReceipt] = []
        for index in sorted(scope_cues):
            required = set(contexts.get(index, ())) | {index}
            if not required <= by_index.keys():
                raise DiscoveryContractError("SCAN_DEPENDENCY_INVALID")
            added.append(ScanReceipt(channel, scan_id, by_index[index],
                tuple(by_index[i] for i in sorted(required))))
        # A later completed scan supersedes this channel/cue's old receipt.
        retained = tuple(r for r in self.receipts
            if r.channel != channel or r.cue.cue_index not in scope_cues)
        return replace(self, receipts=tuple(sorted((*retained, *added),
            key=lambda r: (r.cue.cue_index, r.channel))))

    def revise(
        self, cues: tuple[CueVersion, ...], *, affected_cues: frozenset[int],
        dependencies: tuple[tuple[int, tuple[int, ...]], ...] | None = None,
    ) -> ScanCoverage:
        contexts = dict(dependencies) if dependencies is not None else None
        current = {cue.cue_index: cue for cue in cues}
        # Clock changes, new/deleted cues and changed dependency bytes invalidate
        # coverage even when the caller's affected set accidentally omits them.
        receipts = tuple(r for r in self.receipts
            if r.cue.cue_index not in affected_cues
            and current.get(r.cue.cue_index) == r.cue
            and (contexts is None or {d.cue_index for d in r.dependencies}
                 == set(contexts.get(r.cue.cue_index, ())) | {r.cue.cue_index})
            and all(current.get(dep.cue_index) == dep for dep in r.dependencies))
        return replace(self, cues=cues, receipts=receipts)

    def gaps(self, channels: tuple[str, ...]) -> tuple[CoverageGap, ...]:
        current = {cue.cue_index: cue for cue in self.cues}
        covered = {(r.cue.cue_index, r.channel) for r in self.receipts
            if current.get(r.cue.cue_index) == r.cue
            and all(current.get(d.cue_index) == d for d in r.dependencies)}
        return tuple(CoverageGap(cue.cue_index, channel)
            for cue in self.cues for channel in sorted(set(channels))
            if (cue.cue_index, channel) not in covered)


def cue_versions(srt_text: str) -> tuple[CueVersion, ...]:
    from src.autoslice.jingting_chunker import parse_srt_cues
    return tuple(CueVersion(index, cue.start_ms, cue.end_ms,
        sha256(cue.text.encode()).hexdigest())
        for index, cue in enumerate((c for c in parse_srt_cues(srt_text) if c.text.strip()), 1))


def dependency_scopes(srt_text: str, *, source_offset_ms: int) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """Use WP-B's sole C(P) definition, including old/new relation handling there."""
    from typing import Callable, cast
    from src.autoslice.finding_context import context_for_check
    from src.autoslice.discovery_channels import DiscoveryContractError, json_object
    result: list[tuple[int, tuple[int, ...]]] = []
    for cue in cue_versions(srt_text):
        check = {'whole_clip_current_srt': srt_text, 'cue_indexes': [cue.cue_index],
            'source_media_timeline_offset_ms': source_offset_ms}
        context = json_object(cast(Callable[..., object], context_for_check)(check))
        indexes = context.get('scope_cue_indexes')
        if not isinstance(indexes, list) or any(type(i) is not int for i in indexes):
            raise DiscoveryContractError('SCAN_DEPENDENCY_INVALID')
        result.append((cue.cue_index, tuple(cast(int, i) for i in indexes)))
    return tuple(result)


def coverage_json(coverage: ScanCoverage) -> str:
    """The coverage ledger's single serialization boundary."""
    from dataclasses import asdict
    import json
    return json.dumps({'schema_version': 'scan-coverage.v1', **asdict(coverage)},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
