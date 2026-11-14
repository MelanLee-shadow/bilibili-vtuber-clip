#!/usr/bin/env python3
"""Download the fan emote collection from 沙按钮 (https://lu-91015.github.io/shadowlee.github.io/) and write a numbered
contact sheet for picking. usage: fetch_shabutton.py --out DIR"""
import argparse, os, re, urllib.parse, urllib.request
from PIL import Image, ImageDraw, ImageFont
SITE = 'https://lu-91015.github.io'

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--out', required=True); a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    html = urllib.request.urlopen(urllib.request.Request(SITE + '/shadowlee.github.io/', headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read().decode('utf-8')
    urls = sorted(set(re.findall(r'src="(/shadowlee\.github\.io/assets/images/emotes/[^"]+)"', html)))
    files = []
    for u in urls:
        name = urllib.parse.unquote(u.rsplit('/', 1)[1]); path = os.path.join(a.out, name)
        if not os.path.exists(path):
            open(path, 'wb').write(urllib.request.urlopen(urllib.request.Request(SITE + u, headers={'User-Agent': 'Mozilla/5.0'}), timeout=30).read())
        files.append(path)
    tw, cols = 180, 10; rows = (len(files) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * (tw + 6) + 6, rows * (tw + 30) + 6), 'white'); d = ImageDraw.Draw(sheet)
    try: f = ImageFont.truetype('/System/Library/Fonts/Hiragino Sans GB.ttc', 16)
    except OSError: f = ImageFont.load_default()
    with open(os.path.join(a.out, 'index.txt'), 'w', encoding='utf-8') as idx:
        for i, p in enumerate(files):
            idx.write(f'{i:03d}\t{os.path.basename(p)}\n')
            try: im = Image.open(p); im.seek(0); im = im.convert('RGBA')
            except Exception: continue
            im.thumbnail((tw, tw)); bg = Image.new('RGB', (tw, tw), (235, 235, 235)); bg.paste(im, ((tw - im.width) // 2, (tw - im.height) // 2), im)
            x = 6 + (i % cols) * (tw + 6); y = 6 + (i // cols) * (tw + 30); sheet.paste(bg, (x, y))
            d.text((x, y + tw + 2), f'{i:03d} ' + os.path.basename(p)[:12], font=f, fill='black')
    sheet.save(os.path.join(a.out, 'contact.jpg'), quality=82); print(len(files), 'emotes ->', a.out)

if __name__ == '__main__':
    main()
