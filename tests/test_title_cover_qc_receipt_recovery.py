"""Crash-safe occupied canonical title/cover QC receipt recovery tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import run_title_cover_joint_qc as qc
from src.autoslice import title_cover_qc_receipt_recovery as recovery
from src.autoslice.title_cover_qc_locator_successor import (
    build_title_cover_qc_locator_successor,
)
from src.autoslice.title_cover_qc_receipt_recovery import (
    STATUS_VERIFIED,
    TitleCoverQcRecoveryError,
    plan_title_cover_qc_receipt_recovery,
    recover_title_cover_qc_receipt,
)


CANDIDATE = "auto_fixture_qc_recovery"
TITLE = "【李豆沙】旧回执占位时原子恢复"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha(path.read_bytes())


def _inventory(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            _sha_file(path),
            path.stat().st_size,
            path.stat().st_mode & 0o7777,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, mode)


def _package(root: Path, *, cover_bytes: bytes) -> Path:
    root.mkdir(parents=True)
    os.chmod(root, 0o700)
    (root / "clip.mp4").write_bytes(b"fixture-video")
    cover = root / "clip.cover.png"
    cover.write_bytes(cover_bytes)
    reference = root / "evidence" / f"{CANDIDATE}.reference.png"
    reference.parent.mkdir(mode=0o700)
    reference.write_bytes(b"fixture-reference")
    record = {"story_contract": {"candidate_id": CANDIDATE}}
    publish = {
        "title": TITLE,
        "cover_generation": {
            "final_cover": cover.name,
            "final_cover_sha256": "sha256:" + _sha_file(cover),
        },
    }
    _write_json(root / "clip.record.json", record)
    _write_json(root / "clip.publish.json", publish)
    _write_json(
        root / "review_manifest.json",
        {
            "items": [
                {
                    "candidate_id": CANDIDATE,
                    "title": TITLE,
                    "video": "clip.mp4",
                    "cover": cover.name,
                    "record": "clip.record.json",
                    "publish_json": "clip.publish.json",
                }
            ]
        },
    )
    return reference


def _receipt(root: Path) -> Path:
    cover = root / "clip.cover.png"
    reference = root / "evidence" / f"{CANDIDATE}.reference.png"
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Synthetic receipt; no provider call occurred in this test.",
        "pass": True,
    }

    def probe(path: Path, _prompt: str) -> dict[str, object]:
        return {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "status": "OBSERVED",
            "model": "fixture-model",
            "image_path": str(path.resolve()),
            "image_sha256": _sha_file(path),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "response_sha256": "fixture-response-sha",
            "reference_image": {
                "image_path": str(reference.resolve()),
                "image_sha256": _sha_file(reference),
                "frame_sha256": "fixture-reference-frame",
            },
        }

    value = qc.build_joint_qc_receipt(
        cover_path=cover,
        title=TITLE,
        candidate_id=CANDIDATE,
        image_probe=probe,
    )
    value["source_reference"] = {
        "schema_version": "joint-qc-package-source-reference.v1",
        "candidate_id": CANDIDATE,
        "cover_sha256": value["cover_sha256"],
        "package_binding_sha256": "sha256:" + "a" * 64,
        "reference_path": reference.relative_to(root).as_posix(),
        "reference_sha256": "sha256:" + _sha_file(reference),
    }
    path = root / f"{CANDIDATE}.title-cover-joint-qc.json"
    _write_json(path, value)
    return path


@pytest.fixture
def packages(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    superseded = tmp_path / "superseded"
    _package(source, cover_bytes=b"current-cover")
    _package(destination, cover_bytes=b"current-cover")
    _package(superseded, cover_bytes=b"old-cover")
    source_receipt = _receipt(source)
    superseded_receipt = _receipt(superseded)
    canonical = destination / f"{CANDIDATE}.title-cover-joint-qc.json"
    canonical.write_bytes(superseded_receipt.read_bytes())
    os.chmod(canonical, 0o600)
    return {
        "source": source,
        "destination": destination,
        "superseded": superseded,
        "source_receipt": source_receipt,
        "superseded_receipt": superseded_receipt,
        "canonical": canonical,
    }


def _journal_states(path: Path) -> list[str]:
    return [
        json.loads(line)["state"]
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _stable_package_files(root: Path) -> dict[str, tuple[str, int, int]]:
    ignored_prefixes = (
        "verification/title-cover-qc-recovery/",
        f".{CANDIDATE}.title-cover-joint-qc.recovery-",
    )
    ignored_exact = {f"{CANDIDATE}.title-cover-joint-qc.json"}
    return {
        key: value
        for key, value in _inventory(root).items()
        if key not in ignored_exact
        and not any(key.startswith(prefix) for prefix in ignored_prefixes)
    }


def test_dry_run_validates_without_writing(packages: dict[str, object]) -> None:
    destination = packages["destination"]
    before = _inventory(destination)

    result = plan_title_cover_qc_receipt_recovery(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
    )

    assert result["status"] == "WOULD_RECOVER"
    assert result["candidate_id"] == CANDIDATE
    assert result["provider_calls"] == 0
    assert result["writes"] == 0
    assert _inventory(destination) == before
    assert not (destination / "verification").exists()


def test_recovery_archives_old_bytes_and_installs_replayable_successor(
    packages: dict[str, object],
) -> None:
    destination = packages["destination"]
    canonical = packages["canonical"]
    old_bytes = canonical.read_bytes()
    stable_before = _stable_package_files(destination)

    result = recover_title_cover_qc_receipt(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
    )

    assert result["status"] == STATUS_VERIFIED
    assert result["provider_calls"] == 0
    assert result["cache_reused"] is False
    assert Path(result["archive"]).read_bytes() == old_bytes
    stored = json.loads(canonical.read_text(encoding="utf-8"))
    assert qc.reuse_valid_qc(destination, TITLE, canonical) == stored
    assert stored["cover_sha256"] == "sha256:" + _sha_file(
        destination / "clip.cover.png"
    )
    assert stored["locator_successor"]["provider_calls"] == 0
    assert canonical.stat().st_mode & 0o7777 == 0o600
    assert _journal_states(Path(result["journal"])) == [
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "VERIFIED",
    ]
    recovery_receipt = json.loads(
        Path(result["recovery_receipt"]).read_text(encoding="utf-8")
    )
    assert recovery_receipt["status"] == STATUS_VERIFIED
    assert recovery_receipt["provider_calls"] == 0
    assert recovery_receipt["upload_allowed"] is False
    assert _stable_package_files(destination) == stable_before

    replay = recover_title_cover_qc_receipt(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
    )
    assert replay["status"] == STATUS_VERIFIED
    assert replay["cache_reused"] is True
    assert replay["terminal_row_sha256"] == result["terminal_row_sha256"]
    assert _journal_states(Path(result["journal"])) == [
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "VERIFIED",
    ]


def test_crash_after_atomic_exchange_resumes_without_second_projection(
    packages: dict[str, object],
) -> None:
    class ProcessDeath(BaseException):
        pass

    def crash(point: str) -> None:
        if point == "after_exchange":
            raise ProcessDeath()

    with pytest.raises(ProcessDeath):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
            checkpoint=crash,
        )

    destination = packages["destination"]
    canonical = packages["canonical"]
    assert qc.reuse_valid_qc(destination, TITLE, canonical)["pass"] is True
    journal = next(
        destination.glob(
            "verification/title-cover-qc-recovery/"
            f"{CANDIDATE}/*/journal.jsonl"
        )
    )
    assert _journal_states(journal) == ["PLANNED", "INSTALL_INTENT"]

    resumed = recover_title_cover_qc_receipt(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
    )
    assert resumed["status"] == STATUS_VERIFIED
    assert resumed["cache_reused"] is True
    assert _journal_states(journal) == [
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "VERIFIED",
    ]
    assert not list(destination.glob(f".{CANDIDATE}.*.swap"))


def test_target_replay_failure_rolls_back_exact_old_canonical(
    packages: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = packages["canonical"]
    old_bytes = canonical.read_bytes()
    old_sha = _sha(old_bytes)
    original = recovery._reuse_valid_qc

    def fail_installed(root: Path, title: str, receipt_path: Path):
        if Path(root).resolve() == Path(packages["destination"]).resolve() and (
            _sha_file(Path(receipt_path)) != old_sha
        ):
            raise ValueError("synthetic installed target replay failure")
        return original(root, title, receipt_path)

    monkeypatch.setattr(recovery, "_reuse_valid_qc", fail_installed)
    with pytest.raises(
        TitleCoverQcRecoveryError,
        match="superseded receipt restored",
    ):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
        )

    assert canonical.read_bytes() == old_bytes
    journal = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/"
            f"{CANDIDATE}/*/journal.jsonl"
        )
    )
    assert _journal_states(journal) == [
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "ROLLBACK_INTENT",
        "ROLLED_BACK",
    ]
    assert not list(Path(packages["destination"]).glob(f".{CANDIDATE}.*.swap"))
    with pytest.raises(TitleCoverQcRecoveryError, match="terminal ROLLED_BACK"):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
        )


def test_source_drift_after_exchange_restores_superseded_receipt(
    packages: dict[str, object],
) -> None:
    class ProcessDeath(BaseException):
        pass

    old_bytes = Path(packages["canonical"]).read_bytes()

    def crash(point: str) -> None:
        if point == "after_exchange":
            raise ProcessDeath()

    with pytest.raises(ProcessDeath):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
            checkpoint=crash,
        )

    source_receipt = Path(packages["source_receipt"])
    changed = json.loads(source_receipt.read_text(encoding="utf-8"))
    changed["generated_at"] = "2099-01-01T00:00:00Z"
    _write_json(source_receipt, changed)

    with pytest.raises(
        TitleCoverQcRecoveryError,
        match="plan evidence is no longer valid",
    ):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=source_receipt,
            destination_package_root=packages["destination"],
            title=TITLE,
        )

    assert Path(packages["canonical"]).read_bytes() == old_bytes
    journal = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/"
            f"{CANDIDATE}/*/journal.jsonl"
        )
    )
    assert _journal_states(journal)[-2:] == ["ROLLBACK_INTENT", "ROLLED_BACK"]


def test_third_party_canonical_drift_is_preserved_and_blocks_exchange(
    packages: dict[str, object],
) -> None:
    unknown = b'{"third_party":true}\n'

    def mutate(point: str) -> None:
        if point != "after_planned":
            return
        canonical = Path(packages["canonical"])
        temporary = canonical.with_name("third-party.json")
        temporary.write_bytes(unknown)
        os.chmod(temporary, 0o600)
        os.replace(temporary, canonical)

    with pytest.raises(TitleCoverQcRecoveryError, match="unowned bytes"):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
            checkpoint=mutate,
        )

    assert Path(packages["canonical"]).read_bytes() == unknown
    operation = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/" f"{CANDIDATE}/*"
        )
    )
    archive = operation / "superseded-receipt.json"
    assert archive.read_bytes() == Path(packages["superseded_receipt"]).read_bytes()
    assert _journal_states(operation / "journal.jsonl")[-1] == "BLOCKED_DRIFT"


def test_journal_tamper_blocks_resume_without_touching_canonical(
    packages: dict[str, object],
) -> None:
    class ProcessDeath(BaseException):
        pass

    old_bytes = Path(packages["canonical"]).read_bytes()

    def crash(point: str) -> None:
        if point == "after_planned":
            raise ProcessDeath()

    with pytest.raises(ProcessDeath):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
            checkpoint=crash,
        )

    journal = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/"
            f"{CANDIDATE}/*/journal.jsonl"
        )
    )
    row = json.loads(journal.read_text(encoding="utf-8"))
    row["details"]["provider_calls"] = 99
    journal.write_text(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(journal, 0o600)

    with pytest.raises(TitleCoverQcRecoveryError, match="binding drifts"):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
        )
    assert Path(packages["canonical"]).read_bytes() == old_bytes


def test_already_valid_canonical_is_a_noop(packages: dict[str, object]) -> None:
    destination = Path(packages["destination"])
    canonical = Path(packages["canonical"])
    projected = build_title_cover_qc_locator_successor(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
        projected_at="2026-09-30T00:00:00Z",
    )
    canonical.write_text(
        json.dumps(projected, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    os.chmod(canonical, 0o600)
    before = _inventory(destination)

    result = recover_title_cover_qc_receipt(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=destination,
        title=TITLE,
    )

    assert result["status"] == "ALREADY_VALID"
    assert result["provider_calls"] == 0
    assert _inventory(destination) == before
    assert not (destination / "verification").exists()


def test_crash_after_plan_before_first_journal_row_resumes(
    packages: dict[str, object],
) -> None:
    class ProcessDeath(BaseException):
        pass

    old_bytes = Path(packages["canonical"]).read_bytes()

    def crash(point: str) -> None:
        if point == "after_plan_persisted":
            raise ProcessDeath()

    with pytest.raises(ProcessDeath):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
            checkpoint=crash,
        )

    operation = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/" f"{CANDIDATE}/*"
        )
    )
    assert (operation / "plan.json").is_file()
    assert not (operation / "journal.jsonl").exists()
    assert not (operation / "superseded-receipt.json").exists()
    assert not (operation / "successor-receipt.json").exists()
    assert Path(packages["canonical"]).read_bytes() == old_bytes

    resumed = recover_title_cover_qc_receipt(
        source_package_root=packages["source"],
        source_receipt_path=packages["source_receipt"],
        destination_package_root=packages["destination"],
        title=TITLE,
    )

    assert resumed["status"] == STATUS_VERIFIED
    assert resumed["cache_reused"] is True
    assert (operation / "superseded-receipt.json").read_bytes() == old_bytes
    assert _journal_states(operation / "journal.jsonl") == [
        "PLANNED",
        "INSTALL_INTENT",
        "INSTALLED",
        "VERIFIED",
    ]
    first_row = json.loads(
        (operation / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_row["details"]["resumed_before_first_journal_row"] is True


def test_atomic_exchange_race_restores_third_party_canonical(
    packages: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_exchange = recovery._exchange_siblings
    third_party = b'{"third_party":"won-before-exchange"}\n'
    injected = {"done": False}

    def exchange_with_race(parent: Path, first: str, second: str) -> None:
        if not injected["done"]:
            injected["done"] = True
            canonical = parent / first
            temporary = parent / ".third-party-race.json"
            temporary.write_bytes(third_party)
            os.chmod(temporary, 0o600)
            os.replace(temporary, canonical)
        original_exchange(parent, first, second)

    monkeypatch.setattr(recovery, "_exchange_siblings", exchange_with_race)

    with pytest.raises(
        TitleCoverQcRecoveryError,
        match="encountered unowned bytes",
    ):
        recover_title_cover_qc_receipt(
            source_package_root=packages["source"],
            source_receipt_path=packages["source_receipt"],
            destination_package_root=packages["destination"],
            title=TITLE,
        )

    canonical = Path(packages["canonical"])
    assert injected["done"] is True
    assert canonical.read_bytes() == third_party
    operation = next(
        Path(packages["destination"]).glob(
            "verification/title-cover-qc-recovery/" f"{CANDIDATE}/*"
        )
    )
    assert (operation / "superseded-receipt.json").read_bytes() == Path(
        packages["superseded_receipt"]
    ).read_bytes()
    assert _journal_states(operation / "journal.jsonl") == [
        "PLANNED",
        "INSTALL_INTENT",
        "BLOCKED_DRIFT",
    ]
    assert not list(
        Path(packages["destination"]).glob(f".{CANDIDATE}.*.swap")
    )
