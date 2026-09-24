from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import package_import as pi
from tests.test_import_external_package import CANDIDATE, _build_external_package


def _inventory(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _mixed_internal_locators(tmp_path: Path):
    fixture = _build_external_package(tmp_path)
    package = fixture.staging_package
    record_path = package / f"{CANDIDATE}.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))

    # The producer physically included the frozen context in the package, but
    # left two mutable runtime locators naming transient assembly directories.
    context_name = f"{CANDIDATE}.clip-context.json"
    (package / context_name).write_bytes((fixture.staging_candidate / context_name).read_bytes())
    record["publish_staging"]["publish_json_path"] = (
        f"/transient/review-package/{CANDIDATE}.recut.publish.json"
    )
    record["clip_context_path"] = f"/transient/work/{context_name}"
    record_path.write_bytes(pi.json_bytes(record))
    return fixture, record_path


def test_projects_hash_bound_package_internal_locators_without_source_mutation(
    tmp_path: Path,
) -> None:
    fixture, record_path = _mixed_internal_locators(tmp_path)
    before = _inventory(fixture.staging_package)
    declared_package = Path(
        json.loads(record_path.read_text(encoding="utf-8"))["subtitle_path"]
    ).parent

    plan = pi.plan_import(
        source_package_dir=fixture.staging_package,
        destination_package_root=fixture.destination_package,
        destination_repo_root=fixture.repo_root,
        candidate_id=CANDIDATE,
    )

    assert plan.documents.record["publish_staging"]["publish_json_path"] == str(
        declared_package / f"{CANDIDATE}.recut.publish.json"
    )
    assert plan.documents.record["clip_context_path"] == str(
        declared_package / f"{CANDIDATE}.clip-context.json"
    )
    assert _inventory(fixture.staging_package) == before


def test_projected_internal_locators_flow_through_transactional_relocation(
    tmp_path: Path,
) -> None:
    fixture, _record_path = _mixed_internal_locators(tmp_path)
    plan = pi.plan_import(
        source_package_dir=fixture.staging_package,
        destination_package_root=fixture.destination_package,
        destination_repo_root=fixture.repo_root,
        candidate_id=CANDIDATE,
    )
    pi.execute_copy(plan, apply=True)
    result = pi.relocate_package(
        package_root=fixture.destination_package,
        candidate_id=CANDIDATE,
        roots=plan.roots,
        apply=True,
    )

    assert result["status"] == "RELOCATED"
    journal = json.loads(Path(result["journal_path"]).read_text(encoding="utf-8"))
    changed = journal["documents"]["record"]["changed_pointers"]
    assert "/publish_staging/publish_json_path" in changed
    assert "/clip_context_path" in changed
    record = json.loads(
        (fixture.destination_package / f"{CANDIDATE}.record.json").read_text(encoding="utf-8")
    )
    assert record["publish_staging"]["publish_json_path"].startswith(
        str(fixture.destination_package)
    )
    assert record["clip_context_path"].startswith(str(fixture.destination_package))


def test_package_internal_context_hash_drift_refuses(tmp_path: Path) -> None:
    fixture, _record_path = _mixed_internal_locators(tmp_path)
    (fixture.staging_package / f"{CANDIDATE}.clip-context.json").write_bytes(b'{"drift":true}\n')

    with pytest.raises(pi.PackageImportError, match="PACKAGE_INTERNAL_LOCATOR_INCONSISTENT"):
        pi.plan_import(
            source_package_dir=fixture.staging_package,
            destination_package_root=fixture.destination_package,
            destination_repo_root=fixture.repo_root,
            candidate_id=CANDIDATE,
        )


def test_package_internal_publish_byte_drift_refuses(tmp_path: Path) -> None:
    fixture, _record_path = _mixed_internal_locators(tmp_path)
    publish = fixture.staging_package / f"{CANDIDATE}.recut.publish.json"
    publish.write_bytes(publish.read_bytes().rstrip() + b" \n")

    with pytest.raises(pi.PackageImportError, match="PACKAGE_INTERNAL_LOCATOR_INCONSISTENT"):
        pi.plan_import(
            source_package_dir=fixture.staging_package,
            destination_package_root=fixture.destination_package,
            destination_repo_root=fixture.repo_root,
            candidate_id=CANDIDATE,
        )


def test_package_internal_context_symlink_refuses(tmp_path: Path) -> None:
    fixture, _record_path = _mixed_internal_locators(tmp_path)
    context = fixture.staging_package / f"{CANDIDATE}.clip-context.json"
    target = tmp_path / "outside-context.json"
    target.write_bytes(context.read_bytes())
    context.unlink()
    context.symlink_to(target)

    with pytest.raises(pi.PackageImportError, match="PACKAGE_INTERNAL_LOCATOR_INCONSISTENT"):
        pi.plan_import(
            source_package_dir=fixture.staging_package,
            destination_package_root=fixture.destination_package,
            destination_repo_root=fixture.repo_root,
            candidate_id=CANDIDATE,
        )


def test_suspicious_relative_internal_locator_refuses(tmp_path: Path) -> None:
    fixture, record_path = _mixed_internal_locators(tmp_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["clip_context_path"] = f"../{CANDIDATE}.clip-context.json"
    record_path.write_bytes(pi.json_bytes(record))

    with pytest.raises(pi.PackageImportError, match="PACKAGE_INTERNAL_LOCATOR_INCONSISTENT"):
        pi.plan_import(
            source_package_dir=fixture.staging_package,
            destination_package_root=fixture.destination_package,
            destination_repo_root=fixture.repo_root,
            candidate_id=CANDIDATE,
        )
