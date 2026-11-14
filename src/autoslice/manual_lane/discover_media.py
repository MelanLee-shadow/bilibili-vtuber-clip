"""`discover`, the media side: what the recordings of a day are, and each recording made ready for the selection.

- the archive: the recorder's final archive on the media host, `<n100_archive_root>/<room id>/<YYYY-MM-DD>/` (the topology
  file `ops/recording/oci3-recording-topology.json` is the one place that names the root; docs/pipeline/10-source-recording.md).
  Only root can read it, so every listing, copy and read of a recording runs as root through `adapters.root_script`
  (`source_read` for the recording itself) and what it writes is handed back to the lane's user.
- `inventory`: every `.flv` of a day with its size and modification time and whether the same-name `.xml` (danmaku) is
  there; a file under `stub_max_bytes` is the empty shell of a stream's first moment and is not prepared.
- `prepare`: the recording's audio track (a host CPU job: the `host_encode` gate), its whole-recording BCUT subtitles
  (paced across every clip and process on the host by free_asr_paced.py) and its danmaku XML, copied beside the task as
  `<session dir>/{bcut.srt,bcut.json,source.xml,prepared.json}` with the fingerprints of the inputs.  A recording whose
  fingerprint did not change is not prepared again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, TypedDict

from .adapters import Remote, root_script
from .lane_rules import rules
from .layout import layout
from .parallel import slot
from .source_read import HOST_ENCODE, source_audio_script
from .state import StateError, sha256_file

if TYPE_CHECKING:
    from .adapters import RemoteResult

PREPARED_SCHEMA = "manual-lane.discover-prepared.v1"
AUDIO_TIMEOUT_S = 2400            # a 30-minute segment takes minutes; the manual run's own bound
NO_SPEECH = "NO_SPEECH"            # a recording nobody speaks in (the few seconds before or after a stream): done, not failed


class DiscoverError(StateError):
    """A step of discovery failed; `code` is the reason that goes into the ledger, the message its detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class XmlFacts(TypedDict):
    exists: bool
    sha256: str | None


class Prepared(TypedDict):
    schema: str
    date: str
    segment: str
    path: str
    size: int
    mtime: str
    source_fp: str
    directory: str
    remote_dir: str
    xml: XmlFacts
    xml_fp: str | None
    srt_sha256: str


@dataclass(frozen=True)
class Segment:
    """One recording file of a day, as the media host lists it."""
    date: str
    path: str
    size: int
    mtime: str                  # the host's `%T@` text, kept as is: it is part of the fingerprint
    xml_size: int | None        # None: no same-name XML
    xml_mtime: str | None
    stub: bool

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def xml_path(self) -> str:
        return str(PurePosixPath(self.path).with_suffix(".xml"))

    @property
    def source_fp(self) -> str:
        return _fingerprint(self.path, self.size, self.mtime)

    @property
    def xml_fp(self) -> str | None:
        return None if self.xml_size is None else _fingerprint(self.xml_path, self.xml_size, self.xml_mtime)


@dataclass(frozen=True)
class DayInventory:
    date: str
    now: float                  # the host's clock when it listed
    exists: bool                # the day's directory is in the archive
    segments: tuple[Segment, ...]
    newest: float = 0.0         # when a file of the day (recording or danmaku) was last written
    lone_danmaku: tuple[float, ...] = ()        # when each danmaku file that has no recording next to it was written

    def settled(self, minutes: float, lone_danmaku_hours: float) -> bool:
        """Nothing of the day was written in the last `minutes`, and no danmaku file is still waiting for its recording:
        the recorder uploads a recording after its danmaku, hours later for a long one; a recording that has not come
        `lone_danmaku_hours` after its danmaku was written is not coming.  By the host's clock."""
        waiting = any(self.now - written <= lone_danmaku_hours * 3600 for written in self.lone_danmaku)
        return bool(self.segments) and not waiting and self.now - self.newest > minutes * 60

    def same_recordings(self, recorded: Sequence[Mapping[str, Any]]) -> bool:
        """The day's recordings are the ones in `recorded` (a ledger day's segments): the same file names with the same
        sizes.  Where they are read from does not matter (the archive, or the copy fetched straight from the recorder)."""
        return ({(s.name, s.size) for s in self.segments}
                == {(PurePosixPath(str(r["path"])).stem, r["size"]) for r in recorded})


def is_date(text: str) -> bool:
    """`text` is a calendar day written YYYY-MM-DD (how the recorder names its day directories)."""
    try:
        return len(text) == 10 and dt.date.fromisoformat(text).isoformat() == text
    except ValueError:
        return False


def _fingerprint(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def archive_room() -> str:
    """The room's recordings on the media host (layout.archive_room_dir: manual_lane.json `archive_root`, else the topology)."""
    return layout().archive_room_dir


def archive_dir(date: str) -> str:
    """The day's directory in the recorder's archive."""
    if not is_date(date):
        raise DiscoverError("BAD_DATE", f"{date!r} is not YYYY-MM-DD")
    return f"{archive_room()}/{date}"


def direct_room() -> str | None:
    """The room's directory of recordings copied straight from the recorder (layout `recordings_direct_root`), or None.
    The archive gets a recording hours after the stream (the recorder uploads it to the cloud drive, measured 6-12 h);
    the direct copy is there minutes after.  A day that has a directory there is read from there."""
    root = layout().recordings_direct_root
    return f"{root}/{layout().room_id}" if root else None


def copy_direct(remote: Remote, done: Sequence[str] = ()) -> dict[str, str] | None:
    """Copy the recorder's newest days straight to the media host (layout `recordings_direct_host` / `_root`): of the
    newest `discover.auto_recent_dates` days, those not earlier than `auto_not_before` and not in `done` (the days
    discover has completed: nothing reads them again).  `direct_keep_days` copies are kept.  None when it is not
    configured; a failure is said and stops nothing: the days that are here, and the archive, are still read."""
    host, room = layout().recordings_direct_host, direct_room()
    if not host or not room:
        return None
    cfg = rules("discover")
    day = "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    source = shlex.quote(f"{host}:{layout().room_id}/")
    skip = " ".join(shlex.quote(d) for d in done if is_date(d))
    body = (f"mkdir -p {shlex.quote(room)} || exit 20\n"
            f"days=$(rsync --list-only {source} | awk '{{print $NF}}' | grep -E '{day}' | sort | tail -n {int(cfg['auto_recent_dates'])}"
            f" | awk -v first={shlex.quote(str(cfg['auto_not_before']))} '$0 >= first') || exit 21\n"
            f"for skip in {skip}; do days=$(printf '%s\\n' $days | grep -vxF \"$skip\"); done\n"
            f"for d in $days; do rsync -a --partial --chmod=Du+rwx,Fu+rw {source}\"$d\" {shlex.quote(room + '/')} || exit 22; done\n"
            f"ls {shlex.quote(room)} | grep -E '{day}' | sort | head -n -{int(cfg['direct_keep_days'])} | "
            f"while read -r d; do rm -rf {shlex.quote(room)}/\"$d\"; done\n"
            "echo DIRECT_COPY_OK $days\n")
    result = remote.run_script(name="disc-direct-copy", body=body, timeout=7200)
    if result.rc != 0 or "DIRECT_COPY_OK" not in result.stdout:
        return {"status": "FAILED", "detail": f"rc={result.rc} {(result.stdout + result.stderr)[-300:]}"}
    return {"status": "OK", "detail": result.stdout.strip().splitlines()[-1]}


def _as_root(remote: Remote, name: str, command: str, *, timeout: int, owned: str | None = None) -> RemoteResult:
    """`command` as root on the media host; with `owned`, what it wrote under that directory is handed back to the lane's user."""
    body = root_script(command.replace("'", "'\\''"), env=(), owned=owned, recursive=True)
    return remote.run_script(name=name, body=body, timeout=timeout)


def _need_ok(result: RemoteResult, code: str) -> RemoteResult:
    if result.rc != 0:
        raise DiscoverError(code, f"rc={result.rc} {result.stdout[-300:]} {result.stderr[-300:]}")
    return result


def _now(stdout: str) -> float:
    for line in stdout.splitlines():
        if line.startswith("NOW "):
            return float(line.split()[1])
    raise DiscoverError("HOST_CLOCK_MISSING", f"the listing did not say what time it is: {stdout[-200:]}")


def list_dates(remote: Remote) -> tuple[list[str], float, bool]:
    """The date directories of the room in the archive (oldest first), the host's clock, and whether the room has a
    directory at all.  A recordings root that is not there is a wrong setting and stops; a room with no directory yet
    under an existing root has simply not been recorded."""
    room = archive_room()
    root = os.path.dirname(room)
    find = (f"if [ ! -d {shlex.quote(root)} ]; then echo NO_ARCHIVE_ROOT; elif [ -d {shlex.quote(room)} ]; then " + shlex.join(
        ['find', room, '-mindepth', '1', '-maxdepth', '1', '-type', 'd']) + r" -printf 'D\t%f\n'; else echo NO_ROOM_DIR; fi")
    direct = direct_room()
    if direct:           # the days copied straight from the recorder count too (the archive may not have the day yet)
        find += f"; if [ -d {shlex.quote(direct)} ]; then " + shlex.join(
            ['find', direct, '-mindepth', '1', '-maxdepth', '1', '-type', 'd']) + r" -printf 'D\t%f\n'; fi"
    out = _need_ok(_as_root(remote, "disc-dates", f"echo NOW $(date +%s); {find}", timeout=300), "ARCHIVE_LISTING_FAILED").stdout
    if "NO_ARCHIVE_ROOT" in out.splitlines():
        raise DiscoverError("ARCHIVE_ROOT_MISSING", f"{root} does not exist on the media host (manual_lane.json archive_root)")
    dates = sorted({line.split("\t", 1)[1] for line in out.splitlines() if line.startswith("D\t")})
    return [d for d in dates if is_date(d)], _now(out), "NO_ROOM_DIR" not in out.splitlines()


def inventory(remote: Remote, date: str) -> DayInventory:
    """Every recording of the day: path, size, modification time, and its danmaku XML when there is one."""
    def listing(directory: str) -> str:
        find = shlex.join(["find", directory, "-type", "f", "(", "-name", "*.flv", "-o", "-name", "*.xml", ")"])
        return f"echo DIR; {find}" + r" -printf 'F\t%p\t%s\t%T@\n'"

    directory, direct = archive_dir(date), direct_room()
    command = f"if [ -d {shlex.quote(directory)} ]; then {listing(directory)}; fi"
    if direct:           # the day as the recorder itself has it, when it was copied here: read it from there
        command = f"if [ -d {shlex.quote(direct + '/' + date)} ]; then {listing(direct + '/' + date)}; el{command}"
    command = "echo NOW $(date +%s); " + command
    out = _need_ok(_as_root(remote, f"disc-inv-{date}", command, timeout=600), "ARCHIVE_LISTING_FAILED").stdout
    files: dict[str, tuple[int, str]] = {}
    for line in out.splitlines():
        if line.startswith("F\t"):
            _tag, path, size, mtime = line.split("\t")
            files[path] = (int(size), mtime)
    limit = int(rules("discover")["stub_max_bytes"])
    segments = []
    for path in sorted(p for p in files if p.endswith(".flv")):
        size, mtime = files[path]
        xml = files.get(str(PurePosixPath(path).with_suffix(".xml")))
        segments.append(Segment(date=date, path=path, size=size, mtime=mtime, xml_size=xml[0] if xml else None,
                                xml_mtime=xml[1] if xml else None, stub=size < limit))
    lone = tuple(float(mtime) for path, (_size, mtime) in files.items()
                 if path.endswith(".xml") and str(PurePosixPath(path).with_suffix(".flv")) not in files)
    return DayInventory(date=date, now=_now(out), exists="DIR" in out.splitlines(), segments=tuple(segments),
                        newest=max((float(mtime) for _size, mtime in files.values()), default=0.0), lone_danmaku=lone)


def remote_dir_of(segment: Segment) -> str:
    return f"{layout().session_root(segment.date)}/discover/{segment.name}"


def write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_prepared(directory: Path) -> Prepared | None:
    path = directory / "prepared.json"
    if not path.exists():
        return None
    doc: Prepared = json.loads(path.read_text(encoding="utf-8"))
    return doc if doc.get("schema") == PREPARED_SCHEMA else None


def _subtitles_current(prior: Prepared | None, segment: Segment, directory: Path) -> bool:
    srt = directory / "bcut.srt"
    return bool(prior and prior["source_fp"] == segment.source_fp and srt.exists() and sha256_file(srt) == prior["srt_sha256"])


def _fetch_subtitles(remote: Remote, segment: Segment, directory: Path) -> None:
    """The audio track and its BCUT subtitles on the media host, then the subtitles beside the task."""
    from .step3_subtitles import FREE_ASR_TOOL, free_asr_body

    rd, fp = remote_dir_of(segment), segment.source_fp
    with slot(HOST_ENCODE):               # the audio is a host CPU job: the gate steps 2 and 5 take
        made = remote.run_script(name=f"disc-audio-{segment.name}", timeout=AUDIO_TIMEOUT_S,
                                 body=f"mkdir -p {shlex.quote(rd)}\n" + source_audio_script(segment.path, workdir=rd, fingerprint=fp))
    if made.rc != 0 or "AUDIO_" not in made.stdout:
        raise DiscoverError("AUDIO_FAILED", f"rc={made.rc} {made.stdout[-200:]} {made.stderr[-300:]}")
    remote.put(FREE_ASR_TOOL, f"{rd}/tools/free_asr_paced.py")
    asr_body, timeout = free_asr_body(rd, media=f"{rd}/audio.mp3", providers=("bcut",))
    done = f"{rd}/asr/asr.fp"      # the recognition of this very audio is kept: a fetch that failed after it is not paid twice
    body = (f'if [ "$(cat {done} 2>/dev/null)" = {fp} ] && [ -s {rd}/asr/bcut.srt ]; then echo ASR_OK; exit 0; fi\n'
            f"rm -rf {rd}/asr\n{asr_body}if [ -s {rd}/asr/bcut.srt ]; then printf %s {fp} > {done}; fi\n")
    res = remote.run_script(name=f"disc-asr-{segment.name}", body=body, timeout=timeout)
    if "ASR_OK" not in res.stdout:
        if "FREE_ASR bcut OK" in res.stdout:      # the recogniser answered and found nothing to write: nobody speaks
            raise DiscoverError(NO_SPEECH, "the recogniser answered with no subtitles")
        raise DiscoverError("ASR_MISSING_SOURCE", f"rc={res.rc} {res.stdout[-200:]} {res.stderr[-300:]}")
    remote.get(f"{rd}/asr/bcut.srt", directory / "bcut.srt")
    remote.get(f"{rd}/asr/bcut.json", directory / "bcut.json")


def _fetch_xml(remote: Remote, segment: Segment, directory: Path) -> None:
    rd = remote_dir_of(segment)
    command = shlex.join(["cp", "-f", segment.xml_path, f"{rd}/source.xml"])
    _need_ok(_as_root(remote, f"disc-xml-{segment.name}", f"mkdir -p {shlex.quote(rd)}; {command}", timeout=600, owned=rd), "XML_COPY_FAILED")
    remote.get(f"{rd}/source.xml", directory / "source.xml")


def prepare(remote: Remote, segment: Segment, directory: Path, prior: Prepared | None) -> Prepared:
    """`segment` made ready under `directory` (the session directory of the recording); `prior` is what an earlier run
    recorded there.  The audio and the subtitles are made again only when the recording's fingerprint changed (or the
    subtitles file is gone); the XML only when its fingerprint changed.  A missing XML is recorded, not an error."""
    directory.mkdir(parents=True, exist_ok=True)
    if not _subtitles_current(prior, segment, directory):
        _fetch_subtitles(remote, segment, directory)
    xml = directory / "source.xml"
    if segment.xml_fp is None:
        xml.unlink(missing_ok=True)
    elif not (prior and prior["xml_fp"] == segment.xml_fp and xml.exists() and prior["xml"]["sha256"] == sha256_file(xml)):
        _fetch_xml(remote, segment, directory)
    doc = Prepared(schema=PREPARED_SCHEMA, date=segment.date, segment=segment.name, path=segment.path, size=segment.size,
                   mtime=segment.mtime, source_fp=segment.source_fp, directory=str(directory), remote_dir=remote_dir_of(segment),
                   xml=XmlFacts(exists=xml.exists(), sha256=sha256_file(xml) if xml.exists() else None),
                   xml_fp=segment.xml_fp, srt_sha256=sha256_file(directory / "bcut.srt"))
    write_json(directory / "prepared.json", doc)
    return doc
