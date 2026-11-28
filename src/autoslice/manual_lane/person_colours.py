"""Each person's subtitle colour (the channel's request).

assets/manual_lane/person_colours.v1.json gives a person the outline colour of her burned lines, with where it comes
from and when it was taken (its `policy`).  A guest line step 4 names (speaker/persons.json) is burned in the guest style
with that outline (subtitle_rendering.PERSON_STYLE_PREFIX + her id); a person without a colour there, and every line not
named, keeps the channel's guest style.  The channel's own host keeps the profile's host style.  The public export
strips the file (scripts/export_oss_snapshot.py): without it no guest has a colour of her own."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast

from src.autoslice.subtitle_rendering import PERSON_STYLE_PREFIX

from .layout import REPO_ROOT

REGISTRY = REPO_ROOT / "assets/manual_lane/person_colours.v1.json"
SCHEMA = "manual-lane-person-colours.v1"
OUTLINE_FIELD = 4           # OutlineColour among the 22 fields of an ASS style value after its name
BACK_FIELD, WIDTH_FIELD = 5, 15   # BackColour (the shadow's colour) and Outline (the outline's width)
LIGHT_OUTLINE = 3.0         # below this contrast with the white letters (WCAG 2 AA for large text) an outline is light:
                            # on the light bottom band of a stream the letters lose their edge at phone size (礼墨Sumi's
                            # gold, 2.81: 1.85-2.73 against the band) ...
SOLID_SHADOW = "&H00000000" # ... so her lines get an outline one pixel wider and an opaque black shadow, her colour unchanged


def people() -> dict[str, Mapping[str, object]]:
    """{name: entry} of the registry; {} without the file (the public export)."""
    if not REGISTRY.is_file():
        return {}
    doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    if doc.get("schema_version") != SCHEMA:
        raise ValueError(f"{REGISTRY}: schema_version must be {SCHEMA}")
    out: dict[str, Mapping[str, object]] = {}
    for row in cast("list[Mapping[str, object]]", doc["people"]):
        if not re.fullmatch(r"#[0-9A-F]{6}", str(row["outline"])) or not re.fullmatch(r"[a-z0-9]+", str(row["id"])):
            raise ValueError(f"{REGISTRY}: {row.get('name')}: outline must be #RRGGBB and id lower-case letters and digits")
        out[str(row["name"])] = row
    return out


def ass_colour(hex_colour: str) -> str:
    """#RRGGBB as an opaque ASS colour (&H00BBGGRR)."""
    r, g, b = hex_colour[1:3], hex_colour[3:5], hex_colour[5:7]
    return f"&H00{b}{g}{r}"


def channel_guest_style() -> str:
    """The channel's guest style (profile asset subtitle_burn_style `guest`, the one the renderer burns unnamed lines in)."""
    from src.autoslice.channel_profile import load_channel_profile

    path = load_channel_profile(REPO_ROOT).asset_file("subtitle_burn_style")
    value = json.loads(path.read_text(encoding="utf-8")).get("guest")
    if not isinstance(value, str) or len(value.split(",")) != 22:
        raise ValueError(f"{path}: guest is not one ASS style value (22 comma-separated fields after the name)")
    return value


def guest_style(name: str, guest: str, known: Mapping[str, Mapping[str, object]]) -> tuple[str, str] | None:
    """(style name, ASS style value) of `name`'s guest lines: the channel's guest style `guest` with her outline (a light
    one, below LIGHT_OUTLINE, one pixel wider over an opaque black shadow); None when the registry has no colour for her."""
    row = known.get(name)
    if row is None:
        return None
    from .preflight import OUTLINE_MIN_CONTRAST, outline_contrast

    fields = guest.split(",")
    fields[OUTLINE_FIELD] = ass_colour(str(row["outline"]))
    style = ",".join(fields)
    contrast = outline_contrast(style)
    if contrast is None or contrast < OUTLINE_MIN_CONTRAST:
        raise ValueError(f"{REGISTRY}: {name} 的描边 {row['outline']} 和嘉宾字色对比只有 {contrast}"
                         f"（至少 {OUTLINE_MIN_CONTRAST}），烧进去看不清")
    if contrast < LIGHT_OUTLINE:
        fields[BACK_FIELD] = SOLID_SHADOW
        fields[WIDTH_FIELD] = f"{float(fields[WIDTH_FIELD]) + 1:g}"
        style = ",".join(fields)
    return PERSON_STYLE_PREFIX + str(row["id"]), style


def outline_hex(style: str) -> str:
    """The outline colour of an ASS style value as #RRGGBB."""
    bgr = style.split(",")[OUTLINE_FIELD].strip().upper().removeprefix("&H")[-6:]
    return f"#{bgr[4:6]}{bgr[2:4]}{bgr[0:2]}"


def legend(host: str, guests: Sequence[str]) -> str:
    """Which outline colour each speaker's burned lines carry (white letters throughout): the host in the profile's host
    style, each guest the registry gives a colour, the other guest lines in the channel's guest style.  For a reader of
    the subtitles who cannot see the picture (the review, step 9)."""
    from src.autoslice.channel_profile import load_channel_profile

    styles = json.loads(load_channel_profile(REPO_ROOT).asset_file("subtitle_burn_style").read_text(encoding="utf-8"))
    known = people()
    parts = [f"{host} 描边 {outline_hex(str(styles['host']))}（主播样式）"]
    parts += [f"{g} 描边 {known[g]['outline']}" for g in guests if g in known]
    parts.append(f"其余嘉宾句（没定到人或没有登记颜色）描边 {outline_hex(channel_guest_style())}（通用嘉宾样式）")
    return "字幕都是白字，按描边颜色分人：" + "；".join(parts)
