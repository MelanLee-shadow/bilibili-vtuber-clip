from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
from src.autoslice import song_delivery
from src.autoslice.producer_delivery_transaction import (
    DeliveryArtifact,
    prepare_delivery,
)
from src.autoslice.repository_asset_authority import _canonical_sha256


DATE = "2026-07-25"
CANDIDATE = "semanticsong_362280_490660"
TITLE = "【李豆沙】豆沙歌，《历历万乡》"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _seal_runtime(base: Path) -> None:
    repo = base / "repo"
    repo.mkdir(parents=True)
    commit = "3" * 40
    body = {
        "schema_version": "deployed-authority-manifest.v1",
        "deployed_commit": commit,
        "entries": {
            "docs/pipeline/50-song-lane.md": {
                "bytes": 1,
                "sha256": "sha256:" + "a" * 64,
            }
        },
    }
    manifest = dict(body)
    manifest["manifest_sha256"] = _canonical_sha256(body)
    (repo / "DEPLOYED_COMMIT").write_text(commit + "\n", encoding="utf-8")
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    base = tmp_path / "autoslice"
    base.mkdir()
    _seal_runtime(base)
    monkeypatch.setattr(runner, "BASE", base)

    authority = base / "production-inputs" / DATE / "run" / "song-selector" / CANDIDATE
    authority.mkdir(parents=True)
    publish = authority / "candidate.publish.json"
    publish.write_text(
        json.dumps(
            {
                "schema_version": "shadow-publish-draft.v1",
                "candidate_id": CANDIDATE,
                "title": TITLE,
                "upload_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    summary = authority / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "candidate_id": CANDIDATE,
                        "decision_action": "AUTO_UPLOAD",
                        "reason_codes": [],
                        "materialized_recut": {
                            "status": "MATERIALIZED",
                            "candidate_id": CANDIDATE,
                            "publish_staging": {
                                "status": "STAGED",
                                "title": TITLE,
                                "upload_enabled": False,
                                "publish_json_path": str(publish),
                            },
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    source = base / "out" / DATE / CANDIDATE / "input.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"prepared-song-input")
    target = base / "lidousha" / DATE / "prepared-song.mp4"
    handle = prepare_delivery(
        runtime_root=base,
        lane="song",
        candidate_id=CANDIDATE,
        artifacts=[
            DeliveryArtifact("video", source, target, _sha(source)),
        ],
    )
    result = {
        "status": "delivery_prepared_no_target",
        "prepared_delivery": {
            "manifest_path": str(handle.manifest_path),
            "prepared_sha256": "sha256:" + handle.prepared_sha256,
            "upload_enabled": False,
        },
    }
    return {
        "base": base,
        "summary": summary,
        "summary_sha": _sha(summary),
        "handle": handle,
        "result": result,
        "result_path": base / "private-runs" / "song-result.json",
    }


def test_selector_prepare_is_create_only_and_reentry_is_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    calls: list[dict] = []

    def prepare(**kwargs: object) -> dict:
        calls.append(kwargs)
        return fx["result"]

    monkeypatch.setattr(song_delivery, "prepare_verified_song_package", prepare)
    first = song_delivery.prepare_song_from_selector_summary(
        selector_summary=fx["summary"],
        selector_summary_sha256=fx["summary_sha"],
        candidate_id=CANDIDATE,
        date=DATE,
        result_path=fx["result_path"],
        completion_verifier=lambda _record: {},
    )
    first_bytes = fx["result_path"].read_bytes()
    second = song_delivery.prepare_song_from_selector_summary(
        selector_summary=fx["summary"],
        selector_summary_sha256=fx["summary_sha"],
        candidate_id=CANDIDATE,
        date=DATE,
        result_path=fx["result_path"],
        completion_verifier=lambda _record: {},
    )
    assert len(calls) == 1
    assert first == second
    assert fx["result_path"].read_bytes() == first_bytes
    assert first["status"] == "delivery_prepared_no_target"
    assert "delivered" not in first
    assert "delivery_manifest_path" not in first


def test_selector_prepare_defaults_to_exact_materialized_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    seen: list[dict] = []

    def completion(record: dict) -> dict:
        seen.append(record)
        return {}

    import src.autoslice.song_materialized_successor as successor

    monkeypatch.setattr(successor, "completion", completion)

    def prepare(**kwargs: object) -> dict:
        verifier = kwargs["completion_verifier"]
        assert callable(verifier)
        verifier({"candidate_id": CANDIDATE})
        return fx["result"]

    monkeypatch.setattr(song_delivery, "prepare_verified_song_package", prepare)
    song_delivery.prepare_song_from_selector_summary(
        selector_summary=fx["summary"],
        selector_summary_sha256=fx["summary_sha"],
        candidate_id=CANDIDATE,
        date=DATE,
        result_path=fx["result_path"],
    )
    assert seen == [{"candidate_id": CANDIDATE}]


def test_selector_prepare_refuses_result_tamper_and_wrong_summary_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        song_delivery,
        "prepare_verified_song_package",
        lambda **_kwargs: fx["result"],
    )
    song_delivery.prepare_song_from_selector_summary(
        selector_summary=fx["summary"],
        selector_summary_sha256=fx["summary_sha"],
        candidate_id=CANDIDATE,
        date=DATE,
        result_path=fx["result_path"],
        completion_verifier=lambda _record: {},
    )
    tampered = json.loads(fx["result_path"].read_text(encoding="utf-8"))
    tampered["title"] = "drift"
    fx["result_path"].write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(song_delivery.SongDeliveryError, match="selector binding"):
        song_delivery.prepare_song_from_selector_summary(
            selector_summary=fx["summary"],
            selector_summary_sha256=fx["summary_sha"],
            candidate_id=CANDIDATE,
            date=DATE,
            result_path=fx["result_path"],
            completion_verifier=lambda _record: {},
        )

    with pytest.raises(song_delivery.SongDeliveryError, match="hash mismatch"):
        song_delivery.prepare_song_from_selector_summary(
            selector_summary=fx["summary"],
            selector_summary_sha256="sha256:" + "0" * 64,
            candidate_id=CANDIDATE,
            date=DATE,
            result_path=tmp_path / "second-result.json",
            completion_verifier=lambda _record: {},
        )


def test_selector_prepare_refuses_summary_outside_registered_input_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    outside = fx["base"] / "summary.json"
    outside.write_bytes(fx["summary"].read_bytes())
    with pytest.raises(song_delivery.SongDeliveryError, match="outside BASE"):
        song_delivery.prepare_song_from_selector_summary(
            selector_summary=outside,
            selector_summary_sha256=_sha(outside),
            candidate_id=CANDIDATE,
            date=DATE,
            result_path=fx["result_path"],
            completion_verifier=lambda _record: {},
        )


def test_direct_commit_cannot_enable_registered_summary_scope(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        song_delivery.SongDeliveryError,
        match="only for prepare mode",
    ):
        song_delivery._commit_verified_song_package(
            date=DATE,
            delivery_candidate_id=CANDIDATE,
            summary_record={},
            title=TITLE,
            selector_rc=0,
            summary_authority_root=tmp_path,
            allow_registered_summary_root=True,
        )


def test_selector_prepare_rejects_lexical_summary_parent_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = _fixture(tmp_path, monkeypatch)
    alias = fx["base"] / "production-inputs" / DATE / "alias"
    alias.symlink_to(fx["summary"].parent, target_is_directory=True)
    aliased_summary = alias / "summary.json"
    with pytest.raises(song_delivery.SongDeliveryError, match="outside BASE"):
        song_delivery.prepare_song_from_selector_summary(
            selector_summary=aliased_summary,
            selector_summary_sha256=_sha(aliased_summary),
            candidate_id=CANDIDATE,
            date=DATE,
            result_path=fx["result_path"],
            completion_verifier=lambda _record: {},
        )


def test_create_only_result_never_leaves_partial_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "result.json"
    payload = {"status": "delivery_prepared_no_target"}

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(song_delivery.os, "link", fail_link)
    with pytest.raises(OSError, match="simulated publication failure"):
        song_delivery._write_prepared_result_create_only(path, payload)
    assert not path.exists()
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
