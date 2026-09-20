"""Offline regression for joint-QC costume assumptions and retained rejection."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import build_joint_qc_receipt


def _verdict() -> dict:
    # Explicit synthetic transport response, never a real visual observation.
    return {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Synthetic contract control, not an image assessment.",
        "pass": True,
    }


def _build(tmp_path: Path, verdict: dict, *, status: str = "OBSERVED") -> tuple[dict, dict]:
    cover = tmp_path / "synthetic-cover.png"
    cover.write_bytes(b"synthetic-image-bytes-not-a-real-PNG")
    captured = {}

    def probe(image_path: Path, question: str) -> dict:
        captured.update(image_path=image_path, question=question)
        return {
            "status": status,
            "answer": json.dumps(verdict, ensure_ascii=False),
            "image_path": str(image_path),
            "image_sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
            "provider": "synthetic-test-transport",
        }

    receipt = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】当场造型测试",
        candidate_id="synthetic_session",
        image_probe=probe,
    )
    return receipt, captured


def test_joint_qc_question_uses_current_session_not_fixed_costume(tmp_path: Path) -> None:
    receipt, call = _build(tmp_path, _verdict())
    question = call["question"]
    assert "以当场直播形象为准" in question
    assert "不能把固定发色、熊猫耳、耳朵位置、帽子、墨镜或发饰" in question
    assert "证据不足时不得凭标题、白发或熊猫元素猜测身份" in question
    assert "熊猫耳长在头上不是头套/帽子" not in question
    assert "白发+头顶小熊猫耳" not in question
    assert call["image_path"] == tmp_path / "synthetic-cover.png"
    assert receipt["schema_version"] == "lidousha-title-cover-joint-qc.v1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("lidousha_primary", False),
        ("thumbnail_readable", False),
        ("single_clear_hook", False),
        ("text_overcrowded", True),
        ("title_cover_aligned", False),
        ("physical_text_line_count", 3),
        ("unrelated_or_misleading_elements", ["visible identity conflict"]),
        ("reason", ""),
        ("pass", False),
    ],
)
def test_session_wording_does_not_waive_any_existing_rejection(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    verdict = _verdict()
    verdict[field] = value
    frozen = copy.deepcopy(verdict)
    receipt, _call = _build(tmp_path, verdict)
    assert receipt["status"] == "FAIL"
    assert receipt["pass"] is False
    assert receipt["verdict"] == frozen
    assert json.loads(receipt["witness"]["answer"]) == frozen


def test_costume_dissent_is_not_rewritten_or_cleared(tmp_path: Path) -> None:
    verdict = _verdict()
    verdict.update(
        lidousha_primary=False,
        unrelated_or_misleading_elements=["蓝白兽耳帽不同于我预想的熊猫耳。"],
        reason="Synthetic prior-style dissent must remain intact.",
        **{"pass": False},
    )
    receipt, _call = _build(tmp_path, verdict)
    assert receipt["status"] == "FAIL"
    assert receipt["verdict"] == verdict
    assert json.loads(receipt["witness"]["answer"]) == verdict


@pytest.mark.parametrize("status", ["UNAVAILABLE", "FAILED", ""])
def test_unobserved_identity_is_not_accepted(tmp_path: Path, status: str) -> None:
    receipt, _call = _build(tmp_path, _verdict(), status=status)
    assert receipt["status"] == "FAIL"
    assert receipt["pass"] is False


def test_positive_transport_control_preserves_exact_verdict(tmp_path: Path) -> None:
    verdict = _verdict()
    receipt, _call = _build(tmp_path, verdict)
    assert receipt["status"] == "PASS"
    assert receipt["verdict"] == verdict
    assert json.loads(receipt["witness"]["answer"]) == verdict
