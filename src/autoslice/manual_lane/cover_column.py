"""Step 6 cover: the rows of a column of cover words.

The column templates of the channel's composition table (cover_layout_templates.json, cover_layout_rotation; owner
the words all in a column on the left, or all on the right) need the cover lines broken into short rows at
word boundaries: the cover brief (one model call, which reads the lines anyway) proposes the rows, and `rows_of` keeps
them only when they are the cover lines, letter for letter, within the row lengths the column has room for.  Whether a
column then fits beside the person is the media-host tool's to find out (cover_candidates.Run.compose).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from . import text_overlap


def rows_of(lines: Sequence[str], answer: object, rules: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The column rows of these cover lines, or None when the brief's answer is not them: 2 to `column_rows_max` rows;
    the rows of the punchline (the last cover line) are marked and come last; joined, each part is its line letter for
    letter; no row is longer than its part allows (`column_punch_row_max_chars`, `column_context_row_max_chars`)."""
    if not lines or not isinstance(answer, list) or not 2 <= len(answer) <= int(rules["column_rows_max"]):
        return None
    rows = []
    for item in answer:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            return None
        rows.append({"text": item["text"].strip(), "punch": item.get("punch") is True})
    marks = [r["punch"] for r in rows]
    if not marks[-1] or marks != sorted(marks):                    # the punchline's rows are the last ones
        return None
    for punch, text in ((False, "".join(lines[:-1])), (True, lines[-1])):
        part = [r["text"] for r in rows if r["punch"] is punch]
        most = int(rules["column_punch_row_max_chars" if punch else "column_context_row_max_chars"])
        if text_overlap.letters("".join(part)) != text_overlap.letters(text) or any(len(row) > most for row in part):
            return None
    return rows
