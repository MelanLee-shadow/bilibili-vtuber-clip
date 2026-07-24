import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice.recovery_title_authority import (
    ROOT,
    RecoveryTitleAuthorityError,
    build_recovery_publication_authorities,
    build_recovery_title_authority,
    expected_recovery_publish_title,
    validate_recovery_publication_authority,
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
PUBLICATION_ASSET = (
    ROOT / "assets/lidousha/recovery_publication_authority.v1.json"
)
PUBLICATION_ASSET_SHA256 = (
    "sha256:be9ffbd42008b94d9e47ea714e1fae5d032f576bb0e71841624df3b77ea53757"
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


def test_publication_registry_covers_public_and_manual_title_modes():
    candidate_ids = {
        "auto_193450_3573_3665",
        "auto_193450_672_945",
        "auto_193450_1863_2056",
        "auto_193450_1573_1672",
        "auto_193450_1475_1543",
    }
    authorities = build_recovery_publication_authorities(
        candidate_ids=candidate_ids,
        registry_path=PUBLICATION_ASSET,
        expected_registry_sha256=PUBLICATION_ASSET_SHA256,
    )

    assert set(authorities) == candidate_ids
    assert authorities["auto_193450_3573_3665"]["title_mode"] == (
        "ivan_manual_override"
    )
    assert authorities["auto_193450_1475_1543"]["title_mode"] == (
        "verified_public_exact"
    )
    assert {
        candidate_id: authority["required_given_end_ms"]
        for candidate_id, authority in authorities.items()
    } == {
        "auto_193450_3573_3665": 3_665_850,
        "auto_193450_672_945": 951_900,
        "auto_193450_1863_2056": 2_056_480,
        "auto_193450_1573_1672": 1_679_990,
        "auto_193450_1475_1543": 1_543_760,
    }
    assert {
        candidate_id: authority["boundary_end_mode"]
        for candidate_id, authority in authorities.items()
    } == {
        "auto_193450_3573_3665": "semantic_lower_bound",
        "auto_193450_672_945": "semantic_lower_bound",
        "auto_193450_1863_2056": "exact_source_pin",
        "auto_193450_1573_1672": "semantic_lower_bound",
        "auto_193450_1475_1543": "semantic_lower_bound",
    }
    for candidate_id, authority in authorities.items():
        assert (
            validate_recovery_publication_authority(
                authority,
                candidate_id=candidate_id,
                expected_final_title=expected_recovery_publish_title(
                    authority
                ),
            )
            == authority
        )


def test_publication_registry_requires_every_requested_candidate():
    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLICATION_CANDIDATE_MISSING",
    ):
        build_recovery_publication_authorities(
            candidate_ids={"auto_missing"},
            registry_path=PUBLICATION_ASSET,
            expected_registry_sha256=PUBLICATION_ASSET_SHA256,
        )


def test_publication_registry_exact_mode_rejects_omitted_candidate():
    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLICATION_CANDIDATE_SET_MISMATCH",
    ):
        build_recovery_publication_authorities(
            candidate_ids={"auto_193450_3573_3665"},
            registry_path=PUBLICATION_ASSET,
            expected_registry_sha256=PUBLICATION_ASSET_SHA256,
            require_exact_candidate_set=True,
        )


def test_publication_authority_rejects_wrong_registry_hash():
    with pytest.raises(
        RecoveryTitleAuthorityError,
        match="RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH",
    ):
        build_recovery_publication_authorities(
            candidate_ids={CANDIDATE_ID},
            registry_path=PUBLICATION_ASSET,
            expected_registry_sha256="sha256:" + "0" * 64,
        )
