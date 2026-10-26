"""Span-level wording disagreements between the final SRT and the BCUT witness.

The independent post-burn dictation witness is evidence only.  This module
turns every wording difference that survives deterministic normalization into
a *nomination*: a question for the ordinary exact-final review, which must
hear the audio and choose between the current words and the witness words.
Nothing here edits a subtitle or decides which side is right.

Timebase (same as ``subtitle_audio_correspondence``)::

    witness_ms = final_delivery_ms + intro_offset_ms        (apply once)

Not disagreements (normalized away before comparing):

* punctuation and whitespace;
* stutter repetition (``你，你`` == ``你你`` == ``你``);
* third-person pronouns 他/她/它/牠/祂/TA (another stage owns pronouns);
* channel-fixed spellings: the witness is first passed through the profile's
  surface canon (``surface_canon``), e.g. 直女 -> 侄女.

Every other difference whose toneless pinyin differs yields one nomination.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import unicodedata
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.autoslice.text_scripts import script_profile

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.restatement_recall import pinyin_tokens
from src.autoslice.surface_canon import (
    canonicalize_caption_display_surfaces,
    canonicalize_expected_value_surfaces,
    canonicalize_hard_meme_surfaces,
)

SCHEMA_VERSION = "subtitle-witness-disagreement.v1"
PROVENANCE_KIND = "subtitle_audio_witness_disagreement"
EVIDENCE_SOURCE = "witness"
WHY_PREFIX = "独立听写见证与当前字幕用词不同"

# Same bounds as final_review_auditor (MAX_EDIT_SPAN_CODEPOINTS / DELTA); a
# larger span is nominated without a closed-set proposal.
_MAX_PROPOSAL_SPAN = 24
_MAX_PROPOSAL_DELTA = 8
_MIN_OVERLAP_MS = 120
_EDGE_TOLERANCE_MS = 400
_PRONOUN_CHARS = "他她它牠祂"
_TA_RE = re.compile(r"(?<![A-Za-z])[Tt][Aa](?![A-Za-z])")
_LATIN_RE = re.compile(r"[A-Za-z]")
_REPEAT_RE = re.compile(r"(.{1,3}?)\1+")


@dataclass(frozen=True)
class _Normalized:
    text: str
    origin: tuple[tuple[int, int], ...]  # normalized index -> (start, end) in source


@dataclass(frozen=True)
class NativeSegment:
    """A provider segment on the physical source clock; never a subtitle clock."""
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if not 0 <= self.start_ms < self.end_ms or not self.text.strip():
            raise ValueError("WITNESS_NATIVE_SEGMENT_INVALID")


def segments_srt(segments: tuple[NativeSegment, ...]) -> str:
    def stamp(ms: int) -> str:
        hours, rest = divmod(ms, 3600000)
        minutes, rest = divmod(rest, 60000)
        seconds, millis = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
    return "\n\n".join(f"{i}\n{stamp(s.start_ms)} --> {stamp(s.end_ms)}\n{s.text}"
        for i, s in enumerate(segments, 1)) + "\n"


def _phonetic_normalize(norm: _Normalized, symbols: dict[str, str]) -> _Normalized:
    chars = []
    for char in norm.text:
        tokens = _pinyin(char) if script_profile(char)["han"] else None
        if tokens and len(tokens) == 1:
            token = tokens[0]
            if token not in symbols:
                symbols[token] = chr(0xF0000 + len(symbols))
            chars.append(symbols[token])
        else:
            chars.append(char)
    return _Normalized("".join(chars), norm.origin)


def agreement_key(text: str) -> str:
    """R2 compares normalized words, not two different homophonic guesses."""
    return _normalize(text).text


def reading_key(text: str) -> str:
    """The comparison's own normalized reading, used by the R2 registry gate."""
    normalized = _normalize(text).text
    return " ".join(_pinyin(normalized) or list(normalized))


def native_script_mismatch(
    final_srt_text: str, segments: tuple[NativeSegment, ...],
    expected_scripts: frozenset[str] = frozenset(),
) -> bool:
    """Reject an unsupported script even when no cue aligns to the observation.

    Validated language observations may explicitly allow another script. Han
    characters shared with Japanese cannot make unsupported kana acceptable.
    """
    if not expected_scripts:
        expected_scripts = frozenset(k for k, v in script_profile(final_srt_text).items()
            if v and k != "other")
    return any(count and script not in expected_scripts and script != "other"
        for script, count in script_profile("".join(s.text for s in segments)).items())


def _canon_witness(text: str) -> str:
    for canonicalize in (
        canonicalize_hard_meme_surfaces,
        canonicalize_caption_display_surfaces,
        canonicalize_expected_value_surfaces,
    ):
        text, _ = canonicalize(text)
    return text


def _normalize(text: str) -> _Normalized:
    """Normalize while remembering where each surviving char came from."""

    chars: list[str] = []
    origin: list[tuple[int, int]] = []
    ta_spans = {m.start(): m.end() for m in _TA_RE.finditer(text)}
    index = 0
    while index < len(text):
        if index in ta_spans:  # "TA" is one pronoun; keep source offsets honest
            chars.append("他")
            origin.append((index, ta_spans[index]))
            index = ta_spans[index]
            continue
        for piece in unicodedata.normalize("NFKC", text[index]).casefold():
            if unicodedata.category(piece)[0] in "PSZC":
                continue
            chars.append("他" if piece in _PRONOUN_CHARS else piece)
            origin.append((index, index + 1))
        index += 1
    while True:
        match = _REPEAT_RE.search("".join(chars))
        if match is None:
            break
        drop = range(match.start() + len(match.group(1)), match.end())
        chars = [c for i, c in enumerate(chars) if i not in drop]
        origin = [o for i, o in enumerate(origin) if i not in drop]
    return _Normalized("".join(chars), tuple(origin))


def _pinyin(text: str) -> list[str] | None:
    return pinyin_tokens(text) if text else []


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _components(final_cues: Sequence[Any], witness_cues: Sequence[Any], offset: int):
    """Group final/witness cues connected by real time overlap."""

    links: list[tuple[int, int]] = []
    for fi, fcue in enumerate(final_cues):
        fs, fe = fcue.start_ms + offset, fcue.end_ms + offset
        for wi, wcue in enumerate(witness_cues):
            overlap = min(fe, wcue.end_ms) - max(fs, wcue.start_ms)
            if overlap >= min(_MIN_OVERLAP_MS, fe - fs, wcue.end_ms - wcue.start_ms):
                links.append((fi, wi))
    parent: dict[tuple[str, int], tuple[str, int]] = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for fi, wi in links:
        parent[find(("f", fi))] = find(("w", wi))
    groups: dict[tuple[str, int], tuple[set[int], set[int]]] = {}
    for fi, wi in links:
        root = find(("f", fi))
        groups.setdefault(root, (set(), set()))
        groups[root][0].add(fi)
        groups[root][1].add(wi)
    return [(sorted(f), sorted(w)) for f, w in groups.values()]


def _interp_time(start: int, end: int, position: int, total: int) -> float:
    return start + (end - start) * (position + 0.5) / max(total, 1)


def find_witness_disagreements(
    final_srt_text: str,
    witness_srt_text: str,
    *,
    intro_offset_ms: int,
    native_segments: tuple[NativeSegment, ...] | None = None,
    observation_window_ms: tuple[int, int] | None = None,
    expected_scripts: frozenset[str] = frozenset(),
    include_rejected: bool = False,
) -> list[dict[str, Any]]:
    """Return one nomination per span-level wording disagreement."""

    if type(intro_offset_ms) is not int or intro_offset_ms < 0:
        raise ValueError("WITNESS_DISAGREEMENT_INTRO_OFFSET_INVALID")
    if native_segments is not None:
        if observation_window_ms is None or not 0 <= observation_window_ms[0] < observation_window_ms[1]:
            raise ValueError("WITNESS_OBSERVATION_WINDOW_REQUIRED")
        witness_srt_text = segments_srt(native_segments)
    final_cues = [c for c in parse_srt_cues(final_srt_text) if c.text.strip()]
    witness_cues = [c for c in parse_srt_cues(witness_srt_text) if c.text.strip()]
    witness_texts = [_canon_witness(c.text) for c in witness_cues]
    final_norm = [_normalize(c.text) for c in final_cues]
    witness_norm = [_normalize(t) for t in witness_texts]
    if native_segments is not None:
        # Align at the existing toneless-pinyin equivalence level. This also
        # aligns traditional/simplified forms without a partial lookup table.
        symbols: dict[str, str] = {}
        final_norm = [_phonetic_normalize(n, symbols) for n in final_norm]
        witness_norm = [_phonetic_normalize(n, symbols) for n in witness_norm]
    nominations: list[dict[str, Any]] = []

    for final_ids, witness_ids in _components(final_cues, witness_cues, intro_offset_ms):
        # One concatenated string per side, with per-char owner and clock.
        f_text, f_owner, f_time = "", [], []
        for fi in final_ids:
            cue, norm = final_cues[fi], final_norm[fi]
            for k, char in enumerate(norm.text):
                f_owner.append((fi, k))
                f_time.append(_interp_time(
                    cue.start_ms + intro_offset_ms, cue.end_ms + intro_offset_ms,
                    k, len(norm.text)))
            f_text += norm.text
        w_text, w_owner, w_time = "", [], []
        for wi in witness_ids:
            cue, norm = witness_cues[wi], witness_norm[wi]
            for k, char in enumerate(norm.text):
                w_owner.append((wi, k))
                w_time.append(_interp_time(cue.start_ms, cue.end_ms, k, len(norm.text)))
            w_text += norm.text
        if f_text == w_text or not f_text:
            continue
        window_start = final_cues[final_ids[0]].start_ms + intro_offset_ms - _EDGE_TOLERANCE_MS
        window_end = final_cues[final_ids[-1]].end_ms + intro_offset_ms + _EDGE_TOLERANCE_MS
        matcher = difflib.SequenceMatcher(None, f_text, w_text, autojunk=False)
        for tag, a0, a1, b0, b1 in _merge_close_ops(matcher.get_opcodes()):
            if tag == "equal":
                continue
            if b1 > b0 and not any(window_start <= w_time[i] <= window_end for i in range(b0, b1)):
                # Witness-only speech outside the final cues' time (intro,
                # neighbouring cue of another clip) is not a wording claim.
                if a1 == a0:
                    continue
                b0, b1 = _clip_to_window(w_time, b0, b1, window_start, window_end)
                if b1 <= b0 and tag == "replace":
                    tag = "delete"
            if a1 == a0:
                anchor = min(max(a0 - 1, 0), len(f_owner) - 1) if a0 else 0
                owner_fi, k = f_owner[anchor]
                f_lo = f_hi = k + 1 if a0 else 0
            else:
                owner_fi = f_owner[a0][0]
                # A span crossing a cue boundary is attributed per cue.
                for fi in sorted({f_owner[i][0] for i in range(a0, a1)}):
                    ks = [f_owner[i][1] for i in range(a0, a1) if f_owner[i][0] == fi]
                    nom = _nomination(
                        final_cues[fi], final_norm[fi], fi, min(ks), max(ks) + 1,
                        witness_cues, witness_texts, witness_norm,
                        w_owner[b0:b1], intro_offset_ms)
                    if nom is not None:
                        nominations.append(nom)
                continue
            nom = _nomination(
                final_cues[owner_fi], final_norm[owner_fi], owner_fi, f_lo, f_hi,
                witness_cues, witness_texts, witness_norm,
                w_owner[b0:b1], intro_offset_ms)
            if nom is not None:
                nominations.append(nom)

    nominations.sort(key=lambda n: (n["cue_index"], n["final_span_start_codepoint"]))
    # Several opcodes in one cue stay separate questions; identical ids merge.
    unique: dict[str, dict[str, Any]] = {}
    for nom in nominations:
        unique.setdefault(nom["nomination_id"], nom)
    result = list(unique.values())
    if native_segments is None:
        return result  # BCUT's existing comparison contract is unchanged.
    assert observation_window_ms is not None
    lo, hi = observation_window_ms
    mismatch = native_script_mismatch(final_srt_text, native_segments, expected_scripts)
    for nomination in result:
        cue = final_cues[nomination["cue_index"] - 1]
        start = cue.start_ms + intro_offset_ms
        end = cue.end_ms + intro_offset_ms
        total = max(len(cue.text), 1)
        a = start + (end - start) * nomination["final_span_start_codepoint"] / total
        b = start + (end - start) * max(nomination["final_span_end_codepoint"],
            nomination["final_span_start_codepoint"] + 1) / total
        reason = ("WITNESS_SCRIPT_MISMATCH" if mismatch else
            "NEAR_WINDOW_EDGE" if start < lo or end > hi
            or a - lo < _EDGE_TOLERANCE_MS or hi - b < _EDGE_TOLERANCE_MS else None)
        nomination["reject_reason"] = reason
    return result if include_rejected else [n for n in result if n["reject_reason"] is None]


def _merge_close_ops(opcodes):
    """Fuse differences separated by a single matching char into one span
    (看了不许 / 看来我不去 is one wording question, not two)."""

    merged: list[list] = []
    for op in opcodes:
        tag, a0, a1, b0, b1 = op
        if (
            merged and tag != "equal" and len(merged) >= 2
            and merged[-1][0] == "equal" and merged[-1][2] - merged[-1][1] <= 1
            and merged[-2][0] != "equal"
        ):
            gap = merged.pop()
            prev = merged.pop()
            merged.append(["replace", prev[1], a1, prev[3], b1])
            continue
        merged.append([tag, a0, a1, b0, b1])
    return [tuple(op) for op in merged]


def _clip_to_window(times, b0, b1, lo, hi):
    inside = [i for i in range(b0, b1) if lo <= times[i] <= hi]
    return (inside[0], inside[-1] + 1) if inside else (b0, b0)


def _nomination(
    cue, norm: _Normalized, cue_pos: int, lo: int, hi: int,
    witness_cues, witness_texts, witness_norm, witness_owner, intro_offset_ms: int,
) -> dict[str, Any] | None:
    text = cue.text
    if hi > lo:
        start = norm.origin[lo][0]
        end = norm.origin[hi - 1][1]
    else:  # pure insertion on the witness side
        start = end = norm.origin[lo - 1][1] if lo else 0
    final_span = text[start:end]
    # Witness span in its own (canonicalized) cue text, joined across cues.
    pieces: list[str] = []
    witness_indices: list[int] = []
    for wi in sorted({o[0] for o in witness_owner}):
        ks = [k for w, k in witness_owner if w == wi]
        wnorm = witness_norm[wi]
        s = wnorm.origin[min(ks)][0]
        e = wnorm.origin[max(ks)][1]
        pieces.append(witness_texts[wi][s:e].strip())
        witness_indices.append(wi + 1)
    witness_span = "".join(pieces)
    f_py, w_py = _pinyin(_strip(final_span)), _pinyin(_strip(witness_span))
    if f_py is not None and w_py is not None and f_py == w_py:
        return None
    proposal: str | None = text[:start] + witness_span + text[end:]
    if (
        proposal == text
        or not proposal.strip()
        or end - start > _MAX_PROPOSAL_SPAN
        or abs(len(witness_span) - len(final_span)) > _MAX_PROPOSAL_DELTA
        or _LATIN_RE.search(witness_span)
    ):
        proposal = None
    # Pure insertion/deletion needs the bounded spoken_unit lane; a swap of
    # words is a phonetic closed-set question.
    repair_class = "spoken_unit" if not final_span.strip() or not witness_span.strip() else "phonetic"
    base_sha = _sha(text)
    ident = _sha(json.dumps(
        [base_sha, start, end, final_span, witness_span], ensure_ascii=False))[:16]
    return {
        "schema_version": SCHEMA_VERSION,
        "nomination_id": ident,
        "cue_index": cue_pos + 1,
        "base_text_sha256": base_sha,
        "final_span": final_span,
        "witness_span": witness_span,
        "final_pinyin": f_py,
        "witness_pinyin": w_py,
        "final_span_start_codepoint": start,
        "final_span_end_codepoint": end,
        "proposed_full_cue": proposal,
        "repair_class": repair_class,
        "witness_cue_indices": witness_indices,
        "final_window_ms": [cue.start_ms, cue.end_ms],
        "intro_offset_ms": intro_offset_ms,
        "evidence_source": EVIDENCE_SOURCE,
    }


def _strip(text: str) -> str:
    return "".join(c for c in text if unicodedata.category(c)[0] not in "PSZC")


def _row_suspect(nom: Mapping[str, Any], final_srt_text: str) -> str:
    """A bounded verbatim suspect; a bare insertion uses the adjacent char."""

    if nom["final_span"] or nom["proposed_full_cue"] is not None:
        return nom["final_span"]
    cues = [c for c in parse_srt_cues(final_srt_text) if c.text.strip()]
    text = cues[nom["cue_index"] - 1].text
    pos = nom["final_span_start_codepoint"]
    lo = max(pos - 1, 0)
    return text[lo:lo + 1] or text[:1]


def nomination_digest(nominations: Sequence[Mapping[str, Any]]) -> str:
    return "sha256:" + _sha(json.dumps(
        sorted(n["nomination_id"] for n in nominations), ensure_ascii=False))


def to_priority_findings(
    nominations: Sequence[Mapping[str, Any]], *,
    final_srt_text: str, witness_srt_sha256: str,
) -> list[dict[str, Any]]:
    """Legacy row projection for the discovery registry; never mutation authority.

    The row names both candidates; the exact-final reviewer must hear the audio
    and choose.  ``mutation_authorized`` stays False: the witness is evidence.
    """

    current_sha = "sha256:" + _sha(final_srt_text)
    rows = []
    for nom in nominations:
        rows.append({
            "cue": nom["cue_index"],
            "kind": "context",
            "suspect": _row_suspect(nom, final_srt_text),
            "proposed_full_cue": nom["proposed_full_cue"],
            "repair_class": nom["repair_class"],
            "evidence_cue_ids": [nom["cue_index"]],
            "base_text_sha256": nom["base_text_sha256"],
            "why": (f"{WHY_PREFIX}：当前「{nom['final_span']}」，见证「{nom['witness_span']}」"
                    "；须听音在两者间裁定"),
            "candidate_provenance": {
                "kind": PROVENANCE_KIND,
                "nomination_id": nom["nomination_id"],
                "mutation_authorized": False,
                **({"decision_authority": "NONE_DISCOVERY_ONLY"}
                   if os.environ.get("AUTOSLICE_FINAL_REVIEW_MODE") == "per_finding" else {}),
                "evidence_source": EVIDENCE_SOURCE,
                "current_srt_sha256": current_sha,
                "witness_srt_sha256": witness_srt_sha256,
                "current_span": nom["final_span"],
                "witness_span": nom["witness_span"],
                "final_pinyin": nom["final_pinyin"],
                "witness_pinyin": nom["witness_pinyin"],
                "intro_offset_ms": nom["intro_offset_ms"],
            },
        })
    return rows


def decided_nomination_ids(
    nominations: Sequence[Mapping[str, Any]], resolved_findings: Sequence[Any],
) -> set[str]:
    """Nominations the exact review already decided for the same current bytes.

    ``resolved_findings`` is the exact-final review's decided set (audio-backed
    resolutions and keep-current disclosures) for this very SRT.  A row settles
    a nomination only when it is about the same cue text *and* the same witness
    question (provenance id, same closed-set proposal, or same suspect span);
    an unrelated finding on that cue never settles it.
    """

    decided: set[str] = set()
    for nom in nominations:
        for row in resolved_findings or ():
            if not isinstance(row, Mapping):
                continue
            if row.get("cue_index") != nom["cue_index"]:
                continue
            if row.get("base_text_sha256") != nom["base_text_sha256"]:
                continue
            provenance = row.get("candidate_provenance")
            if (
                (isinstance(provenance, Mapping)
                 and provenance.get("kind") == PROVENANCE_KIND
                 and provenance.get("nomination_id") == nom["nomination_id"])
                or (nom["proposed_full_cue"] is not None
                    and row.get("proposed_full_cue") == nom["proposed_full_cue"])
                or (nom["final_span"] and row.get("suspect") == nom["final_span"]
                    and row.get("why", "").startswith(WHY_PREFIX))
            ):
                decided.add(nom["nomination_id"])
                break
    return decided


def assess_correspondence_text(
    final_srt_text: str, witness_srt_text: str, *, intro_offset_ms: int,
) -> dict[str, Any]:
    """Receipt block recorded next to the timing verdict."""

    nominations = find_witness_disagreements(
        final_srt_text, witness_srt_text, intro_offset_ms=intro_offset_ms)
    return {
        "schema_version": SCHEMA_VERSION,
        "nomination_count": len(nominations),
        "nomination_ids": [n["nomination_id"] for n in nominations],
        "nomination_digest": nomination_digest(nominations),
        "resolution_authority": "exact-final review hears audio; this receipt repairs nothing",
    }
