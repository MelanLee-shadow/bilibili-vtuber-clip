#!/usr/bin/env python3
"""Idempotently replace the autoslice runner's blrec API gate with status JSON.

Production can be at the committed base while the local workspace contains
unrelated in-progress edits.  This narrow deploy helper changes only the
recorder boundary and refuses unknown source text.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import py_compile
import sys


OLD_CONSTANT = 'BLREC_PORT = int(os.environ.get("AUTOSLICE_BLREC_PORT", "22333"))'
NEW_CONSTANT = '''RECORDER_STATUS_PATH = Path(
    os.environ.get(
        "AUTOSLICE_RECORDER_STATUS_PATH",
        "/opt/bilive/recording/status.json",
    )
)
RECORDER_STATUS_MAX_AGE_SECONDS = int(
    os.environ.get("AUTOSLICE_RECORDER_STATUS_MAX_AGE_SECONDS", "180")
)'''

OLD_FUNCTION = '''def blrec_live_status() -> bool | None:
    """True=live, False=not live, None=unknown (API down → fail-safe skip)."""
    key = load_env_file(BILIVE_ENV).get("RECORD_KEY", "")
    if not key:
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{BLREC_PORT}/api/v1/tasks/{ROOM}/data",
        headers={"x-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return int(data.get("room_info", {}).get("live_status", 0)) == 1
    except Exception as exc:  # noqa: BLE001 — any API failure means "unknown"
        log(f"blrec API unavailable: {exc}")
        return None
'''

NEW_FUNCTION = '''def recorder_live_status() -> bool | None:
    """True=active, False=sealed, None=unknown (stale/down → fail-safe skip)."""

    try:
        if RECORDER_STATUS_PATH.is_symlink() or not RECORDER_STATUS_PATH.is_file():
            raise ValueError("status file missing or symlinked")
        data = json.loads(RECORDER_STATUS_PATH.read_text(encoding="utf-8"))
        if data.get("schema_version") != "recorder-neutral-status.v1":
            raise ValueError("status schema mismatch")
        if str(data.get("room_id")) != str(ROOM):
            raise ValueError("status room mismatch")
        generated = float(data["generated_at_epoch"])
        age = time.time() - generated
        if age < -300 or age > RECORDER_STATUS_MAX_AGE_SECONDS:
            raise ValueError(f"status stale ({age:.0f}s)")
        if data.get("service_reachable") is not True or data.get("error"):
            raise ValueError(str(data.get("error") or "recorder service unreachable"))
        if data.get("finalizing") is True:
            return True
        if data.get("streaming") is True or data.get("recording") is True:
            return True
        live_status = data.get("live_status")
        if live_status not in (0, 1, False, True):
            raise ValueError("live status is unknown")
        return bool(live_status)
    except Exception as exc:  # noqa: BLE001 — any status failure means "unknown"
        log(f"recorder status unavailable: {exc}")
        return None
'''

REPLACEMENTS = (
    (OLD_CONSTANT, NEW_CONSTANT),
    (OLD_FUNCTION, NEW_FUNCTION),
    ("    live = blrec_live_status()\n", "    live = recorder_live_status()\n"),
    (
        '            write_heartbeat("live=? source=ok (blrec API unavailable — fail-safe skip)")\n',
        '            write_heartbeat("live=? source=ok (recorder status unavailable — fail-safe skip)")\n',
    ),
)


def transform(source: str) -> tuple[str, int]:
    output = source
    changed = 0
    for old, new in REPLACEMENTS:
        if new in output:
            continue
        count = output.count(old)
        if count != 1:
            raise ValueError(
                f"expected exactly one recorder-boundary anchor, found {count}: {old[:80]!r}"
            )
        output = output.replace(old, new, 1)
        changed += 1
    return output, changed


def apply(path: Path) -> int:
    source = path.read_text(encoding="utf-8")
    output, changed = transform(source)
    if not changed:
        return 0
    tmp = path.with_name(f".{path.name}.recorder-status.tmp-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, path.stat().st_mode & 0o777)
        py_compile.compile(str(tmp), doraise=True)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    source = args.path.read_text(encoding="utf-8")
    _output, changed = transform(source)
    if args.apply:
        changed = apply(args.path)
    print(f"recorder_status_patch_changes={changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
