from __future__ import annotations

import copy
import hashlib
import json
import runpy
from pathlib import Path

import pytest

from src.autoslice import host_only_identity_card_successor as adapter
from src.autoslice import host_only_identity_card_trial as trial
from src.autoslice import host_only_v4_successor as v4
from src.autoslice.cover_host_identity_gate import (
    _comparison_path,
    validate_final_host_identity_verification,
    verify_final_host_identity,
)
from src.autoslice.host_only_v4_package_binding import validate_package_binding

HELPERS = runpy.run_path(str(Path(__file__).with_name("test_host_only_identity_card_trial.py")))


def _json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _hash(p) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def ready_input(tmp_path, monkeypatch):
    """Synthetic media/vision, real trial rendering and native V4 byte binding."""
    f = HELPERS["_write_package"](tmp_path)
    HELPERS["_patch_current_contract"](monkeypatch)
    source = f["source"]
    video = source / "video.mp4"
    video.write_bytes(b"synthetic-test-video-not-real-media")
    (source / "subtitles.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nfixture\n")
    for key in ("evidence", "record"):
        d = json.loads(f[key].read_text())
        d["artifact_hashes"] = {
            "video_sha256": _hash(video),
            "publish_draft_sha256": _hash(f["publish"]),
        }
        _json(f[key], d)
    manifest_path = source / "review_manifest.json"
    m = json.loads(manifest_path.read_text())
    i = m["items"][0]
    i["video"] = video.name
    i["subtitle_srt"] = "subtitles.srt"
    i["sha256"] = {
        "cover": _hash(f["old_cover"])[7:],
        "video": _hash(video)[7:],
        "evidence_json": _hash(f["evidence"])[7:],
        "publish_json": _hash(f["publish"])[7:],
    }
    g = json.loads(f["publish"].read_text())["cover_generation"]
    m["cover_route_attestations"] = [
        {
            "candidate_id": f["candidate"],
            **{
                k: copy.deepcopy(g[k])
                for k in ("route_decision", "final_cover_sha256", "reference_sha256", "method")
            },
        }
    ]
    m["upload_allowed"] = False
    _json(manifest_path, m)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    made = trial.build_host_only_identity_card_trial(
        source_package=source, destination=private / "trial", candidate_id=f["candidate"]
    )
    generation = json.loads(Path(made["outputs"]["generation"]["path"]).read_text())
    from src.autoslice import cpa_frame_witness

    verdict = {
        "source_lidousha_located": True,
        "primary_subject_is_lidousha": True,
        "primary_subject_matches_other_source_participant": False,
        "primary_subject_is_visually_dominant": True,
        "primary_subject_face_is_large_and_clear": True,
        "primary_subject_carries_story_reaction": True,
        "excessive_dead_space": False,
        "meaningless_dominant_decoration": False,
        "thumbnail_has_clear_click_hook": True,
        "identity_conflicts": [],
        "composition_conflicts": [],
        "reason": "Synthetic fixture only, not a real image judgment.",
        "other_recognizable_people_or_avatars_visible": False,
        "other_recognizable_people_or_avatars": [],
    }
    monkeypatch.setattr(cpa_frame_witness, "image_vision_probe", HELPERS["_probe"](verdict))
    final = Path(made["outputs"]["cover"]["path"])
    receipt = verify_final_host_identity(
        final_cover_path=final,
        final_cover_sha256=_hash(final),
        reference_path=Path(made["source_inputs"]["reference"]["path"]),
        host_only_required=True,
    )
    assert receipt["status"] == "PASS"
    witness = private / "witness.json"
    _json(witness, receipt)
    # Fixture StoryContract/route is deliberately minimal. Keep the actual V4
    # validator and package byte binding live, stub only the unrelated routing.
    monkeypatch.setattr(adapter, "validate_cover_route_decision", lambda *a, **k: True)
    monkeypatch.setattr(v4, "validate_cover_route_decision", lambda *a, **k: True)
    monkeypatch.setattr(
        v4, "host_only_identity_route_blocker_detail", lambda g: "missing current witness"
    )
    run = private / "successor"
    run.mkdir(mode=0o700)
    return {
        "candidate": f["candidate"],
        "source": source,
        "destination": run / "package",
        "trial_result": private / "trial/TRIAL-RESULT.json",
        "witness": witness,
        "comparison": _comparison_path(final),
        "trial": made,
        "generation": generation,
    }


def _checked_audit(root: Path) -> dict:
    """Unit-test callback is not a canonical video audit."""
    m = json.loads((root / "review_manifest.json").read_text())
    i = m["items"][0]
    publish = json.loads((root / i["publish_json"]).read_text())
    g = publish["cover_generation"]
    assert validate_final_host_identity_verification(g)
    validate_package_binding(root=root, item=i, generation=g)
    for key in ("evidence_json", "record"):
        document = json.loads((root / i[key]).read_text())
        assert document["publish_staging"]["cover_generation"] == g
        assert document["artifact_hashes"]["publish_draft_sha256"] == _hash(
            root / i["publish_json"]
        )
    assert (root / i["video"]).read_bytes() == b"synthetic-test-video-not-real-media"
    return {
        "root": str(root),
        "passed": True,
        "issues": [],
        "issue_count": 0,
        "blocking_issue_count": 0,
    }


def _run(f: dict, auditor=_checked_audit):
    return adapter.build_identity_card_successor(
        source_package=f["source"],
        destination_package=f["destination"],
        candidate_id=f["candidate"],
        trial_result=f["trial_result"],
        witness_receipt=f["witness"],
        comparison_image=f["comparison"],
        audit_package=auditor,
    )


def test_installs_existing_pixels_and_preserves_source_media(ready_input):
    f = ready_input
    before = _inventory(f["source"])
    result = _run(f)
    assert result["status"] == "PASS"
    assert result["source_preimage_unchanged"]
    assert result["original_cover_pixels_changed"]
    assert result["installed_existing_pixels"]
    assert result["video_pixels_changed"] is False
    assert result["provider_calls"] == result["upload_calls"] == 0
    assert result["upload_allowed"] is False
    assert _inventory(f["source"]) == before
    m = json.loads((f["destination"] / "review_manifest.json").read_text())
    assert (
        _hash(f["destination"] / m["items"][0]["cover"]) == f["trial"]["outputs"]["cover"]["sha256"]
    )


@pytest.mark.parametrize(
    "drift", ["candidate", "cover", "witness", "reference", "record", "generation"]
)
def test_rejects_unbound_or_changed_inputs(ready_input, drift):
    f = ready_input
    if drift == "candidate":
        f["candidate"] = "another-candidate"
    elif drift == "cover":
        Path(f["trial"]["outputs"]["cover"]["path"]).write_bytes(b"changed pixels")
    elif drift == "witness":
        d = json.loads(f["witness"].read_text())
        d["status"] = "FAIL"
        _json(f["witness"], d)
    elif drift == "reference":
        d = json.loads(f["witness"].read_text())
        d["reference_sha256"] = "sha256:" + "0" * 64
        _json(f["witness"], d)
    elif drift == "record":
        m = json.loads((f["source"] / "review_manifest.json").read_text())
        p = f["source"] / m["items"][0]["record"]
        d = json.loads(p.read_text())
        d["extra"] = "external change"
        _json(p, d)
    else:
        gpath = Path(f["trial"]["outputs"]["generation"]["path"])
        d = json.loads(gpath.read_text())
        d["cover_text"] = "unapproved text"
        _json(gpath, d)
        result = json.loads(f["trial_result"].read_text())
        result["outputs"]["generation"].update(sha256=_hash(gpath), bytes=gpath.stat().st_size)
        _json(f["trial_result"], result)
    before = _inventory(f["source"])
    with pytest.raises(ValueError):
        _run(f)
    assert not f["destination"].exists()
    assert _inventory(f["source"]) == before
    assert not (f["destination"].parent / "IDENTITY-CARD-SUCCESSOR.json").exists()


def test_canonical_audit_failure_is_not_reported_as_success(ready_input):
    f = ready_input

    def bad_audit(root):
        return {
            "root": str(root),
            "passed": False,
            "issues": [{"code": "FIXTURE_BLOCK"}],
            "issue_count": 1,
            "blocking_issue_count": 1,
        }

    before = _inventory(f["source"])
    with pytest.raises(ValueError, match="audit did not pass"):
        _run(f, bad_audit)
    assert _inventory(f["source"]) == before
    assert (f["destination"] / "package_audit.json").is_file()
    assert not (f["destination"].parent / "IDENTITY-CARD-SUCCESSOR.json").exists()


def test_only_cover_and_bound_document_surfaces_change(ready_input):
    f = ready_input
    source = f["source"]
    original_manifest = json.loads((source / "review_manifest.json").read_text())
    item = original_manifest["items"][0]
    allowed = {
        "review_manifest.json",
        "package_audit.json",
        item["cover"],
        item["evidence_json"],
        item["record"],
        item["publish_json"],
    }
    original = {
        p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()
    }
    _run(f)
    for relative, raw in original.items():
        if relative not in allowed:
            assert (f["destination"] / relative).read_bytes() == raw, relative
    for key in ("evidence_json", "record", "publish_json"):
        before = json.loads(original[item[key]])
        after = json.loads((f["destination"] / item[key]).read_text())
        if key != "publish_json":
            before.pop("artifact_hashes", None)
            after.pop("artifact_hashes", None)
            for document in (before, after):
                document["publish_staging"].pop("cover_generation", None)
                document["publish_staging"].pop("cover_path", None)
        else:
            for document in (before, after):
                for name in ("cover_generation", "cover_path", "artifact_hashes"):
                    document.pop(name, None)
        assert before == after
    final_generation = json.loads((f["destination"] / item["publish_json"]).read_text())[
        "cover_generation"
    ]
    assert (
        final_generation["source_composition_verification"]
        == f["generation"]["source_composition_verification"]
    )
    assert final_generation["story_contract"] == f["generation"]["story_contract"]
    assert final_generation["art_direction"] == f["generation"]["art_direction"]


@pytest.mark.parametrize(
    "unsafe", ["witness_symlink", "comparison_symlink", "destination_exists", "parent_public"]
)
def test_unsafe_paths_fail_before_package_write(ready_input, unsafe):
    f = ready_input
    if unsafe.endswith("symlink"):
        name = "witness" if unsafe.startswith("witness") else "comparison"
        link = f[name].with_name("linked-" + f[name].name)
        link.symlink_to(f[name])
        f[name] = link
    elif unsafe == "destination_exists":
        f["destination"].mkdir()
    else:
        f["destination"].parent.chmod(0o755)
    before = _inventory(f["source"])
    with pytest.raises(ValueError):
        _run(f)
    assert _inventory(f["source"]) == before
    assert not (f["destination"].parent / "ORIGINAL-PACKAGE-PREIMAGE.json").exists()


def test_recomposition_mismatch_cannot_be_accepted(ready_input, monkeypatch):
    f = ready_input
    original = trial.build_host_only_identity_card_trial

    def mismatched_verification(**kwargs):
        value = original(**kwargs)
        value["outputs"]["base"]["sha256"] = "sha256:" + "0" * 64
        return value

    monkeypatch.setattr(trial, "build_host_only_identity_card_trial", mismatched_verification)
    with pytest.raises(ValueError, match="recomposition differs"):
        _run(f)
    assert not f["destination"].exists()


@pytest.mark.parametrize(
    "route,expected",
    [
        (
            {"actual_treatment": "screenshot_direct", "execution_status": "READY_DEGRADED"},
            "READY_DEGRADED",
        ),
        ({"actual_treatment": "screenshot_direct", "execution_status": "READY"}, "READY"),
        ({"actual_treatment": "screenshot_polish", "execution_status": "READY_DEGRADED"}, "READY"),
        ({"actual_treatment": "screenshot_direct", "execution_status": "BLOCKED"}, "READY"),
    ],
)
def test_existing_explicit_direct_degradation_is_preserved(route, expected):
    assert adapter._existing_direct_execution_status({"route_decision": route}) == expected


def test_degraded_status_does_not_waive_native_route_evidence(ready_input, monkeypatch):
    f = ready_input
    observed = []

    def reject_route(generation, **kwargs):
        observed.append(generation["route_decision"]["execution_status"])
        return False

    monkeypatch.setattr(adapter, "validate_cover_route_decision", reject_route)
    with pytest.raises(ValueError, match="route does not replay"):
        _run(f)
    assert observed
    assert not f["destination"].exists()
