from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

import src.autoslice.content_bound_request_ledger as ledger_module
from src.autoslice.content_bound_request_ledger import (
    ContentBoundRequestLedger,
    RequestLedgerError,
)


REQUEST_ID_A = "67ee89ba-7050-4c04-a3d7-ac61a63499b3"
REQUEST_ID_B = "775601c4-0417-4c0c-9da9-9e4380de5fe1"


def _binding(audio_sha: str = "a" * 64) -> dict:
    return {
        "provider": "doubao_flash",
        "resource_id": "volc.bigasr.auc_turbo",
        "input_audio_sha256": audio_sha,
        "request_config_sha256": "c" * 64,
        "candidate_exposure": "none",
    }


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "ledger"
    root.mkdir(mode=0o700)
    return root


def test_prepare_is_content_bound_and_reuses_one_request_id(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())

    first = ledger.prepare(request_id=REQUEST_ID_A)
    second = ledger.prepare(request_id=REQUEST_ID_B)

    assert first == second
    assert second["request_id"] == REQUEST_ID_A
    assert second["state"] == "PREPARED"
    assert second["binding"] == _binding()
    assert second["record_sha256"]
    assert ledger.path.name == f"{ledger.binding_sha256}.json"

    other = ContentBoundRequestLedger(ledger.root, _binding("d" * 64))
    assert other.path != ledger.path
    assert other.prepare(request_id=REQUEST_ID_B)["request_id"] == REQUEST_ID_B


def test_ambiguous_submit_cannot_return_to_prepared_or_resubmit(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    ledger.prepare(request_id=REQUEST_ID_A)
    ledger.transition("SUBMIT_DISPATCHED", event="SUBMIT_INTENT_PERSISTED")
    ledger.transition(
        "SUBMIT_AMBIGUOUS",
        event="SUBMIT_TRANSPORT_AMBIGUOUS",
        reason_code="DOUBAO_SUBMIT_TRANSPORT_ERROR",
    )

    with pytest.raises(RequestLedgerError) as exc_info:
        ledger.transition("SUBMIT_DISPATCHED", event="SECOND_SUBMIT")

    assert exc_info.value.reason_code == "REQUEST_LEDGER_TRANSITION_INVALID"
    resumed = ledger.prepare(request_id=REQUEST_ID_B)
    assert resumed["request_id"] == REQUEST_ID_A
    assert resumed["state"] == "SUBMIT_AMBIGUOUS"

    resumed = ledger.transition(
        "PROCESSING",
        event="QUERY_RESULT",
        metadata={"provider_status_code": 20000001},
    )
    assert resumed["state"] == "PROCESSING"


def test_terminal_state_is_immutable(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    ledger.prepare(request_id=REQUEST_ID_A)
    ledger.transition("SUBMIT_DISPATCHED", event="SUBMIT_INTENT_PERSISTED")
    ledger.transition("SUBMITTED", event="SUBMIT_ACCEPTED")
    ledger.transition("RESULT_AVAILABLE", event="QUERY_SUCCESS")
    finished = ledger.transition(
        "COMPLETED",
        event="RESULT_VALIDATED",
        metadata={"response_sha256": "d" * 64, "result_sha256": "e" * 64},
    )

    assert finished["state"] == "COMPLETED"
    with pytest.raises(RequestLedgerError) as exc_info:
        ledger.transition("FAILED", event="LATE_FAILURE")
    assert exc_info.value.reason_code == "REQUEST_LEDGER_TRANSITION_INVALID"


def test_raw_urls_and_secret_fields_are_rejected(tmp_path: Path):
    root = _root(tmp_path)
    with pytest.raises(RequestLedgerError) as raw_url:
        ContentBoundRequestLedger(root, {**_binding(), "audio_url": "https://example.test/a.mp3"})
    assert raw_url.value.reason_code == "REQUEST_LEDGER_SECRET_FIELD"

    with pytest.raises(RequestLedgerError) as secret:
        ContentBoundRequestLedger(root, {**_binding(), "api_key": "not-for-disk"})
    assert secret.value.reason_code == "REQUEST_LEDGER_SECRET_FIELD"

    ledger = ContentBoundRequestLedger(root, _binding())
    ledger.prepare(request_id=REQUEST_ID_A)
    with pytest.raises(RequestLedgerError) as metadata_url:
        ledger.transition(
            "SUBMIT_DISPATCHED",
            event="BAD_EVENT",
            metadata={"location": "https://example.test/a.mp3"},
        )
    assert metadata_url.value.reason_code == "REQUEST_LEDGER_SECRET_VALUE"


def test_tamper_and_symlink_fail_closed_without_overwrite(tmp_path: Path):
    root = _root(tmp_path)
    ledger = ContentBoundRequestLedger(root, _binding())
    ledger.prepare(request_id=REQUEST_ID_A)
    original = json.loads(ledger.path.read_text(encoding="utf-8"))
    original["state"] = "COMPLETED"
    ledger.path.write_text(json.dumps(original), encoding="utf-8")
    ledger.path.chmod(0o600)

    with pytest.raises(RequestLedgerError) as tampered:
        ledger.snapshot()
    assert tampered.value.reason_code == "REQUEST_LEDGER_HASH_MISMATCH"
    assert json.loads(ledger.path.read_text(encoding="utf-8"))["state"] == "COMPLETED"

    ledger.path.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    ledger.path.symlink_to(target)
    with pytest.raises(RequestLedgerError) as symlink:
        ledger.prepare(request_id=REQUEST_ID_A)
    assert symlink.value.reason_code == "REQUEST_LEDGER_READ_FAILED"
    assert target.read_text(encoding="utf-8") == "{}"


def test_concurrent_prepare_creates_exactly_one_request_identity(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    request_ids = [REQUEST_ID_A, REQUEST_ID_B] * 8

    def prepare(request_id: str) -> str:
        return ledger.prepare(request_id=request_id)["request_id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(prepare, request_ids))

    assert len(set(observed)) == 1
    snapshot = ledger.snapshot()
    assert snapshot["revision"] == 1
    assert snapshot["events"] == [
        {
            "sequence": 1,
            "state": "PREPARED",
            "event": "REQUEST_ID_PREPARED",
            "reason_code": None,
            "metadata": {},
        }
    ]


def _rewrite_and_reseal(ledger: ContentBoundRequestLedger, mutate) -> None:
    value = json.loads(ledger.path.read_text(encoding="utf-8"))
    mutate(value)
    unsigned = dict(value)
    unsigned.pop("record_sha256", None)
    value["record_sha256"] = ledger_module._digest(unsigned)
    ledger.path.write_text(json.dumps(value), encoding="utf-8")
    ledger.path.chmod(0o600)


def test_resealed_illegal_history_transition_is_rejected(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    ledger.prepare(request_id=REQUEST_ID_A)

    def mutate(value: dict) -> None:
        value["state"] = "COMPLETED"
        value["revision"] = 2
        value["events"].append(
            {
                "sequence": 2,
                "state": "COMPLETED",
                "event": "ILLEGAL_DIRECT_COMPLETION",
                "reason_code": None,
                "metadata": {},
            }
        )

    _rewrite_and_reseal(ledger, mutate)
    with pytest.raises(RequestLedgerError) as exc_info:
        ledger.snapshot()
    assert exc_info.value.reason_code == "REQUEST_LEDGER_TRANSITION_INVALID"


def test_resealed_noncanonical_initial_event_is_rejected(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    ledger.prepare(request_id=REQUEST_ID_A)

    def mutate(value: dict) -> None:
        value["events"][0]["event"] = "FORGED_INITIAL_EVENT"

    _rewrite_and_reseal(ledger, mutate)
    with pytest.raises(RequestLedgerError) as exc_info:
        ledger.snapshot()
    assert exc_info.value.reason_code == "REQUEST_LEDGER_STATE_INVALID"


def test_resealed_terminal_history_continuation_is_rejected(tmp_path: Path):
    ledger = ContentBoundRequestLedger(_root(tmp_path), _binding())
    ledger.prepare(request_id=REQUEST_ID_A)
    ledger.transition("SUBMIT_DISPATCHED", event="SUBMIT_INTENT_PERSISTED")
    ledger.transition("FAILED", event="PROVIDER_FAILED")

    def mutate(value: dict) -> None:
        value["state"] = "PROCESSING"
        value["revision"] += 1
        value["events"].append(
            {
                "sequence": value["revision"],
                "state": "PROCESSING",
                "event": "ILLEGAL_POST_TERMINAL_EVENT",
                "reason_code": None,
                "metadata": {},
            }
        )

    _rewrite_and_reseal(ledger, mutate)
    with pytest.raises(RequestLedgerError) as exc_info:
        ledger.snapshot()
    assert exc_info.value.reason_code == "REQUEST_LEDGER_TRANSITION_INVALID"
