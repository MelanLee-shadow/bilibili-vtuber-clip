"""Runner-facing bridge from prepared lane results to one batch commit."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.producer_batch_projection import (
    prepared_batch_entry,
    project_materialized_song,
    project_materialized_talk,
)
from src.autoslice.producer_batch_transaction import (
    PreparedBatchEntry,
    commit_prepared_prefix,
    resume_pending_batch,
)
from src.autoslice.producer_delivery_transaction import load_prepared_delivery
from src.autoslice.published_cover_carry import finalize_talk_delivery_status
from src.autoslice.qixi_transaction_core import exclusive_runner_commit
from src.autoslice import runner_state_writeback


@dataclass
class TalkCheckpointState:
    """Mutable counters and CAS preimage for one streamed Talk lane."""

    checkpoint_before: bytes | None
    retry: list[dict]
    rejected: int = 0
    recoverable_failure: bool = False


def project_prepared_results(
    *, runtime_root: Path, date: str, lane: str, items: Sequence[Mapping[str, object]],
    results: Sequence[dict], runner: object,
) -> list[PreparedBatchEntry]:
    """Project every valid prepared result in the ordered dispatch prefix.

    A prior failure/retry has no target to install, so it can coexist in the
    exact after-image with a later prepared candidate.  Entries retain input
    order and the one batch journal atomically binds both the state result rows
    and the successful target subsequence.
    """

    entries: list[PreparedBatchEntry] = []
    if len(results) > len(items):
        raise ValueError("producer result count exceeds its ordered input prefix")
    # Dispatch deliberately yields only an input prefix when deployment/live
    # authority appears.  Never require the deferred tail to have a result.
    for item, result in zip(items[:len(results)], results, strict=True):
        candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
        entry = prepared_batch_entry(
            runtime_root=runtime_root, lane=lane, candidate_id=candidate_id, result=result,
        )
        if entry is None:
            continue
        if lane == "talk":
            project_materialized_talk(
                result, item=item, date=date, work_root=runtime_root / "out" / date,
                runner=runner, finalize=finalize_talk_delivery_status,
            )
        else:
            project_materialized_song(
                result, runtime_root=runtime_root, candidate_id=candidate_id,
                song_status=runner.song_status,
            )
        entries.append(entry)
    return entries


def reject_materialized_prepare_results(
    *, runtime_root: Path, lane: str, items: Sequence[Mapping[str, object]], results: Sequence[Mapping[str, object]],
) -> None:
    """Reject a real producer that ignored runner-only ``prepare_only``.

    A test double may return a status-only failure/success projection without
    an ``rc`` or public paths.  A real successful subprocess, however, must
    return the private handle status; otherwise its public target claim could
    pass through the legacy state-write branch without an owned commit.  A
    zero exit alone is not evidence of delivery: Song uses ``rc=0`` for
    blocked and terminally rejected proof results too.
    """

    if len(results) > len(items):
        raise ValueError("producer result count exceeds its ordered input prefix")
    materialized_fields = {
        "delivered", "delivery_manifest_path", "delivered_sidecars",
        "delivery_upload_enabled",
    }
    for item, result in zip(items[:len(results)], results, strict=True):
        candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
        status = result.get("status")
        if status == "delivery_prepared_no_target":
            entry = prepared_batch_entry(
                runtime_root=runtime_root, lane=lane, candidate_id=candidate_id, result=result,
            )
            if entry is None:
                raise ValueError("prepare-only result lacks a private handle")
            load_prepared_delivery(
                runtime_root=runtime_root, manifest_path=entry.handle.manifest_path,
            )
            continue
        # A zero exit only says that the pipeline ran.  Song uses rc=0 for
        # blocked and terminally rejected proof results too; only public
        # delivery fields or a known positive status prove materialization.
        if any(field in result for field in materialized_fields) or (
            result.get("rc") == 0
            and isinstance(status, str)
            and status in {
                "ok", "review_ready", "quarantine", "published",
                # Talk media is already materialized even while its cover is
                # pending; it still cannot bypass the prepare transaction.
                "media_ready_cover_pending",
            }
        ):
            raise ValueError("prepare-only producer returned a materialized success result")


def make_talk_result_checkpoint(
    *, runtime_root: Path, date: str, state_path: Path, state: dict,
    talk_items: Sequence[Mapping[str, object]], rescore_blocked_items: Sequence[dict],
    frozen_talk_candidate_ids, production_preimage: Mapping[str, object] | None,
    exact_contract_ids: set[str], runner: object, checkpoint_before: bytes | None,
    replace_pending: Callable[..., None], seal_transition: Callable[..., bool],
    apply_rejection: Callable[..., bool], delivered_statuses, cover_pending_status: str,
    title_max_attempts: int, persist_state: Callable[[], None], log: Callable[[str], None],
) -> tuple[Callable[[int, Mapping[str, object], dict], bool], TalkCheckpointState]:
    """Build the runner's per-result Talk projection and atomic checkpoint."""

    checkpoint_state = TalkCheckpointState(
        checkpoint_before=checkpoint_before, retry=[],
    )

    def checkpoint_result(
        index: int, item: Mapping[str, object], result: dict,
    ) -> bool:
        entries = project_prepared_results(
            runtime_root=runtime_root, date=date, lane="talk", items=(item,),
            results=(result,), runner=runner,
        )
        title_retry = False
        if result.get("status") == "title_failed":
            item["title_attempts"] = item.get("title_attempts", 0) + 1
            log(
                f"{item['cid']}: title generation failed "
                f"(attempt {item['title_attempts']})"
            )
            if item["title_attempts"] < title_max_attempts:
                checkpoint_state.retry.append(item)
                title_retry = True
            else:
                result["status"] = "failed"
                result["error"] = "title generation failed 3x"
        if not title_retry:
            candidate_id = str(item.get("cid") or item.get("candidate_id") or "")
            if apply_rejection(
                result, exact_selected=candidate_id in exact_contract_ids
            ):
                checkpoint_state.rejected += 1
            if result.get("failure_recoverable") is True:
                checkpoint_state.recoverable_failure = True
            if result.get("status") in delivered_statuses:
                result["bundle_lifecycle"] = "CURRENT"
                result["bundle_compliance"] = "COMPLIANT"
            elif result.get("status") == cover_pending_status:
                result["bundle_lifecycle"] = "PENDING_COVER"
                result["bundle_compliance"] = "COVER_REQUIRED"
            state["picks"].append(result)
        replace_pending(
            state, frozen_talk_candidate_ids,
            checkpoint_state.retry + list(talk_items[index + 1 :]) + list(rescore_blocked_items),
        )
        if not seal_transition(
            date, state, frozen_talk_candidate_ids, production_preimage
        ):
            if not entries:
                persist_state()
            raise ValueError("Talk production transition could not be sealed")
        if entries:
            commit_projected_prefix(
                runtime_root=runtime_root, date=date, state_path=state_path,
                state_before=checkpoint_state.checkpoint_before, state=state,
                entries=entries,
            )
        else:
            persist_state()
        checkpoint_state.checkpoint_before = runner_state_writeback.state_bytes(state)
        return not (title_retry or checkpoint_state.recoverable_failure)

    return checkpoint_result, checkpoint_state


def dispatch_prepared_lane(
    *, runtime_root: Path, date: str, lane: str, items: Sequence[Mapping[str, object]],
    state_path: Path, state: Mapping[str, object], produce_batch, produce_fn, runner: object,
    on_result: Callable[[int, Mapping[str, object], dict], bool | None] | None = None,
) -> tuple[bytes | None, list[dict], list[PreparedBatchEntry]]:
    """Bind the live state image, then run one identity-preserving prepare dispatch.

    ``state`` is the tentative in-memory document the caller will later seal
    into the batch journal.  It must still equal the disk authority before a
    provider is allowed to start: otherwise a compliant writer could have
    updated the date between the runner's last ordinary write and dispatch,
    and a later exact-CAS would silently install an after-image derived from a
    stale mapping.
    """

    with exclusive_runner_commit(runtime_root):
        before = runner_state_writeback.read_exact_state_preimage(
            state_path, runtime_root=runtime_root,
        )
        if before != runner_state_writeback.state_bytes(state):
            raise runner_state_writeback.RunnerStateWritebackError(
                "RUNNER_STATE_WRITEBACK_DISPATCH_PREIMAGE_DRIFT"
            )
    if on_result is None:
        results = produce_batch(date, list(items), produce_fn, prepare_only=True)
        reject_materialized_prepare_results(
            runtime_root=runtime_root, lane=lane, items=items, results=results,
        )
        entries = project_prepared_results(
            runtime_root=runtime_root, date=date, lane=lane, items=items,
            results=results, runner=runner,
        )
        return before, results, entries

    def handle_result(index: int, item: Mapping[str, object], result: dict) -> bool | None:
        reject_materialized_prepare_results(
            runtime_root=runtime_root, lane=lane, items=(item,), results=(result,),
        )
        return on_result(index, item, result)

    # Keep old test/embedding doubles that have not adopted the callback
    # keyword on the safe, complete-batch path.  Real runner dispatch always
    # exposes the keyword, so completed results are handed off before the next
    # candidate is scheduled.
    try:
        parameters = inspect.signature(produce_batch).parameters.values()
    except (TypeError, ValueError):
        supports_callback = True
    else:
        supports_callback = any(
            parameter.name == "on_result"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    if supports_callback:
        results = produce_batch(
            date, list(items), produce_fn, prepare_only=True, on_result=handle_result,
        )
        return before, results, []

    results = produce_batch(date, list(items), produce_fn, prepare_only=True)
    for index, (item, result) in enumerate(
        zip(items[:len(results)], results, strict=True)
    ):
        handle_result(index, item, result)
    return before, results, []


def resume_prepared_batch_if_present(*, runtime_root: Path, date: str) -> dict | None:
    """Take the short commit lease only to replay an existing batch journal."""

    root = runtime_root / ".producer-batch-journal"
    if not root.exists() and not root.is_symlink():
        return None
    with exclusive_runner_commit(runtime_root) as lease:
        return resume_pending_batch(runtime_root=runtime_root, date=date, lease=lease)


def maintain_selected_source_fact_recovery(
    *, runtime_root: Path, maintain, date: str, state: dict,
    automatic_maintenance: bool, candidate_ids, persist, readback, log,
    song_pipeline_fingerprint,
):
    """Run the runner's existing deterministic recovery under its short lease."""

    with exclusive_runner_commit(runtime_root):
        return maintain(
            date, state, automatic_maintenance=automatic_maintenance,
            candidate_ids=candidate_ids, persist=persist, readback=readback,
            log=log, song_pipeline_fingerprint=song_pipeline_fingerprint,
        )


def finish_producer_date(
    *, runtime_root: Path, date: str, state: dict, mutate_songs: bool, repair_covers, automatic_maintenance: bool,
    candidate_ids, project_terminal, persist_state, write_reports, log, capture_candidates,
    routing_claim, queue_collab,
) -> None:
    """Finish projection with legacy cover repair serialized under runner lease.

    ``cover_maintenance.repair_covers`` still has a multi-file target bind
    whose provider-backed implementation predates private producer prepare.
    It is intentionally a documented serialized exception: the normal
    Talk/Song packages above this helper prepare outside the lock, while this
    legacy cover-only transaction keeps its provider work and target/state bind
    under one canonical lease until it receives its own staged transaction.
    """

    with exclusive_runner_commit(runtime_root):
        repair_covers(date, state, automatic_maintenance=automatic_maintenance, candidate_ids=candidate_ids)
        terminal = project_terminal(state, mutate_songs=mutate_songs)
        persist_state()
    write_reports(date, state)
    log(
        f"{date} batch finished [{state['status']}]: talk {len(terminal['delivered_talk'])}/{len(terminal['picks'])} delivered"
        f" ({len(terminal['repaired'])} boundary-self-repaired), song {len(terminal['delivered_songs'])} delivered"
        f" / {len(terminal['blocked_songs'])} retry-blocked / {len(terminal['rejected_songs'])} rejected"
        f" / {len(terminal['songs'])} attempted, {len(terminal['failures'])} failure(s)"
    )
    queue_collab(date, state, capture_candidates, routing_claim=routing_claim)
    # Collab receipt projection may change state metadata, so commit it in a
    # second short normal writer lease rather than holding runner.lock while
    # it emits reports or local evidence.
    persist_state()
    write_reports(date, state)


def pre_dispatch_block_message(state: Mapping[str, object], *, date: str) -> tuple[str, str, str] | None:
    """Return the fail-closed state-corruption/reconciliation terminal notice."""

    if state.get("status") == "state_corrupt_blocked":
        detail = str(state.get("state_error", "state file corrupt"))
        return (
            "STATE_CORRUPT", f"{date}: {detail} — date BLOCKED, needs human",
            f"{date}: state corrupt — blocked, not reprocessing (would re-deliver everything)",
        )
    if state.get("status") == "publication_reconciliation_blocked":
        detail = str(state.get("publication_reconciliation_error", "invalid publication authority"))
        return (
            "PUBLICATION_RECONCILIATION_BLOCKED", f"{date}: {detail}",
            f"{date}: publication reconciliation invalid — blocked fail-closed",
        )
    return None


def initialize_date_state(read_state, date: str) -> dict:
    """Read and normalize the ordinary runner's minimal state defaults."""

    state = read_state(date)
    state.setdefault("run_mode", "PRODUCTION")
    state.setdefault("source_authority", "RECORDER")
    state.setdefault("upload_allowed", False)
    return state


def commit_projected_prefix(
    *, runtime_root: Path, date: str, state_path: Path, state_before: bytes | None,
    state: Mapping[str, object], entries: Sequence[PreparedBatchEntry],
) -> bool:
    """Install prepared targets and the exact tentative state under one lease."""

    if not entries:
        return False
    with exclusive_runner_commit(runtime_root) as lease:
        commit_prepared_prefix(
            runtime_root=runtime_root, date=date, state_path=state_path,
            state_before=state_before, after_state=state, entries=entries, lease=lease,
        )
    return True
