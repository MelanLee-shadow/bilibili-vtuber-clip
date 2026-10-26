#!/usr/bin/env python3
"""Opt-in native CPA replacement for the legacy live-transcription launchers."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autoslice import cpa_gemini_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('lane', choices=['talk', 'song'])
    parser.add_argument('media', type=Path)
    parser.add_argument('srt', type=Path)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args()
    if not cpa_gemini_client.enabled():
        raise ValueError('CPA_GEMINI_SWITCH_DISABLED')
    notes = ('talking_confirmed: bool, funny_moments: [{start: HH:MM:SS, end: HH:MM:SS, '
             'summary: str, why_funny: str}], topics: [str], notes: str' if args.lane == 'talk' else
             'singing_confirmed: bool, songs: [{probable_song_title: str, start: HH:MM:SS, '
             'end: HH:MM:SS, complete: bool}], notes: str')
    prompt = ('Listen to the full attached live-stream capture from start to end. Transcribe '
        'everything spoken' + (' and sung, one lyric line per cue' if args.lane == 'song' else
        ', including viewer comments read aloud, keeping colloquial wording, particles and tone') +
        '. Keep accurate per-utterance timestamps, cover all audible speech and singing without '
        'multi-minute omissions. Return exactly one JSON object with keys "srt" (a complete '
        'simplified-Chinese SRT string, no markdown) and "notes" (an object with these fields: ' +
        notes + '). Base every time, topic and event on the attached media only.')
    completion = cpa_gemini_client.generate_files(prompt=prompt, files=[args.media],
        model=os.environ.get('AGY_MODEL', 'Gemini 3.8 Flash (High)'),
        purpose='live_' + args.lane + '_transcription', timeout_seconds=2400)
    payload = json.loads(completion.text)
    if not isinstance(payload.get('srt'), str) or '-->' not in payload['srt'] or not isinstance(payload.get('notes'), dict):
        raise ValueError('CPA_GEMINI_TRANSCRIPTION_INVALID')
    args.srt.write_text(payload['srt'], encoding='utf-8')
    args.notes.write_text(json.dumps(payload['notes'], ensure_ascii=False) + '\n', encoding='utf-8')
    args.srt.with_suffix('.transport.json').write_text(json.dumps(dict(completion.receipt)) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
