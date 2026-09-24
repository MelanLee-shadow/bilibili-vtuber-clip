from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from src.autoslice.builtin_imagegen_cover import METHOD, PROVIDER, SCHEMA
from src.autoslice.package_import import PackageImportError
from src.autoslice.package_import_builtin_imagegen import (
    project_builtin_imagegen_locators,
)
from src.autoslice.package_relocation_contract import project_uniform_host_locators


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package(tmp_path: Path) -> tuple[Path, str, dict, dict]:
    candidate = "auto_builtin_fixture"
    package = tmp_path / "physical" / candidate / "replacement_recuts"
    source = package / "builtin_imagegen_source"
    source.mkdir(parents=True)

    def asset(name: str, value: str | Image.Image) -> dict[str, str]:
        path = source / name
        if isinstance(value, Image.Image):
            value.save(path)
        else:
            path.write_text(value, encoding="utf-8")
        return {"path": name, "sha256": _sha(path)}

    reference = asset("reference.png", Image.new("RGB", (32, 18), "blue"))
    raw = asset("raw.png", Image.new("RGBA", (32, 18), "red"))
    with Image.open(source / raw["path"]) as image:
        background = asset(
            "background.png",
            image.resize((1920, 1080), Image.Resampling.LANCZOS),
        )
    final = asset("final.png", Image.new("RGBA", (1920, 1080), "green"))
    call_id = "call_fixture"
    generated_path = "/generated/fixture-owner/raw.png"
    invocation = asset(
        "invocation.json",
        json.dumps(
            {
                "payload": {
                    "call_id": call_id,
                    "input": 'await tools.image_gen__imagegen({prompt:"draw"})',
                }
            }
        ),
    )
    result = asset(
        "result.json",
        json.dumps(
            {
                "payload": {
                    "output": [
                        {"type": "input_text", "text": generated_path},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,"
                            + base64.b64encode((source / raw["path"]).read_bytes()).decode(),
                        },
                    ]
                }
            }
        ),
    )
    prompt = asset("prompt.txt", "draw")
    source_document = {
        "schema_version": SCHEMA,
        "provider": PROVIDER,
        "method": METHOD,
        "model": None,
        "owner_task_id": "fixture-owner",
        "candidate_id": candidate,
        "calls": [
            {
                "call_id": call_id,
                "generated_path": generated_path,
                "invocation": invocation,
                "result": result,
                "prompt": prompt,
                "output": raw,
                "references": [reference],
            }
        ],
        "identity_reference": reference,
        "background": background,
        "final_cover": final,
    }
    provenance = source / "source.json"
    provenance.write_text(json.dumps(source_document), encoding="utf-8")
    cover = package / "cover.png"
    route_background = package / "route-background.png"
    shutil.copyfile(source / final["path"], cover)
    shutil.copyfile(source / background["path"], route_background)
    (package / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "candidate_id": candidate,
                        "cover_builtin_provenance": "builtin_imagegen_source/source.json",
                        "cover": "cover.png",
                        "cover_route_background": "route-background.png",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    declared = Path("/producer/out/2026-08-20") / candidate / "replacement_recuts"
    generation = {
        "provider": PROVIDER,
        "method": METHOD,
        "image_gen_model": PROVIDER,
        "model": None,
        "model_disclosed": False,
        "attempted_models": [],
        "fallback_used": False,
        "candidate_id": candidate,
        "builtin_imagegen_provenance_path": "/producer/transient/source.json",
        "builtin_imagegen_provenance_sha256": _sha(provenance),
        "reference_image": "/producer/transient/reference.png",
        "reference_sha256": reference["sha256"],
        "ai_background": "/producer/transient/background.png",
        "ai_background_sha256": background["sha256"],
        "final_cover": "/producer/transient/final.png",
        "final_cover_sha256": final["sha256"],
    }
    publish = {
        "candidate_id": candidate,
        "cover_path": generation["final_cover"],
        "cover_generation": generation,
    }
    record = {
        "subtitle_path": str(declared / f"{candidate}.recut.srt"),
        "publish_staging": copy.deepcopy(publish),
    }
    return package, candidate, record, publish


def test_projects_contained_builtin_source_then_relocates(tmp_path: Path) -> None:
    package, candidate, record, publish = _package(tmp_path)
    projected_record, projected_publish = project_builtin_imagegen_locators(
        record,
        publish,
        package_root=package,
        candidate_id=candidate,
    )
    source_package = f"/producer/out/2026-08-20/{candidate}/replacement_recuts"
    destination_package = f"/destination/out/2026-08-20/{candidate}/replacement_recuts"
    generation = projected_publish["cover_generation"]
    assert generation["builtin_imagegen_provenance_path"] == (
        source_package + "/builtin_imagegen_source/source.json"
    )
    assert generation["reference_image"] == (
        source_package + "/builtin_imagegen_source/reference.png"
    )
    assert generation["ai_background"] == source_package + "/route-background.png"
    assert generation["final_cover"] == source_package + "/cover.png"
    assert record != projected_record
    assert publish != projected_publish

    mappings = (
        (source_package, destination_package),
        (
            f"/producer/out/2026-08-20/{candidate}",
            f"/destination/out/2026-08-20/{candidate}",
        ),
        ("/producer/repo", "/destination/repo"),
    )
    relocated_publish = project_uniform_host_locators(
        projected_publish,
        kind="publish",
        mappings=mappings,
        source_workspace_root="/producer",
    )
    relocated_record = project_uniform_host_locators(
        projected_record,
        kind="record",
        mappings=mappings,
        source_workspace_root="/producer",
    )
    assert (
        relocated_publish["cover_generation"]["builtin_imagegen_provenance_path"]
        == destination_package + "/builtin_imagegen_source/source.json"
    )
    assert (
        relocated_record["publish_staging"]["cover_generation"]["builtin_imagegen_provenance_path"]
        == destination_package + "/builtin_imagegen_source/source.json"
    )


def test_rejects_changed_package_provenance(tmp_path: Path) -> None:
    package, candidate, record, publish = _package(tmp_path)
    (package / "builtin_imagegen_source/prompt.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(PackageImportError) as caught:
        project_builtin_imagegen_locators(
            record,
            publish,
            package_root=package,
            candidate_id=candidate,
        )
    assert caught.value.code == "BUILTIN_IMAGEGEN_LOCATOR_INCONSISTENT"


def test_v4_projection_runs_before_builtin_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.autoslice import package_import_builtin_imagegen as builtin
    from src.autoslice import package_import_cover as cover
    from src.autoslice import package_import_v4_cover as v4
    from src.autoslice.cover_host_identity_gate import HOST_ONLY_SCHEMA_VERSION

    calls: list[str] = []
    generation = {
        "method": METHOD,
        "final_host_identity_verification": {
            "schema_version": HOST_ONLY_SCHEMA_VERSION,
        },
    }
    record = {"publish_staging": {"cover_generation": copy.deepcopy(generation)}}
    publish = {"cover_generation": copy.deepcopy(generation)}

    def fake_v4(record_doc, publish_doc, **_kwargs):
        calls.append("v4")
        after_record = copy.deepcopy(record_doc)
        after_publish = copy.deepcopy(publish_doc)
        after_publish["cover_generation"]["v4_projected"] = True
        after_record["publish_staging"]["cover_generation"]["v4_projected"] = True
        return after_record, after_publish

    def fake_builtin(record_doc, publish_doc, **_kwargs):
        calls.append("builtin")
        assert publish_doc["cover_generation"]["v4_projected"] is True
        assert record_doc["publish_staging"]["cover_generation"]["v4_projected"] is True
        return record_doc, publish_doc

    monkeypatch.setattr(v4, "project_v4_cover_locators", fake_v4)
    monkeypatch.setattr(builtin, "project_builtin_imagegen_locators", fake_builtin)

    after_record, after_publish = cover.project_preserved_cover_locators(
        record,
        publish,
        package_root=Path("/unused"),
        candidate_id="auto_fixture",
    )
    assert calls == ["v4", "builtin"]
    assert after_publish["cover_generation"]["v4_projected"] is True
    assert after_record["publish_staging"]["cover_generation"]["v4_projected"] is True
