"""Never split one known proper noun across two adjacent subtitle cues.

Cue boundaries in this pipeline come only from the ASR provider (one
utterance = one cue, see ``scripts/free_asr_client.py`` ``to_srt``).  Every
downstream stage (jingting chunker merge, agy refine prompt, CPA reconcile)
enforces a structural 1:1 cue lock — same cue count, indices, timestamps in
and out.  So if a known term's surface literally straddles a cue boundary
(e.g. ``梦限``/``大`` split across two cues), neither cue contains the full
surface and no later glossary/term normalization pass can ever repair it:
each pass only ever sees one half.

This module is the one place that may move *text* (never timestamps, never
cue count) across an adjacent cue boundary, and only when a known term's
surface is found straddling it.  It runs once, deterministically, right
after the ASR draft is produced — before anything downstream locks the cue
shape in.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Sequence

# How many trailing/leading characters of each cue we look at when deciding
# whether a term surface straddles the boundary between them.  Real observed
# splits (梦限大 across "...梦" | "限大...") are 1-2 characters from the
# boundary, so 6 is generous headroom while keeping the window narrow enough
# that unrelated repeated substrings elsewhere in a cue can't spuriously
# match.  Terms whose surface needs more than WINDOW_CHARS on either side of
# the boundary to be recovered are outside what this pass can fix.
WINDOW_CHARS = 6

_HAS_CONTENT = re.compile(r"\w")


@dataclass(frozen=True)
class TermBoundaryMove:
    """One audit record: a term surface was reassembled across a cue pair."""

    pair: tuple[int, int]
    term: str
    direction: str  # "forward" (A -> B) or "backward" (B -> A)
    moved_text: str
    donor_cue_index: Any
    receiver_cue_index: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "pair": list(self.pair),
            "term": self.term,
            "direction": self.direction,
            "moved_text": self.moved_text,
            "donor_cue_index": self.donor_cue_index,
            "receiver_cue_index": self.receiver_cue_index,
        }


def _has_content(text: str) -> bool:
    """True if ``text`` has at least one non-whitespace, non-punctuation char.

    ``\\w`` matches Unicode word characters, which includes CJK ideographs
    (general category Lo) as well as ASCII letters/digits, so this rejects
    both "" and pure-punctuation remainders like "，" or "——".
    """

    return bool(_HAS_CONTENT.search(text))


def _dedup_terms_longest_first(terms: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for term in terms:
        surface = str(term).strip()
        if len(surface) < 2:
            continue
        seen.setdefault(surface, None)
    # Stable sort: longest surface first; ties keep first-seen order so
    # candidate order stays deterministic run to run.
    return sorted(seen.keys(), key=len, reverse=True)


def _find_straddling_match(boundary: str, boundary_idx: int, term: str) -> tuple[int, int] | None:
    """First occurrence of ``term`` in ``boundary`` that spans ``boundary_idx``."""

    start = 0
    while True:
        found = boundary.find(term, start)
        if found < 0:
            return None
        end = found + len(term)
        if found < boundary_idx < end:
            return found, end
        start = found + 1


def unify_terms_across_cues(
    cues: Sequence[Any], terms: Sequence[str]
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Move text (never timing, never cue count) so no known term is split.

    ``cues`` are parsed SRT entries exposing ``.index``, ``.start_ms``,
    ``.end_ms``, ``.text`` (the repo's ``SrtCue`` from
    ``src.autoslice.jingting_chunker`` satisfies this).  ``terms`` are known
    surface strings (canonical names + aliases/readings); order does not
    matter, longest surfaces are always tried first per boundary.

    Returns ``(cues, moves)`` — a new cue list (same length, same timestamps,
    same cue types) plus a list of JSON-able move records for audit.  Running
    this twice on its own output is a no-op: once a term is whole in one cue,
    no further straddling match exists to move.
    """

    sorted_terms = _dedup_terms_longest_first(terms)
    result = list(cues)
    if not sorted_terms or len(result) < 2:
        return result, []

    moves: list[dict[str, Any]] = []
    for i in range(len(result) - 1):
        a = result[i]
        b = result[i + 1]
        a_text = a.text
        b_text = b.text
        for term in sorted_terms:
            a_stripped = a_text.rstrip()
            b_stripped = b_text.lstrip()
            if not a_stripped or not b_stripped:
                continue
            a_window = a_stripped[-WINDOW_CHARS:]
            b_window = b_stripped[:WINDOW_CHARS]
            boundary = a_window + b_window
            boundary_idx = len(a_window)
            match = _find_straddling_match(boundary, boundary_idx, term)
            if match is None:
                continue
            start, end = match
            a_len = boundary_idx - start
            b_len = end - boundary_idx
            if a_len <= 0 or b_len <= 0:
                continue
            if b_len >= a_len:
                # Majority of the term is in B (or tied -> later cue wins):
                # move A's trailing a_len chars forward into B.
                moved = a_stripped[-a_len:]
                donor_remainder = a_stripped[:-a_len]
                if not _has_content(donor_remainder):
                    continue  # never empty a cue
                a_text = donor_remainder
                b_text = moved + b_stripped
                direction = "forward"
            else:
                # Majority of the term is in A: move B's leading b_len chars
                # backward into A.
                moved = b_stripped[:b_len]
                donor_remainder = b_stripped[b_len:]
                if not _has_content(donor_remainder):
                    continue  # never empty a cue
                b_text = donor_remainder
                a_text = a_stripped + moved
                direction = "backward"
            moves.append(
                TermBoundaryMove(
                    pair=(i, i + 1),
                    term=term,
                    direction=direction,
                    moved_text=moved,
                    donor_cue_index=a.index if direction == "forward" else b.index,
                    receiver_cue_index=b.index if direction == "forward" else a.index,
                ).as_dict()
            )
        if a_text != a.text:
            a = type(a)(index=a.index, start_ms=a.start_ms, end_ms=a.end_ms, text=a_text)
        if b_text != b.text:
            b = type(b)(index=b.index, start_ms=b.start_ms, end_ms=b.end_ms, text=b_text)
        result[i] = a
        result[i + 1] = b

    return result, moves
