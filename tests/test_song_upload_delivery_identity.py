"""Authorized upload resolves a verified Song's publication identity from its id pair.

真数据背景 ——《快乐手帐》``song_130025_580``：全源 seeded 重试后，审片包与 active
record 的 ``delivery_candidate_id`` 是外层交付 id，record 的 ``candidate_id`` /
``source_candidate_id`` 是选片行 ``seededsong_120000_349590``。``make-manifest`` 把两者都当交付身份，
以 ``FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH`` 拒收；10-15《历历万乡》两 id 相同才没撞上。
"""

import pytest

from src.autoslice import authorized_upload_final_media

DELIVERY = "song_130025_580"
SOURCE = "seededsong_120000_349590"


def _resolve(record, review_item, tmp_path):
    return authorized_upload_final_media._candidate_id(
        record, review_item, package_root=tmp_path, manifest={}
    )


def _song_record(**overrides):
    record = {
        "candidate_id": SOURCE,
        "delivery_candidate_id": DELIVERY,
        "source_candidate_id": SOURCE,
    }
    record.update(overrides)
    return record


def test_seeded_song_record_resolves_to_its_delivery_id(tmp_path):
    assert _resolve(_song_record(), {"candidate_id": DELIVERY}, tmp_path) == (DELIVERY, [])


def test_same_id_song_record_is_unchanged(tmp_path):
    record = _song_record(candidate_id=DELIVERY, source_candidate_id=DELIVERY)
    assert _resolve(record, {"candidate_id": DELIVERY}, tmp_path) == (DELIVERY, [])


@pytest.mark.parametrize(
    "record, review_item",
    [
        (_song_record(candidate_id="seededsong_1_2"), {"candidate_id": DELIVERY}),
        (_song_record(), {"candidate_id": SOURCE}),
        (_song_record(), {"candidate_id": "song_other_1"}),
    ],
    ids=["record-row-not-source", "review-item-uses-row-id", "review-item-other"],
)
def test_song_identity_drift_still_fails_closed(record, review_item, tmp_path):
    assert _resolve(record, review_item, tmp_path) == (
        None,
        ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"],
    )


def test_record_without_source_pair_keeps_generic_resolution(tmp_path):
    # Without the verified Song pair, candidate_id and delivery_candidate_id are
    # both declared publication identities and must agree, as before.
    record = {"candidate_id": "auto_1", "delivery_candidate_id": "auto_2"}
    assert _resolve(record, None, tmp_path) == (
        None,
        ["FINAL_MEDIA_REVIEW_CANDIDATE_ID_MISMATCH"],
    )
    assert _resolve({"candidate_id": "auto_1"}, {"candidate_id": "auto_1"}, tmp_path) == (
        "auto_1",
        [],
    )
