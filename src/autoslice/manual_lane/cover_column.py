"""Step 6 cover: which text layout a cover takes, and the rows of a column.

Owner every cover had one line top left and one line bottom right; the layout should change from cover to
cover (the words all in a column on the left, or all on the right).  The skill renderer has the layouts; the lane used
one.  Here the lane takes turns: a cover takes the first layout of lane_rules `cover.text_layouts` that the last
`text_layout_recent_covers` covers did not use.  A column needs the words broken into short rows at word boundaries:
the cover brief (one model call, which reads the lines anyway) proposes the rows, and `rows_of` keeps them only when
they are the cover lines, letter for letter, within the row lengths the column has room for.  Whether the column then
fits beside the person is the media-host tool's to measure (cover_layout.Column); what does not fit stays on top.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TOP = "top"
COLUMNS = ("left", "right")


def _letters(text: object) -> str:
    return "".join(ch for ch in str(text) if ch.isalnum())


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
        if _letters("".join(part)) != _letters(text) or any(len(row) > most for row in part):
            return None
    return rows


def next_layout(recent: Sequence[str | None], rules: Mapping[str, Any]) -> tuple[str, str]:
    """(layout, why) for the next cover: the first of `text_layouts` the recent covers did not use; when they used them
    all, the one used longest ago."""
    order = [str(x) for x in rules["text_layouts"]]
    used = [x for x in recent if x in order][-int(rules["text_layout_recent_covers"]):]
    fresh = [x for x in order if x not in used]
    pick = fresh[0] if fresh else min(order, key=lambda x: max(i for i, u in enumerate(used) if u == x))
    return pick, f"最近 {len(used)} 张用过 {'、'.join(used) or '无'}，这张轮到 {pick}"
