"""Combine duplicate discovery nominations before any exact-review dispatch.

This is an agenda transformation, never a verdict cache. All evidence and
control fields must agree; only discovery descriptions and cited cue IDs are
combined. In particular a new pending audio gate is not discarded.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.autoslice.jingting_chunker import parse_srt_cues

_PENDING = frozenset({"exact_release_adjudication", "_history_audio_srt_successor"})
_DESCRIPTIVE = frozenset({"why", "kind", "evidence_cue_ids", "input_contract_normalizations"})


def coalesce_exact_review_agenda(
    srt_text: str, findings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    cues = [cue for cue in parse_srt_cues(srt_text) if cue.text.strip()]
    output: list[dict[str, Any]] = []
    groups: dict[str, list[list[dict[str, Any]]]] = {}
    for finding in findings:
        row = copy.deepcopy(dict(finding))
        index = row.get("cue_index")
        # Only whole-current-cue doubts without a proposed mutation can share
        # an agenda item. Different spans, candidates or authorities stay apart.
        eligible = (
            type(index) is int and 1 <= index <= len(cues)
            and row.get("proposed_full_cue") is None
            and not row.get("suggestion")
            and row.get("suspect") == cues[index - 1].text
            and row.get("base_text_sha256") == hashlib.sha256(
                cues[index - 1].text.encode("utf-8")
            ).hexdigest()
            and isinstance(row.get("why"), str)
            and isinstance(row.get("evidence_cue_ids", []), list)
            and all(type(i) is int and 1 <= i <= len(cues)
                    for i in row.get("evidence_cue_ids", []))
            and isinstance(row.get("input_contract_normalizations", []), list)
            and all(isinstance(i, str) for i in row.get("input_contract_normalizations", []))
        )
        if not eligible:
            output.append(row)
            continue
        try:
            key = json.dumps({k: v for k, v in row.items() if k not in _DESCRIPTIVE | _PENDING},
                             sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            output.append(row)
            continue
        compatible = next((group for group in groups.get(key, []) if all(
            field not in row or field not in group[0] or row[field] == group[0][field]
            for field in _PENDING
        )), None)
        if compatible is None:
            groups.setdefault(key, []).append([row])
            output.append(row)
        else:
            # A rediscovery with no old gate cannot erase the carried obligation.
            # Conflicting gates are kept separate for the normal strict validator.
            for field in _PENDING:
                if field in row:
                    compatible[0].setdefault(field, copy.deepcopy(row[field]))
            compatible.append(row)
    for rows in (group for variants in groups.values() for group in variants):
        if len(rows) < 2:
            continue
        target = rows[0]
        descriptions = [{k: copy.deepcopy(row[k]) for k in sorted(_DESCRIPTIVE) if k in row}
                        for row in rows]
        # The original reasons and classifications reach the first CPA request,
        # together with the union of cited evidence. Preserve them in the audit.
        target["why"] = "\n".join(sorted({
            f"[{row.get('kind', 'context')}] {row['why']}" for row in rows
        }))
        target["evidence_cue_ids"] = sorted({i for row in rows for i in row.get("evidence_cue_ids", [])})
        target["input_contract_normalizations"] = sorted({
            i for row in rows for i in row.get("input_contract_normalizations", [])
        })
        target["coalesced_discovery_descriptions"] = sorted(
            descriptions, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
    return output


def reviewer_reason(finding: Mapping[str, Any], *, limit: int) -> str:
    """Keep every merged nomination visible instead of truncating the union."""
    reason = str(finding.get("why") or "")
    from src.autoslice.semantic_retry_progress import continuation_plan
    plan = continuation_plan(finding.get("continuation_plan"))
    if not isinstance(finding.get("coalesced_discovery_descriptions"), list):
        reason = reason[:limit]
    if plan:
        reason += "\n本轮具体核查计划（不是结论）：" + json.dumps(plan, ensure_ascii=False, sort_keys=True)
    return reason
