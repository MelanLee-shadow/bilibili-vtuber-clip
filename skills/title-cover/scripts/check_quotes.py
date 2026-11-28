#!/usr/bin/env python3
"""Check that every quoted phrase in a title and every cover line is backed by the final subtitles.

usage: check_quotes.py --srt CLIP.srt [--speaker-srt CLIP.speaker.srt] --title "..." --cover "第一行|第二行" [--quote "..."]
- Quoted spans in the title (“…” "…" 「…」) and --quote values must be what one person said (punctuation/space ignored):
  verbatim (OK), or cut down from it (CONDENSED: every character, in order, stands in that speaker's lines within
  WINDOW neighbouring cues; characters and whole lines may be left out, none added or changed; a negation 不没别未无非勿莫
  may not be left out of a clause a later kept character comes from: 「我不是说你是猪」 cut to 「我是猪」 says the opposite).
  Exit 1 otherwise.
- Cover lines are reported as VERBATIM or PARAPHRASE: a paraphrase is allowed only without quote marks and must keep the
  meaning; for "名字：原话" lines only the part after the colon is checked.
Verbatim matches may span adjacent subtitle cues. Speaker labels like "[李豆沙]" are reported; a condensed quote never
mixes two speakers' lines.
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

CLAUSE = re.compile(r'[\s，。！？、；：,.!?;:…—~～]+')
NEGATIONS = '不没别未无非勿莫'
NOT_NEGATIONS = ('非常', '不过', '不管', '无论', '不论')   # a negation letter that negates nothing

def flips(texts, n):
    """Taking n's characters greedily from texts, is a negation left out of a clause a later character is taken from?"""
    at = 0
    for text in texts:
        for clause in filter(None, (PUNCT.sub('', c) for c in CLAUSE.split(text))):
            taken = set()
            for i, ch in enumerate(clause):
                if at < len(n) and ch == n[at]: at += 1; taken.add(i)
            if taken and any(ch in NEGATIONS and i not in taken
                             and not any(clause.startswith(w, j) for w in NOT_NEGATIONS for j in range(max(0, i - len(w) + 1), i + 1))
                             for i, ch in enumerate(clause[:max(taken)])):
                return True
    return False

def find(needle, cs):
    n = PUNCT.sub('', needle)
    if not n: return None
    for size in range(1, 5):                      # smallest window of adjacent cues that contains the phrase
        for i in range(len(cs) - size + 1):
            if n in ''.join(PUNCT.sub('', c[2]) for c in cs[i:i + size]) and not flips([c[2] for c in cs[i:i + size]], n):
                return cs[i][0], cs[i][1] or cs[i + size - 1][1], ' / '.join(c[2] for c in cs[i:i + size])
    return None

WINDOW = 6    # neighbouring cues one speaker's condensed quote may be drawn from

def find_condensed(needle, cs):
    n = PUNCT.sub('', needle)
    if not n: return None
    for size in range(1, WINDOW + 1):             # smallest window first
        for i in range(len(cs) - size + 1):
            win = cs[i:i + size]
            for who in dict.fromkeys(c[1] for c in win):
                at, used = 0, []
                for c in (c for c in win if c[1] == who):
                    before = at
                    for ch in PUNCT.sub('', c[2]):
                        if at < len(n) and ch == n[at]: at += 1
                    if at > before: used.append(c)
                if at == len(n) and not flips([c[2] for c in win if c[1] == who], n):
                    return used[0][0], who, ' / '.join(c[2] for c in used)
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
        cut = None if hit else find_condensed(q, cs)
        if hit: print(f'OK      引用「{q}」 {hit[0]} [{hit[1] or "?"}] {hit[2]}')
        elif cut: print(f'CONDENSED 引用「{q}」 {cut[0]} [{cut[1] or "?"}] {cut[2]}（删过字或并过句：确认意思没变）')
        else: print(f'MISSING 引用「{q}」 不是哪一个人说过的话：字幕里找不到，或添了字、换了字、拼了两个人的话，或删掉了否定词'); bad += 1
    for line in [l for l in a.cover.split('|') if l.strip()]:
        body = re.split(r'[：:]', line, maxsplit=1)[-1]
        hit = find(body, cs)
        if hit: print(f'VERBATIM 封面「{line}」 {hit[0]} [{hit[1] or "?"}] {hit[2]}')
        else: print(f'PARAPHRASE 封面「{line}」 不是逐字原话：确认它是不加引号的语义缩写且意思没变')
    sys.exit(1 if bad else 0)

if __name__ == '__main__':
    main()
