"""Hermetic tests for the review_ready -> uniform_host speaker rerender lane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.requeue_review_ready_for_speaker_rerender as requeue_script
from src.autoslice import delivery_recovery, publication_registry


DATE = "2026-08-11"
CID = "auto_173005_934_1166"
OTHER_CID = "synthetic_other_candidate"
AUTHORITY_QUOTE = (
    "目前先把host改成单一host，全部只有李豆沙一人直播，默认这样。重新做一下这些切片。"
)
AUTHORITY_TIMESTAMP = "2026-08-18T00:00:00Z"


def _write_registry(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "publication-registry.v1",
                "authority": "test registry",
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _pick_row(cid: str, *, status: str = "review_ready") -> dict:
    return {
        "candidate_id": cid,
        "status": status,
        "cover_path": None,  # overwritten by harness with a real tmp file
        "cover_sha256": "sha256:" + ("a" * 64),
        "video_sha256": "sha256:" + ("b" * 64),
        "title": "测试标题",
        "pipeline_fingerprint": "sha256:" + ("c" * 64),
    }


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    base = tmp_path / "autoslice"
    (base / "state").mkdir(parents=True)
    state_path = base / "state" / f"{DATE}.json"

    package_root = tmp_path / "lidousha" / DATE
    package_root.mkdir(parents=True)
    cover = package_root / f"{CID}.cover.png"
    cover.write_bytes(b"cover-bytes")

    row = _pick_row(CID)
    row["cover_path"] = str(cover)
    state = {
        "picks": [row],
        "pending_talk": [],
        "talk_backlog": [],
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    registry_path = tmp_path / "registry.json"
    _write_registry(registry_path, [])
    monkeypatch.setattr(publication_registry, "DEFAULT_REGISTRY_PATH", registry_path)
    monkeypatch.setenv("AUTOSLICE_BASE", str(base))

    return {
        "base": base,
        "state_path": state_path,
        "package_root": package_root,
        "registry_path": registry_path,
        "cover": cover,
    }


def _argv(h: dict, *extra: str) -> list[str]:
    return [
        "--date",
        DATE,
        "--base",
        str(h["base"]),
        "--candidate-id",
        CID,
        "--package-root",
        str(h["package_root"]),
        "--authority-quote",
        AUTHORITY_QUOTE,
        "--authority-timestamp",
        AUTHORITY_TIMESTAMP,
        *extra,
    ]


def _state(h: dict) -> dict:
    return json.loads(h["state_path"].read_text(encoding="utf-8"))


def _pick(h: dict, cid: str = CID) -> dict:
    for row in _state(h)["picks"]:
        if row["candidate_id"] == cid:
            return row
    raise AssertionError(f"{cid} not found in picks")


def test_review_ready_apply_flips_to_recoverable_failed(harness):
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness))
    assert rc == 0
    assert harness["state_path"].read_bytes() == preimage  # dry-run default

    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 0
    row = _pick(harness)
    assert row["status"] == "failed"
    assert row["failure_recoverable"] is True
    assert row["revivals"][-1]["schema_version"] == "candidate-revival.v1"
    assert row["sanctioned_revival_retry"]["status"] == "PENDING"
    assert row["speaker_rerender_authority"]["reason"] == "speaker_mode uniform_host rerender"
    assert (
        row["speaker_rerender_authority"]["user_authorization"]["quote"]
        == AUTHORITY_QUOTE
    )
    assert row["speaker_rerender_authority"]["old_package"]["package_root"] == str(
        harness["package_root"]
    )

    # This is the load-bearing check: the production tick machinery itself
    # must recognize this exact row shape as an approved sanctioned revival,
    # not just our own field spelling.
    assert delivery_recovery._pending_sanctioned_revival_retry(row) is not None


def test_dry_run_makes_zero_writes(harness):
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness))
    assert rc == 0
    assert harness["state_path"].read_bytes() == preimage
    row = _pick(harness)
    assert row["status"] == "review_ready"
    assert "sanctioned_revival_retry" not in row


def test_published_candidate_is_refused(harness):
    _write_registry(
        harness["registry_path"],
        [
            {
                "candidate_id": CID,
                "recording_date": DATE,
                "status": "published",
                "bvid": "BV1example",
            }
        ],
    )
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage
    assert _pick(harness)["status"] == "review_ready"


def test_hold_pending_review_candidate_is_refused(harness):
    _write_registry(
        harness["registry_path"],
        [
            {
                "candidate_id": CID,
                "recording_date": DATE,
                "status": "hold_pending_review",
                "note": "维护者 is still reviewing this one",
            }
        ],
    )
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage
    assert _pick(harness)["status"] == "review_ready"


def test_failed_status_candidate_is_refused(harness):
    state = _state(harness)
    state["picks"][0]["status"] = "failed"
    harness["state_path"].write_text(json.dumps(state), encoding="utf-8")
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage
    assert _pick(harness)["status"] == "failed"


def test_missing_candidate_is_refused(harness):
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(
        [
            "--date",
            DATE,
            "--base",
            str(harness["base"]),
            "--candidate-id",
            "auto_does_not_exist",
            "--package-root",
            str(harness["package_root"]),
            "--authority-quote",
            AUTHORITY_QUOTE,
            "--authority-timestamp",
            AUTHORITY_TIMESTAMP,
            "--apply",
        ]
    )
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage


def test_missing_package_file_is_refused(harness):
    state = _state(harness)
    state["picks"][0]["cover_path"] = str(harness["package_root"] / "gone.png")
    harness["state_path"].write_text(json.dumps(state), encoding="utf-8")
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage


def test_already_queued_candidate_is_refused(harness):
    state = _state(harness)
    state["pending_talk"].append({"cid": CID})
    harness["state_path"].write_text(json.dumps(state), encoding="utf-8")
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(_argv(harness, "--apply"))
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage


def test_batch_poison_one_bad_candidate_blocks_whole_batch(harness):
    state = _state(harness)
    other_cover = harness["package_root"] / f"{OTHER_CID}.cover.png"
    other_cover.write_bytes(b"cover-bytes-2")
    other_row = _pick_row(OTHER_CID)
    other_row["cover_path"] = str(other_cover)
    other_row["status"] = "failed"  # this one is invalid on purpose
    state["picks"].append(other_row)
    harness["state_path"].write_text(json.dumps(state), encoding="utf-8")
    preimage = harness["state_path"].read_bytes()

    argv = [
        "--date",
        DATE,
        "--base",
        str(harness["base"]),
        "--candidate-id",
        CID,
        "--candidate-id",
        OTHER_CID,
        "--package-root",
        str(harness["package_root"]),
        "--authority-quote",
        AUTHORITY_QUOTE,
        "--authority-timestamp",
        AUTHORITY_TIMESTAMP,
        "--apply",
    ]
    rc = requeue_script.main(argv)
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage
    assert _pick(harness, CID)["status"] == "review_ready"


def test_atomic_write_failure_leaves_state_untouched(harness, monkeypatch: pytest.MonkeyPatch):
    preimage = harness["state_path"].read_bytes()

    def _boom(_path, _payload):
        raise RuntimeError("simulated disk failure mid-write")

    monkeypatch.setattr(requeue_script, "_atomic_write", _boom)
    with pytest.raises(RuntimeError):
        requeue_script.main(_argv(harness, "--apply"))
    assert harness["state_path"].read_bytes() == preimage
    assert _pick(harness)["status"] == "review_ready"


def test_bad_authority_timestamp_is_refused(harness):
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(
        [
            "--date",
            DATE,
            "--base",
            str(harness["base"]),
            "--candidate-id",
            CID,
            "--package-root",
            str(harness["package_root"]),
            "--authority-quote",
            AUTHORITY_QUOTE,
            "--authority-timestamp",
            "not-a-timestamp",
            "--apply",
        ]
    )
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage


def test_missing_base_directory_is_refused(harness):
    missing_base = harness["base"] / "does-not-exist"
    rc = requeue_script.main(
        [
            "--date",
            DATE,
            "--base",
            str(missing_base),
            "--candidate-id",
            CID,
            "--package-root",
            str(harness["package_root"]),
            "--authority-quote",
            AUTHORITY_QUOTE,
            "--authority-timestamp",
            AUTHORITY_TIMESTAMP,
            "--apply",
        ]
    )
    assert rc == 2
    assert not missing_base.exists()


def test_short_authority_quote_is_refused(harness):
    preimage = harness["state_path"].read_bytes()
    rc = requeue_script.main(
        [
            "--date",
            DATE,
            "--base",
            str(harness["base"]),
            "--candidate-id",
            CID,
            "--package-root",
            str(harness["package_root"]),
            "--authority-quote",
            "ok",
            "--authority-timestamp",
            AUTHORITY_TIMESTAMP,
            "--apply",
        ]
    )
    assert rc == 2
    assert harness["state_path"].read_bytes() == preimage
