"""Regression tests for receipt history and pre-dispatch admission.

Only synthetic source bytes and in-memory budgets are used. A failed admission
must not reach extraction or provider observation, even when both surfaces agree.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import native_audio_budget_receipt as receipts
from src.autoslice import native_foreign_witness as native
from src.autoslice.moss_transcription import MOSS_MODEL
from src.autoslice.supplement_audio_budget import start_budget


def _persist(source: Path, path: Path) -> dict:
    return receipts.persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=path,
    )


@pytest.mark.parametrize("compatibility", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_unrelated_newer_ledger_does_not_overwrite_history(
    tmp_path: Path, compatibility: bool, pending: bool,
) -> None:
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"synthetic-source")
    path = tmp_path / "native-audio-budget.json"
    old = start_budget(source)
    attempt = old.consume("moss", MOSS_MODEL, 1_000, 2_000)
    if not pending:
        old.finish_attempt(attempt, status="OBSERVED")
    _persist(source, path)
    before = path.read_bytes()

    # Simulate a different process ledger, not a new real spending allowance.
    newer = start_budget(source)
    for start in ([5_000] if pending else [5_000, 7_000]):
        attempt = newer.consume("moss", MOSS_MODEL, start, start + 1_000)
        newer.finish_attempt(attempt, status="OBSERVED")
    assert newer.snapshot()["revision"] > json.loads(before)["revision"]
    with pytest.raises(receipts.BudgetReceiptPersistenceError):
        if compatibility:
            native._persist_budget_receipt(
                source_media=source,
                source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                receipt_path=path,
            )
        else:
            _persist(source, path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("defect", ["unknown", "total", "boolean", "attempt_id", "pending"])
def test_matching_malformed_surfaces_stop_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"synthetic-source")
    path = tmp_path / "out" / "native-audio-budget.json"
    budget = start_budget(source)
    first = budget.consume("moss", MOSS_MODEL, 1_000, 2_000)
    budget.finish_attempt(first, status="OBSERVED")
    _persist(source, path)
    observe = native.build_native_foreign_witness(
        source_media=source, output_dir=path.parent, provider="moss",
    )
    if defect == "pending":
        budget.consume("moss", MOSS_MODEL, 3_000, 4_000)
        _persist(source, path)
    original_snapshot = budget.snapshot
    original = original_snapshot()
    malformed = deepcopy(original)
    if defect == "unknown":
        malformed["unknown_fixture_field"] = "not-a-real-event"
    elif defect == "total":
        malformed["total_audio_ms"] += 1
    elif defect == "boolean":
        malformed["attempt_count"] = True
    elif defect == "attempt_id":
        malformed["attempts"][0]["attempt_id"] = 7
    else:
        malformed["pending_attempt_count"] = 0
    packet = json.loads(path.read_text())
    packet["budget"] = malformed
    packet["receipt_sha256"] = receipts.receipt_sha256(packet)
    path.write_text(json.dumps(packet))
    before = path.read_bytes()
    monkeypatch.setattr(budget, "snapshot", lambda: deepcopy(malformed))
    entered = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        entered.append("forbidden-media-or-provider")
        raise AssertionError("invalid budget reached extraction or observation")

    monkeypatch.setattr(native, "_extract_exact_mp3", forbidden)
    monkeypatch.setattr(native, "observe_secondary", forbidden)
    with pytest.raises(receipts.BudgetReceiptPersistenceError):
        observe(start_ms=10_000, end_ms=11_000)
    assert entered == []
    assert path.read_bytes() == before
    assert original_snapshot() == original


def test_pending_finalization_and_append_remain_valid(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"synthetic-source")
    path = tmp_path / "native-audio-budget.json"
    budget = start_budget(source)
    first = budget.consume("moss", MOSS_MODEL, 1_000, 2_000)
    _persist(source, path)
    budget.finish_attempt(first, status="OBSERVED")
    final = _persist(source, path)
    assert final["budget"]["attempts"][0]["status"] == "OBSERVED"
    second = budget.consume("moss", MOSS_MODEL, 3_000, 4_000)
    budget.finish_attempt(second, status="FAILED", reason_code="SYNTHETIC_FAILURE")
    appended = _persist(source, path)
    assert appended["budget"]["attempts"][0] == final["budget"]["attempts"][0]
    assert appended["budget"]["attempt_count"] == 2
    before = path.read_bytes()
    assert _persist(source, path) == appended
    assert path.read_bytes() == before


def test_initial_receipt_failure_stops_before_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "synthetic-source.bin"
    source.write_bytes(b"synthetic-source")
    entered = []

    def unavailable(**_kwargs: object) -> None:
        raise receipts.BudgetReceiptPersistenceError("synthetic initial failure")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        entered.append("audio")
        raise AssertionError("initial persistence failure reached audio")

    monkeypatch.setattr(native, "_persist_budget_receipt", unavailable)
    monkeypatch.setattr(native, "_extract_exact_mp3", forbidden)
    monkeypatch.setattr(native, "observe_secondary", forbidden)
    with pytest.raises(receipts.BudgetReceiptPersistenceError):
        native.build_native_foreign_witness(
            source_media=source, output_dir=tmp_path / "out", provider="moss",
        )
    assert entered == []
