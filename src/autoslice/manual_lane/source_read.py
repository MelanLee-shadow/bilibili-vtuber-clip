"""The one way the manual lane reads a recording on the media host.

Every command that takes a source recording as ffmpeg input is built here and nowhere else (tests/manual_lane/
test_source_read_guard.py scans the lane's sources for it).  What the three readers of a recording (the first cut,
the pieces of a compilation, the pieces of a trim) and the silence probe before the cut share:

- where the recording is: the clip's own record (`clip.source_dir`), else the directory of this run, else the work
  directory's `sources/`; `clip_source_dir` finds it and records it, `require_source` names what was looked for when
  there is no file;
- who can read it: the recording may be a link into a mount only root can read, so the existence check and the
  reading run as root through `adapters.root_script`, and what they write is handed back to the lane's user;
- how it is encoded: one set of encoder arguments, `source_cut_script` below; the whole-recording audio track that discovery
  recognises is `source_audio_script`.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from .adapters import Remote, root_script
from .state import ClipState, StateError

HOST_ENCODE = "host_encode"   # lane_rules concurrency row: how many clips of the process encode video on the media host at once
PREROLL_S = 30.0       # coarse input seek before the cut point: decoding headroom for keyframe landing errors (FLV, TS)
AUDIO_FILTER = "aresample=async=1:first_pts=0"      # fills audio timestamp gaps without shifting speech
VIDEO_ENCODE = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", "60")
AUDIO_ENCODE = ("-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2")


class SourceMissing(StateError):
    """No file of the named recording in any directory it could be in; the message says which were looked in."""


def _as_root(command: str, owned_dir: str, *, recursive: bool = True) -> str:
    """`command` as root, its exit code kept, `owned_dir` handed back to the lane's user (root_script puts the command in
    single quotes, so a quote inside it is closed, escaped and reopened)."""
    return root_script(command.replace("'", "'\\''"), env=(), owned=owned_dir, recursive=recursive)


def source_cut_script(source: str, *, start_s: float, duration_s: float, output: str, fade: str | None = None) -> str:
    """The script that cuts `duration_s` seconds from `start_s` of the recording `source` into `output`: coarse input
    seek, then exact seek on the output side, constant 60 fps, the audio clock gap-filled; `fade` is an extra audio
    filter (the short fades at the seams of a joined clip) whose clock is the product's: its time 0 is the first frame of
    `output`, however far before the cut point the coarse seek lands.  The filters run on the clock of the coarse seek
    (the part before the cut point is thrown away only after them, by the output-side seek), so the clock is moved back
    by that part around `fade` and forward again after it: the cut point, the throw-away and the audio/video offset are
    the ones of a cut without `fade`.  The script ends with `exit <rc of the cut>`: put it in a subshell to go on after it."""
    coarse = max(0.0, start_s - PREROLL_S)
    lead = start_s - coarse
    audio = AUDIO_FILTER + (f",asetpts=PTS-{lead:.6f}/TB,{fade},asetpts=PTS+{lead:.6f}/TB" if fade else "")
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{coarse:.3f}", "-i", source,
            "-ss", f"{lead:.3f}", "-map", "0:v:0", "-map", "0:a:0", *VIDEO_ENCODE, "-af", audio,
            *AUDIO_ENCODE, "-t", f"{duration_s:.3f}", "-movflags", "+faststart", output]
    return _as_root(shlex.join(argv), str(PurePosixPath(output).parent))


def source_audio_probe_script(source: str, windows: Mapping[str, tuple[float, float]], *, audio_filter: str,
                              log_dir: str) -> str:
    """The script that runs `audio_filter` over each window (start, end) of the recording and leaves ffmpeg's report in
    `<log_dir>/snap-<name>.log`.  A failed window leaves a log without the filter's lines; the exit code is not the
    windows' result."""
    commands = []
    for name, (start, end) in windows.items():
        argv = ["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", source,
                "-vn", "-af", audio_filter, "-f", "null", "-"]
        commands.append(f"{shlex.join(argv)} 2> {shlex.quote(f'{log_dir}/snap-{name}.log')}")
    return _as_root("; ".join(commands), log_dir)


def source_audio_script(source: str, *, workdir: str, fingerprint: str) -> str:
    """The script that makes `<workdir>/audio.mp3` (16 kHz mono, 64 kb/s) from the whole recording `source`, as root, and
    leaves the recording's `fingerprint` in `<workdir>/audio.fp`.  A track already there under the same fingerprint is
    kept (the script prints AUDIO_REUSED, else AUDIO_OK); the track is written beside its name and renamed into place,
    so a killed run never leaves half a file under the name."""
    audio, part, mark = f"{workdir}/audio.mp3", f"{workdir}/audio.part.mp3", f"{workdir}/audio.fp"
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "2", "-i", source, "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", part]
    kept = f'[ -s {shlex.quote(audio)} ] && [ "$(cat {shlex.quote(mark)} 2>/dev/null)" = {shlex.quote(fingerprint)} ]'
    command = (f"if {kept}; then echo AUDIO_REUSED; else rm -f {shlex.quote(audio)} {shlex.quote(part)} {shlex.quote(mark)}; "
               f"{shlex.join(argv)} && mv {shlex.quote(part)} {shlex.quote(audio)} "
               f"&& printf %s {shlex.quote(fingerprint)} > {shlex.quote(mark)} && echo AUDIO_OK; fi")
    return _as_root(command, workdir)


def source_exists_script(path: str) -> str:
    """Exit code 0 when `path` (a link is followed) is a non-empty file; asked as root, like the reading."""
    return root_script(f"test -s {shlex.quote(path)}".replace("'", "'\\''"), env=())


def find_source_dir(remote: Remote, name: str, directories: Sequence[str]) -> str | None:
    """The first of `directories` that holds a file `name`, or None."""
    for directory in dict.fromkeys(directories):
        path = f"{directory.rstrip('/')}/{name}"
        if remote.run_script(name="source-exists", body=source_exists_script(path), timeout=120).rc == 0:
            return directory
    return None


def require_source_dir(remote: Remote, *, cid: str, name: str, directories: Sequence[str]) -> str:
    found = find_source_dir(remote, name, directories)
    if found is None:
        raise SourceMissing(f"{cid}: the recording {name} is in none of the directories looked in: "
                            f"{', '.join(dict.fromkeys(directories)) or '(none)'}")
    return found


def clip_source_dir(remote: Remote, state: ClipState, *, requested: str | None, default_dir: str) -> str:
    """Where the recording of the state's clip is: the directory recorded in the clip, else `requested` (this run's
    --source-dir), else `default_dir`, the first of them that holds the file.  A clip without a record keeps the
    directory it was found in (`clip.source_dir`), so no later run needs the argument."""
    clip = state.data["clip"]
    recorded = clip.get("source_dir")
    found = require_source_dir(remote, cid=clip["cid"], name=clip["source_name"],
                               directories=[d for d in (recorded, requested, default_dir) if d])
    if not recorded:
        clip["source_dir"] = found
        state.save()
    return found
