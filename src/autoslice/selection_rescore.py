"""Bounded selection-rescore lane state machine .

the internal source-fact rescore design note §2-§5.  When
source-fact repair edits a candidate's hook and the old selection scorecard
is judged INCOMPATIBLE, this module carries that fact from the standalone
producer subprocess (``producer_package_finalization.py``) across the
process boundary to the unattended runner (``talk_lane.py`` /
``delivery_recovery.py``), and owns the one-shot-per-repaired-hook fingerprint
bookkeeping that keeps the rescore lane bounded instead of an infinite spin.

Deliberately a standalone module rather than growing the two module-debt-
ledger files it is called from (``producer_package_finalization.py``,
``delivery_recovery.py``): both are frozen at their line counts
and any thin call site there is the only budget spent against that ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Mapping

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()

RESCORE_RECEIPT_SCHEMA = "source-fact-rescore.v1"
_SIDECAR_GLOB = "replacement_recuts/*.source-fact-rescore.json"
# 一次修正 hook 一次追改；第三个不同 hook 说明 review 本身在震荡，交终态。
SOURCE_FACT_RESCORE_CAP = 2


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sidecar_path(recut_dir: Path, candidate_id: str) -> Path:
    return recut_dir / f"{candidate_id}.source-fact-rescore.json"


def classify_source_fact_review_marker(source_fact_review: object) -> str:
    """Pick the ``SystemExit`` marker for a non-passing source-fact receipt.

    Pure/testable counterpart of ``producer_package_finalization.py``'s
    marker-selection branch (design §3.2, 狍哥案修复):

    - ``SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE`` → the bounded rescore
      lane, never the terminal EXHAUSTED wording (only one review pass ran).
    - provider-unavailable/call-failed → the pre-existing infra-wait marker.
    - a real 5-pass ``REPAIR_EXHAUSTED`` decision → keeps the EXHAUSTED word.
    - everything else (REPAIR_CYCLE, title-authority block, first-pass
      invalid shape, missing receipt) → a neutral "unresolved" marker; the
      EXHAUSTED word is reserved for genuine pass exhaustion, not a catch-all.
    """

    reason = (
        str(source_fact_review.get("reason_code") or "unknown")
        if isinstance(source_fact_review, Mapping)
        else "missing_receipt"
    )
    if reason == "SOURCE_FACT_REPAIRED_HOOK_SCORECARD_STALE":
        return "SOURCE_FACT_REPAIRED_RESCORE_REQUIRED"
    if reason in {"CPA_TEXT_REVIEW_UNAVAILABLE", "CPA_TEXT_REVIEW_CALL_FAILED"}:
        return "SOURCE_FACT_REVIEW_INFRA_UNRESOLVED"
    if (
        isinstance(source_fact_review, Mapping)
        and source_fact_review.get("decision") == "REPAIR_EXHAUSTED"
    ):
        return "SOURCE_FACT_REPAIR_EXHAUSTED"
    return "SOURCE_FACT_REVIEW_UNRESOLVED"


def write_pending_rescore_sidecar(
    recut_dir: Path,
    *,
    candidate_id: str,
    source_fact_review: Mapping[str, object],
) -> Path:
    """Persist the PENDING rescore receipt where the runner can read it back.

    Raises ``ValueError`` if the review receipt lacks the ``rescore_candidate``
    block a REPAIR_SCORECARD_STALE decision must always carry (file 1 of this
    fix) — a defensive fail-closed check, not an expected path.
    """

    block = source_fact_review.get("rescore_candidate")
    if (
        source_fact_review.get("decision") != "REPAIR_SCORECARD_STALE"
        or not isinstance(block, Mapping)
        or not isinstance(block.get("repaired_selection_hook"), str)
        or not isinstance(block.get("repaired_selection_hook_sha256"), str)
    ):
        raise ValueError("SOURCE_FACT_RESCORE_CANDIDATE_BLOCK_MISSING_OR_INVALID")
    passes = source_fact_review.get("passes")
    last_pass = (
        passes[-1] if isinstance(passes, list) and passes and isinstance(passes[-1], Mapping) else {}
    )
    receipt = {
        "schema_version": RESCORE_RECEIPT_SCHEMA,
        "candidate_id": candidate_id,
        "review_receipt_sha256": source_fact_review.get("receipt_sha256"),
        # 闭环接线（维护者 狍哥案实施指令）：重评分执行位需要
        # hash 校验它重读的最终字幕/clip-context 就是当初判 STALE 那一份，
        # 而不是信任任意一次同名文件重读。
        "final_transcript_sha256": last_pass.get("final_transcript_sha256"),
        "clip_context_prompt_sha256": last_pass.get("clip_context_prompt_sha256"),
        "repaired_hook": block.get("repaired_selection_hook"),
        "repaired_hook_sha256": block.get("repaired_selection_hook_sha256"),
        "repaired_title": block.get("repaired_title"),
        "repaired_title_sha256": block.get("repaired_title_sha256"),
        "stale_scorecard_sha256": block.get("stale_selection_scorecard_sha256"),
        "stale_reason": (
            str(
                (block.get("selection_scorecard_review") or {}).get("reason")
                or ""
            )
            if isinstance(block.get("selection_scorecard_review"), Mapping)
            else ""
        ),
        "attempts": [],
        "status": "PENDING",
    }
    path = _sidecar_path(recut_dir, candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _read_sidecar(work_dir: Path, *, candidate_id: str) -> dict[str, object] | None:
    for path in sorted(work_dir.glob(_SIDECAR_GLOB)):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(document, dict)
            and document.get("schema_version") == RESCORE_RECEIPT_SCHEMA
            and document.get("candidate_id") == candidate_id
            and isinstance(document.get("repaired_hook"), str)
            and isinstance(document.get("repaired_hook_sha256"), str)
            and document["repaired_hook_sha256"]
            == _sha256_text(document["repaired_hook"])
        ):
            return document
    return None


def rescore_fingerprint(failure_fingerprint: str, repaired_hook_sha256: str) -> str:
    """One fingerprint per (this failure identity, this repaired hook).

    A different repaired hook (review oscillated to a new proposal) earns a
    fresh budget slot instead of being silently blocked by the previous
    hook's consumption record; the same hook retried again does not.
    """

    return "sha256:" + hashlib.sha256(
        f"{failure_fingerprint}\0{repaired_hook_sha256}".encode("utf-8")
    ).hexdigest()


def attach_pending_rescore_receipt(
    work_dir: Path,
    *,
    candidate_id: str,
    failure_fingerprint: str,
) -> dict[str, object] | None:
    """Read the finalization-side sidecar and bind it to this pick's failure.

    Returns ``None`` when no sidecar exists (finalization failed before
    writing one, or this failure was not a scorecard-stale rescore case) so
    the caller can leave the pick without a ``source_fact_rescore`` receipt.
    """

    receipt = _read_sidecar(work_dir, candidate_id=candidate_id)
    if receipt is None:
        return None
    return {
        **receipt,
        "rescore_fingerprint": rescore_fingerprint(
            failure_fingerprint, receipt["repaired_hook_sha256"]
        ),
    }


def unconsumed_rescore_fingerprint(record: Mapping[str, object]) -> str | None:
    """Return the fingerprint eligible for one bounded requeue, or ``None``.

    Mirrors ``_unconsumed_final_review_carryover`` in ``delivery_recovery.py``
    (same consumed-ledger pattern, ``rescore_consumed_fingerprints`` instead
    of ``final_review_carryover_consumed_fingerprints``), capped at
    ``SOURCE_FACT_RESCORE_CAP`` instead of the carryover lane's own cap.
    """

    receipt = record.get("source_fact_rescore")
    if (
        record.get("status") != "failed"
        or record.get("failure_kind") != "selection_rescore"
        or record.get("failure_recoverable") is not True
        or not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != RESCORE_RECEIPT_SCHEMA
        or receipt.get("status") != "PENDING"
    ):
        return None
    fingerprint = receipt.get("rescore_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return None
    consumed = record.get("rescore_consumed_fingerprints")
    consumed_fingerprints = (
        [value for value in consumed if isinstance(value, str)]
        if isinstance(consumed, list)
        else []
    )
    if (
        fingerprint in consumed_fingerprints
        or len(consumed_fingerprints) >= SOURCE_FACT_RESCORE_CAP
    ):
        return None
    return fingerprint


# --- Bounded rescore execution (维护者 狍哥案实施指令：闭环接线) ---
#
# Closes the gap the design left open: a requeued rescore_pending item used
# to re-enter pending_talk with a cleared scorecard and nothing ever
# regenerated it.  This section is the runner-side executor: it re-derives
# the clip-scoped cues and clip context that were in scope inside the
# producer subprocess (from the same deterministic on-disk paths that
# ``read_publish_meta``/the sidecar reader already use), hash-checks them
# against ``final_transcript_sha256``/``clip_context_prompt_sha256`` in the
# receipt (persisted above), and calls the single-candidate rescore
# primitive in ``semantic_candidate_selector.py``.


def build_rescore_llm_call() -> Callable[[str], str]:
    """Module-level LLM-call builder for the rescore lane.

    Tests monkeypatch this name directly (repo convention: patch the calling
    module's own builder, not ``llm_client.build_llm_call`` — see
    ``conftest.py``'s hermetic-CPA guard).
    """

    from src.autoslice.llm_client import LlmConfig, build_llm_call

    return build_llm_call(
        LlmConfig(
            transport="command",
            command_template=(
                "bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} "
                "'gpt-5.6-sol gpt-5.5 gpt-5.4' medium"
            ),
            timeout_seconds=180.0,
        )
    )


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _rescore_candidate_out_root(date: str, candidate_id: str) -> Path:
    return _runner.BASE / "out" / date / candidate_id


def _rescore_source_cues(
    candidate_out_root: Path, candidate_id: str, *, expected_sha256: object
) -> list | None:
    """Rebuild the final-recut cues exactly as producer_package_finalization
    built them, then reject a stale/foreign file by hash instead of trusting
    any same-named file found on disk."""

    from src.autoslice.jingting_chunker import parse_srt_cues
    from src.autoslice.review_evidence import SourceCue

    subtitle_path = (
        candidate_out_root / "replacement_recuts" / f"{candidate_id}.recut.srt"
    )
    try:
        srt_text = subtitle_path.read_text(encoding="utf-8")
    except OSError:
        return None
    cues = [
        SourceCue(
            f"text_final_{index:04d}", cue.start_ms, cue.end_ms, cue.text.strip(),
            "zh", "speech", 1.0,
        )
        for index, cue in enumerate(parse_srt_cues(srt_text), start=1)
        if cue.text.strip()
    ]
    transcript_text = "\n".join(cue.text for cue in cues)
    if (
        isinstance(expected_sha256, str)
        and expected_sha256
        and _sha256_text(transcript_text) != expected_sha256
    ):
        return None
    return cues


def _rescore_clip_context_prompt(
    candidate_out_root: Path, candidate_id: str, *, expected_sha256: object
) -> str:
    """Best-effort clip-context enrichment; missing/stale is not fatal — the
    rescore prompt already tolerates an empty clip context (it only adds
    danmaku/SC color, the transcript cues carry the actual evidence)."""

    from src.autoslice.clip_context import ClipContextError, clip_context_prompt_text

    context_path = candidate_out_root / f"{candidate_id}.clip-context.json"
    try:
        payload = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    try:
        text = clip_context_prompt_text(payload)
    except ClipContextError:
        return ""
    if (
        isinstance(expected_sha256, str)
        and expected_sha256
        and _sha256_text(text) != expected_sha256
    ):
        return ""
    return text


def produce_eligible(item: Mapping[str, object]) -> bool:
    """A rescore-pending talk item must not be dispatched to produce this tick.

    Its hook/scorecard identity is not settled yet — producing it now would
    burn a produce attempt on content that may still change under it.
    """

    return not bool(item.get("rescore_pending"))


def split_produce_blocked_talk_items(
    items: list,
) -> tuple[list, list]:
    """Partition one tick's talk items into (eligible, rescore-blocked)."""

    eligible: list = []
    blocked: list = []
    for item in items:
        if isinstance(item, dict) and not produce_eligible(item):
            blocked.append(item)
        else:
            eligible.append(item)
    return eligible, blocked


def _record_rescore_attempt(
    receipt: dict[str, object],
    *,
    fingerprint: object,
    outcome: str,
    reason_code: str | None = None,
    new_scorecard_sha256: str | None = None,
    new_tier: object = None,
    new_effective_score: object = None,
) -> None:
    attempts = list(receipt.get("attempts") or [])
    entry: dict[str, object] = {"rescore_fingerprint": fingerprint, "outcome": outcome}
    if reason_code:
        entry["reason_code"] = reason_code
    if new_scorecard_sha256:
        entry["new_scorecard_sha256"] = new_scorecard_sha256
    if new_tier is not None:
        entry["new_tier"] = new_tier
    if new_effective_score is not None:
        entry["new_effective_score"] = new_effective_score
    attempts.append(entry)
    receipt["attempts"] = attempts


def _apply_one_pending_rescore(
    date: str, item: dict, *, llm_call: Callable[[str], str]
) -> bool:
    """Mutate ``item`` in place for one rescore attempt.

    Returns ``True`` when the item must move out of ``pending_talk`` into a
    terminal ``candidate_rejected`` pick (UNSUPPORTED/INVALID_CARD).  Provider
    failures and successes both return ``False``: a success stays in
    ``pending_talk`` (now competing normally in ``prioritize()``), a provider
    failure stays ``rescore_pending`` for a later tick with its fingerprint
    left unconsumed.
    """

    from src.autoslice.semantic_candidate_selector import rescore_candidate_scorecard

    candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
    receipt = dict(item.get("source_fact_rescore") or {})
    consumed = item.get("rescore_consumed_fingerprints")
    fingerprint = (
        consumed[-1] if isinstance(consumed, list) and consumed else None
    )
    out_root = _rescore_candidate_out_root(date, candidate_id)
    cues = _rescore_source_cues(
        out_root, candidate_id, expected_sha256=receipt.get("final_transcript_sha256")
    )
    if cues is None:
        _record_rescore_attempt(
            receipt,
            fingerprint=fingerprint,
            outcome="PROVIDER_UNAVAILABLE",
            reason_code="RESCORE_SOURCE_ARTIFACT_UNAVAILABLE",
        )
        item["source_fact_rescore"] = receipt
        return False
    clip_context_prompt = _rescore_clip_context_prompt(
        out_root, candidate_id, expected_sha256=receipt.get("clip_context_prompt_sha256")
    )
    outcome = rescore_candidate_scorecard(
        candidate_id=candidate_id,
        cues=cues,
        repaired_hook=str(receipt.get("repaired_hook") or ""),
        clip_context_prompt=clip_context_prompt,
        llm_call=llm_call,
        stale_scorecard=None,
    )
    if outcome["outcome"] == "RESCORED":
        new_card = outcome["selection_scorecard"]
        _record_rescore_attempt(
            receipt,
            fingerprint=fingerprint,
            outcome="RESCORED",
            new_scorecard_sha256=_sha256_json(new_card),
            new_tier=new_card.get("tier"),
            new_effective_score=new_card.get("effective_score"),
        )
        receipt["status"] = "RESCORED_REQUEUED"
        item["source_fact_rescore"] = receipt
        item["hook"] = str(receipt.get("repaired_hook") or item.get("hook", ""))
        item["selection_scorecard"] = new_card
        item["rescore_pending"] = False
        # 设计 §5：重评分成功后以新卡竞争全场排序（可被挤出 Top-N），不再
        # 像普通 retry 那样无条件钉在 pending_talk——清掉 selected_repair
        # pin 让它重新流经 prioritize() 的排名池。
        item["selected_repair"] = False
        return False
    if outcome["outcome"] == "PROVIDER_UNAVAILABLE":
        _record_rescore_attempt(
            receipt,
            fingerprint=fingerprint,
            outcome="PROVIDER_UNAVAILABLE",
            reason_code=str(outcome.get("reason_code") or ""),
        )
        item["source_fact_rescore"] = receipt
        return False
    # UNSUPPORTED / INVALID_CARD: terminal, matching the lane design's
    # ``selection_rescore_failed`` rejection (not a candidate the backfill
    # policy may silently keep retrying).
    _record_rescore_attempt(
        receipt,
        fingerprint=fingerprint,
        outcome=str(outcome["outcome"]),
        reason_code=str(outcome.get("reason_code") or ""),
    )
    receipt["status"] = "FAILED"
    item["source_fact_rescore"] = receipt
    item["rescore_pending"] = False
    item["status"] = "candidate_rejected"
    item["failure_kind"] = "selection_rescore"
    item["rejection_reason"] = "selection_rescore_failed"
    return True


def execute_pending_rescores(
    date: str,
    state: dict,
    *,
    llm_call: Callable[[str], str] | None = None,
    candidate_ids: Collection[str] | None = None,
) -> int:
    """Run the bounded rescore lane over every ``rescore_pending`` item.

    Scans the whole of ``pending_talk`` (not just a fresh requeue batch) so a
    provider-failure leftover from an earlier tick gets another attempt —
    that item never returns to ``picks`` on its own.
    """

    pending = state.get("pending_talk")
    if not isinstance(pending, list):
        return 0
    allowed = set(candidate_ids) if candidate_ids is not None else None
    targets = [
        item
        for item in pending
        if isinstance(item, dict)
        and item.get("rescore_pending")
        and (
            allowed is None
            or str(item.get("cid") or item.get("candidate_id") or "") in allowed
        )
    ]
    if not targets:
        return 0
    resolved_llm_call = llm_call if llm_call is not None else build_rescore_llm_call()
    terminal_ids = {
        id(item)
        for item in targets
        if _apply_one_pending_rescore(date, item, llm_call=resolved_llm_call)
    }
    if terminal_ids:
        state["pending_talk"] = [item for item in pending if id(item) not in terminal_ids]
        state.setdefault("picks", []).extend(
            item for item in pending if id(item) in terminal_ids
        )
    return len(targets)
