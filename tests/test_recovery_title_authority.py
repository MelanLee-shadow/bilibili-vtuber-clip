import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.recovery_title_authority import (
    ROOT,
    RecoveryTitleAuthorityError,
    build_recovery_title_authority,
    validate_recovery_title_authority,
)


CANDIDATE_ID = "auto_193450_1475_1543"
EVIDENCE = (
    ROOT
    / "reports/authorized_uploads/2026-07-22-v8-final"
    / f"{CANDIDATE_ID}.public_verify.json"
)
EVIDENCE_SHA256 = (
    "sha256:c3af4c8485ca2cf3f17c1d4a660c53cd4f1d23a3f9d254e77924d9875a07d771"
)


def _authority():
    return build_recovery_title_authority(
        candidate_id=CANDIDATE_ID,
        evidence_path=EVIDENCE,
        expected_evidence_sha256=EVIDENCE_SHA256,
    )


def _resign(authority: dict[str, object]) -> None:
    unsigned = {
        key: value
        for key, value in authority.items()
        if key != "authority_sha256"
    }
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    authority["authority_sha256"] = (
        "sha256:" + hashlib.sha256(canonical).hexdigest()
    )


def test_build_recovery_title_authority_replays_all_public_surfaces():
    authority = _authority()

    assert authority["schema_version"] == (
        "recovery-public-title-authority.v1"
    )
    assert authority["candidate_id"] == CANDIDATE_ID
    assert authority["bvid"] == "BV1acg46XEjx"
    assert authority["aid"] == 116969625884294
    assert authority["cid"] == 40231177971
    assert authority["title"].startswith("【李豆沙】")
    assert validate_recovery_title_authority(
        authority,
        candidate_id=CANDIDATE_ID,
        expected_title=str(authority["title"]),
    ) == authority


def test_recovery_title_authority_rejects_resigned_title_forgery():
    authority = _authority()
    authority["title"] = "【李豆沙】伪造但看似合规的标题"
    _resign(authority)

    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLIC_TITLE_AUTHORITY_BINDING_MISMATCH",
    ):
        validate_recovery_title_authority(
            authority,
            candidate_id=CANDIDATE_ID,
        )


def test_recovery_title_authority_rejects_evidence_hash_drift():
    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLIC_TITLE_EVIDENCE_SHA_MISMATCH",
    ):
        build_recovery_title_authority(
            candidate_id=CANDIDATE_ID,
            evidence_path=EVIDENCE,
            expected_evidence_sha256="sha256:" + "0" * 64,
        )


def test_recovery_public_title_cannot_override_ivan_manual_title():
    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLIC_TITLE_CONFLICTS_MANUAL_OVERRIDE",
    ):
        build_recovery_title_authority(
            candidate_id="auto_193450_3573_3665",
            evidence_path=EVIDENCE,
            expected_evidence_sha256=EVIDENCE_SHA256,
        )


def test_recovery_title_authority_rejects_symlinked_evidence(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "receipt.json"
    target.write_bytes(EVIDENCE.read_bytes())
    link = repo / "link.json"
    link.symlink_to(target)

    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLIC_TITLE_EVIDENCE_NOT_REGULAR",
    ):
        build_recovery_title_authority(
            candidate_id=CANDIDATE_ID,
            evidence_path=link,
            expected_evidence_sha256=EVIDENCE_SHA256,
            repo_root=repo,
        )
