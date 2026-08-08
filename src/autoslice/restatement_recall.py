"""In-session restatement recall: pair garbled cues with later clean restatements.

Ivan 2026-08-08 mechanism request: when a line is interrupted / rushed so ASR
garbles it, 李豆沙 often restates the same sentence slowly and completely a few
cues later. The later restatement is then independent structured text support
for repairing the earlier cue — the same trust shape as the danmu read-aloud
lane (session-internal verbatim text, not model imagination).

This module only *detects and proposes* pairs. It never edits text: proposals
flow into the existing candidate adjudication chain, where the candidate-blind
acoustic witness decides whether the early cue's audio actually contains the
restated sentence (保向铁律 — a true disfluency like a bare restart "我是" must
survive unchanged because its audio really is just the fragment).

Flagship case (2026-08-07 auto_200736_298_383): cue17 final text
「这是我的小孩就是了」(overlapped, machine-labeled 连线) is repaired by cue29
「这是我今天的宣言」(host, slow). Machine speaker labels are deliberately not a
hard pre-filter on the early side — the garbled cue's label is itself part of
what went wrong.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.jingting_chunker import parse_srt_cues

try:  # pragma: no cover - exercised via the pinyin-present path in tests
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover
    _lazy_pinyin = None

# Ivan 2026-08-08 pipeline wiring: findings carry a deterministic candidate
# (the restatement text itself) — unlike the microcue lane, which is
# proposal-less by design. The candidate-blind acoustic witness and CPA
# CURRENT/PROPOSED judge still own every mutation; this schema only records
# what was discovered and offered, never what was applied.
SCHEMA_VERSION = "restatement-candidates.v1"

_LABEL_PREFIX_RE = re.compile(r"^\[([^\[\]]{1,20})\]\s*")

_NON_LEXICAL_RE = re.compile(r"[^0-9A-Za-z一-鿿]+")
_INTERJECTION_RE = re.compile(r"^[哈啊呃哦嗯呀哎唉噢喔诶欸]+$")
# 她的 cue 开头高频语篇连接词/填充词。只影响配对打分的锚定，绝不参与改字：
# 「然后我…」「但是我…」这类开场会制造大量假前缀锚（探针实测 8/7 五候选
# sim>=0.45+prefix>=3 的 7 对里 4 对是纯连接词锚），剥掉后 prefix_run 只认
# 内容音节。迭代剥离，直到开头不再命中。
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:然后呢?|但是|就是说?|所以说?|因为|反正|可是|而且|其实|我操|"
    r"[呃嗯啊哦哎唉噢喔诶欸])"
)


@dataclass(frozen=True)
class RestatementCue:
    index: int
    start_seconds: float
    label: str | None
    text: str


@dataclass(frozen=True)
class RestatementPair:
    early_index: int
    late_index: int
    early_text: str
    late_text: str
    similarity: float
    prefix_run: int
    gap_seconds: float


def _lexical(text: str) -> str:
    return _NON_LEXICAL_RE.sub("", text)


def strip_leading_connectors(text: str) -> str:
    """Drop stacked discourse connectors/fillers from the front (scoring only)."""
    lexical = _lexical(text)
    while True:
        stripped = _LEADING_CONNECTOR_RE.sub("", lexical, count=1)
        if stripped == lexical:
            return lexical
        lexical = stripped


def pinyin_tokens(text: str) -> list[str] | None:
    """Toneless pinyin tokens of the lexical content; None when backend absent."""
    if _lazy_pinyin is None:
        return None
    lexical = _NON_LEXICAL_RE.sub(" ", text)
    return [
        re.sub(r"\s+", "", str(token).lower())
        for token in _lazy_pinyin(lexical.split())
        if str(token).strip()
    ]


def _prefix_run(early: list[str], late: list[str]) -> int:
    run = 0
    for a, b in zip(early, late):
        if a != b:
            break
        run += 1
    return run


def score_pair(early_text: str, late_text: str) -> tuple[float, int] | None:
    """(similarity, prefix_run) on toneless pinyin; None when backend absent.

    Similarity is measured on the full lexical content; the prefix anchor is
    measured after connector stripping so ubiquitous openers (然后/但是/…)
    cannot fake an interrupted-start anchor.
    """
    early = pinyin_tokens(early_text)
    late = pinyin_tokens(late_text)
    if early is None or late is None:
        return None
    if not early or not late:
        return (0.0, 0)
    early_anchor = pinyin_tokens(strip_leading_connectors(early_text)) or []
    late_anchor = pinyin_tokens(strip_leading_connectors(late_text)) or []
    return (
        SequenceMatcher(None, early, late).ratio(),
        _prefix_run(early_anchor, late_anchor),
    )


def find_restatement_pairs(
    cues: list[RestatementCue],
    *,
    min_early_chars: int = 4,
    min_late_chars: int = 6,
    min_cue_gap: int = 2,
    max_gap_seconds: float = 90.0,
    min_similarity: float = 0.45,
    min_prefix_run: int = 3,
    host_label: str | None = "李豆沙",
) -> list[RestatementPair]:
    """Ordered restatement proposals; empty when the pinyin backend is absent.

    A pair is proposed when a later, longer-window cue reads as a phonetically
    close restatement of an earlier cue: pinyin similarity over the threshold
    AND a shared spoken prefix (interrupted starts anchor at the front). Exact
    repeats are skipped (nothing to repair), as are pure interjection cues.
    When ``host_label`` is set and the transcript carries labels, only host
    cues qualify as the restatement side; the early side is never label
    filtered.

    Defaults are probe-tuned on the 2026-08-07 five-candidate harvest (408
    cues, scripts/probe_restatement_recall.py): sim>=0.45 with a >=3-syllable
    content-prefix anchor keeps 4 proposals across 5 slices — the flagship
    cue17->29 true positive plus three witness-rejectable benign pairs — with
    zero wrong-repair surface.
    """

    pairs: list[RestatementPair] = []
    for late_pos, late in enumerate(cues):
        if len(_lexical(late.text)) < min_late_chars:
            continue
        if _INTERJECTION_RE.match(_lexical(late.text)):
            continue
        if host_label is not None and late.label is not None and late.label != host_label:
            continue
        for early in cues[:late_pos]:
            if late.index - early.index < min_cue_gap:
                continue
            gap = late.start_seconds - early.start_seconds
            if gap <= 0 or gap > max_gap_seconds:
                continue
            early_lexical = _lexical(early.text)
            if len(early_lexical) < min_early_chars:
                continue
            if _INTERJECTION_RE.match(early_lexical):
                continue
            if early_lexical == _lexical(late.text):
                continue
            scored = score_pair(early.text, late.text)
            if scored is None:
                return []
            similarity, prefix_run = scored
            if similarity < min_similarity or prefix_run < min_prefix_run:
                continue
            pairs.append(
                RestatementPair(
                    early_index=early.index,
                    late_index=late.index,
                    early_text=early.text,
                    late_text=late.text,
                    similarity=round(similarity, 4),
                    prefix_run=prefix_run,
                    gap_seconds=round(gap, 3),
                )
            )
    return pairs


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _split_label_prefix(text: str) -> tuple[str, str | None, str]:
    """Split a leading ``[label] `` marker off a cue's SRT text.

    Returns ``(verbatim_prefix, label, rest)`` — the prefix excludes its
    trailing whitespace so callers can rejoin it with a single space. Absent
    a bracket (uniform-host period), label is ``None`` and the text is
    returned unchanged.
    """
    match = _LABEL_PREFIX_RE.match(text)
    if match is None:
        return "", None, text
    return match.group(0).rstrip(), match.group(1), text[match.end():]


def discover_restatement_findings(
    srt_text: str, *, host_label: str | None = "李豆沙"
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Proposal-carrying exact-final findings from in-session restatement pairs.

    Each finding names the early (garbled) cue as the suspect and offers the
    later restatement as ``proposed_full_cue`` — a deterministic candidate,
    not a mutation. The candidate-blind acoustic witness and CPA
    CURRENT/PROPOSED judge downstream still decide whether the early cue's
    audio actually contains the restated sentence.

    Never raises: any detection failure is caught here and reported as an
    ``ERROR`` receipt with zero findings — fail-open to "no restatement
    proposal", never fail-open to a text mutation.
    """
    srt_sha256 = "sha256:" + hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "decision_authority": "NONE_DISCOVERY_ONLY",
        "mutation_authorized": False,
        "srt_sha256": srt_sha256,
        "parameters": {"host_label": host_label},
        "pairs": [],
        "findings": [],
    }
    try:
        raw_cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
        raw_text_by_index: dict[int, str] = {}
        cues: list[RestatementCue] = []
        for ordinal, cue in enumerate(raw_cues, start=1):
            raw_text_by_index[ordinal] = cue.text
            _prefix, label, rest = _split_label_prefix(cue.text)
            cues.append(
                RestatementCue(
                    index=ordinal,
                    start_seconds=cue.start_ms / 1000.0,
                    label=label,
                    text=rest,
                )
            )
        pairs = find_restatement_pairs(cues, host_label=host_label)
    except Exception as exc:  # defensive: detector itself is unit-tested
        receipt.update(
            status="ERROR",
            reason_code="RESTATEMENT_DISCOVERY_ERROR",
            error_type=type(exc).__name__,
        )
        return [], receipt

    findings: list[dict[str, Any]] = []
    for pair in pairs:
        early_raw = raw_text_by_index[pair.early_index]
        late_raw = raw_text_by_index[pair.late_index]
        early_prefix, _early_label, _early_rest = _split_label_prefix(early_raw)
        _late_prefix, _late_label, late_rest = _split_label_prefix(late_raw)
        proposed_full_cue = f"{early_prefix} {late_rest}" if early_prefix else late_rest
        finding = {
            "cue": pair.early_index,
            "kind": "context",
            "proposed_full_cue": proposed_full_cue,
            "repair_class": "phonetic",
            "source_surface": None,
            "candidate_memory_id": None,
            "evidence_cue_ids": [pair.late_index],
            "suspect": early_raw,
            "why": (
                "[会话内重述候选] 晚 cue "
                f"{pair.late_index} 疑似早 cue {pair.early_index} 的会话内重述"
                f"（拼音相似度 {pair.similarity}，内容前缀锚 {pair.prefix_run}"
                f" 音节，间隔 {pair.gap_seconds}s）；仍需盲听证人与 CPA 闭集裁决"
            ),
            "candidate_provenance": {
                "kind": "session_restatement",
                "mutation_authorized": False,
                "source_cue": pair.late_index,
                "similarity": pair.similarity,
                "prefix_run": pair.prefix_run,
                "late_text_sha256": "sha256:"
                + hashlib.sha256(late_raw.encode("utf-8")).hexdigest(),
            },
        }
        findings.append(finding)
        receipt["pairs"].append(
            {
                "early_index": pair.early_index,
                "late_index": pair.late_index,
                "early_text": pair.early_text,
                "late_text": pair.late_text,
                "similarity": pair.similarity,
                "prefix_run": pair.prefix_run,
                "gap_seconds": pair.gap_seconds,
            }
        )
        receipt["findings"].append(
            {
                "cue_index": pair.early_index,
                "finding_sha256": "sha256:" + _sha256_json(finding),
            }
        )
    receipt["finding_count"] = len(findings)
    return findings, receipt


def merge_restatement_priority_findings(
    final_srt_text: str,
    microcue_findings: list[dict[str, Any]],
    microcue_audit: dict[str, Any],
    *,
    out_root: Path,
    cid: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fold restatement findings into the exact-final priority findings lane.

    ``discover_restatement_findings`` never raises (its own detector failure
    fails open to zero proposals). This function's only added job is to
    persist the restatement receipt as its own sidecar next to the microcue
    receipt; ``microcue_audit`` is returned unchanged — its schema stays
    owned by the microcue lane.
    """
    restatement_findings, receipt = discover_restatement_findings(final_srt_text)
    receipt_path = out_root / f"{cid}.restatement-candidates.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return [*microcue_findings, *restatement_findings], microcue_audit
