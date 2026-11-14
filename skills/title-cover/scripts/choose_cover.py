#!/usr/bin/env python3
"""Score candidate covers for one clip and pick the route. Renders every candidate with render_cover.py, measures what can be
measured, combines it with the judge's notes from the scorecard, and writes a comparison sheet.

usage: choose_cover.py SCORECARD.json [--font PATH] [--styles PATH] --out-dir DIR
       choose_cover.py SCORECARD.json --suggest-emote --emote-library PATH --emote-dir DIR --out-dir DIR
  --styles: lidousha/cover_style_rotation.json (or env LIDOUSHA_COVER_STYLES). Required to render; there is no built-in table.
  --suggest-emote: do not render. Pick the official emote whose manifest keywords match the cover lines + quotes, write its
  spec to DIR and print a ready candidate row for the scorecard. "No match" is printed as a reason, never an error.

SCORECARD.json:
{
  "clip": "8/17 女友感",
  "participants": ["李豆沙", "kmx"],          # everyone the story is about (from step 1); kmx counts when the hook is about them
  "recent": {"routes": ["screenshot", "emote"], "emotes": ["13_李豆沙_得意.png"]},   # last 5 published covers, oldest first;
                                                # emotes = their MAIN emotes (kmx's 只是熊猫 as a side layer does not count)
  "cover_lines": ["女友感", "只是熊猫"],       # --suggest-emote only: the fixed cover lines (same as every candidate's spec)
  "quotes": ["kmx：只是熊猫"],                 # --suggest-emote only: final-subtitle quotes of the participants
  "candidates": [
    {"name": "A 官方表情包拼贴", "route": "emote", "spec": "spec_A.json",
     "present": ["李豆沙", "kmx"],             # who is visibly represented (real screenshot, official emote/立绘, cut-out)
     "emotes": ["13_李豆沙_得意.png"],          # main emote of this candidate, if any
     "hook_fit": 2, "hook_note": "得意脸 = 'kmx是桌宠'的得意口气",   # 2 picture shows the cover line's emotion/moment, 1 neutral, 0 contradicts
     "fabricated": false, "identity_ok": true,
     "clean_layers": true}                     # every pasted layer is a whole figure with clean edges (no square crop, no cut-off head)
  ]
}
route: screenshot | screenshot_collage | emote | cutout | ai_sticker | ai_scene

Anti-gaming: all candidates must carry the same cover lines (fixed before scoring), so text size cannot be bought by
rewording; the face-height floor stops shrinking the person; the size score stops at 200px.

Score (higher is better), research-backed parts weighted highest:
  people  3 x P   P = 2 all participants visible, 1 some others missing but she is there, 0 she is missing   (210 covers: 多人同框 17/5)
  text    2 x T   T = 2 punchline >= 200px, 1 150-199px, 0 120-149px; nothing extra above 200px              (粗描边大字 13/2, 大字 7/1)
  hook    1 x H   judge's hook_fit (0-2)                                                                    (editorial: picture carries the line)
  real    1 x C   C = 2 screenshot / official or fan emote, 1 cut-out by AI, 0 AI-drawn                    (no identity drift, no cost)
  fresh   1 x V   V = 1 if the route differs from the previous cover and the main emote is not among the last 5, else 0
Gates (candidate dropped): fabricated, identity not ok, a layer that is not a clean whole figure, face < 25% of the height,
       main emote already used in the last five covers, render refused (text < 120px, text on face or on a keep box, missing glyph ...).
The chosen candidate is the highest total; a manual override must be written down with its score gap and reason.
"""
import argparse, importlib.util, json, os, re, sys, io, contextlib
from PIL import Image, ImageDraw, ImageFont

REAL = {'screenshot': 2, 'screenshot_collage': 2, 'emote': 2, 'cutout': 1, 'ai_sticker': 0, 'ai_scene': 0}
# Keyword rule for --suggest-emote. Same body as src/autoslice/cover_emote.py (emote_keywords / pick_emote_for_text);
# the two copies are kept in step by hand because the skill cannot import the repo.
EMOTE_KEYWORD_SPLIT = re.compile(r"[/、，,；;：:（）()“”\"'\s]+")

def load_renderer():
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location('render_cover', os.path.join(here, 'render_cover.py'))
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def emote_keywords(entry):
    parts = [entry.get('label') or '', *EMOTE_KEYWORD_SPLIT.split(entry.get('use_when') or '')]
    return tuple(dict.fromkeys(p for p in parts if len(p) >= 2))

def pick_emote_for_text(text, entries, excluded=()):
    """Pick the lidousha sticker whose keywords occur most often in text (cover lines + quotes). The panda creature (25)
    is never the main emote; excluded holds ids used as main emote recently. Ties keep manifest order. No match returns
    (None, reason): a record, not a gate."""
    if not entries:
        return None, 'EMOTE_LIBRARY_DISABLED'
    blocked = set(excluded); best = None
    for entry in entries:
        subject = entry.get('subject') if entry.get('subject') in ('lidousha', 'panda_creature') else 'lidousha'
        if subject != 'lidousha' or entry.get('id') in blocked:
            continue
        matched = [key for key in emote_keywords(entry) if key in text]
        if matched and (best is None or len(matched) > len(best[1])):
            best = (entry, matched)
    if best is None:
        return None, 'NO_EMOTION_MATCH: 官方表情包清单中没有与这段原话情绪相符的条目'
    return best[0]['id'], 'MATCHED: ' + '、'.join(best[1])

def suggest_emote(card, a):
    with open(a.emote_library, encoding='utf-8') as fh:
        entries = json.load(fh)['emotes']
    by_id = {e['id']: e for e in entries}
    text = ' '.join([*card.get('cover_lines', []), *card.get('quotes', [])])
    excluded = {name.split('_', 1)[0] for name in (card.get('recent') or {}).get('emotes', [])}
    emote_id, reason = pick_emote_for_text(text, entries, excluded)
    if emote_id is None:
        print(json.dumps({'status': 'SKIPPED', 'reason': reason}, ensure_ascii=False, indent=1)); return
    main = by_id[emote_id]
    def layer(entry, x, y, h):
        return {'src': os.path.join(a.emote_dir, entry['hd_file']),
                'cutout': 'mask:' + os.path.join(a.emote_dir, entry['file']), 'x': x, 'y': y, 'h': h}
    # emotes = MAIN emote only; the kmx panda is a side layer and does not count (same rule as recent.emotes).
    stickers, present, emotes = [layer(main, 980, 110, 860)], ['李豆沙'], [main['file']]
    if 'kmx' in card['participants'] and '25' in by_id:
        stickers.append(layer(by_id['25'], 1560, 700, 300)); present.append('kmx')
    spec = {'id': 'auto-emote-' + emote_id, 'bg': ['#dff0ff', '#ffe3f1'], 'stickers': stickers,
            'face': [1195, 196, 1625, 626], 'keep': [], 'layout': 'auto',
            'lines': [{'text': t} for t in card['cover_lines']]}
    os.makedirs(a.out_dir, exist_ok=True)
    spec_path = os.path.join(a.out_dir, f'auto-emote-{emote_id}.spec.json')
    with open(spec_path, 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=1)
    candidate = {'name': f'E 官方表情包（自动 {emote_id}）', 'route': 'emote', 'spec': spec_path, 'present': present,
                 'emotes': emotes, 'hook_fit': 1, 'hook_note': f'自动补充（{reason}），贴合分看图后改',
                 'fabricated': False, 'identity_ok': True, 'clean_layers': True}
    missing = [p for p in card['participants'] if p not in present]
    out = {'status': 'ADDED', 'reason': reason, 'spec': spec_path, 'candidate': candidate, 'missing': missing}
    if missing:
        out['note'] = '嘉宾不在本自动候选内：需人工加一张同帧截图层（stream 来源 cutout none + crop），不能 AI 抠图'
    print(json.dumps(out, ensure_ascii=False, indent=1))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('scorecard'); ap.add_argument('--font', default=os.environ.get('LIDOUSHA_COVER_FONT'))
    ap.add_argument('--styles', default=os.environ.get('LIDOUSHA_COVER_STYLES'))
    ap.add_argument('--suggest-emote', action='store_true'); ap.add_argument('--emote-library'); ap.add_argument('--emote-dir', default=os.environ.get('LIDOUSHA_EMOTE_DIR'))
    ap.add_argument('--out-dir', required=True); a = ap.parse_args(); os.makedirs(a.out_dir, exist_ok=True)
    rc = load_renderer(); card = json.load(open(a.scorecard, encoding='utf-8')); base = os.path.dirname(os.path.abspath(a.scorecard))
    if a.suggest_emote:
        if not a.emote_library or not a.emote_dir:
            raise SystemExit('--suggest-emote 需要 --emote-library 与 --emote-dir（或 env LIDOUSHA_EMOTE_DIR）')
        return suggest_emote(card, a)
    if not a.styles:
        raise SystemExit('need --styles or LIDOUSHA_COVER_STYLES pointing at lidousha/cover_style_rotation.json (no built-in default)')
    # The renderer draws from the installed rotation table; without this call every candidate fails with an empty palette.
    rc.install_table(rc.load_styles(a.styles))
    parts = card['participants']; recent = card.get('recent', {}); results = []
    # anti-gaming 1: the cover text is fixed in step 3 BEFORE routes are compared; every candidate must carry the same lines,
    # so the size score only measures how much room the picture leaves, never a reworded / shortened text.
    texts = {json.dumps([ln['text'] for ln in json.load(open(os.path.join(base, c['spec']), encoding='utf-8'))['lines']], ensure_ascii=False)
             for c in card['candidates']}
    if len(texts) > 1:
        raise SystemExit(f'候选的封面字不一致 {sorted(texts)}：封面字要在第 3 步定稿后再比做法，改字要回第 3 步重新核对')
    for c in card['candidates']:
        r = dict(name=c['name'], route=c['route'])
        spec = json.load(open(os.path.join(base, c['spec']), encoding='utf-8'))
        if c.get('fabricated') or not c.get('identity_ok', True) or not c.get('clean_layers', True):
            why = '编造事实' if c.get('fabricated') else '人物走样' if not c.get('identity_ok', True) else '图层不完整：' + c.get('layer_note', '')
            r.update(status='DROP', why=why); results.append(r); continue
        main_emote = (c.get('emotes') or [None])[0]
        if main_emote and main_emote in recent.get('emotes', []):
            r.update(status='DROP', why='主表情包近 5 条已用过：' + main_emote); results.append(r); continue
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err): img, rec, _selection = rc.render(spec, a.font)
        except SystemExit as e:
            r.update(status='DROP', why=f'渲染被拦：{e}'); results.append(r); continue
        path = os.path.join(a.out_dir, f"{spec.get('id', 'c')}.png"); img.save(path)
        punch = rec[-1]['size']; present = set(c.get('present', []))
        # anti-gaming 2: the person may not be shrunk to make room for text. Main face >= 25% of the frame height
        # (a reaction inset counts as her face; the watched picture is the subject there).
        fh = spec['face'][3] - spec['face'][1]
        if fh < 0.25 * rc.H:
            r.update(status='DROP', why=f'人物太小：脸高 {fh}px，不到画面高度的 25%'); results.append(r); continue
        P = 2 if set(parts) <= present else (1 if '李豆沙' in present else 0)
        T = 2 if punch >= 200 else 1 if punch >= 150 else 0
        H = int(c.get('hook_fit', 1)); C = REAL[c['route']]
        prev = (recent.get('routes') or [None])[-1]
        V = 0 if (c['route'] == prev or set(c.get('emotes', [])) & set(recent.get('emotes', []))) else 1
        score = 3 * P + 2 * T + H + C + V
        r.update(status='OK', score=score, P=P, T=T, H=H, C=C, V=V, punch=punch, layout=rec[-1].get('layout'), img=path,
                 note=c.get('hook_note', ''), missing=sorted(set(parts) - present))
        results.append(r)
    ok = sorted([r for r in results if r['status'] == 'OK'], key=lambda r: (-r['score'], -r['C']))
    for r in results:
        if r['status'] == 'OK':
            print(f"{r['score']:2d}  {r['name']:<22} 人物{r['P']}×3 字号{r['T']}×2({r['punch']}px) 贴合{r['H']} 真实{r['C']} 新鲜{r['V']}"
                  + (f"  缺：{'、'.join(r['missing'])}" if r['missing'] else ''))
        else:
            print(f" -  {r['name']:<22} 淘汰：{r['why']}")
    if ok: print('选用：', ok[0]['name'])
    # comparison sheet
    f = ImageFont.truetype('/System/Library/Fonts/Hiragino Sans GB.ttc', 22) if os.path.exists('/System/Library/Fonts/Hiragino Sans GB.ttc') else ImageFont.load_default()
    cw, ch = 640, 360; n = len(results); cols = 2; rows = (n + 1) // cols
    sheet = Image.new('RGB', (cols * (cw + 10) + 10, rows * (ch + 96) + 50), 'white'); d = ImageDraw.Draw(sheet)
    d.text((10, 10), f"{card.get('clip', '')}　参与者：{'、'.join(parts)}" + (f"　→ 选用 {ok[0]['name']}" if ok else ''), font=f, fill='black')
    for i, r in enumerate(results):
        x = 10 + (i % cols) * (cw + 10); y = 50 + (i // cols) * (ch + 96)
        if r['status'] == 'OK':
            sheet.paste(Image.open(r['img']).convert('RGB').resize((cw, ch)), (x, y))
            if ok and r is ok[0]: d.rectangle((x - 4, y - 4, x + cw + 3, y + ch + 3), outline=(230, 40, 120), width=6)
            d.text((x, y + ch + 6), f"{r['name']}　总分 {r['score']}", font=f, fill='black')
            d.text((x, y + ch + 34), f"人物{r['P']} 字号{r['T']}({r['punch']}px) 贴合{r['H']} 真实{r['C']} 新鲜{r['V']}", font=f, fill='#444')
            d.text((x, y + ch + 62), r['note'][:30], font=f, fill='#666')
        else:
            d.rectangle((x, y, x + cw, y + ch), fill=(240, 240, 240)); d.text((x + 20, y + ch // 2), f"{r['name']} 淘汰：{r['why'][:24]}", font=f, fill='black')
    sheet.save(os.path.join(a.out_dir, 'choose.jpg'), quality=88)
    json.dump(results, open(os.path.join(a.out_dir, 'choose.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
