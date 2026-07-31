"""出版登记闸口：published/hold 候选不得再走新投稿（BV1ec3A6bEWF 事故）。"""

from __future__ import annotations

import json

import pytest

from src.autoslice.publication_registry import (
    cover_maintenance_block_reason,
    load_publication_registry,
    manifest_upload_block_reason,
    upload_block_reason,
)


REGISTRY = {
    "schema_version": "publication-registry.v1",
    "entries": [
        {
            "candidate_id": "auto_193129_850_940",
            "recording_date": "2026-07-24",
            "status": "published",
            "bvid": "BV1ec3A6bEWF",
        },
        {
            "candidate_id": "auto_183122_1209_1410",
            "recording_date": "2026-07-24",
            "status": "hold_pending_review",
            "bvid": None,
            "note": "7/24 争议搁置件",
        },
        {
            "candidate_id": "auto_183122_1209_1410_released",
            "recording_date": "2026-07-24",
            "status": "released_for_upload",
            "bvid": None,
            "note": "Ivan released after correction",
        },
    ],
}


def test_published_candidate_blocks_new_upload():
    reason = upload_block_reason("auto_193129_850_940", registry=REGISTRY)
    assert (
        reason
        and "BV1ec3A6bEWF" in reason
        and "authorized same-BV repair lane" in reason
    )


def test_published_candidate_blocks_generic_cover_maintenance():
    reason = cover_maintenance_block_reason(
        "auto_193129_850_940",
        recording_date="2026-07-24",
        registry=REGISTRY,
    )
    assert (
        reason
        and "BV1ec3A6bEWF" in reason
        and "authorized same-BV repair lane" in reason
    )
    assert (
        cover_maintenance_block_reason(
            "auto_183122_1209_1410_released",
            recording_date="2026-07-24",
            registry=REGISTRY,
        )
        is None
    )


def test_unreadable_registry_blocks_cover_maintenance_fail_closed(tmp_path):
    broken = tmp_path / "registry.json"
    broken.write_text("{not json", encoding="utf-8")
    reason = cover_maintenance_block_reason(
        "auto_1_2_3",
        registry_path=broken,
    )
    assert reason and "fail-closed" in reason


def test_held_candidate_blocks_until_cleared():
    reason = upload_block_reason("auto_183122_1209_1410", registry=REGISTRY)
    assert reason and "held pending Ivan" in reason


def test_released_candidate_keeps_audit_history_without_blocking():
    assert (
        upload_block_reason(
            "auto_183122_1209_1410_released",
            registry=REGISTRY,
        )
        is None
    )


def test_unlisted_candidate_and_date_mismatch_allow():
    assert upload_block_reason("auto_999999_1_2", registry=REGISTRY) is None
    # 同 candidate_id 但另一天的录播不受这行约束
    assert (
        upload_block_reason(
            "auto_193129_850_940", recording_date="2026-08-01", registry=REGISTRY
        )
        is None
    )


def test_unreadable_registry_fails_closed(tmp_path):
    broken = tmp_path / "registry.json"
    broken.write_text("{not json", encoding="utf-8")
    reason = upload_block_reason("auto_1_2_3", registry_path=broken)
    assert reason and "fail-closed" in reason


def test_committed_registry_loads_and_lists_the_incident():
    registry = load_publication_registry()
    rows = {r["candidate_id"]: r for r in registry["entries"]}
    assert rows["auto_193129_850_940"]["bvid"] == "BV1ec3A6bEWF"
    assert rows["auto_183122_1209_1410"] == {
        "candidate_id": "auto_183122_1209_1410",
        "recording_date": "2026-07-24",
        "status": "published",
        "bvid": "BV1AD366DEd9",
        "note": (
            "2026-07-28 新稿发布；后续修复只允许 authorized_upload.py "
            "repair-* 的原 BV 修复链"
        ),
    }
    assert rows["auto_192000_909_1014"]["bvid"] == "BV1s7326qEc9"
    assert rows["auto_192000_909_1014"]["status"] == "published"
    assert rows["auto_224211_80_141"]["bvid"] == "BV1ngGc6BEBS"
    assert rows["song_192000_1321"]["bvid"] == "BV1BJGc6aEWf"
    assert rows["auto_145940_1533_1619"]["bvid"] == "BV1ELGc6NEC5"
    assert rows["auto_224211_181_480"]["bvid"] == "BV1EpGA6MExh"
    assert rows["auto_225056_1013_1116"]["bvid"] == "BV154GA6vEyD"
    assert rows["auto_225056_235_339"]["bvid"] == "BV1UfGw69EMG"


def test_manifest_gate_reads_attested_record(tmp_path):
    record = tmp_path / "auto_193129_850_940.record.json"
    record.write_text(
        json.dumps(
            {"story_contract": {"candidate_id": "auto_193129_850_940"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = {
        "package_attestation": {
            "package_root": "/opt/bilive/autoslice/out/2026-07-24/auto_193129_850_940/replacement_recuts",
            "record": {"path": str(record)},
        }
    }
    reason = manifest_upload_block_reason(manifest)
    assert reason and "BV1ec3A6bEWF" in reason

    # attested record 不可读 → fail-closed；没有 attestation 的旧 lane 放行
    record.write_text("{broken", encoding="utf-8")
    assert "fail-closed" in (manifest_upload_block_reason(manifest) or "")
    assert manifest_upload_block_reason({}) is None


def test_manifest_gate_reads_verified_song_delivery_candidate_without_talk_story(tmp_path):
    record = tmp_path / "song_192000_1321.record.json"
    record.write_text(
        json.dumps(
            {
                "delivery_candidate_id": "song_192000_1321",
                "source_candidate_id": "seededsong_120000_421470",
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "package_attestation": {
            "package_root": "/opt/bilive/autoslice/out/2026-07-25/song_192000_1321/replacement_recuts",
            "record": {"path": str(record)},
        }
    }
    reason = manifest_upload_block_reason(manifest)
    assert reason and "BV1BJGc6aEWf" in reason

    # A legacy Talk-like record without a StoryContract does not acquire Song
    # registry semantics merely from an arbitrary delivery-like field.
    record.write_text(json.dumps({"delivery_candidate_id": "song_192000_1321"}), encoding="utf-8")
    assert manifest_upload_block_reason(manifest) is None


def test_malformed_committed_row_raises(tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": "publication-registry.v1",
                "entries": [{"candidate_id": "x", "status": "published"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_publication_registry(bad)
