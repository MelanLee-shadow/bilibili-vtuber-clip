#!/usr/bin/env python3
"""Ask CPA images/edits for a TEXT-FREE cover background redrawn from a real frame of the clip.
The text is added afterwards by render_cover.py; this script only builds the prompt from a small brief and calls CPA.

usage: redraw_background.py BRIEF.json --reference FRAME.png --out OUT.png [--mode scene|cutout|sticker] [--dry-run]
       cutout and sticker come back on flat green; render_cover.py keys it out with "cutout": "green"
env:   CPA_BASE_URL, CPA_API_KEY (production keeps them in /opt/bilive/autoslice/cpa.env on OCI3)

BRIEF.json (English is fine for the model; keep every item true to the final subtitles):
{
  "expression": "smug playful grin, eyes open and sparkling",       # from the hook line's emotion
  "action": "raising one index finger as if announcing a fact",      # what she does while saying it
  "others": "three tiny round fluffy chibi bears (her fans, 'kmx') sitting on her desk like desk pets",  # or "" ; real
             # people only with their own reference image, otherwise as a silhouette or back view
  "setting": "cozy library study room like the reference background",
  "text_side": "left",                                               # left | right | top : area kept empty for the title (scene mode)
  "style": "soft hand-drawn watercolor picture-book illustration"    # optional, sticker mode; default = her official emote style
}
Writes OUT.png and OUT.request.json (prompt, model, reference sha256; never the key).
"""
import argparse, hashlib, json, os, sys, time, uuid, base64, urllib.request, urllib.error

IDENTITY = ("Li Dousha is a cute anime VTuber. The reference image is authoritative for her visible appearance: preserve the "
            "actual hairstyle, head and face features, clothing, accessories and any headwear shown there. Her default identity "
            "is WHITE hair with small PANDA EARS growing from the top of her head, not a panda hood or hat; use those ears only "
            "when they are visible in the reference. Never add a hood, hat, headwear, outfit change or accessory the reference "
            "does not show.")
NO_TEXT = ("CRITICAL - render ABSOLUTELY NO text of any kind: no letters, words, Chinese/Japanese/English characters, numbers, "
           "watermark, logo, UI, chat box, subtitles or speech-bubble text anywhere. The title is added separately afterwards.")
SIDE = {   # text_side -> (where she goes, where the text goes)
    'left': ('the RIGHT 45% of the image width (her face between 55% and 87% of the width)', 'LEFT 55%'),
    'right': ('the LEFT 45% of the image width (her face between 13% and 45% of the width)', 'RIGHT 55%'),
    'top': ('the LOWER 60% of the image, centered (her face below 40% of the height)', 'TOP 40%')}

GREEN = ("BACKGROUND: one perfectly flat, uniform pure green (#00FF00) color everywhere around the character - no gradient, "
         "no shadow, no scenery, no floor - so it can be keyed out. Do not use green anywhere on the character.")

def build_prompt(b, mode='scene'):
    if mode == 'cutout':      # faithful cut-out of the live model from a stream frame
        return "\n".join([
            "Remove the background of this livestream screenshot. Keep the character EXACTLY as she is: same pose, expression, "
            "face, hair, outfit, accessories, colors and rendering - do not redraw, restyle or improve her. Remove chat boxes, UI, "
            "subtitles, the desk and the room.", GREEN, NO_TEXT])
    if mode == 'sticker':     # new chibi sticker of this moment, in the style of her official emotes
        return "\n".join([
            "Draw a single chibi (super-deformed, big head) sticker of the character in the reference image, in this style: "
            + b.get('style', "her official LINE-style emote stickers: thick clean dark outlines, flat cel shading, big expressive eyes") + ".",
            "IDENTITY: " + IDENTITY,
            f"EXPRESSION AND ACTION: {b['expression']}; {b['action']}. Exaggerate the emotion like a meme sticker. "
            "She must NEVER stick her tongue out.",
            "Show her from the waist up, centered, filling most of the image, with a thick white sticker border around her.",
            GREEN, NO_TEXT])
    face_side, text_side = SIDE[b.get('text_side', 'left')]
    parts = [
        "Redraw the character in the reference image as a bold, high-quality 16:9 anime cover illustration for a Bilibili "
        "livestream clip. Clean crisp linework, vivid shading, high resolution.",
        "IDENTITY: " + IDENTITY,
        f"EXPRESSION AND ACTION (must read instantly at thumbnail size): {b['expression']}; {b['action']}. "
        "Make the emotion clear and lively. She must NEVER stick her tongue out; never provocative or sexy.",
        f"FRAMING (compose it like a video thumbnail): a large close-up - her face and hair fill about half of the image "
        f"height. She and everything important stay inside {face_side}. Nothing important in the outer 13% of the width.",
    ]
    if b.get('others'):
        parts.append(f"OTHERS IN THE SCENE: {b['others']}. Keep them clearly visible but secondary to her; she stays the main subject.")
    parts.append(f"SETTING: {b.get('setting', 'a simple bright background matching the reference')}. The {text_side} of the "
                 "image must contain ONLY soft, slightly blurred background - no face, hair, hands, characters or objects there; "
                 "the title text goes there.")
    parts.append(NO_TEXT)
    return "\n".join(parts)

def multipart(fields, files):
    boundary = uuid.uuid4().hex; out = []
    for k, v in fields.items():
        out += [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()]
    for k, (name, data, ctype) in files:
        out += [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode(), data, b'\r\n']
    out.append(f'--{boundary}--\r\n'.encode())
    return b''.join(out), f'multipart/form-data; boundary={boundary}'

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('brief'); ap.add_argument('--reference', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--model', default='gpt-image-2.6-sunburst')
    ap.add_argument('--size', default='1920x1088'); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--mode', default='scene', choices=['scene', 'cutout', 'sticker'],
                    help='scene: full redraw background; cutout: remove the background of a frame (faithful); sticker: chibi emote-style sticker of this moment')
    a = ap.parse_args()
    brief = json.load(open(a.brief, encoding='utf-8')); prompt = build_prompt(brief, a.mode)
    ref = open(a.reference, 'rb').read()
    record = dict(model=a.model, size=a.size, prompt=prompt, brief=brief, reference=os.path.basename(a.reference),
                  reference_sha256=hashlib.sha256(ref).hexdigest())
    json.dump(record, open(a.out + '.request.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    if a.dry_run:
        print(prompt); return
    base, key = os.environ.get('CPA_BASE_URL', '').rstrip('/'), os.environ.get('CPA_API_KEY', '')
    if not base or not key: raise SystemExit('CPA_BASE_URL / CPA_API_KEY not set (source cpa.env first)')
    body, ctype = multipart({'model': a.model, 'prompt': prompt, 'size': a.size}, [('image', ('reference.png', ref, 'image/png'))])
    req = urllib.request.Request(f'{base}/images/edits', data=body, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': ctype,
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'})   # CPA sits behind Cloudflare
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=600) as r: payload = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise SystemExit(f'CPA HTTP {e.code}: {e.read().decode("utf-8", "replace")[:400]}')
    item = (payload.get('data') or [{}])[0]
    if item.get('b64_json'): img = base64.b64decode(item['b64_json'])
    elif item.get('url'): img = urllib.request.urlopen(urllib.request.Request(item['url'], headers={'User-Agent': 'Mozilla/5.0'}), timeout=120).read()
    else: raise SystemExit(f'CPA returned no image: {str(payload)[:300]}')
    open(a.out, 'wb').write(img)
    record.update(elapsed_seconds=round(time.time() - t0, 1), output_sha256=hashlib.sha256(img).hexdigest(),
                  reported_model=payload.get('model'), usage=payload.get('usage'))
    json.dump(record, open(a.out + '.request.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(a.out)

if __name__ == '__main__':
    main()
