"""Scoring of cover candidates by route and the contact sheet they are compared on.

Split out of title_cover_candidates so the lane scores and sheets candidates without loading the validation and
witness machinery of that module; title_cover_candidates re-exports the names for its existing importers."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps  # type: ignore[import-not-found, unused-ignore]

ROUTES = ("screenshot", "screenshot_collage", "emote", "cutout", "ai_sticker", "ai_scene")

REALISM = dict(zip(ROUTES, (2, 2, 2, 1, 0, 0)))

AXES = ("people", "hook_fit", "clean_layers", "identity", "source_faithful", "no_fabrication")

SAFE_FLAGS = ("clean_layers", "identity", "source_faithful", "no_fabrication")


def strict_file(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("COVER_CHOICE_PATH_INVALID")
    if ".." in Path(relative).parts:
        raise ValueError("COVER_CHOICE_PATH_INVALID")
    candidate = root / relative
    if any(p.is_symlink() for p in (candidate, *candidate.parents)):
        raise ValueError("COVER_CHOICE_SYMLINK")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)) or not resolved.is_file():
        raise ValueError("COVER_CHOICE_PATH_ESCAPE")
    return resolved


def score_candidate(row: Mapping[Any, Any], *, participants: list[str], recent: Mapping[Any, Any]) -> dict[Any, Any]:
    """Skill choose_cover formula: T saturates at 200; C breaks score ties."""
    vetoes = []
    route = row.get("route")
    if route not in ROUTES:
        raise ValueError("COVER_CANDIDATE_ROUTE_INVALID")
    verdict = row.get("verdict")
    if not isinstance(verdict, Mapping):
        return {"status": "VETO", "vetoes": ["CPA_IMAGE_VERDICT_MISSING"]}
    for key in AXES:
        axis = verdict.get(key)
        if (
            not isinstance(axis, Mapping)
            or not isinstance(axis.get("reason"), str)
            or not axis["reason"].strip()
        ):
            vetoes.append("CPA_REASON_MISSING:" + key)
    for key in SAFE_FLAGS:
        if not isinstance(verdict.get(key), Mapping) or verdict[key].get("pass") is not True:
            vetoes.append("CPA_VETO:" + key)
    if verdict.get("people", {}).get("expected") != participants:
        vetoes.append("STORY_PARTICIPANTS_DISAGREE")
    present = verdict.get("people", {}).get("present")
    if (
        not isinstance(present, list)
        or not all(isinstance(p, str) for p in present)
        or len(set(present)) != len(present)
    ):
        vetoes.append("CPA_PEOPLE_INVALID")
        present = []
    if set(present) - set(participants):
        vetoes.append("UNSOURCED_PARTICIPANT")
    if "李豆沙" not in present:
        vetoes.append("HOST_MISSING")
    face = verdict.get("face")
    if not isinstance(face, list) or len(face) != 4 or any(type(v) is not int for v in face):
        vetoes.append("CPA_FACE_BOX_INVALID")
    elif not (0 <= face[0] < face[2] <= 1920 and 0 <= face[1] < face[3] <= 1080):
        vetoes.append("CPA_FACE_BOX_INVALID")
    elif face[3] - face[1] < 270:
        vetoes.append("MAIN_FACE_BELOW_25_PERCENT")
    keep = verdict.get("keep", [])
    if not isinstance(keep, list):
        vetoes.append("CPA_KEEP_BOX_INVALID")
        keep = []
    protected = ([face] if "CPA_FACE_BOX_INVALID" not in vetoes else []) + keep
    measured = (row.get("render") or {}).get("measured_lines", [])
    for box in protected:
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(v) is not int for v in box)
            or not (0 <= box[0] < box[2] <= 1920 and 0 <= box[1] < box[3] <= 1080)
        ):
            vetoes.append("CPA_KEEP_BOX_INVALID")
            continue
        for line in measured:
            if max(line["x"], box[0]) < min(line["x"] + line["w"], box[2]) and max(
                line["y"], box[1]
            ) < min(line["y"] + line["h"], box[3]):
                vetoes.append("FINAL_TEXT_OVERLAPS_OBSERVED_FACE_OR_IDENTITY")
    size = row.get("punch_font_size")
    if type(size) is not int or size < 120:
        vetoes.append("PUNCH_BELOW_120")
        size = 0
    hook = verdict.get("hook_fit", {}).get("score")
    if type(hook) is not int or hook not in (0, 1, 2):
        vetoes.append("CPA_HOOK_SCORE_INVALID")
        hook = 0
    emote = row.get("main_emote")
    last_five = recent.get("last_five_main_emotes", [])
    if emote and (emote in last_five or emote in recent.get("source_session_main_emotes", [])):
        vetoes.append("MAIN_EMOTE_REPEATED")
    people = 2 if set(participants) <= set(present) else 1 if "李豆沙" in present else 0
    typography = 2 if size >= 200 else 1 if size >= 150 else 0
    fresh = int(route != recent.get("previous_route") and (not emote or emote not in last_five))
    realism = REALISM[route]
    return {
        "status": "VETO" if vetoes else "ELIGIBLE",
        "vetoes": vetoes,
        "P": people,
        "T": typography,
        "H": hook,
        "C": realism,
        "V": fresh,
        "total": 3 * people + 2 * typography + hook + realism + fresh,
    }


def contact_sheet(root: Path, rows: list[dict[Any, Any]], selected: str) -> Path:
    from PIL import ImageDraw  # type: ignore[import-not-found, unused-ignore]

    sheet = Image.new("RGB", (960, 320 * ((len(rows) + 1) // 2)), "#20202a")
    draw = ImageDraw.Draw(sheet)
    for i, row in enumerate(rows):
        x, y = (i % 2) * 480, (i // 2) * 320
        if row.get("status") == "RENDERED":
            with Image.open(strict_file(root, row["final_path"])) as src:
                sheet.paste(ImageOps.fit(src.convert("RGB"), (480, 270)), (x, y))
        label = f"{row['id']} {row['route']} {row['score'].get('total', '-')} {'SELECTED' if row['id'] == selected else row['score']['status']}"
        draw.text((x + 8, y + 280), label, fill="white")
    target = root / "candidates.jpg"
    sheet.save(target, quality=95)
    return target
