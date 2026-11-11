"""Source-window overlap counts only when the two uploads are basically the same video.

维护者 U69 : "同样的视频不算重复。不是都说了我的标题不要改吗". A different video
that quotes the same recording is not a duplicate; the overlap must cover at least 80%
(administrative criterion) of BOTH uploads' own source time.
"""
from __future__ import annotations

from scripts.authorized_upload import append_ledger, ledger_guard
from src.autoslice.upload_ledger_scope import (
    SOURCE_OVERLAP_SAME_VIDEO_MIN_PERCENT, content_identity, row_matches_target)

from tests.test_upload_ledger_scope import bound_manifest

SRC = 'c' * 64


def identity(windows, *, source=SRC, candidate='cand', locator=''):
    return {'schema_version': 'authorized-upload-content-identity.v1',
            'candidate_ids': [candidate], 'lineage_candidate_ids': [], 'same_day_candidate_ids': [],
            'recording_date': '2026-10-13', 'package_locator': locator,
            'source_windows': [{'source_sha256': source, 'start_ms': a, 'end_ms': b} for a, b in windows]}


def match(previous_windows, target_windows, *, previous_source=SRC, target_source=SRC):
    row = {'video_sha256': 'old-video',
           'content_identity': identity(previous_windows, source=previous_source, candidate='prev')}
    target = identity(target_windows, source=target_source, candidate='tgt')
    return row_matches_target(row, 'new-video', {}, identity=target)


# Real data: single clip 1 (~137 s) vs the 101-piece compilation (~1854 s).
CLIP1 = [(1589620, 1726620)]
REAL_OVERLAPS = [(1589620, 1595687), (1607960, 1637393)]


def compilation_windows():
    others = [(2_000_000 + i * 20_000, 2_000_000 + i * 20_000 + 18_000) for i in range(99)]
    windows = REAL_OVERLAPS + others
    assert len(windows) == 101
    assert 1_800_000 < sum(b - a for a, b in windows) < 1_900_000
    return windows


def test_threshold_constant_is_eighty_percent():
    assert SOURCE_OVERLAP_SAME_VIDEO_MIN_PERCENT == 80


def test_witness_compilation_quoting_two_pieces_of_published_clip_is_not_a_duplicate():
    assert not match(CLIP1, compilation_windows())
    # Re-uploading clip 1 against the compilation row overlaps only ~26% of clip 1.
    assert not match(compilation_windows(), CLIP1)


def test_witness_through_ledger_guard_with_bound_manifests(tmp_path):
    clip = bound_manifest(tmp_path, 'clip1', pieces=[
        {'source_media_sha256': 'sha256:' + SRC, 'start_ms': a, 'end_ms': b} for a, b in CLIP1])
    compilation = bound_manifest(tmp_path, 'compilation', pieces=[
        {'source_media_sha256': 'sha256:' + SRC, 'start_ms': a, 'end_ms': b}
        for a, b in compilation_windows()])
    ledger = tmp_path / 'ledger.jsonl'
    append_ledger(ledger, {'video_sha256': 'clip1-video', 'rc': 0, 'bvid': 'BV1ABC234XYZ',
                          'content_identity': content_identity(clip)})
    assert ledger_guard(ledger, 'compilation-video', manifest=compilation) == (None, None, [])
    # A re-encode of the clip itself is still held.
    assert ledger_guard(ledger, 'clip1-reencode', manifest=clip)[0] == 'uploaded'


def test_guard_recut_of_same_span_under_new_candidate_id_matches():
    assert match([(1000, 9000)], [(1000, 9000)])


def test_guard_recut_with_ten_extra_seconds_each_side_matches():
    # Old 120 s, new 140 s, overlap 120 s: 86% of the new upload, 100% of the old one.
    old = [(110_000, 230_000)]
    new = [(100_000, 240_000)]
    assert match(old, new)
    # The same pair in the opposite direction is the same decision.
    assert match(new, old)


def test_guard_exactly_eighty_percent_both_ways_matches_inclusive():
    assert match([(0, 10000)], [(2000, 12000)])


def test_guard_reencode_same_source_window_matches():
    # Different video bytes, identical source window: row_matches_target never sees
    # the video SHA equal, so only the window identity can hold it.
    assert match([(5000, 65000)], [(5000, 65000)])


def test_guard_reencode_through_ledger_guard_is_held(tmp_path):
    pieces = [{'source_media_sha256': 'sha256:' + SRC, 'start_ms': 5000, 'end_ms': 65000}]
    original = bound_manifest(tmp_path, 'orig', pieces=pieces)
    reencode = bound_manifest(tmp_path, 'reenc', pieces=pieces)
    ledger = tmp_path / 'ledger.jsonl'
    append_ledger(ledger, {'video_sha256': 'orig-video', 'rc': 0, 'bvid': 'BV1ABC234XYZ',
                          'content_identity': content_identity(original)})
    assert ledger_guard(ledger, 'reencoded-video', manifest=reencode)[0] == 'uploaded'


def test_guard_extended_version_twice_as_long_does_not_match():
    # Previous clip fully inside the new upload: 50% of the new one, 100% of the old one.
    assert not match([(0, 5000)], [(0, 10000)])


def test_guard_compilation_mostly_made_of_one_clip_does_not_match():
    # 60% of the new compilation is the old clip, 100% of the old clip is in it.
    assert not match([(0, 6000)], [(0, 10000)])


def test_guard_truncated_cut_of_published_clip_does_not_match():
    # 100% of the new upload lies inside the old one, but only 70% of the old one.
    assert not match([(0, 10000)], [(0, 7000)])


def test_guard_one_millisecond_under_eighty_percent_does_not_match():
    # Overlap 7999 ms of 10000 ms on both sides = 79.99%.
    assert not match([(0, 10000)], [(2001, 12001)])
    assert not match([(2001, 12001)], [(0, 10000)])


def test_guard_same_time_span_on_a_different_source_does_not_match():
    assert not match([(0, 10000)], [(0, 10000)], previous_source='d' * 64)


def test_overlapping_windows_are_unioned_before_counting():
    # Target pieces overlap each other: union is 0-6000; history 0-5000 is 83% / 100%.
    assert match([(0, 5000)], [(0, 4000), (2000, 6000)])
    assert match([(0, 6000)], [(0, 4000), (2000, 6000)])
    # Union 0-6000 on the target, history 2000-4000: 33% of the target.
    assert not match([(2000, 4000)], [(0, 4000), (2000, 6000)])
    # Historical windows overlapping each other must not double count the shared slice:
    # union 0-6000 covers 60% of the 10000 ms target (naive sum 8000 would reach 80%).
    assert not match([(0, 4000), (2000, 6000)], [(0, 10000)])


def test_empty_windows_on_either_side_never_match_by_source_overlap():
    assert not match([(0, 10000)], [])
    assert not match([], [(0, 10000)])
    assert not match([], [])


def test_exact_identity_matches_do_not_depend_on_source_overlap():
    far = [(500000, 600000)]
    row = {'video_sha256': 'old-video', 'content_identity': identity([(0, 10)], candidate='shared')}
    assert row_matches_target(row, 'new-video', {}, identity=identity(far, candidate='shared'))
    assert row_matches_target(row, 'old-video', {}, identity=identity([], candidate='x'))
    row2 = {'video_sha256': 'old', 'content_identity': identity([], candidate='y', locator='/out/2026-10-13/shared')}
    assert row_matches_target(row2, 'new', {}, identity=identity(far, candidate='x', locator='/out/2026-10-13/shared'))
