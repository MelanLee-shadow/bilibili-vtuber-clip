import hashlib
import json
from pathlib import Path

from scripts.run_title_cover_joint_qc import resolve_package_inputs


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_joint_qc_resolves_exact_same_stem_cover(tmp_path: Path) -> None:
    title = "【李豆沙】一条自动标题"
    candidate = "auto_test"
    stem = candidate + ".recut.burned-final-speaker"
    generated = tmp_path / "covers" / "generated.png"
    generated.parent.mkdir()
    generated.write_bytes(b"cover-bytes")
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"video")
    same_stem = tmp_path / f"{stem}.cover.png"
    same_stem.write_bytes(generated.read_bytes())
    digest = hashlib.sha256(generated.read_bytes()).hexdigest()
    record = tmp_path / f"{stem}.record.json"
    publish = tmp_path / f"{candidate}.recut.publish.json"
    _write_json(record, {"story_contract": {"candidate_id": candidate}})
    _write_json(
        publish,
        {
            "title": title,
            "cover_generation": {
                "final_cover": str(generated),
                "final_cover_sha256": "sha256:" + digest,
            },
        },
    )
    _write_json(
        tmp_path / "review_manifest.json",
        {
            "items": [
                {
                    "title": title,
                    "video": video.name,
                    "record": record.name,
                    "publish_json": publish.name,
                    "cover": same_stem.name,
                }
            ]
        },
    )

    loaded_record, loaded_publish, loaded_cover = resolve_package_inputs(
        tmp_path, title
    )

    assert loaded_record["story_contract"]["candidate_id"] == candidate
    assert loaded_publish["title"] == title
    assert loaded_cover == same_stem


def test_joint_qc_rejects_same_stem_cover_byte_drift(tmp_path: Path) -> None:
    title = "【李豆沙】一条自动标题"
    generated = tmp_path / "generated.png"
    generated.write_bytes(b"frozen")
    same_stem = tmp_path / "video.cover.png"
    same_stem.write_bytes(b"drift")
    (tmp_path / "video.mp4").write_bytes(b"video")
    digest = hashlib.sha256(generated.read_bytes()).hexdigest()
    _write_json(tmp_path / "video.record.json", {})
    _write_json(
        tmp_path / "source.publish.json",
        {
            "title": title,
            "cover_generation": {
                "final_cover": str(generated),
                "final_cover_sha256": "sha256:" + digest,
            },
        },
    )
    _write_json(
        tmp_path / "review_manifest.json",
        {
            "items": [
                {
                    "title": title,
                    "video": "video.mp4",
                    "record": "video.record.json",
                    "publish_json": "source.publish.json",
                    "cover": same_stem.name,
                }
            ]
        },
    )

    try:
        resolve_package_inputs(tmp_path, title)
    except ValueError as exc:
        assert "differs from frozen" in str(exc)
    else:
        raise AssertionError("same-stem cover drift was accepted")


def test_joint_qc_rejects_wrong_stem_cover(tmp_path: Path) -> None:
    title = "【李豆沙】一条自动标题"
    (tmp_path / "video.mp4").write_bytes(b"video")
    (tmp_path / "wrong.cover.png").write_bytes(b"cover")
    (tmp_path / "video.record.json").write_text("{}", encoding="utf-8")
    (tmp_path / "source.publish.json").write_text("{}", encoding="utf-8")
    _write_json(
        tmp_path / "review_manifest.json",
        {"items": [{
            "title": title,
            "video": "video.mp4",
            "record": "video.record.json",
            "publish_json": "source.publish.json",
            "cover": "wrong.cover.png",
        }]},
    )

    try:
        resolve_package_inputs(tmp_path, title)
    except ValueError as exc:
        assert "not same-stem" in str(exc)
    else:
        raise AssertionError("wrong-stem cover was accepted")


def test_joint_qc_accepts_copied_song_cover_when_hash_matches(tmp_path: Path) -> None:
    """Song review packages are copied byte-for-byte into an independent
    package root by build_lidousha_song_review_manifest.py; publish.json's
    cover_generation still names the pre-copy delivery path, which no longer
    exists once the review package is the only surviving copy.  The
    same-stem in-package cover must be trusted once its bytes hash matches
    the frozen cover-generation hash -- exercised here via the
    ``publish.cover_path`` fallback key (no ``final_cover`` in generation).
    """

    title = "【李豆沙】豆沙歌，《泡沫 Bubble》"
    candidate = "song_test"
    stem = candidate + ".recut.burned-final-speaker"
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"video")
    same_stem = tmp_path / f"{stem}.cover.png"
    same_stem.write_bytes(b"song-cover-bytes")
    digest = hashlib.sha256(same_stem.read_bytes()).hexdigest()
    # The original delivery directory this path names does not exist inside
    # this test's tmp_path tree at all -- the fix must never touch it.
    gone_delivery_path = tmp_path.parent / "gone-delivery-day" / "generated.png"
    record = tmp_path / f"{stem}.record.json"
    publish = tmp_path / f"{candidate}.recut.publish.json"
    _write_json(record, {"story_contract": {"candidate_id": candidate}})
    _write_json(
        publish,
        {
            "title": title,
            "cover_path": str(gone_delivery_path),
            "cover_generation": {
                "final_cover_sha256": "sha256:" + digest,
            },
        },
    )
    _write_json(
        tmp_path / "review_manifest.json",
        {
            "items": [
                {
                    "title": title,
                    "video": video.name,
                    "record": record.name,
                    "publish_json": publish.name,
                    "cover": same_stem.name,
                }
            ]
        },
    )

    assert not gone_delivery_path.exists()

    loaded_record, loaded_publish, loaded_cover = resolve_package_inputs(
        tmp_path, title
    )

    assert loaded_record["story_contract"]["candidate_id"] == candidate
    assert loaded_publish["title"] == title
    assert loaded_cover == same_stem


def test_joint_qc_rejects_copied_song_cover_hash_mismatch(tmp_path: Path) -> None:
    """Same copied-package shape as above, but the in-package cover bytes do
    not match the frozen cover-generation hash -- must still hard-fail, and
    must not read the escaped (nonexistent) delivery path to try to recover.
    """

    title = "【李豆沙】豆沙歌，《泡沫 Bubble》"
    candidate = "song_test"
    stem = candidate + ".recut.burned-final-speaker"
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"video")
    same_stem = tmp_path / f"{stem}.cover.png"
    same_stem.write_bytes(b"drifted-song-cover-bytes")
    frozen_digest = hashlib.sha256(b"original-song-cover-bytes").hexdigest()
    gone_delivery_path = tmp_path.parent / "gone-delivery-day" / "generated.png"
    record = tmp_path / f"{stem}.record.json"
    publish = tmp_path / f"{candidate}.recut.publish.json"
    _write_json(record, {"story_contract": {"candidate_id": candidate}})
    _write_json(
        publish,
        {
            "title": title,
            "cover_generation": {
                "final_cover": str(gone_delivery_path),
                "final_cover_sha256": "sha256:" + frozen_digest,
            },
        },
    )
    _write_json(
        tmp_path / "review_manifest.json",
        {
            "items": [
                {
                    "title": title,
                    "video": video.name,
                    "record": record.name,
                    "publish_json": publish.name,
                    "cover": same_stem.name,
                }
            ]
        },
    )

    assert not gone_delivery_path.exists()

    try:
        resolve_package_inputs(tmp_path, title)
    except ValueError as exc:
        assert "escapes package root" in str(exc)
    else:
        raise AssertionError("mismatched copied-package cover was accepted")


def test_joint_qc_rejects_symlinked_record(tmp_path: Path) -> None:
    title = "【李豆沙】一条自动标题"
    (tmp_path / "video.mp4").write_bytes(b"video")
    external = tmp_path.parent / "external-record.json"
    external.write_text("{}", encoding="utf-8")
    (tmp_path / "video.record.json").symlink_to(external)
    (tmp_path / "video.cover.png").write_bytes(b"cover")
    (tmp_path / "source.publish.json").write_text("{}", encoding="utf-8")
    _write_json(
        tmp_path / "review_manifest.json",
        {"items": [{
            "title": title,
            "video": "video.mp4",
            "record": "video.record.json",
            "publish_json": "source.publish.json",
            "cover": "video.cover.png",
        }]},
    )

    try:
        resolve_package_inputs(tmp_path, title)
    except ValueError as exc:
        assert "traverses a symlink" in str(exc)
    else:
        raise AssertionError("symlinked record was accepted")
