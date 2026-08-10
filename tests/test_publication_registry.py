"""出版登记闸口：published/hold 候选不得再走新投稿（BV1ec3A6bEWF 事故）。"""

from __future__ import annotations

import json
import hashlib

import pytest

from src.autoslice.publication_registry import (
    _BLOCKING_STATUSES,
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


def test_held_candidate_blocks_cover_maintenance_and_release_restores_it():
    """搁置件不做封面（贪生怕死 auto_223750_913_1322，Ivan 2026-08-10 逐字
    「贪生怕死不需要进行上传，就不需要封面了」）。

    published 与 hold_pending_review 结论相同、理由不同：前者是公开像素只许走
    授权同 BV 修复链，后者是这件根本不会被上传，出的每一张图都是纯浪费。判据
    读的是 committed registry，放行后必须能自然恢复。
    """

    reason = cover_maintenance_block_reason(
        "auto_183122_1209_1410",
        recording_date="2026-07-24",
        registry=REGISTRY,
    )
    assert reason and "held pending Ivan" in reason
    # 运维日志要带上 Ivan 的原始理由，否则 tick 里只看得到一句无来由的抑制。
    assert "7/24 争议搁置件" in reason
    assert "no cover is generated or repaired" in reason
    # 绝不能顺手把上传门说成"已放行"——上传唯一授权仍是这张登记表。
    assert upload_block_reason("auto_183122_1209_1410", registry=REGISTRY)

    # Ivan 放行后同一条候选恢复做封面，没有需要人工清理的不可逆终态。
    released = {
        "schema_version": REGISTRY["schema_version"],
        "entries": [
            {**row, "status": "released_for_upload"}
            if row["candidate_id"] == "auto_183122_1209_1410"
            else row
            for row in REGISTRY["entries"]
        ],
    }
    assert (
        cover_maintenance_block_reason(
            "auto_183122_1209_1410",
            recording_date="2026-07-24",
            registry=released,
        )
        is None
    )


def test_cover_maintenance_refuses_every_upload_blocking_status():
    """封面闸口的成员判据就是 _BLOCKING_STATUSES 本身。

    以前它硬编码 published，于是 hold 件继续烧图片额度。任何新增的阻断状态都
    必须默认不出图（fail-closed 方向：不确定时省钱且无害）。
    """

    for status in _BLOCKING_STATUSES:
        registry = {
            "schema_version": "publication-registry.v1",
            "entries": [
                {
                    "candidate_id": "auto_blocking_probe",
                    "recording_date": "2026-08-07",
                    "status": status,
                    "bvid": "BV1PROBE",
                }
            ],
        }
        assert (
            cover_maintenance_block_reason(
                "auto_blocking_probe",
                recording_date="2026-08-07",
                registry=registry,
            )
            is not None
        ), status


def test_unlisted_and_released_candidates_keep_normal_cover_maintenance():
    """普通候选不受影响：不在登记表里、或已放行的，照常做封面。"""

    assert cover_maintenance_block_reason("auto_999999_1_2", registry=REGISTRY) is None
    assert (
        cover_maintenance_block_reason(
            "auto_183122_1209_1410_released",
            recording_date="2026-07-24",
            registry=REGISTRY,
        )
        is None
    )
    # 同 candidate_id 但另一天的录播不受这行 hold 约束
    assert (
        cover_maintenance_block_reason(
            "auto_183122_1209_1410",
            recording_date="2026-08-01",
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
    assert rows["auto_225056_814_887"]["bvid"] == "BV1FBGw6uE6i"


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


def test_hash_bound_runtime_registry_overlay_blocks_duplicate_before_deploy(
    tmp_path,
):
    static = tmp_path / "static.json"
    static.write_text(
        json.dumps(
            {"schema_version": "publication-registry.v1", "entries": []}
        ),
        encoding="utf-8",
    )
    authority = tmp_path / "authority.json"
    authority.write_text(
        json.dumps(
            {
                "schema_version": (
                    "new-bv-publication-reconciliation-authority.v1"
                ),
                "status": "VERIFIED_PUBLIC",
                "candidate_id": "candidate-runtime",
                "recording_date": "2026-07-29",
                "bvid": "BV1RUNTIME",
                "aid": 1,
                "cid": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    authority_sha = hashlib.sha256(authority.read_bytes()).hexdigest()
    publication = {
        "schema_version": "publication-reconciliation.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": "candidate-runtime",
        "recording_date": "2026-07-29",
        "bvid": "BV1RUNTIME",
        "aid": 1,
        "cid": 2,
        "authority": {
            "path": str(authority),
            "sha256": authority_sha,
            "bytes": authority.stat().st_size,
        },
    }
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps(
            {
                "schema_version": (
                    "publication-reconciliation-registry.v1"
                ),
                "entries": [
                    {
                        "candidate_id": "candidate-runtime",
                        "recording_date": "2026-07-29",
                        "status": "published",
                        "bvid": "BV1RUNTIME",
                        "publication_reconciliation": publication,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = load_publication_registry(static, runtime_path=runtime)
    assert registry["entries"][0]["bvid"] == "BV1RUNTIME"
    assert (
        upload_block_reason("candidate-runtime", registry=registry)
        is not None
    )

    authority.write_text("drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="RUNTIME_REGISTRY_INVALID"):
        load_publication_registry(static, runtime_path=runtime)
