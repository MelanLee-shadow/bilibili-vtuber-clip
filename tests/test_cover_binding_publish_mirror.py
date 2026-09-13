"""Cover updates must preserve the importer's exact publish/staging mirror."""
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.cover_repair import _updated_cover_document
from src.autoslice.package_publish_mirror import (
    PUBLISH_NON_STAGING_KEYS,
    PublishStagingMirrorError,
    validate_publish_staging_mirror,
)


def _pair(existing_binding: bool):
    publish = {
        "schema_version": "shadow-publish-draft.v1", "candidate_id": "test_cover",
        "title": "Existing title", "upload_enabled": False,
        "cover_status": "AI_COVER_READY", "cover_path": "/tmp/old.png",
        "cover_generation": {"final_cover": "/tmp/old.png"}, "cover_text": "Old words",
        "reason_codes": [], "artifact_hashes": {}, "video_path": "/tmp/final.mp4",
    }
    if existing_binding:
        publish["cover_repair_binding"] = {"path": "/tmp/old.json", "sha256": "sha256:" + "1" * 64}
    staging = {k: deepcopy(v) for k, v in publish.items() if k not in PUBLISH_NON_STAGING_KEYS}
    staging.update(publish_json_path="/tmp/draft.json", status="STAGED")
    record = {"schema_version": "delivery-record.v1", "candidate_id": "test_cover",
              "publish_staging": staging, "artifact_hashes": {},
              "cover_repair_history": [{"status": "FAIL", "note": "Keep old evidence"}]}
    validate_publish_staging_mirror(record, publish)
    return record, publish


@pytest.mark.parametrize("existing_binding", [False, True])
@pytest.mark.parametrize("top_level_generation", [False, True])
def test_cover_update_keeps_binding_on_both_publish_surfaces(existing_binding, top_level_generation):
    record, publish = _pair(existing_binding)
    if top_level_generation:
        record["cover_generation"] = deepcopy(publish["cover_generation"])
    before_record, before_publish = deepcopy(record), deepcopy(publish)
    kwargs = dict(cover=Path("/tmp/new.png"), cover_sha256="sha256:" + "a" * 64,
                  generation={"final_cover": "/tmp/new.png", "cover_text": "New words"},
                  binding_path=Path("/tmp/new-binding.json"), binding_sha256="sha256:" + "b" * 64)
    current_record = _updated_cover_document(record, **kwargs)
    current_publish = _updated_cover_document(publish, **kwargs)
    # Exercise the real consumer instead of merely asserting a status field.
    staging = validate_publish_staging_mirror(current_record, current_publish)
    assert staging["cover_repair_binding"] == current_record["cover_repair_binding"]
    assert staging["cover_repair_binding"] == current_publish["cover_repair_binding"]
    assert staging["upload_enabled"] is False
    assert staging["title"] == before_publish["title"]
    assert current_record["cover_repair_history"] == before_record["cover_repair_history"]
    assert record == before_record and publish == before_publish


def test_mismatched_binding_still_rejected():
    record, publish = _pair(True)
    record["publish_staging"]["cover_repair_binding"]["sha256"] = "sha256:" + "c" * 64
    with pytest.raises(PublishStagingMirrorError) as err:
        validate_publish_staging_mirror(record, publish)
    assert err.value.code == "PUBLISH_STAGING_VALUE_DRIFT"


def test_missing_binding_still_rejected():
    record, publish = _pair(True)
    del record["publish_staging"]["cover_repair_binding"]
    with pytest.raises(PublishStagingMirrorError) as err:
        validate_publish_staging_mirror(record, publish)
    assert err.value.code == "PUBLISH_STAGING_FIELD_SET_DRIFT"
