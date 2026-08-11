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

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.autoslice.acoustic_witness_adjudication import (
    valid_inaudible_drop_authority,
    valid_inaudible_witness_override,
)
from src.autoslice.final_review_contract import (
    correction_carryover_consumed,
)

SCHEMA_VERSION = "final-review-carryover.v1"
# 硬退出侧车（Ivan 2026-08-10 15:05Z 交棒清单第 7 项「硬退出丢 carryover
# (超时/崩溃跳过侧车落盘)」）：exact 终审的确证行只在**整轮结束**时才落盘，
# 中间被 SIGKILL（runner `subprocess.run(timeout=5400)` 超时即 kill）或被非
# `SystemExit` 异常打断，这一轮算出来的行就没了。checkpoint 是同一批行的
# 「算出来就写」副本，与封存件同源同谓词、同样零特权。
CHECKPOINT_SCHEMA_VERSION = "final-review-carryover-checkpoint.v1"
INTEGRITY_SCHEMA_VERSION = "final-review-carryover-integrity.v1"
_ROW_KEYS = (
    "cue",
    "base_text_sha256",
    "kind",
    "proposed_full_cue",
    "repair_class",
    "source_surface",
    "candidate_provenance",
    "draft_fidelity_kept_provenance",
    "candidate_memory_id",
    "evidence_cue_ids",
    "suspect",
    "replacement",
    "why",
    "exact_release_adjudication",
)


def carryover_path(out_root: Path, cid: str) -> Path:
    return out_root / f"{cid}.final-review-carryover.json"


def carryover_checkpoint_path(path: Path) -> Path:
    """The in-flight sibling of a sealed carryover sidecar."""

    return path.with_suffix(".checkpoint.json")


def _rows_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_carryover_atomic(
    path: Path, rows: list[dict[str, Any]], *, schema_version: str
) -> None:
    """Replace ``path`` in one step, never leaving a half-written sidecar.

    ``Path.write_text`` truncates in place: a producer killed mid-write used to
    destroy the **previous** round's still-unconsumed rows and leave bytes that
    no longer parse.  Write a sibling temp file, fsync it, then ``os.replace``
    — a reader sees either the whole old file or the whole new one.  The
    integrity block is the second half of the same guarantee for any path that
    does not go through this function (a truncated copy, an interrupted
    transfer): a payload whose count/digest disagree with its rows is treated
    as absent rather than consumed as a complete round.
    """

    payload = {
        "schema_version": schema_version,
        "findings": rows,
        "integrity": {
            "schema_version": INTEGRITY_SCHEMA_VERSION,
            "finding_count": len(rows),
            "findings_sha256": _rows_digest(rows),
        },
    }
    text = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
    except BaseException:
        # A SIGKILL still strands the temp file (the next write reuses the same
        # name); an ordinary failure should not.
        temporary.unlink(missing_ok=True)
        raise


def _load_carryover_rows(path: Path, *, schema_version: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != schema_version
    ):
        return []
    rows = [
        dict(raw) for raw in payload.get("findings") or [] if isinstance(raw, Mapping)
    ]
    integrity = payload.get("integrity")
    if integrity is None:
        # Sidecars written before the marker existed (production still holds
        # several) stay readable; they were sealed by the same predicate.
        return rows
    if not (
        isinstance(integrity, Mapping)
        and integrity.get("schema_version") == INTEGRITY_SCHEMA_VERSION
        and integrity.get("finding_count") == len(rows)
        and integrity.get("findings_sha256") == _rows_digest(rows)
    ):
        return []
    return rows


def _row_key(row: Mapping[str, Any]) -> tuple[object, object, object]:
    return (
        row.get("base_text_sha256") or row.get("cue"),
        row.get("suspect"),
        row.get("proposed_full_cue"),
    )


def _deduplicated_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: dict[tuple[object, object, object], dict[str, Any]] = {}
    for row in rows:
        deduplicated[_row_key(row)] = row
    return list(deduplicated.values())


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
        and isinstance(proposed, str)
        and isinstance(current, str)
        and proposed != current
    ):
        return None
    if proposed == "":
        if not (
            finding.get("repair_class") == "acoustic_drop_cue"
            and request.get("repair_class") == "acoustic_drop_cue"
            and valid_inaudible_drop_authority(adjudication)
        ):
            return None
    else:
        if not proposed.strip() or judge.get("choice") != "PROPOSED":
            return None
        verdict = adjudication.get("verdict")
        if (
            isinstance(verdict, Mapping)
            and verdict.get("status") == "OBSERVED"
            and verdict.get("target_audible") is False
            and not valid_inaudible_witness_override(
                check_request=request,
                witness=verdict,
                witness_judge=witness_judge,
            )
        ):
            return None
    return proposed


def _confirmed_exact_final_rows(
    audit: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Rows for exact-final findings the acoustic/judge chain已确证该改。

    Single predicate shared by the end-of-round seal and the in-flight
    checkpoint: a checkpoint must never be able to carry a row the sealed write
    would have refused.
    """

    rows: list[dict[str, Any]] = []
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
    return rows


def checkpoint_final_review_carryover(
    path: Path, audit: Mapping[str, Any]
) -> int:
    """Write this exact-final pass's confirmed rows before the round ends.

    The sealed sidecar is only written once the whole exact-final gate reaches
    a verdict — after up to five more self-heal passes, each another full
    acoustic/LLM scan.  A producer killed in that window (the runner's
    ``subprocess.run(timeout=5400)`` sends SIGKILL, which no ``finally`` and no
    signal handler can survive) used to lose everything this round confirmed.

    Deliberately additive-only: it never rewrites and never deletes the sealed
    file, so an interrupted round can only ever *add* replay candidates.  The
    rows carry no privilege — the next correction pass routes, adjudicates and
    stale-guards them exactly like a fresh LLM finding.
    """

    checkpoint = carryover_checkpoint_path(path)
    rows = _deduplicated_rows(
        [
            *_load_carryover_rows(
                checkpoint, schema_version=CHECKPOINT_SCHEMA_VERSION
            ),
            *_confirmed_exact_final_rows(audit),
        ]
    )
    if not rows:
        return 0
    _write_carryover_atomic(
        checkpoint, rows, schema_version=CHECKPOINT_SCHEMA_VERSION
    )
    return len(rows)


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
        consumed = correction_carryover_consumed(finding)
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
    rows.extend(_confirmed_exact_final_rows(audit))
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
    rows = _deduplicated_rows(rows)
    if not rows:
        # Nothing outstanding: the in-flight checkpoint of this same round is
        # now superseded by the clean verdict and must not outlive it.
        path.unlink(missing_ok=True)
        carryover_checkpoint_path(path).unlink(missing_ok=True)
        return 0
    _write_carryover_atomic(path, rows, schema_version=SCHEMA_VERSION)
    return len(rows)


def load_final_review_carryover(path: Path) -> list[dict[str, Any]]:
    """Load sealed prior-round findings; malformed/absent → [] (fail-open).

    Sealed-only on purpose.  The same-run exact-final replay
    (``producer_package_finalization._replayable_exact_final_carryover_findings``)
    reads through here and must keep seeing only rows a completed round sealed;
    the checkpoint is a next-round replay candidate, never a same-run authority.
    """

    return _load_carryover_rows(path, schema_version=SCHEMA_VERSION)


def load_replayable_final_review_carryover(path: Path) -> list[dict[str, Any]]:
    """Sealed rows plus anything a hard-exited round only got as far as checkpointing.

    Consumed by the next round's correction pass, which is the one place that
    is allowed to see both.  Sealed rows win on a collision: a completed round
    outranks an interrupted one.
    """

    sealed = _load_carryover_rows(path, schema_version=SCHEMA_VERSION)
    checkpointed = _load_carryover_rows(
        carryover_checkpoint_path(path),
        schema_version=CHECKPOINT_SCHEMA_VERSION,
    )
    sealed_keys = {_row_key(row) for row in sealed}
    return [
        *sealed,
        *(row for row in checkpointed if _row_key(row) not in sealed_keys),
    ]
