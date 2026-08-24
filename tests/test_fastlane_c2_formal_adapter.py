from src.autoslice.fastlane_c2_formal_adapter import (
    CID,
    SCHEMA,
    audit_fastlane_c2_formal_package,
    is_fastlane_c2_formal_manifest,
)


def test_c2_formal_adapter_is_candidate_scoped(tmp_path):
    assert is_fastlane_c2_formal_manifest({"schema_version": SCHEMA, "candidate_id": CID})
    assert not is_fastlane_c2_formal_manifest({"schema_version": SCHEMA, "candidate_id": "other"})
    assert audit_fastlane_c2_formal_package(tmp_path)[0]["code"] == "C2_FORMAL_MANIFEST_INVALID"
