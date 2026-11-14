#!/usr/bin/env python3
"""Check that every quoted phrase in a title and every cover line is backed by the final subtitles.

usage: check_quotes.py --srt CLIP.srt [--speaker-srt CLIP.speaker.srt] --title "..." --cover "第一行|第二行" [--quote "..."]
- Quoted spans in the title (“…” "…" 「…」) and --quote values must appear verbatim (punctuation/space ignored); exit 1 if not.
- Cover lines are reported as VERBATIM or PARAPHRASE: a paraphrase is allowed only without quote marks and must keep the
  meaning; for "名字：原话" lines only the part after the colon is checked.
Matches may span adjacent subtitle cues. Speaker labels like "[李豆沙]" are reported, not matched.
"""
import argparse, re, sys

PUNCT = re.compile(r'[\s，。！？、；：,.!?;:“”"\'‘’「」『』（）()《》〈〉…—\-~～·]')

def cues(path):
    out = []
    for block in open(path, encoding='utf-8').read().strip().split('\n\n'):
        lines = block.strip().split('\n')
        if len(lines) < 3: continue
        text = ' '.join(lines[2:]); spk = ''
        m = re.match(r'\[([^\]]+)\]\s*(.*)', text)
        if m: spk, text = m.group(1), m.group(2)
        out.append((lines[1].split(' --> ')[0][:8], spk, text))
    return out

def find(needle, cs):
    n = PUNCT.sub('', needle)
    if not n: return None
    for size in range(1, 5):                      # smallest window of adjacent cues that contains the phrase
        for i in range(len(cs) - size + 1):
            if n in ''.join(PUNCT.sub('', c[2]) for c in cs[i:i + size]):
                return cs[i][0], cs[i][1] or cs[i + size - 1][1], ' / '.join(c[2] for c in cs[i:i + size])
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--srt', required=True); ap.add_argument('--speaker-srt'); ap.add_argument('--title', default='')
    ap.add_argument('--cover', default=''); ap.add_argument('--quote', action='append', default=[])
    a = ap.parse_args()
    cs = cues(a.speaker_srt or a.srt)
    quotes = re.findall(r'[“"「]([^”"」]+)[”"」]', a.title) + a.quote
    bad = 0
    for q in quotes:
        hit = find(q, cs)
        if hit: print(f'OK      引用「{q}」 {hit[0]} [{hit[1] or "?"}] {hit[2]}')
        else: print(f'MISSING 引用「{q}」 字幕里找不到逐字原话'); bad += 1
    for line in [l for l in a.cover.split('|') if l.strip()]:
        body = re.split(r'[：:]', line, maxsplit=1)[-1]
        hit = find(body, cs)
        if hit: print(f'VERBATIM 封面「{line}」 {hit[0]} [{hit[1] or "?"}] {hit[2]}')
        else: print(f'PARAPHRASE 封面「{line}」 不是逐字原话：确认它是不加引号的语义缩写且意思没变')
    sys.exit(1 if bad else 0)

if __name__ == '__main__':
    main()
