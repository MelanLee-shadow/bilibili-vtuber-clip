"""Shared fakes for the manual-lane tests: remote (ssh), CPA judge, danmaku source, and a clip context.

No test using these reaches ssh, ffmpeg, CPA or the network.

TASK-31: the lane calls independent providers from several threads at once, so every fake here is thread-safe: the
call/prompt logs are appended under one lock (the two lists stay aligned), and `FakeCpa` answers the title judge's
runs by their run number, not by whichever thread arrives first — otherwise two concurrent runs would take each
other's scripted answer and a test would flake.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shlex
import threading
import time
from pathlib import Path

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import Adapters, LocalRemote, RemoteResult
from src.autoslice.manual_lane.runner import Ctx
from src.autoslice.manual_lane.state import ClipState

SESSION = {"recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
           "participants": [["李豆沙"], ["雾深Girimi"]], "own_uploader_mids": [1]}


def srt(n: int = 40, step: float = 2.0) -> str:
    blocks = []
    for i in range(1, n + 1):
        s = (i - 1) * step
        blocks.append(f"{i}\n{si.fmt_ts(s)} --> {si.fmt_ts(s + step)}\n句子{i}")
    return "\n\n".join(blocks) + "\n"


def fake_title_skill(tmp_path: Path, monkeypatch) -> Path:
    """Synthetic files for tests of title/history/review plumbing, not editorial quality.

    Supplies SKILL.md sections 四 (标题), 五 (封面文案), 六 (end marker), 八.3 (意思 through 判官守则),
    references/examples.md and the references/judge-prompt.md template, builds the deployed projection from them
    (scripts/build_title_editorial_method.py's own functions) and points the loader at it.  The real authority
    readers, prompt builders, hard gates and judge calls still run.
    """
    from src.autoslice import title_editorial_method

    root = tmp_path / "title-cover-skill"
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "# Synthetic title-cover skill for tests\n"
        "## 四、标题\n标题依据本片事实。\n"
        "## 五、封面文案\n封面字依据本片事实。\n"
        "## 六、画面\n"
        "## 八、验收\n"
        "3. **意思**：核对事实核与修辞。\n"
        "**判官守则**：不编造事实。\n", encoding="utf-8",
    )
    (root / "references/examples.md").write_text("# 测试示范\n- 测试标题只作风格占位。\n", encoding="utf-8")
    (root / "references/judge-prompt.md").write_text(
        "# Synthetic judge template\n\n---\n"
        "@@RULES@@\n@@SUBTITLE@@\n@@PREFIX@@@@TITLE@@\n@@COVER1@@\n@@COVER2@@\n", encoding="utf-8",
    )
    projection = tmp_path / "title_editorial_method.json"
    projection.write_text(json.dumps(title_editorial_method.projection_data(
        title_editorial_method.extract_title_editorial_method(root)), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(title_editorial_method, "TITLE_EDITORIAL_METHOD", projection)
    return root


class InFlight:
    """Thread-safe count of the calls a fake has open at once, with a random delay inside each (TASK-31 tests)."""

    def __init__(self, delay: tuple[float, float] = (0.0, 0.0)):
        self._lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.calls = 0
        self.delay = delay

    def __enter__(self) -> "InFlight":
        with self._lock:
            self.now += 1
            self.calls += 1
            self.peak = max(self.peak, self.now)
        if self.delay[1] > 0:
            time.sleep(random.uniform(*self.delay))
        return self

    def __exit__(self, *_exc) -> None:
        with self._lock:
            self.now -= 1


def root_commands(body: str) -> list[str]:
    """The command of each `sudo -n bash -c '...'` line of a script (adapters.root_script), without the wrapper's own
    prelude: what the media host runs as root."""
    prelude = "set -a; set +a; export PYTHONDONTWRITEBYTECODE=1; "
    return [line[len("sudo -n bash -c '"):-1].removeprefix(prelude).replace("'\\''", "'")
            for line in body.splitlines() if line.startswith("sudo -n bash -c '")]


class FakeRemote:
    def __init__(self, on_script=None):
        self.scripts: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {}
        self.on_script = on_script
        self.rc = 0
        self.silences: list[list[float]] = []    # what the audio-silence check of step 8 finds in the finished clip (none: sound everywhere)
        self._lock = threading.Lock()
        self._running = threading.local()      # the script this thread is running (several cover jobs run at once)

    def run_script(self, *, name, body, timeout):
        with self._lock:
            self.scripts.append((name, body))
        self._running.body = body
        if name.startswith('cpa-recover-'):
            out_path, prompt_path = shlex.split(body)[-2:]
            with self._lock:
                out, prompt = self.files.get(out_path), self.files.get(prompt_path)
            if out is None:
                return RemoteResult(44, '', '')
            if prompt is None:
                return RemoteResult(45, '', '')
            payload = {'out': base64.b64encode(out).decode('ascii'), 'prompt': base64.b64encode(prompt).decode('ascii')}
            return RemoteResult(0, json.dumps(payload), '')
        if name == "s8-audio-silences":
            return RemoteResult(0, json.dumps({"duration": 60.0, "silences": self.silences}) + "\n", "")
        if self.on_script:
            return self.on_script(name, body, self)
        return RemoteResult(self.rc, "", "")

    def sha256(self, remote_path):
        return None

    def put(self, local, remote_path):
        data = Path(local).read_bytes()
        with self._lock:
            self.files[remote_path] = data

    def get(self, remote_path, local):
        with self._lock:
            data = self.files.get(remote_path)
        if data is None:
            raise RuntimeError(f"get {remote_path} failed: missing (fake)")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(data)


COVER_EMOTE = json.dumps({"choices": [], "reason": "测试：说明里没有一张对得上"}, ensure_ascii=False)   # the cover-emote call: none fits
COVER_BRIEF = json.dumps({"quotes": [], "expression": "surprised", "action": "talking", "scene_needed": False,
                          "scene_reason": "", "setting": "", "kmx": False}, ensure_ascii=False)
# The skill judge's answer (references/judge-prompt.md output shape), PASS unless a test scripts its own.
JUDGE_PASS = json.dumps({"fact_core": "x", "claims": [], "quotes": [], "rhetoric": [], "fabrication": {},
                         "cover": [], "combined_reading": "不会", "overall": "PASS", "fail_reasons": []},
                        ensure_ascii=False)


# The clip-understanding answers (title_understand.md / title_understand_merge.md output shape).
READING = json.dumps({"main_line": "李豆沙讲了她的主线", "punchline": "她先嘴硬，接着自己拆台（序号 3）",
                      "punchline_cues": [3], "who_is_who": [], "side_topics": []}, ensure_ascii=False)
MERGE = json.dumps({"agree": True, "main_line": "李豆沙讲了她的主线", "punchline": "她先嘴硬，接着自己拆台（序号 3）",
                    "punchline_cues": [3], "who_is_who": [{"term": "它", "means": "她的小号", "cues": [2]}],
                    "side_topics": ["顺带聊了晚饭"], "hook_holds": True, "conflicts": []}, ensure_ascii=False)


UNDERSTANDING_CALLS = ["title-read-1", "title-read-2", "title-merge"]   # what step 6 asks before the first title


def understanding_answer(call_id: str) -> str | None:
    """The scripted answer of a clip-understanding call (title-read-N, title-merge), None for any other call."""
    if call_id.startswith("title-read-"):
        return READING
    return MERGE if call_id == "title-merge" else None


def context_answer(prompt_text: str, found=None) -> str:
    """The circle-context answer (prompts/title_context.md output shape) for every term the prompt lists: found false
    unless `found` maps the term to (note, sources)."""
    items = json.loads(prompt_text.split("检索结果）：\n", 1)[1])
    rows = []
    for item in items:
        note, sources = (found or {}).get(item["term"], ("", []))
        rows.append({"term": item["term"], "found": bool(note), "note": note, "sources": list(sources),
                     "why_not": "" if note else "结果没有说到它"})
    return json.dumps(rows, ensure_ascii=False)


class FakeSearch:
    """The lookup adapter's web search (work_lookup.WebLookup.web), never configured unless a test says so.
    `rows` maps a query to its results; any other query gets `default`; `raises` makes every query raise."""

    def __init__(self, *, configured: bool = False, rows=None, default=(), raises: Exception | None = None):
        self.web_configured = configured
        self.rows = dict(rows or {})
        self.default = list(default)
        self.raises = raises
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def web(self, query, *, limit=5):
        with self._lock:
            self.calls.append((query, limit))
        if self.raises is not None:
            raise self.raises
        return list(self.rows.get(query, self.default))[:limit]

    @property
    def queries(self) -> list[str]:
        return [query for query, _limit in self.calls]


def pick_answer(order, grades=None, *, whys=None):
    """A title-pick answer (prompts/title_pick.md output shape): `order` as given, `grades[i]` the grade predicted for
    candidate i (every one 还可以 by default)."""
    n = len(order)
    return json.dumps({"order": list(order), "predicted": [
        {"index": i, "grade": (grades or {}).get(i, "还可以"), "why": (whys or {}).get(i, "x")} for i in range(n)]},
        ensure_ascii=False)


def default_pick_answer(prompt_text: str) -> str:
    """The ranking a test that does not care about it gets: the candidates in their original order, all 还可以."""
    block = prompt_text.split("候选（序号从 0 起）：\n", 1)[1].split("\n\n按他的口味", 1)[0]
    return pick_answer(range(len(re.findall(r"(?m)^\d+ ", block))))


def is_pick(call_id: str) -> bool:
    return call_id.startswith("title-pick")


def is_writing_canon(call_id: str) -> bool:
    return call_id.startswith("writing-canon")


def writing_canon_answer(prompt_text: str, same_word=()) -> str:
    """The writing-canon answer (prompts/writing_canon.md output shape) for every hit the prompt lists: not the same word
    (kept) unless its writing is in `same_word`.  What a test that does not care about it gets: all false."""
    rows = json.loads(prompt_text.split("待判的处：\n", 1)[1])
    return json.dumps([{"id": r["id"], "same_word": r["写法"] in same_word, "why": "fake"} for r in rows],
                      ensure_ascii=False)


def judge_fail(*reasons):
    return json.dumps({"overall": "FAIL", "fail_reasons": list(reasons) or ["x"]}, ensure_ascii=False)


def is_judge(call_id: str) -> bool:
    return "title-judge" in call_id


def cover_verdict(people=("李豆沙", "雾深Girimi"), *, hook=2, face=(1300, 300, 1600, 650), clean=True):
    """A CPA vision answer for one candidate, in the shape cover_judge.md asks for."""
    ok = {"pass": True, "reason": "看图"}
    return {"people": {"expected": ["李豆沙", "雾深Girimi"], "present": list(people), "reason": "看图"},
            "hook_fit": {"score": hook, "reason": "看图"}, "clean_layers": dict(ok, **{"pass": clean}),
            "identity": ok, "source_faithful": ok, "no_fabrication": ok, "face": list(face), "keep": []}


def fake_cover(remote, rd, rows=None, calls=None, body=None, extra=None):
    """What cover_candidates.py leaves on OCI3: its result file, each rendered candidate (png + jpg) and its raw judge
    response.  Default: screenshot (face too small, vetoed), cutout and ai_sticker (both eligible); one image edit each.
    `body` is the run's script: it names the job file (cover/job-<routes>.json) whose routes the tool builds and the
    result file (result-<routes>.json) it writes.  `extra[route]` adds fields to that route's row (a measured face ...)."""
    if rows is None:
        rows = [("screenshot", cover_verdict(face=(1380, 470, 1600, 650))), ("cutout", cover_verdict()),
                ("ai_sticker", cover_verdict())]
    # the real tool builds only the routes of the job it was started with (job-<routes>.json "routes")
    body = body or getattr(remote._running, "body", None)
    found = re.search(r"cover/(job-[^ ]+\.json)", body or "")
    job_name = found.group(1) if found else "job.json"
    job = remote.files.get(f"{rd}/cover/{job_name}")
    routes = json.loads(job.decode()).get("routes") if job else None
    result_name = json.loads(job.decode()).get("result", "result.json") if job else "result.json"
    if routes:
        rows = [(name, verdict) for name, verdict in rows if name in routes]
    out = []
    for name, verdict in rows:
        face = verdict.get("face") or [0, 0, 1, 1]
        # the real tool measures the guest's face too (TASK-20): the default guest is as large as the host
        guest = [max(0, face[0] - 1000), face[1], max(1, face[2] - 1000), face[3]]
        out.append({"id": name, "route": name, "status": "RENDERED", "note": name, "main_emote": None,
                    "guest_face_measured": guest if "雾深Girimi" in verdict["people"]["present"] else None,
                    "punch_font_size": 180, "render": {"measured_lines": [{"x": 300, "y": 40, "w": 600, "h": 180}],
                                                       "layout": "top", "composition": "peers_equal"},
                    "verdict": verdict, "answer": json.dumps(verdict, ensure_ascii=False),
                    "generation": ({name: {"model": "gpt-image-2.5", "reported_model": "UNKNOWN", "reused": False}}
                                   if name in ("cutout", "ai_sticker", "ai_scene") else {}), **((extra or {}).get(name) or {})})
        remote.files[f"{rd}/cover/cand/{name}.png"] = b"png-" + name.encode()
        remote.files[f"{rd}/cover/cand/{name}.jpg"] = _JPEG
        remote.files[f"{rd}/cover/cand/{name}.judge.response.json"] = b"{}"
    if calls is None:
        calls = ([{"kind": "image_edit", "candidate": r, "rc": 0, "status": "AI_BACKGROUND_READY",
                   "selected_model": "gpt-image-2.5", "attempted_models": ["gpt-image-2.5"], "reported_model": "UNKNOWN"}
                  for r in ("cutout", "ai_sticker") if r in [name for name, _ in rows]]
                 + [{"kind": "vision", "candidate": name, "model": "gpt-6.1-sol", "rc": 0} for name, _ in rows])
    remote.files[f"{rd}/cover/cand/{result_name}"] = json.dumps({"rows": out, "calls": calls}, ensure_ascii=False).encode()


def _jpeg() -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 18), "white").save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()

_POSITION_TAIL = re.compile(r"-(?:d)?(\d+)$")


def _family(call_id: str) -> str:
    """The family of one call id: its text without the trailing position (`title-judge-r0-2` -> `title-judge-r0`,
    `concat-d005` -> `concat`, `adjudicate-p1-3` -> `adjudicate-p1`)."""
    return _POSITION_TAIL.sub("", call_id)


def _position(call_id: str) -> int:
    match = _POSITION_TAIL.search(call_id)
    return int(match.group(1)) if match else 0


def serial_order(call_ids: list[str]) -> list[str]:
    """A fake's call log in the order a serial run produced it (TASK-31).

    A fake records a call when its thread reaches it, so a log is in completion order.  Every assertion about a
    parallel pass wants the order the pass was given its work in, so the log is read back through this: a family's
    first appearance fixes where that family sits (families of one step that do not overlap keep their order), and the
    numbered calls of one family (`adjudicate-p1-1`, `-2`; `title-judge-r0-1`, `-2`; `concat-d005`) follow by their
    number, which is their input position (TASK-37)."""
    rows = [(call_id, "") for call_id in call_ids]
    return [call_id for call_id, _prompt in _parallel_key(rows)]


def _parallel_key(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    first: dict[str, int] = {}
    for index, (call_id, _prompt) in enumerate(rows):
        first.setdefault(_family(call_id), index)

    def key(row: tuple[str, str]) -> tuple[int, int]:
        return (first[_family(row[0])], _position(row[0]))
    return sorted(rows, key=key)


class FakeCpa:
    """Answers each ask() with the next scripted completion text (or a failure record).  The step-6 cover brief is
    answered with COVER_BRIEF and the title judge with `judge` (then JUDGE_PASS), without taking a scripted answer.

    Thread-safe (TASK-31): the calls are logged under one lock and read back in serial order (`_parallel_key`).  The
    title judge's runs of ONE judge call (`title-judge-r0-1`, `-2`) are asked concurrently; they share that call's
    scripted verdict, and the next judge call in the round takes the next one."""

    def __init__(self, responses, *, judge=(), delay: tuple[float, float] = (0.0, 0.0), readings=None, merges=None,
                 pick=(), writing_canon=None, context=None):
        self.responses = list(responses)
        self.writing_canon = writing_canon   # a callable(prompt) -> answer text; every hit "not the same word" if None
        self.pick = list(pick)            # one ranking answer per title-pick call; the original order after they run out
        self.readings = readings if readings is not None else READING   # a text, or {run: text}
        self.merges = list(merges) if merges is not None else [MERGE]   # one per ask; the last repeats
        self.context = context            # the title-context answer: a text or a callable(prompt) -> text (None text = a failed call); all found false if unset
        self.judge = list(judge)          # one verdict per judge call (its runs share it); JUDGE_PASS after they run out
        self._rows: list[tuple[str, str]] = []
        self.meter = InFlight(delay)
        self._lock = threading.Lock()
        self._judge_taken: dict[str, str] = {}

    @property
    def calls(self) -> list[str]:
        return [call_id for call_id, _prompt in _parallel_key(self._rows)]

    @property
    def prompts(self) -> list[str]:
        return [prompt for _call_id, prompt in _parallel_key(self._rows)]

    @property
    def title_prompts(self) -> list[str]:
        """The prompts of the title rounds (title, title-regen-N), without the clip-understanding calls before them."""
        return [prompt for call_id, prompt in _parallel_key(self._rows)
                if call_id == "title" or call_id.startswith("title-regen-")]

    def _judge_answer(self, call_id: str) -> str:
        group = _family(call_id)          # title-judge-r0-1 and -2 are one judge call
        with self._lock:
            if group not in self._judge_taken:
                index = len(self._judge_taken)
                self._judge_taken[group] = self.judge[index] if index < len(self.judge) else JUDGE_PASS
            return self._judge_taken[group]

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir, max_output_tokens=None):
        with self.meter:
            if is_judge(call_id):
                text = self._judge_answer(call_id)
            elif is_pick(call_id):
                with self._lock:
                    text = self.pick.pop(0) if self.pick else default_pick_answer(prompt_text)
            elif is_writing_canon(call_id):
                text = self.writing_canon(prompt_text) if callable(self.writing_canon) else writing_canon_answer(prompt_text)
            elif call_id.startswith("title-read-"):
                run = int(re.match(r"title-read-(\d+)", call_id).group(1))
                text = self.readings[run] if isinstance(self.readings, dict) else self.readings
            elif call_id.startswith("title-context"):
                text = context_answer(prompt_text) if self.context is None else (
                    self.context(prompt_text) if callable(self.context) else self.context)
            elif call_id.startswith("title-merge"):
                with self._lock:
                    text = self.merges.pop(0) if len(self.merges) > 1 else self.merges[0]
            else:
                with self._lock:
                    text = (COVER_BRIEF if call_id == "cover-brief" else COVER_EMOTE if call_id == "cover-emote"
                            else self.responses.pop(0))
        with self._lock:
            self._rows.append((call_id, prompt_text))
        if text is None:
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 1, "elapsed_s": 0.1,
                    "response_text": "", "stderr_tail": "CPA_HTTP_ERROR (fake)"}
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": text, "stderr_tail": ""}


class IdCpa:
    """Answers each adjudication prompt by the doubt ids it contains: `rows` overrides a row by id, `omit` drops
    an id from the answer, and every other id gets KEEP (TASK-5: every cue is a doubt).  Thread-safe (TASK-31)."""

    def __init__(self, rows=None, omit=(), delay: tuple[float, float] = (0.0, 0.0)):
        self.rows = {r["id"]: r for r in (rows or [])}
        self.omit = set(omit)
        self._rows: list[tuple[str, str]] = []
        self.meter = InFlight(delay)
        self._lock = threading.Lock()

    @property
    def calls(self) -> list[str]:
        return [call_id for call_id, _prompt in _parallel_key(self._rows)]

    @property
    def prompts(self) -> list[str]:
        return [prompt for _call_id, prompt in _parallel_key(self._rows)]

    def ask(self, *, call_id, prompt_text, model, effort, remote_dir):
        ids = list(dict.fromkeys(re.findall(r'"id": "(d\d+|维护者\d+|k\d+)"', prompt_text)))
        if is_writing_canon(call_id):
            with self.meter, self._lock:
                self._rows.append((call_id, prompt_text))
            return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                    "response_text": writing_canon_answer(prompt_text), "stderr_tail": ""}
        with self.meter:
            out = []
            for doubt_id in ids:
                if doubt_id in self.omit:
                    continue
                out.append(self.rows.get(doubt_id, {"id": doubt_id, "state": "KEEP", "final_text": "",
                                                    "confidence": "高", "affects_meaning": False,
                                                    "reason_key": "同音", "basis": "默认保留"}))
        with self._lock:
            self._rows.append((call_id, prompt_text))
        return {"call_id": call_id, "model": model, "effort": effort, "rc": 0, "elapsed_s": 0.1,
                "response_text": json.dumps(out, ensure_ascii=False), "stderr_tail": ""}


class FakeDanmaku:
    def __init__(self, xml: bytes = b"<i></i>", segments: dict | None = None):
        self.xml = xml
        self.calls: list[str] = []
        self.segments = segments or {}   # other segment file name -> bytes, or an exception to raise

    def fetch(self, remote_path):
        self.calls.append(remote_path)
        other = self.segments.get(remote_path.rsplit("/", 1)[-1])
        if isinstance(other, Exception):
            raise other
        return self.xml if other is None else other

    def names(self, remote_dir):
        return list(self.segments)


def make_ctx(tmp_path: Path, *, remote, cpa, session=None, released=(), reviewer_labels=None,
             danmaku_xml: bytes = b"<i></i>", bcut_text: str | None = None, boundary=None,
             remote_dir: str = "/r/clip") -> Ctx:
    """A clip context on disk: BCUT transcript, clip window of cues 6..40, empty state."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    srt_path = tmp_path / "bcut.srt"
    srt_path.write_text(bcut_text if bcut_text is not None else srt(), encoding="utf-8")
    clip = {"cid": "clip", "recording_date": "2026-10-24", "source_name": "src.flv", "start_ms": 10_000,
            "end_ms": 80_000, "start_cue": 6, "end_cue": 40, "hook": "测试钩子",
            "mac_bcut_srt": str(srt_path), "remote_dir": remote_dir}
    state = ClipState.create(tmp_path / "state" / "clip.json", clip=clip, session=session or SESSION)
    state.data["released"] = [{"reason": r} for r in released]
    if reviewer_labels is not None:
        state.data["reviewer_labels"] = reviewer_labels
    state.save()
    adapters = Adapters(remote=remote, cpa=cpa, danmaku=FakeDanmaku(danmaku_xml))
    return Ctx(state=state, session=state.data["session"], clip_dir=tmp_path / "clips" / "clip",
               remote_dir=remote_dir, adapters=adapters)


def _push_srt(rows):
    return "\n".join(f"{i}\n{si.fmt_ts(a)} --> {si.fmt_ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(rows, 1))


def push_dir(tmp_path, clip, rows):
    """A hand push on disk (subs/final.srt, speaker/speaker.srt, subs/review.tsv, boundary/decision.json) for rows
    (start, end, speaker, text), every row high confidence."""
    src = tmp_path / "push"
    for d in ("subs", "speaker", "boundary"):
        (src / d).mkdir(parents=True, exist_ok=True)
    (src / "subs/final.srt").write_text(_push_srt([(a, b, t) for a, b, _s, t in rows]), encoding="utf-8")
    (src / "speaker/speaker.srt").write_text(_push_srt([(a, b, f"[{s}] {t}") for a, b, s, t in rows]), encoding="utf-8")
    head = "no\ttid\tstart\tend\tspeaker\ttext\ttext_conf\tspeaker_conf\talt\tbasis\tbcut_text\n"
    body = "".join(f"{i}\tB{i}\t{a}\t{b}\t{s}\t{t}\t高\t高\t\t依据\t{t}\n" for i, (a, b, s, t) in enumerate(rows, 1))
    (src / "subs/review.tsv").write_text(head + body, encoding="utf-8")
    (src / "boundary/decision.json").write_text(json.dumps({"adopted": {"start_ms": clip["start_ms"],
                                                                        "end_ms": clip["end_ms"]}}), encoding="utf-8")
    return src


def rules_with(tmp_path: Path, monkeypatch, **sections) -> None:
    """Point MANUAL_LANE_RULES at a copy of assets/manual_lane/lane_rules.json with some fields changed
    (sections: {section: {key: value}}), for a test that needs a switch off or another number."""
    from src.autoslice.manual_lane.lane_rules import RULES_FILE

    doc = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    for name, fields in sections.items():
        doc[name].update(fields)
    path = tmp_path / "lane_rules.test.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("MANUAL_LANE_RULES", str(path))


def screen_remote(*, playing=True, subtitles=None, broken=(), no_output=(), on_other=None) -> FakeRemote:
    """A media host for step 4's picture reading (read_screen_subtitles.py): the script `s4-screen-<mode>` answers every
    requested frame.  `playing` is whether a watched video is on screen; `subtitles` maps a cue index to the screen
    subtitle read in that cue's frames (screen frames read none); `broken` is the passes (screen/cue) whose frames
    all fail to read; `no_output` the passes whose script leaves no result file.  Other scripts go to `on_other`."""
    subtitles = subtitles or {}

    def script(name, body, remote):
        if not name.startswith("s4-screen-"):
            return on_other(name, body, remote) if on_other else RemoteResult(0, "", "")
        mode = name.removeprefix("s4-screen-")
        if mode in no_output:
            return RemoteResult(1, "", "tool failed")
        frames = json.loads(remote.files[f"/r/clip/speaker/{mode}-frames.json"])["frames"]
        rows, calls = [], []
        for f in frames:
            if mode in broken:
                rows.append({"id": f["id"], "t": f["t"], "ok": False, "error": "URLError"})
            else:
                text = subtitles.get(int(f["id"][1:].split("-")[0]), "") if mode == "cue" else ""
                rows.append({"id": f["id"], "t": f["t"], "ok": True, "video_playing": playing,
                             "video_subtitle": text if playing else "", "subtitle_language": "中文" if text else "无",
                             "other_text": ""})
            calls.append({"kind": "subtitle", "t": f["t"], "model": "gpt-6.1-sol", "effort": "low", "rc": 0,
                          "elapsed_s": 0.1, "usage": {"input_tokens": 1000, "output_tokens": 50}})
        doc = {"question": "q", "model": "gpt-6.1-sol", "frames": rows, "calls": calls}
        remote.files[f"/r/clip/speaker/{mode}-out.json"] = json.dumps(doc, ensure_ascii=False).encode()
        return RemoteResult(0, "", "")

    return FakeRemote(on_script=script)


# ----- the media host's own conditions (the controller runs on the media host) -------------------------------------------
class HostFiles:
    """`FakeRemote.files` for a host whose paths are real: a remote path is a file on this machine."""

    def __getitem__(self, path: str) -> bytes:
        if not Path(path).is_file():
            raise KeyError(path)
        return Path(path).read_bytes()

    def __setitem__(self, path: str, data: bytes) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)

    def __contains__(self, path: object) -> bool:
        return Path(str(path)).is_file()

    def get(self, path: str, default=None):
        return self[path] if path in self else default


class SameDirRemote(LocalRemote):
    """The remote of `run --on-media-host`: the lane's own LocalRemote (put/get/sha256 on the real file system, a file
    copied onto itself left alone), so a clip's local directory and its remote directory are one directory.  What a script
    does is scripted as with FakeRemote (`on_script(name, body, remote)`; `remote.files[path] = data` writes the file where
    the script would have), and nothing is executed: no ffmpeg, no ssh."""

    REAL_SCRIPTS = ("revise-copy-cut", "redo-replace-source", "redo-discard-source", "compile-clean")      # plain mkdir/cp/mv/rm

    def __init__(self, scripts_dir: str, on_script=None):
        super().__init__(scripts_dir=scripts_dir)
        self._scripted = FakeRemote(on_script=on_script)
        self._scripted.files = HostFiles()      # type: ignore[assignment]

    files = property(lambda self: self._scripted.files)
    scripts = property(lambda self: self._scripted.scripts)

    @property
    def on_script(self):
        return self._scripted.on_script

    @on_script.setter
    def on_script(self, fn) -> None:
        self._scripted.on_script = fn

    def run_script(self, *, name, body, timeout):
        """A script that only moves files (REAL_SCRIPTS) is run, because the host's file system is the real one here and the
        lane's own copy and replace must meet it; any other (ffmpeg, ssh, models) is scripted."""
        if name in self.REAL_SCRIPTS:
            with self._scripted._lock:
                self._scripted.scripts.append((name, body))
            return super().run_script(name=name, body=body, timeout=timeout)
        return self._scripted.run_script(name=name, body=body, timeout=timeout)


ROOT_ONLY_CALLS = ("s10-registry-row", "s8-published-status", "s8-first-publication", "s8-first-digests", "s6-published-titles", "s7-online-tags", "s8-title-receipt", "s8-used-packages")


class RootOnlyRegistry:
    """The media host's registry conditions: the publication registry, the first-publication receipt and the first manifest
    are root-only, so this process reading them directly fails and only the root boundary (the script `history_reader.py`
    run under `sudo -n`, which `answer` plays by running `title_history.serve` as root) gets them back.  Installed over
    whatever registry loader the test has put in place, so install it after the registry fixture.

    Two ways to make this process unable to read:
    - `unreadable=[paths]`: every file under these paths really is unreadable (permission bits 000, restored only while
      `answer` plays root), so the code under test raises what it raises on the host (the registry loader's
      PermissionError, `evidence_identity.file_sha256`'s plain OSError, ...), not an exception we picked.  The test must
      run as an ordinary user (it is skipped as root, who reads everything).
    - without it, the registry and builder readers are replaced by ones that raise PermissionError.  Kept only for tests whose
      registry is a stub (rows in a lambda, no files) so there is nothing to make unreadable."""

    def __init__(self, monkeypatch, *, root_fails: str | None = None, unreadable=None):
        from src.autoslice import new_bv_repair_authority, publication_registry
        from src.autoslice.manual_lane import title_history

        self.calls: list[tuple[str, dict]] = []
        self.as_root = False
        self.root_fails = root_fails
        self._locked: dict[Path, int] = {}
        if unreadable is not None:
            self.lock(unreadable)
            return

        def guarded(real, what):
            def read(*args, **kwargs):
                if not self.as_root:
                    raise PermissionError(f"[Errno 13] Permission denied: {what}")
                return real(*args, **kwargs)
            return read

        monkeypatch.setattr(publication_registry, "load_publication_registry",
                            guarded(publication_registry.load_publication_registry, "publication_registry.runtime.v1.json"))
        monkeypatch.setattr(title_history, "load_publication_registry",
                            guarded(title_history.load_publication_registry, "publication_registry.runtime.v1.json"))
        for name in ("build_new_bv_repair_authority", "replay_first_publication_manifest"):
            monkeypatch.setattr(new_bv_repair_authority, name, guarded(getattr(new_bv_repair_authority, name), "the first-publication receipt"))

    def lock(self, roots) -> None:
        import pytest

        if os.geteuid() == 0:
            pytest.skip("root reads every file: files cannot be made unreadable to this process")
        for root in map(Path, roots):
            for path in ([root] if root.is_file() else sorted(root.rglob("*"))):
                if path.is_file() and not path.is_symlink() and path not in self._locked:
                    self._locked[path] = path.stat().st_mode & 0o777
                    path.chmod(0)

    def _set_modes(self, readable: bool) -> None:
        for path, mode in self._locked.items():
            path.chmod(mode if readable else 0)

    def answer(self, name: str, body: str, read_file) -> RemoteResult | None:
        """The boundary's answer to the script `name`, or None when it is not a boundary script."""
        from src.autoslice.manual_lane import title_history

        if name not in ROOT_ONLY_CALLS:
            return None
        assert "sudo -n" in body and "history_reader.py" in body            # run through the root wrapper, as the titles are
        request = json.loads(read_file(shlex.split(body.strip().splitlines()[-1])[-2]))
        self.calls.append((name, request))
        if self.root_fails:
            return RemoteResult(1, "", self.root_fails)
        self.as_root = True
        self._set_modes(True)
        try:
            return RemoteResult(0, json.dumps(title_history.serve(request), ensure_ascii=False), "")
        finally:
            self.as_root = False
            self._set_modes(False)


class RealMediaHost:
    """The conditions a hand push meets on the media host, as one object for the tests of any command that writes a clip's
    directory or a remote directory: (1) the local clip directory IS the remote one (`remote_dir(cid)` is `clip_dir(cid)`,
    SameDirRemote), (2) the registry and the receipt are root-only (`root_only_registry`).  `script(inner)` is the host's
    script table: the root-boundary scripts are answered as root, any other goes to `inner`."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.task = tmp_path / "task"
        self.registry: RootOnlyRegistry | None = None
        self.inner = None
        self.remote = SameDirRemote(str(tmp_path / "host-scripts"), on_script=self._on_script)

    def clip_dir(self, cid: str) -> Path:
        return self.task / "clips" / cid

    def remote_dir(self, cid: str) -> str:
        return str(self.clip_dir(cid))

    def root_only_registry(self, *, root_fails: str | None = None, unreadable=None) -> RootOnlyRegistry:
        self.registry = RootOnlyRegistry(self.monkeypatch, root_fails=root_fails, unreadable=unreadable)
        return self.registry

    def script(self, inner) -> None:
        self.inner = inner

    def _on_script(self, name, body, remote):
        if self.registry is not None and (answer := self.registry.answer(name, body, lambda p: Path(p).read_bytes())) is not None:
            return answer
        return self.inner(name, body, remote) if self.inner else RemoteResult(0, "", "")

    def adapters_for(self, cpa=None):
        return lambda _remote_dir: Adapters(remote=self.remote, cpa=cpa, danmaku=FakeDanmaku())
