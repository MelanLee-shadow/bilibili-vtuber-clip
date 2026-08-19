"""F12 受话人归属判项：hook/标题事实审补上"这话是对谁说的"。

出处（维护者 深夜纠错，受骗片标题案，
内部取证综述文档留存「深夜追加:F12」）：
自动 hook「{host}刚被劝别再受骗」事实错——字幕 cue1-3 的「别再被骗」是连线主持
对**上一位选手**说的，cue4「下一位，我们的08号」之后本人才上场，她从未被劝。
`source_fact_review` 只验"这话说过没有"（词面确实在最终字幕里），**不验"对谁说"**，
所以把场上刚发生的一句话错归到主角头上是它结构上看不见的一类错。

本模块提供该判项的确定性半边：

* `build_addressee_transcripts` —— 从已产出的 speaker-final SRT 取出**带说话人
  标签**的转写（hash-bound + 与最终字幕逐条对齐才采用），作为判官的第二份文字
  authority；纯文字 `final_transcript` 逐字不变（既有回执/校验全部靠它绑定）。
* `requires_addressee_attribution` —— 文案里出现指向性言语行为标记时，判官**不准
  交空判项**（F15 盲证人同款纪律：可以答"无法判定"，不可以装作没这回事）。
* `evaluate_addressee_attribution` —— 判项形状与证据绑定校验；
  `WRONG_ADDRESSEE` 与 `status=KEEP` 互斥（fail-closed 打回重生成），
  无说话人转写时只允许 `UNVERIFIABLE`（没有标签就没有归属权威）。

刻意不做的事：不改字幕、不改说话人二分、不新增 schema 版本
（`source_fact_review` 的 SCHEMA_VERSION 是已落盘回执的相等性锚，动它等于让历史
回执整批失效）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SPEAKER_FINALIZATION_SCHEMA,
)
from src.autoslice.speaker_guess import (
    RUNG_LABELS as SPEAKER_GUESS_RUNG_LABELS,
    SPEAKER_GUESS_SCHEMA,
    SPEAKER_GUESS_STATUS,
)

SPEAKER_TRANSCRIPT_LABEL = "speaker_transcript"
ADDRESSEE_VERDICTS = ("SUPPORTED", "WRONG_ADDRESSEE", "UNVERIFIABLE")
ADDRESSEE_UNRESOLVED_REASON = "CPA_TEXT_REVIEW_ADDRESSEE_ATTRIBUTION_UNRESOLVED"
MODE_SPEAKER_LABELLED = "speaker_labelled"
MODE_UNVERIFIABLE = "unverifiable_no_speaker_transcript"

_SPEAKER_LINE_RE = re.compile(
    r"^\[("
    + "|".join(re.escape(value) for value in (HOST_SPEAKER, GUEST_SPEAKER))
    + r")\]\s*(.*)\Z",
    re.S,
)
_SRT_CLOCK_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\Z")
_SRT_TIMING_LINE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+"
    r"(\d{2}:\d{2}:\d{2},\d{3})\Z"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

ALIGNMENT_SCHEMA_VERSION = "speaker-cue-subsegment-alignment.v1"
ALIGNMENT_POLICY_ID = "speaker_cue_subsegment_alignment/v1"
TEXT_PARTITION_POLICY_ID = "compact_ws/v1"
TIMING_POLICY_ID = "half_open_integer_ms_exact/v1"
ABSENCE_POLICY_ID = "speaker_mode_uniform_host/v1"


class SpeakerEvidenceState(str, Enum):
    """Explicit states of the fail-closed speaker-evidence boundary."""

    PRESENT_VALID = "PresentValid"
    ABSENT_AUTHORIZED = "AbsentAuthorized"
    REJECTED = "Rejected"


class SpeakerEvidenceRejected(ValueError):
    """Present, incomplete, or drifted speaker evidence (never absence).

    ``code`` is deliberately stable so production/package callers can expose a
    deterministic blocker without parsing exception prose.
    """

    state = SpeakerEvidenceState.REJECTED

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class SpeakerGuessReviewRequired(SpeakerEvidenceRejected):
    """A fully bound guess is review material, never addressee evidence."""

    code = "SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"

    def __init__(self, detail: str) -> None:
        super().__init__(self.code, detail)


@dataclass(frozen=True)
class SpeakerEvidenceResult:
    """Canonical, JSON-ready evidence supplied to the source-fact judge."""

    state: SpeakerEvidenceState
    transcript: str | None
    speaker_evidence: dict[str, object]
    speaker_evidence_sha256: str


@dataclass(frozen=True)
class _SpeakerSegment:
    index: int
    start: str
    end: str
    start_ms: int
    end_ms: int
    speaker: str
    text: str


@dataclass(frozen=True)
class _PlainCue:
    source_index: int
    cue_id: str
    start_ms: int
    end_ms: int
    text: str


# 指向性言语行为标记：出现任一即认为文案含"谁对谁做/说了什么"的归属断言，
# 判官必须给出判项。刻意宁滥勿缺——过触发的代价只是多一条 UNVERIFIABLE，
# 漏触发的代价是受骗片那种把别人挨的话安到主角头上的事实错原样发出去。
_ATTRIBUTION_MARKERS = (
    "被",
    "劝",
    "让",
    "叫",
    "告诉",
    "问",
    "回应",
    "回怼",
    "怼",
    "骂",
    "夸",
    "催",
    "求",
    "提醒",
    "警告",
    "喊话",
    "喊",
    "教",
    "训",
    "怪",
    "怼回",
    "对我说",
    "对她说",
    "对他说",
    "跟我说",
    "跟她说",
    "跟他说",
    "说她",
    "说他",
    "点名",
    "邀请",
    "拒绝",
    "答应",
    "道歉",
    "表白",
    "吐槽",
    "安慰",
    "祝",
)


def _compact(value: object) -> str:
    return "".join(str(value or "").split())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bound_digest(value: object, *, code: str, label: str) -> str:
    if not isinstance(value, str):
        raise SpeakerEvidenceRejected(code, f"{label} is missing")
    digest = value.removeprefix("sha256:")
    if _SHA256_RE.fullmatch(digest) is None:
        raise SpeakerEvidenceRejected(code, f"{label} is not a SHA-256 digest")
    return digest


def _clock_ms(value: str, *, code: str, label: str) -> int:
    match = _SRT_CLOCK_RE.fullmatch(value)
    if match is None:
        raise SpeakerEvidenceRejected(code, f"{label} has invalid SRT time {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        raise SpeakerEvidenceRejected(code, f"{label} has invalid SRT time {value!r}")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def _parse_speaker_srt_bytes(value: bytes) -> list[_SpeakerSegment]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpeakerEvidenceRejected(
            "SPEAKER_SRT_INVALID", "speaker-final SRT is not UTF-8"
        ) from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise SpeakerEvidenceRejected("SPEAKER_SRT_INVALID", "speaker-final SRT is empty")
    rows: list[_SpeakerSegment] = []
    for expected_index, block in enumerate(re.split(r"\n\s*\n", normalized), start=1):
        lines = block.splitlines()
        if len(lines) < 3 or lines[0].strip() != str(expected_index):
            raise SpeakerEvidenceRejected(
                "SPEAKER_SRT_INVALID",
                "speaker-final SRT indices must be contiguous from 1",
            )
        timing = _SRT_TIMING_LINE_RE.fullmatch(lines[1].strip())
        if timing is None:
            raise SpeakerEvidenceRejected(
                "SPEAKER_SRT_INVALID", f"speaker segment {expected_index} has invalid timing"
            )
        payload = "\n".join(lines[2:]).strip()
        labelled = _SPEAKER_LINE_RE.fullmatch(payload)
        if labelled is None or not labelled.group(2).strip():
            raise SpeakerEvidenceRejected(
                "SPEAKER_SRT_INVALID",
                f"speaker segment {expected_index} lacks a canonical non-empty label",
            )
        start, end = timing.groups()
        start_ms = _clock_ms(
            start, code="SPEAKER_SRT_INVALID", label=f"speaker segment {expected_index} start"
        )
        end_ms = _clock_ms(
            end, code="SPEAKER_SRT_INVALID", label=f"speaker segment {expected_index} end"
        )
        if end_ms <= start_ms:
            raise SpeakerEvidenceRejected(
                "SPEAKER_SRT_INVALID",
                f"speaker segment {expected_index} has non-positive duration",
            )
        rows.append(
            _SpeakerSegment(
                index=expected_index,
                start=start,
                end=end,
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=labelled.group(1),
                text=labelled.group(2).strip(),
            )
        )
    return rows


def parse_speaker_labelled_srt(text: str) -> list[tuple[str, str]] | None:
    """Return ``[(speaker, text)]`` for a canonical speaker-final SRT, else None."""

    try:
        rows = _parse_speaker_srt_bytes(text.encode("utf-8"))
    except SpeakerEvidenceRejected:
        return None
    return [(row.speaker, row.text) for row in rows]


def render_speaker_transcript(rows: Sequence[tuple[str, str]]) -> str:
    """Numbered speaker-labelled lines: turn order is the whole point here."""

    return "\n".join(
        f"{index} [{speaker}] {text}" for index, (speaker, text) in enumerate(rows, start=1)
    )


def _plain_cues(cues: Sequence[object]) -> list[_PlainCue]:
    normalized: list[_PlainCue] = []
    previous_end: int | None = None
    for source_index, cue in enumerate(cues, start=1):
        start = getattr(cue, "source_start_ms", None)
        end = getattr(cue, "source_end_ms", None)
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            raise SpeakerEvidenceRejected(
                "SPEAKER_CUE_GRID_INVALID",
                f"plain cue {source_index} lacks integer-millisecond timing",
            )
        if end <= start:
            raise SpeakerEvidenceRejected(
                "SPEAKER_CUE_GRID_INVALID",
                f"plain cue {source_index} has non-positive duration",
            )
        if previous_end is not None and start < previous_end:
            raise SpeakerEvidenceRejected("SPEAKER_CUE_GRID_INVALID", "plain cue intervals overlap")
        previous_end = end
        normalized.append(
            _PlainCue(
                source_index=source_index,
                cue_id=str(getattr(cue, "cue_id", source_index)),
                start_ms=start,
                end_ms=end,
                text=str(getattr(cue, "text", "")).strip(),
            )
        )
    if not normalized:
        raise SpeakerEvidenceRejected("SPEAKER_CUE_GRID_INVALID", "plain cue grid is empty")
    return normalized


def _manifest_document(value: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_INVALID", "speaker-final manifest is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_INVALID", "speaker-final manifest must be an object"
        )
    return decoded


def _validate_speaker_guess_receipt(manifest: Mapping[str, object]) -> None:
    """Accept only the producer's explicit, non-uploadable manual-review envelope."""

    receipt = manifest.get("speaker_guess")
    if not isinstance(receipt, Mapping):
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt is missing",
        )
    expected = {
        "schema_version": SPEAKER_GUESS_SCHEMA,
        "status": SPEAKER_GUESS_STATUS,
        "speaker_authority": "GUESSED_NOT_EVIDENCE_BACKED",
        "upload_authorized": False,
        "review_authority": "HUMAN_OPERATOR",
        "resolution": "SPEAKER_TURN_OVERRIDE_OR_EXPLICIT_REJECTION",
        "thresholds_unchanged": True,
    }
    for receipt_field, expected_value in expected.items():
        if receipt.get(receipt_field) != expected_value:
            raise SpeakerEvidenceRejected(
                "SPEAKER_GUESS_RECEIPT_INVALID",
                f"speaker guess receipt has invalid {receipt_field}",
            )
    rung = receipt.get("rung")
    if rung not in SPEAKER_GUESS_RUNG_LABELS:
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt has an unknown review rung",
        )
    if receipt.get("rung_label") != SPEAKER_GUESS_RUNG_LABELS[rung]:
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt rung label differs from its rung",
        )
    source_cue_count = receipt.get("source_cue_count")
    low_confidence_cue_count = receipt.get("low_confidence_cue_count")
    low_confidence_cues = receipt.get("low_confidence_cues")
    low_confidence_cues_truncated = receipt.get("low_confidence_cues_truncated")
    speaker_counts = receipt.get("speaker_counts")
    if (
        not isinstance(source_cue_count, int)
        or isinstance(source_cue_count, bool)
        or source_cue_count < 1
        or not isinstance(low_confidence_cue_count, int)
        or isinstance(low_confidence_cue_count, bool)
        or low_confidence_cue_count < 0
        or not isinstance(low_confidence_cues, list)
        or not all(isinstance(row, Mapping) for row in low_confidence_cues)
        or not isinstance(low_confidence_cues_truncated, bool)
        or not isinstance(speaker_counts, Mapping)
        or not speaker_counts
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt cue disclosure is invalid",
        )
    if (
        low_confidence_cue_count > source_cue_count
        or low_confidence_cue_count < len(low_confidence_cues)
        or low_confidence_cues_truncated != (low_confidence_cue_count > len(low_confidence_cues))
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt low-confidence counts are incoherent",
        )
    if (
        any(
            not isinstance(speaker, str)
            or not speaker
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            for speaker, count in speaker_counts.items()
        )
        or sum(speaker_counts.values()) != source_cue_count
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt speaker counts are incoherent",
        )
    disclosed_indices = [row.get("source_index") for row in low_confidence_cues]
    if any(
        not isinstance(index, int)
        or isinstance(index, bool)
        or index < 1
        or index > source_cue_count
        for index in disclosed_indices
    ) or len(set(disclosed_indices)) != len(disclosed_indices):
        raise SpeakerEvidenceRejected(
            "SPEAKER_GUESS_RECEIPT_INVALID",
            "speaker guess receipt cue indices are invalid or duplicated",
        )


def _hash_claims(record: Mapping[str, object]) -> Mapping[str, object]:
    hashes = record.get("artifact_hashes")
    if not isinstance(hashes, Mapping):
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_INCOMPLETE", "record.artifact_hashes is missing"
        )
    return hashes


def _has_speaker_claim(record: Mapping[str, object]) -> bool:
    hashes = record.get("artifact_hashes")
    values = [
        record.get("speaker_review_srt_path"),
        record.get("speaker_finalization_manifest_path"),
        record.get("speaker_finalization_manifest_sha256"),
        record.get("speaker_finalization"),
        hashes.get("speaker_review_srt_sha256") if isinstance(hashes, Mapping) else None,
    ]
    return any(value not in (None, "", {}) for value in values)


def _absent_result() -> SpeakerEvidenceResult:
    evidence: dict[str, object] = {
        "state": SpeakerEvidenceState.ABSENT_AUTHORIZED.value,
        "reason": "speaker_mode_uniform_host",
        "policy_ids": {
            "alignment": ALIGNMENT_POLICY_ID,
            "text": TEXT_PARTITION_POLICY_ID,
            "timing": TIMING_POLICY_ID,
            "absence": ABSENCE_POLICY_ID,
        },
    }
    return SpeakerEvidenceResult(
        state=SpeakerEvidenceState.ABSENT_AUTHORIZED,
        transcript=None,
        speaker_evidence=evidence,
        speaker_evidence_sha256="sha256:" + _sha256_bytes(_canonical_json_bytes(evidence)),
    )


def _alignment_and_transcript(
    cues: Sequence[_PlainCue],
    segments: Sequence[_SpeakerSegment],
    decisions: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], str]:
    if len(decisions) != len(segments):
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_INVALID", "final_decisions count differs from speaker SRT"
        )

    grouped: dict[int, list[_SpeakerSegment]] = {cue.source_index: [] for cue in cues}
    previous_source_index = 0
    previous_segment: _SpeakerSegment | None = None
    for segment, decision in zip(segments, decisions):
        source_index = decision.get("source_index")
        if not isinstance(source_index, int) or isinstance(source_index, bool):
            raise SpeakerEvidenceRejected(
                "SPEAKER_MANIFEST_INVALID",
                f"final decision {segment.index} lacks integer source_index",
            )
        if (
            decision.get("start") != segment.start
            or decision.get("end") != segment.end
            or decision.get("speaker") != segment.speaker
            or decision.get("text") != segment.text
        ):
            raise SpeakerEvidenceRejected(
                "SPEAKER_MANIFEST_SRT_MISMATCH",
                f"final decision {segment.index} differs from speaker SRT",
            )
        if decision.get("layer", 0) != 0 or decision.get("placement", "main") != "main":
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_UNSUPPORTED_LAYER",
                f"speaker segment {segment.index} is not a main layer-0 segment",
            )
        temporal_owners = [
            cue
            for cue in cues
            if max(cue.start_ms, segment.start_ms) < min(cue.end_ms, segment.end_ms)
        ]
        if len(temporal_owners) != 1:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_OWNER_INVALID",
                f"speaker segment {segment.index} has {len(temporal_owners)} temporal owners",
            )
        owner = temporal_owners[0]
        if segment.start_ms < owner.start_ms or segment.end_ms > owner.end_ms:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_CROSS_BOUNDARY",
                f"speaker segment {segment.index} escapes plain cue {owner.source_index}",
            )
        if source_index != owner.source_index:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_OWNER_MISMATCH",
                f"speaker segment {segment.index} temporal owner disagrees with manifest source_index",
            )
        if source_index < previous_source_index:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_NON_MONOTONE", "manifest source_index ownership is non-monotone"
            )
        if previous_segment is not None and segment.start_ms < previous_segment.end_ms:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_SEGMENT_OVERLAP", "speaker segments overlap"
            )
        previous_source_index = source_index
        previous_segment = segment
        grouped[source_index].append(segment)

    rendered: list[str] = []
    group_documents: list[dict[str, object]] = []
    for cue in cues:
        owned = grouped[cue.source_index]
        if _compact(cue.text) and not owned:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_CUE_UNCOVERED",
                f"non-empty plain cue {cue.source_index} has no speaker segment",
            )
        if not owned:
            continue
        if not _compact(cue.text):
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_EMPTY_CUE_OWNED",
                f"empty plain cue {cue.source_index} owns speaker content",
            )
        if owned[0].start_ms != cue.start_ms or owned[-1].end_ms != cue.end_ms:
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_COVERAGE_GAP",
                f"speaker segments do not exactly cover plain cue {cue.source_index}",
            )
        for left, right in zip(owned, owned[1:]):
            if left.end_ms != right.start_ms:
                raise SpeakerEvidenceRejected(
                    "SPEAKER_ALIGNMENT_COVERAGE_GAP",
                    f"speaker segments have a gap/overlap inside plain cue {cue.source_index}",
                )
        if _compact("".join(segment.text for segment in owned)) != _compact(cue.text):
            raise SpeakerEvidenceRejected(
                "SPEAKER_ALIGNMENT_TEXT_MISMATCH",
                f"speaker text is not a whitespace-only partition of plain cue {cue.source_index}",
            )
        segment_documents: list[dict[str, object]] = []
        split = len(owned) > 1
        for sub_index, segment in enumerate(owned, start=1):
            rendered_id = f"{cue.source_index}.{sub_index}" if split else str(cue.source_index)
            rendered.append(f"{rendered_id} [{segment.speaker}] {segment.text}")
            segment_documents.append(
                {
                    "segment_index": segment.index,
                    "rendered_id": rendered_id,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "speaker": segment.speaker,
                    "text": segment.text,
                }
            )
        group_documents.append(
            {
                "source_index": cue.source_index,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "text": cue.text,
                "segments": segment_documents,
            }
        )
    alignment: dict[str, object] = {
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "policy_ids": {
            "alignment": ALIGNMENT_POLICY_ID,
            "text": TEXT_PARTITION_POLICY_ID,
            "timing": TIMING_POLICY_ID,
        },
        "source_cue_count": len(cues),
        "speaker_segment_count": len(segments),
        "groups": group_documents,
    }
    return alignment, "\n".join(rendered)


def rebuild_speaker_evidence(
    record: Mapping[str, object],
    cues: Sequence[object],
    *,
    speaker_srt_bytes: bytes | None = None,
    speaker_manifest_bytes: bytes | None = None,
) -> SpeakerEvidenceResult:
    """Purely rebuild speaker evidence from caller-supplied, package-safe bytes.

    This function never follows the paths declared by ``record``.  Package
    import/audit callers must resolve containment themselves and pass the exact
    bytes.  ``None`` is represented only by explicit ``uniform_host`` authority;
    claimed or incomplete evidence raises :class:`SpeakerEvidenceRejected`.
    """

    claimed = _has_speaker_claim(record)
    if record.get("speaker_mode") == "uniform_host":
        if claimed or speaker_srt_bytes is not None or speaker_manifest_bytes is not None:
            raise SpeakerEvidenceRejected(
                "SPEAKER_EVIDENCE_MODE_CONFLICT",
                "uniform_host record also claims speaker-final evidence",
            )
        return _absent_result()
    if not claimed:
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_ABSENCE_UNAUTHORIZED",
            "speaker evidence is absent without uniform_host authority",
        )
    if speaker_srt_bytes is None or speaker_manifest_bytes is None:
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_BYTES_REQUIRED",
            "claimed speaker evidence requires exact SRT and manifest bytes",
        )
    if not isinstance(record.get("speaker_review_srt_path"), str) or not record.get(
        "speaker_review_srt_path"
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_INCOMPLETE", "speaker_review_srt_path is missing"
        )
    if not isinstance(record.get("speaker_finalization_manifest_path"), str) or not record.get(
        "speaker_finalization_manifest_path"
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_INCOMPLETE", "speaker_finalization_manifest_path is missing"
        )

    hashes = _hash_claims(record)
    expected_srt = _bound_digest(
        hashes.get("speaker_review_srt_sha256"),
        code="SPEAKER_EVIDENCE_INCOMPLETE",
        label="artifact_hashes.speaker_review_srt_sha256",
    )
    actual_srt = _sha256_bytes(speaker_srt_bytes)
    if actual_srt != expected_srt:
        raise SpeakerEvidenceRejected(
            "SPEAKER_SRT_HASH_MISMATCH", "speaker-final SRT bytes differ from record hash"
        )
    expected_manifest = _bound_digest(
        record.get("speaker_finalization_manifest_sha256"),
        code="SPEAKER_EVIDENCE_INCOMPLETE",
        label="speaker_finalization_manifest_sha256",
    )
    actual_manifest = _sha256_bytes(speaker_manifest_bytes)
    if actual_manifest != expected_manifest:
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_HASH_MISMATCH",
            "speaker-final manifest bytes differ from record hash",
        )
    manifest = _manifest_document(speaker_manifest_bytes)
    embedded = record.get("speaker_finalization")
    if not isinstance(embedded, Mapping) or dict(embedded) != manifest:
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_EMBEDDED_MISMATCH",
            "standalone speaker-final manifest differs from embedded record manifest",
        )
    if manifest.get("schema_version") != SPEAKER_FINALIZATION_SCHEMA:
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_NOT_READY",
            "speaker-final manifest is not the production-ready schema/state",
        )
    manifest_status = manifest.get("status")
    is_speaker_guess = manifest_status == SPEAKER_GUESS_STATUS
    if is_speaker_guess:
        if manifest.get("production_ready") is not False:
            raise SpeakerEvidenceRejected(
                "SPEAKER_GUESS_RECEIPT_INVALID",
                "speaker guess must never claim production_ready",
            )
        _validate_speaker_guess_receipt(manifest)
    elif manifest_status != "READY" or manifest.get("production_ready") is not True:
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_NOT_READY",
            "speaker-final manifest is not the production-ready schema/state",
        )
    if (
        _bound_digest(
            manifest.get("output_review_srt_sha256"),
            code="SPEAKER_MANIFEST_INVALID",
            label="manifest.output_review_srt_sha256",
        )
        != actual_srt
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_SRT_HASH_MISMATCH",
            "manifest output_review_srt_sha256 differs from exact SRT bytes",
        )

    plain_record_hash = hashes.get("subtitle_sha256")
    plain_manifest_hash = manifest.get("text_final_srt_sha256")
    plain_digest: str | None = None
    if plain_record_hash is not None:
        plain_digest = _bound_digest(
            plain_record_hash,
            code="SPEAKER_EVIDENCE_INCOMPLETE",
            label="artifact_hashes.subtitle_sha256",
        )
    if plain_manifest_hash is not None:
        manifest_plain_digest = _bound_digest(
            plain_manifest_hash,
            code="SPEAKER_MANIFEST_INVALID",
            label="manifest.text_final_srt_sha256",
        )
        if plain_digest is not None and manifest_plain_digest != plain_digest:
            raise SpeakerEvidenceRejected(
                "SPEAKER_PLAIN_SRT_HASH_MISMATCH",
                "record and speaker manifest bind different plain SRT hashes",
            )
        plain_digest = manifest_plain_digest

    cue_rows = _plain_cues(cues)
    segments = _parse_speaker_srt_bytes(speaker_srt_bytes)
    source_count = manifest.get("source_cue_count")
    output_count = manifest.get("output_cue_count")
    if (
        not isinstance(source_count, int)
        or isinstance(source_count, bool)
        or source_count != len(cue_rows)
        or not isinstance(output_count, int)
        or isinstance(output_count, bool)
        or output_count != len(segments)
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_COUNT_MISMATCH",
            "manifest source/output cue counts differ from exact artifacts",
        )
    if is_speaker_guess:
        guess_receipt = manifest["speaker_guess"]
        assert isinstance(guess_receipt, Mapping)
        if guess_receipt.get("source_cue_count") != source_count:
            raise SpeakerEvidenceRejected(
                "SPEAKER_GUESS_RECEIPT_INVALID",
                "speaker guess receipt source cue count differs from manifest",
            )
    raw_decisions = manifest.get("final_decisions")
    if not isinstance(raw_decisions, list) or not all(
        isinstance(row, Mapping) for row in raw_decisions
    ):
        raise SpeakerEvidenceRejected(
            "SPEAKER_MANIFEST_INVALID", "manifest final_decisions must be an object list"
        )
    decisions = [dict(row) for row in raw_decisions]
    alignment, transcript = _alignment_and_transcript(cue_rows, segments, decisions)
    if is_speaker_guess:
        raise SpeakerGuessReviewRequired(
            "hash-bound SPEAKER_GUESS is reserved for human speaker correction"
        )
    alignment_sha256 = _sha256_bytes(_canonical_json_bytes(alignment))
    transcript_sha256 = _sha256_text(transcript)
    evidence: dict[str, object] = {
        "state": SpeakerEvidenceState.PRESENT_VALID.value,
        "policy_ids": {
            "alignment": ALIGNMENT_POLICY_ID,
            "text": TEXT_PARTITION_POLICY_ID,
            "timing": TIMING_POLICY_ID,
        },
        "speaker_final_srt_sha256": "sha256:" + actual_srt,
        "alignment": alignment,
        "alignment_sha256": "sha256:" + alignment_sha256,
        "speaker_transcript": transcript,
        "speaker_transcript_sha256": "sha256:" + transcript_sha256,
    }
    if plain_digest is not None:
        evidence["plain_srt_sha256"] = "sha256:" + plain_digest
    return SpeakerEvidenceResult(
        state=SpeakerEvidenceState.PRESENT_VALID,
        transcript=transcript,
        speaker_evidence=evidence,
        speaker_evidence_sha256="sha256:" + _sha256_bytes(_canonical_json_bytes(evidence)),
    )


def _read_bound_regular_file(path_value: object, *, label: str) -> bytes:
    if not isinstance(path_value, str) or not path_value:
        raise SpeakerEvidenceRejected("SPEAKER_EVIDENCE_INCOMPLETE", f"{label} path is missing")
    path = Path(path_value).absolute()
    try:
        cursor = Path(path.anchor)
        for part in path.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SpeakerEvidenceRejected(
                    "SPEAKER_EVIDENCE_FILE_INVALID",
                    f"{label} path contains a symlink",
                )
        if not path.is_file():
            raise SpeakerEvidenceRejected(
                "SPEAKER_EVIDENCE_FILE_INVALID", f"{label} is not a regular non-symlink file"
            )
        return path.read_bytes()
    except SpeakerEvidenceRejected:
        raise
    except OSError as exc:
        raise SpeakerEvidenceRejected(
            "SPEAKER_EVIDENCE_FILE_INVALID", f"cannot read {label}: {exc}"
        ) from exc


def speaker_evidence_from_record(
    record: Mapping[str, object], cues: Sequence[object]
) -> SpeakerEvidenceResult:
    """Producer filesystem wrapper around :func:`rebuild_speaker_evidence`."""

    if not _has_speaker_claim(record):
        return rebuild_speaker_evidence(record, cues)
    speaker_srt_bytes = _read_bound_regular_file(
        record.get("speaker_review_srt_path"), label="speaker-final SRT"
    )
    speaker_manifest_bytes = _read_bound_regular_file(
        record.get("speaker_finalization_manifest_path"), label="speaker-final manifest"
    )
    return rebuild_speaker_evidence(
        record,
        cues,
        speaker_srt_bytes=speaker_srt_bytes,
        speaker_manifest_bytes=speaker_manifest_bytes,
    )


def speaker_transcript_from_record(
    record: Mapping[str, object], cues: Sequence[object]
) -> str | None:
    """Return a valid transcript or authorized absence; invalid evidence raises."""

    return speaker_evidence_from_record(record, cues).transcript


def build_addressee_transcripts(
    record: Mapping[str, object], cues: Sequence[object]
) -> tuple[str, str | None]:
    """``(final_transcript, speaker_transcript)`` for the source-fact judge.

    ``final_transcript`` is byte-identical to what this call site produced
    before F12 — every persisted receipt binds its sha256.
    """

    plain, evidence = build_addressee_evidence(record, cues)
    return plain, evidence.transcript


def build_addressee_evidence(
    record: Mapping[str, object], cues: Sequence[object]
) -> tuple[str, SpeakerEvidenceResult]:
    """Build the byte-stable plain transcript and read speaker files once."""

    texts = [str(getattr(cue, "text", "")).strip() for cue in cues]
    kept = [text for text in texts if text]
    return "\n".join(kept), speaker_evidence_from_record(record, cues)


def requires_addressee_attribution(*, selection_hook: str, title: str) -> bool:
    """True when the copy carries a person-directed claim the judge must rule on."""

    surface = _compact(selection_hook) + _compact(title)
    return any(marker in surface for marker in _ATTRIBUTION_MARKERS)


_RENDERED_LINE_RE = re.compile(r"(\d+(?:\.\d+)?)\s+\[([^\]]+)\]\s*(.*)\Z")


def _rendered_lines(speaker_transcript: str) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in speaker_transcript.splitlines():
        match = _RENDERED_LINE_RE.fullmatch(line.strip())
        if match is not None:
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def _quoted_evidence(value: str) -> str | None:
    match = re.fullmatch(
        rf"\s*{SPEAKER_TRANSCRIPT_LABEL}\s*:\s*(.+?)\s*",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _compact(match.group(1)) if match is not None else None


def speaker_evidence_row_is_bound(value: str, *, speaker_transcript: str) -> bool:
    """Accept ``speaker_transcript: <quoted line>`` bound to the real transcript."""

    quoted = _quoted_evidence(value)
    return bool(quoted and quoted in _compact(speaker_transcript))


def cited_lines(evidence: Sequence[str], *, speaker_transcript: str) -> list[tuple[str, str, str]]:
    """The transcript lines an evidence list actually quotes."""

    quotes = [quote for quote in (_quoted_evidence(row) for row in evidence) if quote]
    return [
        row
        for row in _rendered_lines(speaker_transcript)
        if any(quote in _compact(f"{row[0]} [{row[1]}] {row[2]}") for quote in quotes)
    ]


def host_not_yet_on_air(evidence: Sequence[str], *, speaker_transcript: str) -> bool:
    """The 受骗片 shape: every cited line is the guest, before the host's first line.

    Deterministic half of the F12 judgment — no model opinion involved.  If a
    host-subject claim is only supported by guest speech that happens *before*
    the host has a single line in this clip, the host cannot be the party the
    words were said to, whatever the judge asserts.
    """

    rows = _rendered_lines(speaker_transcript)
    cited = cited_lines(evidence, speaker_transcript=speaker_transcript)
    if not rows or not cited:
        return False
    host_positions = [position for position, row in enumerate(rows) if row[1] == HOST_SPEAKER]
    first_host = min(host_positions) if host_positions else None
    cited_ids = {identifier for identifier, _speaker, _text in cited}
    return all(
        speaker != HOST_SPEAKER and (first_host is None or position < first_host)
        for position, (identifier, speaker, _text) in enumerate(rows)
        if identifier in cited_ids
    )


@dataclass(frozen=True)
class AddresseeAttributionState:
    valid: bool
    mode: str
    rows: list[dict[str, object]] = field(default_factory=list)
    wrong_addressee: bool = False
    reason_code: str | None = None


def evaluate_addressee_attribution(
    value: object,
    *,
    selection_hook: str,
    title: str,
    speaker_transcript: str | None,
    status: object,
) -> AddresseeAttributionState:
    """Validate the judge's addressee rulings and apply the fail-closed rule."""

    mode = MODE_SPEAKER_LABELLED if speaker_transcript else MODE_UNVERIFIABLE
    if not isinstance(value, list):
        return AddresseeAttributionState(False, mode)
    rows = [dict(row) for row in value if isinstance(row, Mapping)]
    if len(rows) != len(value):
        return AddresseeAttributionState(False, mode)
    if (
        not rows
        and speaker_transcript
        and requires_addressee_attribution(selection_hook=selection_hook, title=title)
    ):
        # Silence is not an answer *when the judge can actually answer*: with a
        # speaker-labelled transcript in hand and a person-directed claim in the
        # copy, ducking the question is the F15 blind-witness hole again.
        # Without labels the verdict is predetermined (UNVERIFIABLE) and the
        # receipt already discloses that through addressee_attribution_mode, so
        # forcing a boilerplate row there would buy nothing.
        return AddresseeAttributionState(False, mode, rows, False, ADDRESSEE_UNRESOLVED_REASON)
    copy_surface = _compact(selection_hook) + "\n" + _compact(title)
    wrong = False
    for row in rows:
        verdict = row.get("verdict")
        assertion = row.get("assertion")
        reason = row.get("reason")
        if (
            verdict not in ADDRESSEE_VERDICTS
            or not isinstance(assertion, str)
            or not _compact(assertion)
            or _compact(assertion) not in copy_surface
            or not isinstance(reason, str)
            or len(reason.strip()) < 4
        ):
            return AddresseeAttributionState(False, mode, rows)
        if verdict == "UNVERIFIABLE":
            continue
        if speaker_transcript is None:
            # No speaker labels means no attribution authority.  A judge that
            # claims either way here is asserting what it cannot see.
            return AddresseeAttributionState(False, mode, rows)
        evidence = row.get("evidence")
        if not (
            isinstance(evidence, list)
            and evidence
            and all(
                isinstance(item, str)
                and speaker_evidence_row_is_bound(item, speaker_transcript=speaker_transcript)
                for item in evidence
            )
        ):
            return AddresseeAttributionState(False, mode, rows)
        if (
            verdict == "SUPPORTED"
            and GUEST_SPEAKER not in str(assertion)
            and host_not_yet_on_air(evidence, speaker_transcript=speaker_transcript)
        ):
            # 受骗片形状的确定性反证：断言的主语是主角（文案没点名连线），可判官
            # 引用的全是主角上场之前的连线发言。这句话不可能是对主角说的，
            # SUPPORTED 无论理由多漂亮都不成立。
            return AddresseeAttributionState(False, mode, rows, False, ADDRESSEE_UNRESOLVED_REASON)
        wrong = wrong or verdict == "WRONG_ADDRESSEE"
    if wrong and status == "KEEP":
        # fail-closed 打回：错归属绝不能以 KEEP 落地，判官必须给出去掉该断言的
        # 完整替代文案（走既有 REPAIR 复审环，理由留在 reason/summary 里）。
        return AddresseeAttributionState(False, mode, rows, True, ADDRESSEE_UNRESOLVED_REASON)
    return AddresseeAttributionState(True, mode, rows, wrong)


def addressee_prompt_block(speaker_transcript: str | None) -> str:
    """The judge-side contract for the attribution ruling."""

    if speaker_transcript:
        authority = (
            "带说话人标签的最终转写（受话人归属的唯一文字 authority；"
            f"标签只有「{HOST_SPEAKER}」和「{GUEST_SPEAKER}」两类，"
            "标识 q.s 表示 plain cue q 的第 s 个人工子段；没有小数点的 q "
            "表示该 cue 只有一个说话人段。标识保持 plain cue 归属，文件顺序保持"
            "真实说话顺序；说话人切换点、称呼语、上场/介绍标记都在里面）:\n"
            + speaker_transcript
            + "\n"
        )
        rule = (
            "逐条检查两份文案里每个「主角被X」「主角对X说Y」「X劝/问/骂/夸主角」"
            "这类**归属断言**：说这句话的是谁、这句话是对谁说的、主角当时是否已经"
            "在场。判定填 SUPPORTED（说话人序列与对话结构支持该归属）、"
            "WRONG_ADDRESSEE（这话确实说过，但说话人或受话人不是文案写的那个——"
            "例如它是连线主持对上一位嘉宾说的，主角在其后才上场）或 UNVERIFIABLE"
            "（标签序列不足以判定）。SUPPORTED 与 WRONG_ADDRESSEE 必须各带至少一条"
            f'逐字证据，格式 "{SPEAKER_TRANSCRIPT_LABEL}: <上面那份转写里的整行>"。\n'
            "只要有任何一条 WRONG_ADDRESSEE，status 必须是 REPAIR，"
            "并给出删掉/改正该错误归属后的完整 final_selection_hook 与 final_title；"
            "带着 WRONG_ADDRESSEE 判 KEEP 一律作废重来。\n"
        )
    else:
        authority = "（本片没有可信的说话人标签转写。）\n"
        rule = (
            "没有说话人标签就没有受话人归属权威：addressee_attribution 里每条的"
            "verdict 只能是 UNVERIFIABLE，不得声称 SUPPORTED 或 WRONG_ADDRESSEE。\n"
        )
    return (
        "受话人归属判项（F12，维护者 2026-08-08 受骗片标题案）：source-fact 复审"
        '此前只验"这话说过没有"，不验"对谁说"，于是把别人挨的话安到主角头上也能'
        "通过。现在必须额外产出 addressee_attribution 数组。\n"
        + authority
        + rule
        + "这里的 selection_hook/title 始终指本轮 prompt 输入、也就是当前待复审表面，"
        "不是本轮新生成的 final_selection_hook/final_title。REPAIR 时每条 "
        "addressee_attribution.assertion 仍只能逐字复制本轮输入的 selection_hook 或 title；"
        "不得把只存在于新 final 文案里的归属断言提前填入本轮 addressee_attribution。"
        "新 final 文案新增或改写的归属内容，本轮只在对应 changed_surfaces 的 "
        "before/after/reason/evidence 中说明并绑定 source evidence；流水线会把新 final 文案"
        "作为下一轮输入，下一轮再对它产出 addressee_attribution 并作 KEEP/REPAIR 裁决。"
        "文案里若确实没有任何归属断言，addressee_attribution 填空数组。\n"
    )
