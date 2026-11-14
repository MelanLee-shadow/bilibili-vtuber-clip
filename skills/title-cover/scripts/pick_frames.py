#!/usr/bin/env python3
"""Extract candidate cover frames around a subtitle moment and tile them into one labelled contact sheet.

usage: pick_frames.py VIDEO --at MM:SS[.ms] [--record CLIP.record.json | --offset-ms N] [--span 8] [--fps 1] --out DIR
--at is a time on the SUBTITLE timeline. Finished talk clips start with a branding intro (about 6 s) that the .srt does not
include; --record reads burned_preview.branding_intro.intro_offset_ms and shifts the cut so frames line up with the line.
Writes DIR/f_<seconds>.png (full resolution) and DIR/contact.jpg; file names and tile labels use SUBTITLE time.
"""
import argparse, os, subprocess, glob, re
from PIL import Image, ImageDraw, ImageFont

def secs(t):
    parts = [float(p) for p in t.split(':')]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video'); ap.add_argument('--at', required=True); ap.add_argument('--span', type=float, default=8)
    ap.add_argument('--fps', type=float, default=1); ap.add_argument('--out', required=True)
    ap.add_argument('--record'); ap.add_argument('--offset-ms', type=int)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    off = a.offset_ms
    if off is None and a.record:
        import json
        off = (json.load(open(a.record, encoding='utf-8')).get('burned_preview', {}).get('branding_intro', {}) or {}).get('intro_offset_ms')
        if off is None: print('record has no intro_offset_ms; assuming the video has no intro', flush=True)
    off = (off or 0) / 1000
    start = max(0, secs(a.at) + off - a.span)
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-t', f'{2 * a.span:.3f}', '-i', a.video,
                    '-vf', f'fps={a.fps}', os.path.join(a.out, 'raw_%03d.png')], check=True)
    frames = []
    for i, p in enumerate(sorted(glob.glob(os.path.join(a.out, 'raw_*.png')))):
        t = start + i / a.fps - off    # back on the subtitle timeline, so names/labels match the .srt
        q = os.path.join(a.out, f'f_{t:07.2f}.png'); os.replace(p, q); frames.append((t, q))
    if not frames:
        raise SystemExit('no frames extracted (check --at against the video length)')
    tw = 480; th = round(tw * Image.open(frames[0][1]).height / Image.open(frames[0][1]).width); cols = 4
    rows = (len(frames) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * (tw + 6) + 6, rows * (th + 6) + 6), 'white'); d = ImageDraw.Draw(sheet)
    try: font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 26)
    except OSError: font = ImageFont.load_default()
    for k, (t, q) in enumerate(frames):
        x = 6 + (k % cols) * (tw + 6); y = 6 + (k // cols) * (th + 6)
        sheet.paste(Image.open(q).convert('RGB').resize((tw, th)), (x, y))
        m, s = divmod(t, 60); d.rectangle((x, y, x + 120, y + 32), fill='black'); d.text((x + 6, y + 2), f'{int(m):02d}:{s:05.2f}', font=font, fill='yellow')
    sheet.save(os.path.join(a.out, 'contact.jpg'), quality=85)
    print(os.path.join(a.out, 'contact.jpg')); [print(q) for _, q in frames]

if __name__ == '__main__':
    main()
