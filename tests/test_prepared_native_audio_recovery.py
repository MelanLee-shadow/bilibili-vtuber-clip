import hashlib
import json

import pytest

from src.autoslice.native_audio_budget_receipt import persist_native_audio_budget_receipt
from src.autoslice.prepared_native_audio_recovery import inherit_prepared_native_audio
from src.autoslice.subtitle_audio_evidence import CONTRACT, _digest
from src.autoslice.supplement_audio_budget import get_budget, start_budget


def _seed(tmp_path, *, enforced=False):
    parent = tmp_path / "old-output"
    parent.mkdir()
    source = parent / "padded.mp4"
    source.write_bytes(b"unique source")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    budget = start_budget(source, max_windows=1 if enforced else 3, enforce_limits=enforced)
    for index, provider in enumerate(("mai", "moss")):
        model = {"mai": "MAI-Transcribe-2", "moss": "moss-transcribe-diarize-pro"}[provider]
        token = budget.consume(provider, model, 1000, 2500)
        budget.finish_attempt(token, status="OBSERVED")
        job = parent / "native-exact" / provider / f"{sha[:20]}-1000-2500"
        job.mkdir(parents=True)
        audio = job / "input.mp3"
        audio.write_bytes(b"audio" + bytes([index]))
        binding = {
            "contract": CONTRACT,
            "provider": provider,
            "model": model,
            "input_audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            "duration_ms": 1500,
            "candidate_exposure": "none",
        }
        evidence = {**binding, "status": "OBSERVED", "raw_response": {"text": "fixture"}}
        audio.with_suffix(f".audio-evidence.{provider}.json").write_text(
            json.dumps(
                {"binding": binding, "evidence": evidence, "evidence_sha256": _digest(evidence)}
            )
        )
    persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=sha,
        receipt_path=parent / "native-audio-budget.json",
    )
    frozen = {str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()}
    start_budget(source, max_windows=1 if enforced else 3, enforce_limits=enforced)
    return parent, source, frozen


def test_new_output_retry_shares_monotonic_source_account(tmp_path, monkeypatch):
    parent, source, frozen = _seed(tmp_path)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    first = inherit_prepared_native_audio(
        cid="different_candidate", parent=parent, source=source, runtime_root=tmp_path
    )
    budget = get_budget(source)
    assert budget.snapshot()["attempt_count"] == 2
    assert budget.snapshot()["total_audio_ms"] == 3000
    assert budget.snapshot()["enforce_limits"] is False
    budget.record_cache_hit(provider="mai", model="MAI-Transcribe-2", start_ms=1000, end_ms=2500)
    persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=first.budget_receipt,
    )
    before = budget.snapshot()
    start_budget(source)  # actual next-process shape, empty in-memory registry entry
    second = inherit_prepared_native_audio(
        cid="different_candidate", parent=parent, source=source, runtime_root=tmp_path
    )
    assert first == second and get_budget(source).snapshot() == before
    assert {
        str(p.relative_to(parent)): p.read_bytes() for p in parent.rglob("*") if p.is_file()
    } == frozen
    assert len(json.loads((first.cache_root / "parent.json").read_bytes())["parents"]) == 1


def test_repeated_current_account_read_does_not_rewrite_original_state(tmp_path, monkeypatch):
    from src.autoslice import prepared_native_audio_recovery as recovery
    from src.autoslice import native_audio_budget_receipt as receipts

    parent, source, _ = _seed(tmp_path)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    state = inherit_prepared_native_audio(cid="candidate", parent=parent, source=source, runtime_root=tmp_path)
    files = [p for p in state.cache_root.rglob('*') if p.is_file()]
    before = {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in files}
    start_budget(source)  # Restarted ordinary producer: restore the same history.
    def no_write(*args, **kwargs):
        pytest.fail('an unchanged native account was rewritten')
    monkeypatch.setattr(recovery, '_write_json_atomic', no_write)
    monkeypatch.setattr(recovery, 'atomic_write_bytes', no_write)
    monkeypatch.setattr(receipts, '_write_atomic', no_write)
    assert inherit_prepared_native_audio(cid="candidate", parent=parent, source=source, runtime_root=tmp_path) == state
    assert get_budget(source).snapshot()['attempt_count'] == 2
    assert {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in files} == before


@pytest.mark.parametrize(
    "damage",
    [
        "missing_budget",
        "bad_budget",
        "missing_cache",
        "cache_audio",
        "cache_raw",
        "canonical_cache",
        "canonical_ledger",
    ],
)
def test_native_parent_or_account_drift_never_turns_into_fresh_allowance(
    tmp_path, monkeypatch, damage
):
    parent, source, _ = _seed(tmp_path)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    state = inherit_prepared_native_audio(
        cid="candidate", parent=parent, source=source, runtime_root=tmp_path
    )
    if damage == "missing_budget":
        (parent / "native-audio-budget.json").unlink()
    elif damage == "bad_budget":
        (parent / "native-audio-budget.json").write_text("{}")
    elif damage == "missing_cache":
        next((parent / "native-exact").rglob("*.json")).unlink()
    elif damage == "cache_audio":
        next((parent / "native-exact").rglob("input.mp3")).write_bytes(b"wrong source audio")
    elif damage == "cache_raw":
        next((parent / "native-exact").rglob("*.json")).write_text("{}")
    elif damage == "canonical_cache":
        next((state.cache_root / "native-exact").rglob("input.mp3")).write_bytes(
            b"wrong copied audio"
        )
    else:
        state.budget_receipt.unlink()
    before = get_budget(source).snapshot()
    with pytest.raises((ValueError, OSError)):
        inherit_prepared_native_audio(
            cid="candidate", parent=parent, source=source, runtime_root=tmp_path
        )
    assert get_budget(source).snapshot() == before


def test_legitimate_member_without_native_history_starts_at_zero(tmp_path, monkeypatch):
    parent = tmp_path / "no-native"
    parent.mkdir()
    source = parent / "padded.mp4"
    source.write_bytes(b"other untouched member")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    state = inherit_prepared_native_audio(
        cid="other", parent=parent, source=source, runtime_root=tmp_path
    )
    assert get_budget(source).snapshot()["attempt_count"] == 0
    assert not state.budget_receipt.exists()


def test_dangling_parent_budget_is_damage_not_a_legal_empty_member(tmp_path, monkeypatch):
    parent = tmp_path / "damaged"
    parent.mkdir()
    source = parent / "padded.mp4"
    source.write_bytes(b"source")
    (parent / "native-audio-budget.json").symlink_to(parent / "missing.json")
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    with pytest.raises(ValueError, match="symlink"):
        inherit_prepared_native_audio(
            cid="candidate", parent=parent, source=source, runtime_root=tmp_path
        )


@pytest.mark.parametrize("enforced", [False, True])
def test_new_window_retains_original_soft_or_enforced_policy(tmp_path, monkeypatch, enforced):
    from src.autoslice.supplement_audio_budget import BudgetExceeded

    parent, source, _ = _seed(tmp_path, enforced=enforced)
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))
    state = inherit_prepared_native_audio(
        cid="candidate", parent=parent, source=source, runtime_root=tmp_path
    )
    budget = get_budget(source)
    assert budget.snapshot()["attempt_count"] == 2
    if enforced:
        with pytest.raises(BudgetExceeded):
            budget.consume("mai", "MAI-Transcribe-2", 5000, 6000)
        assert budget.snapshot()["attempt_count"] == 2
        assert budget.snapshot()["refusal_count"] == 1
    else:
        token = budget.consume("mai", "MAI-Transcribe-2", 5000, 6000)
        budget.finish_attempt(token, status="FAILED", reason_code="TEST_TRANSPORT_FAILURE")
        assert budget.snapshot()["attempt_count"] == 3
        assert budget.snapshot()["total_audio_ms"] == 4000
    persist_native_audio_budget_receipt(
        source_media=source,
        source_media_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        receipt_path=state.budget_receipt,
    )
    expected = budget.snapshot()
    start_budget(source, max_windows=1 if enforced else 3, enforce_limits=enforced)
    inherit_prepared_native_audio(
        cid="candidate", parent=parent, source=source, runtime_root=tmp_path
    )
    assert get_budget(source).snapshot() == expected
