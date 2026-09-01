from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.autoslice import qixi_post_correction_diagnostics as diagnostics


AUTHORITY_SHA = "sha256:" + "a" * 64


def _body() -> dict[str, object]:
    return {
        "schema_version": "qixi-public-surface-full-dry-run-diagnostic.v1",
        "deployed_commit": "b" * 40,
        "authority_sha256": AUTHORITY_SHA,
        "candidate_id": "auto_123655_771_844",
        "recording_date": "2026-08-17",
        "runtime_preimages": {},
        "matrix": {"schema_version": "qixi-public-surface-predicate-matrix.v1", "predicates": []},
        "stage_manifest": [],
        "provider_evidence": {
            "source_fact": {"attempt_status": "NOT_ATTEMPTED", "receipt_sha256s": []},
            "cover": {"attempt_status": "UNKNOWN", "receipt_sha256s": []},
        },
    }


def _write(root: Path, body: dict[str, object]) -> Path:
    digest = diagnostics.canonical_sha256(body)[7:]
    return diagnostics.write_failure_receipt(
        root=root, filename=f"diagnostic-{digest}.json", body=body
    )


def test_receipt_is_body_bound_create_only_and_exact_idempotent(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    receipt = _write(root, body)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert _write(root, body) == receipt
    payload = json.loads(receipt.read_text())
    assert payload.pop("receipt_sha256") == diagnostics.canonical_sha256(payload)
    with pytest.raises(RuntimeError, match="body-bound"):
        diagnostics.write_failure_receipt(root=root, filename="diagnostic-wrong.json", body=body)
    body["deployed_commit"] = "c" * 40
    with pytest.raises(RuntimeError, match="body-bound"):
        diagnostics.write_failure_receipt(
            root=root,
            filename=receipt.name,
            body=body,
        )


def test_receipt_rejects_foreign_or_broken_symlink_inventory(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    foreign = root / "foreign"
    foreign.symlink_to(root / "missing")
    with pytest.raises(RuntimeError, match="inventory"):
        _write(root, _body())


def test_receipt_recovers_only_a_complete_body_bound_pending(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    document = dict(body)
    digest = diagnostics.canonical_sha256(body)
    document["receipt_sha256"] = digest
    pending = root / f".{digest[7:]}.pending"
    pending.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(pending, 0o600)
    receipt = _write(root, body)
    assert receipt.is_file() and not pending.exists()
    bad = root / ("." + "f" * 64 + ".pending")
    bad.write_bytes(b"partial")
    os.chmod(bad, 0o600)
    with pytest.raises(RuntimeError, match="pending"):
        _write(root, {**body, "deployed_commit": "c" * 40})


def test_pending_source_swap_never_becomes_a_trusted_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    digest = diagnostics.canonical_sha256(body)[7:]
    pending = root / f".{digest}.pending"
    document = {**body, "receipt_sha256": "sha256:" + digest}
    pending.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(pending, 0o600)
    original_link = diagnostics.os.link

    def swap_source(*args: object, **kwargs: object) -> None:
        saved = root / "foreign-pending-preserved"
        os.replace(pending, saved)
        pending.write_bytes(b"foreign pending bytes")
        os.chmod(pending, 0o600)
        original_link(*args, **kwargs)

    monkeypatch.setattr(diagnostics.os, "link", swap_source)
    with pytest.raises(RuntimeError, match="ownership drifted"):
        _write(root, body)
    assert (root / "foreign-pending-preserved").read_bytes().startswith(b"{")
    assert pending.read_bytes() == b"foreign pending bytes"
    target = root / f"diagnostic-{digest}.json"
    assert not target.exists()
    with pytest.raises(RuntimeError, match="inventory"):
        _write(root, {**body, "deployed_commit": "c" * 40})


def test_root_path_swap_refuses_to_return_a_receipt_in_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    parked = candidate / "parked-original-root"
    original_recover = diagnostics._recover_pending

    def recover_then_swap(root_fd: int, name: str) -> None:
        original_recover(root_fd, name)
        os.replace(root, parked)
        root.mkdir(mode=0o700)

    monkeypatch.setattr(diagnostics, "_recover_pending", recover_then_swap)
    with pytest.raises(RuntimeError, match="path drifted"):
        _write(root, _body())
    assert not list(root.iterdir())
    assert any(path.name.startswith("diagnostic-") for path in parked.iterdir())


def test_pending_same_inode_content_swap_cannot_publish_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    digest = diagnostics.canonical_sha256(body)[7:]
    pending = root / f".{digest}.pending"
    document = {**body, "receipt_sha256": "sha256:" + digest}
    original_payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    pending.write_bytes(original_payload)
    os.chmod(pending, 0o600)
    original_link = diagnostics.os.link

    def mutate_same_inode(*args: object, **kwargs: object) -> None:
        pending.write_bytes(b"x" * len(original_payload))
        os.chmod(pending, 0o600)
        original_link(*args, **kwargs)

    monkeypatch.setattr(diagnostics.os, "link", mutate_same_inode)
    with pytest.raises(RuntimeError, match="ownership drifted"):
        _write(root, body)
    assert pending.read_bytes() == b"x" * len(original_payload)
    assert not (root / f"diagnostic-{digest}.json").exists()


def test_pending_phase_recovery_handles_prelink_and_postlink_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    digest = diagnostics.canonical_sha256(body)[7:]
    pending = root / f".{digest}.pending"
    original_recover = diagnostics._recover_pending

    def crash_before_link(*_args: object, **_kwargs: object) -> None:
        raise BaseException("crash after pending fsync")

    monkeypatch.setattr(diagnostics, "_recover_pending", crash_before_link)
    with pytest.raises(BaseException, match="pending fsync"):
        _write(root, body)
    assert pending.is_file()
    monkeypatch.setattr(diagnostics, "_recover_pending", original_recover)
    receipt = _write(root, body)
    assert receipt.is_file() and not pending.exists()

    body = {**body, "deployed_commit": "c" * 40}
    digest = diagnostics.canonical_sha256(body)[7:]
    pending_name = f".{digest}.pending"
    original_unlink = diagnostics.os.unlink

    def crash_after_link(path: str, *args: object, **kwargs: object) -> None:
        if path == pending_name:
            raise BaseException("crash after receipt link")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(diagnostics.os, "unlink", crash_after_link)
    with pytest.raises(BaseException, match="receipt link"):
        _write(root, body)
    assert (root / f"diagnostic-{digest}.json").is_file()
    assert (root / pending_name).is_file()
    monkeypatch.setattr(diagnostics.os, "unlink", original_unlink)
    assert _write(root, body).is_file()
    assert not (root / pending_name).exists()


def test_pending_recovers_a_real_partial_write_after_first_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    digest = diagnostics.canonical_sha256(body)[7:]
    pending = root / f".{digest}.pending"
    original_write = diagnostics.os.write
    writes = 0

    def write_prefix_then_crash(fd: int, payload: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            original_write(fd, payload[:29])
            os.fsync(fd)
            raise BaseException("crash after first pending write and fsync")
        return original_write(fd, payload)

    monkeypatch.setattr(diagnostics.os, "write", write_prefix_then_crash)
    with pytest.raises(BaseException, match="first pending write"):
        _write(root, body)
    assert pending.is_file() and 0 < pending.stat().st_size
    monkeypatch.setattr(diagnostics.os, "write", original_write)
    receipt = _write(root, body)
    assert receipt.is_file() and not pending.exists()


def test_pending_inventory_rejects_foreign_regular_and_broken_symlink(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    regular = root / ("." + "f" * 64 + ".pending")
    regular.write_bytes(b"foreign pending")
    os.chmod(regular, 0o600)
    with pytest.raises((RuntimeError, OSError)):
        _write(root, _body())
    regular.unlink()
    broken = root / ("." + "e" * 64 + ".pending")
    broken.symlink_to(root / "missing")
    with pytest.raises((RuntimeError, OSError)):
        _write(root, _body())


def test_current_digest_partial_prefix_is_rewritten_but_foreign_pending_is_preserved(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(mode=0o700)
    root = diagnostics.diagnostic_root(candidate_root=candidate, authority_sha256=AUTHORITY_SHA)
    body = _body()
    digest = diagnostics.canonical_sha256(body)[7:]
    pending = root / f".{digest}.pending"
    document = {**body, "receipt_sha256": "sha256:" + digest}
    expected = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    pending.write_bytes(expected[:31])
    os.chmod(pending, 0o600)
    receipt = _write(root, body)
    assert receipt.is_file() and not pending.exists()

    foreign_body = {**body, "deployed_commit": "c" * 40}
    foreign_digest = diagnostics.canonical_sha256(foreign_body)[7:]
    foreign = root / f".{foreign_digest}.pending"
    foreign.write_bytes(b"not-a-prefix")
    os.chmod(foreign, 0o600)
    with pytest.raises(RuntimeError, match="pending"):
        _write(root, foreign_body)
    assert foreign.read_bytes() == b"not-a-prefix"
