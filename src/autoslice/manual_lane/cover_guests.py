"""Step 6 cover: the guests of a collaboration on the cover (70-cover 「联动嘉宾」; owner on a collaboration
whose covers showed only the host: 联动的其他人也能上封面最好; 11-02, the channel's request).

In a collaboration where each streamer broadcasts from her own room the host's frame never shows the others, and the
frame was the cover's only source of a guest.  The second source is the person asset pack
(assets/manual_lane/person_assets.v1.json: public official pictures, fetched by scripts/fetch_person_assets.py, the
pictures themselves on the media host).  `roster` says, for one clip, which guests the cover must show and where each
one's picture comes from; `roster_vetoes` holds a rendered candidate to it; `line_speakers` says who says each cover
line, so the layout can stand each line by its speaker.

Four sets are kept apart (plan B5.2): `present`, the session's guests (in the call); `mentioned`, people the
clip's subtitles name who are not in the call (never asked on the cover); `needed_on_cover`, the guests this clip's cover
must account for; `actually_visible`, the people the judge sees on the final picture (step6_cover_round.record_cover:
read off the pixels, not the tool's word that it put them there).

Who a pack layer shows is the manifest's name and sha256, never a vision model's guess.  A guest the pack lists but
whose picture the media host cannot read stops the cover (`UNREADABLE`, with her name and the path): until 11-25 such a
guest was silently left off and the cover went out with the host alone.  A guest the pack has no usable picture of is
named as missing, the cover is made without her, never with someone else's picture, and the day's record of pictures
to fetch (`WANTED`) names her."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import text_overlap
from .layout import REPO_ROOT, layout

if TYPE_CHECKING:
    from .runner import Ctx
    from .step6_cover_types import CoverCandidate, CoverPlan

MANIFEST = REPO_ROOT / "assets/manual_lane/person_assets.v1.json"
SCHEMA = "manual-lane-person-assets.v1"
ROSTER_INCOMPLETE = "ROSTER_INCOMPLETE"
GUESTS = "<嘉宾之一>"     # a line said by one of the guests, who are not told apart (cover_pack_guests.GUESTS, the same word)
UNREADABLE = "GUEST_ASSET_UNREADABLE"   # the media host cannot read a listed picture (cover_pack_guests.UNREADABLE)
FACE_UNREAD = "GUEST_FACE_UNREAD"       # her picture is there, her face is found in it by nobody (cover_pack_guests)
FACE_READ_FAILED = "GUEST_FACE_READ_FAILED"   # the reading of her face got no answer (cover_pack_guests): the cover stops
WANTED = "knowledge/person-assets-wanted-{date}.json"   # the day's guests whose picture the pack lacks, by clip


class GuestLayer(TypedDict):
    name: str
    asset_id: str
    kind: str
    src: str                    # the picture on the media host
    sha256: str
    fig: list[int]              # the figure's box in the picture
    face: list[int] | None      # her face in the picture as measured into the manifest; None: read on the media host
    source_url: str
    portrait_options: NotRequired[list[dict[str, object]]]
    recent_asset_ids: NotRequired[list[str]]
    portrait_only: NotRequired[bool]


class CoverRoster(TypedDict):
    present: list[str]          # the session's guests (in the call)
    mentioned: list[str]        # people the clip's subtitles name who are not in the call: never asked on the cover
    needed_on_cover: list[str]  # the session's guests who are part of this clip: the cover must show or name-miss them
    source: str                 # frame | pack | none: where the cover takes them from
    layers: list[GuestLayer]    # source pack: one picture per guest who has one
    missing: list[dict[str, str]]   # guests the cover cannot show, each with why
    required: list[str]         # the guests a candidate must show to be eligible
    line_speakers: list[str | None]
    line: str                   # the PROCESS line


def load_manifest() -> dict[str, object] | None:
    """The pack's manifest; None in a repository that carries none (the public snapshot: export_oss_snapshot strips it,
    and every guest is then named as missing for that reason)."""
    if not MANIFEST.is_file():
        return None
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if doc.get("schema_version") != SCHEMA:
        raise ValueError(f"{MANIFEST}: schema_version must be {SCHEMA}")
    return cast("dict[str, object]", doc)


def media_root(manifest: Mapping[str, object]) -> str:
    """Where the pack's pictures are on the media host: the manifest's env override, else the lane's own shared assets
    (layout.people_dir, <work_root>/assets/people), which the lane's unprivileged user creates and reads.  A root-owned
    deploy directory is not such a place: until 11-25 the manifest named one, no picture ever got there, and every
    collaboration cover went out with the host alone."""
    root = cast("Mapping[str, object]", manifest["media_root"])
    return os.environ.get(str(root["env"])) or layout().people_dir


def cover_layer(person: Mapping[str, object], root: str, recent: Sequence[str] = ()) -> GuestLayer | str:
    """The person's picture for a cover (manifest policy `cover_layer`: transparent ground, the figure cut only by the
    picture's bottom edge; least recently used first, with a measured face breaking ties), or why she has none.
    A face the manifest does not carry (a colleague just added, a picture the official
    site changed) is read on the media host, inside her figure, by the vision reading that finds a guest's face in a
    frame or a cut-out (cover_pack_guests.faces)."""
    assets = cast("list[Mapping[str, object]]", person.get("assets") or [])
    whole = [a for a in assets if a.get("alpha") and a.get("cut_edges") == ["B"]]
    if not whole:
        portraits = portrait_options(person, root)
        if not portraits:
            return "素材包里没有可用的完整人物图或官方头像"
        last_used = {asset_id: i for i, asset_id in enumerate(recent)}
        chosen = min(portraits, key=lambda asset: (last_used.get(str(asset["asset_id"]), -1), not bool(asset.get("face"))))
        return cast(GuestLayer, {**chosen, "portrait_only": True})
    last_used = {asset_id: i for i, asset_id in enumerate(recent)}
    a = min(whole, key=lambda asset: (last_used.get(str(asset["id"]), -1), not bool(asset.get("face"))))
    face = a.get("face")
    return {"name": str(person["name"]), "asset_id": str(a["id"]), "kind": str(a["kind"]), "src": f"{root}/{a['file']}",
            "sha256": str(a["sha256"]), "fig": list(cast("list[int]", a["fig"])),
            "face": list(cast("list[int]", face)) if face else None, "source_url": str(a["source_url"])}


def portrait_options(person: Mapping[str, object], root: str) -> list[dict[str, object]]:
    """Official avatar pictures may be cropped to a circle even when their own image cuts the hair at an edge."""
    options = []
    for asset in cast("list[Mapping[str, object]]", person.get("assets") or []):
        if asset.get("kind") not in ("avatar", "face"):
            continue
        width, height = cast("list[int]", asset["size"])
        options.append({"name": str(person["name"]), "asset_id": str(asset["id"]), "kind": str(asset["kind"]),
                        "src": f"{root}/{asset['file']}", "sha256": str(asset["sha256"]),
                        "fig": list(cast("list[int]", asset.get("fig") or [0, 0, width, height])),
                        "face": asset.get("face"), "source_url": str(asset["source_url"])})
    return options


def _named(line: str, names: Sequence[str]) -> str | None:
    """The speaker a cover line names itself (「名字：原话」, 70-cover), when that name is a participant."""
    written = line.replace(":", "：")
    head = written.split("：", 1)[0].strip() if "：" in written else ""
    return next((n for n in names if len(head) >= 2 and (head in n or n in head)), None)


def line_speakers(track: Sequence[Mapping[str, object]], known: Mapping[int, str], cover_lines: Sequence[str],
                  names: Sequence[str], *, host_label: str) -> list[str | None]:
    """Who says each cover line: the name the line itself gives, else the speaker of the one subtitle line it stands in
    letter for letter (the host's label is the host; a guest line is the guest guest_identity named, or the session's
    only guest, or GUESTS when the session has several guests and none was named for it: one of them, not guessed).
    None when the line is a rewording, stands in lines of different speakers, or the subtitles carry no speakers."""
    host, guests = names[0], list(names[1:])
    out: list[str | None] = []
    for line in cover_lines:
        said = _named(line, names)
        key = text_overlap.key(line.replace(":", "：").split("：", 1)[-1])
        speakers = set()
        for index, cue in enumerate(track, 1):
            if said is None and key and key in text_overlap.key(cue["text"]):
                label = cue.get("speaker")
                speakers.add(host if label == host_label else None if not label
                             else known.get(index) or (guests[0] if len(guests) == 1 else GUESTS))
        out.append(said or (next(iter(speakers)) if len(speakers) == 1 else None))
    return out


def clip_guests(track: Sequence[Mapping[str, object]], known: Mapping[int, str], names: Sequence[str],
                speakers: Sequence[str | None], *, host_label: str) -> list[str]:
    """The session's guests who are part of this clip: the ones who speak in it (every guest when a guest line is not
    told apart) and the ones a cover line is said by.  A clip where only the host speaks, after the call, has none."""
    guests = list(names[1:])
    heard = {index for index, cue in enumerate(track, 1) if cue.get("speaker") not in (None, host_label)}
    if any(index not in known for index in heard):
        return guests
    there = {known[index] for index in heard} | {s for s in speakers if s}
    return [g for g in guests if g in there]


def roster(ctx: Ctx, names: Sequence[str], boxes: Mapping[str, list[list[int]]] | None,
           cover_lines: Sequence[str], *, recent_assets: Mapping[str, Sequence[str]] | None = None) -> CoverRoster:
    """This clip's cover roster (see the module doc).  `names`: the session's participants, host first; `boxes`: the
    frame reading (its `guest` boxes are the other streamers the host's own frame shows)."""
    from . import subtitle_track
    from .guest_identity import saved_lines
    from .speaker_v8 import HOST as HOST_LABEL

    track = cast("list[Mapping[str, object]]", subtitle_track.read_track(ctx.clip_dir))
    known = saved_lines(ctx)
    speakers = line_speakers(track, known, cover_lines, names, host_label=HOST_LABEL)
    guests = clip_guests(track, known, names, speakers, host_label=HOST_LABEL)
    in_frame = len((boxes or {}).get("guest") or [])
    mentioned = named_not_present(ctx, [str(cue["text"]) for cue in track], names)
    out: CoverRoster = {"present": list(names[1:]), "mentioned": mentioned, "needed_on_cover": guests, "source": "none",
                        "layers": [], "missing": [], "required": [], "line_speakers": speakers, "line": ""}
    if mentioned:
        out["line"] = f"只被提到、不在连线里的人：{'、'.join(mentioned)}（不上封面）；"
    if not guests:
        out["line"] += "联动嘉宾：这条片里没有嘉宾说话，封面字也没引嘉宾的话，封面不加嘉宾" if len(names) > 1 else ""
        return out
    if in_frame >= len(names) - 1:
        # her own frame shows every other streamer of the session: they are taken from the frame (who is which box is
        # not known; the judge reads who is on the picture)
        out["source"] = "frame"
        out["required"] = guests
        out["line"] += f"联动嘉宾：源画面里有 {in_frame} 位别的主播，嘉宾取自源画面"
        return out
    # the frame shows none of them, or fewer than the session has (who is missing cannot be told): the pack's pictures
    # stand for all of them (until 11-25 a frame short of someone waived every guest, and nothing was recorded)
    out["line"] += (f"联动嘉宾：源画面里只有 {in_frame} 位别的主播，少于名单（{'、'.join(names[1:])}），分不清缺谁，嘉宾都从素材包取；"
                    if in_frame else "联动嘉宾：源画面里没有别的主播；")
    manifest = load_manifest()
    people = {str(p["name"]): p for p in cast("list[Mapping[str, object]]", manifest["people"])} if manifest else {}
    for guest in guests:
        layer = (cover_layer(people[guest], media_root(cast("Mapping[str, object]", manifest)), (recent_assets or {}).get(guest, ())) if guest in people
                 else "素材包里没有这个人" if manifest else "这个仓库没有人物素材包清单")
        if isinstance(layer, str):
            out["missing"].append({"name": guest, "why": layer})
        else:
            layer["portrait_options"] = portrait_options(people[guest], media_root(cast("Mapping[str, object]", manifest)))
            layer["recent_asset_ids"] = list((recent_assets or {}).get(guest, ()))
            out["layers"].append(layer)
    out["source"] = "pack" if out["layers"] else "none"
    out["required"] = [layer["name"] for layer in out["layers"]]
    used = "、".join(f"{layer['name']} 用素材包的 {layer['asset_id']}（sha256 {layer['sha256'][:12]}"
                     + ("" if layer["face"] else "，清单没量过脸，媒体主机看图量") + "）" for layer in out["layers"])
    lacking = "；".join(f"缺{m['name']}的素材（{m['why']}），这位不上封面" for m in out["missing"])
    out["line"] += ("；".join(part for part in (used, lacking) if part)
                    + ("" if out["layers"] else "；封面照没有素材包时的做法做"))
    return out


def named_not_present(ctx: Ctx, texts: Sequence[str], names: Sequence[str]) -> list[str]:
    """The knowledge graph's people (collab_guests.streamers: formal name -> spellings) the texts name, who are not among
    `names` (the session's participants): talked about, not in the call."""
    from .collab_guests import mentions, streamers

    return sorted(name for name, lines in mentions(texts, streamers(ctx)).items() if lines and name not in names)


def unreadable(rows: Sequence[CoverCandidate]) -> list[dict[str, str]]:
    """The pack pictures the media host could not use for a reason that is not the pack's (the tool's
    `guest_assets_dropped` with UNREADABLE: not readable as the manifest lists it; FACE_READ_FAILED: the reading of her
    face got no answer), each once: the cover stops on them, never goes without her."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        for d in row.get("guest_assets_dropped") or []:
            if d["code"] in (UNREADABLE, FACE_READ_FAILED):
                out.setdefault((d["name"], d["src"]), d)
    return list(out.values())


def unreadable_note(dropped: Sequence[Mapping[str, str]]) -> str:
    return "；".join(f"{d['name']}：{d['why']}" for d in dropped)


def faceless(row: CoverCandidate) -> list[dict[str, str]]:
    """The guests whose pack picture the media host has but in which nobody found her face: as if the pack had none."""
    return [d for d in row.get("guest_assets_dropped") or [] if d["code"] == FACE_UNREAD]


def roster_vetoes(row: CoverCandidate, plan: CoverPlan) -> list[str]:
    """70-cover: a candidate that leaves out a guest the cover can show is not eligible.  Shown is what the judge sees on
    the rendered picture (`people.present`, confirmed by readings like a veto: cover_confirm `needed_present`), never
    the tool's word that it put her there.  Only a guest the pack has no usable picture of (FACE_UNREAD) is not asked
    for; a listed picture the media host cannot read stops the cover before this (step6_cover._render_cover)."""
    lost = {d["name"] for d in faceless(row)}
    required = [name for name in (plan.get("roster") or {}).get("required") or [] if name not in lost]
    present = ((row.get("verdict") or {}).get("people") or {}).get("present")
    shown = set(present if isinstance(present, list) else [])
    absent = [name for name in required if name not in shown]
    return [f"{ROSTER_INCOMPLETE}:{'、'.join(absent)}"] if absent else []


def visible(row: CoverCandidate, host: str) -> list[str]:
    """The people other than the host the judge sees on this picture (the cover's `actually_visible`)."""
    present = ((row.get("verdict") or {}).get("people") or {}).get("present")
    return [p for p in (present if isinstance(present, list) else []) if p != host]


def note_wanted(ctx: Ctx, missing: Sequence[Mapping[str, str]]) -> str:
    """Add the clip's guests the cover went without for want of a picture to the day's record (WANTED: who, why, which
    clips; the list of pictures to fetch, scripts/fetch_person_assets.py --add) and return its path.  Called under the
    batch's cover lock (step6_title.cover_batch_lock), so the clips of one run add to it one at a time."""
    from .step_io import _write_atomic

    date = str(ctx.session["recording_date"])
    if not date:
        raise ValueError(f"{ctx.clip_dir.name}: the session has no recording_date, the day's record of pictures to fetch has no name")
    path = ctx.clip_dir.parent.parent / WANTED.format(date=date)
    record = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
              else {"date": date, "wanted": {}})
    for m in missing:
        entry = record["wanted"].setdefault(m["name"], {"why": m["why"], "clips": []})
        entry["why"] = m["why"]
        if ctx.clip_dir.name not in entry["clips"]:
            entry["clips"].append(ctx.clip_dir.name)
    _write_atomic(path, json.dumps(record, ensure_ascii=False, indent=1))
    return str(path)


def wanted(task: Path) -> dict[str, dict[str, object]]:
    """The task's days' records of pictures to fetch (WANTED), by guest: why, which days, which clips; a guest the pack
    now has a cover picture of with its face measured (fetched since) is left out.  Read by `status` and the unattended round's report, which
    are what someone looks at: the record is the to-do of scripts/fetch_person_assets.py --add."""
    manifest = load_manifest()
    people = {str(p["name"]): p for p in cast("list[Mapping[str, object]]", manifest["people"])} if manifest else {}
    out: dict[str, dict[str, object]] = {}
    for path in sorted(task.glob(WANTED.format(date="*"))):
        record = json.loads(path.read_text(encoding="utf-8"))
        for name, entry in record["wanted"].items():
            layer = cover_layer(people[name], "") if name in people else None
            if isinstance(layer, dict) and layer["face"]:     # a cover picture with its face measured: fetched since
                continue
            row = out.setdefault(name, {"why": entry["why"], "dates": [], "clips": []})
            row["why"] = entry["why"]
            cast("list[str]", row["dates"]).append(record["date"])
            cast("list[str]", row["clips"]).extend(entry["clips"])
    return out


def wanted_lines(task: Path) -> list[str]:
    """`status`'s lines for `wanted`."""
    return [f"待补人物素材：{name}（{row['why']}；{'、'.join(cast('list[str]', row['dates']))} "
            f"{len(cast('list[str]', row['clips']))} 条片封面缺她；补图：scripts/fetch_person_assets.py --add {name}:<B站uid>）"
            for name, row in wanted(task).items()]
