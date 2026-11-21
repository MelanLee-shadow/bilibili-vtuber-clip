"""The official emote as a clean layer (step 6, 70-cover).

The library gives a small transparent original and a redrawn HD picture on a flat backdrop.  Taking the small one's alpha
as the HD picture's mask (the renderer's `mask:`) left two defects the judge kept vetoing (2026-11-21: 19 of 34 emote
candidates): the redraw does not sit exactly where the original did, so backdrop showed inside the mask or the figure was
cut by it; and most originals are cut by their own square frame (an ear, the hair), so pasted inside the canvas the cut
is a straight edge.

Here the HD picture's own edge is used: the backdrop is flooded from where the small original is transparent, through
pixels of the backdrop's own flat colours, and stops at the sticker's outline.  A side where the figure runs into the frame is
put on the canvas edge by the layout when it is a real cut and the cover has no other layer beside it (flush_side), and
faded out otherwise, instead of ending in a straight line inside the canvas (the bottom is left: the layouts stand the
figure on the canvas's bottom edge).  The work is done once per (pictures, numbers) and reused."""
import hashlib
import json
import os
from collections import deque

from PIL import Image, ImageChops, ImageFilter

WORK = 512            # the flood runs on a copy this large; the alpha is scaled back to the picture


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _colour_key(px):
    return (px[0] >> 4, px[1] >> 4, px[2] >> 4)       # 16 levels a channel: a flat backdrop is a handful of keys


def backdrop_alpha(hd, small_alpha, rules):
    """The HD picture's alpha (L, hd's size): everything the backdrop flood does not reach.  Returns (alpha, facts);
    alpha is None when the picture has no backdrop to read or every flood ate into the figure."""
    w, h = hd.size
    rgb = hd.convert("RGB").resize((WORK, WORK), Image.LANCZOS)
    inside = small_alpha.resize((WORK, WORK), Image.LANCZOS).point(lambda v: 255 if v > 128 else 0)
    grow = 2 * round(WORK * rules["emote_seed_margin_frac"]) + 1
    outside = inside.filter(ImageFilter.MaxFilter(grow)).point(lambda v: 255 - v)       # surely not the figure
    px, po = rgb.load(), outside.load()
    counts = {}
    for y in range(WORK):
        for x in range(WORK):
            if po[x, y]:
                key = _colour_key(px[x, y])
                counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values())
    if total < WORK * WORK * rules["emote_backdrop_min_frac"]:
        return None, {"backdrop": "too little of the picture is outside the original"}
    was = 1 - inside.histogram()[0] / (WORK * WORK)
    tried = []
    # the most thorough reading of "backdrop colour" first; one that eats into the figure (a redraw whose figure spills
    # past the original puts figure colours outside it) is dropped for the next, stricter one
    for share, spread in rules["emote_backdrop_tries"]:
        main = [key for key, n in counts.items() if n >= total * share]
        near = range(-spread, spread + 1)
        keep = {(r + dr, g + dg, b + db) for r, g, b in main for dr in near for dg in near for db in near}
        flooded = Image.new("L", (WORK, WORK), 0)
        pf = flooded.load()
        queue = deque((x, y) for y in range(WORK) for x in range(WORK) if po[x, y] and _colour_key(px[x, y]) in keep)
        while queue:
            x, y = queue.popleft()
            if pf[x, y] or _colour_key(px[x, y]) not in keep:
                continue
            pf[x, y] = 255
            if x > 0:
                queue.append((x - 1, y))
            if x < WORK - 1:
                queue.append((x + 1, y))
            if y > 0:
                queue.append((x, y - 1))
            if y < WORK - 1:
                queue.append((x, y + 1))
        alpha = flooded.point(lambda v: 255 - v).filter(ImageFilter.MinFilter(3))
        kept = 1 - alpha.histogram()[0] / (WORK * WORK)
        tried.append({"share": share, "spread": spread, "figure_frac": round(kept, 3)})
        edge = alpha.load()                 # still a rectangle: the backdrop was not this colour, nothing was removed
        boxed = all(sum(edge[x, y] > 0 for x, y in line) >= WORK * rules["emote_boxed_side_frac"] for line in (
            [(0, y) for y in range(WORK)], [(WORK - 1, y) for y in range(WORK)], [(x, 0) for x in range(WORK)]))
        tried[-1]["boxed"] = boxed
        if kept >= was * rules["emote_keep_min_ratio"] and not boxed:
            alpha = alpha.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.2)).point(
                lambda v: 0 if v < 70 else 255 if v > 190 else v)
            return alpha, {"original_frac": round(was, 3), "tried": tried}
    return None, {"original_frac": round(was, 3), "tried": tried, "backdrop": "every flood ate into the figure or removed nothing"}


def cut_sides(alpha, rules):
    """The sides (L, R, T) where the figure runs into the picture's frame: the share of that border that is opaque."""
    w, h = alpha.size
    px = alpha.load()
    share = {"L": sum(px[0, y] > 128 for y in range(h)) / h, "R": sum(px[w - 1, y] > 128 for y in range(h)) / h,
             "T": sum(px[x, 0] > 128 for x in range(w)) / w}
    return {side: round(v, 3) for side, v in share.items() if v >= rules["emote_cut_side_min_frac"]}


def faded(alpha, sides, rules):
    """`alpha` with a ramp from 0 at each cut side to full at `emote_fade_frac` of the picture in."""
    w, h = alpha.size
    ramp = Image.new("L", (w, h), 255)
    pr = ramp.load()
    depth = {"L": round(w * rules["emote_fade_frac"]), "R": round(w * rules["emote_fade_frac"]), "T": round(h * rules["emote_fade_frac"])}
    for side in sides:
        d = max(1, depth[side])
        for i in range(d):
            value = round(255 * i / d)
            if side in ("L", "R"):
                x = i if side == "L" else w - 1 - i
                for y in range(h):
                    pr[x, y] = min(pr[x, y], value)
            else:
                for x in range(w):
                    pr[x, i] = min(pr[x, i], value)
    return ImageChops.multiply(alpha, ramp)


def read(hd_path, mask_path, out_dir, rules):
    """The emote's alpha at the HD picture's own edge, before any fade, as a file, with what was found: (path, facts).
    Worked out once per (pictures, numbers); facts["cut_sides"] are the sides where the figure runs into the frame."""
    numbers = {k: rules[k] for k in sorted(rules) if k.startswith("emote_")}
    key = hashlib.sha256(json.dumps([_sha(hd_path), _sha(mask_path), numbers], sort_keys=True).encode()).hexdigest()[:16]
    out = os.path.join(out_dir, f"emote-alpha-{key}.png")
    if os.path.exists(out) and os.path.exists(out + ".json"):
        with open(out + ".json", encoding="utf-8") as fh:
            return out, dict(json.load(fh), reused=True)
    hd = Image.open(hd_path).convert("RGB")
    small_alpha = Image.open(mask_path).convert("RGBA").getchannel("A")
    alpha, facts = backdrop_alpha(hd, small_alpha, rules)
    if alpha is None:                    # no backdrop to read: the original's alpha, as before
        alpha = small_alpha.resize(hd.size, Image.LANCZOS).filter(ImageFilter.GaussianBlur(1.2)).point(
            lambda v: 0 if v < 70 else 255 if v > 190 else v)
    facts["cut_sides"] = cut_sides(alpha, rules)
    alpha.save(out + ".tmp.png")
    os.replace(out + ".tmp.png", out)
    with open(out + ".json", "w", encoding="utf-8") as fh:
        json.dump(facts, fh, ensure_ascii=False)
    return out, facts


def flush_side(sides, rules):
    """The side (L or R) to put on the canvas edge: where the frame cuts the figure most, when that is a real cut (an ear,
    half the hair) and not a strand.  A cut that lies on the canvas edge is no cut at all; a fade inside the canvas
    still reads as one (2026-11-21: emote 05's cut ear, faded, was vetoed by three readings of three)."""
    lateral = {side: share for side, share in sides.items() if side in ("L", "R") and share >= rules["emote_flush_min_frac"]}
    return max(lateral, key=lambda side: lateral[side]) if lateral else None


def layer_for(hd_path, mask_path, out_dir, rules, may_flush=False):
    """The emote as an RGBA picture of the HD picture's size, ready for the renderer's `alpha` cut-out: (path, side,
    facts).  `side` is the cut side the layout must put on the canvas edge (None: none, or `may_flush` is false because
    the cover has another layer beside it); every other cut side is faded."""
    alpha_path, facts = read(hd_path, mask_path, out_dir, rules)
    sides = facts["cut_sides"]
    side = flush_side(sides, rules) if may_flush else None
    fade = sorted(s for s in sides if s != side)
    out = alpha_path.replace("emote-alpha-", "emote-layer-")[:-4] + f"-fade{''.join(fade) or '0'}.png"
    if not os.path.exists(out):
        alpha = Image.open(alpha_path).convert("L")
        layer = Image.open(hd_path).convert("RGBA")
        layer.putalpha(faded(alpha, fade, rules) if fade else alpha)
        layer.save(out + ".tmp.png")
        os.replace(out + ".tmp.png", out)
    return out, side, facts
