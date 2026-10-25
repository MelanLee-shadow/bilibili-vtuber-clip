"""Release gate: no unresolved subtitle doubt may ship (维护者).

Disclosure-and-release is void. A subtitle finding is resolved only when a
completed listening ruling kept the original text, or SUB-18's
exhausted-audio meaning-equivalent keep applies (the union
``is_release_resolved_keep``). Everything else that sits in an
``unresolved_findings_disclosed`` list -- ``noncritical_uncertainty``, a
meaning-equivalence disclosure, a row carrying an unapplied
``proposed_full_cue`` -- is still an open doubt and blocks the package.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.autoslice.final_review_contract import is_release_resolved_keep

UNRESOLVED_FINDINGS_CODE = "POSTBURN_UNRESOLVED_FINDINGS"

# Record keys that carry a final-review structure with the disclosed list.
_REVIEW_KEYS = (
    "postburn_subtitle_audio_review",
    "exact_final_review",
    "final_review_audit",
    "final_review",
)


def _review_structures(record: object) -> list[tuple[str, Mapping[str, Any]]]:
    if not isinstance(record, Mapping):
        return []
    found: list[tuple[str, Mapping[str, Any]]] = []
    if "unresolved_findings_disclosed" in record:
        found.append(("record", record))
    for key in _REVIEW_KEYS:
        value = record.get(key)
        if isinstance(value, Mapping):
            found.append((key, value))
            nested = value.get("final_review_audit")
            if isinstance(nested, Mapping):
                found.append((f"{key}.final_review_audit", nested))
    return found


def unresolved_disclosed_findings(record: object) -> list[dict[str, Any]]:
    """Every disclosed row that is not a release-resolved keep (``is_release_resolved_keep``)."""

    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for source, review in _review_structures(record):
        disclosed = review.get("unresolved_findings_disclosed")
        if disclosed is None:
            continue
        if not isinstance(disclosed, list):
            rows.append({"source": source, "cue_index": None, "suspect": None,
                         "content_risk": "malformed_disclosed_list"})
            continue
        for row in disclosed:
            if isinstance(row, Mapping) and is_release_resolved_keep(row):
                continue
            item = row if isinstance(row, Mapping) else {}
            key = (item.get("cue_index"), item.get("base_text_sha256"),
                   item.get("suspect"), item.get("proposed_full_cue"))
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "source": source,
                "cue_index": item.get("cue_index"),
                "suspect": item.get("suspect"),
                "content_risk": item.get("content_risk"),
                "proposed_full_cue": item.get("proposed_full_cue"),
                "suggestion": item.get("suggestion"),
            })
    return rows


def unresolved_findings_detail(rows: list[dict[str, Any]]) -> str:
    """Human-readable detail listing cue_index and suspect for each open row."""

    return "; ".join(
        f"cue {row.get('cue_index')}: {row.get('suspect')!r}"
        + (f" -> proposed {row.get('proposed_full_cue')!r} (not applied)"
           if row.get("proposed_full_cue") else "")
        for row in rows
    )


def unresolved_findings_problem(record: object) -> str | None:
    """``None`` when clean; otherwise a single refusal string with the code."""

    rows = unresolved_disclosed_findings(record)
    if not rows:
        return None
    return f"{UNRESOLVED_FINDINGS_CODE}: {len(rows)} unresolved finding(s): " + unresolved_findings_detail(rows)
