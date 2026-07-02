from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

OUT = Path('/opt/bilive/reports/full_live_acceptance/22966160-2026-06-17-BV1T6L96zE8n')
SRC = Path('/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-06-17/replacement_source/BV1T6L96zE8n')
DATE_DIR = Path('/root/clouddrive2/CloudNAS/CloudDrive/123云盘/live-streaming/22966160/2026-06-17')
VIDEO = OUT / 'full_replacement_source.mp4'
FULL_SRT = OUT / 'full_session_from_segment_srt.srt'


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def parse_ms(ts: str) -> int:
    return ((int(ts[0:2]) * 60 + int(ts[3:5])) * 60 + int(ts[6:8])) * 1000 + int(ts[9:12])


def fmt(ms: int) -> str:
    ms = max(0, int(ms))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f'{h:02d}:{m:02d}:{s:02d},{ms:03d}'


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    concat = OUT / 'concat.txt'
    parts = [SRC / '01-BV1T6L96zE8n_p1.remux.mp4', SRC / '02-BV1T6L96zE8n_p2.remux.mp4']
    concat.write_text(''.join(f"file {shlex.quote(str(part))}\n" for part in parts), encoding='utf-8')
    if not VIDEO.exists() or VIDEO.stat().st_size == 0:
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(concat), '-c', 'copy', str(VIDEO)])

    srt_dir = DATE_DIR / 'subtitles'
    pattern = re.compile(r'22966160_(\d{4}-\d{2}-\d{2})-(\d{2})-(\d{2})-(\d{2})-\.srt$')
    base = datetime(2026, 6, 17, 20, 1, 17)
    rows: list[str] = []
    source_files: list[dict[str, object]] = []
    index = 1
    for path in sorted(srt_dir.glob('22966160_2026-06-17-*.srt')):
        match = pattern.match(path.name)
        if match is None:
            continue
        start = datetime.strptime(
            f'{match.group(1)} {match.group(2)}:{match.group(3)}:{match.group(4)}',
            '%Y-%m-%d %H:%M:%S',
        )
        offset_ms = int((start - base).total_seconds() * 1000)
        if offset_ms < -2_000:
            continue
        blocks = re.split(r'\n\s*\n', path.read_text(encoding='utf-8', errors='replace').strip())
        count = 0
        for block in blocks:
            lines = [line.rstrip('\r') for line in block.splitlines() if line.strip()]
            if len(lines) < 3 or '-->' not in lines[1]:
                continue
            start_text, end_text = [part.strip() for part in lines[1].split('-->', 1)]
            start_ms = parse_ms(start_text) + offset_ms
            end_ms = parse_ms(end_text) + offset_ms
            if end_ms <= start_ms:
                continue
            body = '\n'.join(lines[2:])
            rows.append(f'{index}\n{fmt(start_ms)} --> {fmt(end_ms)}\n{body}\n')
            index += 1
            count += 1
        source_files.append({'path': str(path), 'offset_ms': offset_ms, 'cues': count})
    FULL_SRT.write_text('\n'.join(rows).rstrip() + ('\n' if rows else ''), encoding='utf-8')

    probe = subprocess.check_output(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration,size', '-of', 'json', str(VIDEO)],
        text=True,
    )
    manifest = {
        'schema_version': 'full-live-acceptance-source.v1',
        'room_id': '22966160',
        'date': '2026-06-17',
        'bvid': 'BV1T6L96zE8n',
        'source_video': str(VIDEO),
        'source_srt': str(FULL_SRT),
        'video_sha256': sha256(VIDEO),
        'source_srt_sha256': sha256(FULL_SRT),
        'source_srt_cues': len(rows),
        'srt_base_wall_clock': '2026-06-17T20:01:17',
        'source_srt_files': source_files,
        'ffprobe': json.loads(probe),
    }
    (OUT / 'acceptance_source_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8',
    )
    print(json.dumps({
        'source_video': manifest['source_video'],
        'source_srt': manifest['source_srt'],
        'source_srt_cues': manifest['source_srt_cues'],
        'ffprobe': manifest['ffprobe'],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
