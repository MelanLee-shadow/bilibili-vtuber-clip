"""Package projection of the sealed title/cover candidate evidence."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from collections.abc import Mapping

from .title_cover_candidates import SCHEMA, strict_file, sha, validate_choice_bundle

ITEM_KEY = "cover_choice"


def choice_source_files(generation: Mapping) -> list[tuple[str, Path, str]]:
    if generation.get("method") != "skill.candidates":
        return []
    problems = validate_choice_bundle(generation)
    if problems:
        raise ValueError("COVER_CHOICE_EVIDENCE_INVALID: " + "; ".join(problems))
    root = Path(generation["candidate_selection"]["choose_path"]).parent
    manifest = json.loads((root / "choose.json").read_text())
    return [
        (f"cover_choice_{index:04}", strict_file(root, relative), ".title-cover/" + relative)
        for index, relative in enumerate(["choose.json", *sorted(manifest["artifacts"])])
    ]


def choice_manifest_fields(generation: Mapping, *, package_root: Path) -> dict:
    if generation.get("method") != "skill.candidates":
        return {}
    binding = generation["candidate_selection"]
    if binding.get("schema_version") != SCHEMA:
        raise ValueError("COVER_CHOICE_SCHEMA_INVALID")
    cid = binding["candidate_id"]
    if (
        not isinstance(cid, str)
        or not cid
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in cid
        )
    ):
        raise ValueError("COVER_CHOICE_CANDIDATE_INVALID")
    relative_root = "cover_choices/" + cid
    destination = package_root / relative_root
    # Reuse a complete portable copy only after independent verification.
    if (destination / "choose.json").is_file():
        if validate_choice_bundle(generation, root=destination):
            raise ValueError("COVER_CHOICE_PACKAGE_COPY_DRIFT")
    else:
        portable = []
        for path in package_root.glob("*.title-cover/choose.json"):
            if path.is_symlink():
                raise ValueError("COVER_CHOICE_PACKAGE_SYMLINK")
            payload = json.loads(path.read_text())
            if payload.get("candidate_id") == cid and sha(path) == binding["choose_sha256"]:
                portable.append(path)
        if len(portable) > 1:
            raise ValueError("COVER_CHOICE_PACKAGE_AMBIGUOUS")
        source_generation = dict(generation)
        if portable:
            source_generation["candidate_selection"] = {**binding, "choose_path": str(portable[0])}
        files = choice_source_files(source_generation)
        destination.mkdir(parents=True, exist_ok=False)
        for _, source, suffix in files:
            relative = suffix.removeprefix(".title-cover/")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if validate_choice_bundle(generation, root=destination):
            raise ValueError("COVER_CHOICE_PACKAGE_COPY_DRIFT")
    return {
        ITEM_KEY: relative_root + "/choose.json",
        ITEM_KEY + "_sha256": binding["choose_sha256"],
    }


def package_choice_problems(root: Path, item: Mapping, generation: Mapping) -> list[str]:
    try:
        path = strict_file(root, item[ITEM_KEY])
        if path.name != "choose.json" or sha(path) != item[ITEM_KEY + "_sha256"]:
            raise ValueError("COVER_CHOICE_PACKAGE_BINDING_INVALID")
        if item[ITEM_KEY + "_sha256"] != generation["candidate_selection"]["choose_sha256"]:
            raise ValueError("COVER_CHOICE_PACKAGE_BINDING_DRIFT")
        final_path = strict_file(root, item["cover"])
        problems = validate_choice_bundle(generation, root=path.parent, final_cover=final_path)
        payload = json.loads(path.read_text())
        subtitle = item.get("subtitle_srt") or item.get("subtitle")
        if not subtitle or sha(strict_file(root, subtitle)) != payload["final_srt_sha256"]:
            problems.append("COVER_CHOICE_PACKAGE_FINAL_SRT_DRIFT")
        if (item.get("candidate_id") or item.get("id")) != payload["candidate_id"]:
            problems.append("COVER_CHOICE_PACKAGE_CANDIDATE_DRIFT")
        return problems
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [str(exc)]
