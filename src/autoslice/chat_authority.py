"""Structured danmaku/Super Chat evidence and exact read-aloud finalization.

Chat text is untrusted *content*, but when the streamer demonstrably reads it
aloud its wording is a stronger transcription source than ASR.  This module
keeps acquisition, candidate matching, and final-text application deterministic:
an LLM may use the evidence as context, but it cannot be the component that
decides whether the exact source survived into the delivered subtitle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.autoslice.jingting_chunker import parse_srt_cues


_NON_TEXT = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.IGNORECASE)
_SEGMENT_TIME = re.compile(r"(?P<date>20\d{6})[-_](?P<hour>\d{2})[-_](?P<minute>\d{2})[-_](?P<second>\d{2})")
_QUESTION_TAIL = frozenset("吗呢吧嘛呀啊？?")
_CODE_SWITCH_CANONICAL_SURFACES = (
    ("哇哭哇哭", "wakuwaku"),
    ("哇库哇库", "wakuwaku"),
)


@dataclass(frozen=True)
class ChatEvidence:
    kind: str  # danmaku | superchat
    offset_ms: int
    text: str
    sender: str = ""
    source: str = ""
    source_sha256: str = ""
    source_event_id: str = ""

    @property
    def evidence_id(self) -> str:
        raw = (
            f"{self.kind}\0{self.offset_ms}\0{self.sender}\0{self.text}\0{self.source_event_id}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReferentEntity:
    """One canonical entity and every surface that may denote it.

    Surfaces are intentionally separate from canonicals.  For example
    ``母鸡卡`` and ``Mujica`` are two spellings of the same ``Ave Mujica``
    entity, not two competing referents.
    """

    canonical: str
    surfaces: tuple[str, ...]
    readings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferentGroup:
    entities: tuple[ReferentEntity, ...]
    reason: str = ""
    audio_verify_all_surfaces: bool = False


EntityVerifier = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


def normalize_chat_text(text: str) -> str:
    return _NON_TEXT.sub("", str(text)).lower()


_SPEAKER_LABEL = re.compile(r"^\[(?:李豆沙|连线)\]\s*")


def normalize_srt_payload_text(srt_text: str, *, strip_speaker_labels: bool = False) -> str:
    """Normalize only subtitle payload, never SRT indices/timestamps."""

    return normalize_chat_text(
        "".join(
            _SPEAKER_LABEL.sub("", cue.text) if strip_speaker_labels else cue.text
            for cue in parse_srt_cues(srt_text)
            if cue.text.strip()
        )
    )


def normalize_srt_payload_window(
    srt_text: str,
    *,
    start_ms: int,
    end_ms: int,
    strip_speaker_labels: bool = False,
    tolerance_ms: int = 250,
) -> str:
    """Normalize payload only from cues overlapping one bound evidence span."""

    texts = []
    for cue in parse_srt_cues(srt_text):
        if cue.end_ms < start_ms - tolerance_ms or cue.start_ms > end_ms + tolerance_ms:
            continue
        text = _SPEAKER_LABEL.sub("", cue.text) if strip_speaker_labels else cue.text
        if text.strip():
            texts.append(text)
    return normalize_chat_text("".join(texts))


def normalize_code_switch_surfaces(srt_text: str) -> tuple[str, dict[str, Any]]:
    """Canonicalize narrow phonetic spellings of known Japanese insertions."""

    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    repairs: list[dict[str, Any]] = []
    for offset, before in enumerate(list(texts)):
        after = before
        replaced: list[dict[str, str]] = []
        for surface, canonical in _CODE_SWITCH_CANONICAL_SURFACES:
            if surface not in after:
                continue
            after = after.replace(surface, canonical)
            replaced.append({"surface": surface, "canonical": canonical})
        if after == before:
            continue
        texts[offset] = after
        repairs.append(
            {
                "cue_index": offset + 1,
                "matched_start_ms": cues[offset].start_ms,
                "matched_end_ms": cues[offset].end_ms,
                "before": before,
                "after": after,
                "replacements": replaced,
                "authority": "lidousha-code-switch-canon.v1",
            }
        )
    output = _render_srt(cues, texts) if repairs else srt_text
    return output, {
        "schema_version": "code-switch-surface-audit.v1",
        "status": "APPLIED" if repairs else "NO_CHANGE",
        "repairs": repairs,
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }


def sanitize_chat_display_text(text: str, *, max_chars: int = 500) -> str:
    """Render-safe chat data; never treat source text as prompt instructions."""

    value = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", value)
    value = value.replace("-->", "→").replace("{", "｛").replace("}", "｝")
    return value[:max_chars].strip()


def load_referent_groups(path: str | Path) -> list[ReferentGroup]:
    """Load canonical/surface-aware mutually-confusable entity groups."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") not in {
        "lidousha-referent-groups.v1",
        "lidousha-referent-groups.v2",
    }:
        return []
    groups: list[ReferentGroup] = []
    for row in payload.get("groups") or []:
        entities = row.get("entities") if isinstance(row, dict) else None
        if not isinstance(entities, list):
            continue
        parsed: list[ReferentEntity] = []
        for value in entities:
            if isinstance(value, str):
                canonical = sanitize_chat_display_text(value, max_chars=80)
                if canonical:
                    parsed.append(ReferentEntity(canonical, (canonical,)))
                continue
            if not isinstance(value, dict):
                continue
            canonical = sanitize_chat_display_text(value.get("canonical", ""), max_chars=80)
            raw_surfaces = value.get("surfaces") or []
            raw_readings = value.get("readings") or []
            if not canonical or not isinstance(raw_surfaces, list) or not isinstance(raw_readings, list):
                continue
            surfaces = [canonical]
            surfaces.extend(
                sanitize_chat_display_text(surface, max_chars=80)
                for surface in raw_surfaces
            )
            surfaces = list(dict.fromkeys(surface for surface in surfaces if surface))
            readings = tuple(
                dict.fromkeys(
                    sanitize_chat_display_text(reading, max_chars=120)
                    for reading in raw_readings
                    if sanitize_chat_display_text(reading, max_chars=120)
                )
            )
            parsed.append(ReferentEntity(canonical, tuple(surfaces), readings))
        canonicals = {entity.canonical.lower() for entity in parsed}
        if len(parsed) >= 2 and len(canonicals) == len(parsed):
            groups.append(
                ReferentGroup(
                    tuple(parsed),
                    sanitize_chat_display_text(row.get("reason", ""), max_chars=500),
                    row.get("audio_verify_all_surfaces") is True,
                )
            )
    return groups


def _coerce_referent_groups(
    groups: Sequence[ReferentGroup | Sequence[str]],
) -> list[ReferentGroup]:
    out: list[ReferentGroup] = []
    for raw_group in groups:
        if isinstance(raw_group, ReferentGroup):
            out.append(raw_group)
            continue
        parsed = tuple(
            ReferentEntity(str(value), (str(value),))
            for value in raw_group
            if str(value)
        )
        if len(parsed) >= 2:
            out.append(ReferentGroup(parsed))
    return out


def _entity_occurrences(text: str, group: ReferentGroup) -> list[dict[str, Any]]:
    """Find longest non-overlapping surfaces and retain canonical identity."""

    lowered = str(text).lower()
    occupied: set[int] = set()
    found: list[dict[str, Any]] = []
    candidates = [
        (surface, entity.canonical)
        for entity in group.entities
        for surface in entity.surfaces
        if surface
    ]
    for surface, canonical in sorted(candidates, key=lambda pair: len(pair[0]), reverse=True):
        for match in re.finditer(re.escape(surface.lower()), lowered):
            indexes = set(range(match.start(), match.end()))
            if indexes & occupied:
                continue
            occupied.update(indexes)
            found.append(
                {
                    "canonical": canonical,
                    "surface": text[match.start() : match.end()],
                    "start": match.start(),
                    "end": match.end(),
                }
            )
    return sorted(found, key=lambda row: row["start"])


def _request_sha256(request: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in request.items() if key != "request_sha256"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _valid_sha256(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", str(value or "")))


def build_human_text_entity_verifier(
    document_path: str | Path,
    *,
    candidate_id: str,
) -> EntityVerifier:
    """Build a deferred verifier from one hash-bound Ivan text decision asset.

    The decision is allowed to suppress a conflicting exact-chat proposal now,
    but it does not become complete authority until
    :func:`reconcile_pending_text_overrides` proves the declared source and
    final SRT hashes after the normal text-override stage.
    """

    source = Path(document_path)
    raw = source.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("text override schema_version must be 1")
    if payload.get("candidate_id") != candidate_id:
        raise ValueError("text override candidate_id mismatch")
    source_hash = str(payload.get("source_srt_sha256") or "")
    final_hash = str(payload.get("text_final_srt_sha256") or "")
    if not _valid_sha256(source_hash) or not _valid_sha256(final_hash):
        raise ValueError("text override must bind source and final SRT SHA256")
    rows = payload.get("chat_entity_verdicts") or []
    if not isinstance(rows, list):
        raise ValueError("chat_entity_verdicts must be a list")
    by_evidence: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise ValueError("chat_entity_verdict row must be an object")
        evidence_id = str(raw_row.get("evidence_id") or "")
        canonical = sanitize_chat_display_text(raw_row.get("canonical_entity", ""), max_chars=80)
        if not _valid_sha256(evidence_id) or not canonical or evidence_id in by_evidence:
            raise ValueError("invalid or duplicate chat entity verdict")
        by_evidence[evidence_id] = {**raw_row, "canonical_entity": canonical}
    document_hash = hashlib.sha256(raw).hexdigest()

    def verifier(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        row = by_evidence.get(str(request.get("evidence_id") or ""))
        if row is None:
            return None
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request.get("request_sha256"),
            "status": "RESOLVED",
            "canonical_entity": row["canonical_entity"],
            "authority_kind": "ivan_text_override",
            "defer_to_text_override": True,
            "candidate_id": candidate_id,
            "override_document_sha256": document_hash,
            "source_srt_sha256": source_hash,
            "text_final_srt_sha256": final_hash,
            "authority": str(row.get("authority") or "Ivan direct correction"),
        }

    return verifier


def _srt_clock_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value)
    if match is None:
        raise ValueError(f"invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def reconcile_pending_text_overrides(
    audit: dict[str, Any],
    text_manifest: Mapping[str, Any] | None,
    *,
    delivery_start_ms: int,
) -> bool:
    """Close deferred human entity verdicts against the actual override result."""

    pending = audit.get("pending_text_overrides") or []
    if not pending:
        return True
    if not isinstance(text_manifest, Mapping) or text_manifest.get("status") != "READY":
        return False
    decisions = text_manifest.get("decisions") or []
    if not isinstance(decisions, list):
        return False
    document_hash = str(text_manifest.get("override_document_sha256") or "")
    source_hash = str(text_manifest.get("source_srt_sha256") or "")
    final_hash = str(text_manifest.get("output_srt_sha256") or "")
    rebound_document: dict[str, Any] | None = None

    def rebind_deferred_verdict(
        pending_row: Mapping[str, Any], verdict: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Move an unchanged Ivan entity verdict onto a newly frozen ASR variant.

        A generative text pass may drift between attempts even though the
        evidence id and Ivan's entity decision do not.  Rebinding is allowed
        only through the exact decision document that produced this READY
        text manifest; the entity itself may not change.
        """

        nonlocal rebound_document
        if (
            verdict.get("authority_kind") != "ivan_text_override"
            or verdict.get("defer_to_text_override") is not True
            or not _valid_sha256(document_hash)
            or not _valid_sha256(source_hash)
            or not _valid_sha256(final_hash)
        ):
            return None
        document_value = text_manifest.get("override_document")
        if not isinstance(document_value, str) or not document_value:
            return None
        document_path = Path(document_value)
        try:
            raw = document_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != document_hash:
                return None
            if rebound_document is None:
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return None
                rebound_document = payload
        except (OSError, ValueError):
            return None
        document = rebound_document
        if (
            document.get("schema_version") != 1
            or document.get("candidate_id") != verdict.get("candidate_id")
            or document.get("source_srt_sha256") != source_hash
            or document.get("text_final_srt_sha256") != final_hash
        ):
            return None
        evidence_id = str(pending_row.get("evidence_id") or "")
        rows = [
            row
            for row in document.get("chat_entity_verdicts") or []
            if isinstance(row, dict) and str(row.get("evidence_id") or "") == evidence_id
        ]
        if (
            len(rows) != 1
            or rows[0].get("canonical_entity") != verdict.get("canonical_entity")
        ):
            return None
        return {
            **verdict,
            "override_document_sha256": document_hash,
            "source_srt_sha256": source_hash,
            "text_final_srt_sha256": final_hash,
            "authority": str(rows[0].get("authority") or verdict.get("authority") or ""),
        }

    repaired: list[dict[str, Any]] = []
    used_decisions: set[int] = set()
    for pending_row in pending:
        verdict = dict(pending_row.get("verdict") or {})
        if (
            verdict.get("override_document_sha256") != document_hash
            or verdict.get("source_srt_sha256") != source_hash
            or verdict.get("text_final_srt_sha256") != final_hash
        ):
            rebound = rebind_deferred_verdict(pending_row, verdict)
            if rebound is None:
                return False
            verdict_events = audit.get("entity_verdicts")
            if isinstance(verdict_events, list) and verdict_events:
                event_matches = [
                    event
                    for event in verdict_events
                    if isinstance(event, dict)
                    and event.get("evidence_id") == pending_row.get("evidence_id")
                    and isinstance(event.get("verdict"), dict)
                    and event["verdict"].get("request_sha256")
                    == verdict.get("request_sha256")
                    and event["verdict"].get("canonical_entity")
                    == verdict.get("canonical_entity")
                ]
                if len(event_matches) != 1:
                    return False
                event_matches[0]["verdict"] = dict(rebound)
            pending_row["verdict_rebinding"] = {
                "status": "UNCHANGED_ENTITY_REBOUND_TO_FROZEN_SOURCE",
                "previous_override_document_sha256": verdict.get(
                    "override_document_sha256"
                ),
                "previous_source_srt_sha256": verdict.get("source_srt_sha256"),
                "previous_text_final_srt_sha256": verdict.get(
                    "text_final_srt_sha256"
                ),
                "override_document_sha256": document_hash,
                "source_srt_sha256": source_hash,
                "text_final_srt_sha256": final_hash,
            }
            pending_row["verdict"] = rebound
            verdict = rebound
        evidence_id = str(pending_row.get("evidence_id") or "")
        match_index = next(
            (
                index
                for index, decision in enumerate(decisions)
                if index not in used_decisions
                and isinstance(decision, dict)
                and decision.get("supersedes_chat_evidence_id") == evidence_id
            ),
            None,
        )
        if match_index is None:
            return False
        decision = decisions[match_index]
        used_decisions.add(match_index)
        source_cue = decision.get("source") or {}
        before = str(source_cue.get("text") or "")
        after = str(decision.get("output_text") or "")
        expected = str(verdict.get("canonical_entity") or "")
        request = pending_row.get("request") or {}
        entities = request.get("candidate_entities") or []
        decision_group = ReferentGroup(
            tuple(
                ReferentEntity(
                    str(entity.get("canonical") or ""),
                    tuple(str(surface) for surface in entity.get("surfaces") or [] if str(surface)),
                    tuple(str(reading) for reading in entity.get("readings") or [] if str(reading)),
                )
                for entity in entities
                if isinstance(entity, dict) and str(entity.get("canonical") or "")
            )
        )
        occurrences = _entity_occurrences(before, decision_group)
        minimal = (
            len(occurrences) == 1
            and re.sub(
                re.escape(str(occurrences[0]["surface"])),
                expected,
                before,
                count=1,
                flags=re.IGNORECASE,
            )
            == after
        )
        # A read-chat cue can legitimately combine two independent authorities:
        # the exact platform text owns the sentence scaffold, while Ivan's
        # hash-bound listening verdict owns only the confusable entity slot.
        # Example: ASR ``是Mujica的风险`` + danmaku ``有母鸡卡的风险``
        # becomes ``有梦限大的风险``.  This is not an arbitrary whole-cue
        # override: replacing the decided entity in the output with the exact
        # chat surface must reconstruct a contiguous substring of the platform
        # message, and every surface must belong to the same declared group.
        after_occurrences = _entity_occurrences(after, decision_group)
        exact_chat = str(pending_row.get("exact_text") or "")
        chat_occurrences = _entity_occurrences(exact_chat, decision_group)
        chat_scaffold = False
        structured_expectation = ""
        if (
            len(occurrences) == 1
            and len(after_occurrences) == 1
            and after_occurrences[0]["canonical"] == expected
            and len(chat_occurrences) == 1
            and chat_occurrences[0]["canonical"] != expected
        ):
            after_entity = after_occurrences[0]
            projected = (
                after[: int(after_entity["start"])]
                + str(chat_occurrences[0]["surface"])
                + after[int(after_entity["end"]) :]
            )
            projected_normalized = normalize_chat_text(projected)
            chat_scaffold = bool(
                projected_normalized
                and projected_normalized in normalize_chat_text(exact_chat)
            )
            if chat_scaffold:
                chat_entity = chat_occurrences[0]
                structured_expectation = (
                    exact_chat[: int(chat_entity["start"])]
                    + expected
                    + exact_chat[int(chat_entity["end"]) :]
                )
        if not (minimal or chat_scaffold):
            return False
        relative_start = _srt_clock_ms(str(source_cue.get("start") or ""))
        relative_end = _srt_clock_ms(str(source_cue.get("end") or ""))
        absolute_start = delivery_start_ms + relative_start
        absolute_end = delivery_start_ms + relative_end
        if (
            absolute_end < int(pending_row["matched_start_ms"]) - 500
            or absolute_start > int(pending_row["matched_end_ms"]) + 500
        ):
            return False
        pending_row["reconciliation_status"] = "APPLIED_AND_HASH_VERIFIED"
        pending_row["text_override_decision_index"] = match_index
        verification_start = absolute_start
        verification_end = absolute_end
        if chat_scaffold:
            # The exact platform message may span adjacent subtitle cues.  The
            # full read window, not only the cue whose entity slot changed,
            # owns the final grammar scaffold (including edge particles such
            # as ``吗`` and prefixes such as ``还没看``).
            verification_start = int(pending_row["matched_start_ms"])
            verification_end = int(pending_row["matched_end_ms"])
        repaired.append(
            {
                "evidence_id": evidence_id,
                "mode": (
                    "entity_only_human_text_override"
                    if minimal
                    else "chat_scaffold_plus_human_entity_override"
                ),
                "expected_entity": expected,
                "cue_indexes": [int(source_cue.get("source_index") or 0)],
                "matched_start_ms": verification_start,
                "matched_end_ms": verification_end,
                "override_cue_start_ms": absolute_start,
                "override_cue_end_ms": absolute_end,
                "before": [before],
                "after": [after],
                "structured_exact_text": structured_expectation or None,
                "verdict": verdict,
                "text_override_document_sha256": document_hash,
                "survived": True,
            }
        )
    audit.setdefault("entity_repairs", []).extend(repaired)
    audit["pending_text_override_reconciliation"] = {
        "status": "APPLIED_AND_HASH_VERIFIED",
        "override_document_sha256": document_hash,
        "source_srt_sha256": source_hash,
        "final_srt_sha256": final_hash,
        "repaired_count": len(repaired),
    }
    audit["status"] = "APPLIED_AND_VERIFIED"
    return True


def _validated_entity_verdict(
    verdict: Mapping[str, Any] | None,
    *,
    request: Mapping[str, Any],
    group: ReferentGroup,
) -> dict[str, Any] | None:
    if not isinstance(verdict, Mapping):
        return None
    row = dict(verdict)
    if row.get("schema_version") != "chat-entity-verdict.v1":
        return None
    if row.get("request_sha256") != request.get("request_sha256"):
        return None
    if row.get("status") not in {"RESOLVED", "UNCERTAIN"}:
        return None
    if row.get("status") == "UNCERTAIN":
        return row
    canonicals = {entity.canonical for entity in group.entities}
    if row.get("canonical_entity") not in canonicals:
        return None
    authority_kind = row.get("authority_kind")
    if authority_kind == "audio_forced_choice":
        required_hashes = (
            "source_media_sha256",
            "audio_clip_sha256",
            "prompt_sha256",
            "response_sha256",
        )
        if not all(_valid_sha256(row.get(key)) for key in required_hashes):
            return None
        confidence = row.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or confidence < 0.80:
            return {**row, "status": "UNCERTAIN", "reason_code": "ENTITY_AUDIO_CONFIDENCE_LOW"}
    elif authority_kind == "ivan_text_override":
        if not row.get("defer_to_text_override"):
            return None
        if not all(
            _valid_sha256(row.get(key))
            for key in (
                "override_document_sha256",
                "source_srt_sha256",
                "text_final_srt_sha256",
            )
        ):
            return None
        if not str(row.get("candidate_id") or ""):
            return None
    else:
        return None
    return row


def recording_start_epoch_ms(path: str | Path, *, timezone: str = "Asia/Shanghai") -> int | None:
    """Derive recording t=0 from blrec metadata, then its segment filename.

    The old implementation used the earliest event as t=0, shifting every
    message when the event log began after recording.  ``RecordStartTime`` is
    the recorder authority.  Its filenames use the stream's China wall clock,
    regardless of the Mac/operator timezone; filename parsing is only fallback.
    """

    source = Path(path)
    meta_path = source.with_suffix(".meta.json")
    if meta_path.is_file():
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            description = payload.get("description") if isinstance(payload, dict) else None
            value = description.get("RecordStartTime") if isinstance(description, dict) else None
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is not None:
                return int(parsed.timestamp() * 1000)
        except (OSError, TypeError, ValueError):
            pass
    match = _SEGMENT_TIME.search(source.stem)
    if match is None:
        return None
    value = match.group("date") + match.group("hour") + match.group("minute") + match.group("second")
    parsed = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(timezone))
    return int(parsed.timestamp() * 1000)


def _event_epoch_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # blrec deployments have emitted both epoch seconds (current production)
    # and epoch milliseconds (older fixtures/exports).
    if value >= 100_000_000_000:
        return int(value)
    if value >= 100_000_000:
        return int(float(value) * 1000)
    return int(value)


def _send_time_ms(payload: dict, command: str = "") -> int | None:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    # blrec's top-level send_time is an ingestion timestamp on current files,
    # not the live-event timestamp.  Bilibili carries the authoritative epoch
    # in DANMU_MSG info[0][4] and SC data.ts/start_time.
    if command.startswith("DANMU_MSG"):
        info = payload.get("info") or data.get("info")
        if (
            isinstance(info, list)
            and info
            and isinstance(info[0], list)
            and len(info[0]) > 4
        ):
            event_ms = _event_epoch_ms(info[0][4])
            if event_ms is not None:
                return event_ms
    if command.startswith("SUPER_CHAT_MESSAGE"):
        top_level = payload.get("send_time")
        # CN SC events retain millisecond precision here.  JPN twins commonly
        # omit it and fall back to second-precision data.ts/start_time.
        if isinstance(top_level, (int, float)) and top_level >= 100_000_000_000:
            return _event_epoch_ms(top_level)
        for key in ("ts", "start_time", "send_time"):
            event_ms = _event_epoch_ms(data.get(key))
            if event_ms is not None:
                return event_ms
    value = payload.get("send_time")
    if not isinstance(value, (int, float)):
        value = data.get("send_time")
    return _event_epoch_ms(value)


def _danmaku_text(payload: dict) -> str:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    info = payload.get("info") or data.get("info")
    if isinstance(info, list) and len(info) > 1 and isinstance(info[1], str):
        return info[1].strip()
    for key in ("message", "text", "content"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def load_chat_jsonl(
    path: str | Path,
    *,
    recording_start_ms: int | None = None,
) -> list[ChatEvidence]:
    """Load exact DANMU_MSG and SUPER_CHAT text from a blrec JSONL sidecar."""

    source = Path(path)
    if not source.is_file():
        return []
    parsed: list[tuple[int, str, str, str, str, bool]] = []
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    earliest: int | None = None
    for raw_line in source.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        command = str(payload.get("cmd") or "")
        event_ms = _send_time_ms(payload, command)
        if event_ms is None:
            continue
        earliest = event_ms if earliest is None else min(earliest, event_ms)
        if command.startswith("DANMU_MSG"):
            text = _danmaku_text(payload)
            if text:
                parsed.append((event_ms, "danmaku", "", text, "", False))
        elif command.startswith("SUPER_CHAT_MESSAGE"):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            text = data.get("message")
            user_info = data.get("user_info") if isinstance(data.get("user_info"), dict) else {}
            sender = user_info.get("uname") or ""
            if isinstance(text, str) and text.strip():
                event_id = data.get("id") or payload.get("msg_id") or ""
                precise = (
                    isinstance(payload.get("send_time"), (int, float))
                    and payload["send_time"] >= 100_000_000_000
                )
                parsed.append(
                    (
                        event_ms,
                        "superchat",
                        str(sender).strip(),
                        text.strip(),
                        str(event_id),
                        precise,
                    )
                )
    base = recording_start_ms if recording_start_ms is not None else earliest
    if base is None:
        return []
    # Deduplicate before materialization so a second-precision localized row
    # that sorts first can be replaced by the later precise CN twin. Distinct
    # nonempty event ids prove two real same-text SCs and are never collapsed.
    deduped: list[tuple[int, str, str, str, str, bool]] = []
    for row in sorted(parsed):
        event_ms, kind, sender, text, event_id, precise = row
        twin_index = None
        if kind == "superchat":
            for index in range(len(deduped) - 1, -1, -1):
                prior_ms, prior_kind, prior_sender, prior_text, prior_id, prior_precise = deduped[index]
                if event_ms - prior_ms > 2_000:
                    break
                sender_compatible = prior_sender == sender or not prior_sender or not sender
                if prior_kind != kind or not sender_compatible or prior_text != text:
                    continue
                if prior_id and event_id and prior_id != event_id:
                    continue
                twin_index = index
                if (precise and not prior_precise) or (not prior_sender and bool(sender)):
                    deduped[index] = row
                break
        if twin_index is None:
            deduped.append(row)

    out: list[ChatEvidence] = []
    seen: set[tuple[str, str, str, int]] = set()
    for event_ms, kind, sender, text, event_id, _precise in sorted(deduped):
        key = (kind, sender, text, event_ms)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ChatEvidence(
                kind,
                event_ms - base,
                text,
                sender,
                str(source),
                source_sha256,
                event_id,
            )
        )
    return out


def _srt_timestamp(ms: int) -> str:
    hours, rem = divmod(max(0, int(ms)), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _render_srt(cues: Sequence[object], texts: Sequence[str]) -> str:
    blocks = []
    for index, (cue, text) in enumerate(zip(cues, texts), start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start_ms)} --> {_srt_timestamp(cue.end_ms)}\n{text.strip()}"
        )
    return "\n\n".join(blocks) + "\n"


def _match_metrics(authority: str, candidate: str) -> tuple[float, float, float, float, int]:
    left, right = normalize_chat_text(authority), normalize_chat_text(candidate)
    if not left or not right:
        return 0.0, 0.0, 0.0, 0.0, 0
    matcher = SequenceMatcher(None, left, right)
    common = sum(block.size for block in matcher.get_matching_blocks())
    coverage = common / len(left)
    precision = common / len(right)
    ratio = matcher.ratio()
    score = 0.50 * ratio + 0.35 * coverage + 0.15 * precision
    return score, ratio, coverage, precision, common


def _best_text_split(authority: str, cue_texts: Sequence[str]) -> list[str]:
    """Split exact source text across an already-authoritative cue timeline."""

    if len(cue_texts) <= 1:
        return [authority]
    length = len(authority)

    @lru_cache(maxsize=None)
    def solve(part: int, start: int) -> tuple[float, tuple[str, ...]]:
        remaining = len(cue_texts) - part
        if remaining == 1:
            segment = authority[start:]
            return SequenceMatcher(None, normalize_chat_text(segment), normalize_chat_text(cue_texts[part])).ratio(), (segment,)
        best = (-1.0, tuple())
        for end in range(start + 1, length - remaining + 2):
            segment = authority[start:end]
            local = SequenceMatcher(None, normalize_chat_text(segment), normalize_chat_text(cue_texts[part])).ratio()
            tail_score, tail = solve(part + 1, end)
            candidate = (local + tail_score, (segment, *tail))
            if candidate[0] > best[0]:
                best = candidate
        return best

    return list(solve(0, 0)[1])


def _partial_question_patch(authority: str, candidate: str) -> str | None:
    """Restore a dropped final particle without deleting a same-cue reply."""

    left = normalize_chat_text(authority)
    right = normalize_chat_text(candidate)
    if len(left) < 4 or not left or left[-1] not in _QUESTION_TAIL:
        return None
    prefix = 0
    for expected, actual in zip(left, right):
        if expected != actual:
            break
        prefix += 1
    if prefix < 3 or prefix != len(left) - 1 or right.startswith(left):
        return None
    # Find the equivalent normalized prefix in the original candidate.  This
    # keeps punctuation and the reply that ASR merged into the same cue.
    consumed = 0
    cut = 0
    for cut, char in enumerate(candidate, start=1):
        if normalize_chat_text(char):
            consumed += 1
        if consumed >= prefix:
            break
    suffix = candidate[cut:].lstrip("，,。！？!? ")
    return authority + (f"，{suffix}" if suffix else "")


def _matched_read_prefix(
    authority: str,
    candidate: str,
    *,
    cue_boundaries: Iterable[int] = (),
) -> tuple[str, str] | None:
    """Split a fuzzy read prefix from an acoustic same-cue reply.

    Returning ``None`` means there is no defensible suffix boundary.  This is
    preferable to deleting a reply merely because the combined cue happened to
    clear a fuzzy whole-cue similarity threshold.
    """

    authority_norm = normalize_chat_text(authority)
    candidate_norm = normalize_chat_text(candidate)
    if len(candidate_norm) <= len(authority_norm) + 1:
        return None
    best: tuple[float, str, str] | None = None
    structural_cuts = set(cue_boundaries)
    punctuation = "，,。！？!?；;：:"
    for cut in range(1, len(candidate)):
        # A fuzzy maximum inside a word is not a defensible read/reply
        # boundary.  In particular, `唱不同的，我...` used to be cut after
        # `唱不`, leaving the fabricated suffix `同的，我...` behind after the
        # exact SC body was restored.  Preserve a suffix only at an original
        # cue edge or a visible clause boundary.
        structural = (
            cut in structural_cuts
            or candidate[cut - 1] in punctuation
            or candidate[cut] in punctuation
        )
        prefix, suffix = candidate[:cut], candidate[cut:].lstrip("，,。！？!? ")
        prefix_extent = len(normalize_chat_text(prefix)) / max(1, len(authority_norm))
        score, _ratio, coverage, precision, common = _match_metrics(authority, prefix)
        if not structural and not (
            prefix_extent >= 0.88
            and coverage >= 0.72
            and precision >= 0.75
        ):
            # A punctuation-free ASR merge can still have a defensible split
            # when the prefix covers almost the entire message.  This admits
            # `...恋死我自己...` after the near-complete read, but rejects the
            # old mid-word `...唱不|同的...` cut.
            continue
        if len(normalize_chat_text(suffix)) < 2:
            continue
        if (
            score < 0.68
            or coverage < 0.60
            or precision < 0.52
            or common < min(6, len(authority_norm))
        ):
            continue
        candidate_row = (score, prefix, suffix)
        if best is None or candidate_row[0] > best[0]:
            best = candidate_row
    return (best[1], best[2]) if best is not None else None


def _authority_tail_continues_in_next_cue(
    authority: str,
    candidate: str,
    next_cue: str,
) -> bool:
    """Detect an exact message tail split across the next subtitle cue.

    A nearly complete first cue must not win merely because it clears fuzzy
    thresholds: replacing it with the full message would duplicate the last
    particle/character that already opens the following cue.
    """

    authority_norm = normalize_chat_text(authority)
    candidate_norm = normalize_chat_text(candidate)
    next_norm = normalize_chat_text(next_cue)
    if not candidate_norm or not authority_norm.startswith(candidate_norm):
        return False
    tail = authority_norm[len(candidate_norm) :]
    return bool(tail and next_norm.startswith(tail))


def _spoken_sender_alias(sender: str) -> str:
    cjk_prefix = re.match(r"[\u3400-\u9fff]+", sender)
    return cjk_prefix.group(0) if cjk_prefix else sender.strip()


_THANK_NAME = re.compile(
    r"(?P<prefix>(?:谢谢|感谢|谢)(?:一下)?)"
    r"(?P<name>[^，。！？!?\s]{1,24}?)"
    r"(?P<suffix>送的|的\s*SC|的醒目留言)",
    re.IGNORECASE,
)


def _repair_sc_sender(text: str, sender: str) -> str | None:
    alias = _spoken_sender_alias(sender)
    if not alias:
        return None
    match = _THANK_NAME.search(text)
    if match is None or alias.lower() == match.group("name").lower():
        return None
    return text[: match.start("name")] + alias + text[match.end("name") :]


def apply_audio_entity_verification(
    srt_text: str,
    *,
    referent_groups: Sequence[ReferentGroup | Sequence[str]],
    entity_verifier: EntityVerifier | None,
    excluded_cue_indexes: Iterable[int] = (),
) -> tuple[str, dict[str, Any]]:
    """Independently arbitrate confusable entities not owned by exact chat.

    This closes the ordinary-speech path (for example ``立希`` vs ``Saki``),
    where there may be no danmaku at all.  Only one narrow entity surface can be
    changed; all surrounding words/timing remain untouched.
    """

    groups = _coerce_referent_groups(referent_groups)
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    excluded = {int(value) for value in excluded_cue_indexes}
    confirmed: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    required: list[dict[str, Any]] = []
    for cue_offset, cue in enumerate(cues):
        cue_index = cue_offset + 1
        if cue_index in excluded:
            continue
        matches: list[tuple[ReferentGroup, list[dict[str, Any]]]] = []
        for group in groups:
            occurrences = _entity_occurrences(texts[cue_offset], group)
            if not occurrences:
                continue
            suspicious = group.audio_verify_all_surfaces or any(
                str(row["surface"]).lower() != str(row["canonical"]).lower()
                for row in occurrences
            )
            if suspicious:
                matches.append((group, occurrences))
        if not matches:
            continue
        if len(matches) != 1 or len(matches[0][1]) != 1:
            required.append(
                {
                    "cue_index": cue_index,
                    "matched_start_ms": cue.start_ms,
                    "matched_end_ms": cue.end_ms,
                    "reason_code": "TRANSCRIPT_ENTITY_SLOT_AMBIGUOUS",
                }
            )
            continue
        group, occurrences = matches[0]
        occurrence = occurrences[0]
        evidence_id = hashlib.sha256(
            (
                f"transcript-entity\0{hashlib.sha256(srt_text.encode()).hexdigest()}\0"
                f"{cue_index}\0{cue.start_ms}\0{cue.end_ms}\0{occurrence['canonical']}"
            ).encode("utf-8")
        ).hexdigest()
        request: dict[str, Any] = {
            "schema_version": "transcript-entity-verification-request.v1",
            "evidence_id": evidence_id,
            "kind": "transcript_entity",
            "cue_indexes": [cue_index],
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "matched_audio_text": texts[cue_offset],
            "transcript_canonical": occurrence["canonical"],
            "transcript_surface": occurrence["surface"],
            "candidate_entities": [
                {
                    "canonical": entity.canonical,
                    "surfaces": list(entity.surfaces),
                    "readings": list(entity.readings),
                }
                for entity in group.entities
            ],
            "reason": group.reason,
        }
        request["request_sha256"] = _request_sha256(request)
        try:
            raw_verdict = entity_verifier(request) if entity_verifier is not None else None
        except Exception as exc:
            raw_verdict = {
                "schema_version": "chat-entity-verdict.v1",
                "request_sha256": request["request_sha256"],
                "status": "UNCERTAIN",
                "reason_code": "ENTITY_VERIFIER_ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }
        verdict = _validated_entity_verdict(raw_verdict, request=request, group=group)
        if verdict is None or verdict.get("status") != "RESOLVED":
            required.append(
                {
                    "cue_index": cue_index,
                    "matched_start_ms": cue.start_ms,
                    "matched_end_ms": cue.end_ms,
                    "request": request,
                    "verdict": verdict or raw_verdict,
                    "reason_code": str((verdict or {}).get("reason_code") or "ENTITY_VERDICT_REQUIRED"),
                }
            )
            continue
        resolved = str(verdict["canonical_entity"])
        base_row = {
            "evidence_id": evidence_id,
            "cue_indexes": [cue_index],
            "matched_start_ms": cue.start_ms,
            "matched_end_ms": cue.end_ms,
            "transcript_canonical": occurrence["canonical"],
            "transcript_surface": occurrence["surface"],
            "resolved_canonical": resolved,
            "verdict": verdict,
        }
        if resolved == occurrence["canonical"]:
            confirmed.append(base_row)
            continue
        before = texts[cue_offset]
        after = re.sub(
            re.escape(str(occurrence["surface"])),
            resolved,
            before,
            count=1,
            flags=re.IGNORECASE,
        )
        if before == after:
            required.append({**base_row, "reason_code": "ENTITY_SLOT_NOT_FOUND_FOR_MINIMAL_REPAIR"})
            continue
        texts[cue_offset] = after
        repairs.append(
            {
                **base_row,
                "mode": "transcript_entity_only",
                "expected_entity": resolved,
                "before": [before],
                "after": [after],
                "survived": resolved.lower() in after.lower(),
            }
        )
    output = _render_srt(cues, texts) if repairs else srt_text
    return output, {
        "schema_version": "transcript-entity-audit.v1",
        "status": "ENTITY_VERDICT_REQUIRED" if required else "APPLIED_AND_VERIFIED" if repairs else "VERIFIED" if confirmed else "NO_ENTITY",
        "input_srt_sha256": hashlib.sha256(srt_text.encode()).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "confirmed": confirmed,
        "repairs": repairs,
        "entity_verdict_required": required,
    }


def apply_authoritative_chat_evidence(
    srt_text: str,
    evidence: Iterable[ChatEvidence],
    *,
    max_cues: int = 4,
    support_srt_texts: Sequence[str] = (),
    referent_groups: Sequence[ReferentGroup | Sequence[str]] = (),
    entity_verifier: EntityVerifier | None = None,
) -> tuple[str, dict]:
    """Apply only high-confidence exact read-aloud spans to an SRT.

    The match combines source time and character-sequence similarity.  It does
    not treat nearby chat as a command and never asks an LLM to execute it.
    Replies remain untouched, except for the narrow case where ASR merged a
    read question missing only its final particle with the reply in one cue.
    """

    # Normalize every caller, not only JSONL ingestion: XML, fixtures, and
    # future adapters must not be able to inject SRT blocks/control sequences
    # into the output even when their spoken words genuinely match the audio.
    evidence = [
        ChatEvidence(
            item.kind,
            item.offset_ms,
            sanitize_chat_display_text(item.text),
            sanitize_chat_display_text(item.sender, max_chars=100),
            item.source,
            item.source_sha256,
            item.source_event_id,
        )
        for item in evidence
        if sanitize_chat_display_text(item.text)
    ]
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    texts = [cue.text for cue in cues]
    entity_groups = _coerce_referent_groups(referent_groups)
    proposals: list[dict] = []
    entity_verdicts: list[dict] = []
    entity_verdict_required: list[dict] = []
    pending_text_overrides: list[dict] = []
    superseded_chat_proposals: list[dict] = []
    for item in evidence:
        authority_norm = normalize_chat_text(item.text)
        if len(authority_norm) < 4:
            continue
        best: dict | None = None
        for start in range(len(cues)):
            if item.kind == "danmaku":
                if item.offset_ms >= 0:
                    delay = cues[start].start_ms - item.offset_ms
                    if delay < -2_000 or delay > 90_000:
                        continue
                elif cues[start].start_ms > 90_000:
                    continue
            elif item.offset_ms >= 0 and cues[start].start_ms < item.offset_ms - 2_000:
                continue
            for count in range(1, min(max_cues, len(cues) - start) + 1):
                candidate_parts = texts[start : start + count]
                candidate = "".join(candidate_parts)
                cue_boundaries: set[int] = set()
                cursor = 0
                for part in candidate_parts[:-1]:
                    cursor += len(part)
                    cue_boundaries.add(cursor)
                score, ratio, coverage, precision, common = _match_metrics(item.text, candidate)
                preserved_suffix = _matched_read_prefix(
                    item.text,
                    candidate,
                    cue_boundaries=cue_boundaries,
                )
                if preserved_suffix is not None:
                    score, ratio, coverage, precision, common = _match_metrics(
                        item.text, preserved_suffix[0]
                    )
                matched_candidate = preserved_suffix[0] if preserved_suffix is not None else candidate
                extent = len(normalize_chat_text(matched_candidate)) / max(1, len(authority_norm))
                full = (
                    len(authority_norm) >= 4
                    and score >= 0.68
                    and coverage >= 0.60
                    and precision >= 0.52
                    and extent >= 0.82
                    and common >= min(6, len(authority_norm))
                )
                if (
                    full
                    and len(normalize_chat_text(candidate)) > len(authority_norm) + 1
                    and preserved_suffix is None
                ):
                    full = False
                partial = None
                if count == 1 and item.kind == "danmaku":
                    near = item.offset_ms < 0 or cues[start].start_ms - item.offset_ms <= 20_000
                    partial = _partial_question_patch(item.text, candidate) if near else None
                if not full and partial is None:
                    continue
                if (
                    full
                    and start + count < len(cues)
                    and _authority_tail_continues_in_next_cue(
                        item.text,
                        candidate,
                        texts[start + count],
                    )
                ):
                    continue
                proposal = {
                    "evidence": item,
                    "start": start,
                    "count": count,
                    "score": score,
                    "ratio": ratio,
                    "coverage": coverage,
                    "precision": precision,
                    "common_chars": common,
                    "mode": "exact_span" if full else "question_particle_patch",
                    "replacement": partial,
                    "preserved_suffix": preserved_suffix,
                }
                # The first viable cue span is safest: adding a later cue can
                # improve fuzzy score merely by consuming the beginning of an
                # acoustic reply.  A truly multi-cue read will not meet the
                # coverage threshold until enough read cues are present.
                if best is None or (count, -score) < (best["count"], -best["score"]):
                    best = proposal
        if best is not None:
            acoustic_span = "".join(
                texts[best["start"] : best["start"] + best["count"]]
            )
            support_scores: list[float] = []
            for support_text in support_srt_texts:
                support_cues = [cue for cue in parse_srt_cues(support_text) if cue.text.strip()]
                best_support = 0.0
                for support_start in range(len(support_cues)):
                    if item.kind == "danmaku" and item.offset_ms >= 0:
                        delay = support_cues[support_start].start_ms - item.offset_ms
                        if delay < -2_000 or delay > 90_000:
                            continue
                    for support_count in range(1, min(max_cues, len(support_cues) - support_start) + 1):
                        support_candidate = "".join(
                            cue.text for cue in support_cues[support_start : support_start + support_count]
                        )
                        support_score, _ratio, support_coverage, _precision, support_common = _match_metrics(
                            item.text, support_candidate
                        )
                        support_extent = len(normalize_chat_text(support_candidate)) / max(1, len(authority_norm))
                        # SCs are commonly paraphrased by the streamer and
                        # degraded by ASR (唱不到 -> 唱不同的).  The concrete
                        # platform event, long near-full acoustic span and
                        # common-character floor still bind the read; keeping
                        # the danmaku threshold stricter avoids turning a short
                        # coincidental overlap into an exact ordinary-chat read.
                        support_coverage_floor = 0.65 if item.kind == "superchat" else 0.72
                        if (
                            support_score >= 0.62
                            and support_coverage >= support_coverage_floor
                            and support_extent >= 0.82
                            and support_common >= min(6, len(authority_norm))
                        ):
                            best_support = max(best_support, support_score)
                        elif (
                            support_count == 1
                            and item.kind == "danmaku"
                            and _partial_question_patch(item.text, support_candidate) is not None
                        ):
                            best_support = max(best_support, 0.62)
                if best_support:
                    support_scores.append(best_support)
            best["support_scores"] = support_scores

            matched_groups: list[tuple[ReferentGroup, dict[str, Any]]] = []
            for group in entity_groups:
                occurrences = _entity_occurrences(item.text, group)
                canonicals = {row["canonical"] for row in occurrences}
                if occurrences:
                    matched_groups.append(
                        (
                            group,
                            {
                                "occurrences": occurrences,
                                "canonicals": sorted(canonicals),
                            },
                        )
                    )

            if matched_groups:
                cue_indexes = [
                    index + 1
                    for index in range(best["start"], best["start"] + best["count"])
                ]
                base_row = {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "source": item.source,
                    "source_sha256": item.source_sha256,
                    "source_event_id": item.source_event_id,
                    "source_offset_ms": item.offset_ms,
                    "exact_text": item.text,
                    "cue_indexes": cue_indexes,
                    "matched_start_ms": cues[best["start"]].start_ms,
                    "matched_end_ms": cues[best["start"] + best["count"] - 1].end_ms,
                    "matched_audio_text": acoustic_span,
                }
                if (
                    len(matched_groups) != 1
                    or len(matched_groups[0][1]["canonicals"]) != 1
                    or len(matched_groups[0][1]["occurrences"]) != 1
                ):
                    entity_verdict_required.append(
                        {
                            **base_row,
                            "reason_code": "ENTITY_VERDICT_AMBIGUOUS_CHAT_ENTITY",
                        }
                    )
                    continue
                group, chat_match = matched_groups[0]
                chat_canonical = chat_match["canonicals"][0]
                chat_surface = chat_match["occurrences"][0]["surface"]
                request: dict[str, Any] = {
                    "schema_version": "chat-entity-verification-request.v1",
                    **base_row,
                    "structured_chat_canonical": chat_canonical,
                    "structured_chat_surface": chat_surface,
                    "candidate_entities": [
                        {
                            "canonical": entity.canonical,
                            "surfaces": list(entity.surfaces),
                            "readings": list(entity.readings),
                        }
                        for entity in group.entities
                    ],
                    "reason": group.reason,
                }
                request["request_sha256"] = _request_sha256(request)
                try:
                    raw_verdict = entity_verifier(request) if entity_verifier is not None else None
                except Exception as exc:  # verifier failure is evidence, never permission
                    raw_verdict = {
                        "schema_version": "chat-entity-verdict.v1",
                        "request_sha256": request["request_sha256"],
                        "status": "UNCERTAIN",
                        "reason_code": "ENTITY_VERIFIER_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                verdict = _validated_entity_verdict(raw_verdict, request=request, group=group)
                if verdict is None or verdict.get("status") != "RESOLVED":
                    entity_verdict_required.append(
                        {
                            **base_row,
                            "request": request,
                            "verdict": verdict or raw_verdict,
                            "reason_code": (
                                str((verdict or {}).get("reason_code") or "ENTITY_VERDICT_REQUIRED")
                            ),
                        }
                    )
                    continue
                resolved = {**base_row, "request": request, "verdict": verdict}
                entity_verdicts.append(resolved)
                if verdict["authority_kind"] == "ivan_text_override":
                    pending_text_overrides.append(resolved)
                    superseded_chat_proposals.append(
                        {
                            **base_row,
                            "reason_code": "EXACT_CHAT_SUPERSEDED_BY_IVAN_TEXT_OVERRIDE",
                            "structured_chat_canonical": chat_canonical,
                            "resolved_canonical": verdict["canonical_entity"],
                        }
                    )
                    continue
                best["entity_group"] = group
                best["entity_verdict"] = verdict
                best["structured_chat_canonical"] = chat_canonical
                if verdict["canonical_entity"] != chat_canonical:
                    best["mode"] = "entity_only"
                    superseded_chat_proposals.append(
                        {
                            **base_row,
                            "reason_code": "EXACT_CHAT_REJECTED_BY_AUDIO_ENTITY_VERDICT",
                            "structured_chat_canonical": chat_canonical,
                            "resolved_canonical": verdict["canonical_entity"],
                        }
                    )
                proposals.append(best)
                continue

            # For ordinary (non-confusable) wording, at least one pre-CPA
            # audio-derived transcript must support the read.  Confusable
            # entities instead require the direct verifier path above; a
            # chat-conditioned ASR/AGY agreement can never self-authorize them.
            if support_scores:
                proposals.append(best)

    proposals.sort(key=lambda row: (row["score"], -row["count"]), reverse=True)
    occupied: set[int] = set()
    applied: list[dict] = []
    applied_proposals: list[dict] = []
    entity_repairs: list[dict] = []
    coreference_anchors: list[dict] = []
    for proposal in proposals:
        indexes = set(range(proposal["start"], proposal["start"] + proposal["count"]))
        if indexes & occupied:
            continue
        item = proposal["evidence"]
        before = texts[proposal["start"] : proposal["start"] + proposal["count"]]
        if proposal["mode"] == "entity_only":
            group: ReferentGroup = proposal["entity_group"]
            expected_canonical = str(proposal["entity_verdict"]["canonical_entity"])
            source_occurrences = _entity_occurrences("".join(before), group)
            if len(source_occurrences) != 1:
                entity_verdict_required.append(
                    {
                        "evidence_id": item.evidence_id,
                        "exact_text": item.text,
                        "cue_indexes": [index + 1 for index in sorted(indexes)],
                        "matched_start_ms": cues[proposal["start"]].start_ms,
                        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                        "matched_audio_text": "".join(before),
                        "reason_code": "ENTITY_SLOT_AMBIGUOUS_FOR_MINIMAL_REPAIR",
                        "verdict": proposal["entity_verdict"],
                    }
                )
                continue
            candidates = sorted(
                (
                    (surface, entity.canonical)
                    for entity in group.entities
                    for surface in entity.surfaces
                    if surface
                ),
                key=lambda pair: len(pair[0]),
                reverse=True,
            )
            replacements = list(before)
            replaced_surface: str | None = None
            replaced_canonical: str | None = None
            for local_index, before_text in enumerate(before):
                match_row = next(
                    (
                        (surface, canonical)
                        for surface, canonical in candidates
                        if re.search(re.escape(surface), before_text, flags=re.IGNORECASE)
                    ),
                    None,
                )
                if match_row is None:
                    continue
                replaced_surface, replaced_canonical = match_row
                replacements[local_index] = re.sub(
                    re.escape(replaced_surface),
                    expected_canonical,
                    before_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                break
            if replaced_surface is None:
                entity_verdict_required.append(
                    {
                        "evidence_id": item.evidence_id,
                        "exact_text": item.text,
                        "cue_indexes": [index + 1 for index in sorted(indexes)],
                        "matched_start_ms": cues[proposal["start"]].start_ms,
                        "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                        "matched_audio_text": "".join(before),
                        "reason_code": "ENTITY_SLOT_NOT_FOUND_FOR_MINIMAL_REPAIR",
                        "verdict": proposal["entity_verdict"],
                    }
                )
                continue
            texts[proposal["start"] : proposal["start"] + proposal["count"]] = replacements
            occupied.update(indexes)
            repair_row = {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "source": item.source,
                "source_sha256": item.source_sha256,
                "source_event_id": item.source_event_id,
                "source_offset_ms": item.offset_ms,
                "cue_indexes": [index + 1 for index in sorted(indexes)],
                "matched_start_ms": cues[proposal["start"]].start_ms,
                "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                "mode": "entity_only",
                "expected_entity": expected_canonical,
                "replaced_entity": replaced_canonical,
                "replaced_surface": replaced_surface,
                "before": before,
                "after": replacements,
                "verdict": proposal["entity_verdict"],
            }
            entity_repairs.append(repair_row)
            coreference_anchors.append(
                {
                    "proposal": proposal,
                    "group": group,
                    "expected_canonical": expected_canonical,
                    "expected_surface": expected_canonical,
                }
            )
            continue
        if proposal["mode"] == "question_particle_patch":
            replacements = [proposal["replacement"]]
        else:
            replacements = _best_text_split(item.text, before)
            if proposal.get("preserved_suffix") is not None:
                replacements[-1] = replacements[-1].rstrip("，,。！？!? ") + "，" + proposal["preserved_suffix"][1]
        texts[proposal["start"] : proposal["start"] + proposal["count"]] = replacements
        occupied.update(indexes)
        applied_proposals.append(proposal)
        if proposal.get("entity_group") is not None:
            coreference_anchors.append(
                {
                    "proposal": proposal,
                    "group": proposal["entity_group"],
                    "expected_canonical": proposal["structured_chat_canonical"],
                    "expected_surface": next(
                        row["surface"]
                        for row in _entity_occurrences(item.text, proposal["entity_group"])
                        if row["canonical"] == proposal["structured_chat_canonical"]
                    ),
                }
            )
        applied.append(
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "sender": item.sender,
                "source": item.source,
                "source_sha256": item.source_sha256,
                "source_event_id": item.source_event_id,
                "source_offset_ms": item.offset_ms,
                "exact_text": item.text,
                "cue_indexes": [index + 1 for index in sorted(indexes)],
                "matched_start_ms": cues[proposal["start"]].start_ms,
                "matched_end_ms": cues[proposal["start"] + proposal["count"] - 1].end_ms,
                "mode": proposal["mode"],
                "score": round(proposal["score"], 4),
                "coverage": round(proposal["coverage"], 4),
                "audio_transcript_support_count": len(proposal.get("support_scores") or []),
                "audio_transcript_support_scores": [round(score, 4) for score in proposal.get("support_scores") or []],
                "alignment_basis": (
                    "raw-audio-forced-choice.v1"
                    if proposal.get("entity_verdict") is not None
                    else "audio-derived-transcript-proxy.v1"
                ),
                "entity_verdict": proposal.get("entity_verdict"),
                "before": before,
                "after": replacements,
            }
        )

    # A matched SC body identifies the specific sender.  If the immediately
    # preceding acoustic cue has an explicit thank-name grammar, repair only
    # that name slot; nearby SCs without a matched body cannot trigger this.
    sender_repairs: list[dict] = []
    sender_verdict_required: list[dict] = []
    for proposal, applied_row in zip(applied_proposals, applied, strict=True):
        item = proposal["evidence"]
        if item.kind != "superchat" or not item.sender:
            continue
        repair_candidate: tuple[int, str] | None = None
        for index in range(proposal["start"] - 1, max(-1, proposal["start"] - 3), -1):
            if index < 0 or cues[proposal["start"]].start_ms - cues[index].end_ms > 10_000:
                break
            repaired = _repair_sc_sender(texts[index], item.sender)
            if repaired is not None:
                repair_candidate = (index, repaired)
                break
        if repair_candidate is None:
            continue
        index, repaired = repair_candidate
        matched_cue_start = cues[proposal["start"]].start_ms
        same_body_events = [
            other
            for other in evidence
            if other.kind == "superchat"
            and normalize_chat_text(other.text) == normalize_chat_text(item.text)
            and (other.offset_ms < 0 or matched_cue_start >= other.offset_ms - 2_000)
        ]
        event_identities = {
            str(other.source_event_id or other.evidence_id) for other in same_body_events
        }
        spoken_senders = {
            _spoken_sender_alias(other.sender).lower()
            for other in same_body_events
            if _spoken_sender_alias(other.sender)
        }
        if len(event_identities) > 1 and len(spoken_senders) > 1:
            sender_verdict_required.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_event_id": item.source_event_id,
                    "thank_cue_index": index + 1,
                    "cue_indexes": [
                        index + 1
                        for index in range(
                            proposal["start"], proposal["start"] + proposal["count"]
                        )
                    ],
                    "matched_start_ms": cues[proposal["start"]].start_ms,
                    "matched_end_ms": cues[
                        proposal["start"] + proposal["count"] - 1
                    ].end_ms,
                    "reason_code": "DUPLICATE_SC_BODY_SENDER_AMBIGUOUS",
                    "candidate_event_ids": sorted(event_identities),
                    "candidate_spoken_senders": sorted(spoken_senders),
                }
            )
            continue
        # The exact matched SC body identifies one concrete platform event.
        # Its sender field is therefore direct structured authority for the
        # narrow thank-name slot immediately before that body; requiring a
        # chat-conditioned ASR to have already spelled the same username was
        # circular and left 十麻乃-class errors unrepaired.
        before_text = texts[index]
        texts[index] = repaired
        sender_repairs.append(
            {
                "evidence_id": item.evidence_id,
                "source_event_id": item.source_event_id,
                "sender": item.sender,
                "spoken_sender": _spoken_sender_alias(item.sender),
                "cue_index": index + 1,
                "matched_start_ms": cues[index].start_ms,
                "matched_end_ms": cues[index].end_ms,
                "before": before_text,
                "after": repaired,
                "alignment_basis": "matched-superchat-body-plus-platform-sender.v1",
            }
        )

    # Immediate replies inherit the entity slot of the exact message unless
    # the speaker explicitly contrasts/switches entities.  Confusable groups
    # are data, not hard-coded model guesses.
    coreference_repairs: list[dict] = []
    contrast_markers = ("不是", "而是", "还是", "或者", "对比", "相比")
    for anchor in coreference_anchors:
        proposal = anchor["proposal"]
        item = proposal["evidence"]
        group: ReferentGroup = anchor["group"]
        expected_canonical = anchor["expected_canonical"]
        expected = anchor["expected_surface"]
        alternatives = sorted(
            (
                surface
                for entity in group.entities
                if entity.canonical != expected_canonical
                for surface in entity.surfaces
            ),
            key=len,
            reverse=True,
        )
        if not alternatives:
            continue
        prior_end = cues[proposal["start"] + proposal["count"] - 1].end_ms
        for index in range(proposal["start"] + proposal["count"], min(len(cues), proposal["start"] + proposal["count"] + 2)):
            if index in occupied or cues[index].start_ms - prior_end > 8_000:
                break
            before_text = texts[index]
            expected_entity = next(
                entity for entity in group.entities if entity.canonical == expected_canonical
            )
            if (
                any(surface.lower() in before_text.lower() for surface in expected_entity.surfaces)
                or any(marker in before_text for marker in contrast_markers)
            ):
                break
            found = next((alt for alt in alternatives if alt.lower() in before_text.lower()), None)
            if found is None:
                break
            after_text = re.sub(re.escape(found), expected, before_text, flags=re.IGNORECASE)
            texts[index] = after_text
            coreference_repairs.append(
                {
                    "evidence_id": item.evidence_id,
                    "expected_entity": expected,
                    "expected_canonical_entity": expected_canonical,
                    "replaced_confusable": found,
                    "cue_index": index + 1,
                    "matched_start_ms": cues[index].start_ms,
                    "matched_end_ms": cues[index].end_ms,
                    "before": before_text,
                    "after": after_text,
                }
            )
            break

    output = (
        _render_srt(cues, texts)
        if applied or entity_repairs or sender_repairs or coreference_repairs
        else srt_text
    )
    for row in applied:
        span_text = "".join(texts[index - 1] for index in row["cue_indexes"])
        row["survived"] = normalize_chat_text(row["exact_text"]) in normalize_chat_text(span_text)
    for row in entity_repairs:
        span_text = "".join(texts[index - 1] for index in row["cue_indexes"])
        row["survived"] = normalize_chat_text(row["expected_entity"]) in normalize_chat_text(span_text)
    if sender_verdict_required:
        status = "SC_SENDER_VERDICT_REQUIRED"
    elif entity_verdict_required:
        status = "ENTITY_VERDICT_REQUIRED"
    elif pending_text_overrides:
        status = "PENDING_TEXT_OVERRIDE"
    elif applied and not all(row["survived"] for row in applied):
        status = "FAILED"
    elif entity_repairs and not all(row["survived"] for row in entity_repairs):
        status = "FAILED"
    elif applied or entity_repairs:
        status = "APPLIED_AND_VERIFIED"
    else:
        status = "NO_MATCH"
    audit = {
        "schema_version": "chat-authority-audit.v2",
        "status": status,
        "evidence_considered": len(evidence),
        "input_srt_sha256": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "applied": applied,
        "entity_verdicts": entity_verdicts,
        "entity_verdict_required": entity_verdict_required,
        "pending_text_overrides": pending_text_overrides,
        "superseded_chat_proposals": superseded_chat_proposals,
        "entity_repairs": entity_repairs,
        "sender_repairs": sender_repairs,
        "sender_verdict_required": sender_verdict_required,
        "coreference_repairs": coreference_repairs,
    }
    return output, audit
