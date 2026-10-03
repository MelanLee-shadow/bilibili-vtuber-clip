"""Pure shape checks for a source-fact scorecard-rescore candidate receipt."""

from __future__ import annotations

import hashlib
from typing import Mapping


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_fact_rescore_candidate_shape_is_valid(
    review: Mapping[str, object],
    *,
    selection_hook: str,
    title: str,
    selection_scorecard_sha256: str,
    schema_version: str,
    rescore_candidate_schema_version: str,
) -> bool:
    """Validate the closed rescore block after shared receipt bindings pass."""

    if (
        review.get("schema_version") != schema_version
        or review.get("status") != "FAILED"
        or review.get("decision") != "REPAIR_SCORECARD_STALE"
        or review.get("reason_code") != "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE"
        or review.get("final_selection_hook") != selection_hook
        or review.get("final_title") != title
    ):
        return False
    passes = review.get("passes")
    if not isinstance(passes, list) or not passes or not isinstance(passes[-1], Mapping):
        return False
    last_pass = passes[-1]
    if (
        last_pass.get("status") != "REPAIR"
        or last_pass.get("selection_scorecard_sha256") != selection_scorecard_sha256
    ):
        return False
    block = review.get("rescore_candidate")
    if not isinstance(block, Mapping):
        return False
    repaired_hook = last_pass.get("final_selection_hook")
    repaired_title = last_pass.get("final_title")
    return bool(
        block.get("schema_version") == rescore_candidate_schema_version
        and isinstance(repaired_hook, str)
        and block.get("repaired_selection_hook") == repaired_hook
        and block.get("repaired_selection_hook_sha256") == _sha256_text(repaired_hook)
        and isinstance(repaired_title, str)
        and block.get("repaired_title") == repaired_title
        and block.get("repaired_title_sha256") == _sha256_text(repaired_title)
        and block.get("stale_selection_scorecard_sha256") == selection_scorecard_sha256
        and block.get("selection_scorecard_review")
        == last_pass.get("selection_scorecard_review")
    )
