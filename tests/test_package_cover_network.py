import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.autoslice.package_cover_network import source_cover_network, validate_package_cover_network
from src.autoslice.publish_staging_paths import current_editorial_binding


def sha(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "covers").mkdir()
    (source / "evidence").mkdir()
    package = tmp_path / "package"
    package.mkdir()
    cid = "semanticsong_1_2"
    title = "【李豆沙】豆沙歌，《历历万乡》"
    cover = source / "covers" / f"{cid}.cover.png"
    cover.write_bytes(b"actual-cover")
    subtitle = source / "final.srt"
    subtitle.write_bytes(b"final lyrics")
    gen = {"final_cover": str(cover), "final_cover_sha256": sha(cover)}
    for role in ("request", "response"):
        p = source / "evidence" / f"{cid}.{role}.json"
        p.write_text(json.dumps({"original": role}))
        gen.update({role + "_path": str(p), role + "_sha256": sha(p)})
    record = {
        "candidate_id": cid,
        "delivery_candidate_id": cid,
        "source_candidate_id": cid,
        "subtitle_path": str(subtitle),
    }
    staging = {
        "candidate_id": cid,
        "title": title,
        "cover_status": "AI_COVER_READY",
        "cover_path": str(cover),
        "publish_json_path": str(source / "publish.json"),
        "cover_generation": gen,
        "upload_enabled": False,
    }
    staging["current_binding"] = current_editorial_binding(record, staging, candidate_id=cid)
    gen["input_binding"] = {
        k: v for k, v in staging["current_binding"].items() if k != "cover_sha256"
    }
    record["publish_staging"] = staging
    publish = {
        **copy.deepcopy(staging),
        "schema_version": "shadow-publish-draft.v1",
        "subtitle_sha256": sha(subtitle),
    }
    (source / "publish.json").write_text(json.dumps(publish))
    item = {
        "candidate_id": cid,
        "id": cid,
        "classification": "Song",
        "kind": "song",
        "stem": cid,
        "title": title,
        "video": cid + ".mp4",
        "record": cid + ".record.json",
        "evidence_json": cid + ".record.json",
        "publish_json": cid + ".publish.json",
        "cover": cid + ".cover.png",
        "subtitle_srt": cid + ".srt",
    }
    for key, origin in [("cover", cover), ("subtitle_srt", subtitle)]:
        shutil.copyfile(origin, package / item[key])
    for key, value in [("record", record), ("publish_json", publish)]:
        (package / item[key]).write_text(json.dumps(value))
    (package / item["video"]).write_bytes(b"video")
    for role in ("request", "response"):
        item["cover_" + role] = cid + ".cover-" + role + ".json"
        shutil.copyfile(gen[role + "_path"], package / item["cover_" + role])
    (package / "review_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "lidousha-song-review-manifest.v1",
                "story_contract_required": False,
                "items": [item],
            }
        )
    )
    return source, package, record, publish, item, subtitle, cover


def test_same_bound_closure_reaches_real_joint_qc_resolver(tmp_path):
    source, package, record, publish, item, subtitle, cover = fixture(tmp_path)
    sources = source_cover_network(record, publish, subtitle_path=subtitle, cover_path=cover)
    assert set(sources) == {"request", "response"}
    before = {p: p.read_bytes() for p in package.iterdir()}
    from scripts.run_title_cover_joint_qc import resolve_package_inputs

    for _ in range(2):
        validate_package_cover_network(
            package,
            item,
            record,
            publish,
            subtitle_path=package / item["subtitle_srt"],
            cover_path=package / item["cover"],
        )
        assert resolve_package_inputs(package, item["title"])[2] == package / item["cover"]
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("mutation", ["missing", "hash", "candidate", "cover", "symlink", "escape"])
def test_source_closure_refuses_missing_or_unbound_originals(tmp_path, mutation):
    source, package, record, publish, item, subtitle, cover = fixture(tmp_path)
    gen = record["publish_staging"]["cover_generation"]
    request = Path(gen["request_path"])
    if mutation == "missing":
        request.unlink()
    elif mutation == "hash":
        request.write_text("changed")
    elif mutation == "candidate":
        record["candidate_id"] = "other"
    elif mutation == "cover":
        cover.write_bytes(b"changed")
    elif mutation == "symlink":
        original = request.read_bytes()
        request.unlink()
        target = tmp_path / "other.json"
        target.write_bytes(original)
        request.symlink_to(target)
    else:
        target = tmp_path / "foreign.json"
        target.write_bytes(request.read_bytes())
        gen["request_path"] = str(target)
        publish["cover_generation"] = copy.deepcopy(gen)
        (source / "publish.json").write_text(json.dumps(publish))
    with pytest.raises((OSError, ValueError)):
        source_cover_network(record, publish, subtitle_path=subtitle, cover_path=cover)


@pytest.mark.parametrize(
    "mutation", ["missing", "hash", "candidate", "cover", "symlink", "escape", "item_hash"]
)
def test_auditor_and_joint_qc_both_reject_before_provider(tmp_path, mutation):
    _, package, record, publish, item, _, _ = fixture(tmp_path)
    request = package / item["cover_request"]
    if mutation == "missing":
        request.unlink()
    elif mutation == "hash":
        request.write_bytes(b"tampered")
    elif mutation == "candidate":
        record["candidate_id"] = "other"
        (package / item["record"]).write_text(json.dumps(record))
    elif mutation == "cover":
        (package / item["cover"]).write_bytes(b"other-cover")
    elif mutation == "symlink":
        request.unlink()
        request.symlink_to(package / item["cover_response"])
    elif mutation == "item_hash":
        item["sha256"] = {"cover_request": "b" * 64}
        (package / "review_manifest.json").write_text(json.dumps({"items": [item]}))
    else:
        item["cover_request"] = "../source/evidence/semanticsong_1_2.request.json"
        (package / "review_manifest.json").write_text(json.dumps({"items": [item]}))
    from scripts.audit_review_package import audit_package
    from scripts.run_title_cover_joint_qc import run_qc

    result = audit_package(package)
    assert "COVER_NETWORK_INPUT_CLOSURE_INVALID" in {issue["code"] for issue in result["issues"]}
    calls = []
    with pytest.raises((OSError, ValueError)):
        run_qc(
            package,
            item["title"],
            package / "never-qc.json",
            image_probe=lambda *_: calls.append(True),
        )
    assert not calls and not (package / "never-qc.json").exists()
