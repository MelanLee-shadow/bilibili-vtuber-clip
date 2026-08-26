from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.autoslice.fastlane_c9_joint_qc import (
    BASE,
    JOINT_QC_NAME,
    FastlaneC9JointQCError,
    derive_c9_joint_qc,
    materialize_c9_joint_qc,
    validate_c9_direct_authority,
    validate_c9_joint_qc,
)


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / BASE / "auto_143025_1112_1285.ivan-direct-title-cover-authority.v1.json"
COVER = ROOT / BASE / "auto_143025_1112_1285.cover.png"


def _reseal(value: dict[str, object]) -> None:
    body = dict(value)
    body.pop("self_sha256", None)
    value["self_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _copy_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "assets", repo / "assets")
    return repo


def test_c9_derived_joint_qc_is_exact_provider_free_and_replayable() -> None:
    checked = validate_c9_direct_authority(ROOT)
    receipt = derive_c9_joint_qc(ROOT)

    assert checked["cover_sha256"] == "sha256:4222118c60e02b449aa75abf53ce36fef43a2b4a8f7ea34fb7b83428edec8d19"
    assert receipt["status"] == "PASS"
    assert receipt["provider_attempted"] is False
    assert receipt["provider_result"] is False
    assert validate_c9_joint_qc(receipt, ROOT)


def test_c9_joint_qc_materialization_is_create_only(tmp_path: Path) -> None:
    target = tmp_path / JOINT_QC_NAME
    receipt = materialize_c9_joint_qc(repo_root=ROOT, output_path=target)
    assert json.loads(target.read_text(encoding="utf-8")) == receipt
    with pytest.raises(FastlaneC9JointQCError, match="CREATE_ONLY"):
        materialize_c9_joint_qc(repo_root=ROOT, output_path=target)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.__setitem__("candidate_id", "auto_143025_1112_1286"),
        lambda d: d.__setitem__("recording_date", "2026-08-16"),
        lambda d: d.__setitem__("selection_hook_sha256", "sha256:" + "0" * 64),
        lambda d: d.__setitem__("title_sha256", "sha256:" + "0" * 64),
        lambda d: d.__setitem__("cover_sha256", "sha256:" + "0" * 64),
        lambda d: d["predecessors"]["cpa_redraw"].__setitem__("receipt_sha256", "sha256:" + "0" * 64),
        lambda d: d["direct_authority"].__setitem__("answer_sha256", "sha256:" + "0" * 64),
    ],
)
def test_c9_direct_authority_rejects_resealed_near_misses(tmp_path: Path, mutation) -> None:
    repo = _copy_repo(tmp_path)
    path = repo / BASE / AUTHORITY.name
    document = json.loads(path.read_text(encoding="utf-8"))
    mutation(document)
    _reseal(document)
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(FastlaneC9JointQCError):
        validate_c9_direct_authority(repo)


def test_c9_joint_qc_rejects_cover_byte_mutation(tmp_path: Path) -> None:
    repo = _copy_repo(tmp_path)
    (repo / BASE / COVER.name).write_bytes(b"mutated-cover")
    with pytest.raises(FastlaneC9JointQCError, match="COVER_BYTES_DRIFT"):
        validate_c9_direct_authority(repo)
