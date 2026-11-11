"""Same-session theme compilation (U184): CPA picks several disjoint intervals of one live session and their order; the
pieces are cut and joined into one virtual source that the unchanged manual-lane steps treat as a plain recording.

What is here
- `select`: one CPA call (prompts/compilation_select.md) over the whole session's subtitles.  CPA alone answers whether
  the session holds a compilation, which intervals, in which order.  The code checks only cue numbers, order within an
  interval and overlap inside one recording; there is no quota on the number or length of intervals and no semantic gate.
- `build_media`: cut every interval at cue boundaries (a small head and tail margin, never past the middle of the gap to
  the neighbouring cue) and join them with the producer's own multi-piece concat (producer_source_media), then measure.
- `compilation.json` (clips/<cid>/): each interval's real recording, source start/end, and its start/end on the joined
  clock, from measured durations.  step 1 (boundary), step 0 (duplicate check) and step 8 (package) read it through
  `load_manifest`; a clip without the file takes the ordinary path unchanged.
- `remap_danmaku`: per-recording danmaku moved onto the joined clock, written where step 3 looks for its cached XML.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from . import session_events
from .adapters import Adapters, Remote
from .reselect import load_cues
from .recorder_events import Danmaku, event_lookback_s, event_xml, recorder_events
from .runner import Ctx
from .parallel import slot
from .source_read import HOST_ENCODE, require_source_dir, source_cut_script
from .srt_text import SrtCue, _json_from_text, format_srt, parse_srt
from .state import SCHEMA as STATE_SCHEMA
from .state import ClipData, ClipState
from .step_io import _ask_cpa, _cpa_ok, _log, _render

if TYPE_CHECKING:
    from .config import SessionBlock, SessionConfig
    from .state import CpaEvent

MANIFEST_SCHEMA = "manual-lane.compilation.v1"
MANIFEST_FILE = "compilation.json"
WORK_DIR = "compilation"                  # clips/<cid>/compilation/: the selection answer, the joined clock's BCUT
HEAD_MARGIN_S = 0.12                      # before the first kept cue (precedent: the hand-made supercut's cut padding)
TAIL_MARGIN_S = 0.35                      # after the last kept cue
CONCAT_TOLERANCE_MS = 250                 # joined file vs the sum of the measured pieces
SEAM_FADE_S = 0.015                       # audio fade out/in on each side of an internal seam: short enough that no level
                                          # change is heard, long enough to cover the sample jump of a hard join
SELECT_PURPOSE = "compilation-select"
KIND_TRIM = "trim"                        # Manifest["kind"] of a clip made by compile_pieces


class CompilationError(ValueError):
    """A mechanical failure of the compilation entry; `code` is the original reason, `detail` the original text."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ----- types -------------------------------------------------------------------------------------------
class SegmentAnswer(TypedDict):
    segment: int
    start_cue: int
    end_cue: int
    why: str


@dataclass(frozen=True)
class Selection:
    """CPA's answer, after the mechanical check.  `segments` is empty exactly when `compilation` is False."""

    compilation: bool
    reason: str
    hook: str
    segments: tuple[SegmentAnswer, ...]


class ManifestPiece(TypedDict):
    index: int
    segment: int
    recording: str
    start_cue: int
    end_cue: int
    why: str
    source_start_ms: int
    source_end_ms: int
    measured_ms: int
    compiled_start_ms: int
    compiled_end_ms: int


class ManifestRecording(TypedDict):
    segment: int
    name: str
    path: str
    bcut_srt: str
    bcut_source_zero_ms: int


class TrimOrigin(TypedDict):
    cid: str
    removed: list[list[int]]


class Manifest(TypedDict):
    schema: str
    cid: str
    theme: str
    hook: str
    reason: str
    virtual_source: str
    virtual_path: str
    duration_ms: int
    recordings: list[ManifestRecording]
    pieces: list[ManifestPiece]
    cpa: NotRequired[CpaEvent]
    kind: NotRequired[str]            # "trim": a clip with holes cut out of one finished clip (compile_pieces); absent: a theme compilation
    origin: NotRequired[TrimOrigin]


class MemberPiece(TypedDict):
    """One FMR member `source.pieces` row; the recording fields are written only for a compilation."""

    path: str
    start_ms: int
    end_ms: int
    recording: NotRequired[str]
    recording_start_ms: NotRequired[int]
    recording_end_ms: NotRequired[int]
    recording_context_srt: NotRequired[dict[str, str | int]]


class SourceInterval(TypedDict):
    recording: str
    source_start_ms: int
    source_end_ms: int
    compiled_start_ms: int
    compiled_end_ms: int


@dataclass(frozen=True)
class RecordingInput:
    name: str
    bcut_srt: Path
    zero_ms: int = 0              # recording time at the BCUT file's zero (a BCUT on a session-cumulative clock is negative)


@dataclass(frozen=True)
class PieceCut:
    recording: str
    path: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class CompileResult:
    compilation: bool
    reason: str
    hook: str = ""
    selection_file: Path | None = None
    bcut_srt: Path | None = None
    manifest: Manifest | None = None
    first_cue: int = 0
    last_cue: int = 0


# ----- 1. selection ------------------------------------------------------------------------------
def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def transcript(cues_by_segment: Sequence[Sequence[SrtCue]]) -> str:
    blocks = []
    for number, cues in enumerate(cues_by_segment, 1):
        lines = [f"{c['id']} {_clock(c['start_s'])} {c['text']}" for c in cues]
        blocks.append(f"== 段 {number} ==\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def select_prompt(theme: str, cues_by_segment: Sequence[Sequence[SrtCue]]) -> str:
    return _render("compilation_select.md", theme=theme, transcript=transcript(cues_by_segment))


def _whole(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_selection(text: str, cues_by_segment: Sequence[Sequence[SrtCue]]) -> Selection:
    """Read CPA's answer and check what code may check: the shape, that every cue number exists in its recording, that
    an interval starts no later than it ends, and that no two intervals of one recording overlap.  Nothing about
    how many intervals, how long, or whether they fit the theme."""
    try:
        value = _json_from_text(text)
    except ValueError as exc:
        raise CompilationError("COMPILATION_ANSWER_UNPARSEABLE", str(exc)) from exc
    if not isinstance(value, dict) or not isinstance(value.get("compilation"), bool):
        raise CompilationError("COMPILATION_ANSWER_SHAPE", f"no boolean `compilation` in {str(value)[:200]}")
    reason = str(value.get("reason") or "").strip()
    rows = value.get("segments")
    if not value["compilation"]:
        return Selection(False, reason, "", ())
    hook = str(value.get("hook") or "").strip()
    if not hook:
        raise CompilationError("COMPILATION_ANSWER_SHAPE", "compilation is true but `hook` is empty")
    if not isinstance(rows, list) or not rows:
        raise CompilationError("COMPILATION_ANSWER_SHAPE", "compilation is true but `segments` is empty")
    chosen: list[SegmentAnswer] = []
    taken: dict[int, list[tuple[int, int, int]]] = {}        # segment -> (first position, last position, row number)
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise CompilationError("COMPILATION_ANSWER_SHAPE", f"segments[{number}] is not an object")
        seg, start, end = _whole(row.get("segment")), _whole(row.get("start_cue")), _whole(row.get("end_cue"))
        if seg is None or start is None or end is None:
            raise CompilationError("COMPILATION_ANSWER_SHAPE", f"segments[{number}] needs integer segment/start_cue/end_cue")
        if not 1 <= seg <= len(cues_by_segment):
            raise CompilationError("COMPILATION_SEGMENT_UNKNOWN", f"segments[{number}]: recording {seg} of {len(cues_by_segment)}")
        position = {int(c["id"]): index for index, c in enumerate(cues_by_segment[seg - 1])}
        if start not in position or end not in position:
            raise CompilationError("COMPILATION_CUE_UNKNOWN", f"segments[{number}]: cue {start}-{end} is not in recording {seg}")
        first, last = position[start], position[end]
        if first > last:
            raise CompilationError("COMPILATION_CUE_ORDER", f"segments[{number}]: start_cue {start} comes after end_cue {end}")
        for a, b, other in taken.get(seg, ()):
            if first <= b and a <= last:
                raise CompilationError("COMPILATION_OVERLAP", f"segments[{number}] and segments[{other}] overlap in recording {seg}")
        taken.setdefault(seg, []).append((first, last, number))
        chosen.append({"segment": seg, "start_cue": start, "end_cue": end, "why": str(row.get("why") or "").strip()})
    return Selection(True, reason, hook, tuple(chosen))


# ----- 2. cut points and the joined file --------------------------------------------------------------
def cut_window_ms(cues: Sequence[SrtCue], start_cue: int, end_cue: int) -> tuple[int, int]:
    """The recording interval [start, end) in milliseconds for kept cues start_cue..end_cue: a head and a tail margin
    around the kept speech, but never past the middle of the gap to the cue before or after (it would carry that cue's
    speech into the piece)."""
    position = {int(c["id"]): index for index, c in enumerate(cues)}
    first, last = position[start_cue], position[end_cue]
    kept = cues[first:last + 1]
    begin = min(float(c["start_s"]) for c in kept)
    finish = max(float(c["end_s"]) for c in kept)
    t0, t1 = begin - HEAD_MARGIN_S, finish + TAIL_MARGIN_S
    if first > 0:
        t0 = max(t0, (float(cues[first - 1]["end_s"]) + begin) / 2)
    if last + 1 < len(cues):
        t1 = min(t1, (finish + float(cues[last + 1]["start_s"])) / 2)
    t0, t1 = max(0.0, min(t0, begin)), max(t1, finish)
    return round(t0 * 1000), round(t1 * 1000)


def _probe_line(tag: str, path: str) -> str:
    """A shell line that prints `PROBE <tag> <one-line ffprobe json>` for the file."""
    return f'echo "PROBE {tag} $(ffprobe -v error -of json -show_format -show_streams {shlex.quote(path)} | tr -d \'\\n\')"'


class _ProbeStream(TypedDict, total=False):
    codec_type: str
    codec_name: str
    width: int
    height: int
    pix_fmt: str
    r_frame_rate: str
    sample_rate: str
    channels: int


class _ProbeFormat(TypedDict):
    duration: str


class _Probe(TypedDict):
    format: _ProbeFormat
    streams: NotRequired[list[_ProbeStream]]


def _probe_row(output: str, tag: str) -> _Probe:
    for line in output.splitlines():
        head, _, rest = line.partition(" ")
        if head == "PROBE" and rest.split(" ", 1)[0] == tag:
            return cast("_Probe", json.loads(rest.split(" ", 1)[1]))
    raise CompilationError("COMPILATION_PROBE_MISSING", f"no probe line for {tag}: {output[-300:]}")


def _duration_ms(probe: _Probe) -> int:
    return round(float(probe["format"]["duration"]) * 1000)


def _stream_shape(probe: _Probe) -> tuple[tuple[object, ...], tuple[object, ...]]:
    streams = probe.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise CompilationError("COMPILATION_PIECE_STREAMS", f"a piece lacks video or audio: {[s.get('codec_type') for s in streams]}")
    return ((video.get("codec_name"), video.get("width"), video.get("height"), video.get("pix_fmt"),
             video.get("r_frame_rate")), (audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels")))


def _seam_fade_filter(index: int, count: int, duration_ms: int) -> str | None:
    """The audio filter of piece `index` of `count`: SEAM_FADE_S of fade-in at its head unless it is the first piece and
    of fade-out at its tail unless it is the last, so only the internal seams are touched.  None for a single piece."""
    fades = []
    if index > 0:
        fades.append(f"afade=t=in:st=0:d={SEAM_FADE_S}")
    if index < count - 1:
        fades.append(f"afade=t=out:st={duration_ms / 1000 - SEAM_FADE_S:.3f}:d={SEAM_FADE_S}")
    return ",".join(fades) or None


def build_media(remote: Remote, cuts: Sequence[PieceCut], *, work: str, out: str) -> tuple[list[int], int]:
    """Cut each interval exactly (source_read.source_cut_script: the first cut's one definition), join with the producer's
    multi-piece concat (video kept, audio decoded piece by piece), and write `out` as an FLV like the recordings the
    lane reads.  Each internal seam gets SEAM_FADE_S of audio fade-out/fade-in on its two sides (video and durations
    unchanged).  Returns the measured duration of every piece and of the joined file, in milliseconds."""
    from src.autoslice.producer_source_media import _concat_piece_command

    paths = [Path(f"{work}/pieces/p{index}.mp4") for index in range(len(cuts))]
    lines = [f"mkdir -p {shlex.quote(work)}/pieces {shlex.quote(str(Path(out).parent))}"]
    for index, (cut, path) in enumerate(zip(cuts, paths, strict=True)):
        script = source_cut_script(cut.path, start_s=cut.start_ms / 1000, duration_s=(cut.end_ms - cut.start_ms) / 1000,
                                   output=str(path), fade=_seam_fade_filter(index, len(cuts), cut.end_ms - cut.start_ms))
        lines.append(f"(\n{script}) || {{ echo COMPILATION_PIECE_FAILED {index}; exit 1; }}")
    for index, path in enumerate(paths):
        lines.append(_probe_line(str(index), str(path)))
    with slot(HOST_ENCODE):          # not a step (cli `compile`, trim_run): the same host-CPU gate the steps 2 and 5 take
        cut_run = remote.run_script(name="compile-cut", body="\n".join(lines) + "\n", timeout=3600)
    if cut_run.rc != 0:
        raise CompilationError("COMPILATION_CUT_FAILED", f"rc={cut_run.rc} {cut_run.stdout[-300:]} {cut_run.stderr[-400:]}")
    probes = [_probe_row(cut_run.stdout, str(index)) for index in range(len(cuts))]
    durations = [_duration_ms(p) for p in probes]
    shapes = {_stream_shape(p) for p in probes}
    if len(shapes) != 1:
        raise CompilationError("COMPILATION_PIECES_INCOMPATIBLE", f"the pieces differ in video/audio format: {sorted(map(str, shapes))}")
    part = f"{out}.part"
    concat = _concat_piece_command(paths, durations, Path(f"{work}/concat.txt"), Path(part))
    concat = concat[:-1] + ["-f", "flv", "-flvflags", "add_keyframe_index", concat[-1]]
    listing = "".join(f"file '{p}'\nduration {d / 1000:.3f}\n" for p, d in zip(paths, durations, strict=True))
    body = (f"cat > {shlex.quote(work)}/concat.txt <<'COMPILATION_CONCAT_EOF'\n{listing}COMPILATION_CONCAT_EOF\n"
            f"{shlex.join(concat)} || {{ echo COMPILATION_CONCAT_FAILED; exit 1; }}\n"
            f"mv {shlex.quote(part)} {shlex.quote(out)}\n"
            f"{_probe_line('final', out)}\n")
    join_run = remote.run_script(name="compile-concat", body=body, timeout=3600)
    if join_run.rc != 0:
        raise CompilationError("COMPILATION_CONCAT_FAILED", f"rc={join_run.rc} {join_run.stdout[-300:]} {join_run.stderr[-400:]}")
    total = _duration_ms(_probe_row(join_run.stdout, "final"))
    if abs(total - sum(durations)) > CONCAT_TOLERANCE_MS:
        raise CompilationError("COMPILATION_CONCAT_DURATION", f"joined {total} ms vs pieces {sum(durations)} ms {durations}")
    return durations, total


def manifest_pieces(cuts: Sequence[PieceCut], answers: Sequence[SegmentAnswer], durations: Sequence[int]) -> list[ManifestPiece]:
    """The joined clock: pieces follow each other with their measured durations."""
    pieces: list[ManifestPiece] = []
    offset = 0
    for index, (cut, answer, measured) in enumerate(zip(cuts, answers, durations, strict=True)):
        pieces.append({"index": index, "segment": answer["segment"], "recording": cut.recording,
                       "start_cue": answer["start_cue"], "end_cue": answer["end_cue"], "why": answer["why"],
                       "source_start_ms": cut.start_ms, "source_end_ms": cut.end_ms, "measured_ms": measured,
                       "compiled_start_ms": offset, "compiled_end_ms": offset + measured})
        offset += measured
    return pieces


# ----- what the steps read ------------------------------------------------------------------------
def load_manifest(clip_dir: Path) -> Manifest | None:
    path = Path(clip_dir) / MANIFEST_FILE
    if not path.exists():
        return None
    data = cast(Manifest, json.loads(path.read_text(encoding="utf-8")))
    if data.get("schema") != MANIFEST_SCHEMA:
        raise CompilationError("COMPILATION_MANIFEST_SCHEMA", f"{path}: schema {data.get('schema')!r}")
    return data


def recording_bcut_path(clip_dir: Path, recording: ManifestRecording) -> Path:
    """Prefer the portable source snapshot created with the compilation; retain old manifests unchanged."""
    snapshot = Path(clip_dir) / WORK_DIR / "recordings" / f"{recording['segment']}.srt"
    return snapshot if snapshot.is_file() else Path(recording["bcut_srt"])


def snapshot_recording_bcuts(clip_dir: Path, recordings: Sequence[RecordingInput]) -> None:
    """Keep the same source bytes with the task when its controller moves to the media host."""
    directory = Path(clip_dir) / WORK_DIR / "recordings"
    directory.mkdir(parents=True, exist_ok=True)
    for number, recording in enumerate(recordings, 1):
        target = directory / f"{number}.srt"
        if not target.exists():
            shutil.copyfile(recording.bcut_srt, target)
        elif target.read_bytes() != recording.bcut_srt.read_bytes():
            raise CompilationError("COMPILATION_BCUT_SNAPSHOT_MISMATCH", f"{recording.name}: {target}")


def _overlaps(manifest: Manifest, start_ms: int, end_ms: int) -> list[tuple[ManifestPiece, int, int]]:
    """Each piece with the part of [start_ms, end_ms) (joined clock) that lies in it: (piece, from, to)."""
    rows = []
    for piece in manifest["pieces"]:
        lo, hi = max(start_ms, piece["compiled_start_ms"]), min(end_ms, piece["compiled_end_ms"])
        if hi > lo:
            rows.append((piece, lo, hi))
    return rows


def _source_range(piece: ManifestPiece, lo: int, hi: int) -> tuple[int, int]:
    """Source interval of the joined-clock part [lo, hi): one to one from the piece's source start, never past the
    declared source end (encoder tail rounding does not create recording content)."""
    begin = piece["source_start_ms"] + (lo - piece["compiled_start_ms"])
    return begin, min(piece["source_end_ms"], piece["source_start_ms"] + (hi - piece["compiled_start_ms"]))


def source_intervals(manifest: Manifest, start_ms: int, end_ms: int) -> list[SourceInterval]:
    """The real recording intervals behind the part [start_ms, end_ms) of the joined clock (a cut snapped inside the
    joined file maps back to the recordings it actually holds)."""
    rows: list[SourceInterval] = []
    for piece, lo, hi in _overlaps(manifest, start_ms, end_ms):
        begin, finish = _source_range(piece, lo, hi)
        if finish > begin:
            rows.append({"recording": piece["recording"], "source_start_ms": begin, "source_end_ms": finish,
                         "compiled_start_ms": lo, "compiled_end_ms": hi})
    return rows


def member_pieces(manifest: Manifest, cut_path: str, start_ms: int, duration_ms: int) -> list[MemberPiece]:
    """FMR member `source.pieces` for a cut taken from the joined file: one piece per real interval, on the cut's own
    clock (the cut starts at 0), each naming its real recording and the interval in it.  `path` stays the local cut, the
    only media the review machine holds."""
    rows: list[MemberPiece] = []
    for piece, lo, hi in _overlaps(manifest, start_ms, start_ms + duration_ms):
        begin, finish = _source_range(piece, lo, hi)
        rows.append({"path": cut_path, "start_ms": lo - start_ms, "end_ms": hi - start_ms,
                     "recording": piece["recording"], "recording_start_ms": begin, "recording_end_ms": finish})
    return rows


# ----- danmaku on the joined clock ------------------------------------------------------------------
_DANMAKU = re.compile(r'<d p="([^"]*)"[^>]*>([^<]*)</d>')


def remap_danmaku(manifest: Manifest, xml_by_recording: Mapping[str, str],
                  session_before: Sequence[Danmaku] | None = None) -> str:
    """One XML on the joined clock from each recording's own.  A comment belongs to the piece whose recording interval
    holds its send time; a comment sent shortly before the first piece or after the last one (the windows step 3 reads:
    up to 90 s before, 30 s after) keeps its place outside the joined clock.  Anything else is left out: before a middle
    piece it would land inside the previous piece's cues.  `session_before` is the live session's chat on the first
    recording's clock (session_events.load): what lies before the first piece comes from it, so a recording's file
    boundary does not cut the lookup, and SC/gifts/guards reach back to the start of the broadcast."""
    from .evidence_attach import DANMAKU_AFTER_S, DANMAKU_BEFORE_S

    pieces = manifest["pieces"]
    rows: list[tuple[float, str]] = []

    def mapped(recording: str, sent: float, before: float) -> float | None:
        for piece in pieces:
            if piece["recording"] == recording and piece["source_start_ms"] / 1000 <= sent <= piece["source_end_ms"] / 1000:
                return piece["compiled_start_ms"] / 1000 + sent - piece["source_start_ms"] / 1000
        first, last = pieces[0], pieces[-1]
        if (session_before is None and first["recording"] == recording
                and first["source_start_ms"] / 1000 - before <= sent < first["source_start_ms"] / 1000):
            return first["compiled_start_ms"] / 1000 + sent - first["source_start_ms"] / 1000
        if last["recording"] == recording and last["source_end_ms"] / 1000 < sent <= last["source_end_ms"] / 1000 + DANMAKU_AFTER_S:
            return last["compiled_start_ms"] / 1000 + sent - last["source_start_ms"] / 1000
        return None

    for recording, xml in xml_by_recording.items():
        for match in _DANMAKU.finditer(xml):
            fields = match.group(1).split(",", 1)
            sent = float(fields[0])
            rest = fields[1] if len(fields) > 1 else ""
            at = mapped(recording, sent, DANMAKU_BEFORE_S)
            if at is not None:
                rows.append((at, f'<d p="{at:.3f},{rest}">{match.group(2)}</d>'))
        for event in recorder_events(xml.encode("utf-8")):
            at = mapped(recording, event["t_s"], event_lookback_s(event["type"]))
            if at is not None:
                rows.append((at, event_xml(event, at)))
    if session_before is not None:
        origin = pieces[0]["source_start_ms"] / 1000
        for event in session_before:
            if event["t_s"] >= origin:
                continue
            at = pieces[0]["compiled_start_ms"] / 1000 + event["t_s"] - origin
            if "type" in event:
                rows.append((at, event_xml(event, at)))
            elif event["t_s"] >= origin - DANMAKU_BEFORE_S:
                rows.append((at, f'<d p="{at:.3f},{event["p_rest"]}">{event["raw"]}</d>'))
    rows.sort(key=lambda r: r[0])
    body = "".join(f'{node}\n' for _, node in rows)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<i>\n{body}</i>\n'


# ----- the entry: select, cut, ASR, danmaku, selection record ---------------------------------------
def _selection_ctx(task: Path, cid: str, remote_dir: str, adapters: Adapters) -> Ctx:
    """The context `_ask_cpa` needs before the clip exists: its clip directory, work directory and adapters.  The state is
    in memory only (the clip's state file is made afterwards by the ordinary `init`)."""
    state = ClipState(task / "state" / f"{cid}.json", cast(ClipData, {"schema": STATE_SCHEMA, "clip": {"cid": cid}, "steps": {}}))
    return Ctx(state=state, session=cast("SessionBlock", {}), clip_dir=task / "clips" / cid, remote_dir=remote_dir,
               adapters=adapters)


def _require_recordings(remote: Remote, cid: str, cuts: Sequence[PieceCut], source_dir: str) -> None:
    """Before any cut: every recording the pieces are cut from is in `source_dir` (SourceMissing names the clip and
    the directory looked in, instead of ffmpeg's own message)."""
    for name in dict.fromkeys(cut.recording for cut in cuts):
        require_source_dir(remote, cid=cid, name=name, directories=[source_dir])


def _remote_exists(remote: Remote, path: str) -> bool:
    return remote.run_script(name="compile-exists", body=f"test -s {shlex.quote(path)}\n", timeout=60).rc == 0


def _write_danmaku(clip_dir: Path, manifest: Manifest, recordings: Sequence[RecordingInput], recording_date: str,
                   adapters: Adapters) -> None:
    """Danmaku of every recording on the joined clock, where step 3 reads its cached XML (kept if already there)."""
    danmaku_local = clip_dir / "danmaku" / "source.xml"
    if danmaku_local.exists():
        return
    from .step3_evidence import DANMAKU_XML_ROOT

    xml = {r.name: adapters.danmaku.fetch(f"{DANMAKU_XML_ROOT}/{recording_date}/{Path(r.name).stem}.xml").decode(
        "utf-8", errors="replace") for r in recordings if any(p["recording"] == r.name for p in manifest["pieces"])}
    first_name = manifest["pieces"][0]["recording"]
    session_rows, unknown = session_events.load(
        adapters.danmaku, f"{DANMAKU_XML_ROOT}/{recording_date}", Path(first_name).stem,
        xml[first_name].encode("utf-8"))
    danmaku_local.parent.mkdir(parents=True, exist_ok=True)
    danmaku_local.write_text(remap_danmaku(manifest, xml, session_rows), encoding="utf-8")
    if unknown:   # a segment that could not be read is unknown, not empty: say which
        (danmaku_local.parent / "unknown-segments.json").write_text(
            json.dumps(unknown, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_entry(work: Path, cid: str, virtual_path: str, manifest: Manifest, virtual_cues: Sequence[SrtCue]
                 ) -> tuple[Path, int, int]:
    """The input_pool entry (selection-result.json) the existing `init` takes as an ordinary candidate."""
    entry_file = work / "selection-result.json"
    first, last = int(virtual_cues[0]["id"]), int(virtual_cues[-1]["id"])
    entry_file.write_text(json.dumps({"input_pool": [{
        "cid": cid, "segment_path": virtual_path, "start_ms": 0, "end_ms": manifest["duration_ms"],
        "selection_scorecard": {"start_cue": first, "end_cue": last}, "hook": manifest["hook"]}]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return entry_file, first, last


def compile_session(*, task: Path, cid: str, theme: str, session: SessionConfig, recordings: Sequence[RecordingInput],
                    source_dir: str, remote_root: str, adapters: Adapters) -> CompileResult:
    clip_dir = task / "clips" / cid
    work = clip_dir / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    remote_dir = f"{remote_root}/{cid}"
    remote: Remote = adapters.remote
    cues_by_segment = [load_cues(r.bcut_srt, source_zero_ms=r.zero_ms) for r in recordings]

    # 1. CPA chooses (one call per session; an identical prompt is read from the cache, not asked again)
    selection_file = work / "selection.json"
    if selection_file.exists():
        stored = json.loads(selection_file.read_text(encoding="utf-8"))
        selection = parse_selection(json.dumps(stored["answer"], ensure_ascii=False), cues_by_segment)
        cpa_event = cast("CpaEvent | None", stored.get("cpa"))
    else:
        record = _ask_cpa(_selection_ctx(task, cid, remote_dir, adapters), SELECT_PURPOSE,
                          select_prompt(theme, cues_by_segment), purpose=SELECT_PURPOSE)
        if not _cpa_ok(record):
            raise CompilationError("CPA_CALL_FAILED", str(record.get("stderr_tail", "")))
        selection = parse_selection(record["response_text"], cues_by_segment)
        cpa_event = _log(record)
        selection_file.write_text(json.dumps({"theme": theme, "answer": _json_from_text(record["response_text"]),
                                              "cpa": cpa_event}, ensure_ascii=False, indent=2), encoding="utf-8")
    if not selection.compilation:
        return CompileResult(False, selection.reason, selection_file=selection_file)

    snapshot_recording_bcuts(clip_dir, recordings)

    # 2. cut and join
    virtual_name = f"compilation-{cid}.flv"
    virtual_path = f"{remote_root}/sources/{virtual_name}"
    cuts = [PieceCut(recordings[a["segment"] - 1].name, f"{source_dir}/{recordings[a['segment'] - 1].name}",
                     *cut_window_ms(cues_by_segment[a["segment"] - 1], a["start_cue"], a["end_cue"]))
            for a in selection.segments]
    manifest_file = clip_dir / MANIFEST_FILE
    if manifest_file.exists() and _remote_exists(remote, virtual_path):
        manifest = cast(Manifest, load_manifest(clip_dir))
    else:
        _require_recordings(remote, cid, cuts, source_dir)
        durations, total = build_media(remote, cuts, work=f"{remote_dir}/{WORK_DIR}", out=virtual_path)
        manifest = {"schema": MANIFEST_SCHEMA, "cid": cid, "theme": theme, "hook": selection.hook, "reason": selection.reason,
                    "virtual_source": virtual_name, "virtual_path": virtual_path, "duration_ms": total,
                    "recordings": [{"segment": n, "name": r.name, "path": f"{source_dir}/{r.name}", "bcut_srt": str(r.bcut_srt),
                                    "bcut_source_zero_ms": r.zero_ms}
                                   for n, r in enumerate(recordings, 1)],
                    "pieces": manifest_pieces(cuts, selection.segments, durations)}
        if cpa_event is not None:
            manifest["cpa"] = cpa_event
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        remote.put(manifest_file, f"{remote_root}/sources/{virtual_name}.compilation.json")
        remote.run_script(name="compile-clean", body=f"rm -rf {shlex.quote(remote_dir)}/{WORK_DIR}/pieces\n", timeout=300)

    # 3. the joined media's BCUT is the "original BCUT" of the virtual recording (BCUT only: jianying has no consumer
    # before step 3, which recognises the cut with both providers as for any clip)
    from .step3_subtitles import FREE_ASR_TOOL, free_asr_body

    bcut_local = work / "virtual.bcut.srt"
    if not bcut_local.exists():
        remote.put(FREE_ASR_TOOL, f"{remote_dir}/{WORK_DIR}/tools/free_asr_paced.py")
        body, timeout = free_asr_body(f"{remote_dir}/{WORK_DIR}", media=virtual_path, providers=("bcut",))
        res = remote.run_script(name="compile-asr", body=body, timeout=timeout)
        if "ASR_OK" not in res.stdout:
            raise CompilationError("ASR_MISSING_SOURCE", f"rc={res.rc} stdout={res.stdout[-200:]} stderr={res.stderr[-300:]}")
        remote.get(f"{remote_dir}/{WORK_DIR}/asr/bcut.srt", bcut_local)
    virtual_cues = parse_srt(bcut_local.read_text(encoding="utf-8"))
    if not virtual_cues:
        raise CompilationError("COMPILATION_ASR_EMPTY", f"{bcut_local} has no cues")

    # 4. danmaku of every recording on the joined clock, where step 3 reads its cached XML
    _write_danmaku(clip_dir, manifest, recordings, session.recording_date, adapters)

    # 5. one input_pool entry: the existing `init` and `run` take it as an ordinary candidate
    entry_file, first, last = _write_entry(work, cid, virtual_path, manifest, virtual_cues)
    return CompileResult(True, selection.reason, hook=manifest["hook"], selection_file=entry_file, bcut_srt=bcut_local,
                         manifest=manifest, first_cue=first, last_cue=last)


def _check_pieces(pieces_ms: Sequence[tuple[int, int]]) -> None:
    if not pieces_ms:
        raise CompilationError("COMPILATION_PIECES_INVALID", "no piece")
    for start, end in pieces_ms:
        if not 0 <= start < end:
            raise CompilationError("COMPILATION_PIECES_INVALID", f"piece {start}-{end} is empty, reversed or before 0")
    for (_, end), (start, _) in zip(pieces_ms, pieces_ms[1:]):
        if start < end:
            raise CompilationError("COMPILATION_PIECES_INVALID", f"pieces overlap or are out of order: {list(pieces_ms)}")


def compile_pieces(*, task: Path, cid: str, recording: RecordingInput, recording_date: str,
                   pieces_ms: Sequence[tuple[int, int]], hook: str, origin: TrimOrigin, bcut_cues: Sequence[SrtCue],
                   source_dir: str, remote_root: str, adapters: Adapters) -> CompileResult:
    """The compilation entry for intervals the caller already decided (a finished clip with holes cut out of it,
    trim_run.py): `pieces_ms` are millisecond intervals of the one `recording`, ascending and disjoint, cut exactly as
    given (no margins, no CPA selection).  compilation.json carries `kind: "trim"` and `origin`, the theme and each
    piece's `why` are empty.  The joined file's BCUT is `bcut_cues` (already on the joined clock): no ASR is run.
    Everything else is what `compile_session` writes, except the joined danmaku file, so the ordinary `init` takes the
    result."""
    _check_pieces(pieces_ms)
    clip_dir = task / "clips" / cid
    work = clip_dir / WORK_DIR
    work.mkdir(parents=True, exist_ok=True)
    remote_dir = f"{remote_root}/{cid}"
    remote: Remote = adapters.remote
    snapshot_recording_bcuts(clip_dir, [recording])
    cuts = [PieceCut(recording.name, f"{source_dir}/{recording.name}", start, end) for start, end in pieces_ms]
    virtual_name = f"compilation-{cid}.flv"
    virtual_path = f"{remote_root}/sources/{virtual_name}"
    manifest_file = clip_dir / MANIFEST_FILE
    if manifest_file.exists() and _remote_exists(remote, virtual_path):
        manifest = cast(Manifest, load_manifest(clip_dir))
    else:
        _require_recordings(remote, cid, cuts, source_dir)
        durations, total = build_media(remote, cuts, work=f"{remote_dir}/{WORK_DIR}", out=virtual_path)
        answers: list[SegmentAnswer] = [{"segment": 1, "start_cue": 0, "end_cue": 0, "why": ""} for _ in cuts]
        manifest = {"schema": MANIFEST_SCHEMA, "cid": cid, "theme": "", "hook": hook, "reason": "",
                    "virtual_source": virtual_name, "virtual_path": virtual_path, "duration_ms": total,
                    "recordings": [{"segment": 1, "name": recording.name, "path": f"{source_dir}/{recording.name}",
                                    "bcut_srt": str(recording.bcut_srt), "bcut_source_zero_ms": recording.zero_ms}],
                    "pieces": manifest_pieces(cuts, answers, durations), "kind": KIND_TRIM, "origin": origin}
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        remote.put(manifest_file, f"{remote_root}/sources/{virtual_name}.compilation.json")
        remote.run_script(name="compile-clean", body=f"rm -rf {shlex.quote(remote_dir)}/{WORK_DIR}/pieces\n", timeout=300)
    if not bcut_cues:
        raise CompilationError("COMPILATION_ASR_EMPTY", "the joined clip has no BCUT cues")
    bcut_local = work / "virtual.bcut.srt"
    bcut_local.write_text(format_srt(bcut_cues), encoding="utf-8")
    virtual_cues = parse_srt(bcut_local.read_text(encoding="utf-8"))
    # no danmaku is fetched: the successor takes its decided subtitles by import and reads none (and the archive host
    # is not reachable from every place this runs)
    entry_file, first, last = _write_entry(work, cid, virtual_path, manifest, virtual_cues)
    return CompileResult(True, "", hook=manifest["hook"], selection_file=entry_file, bcut_srt=bcut_local,
                         manifest=manifest, first_cue=first, last_cue=last)
