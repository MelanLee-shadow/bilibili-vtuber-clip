from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from scripts import freeze_speaker_holdout_sources as freeze


NOW_EPOCH = 1_800_000_000.0
DATE_ROLES = {
    "2026-08-10": "HOLDOUT_A_SOURCE_FROZEN_UNLABELED",
    "2026-08-11": "HOLDOUT_B_SOURCE_FROZEN_UNLABELED",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "recordings" / freeze.ROOM_ID
    finalized: dict[str, object] = {}
    events: list[dict[str, object]] = []
    for date in DATE_ROLES:
        date_root = root / date
        date_root.mkdir(parents=True)
        stem = f"{freeze.ROOM_ID}_{date.replace('-', '')}-10-00-00"
        media = date_root / f"{stem}.mp4"
        flv = date_root / f"{stem}.flv"
        media.write_bytes(f"media:{date}".encode())
        flv.write_bytes(f"flv:{date}".encode())
        media.with_suffix(".xml").write_text("<i/>", encoding="utf-8")
        media.with_suffix(".jsonl").write_text("{}\n", encoding="utf-8")
        media.with_suffix(".meta.json").write_text(
            json.dumps({"event_evidence": "fixture"}), encoding="utf-8"
        )
        adapter_key = f"{date}/{stem}.flv"
        finalized[adapter_key] = {
            "source_size": flv.stat().st_size,
            "source_mtime_ns": flv.stat().st_mtime_ns,
            "target": f"/adapter/Videos/{freeze.ROOM_ID}/{date}/{stem}.mp4",
            "target_sha256": _sha(media),
            "finalized_at": "fixture",
        }
        events.append(
            {
                "EventType": "FileClosed",
                "EventId": f"event:{date}",
                "EventTimestamp": "fixture",
                "EventData": {
                    "RelativePath": f"Videos/{freeze.ROOM_ID}/{date}/{stem}.flv",
                    "FileSize": flv.stat().st_size,
                },
            }
        )

    status = tmp_path / "status.json"
    status.write_text(
        json.dumps(
            {
                "room_id": int(freeze.ROOM_ID),
                "streaming": False,
                "recording": False,
                "finalizing": False,
                "service_reachable": True,
                "running_status": "idle",
                "error": None,
                "generated_at": dt.datetime.fromtimestamp(
                    NOW_EPOCH - 30, tz=dt.UTC
                ).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    adapter = tmp_path / "adapter-state.json"
    adapter.write_text(json.dumps({"finalized": finalized}), encoding="utf-8")
    journal = tmp_path / "events.jsonl"
    journal.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    return {"root": root, "status": status, "adapter": adapter, "journal": journal}


def _run(paths: dict[str, Path], **kwargs: object) -> dict[str, object]:
    return freeze.freeze_sources(
        recording_root=paths["root"],
        status_path=paths["status"],
        adapter_state_path=paths["adapter"],
        webhook_journal_path=paths["journal"],
        date_roles=DATE_ROLES,
        stability_seconds=0,
        now=lambda: NOW_EPOCH,
        sleep=lambda _seconds: None,
        mount_evidence={
            "target": str(paths["root"].parents[1]),
            "source": "CloudFS",
            "filesystem_type": "fuse.test",
        },
        probe=lambda _path: {
            "duration_ms": 1_000,
            "video_codec": "h264",
            "width": 1920,
            "height": 1080,
            "audio_codec": "aac",
        },
        **kwargs,
    )


def test_freeze_binds_two_sealed_source_sessions_without_claiming_cues(
    tmp_path: Path,
) -> None:
    payload = _run(_fixture(tmp_path))

    assert payload["status"] == "SOURCE_FROZEN"
    assert payload["source_inventory_total"] == 2
    assert payload["source_inventory_count_by_date"] == {
        "2026-08-10": 1,
        "2026-08-11": 1,
    }
    assert payload["authority"] == {
        "asr_frozen": False,
        "cue_table_frozen": False,
        "predictions_frozen": False,
        "human_truth_opened": False,
        "production_authority": False,
        "deployment_authority": False,
        "next_required_state": "PRELABEL_PACKAGE_FROZEN",
    }
    assert all(
        row["source_media"]["sha256"] == row["source_media"]["adapter_target_sha256"]
        for row in payload["segments"]
    )


def test_two_clean_extractions_have_identical_deterministic_payload(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    first = _run(paths)
    second = _run(paths)
    assert first["deterministic_payload_sha256"] == second["deterministic_payload_sha256"]


def test_identical_fuse_listing_entries_collapse_to_one_path(tmp_path: Path) -> None:
    path = tmp_path / "same.mp4"
    path.write_bytes(b"same")
    rows, duplicate_count = freeze._deduplicate_listing([path, Path(str(path)), path])
    assert rows == [path]
    assert duplicate_count == 2


def test_adapter_target_hash_drift_fails_closed(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    payload = json.loads(paths["adapter"].read_text(encoding="utf-8"))
    first_key = next(iter(payload["finalized"]))
    payload["finalized"][first_key]["target_sha256"] = "0" * 64
    paths["adapter"].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(freeze.HoldoutSourceFreezeError, match="target hash drifted"):
        _run(paths)


def test_missing_file_closed_event_fails_closed(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    lines = paths["journal"].read_text(encoding="utf-8").splitlines()
    paths["journal"].write_text(lines[0] + "\n", encoding="utf-8")
    with pytest.raises(freeze.HoldoutSourceFreezeError, match="one FileClosed"):
        _run(paths)


def test_live_recording_status_cannot_be_frozen(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    payload = json.loads(paths["status"].read_text(encoding="utf-8"))
    payload["streaming"] = True
    paths["status"].write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(freeze.HoldoutSourceFreezeError, match="not sealed idle"):
        _run(paths)


def test_inventory_mutation_during_stability_observation_fails(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    target = next(paths["root"].glob("*/*.mp4"))

    def mutate(_seconds: float) -> None:
        target.write_bytes(target.read_bytes() + b"drift")

    with pytest.raises(freeze.HoldoutSourceFreezeError, match="changed during stability"):
        freeze.freeze_sources(
            recording_root=paths["root"],
            status_path=paths["status"],
            adapter_state_path=paths["adapter"],
            webhook_journal_path=paths["journal"],
            date_roles=DATE_ROLES,
            stability_seconds=1,
            now=lambda: NOW_EPOCH,
            sleep=mutate,
            mount_evidence={"target": "/", "source": "CloudFS", "filesystem_type": "fuse"},
            probe=lambda _path: {},
        )


def test_create_only_output_cannot_be_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "freeze.json"
    freeze._write_create_only(output, {"ok": True})
    assert json.loads(output.read_text(encoding="utf-8")) == {"ok": True}
    with pytest.raises(freeze.HoldoutSourceFreezeError, match="create-only"):
        freeze._write_create_only(output, {"ok": False})
