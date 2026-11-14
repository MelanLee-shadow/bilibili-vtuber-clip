"""A session's chat XML, read on the media host itself.

The recorder's files reach the media host through the archive mount (`n100_archive_root` of the recording topology,
ops/recording/oci3-recording-topology.json: `<root>/<room id>/<date>/<name>.xml`); only root can read it.  When the lane's
controller runs on the media host (`--on-media-host`) the chat is read there: the recorder host cannot be reached from the
media host, and a chat that cannot be read leaves the session's SCs, gifts and read-out comments unknown.  The paths the lane
asks for are the recorder's (`layout().danmaku_dir/<date>/<name>`); below the room directory the archive has the same shape,
so the same question is answered from the archive.  Same protocol as adapters.DanmakuSource (session_events.SegmentSource)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import PurePosixPath

from .layout import REPO_ROOT, layout

TOPOLOGY_FILE = REPO_ROOT / "ops" / "recording" / "oci3-recording-topology.json"
READ_TIMEOUT_S = 600


def archive_room_dir() -> str:
    """The room's directory in the recorder's archive on the media host (the topology file is the one place that names the root)."""
    return f"{json.loads(TOPOLOGY_FILE.read_text(encoding='utf-8'))['n100_archive_root']}/{layout().room_id}"


class ArchiveChat:
    """`reader` is the command prefix that can read the mount (root on the media host)."""

    def __init__(self, *, room_dir: str | None = None, reader: Sequence[str] = ("sudo", "-n")):
        self.room_dir = room_dir or archive_room_dir()
        self.reader = tuple(reader)

    def _here(self, recorder_path: str) -> str:
        try:
            below = PurePosixPath(recorder_path).relative_to(layout().danmaku_dir)
        except ValueError as exc:
            raise RuntimeError(f"chat path is not below the recorder's room directory: {recorder_path}") from exc
        return str(PurePosixPath(self.room_dir) / below)

    def _read(self, argv: Sequence[str], what: str) -> bytes:
        proc = subprocess.run([*self.reader, *argv], capture_output=True, timeout=READ_TIMEOUT_S, check=False)   # rc checked below
        if proc.returncode != 0:
            raise RuntimeError(f"{what} failed in the archive: rc={proc.returncode} {proc.stderr.decode('utf-8', 'replace')[-200:]}")
        return proc.stdout

    def fetch(self, remote_path: str) -> bytes:
        return self._read(["cat", self._here(remote_path)], "danmaku fetch")

    def names(self, remote_dir: str) -> list[str]:
        """The XML files of one recording date (one live session), by name."""
        listing = self._read(["ls", "-1", self._here(remote_dir)], "danmaku listing")
        return [n for n in listing.decode("utf-8").splitlines() if n.endswith(".xml")]
