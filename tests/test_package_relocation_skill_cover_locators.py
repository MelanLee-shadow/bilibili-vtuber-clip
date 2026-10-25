"""Skill-candidate cover locators vs legacy cover provenance under relocation.

80-package-delivery L552-557 / L953-955: relocation may only rewrite runtime
locators; cover provenance stays byte-for-byte.  ``choose_path`` /
``request_path`` / ``response_path`` are package-internal runtime locators only
for ``method=skill.candidates`` covers (80 "非歌切候选封面的交付闭包").
Offline: no provider, no network, no credentials.
"""

from __future__ import annotations

import copy

import pytest

from src.autoslice import package_import, package_relocation
from src.autoslice.failed_pick_import import PackageImportError
from src.autoslice.package_relocation_contract import (
    PackageRelocationError,
    is_mutable_pointer,
)

SOURCE_PACKAGE = "/home/producer/out/2099-01-01/auto_synthetic"
SOURCE_CANDIDATE = "/home/producer/out/2099-01-01/candidate"
SOURCE_REPO = "/home/producer/repo"
DEST_PACKAGE = "/srv/delivery/2099-01-01/auto_synthetic"
DEST_CANDIDATE = "/srv/delivery/2099-01-01/candidate"
DEST_REPO = "/srv/repo"
MAPPINGS = (
    (SOURCE_PACKAGE, DEST_PACKAGE),
    (SOURCE_CANDIDATE, DEST_CANDIDATE),
    (SOURCE_REPO, DEST_REPO),
)
SKILL_POINTERS = (
    ("cover_generation", "request_path"),
    ("cover_generation", "response_path"),
    ("cover_generation", "candidate_selection", "choose_path"),
)


def _publish(method: str) -> dict:
    generation = {
        "method": method,
        "final_cover": f"{SOURCE_PACKAGE}/covers/c.cover.png",
        "request_path": f"{SOURCE_PACKAGE}/evidence/request.json",
        "response_path": f"{SOURCE_PACKAGE}/evidence/response.json",
    }
    if method == "skill.candidates":
        generation["candidate_selection"] = {
            "choose_path": f"{SOURCE_PACKAGE}/c.title-cover/choose.json"
        }
    return {
        "video_path": f"{SOURCE_PACKAGE}/c.mp4",
        "cover_path": f"{SOURCE_PACKAGE}/covers/c.cover.png",
        "cover_generation": generation,
    }


def test_skill_cover_locators_are_mutable_only_for_their_skill_document() -> None:
    legacy = _publish("images.edit")
    skill = _publish("skill.candidates")
    for pointer in SKILL_POINTERS:
        # Without the owning document the guard fails closed (pre-D contract).
        assert is_mutable_pointer("publish", pointer) is False
        assert is_mutable_pointer("publish", pointer, legacy) is False
        assert is_mutable_pointer("publish", pointer, skill) is True
        record_pointer = ("publish_staging", *pointer)
        assert is_mutable_pointer("record", record_pointer) is False
        assert is_mutable_pointer(
            "record", record_pointer, {"publish_staging": legacy}
        ) is False
        assert is_mutable_pointer(
            "record", record_pointer, {"publish_staging": skill}
        ) is True


def test_legacy_cover_request_path_change_is_rejected_by_both_guards() -> None:
    legacy = _publish("images.edit")
    tampered = copy.deepcopy(legacy)
    tampered["cover_generation"]["request_path"] = f"{DEST_PACKAGE}/evidence/request.json"

    with pytest.raises(PackageImportError, match="frozen evidence"):
        package_import._guard_immutable(
            "publish", legacy, tampered, source_workspace_root="/home/producer"
        )
    assert not package_relocation._allowed_changed_pointer(
        "publish", ("cover_generation", "request_path"), legacy
    )
    assert not package_relocation._allowed_changed_pointer(
        "publish", ("cover_generation", "request_path")
    )


def test_legacy_cover_request_path_is_not_rewritten_by_relocation() -> None:
    legacy = _publish("images.edit")
    with pytest.raises(PackageRelocationError, match="unknown external-host path"):
        # The frozen provenance keeps its producer path; a stray producer path
        # outside the frozen subtree still fails closed.
        broken = copy.deepcopy(legacy)
        broken["stray"] = f"{SOURCE_PACKAGE}/x"
        package_relocation._transform_publish(
            broken, mappings=MAPPINGS, source_workspace_root="/home/producer"
        )
    result = package_relocation._transform_publish(
        legacy, mappings=MAPPINGS, source_workspace_root="/home/producer"
    )
    assert result["video_path"] == f"{DEST_PACKAGE}/c.mp4"
    assert result["cover_generation"]["final_cover"] == f"{DEST_PACKAGE}/covers/c.cover.png"
    assert result["cover_generation"]["request_path"] == legacy["cover_generation"]["request_path"]
    assert result["cover_generation"]["response_path"] == legacy["cover_generation"]["response_path"]


def test_skill_candidate_cover_locators_relocate_inside_the_package() -> None:
    skill = _publish("skill.candidates")
    result = package_relocation._transform_publish(
        skill, mappings=MAPPINGS, source_workspace_root="/home/producer"
    )
    generation = result["cover_generation"]
    assert generation["request_path"] == f"{DEST_PACKAGE}/evidence/request.json"
    assert generation["response_path"] == f"{DEST_PACKAGE}/evidence/response.json"
    assert generation["candidate_selection"]["choose_path"] == (
        f"{DEST_PACKAGE}/c.title-cover/choose.json"
    )
    package_import._guard_immutable(
        "publish", skill, result, source_workspace_root="/home/producer"
    )
