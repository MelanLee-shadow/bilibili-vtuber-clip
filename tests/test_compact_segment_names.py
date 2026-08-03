"""blrec-native (compact) segment naming compatibility .

The old bilive control plane (src.burn.scan) used to re-encode blrec's remuxed
`123456_20260709-20-00-28.mp4` into a dashed `123456_2026-07-09-20-00-28-.mp4`
and the runner only ever saw the dashed form.  With the old plane retired the
runner consumes blrec's compact output directly — these tests pin that the
name-sensitive helpers work for BOTH forms (old dates keep dashed deliveries).
"""
import scripts.session_autoslice as runner


def test_list_segments_accepts_compact_and_dashed_names(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path)
    date_dir = tmp_path / "2026-07-10"
    date_dir.mkdir()
    compact = date_dir / f"{runner.ROOM}_20260710-19-30-36.mp4"
    dashed = date_dir / f"{runner.ROOM}_2026-07-10-20-00-28-.mp4"
    compact.write_bytes(b"v")
    dashed.write_bytes(b"v")
    # non-segment files blrec leaves around must be ignored
    (date_dir / f"{runner.ROOM}_20260710-19-30-36.m4s").write_bytes(b"raw")
    (date_dir / f"{runner.ROOM}_20260710-19-30-36.flv").write_bytes(b"raw")
    (date_dir / f"{runner.ROOM}_20260710-19-30-36.m3u8").write_bytes(b"idx")
    # other rooms / nested files must not leak in
    (date_dir / "99999999_20260710-19-30-36.mp4").write_bytes(b"v")
    nested = date_dir / "sources"
    nested.mkdir()
    (nested / f"{runner.ROOM}_20260710-18-00-00.mp4").write_bytes(b"v")

    assert runner.list_segments("2026-07-10") == [dashed, compact]


def test_find_danmaku_xml_matches_compact_stem_in_parent_and_sources(tmp_path):
    date_dir = tmp_path / "2026-07-10"
    (date_dir / "sources").mkdir(parents=True)
    segment = date_dir / f"{runner.ROOM}_20260710-19-30-36.mp4"
    segment.write_bytes(b"v")
    # blrec writes the danmaku xml next to the recording (same compact stem)
    xml = date_dir / f"{runner.ROOM}_20260710-19-30-36.xml"
    xml.write_text("<i/>", encoding="utf-8")
    assert runner.find_danmaku_xml(segment) == xml

    # old dates: the retired render step moved sidecars into sources/ — the
    # dashed segment must still find the compact-named xml there (digit match)
    xml.unlink()
    moved = date_dir / "sources" / f"{runner.ROOM}_20260710-19-30-36.xml"
    moved.write_text("<i/>", encoding="utf-8")
    dashed_segment = date_dir / f"{runner.ROOM}_2026-07-10-19-30-36-.mp4"
    dashed_segment.write_bytes(b"v")
    assert runner.find_danmaku_xml(dashed_segment) == moved
    assert runner.find_danmaku_xml(segment) == moved

    # a different segment's xml (different digits) must never match
    other = date_dir / f"{runner.ROOM}_20260710-20-00-28.mp4"
    other.write_bytes(b"v")
    assert runner.find_danmaku_xml(other) is None
