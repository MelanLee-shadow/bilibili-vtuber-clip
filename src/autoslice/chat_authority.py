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
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from src.autoslice.jingting_chunker import parse_srt_cues


_NON_TEXT = re.compile(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", re.IGNORECASE)
_SEGMENT_TIME = re.compile(r"(?P<date>20\d{6})[-_](?P<hour>\d{2})[-_](?P<minute>\d{2})[-_](?P<second>\d{2})")
_QUESTION_TAIL = frozenset("吗呢吧嘛呀啊？?")


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


def sanitize_chat_display_text(text: str, *, max_chars: int = 500) -> str:
    """Render-safe chat data; never treat source text as prompt instructions."""

    value = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]", "", value)
    value = value.replace("-->", "→").replace("{", "｛").replace("}", "｝")
    return value[:max_chars].strip()


def load_referent_groups(path: str | Path) -> list[list[str]]:
    """Load explicit mutually-confusable entity groups for reply continuity."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema_version") != "lidousha-referent-groups.v1":
        return []
    groups = []
    for row in payload.get("groups") or []:
        entities = row.get("entities") if isinstance(row, dict) else None
        if isinstance(entities, list):
            clean = [sanitize_chat_display_text(value, max_chars=80) for value in entities]
            clean = [value for value in clean if value]
            if len(clean) >= 2:
                groups.append(clean)
    return groups


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


def _support_has_sender(
    support_srt_texts: Sequence[str],
    *,
    start_ms: int,
    end_ms: int,
    sender: str,
) -> bool:
    alias = _spoken_sender_alias(sender).lower()
    if not alias:
        return False
    for support_text in support_srt_texts:
        for cue in parse_srt_cues(support_text):
            if cue.end_ms < start_ms - 500 or cue.start_ms > end_ms + 500:
                continue
            if alias in cue.text.lower() and _THANK_NAME.search(cue.text):
                return True
    return False


def apply_authoritative_chat_evidence(
    srt_text: str,
    evidence: Iterable[ChatEvidence],
    *,
    max_cues: int = 4,
    support_srt_texts: Sequence[str] = (),
    referent_groups: Sequence[Sequence[str]] = (),
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
    proposals: list[dict] = []
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
                        if (
                            support_score >= 0.62
                            and support_coverage >= 0.72
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
            # A generative final transcript agreeing with chat is not acoustic
            # proof.  Require at least one pre-CPA audio-derived transcript
            # (raw ASR or AGY second-listen) to independently support the span.
            if support_scores:
                best["support_scores"] = support_scores
                proposals.append(best)

    proposals.sort(key=lambda row: (row["score"], -row["count"]), reverse=True)
    occupied: set[int] = set()
    applied: list[dict] = []
    applied_proposals: list[dict] = []
    for proposal in proposals:
        indexes = set(range(proposal["start"], proposal["start"] + proposal["count"]))
        if indexes & occupied:
            continue
        item = proposal["evidence"]
        before = texts[proposal["start"] : proposal["start"] + proposal["count"]]
        if proposal["mode"] == "question_particle_patch":
            replacements = [proposal["replacement"]]
        else:
            replacements = _best_text_split(item.text, before)
            if proposal.get("preserved_suffix") is not None:
                replacements[-1] = replacements[-1].rstrip("，,。！？!? ") + "，" + proposal["preserved_suffix"][1]
        texts[proposal["start"] : proposal["start"] + proposal["count"]] = replacements
        occupied.update(indexes)
        applied_proposals.append(proposal)
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
                "alignment_basis": "audio-derived-transcript-proxy.v1",
                "before": before,
                "after": replacements,
            }
        )

    # A matched SC body identifies the specific sender.  If the immediately
    # preceding acoustic cue has an explicit thank-name grammar, repair only
    # that name slot; nearby SCs without a matched body cannot trigger this.
    sender_repairs: list[dict] = []
    for proposal, applied_row in zip(applied_proposals, applied, strict=True):
        item = proposal["evidence"]
        if item.kind != "superchat" or not item.sender:
            continue
        for index in range(proposal["start"] - 1, max(-1, proposal["start"] - 3), -1):
            if index < 0 or cues[proposal["start"]].start_ms - cues[index].end_ms > 10_000:
                break
            repaired = _repair_sc_sender(texts[index], item.sender)
            if repaired is None or not _support_has_sender(
                support_srt_texts,
                start_ms=cues[index].start_ms,
                end_ms=cues[index].end_ms,
                sender=item.sender,
            ):
                continue
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
                }
            )
            break

    # Immediate replies inherit the entity slot of the exact message unless
    # the speaker explicitly contrasts/switches entities.  Confusable groups
    # are data, not hard-coded model guesses.
    coreference_repairs: list[dict] = []
    contrast_markers = ("不是", "而是", "还是", "或者", "对比", "相比")
    for proposal in applied_proposals:
        item = proposal["evidence"]
        source_lower = item.text.lower()
        for raw_group in referent_groups:
            group = [str(value) for value in raw_group if str(value)]
            present = [value for value in group if value.lower() in source_lower]
            if len(present) != 1:
                continue
            expected = present[0]
            alternatives = sorted(
                (value for value in group if value != expected),
                key=len,
                reverse=True,
            )
            prior_end = cues[proposal["start"] + proposal["count"] - 1].end_ms
            for index in range(proposal["start"] + proposal["count"], min(len(cues), proposal["start"] + proposal["count"] + 2)):
                if index in occupied or cues[index].start_ms - prior_end > 8_000:
                    break
                before_text = texts[index]
                if expected.lower() in before_text.lower() or any(marker in before_text for marker in contrast_markers):
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
                        "replaced_confusable": found,
                        "cue_index": index + 1,
                        "matched_start_ms": cues[index].start_ms,
                        "matched_end_ms": cues[index].end_ms,
                        "before": before_text,
                        "after": after_text,
                    }
                )
                break

    output = _render_srt(cues, texts) if applied or sender_repairs or coreference_repairs else srt_text
    for row in applied:
        span_text = "".join(texts[index - 1] for index in row["cue_indexes"])
        row["survived"] = normalize_chat_text(row["exact_text"]) in normalize_chat_text(span_text)
    audit = {
        "schema_version": "chat-authority-audit.v1",
        "status": "APPLIED_AND_VERIFIED" if applied and all(row["survived"] for row in applied) else "NO_MATCH" if not applied else "FAILED",
        "evidence_considered": len(evidence),
        "input_srt_sha256": hashlib.sha256(srt_text.encode("utf-8")).hexdigest(),
        "output_srt_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        "applied": applied,
        "sender_repairs": sender_repairs,
        "coreference_repairs": coreference_repairs,
    }
    return output, audit
