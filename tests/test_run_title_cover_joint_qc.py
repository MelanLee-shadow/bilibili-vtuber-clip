import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.run_title_cover_joint_qc import (
    build_joint_qc_receipt,
    preflight_create_only_output,
    package_story_context,
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


def _event_package(root: Path):
    from src.autoslice.story_contract import cover_story_contract_binding

    title, cover = _c2_package(root)
    review = json.loads((root / "review_manifest.json").read_text())
    item = review["items"][0]
    record_path, publish_path = root / item["record"], root / item["publish_json"]
    record, publish = json.loads(record_path.read_text()), json.loads(publish_path.read_text())
    hook = "读到游戏女主对青梅心动，主播被观众调侃后两次否认。"
    story = {
        "schema_version": "lidousha-story-contract.v1", "candidate_id": item["candidate_id"],
        "selection_hook": hook,
        "selection_hook_sha256": "sha256:" + hashlib.sha256(hook.encode()).hexdigest(),
        "relation_state": "UNKNOWN", "participants": [],
        "cover_counterpart_reference_available": False, "cover_reference_authority": None,
        "source_media_sha256s": ["sha256:" + "1" * 64],
        "clip_context_binding": {"context_sha256": "sha256:" + "2" * 64},
        "cover_fallback_mode": "HOST_ONLY_GENERIC",
        "boundary_semantic_review": None, "human_boundary_authority": None,
    }
    record["story_contract"] = story
    publish["cover_generation"]["story_contract"] = cover_story_contract_binding(story)
    _write_json(record_path, record)
    _write_json(publish_path, publish)
    out_parent = root / "out"
    out_parent.mkdir(mode=0o700)
    return title, cover, record_path, publish_path, out_parent / "qc.json"


@pytest.mark.parametrize("accepted", [True, False])
def test_current_bound_event_reaches_pixel_review_without_overriding_verdict(tmp_path, accepted):
    title, cover, _, _, out = _event_package(tmp_path)
    verdict = _joint_verdict(**{"pass": accepted})
    prompts = []
    def probe(path, prompt):
        assert path == cover
        prompts.append(prompt)
        return {"status": "OBSERVED", "answer": json.dumps(verdict)}
    receipt = run_qc(tmp_path, title, out, image_probe=probe)
    assert len(prompts) == 1
    assert "游戏女主对青梅心动" in prompts[0]
    assert receipt["source_story_context"]["context_sha256"] in prompts[0]
    assert receipt["verdict"] == verdict
    assert receipt["pass"] is accepted


@pytest.mark.parametrize("drift", ["candidate", "hook_hash", "projection"])
def test_event_binding_drift_stops_before_provider(tmp_path, drift):
    title, _, record_path, publish_path, out = _event_package(tmp_path)
    record, publish = json.loads(record_path.read_text()), json.loads(publish_path.read_text())
    if drift == "candidate":
        record["story_contract"]["candidate_id"] = "another"
    elif drift == "hook_hash":
        record["story_contract"]["selection_hook_sha256"] = "sha256:" + "0" * 64
    else:
        publish["cover_generation"]["story_contract"]["selection_hook"] = "无源新事件"
    _write_json(record_path, record)
    _write_json(publish_path, publish)
    def forbidden(*_):
        raise AssertionError("provider must not consume drifted story")
    with pytest.raises(ValueError):
        run_qc(tmp_path, title, out, image_probe=forbidden)
    assert not out.exists()


def test_story_changed_during_probe_cannot_seal_receipt(tmp_path):
    title, _, record_path, _, out = _event_package(tmp_path)
    def probe(*_):
        record = json.loads(record_path.read_text())
        record["story_contract"]["selection_hook"] = "changed during call"
        _write_json(record_path, record)
        return {"status": "OBSERVED", "answer": json.dumps(_joint_verdict())}
    with pytest.raises(ValueError, match="story"):
        run_qc(tmp_path, title, out, image_probe=probe)
    assert not out.exists()


@pytest.mark.parametrize("drift", [None, "subtitle", "cover", "title", "story", "response"])
def test_joint_qc_consumes_current_editorial_bytes_before_provider(tmp_path, drift):
    from copy import deepcopy
    from src.autoslice.publish_staging_paths import current_editorial_binding
    title, cover, record_path, publish_path, out = _event_package(tmp_path)
    manifest = json.loads((tmp_path / "review_manifest.json").read_text())
    item = manifest["items"][0]
    subtitle = tmp_path / "final.srt"
    subtitle.write_bytes(b"current subtitle")
    response = tmp_path / "cover-response.json"
    response.write_bytes(b"actual completed cover response")
    item.update(subtitle_srt=subtitle.name, cover_response=response.name)
    record, publish = json.loads(record_path.read_text()), json.loads(publish_path.read_text())
    record["subtitle_path"] = str(subtitle)
    staging = {**deepcopy(publish), "candidate_id": item["candidate_id"], "cover_path": str(cover)}
    staging["cover_generation"].update(response_path=str(response), response_sha256="sha256:" + hashlib.sha256(response.read_bytes()).hexdigest())
    staging["current_binding"] = current_editorial_binding(record, staging, candidate_id=item["candidate_id"])
    record["publish_staging"] = deepcopy(staging)
    publish = deepcopy(staging)
    publish["subtitle_sha256"] = staging["current_binding"]["subtitle_sha256"]
    if drift == "subtitle":
        subtitle.write_bytes(b"old subtitle")
    if drift == "cover":
        cover.write_bytes(b"old cover")
    if drift == "title":
        publish["title"] += "changed"
        manifest["items"][0]["title"] = publish["title"]
        title = publish["title"]
    if drift == "story":
        record["story_contract"]["selection_hook"] = "different final story"
    if drift == "response":
        response.write_bytes(b"old response")
    _write_json(record_path, record)
    _write_json(publish_path, publish)
    _write_json(tmp_path / "review_manifest.json", manifest)
    calls = []
    def probe(*_args):
        calls.append(True)
        return {"status": "OBSERVED", "answer": json.dumps(_joint_verdict())}
    if drift is None:
        run_qc(tmp_path, title, out, image_probe=probe)
        assert calls == [True]
    else:
        with pytest.raises(ValueError, match="CURRENT_EDITORIAL"):
            run_qc(tmp_path, title, out, image_probe=probe)
        assert calls == []
        assert not out.exists()


def test_story_context_hash_changes_with_source_binding(tmp_path):
    _, _, record_path, publish_path, _ = _event_package(tmp_path)
    record, publish = json.loads(record_path.read_text()), json.loads(publish_path.read_text())
    first = package_story_context(record=record, generation=publish["cover_generation"],
                                  candidate_id=record["candidate_id"])
    record["story_contract"]["source_media_sha256s"] = ["sha256:" + "3" * 64]
    publish["cover_generation"]["story_contract"]["source_media_sha256s"] = ["sha256:" + "3" * 64]
    second = package_story_context(record=record, generation=publish["cover_generation"],
                                   candidate_id=record["candidate_id"])
    assert first["context_sha256"] != second["context_sha256"]


def test_joint_qc_consumes_hash_bound_final_srt_context(tmp_path):
    title, _cover, record_path, publish_path, out = _event_package(tmp_path)
    final_srt = tmp_path / "final.srt"
    srt_text = "1\n00:00:00,000 --> 00:00:01,000\n她先听到过去转述，再当场否认。\n"
    final_srt.write_text(srt_text, encoding="utf-8")
    record = json.loads(record_path.read_text())
    record["subtitle_path"] = str(final_srt)
    _write_json(record_path, record)
    publish = json.loads(publish_path.read_text())
    publish["cover_generation"]["art_direction"] = {
        "is_song": False,
        "research_application": {
            "choice": "APPLY",
            "applicable": True,
            "reason": "A current ordinary talk research choice.",
        },
    }
    _write_json(publish_path, publish)
    prompts = []

    def probe(_path, prompt):
        prompts.append(prompt)
        return {"status": "OBSERVED", "answer": json.dumps(_joint_verdict())}

    receipt = run_qc(tmp_path, title, out, image_probe=probe)
    final_context = receipt["source_story_context"]["final_srt"]
    assert final_context["status"] == "AVAILABLE"
    assert final_context["sha256"] == "sha256:" + hashlib.sha256(srt_text.encode()).hexdigest()
    assert final_context["text"] == srt_text
    assert prompts[0].count(srt_text) == 1


@pytest.mark.parametrize("drift", [None, "receipt", "source"])
def test_upload_replay_checks_current_event_context(tmp_path, drift):
    from scripts.run_title_cover_joint_qc import reuse_valid_qc

    title, cover, record_path, publish_path, out = _event_package(tmp_path)
    def probe(path, _prompt):
        return {"schema_version": "cpa-frame-witness.v1", "status": "OBSERVED", "provider": "cpa",
                "model": "synthetic", "image_path": str(path.resolve()),
                "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "answer": json.dumps(_joint_verdict())}
    receipt = run_qc(tmp_path, title, out, image_probe=probe)
    if drift == "receipt":
        receipt["source_story_context"]["selection_hook"] = "unobserved claim"
        _write_json(out, receipt)
    elif drift == "source":
        record, publish = json.loads(record_path.read_text()), json.loads(publish_path.read_text())
        record["story_contract"]["source_media_sha256s"] = ["sha256:" + "4" * 64]
        publish["cover_generation"]["story_contract"]["source_media_sha256s"] = ["sha256:" + "4" * 64]
        _write_json(record_path, record)
        _write_json(publish_path, publish)
    if drift is None:
        assert reuse_valid_qc(tmp_path, title, out) == receipt
    else:
        with pytest.raises(ValueError, match="source story context"):
            reuse_valid_qc(tmp_path, title, out)


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


def test_create_only_writer_preserves_concurrent_replacement_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.chmod(tmp_path, 0o700)
    target = tmp_path / "race.json"
    parent_fd, name = preflight_create_only_output(target)

    def replace_then_zero(_fd: int, _data: bytes) -> int:
        os.unlink(name, dir_fd=parent_fd)
        target.write_bytes(b"replacement-bytes")
        return 0

    monkeypatch.setattr(os, "write", replace_then_zero)
    try:
        with pytest.raises(OSError, match="short write"):
            write_receipt_create_only(parent_fd, name, {"a": 1})
    finally:
        os.close(parent_fd)
    assert target.read_bytes() == b"replacement-bytes"


def test_create_only_writer_preserves_original_error_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.chmod(tmp_path, 0o700)
    target = tmp_path / "unlink-failure.json"
    parent_fd, name = preflight_create_only_output(target)
    monkeypatch.setattr(os, "write", lambda _fd, _data: 0)

    def fail_unlink(*_args, **_kwargs) -> None:
        raise OSError("cleanup denied")

    monkeypatch.setattr(os, "unlink", fail_unlink)
    try:
        with pytest.raises(OSError, match="short write"):
            write_receipt_create_only(parent_fd, name, {"a": 1})
    finally:
        os.close(parent_fd)
    assert target.exists()


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
    package root by build_song_review_manifest.py; publish.json's
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


def _joint_verdict(**changes) -> dict:
    return {
        "lidousha_primary": True, "thumbnail_readable": True,
        "single_clear_hook": True, "text_overcrowded": False,
        "title_cover_aligned": True, "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Visible host and readable text carry the spoken hook.",
        "research_application": {
            "applicable": True,
            "reason": "The host's supported response is the story hook; a single-host visual choice is sufficient.",
            "commitment_fidelity": "SUPPORTED",
            "naturalness": "NATURAL",
            "click_reason": "The concrete response gives a stranger a clear event to understand.",
        },
        "pass": True, **changes,
    }


def _ordinary_talk_redraw_generation() -> dict:
    from src.autoslice.cover_generation import ROOT, ordinary_talk_redraw_style_binding

    binding = ordinary_talk_redraw_style_binding(
        hook_color="pink",
        style_reference_path=ROOT / "assets/lidousha/cover_style/ordinary_talk_reference.png",
    )
    assert binding is not None and binding["status"] == "READY"
    palette = binding["text_palette"]
    return {
        "method": "images.edit",
        "cover_origin": "AI_REDRAW",
        "art_direction": {
            "is_song": False,
            "hook_color": palette["primary"],
            "secondary_fill": palette["secondary"],
            "research_application": {
                "schema_version": "lidousha-cover-research-application.v1",
                "choice": "APPLY",
                "applicable": True,
                "reason": "The specific host response is the selected story hook.",
            },
        },
        "route_decision": {
            "selected_treatment": "cpa_redraw",
            "actual_treatment": "cpa_redraw",
            "scene_kind": "talk",
            "relationship_visual_required": False,
        },
        "art_direction_style": binding,
        "style_reference_images": [binding["reference_image"]],
        "rendered_text_pixels": {
            "render_spec": {"text_palette": palette},
        },
    }


@pytest.mark.parametrize("style_matches", [True, False])
def test_joint_qc_checks_research_choice_without_requiring_style_sample(tmp_path, style_matches):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"final redraw pixels without a style sample")
    generation = _ordinary_talk_redraw_generation()
    generation["art_direction_style"].pop("reference_image")
    generation.pop("style_reference_images")
    prompts = []

    def probe(path, question):
        assert path == cover
        assert "第二张图" not in question
        prompts.append(question)
        return {
            "status": "OBSERVED", "image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "answer": json.dumps(_joint_verdict(style_matches_requested=style_matches)),
        }

    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】普通杂谈风格测试", candidate_id="no-sample",
        image_probe=probe, cover_generation=generation,
    )
    assert result["status"] == "PASS"
    assert result["style_expectation"]["binding_status"] == "BOUND"
    assert "reference_image" not in result["style_expectation"]["expected_asset"]
    assert result["research_application"]["applicable"] is True
    assert len(prompts) == 1


def test_unavailable_optional_sample_does_not_block_text_art_direction(tmp_path):
    from src.autoslice import cover_generation

    binding = cover_generation.ordinary_talk_redraw_style_binding(
        style_reference_path=tmp_path / "absent-optional-sample.png",
    )
    assert binding["status"] == "READY"
    assert binding["text"]
    assert "reference_image" not in binding
    assert binding["reference_image_unavailable"] is True


def test_joint_qc_binds_profile_style_and_passes_it_to_the_real_probe(tmp_path):
    cover = tmp_path / "ordinary.cover.png"
    cover.write_bytes(b"ordinary redraw pixels")
    generation = _ordinary_talk_redraw_generation()
    prompts = []

    def probe(path, prompt, *, reference_image_path):
        prompts.append(prompt)
        assert str(reference_image_path) == generation["art_direction_style"]["reference_image"]["path"]
        return {
            "status": "OBSERVED",
            "reference_image": {"image_sha256": generation["art_direction_style"]["reference_image"]["sha256"].removeprefix("sha256:")},
            "answer": json.dumps(_joint_verdict(style_matches_requested=True)),
        }

    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】普通杂谈风格测试",
        candidate_id="ordinary-style",
        image_probe=probe,
        cover_generation=generation,
    )
    assert len(prompts) == 1
    assert "PROFILE ART-DIRECTION ASSET" not in prompts[0]
    assert "浅青纸感、粗深蓝线和可见笔触" in prompts[0]
    assert generation["art_direction_style"]["text"] in prompts[0]
    assert "style_matches_requested" in prompts[0]
    assert result["style_expectation"]["binding_status"] == "BOUND"
    assert result["verdict"]["style_matches_requested"] is True
    assert result["style_matches_requested"] is True
    assert result["status"] == "PASS"


def test_joint_qc_does_not_turn_legacy_style_keyword_into_a_gate(tmp_path):
    cover = tmp_path / "polished.cover.png"
    cover.write_bytes(b"clean polished source-led pixels")
    generation = _ordinary_talk_redraw_generation()
    generation["art_direction_style"].pop("reference_image")
    generation.pop("style_reference_images")
    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】普通杂谈风格反例",
        candidate_id="ordinary-polished",
        image_probe=lambda *_, **_kw: {
            "status": "OBSERVED",
            "answer": json.dumps(_joint_verdict(style_matches_requested=False)),
        },
        cover_generation=generation,
    )
    assert result["style_matches_requested"] is False
    assert result["status"] == "PASS"
    assert result["pass"] is True


@pytest.mark.parametrize("reference_state", ["missing", "not_sent", "wrong_observed_hash"])
def test_joint_qc_cannot_accept_a_true_style_verdict_without_consumed_sample(tmp_path, reference_state):
    cover = tmp_path / "ordinary.cover.png"
    cover.write_bytes(b"ordinary redraw pixels")
    generation = _ordinary_talk_redraw_generation()
    reference = generation["art_direction_style"]["reference_image"]
    if reference_state == "missing":
        generation.pop("style_reference_images")
    elif reference_state == "not_sent":
        generation["style_reference_images"][0] = {**reference, "sent": False, "status": "NOT_SENT_DEDUPLICATED"}
    observed_hash = "0" * 64 if reference_state == "wrong_observed_hash" else reference["sha256"].removeprefix("sha256:")
    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】普通杂谈风格测试", candidate_id="missing-style",
        image_probe=lambda *_, **_kw: {
            "status": "OBSERVED", "reference_image": {"image_sha256": observed_hash},
            "answer": json.dumps(_joint_verdict(style_matches_requested=True)),
        },
        cover_generation=generation,
    )
    assert result["status"] == "FAIL"
    assert result["style_matches_requested"] is False


@pytest.mark.parametrize(
    "style_matches_requested, expected_status",
    [(True, "PASS"), (False, "PASS")],
)
def test_joint_qc_checks_builtin_imagegen_ordinary_style(
    tmp_path, style_matches_requested, expected_status
):
    cover = tmp_path / "builtin.cover.png"
    cover.write_bytes(b"builtin redraw pixels")
    generation = _ordinary_talk_redraw_generation()
    generation.update(
        {
            "method": "image_gen.imagegen",
            "cover_origin": "BUILTIN_AI_REDRAW",
        }
    )
    # The imported builtin lane may carry only its route and trusted render
    # spec; the QC must still recognize it as an ordinary talk redraw.
    generation.pop("art_direction")
    generation["route_decision"].update(
        {
            "selected_treatment": "builtin_imagegen_redraw",
            "actual_treatment": "builtin_imagegen_redraw",
        }
    )
    prompts = []
    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】builtin 普通杂谈风格",
        candidate_id="builtin-style",
        image_probe=lambda _path, prompt, **_kw: (
            prompts.append(prompt)
            or {
                "status": "OBSERVED",
                "reference_image": {"image_sha256": generation["art_direction_style"]["reference_image"]["sha256"].removeprefix("sha256:")},
                "answer": json.dumps(
                    _joint_verdict(style_matches_requested=style_matches_requested)
                ),
            }
        ),
        cover_generation=generation,
    )
    assert result["style_expectation"]["binding_status"] == "BOUND"
    assert result["style_matches_requested"] is style_matches_requested
    assert result["status"] == expected_status
    assert "制作采用以下当前 profile 画风要求" in prompts[0]
    assert "实际 builtin prompt 或 source provenance" in prompts[0]
    assert "生成阶段声明实际消费了以下 profile 风格资产" not in prompts[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"art_direction": {"is_song": True}},
        {"route_decision": {"selected_treatment": "screenshot_direct", "actual_treatment": "screenshot_direct", "scene_kind": "game", "relationship_visual_required": False}},
        {"route_decision": {"selected_treatment": "cpa_redraw", "actual_treatment": "cpa_redraw", "scene_kind": "talk", "relationship_visual_required": True}},
    ],
)
def test_joint_qc_style_requirement_is_excluded_for_song_game_and_relation(tmp_path, overrides):
    cover = tmp_path / "excluded.cover.png"
    cover.write_bytes(b"excluded route pixels")
    generation = _ordinary_talk_redraw_generation()
    generation.update(overrides)
    if "route_decision" in overrides:
        generation["method"] = "images.edit"
        generation["cover_origin"] = "AI_REDRAW"
    prompts = []
    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】非普通杂谈路线",
        candidate_id="excluded-style",
        image_probe=lambda _path, prompt, **_kw: (
            prompts.append(prompt)
            or {"status": "OBSERVED", "answer": json.dumps(_joint_verdict())}
        ),
        cover_generation=generation,
    )
    assert result["style_matches_requested"] is None
    assert "style_expectation" not in result
    assert "style_matches_requested" not in prompts[0]
    assert result["status"] == "PASS"


def test_joint_qc_does_not_silently_skip_registered_style_when_binding_missing(tmp_path):
    cover = tmp_path / "unbound.cover.png"
    cover.write_bytes(b"unbound redraw pixels")
    generation = _ordinary_talk_redraw_generation()
    generation.pop("art_direction_style")
    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】缺少风格绑定",
        candidate_id="ordinary-unbound",
        image_probe=lambda *_, **_kw: {
            "status": "OBSERVED",
            "answer": json.dumps(_joint_verdict(style_matches_requested=True)),
        },
        cover_generation=generation,
    )
    assert result["style_expectation"]["binding_status"] == "MISSING_DECLARATION"
    assert result["style_matches_requested"] is False
    assert result["status"] == "FAIL"
    assert result["pass"] is False


def test_joint_qc_rejects_style_when_render_palette_binding_is_missing(tmp_path):
    cover = tmp_path / "unbound-palette.cover.png"
    cover.write_bytes(b"unbound palette pixels")
    generation = _ordinary_talk_redraw_generation()
    generation.pop("text_palette", None)
    generation["rendered_text_pixels"]["render_spec"].pop("text_palette")
    result = build_joint_qc_receipt(
        cover_path=cover,
        title="【李豆沙】缺少渲染配色绑定",
        candidate_id="ordinary-unbound-palette",
        image_probe=lambda *_, **_kw: {
            "status": "OBSERVED",
            "answer": json.dumps(_joint_verdict(style_matches_requested=True)),
        },
        cover_generation=generation,
    )
    assert result["style_expectation"]["binding_status"] == "MISSING_PALETTE_BINDING"
    assert result["style_matches_requested"] is False
    assert result["status"] == "FAIL"


def test_ordinary_talk_binding_records_research_provenance_and_selected_palette():
    from src.autoslice.cover_generation import ordinary_talk_redraw_style_binding

    binding = ordinary_talk_redraw_style_binding(hook_color="purple")
    assert binding["status"] == "READY"
    assert binding["text_palette"]["primary"] == "purple"
    assert binding["text_palette"]["secondary"] == "yellow"
    projection = binding["research_projection"]
    assert projection["version"] == "public-cover-art-direction.v1"
    assert projection["source_title"] == "cover_art_direction.md"
    assert projection["handoff_title"] == "cover_art_direction.md"
    assert not projection["source_path"].startswith("/")
    assert not projection["handoff_path"].startswith("/")
    assert projection["source_path"].endswith(
        "assets/lidousha/cover_art_direction.md"
    )
    assert projection["handoff_path"].endswith(
        "assets/lidousha/cover_art_direction.md"
    )
    assert projection["source_sha256"] == (
        "sha256:4363435809aedceb22417fdaa316b5109b90c57c3e12be6ef4d24174d523cd80"
    )
    assert projection["handoff_sha256"] == (
        "sha256:4363435809aedceb22417fdaa316b5109b90c57c3e12be6ef4d24174d523cd80"
    )
    assert projection["primary_public_urls"] == [
        "https://www.bilibili.com/video/BV13Sac66Eji",
        "https://www.bilibili.com/video/BV1uKuC6hE9j",
        "https://www.bilibili.com/video/BV1GEYp6NEPJ",
        "https://www.bilibili.com/video/BV1g8Hp6qEGy",
        "https://www.bilibili.com/video/BV1PbHt6CEeJ",
        "https://www.bilibili.com/video/BV1RGY36zETk",
        "https://www.bilibili.com/video/BV1ScGN6cE47",
    ]
    assert projection["applicable_to"]
    assert projection["limitations"]


def test_joint_qc_requires_actual_research_semantic_record(tmp_path):
    cover = tmp_path / "ordinary.cover.png"
    cover.write_bytes(b"ordinary redraw pixels")
    generation = _ordinary_talk_redraw_generation()

    def probe(*_args, **_kwargs):
        verdict = _joint_verdict()
        verdict.pop("research_application")
        return {"status": "OBSERVED", "answer": json.dumps(verdict)}

    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】研究记录缺失", candidate_id="missing-research",
        image_probe=probe, cover_generation=generation,
    )
    assert result["status"] == "FAIL"
    assert result["pass"] is False
    assert result["research_application"] is None


def test_director_and_generation_prompt_consume_story_research_choice():
    from src.autoslice.cover_generation import (
        LidoushaCoverArtDirection,
        _cover_art_direction_prompt,
        _cover_prompt,
        _normalize_cover_art_direction,
    )

    baseline = LidoushaCoverArtDirection(
        role="shocked_bites_back",
        expression_en="wide eyes and a supported startled reaction",
        background_style="source-led",
        layout="banner",
        hook_color="purple",
        is_song=False,
    )
    director_prompt = _cover_art_direction_prompt(
        title="【李豆沙】她先听到消息再当场否认",
        cover_text="听到消息？当场否认！",
        story_hook="她先听到过去转述的消息，随后当场否认，不是新的现实事件。",
        baseline=baseline,
    )
    assert "research_application" in director_prompt
    assert "固定画风" in director_prompt
    assert "4363435809aedceb22417fdaa316b5109b90c57c3e12be6ef4d24174d523cd80" in director_prompt

    direction = _normalize_cover_art_direction(
        {
            "research_application": {
                "applicable": False,
                "reason": "本条是过去转述后的当场否认，另选更清楚的回应构图。",
            },
        },
        baseline,
        "听到消息？当场否认！",
        ordinary_talk_redraw=True,
    )
    assert direction.research_application["choice"] == "DO_NOT_APPLY"
    generation_prompt = _cover_prompt(
        title="【李豆沙】她先听到消息再当场否认",
        cover_text="听到消息？当场否认！",
        art_direction=direction,
        style_binding=None,
    )
    assert "choice=DO_NOT_APPLY" in generation_prompt
    assert "Do not ignore conflicting visual-brief instructions" in generation_prompt
    assert "permanent style" in generation_prompt


def test_director_prompt_consumes_current_story_context_and_bound_srt():
    from src.autoslice.cover_generation import (
        LidoushaCoverArtDirection,
        _cover_art_direction_prompt,
    )

    baseline = LidoushaCoverArtDirection(
        role="shocked_bites_back",
        expression_en="supported startled reaction",
        background_style="source-led",
        layout="banner",
        hook_color="purple",
        is_song=False,
    )
    final_srt = "1\n00:00:00,000 --> 00:00:01,000\n她听到过去转述，马上否认。\n"
    final_srt_sha = "sha256:" + hashlib.sha256(final_srt.encode()).hexdigest()
    prompt = _cover_art_direction_prompt(
        title="【李豆沙】听到转述后当场否认",
        cover_text="听到转述？当场否认！",
        story_hook="她先听到过去转述，随后当场否认。",
        baseline=baseline,
        research_story_context={
            "candidate_id": "ordinary-context",
            "selection_hook": "她先听到过去转述，随后当场否认。",
            "selection_hook_sha256": "sha256:hook",
            "final_srt": {"status": "AVAILABLE", "sha256": final_srt_sha, "text": final_srt},
        },
    )
    assert json.dumps(final_srt, ensure_ascii=False) in prompt
    assert final_srt_sha in prompt
    assert "当前语义判断" in prompt
    assert "generation rationale" in prompt


def test_director_prompt_blocks_drifted_bound_srt():
    from src.autoslice.cover_generation import (
        LidoushaCoverArtDirection,
        _cover_art_direction_prompt,
    )

    baseline = LidoushaCoverArtDirection(
        role="shocked_bites_back",
        expression_en="supported startled reaction",
        background_style="source-led",
        layout="banner",
        hook_color="purple",
        is_song=False,
    )
    final_srt = "1\n00:00:00,000 --> 00:00:01,000\n正文发生漂移。\n"
    prompt = _cover_art_direction_prompt(
        title="【李豆沙】正文漂移",
        cover_text="正文漂移？",
        story_hook="仅有旧 hook。",
        baseline=baseline,
        research_story_context={
            "final_srt": {"status": "AVAILABLE", "sha256": "sha256:" + "0" * 64, "text": final_srt},
        },
    )
    assert "FINAL_SRT_HASH_MISMATCH" in prompt
    assert json.dumps(final_srt, ensure_ascii=False) not in prompt


def test_regenerate_direction_metadata_retains_research_choice():
    from scripts.regenerate_channel_cover import _art_direction_metadata
    from src.autoslice.cover_generation import LidoushaCoverArtDirection

    direction = LidoushaCoverArtDirection(
        role="shocked_bites_back",
        expression_en="supported startled reaction",
        background_style="source-led",
        layout="banner",
        hook_color="purple",
        is_song=False,
        research_application={
            "schema_version": "lidousha-cover-research-application.v1",
            "choice": "DO_NOT_APPLY",
            "applicable": False,
            "reason": "The source story needs a different visual language.",
        },
    )
    metadata = _art_direction_metadata(direction)
    assert metadata["research_application"]["choice"] == "DO_NOT_APPLY"
    assert metadata["research_application"]["applicable"] is False


def test_joint_question_distinguishes_background_from_factual_contradiction(tmp_path):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"frozen source-backed cover")
    prompts = []

    def probe(path, question):
        assert path == cover
        prompts.append(question)
        return {"status": "OBSERVED", "answer": json.dumps(_joint_verdict())}

    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】一次口头回应", candidate_id="test",
        image_probe=probe,
    )
    assert len(prompts) == 1
    assert "不要求普通室内或虚拟直播背景逐字演示标题" in prompts[0]
    assert "不能仅凭背景没有被标题提及就认定图文无关" in prompts[0]
    assert "具体图文矛盾、虚构人物或事件、抢占主体的无关元素仍须列出" in prompts[0]
    assert result["status"] == "PASS"


@pytest.mark.parametrize("changes", [{}, {"lidousha_primary": False}, {"title_cover_aligned": False}, {"single_clear_hook": False}])
def test_song_joint_question_uses_performance_and_preserves_negative_answer(tmp_path, changes):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"unit song cover pixels")
    verdict = _joint_verdict(**changes)

    def probe(path, question):
        assert path == cover
        assert "本条是歌切 song-clean" in question
        assert "不要求杂谈事件" in question
        assert "歌名大小写、标点须完整正确" in question
        assert "不能仅凭背景没有被标题提及" not in question
        assert "普通单人杂谈 AI 重绘" not in question
        return {"status": "OBSERVED", "answer": json.dumps(verdict)}

    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】豆沙歌，《world.execute(me);》", candidate_id="song",
        image_probe=probe, cover_generation={"art_direction": {"is_song": True}},
    )
    assert result["verdict"] == verdict
    assert result["status"] == ("FAIL" if changes else "PASS")


@pytest.mark.parametrize("changes", [
    {"unrelated_or_misleading_elements": ["The image invents an event absent from the story."]},
    {"unrelated_or_misleading_elements": ["Background unrelated to the livestream."]},
    {"title_cover_aligned": False},
    {"lidousha_primary": False},
    {"single_clear_hook": False},
    {"text_overcrowded": True},
    {"pass": False},
])
def test_joint_scene_clarification_does_not_override_any_failed_verdict(tmp_path, changes):
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"test pixels")
    verdict = _joint_verdict(**changes)
    result = build_joint_qc_receipt(
        cover_path=cover, title="【李豆沙】一次口头回应", candidate_id="test",
        image_probe=lambda *_: {"status": "OBSERVED", "answer": json.dumps(verdict)},
    )
    assert result["verdict"] == verdict
    assert result["status"] == "FAIL"
    assert result["pass"] is False


def _bound_identity_package(root: Path, *, identity_valid: bool = True, song: bool = False, legacy: bool = False):
    from src.autoslice.cover_host_identity_gate import HOST_ONLY_AUTHORITY, HOST_ONLY_SCHEMA_VERSION, LEGACY_HOST_ONLY_SCHEMA_VERSION, AUTHORITY, SCHEMA_VERSION
    from src.autoslice.host_only_v4_package_binding import build_binding

    title, cover = _c2_package(root)
    review = json.loads((root / 'review_manifest.json').read_text())
    item = review['items'][0]
    record_path = root / item['record']
    record = json.loads(record_path.read_text())
    record['story_contract'] = {'candidate_id': item['candidate_id']}
    _write_json(record_path, record)
    publish_path = root / item['publish_json']
    publish = json.loads(publish_path.read_text())
    generation = publish['cover_generation']
    generation['story_contract'] = {'cover_fallback_mode': 'HOST_ONLY_GENERIC'}
    evidence = root / 'evidence'
    evidence.mkdir()
    reference, comparison, receipt_path = [evidence / n for n in ('reference.png', 'comparison.png', 'identity.json')]
    reference.write_bytes(b'synthetic named source frame')
    comparison.write_bytes(b'synthetic source-final comparison')
    def sha(p):
        return 'sha256:' + hashlib.sha256(p.read_bytes()).hexdigest()
    verdict = {
        'source_lidousha_located': True, 'primary_subject_is_lidousha': identity_valid,
        'primary_subject_matches_other_source_participant': False,
        'primary_subject_is_visually_dominant': True, 'primary_subject_face_is_large_and_clear': True,
        'primary_subject_carries_story_reaction': True, 'excessive_dead_space': False,
        'meaningless_dominant_decoration': False, 'thumbnail_has_clear_click_hook': True,
        'identity_conflicts': [], 'composition_conflicts': [],
        'other_recognizable_people_or_avatars_visible': False, 'other_recognizable_people_or_avatars': [],
        'source_visible_feature_observations': {
            'clothing': ['synthetic visible clothing'],
            'head_appearance': ['synthetic visible head appearance'],
            'accessories': ['synthetic visible accessory'],
        },
        'final_feature_matches': {
            'clothing': True, 'head_appearance': True, 'accessories': True,
        },
        'final_feature_conflicts': [],
        'reason': 'Synthetic source nameplate identifies the same host.',
    }
    if not song and not legacy:
        verdict['final_feature_comparisons']={key:[{'source_feature':feature,'status':'MATCH',
            'final_observation':'Same synthetic visible feature on the final host.'} for feature in features]
            for key,features in verdict['source_visible_feature_observations'].items()}
    proof_schema=LEGACY_HOST_ONLY_SCHEMA_VERSION if legacy else HOST_ONLY_SCHEMA_VERSION
    proof = {
        'schema_version': proof_schema, 'authority': HOST_ONLY_AUTHORITY,
        'host_only_required': True, 'status': 'PASS', 'final_cover_sha256': sha(cover),
        'reference_sha256': sha(reference), 'comparison_sha256': sha(comparison), 'verdict': verdict,
        'witness': {'status': 'OBSERVED', 'provider': 'cpa', 'model': 'synthetic',
                    'image_sha256': sha(comparison).removeprefix('sha256:'), 'answer': json.dumps(verdict)},
    }
    item_key = 'host_only_v4_binding'
    binding_options = {'receipt_schema_version':proof_schema}
    if song:
        from src.autoslice.host_only_v4_package_binding import SONG_BINDING_ITEM_KEY, SONG_BINDING_SCHEMA_VERSION
        generation.pop('story_contract')
        generation['art_direction'] = {'is_song': True}
        verdict['primary_subject_visible_head_is_large_and_clear'] = verdict.pop('primary_subject_face_is_large_and_clear')
        verdict['primary_subject_carries_song_performance'] = verdict.pop('primary_subject_carries_story_reaction')
        verdict['primary_subject_identity'] = 'Synthetic same-scene monitor-head performer'
        proof.update(schema_version=SCHEMA_VERSION, authority=AUTHORITY, scene_kind='song')
        proof.pop('host_only_required')
        proof['witness']['answer'] = json.dumps(verdict)
        proof['reference_path'] = str(reference)
        proof['comparison_path'] = str(comparison)
        item_key = SONG_BINDING_ITEM_KEY
        binding_options = {'binding_schema_version': SONG_BINDING_SCHEMA_VERSION,
                           'receipt_schema_version': SCHEMA_VERSION, 'receipt_authority': AUTHORITY}
    generation['final_host_identity_verification'] = proof
    _write_json(receipt_path, proof)
    item[item_key] = build_binding(
        candidate_id=item['candidate_id'], receipt_path='evidence/identity.json', receipt_bytes=receipt_path.read_bytes(),
        reference_path='evidence/reference.png', reference_bytes=reference.read_bytes(),
        comparison_path='evidence/comparison.png', comparison_bytes=comparison.read_bytes(),
        final_cover_path=cover.name, final_cover_bytes=cover.read_bytes(),
        **binding_options,
    )
    _write_json(publish_path, publish)
    _write_json(root / 'review_manifest.json', review)
    out_parent = root / 'out'
    out_parent.mkdir(mode=0o700)
    return title, cover, out_parent / 'qc.json'


@pytest.mark.parametrize('bare_cover_digest', [False, True])
@pytest.mark.parametrize('legacy',[False,True])
def test_bound_source_identity_reaches_joint_probe_and_retains_current_verdict(tmp_path, bare_cover_digest,legacy):
    title, cover, out = _bound_identity_package(tmp_path,legacy=legacy)
    if bare_cover_digest:
        review_path = tmp_path / 'review_manifest.json'
        review = json.loads(review_path.read_text())
        binding = review['items'][0]['host_only_v4_binding']
        binding['final_cover_sha256'] = binding['final_cover_sha256'].removeprefix('sha256:')
        _write_json(review_path, review)
    verdict = _joint_verdict(lidousha_primary=False, **{'pass': False})
    prompts = []

    def probe(path, prompt):
        assert path == cover
        prompts.append(prompt)
        return {'status': 'OBSERVED', 'answer': json.dumps(verdict)}

    result = run_qc(tmp_path, title, out, image_probe=probe)
    assert len(prompts) == 1
    assert 'Synthetic source nameplate identifies the same host.' in prompts[0]
    assert '独立源图—成品身份对比证据' in prompts[0]
    assert '如当前可见内容与证据矛盾' in prompts[0]
    assert result['source_identity_context']['package_binding']['reference_sha256'] in prompts[0]
    assert result['source_identity_context']['package_binding']['final_cover_sha256'] == 'sha256:' + hashlib.sha256(cover.read_bytes()).hexdigest()
    assert result['verdict'] == verdict
    assert result['status'] == 'FAIL'
    assert result['pass'] is False


@pytest.mark.parametrize('drift', ['reference', 'comparison', 'receipt', 'cover', 'missing_binding', 'negative_identity'])
def test_invalid_bound_identity_stops_before_provider(tmp_path, drift):
    title, _cover, out = _bound_identity_package(tmp_path, identity_valid=drift != 'negative_identity')
    if drift in ('reference', 'comparison', 'receipt'):
        name = {'reference': 'reference.png', 'comparison': 'comparison.png', 'receipt': 'identity.json'}[drift]
        (tmp_path / 'evidence' / name).write_bytes(b'changed identity evidence')
    elif drift == 'cover':
        _cover.write_bytes(b'changed final pixels')
    elif drift == 'missing_binding':
        review = json.loads((tmp_path / 'review_manifest.json').read_text())
        del review['items'][0]['host_only_v4_binding']
        _write_json(tmp_path / 'review_manifest.json', review)

    def forbidden(*_):
        raise AssertionError('invalid identity must not reach provider')

    with pytest.raises(ValueError):
        run_qc(tmp_path, title, out, image_probe=forbidden)
    assert not out.exists()


def test_bound_identity_drift_during_probe_does_not_write_receipt(tmp_path):
    title, _cover, out = _bound_identity_package(tmp_path)

    def probe(*_):
        (tmp_path / 'evidence' / 'reference.png').write_bytes(b'changed during call')
        return {'status': 'OBSERVED', 'answer': json.dumps(_joint_verdict())}

    with pytest.raises(ValueError):
        run_qc(tmp_path, title, out, image_probe=probe)
    assert not out.exists()


@pytest.mark.parametrize('song', [False, True])
@pytest.mark.parametrize('drift', [None, 'reference', 'context'])
def test_upload_qc_replay_revalidates_source_identity_context(tmp_path, drift, song):
    from scripts.run_title_cover_joint_qc import reuse_valid_qc

    title, cover, out = _bound_identity_package(tmp_path, song=song)

    def probe(path, prompt):
        return {'schema_version': 'cpa-frame-witness.v1', 'status': 'OBSERVED', 'provider': 'cpa',
                'model': 'synthetic', 'image_path': str(path),
                'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'answer': json.dumps(_joint_verdict())}

    receipt = run_qc(tmp_path, title, out, image_probe=probe)
    assert receipt['status'] == 'PASS'
    if drift == 'reference':
        (tmp_path / 'evidence' / 'reference.png').write_bytes(b'changed source identity')
    elif drift == 'context':
        receipt['source_identity_context']['package_binding']['reference_sha256'] = 'sha256:' + '0' * 64
        _write_json(out, receipt)
    if drift is None:
        assert reuse_valid_qc(tmp_path, title, out) == receipt
    else:
        with pytest.raises(ValueError, match='source identity context'):
            reuse_valid_qc(tmp_path, title, out)


@pytest.mark.parametrize('accepted', [True, False])
def test_bound_song_source_identity_reaches_joint_cpa_without_overriding_verdict(tmp_path, accepted):
    title, cover, out = _bound_identity_package(tmp_path, song=True)
    verdict = _joint_verdict(lidousha_primary=accepted, **{'pass': accepted})
    prompts = []
    def probe(path, prompt):
        assert path == cover
        prompts.append(prompt)
        return {'status': 'OBSERVED', 'answer': json.dumps(verdict)}
    result = run_qc(tmp_path, title, out, image_probe=probe)
    assert len(prompts) == 1
    assert 'Synthetic same-scene monitor-head performer' in prompts[0]
    assert '本次lidousha_primary与pass仍由你裁决' in prompts[0]
    assert result['verdict'] == verdict
    assert result['pass'] == accepted


@pytest.mark.parametrize('drift', ['reference', 'comparison', 'receipt', 'missing_binding', 'negative_identity'])
def test_bound_song_identity_drift_stops_before_provider(tmp_path, drift):
    title, cover, out = _bound_identity_package(tmp_path, song=True, identity_valid=drift != 'negative_identity')
    if drift in ('reference', 'comparison', 'receipt'):
        filename = {'reference': 'reference.png', 'comparison': 'comparison.png', 'receipt': 'identity.json'}[drift]
        (tmp_path / 'evidence' / filename).write_bytes(b'changed source authority')
    elif drift == 'missing_binding':
        review = json.loads((tmp_path / 'review_manifest.json').read_text())
        del review['items'][0]['song_source_final_identity_binding']
        _write_json(tmp_path / 'review_manifest.json', review)
    def forbidden(*_):
        raise AssertionError('invalid evidence must stop before provider')
    with pytest.raises(ValueError):
        run_qc(tmp_path, title, out, image_probe=forbidden)
    assert not out.exists()


def test_song_identity_materializer_copies_actual_images_and_receipt(tmp_path):
    from src.autoslice.host_only_v4_package_binding import materialize_package_binding, validate_package_binding
    original = tmp_path / 'original'
    original.mkdir()
    title, cover, out = _bound_identity_package(original, song=True)
    review = json.loads((original / 'review_manifest.json').read_text())
    item = review['items'][0]
    generation = json.loads((original / item['publish_json']).read_text())['cover_generation']
    destination = tmp_path / 'portable'
    destination.mkdir()
    (destination / item['cover']).write_bytes(cover.read_bytes())
    binding = materialize_package_binding(root=destination, item=item, generation=generation, allow_song=True)
    assert binding['receipt_schema_version'].endswith('.v3')
    item['song_source_final_identity_binding'] = binding
    validate_package_binding(root=destination, item=item, generation=generation)
    assert (destination / binding['reference_path']).read_bytes() == (original / 'evidence/reference.png').read_bytes()
    assert (destination / binding['comparison_path']).read_bytes() == (original / 'evidence/comparison.png').read_bytes()


@pytest.mark.parametrize('damage', [None, 'bytes', 'missing', 'symlink', 'text', 'palette'])
def test_completed_qc_reuses_identical_style_bytes_after_runtime_relocation(tmp_path, monkeypatch, damage):
    import copy
    from scripts import run_title_cover_joint_qc as qc
    title,cover,record_path,publish_path,out = _event_package(tmp_path)
    publish=json.loads(publish_path.read_text());generation=publish['cover_generation']
    ordinary=_ordinary_talk_redraw_generation()
    ordinary['art_direction_style'].pop('reference_image')
    ordinary.pop('style_reference_images')
    generation.update(ordinary)
    binding=copy.deepcopy(generation['art_direction_style'])
    raw=Path(binding['path']).read_bytes()
    old=tmp_path/'old-style.md';new=tmp_path/'new-style.md'
    old.write_bytes(raw);new.write_bytes(raw)
    binding['path']=str(old);generation['art_direction_style']['path']=str(old)
    publish_path.write_text(json.dumps(publish))
    monkeypatch.setattr(qc,'ordinary_talk_redraw_style_binding',lambda **kw:copy.deepcopy(binding))
    calls=[]
    def probe(path,prompt):
        calls.append(prompt)
        from scripts.authorized_upload import TITLE_COVER_QC_WITNESS_SCHEMA_VERSION
        return {'schema_version':TITLE_COVER_QC_WITNESS_SCHEMA_VERSION,
                'provider':'cpa','model':'test-current','image_path':str(path.resolve()),
                'status':'OBSERVED','image_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'answer':json.dumps(_joint_verdict())}
    first=run_qc(tmp_path,title,out,image_probe=probe)
    assert first['status']=='PASS'
    before={path:path.read_bytes() for path in [out,record_path,publish_path,tmp_path/'review_manifest.json',cover]}
    binding['path']=str(new)
    if damage=='bytes':new.write_bytes(b'different style bytes')
    elif damage=='missing':old.unlink()
    elif damage=='symlink':old.unlink();old.symlink_to(new)
    elif damage=='text':binding['text']+=' different instruction'
    elif damage=='palette':binding['text_palette']={**binding['text_palette'],'primary':'#000000'}
    if damage is None:
        assert run_qc(tmp_path,title,out,image_probe=lambda *a,**kw:pytest.fail('no new vision call'),reuse_existing=True)==first
    else:
        with pytest.raises(ValueError,match='not reusable'):
            run_qc(tmp_path,title,out,image_probe=lambda *a,**kw:pytest.fail('no new vision call'),reuse_existing=True)
    assert len(calls)==1 and {p:p.read_bytes() for p in before}==before
