"""Distinguish delivered content pieces from cross-segment reserve pieces.

A talk candidate's ``spec["pieces"]`` is ordinarily every entry a delivered
content interval on the SAME segment timeline as ``spec["semantic_end_ms"]``.
The boundary-review witness-reserve retry (``talk_lane.py``) may append one
additional piece drawn from the NEXT recording segment purely to satisfy the
review's forward-context requirement when the candidate sits near a segment
boundary. That reserve piece must never be mistaken for the semantic "last
piece" by any of the boundary-math consumers that anchor
``prior_piece_duration_ms`` / ``last_piece_start_ms`` on ``pieces[-1]`` — it
lives on a different source file and its ``start_ms`` is unrelated to
``semantic_end_ms``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

BOUNDARY_WITNESS_RESERVE_ROLE = "boundary_witness_reserve"


def is_reserve_piece(piece: Mapping[str, object]) -> bool:
    """True when ``piece`` is a cross-segment witness-reserve piece."""

    return piece.get("piece_role") == BOUNDARY_WITNESS_RESERVE_ROLE


def last_content_piece_index(pieces: Sequence[Mapping[str, object]]) -> int:
    """Index of the last non-reserve (content) piece.

    A reserve piece is only ever appended after every content piece, so this
    is the true anchor for ``semantic_end_ms`` / ``prior_piece_duration_ms``
    math regardless of whether a reserve piece trails the list.
    """

    for index in range(len(pieces) - 1, -1, -1):
        if not is_reserve_piece(pieces[index]):
            return index
    raise ValueError("PIECES_HAVE_NO_CONTENT_PIECE")


def content_only(
    pieces: Sequence[Mapping[str, object]],
    parallel: Sequence[object],
) -> list[object]:
    """Filter ``parallel`` (e.g. ``durations``) down to content-piece entries.

    ``parallel`` must have one entry per piece in ``pieces``, same order.
    """

    if len(pieces) != len(parallel):
        raise ValueError("PIECES_PARALLEL_LENGTH_MISMATCH")
    return [
        value
        for piece, value in zip(pieces, parallel)
        if not is_reserve_piece(piece)
    ]
