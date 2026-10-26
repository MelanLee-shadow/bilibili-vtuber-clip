#!/usr/bin/env python3
"""Synchronize configured execution workers, then publish their release binding."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.autoslice.overflow_install import sync, sync_native_audio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, default=Path('/opt/bilive/autoslice'))
    parser.add_argument('--config', type=Path)
    parser.add_argument('--native-audio-only', action='store_true',
                        help='repair protected provider configuration without switching code or jobs')
    parser.add_argument('--legacy-adoption', action='append', default=[], metavar='HOST=INVENTORY_JSON')
    args = parser.parse_args()
    adoptions = {}
    for raw in args.legacy_adoption:
        host, separator, path = raw.partition('=')
        if not separator or not Path(path).is_absolute() or host in adoptions:
            parser.error('legacy adoption requires unique HOST=absolute-controller-inventory-path')
        adoptions[host] = path
    try:
        if args.native_audio_only and adoptions:
            parser.error('--native-audio-only does not adopt or modify job stores')
        config = args.config or args.base / 'execution-backends.json'
        result = sync_native_audio(args.base, config) if args.native_audio_only else sync(args.base, config, adoptions)
    except Exception as exc:
        # Preserve typed subprocess diagnostics without emitting remote command text.
        error = (f'{type(exc).__name__}: exit {exc.returncode}'
                 if hasattr(exc, 'returncode') else str(exc))
        print(json.dumps({'status': 'NOT_REGISTERED', 'error': error,
                          'detail': getattr(exc, 'stderr', b'').decode(errors='replace')
                          if isinstance(getattr(exc, 'stderr', b''), bytes) else str(getattr(exc, 'stderr', ''))}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
