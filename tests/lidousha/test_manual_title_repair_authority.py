import pytest

from src.autoslice.manual_title_repair_authority import (
    ManualTitleRepairAuthorityError,
    load_manual_title_repair_authority,
    validate_manual_title_repair_authority,
)
from src.autoslice.source_fact_review import (
    authorize_manual_title_repair,
    source_fact_review_passes,
)


def _authority() -> dict[str, object]:
    authority = load_manual_title_repair_authority("auto_152944_964_1091")
    assert authority is not None
    return authority


def test_committed_964_authority_is_exactly_bound() -> None:
    authority = _authority()
    consumption = validate_manual_title_repair_authority(
        authority,
        candidate_id="auto_152944_964_1091",
        original_title="【李豆沙】长沙人李豆沙亲自打假“长沙大香肠”，话还没说完，弹幕又提议把技能叫“李姐拉拉”",
        original_selection_hook="长沙人李豆沙亲自打假“长沙大香肠”，话还没说完，弹幕又把她的技能强行命名成“李姐拉拉”。",
        source_fact_receipt_sha256="sha256:0713e2aa7a68e86bd46c52f9c99a606f0b32871acedf9f02da139e039a0f60cd",
        final_title="【李豆沙】长沙人李豆沙否认“长沙大香肠”是长沙特产，SC提议把技能叫“李姐拉拉”",
        final_selection_hook="长沙人李豆沙否认“长沙大香肠”是长沙特产，期间一条SC提议把她的技能叫“李姐拉拉”。",
    )
    assert consumption["status"] == "CONSUMED"
    assert consumption["authority_sha256"] == authority["authority_sha256"]


def test_964_authority_rejects_any_replacement_drift() -> None:
    authority = _authority()
    with pytest.raises(ManualTitleRepairAuthorityError, match="BINDING_MISMATCH"):
        validate_manual_title_repair_authority(
            authority,
            candidate_id="auto_152944_964_1091",
            original_title=str(authority["original_title"]),
            original_selection_hook=str(authority["original_selection_hook"]),
            source_fact_receipt_sha256=str(authority["source_fact_receipt_sha256"]),
            final_title="【李豆沙】任意另一标题",
            final_selection_hook=str(authority["final_selection_hook"]),
        )


def test_authorized_receipt_preserves_the_blocked_receipt_hash() -> None:
    authority = _authority()
    consumption = validate_manual_title_repair_authority(
        authority,
        candidate_id=str(authority["candidate_id"]),
        original_title=str(authority["original_title"]),
        original_selection_hook=str(authority["original_selection_hook"]),
        source_fact_receipt_sha256=str(authority["source_fact_receipt_sha256"]),
        final_title=str(authority["final_title"]),
        final_selection_hook=str(authority["final_selection_hook"]),
    )
    blocked = {
        "schema_version": "lidousha-source-fact-review.v1",
        "status": "FAILED",
        "decision": "REPAIR_REQUIRES_TITLE_AUTHORITY",
        "reason_code": "SOURCE_FACT_TITLE_AUTHORITY_REQUIRED",
        "original_title": authority["original_title"],
        "original_selection_hook": authority["original_selection_hook"],
        "receipt_sha256": authority["source_fact_receipt_sha256"],
        "passes": [
            {
                "final_title": authority["final_title"],
                "final_selection_hook": authority["final_selection_hook"],
            }
        ],
    }
    repaired = authorize_manual_title_repair(blocked, consumption=consumption)
    assert source_fact_review_passes(repaired)
    assert repaired["final_title"] == authority["final_title"]
    assert repaired["manual_title_repair_authority_consumption"] == consumption
