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
from src.autoslice.surface_canon import CHANNEL_PROFILE


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
    "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
)
PUBLISHED_RECALL_ASSET = (
    ROOT
    / "assets/lidousha"
    / "recovery_publication_authority_2026-07-24_1209.v1.json"
)
C4_PREDECESSOR_CANDIDATE_ID = "auto_113028_1602_1698"
C4_PREDECESSOR_TITLE = (
    "【李豆沙】线上直播间老公不可能是女生，李姐开始男女身份排列组合，"
    "竟敢磕男的女的？违背直播间世界观了"
)
C4_PREDECESSOR_PUBLICATION_ASSET = (
    ROOT
    / "assets/lidousha"
    / "recovery_publication_authority_2026-08-25_c4_timeaxis.v1.json"
)
C4_PREDECESSOR_PUBLICATION_ASSET_SHA256 = (
    "sha256:08d549fa718b41c6568a8d7577cb2a2d7d918917a0455604c4d6a579c3aeefc7"
)
C4_PREDECESSOR_AUTHORITY_SHA256 = (
    "sha256:8f16a9849b0df1be496f2ddd7578563d0f7f63dc5a1b9239a4c3b6652f1b7937"
)
COMBINED_C4_C5_PUBLICATION_ASSET = (
    ROOT
    / "assets/lidousha"
    / "recovery_publication_authority_2026-08-25_c4_c5_timeaxis.v1.json"
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
        "auto_193450_1573_1672": 1_672_970,
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


def test_publication_registry_accepts_typed_published_recall_anchor():
    raw = PUBLISHED_RECALL_ASSET.read_bytes()
    authorities = build_recovery_publication_authorities(
        candidate_ids={"auto_183122_1209_1410"},
        registry_path=PUBLISHED_RECALL_ASSET,
        expected_registry_sha256=(
            "sha256:" + hashlib.sha256(raw).hexdigest()
        ),
    )

    authority = authorities["auto_183122_1209_1410"]
    assert authority["boundary_end_mode"] == "published_recall_anchor"
    assert authority["required_given_end_ms"] == 1_410_240
    assert validate_recovery_publication_authority(
        authority,
        candidate_id="auto_183122_1209_1410",
    ) == authority


def test_publication_registry_accepts_explicit_section_drift_identity_receipt(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    evidence_dir = repo / "reports" / "captured"
    asset_dir = repo / "assets" / "channel"
    evidence_dir.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    candidate_id = "auto_162016_20_319"
    title = f"{CHANNEL_PROFILE.talk_title_prefix}已发稿件的粉色小姐姐故事"
    receipt = {
        "schema_version": "recovery-publication-identity.v1",
        "status": "VERIFIED_TITLE_AND_TARGET_IDENTITY",
        "bvid": "BV1RPNR6dET9",
        "manifest_title": title,
        "expected": {"title": title},
        "public_view": {
            "code": 0,
            "state": 0,
            "aid": 116916727323413,
            "cid": 39933773974,
            "title": title,
        },
        "member_archive": {
            "aid": 116916727323413,
            "bvid": "BV1RPNR6dET9",
            "title": title,
        },
        "section_api": {
            "code": 0,
            "episode_match_count": 1,
            "episode_titles": [f"{CHANNEL_PROFILE.talk_title_prefix}历史旧标题"],
        },
        "problems": ["exact section episode title mismatch"],
    }
    receipt_path = evidence_dir / f"{candidate_id}.publication_identity.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt_sha = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    registry = {
        "schema_version": "lidousha-recovery-publication-authority.v1",
        "authority": "Ivan directed an in-place latest-pipeline repair.",
        "entries": [
            {
                "candidate_id": candidate_id,
                "title_mode": "verified_public_exact",
                "observed_public_title": title,
                "required_given_end_ms": 319810,
                "boundary_end_mode": "published_recall_anchor",
                "bvid": "BV1RPNR6dET9",
                "aid": 116916727323413,
                "cid": 39933773974,
                "source_public_verify_repo_path": receipt_path.relative_to(repo).as_posix(),
                "source_public_verify_sha256": receipt_sha,
                "source_public_verify_schema_version": "recovery-publication-identity.v1",
                "source_public_verify_status": "VERIFIED_TITLE_AND_TARGET_IDENTITY",
            }
        ],
    }
    registry_path = asset_dir / "publication.json"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    authorities = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry_path,
        expected_registry_sha256=(
            "sha256:" + hashlib.sha256(registry_path.read_bytes()).hexdigest()
        ),
        repo_root=repo,
    )

    assert authorities[candidate_id]["cid"] == 39933773974
    assert authorities[candidate_id]["source_public_verify_status"] == (
        "VERIFIED_TITLE_AND_TARGET_IDENTITY"
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


def test_c4_predecessor_registry_replays_exact_historical_authority():
    raw = C4_PREDECESSOR_PUBLICATION_ASSET.read_bytes()
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == (
        C4_PREDECESSOR_PUBLICATION_ASSET_SHA256
    )

    authorities = build_recovery_publication_authorities(
        candidate_ids={C4_PREDECESSOR_CANDIDATE_ID},
        registry_path=C4_PREDECESSOR_PUBLICATION_ASSET,
        expected_registry_sha256=(
            C4_PREDECESSOR_PUBLICATION_ASSET_SHA256
        ),
        require_exact_candidate_set=True,
    )

    assert set(authorities) == {C4_PREDECESSOR_CANDIDATE_ID}
    authority = authorities[C4_PREDECESSOR_CANDIDATE_ID]
    assert authority["authority_sha256"] == (
        C4_PREDECESSOR_AUTHORITY_SHA256
    )
    assert authority["registry_repo_path"] == (
        "assets/lidousha/"
        "recovery_publication_authority_2026-08-25_c4_timeaxis.v1.json"
    )
    assert authority["cid"] == 41244820167
    assert validate_recovery_publication_authority(
        authority,
        candidate_id=C4_PREDECESSOR_CANDIDATE_ID,
        expected_final_title=C4_PREDECESSOR_TITLE,
    ) == authority


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("missing", "RECOVERY_PUBLIC_TITLE_EVIDENCE_MISSING"),
        ("single_byte", "RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH"),
        ("symlink", "RECOVERY_PUBLIC_TITLE_EVIDENCE_NOT_REGULAR"),
        ("wrong_path", "RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH"),
        ("combined_only", "RECOVERY_PUBLIC_TITLE_EVIDENCE_MISSING"),
    ],
)
def test_c4_predecessor_registry_binding_fails_closed(
    tmp_path: Path,
    damage: str,
    error: str,
):
    repo = tmp_path / "repo"
    historical_relative = Path(
        "assets/lidousha/"
        "recovery_publication_authority_2026-08-25_c4_timeaxis.v1.json"
    )
    historical_path = repo / historical_relative
    historical_path.parent.mkdir(parents=True)
    historical_path.write_bytes(C4_PREDECESSOR_PUBLICATION_ASSET.read_bytes())
    registry = json.loads(historical_path.read_text(encoding="utf-8"))
    source_relative = Path(
        registry["entries"][0]["source_public_verify_repo_path"]
    )
    source_path = repo / source_relative
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes((ROOT / source_relative).read_bytes())
    authority = build_recovery_publication_authorities(
        candidate_ids={C4_PREDECESSOR_CANDIDATE_ID},
        registry_path=historical_path,
        expected_registry_sha256=(
            C4_PREDECESSOR_PUBLICATION_ASSET_SHA256
        ),
        require_exact_candidate_set=True,
        repo_root=repo,
    )[C4_PREDECESSOR_CANDIDATE_ID]

    combined_relative = Path(
        "assets/lidousha/"
        "recovery_publication_authority_2026-08-25_c4_c5_timeaxis.v1.json"
    )
    combined_path = repo / combined_relative
    if damage in {"wrong_path", "combined_only"}:
        combined_path.write_bytes(COMBINED_C4_C5_PUBLICATION_ASSET.read_bytes())
    if damage == "missing":
        historical_path.unlink()
    elif damage == "single_byte":
        historical_path.write_bytes(historical_path.read_bytes() + b" ")
    elif damage == "symlink":
        target = historical_path.with_name("historical-target.json")
        historical_path.replace(target)
        historical_path.symlink_to(target)
    elif damage == "wrong_path":
        authority["registry_repo_path"] = combined_relative.as_posix()
        _resign(authority)
    elif damage == "combined_only":
        historical_path.unlink()
    else:
        raise AssertionError(f"unknown damage mode: {damage}")

    with pytest.raises(RecoveryTitleAuthorityError, match=error):
        validate_recovery_publication_authority(
            authority,
            candidate_id=C4_PREDECESSOR_CANDIDATE_ID,
            expected_final_title=C4_PREDECESSOR_TITLE,
            repo_root=repo,
        )


def test_dedicated_daily_same_bv_registry_replays_only_qixi_public_receipt():
    candidate_id = "auto_113022_354_496"
    registry_path = ROOT / "assets/lidousha/daily_same_bv_publication_authority.v1.json"
    authorities = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=registry_path,
        expected_registry_sha256=(
            "sha256:88fd8a35607f9d2e46999bd5fcc7044e2c547e08edf5aa711c9e32965828f3b4"
        ),
        require_exact_candidate_set=True,
    )

    assert set(authorities) == {candidate_id}
    authority = authorities[candidate_id]
    assert authority["boundary_end_mode"] == "semantic_lower_bound"
    assert authority["required_given_end_ms"] == 496_420
    assert authority["source_public_verify_schema_version"] == (
        "authorized-upload-public-verify.v2"
    )
    assert authority["source_public_verify_status"] == "VERIFIED_PUBLIC"
    assert authority["source_public_verify_sha256"] == (
        "sha256:df7fde9f84518c4411db564b4aa09458a68fb37f92bdaeda66e37b69be6c1c37"
    )
