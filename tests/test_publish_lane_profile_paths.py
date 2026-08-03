"""发布/声纹 lane 的资产路径按 profile 派生（默认 profile 字节等价）。"""

from pathlib import Path

from src.autoslice.channel_profile import load_channel_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_channel_profile(ROOT)


def test_publication_registry_path_follows_profile() -> None:
    from src.autoslice.publication_registry import DEFAULT_REGISTRY_PATH

    assert DEFAULT_REGISTRY_PATH == PROFILE.asset_file("publication_registry")
    assert DEFAULT_REGISTRY_PATH.is_file()


def test_final_review_contract_path_and_schema_follow_profile() -> None:
    from src.autoslice.final_human_review import (
        FINAL_MEDIA_REVIEW_CONTRACT_PATH,
        REVIEW_CONTRACT_SCHEMA,
        SCHEMA_VERSION,
    )

    assert FINAL_MEDIA_REVIEW_CONTRACT_PATH == PROFILE.asset_file(
        "final_media_review_contracts"
    )
    assert REVIEW_CONTRACT_SCHEMA == (
        f"{PROFILE.profile_id}-final-media-review-contracts.v1"
    )
    # 终审回执 schema 是包内持久证据词汇：保持冻结拼写，绝不随 profile 变
    assert SCHEMA_VERSION == "lidousha-final-human-review.v2"


def test_owner_audit_truth_ledger_follows_profile() -> None:
    from src.autoslice.review_package_owner_audit import SOURCE_TRUTH_LEDGER_PATH

    assert SOURCE_TRUTH_LEDGER_PATH == PROFILE.asset_file("subtitle_truth_ledger")


def test_manual_title_authority_root_and_schema_follow_profile() -> None:
    from src.autoslice.manual_title_repair_authority import (
        AUTHORITY_ROOT,
        SCHEMA_VERSION,
    )

    assert AUTHORITY_ROOT == PROFILE.asset_directory(
        "manual_title_repair_authorities"
    )
    assert SCHEMA_VERSION == (
        f"{PROFILE.profile_id}-manual-title-repair-authority.v1"
    )


def test_term_lexicon_discovery_uses_profile_output_directory(tmp_path) -> None:
    from src.autoslice.term_lexicon import discover_term_lexicon

    nested = tmp_path / PROFILE.output_directory
    nested.mkdir()
    lexicon = nested / "term_lexicon.json"
    lexicon.write_text("{}", encoding="utf-8")
    probe = tmp_path / "2026-08-02" / "clip.mp4"
    probe.parent.mkdir()
    probe.write_bytes(b"x")

    assert discover_term_lexicon(probe) == lexicon.resolve()


def test_default_profile_paths_are_byte_equal_to_legacy_layout() -> None:
    # 默认 profile 下派生结果必须与旧硬编码路径逐字相同（回归锚）
    assert PROFILE.asset_file("publication_registry") == (
        ROOT / "assets/lidousha/publication_registry.v1.json"
    )
    assert PROFILE.asset_file("final_media_review_contracts") == (
        ROOT / "assets/lidousha/final_media_review_contracts.v1.json"
    )
    assert PROFILE.asset_directory("manual_title_repair_authorities") == (
        ROOT / "assets/lidousha/manual_title_repair_authorities"
    )
