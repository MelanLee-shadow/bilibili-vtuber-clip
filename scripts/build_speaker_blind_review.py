#!/usr/bin/env python3
"""Build a standalone blind-listening review page for speaker labels.

The generated page deliberately contains no model prediction or speaker style.
It is a development-time ground-truth surface, not part of unattended runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path


TIME_RE = re.compile(
    r"^(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})$"
)


@dataclass(frozen=True)
class Cue:
    number: int
    start_ms: int
    end_ms: int
    text: str

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp_ms(match: re.Match[str], prefix: str) -> int:
    return (
        int(match.group(prefix + "h")) * 3_600_000
        + int(match.group(prefix + "m")) * 60_000
        + int(match.group(prefix + "s")) * 1_000
        + int(match.group(prefix + "ms"))
    )


def parse_srt(path: Path) -> list[Cue]:
    blocks = re.split(r"\r?\n\s*\r?\n", path.read_text(encoding="utf-8-sig").strip())
    cues: list[Cue] = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3:
            continue
        try:
            number = int(lines[0].strip())
        except ValueError:
            continue
        match = TIME_RE.match(lines[1].strip())
        if not match:
            continue
        start_ms = _timestamp_ms(match, "s")
        end_ms = _timestamp_ms(match, "e")
        if end_ms <= start_ms:
            continue
        cues.append(Cue(number, start_ms, end_ms, "\n".join(lines[2:]).strip()))
    if not cues:
        raise ValueError(f"no valid cues in {path}")
    return cues


def _spread(items: list[Cue], count: int) -> list[Cue]:
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    indices = [round(index * (len(items) - 1) / (count - 1)) for index in range(count)]
    return [items[index] for index in indices]


def diagnostic_sample(cues: list[Cue], count: int, *, short_ms: int = 1500) -> list[Cue]:
    """Cover the timeline while reserving 40% of the sample for short cues."""

    if count >= len(cues):
        return list(cues)
    short = [cue for cue in cues if cue.duration_ms < short_ms]
    regular = [cue for cue in cues if cue.duration_ms >= short_ms]
    short_target = min(len(short), round(count * 0.4))
    regular_target = min(len(regular), count - short_target)
    if short_target + regular_target < count:
        short_target = min(len(short), count - regular_target)
    selected = _spread(short, short_target) + _spread(regular, regular_target)
    return sorted({cue.number: cue for cue in selected}.values(), key=lambda cue: cue.start_ms)


def parse_source(value: str) -> tuple[str, str, Path, Path, int]:
    parts = value.split("|", 4)
    if len(parts) != 5:
        raise argparse.ArgumentTypeError("source must be ID|TITLE|AUDIO|SRT|COUNT")
    source_id, title, audio, srt, count_text = parts
    try:
        count = int(count_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("source COUNT must be an integer") from exc
    if not source_id or not title or count <= 0:
        raise argparse.ArgumentTypeError("source ID/TITLE must be non-empty and COUNT positive")
    return source_id, title, Path(audio), Path(srt), count


def build_package(*, package_id: str, output: Path, sources: list[tuple[str, str, Path, Path, int]], seed: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    source_rows: list[dict[str, object]] = []
    for source_id, title, audio_path, srt_path, count in sources:
        audio = audio_path.resolve(strict=True)
        srt = srt_path.resolve(strict=True)
        cues = parse_srt(srt)
        sampled = diagnostic_sample(cues, count)
        if len(sampled) != min(count, len(cues)):
            raise RuntimeError(f"unexpected sample count for {source_id}")
        try:
            relative_audio = audio.relative_to(output.resolve())
        except ValueError as exc:
            raise ValueError(f"audio must live under output directory: {audio}") from exc
        source_rows.append(
            {
                "source_id": source_id,
                "title": title,
                "audio": relative_audio.as_posix(),
                "audio_sha256": sha256_file(audio),
                "srt_sha256": sha256_file(srt),
                "total_cues": len(cues),
                "sampled_cues": len(sampled),
                "short_sampled": sum(cue.duration_ms < 1500 for cue in sampled),
            }
        )
        for cue in sampled:
            items.append(
                {
                    "review_id": f"{source_id}:cue-{cue.number}",
                    "source_id": source_id,
                    "source_title": title,
                    "audio": relative_audio.as_posix(),
                    "cue_number": cue.number,
                    "start_ms": cue.start_ms,
                    "end_ms": cue.end_ms,
                    "duration_ms": cue.duration_ms,
                    "text": cue.text,
                }
            )
    random.Random(seed).shuffle(items)
    payload = {
        "schema_version": "lidousha-speaker-blind-review.v1",
        "package_id": package_id,
        "purpose": "development_ground_truth_only",
        "predictions_included": False,
        "runtime_dependency": False,
        "sampling": {
            "seed": seed,
            "strategy": "per-source timeline spread with 40-percent short-cue target",
            "short_cue_ms": 1500,
        },
        "sources": source_rows,
        "items": items,
    }
    manifest_path = output / "review-manifest.json"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    page = render_html(payload)
    page_path = output / "review.html"
    page_path.write_text(page, encoding="utf-8")
    return {"manifest": str(manifest_path), "page": str(page_path), "items": len(items)}


def render_html(payload: dict[str, object]) -> str:
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(str(payload["package_id"]))
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>{title} · 李豆沙人声盲听</title>
<style>
:root {{ color-scheme: dark; font-family: -apple-system,BlinkMacSystemFont,\"PingFang SC\",sans-serif; }}
body {{ margin:0; background:#09111f; color:#eef4ff; }}
main {{ max-width:920px; margin:auto; padding:24px; }}
.top,.card {{ background:#101d32; border:1px solid #29415f; border-radius:16px; padding:18px; margin-bottom:16px; }}
.row {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
button,select,input {{ font:inherit; }}
button {{ border:1px solid #48698f; border-radius:10px; padding:10px 14px; color:#eef4ff; background:#183150; cursor:pointer; }}
button:hover {{ background:#23456e; }}
button.label {{ min-width:150px; min-height:54px; font-weight:700; }}
button.active {{ outline:3px solid #56a8ff; background:#285f91; }}
.muted {{ color:#9fb4cb; }} .big {{ font-size:1.35rem; font-weight:700; }}
.progress {{ height:10px; background:#26364d; border-radius:10px; overflow:hidden; margin:12px 0; }}
.progress>div {{ height:100%; background:#2da8ff; }}
#transcript {{ white-space:pre-wrap; padding:12px; background:#0a1424; border-radius:10px; margin:12px 0; }}
textarea {{ width:100%; min-height:72px; box-sizing:border-box; border-radius:10px; background:#0a1424; color:#eef4ff; border:1px solid #48698f; padding:10px; }}
audio {{ width:100%; margin:14px 0; }}
.warn {{ color:#ffd58a; }}
</style>
</head>
<body><main>
<section class=\"top\">
  <div class=\"big\">李豆沙 / 非李豆沙 · 第一阶段盲听</div>
  <p class=\"muted\">页面不含模型预测和字幕颜色。请只按听到的声音判断；无法可靠判断时不要猜。</p>
  <div id=\"summary\"></div><div class=\"progress\"><div id=\"bar\"></div></div>
  <div class=\"row\">
    <button id=\"export\">导出审阅结果</button><button id=\"importBtn\">导入结果</button>
    <input id=\"importFile\" type=\"file\" accept=\"application/json\" hidden>
    <button id=\"clear\">清空本机记录</button>
  </div>
</section>
<section class=\"card\">
  <div class=\"row\"><span id=\"position\" class=\"big\"></span><span id=\"source\" class=\"muted\"></span></div>
  <audio id=\"audio\" controls preload=\"metadata\"></audio>
  <div class=\"row\">
    <button id=\"play\">▶ 播放当前句（空格）</button>
    <label>前后文 <select id=\"context\"><option value=\"700\">0.7 秒</option><option value=\"1500\" selected>1.5 秒</option><option value=\"3000\">3 秒</option></select></label>
    <label><input id=\"showText\" type=\"checkbox\"> 显示字幕文字</label>
  </div>
  <div id=\"transcript\" hidden></div>
  <p class=\"warn\">1–4 可直接标注；“换人/重叠”表示同一 cue 内不止一种声音。</p>
  <div class=\"row\" id=\"labels\">
    <button class=\"label\" data-label=\"lidousha\">1　李豆沙</button>
    <button class=\"label\" data-label=\"non_lidousha\">2　非李豆沙</button>
    <button class=\"label\" data-label=\"mixed_or_overlap\">3　换人 / 重叠</button>
    <button class=\"label\" data-label=\"unjudgeable\">4　听不清 / 无法判断</button>
  </div>
  <p><label>备注（可选）<textarea id=\"note\"></textarea></label></p>
  <div class=\"row\"><button id=\"prev\">← 上一条</button><button id=\"next\">下一条 →</button></div>
</section>
</main>
<script>
const PACKAGE={embedded};
const STORE='speaker-review:'+PACKAGE.package_id;
let state=JSON.parse(localStorage.getItem(STORE)||'{{}}');
let index=Number(state.__index||0); let stopAt=null;
const $=id=>document.getElementById(id), items=PACKAGE.items;
function save(){{state.__index=index;localStorage.setItem(STORE,JSON.stringify(state));}}
function answer(){{return state[items[index].review_id]||{{}};}}
function render(){{
  index=Math.max(0,Math.min(items.length-1,index)); const item=items[index], a=answer();
  $('position').textContent=`${{index+1}} / ${{items.length}}`;
  $('source').textContent=`${{item.source_title}} · cue ${{item.cue_number}} · ${{(item.duration_ms/1000).toFixed(2)}}s`;
  if($('audio').dataset.src!==item.audio){{$('audio').src=item.audio;$('audio').dataset.src=item.audio;}}
  $('transcript').textContent=item.text; $('transcript').hidden=!$('showText').checked;
  $('note').value=a.note||'';
  document.querySelectorAll('[data-label]').forEach(b=>b.classList.toggle('active',b.dataset.label===a.label));
  const done=items.filter(x=>state[x.review_id]?.label).length;
  $('summary').textContent=`已完成 ${{done}} / ${{items.length}}；结果只保存在本机浏览器，完成后请导出 JSON。`;
  $('bar').style.width=`${{100*done/items.length}}%`; save();
}}
function playCue(){{const item=items[index],ctx=Number($('context').value); const audio=$('audio'); audio.currentTime=Math.max(0,(item.start_ms-ctx)/1000);stopAt=(item.end_ms+ctx)/1000;audio.play();}}
$('audio').addEventListener('timeupdate',()=>{{if(stopAt!==null&&$('audio').currentTime>=stopAt){{$('audio').pause();stopAt=null;}}}});
$('play').onclick=playCue; $('showText').onchange=render;
document.querySelectorAll('[data-label]').forEach(b=>b.onclick=()=>{{state[items[index].review_id]={{...answer(),label:b.dataset.label,updated_at:new Date().toISOString()}};save();render();}});
$('note').oninput=()=>{{state[items[index].review_id]={{...answer(),note:$('note').value,updated_at:new Date().toISOString()}};save();}};
$('prev').onclick=()=>{{index--;render();}};$('next').onclick=()=>{{index++;render();}};
$('export').onclick=()=>{{const answers=items.map(x=>({{review_id:x.review_id,label:state[x.review_id]?.label||null,note:state[x.review_id]?.note||''}}));const out={{schema_version:'lidousha-speaker-human-labels.v1',package_id:PACKAGE.package_id,exported_at:new Date().toISOString(),answers}};const url=URL.createObjectURL(new Blob([JSON.stringify(out,null,2)],{{type:'application/json'}}));const a=document.createElement('a');a.href=url;a.download=PACKAGE.package_id+'.labels.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}};
$('importBtn').onclick=()=>$('importFile').click(); $('importFile').onchange=async e=>{{const d=JSON.parse(await e.target.files[0].text());if(d.package_id!==PACKAGE.package_id){{alert('package_id 不匹配');return;}}for(const a of d.answers||[])if(items.some(x=>x.review_id===a.review_id))state[a.review_id]={{label:a.label,note:a.note||'',updated_at:new Date().toISOString()}};save();render();}};
$('clear').onclick=()=>{{if(confirm('确定清空本机全部标注？')){{localStorage.removeItem(STORE);state={{}};index=0;render();}}}};
document.addEventListener('keydown',e=>{{if(e.target.tagName==='TEXTAREA'||e.target.tagName==='INPUT')return;if(e.code==='Space'){{e.preventDefault();playCue();}}else if(['1','2','3','4'].includes(e.key))document.querySelector(`[data-label="${{['lidousha','non_lidousha','mixed_or_overlap','unjudgeable'][Number(e.key)-1]}}"]`).click();else if(e.key==='ArrowLeft')$('prev').click();else if(e.key==='ArrowRight')$('next').click();}});
render();
</script></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", action="append", type=parse_source, required=True)
    parser.add_argument("--seed", type=int, default=20260712)
    args = parser.parse_args()
    result = build_package(package_id=args.package_id, output=args.output, sources=args.source, seed=args.seed)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
