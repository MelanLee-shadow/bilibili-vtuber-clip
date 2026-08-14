"""Ivan 2026-07-13: GEMINI_KEY_BACKUP is PAID and must stay a last resort.

Free keys are the primary slicing keys. The paid key may fire only after the
complete free-key chain has already failed >= 3 recorded rounds for the same
work item, and never past the daily cap. The key value must never appear in
ledgers or manifests.
"""

import json
from pathlib import Path

import pytest

import src.autoslice.gemini_backup_policy as policy


@pytest.fixture()
def sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    monkeypatch.delenv("GEMINI_KEY_BACKUP", raising=False)
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP", raising=False)
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", raising=False)
    return tmp_path


def test_no_paid_key_configured_never_allows(sandbox: Path) -> None:
    allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=99)
    assert allowed is False
    assert reason == "PAID_KEY_NOT_CONFIGURED"


def test_strike_gate_requires_three_prior_rounds(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-secret")
    for expected_prior in (0, 1, 2):
        prior = policy.free_chain_strikes("item-a")
        assert prior == expected_prior
        policy.record_free_chain_failure("item-a")
        allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=prior)
        assert allowed is False
        assert reason.startswith("FREE_CHAIN_STRIKES_")
    # Fourth round: three complete failed rounds are on record.
    prior = policy.free_chain_strikes("item-a")
    assert prior == 3
    allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=prior)
    assert allowed is True
    assert reason == "FREE_CHAIN_STRIKES_3"


def test_strikes_are_per_item(sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-secret")
    for _ in range(4):
        policy.record_free_chain_failure("item-a")
    allowed, _ = policy.paid_attempt_allowed("item-b", prior_strikes=None)
    assert allowed is False  # item-b never failed


def test_optional_cap_enforced_only_when_configured(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-secret")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "2")
    for _ in range(3):
        policy.record_free_chain_failure("item-a")
    policy.record_paid_use("item-a", purpose="test")
    policy.record_paid_use("item-a", purpose="test")
    allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=3)
    assert allowed is False
    assert reason.startswith("PAID_DAILY_CAP_REACHED_")
    # Ivan 2026-07-13: no hard cap by default — unset env means uncapped.
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP")
    allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=3)
    assert allowed is True
    assert reason == "FREE_CHAIN_STRIKES_3"


def test_dev_exception_bypasses_strike_wait_not_ordering(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev exception (Ivan 2026-07-13): paid may fire the same round the
    free chain fails — the adapters still try every free key first, so
    ordering is preserved; only the multi-round wait is waived."""

    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-secret")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    allowed, reason = policy.paid_attempt_allowed("item-a", prior_strikes=0)
    assert allowed is True
    assert reason == "DEV_EXCEPTION"
    stamp = policy.record_paid_use("item-a", purpose="test")
    assert stamp["mode"] == "dev_exception"
    assert stamp["daily_cap"] is None


def test_exact_transcript_paid_stamp_binds_purpose_item_and_gate(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-secret")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    item_key = "a" * 64
    stamp = policy.record_paid_use(
        item_key, purpose=policy.CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE
    )
    assert policy.valid_paid_policy_stamp(
        stamp,
        expected_purpose=policy.CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE,
        expected_item_key=item_key,
    )
    for field, value in (
        ("purpose", "candidate_blind_audio_witness"),
        ("item_key", "b" * 64),
        ("key_tier", "free"),
    ):
        tampered = dict(stamp)
        tampered[field] = value
        assert not policy.valid_paid_policy_stamp(
            tampered,
            expected_purpose=policy.CANDIDATE_BLIND_EXACT_TRANSCRIPT_PURPOSE,
            expected_item_key=item_key,
        )


def test_ledger_and_stamp_never_contain_key_value(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "paid-secret-value-must-not-leak"
    monkeypatch.setenv("GEMINI_KEY_BACKUP", secret)
    for _ in range(3):
        policy.record_free_chain_failure("item-a")
    stamp = policy.record_paid_use("item-a", purpose="jingting_source_context")
    assert stamp["key_tier"] == "paid_backup"
    assert stamp["free_chain_strikes"] >= 3
    dumped = json.dumps(stamp)
    assert secret not in dumped
    for path in (sandbox / "state" / "gemini-paid-backup").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8")


def test_invalid_or_absent_cap_env_means_uncapped(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DAILY_CAP", "unlimited")
    assert policy.daily_cap() is None
    monkeypatch.delenv("GEMINI_PAID_BACKUP_DAILY_CAP")
    assert policy.daily_cap() is None
