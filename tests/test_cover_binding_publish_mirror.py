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


def test_b2_local_video_and_subtitle_locators_are_not_semantic_mirror_fields():
    record, publish = _pair(False)
    record["publish_staging"]["video_path"] = "/tmp/current-final.mp4"
    record["publish_staging"]["subtitle_path"] = "/tmp/current-final.srt"

    staging = validate_publish_staging_mirror(record, publish)

    assert staging["video_path"] == "/tmp/current-final.mp4"
    assert staging["subtitle_path"] == "/tmp/current-final.srt"
    assert publish["video_path"] == "/tmp/final.mp4"


def test_b2_local_locator_does_not_hide_semantic_field_drift():
    record, publish = _pair(False)
    record["publish_staging"]["video_path"] = "/tmp/current-final.mp4"
    record["publish_staging"]["subtitle_path"] = "/tmp/current-final.srt"
    record["publish_staging"]["title"] = "Changed title"

    with pytest.raises(PublishStagingMirrorError) as err:
        validate_publish_staging_mirror(record, publish)

    assert err.value.code == "PUBLISH_STAGING_VALUE_DRIFT"


@pytest.mark.parametrize("local_key", ["video_path", "subtitle_path"])
def test_optional_local_locator_is_individually_allowed(local_key):
    record, publish = _pair(False)
    record["publish_staging"][local_key] = f"/tmp/current-{local_key}"

    assert validate_publish_staging_mirror(record, publish) is record["publish_staging"]


def test_unknown_staging_extra_remains_rejected():
    record, publish = _pair(False)
    record["publish_staging"]["unexpected_runtime_path"] = "/tmp/unknown"

    with pytest.raises(PublishStagingMirrorError) as err:
        validate_publish_staging_mirror(record, publish)

    assert err.value.code == "PUBLISH_STAGING_FIELD_SET_DRIFT"


def test_local_name_declared_by_publish_remains_semantically_mirrored():
    record, publish = _pair(False)
    publish["subtitle_path"] = "/tmp/published-subtitle.srt"
    record["publish_staging"]["subtitle_path"] = "/tmp/published-subtitle.srt"
    assert validate_publish_staging_mirror(record, publish)

    record["publish_staging"]["subtitle_path"] = "/tmp/other-subtitle.srt"
    with pytest.raises(PublishStagingMirrorError) as err:
        validate_publish_staging_mirror(record, publish)

    assert err.value.code == "PUBLISH_STAGING_VALUE_DRIFT"
