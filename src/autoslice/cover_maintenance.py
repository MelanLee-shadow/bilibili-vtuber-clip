"""Cover-only maintenance driver for delivered unattended slices.

Transactional binding machinery remains in cover_repair. This module owns the
cross-tick repair loop and resolves runner policy lazily, preserving the public
runner patch seam in module and cron script execution modes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.autoslice.runner_proxy import RunnerProxy


_runner = RunnerProxy()


def delivered_paths(date: str, rec: dict) -> tuple[Path, Path] | None:
    """(mp4, cover) delivery paths for a pick/song record, or None if the mp4
    was never delivered (failed/gated records have nothing to repair)."""
    if rec.get("delivered"):  # song lane records the delivered path explicitly
        mp4 = Path(rec["delivered"])
    else:
        name = _runner.safe_name(rec.get("hook", ""), rec.get("candidate_id", ""))
        mp4 = _runner.profile_delivery_root() / date / f"{name}.mp4"
    if not mp4.is_file():
        return None
    return mp4, mp4.with_suffix(".cover.png")


def cover_ref_for(date: str, cid: str) -> Path | None:
    """The producer's CLEAN reference frame (pre-burn).  Preferred over frame
    grabs from the delivered mp4, whose burned subtitles would leak into the
    gpt-image-2 identity reference."""
    return next(iter(sorted((_runner.BASE / "out" / date / cid).glob("**/cover_refs/*.cover-ref.png"))), None)


def _json_file_bytes(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write_bytes_file(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _runner._fsync_directory(path.parent)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json_file(path: Path, payload: dict) -> None:
    _runner._atomic_write_bytes_file(path, _runner._json_file_bytes(payload))


def repair_covers(
    date: str,
    state: dict,
    *,
    candidate_ids: set[str] | frozenset[str] | None = None,
    expected_cover_texts: dict[str, str] | None = None,
) -> None:
    """Phase D: delivered clips whose REAL-AI cover was blocked (CPA image lane
    hiccups: gateway 400s, provider outages) get a bounded cover-only retry —
    the mp4 is already good, nothing is re-produced.  One attempt per record
    per tick; permanently blocked covers stay loud in the review summary."""
    records = state.get("picks", []) + state.get("songs", [])
    expected_cover_texts = dict(expected_cover_texts or {})
    if expected_cover_texts and (
        candidate_ids is None
        or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(candidate_id or "")) is None
            or not isinstance(expected, str)
            or not expected.strip()
            for candidate_id, expected in expected_cover_texts.items()
        )
        or not set(expected_cover_texts).issubset(set(candidate_ids))
    ):
        raise ValueError("expected cover text authority must be scoped to selected candidates")
    if candidate_ids is not None:
        requested = set(candidate_ids)
        if not requested or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(value or "")) is None
            for value in requested
        ):
            raise ValueError("selected cover repair candidate ids are invalid or empty")
        matches: dict[str, list[dict]] = {candidate_id: [] for candidate_id in requested}
        for record in records:
            if isinstance(record, dict) and record.get("candidate_id") in matches:
                matches[str(record["candidate_id"])].append(record)
        invalid = {
            candidate_id: len(rows)
            for candidate_id, rows in matches.items()
            if len(rows) != 1
        }
        if invalid:
            raise ValueError(
                f"selected cover repair candidates are missing or duplicated: {invalid}"
            )
        # Scope recovery, budget refresh, exhaustion handling, and provider
        # calls alike.  A selected repair must never mutate a neighboring
        # candidate merely because that record also happens to need a cover.
        records = [record for record in records if record.get("candidate_id") in requested]
    recovered = False
    for record in records:
        paths = _runner.delivered_paths(date, record)
        if paths is not None:
            recovered = (
                _runner._roll_forward_prepared_cover_transactions(date, record, *paths)
                or recovered
            )
            recovered = _runner._recover_committed_cover_binding(date, record, *paths) or recovered
    if recovered:
        _runner.write_state(date, state)
    fingerprint = _runner.pipeline_fingerprint()
    for record in records:
        if (
            record.get("status") in _runner.DELIVERED_TALK_STATUSES
            or record.get("status") == _runner.TALK_COVER_PENDING_STATUS
            or record.get("delivered")
        ) and record.get("title"):
            _runner._refresh_cover_repair_budget(record, fingerprint)
    needed = [r for r in records if _runner.cover_repair_needed(date, r)]
    exhausted = [r for r in needed if not _runner._cover_repair_eligible(r)]
    for record in exhausted:
        record["cover_integrity_status"] = "INVALID_REPAIR_BUDGET_EXHAUSTED"
        record["cover_repair_exhausted"] = True
        status = str(record.get("cover_status") or "BLOCKED_AI_COVER_REQUIRED")
        if "repair_budget_exhausted" not in status:
            record["cover_status"] = f"{status}(repair_budget_exhausted)"
    if exhausted:
        _runner.write_state(date, state)
    todo = [r for r in needed if _runner._cover_repair_eligible(r)]
    if not todo:
        return
    for rec in todo:
        mp4, cover = _runner.delivered_paths(date, rec)
        generation = rec.get("cover_generation")
        route_decision = (
            generation.get("route_decision")
            if isinstance(generation, dict)
            else None
        )
        selected_treatment = (
            str(route_decision.get("selected_treatment") or "")
            if isinstance(route_decision, dict)
            else ""
        )
        if selected_treatment in {"screenshot_direct", "screenshot_polish"}:
            # The generic repair tool is an image-generation workflow.  A
            # screenshot package that fails its hash/document proof must stay
            # on the screenshot route.  A not-yet-delivered talk gets one
            # bounded normal-producer rerun: screenshot_direct rebuilds its
            # deterministic proof, while screenshot_polish obtains fresh
            # polished pixels and repeats the face gate.  Existing deliveries
            # stay blocked for explicit same-BV-safe repair.
            if (
                rec.get("status") == _runner.TALK_COVER_PENDING_STATUS
                and int(rec.get("talk_transient_retry_count") or 0) < 1
            ):
                rec["status"] = "failed"
                rec["failure_kind"] = "cover_route_regeneration"
                rec["failure_recoverable"] = True
                rec["cover_integrity_status"] = (
                    "INVALID_SCREENSHOT_ROUTE_REGENERATION_QUEUED"
                )
                rec["cover_status"] = "SCREENSHOT_ROUTE_REGENERATION_QUEUED"
                rec["cover_route_preservation_error"] = (
                    "screenshot proof is invalid; queued one bounded "
                    "route-preserving producer rerun"
                )
                _runner.log(
                    f"cover repair {rec.get('candidate_id', '?')}: queued one "
                    "route-preserving producer rerun before image request"
                )
            else:
                rec["cover_integrity_status"] = (
                    "INVALID_SCREENSHOT_ROUTE_REPAIR_REQUIRED"
                )
                rec["cover_status"] = (
                    "BLOCKED_SCREENSHOT_COVER_REPAIR_REQUIRED"
                )
                rec["cover_route_preservation_error"] = (
                    "screenshot proof is invalid; generic AI repair is forbidden"
                )
                _runner.log(
                    f"cover repair {rec.get('candidate_id', '?')}: blocked before "
                    "image request to preserve screenshot route"
                )
            _runner.write_state(date, state)
            continue
        try:
            _runner._cover_authority_preflight(date, rec, mp4)
        except (OSError, ValueError) as exc:
            rec["cover_integrity_status"] = "INVALID_AUTHORITY_PREFLIGHT"
            rec["cover_status"] = "BLOCKED_COVER_AUTHORITY_PREFLIGHT"
            rec["cover_authority_preflight_error"] = f"{type(exc).__name__}: {exc}"
            _runner.log(
                f"cover repair {rec.get('candidate_id', '?')}: authority preflight "
                f"blocked before image request: {type(exc).__name__}: {exc}"
            )
            _runner.write_state(date, state)
            continue
        rec["cover_repair_attempts"] = rec.get("cover_repair_attempts", 0) + 1
        rec["cover_repair_lifetime_attempts"] = rec.get("cover_repair_lifetime_attempts", 0) + 1
        cid = rec.get("candidate_id", "?")
        _runner.log(f"cover repair {cid} (attempt {rec['cover_repair_attempts']}/{_runner.COVER_REPAIR_MAX_ATTEMPTS})")
        log_path = _runner.BASE / "logs" / f"{date}_{cid}_cover.log"
        ref = _runner.cover_ref_for(date, cid)
        src_args = ["--ref", str(ref)] if ref else ["--media", str(mp4)]
        style_args: list[str] = []
        diversity_slot = rec.get("cover_diversity_slot")
        if (
            isinstance(diversity_slot, int)
            and not isinstance(diversity_slot, bool)
            and diversity_slot >= 0
        ):
            style_args.extend(["--diversity-slot", str(diversity_slot)])
        if (
            str(cid) not in expected_cover_texts
            and rec.get("title_authority_status") != "RESOLVED_MANUAL"
        ):
            style_args.append("--allow-punch")
        reviewed_cover_text = expected_cover_texts.get(str(cid))
        if reviewed_cover_text is not None:
            style_args.extend(["--cover-text", reviewed_cover_text])
        repair_root = _runner.BASE / "out" / date / str(cid) / "cover_repair"
        attempt_id = (
            f"{fingerprint.removeprefix('sha256:')[:12]}-"
            f"{rec['cover_repair_attempts']:02d}-{time.time_ns()}"
        )
        generation_root = repair_root / "generations" / attempt_id
        generated_cover = generation_root / "final.cover.png"
        ai_background = generation_root / "ai-background.png"
        rec["cover_repair_last_generation_dir"] = str(generation_root)
        rec["cover_integrity_status"] = "REPAIR_ATTEMPT_IN_PROGRESS"
        rec["cover_repair_exhausted"] = False
        if cover.is_file():
            stale_sha = _runner._sha256_regular_file(cover)
            stale_path = repair_root / "stale_covers" / f"{cover.name}.{stale_sha}.png"
            if not stale_path.is_file():
                stale_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cover, stale_path)
        # Charge and persist the bounded attempt before the external image
        # request.  A process/host crash after provider spend cannot evade the
        # lifetime cap or replay the same budget slot forever.
        _runner.write_state(date, state)
        try:
            with open(log_path, "a", encoding="utf-8") as sink:
                completed = subprocess.run(
                    [sys.executable, str(_runner.profile_tool("cover_regenerator")),
                     "--title", str(rec["title"]), *src_args,
                     "--candidate-id", str(cid), "--ai-bg", str(ai_background),
                     "--out", str(generated_cover), *style_args],
                    check=False, stdout=sink, stderr=subprocess.STDOUT, timeout=1200,
                    cwd=str(_runner.REPO_ROOT), env=_runner.child_env_for_date(date),
                )
            rc = completed.returncode
        except subprocess.TimeoutExpired:
            rc = -1
        bound = False
        if rc == 0 and generated_cover.is_file():
            try:
                expected_cover_text = expected_cover_texts.get(str(cid))
                if expected_cover_text is not None:
                    generation, _generation_path = _runner._validate_repaired_cover_generation(
                        cover=generated_cover,
                        title=str(rec["title"]),
                        candidate_id=str(cid),
                    )
                    rendered_lines = generation.get("rendered_lines")
                    def canonical(value: object) -> str:
                        return re.sub(r"\s+", "", str(value))

                    if (
                        generation.get("cover_text") != expected_cover_text
                        or not isinstance(rendered_lines, list)
                        or not rendered_lines
                        or any(not isinstance(value, str) for value in rendered_lines)
                        or canonical("".join(rendered_lines))
                        != canonical(expected_cover_text)
                    ):
                        raise ValueError(
                            "generated cover did not preserve the reviewed text/punctuation"
                        )
                _runner._bind_repaired_cover(date, rec, mp4, cover, generated_cover)
            except (OSError, ValueError, _runner.SongDeliveryError) as exc:
                with open(log_path, "a", encoding="utf-8") as sink:
                    sink.write(f"\nCOVER_BINDING_FAILED: {type(exc).__name__}: {exc}\n")
                rc = -2
            else:
                bound = True
                rec["cover_integrity_status"] = "VALID_BOUND"
                if rec.get("status") == _runner.TALK_COVER_PENDING_STATUS:
                    rec["status"] = "review_ready"
                    rec["bundle_lifecycle"] = "CURRENT"
                    rec["bundle_compliance"] = "COMPLIANT"
                _runner.log(f"cover repaired and hash-bound → {cover.name}")
        if not bound:
            if (
                rec["cover_repair_attempts"] >= _runner.COVER_REPAIR_MAX_ATTEMPTS
                or rec["cover_repair_lifetime_attempts"] >= _runner.COVER_REPAIR_LIFETIME_ATTEMPT_CAP
            ):
                rec["cover_status"] = f"{rec.get('cover_status') or 'BLOCKED_AI_COVER_REQUIRED'}(repair_failed_x{rec['cover_repair_attempts']})"
                rec["cover_integrity_status"] = "INVALID_REPAIR_BUDGET_EXHAUSTED"
                rec["cover_repair_exhausted"] = True
                _runner.log(f"cover repair failed {rec['cover_repair_attempts']}x — left for human review (see {log_path.name})")
            else:
                rec["cover_integrity_status"] = "INVALID_REPAIR_PENDING"
        _runner.write_state(date, state)
