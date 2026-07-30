"""终审发现的跨轮结转（2026-07-25 7/24 六条死循环案）。

correction pass（扫描 A，text pipeline 内、可改字）与 exact 终审（扫描 B、
只审最终字节、不可改字）是两次独立的 LLM 扫描：B 声学确证了修复
（repaired=True 沙盘）却只能阻断，而它的发现从不喂给下一轮的 A——两边
各自非确定性挑位置，同一处真错字可以永远轮不到 A 修（199_480 悄悄/结晶案
重跑三轮不收敛）。

本模块建确定性闭环：B 的 repaired=True 发现持久化为候选级 sidecar，下轮
A 把它并入自己的 findings 流。carryover 行走与本轮 LLM 发现完全相同的
route→adjudicate→apply 链——无特权：闭集声学仲裁、stale 守卫（suspect 不在
本轮文本即跳过）、baseline/truth 上层权威全部照常，错误提案（礼墨案型）
仍会被拦。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.final_review_contract import (
    is_keep_current_disclosed,
)

SCHEMA_VERSION = "final-review-carryover.v1"
_ROW_KEYS = (
    "cue",
    "base_text_sha256",
    "kind",
    "proposed_full_cue",
    "repair_class",
    "source_surface",
    "candidate_memory_id",
    "evidence_cue_ids",
    "suspect",
    "replacement",
    "why",
)


def carryover_path(out_root: Path, cid: str) -> Path:
    return out_root / f"{cid}.final-review-carryover.json"


def adjudicated_proposed_full_cue(
    finding: Mapping[str, Any],
) -> str | None:
    """Return the exact CPA-authorized target cue, including normalized gaps.

    The normalized finding may deliberately reject a large span edit and leave
    ``proposed_full_cue`` empty even though the later acoustic closed-set
    adjudication built a full candidate and CPA selected ``PROPOSED``.  That
    nested request remains the hash-bound mutation candidate; dropping it from
    exact-final self-heal/carryover turns a recoverable finding into an endless
    disclosure loop.
    """

    direct = finding.get("proposed_full_cue")
    if isinstance(direct, str) and direct.strip():
        return direct
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    mutation = adjudication.get("mutation_authority")
    request = adjudication.get("request")
    witness_judge = adjudication.get("witness_judge")
    judge = (
        witness_judge.get("judge")
        if isinstance(witness_judge, Mapping)
        else None
    )
    proposed = (
        request.get("proposed_cue")
        if isinstance(request, Mapping)
        else None
    )
    current = (
        request.get("current_cue")
        if isinstance(request, Mapping)
        else None
    )
    if not (
        adjudication.get("schema_version")
        == "subtitle-span-adjudication.v1"
        and adjudication.get("status") == "OBSERVED"
        and adjudication.get("decision_authority") == "CPA_JUDGE"
        and adjudication.get("repaired") is True
        and adjudication.get("timing_immutable") is True
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version")
        == "subtitle-correction-mutation-authority.v1"
        and mutation.get("status") == "PASS"
        and isinstance(request, Mapping)
        and request.get("schema_version")
        == "subtitle-span-acoustic-check-request.v1"
        and isinstance(judge, Mapping)
        and judge.get("status") == "JUDGED"
        and judge.get("choice") == "PROPOSED"
        and isinstance(proposed, str)
        and proposed.strip()
        and isinstance(current, str)
        and proposed != current
    ):
        return None
    return proposed


def persist_final_review_carryover(path: Path, audit: Mapping[str, Any]) -> int:
    """Persist B's confirmed findings without losing an unconsumed prior round."""

    prior_rows = load_final_review_carryover(path)
    rows: list[dict[str, Any]] = []
    correction_pass = audit.get("correction_pass")
    correction_findings = (
        correction_pass.get("findings")
        if isinstance(correction_pass, Mapping)
        else None
    )
    for finding in (
        correction_findings if isinstance(correction_findings, list) else []
    ):
        if not isinstance(finding, Mapping):
            continue
        remap = finding.get("carryover_replay_remap")
        adjudication = finding.get("context_audio_adjudication")
        consumed = bool(
            isinstance(adjudication, Mapping)
            and adjudication.get("repaired") is True
        ) or is_keep_current_disclosed(finding)
        if not (
            isinstance(remap, Mapping)
            and remap.get("schema_version")
            == "final-review-carryover-remap.v1"
            and remap.get("status") == "PASS"
            and not consumed
        ):
            continue
        base_sha256 = finding.get("base_text_sha256")
        suspect = finding.get("suspect")
        matched_prior = [
            row
            for row in prior_rows
            if row.get("base_text_sha256") == base_sha256
            and row.get("suspect") == suspect
        ]
        if matched_prior:
            rows.extend(dict(row) for row in matched_prior)
            continue
        row = {
            key: finding.get(key)
            for key in _ROW_KEYS
            if key in finding
        }
        if row:
            rows.append(row)
    for finding in audit.get("findings") or []:
        if not isinstance(finding, Mapping):
            continue
        adjudication = finding.get("exact_release_adjudication")
        if not (
            isinstance(adjudication, Mapping)
            and adjudication.get("repaired") is True
        ):
            continue
        row = {key: finding.get(key) for key in _ROW_KEYS if key in finding}
        proposed_full_cue = adjudicated_proposed_full_cue(finding)
        if proposed_full_cue is not None:
            row["proposed_full_cue"] = proposed_full_cue
        # Normalized final-review findings expose the deterministic edit as
        # ``suggestion`` and the textual spelling witness as
        # ``candidate_provenance``.  The raw schema consumed by the next
        # correction pass calls those fields ``replacement`` and
        # ``source_surface``.  Preserve that translation explicitly,
        # especially for zero-length glossary insertions such as
        # 粉丝灯牌 -> 粉丝团灯牌; otherwise the next pass rejects the carried
        # row as ENTITY_SOURCE_SURFACE_INVALID and the exact reviewer finds
        # the same issue forever.
        if "replacement" not in row and finding.get("suggestion") is not None:
            row["replacement"] = finding.get("suggestion")
        provenance = finding.get("candidate_provenance")
        if (
            "source_surface" not in row
            and isinstance(provenance, Mapping)
            and isinstance(provenance.get("surface"), str)
            and str(provenance["surface"]).strip()
        ):
            row["source_surface"] = str(provenance["surface"]).strip()
        row["cue"] = finding.get("cue_index") or finding.get("cue")
        row["why"] = (
            f"[终审结转] {finding.get('why') or ''} "
            "（上轮 exact 终审已声学确证该修复，本轮由 correction pass 正式落盘）"
        ).strip()
        rows.append(row)
    correction_pass = audit.get("correction_pass")
    correction_discovery = (
        correction_pass.get("discovery")
        if isinstance(correction_pass, Mapping)
        else None
    )
    mutation_authority = audit.get("correction_mutation_authority")
    mutation_failures = (
        mutation_authority.get("failures")
        if isinstance(mutation_authority, Mapping)
        else []
    )
    discovery_incomplete = bool(
        audit.get("status") == "AUDITOR_UNAVAILABLE"
        or (
            isinstance(correction_pass, Mapping)
            and correction_pass.get("status") == "AUDITOR_UNAVAILABLE"
        )
        or (
            isinstance(correction_discovery, Mapping)
            and correction_discovery.get("status")
            in {"MISSING", "AUDITOR_UNAVAILABLE"}
        )
        or any(
            isinstance(failure, Mapping)
            and failure.get("reason_code")
            == "CORRECTION_DISCOVERY_INCOMPLETE"
            for failure in (mutation_failures or [])
        )
    )
    if discovery_incomplete:
        if not rows:
            return len(prior_rows)
        rows = [*prior_rows, *rows]
    deduplicated: dict[tuple[object, object, object], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.get("base_text_sha256") or row.get("cue"),
            row.get("suspect"),
            row.get("proposed_full_cue"),
        )
        deduplicated[key] = row
    rows = list(deduplicated.values())
    if not rows:
        path.unlink(missing_ok=True)
        return 0
    path.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "findings": rows},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return len(rows)


def load_final_review_carryover(path: Path) -> list[dict[str, Any]]:
    """Load prior-round confirmed findings; malformed/absent → [] (fail-open)."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != SCHEMA_VERSION
    ):
        return []
    return [dict(raw) for raw in payload.get("findings") or [] if isinstance(raw, Mapping)]
