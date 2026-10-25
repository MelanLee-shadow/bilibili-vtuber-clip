#!/usr/bin/env python3
"""Read-only public comparison of one package, with a JSON receipt."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.autoslice.external_duplicate_check import PublicAudioASR, PublicBilibili, check, load_package
from src.autoslice.timely_term_crawler import BoundedHttpClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True, help='JSON: candidate_id, recording_date, participants [[streamer aliases],[guest aliases]], source_intervals [{recording,start_seconds,end_seconds}], duration_seconds, srt (relative to JSON), own_uploader_mids, subtitle_stage (final/source_preview)')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path, help='Save public JSON responses for offline replay')
    parser.add_argument('--max-videos', type=int, default=20)
    parser.add_argument('--max-requests', type=int, default=100)
    parser.add_argument('--audio-work-dir', type=Path, help='Temporary public audio directory; audio is deleted after transcription')
    parser.add_argument('--no-audio-asr', action='store_true', help='Disable the default bcut-first public audio fallback')
    args = parser.parse_args()
    if not 1 <= args.max_requests <= 200:
        parser.error('--max-requests must be 1..200')
    now = dt.datetime.now(dt.timezone.utc)
    client = BoundedHttpClient(allowed_hosts=frozenset({'bilibili.com', 'hdslb.com'}),
                               max_requests=args.max_requests, timeout_seconds=12, now=now)
    audio = None if args.no_audio_asr else PublicAudioASR(
        work_dir=args.audio_work_dir,
        evidence_dir=args.evidence_dir / 'audio-asr' if args.evidence_dir else None)
    result = check(load_package(args.package), PublicBilibili(client, now=now, evidence_dir=args.evidence_dir),
                   max_videos=args.max_videos, audio_asr=audio)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('candidate_id', 'decision', 'block_upload')}, ensure_ascii=False))
    return 3 if result['block_upload'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
