from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.autoslice.recovery_title_authority import (
    build_recovery_title_authority,
)
from src.autoslice.review_package_title_audit import (
    audit_recovery_title_authority,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_recovery_title_audit_binds_authority_and_publish_bytes(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_title_authority(
        candidate_id=candidate_id,
        evidence_path=(
            REPO_ROOT
            / "reports/authorized_uploads/2026-07-22-v8-final"
            / f"{candidate_id}.public_verify.json"
        ),
        expected_evidence_sha256=(
            "sha256:"
            "c3af4c8485ca2cf3f17c1d4a660c53cd4f1d23a3f9d254e77924d9875a07d771"
        ),
    )
    publish = tmp_path / "clip.publish.json"
    publish.write_text(
        json.dumps(
            {"title": authority["title"], "recovery_title_authority": authority},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = {
        "recovery_title_authority": authority,
        "artifact_hashes": {
            "publish_draft_sha256": "sha256:"
            + hashlib.sha256(publish.read_bytes()).hexdigest()
        },
    }
    staging = {
        "title": authority["title"],
        "recovery_title_authority": authority,
    }
    kwargs = {
        "item": {"recovery_title_authority": authority},
        "item_candidate_id": candidate_id,
        "item_title": authority["title"],
        "publish_path": publish,
        "record_path": tmp_path / "clip.record.json",
        "record": record,
        "publish_staging": staging,
    }

    assert audit_recovery_title_authority(**kwargs) == ()

    staging["title"] = "错误标题"
    assert [
        issue.code for issue in audit_recovery_title_authority(**kwargs)
    ] == ["RECOVERY_PUBLIC_TITLE_AUTHORITY_BINDING_DRIFT"]

    staging["title"] = authority["title"]
    record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + "0" * 64
    assert [
        issue.code for issue in audit_recovery_title_authority(**kwargs)
    ] == ["RECOVERY_PUBLIC_TITLE_PUBLISH_HASH_DRIFT"]
