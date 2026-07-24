from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.autoslice.recovery_title_authority import (
    build_recovery_publication_authorities,
    expected_recovery_publish_title,
)
from src.autoslice.review_package_title_audit import (
    audit_recovery_title_authority,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_recovery_title_audit_binds_authority_and_publish_bytes(
    tmp_path: Path,
) -> None:
    candidate_id = "auto_193450_1475_1543"
    authority = build_recovery_publication_authorities(
        candidate_ids={candidate_id},
        registry_path=(
            REPO_ROOT
            / "assets/lidousha/recovery_publication_authority.v1.json"
        ),
        expected_registry_sha256=(
            "sha256:"
            "ae15fbfd2b72cbb577fcdda66f94bb2108b79dfb0954f6649bc775ef2e8a6118"
        ),
    )[candidate_id]
    title = expected_recovery_publish_title(authority)
    publish = tmp_path / "clip.publish.json"
    publish.write_text(
        json.dumps(
            {
                "title": title,
                "recovery_publication_authority": authority,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    record = {
        "recovery_publication_authority": authority,
        "artifact_hashes": {
            "publish_draft_sha256": "sha256:"
            + hashlib.sha256(publish.read_bytes()).hexdigest()
        },
    }
    staging = {
        "title": title,
        "recovery_publication_authority": authority,
    }
    kwargs = {
        "item": {"recovery_publication_authority": authority},
        "item_candidate_id": candidate_id,
        "item_title": title,
        "publish_path": publish,
        "record_path": tmp_path / "clip.record.json",
        "record": record,
        "publish_staging": staging,
        "expected_authority": authority,
    }

    assert audit_recovery_title_authority(**kwargs) == ()

    staging["title"] = "错误标题"
    assert [
        issue.code for issue in audit_recovery_title_authority(**kwargs)
    ] == ["RECOVERY_PUBLICATION_AUTHORITY_BINDING_DRIFT"]

    staging["title"] = title
    record["artifact_hashes"]["publish_draft_sha256"] = "sha256:" + "0" * 64
    assert [
        issue.code for issue in audit_recovery_title_authority(**kwargs)
    ] == ["RECOVERY_PUBLICATION_PUBLISH_HASH_DRIFT"]
