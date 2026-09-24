"""Zero-provider title/cover joint-QC locator successor tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import run_title_cover_joint_qc as qc
from src.autoslice.title_cover_qc_locator_successor import (
    TitleCoverQcSuccessorError,
    create_title_cover_qc_locator_successor,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inventory(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        str(path.relative_to(root)): (
            _sha(path.read_bytes()),
            path.stat().st_size,
            path.stat().st_mode & 0o7777,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _package(root: Path, *, candidate_id: str, title: str, cover_bytes: bytes) -> Path:
    root.mkdir(parents=True)
    os.chmod(root, 0o700)
    (root / "clip.mp4").write_bytes(b"fixture-video")
    cover = root / "clip.cover.png"
    cover.write_bytes(cover_bytes)
    reference = root / "evidence" / f"{candidate_id}.reference.png"
    reference.parent.mkdir()
    reference.write_bytes(b"fixture-reference")
    record = {"story_contract": {"candidate_id": candidate_id}}
    publish = {
        "title": title,
        "cover_generation": {
            "final_cover": cover.name,
            "final_cover_sha256": "sha256:" + _sha(cover.read_bytes()),
        },
    }
    (root / "clip.record.json").write_text(json.dumps(record), encoding="utf-8")
    (root / "clip.publish.json").write_text(json.dumps(publish), encoding="utf-8")
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "candidate_id": candidate_id,
                        "title": title,
                        "video": "clip.mp4",
                        "cover": cover.name,
                        "record": "clip.record.json",
                        "publish_json": "clip.publish.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return reference


def _source_receipt(root: Path, *, candidate_id: str, title: str) -> Path:
    cover = root / "clip.cover.png"
    reference = root / "evidence" / f"{candidate_id}.reference.png"
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Synthetic fixture; no provider call occurred.",
        "pass": True,
    }

    def probe(path: Path, _prompt: str) -> dict:
        return {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "status": "OBSERVED",
            "model": "fixture-model",
            "image_path": str(path.resolve()),
            "image_sha256": _sha(path.read_bytes()),
            "answer": json.dumps(verdict, ensure_ascii=False),
            "response_sha256": "fixture-response-sha",
            "reference_image": {
                "image_path": str(reference.resolve()),
                "image_sha256": _sha(reference.read_bytes()),
                "frame_sha256": "fixture-reference-frame",
            },
        }

    receipt = qc.build_joint_qc_receipt(
        cover_path=cover,
        title=title,
        candidate_id=candidate_id,
        image_probe=probe,
    )
    receipt["source_reference"] = {
        "schema_version": "joint-qc-package-source-reference.v1",
        "candidate_id": candidate_id,
        "cover_sha256": receipt["cover_sha256"],
        "package_binding_sha256": "sha256:" + "a" * 64,
        "reference_path": str(reference.relative_to(root)),
        "reference_sha256": "sha256:" + _sha(reference.read_bytes()),
    }
    path = root / "title-cover-joint-qc.json"
    path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)
    return path


@pytest.fixture
def packages(tmp_path: Path):
    candidate_id = "auto_fixture_qc_successor"
    title = "【李豆沙】同字节封面路径接续"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _package(source, candidate_id=candidate_id, title=title, cover_bytes=b"same-cover")
    _package(destination, candidate_id=candidate_id, title=title, cover_bytes=b"same-cover")
    source_receipt = _source_receipt(source, candidate_id=candidate_id, title=title)
    output = destination / f"{candidate_id}.title-cover-joint-qc.json"
    return source, source_receipt, destination, output, candidate_id, title


def test_create_successor_rebinds_only_paths_and_reuses_native_gate(packages) -> None:
    source, source_receipt, destination, output, candidate_id, title = packages
    source_before = _inventory(source)
    destination_before = _inventory(destination)
    original = json.loads(source_receipt.read_text(encoding="utf-8"))

    result = create_title_cover_qc_locator_successor(
        source_package_root=source,
        source_receipt_path=source_receipt,
        destination_package_root=destination,
        output_path=output,
        title=title,
    )

    projected = json.loads(output.read_text(encoding="utf-8"))
    assert result == projected
    assert output.stat().st_mode & 0o7777 == 0o600
    assert projected["candidate_id"] == candidate_id
    assert projected["cover_path"] == str((destination / "clip.cover.png").resolve())
    assert projected["witness"]["image_path"] == projected["cover_path"]
    assert projected["witness"]["reference_image"]["image_path"] == str(
        (destination / "evidence" / f"{candidate_id}.reference.png").resolve()
    )
    assert projected["verdict"] == original["verdict"]
    assert projected["witness"]["answer"] == original["witness"]["answer"]
    assert projected["witness"]["response_sha256"] == original["witness"]["response_sha256"]
    assert projected["generated_at"] == original["generated_at"]
    assert projected["locator_successor"]["provider_calls"] == 0
    assert qc.reuse_valid_qc(destination, title, output) == projected
    assert _inventory(source) == source_before
    assert {
        key: value for key, value in _inventory(destination).items() if key != output.name
    } == destination_before


def test_projection_chains_an_existing_locator_successor(packages) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    prior = json.loads(source_receipt.read_text(encoding="utf-8"))
    prior["locator_successor"] = {
        "schema_version": "prior-locator-successor.v1",
        "source_receipt_sha256": "sha256:" + "a" * 64,
        "target_cover_path": str((source / "clip.cover.png").resolve()),
        "provider_calls": 0,
    }
    source_receipt.write_text(
        json.dumps(prior, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    prior_sha = _sha(source_receipt.read_bytes())

    projected = create_title_cover_qc_locator_successor(
        source_package_root=source,
        source_receipt_path=source_receipt,
        destination_package_root=destination,
        output_path=output,
        title=title,
    )

    assert projected["locator_successor"]["source_receipt_sha256"] == (
        "sha256:" + prior_sha
    )
    assert projected["locator_successor"]["provider_calls"] == 0
    assert qc.reuse_valid_qc(destination, title, output) == projected


@pytest.mark.parametrize("mutation", ["title", "cover", "reference", "candidate"])
def test_projection_refuses_changed_authority(packages, mutation: str) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    if mutation == "title":
        title = title + "漂移"
    elif mutation == "cover":
        (destination / "clip.cover.png").write_bytes(b"changed-cover")
    elif mutation == "reference":
        (destination / "evidence" / "auto_fixture_qc_successor.reference.png").write_bytes(
            b"changed-reference"
        )
    else:
        record = json.loads((destination / "clip.record.json").read_text())
        record["story_contract"]["candidate_id"] = "other-candidate"
        (destination / "clip.record.json").write_text(json.dumps(record))

    with pytest.raises((TitleCoverQcSuccessorError, ValueError)):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert not output.exists()


@pytest.mark.parametrize("existing", ["file", "symlink"])
def test_projection_never_overwrites_existing_output(packages, existing: str) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    sentinel = destination / "sentinel.json"
    sentinel.write_text("sentinel")
    if existing == "file":
        output.write_text("do-not-overwrite")
    else:
        output.symlink_to(sentinel)
    before = output.read_bytes()

    with pytest.raises((FileExistsError, ValueError, OSError)):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert output.read_bytes() == before
    assert sentinel.read_text() == "sentinel"


def test_projection_cleans_its_own_output_when_final_native_replay_fails(
    packages, monkeypatch
) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    from src.autoslice import title_cover_qc_locator_successor as successor

    original = successor._reuse_valid_qc
    calls = 0

    def fail_target(package_root: Path, bound_title: str, receipt_path: Path):
        nonlocal calls
        calls += 1
        if package_root.resolve() == destination.resolve():
            raise ValueError("synthetic target replay failure")
        return original(package_root, bound_title, receipt_path)

    monkeypatch.setattr(successor, "_reuse_valid_qc", fail_target)
    with pytest.raises(TitleCoverQcSuccessorError, match="target replay"):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert calls >= 2
    assert not output.exists()


def test_portable_generic_receipt_resolves_byte_identical_authority(
    tmp_path: Path,
) -> None:
    from src.autoslice.title_cover_qc_locator_successor import (
        resolve_portable_qc_authority,
    )

    candidate_id = "auto_fixture_portable_authority"
    title = "【李豆沙】冻结回执找回原权威"
    authority = tmp_path / "authority"
    portable = tmp_path / "portable"
    _package(authority, candidate_id=candidate_id, title=title, cover_bytes=b"same")
    _package(portable, candidate_id=candidate_id, title=title, cover_bytes=b"same")
    source_receipt = _source_receipt(authority, candidate_id=candidate_id, title=title)
    portable_receipt = portable / "title-cover-joint-qc.json"
    portable_receipt.write_bytes(source_receipt.read_bytes())
    os.chmod(portable_receipt, 0o600)

    assert resolve_portable_qc_authority(
        portable_package_root=portable,
        title=title,
        candidate_id=candidate_id,
    ) == (authority.resolve(), source_receipt.resolve())

    portable_receipt.write_bytes(portable_receipt.read_bytes() + b" ")
    with pytest.raises(TitleCoverQcSuccessorError, match="differs"):
        resolve_portable_qc_authority(
            portable_package_root=portable,
            title=title,
            candidate_id=candidate_id,
        )


def test_portable_candidate_receipt_resolves_byte_identical_authority(
    tmp_path: Path,
) -> None:
    from src.autoslice.title_cover_qc_locator_successor import (
        resolve_portable_qc_authority,
    )

    candidate_id = "auto_fixture_candidate_authority"
    title = "【李豆沙】候选名回执找回原权威"
    authority = tmp_path / "authority"
    portable = tmp_path / "portable"
    _package(authority, candidate_id=candidate_id, title=title, cover_bytes=b"same")
    _package(portable, candidate_id=candidate_id, title=title, cover_bytes=b"same")
    generic = _source_receipt(authority, candidate_id=candidate_id, title=title)
    authority_receipt = authority / f"{candidate_id}.title-cover-joint-qc.json"
    generic.rename(authority_receipt)
    portable_receipt = portable / authority_receipt.name
    portable_receipt.write_bytes(authority_receipt.read_bytes())
    os.chmod(portable_receipt, 0o600)

    assert resolve_portable_qc_authority(
        portable_package_root=portable,
        title=title,
        candidate_id=candidate_id,
    ) == (authority.resolve(), authority_receipt.resolve())


def test_projection_rejects_source_authority_change_after_target_replay(
    packages, monkeypatch
) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    from src.autoslice import title_cover_qc_locator_successor as successor

    original = successor._reuse_valid_qc
    mutated = False

    def mutate_after_target(package_root: Path, bound_title: str, receipt_path: Path):
        nonlocal mutated
        result = original(package_root, bound_title, receipt_path)
        if package_root.resolve() == destination.resolve() and not mutated:
            document = json.loads(source_receipt.read_text(encoding="utf-8"))
            document["generated_at"] = "2099-01-01T00:00:00Z"
            source_receipt.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            mutated = True
        return result

    monkeypatch.setattr(successor, "_reuse_valid_qc", mutate_after_target)
    with pytest.raises(TitleCoverQcSuccessorError, match="source QC authority changed"):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert mutated is True
    assert not output.exists()


def test_projection_rejects_destination_change_during_final_source_replay(
    packages, monkeypatch
) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    from src.autoslice import title_cover_qc_locator_successor as successor

    original = successor._replay_source_authority_unchanged
    mutated = False

    def mutate_destination_after_source_replay(**kwargs):
        nonlocal mutated
        result = original(**kwargs)
        (destination / "clip.cover.png").write_bytes(b"late-destination-drift")
        mutated = True
        return result

    monkeypatch.setattr(
        successor,
        "_replay_source_authority_unchanged",
        mutate_destination_after_source_replay,
    )
    with pytest.raises(TitleCoverQcSuccessorError, match="destination QC package changed"):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert mutated is True
    assert not output.exists()


def test_package_root_ancestor_symlink_refuses(packages, tmp_path: Path) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(source.parent, target_is_directory=True)
    aliased_source = alias_parent / source.name
    aliased_receipt = aliased_source / source_receipt.name

    with pytest.raises(TitleCoverQcSuccessorError, match="symlink"):
        create_title_cover_qc_locator_successor(
            source_package_root=aliased_source,
            source_receipt_path=aliased_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert not output.exists()


def test_source_receipt_symlink_refuses(packages) -> None:
    source, source_receipt, destination, output, _candidate_id, title = packages
    real = source / "receipt.real.json"
    source_receipt.rename(real)
    source_receipt.symlink_to(real)
    with pytest.raises(TitleCoverQcSuccessorError, match="regular file"):
        create_title_cover_qc_locator_successor(
            source_package_root=source,
            source_receipt_path=source_receipt,
            destination_package_root=destination,
            output_path=output,
            title=title,
        )
    assert not output.exists()
