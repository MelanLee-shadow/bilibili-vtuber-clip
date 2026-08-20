import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.apply_subtitle_correction as correction

from scripts.apply_subtitle_correction import (
    DeliveryCopyError,
    _copy_delivery_file,
    _preflight_existing_delivery_copies,
    _preflight_delivery_targets,
    _project_existing_text_onto_timing,
    _project_reviewed_text_onto_timing,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _branding_context(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    z1 = _write(tmp_path / "z1.mp4", "z1 intro")
    z2 = _write(tmp_path / "z2.mp4", "z2 intro")
    context = {
        "intro_id": "z1",
        "media_path": z1,
        "media_sha256": _sha256(z1),
        "candidates": [
            {"intro_id": "z1", "media_path": z1, "media_sha256": _sha256(z1)},
            {"intro_id": "z2", "media_path": z2, "media_sha256": _sha256(z2)},
        ],
        "manifest_path": tmp_path / "branding.json",
        "manifest_sha256": "f" * 64,
    }
    binding = {
        "status": "PREPENDED",
        "intro_id": "z2",
        "intro_media_sha256": _sha256(z2),
        "intro_offset_ms": 6183,
    }
    return context, binding


def _write_normal_delivery_authorities(
    tmp_path: Path,
    *,
    candidate_id: str,
    binding: dict[str, object],
) -> tuple[Path, Path]:
    publish = tmp_path / "prior.publish.json"
    burned_sha = "sha256:" + "a" * 64
    publish.write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "artifact_hashes": {"burned_video_sha256": burned_sha},
            }
        ),
        encoding="utf-8",
    )
    record = tmp_path / "prior.record.json"
    record.write_text(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "artifact_hashes": {
                    "publish_draft_sha256": "sha256:" + _sha256(publish),
                    "burned_video_sha256": burned_sha,
                },
                "burned_preview": {
                    "burned_sha256": burned_sha,
                    "branding_intro": binding,
                },
            }
        ),
        encoding="utf-8",
    )
    return record, publish


def test_reviewed_text_is_projected_onto_authoritative_timing_with_drop(
    tmp_path: Path,
) -> None:
    text_source = _write(
        tmp_path / "automatic.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:02,000 --> 00:00:02,500
背景日语

3
00:00:03,000 --> 00:00:03,400
旧专名
""",
    )
    decision_output = _write(
        tmp_path / "decision.srt",
        """1
00:00:01,000 --> 00:00:01,500
原文一

2
00:00:03,000 --> 00:00:03,400
正确专名
""",
    )
    timing_source = _write(
        tmp_path / "timing.srt",
        """1
00:00:00,800 --> 00:00:01,900
旧文本不构成文字 authority

2
00:00:02,000 --> 00:00:02,900
旧文本不构成文字 authority

3
00:00:03,000 --> 00:00:04,800
旧文本不构成文字 authority
""",
    )
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps(
            {
                "overrides": [
                    {"source_cue": 2, "action": "drop"},
                    {"source_cue": 3, "action": "replace"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    output = _project_reviewed_text_onto_timing(
        text_source=text_source,
        decision_output=decision_output,
        timing_source=timing_source,
        text_override=override,
    )

    assert "00:00:00,800 --> 00:00:01,900\n原文一" in output
    assert "00:00:03,000 --> 00:00:04,800\n正确专名" in output
    assert "背景日语" not in output
    assert "00:00:02,000 --> 00:00:02,900" not in output


def test_refresh_only_keeps_text_but_replaces_every_timing_boundary() -> None:
    text_srt = """1
00:00:01,000 --> 00:00:01,200
完整语音

2
00:00:02,000 --> 00:00:02,300
第二句
"""
    timing_srt = """1
00:00:00,800 --> 00:00:01,900
旧文本

2
00:00:02,000 --> 00:00:03,800
旧文本
"""

    output = _project_existing_text_onto_timing(
        text_srt=text_srt,
        timing_srt=timing_srt,
    )

    assert "00:00:00,800 --> 00:00:01,900\n完整语音" in output
    assert "00:00:02,000 --> 00:00:03,800\n第二句" in output


def test_delivery_copy_skips_same_regular_video_and_sidecar(tmp_path: Path) -> None:
    video = _write(tmp_path / "recut.burned.mp4", "video")
    sidecar = _write(tmp_path / "recut.srt", "subtitle")

    assert _copy_delivery_file(video, video) == "ALREADY_DELIVERED"
    assert _copy_delivery_file(sidecar, sidecar) == "ALREADY_DELIVERED"
    hardlinked_video = tmp_path / "delivery-hardlink.mp4"
    os.link(video, hardlinked_video)
    assert _copy_delivery_file(video, hardlinked_video) == "ALREADY_DELIVERED"
    assert hardlinked_video.read_text(encoding="utf-8") == "video"
    _preflight_existing_delivery_copies([(video, hardlinked_video)])


def test_delivery_copy_copies_distinct_regular_target(tmp_path: Path) -> None:
    source = _write(tmp_path / "source.mp4", "fresh video")
    destination = _write(tmp_path / "delivery.mp4", "old video")

    assert _copy_delivery_file(source, destination) == "COPIED"
    assert destination.read_text(encoding="utf-8") == "fresh video"


def test_delivery_preflight_and_copy_reject_symlink_and_identity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "source.srt", "text")
    destination = tmp_path / "delivery.srt"
    destination.symlink_to(source)
    with pytest.raises(DeliveryCopyError, match="symlink"):
        _preflight_delivery_targets([destination])
    with pytest.raises(DeliveryCopyError, match="symlink"):
        _copy_delivery_file(source, destination)

    distinct = _write(tmp_path / "distinct.srt", "different")
    monkeypatch.setattr(os.path, "samefile", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(DeliveryCopyError, match="cannot determine"):
        _copy_delivery_file(source, distinct)
    with pytest.raises(DeliveryCopyError, match="cannot determine"):
        _preflight_existing_delivery_copies([(source, distinct)])


def test_main_skips_same_burned_video_and_same_srt_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_same_delivery"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = recut_dir / "recut.burned-final-sapphire72.mp4"
    subtitle = delivery.with_suffix(".srt")
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n",
        encoding="utf-8",
    )
    source = _write(recut_dir / "source.mp4", "source")
    (recut_dir / f"{cid}.record.json").write_text(
        json.dumps(
            {
                "subtitle_path": str(subtitle),
                "media_path": str(source),
            }
        ),
        encoding="utf-8",
    )

    def fake_burn(materialized: dict[str, object], **_kwargs: object) -> dict[str, object]:
        staged_media = Path(str(materialized["media_path"]))
        staged_burned = staged_media.with_suffix(".burned-final-sapphire72.mp4")
        staged_ass = staged_media.with_suffix(".final-sapphire72.ass")
        staged_burned.write_text("burned", encoding="utf-8")
        staged_ass.write_text("ass", encoding="utf-8")
        return {"burned_preview": {"path": str(staged_burned), "ass_path": str(staged_ass), "status": "BURNED"}}

    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: object())
    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")

    assert correction.main(
        [
            "--cid",
            cid,
            "--date",
            date,
            "--delivery",
            str(delivery),
            "--replace",
            "旧字幕=新字幕",
            "--out-base",
            str(tmp_path),
        ]
    ) == 0
    assert delivery.read_text(encoding="utf-8") == "burned"
    assert "新字幕" in subtitle.read_text(encoding="utf-8")


def test_existing_delivery_pins_prior_z2_instead_of_new_main_hash_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_existing_delivery"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = recut_dir / "recut.burned-final-sapphire72.mp4"
    delivery.write_text("old burned delivery", encoding="utf-8")
    subtitle = delivery.with_suffix(".srt")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n", encoding="utf-8")
    record_path = recut_dir / f"{cid}.record.json"
    source = _write(recut_dir / "source.mp4", "source")
    record_path.write_text(
        json.dumps({"subtitle_path": str(subtitle), "media_path": str(source)}),
        encoding="utf-8",
    )
    context, z2_binding = _branding_context(tmp_path)
    authority_record, authority_publish = _write_normal_delivery_authorities(
        tmp_path, candidate_id=cid, binding=z2_binding
    )
    observed_contexts: list[object] = []

    def fake_burn(materialized: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed_contexts.append(kwargs["branding_intro"])
        staged_media = Path(str(materialized["media_path"]))
        staged_burned = staged_media.with_suffix(".burned-final-sapphire72.mp4")
        staged_ass = staged_media.with_suffix(".final-sapphire72.ass")
        staged_burned.write_text("new burned delivery", encoding="utf-8")
        staged_ass.write_text("ass", encoding="utf-8")
        return {
            "burned_preview": {
                "path": str(staged_burned), "ass_path": str(staged_ass),
                "status": "BURNED",
                "branding_intro": z2_binding,
            }
        }

    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: context)
    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")

    assert correction.main(
        [
            "--cid", cid, "--date", date, "--delivery", str(delivery),
            "--delivery-authority-record", str(authority_record),
            "--delivery-authority-publish", str(authority_publish),
            "--replace", "旧字幕=新字幕", "--out-base", str(tmp_path),
        ]
    ) == 0
    assert len(observed_contexts) == 1
    pinned = observed_contexts[0]
    assert isinstance(pinned, dict)
    assert [row["intro_id"] for row in pinned["candidates"]] == ["z2"]
    assert pinned["recorded_delivery_binding"]["intro_offset_ms"] == 6183
    updated = json.loads(record_path.read_text(encoding="utf-8"))
    assert updated["burned_preview"]["branding_intro"]["intro_id"] == "z2"
    receipt = json.loads(
        delivery.with_suffix(".human-text-correction.json").read_text(encoding="utf-8")
    )
    assert receipt["delivery_branding_authority"]["branding_intro"]["intro_id"] == "z2"


@pytest.mark.parametrize("bad_binding", ["missing", "unknown_intro", "hash_drift"])
def test_existing_delivery_rejects_unprovable_intro_before_text_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_binding: str,
) -> None:
    cid = "auto_reject_intro"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = recut_dir / "recut.burned-final-sapphire72.mp4"
    delivery.write_text("old burned delivery", encoding="utf-8")
    subtitle = delivery.with_suffix(".srt")
    original = "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n"
    subtitle.write_text(original, encoding="utf-8")
    record_path = recut_dir / f"{cid}.record.json"
    source = _write(recut_dir / "source.mp4", "source")
    record_path.write_text(
        json.dumps({"subtitle_path": str(subtitle), "media_path": str(source)}),
        encoding="utf-8",
    )
    context, binding = _branding_context(tmp_path)
    if bad_binding == "unknown_intro":
        binding["intro_id"] = "z9"
    elif bad_binding == "hash_drift":
        binding["intro_media_sha256"] = "0" * 64
    authority_record, authority_publish = _write_normal_delivery_authorities(
        tmp_path, candidate_id=cid, binding=binding
    )
    if bad_binding == "missing":
        document = json.loads(authority_record.read_text(encoding="utf-8"))
        del document["burned_preview"]["branding_intro"]
        authority_record.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: context)
    monkeypatch.setattr(
        correction,
        "_burn_preview_subtitles",
        lambda *_args, **_kwargs: pytest.fail("burn must not run after authority preflight failure"),
    )

    assert correction.main(
        [
            "--cid", cid, "--date", date, "--delivery", str(delivery),
            "--delivery-authority-record", str(authority_record),
            "--delivery-authority-publish", str(authority_publish),
            "--replace", "旧字幕=新字幕", "--out-base", str(tmp_path),
        ]
    ) == 1
    assert subtitle.read_text(encoding="utf-8") == original
    assert json.loads(record_path.read_text(encoding="utf-8")) == {
        "subtitle_path": str(subtitle),
        "media_path": str(recut_dir / "source.mp4"),
    }


def test_recovery_authority_can_restore_z2_from_explicit_incident_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_qixi_like"
    date = "2026-08-20"
    recut_dir = tmp_path / "out" / date / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = recut_dir / "recut.burned-final-sapphire72.mp4"
    delivery.write_text("polluted z1 delivery", encoding="utf-8")
    subtitle = delivery.with_suffix(".srt")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n污染字幕\n", encoding="utf-8")
    before_srt = "sha256:" + "6" * 64
    after_srt = "sha256:" + _sha256(subtitle)
    new_burned = "sha256:" + "7" * 64
    incident = tmp_path / "incident.human-text-correction.json"
    incident.write_text(
        json.dumps(
            {
                "candidate_id": cid,
                "before_srt_sha256": before_srt.removeprefix("sha256:"),
                "after_srt_sha256": after_srt.removeprefix("sha256:"),
                "burned_media_sha256": new_burned.removeprefix("sha256:"),
            }
        ),
        encoding="utf-8",
    )
    record_path = recut_dir / f"{cid}.record.json"
    source = _write(recut_dir / "source.mp4", "source")
    record_path.write_text(
        json.dumps(
            {
                "subtitle_path": str(subtitle),
                "media_path": str(source),
                "artifact_hashes": {
                    "subtitle_sha256": after_srt,
                    "burned_video_sha256": new_burned,
                },
                "human_text_correction_manifest_path": str(incident),
                "human_text_correction_manifest_sha256": "sha256:" + _sha256(incident),
            }
        ),
        encoding="utf-8",
    )
    prior_burned = "sha256:" + "e" * 64
    prior_record = "sha256:" + "5" * 64
    prior_package = tmp_path / "stale.review-manifest.json"
    prior_package.write_text(json.dumps({
        "schema_version": "lidousha-daily-review-manifest.v1",
        "items": [{
            "candidate_id": cid, "video": "old.mp4", "subtitle_srt": "old.srt", "record": "old.record.json",
            "sha256": {"video": prior_burned.removeprefix("sha256:"), "subtitle_srt": before_srt.removeprefix("sha256:"), "evidence_json": prior_record.removeprefix("sha256:")},
        }],
    }), encoding="utf-8")
    freeze = tmp_path / "final_media_review_contracts.v1.json"
    freeze.write_text(json.dumps({
        "schema_version": "lidousha-final-media-review-contracts.v1",
        "contracts": [{"candidate_id": cid, "subtitle_review_points": [
            {"point_id": "qixi-tomorrow-night-lara-title"},
            {"point_id": "qixi-balance-iiya"},
            {"point_id": "qixi-sweet-or-bitter-ending"},
            {"point_id": "qixi-full-release-text-stability"},
        ]}],
    }), encoding="utf-8")
    context, z2_binding = _branding_context(tmp_path)
    recovery = tmp_path / "recovery.json"
    recovery.write_text(
        json.dumps(
            {
                "schema_version": "delivery-branding-recovery-authority.v1",
                "candidate_id": cid,
                "authority": "explicit operator recovery decision",
                "prior_burned_video_sha256": prior_burned,
                "prior_subtitle_sha256": before_srt,
                "prior_record_sha256": prior_record,
                "intro_id": z2_binding["intro_id"],
                "intro_media_sha256": "sha256:" + str(z2_binding["intro_media_sha256"]),
                "intro_offset_ms": z2_binding["intro_offset_ms"],
                "prior_package_authority_path": str(prior_package),
                "prior_package_authority_sha256": "sha256:" + _sha256(prior_package),
                "operator_freeze_authority_path": str(freeze),
                "operator_freeze_authority_sha256": "sha256:" + _sha256(freeze),
                "incident_correction_manifest_path": str(incident),
                "incident_correction_manifest_sha256": "sha256:" + _sha256(incident),
                "incident_before_srt_sha256": before_srt,
                "incident_after_srt_sha256": after_srt,
                "incident_new_burned_video_sha256": new_burned,
            }
        ),
        encoding="utf-8",
    )
    observed_contexts: list[object] = []

    def fake_burn(materialized: dict[str, object], **kwargs: object) -> dict[str, object]:
        observed_contexts.append(kwargs["branding_intro"])
        staged_media = Path(str(materialized["media_path"]))
        staged_burned = staged_media.with_suffix(".burned-final-sapphire72.mp4")
        staged_ass = staged_media.with_suffix(".final-sapphire72.ass")
        staged_burned.write_text("recovered z2 delivery", encoding="utf-8")
        staged_ass.write_text("ass", encoding="utf-8")
        return {
            "burned_preview": {
                "path": str(staged_burned), "ass_path": str(staged_ass),
                "status": "BURNED",
                "branding_intro": z2_binding,
            }
        }

    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: context)
    monkeypatch.setattr(correction, "ROOT", tmp_path)
    monkeypatch.setattr(correction, "_expected_recovery_authority_path", lambda _cid: recovery)
    monkeypatch.setattr(
        correction, "require_repository_asset_authority",
        lambda **_kwargs: SimpleNamespace(mode="GIT_HEAD", commit="a" * 40, relative_path="assets/lidousha/test.json", file_sha256="sha256:" + _sha256(recovery)),
    )
    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    assert correction.main(
        [
            "--cid", cid, "--date", date, "--delivery", str(delivery),
            "--recovery-branding-authority", str(recovery),
            "--replace", "污染字幕=恢复字幕", "--out-base", str(tmp_path),
        ]
    ) == 0
    assert isinstance(observed_contexts[0], dict)
    assert [row["intro_id"] for row in observed_contexts[0]["candidates"]] == ["z2"]
    receipt = json.loads(
        delivery.with_suffix(".human-text-correction.json").read_text(encoding="utf-8")
    )
    assert receipt["delivery_branding_authority"]["schema_version"] == (
        "delivery-branding-recovery-authority.v1"
    )


def test_canonical_qixi_recovery_asset_has_strict_shape_and_sealed_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/delivery_branding_recovery/auto_113022_354_496.v1.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    correction._validate_recovery_authority_shape(
        document, candidate_id="auto_113022_354_496"
    )
    observed: dict[str, object] = {}

    def fake_seal(**kwargs: object) -> SimpleNamespace:
        observed.update(kwargs)
        return SimpleNamespace(
            mode="DEPLOYED_MANIFEST", commit="a" * 40,
            relative_path="assets/lidousha/delivery_branding_recovery/auto_113022_354_496.v1.json",
            file_sha256="sha256:" + _sha256(path),
        )

    monkeypatch.setattr(correction, "require_repository_asset_authority", fake_seal)
    _document, seal = correction._require_canonical_recovery_authority(
        path, candidate_id="auto_113022_354_496"
    )
    assert observed["relative_path"] == Path(
        "assets/lidousha/delivery_branding_recovery/auto_113022_354_496.v1.json"
    )
    assert seal["mode"] == "DEPLOYED_MANIFEST"


def _qixi_v2_successor_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, object], Path, Path, dict[str, object]]:
    """Build the exact predecessor-receipt/current-record relation used by v2."""

    cid = "auto_113022_354_496"
    monkeypatch.setattr(correction, "ROOT", tmp_path)
    predecessor = tmp_path / "assets/lidousha/delivery_branding_recovery/auto_113022_354_496.v1.json"
    predecessor.parent.mkdir(parents=True)
    predecessor.write_text("{}", encoding="utf-8")
    old_incident = tmp_path / "old-correction.json"
    old_incident.write_text("{}", encoding="utf-8")
    successor = tmp_path / "current-correction.json"
    record = tmp_path / "current.record.json"
    binding = {
        "status": "PREPENDED", "intro_id": "z2", "intro_media_sha256": "sha256:" + "d" * 64,
        "intro_offset_ms": 6183,
    }
    authority: dict[str, object] = {
        "schema_version": "delivery-branding-recovery-authority.v2", "candidate_id": cid,
        "authority": "fixture", "prior_burned_video_sha256": "sha256:" + "e" * 64,
        "prior_subtitle_sha256": "sha256:" + "6" * 64, "prior_record_sha256": "sha256:" + "5" * 64,
        "intro_id": "z2", "intro_media_sha256": binding["intro_media_sha256"], "intro_offset_ms": 6183,
        "prior_package_authority_path": str(tmp_path / "prior.json"), "prior_package_authority_sha256": "sha256:" + "4" * 64,
        "operator_freeze_authority_path": str(tmp_path / "freeze.json"), "operator_freeze_authority_sha256": "sha256:" + "3" * 64,
        "incident_correction_manifest_path": str(old_incident), "incident_correction_manifest_sha256": "sha256:" + _sha256(old_incident),
        "incident_before_srt_sha256": "sha256:" + "6" * 64, "incident_after_srt_sha256": "sha256:" + "2" * 64,
        "incident_new_burned_video_sha256": "sha256:" + "7" * 64,
        "successor_correction_manifest_path": str(successor), "successor_before_srt_sha256": "sha256:" + "2" * 64,
        "successor_after_srt_sha256": "sha256:" + "2" * 64, "successor_burned_video_sha256": "sha256:" + "0" * 64,
        "successor_record_path": str(record), "predecessor_recovery_authority_path": str(predecessor),
        "predecessor_recovery_authority_sha256": "sha256:" + "b" * 64,
        "predecessor_recovery_authority_commit": "a" * 40,
        "predecessor_operator_freeze_authority_path": str(tmp_path / "old-freeze.json"),
        "predecessor_operator_freeze_authority_sha256": "sha256:" + "1" * 64,
    }
    recovery = {
        "schema_version": "delivery-branding-recovery-authority.v1", "authority_path": str(predecessor),
        "authority_sha256": authority["predecessor_recovery_authority_sha256"],
        "authority_repository_seal": {"mode": "DEPLOYED_MANIFEST", "deployed_commit": "a" * 40,
                                      "relative_path": "assets/lidousha/delivery_branding_recovery/auto_113022_354_496.v1.json",
                                      "sha256": authority["predecessor_recovery_authority_sha256"]},
        "prior_burned_video_sha256": authority["prior_burned_video_sha256"],
        "prior_subtitle_sha256": authority["prior_subtitle_sha256"], "prior_record_sha256": authority["prior_record_sha256"],
        "prior_package_authority_path": authority["prior_package_authority_path"],
        "prior_package_authority_sha256": authority["prior_package_authority_sha256"],
        "operator_freeze_authority_path": authority["predecessor_operator_freeze_authority_path"],
        "operator_freeze_authority_sha256": authority["predecessor_operator_freeze_authority_sha256"],
        "incident_correction_manifest_path": str(old_incident), "incident_correction_manifest_sha256": authority["incident_correction_manifest_sha256"],
        "branding_intro": binding,
    }
    successor.write_text(json.dumps({
        "schema_version": "human-subtitle-correction.v2", "candidate_id": cid,
        "before_srt_sha256": "2" * 64, "after_srt_sha256": "2" * 64,
        "burned_media_sha256": "0" * 64, "delivery_branding_authority": recovery,
    }), encoding="utf-8")
    authority["successor_correction_manifest_sha256"] = "sha256:" + _sha256(successor)
    authority["successor_correction_manifest_bytes"] = successor.stat().st_size
    record.write_text(json.dumps({
        "artifact_hashes": {"subtitle_sha256": "sha256:" + "2" * 64, "burned_video_sha256": "sha256:" + "0" * 64},
        "human_text_correction_manifest_path": str(successor),
        "human_text_correction_manifest_sha256": authority["successor_correction_manifest_sha256"],
    }), encoding="utf-8")
    authority["successor_record_sha256"] = "sha256:" + _sha256(record)
    authority["successor_record_bytes"] = record.stat().st_size
    correction._validate_recovery_authority_shape(authority, candidate_id=cid)
    return authority, old_incident, record, binding


def test_v2_recovery_replays_the_current_z2_successor_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, old_incident, record, binding = _qixi_v2_successor_fixture(tmp_path, monkeypatch)
    path, sha = correction._validate_v2_successor_chain(
        authority=authority, working_record_path=record, candidate_id="auto_113022_354_496",
        incident_path=old_incident, incident_sha=str(authority["incident_correction_manifest_sha256"]),
        after_srt=str(authority["incident_after_srt_sha256"]), prior_binding=binding,
    )
    assert path == Path(str(authority["successor_correction_manifest_path"]))
    assert sha == authority["successor_correction_manifest_sha256"]


@pytest.mark.parametrize(
    "tamper", ["missing", "receipt", "record", "embedded_freeze", "current_freeze"]
)
def test_v2_recovery_requires_the_current_z2_successor_before_reburn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    authority, old_incident, record, binding = _qixi_v2_successor_fixture(tmp_path, monkeypatch)
    if tamper == "missing":
        authority.pop("successor_record_sha256")
        with pytest.raises(correction.DeliveryBrandingAuthorityError):
            correction._validate_recovery_authority_shape(authority, candidate_id="auto_113022_354_496")
        return
    if tamper == "current_freeze":
        authority["operator_freeze_authority_sha256"] = authority[
            "predecessor_operator_freeze_authority_sha256"
        ]
        with pytest.raises(correction.DeliveryBrandingAuthorityError):
            correction._validate_recovery_authority_shape(authority, candidate_id="auto_113022_354_496")
        return
    if tamper == "receipt":
        successor = Path(str(authority["successor_correction_manifest_path"]))
        payload = json.loads(successor.read_text(encoding="utf-8"))
        payload["delivery_branding_authority"]["branding_intro"]["intro_offset_ms"] = 1
        successor.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "embedded_freeze":
        successor = Path(str(authority["successor_correction_manifest_path"]))
        payload = json.loads(successor.read_text(encoding="utf-8"))
        payload["delivery_branding_authority"]["operator_freeze_authority_sha256"] = authority[
            "operator_freeze_authority_sha256"
        ]
        successor.write_text(json.dumps(payload), encoding="utf-8")
    else:
        record.write_text("{}", encoding="utf-8")
    with pytest.raises(correction.DeliveryBrandingAuthorityError):
        correction._validate_v2_successor_chain(
            authority=authority, working_record_path=record, candidate_id="auto_113022_354_496",
            incident_path=old_incident, incident_sha=str(authority["incident_correction_manifest_sha256"]),
            after_srt=str(authority["incident_after_srt_sha256"]), prior_binding=binding,
        )


def test_post_render_branding_mismatch_leaves_package_bytes_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_staged_rollback"
    recut_dir = tmp_path / "out" / "2026-08-20" / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = _write(recut_dir / "recut.burned-final-sapphire72.mp4", "old delivery")
    subtitle = _write(delivery.with_suffix(".srt"), "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n")
    source = _write(recut_dir / "source.mp4", "source")
    record = recut_dir / f"{cid}.record.json"
    record.write_text(json.dumps({"subtitle_path": str(subtitle), "media_path": str(source)}), encoding="utf-8")
    context, z2 = _branding_context(tmp_path)
    authority_record, authority_publish = _write_normal_delivery_authorities(
        tmp_path, candidate_id=cid, binding=z2
    )
    before = {path: path.read_bytes() for path in (delivery, subtitle, record)}

    def fake_burn(materialized: dict[str, object], **_kwargs: object) -> dict[str, object]:
        staged_media = Path(str(materialized["media_path"]))
        staged_burned = _write(staged_media.with_suffix(".burned-final-sapphire72.mp4"), "bad staged")
        staged_ass = _write(staged_media.with_suffix(".final-sapphire72.ass"), "ass")
        wrong = dict(z2)
        wrong["intro_offset_ms"] = 1
        return {"burned_preview": {"path": str(staged_burned), "ass_path": str(staged_ass), "status": "BURNED", "branding_intro": wrong}}

    monkeypatch.setattr(correction, "require_branding_intro", lambda _root: context)
    monkeypatch.setattr(correction, "_burn_preview_subtitles", fake_burn)
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    assert correction.main([
        "--cid", cid, "--date", "2026-08-20", "--delivery", str(delivery),
        "--delivery-authority-record", str(authority_record),
        "--delivery-authority-publish", str(authority_publish),
        "--replace", "旧字幕=新字幕", "--out-base", str(tmp_path),
    ]) == 1
    assert {path: path.read_bytes() for path in before} == before
    assert not (recut_dir / f"{cid}.human-text-correction.json").exists()


def test_staged_commit_restores_target_when_install_fails_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _write(tmp_path / "staged", "new")
    target = _write(tmp_path / "target", "old")
    real_replace = os.replace

    def fail_install(source_path: str | Path, target_path: str | Path) -> None:
        if (
            Path(target_path) == target
            and ".subtitle-correction-" in Path(source_path).name
            and "backup" not in Path(source_path).name
        ):
            raise OSError("injected install failure")
        real_replace(source_path, target_path)

    monkeypatch.setattr(os, "replace", fail_install)
    with pytest.raises(DeliveryCopyError, match="package restored"):
        correction._commit_staged_files([(source, target)], [])
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".target.subtitle-correction-backup-*"))


def test_existing_delivery_rejects_text_override_before_any_sidecar_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = "auto_existing_override"
    recut_dir = tmp_path / "out" / "2026-08-20" / cid / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    delivery = _write(recut_dir / "delivery.mp4", "old delivery")
    subtitle = _write(delivery.with_suffix(".srt"), "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n")
    source = _write(recut_dir / "source.mp4", "source")
    record = recut_dir / f"{cid}.record.json"
    record.write_text(json.dumps({"subtitle_path": str(subtitle), "media_path": str(source)}), encoding="utf-8")
    text_source = _write(tmp_path / "automatic.srt", subtitle.read_text(encoding="utf-8"))
    override = _write(tmp_path / "override.json", json.dumps({"schema_version": "operator-reviewed-subtitle-override.v3"}))
    monkeypatch.setattr(correction, "_prepare_correction_branding", lambda **_kwargs: (None, None))
    assert correction.main([
        "--cid", cid, "--date", "2026-08-20", "--delivery", str(delivery),
        "--text-source", str(text_source), "--text-override", str(override),
        "--timing-source", str(text_source), "--out-base", str(tmp_path),
    ]) == 2
    assert not list(recut_dir.glob(f"{cid}.human-reviewed-*"))
    assert subtitle.read_text(encoding="utf-8") == "1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n"
