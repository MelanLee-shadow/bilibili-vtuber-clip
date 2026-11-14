"""Listening queue for 维护者 (`listen-queue`) and import of his labels (`listen-import`).

Only doubts that are UNRESOLVED and affect meaning enter the queue.  Each item carries: a ±6 s mp3 cut from the
clip's cut.mp4 (cut on OCI3, fetched as a small file), the current subtitle, three cues either side, every
source's reading, and candidate texts.  The page is one static index.html: audio is muted and never autoplays.

The export keeps the family of live-20261030-pilot/review: {"labels": {...}, "exported_at", "cues_version"}.
Keys are "<cid>:<doubt_id>" because doubt ids are only unique within a clip.  Import validates every label
against the clip's doubts.json (cue id and current text) and writes nothing unless all labels are valid.
Stage 3 re-checks the anchors before using a label, so a renumbered cue fails closed.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict, cast

from . import text_overlap
from .adapters import RemoteResult
from .doubt_policy import witness_family
from .gates import match_label_anchors
from .stage_impl import REVIEWER_SOURCE, fmt_ts, parse_srt
from .state import ClipState, utc_now

if TYPE_CHECKING:
    from .adapters import Remote
    from .doubt_policy import FinalDoubt
    from .gates import AnchorLabel
    from .srt_text import SrtCue, TimedText
    from .state import ClipInfo
    from .step3_evidence import IvanLabel

QUEUE_VERSION = "listen-queue-v1"
AUDIO_PAD_S = 6.0
CONTEXT_CUES = 3
MP3_BITRATE = "48k"
CHOICES = ("candidate", "custom")


class ListenError(ValueError):
    """A queue, export or import request that cannot be honoured (nothing is written)."""


class QueueCandidate(TypedDict):
    text: str
    sources: list[str]
    label: NotRequired[str]


class QueueContext(TypedDict):
    cue_id: int
    start_s: float
    text: str
    target: bool


class QueueItem(TypedDict):
    key: str
    cid: str
    doubt_id: str
    cue_id: int
    start_s: float
    end_s: float
    current: str
    abs_start_ms: int
    abs_end_ms: int
    reason_key: str
    cpa_state: str
    cpa_text: str | None
    readings: dict[str, str]
    burned: str
    candidates: list[QueueCandidate]
    context: list[QueueContext]
    danmaku: list[str]
    audio_file: str
    audio_start_s: float
    audio_dur_s: float


class QueueState(TypedDict):
    clip: ClipInfo


class QueuePlan(TypedDict):
    cid: str
    remote_dir: str
    cut_remote: str
    items: list[QueueItem]


class QueueSummary(TypedDict):
    page: str
    items: int
    per_clip: dict[str, int]
    audio_dir: str


class ExportLabel(TypedDict):
    cid: str
    doubt_id: str
    abs_start_ms: int
    abs_end_ms: int
    current: str
    text: str
    choice: str
    note: str


class LabelImportResult(TypedDict):
    export_sha256: str
    labels: int
    clips: dict[str, int]


def queue_key(cid: str, doubt_id: str) -> str:
    return f"{cid}:{doubt_id}"


def _audio_file(cid: str, doubt_id: str) -> str:
    return f"{cid}_{doubt_id}.mp3"


def _candidates(current: str, cpa_text: str | None, readings: Mapping[str, str]) -> list[QueueCandidate]:
    """Distinct readings by normalised text; the current subtitle comes first, then CPA's wording, then sources."""
    pool: list[tuple[str, str]] = [("现稿", current)]
    if cpa_text:
        pool.append(("CPA 裁决", cpa_text))
    pool.extend((src, txt) for src, txt in readings.items() if txt)
    merged: dict[str, QueueCandidate] = {}
    for source, text in pool:
        key = text_overlap.key(text)
        if not key:
            continue
        slot = merged.setdefault(key, {"text": text, "sources": []})
        if source not in slot["sources"]:
            slot["sources"].append(source)
    return list(merged.values())


def _final_text_at(final_cues: Sequence[TimedText], cue: TimedText) -> str:
    """The final subtitle text whose start falls inside this BCUT cue's span (one or more final cues)."""
    texts = [str(f["text"]).replace(chr(10), " ") for f in final_cues
             if float(cue["start_s"]) - 0.5 <= float(f["start_s"]) <= float(cue["end_s"]) + 0.5]
    return " ".join(texts).strip()


_SOURCE_NAME = {"asr:jianying": "剪映", "asr:mai": "MAI", "asr:moss": "MOSS", "gemini": "Gemini"}


def _collapsed(text: str) -> str:
    """Normalised text with immediately repeated characters collapsed, so 对对 and 对 compare equal (display only)."""
    return re.sub(r"(.)\1+", r"\1", text_overlap.key(text))


def _focus_candidates(burned: str, before: str, readings: Mapping[str, str]) -> list[QueueCandidate]:
    """The two candidates of a doubt 维护者 was asked to hear explicitly: the text now burned in (现稿) and the reading
    that the maintained override replaced (判定前读法).  A source counts for a candidate when its reading matches the
    candidate after collapsing repeated characters.  This only labels the page; the choice stays with 维护者's ear."""
    out: list[QueueCandidate] = []
    seen: set[str] = set()
    for label, text in (("现稿", burned), ("判定前读法", before)):
        key = _collapsed(text)
        if not key or key in seen:
            continue
        seen.add(key)
        families: list[str] = []
        for source, reading in readings.items():
            family = witness_family(source)
            if family and reading and _collapsed(reading) == key and family not in families:
                families.append(family)
        names = [_SOURCE_NAME.get(f, f) for f in families]
        out.append({"text": text, "label": label, "sources": names or ["无独立来源"]})
    return out


def build_items(*, cid: str, doubts: Sequence[FinalDoubt], bcut_cues: Sequence[SrtCue],
                final_cues: Sequence[TimedText], cut_len: float,
                source_offset_s: float = 0.0, include: Iterable[str] = (),
                dev_low_confidence: bool = False) -> list[QueueItem]:
    """Queue items for one clip.  Pure: no I/O.  final_cues are the stage-3 subtitles in bcut order.
    `include` names doubt ids to queue even when their state is not UNRESOLVED (an explicit request only).
    `dev_low_confidence` (development only, TASK-5): also queue the doubts CPA marked 低把握; the final version does not.
    """
    # final.srt may have more cues than bcut (generation splits long cues), so the final text is matched by time
    position = {c["id"]: i for i, c in enumerate(bcut_cues)}
    listed = {str(x) for x in include}
    found: set[str] = set()
    items: list[QueueItem] = []
    for doubt in doubts:
        explicit = str(doubt.get("id")) in listed
        if explicit:
            found.add(str(doubt.get("id")))
        elif not (doubt.get("state") == "UNRESOLVED" and doubt.get("affects_meaning")
                  or dev_low_confidence and doubt.get("confidence") == "低"):
            continue
        cue_id = int(doubt["cue_id"])
        if cue_id not in position:
            raise ListenError(f"{cid}: doubt {doubt.get('id')} points at cue {cue_id}, not in bcut")
        pos = position[cue_id]
        cue = bcut_cues[pos]
        a = max(0.0, float(cue["start_s"]) - AUDIO_PAD_S)
        b = min(cut_len, float(cue["end_s"]) + AUDIO_PAD_S)
        context: list[QueueContext] = []
        for j in range(max(0, pos - CONTEXT_CUES), min(len(bcut_cues), pos + CONTEXT_CUES + 1)):
            context.append({"cue_id": bcut_cues[j]["id"], "start_s": float(bcut_cues[j]["start_s"]),
                            "text": _final_text_at(final_cues, bcut_cues[j]), "target": j == pos})
        readings = {str(k): str(v) for k, v in dict(doubt.get("readings") or {}).items()}
        current = str(doubt["current"])
        if explicit:
            applied = str(doubt.get("final_text") or "").strip() if doubt.get("state") in ("ADOPT", "FIT_CONTEXT", "KEEP") else ""
            burned = applied or current
            candidates = _focus_candidates(burned, current, readings)
        else:
            burned = current
            candidates = _candidates(current, doubt.get("final_text"), readings)
        items.append({
            "key": queue_key(cid, str(doubt["id"])), "cid": cid, "doubt_id": str(doubt["id"]), "cue_id": cue_id,
            "start_s": float(cue["start_s"]), "end_s": float(cue["end_s"]), "current": str(doubt["current"]),
            "abs_start_ms": round((source_offset_s + float(cue["start_s"])) * 1000),
            "abs_end_ms": round((source_offset_s + float(cue["end_s"])) * 1000),
            "reason_key": str(doubt.get("reason_key") or ""), "cpa_state": str(doubt.get("state")),
            "cpa_text": doubt.get("final_text"), "readings": readings,
            "burned": burned, "candidates": candidates,
            "context": context, "danmaku": list(doubt.get("danmaku") or [])[:12],
            "audio_file": _audio_file(cid, str(doubt["id"])), "audio_start_s": round(a, 3),
            "audio_dur_s": round(b - a, 3),
        })
    missing = listed - found
    if missing:
        raise ListenError(f"{cid}: named doubt(s) not in doubts.json: {', '.join(sorted(missing))}")
    return items


def clip_queue(*, cid: str, state_data: QueueState, clip_dir: Path,
               include: Iterable[str] = (), dev_low_confidence: bool = False) -> QueuePlan | None:
    """Queue plan for one clip, or None when the clip has no stage-3 doubts on disk yet."""
    subs = clip_dir / "subs"
    bcut_path = clip_dir / "asr" / "bcut.srt"
    if not (subs / "doubts.json").exists() or not (subs / "final.srt").exists() or not bcut_path.exists():
        return None
    clip = state_data["clip"]
    if not (clip_dir / "cut" / "probe.json").exists():
        return None   # the cut step has not produced its product yet
    cut_len = (int(clip["end_ms"]) - int(clip["start_ms"])) / 1000
    items = build_items(cid=cid, doubts=cast("list[FinalDoubt]", json.loads((subs / "doubts.json").read_text(encoding="utf-8"))),
                        bcut_cues=parse_srt(bcut_path.read_text(encoding="utf-8")),
                        final_cues=parse_srt((subs / "final.srt").read_text(encoding="utf-8")), cut_len=cut_len,
                        source_offset_s=float(clip["start_ms"]) / 1000, include=include,
                        dev_low_confidence=dev_low_confidence)
    return {"cid": cid, "remote_dir": clip["remote_dir"], "cut_remote": f"{clip['remote_dir']}/cut/cut.mp4",
            "items": items}


def cut_audio(remote: Remote, plan: QueuePlan, audio_dir: Path) -> None:
    """One remote script cuts every excerpt of the clip to mp3 on OCI3; the small files are then fetched."""
    items = list(plan["items"])
    if not items:
        return
    rd, cut, cid = plan["remote_dir"], plan["cut_remote"], plan["cid"]
    lines = [f"mkdir -p {rd}/listen-queue", "rc=0"]
    for it in items:
        lines.append(f"ffmpeg -hide_banner -loglevel error -y -ss {it['audio_start_s']:.3f} "
                     f"-t {it['audio_dur_s']:.3f} -i {cut} -vn -ac 1 -ar 22050 -b:a {MP3_BITRATE} "
                     f"{rd}/listen-queue/{it['audio_file']} || rc=1")
    lines += ["echo AUDIO_DONE", "exit $rc"]
    res: RemoteResult = remote.run_script(name=f"listen-queue-{cid}", body="\n".join(lines) + "\n", timeout=1800)
    if res.rc != 0 or "AUDIO_DONE" not in res.stdout:
        raise ListenError(f"{cid}: audio cut failed rc={res.rc}: {res.stderr[-200:]}")
    audio_dir.mkdir(parents=True, exist_ok=True)
    for it in items:
        remote.get(f"{rd}/listen-queue/{it['audio_file']}", audio_dir / it["audio_file"])


# ----- page -------------------------------------------------------------------------------
_CSS = """
body{margin:0;font:15px/1.5 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
background:#f4f5f7;color:#1b2027}
header{position:sticky;top:0;background:#ffffffee;border-bottom:1px solid #dfe3e8;padding:10px 16px;z-index:5}
header h1{font-size:19px;margin:0 0 4px}
main{max-width:980px;margin:0 auto;padding:12px}
.q{background:#fff;border:1px solid #dfe3e8;border-left:5px solid #d9730d;border-radius:8px;padding:10px 14px;margin:12px 0}
.q h2{font-size:15px;margin:0 0 4px}
.muted{color:#5f6b7a;font-size:13px}
.cur{font-size:17px;margin:6px 0}
.ctx{font-size:13.5px;color:#3d4652;border-top:1px dashed #dfe3e8;padding-top:6px;margin-top:6px}
.ctx .t{font-weight:700;color:#d9730d}
ul.rd{margin:4px 0 6px;padding-left:20px;font-size:13.5px}
fieldset{border:1px solid #dfe3e8;border-radius:6px;margin:8px 0;padding:6px 10px}
fieldset label{display:block;margin:3px 0}
input[type=text]{width:70%;font:inherit;padding:2px 6px}
textarea{width:100%;min-height:140px;font:12px/1.4 ui-monospace,Menlo,monospace}
button{font:inherit;padding:4px 12px}
"""

_JS = """
(function () {
  var VERSION = document.body.getAttribute('data-version');
  var KEY = 'manual-lane-listen-queue-v1';
  var cards = [].slice.call(document.querySelectorAll('article.q'));
  function keyOf(card) { return card.getAttribute('data-cid') + ':' + card.getAttribute('data-doubt'); }
  function collect() {
    var labels = {};
    cards.forEach(function (card) {
      var on = card.querySelector('input[type=radio]:checked');
      if (!on) { return; }
      var text = on.value === 'custom' ? (card.querySelector('.custom').value || '') : (on.getAttribute('data-text') || '');
      text = text.trim();
      if (!text) { return; }
      labels[keyOf(card)] = {
        cid: card.getAttribute('data-cid'), doubt_id: card.getAttribute('data-doubt'),
        cue_id: Number(card.getAttribute('data-cue')), abs_start_ms: Number(card.getAttribute('data-abs-start')),
        abs_end_ms: Number(card.getAttribute('data-abs-end')), current: card.getAttribute('data-current'),
        text: text, choice: on.value === 'custom' ? 'custom' : 'candidate',
        note: (card.querySelector('.note').value || '').trim()
      };
    });
    return { labels: labels, exported_at: new Date().toISOString(), cues_version: VERSION };
  }
  function save() {
    var st = {};
    cards.forEach(function (card) {
      var on = card.querySelector('input[type=radio]:checked');
      st[keyOf(card)] = { value: on ? on.value : null, custom: card.querySelector('.custom').value,
                          note: card.querySelector('.note').value };
    });
    try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* storage unavailable: export still works */ }
  }
  function restore() {
    var st = {};
    try { st = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { st = {}; }
    cards.forEach(function (card) {
      var s = st[keyOf(card)];
      if (!s) { return; }
      if (s.value !== null) {
        var r = card.querySelector('input[type=radio][value="' + s.value + '"]');
        if (r) { r.checked = true; }
      }
      card.querySelector('.custom').value = s.custom || '';
      card.querySelector('.note').value = s.note || '';
    });
  }
  function refresh() {
    var out = collect();
    document.getElementById('cnt').textContent = '已标 ' + Object.keys(out.labels).length + ' / ' + cards.length;
    document.getElementById('exp').value = JSON.stringify(out, null, 1);
    save();
  }
  document.addEventListener('change', refresh);
  document.addEventListener('input', refresh);
  document.getElementById('bExport').addEventListener('click', function () {
    var out = collect();
    var text = JSON.stringify(out, null, 1);
    var stamp = new Date().toISOString().replace(/[:.]/g, '-');
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    a.download = 'reviewer_labels_' + stamp + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    document.getElementById('exp').value = text;
  });
  restore();
  refresh();
})();
"""


def _card(index: int, it: QueueItem) -> str:
    def esc(v: object) -> str:
        return html.escape(str(v), quote=True)

    ctx_lines = "".join(
        f"<div class=\"{'t' if c['target'] else ''}\">{fmt_ts(c['start_s'])} {esc(c['text'])}</div>"
        for c in it["context"])
    reads = "".join(f"<li>{esc(src)}：{esc(txt)}</li>" for src, txt in it["readings"].items())
    radios = []
    for n, cand in enumerate(it["candidates"]):
        tag = f"<b>{esc(cand['label'])}</b> " if cand.get("label") else ""
        radios.append(f"<label><input type=\"radio\" name=\"c{index}\" value=\"{n}\" data-text=\"{esc(cand['text'])}\"> "
                      f"{tag}{esc(cand['text'])} <span class=\"muted\">（{esc('、'.join(cand['sources']))}）</span></label>")
    burned = it.get("burned", it["current"])
    cur_html = f"<div class=\"cur\">现稿：<b>{esc(burned)}</b>"
    if burned != it["current"]:
        cur_html += f" <span class=\"muted\">（判定前：{esc(it['current'])}）</span>"
    cur_html += "</div>\n"
    radios.append(f"<label><input type=\"radio\" name=\"c{index}\" value=\"custom\"> 手填："
                  "<input type=\"text\" class=\"custom\"></label>")
    danmaku = ""
    if it["danmaku"]:
        danmaku = f"<div class=\"muted\">弹幕线索：{esc(' / '.join(it['danmaku']))}</div>"
    return (
        f"<article class=\"q\" data-cid=\"{esc(it['cid'])}\" data-doubt=\"{esc(it['doubt_id'])}\" "
        f"data-cue=\"{it['cue_id']}\" data-abs-start=\"{it['abs_start_ms']}\" data-abs-end=\"{it['abs_end_ms']}\" "
        f"data-current=\"{esc(it['current'])}\">\n"
        f"<h2>{index}. {esc(it['cid'])} · {esc(it['doubt_id'])} · 第 {it['cue_id']} 句 "
        f"<span class=\"muted\">{fmt_ts(it['start_s'])}–{fmt_ts(it['end_s'])} · {esc(it['reason_key'])} · "
        f"CPA：{esc(it['cpa_state'])}</span></h2>\n"
        f"{cur_html}"
        f"<audio controls muted preload=\"none\" src=\"audio/{esc(it['audio_file'])}\"></audio>\n"
        f"<div class=\"ctx\">{ctx_lines}</div>\n"
        f"<div class=\"muted\">各来源读法</div><ul class=\"rd\">{reads}</ul>\n"
        f"{danmaku}\n"
        f"<fieldset><legend>候选写法（选一个，或手填）</legend>{''.join(radios)}</fieldset>\n"
        "<label>备注 <input type=\"text\" class=\"note\"></label>\n"
        "</article>\n"
    )


def render_page(items: Sequence[QueueItem], *, generated_at: str) -> str:
    cards = "".join(_card(i, it) for i, it in enumerate(items, 1))
    count = len(items)
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>亲听疑点队列</title>\n<style>" + _CSS + "</style>\n</head>\n"
        f"<body data-version=\"{QUEUE_VERSION}\">\n"
        "<header><h1>亲听疑点队列（UNRESOLVED 且影响意思；另加显式指定的句）</h1>"
        f"<div class=\"muted\">待听 {count} 条 · 生成于 {html.escape(generated_at)} · 音频默认静音、不自动播放，"
        "点 ▶ 才播放，需要声音请先取消静音。</div>"
        "<div><span id=\"cnt\">已标 0 / " + str(count) + "</span> · "
        "<button id=\"bExport\">导出标注 JSON</button></div></header>\n"
        f"<main>\n{cards}</main>\n"
        "<main><h2>导出内容（只读，可全选复制）</h2><textarea id=\"exp\" readonly></textarea></main>\n"
        f"<script>{_JS}</script>\n</body>\n</html>\n"
    )


def write_queue(out_dir: Path, plans: Sequence[QueuePlan], *, remote_for: Callable[[QueuePlan], Remote],
                generated_at: str | None = None) -> QueueSummary:
    """Cut audio for every plan (fresh audio dir), then write index.html.  Returns a summary for the CLI."""
    out_dir = Path(out_dir)
    audio_dir = out_dir / "audio"
    if audio_dir.exists():
        shutil.rmtree(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    items: list[QueueItem] = []
    for plan in plans:
        cut_audio(remote_for(plan), plan, audio_dir)
        items.extend(plan["items"])
    page = out_dir / "index.html"
    page.write_text(render_page(items, generated_at=generated_at or utc_now()), encoding="utf-8")
    per_clip: dict[str, int] = {}
    for it in items:
        per_clip[it["cid"]] = per_clip.get(it["cid"], 0) + 1
    return {"page": str(page), "items": len(items), "per_clip": per_clip, "audio_dir": str(audio_dir)}


# ----- export and import -------------------------------------------------------------------
def parse_export(obj: Any) -> list[ExportLabel]:
    """Validate an export and return normalised label rows.  Any malformed row raises ListenError."""
    if not isinstance(obj, dict) or not isinstance(obj.get("labels"), dict):
        raise ListenError("export must carry a labels object")
    if obj.get("cues_version") != QUEUE_VERSION:
        raise ListenError(f"export cues_version must be {QUEUE_VERSION!r}, got {obj.get('cues_version')!r}")
    rows: list[ExportLabel] = []
    for key, row in obj["labels"].items():
        if not isinstance(row, dict):
            raise ListenError(f"{key}: label is not an object")
        cid, doubt_id = str(row.get("cid") or ""), str(row.get("doubt_id") or "")
        if not cid or not doubt_id or key != queue_key(cid, doubt_id):
            raise ListenError(f"label key {key!r} does not match cid/doubt_id")
        try:
            abs_start_ms, abs_end_ms = int(row["abs_start_ms"]), int(row["abs_end_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ListenError(f"{key}: abs_start_ms/abs_end_ms missing or invalid ({exc})") from exc
        if abs_end_ms <= abs_start_ms:
            raise ListenError(f"{key}: abs_end_ms must be after abs_start_ms")
        current = row.get("current")
        if not isinstance(current, str):
            raise ListenError(f"{key}: current text missing")
        text = str(row.get("text") or "").strip()
        if not text:
            raise ListenError(f"{key}: empty label text")
        if row.get("choice") not in CHOICES:
            raise ListenError(f"{key}: choice must be one of {CHOICES}")
        rows.append({"cid": cid, "doubt_id": doubt_id, "abs_start_ms": abs_start_ms, "abs_end_ms": abs_end_ms,
                     "current": current, "text": text, "choice": row["choice"],
                     "note": str(row.get("note") or "").strip()})
    return rows


def import_labels(task_dir: Path, export_path: Path) -> LabelImportResult:
    """Validate every label against the clip's current doubts, then store them in each clip's state.

    All-or-nothing: if any label fails validation, no state file is written.  A clip's label set is replaced
    as a whole by the labels for that clip in this export; clips absent from the export are untouched."""
    task_dir, export_path = Path(task_dir), Path(export_path)
    raw = export_path.read_bytes()
    rows = parse_export(json.loads(raw.decode("utf-8")))
    digest = hashlib.sha256(raw).hexdigest()
    by_cid: dict[str, list[ExportLabel]] = {}
    for row in rows:
        by_cid.setdefault(row["cid"], []).append(row)
    plans: dict[str, tuple[Path, list[ExportLabel]]] = {}
    for cid, group in sorted(by_cid.items()):
        state_path = task_dir / "state" / f"{cid}.json"
        doubts_path = task_dir / "clips" / cid / "subs" / "doubts.json"
        if not state_path.exists():
            raise ListenError(f"{cid}: no state file")
        if not doubts_path.exists():
            raise ListenError(f"{cid}: no doubts.json; stage 3 has not produced doubts for this clip")
        doubts = {str(d["id"]): d for d in json.loads(doubts_path.read_text(encoding="utf-8"))}
        for row in group:
            doubt = doubts.get(row["doubt_id"])
            if doubt is None or doubt.get("current") != row["current"]:
                raise ListenError(f"{cid}:{row['doubt_id']} no longer matches doubts.json "
                                  "(current text changed); rebuild the queue and listen again")
            # the label's absolute anchor must still hold in the clip's current subtitles (same rule as the stage gate)
            bcut_path = task_dir / "clips" / cid / "asr" / "bcut.srt"
            if not bcut_path.exists():
                raise ListenError(f"{cid}: no ASR subtitles on disk to anchor the label")
            clip_start_s = float(ClipState.load(state_path).data["clip"]["start_ms"]) / 1000
            try:
                match_label_anchors([cast("AnchorLabel", row)], parse_srt(bcut_path.read_text(encoding="utf-8")),
                                    source_offset_s=clip_start_s)
            except KeyError as exc:
                raise ListenError(f"{cid}:{row['doubt_id']} anchor (source time + text) no longer holds: {exc}") from exc
        plans[cid] = (state_path, group)
    for state_path, group in plans.values():
        state = ClipState.load(state_path)
        state.data["reviewer_labels"] = {
            "source": REVIEWER_SOURCE, "export_file": str(export_path), "export_sha256": digest,
            "imported_at": utc_now(), "labels": cast("list[IvanLabel]", sorted(group, key=lambda r: r["abs_start_ms"])),
        }
        state.save()
    return {"export_sha256": digest, "labels": len(rows), "clips": {cid: len(g) for cid, (_, g) in plans.items()}}
