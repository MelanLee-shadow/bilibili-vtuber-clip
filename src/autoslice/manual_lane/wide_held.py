"""Post-review listening material for still-held doubts; never modifies the doubt evidence table."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypeVar, TypedDict, cast

from . import compilation, step3_evidence, subtitle_track
from .runner import Ctx
from .step_io import _write_atomic

if TYPE_CHECKING:
    from .srt_text import SrtCue
    from .stage_package import ReviewDoubt


class WideTarget(TypedDict):
    id: str
    target_s: list[float]
    win: str


class WideRound(TypedDict, total=False):
    phase: str
    held_ids: list[str]
    after_holds: int
    counts: dict[str, int]
    review_md: str | None
    skipped: list[str]
    cpa_cached: bool


class WideProgress(TypedDict):
    targets: list[WideTarget]
    windows: list[step3_evidence.ListeningWindow]
    rounds: list[WideRound]
    before_holds: int
    pending_rewind: NotRequired[list[str]]


class AsrResult(TypedDict):
    records: list[step3_evidence.WindowAsrRecord]
    stats: dict[str, int]


class ListenResult(TypedDict):
    records: list[step3_evidence.ListeningRecord]
    stats: dict[str, int]


T = TypeVar("T")


class WideListeningError(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _save(path: Path, value: object) -> None:
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def snapshot_before(ctx: Ctx) -> None:
    before = ctx.clip_dir / "listen/wide-held/before"
    before.mkdir(parents=True, exist_ok=True)
    for rel in ("subs/final.srt", "subs/doubts.json", "speaker/speaker.srt", "pkg/REVIEW.md"):
        source = ctx.clip_dir / rel
        if source.exists():
            dest = before / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copyfile(source, dest)


def prepare(ctx: Ctx, held: Sequence[ReviewDoubt], numbers: dict[str, list[int]]) -> WideProgress:
    """Freeze one window per held doubt on the final track and the original pre-listening text files."""
    track = subtitle_track.read_track(ctx.clip_dir)
    clip = ctx.state.data["clip"]
    duration = (clip["end_ms"] - clip["start_ms"]) / 1000
    manifest = compilation.load_manifest(ctx.clip_dir)
    offset = clip["start_ms"] / 1000
    pieces = ([(max(0.0, p["compiled_start_ms"] / 1000 - offset),
                min(duration, p["compiled_end_ms"] / 1000 - offset))
               for p in manifest["pieces"] if p["compiled_end_ms"] / 1000 > offset
               and p["compiled_start_ms"] / 1000 < offset + duration]
              if manifest else [(0.0, duration)])
    bcut = step3_bcut(ctx)
    targets: list[WideTarget] = []
    windows: dict[str, step3_evidence.ListeningWindow] = {}
    for doubt in held:
        did = str(doubt["id"])
        owned = numbers.get(did, [])
        if owned:
            first, last = min(owned), max(owned)
            start = float(track[first - 1]["start_s"])
            end = float(track[last - 1]["end_s"])
        else:
            span = subtitle_track.doubt_span(cast(subtitle_track.DoubtTime, doubt), bcut, int(clip["start_ms"]))
            if span is None:
                raise ValueError(f"held doubt {did} has no usable time span")
            start, end = span
            first, last = 1, 0
        if not (0 <= start < end <= duration):
            raise ValueError(f"held doubt {did} has an invalid cut-clock span")
        # Replace the possibly split target with one synthetic cue, leaving its neighbours as context.
        cues: list[SrtCue] = [{"id": n, "start_s": float(c["start_s"]), "end_s": float(c["end_s"]), "text": c["text"]}
                for n, c in enumerate(track, 1) if not first <= n <= last]
        cues.append({"id": -1, "start_s": start, "end_s": end, "text": ""})
        cues.sort(key=lambda c: (c["start_s"], c["end_s"], c["id"]))
        window = step3_evidence.wide_listen_windows(cues, {-1}, duration, pieces)[0]
        existing = windows.setdefault(window["win"], window)
        if existing["audio"] != window["audio"]:
            raise ValueError("wide window identity collision")
        targets.append({"id": did, "target_s": [start, end], "win": window["win"]})
    snapshot_before(ctx)
    return {"targets": targets, "windows": list(windows.values()), "rounds": [], "before_holds": len(held)}


def step3_bcut(ctx: Ctx) -> dict[int, SrtCue]:
    path = ctx.clip_dir / "asr/bcut.srt"
    if not path.exists():
        return {}
    from .srt_text import parse_srt

    return {c["id"]: c for c in parse_srt(path.read_text(encoding="utf-8"))}


def _load_or_run(path: Path, run: Callable[[], T]) -> T:
    if path.exists():
        return cast(T, json.loads(path.read_text(encoding="utf-8")))
    value = run()
    _save(path, value)
    return value


def collect(ctx: Ctx, wide: WideProgress, held_ids: list[str], number: int) -> tuple[str, dict[str, int]]:
    """Collect only the remaining ids; persist complete provider results for interruption recovery."""
    targets = [t for t in wide["targets"] if t["id"] in held_ids]
    if {t["id"] for t in targets} != set(held_ids):
        raise ValueError("held IDs changed outside the original wide review scope")
    wanted = {t["win"] for t in targets}
    windows = [w for w in wide["windows"] if w["win"] in wanted]
    root = ctx.clip_dir / "listen/wide-held"
    root.mkdir(parents=True, exist_ok=True)

    def asr() -> AsrResult:
        stats: dict[str, int] = {}
        records = step3_evidence._asr_window_pass(ctx, "wide-held-1", windows, ("mai", "moss"), stats_out=stats)
        if records is None:
            raise WideListeningError("ASR_WINDOW_TOOL_FAILED", "wide held window ASR")
        return {"records": records, "stats": stats}

    def listen(slug: str, label: str) -> ListenResult:
        stats: dict[str, int] = {}
        models = [m for m in step3_evidence.LISTEN_MODELS if m["slug"] == slug]
        records = step3_evidence._listen_pass(ctx, label, windows, models, prompt="listen_wide.md", stats=stats)
        if records is None:
            raise WideListeningError("LISTEN_TOOL_FAILED", "wide held listening")
        return {"records": records, "stats": stats}

    first_asr = _load_or_run(root / "asr-1.json", asr)
    first_listen = _load_or_run(root / "listen-1.json", lambda: listen("38m", "wide-held-1"))
    argon: ListenResult = (_load_or_run(root / "listen-2.json", lambda: listen("argon", "wide-held-2"))
             if number == 2 else {"records": [], "stats": {}})
    by_win = {w["win"]: w for w in windows}
    blocks = []
    for target in targets:
        win = by_win[target["win"]]
        lo = float(win["audio"][0])
        readings = []
        for rec in first_asr["records"]:
            if rec.get("win") != target["win"]:
                continue
            segments = [{"start_s": round(lo + float(s["start_ms"]) / 1000, 3),
                         "end_s": round(lo + float(s["end_ms"]) / 1000, 3),
                         "text": s.get("text", ""), "speaker": s.get("speaker")}
                        for s in rec.get("segments", [])]
            readings.append({"channel": rec.get("provider"), "status": rec.get("status"),
                             "segments": segments, "error": rec.get("message", "")})
        for listen_rec in [*first_listen["records"], *argon["records"]]:
            if listen_rec.get("win") != target["win"]:
                continue
            lines = []
            for line in listen_rec.get("lines", []):
                if isinstance(line, dict):
                    start = line.get("start_s", line.get("start"))
                    lines.append({"start_s": round(lo + float(start), 3) if start is not None else None,
                                  "text": line.get("text", "")})
                else:
                    lines.append({"start_s": None, "text": str(line)})
            readings.append({"channel": listen_rec.get("slug"), "status": listen_rec.get("status"),
                             "lines": lines, "error": listen_rec.get("error", "")})
        blocks.append({"id": target["id"], "window_s": win["audio"], "target_s": target["target_s"],
                       "readings": readings})
    counts = {p: sum(1 for r in first_asr["records"] if r.get("provider") == p
                     and not r.get("reused") and r.get("status") != "MISSING") for p in ("mai", "moss")}
    counts["38m"] = int(first_listen["stats"].get("new", 0))
    counts["argon"] = int(argon["stats"].get("new", 0))
    counts["windows"] = len(windows)
    if number == 2:
        counts.update({"mai": 0, "moss": 0, "38m": 0})
    addition = ("\n\n## 加长窗口的新听写\n每条覆盖标注的整段成片时间，包含目标句及前后句；时间均为成片秒数。"
                "各路为整窗听写，不能直接当成本句；说话人编号仅为各路本次标记。本次复审未直接听音。\n"
                + json.dumps(blocks, ensure_ascii=False, indent=1) + "\n")
    return addition, counts
