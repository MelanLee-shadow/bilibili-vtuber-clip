"""Guests of a collaboration who are not in the host's own frame, on the cover from the person asset pack (step 6,
70-cover 「联动嘉宾」; cover_candidates.py on the media host).

The caller's job lists one pack picture per such guest (`guest_assets`: name, picture, sha256, the figure's box and the
measured face in the picture; step6 cover_guests.roster) and who says each cover line (`line_speakers`).  The cover is
then a split screen: one panel per person, left to right, all the same width; the host's panel is her picture of the
route (the live frame, or her emote / cut-out / sticker on a ground), a guest's panel is her pack picture on a ground of
the lane's background table.  Every face has the SAME height and the same top, each in the middle of its panel; a
picture is only scaled, placed and cut by its panel's edges (wide hair cannot reach a neighbour's face): nothing is
drawn or edited.  The panels are painted into one picture the renderer takes as its frame.

- `usable`: a picture this host cannot read (missing, no permission) or whose bytes are not the manifest's is dropped
  and named with UNREADABLE (never replaced; step 6 stops the cover on it: the pack lists her picture, so it must be here);
- `faces`: a picture the manifest carries no measured face for is read here, inside her figure (a guest found by
  nobody is dropped and named with FACE_UNREAD: the picture is there but has no usable face, as if the pack had none;
  a reading that got no answer is FACE_READ_FAILED, and step 6 stops the cover on it like on UNREADABLE);
- `line_up`: left to right, so that each line can stand over its speaker: the punchline is the widest line and the
  feed-safe zone keeps it near the middle, so with three or more people its speaker stands in the middle (two guests
  not told apart stand left of the middle line, the host right) and line 1's speaker left; with two people line 1's
  speaker stands left and the punchline's right (the renderer's top layout puts line 1 left and the punchline right);
- `plan`: the shapes, the face height and where each person's picture goes (numbers only);
- `paint`: the picture;
- `anchor`: each line whose speaker is on the cover gets `at`, the x of that speaker's face centre;
- `setup`, `cover`, `cutout`, `drawn`, `row_facts`: what cover_candidates.Run calls (its routes hand their host picture
  over when the run has pack guests; the tool's own file keeps only those calls).
"""
import math
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

import cover_background as background
import cover_collab_layouts as collab_layouts
import cover_text_fit as text_fit
from cover_face_crop import CHEST, HOST, protect

FRAME_W, FRAME_H = 1920, 1080
FACE_MARGIN = 30     # px a face keeps from each side of its panel (the hair beside it may be cut by the panel, the face not)
FADE = 90            # px over which a frame fades into its own blurred rows above it (cover_band.FEATHER: a hard edge reads as pasted)


UNREADABLE = "GUEST_ASSET_UNREADABLE"   # step6 cover_guests.UNREADABLE, the same word
FACE_UNREAD = "GUEST_FACE_UNREAD"       # step6 cover_guests.FACE_UNREAD, the same word
FACE_READ_FAILED = "GUEST_FACE_READ_FAILED"   # step6 cover_guests.FACE_READ_FAILED, the same word


def usable(assets, sha):
    """(layers whose picture is on this host with the manifest's bytes, [{name, why, code, src}] for the others)."""
    kept, dropped = [], []
    for asset in assets or []:
        try:
            same = sha(asset["src"]) == asset["sha256"]
        except OSError as exc:
            dropped.append({"name": asset["name"], "code": UNREADABLE, "src": asset["src"],
                            "why": f"素材包的图在这台主机上读不到（{exc.strerror or type(exc).__name__}：{asset['src']}）"})
            continue
        if same:
            kept.append(dict(asset, cutout="alpha"))
        else:
            dropped.append({"name": asset["name"], "code": UNREADABLE, "src": asset["src"],
                            "why": f"素材包的图和清单的 sha256 对不上（{asset['src']}）"})
    return kept, dropped


def faces(kept, out_dir, read):
    """(the layers, each with her face, [{name, why}] for those whose face was not found).  A layer without the
    manifest's face is laid on a light ground (its transparent pixels carry no colour a reader can rely on) and `read`
    (cover_candidates.guest_face: one person's face inside a box, confirmed, reused while the bytes are unchanged) finds
    her face inside her figure; `read` raises (ValueError, OSError: cover_confirm.confirm_found strict) when a call gave
    no answer, and she is then named with FACE_READ_FAILED, not as if her picture had no face."""
    out, dropped = [], []
    for layer in kept:
        if layer.get("face"):
            out.append(dict(layer, face_from="manifest"))
            continue
        flat = os.path.join(out_dir, f"pack-{layer['sha256'][:16]}.png")
        if not os.path.exists(flat):
            with Image.open(layer["src"]) as im:
                ground = Image.new("RGBA", im.size, (245, 245, 245, 255))
                ground.alpha_composite(im.convert("RGBA"))
                ground.convert("RGB").save(flat)
        try:
            face = read(flat, f"locate-pack-{layer['sha256'][:16]}", layer["fig"])
        except (ValueError, OSError) as exc:
            dropped.append({"name": layer["name"], "code": FACE_READ_FAILED, "src": layer["src"],
                            "why": f"清单没量过脸，看图量她的脸时调用失败（{type(exc).__name__}: {str(exc)[:160]}），不当作没有素材"})
            continue
        if face is None:
            dropped.append({"name": layer["name"], "code": FACE_UNREAD, "src": layer["src"],
                            "why": f"清单没量过脸，看图也没在素材包的图里找到她的脸（{layer['src']}）"})
        else:
            out.append(dict(layer, face=face, face_from="看图"))
    return out, dropped


GUESTS = "<嘉宾之一>"    # a line said by one of the guests who are not told apart (step6 cover_guests.GUESTS, the same word)


def line_up(host, guests, speakers, host_side):
    """(names left to right, why)."""
    order = [*guests, host] if host_side == "right" else [host, *guests]
    side = f"主播在{'右' if host_side == 'right' else '左'}"
    speakers = list(speakers or [])
    first, last = (speakers[0], speakers[-1]) if len(speakers) >= 2 else (None, speakers[-1] if speakers else None)
    if len(order) == 2:
        if first and last and first != last and first in order and last in order:
            return [first, last], "第一行的说话人站左、笑点行的说话人站右"
        return order, side
    if last == GUESTS:          # the punchline is one of the guests', who are not told apart: it stands over both
        return [*guests, host], "笑点行是分不清哪位嘉宾说的，嘉宾并排站左边、主播站右"
    if last in order:           # the widest line stays near the middle (safe zone): its speaker stands there
        rest = [p for p in order if p != last]
        if first in rest:
            rest = [first, *[p for p in rest if p != first]]
        middle = len(order) // 2
        why = "笑点行的说话人站中间" + ("、第一行的说话人站左" if first in rest else "")
        return [*rest[:middle], last, *rest[middle:]], why
    if first in order:
        return [first, *[p for p in order if p != first]], "第一行的说话人站左"
    return order, side


def pair_row(host, guests, speakers, host_side):
    """Keep the guest quoted on the cover nearest the middle, where long words can stay inside the safe zone."""
    quoted = next((speaker for speaker in reversed(speakers or []) if speaker in guests), None)
    near = quoted or guests[0]
    far = [guest for guest in guests if guest != near]
    return [*far, near, host] if host_side == "right" else [host, near, *far]


def anchor(lines, speakers, faces, host):
    """The spec's lines, each with `at` (its speaker's face centre x) when the speaker is on the cover.  `faces`: {name:
    face}, left to right.  A line of one of the guests not told apart stands over all of them when they stand side by
    side (the host at an end); otherwise it gets no `at`."""
    names = list(faces)
    guests = [faces[name] for name in names if name != host]
    together = guests and host in names and names.index(host) in (0, len(names) - 1)
    out = []
    for line, speaker in zip(lines, list(speakers or []) + [None] * len(lines), strict=False):
        box = faces.get(speaker)
        if speaker == GUESTS and together:
            box = [min(b[0] for b in guests), 0, max(b[2] for b in guests), 0]
        out.append(dict(line, at=round((box[0] + box[2]) / 2)) if box else dict(line))
    return out


def shapes(n, slant):
    """The n panels' corners, left to right: equal widths at mid height, the dividers leaning `slant` px each way."""
    def edge(x, lean):
        return x + lean if 0 < x < FRAME_W else x
    cuts = [round(i * FRAME_W / n) for i in range(n + 1)]
    return [[(edge(a, slant), 0), (edge(b, slant), 0), (edge(b, -slant), FRAME_H), (edge(a, -slant), FRAME_H)]
            for a, b in zip(cuts, cuts[1:], strict=False)]


def span(shape, y):
    """The panel's left and right x at canvas row y."""
    t = y / FRAME_H
    return shape[0][0] + (shape[3][0] - shape[0][0]) * t, shape[1][0] + (shape[2][0] - shape[1][0]) * t


def plan(people, rules, top_min, bottom_room=None, face_frac=None, panels=None):
    """Where everyone stands.  `people`, left to right: {name, kind: "frame" | "layer", face, and for a layer `fig`
    (the figure's box in its picture) and `cut_bottom` (the picture's bottom edge cuts the figure, so it has to leave
    through the frame's bottom)}.  `top_min`: the face top the lines above need.  Returns {shapes, face_px,
    face_top, places: {name: {scale, dx, dy, face}}}: a picture pixel (x, y) lands on the canvas at
    (dx + x * scale, dy + y * scale).  The face height is the lane's target, or the host's own height in a live frame
    when that is larger, as far as every panel is wide enough for a face at that height and the frame's bottom leaves
    room; a frame whose face is larger than that is shrunk to it (her face near the camera: the frame then stands on
    the panel's bottom and `paint` blurs its top rows on above it).  Raises ValueError when the height falls under the
    veto floor, a shrunk frame no longer covers its panel from side to side, or the frame's edge keeps the host's face
    out of her panel.  `bottom_room` (templates panels-bottom, panels-diagonal): words go in a band along the bottom, so the faces
    stand from `top_min` down and leave that many px under their chests (the words keep off the chest and above); a frame may then end above the canvas's
    bottom (`paint` carries its own bottom rows, blurred, down under the band).  `face_frac`: the template's face height."""
    panels = panels or shapes(len(people), rules["panel_slant_px"])
    # the height at which each face still fits its panel: the panel's width where it is narrowest under the lines
    fits = [(min(b - a for a, b in (span(shape, top_min), span(shape, FRAME_H))) - 2 * FACE_MARGIN)
            * (p["face"][3] - p["face"][1]) / (p["face"][2] - p["face"][0]) for p, shape in zip(people, panels, strict=True)]
    frames = [p for p in people if p["kind"] == "frame"]
    target = (face_frac or rules["face_target_frac"]) * FRAME_H
    if bottom_room is None:
        size = min(max([target, *[p["face"][3] - p["face"][1] for p in frames]]),
                   (FRAME_H - top_min) / (1 + rules["chin_margin_frac"]), *fits)
    else:
        # the words stay off everyone's face down to the chest (cover_face_crop.protect, CVR-05), so the band starts
        # that far under the faces; sized with the chin margin, the renderer squeezed the words under the floor
        size = min(target, (FRAME_H - bottom_room - top_min) / (1 + CHEST), *fits)
    if size < rules["face_floor_frac"] * FRAME_H:
        raise ValueError("PANEL_FACE_UNDER_FLOOR")
    top = min(top_min, math.floor(FRAME_H - size * (1 + rules["chin_margin_frac"])))
    for person in frames if bottom_room is None else ():   # her frame stands on the panel's bottom, so its bottom edge
        face, scale = person["face"], size / (person["face"][3] - person["face"][1])   # bounds how high her face can be:
        top = max(top, math.ceil(FRAME_H - (FRAME_H - face[1]) * scale))               # everyone stands level with her
    places = {}
    for person, shape in zip(people, panels, strict=True):
        face = person["face"]
        scale = size / (face[3] - face[1])
        left, right = span(shape, top + size / 2)
        dx, dy = (left + right) / 2 - (face[0] + face[2]) / 2 * scale, top - face[1] * scale
        if person["kind"] == "frame":      # her frame covers the panel from side to side and down to its bottom (above its
            low, high = min(x for x, _ in shape), max(x for x, _ in shape)      # top edge, under the lines, `paint` blurs it on)
            if FRAME_W * scale < high - low:
                raise ValueError("PANEL_FRAME_TOO_SMALL_FOR_PANEL")
            dx = min(max(dx, high - FRAME_W * scale), low)
            if bottom_room is None:
                dy = max(dy, FRAME_H - FRAME_H * scale)
        elif person.get("cut_bottom") and dy + person["fig"][3] * scale < FRAME_H:
            raise ValueError("PANEL_FIGURE_ENDS_ABOVE_FRAME_BOTTOM")      # her picture's own bottom cut would show
        placed = [round(dx + face[0] * scale), round(dy + face[1] * scale), round(dx + face[2] * scale), round(dy + face[3] * scale)]
        if placed[0] < left + FACE_MARGIN - 1 or placed[2] > right - FACE_MARGIN + 1:
            raise ValueError("PANEL_HOST_FACE_OUTSIDE_FRAME_REACH")       # the frame's side edge keeps her out of the middle
        places[person["name"]] = {"scale": scale, "dx": round(dx), "dy": round(dy), "face": placed}
    return {"shapes": panels, "face_px": round(size), "face_top": top, "places": places, "bottom_room": bottom_room}


def paint(people, made, renderer, stickers, grounds, blur, rules, out_path, divider_indices=None):
    """Write the split screen to `out_path`.  `stickers`, `grounds`: {name: sticker} and {name: [top colour, bottom
    colour]} for the people who stand on a ground; `blur`: the boxes of a live frame to blur (frame pixels: the chat
    column and the like)."""
    canvas = Image.new("RGB", (FRAME_W, FRAME_H))
    for person, shape in zip(people, made["shapes"], strict=True):
        place = made["places"][person["name"]]
        scale, dx, dy = place["scale"], place["dx"], place["dy"]
        if person["kind"] == "frame":
            with Image.open(person["src"]) as im:
                picture = im.convert("RGB")
            for x0, y0, x1, y1 in blur or []:      # the renderer's own blur of a frame's boxes, in the frame's pixels
                mask = Image.new("L", picture.size, 0)
                ImageDraw.Draw(mask).rectangle((x0, y0, x1, y1), fill=255)
                picture = Image.composite(picture.filter(ImageFilter.GaussianBlur(22 / scale)), picture,
                                          mask.filter(ImageFilter.GaussianBlur(30 / scale)))
            x0, y0 = max(0, math.floor(-dx / scale)), max(0, math.floor(-dy / scale))
            x1, y1 = min(picture.width, math.ceil((FRAME_W - dx) / scale)), min(picture.height, math.ceil((FRAME_H - dy) / scale))
            part = picture.crop((x0, y0, x1, y1)).resize((round((x1 - x0) * scale), round((y1 - y0) * scale)), Image.LANCZOS)
            part = ImageEnhance.Color(part).enhance(renderer.FRAME_SATURATION)
            fade = Image.new("L", part.size, 255)
            if dy > 0:          # her face stands lower than her frame's top edge allows: above the frame, the frame's own top
                reach = min(FADE, part.height)      # rows blurred and stretched up (the lines stand there), fading into the frame
                content = part.crop((0, 0, part.width, reach)).resize((part.width, round(dy) + reach)).filter(ImageFilter.GaussianBlur(40))
                canvas_part = Image.new("RGB", (FRAME_W, FRAME_H))
                canvas_part.paste(content, (round(dx + x0 * scale), 0))
                fade.paste(Image.linear_gradient("L").resize((part.width, reach)), (0, 0))
                content = canvas_part
            else:
                content = Image.new("RGB", (FRAME_W, FRAME_H))
            content.paste(part, (round(dx + x0 * scale), round(dy + y0 * scale)), fade)
            low = round(dy + y0 * scale) + part.height
            if low < FRAME_H:   # panels-bottom: the frame ends above the canvas's bottom; its own bottom rows, blurred, go on under the band
                reach = min(FADE, part.height)
                tail = part.crop((0, part.height - reach, part.width, part.height)).resize((part.width, FRAME_H - low + reach))
                tail = tail.filter(ImageFilter.GaussianBlur(40))
                mask_tail = Image.new("L", tail.size, 255)
                mask_tail.paste(Image.linear_gradient("L").resize((part.width, reach)), (0, 0))
                content.paste(tail, (round(dx + x0 * scale), low - reach), mask_tail)
        else:
            ground = renderer.pastel({"bg": grounds[person["name"]]}).convert("RGBA")
            renderer.paste_sticker(ground, stickers[person["name"]])
            content = ground.convert("RGB")
        mask = Image.new("L", (FRAME_W, FRAME_H), 0)
        ImageDraw.Draw(mask).polygon(shape, fill=255)
        canvas.paste(content, (0, 0), mask)
    draw = ImageDraw.Draw(canvas)
    for index, shape in enumerate(made["shapes"][:-1]):
        if divider_indices is not None and index not in divider_indices:
            continue
        draw.line([shape[1], shape[2]], fill=(255, 255, 255), width=rules["panel_rule_px"])
    canvas.save(out_path)


def sticker(person, place, tight):
    """A layer person as the renderer's paste_sticker takes her: the figure from its top down to where it leaves the
    canvas, no outline (her panel's edges and the frame's bottom cut her, a rim there would draw the cut).
    `tight(src, cutout, box)`: the box of the painted pixels inside `box` of the picture (the renderer pastes that box)."""
    fig, scale = person["fig"], place["scale"]
    bottom = min(fig[3], math.ceil((FRAME_H - place["dy"]) / scale) + 1)
    crop = tight(person["src"], person["cutout"], [fig[0], fig[1], fig[2], bottom])
    return {"src": person["src"], "cutout": person["cutout"], "crop": crop, "outline": 0,
            "h": round((crop[3] - crop[1]) * scale), "x": round(place["dx"] + crop[0] * scale), "y": round(place["dy"] + crop[1] * scale)}


def painted_box(src, cutout_mode, box, renderer):
    """The box of the painted pixels inside `box` of a picture (None: the whole picture); the renderer pastes that box."""
    with Image.open(src) as im:
        box = list(box or (0, 0, *im.size))
        inner = renderer.cutout(im, cutout_mode).crop(tuple(box)).getchannel("A").getbbox()
    return [box[0] + inner[0], box[1] + inner[1], box[0] + inner[2], box[1] + inner[3]] if inner else box


def setup(run, sha, top_min, read_face):
    """The run's pack guests (none beside guests the frame itself shows), who says each line, and the row.
    `read_face(src, name, box)`: the reading `faces` uses."""
    job = run.job
    run.pack, run.pack_dropped = usable(None if run.guest_boxes else job.get("guest_assets"), sha)
    run.pack, unread = faces(run.pack, run.out, read_face)
    run.pack_dropped = [*run.pack_dropped, *unread]
    run.pack_sha, run.pack_read_face, run.people_used, run.portrait_skipped = sha, read_face, {}, {}
    run.speakers, run.pack_top_min = job.get("line_speakers") or [], top_min
    run.people_faces, run.sides, run.grounds = {}, {}, {}   # by route: {name: face on the cover}; the host's place; the panels' grounds
    if run.pack:
        run.host_name = job["host_name"]
        run.row, run.row_why = line_up(run.host_name, [g["name"] for g in run.pack], run.speakers, run.host_side)


def text_top(run):
    """The face top the punchline needs at the largest size its width allows: the floor size, else the next sizes down
    to the renderer's minimum (Run.need_top is None for a punchline too wide for the floor size; the faces of a split
    screen are placed, not found, so they leave the room the line can use).  None when no size fits."""
    ink = run.text_ink({"lines": run.lines})[1]
    for goal in range(run.rules["punch_floor_px"], run.renderer.MIN_PUNCH - 1, -6):
        top = text_fit.need_face_top(ink, len(run.lines), run.rules, goal)
        if top is not None:
            return top
    return None


def room(template, words, rules, pack_top_min):
    """(top_min, bottom_room) for `plan`.  `words`: the face top the lines need above a face (text_top).  The words on
    top (panels-top): the faces stand under them and under the pack's own band (pack_top_min).  The words in a band
    along the bottom (panels-bottom): nothing above the faces, so they stand from the template's face top (the pack's
    band for lines above is not kept: with it every three-person split fell under the 25 % floor, 2026-11-25 replay of
    the ten covers); the band is the strip the lines need (text_fit.need_face_top) plus the renderer's 10 px over and
    20 px under it."""
    if template["text"] == "bottom":
        return (round((template["framing"].get("face_top_frac") or 0) * FRAME_H),
                words - rules["text_zone_top_px"] - rules["text_zone_gap_px"] + 30)
    return max(pack_top_min, words or 0), None


def diagonal_room(run):
    """(top_min, bottom_room) for `plan` when the words stand on both sides of the faces (template panels-diagonal, the
    renderer's diagonal layout): line 1 in the strip above the faces, the punchline in a band under everyone's chest.
    The punchline is measured at the floor size on the grid of the renderer's diagonal layout (it starts the punchline
    20 px over the usual start, render_cover plan_diagonal, and shrinks it by 6: 270, 264 ... 204), line 1 at the
    style's size ratio of it (what the renderer gives line 1 beside that punchline); the renderer's zones are 30 px
    from the top and 10 px from the faces above, 10 px under the chests and 20 px from the bottom below, and 2 px more
    for the rounding of the placed faces.  None when a line is too wide for the safe zone at that size."""
    if len(run.lines) != 2:
        return None
    style, ink = run.text_ink({"lines": run.lines})
    start = run.rules["punch_start_px"] + 20
    goal = start - 6 * ((start - run.rules["punch_floor_px"]) // 6)
    (w0, h0), (w1, h1) = ink(0, max(80, int(goal * style["size_ratio"]))), ink(1, goal)
    if max(w0, w1) > run.rules["text_safe_width_px"]:
        return None
    return run.rules["text_zone_top_px"] + run.rules["text_zone_gap_px"] + h0, h1 + 32


def layer_host(run, src, cutout_mode, face):
    """The host as a layer person: her emote, cut-out or sticker with its measured face."""
    return {"kind": "layer", "src": src, "cutout": cutout_mode, "face": face, "fig": painted_box(src, cutout_mode, None, run.renderer)}


def collage(run, route, host, note, main_emote, blur, family):
    """Prepare a round-portrait composition and hand the real face boxes to the usual text renderer and judge."""
    chosen = []
    skipped = []
    for guest in run.pack:
        recent = {asset_id: i for i, asset_id in enumerate(guest.get("recent_asset_ids") or [])}
        options = [guest, *(option for option in (guest.get("portrait_options") or [])
                            if option["asset_id"] != guest["asset_id"])]
        options.sort(key=lambda asset: (recent.get(asset["asset_id"], -1), asset.get("kind") != "face"))
        for candidate in options:
            if candidate is guest:
                chosen.append(guest)
                break
            verified, dropped = usable([candidate], run.pack_sha)
            measured, unread = faces(verified, run.out, run.pack_read_face)
            fatal = [d for d in [*dropped, *unread] if d["code"] in (UNREADABLE, FACE_READ_FAILED)]
            if fatal:
                run.pack_dropped.extend(fatal)
                return run.fail(route, "GUESTS_DO_NOT_FIT:PORTRAIT_UNUSABLE")
            if unread:     # this picture has no face; the guest's already verified body is still available
                skipped.extend(unread)
                continue
            chosen.extend(measured)
            break
    original = run.pack
    run.pack = chosen
    try:
        picture, face_map, grounds, row = collab_layouts.build(run, route, host, family, blur)
    except ValueError as exc:
        return run.fail(route, f"GUESTS_DO_NOT_FIT:{exc}")
    finally:
        run.pack = original
    run.people_used[route] = chosen
    run.portrait_skipped[route] = skipped
    guests = [g["name"] for g in run.pack]
    measured = [face_map[name] for name in guests]
    run.people_faces[route], run.grounds[route] = face_map, grounds
    run.sides[route] = "left" if row[0] == run.host_name else "right" if row[-1] == run.host_name else "middle"
    run.plans[route] = {"row": row, "layout": family}
    spec = {"id": route, "frame": picture, "crop": [0, 0, FRAME_W], "saturation": 1.0,
            "composition": f"collab_{family}", **run.column.text(route, run.lines),
            "face": protect(list(face_map.values())), "keep": measured}
    spec["lines"] = anchor(spec["lines"], run.speakers, face_map, run.host_name)
    positions = (f"圆头像从左到右 {'、'.join(row)}" if family == "dialogue" else
                 f"主播为大图，左下 {guests[0]}、右下 {guests[1]}" if family == "portraits" else
                 f"主播在左，右上 {guests[0]}、右下 {guests[1]}")
    note += f"；嘉宾用素材包的官方图裁成圆头像，人物没有重画；{positions}；联动构图 {family}"
    run.specs.append((route, spec, note, main_emote, {"kind": "picture", "src": picture, "face": face_map[run.host_name]}, measured))
    return None


def cover(run, route, host, note, main_emote=None, blur=None):
    """The split screen of this route: `host` ({kind: frame | layer, src, face, and for a layer cutout and fig}) and the
    pack's guests, left to right as line_up says; when a panel of that row is too narrow for its face, the host in
    another place.  Appends the candidate's spec to the run, or records why the row cannot be made."""
    guests = [g["name"] for g in run.pack]
    run.people_used.pop(route, None)
    run.portrait_skipped.pop(route, None)
    template = run.column.template or {"text": "top", "framing": {}}
    family = template.get("pack_layout", "split")
    if family in ("dialogue", "portraits", "stacked"):
        return collage(run, route, host, note, main_emote, blur, family)
    if any(g.get("portrait_only") for g in run.pack):
        return run.fail(route, "GUESTS_DO_NOT_FIT:PORTRAIT_ONLY")
    rows = [run.row] + [r for i in range(len(guests) + 1) if (r := [*guests[:i], run.host_name, *guests[i:]]) != run.row]
    if family == "pair":
        if len(guests) != 2:
            return run.fail(route, "GUESTS_DO_NOT_FIT:COLLAB_LAYOUT_REQUIRES_TWO_GUESTS")
        rows = [pair_row(run.host_name, guests, run.speakers, run.host_side)]
    made = why = None
    if template["text"] == "diagonal":
        if (sides := diagonal_room(run)) is None:
            return run.fail(route, "GUESTS_DO_NOT_FIT:LINE_TOO_WIDE")
        top_min, bottom_room = sides
    else:
        words = text_top(run)
        if template["text"] == "bottom" and words is None:
            return run.fail(route, "GUESTS_DO_NOT_FIT:PUNCHLINE_TOO_WIDE")
        top_min, bottom_room = room(template, words, run.rules, run.pack_top_min)
    for row in rows:
        people = [dict(host, name=name) if name == run.host_name
                  else dict(next(g for g in run.pack if g["name"] == name), kind="layer", cut_bottom=True) for name in row]
        try:
            cuts = ([0, 480, 960, FRAME_W] if row[-1] == run.host_name else [0, 960, 1440, FRAME_W]) if family == "pair" else None
            panels = [[(a, 0), (b, 0), (b, FRAME_H), (a, FRAME_H)] for a, b in zip(cuts, cuts[1:], strict=False)] if cuts else None
            made = plan(people, run.rules, top_min, bottom_room, template["framing"].get("face_frac"), panels)
            break
        except ValueError as exc:
            why = why or str(exc)
    if made is None:
        return run.fail(route, f"GUESTS_DO_NOT_FIT:{why}")
    style = run.text_ink({"lines": run.lines})[0]
    sha_ = run.job.get("content_sha256") or run.renderer.default_content_sha256({"id": route, "lines": run.lines})
    stickers = {p["name"]: sticker(p, made["places"][p["name"]], lambda *a: painted_box(*a, run.renderer))
                for p in people if p["kind"] == "layer"}
    grounds = {name: background.ground(run.renderer, [layer], len(run.lines), made["face_top"], style, route, run.job, sha_)
               for name, layer in stickers.items()}
    picture = os.path.join(run.out, f"{route}.panels.png")
    if family == "pair":
        grounds[guests[1]] = grounds[guests[0]]
    paint(people, made, run.renderer, stickers, {name: g["colors"] for name, g in grounds.items()}, blur, run.rules, picture,
          divider_indices=({1} if row[-1] == run.host_name else {0}) if family == "pair" else None)
    faces = {name: place["face"] for name, place in made["places"].items()}
    measured = [faces[name] for name in guests]
    run.people_faces[route], run.grounds[route] = faces, grounds
    run.sides[route] = "left" if row[0] == run.host_name else "right" if row[-1] == run.host_name else "middle"
    run.plans[route] = {"face_px": made["face_px"], "face_top": made["face_top"], "row": row,
                        "row_why": run.row_why if row == run.row else f"{run.row_why}排不下（{why}），改排"}
    spec = {"id": route, "frame": picture, "crop": [0, 0, FRAME_W], "saturation": 1.0, "composition": "collab_pair" if family == "pair" else "split_panels",
            **run.column.text(route, run.lines), "face": protect(list(faces.values())), "keep": measured}
    spec["lines"] = anchor(spec["lines"], run.speakers, faces, run.host_name)
    note += ("；嘉宾不在源画面里，用素材包的官方图（只缩放、摆放、按格子裁边，没有改画）："
             + "、".join(f"{g['name']}（{g['kind']}）" for g in run.pack)
             + (f"；两格，主播一格、两位嘉宾共一格，从左到右 {'、'.join(row)}，脸同样大小" if family == "pair"
                else f"；分屏每人一格，从左到右 {'、'.join(row)}，各人脸同样大小；格子的底色、斜边和白线是排版"))
    run.specs.append((route, spec, note, main_emote, {"kind": "picture", "src": picture, "face": faces[run.host_name]},
                      measured if len(measured) > 1 else measured[0]))
    return None


def cutout(run, host_face, figures):
    """The cut-out route with pack guests: the host's cut-out figure in her panel."""
    located = host_face(run.job, run.cut_png, run.out)
    fig = next((f for f in figures(run.cut_png, run.renderer, raw=True)
                if located and f[0] <= (located[0] + located[2]) / 2 <= f[2] and f[1] <= (located[1] + located[3]) / 2 <= f[3]), None)
    if fig is None:
        return run.fail("cutout", "HOST_FACE_NOT_FOUND" if located is None else "HOST_FACE_NOT_ON_HER_FIGURE")
    return cover(run, "cutout", {"kind": "layer", "src": run.cut_png, "cutout": "auto", "fig": fig, "face": located},
                 "CPA 生图只去掉截图背景（人物不改）")


def drawn(run, sticker_png, face):
    """The sticker route with pack guests: the drawn sticker of the host in her panel (`face`: her face in it, or None)."""
    if not face:
        return run.fail("ai_sticker", "STICKER_HOST_FACE_UNAVAILABLE")
    return cover(run, "ai_sticker", layer_host(run, sticker_png, "auto", face), f"CPA 生图画的这一刻 Q 版贴纸当{HOST}")


def row_facts(run, route, row):
    """What a candidate's row says about the pack: pictures the host did not have, and who stands on this candidate
    (who they are is the manifest's word, not a reading of the picture)."""
    if run.pack_dropped:
        row["guest_assets_dropped"] = run.pack_dropped
    if run.portrait_skipped.get(route):
        row["portrait_options_skipped"] = run.portrait_skipped[route]
    if route in run.people_faces:
        row["guests_on_cover"] = [{"name": g["name"], "asset_id": g["asset_id"], "sha256": g["sha256"], "source_url": g["source_url"],
                                   "face": run.people_faces[route][g["name"]], "face_from": g["face_from"]} for g in run.people_used.get(route, run.pack)]
        row["expected_guest_count"] = len(run.pack)
        row["panel_grounds"] = run.grounds[route]
