"""An imported cover: the finished cover the channel owner (or a hand push) decided, taken into step 6 as it is.

`import-cover` takes a push directory `cover/cover.png` (1920x1080 PNG) + `cover/cover.json`
(`{"lines": [...], "decided_by": "owner" | "push", "authority": "<basis>", "note": ""}`), keeps both under
clips/<cid>/import_cover/ and records the import in state["imported_cover"]; step 6 and every later step are forgotten so the
next run produces them from it.  Step 6 (step6_title.step6_title_cover) then does its title half as ever and, for the cover,
only writes the imported picture and lines to the places the generated cover has (cover/cover.png, cover/cand/choose.json,
title/candidates.json): nothing draws, looks at or replaces it, and step 9's objections to it are recorded and kept
(review_owner, dissent_register).  The cover follows the manuscript: every successor clip (`revise`, `trim`; any generation) takes the record
and its files over when it is made (`carry`), and an import into the successor replaces them.  The checks of the
finished-media lane (canvas, font, cover words against the subtitles) run on it as on any cover."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections.abc import MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from .runner import Ctx, StepOutcome
from .state import ClipState, StateError, utc_now
from .step_io import _local

if TYPE_CHECKING:
    from src.autoslice.cpa_gemini_client import JsonValue

IMPORT_DIR = "import_cover"
DECIDED_BY = ("owner", "push")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# what a new cover makes obsolete: step 6 and everything built on it (the subtitles and the burned video stay)
REDO = ("title_cover", "tags", "package", "review", "upload")


class CoverRecord(TypedDict):
    source: str
    files: dict[str, str]
    png_sha256: str
    lines: list[str]
    decided_by: str
    authority: str
    note: str
    at: str


def imported_cover(ctx: Ctx) -> CoverRecord | None:
    """The record `import-cover` made (state["imported_cover"]), or None."""
    return ctx.state.data.get("imported_cover")


def _read_push(src: Path) -> tuple[Path, dict[str, object]]:
    from PIL import Image

    from src.autoslice.cover_generation import _COVER_CANVAS

    png, meta_file = src / "cover" / "cover.png", src / "cover" / "cover.json"
    for path in (png, meta_file):
        if not path.is_file():
            raise StateError(f"{path} is missing")
    if png.read_bytes()[:8] != PNG_MAGIC:
        raise StateError(f"{png} is not a PNG")
    with Image.open(png) as image:
        if tuple(image.size) != tuple(_COVER_CANVAS):
            raise StateError(f"{png} is {image.size[0]}x{image.size[1]}, a cover is {_COVER_CANVAS[0]}x{_COVER_CANVAS[1]}")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    lines = meta.get("lines") if isinstance(meta, dict) else None
    if not (isinstance(lines, list) and lines and all(isinstance(x, str) and x.strip() for x in lines)):
        raise StateError(f"{meta_file}: lines must be a non-empty list of non-empty strings")
    return png, meta


def import_cover(state: ClipState, clip_dir: Path, src: Path, *, decided_by: str, authority: str) -> CoverRecord:
    """`import-cover`: validate the push, keep it in clips/<cid>/import_cover/, record it and forget step 6 and later steps
    (and the review's state).  `decided_by` and `authority` are the command's; a push file that names others must agree."""
    if decided_by not in DECIDED_BY:
        raise StateError(f"decided_by must be one of {DECIDED_BY}, not {decided_by!r}")
    if not authority.strip():
        raise StateError("import-cover needs a non-empty authority")
    png, meta = _read_push(src)
    for key, given in (("decided_by", decided_by), ("authority", authority)):
        if key in meta and meta[key] != given:
            raise StateError(f"{src / 'cover' / 'cover.json'}: {key} {meta[key]!r} is not the command's {given!r}")
    dest = clip_dir / IMPORT_DIR
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(png, dest / "cover.png")
    lines = [str(x).strip() for x in cast("list[str]", meta["lines"])]
    (dest / "cover.json").write_text(json.dumps({"lines": lines}, ensure_ascii=False, indent=2), encoding="utf-8")
    rec: CoverRecord = {
        "source": str(src), "files": {"cover.png": str(dest / "cover.png"), "cover.json": str(dest / "cover.json")},
        "png_sha256": hashlib.sha256((dest / "cover.png").read_bytes()).hexdigest(), "lines": lines,
        "decided_by": decided_by, "authority": authority, "note": str(meta.get("note") or ""), "at": utc_now()}
    for key in REDO:
        state.data["steps"].pop(key, None)
    for key in ("review", "rewind", "revisions"):
        cast("MutableMapping[str, JsonValue]", state.data).pop(key, None)
    state.data["imported_cover"] = rec
    state.save()
    return rec


def carry(state_x: ClipState, state_y: ClipState, dir_x: Path, dir_y: Path) -> bool:
    """A successor clip Y (clips dir `dir_y`) of X (`dir_x`) takes X's imported cover over: the files are copied into Y's own
    import_cover/ and the record, with its basis and sha256, points at them.  False when X has none.  Y's steps are not touched
    (it has not run step 6); an `import-cover` on Y later replaces the record and the files as on any clip."""
    rec = state_x.data.get("imported_cover")
    if rec is None:
        return False
    shutil.copytree(dir_x / IMPORT_DIR, dir_y / IMPORT_DIR)
    carried = copy.deepcopy(rec)
    carried["files"] = {name: str(dir_y / IMPORT_DIR / Path(path).name) for name, path in rec["files"].items()}
    state_y.data["imported_cover"] = carried
    state_y.save()
    return True


def adopt(ctx: Ctx, rec: CoverRecord, *, title: str, process: list[str], cpa: list) -> StepOutcome:
    """Step 6's cover half for an imported cover: the picture and its lines go where the generated ones go, and the route
    record says they were imported and on whose basis.  No model is asked."""
    picture = Path(rec["files"]["cover.png"])
    if hashlib.sha256(picture.read_bytes()).hexdigest() != rec["png_sha256"]:
        return StepOutcome(False, reason="IMPORTED_COVER_CHANGED", cpa=cpa, process=process,
                           note=f"{picture} no longer matches the sha256 recorded at import; import the cover again")
    cover_path = _local(ctx, "cover/cover.png")
    shutil.copyfile(picture, cover_path)
    choose_path = _local(ctx, "cover/cand/choose.json")
    row = {"id": "imported", "route": "imported", "status": "IMPORTED", "note": f"{rec['decided_by']}: {rec['authority']}"}
    choose_path.write_text(json.dumps({
        "rows": [row], "chosen": "imported", "cover_lines": rec["lines"], "source": "imported",
        "decided_by": rec["decided_by"], "authority": rec["authority"], "png_sha256": rec["png_sha256"],
        "imported_at": rec["at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    cands_path = ctx.clip_dir / "title/candidates.json"
    cands = json.loads(cands_path.read_text(encoding="utf-8"))
    cands["cover_lines"] = rec["lines"]
    cands["cover_route"] = {"method": "imported", "chosen": {"id": "imported", "route": "imported"}, "image_generation_used": False,
                            "decided_by": rec["decided_by"], "authority": rec["authority"], "png_sha256": rec["png_sha256"],
                            "at": rec["at"], "choose": str(choose_path)}
    cands_path.write_text(json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")
    process.append(f"封面采用导入的定稿封面（{rec['decided_by']}：{rec['authority']}）：{' / '.join(rec['lines'])}；"
                   "不出图、不看图、不替换")
    return StepOutcome(True, products={"title": str(ctx.clip_dir / "title/title.txt"), "candidates": str(cands_path),
                                       "cover": str(cover_path), "choose": str(choose_path)},
                       cpa=cpa, note=f"采用标题 {title}；封面 导入（{rec['decided_by']}）", process=process)


def review_slot(ctx: Ctx) -> str:
    """The review prompt's `cover` slot: only a clip with an imported cover offers it (an objection is the one thing the
    review can say about a decided cover); otherwise nothing is added to the prompt."""
    if imported_cover(ctx) is None:
        return ""
    return '"cover": ["你认为封面字应改成的样子；封面是定稿的，只记为异议、不采用"] 或 null, '
