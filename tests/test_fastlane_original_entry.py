"""Original fastlane entrypoints never regenerate media or grant publication."""

import json
import pytest

from scripts import prepare_fastlane_original_patch as entry
from src.autoslice.fastlane_original_patch import OriginalPatchError, load_original_patch
from tests.test_fastlane_original_patch import CID, git_fixture



def test_original_preparation_can_resume_exact_unchanged_output(tmp_path):
    repo, directory = git_fixture(tmp_path)
    payload, receipt = load_original_patch(repo, directory, CID)
    out = tmp_path.resolve() / "prepared"
    first = entry.prepare_output(CID, out, payload, receipt, reuse_existing=False)
    before = {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in out.iterdir()
    }
    second = entry.prepare_output(CID, out, payload, receipt, reuse_existing=True)
    assert first["status"] == "ORIGINAL_PATCH_PREPARED_NO_UPLOAD"
    assert second["status"] == "ORIGINAL_PATCH_REUSED_NO_UPLOAD"
    assert second["provider_calls"] == 0
    assert {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino) for p in out.iterdir()
    } == before
    with pytest.raises(FileExistsError):
        entry.prepare_output(CID, out, payload, receipt, reuse_existing=False)


@pytest.mark.parametrize("mutation", ["subtitle", "receipt", "partial", "extra", "symlink"])
def test_resume_never_overwrites_partial_or_changed_original_output(tmp_path, mutation):
    repo, directory = git_fixture(tmp_path)
    payload, receipt = load_original_patch(repo, directory, CID)
    out = tmp_path.resolve() / "prepared"
    entry.prepare_output(CID, out, payload, receipt, reuse_existing=False)
    srt = out / (CID + ".srt")
    if mutation == "subtitle":
        srt.write_bytes(payload + b"\n")
    elif mutation == "receipt":
        (out / "original-patch-receipt.json").write_text("{}")
    elif mutation == "partial":
        (out / "original-patch-receipt.json").unlink()
    elif mutation == "extra":
        (out / "unrelated.txt").write_text("preserve")
    else:
        srt.unlink()
        srt.symlink_to(out / "original-patch-receipt.json")
    with pytest.raises((OriginalPatchError, ValueError, OSError)):
        entry.prepare_output(CID, out, payload, receipt, reuse_existing=True)


def test_package_check_requires_original_schema_and_exact_candidate_before_audit(
    tmp_path, monkeypatch
):
    from scripts import audit_review_package as auditor

    monkeypatch.setattr(auditor, "audit_package", lambda *_a: pytest.fail("wrong package audited"))
    root = tmp_path.resolve()
    for document in (
        {"schema_version": "ordinary", "items": [{"candidate_id": CID}]},
        {
            "schema_version": "original-reviewed-fastlane-package.v1",
            "items": [{"candidate_id": "other"}],
        },
    ):
        (root / "review_manifest.json").write_text(json.dumps(document))
        with pytest.raises((OriginalPatchError, ValueError)):
            entry.check_existing_package(CID, root)


def test_original_package_check_dispatches_canonical_audit_but_does_not_hide_failure(
    tmp_path, monkeypatch
):
    from scripts import audit_review_package as auditor

    repo, directory = git_fixture(tmp_path)
    root = tmp_path.resolve() / "package"
    root.mkdir()
    cid = CID
    payload, receipt = load_original_patch(repo, directory, cid)
    monkeypatch.setattr(entry, "_original", lambda candidate: (payload, receipt) if candidate == cid else None)
    (root / "original.srt").write_bytes(payload)
    (root / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "original-reviewed-fastlane-package.v1",
                "upload_allowed": False,
                "items": [{"candidate_id": cid, "subtitle_srt": "original.srt"}],
            }
        )
    )
    calls = []
    result = {"passed": False, "issues": [{"code": "MISSING_ACTUAL_MEDIA"}]}

    def audit(path):
        calls.append(path)
        return result

    monkeypatch.setattr(auditor, "audit_package", audit)
    actual = entry.check_existing_package(cid, root)
    assert calls == [root]
    assert actual["status"] == "ORIGINAL_PACKAGE_CHECK_FAILED"
    assert actual["audit"] == result
    assert actual["provider_calls"] == 0 and actual["upload_authorized"] is False
