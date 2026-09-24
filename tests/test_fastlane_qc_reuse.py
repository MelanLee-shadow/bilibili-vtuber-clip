"""Frozen QC reuse tests. All provider responses are synthetic test fixtures."""

import copy
import hashlib
import json
import os

import pytest

from scripts import run_title_cover_joint_qc as qc
from scripts import import_external_package as importer


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def package(tmp_path):
    root = tmp_path.resolve()
    os.chmod(root, 0o700)
    cid = "auto_fixture_fastlane"
    title = "【李豆沙】原稿定点修改测试"
    (root / "clip.mp4").write_bytes(b"fixture-video")
    cover = root / "clip.cover.png"
    cover.write_bytes(b"fixture-cover")
    record = {"story_contract": {"candidate_id": cid}}
    publish = {
        "title": title,
        "cover_generation": {
            "final_cover": cover.name,
            "final_cover_sha256": "sha256:" + sha(cover.read_bytes()),
        },
    }
    (root / "clip.record.json").write_text(json.dumps(record))
    (root / "clip.publish.json").write_text(json.dumps(publish))
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "candidate_id": cid,
                        "title": title,
                        "video": "clip.mp4",
                        "cover": cover.name,
                        "record": "clip.record.json",
                        "publish_json": "clip.publish.json",
                    }
                ]
            }
        )
    )
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Synthetic test response, not a real image observation",
        "pass": True,
    }

    def probe(path, prompt):
        return {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "status": "OBSERVED",
            "model": "gpt-6-astra",
            "image_path": str(path),
            "image_sha256": "sha256:" + sha(path.read_bytes()),
            "answer": json.dumps(verdict),
        }

    receipt = qc.build_joint_qc_receipt(
        cover_path=cover, title=title, candidate_id=cid, image_probe=probe
    )
    out = root / (cid + ".title-cover-joint-qc.json")
    out.write_text(json.dumps(receipt))
    os.chmod(out, 0o600)
    return root, cid, title, cover, out, receipt


def forbidden(*_args, **_kwargs):
    pytest.fail("a frozen existing QC must not call a provider")


def test_explicit_reuse_accepts_exact_old_receipt_without_writes_or_provider(package):
    root, _, title, _, out, expected = package
    before = (out.read_bytes(), out.stat().st_mtime_ns, out.stat().st_ino)
    assert qc.run_qc(root, title, out, image_probe=forbidden, reuse_existing=True) == expected
    assert (out.read_bytes(), out.stat().st_mtime_ns, out.stat().st_ino) == before


def test_legacy_create_only_remains_create_only(package):
    root, _, title, _, out, _ = package
    with pytest.raises(FileExistsError):
        qc.run_qc(root, title, out, image_probe=forbidden)


@pytest.mark.parametrize(
    "mutation", ["title", "candidate", "hash", "failed", "answer", "symlink", "cover"]
)
def test_bad_existing_receipt_is_neither_overwritten_nor_rerolled(package, mutation):
    root, _, title, cover, out, receipt = package
    value = copy.deepcopy(receipt)
    if mutation == "title":
        value["title"] = "another title"
    elif mutation == "candidate":
        value["candidate_id"] = "another"
    elif mutation == "hash":
        value["cover_sha256"] = "sha256:" + "a" * 64
    elif mutation == "failed":
        value.update(status="FAIL", **{"pass": False})
    elif mutation == "answer":
        value["witness"]["answer"] = "{}"
    elif mutation == "cover":
        cover.write_bytes(b"changed-cover")
    out.write_text(json.dumps(value))
    if mutation == "symlink":
        old = out.with_suffix(".preimage")
        out.rename(old)
        out.symlink_to(old)
    before = out.read_bytes()
    with pytest.raises((ValueError, OSError)):
        qc.run_qc(root, title, out, image_probe=forbidden, reuse_existing=True)
    assert out.read_bytes() == before


def test_importer_consumer_reuses_valid_qc_without_running_qc_command(package):
    root, cid, title, cover, out, expected = package

    class NoCommands:
        def run(self, argv):
            pytest.fail("existing valid QC should be consumed before subprocess dispatch")

    receipt = importer.Receipt(cid, "2026-09-09", True, "2026-09-09T00:00:00Z")
    importer._step_title_cover_qc(
        receipt=receipt,
        gate_runner=NoCommands(),
        destination_package_root=root,
        candidate_id=cid,
        title=title,
        same_stem_cover=cover,
        skip_qc=False,
    )
    row = receipt.steps[-1]
    assert row["status"] == "PASS"
    assert row["reused_existing"] is True and row["provider_calls"] == 0
    assert json.loads(out.read_text()) == expected


def test_reuse_validation_detects_change_during_validation(package, monkeypatch):
    root, _, title, _, out, _ = package
    from scripts import authorized_upload as upload

    original = upload._title_cover_qc_attestation_problems

    def change_after_check(*args, **kwargs):
        result = original(*args, **kwargs)
        out.write_text(out.read_text() + " ")
        return result

    monkeypatch.setattr(upload, "_title_cover_qc_attestation_problems", change_after_check)
    with pytest.raises(ValueError, match="changed"):
        qc.run_qc(root, title, out, image_probe=forbidden, reuse_existing=True)


def test_importer_projects_portable_generic_qc_without_provider(tmp_path):
    from src.autoslice.title_cover_qc_locator_successor import (
        resolve_portable_qc_authority,
    )

    authority = tmp_path / "authority"
    authority.mkdir()
    os.chmod(authority, 0o700)
    cid = "auto_fixture_projected_qc"
    title = "【李豆沙】零调用路径后继"

    def make_package(root):
        root.mkdir(exist_ok=True)
        os.chmod(root, 0o700)
        (root / "clip.mp4").write_bytes(b"fixture-video")
        cover = root / "clip.cover.png"
        cover.write_bytes(b"fixture-cover")
        (root / "clip.record.json").write_text(
            json.dumps({"story_contract": {"candidate_id": cid}})
        )
        (root / "clip.publish.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "cover_generation": {
                        "final_cover": cover.name,
                        "final_cover_sha256": "sha256:" + sha(cover.read_bytes()),
                    },
                }
            )
        )
        (root / "review_manifest.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "candidate_id": cid,
                            "title": title,
                            "video": "clip.mp4",
                            "cover": cover.name,
                            "record": "clip.record.json",
                            "publish_json": "clip.publish.json",
                        }
                    ]
                }
            )
        )
        return cover

    authority_cover = make_package(authority)
    verdict = {
        "lidousha_primary": True,
        "thumbnail_readable": True,
        "single_clear_hook": True,
        "text_overcrowded": False,
        "title_cover_aligned": True,
        "physical_text_line_count": 2,
        "unrelated_or_misleading_elements": [],
        "reason": "Synthetic projection fixture",
        "pass": True,
    }

    def probe(path, _prompt):
        return {
            "schema_version": "cpa-frame-witness.v1",
            "provider": "cpa",
            "status": "OBSERVED",
            "model": "fixture",
            "image_path": str(path.resolve()),
            "image_sha256": sha(path.read_bytes()),
            "answer": json.dumps(verdict),
        }

    generic = authority / "title-cover-joint-qc.json"
    generic.write_text(
        json.dumps(
            qc.build_joint_qc_receipt(
                cover_path=authority_cover,
                title=title,
                candidate_id=cid,
                image_probe=probe,
            )
        )
    )
    os.chmod(generic, 0o600)

    portable = tmp_path / "portable"
    destination = tmp_path / "destination"
    make_package(portable)
    destination_cover = make_package(destination)
    (portable / generic.name).write_bytes(generic.read_bytes())
    os.chmod(portable / generic.name, 0o600)
    assert (
        resolve_portable_qc_authority(
            portable_package_root=portable,
            title=title,
            candidate_id=cid,
        )[0]
        == authority.resolve()
    )

    class NoCommands:
        def run(self, argv):
            pytest.fail("locator successor must prevent provider subprocess dispatch")

    receipt = importer.Receipt(cid, "2026-09-09", True, "2026-09-09T00:00:00Z")
    importer._step_title_cover_qc(
        receipt=receipt,
        gate_runner=NoCommands(),
        destination_package_root=destination,
        candidate_id=cid,
        title=title,
        same_stem_cover=destination_cover,
        skip_qc=False,
        source_package_root=portable,
    )
    row = receipt.steps[-1]
    assert row["status"] == "PASS"
    assert row["provider_calls"] == 0
    assert row["projected_locator_successor"] is True
    output = destination / f"{cid}.title-cover-joint-qc.json"
    assert output.is_file()
    assert qc.reuse_valid_qc(destination, title, output)["cover_path"] == str(
        destination_cover.resolve()
    )
