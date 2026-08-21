"""Regression coverage for the candidate-sealed 女友感 correction lane."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

import scripts.apply_auto_123655_771_844_sealed_correction as runner
import scripts.apply_subtitle_correction as generic_correction
import src.autoslice.sealed_subtitle_correction as sealed_correction
from src.autoslice.sealed_subtitle_correction import (
    CANDIDATE_ID,
    RELATIVE_PATH,
    SealedSubtitleCorrectionError,
    SealedSubtitleCorrectionTransactionContext,
    _validate_correction_receipt,
    _validate_diagnostic_decision_receipt,
    _canonical_sha256,
    _validate_exact_cue_transform,
    expected_output_srt,
    validate_authority,
    validate_diagnostic_assets,
    validate_post_transaction,
    validate_runtime,
)
from src.autoslice.title_policy import manual_title_override


ROOT = Path(__file__).resolve().parents[1]
ASSET = (
    ROOT
    / "assets/lidousha/sealed_subtitle_corrections/auto_123655_771_844.v1.json"
)


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _resign(authority: dict[str, object]) -> None:
    unsigned = dict(authority)
    unsigned.pop("authority_sha256", None)
    authority["authority_sha256"] = _canonical_sha256(unsigned)


def _cue_source(authority: dict[str, object]) -> str:
    correction = authority["correction"]
    assert isinstance(correction, dict)
    targets = {
        row["cue_index"]: row
        for row in correction["replacements"]
        if isinstance(row, dict)
    }
    assertions = {
        row["cue_index"]: row
        for row in correction["assertions"]
        if isinstance(row, dict)
    }
    blocks = []
    for index in range(1, 34):
        if index in targets:
            row = targets[index]
            start, end, text = row["start"], row["end"], row["before"]
        elif index in assertions:
            row = assertions[index]
            start, end, text = row["start"], row["end"], row["text"]
        else:
            start = f"00:00:{index:02d},000"
            end = f"00:00:{index:02d},500"
            text = f"unchanged cue {index}"
        blocks.append(f"{index}\n{start} --> {end}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _write_descriptor(path: Path, payload: bytes, *, mode: int) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    info = path.stat()
    return {
        "path": str(path),
        "sha256": _sha(path),
        "bytes": info.st_size,
        "mode": f"{mode:04o}",
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _runtime_authority(tmp_path: Path) -> tuple[dict[str, object], dict[str, Path]]:
    authority = deepcopy(json.loads(ASSET.read_text(encoding="utf-8")))
    artifacts = authority["artifacts"]
    delivery = authority["delivery"]
    assert isinstance(artifacts, dict) and isinstance(delivery, dict)
    paths: dict[str, Path] = {}
    for role, descriptor in artifacts.items():
        assert isinstance(descriptor, dict)
        path = tmp_path / "package" / f"{role}.bin"
        mode = 0o600 if role in {"srt", "chat"} else 0o644
        if role == "srt":
            path = tmp_path / "package" / "candidate.srt"
            payload = _cue_source(authority).encode()
        elif role == "record":
            path = tmp_path / "package" / "candidate.record.json"
            payload = b"{}\n"
        else:
            payload = f"original {role}\n".encode()
        artifacts[role] = _write_descriptor(path, payload, mode=mode)
        paths[role] = path
    delivery_video = tmp_path / "delivery" / "candidate.mp4"
    delivery_srt = tmp_path / "delivery" / "candidate.srt"
    delivery["video"] = _write_descriptor(
        delivery_video, paths["burn"].read_bytes(), mode=0o644
    )
    delivery["subtitle"] = _write_descriptor(
        delivery_srt, paths["srt"].read_bytes(), mode=0o600
    )
    delivery_record = tmp_path / "delivery" / "candidate.record.json"
    delivery["record"] = _write_descriptor(
        delivery_record, paths["record"].read_bytes(), mode=0o644
    )
    sidecars: dict[str, object] = {}
    for role in ("srt", "ass", "manifest"):
        sidecars[role] = _write_descriptor(
            tmp_path / "delivery" / f"candidate.speaker.{role}",
            f"old speaker {role}\n".encode(),
            mode=0o600,
        )
    delivery["speaker_sidecars"] = sidecars
    delivery["correction_receipts"] = {
        "primary_path": str(tmp_path / "package" / "candidate.human-text-correction.json"),
        "delivery_path": str(tmp_path / "delivery" / "candidate.human-text-correction.json"),
    }
    paths["delivery_video"] = delivery_video
    paths["delivery_srt"] = delivery_srt
    paths["delivery_record"] = delivery_record
    correction = authority["correction"]
    assert isinstance(correction, dict)
    correction["source_srt_sha256"] = artifacts["srt"]["sha256"]
    output = expected_output_srt(authority, paths["srt"].read_text(encoding="utf-8"))
    correction["output_srt_sha256"] = "sha256:" + hashlib.sha256(output.encode()).hexdigest()
    _resign(authority)
    validate_authority(authority)
    return authority, paths


def _materialize_post_transaction(
    authority: dict[str, object], paths: dict[str, Path]
) -> str:
    expected = expected_output_srt(authority, paths["srt"].read_text(encoding="utf-8"))
    paths["srt"].write_text(expected, encoding="utf-8")
    paths["srt"].chmod(0o600)
    paths["ass"].write_text("new final ASS\n", encoding="utf-8")
    paths["burn"].write_bytes(b"new burned media\n")
    receipt = Path(str(authority["delivery"]["correction_receipts"]["primary_path"]))
    receipt.write_text(json.dumps({"candidate_id": CANDIDATE_ID}), encoding="utf-8")
    record = {
        "subtitle_path": str(paths["srt"]),
        "subtitle_ass_path": str(paths["ass"]),
        "artifact_hashes": {
            "subtitle_sha256": _sha(paths["srt"]),
            "ass_sha256": _sha(paths["ass"]),
            "burned_video_sha256": _sha(paths["burn"]),
        },
        "burned_preview": {
            "path": str(paths["burn"]),
            "burned_sha256": _sha(paths["burn"]),
            "ass_path": str(paths["ass"]),
            "branding_intro": authority["delivery"]["branding_intro"],
        },
        "human_text_correction_manifest_path": str(receipt),
        "human_text_correction_manifest_sha256": _sha(receipt),
    }
    paths["record"].write_text(json.dumps(record), encoding="utf-8")
    paths["delivery_video"].write_bytes(paths["burn"].read_bytes())
    paths["delivery_srt"].write_bytes(paths["srt"].read_bytes())
    delivery_record = paths["delivery_record"]
    delivery_receipt = Path(str(authority["delivery"]["correction_receipts"]["delivery_path"]))
    delivery_record.write_bytes(paths["record"].read_bytes())
    delivery_receipt.write_bytes(receipt.read_bytes())
    for descriptor in authority["delivery"]["speaker_sidecars"].values():
        Path(str(descriptor["path"])).unlink()
    return expected


def _sealed_receipt(
    authority: dict[str, object], paths: dict[str, Path]
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    correction = authority["correction"]
    artifacts = authority["artifacts"]
    delivery = authority["delivery"]
    assert isinstance(correction, dict) and isinstance(artifacts, dict) and isinstance(delivery, dict)
    seal = {
        "mode": "GIT_HEAD",
        "deployed_commit": "a" * 40,
        "relative_path": RELATIVE_PATH,
        "sha256": "sha256:" + "b" * 64,
    }
    repository_seal = {
        "absolute_path": Path("/sealed") / RELATIVE_PATH,
        "sha256": seal["sha256"],
        "seal": seal,
    }
    receipt = {
        "schema_version": "human-subtitle-correction.v2",
        "stage_order": "human_text_then_speaker_then_burn",
        "corrected_at": "2026-08-21T00:00:00+00:00",
        "candidate_id": CANDIDATE_ID,
        "before_srt_sha256": correction["source_srt_sha256"].removeprefix("sha256:"),
        "after_srt_sha256": correction["output_srt_sha256"].removeprefix("sha256:"),
        "replace_operations": [],
        "set_line_operations": [
            f"{row['cue_index']}={row['after']}" for row in correction["replacements"]
        ],
        "refresh_only": False,
        "text_source": None,
        "text_source_sha256": None,
        "text_override": None,
        "text_override_sha256": None,
        "text_override_manifest": None,
        "text_override_manifest_sha256": None,
        "timing_source": None,
        "timing_source_sha256": None,
        "text_override_decision_output": None,
        "text_override_decision_output_sha256": None,
        "text_override_output": None,
        "text_override_output_sha256": None,
        "speaker_mode": "uniform_host",
        "speaker_manifest": None,
        "speaker_manifest_sha256": None,
        "burned_media": str(paths["burn"]),
        "burned_media_sha256": _sha(paths["burn"]).removeprefix("sha256:"),
        "delivery_branding_authority": {
            "schema_version": "sealed-subtitle-correction-delivery-authority.v1",
            "authority_path": str(repository_seal["absolute_path"]),
            "authority_sha256": repository_seal["sha256"],
            "authority_repository_seal": seal,
            "record_sha256": artifacts["record"]["sha256"],
            "publish_sha256": artifacts["publish"]["sha256"],
            "burned_video_sha256": artifacts["burn"]["sha256"],
            "branding_intro": delivery["branding_intro"],
        },
        "upload_enabled": False,
    }
    return receipt, repository_seal, seal


def test_deployed_authority_and_manual_title_are_exact() -> None:
    authority = json.loads(ASSET.read_text(encoding="utf-8"))
    normalized = validate_authority(authority)

    assert normalized["manual_title"] == "小李有女友感吗？宿敌是否有点亲密了"
    assert manual_title_override(CANDIDATE_ID) == normalized["manual_title"]
    glossary = (ROOT / "assets/lidousha/glossary.txt").read_text(encoding="utf-8")
    assert "妹感妈" in glossary
    assert "其它候选或其它上下文" in glossary


def test_diagnostic_decision_receipt_is_scoped_and_rejects_drift() -> None:
    authority = validate_authority(json.loads(ASSET.read_text(encoding="utf-8")))
    root = ASSET.parent
    pipeline = (root / "auto_123655_771_844.pipeline-diagnostic.srt").read_text(encoding="utf-8")
    receipt = json.loads(
        (root / "auto_123655_771_844.diagnostic-diff.v1.json").read_text(encoding="utf-8")
    )
    _validate_diagnostic_decision_receipt(
        authority, pipeline_text=pipeline, receipt_payload=receipt
    )
    drifted = deepcopy(receipt)
    drifted["rows"][1]["release_text"] = "unlisted release change"
    with pytest.raises(SealedSubtitleCorrectionError, match="unchanged decision drifts"):
        _validate_diagnostic_decision_receipt(
            authority, pipeline_text=pipeline, receipt_payload=drifted
        )
    with pytest.raises(SealedSubtitleCorrectionError, match="cue geometry drifts"):
        _validate_diagnostic_decision_receipt(
            authority, pipeline_text=pipeline.replace("女友感没有感觉", "drift", 1), receipt_payload=receipt
        )


def test_diagnostic_assets_require_the_sealed_pipeline_to_equal_live_preimage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = validate_authority(json.loads(ASSET.read_text(encoding="utf-8")))
    root = ASSET.parent
    pipeline = (root / "auto_123655_771_844.pipeline-diagnostic.srt").read_bytes()
    receipt = (root / "auto_123655_771_844.diagnostic-diff.v1.json").read_bytes()

    def fake_repository_asset_bytes(*, descriptor, **_kwargs):
        return pipeline if descriptor["path"].endswith(".srt") else receipt

    monkeypatch.setattr(
        sealed_correction, "_repository_asset_bytes", fake_repository_asset_bytes
    )
    validate_diagnostic_assets(
        authority, repo_root=ROOT, expected_source_text=pipeline.decode("utf-8")
    )
    with pytest.raises(SealedSubtitleCorrectionError, match="does not match live preimage"):
        validate_diagnostic_assets(
            authority, repo_root=ROOT, expected_source_text="different live preimage\n"
        )


def test_runtime_requires_exact_four_cue_transform_and_immutable_geometry(tmp_path: Path) -> None:
    authority, paths = _runtime_authority(tmp_path)

    output = validate_runtime(authority)
    source_rows = _cue_source(authority).split("\n\n")
    output_rows = output.split("\n\n")
    assert len(source_rows) == len(output_rows) == 33
    for index, (before, after) in enumerate(zip(source_rows, output_rows), start=1):
        before_lines, after_lines = before.splitlines(), after.splitlines()
        assert before_lines[:2] == after_lines[:2]
        assert (before_lines[2:] != after_lines[2:]) is (index in {1, 3, 6, 27})
    assert "你觉得自己有女友感吗" in output_rows[0]
    assert "感觉kmx比较多吧" in output_rows[4]
    assert "其实是主人" in output_rows[13]

    extra = output.replace("unchanged cue 2", "unlisted change")
    with pytest.raises(SealedSubtitleCorrectionError, match="cue text drifts"):
        _validate_exact_cue_transform(authority, source_text=_cue_source(authority), output_text=extra)


def test_runtime_fails_closed_for_preimage_drift_missing_symlink_and_reentry(tmp_path: Path) -> None:
    authority, paths = _runtime_authority(tmp_path)
    paths["srt"].write_text("drift\n", encoding="utf-8")
    with pytest.raises(SealedSubtitleCorrectionError, match="preimage drifts"):
        validate_runtime(authority)

    authority, paths = _runtime_authority(tmp_path / "missing")
    paths["cover"].unlink()
    with pytest.raises(SealedSubtitleCorrectionError, match="unavailable"):
        validate_runtime(authority)

    authority, paths = _runtime_authority(tmp_path / "symlink")
    paths["main"].unlink()
    paths["main"].symlink_to(paths["cover"])
    with pytest.raises(SealedSubtitleCorrectionError, match="regular non-symlink"):
        validate_runtime(authority)

    authority, paths = _runtime_authority(tmp_path / "reentry")
    paths["srt"].write_text(
        expected_output_srt(authority, paths["srt"].read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(SealedSubtitleCorrectionError, match="preimage drifts"):
        validate_runtime(authority)


def test_post_transaction_requires_all_mirrors_and_rejects_tampering(tmp_path: Path) -> None:
    authority, paths = _runtime_authority(tmp_path)
    expected = _materialize_post_transaction(authority, paths)

    validate_post_transaction(authority, expected_srt=expected)
    paths["delivery_video"].write_bytes(b"tampered delivery\n")
    with pytest.raises(SealedSubtitleCorrectionError, match="delivery mirrors drift"):
        validate_post_transaction(authority, expected_srt=expected)


@pytest.mark.parametrize(
    "fault",
    ["schema", "before", "operations", "text_lane", "speaker", "burn", "authority", "upload"],
)
def test_correction_receipt_rejects_each_closure_class(
    tmp_path: Path, fault: str
) -> None:
    authority, paths = _runtime_authority(tmp_path)
    _materialize_post_transaction(authority, paths)
    record = json.loads(paths["record"].read_text(encoding="utf-8"))
    correction_path = Path(str(authority["delivery"]["correction_receipts"]["primary_path"]))
    receipt, repository_seal, _seal = _sealed_receipt(authority, paths)
    if fault == "schema":
        receipt["schema_version"] = "bad"
    elif fault == "before":
        receipt["before_srt_sha256"] = "0" * 64
    elif fault == "operations":
        receipt["set_line_operations"] = list(reversed(receipt["set_line_operations"]))
    elif fault == "text_lane":
        receipt["text_override"] = "forbidden"
    elif fault == "speaker":
        receipt["speaker_mode"] = "auto"
    elif fault == "burn":
        receipt["burned_media"] = "wrong"
    elif fault == "authority":
        receipt["delivery_branding_authority"]["record_sha256"] = "sha256:" + "0" * 64
    elif fault == "upload":
        receipt["upload_enabled"] = True
    with pytest.raises(SealedSubtitleCorrectionError):
        _validate_correction_receipt(
            authority,
            receipt=receipt,
            correction_path=correction_path,
            record=record,
            burn_path=paths["burn"],
            repository_seal=repository_seal,
        )


def test_runner_dry_run_is_no_transaction_and_apply_has_one_internal_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = validate_authority(json.loads(ASSET.read_text(encoding="utf-8")))
    seal = {"relative_path": "asset.json", "sha256": "sha256:" + "a" * 64}
    calls: list[object] = []
    monkeypatch.setenv("AUTOSLICE_SPEAKER_MODE", "uniform_host")
    monkeypatch.setattr(runner, "load_deployed_authority", lambda _root: (authority, seal))
    monkeypatch.setattr(
        runner, "validate_runtime", lambda _authority, **_kwargs: "sealed output\n"
    )
    monkeypatch.setattr(runner.correction, "main", lambda *args, **kwargs: calls.append((args, kwargs)) or 1)

    assert runner.main(["--dry-run"]) == 0
    assert calls == []
    assert runner.main(["--apply"]) == 1
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert "--cid" in argv[0] and CANDIDATE_ID in argv[0]
    assert "--set-line" in argv[0]
    assert set(kwargs) == {"_sealed_transaction_context"}
    with pytest.raises(SystemExit):
        runner._parse_args(["--dry-run", "--cid", CANDIDATE_ID])


def test_generic_correction_rejects_raw_sealed_context_before_staging(tmp_path: Path) -> None:
    candidate = "auto_reject_raw_sealed_context"
    recut = tmp_path / "out" / "2026-08-17" / candidate / "replacement_recuts"
    recut.mkdir(parents=True)
    srt = recut / "candidate.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧文\n", encoding="utf-8")
    media = recut / "candidate.mp4"
    media.write_bytes(b"main")
    delivery = recut / "candidate.burned.mp4"
    delivery.write_bytes(b"burned")
    (recut / f"{candidate}.record.json").write_text(
        json.dumps({"subtitle_path": str(srt), "media_path": str(media)}),
        encoding="utf-8",
    )

    assert generic_correction.main(
        [
            "--cid", candidate,
            "--date", "2026-08-17",
            "--delivery", str(delivery),
            "--replace", "旧文=新文",
            "--out-base", str(tmp_path),
        ],
        _sealed_transaction_context={"branding": "forged"},
    ) == 2
    assert srt.read_text(encoding="utf-8").endswith("旧文\n")


def test_sealed_context_preserves_tags_and_only_allows_record_correction_fields(
    tmp_path: Path,
) -> None:
    context = SealedSubtitleCorrectionTransactionContext(tmp_path)
    before = {"upload_tags": ["frozen"], "artifact_hashes": {"subtitle_sha256": "old"}, "title": "fixed"}
    allowed = deepcopy(before)
    allowed["artifact_hashes"]["subtitle_sha256"] = "new"
    allowed["burned_preview"] = {"path": "new"}
    context.validate_record_delta(before=before, after=allowed)
    assert not generic_correction._should_regenerate_upload_tags(context)
    assert generic_correction._should_regenerate_upload_tags(None)

    changed_tags = deepcopy(allowed)
    changed_tags["upload_tags"] = ["provider output"]
    with pytest.raises(SealedSubtitleCorrectionError, match="regenerate upload tags"):
        context.validate_record_delta(before=before, after=changed_tags)
    unrelated = deepcopy(allowed)
    unrelated["title"] = "drift"
    with pytest.raises(SealedSubtitleCorrectionError, match="outside the allowlist"):
        context.validate_record_delta(before=before, after=unrelated)
