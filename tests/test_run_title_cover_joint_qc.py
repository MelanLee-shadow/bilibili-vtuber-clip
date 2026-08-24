import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import (
    preflight_create_only_output,
    resolve_candidate_id,
    resolve_package_inputs,
    run_qc,
    write_receipt_create_only,
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _c2_package(root: Path, *, candidate: str = "auto_203011_328_389") -> tuple[str, Path]:
    title = "【李豆沙】C2 测试标题"
    stem = candidate + ".recut.burned-final-speaker"
    (root / f"{stem}.mp4").write_bytes(b"video")
    cover = root / f"{stem}.cover.png"
    cover.write_bytes(b"cover")
    _write_json(root / f"{stem}.record.json", {
        "schema_version": "lidousha-c2-release-record.v1", "candidate_id": candidate,
    })
    _write_json(root / f"{candidate}.recut.publish.json", {
        "title": title,
        "cover_generation": {"final_cover": cover.name, "final_cover_sha256": "sha256:" + hashlib.sha256(b"cover").hexdigest()},
    })
    _write_json(root / "review_manifest.json", {"items": [{
        "candidate_id": candidate, "title": title, "video": f"{stem}.mp4",
        "record": f"{stem}.record.json", "publish_json": f"{candidate}.recut.publish.json",
        "cover": f"{stem}.cover.png",
    }]})
    return title, cover


def test_resolve_candidate_id_accepts_exact_c2_root_binding() -> None:
    assert resolve_candidate_id(
        {"schema_version": "lidousha-c2-release-record.v1", "candidate_id": "c2"},
        {"items": [{"candidate_id": "c2"}]},
    ) == "c2"


@pytest.mark.parametrize("record,review", [
    ({"candidate_id": "c2"}, {"items": [{"candidate_id": "c2"}]}),
    ({"schema_version": "lidousha-c2-release-record.v1", "candidate_id": ""}, {"items": [{"candidate_id": "c2"}]}),
    ({"schema_version": "lidousha-c2-release-record.v1", "candidate_id": "c2"}, {"items": []}),
    ({"schema_version": "lidousha-c2-release-record.v1", "candidate_id": "c2"}, {"items": [{"candidate_id": "c2"}, {"candidate_id": "c2"}]}),
    ({"story_contract": {"candidate_id": "ordinary"}, "delivery_candidate_id": "conflict"}, {"items": []}),
])
def test_resolve_candidate_id_rejects_non_exact_or_conflicted_shapes(record: dict, review: dict) -> None:
    with pytest.raises(ValueError):
        resolve_candidate_id(record, review)


def test_preflight_rejects_existing_target_before_probe(tmp_path: Path) -> None:
    title, _cover = _c2_package(tmp_path)
    out_parent = tmp_path / "out"
    out_parent.mkdir(mode=0o700)
    out = out_parent / "receipt.json"
    out.write_text("existing", encoding="utf-8")
    called = False
    def probe(*_args):
        nonlocal called
        called = True
        raise AssertionError("probe must not run")
    with pytest.raises(FileExistsError):
        run_qc(tmp_path, title, out, image_probe=probe)
    assert not called
    assert out.read_text(encoding="utf-8") == "existing"


def test_preflight_rejects_missing_nonprivate_and_symlink_parents(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        preflight_create_only_output(tmp_path / "missing" / "receipt.json")
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    os.chmod(insecure, 0o755)
    with pytest.raises(ValueError, match="0700"):
        preflight_create_only_output(insecure / "receipt.json")
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        preflight_create_only_output(link / "receipt.json")
    leaf = real / "leaf.json"
    leaf.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FileExistsError):
        preflight_create_only_output(leaf)


def test_create_only_writer_handles_partial_and_zero_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    os.chmod(tmp_path, 0o700)
    parent_fd, name = preflight_create_only_output(tmp_path / "partial.json")
    original_write = os.write
    def partial(fd: int, data: bytes) -> int:
        return original_write(fd, data[:max(1, len(data) // 3)])
    monkeypatch.setattr(os, "write", partial)
    try:
        write_receipt_create_only(parent_fd, name, {"a": "x" * 40})
    finally:
        os.close(parent_fd)
    assert json.loads((tmp_path / name).read_text(encoding="utf-8"))["a"] == "x" * 40
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)
    parent_fd, name = preflight_create_only_output(tmp_path / "zero.json")
    try:
        with pytest.raises(OSError, match="short write"):
            write_receipt_create_only(parent_fd, name, {"a": 1})
    finally:
        os.close(parent_fd)
    assert not (tmp_path / name).exists()


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
