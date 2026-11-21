"""A screenshot with a dead panel is not a cover (step 6, 70-cover).

2026-11-21: a clip cut from a stretch where the stream showed a screen-share window that captured nothing; the cropped
screenshot was one third solid black beside her, and it passed every check (the judge's clean_layers is about pasted
layers).  A dead panel is a block of adjacent columns, or rows, that are dark almost from end to end: a window with
nothing in it, a black bar.  A dark scene is not one (its dark pixels do not line up into whole columns): of 62 cropped
source frames the widest such block was 3.1 % of the width, and 30.7 % for this one."""
from PIL import Image

SIDE = 192     # the picture is measured on a copy this wide


def widest_dead_block(frame_path, crop, rules):
    """The widest dead block of the cropped frame as a share of its width (columns) or height (rows), whichever is
    larger.  `crop` is the cover's [x, y, width] of a 16:9 window, or None for the whole frame."""
    with Image.open(frame_path) as raw:
        im = raw.convert("RGB")
    if crop:
        x, y, w = (int(v) for v in crop[:3])
        im = im.crop((x, y, x + w, y + round(w * 9 / 16)))
    w, h = SIDE, round(SIDE * im.height / im.width)
    px = im.resize((w, h), Image.BILINEAR).load()
    dark = [[max(px[x, y]) <= rules["dead_panel_dark_level"] for x in range(w)] for y in range(h)]
    need = rules["dead_panel_line_frac"]

    def longest(flags):
        best = run = 0
        for flag in flags:
            run = run + 1 if flag else 0
            best = max(best, run)
        return best

    columns = longest(sum(dark[y][x] for y in range(h)) >= need * h for x in range(w)) / w
    rows = longest(sum(dark[y]) >= need * w for y in range(h)) / h
    return round(max(columns, rows), 3)
