"""TASK-20 (维护者 11-02, U158; 10-31, U98): step 6 consumes the cover rules already decided instead of pinned defaults.

- The text style (colours, size ratio, tilt, stroke) rotates from the repo's one style table by the final SRT's hash,
  past the styles of the last three covers (skill section 6); the lane pins no style and keeps no table of its own.
- Equal participants (the session relation ledger's CONFIRMED live collaboration, else the session's own participant
  list) stand the same size: each face clears the 25 % floor and the two face heights are within PEER_FACE_RATIO_MAX.
- Who stands on which side rotates for equals, so five covers of one session are not one template.

The first test runs the ordinary path end to end: step6_title_cover -> job.json -> the real cover_candidates.py and the
skill's render_cover.py on local files (vision and image edits answered by a fake) -> the lane's scoring and record ->
the next clip.  It needs the private cover font and the skill script, as the repo's own renderer tests do."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
from itertools import pairwise
from pathlib import Path

import pytest
from fakes import (
    SESSION,
    FakeCpa,
    FakeDanmaku,
    FakeRemote,
    cover_verdict,
    fake_cover,
    make_ctx,
    srt,
)
from PIL import Image, ImageDraw

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import Adapters, RemoteResult
from src.autoslice.manual_lane.runner import Ctx
from src.autoslice.manual_lane.state import ClipState

FONT = Path(os.environ.get("LIDOUSHA_COVER_FONT") or Path.home() / ".local/share/fonts/lidousha/RuiZiZhenYan.ttf")
needs_renderer = pytest.mark.skipif(not (FONT.is_file() and si.COVER_RENDERER.is_file()),
                                    reason="private cover font or skill renderer not present")
TITLE = json.dumps({"candidates": [{"title": "【李豆沙×雾深Girimi】半分钟后她破防了", "on_stage": ["李豆沙", "雾深Girimi"],
                                    "punchline_person": "她破防了",
                                    "swap_check": {"holds_with_other_host": False, "reason": "测试"}}],
                    "recommended": 0, "cover_lines": ["她破防了", "怎么搞"]}, ensure_ascii=False)
# the live frame: a split screen, the guest left and the host right, both faces small (about 18 % of the height)
FRAME_HOST_FACE, FRAME_GUEST_FACE = [1350, 450, 1550, 640], [300, 430, 480, 620]
FRAMES = {"chosen": 12.0, "checked": [{"t": 12.0, "verdict": "CLEAN"}],
          "boxes": {"danmaku": [[1650, 700, 1830, 1080]], "person": [[1225, 220, 1775, 908]],
                    "guest": [[35, 177, 710, 908]]}}
# the generated cut-out: the same two people, their faces of different sizes in it
CUT_GUEST_FIG, CUT_HOST_FIG = [150, 200, 650, 1080], [1200, 150, 1750, 1080]
CUT_GUEST_FACE, CUT_HOST_FACE = [300, 380, 500, 600], [1350, 400, 1600, 600]
# the generated chibi sticker of the host (stage 3): one figure
STICKER_FIG, STICKER_FACE = [700, 100, 1300, 1080], [850, 260, 1150, 520]


def _picture(path: Path, alpha: bool, sticker: bool = False) -> None:
    im = Image.new("RGBA" if alpha else "RGB", (1920, 1080), (0, 0, 0, 0) if alpha else (230, 230, 240))
    d = ImageDraw.Draw(im)
    figs = ((STICKER_FIG,) if sticker else (CUT_GUEST_FIG, CUT_HOST_FIG) if alpha
            else ([35, 177, 710, 908], [1225, 220, 1775, 908]))
    for fig, colour in zip(figs, ((40, 40, 60, 255), (235, 235, 250, 255)), strict=False):   # a sticker has one figure
        d.rectangle(fig, fill=colour if alpha else colour[:3])
    im.save(path)


class LocalRemote(FakeRemote):
    """OCI3 as a local directory: put/get copy files, the step-6 cover run executes the uploaded cover_candidates.py
    in-process on the uploaded renderer and style table; CPA vision and image edits are answered by fakes."""

    def __init__(self):
        super().__init__()
        self.vision_names: list[str] = []

    def put(self, local, remote_path):
        Path(remote_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, remote_path)

    def get(self, remote_path, local):
        if not Path(remote_path).exists():
            raise RuntimeError(f"get {remote_path} failed: missing (local fake)")
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(remote_path, local)

    def run_script(self, *, name, body, timeout):
        self.scripts.append((name, body))
        cover = Path(body.split("mkdir -p ")[1].split()[0]).parent if "mkdir -p " in body else None
        if name == "s6-frame":
            (cover / "frames.json").write_text(json.dumps(FRAMES), encoding="utf-8")
        if name == "s6-cover":
            (cover / "fonts").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FONT, cover / "fonts/RuiZiZhenYan.ttf")
            _picture(cover / "frame.png", alpha=False)
            spec = importlib.util.spec_from_file_location(f"cc_{len(self.scripts)}", cover / "cover_candidates.py")
            cc = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cc)
            cc.vision = self._vision
            cc.generate = lambda job, kind, prompt, ref, out_path, canvas=None: self._generate(cc, kind, prompt, ref, out_path)
            rc = cc.main(["cover_candidates.py", str(cover / "job.json")])
            return RemoteResult(rc, "", "")
        return RemoteResult(0, "", "")

    def _vision(self, job, name, question, images, out_dir):
        self.vision_names.append(name)
        cut = Path(images[0][0]).name == "gen-cutout.png"
        if name.startswith("locate-guest"):   # asked on a crop of the guest's own figure, answered in its pixels
            face, fig = ((CUT_GUEST_FACE, CUT_GUEST_FIG) if name.endswith("cutout")
                         else (FRAME_GUEST_FACE, FRAMES["boxes"]["guest"][0]))
            assert Path(images[0][0]).name.endswith(".crop.png") and "李豆沙" not in question
            return json.dumps({"face": [face[0] - fig[0], face[1] - fig[1], face[2] - fig[0], face[3] - fig[1]]}), False
        if name.startswith("locate"):
            sticker = Path(images[0][0]).name == "gen-sticker.png"
            return json.dumps({"host_face": CUT_HOST_FACE if cut else STICKER_FACE if sticker else FRAME_HOST_FACE}), False
        return json.dumps(cover_verdict(), ensure_ascii=False), False

    @staticmethod
    def _generate(cc, kind, prompt, ref, out_path):
        _picture(Path(out_path), alpha=True, sticker=kind == "ai_sticker")
        # the generation's key, as the real generate writes it (a later stage reuses the cut-out only by this key)
        Path(out_path + ".key.json").write_text(json.dumps({
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "reference_sha256": cc.sha(ref)}))
        cc.CALLS.append({"kind": "image_edit", "candidate": kind, "rc": 0, "status": "AI_BACKGROUND_READY",
                         "selected_model": "gpt-image-2.5", "attempted_models": ["gpt-image-2.5"]})
        cc.GENERATIONS[kind] = {"model": "gpt-image-2.5", "reported_model": "UNKNOWN", "reused": False}
        return {"status": "AI_BACKGROUND_READY"}


def _clip_ctx(task: Path, cid: str, remote, cpa, n_cues: int) -> Ctx:
    clip = {"cid": cid, "recording_date": "2026-10-24", "source_name": "src.flv", "start_ms": 0, "end_ms": 80_000,
            "start_cue": 1, "end_cue": n_cues, "hook": "测试", "mac_bcut_srt": "", "remote_dir": str(task / "remote" / cid)}
    state = ClipState.create(task / "state" / f"{cid}.json", clip=clip, session=SESSION)
    state.save()
    ctx = Ctx(state=state, session=state.data["session"], clip_dir=task / "clips" / cid,
              remote_dir=str(task / "remote" / cid), adapters=Adapters(remote=remote, cpa=cpa, danmaku=FakeDanmaku()))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(n_cues), encoding="utf-8")   # one final SRT per clip
    return ctx


def _host_side(spec: dict, face: list[int]) -> str:
    """Which half of the cover the host's layer (the one cropped around her face) was placed on."""
    layer = next(s for s in spec["stickers"] if s.get("crop") and s["crop"][0] <= (face[0] + face[2]) / 2
                 <= s["crop"][2] and s["crop"][1] <= (face[1] + face[3]) / 2 <= s["crop"][3])
    with Image.open(layer["src"]) as im:
        bbox = im.convert("RGBA").crop(tuple(layer["crop"])).getchannel("A").getbbox()
    width = (bbox[2] - bbox[0]) * layer["h"] / (bbox[3] - bbox[1])
    return "right" if layer["x"] + width / 2 > 960 else "left"


def _placed_face_height(spec: dict, figure: list[int], face: list[int]) -> int:
    """The height a face of the cut-out has on the cover, from the rendered spec's own layer (crop, alpha box, h):
    measured here, independently of the tool's own report."""
    layer = next(s for s in spec["stickers"] if s.get("crop") and s["crop"][0] <= (face[0] + face[2]) / 2 <= s["crop"][2]
                 and s["crop"][0] >= figure[0] - 1 and s["crop"][2] <= figure[2] + 1)
    with Image.open(layer["src"]) as im:
        bbox = im.convert("RGBA").crop(tuple(layer["crop"])).getchannel("A").getbbox()
    return round((face[3] - face[1]) * layer["h"] / (bbox[3] - bbox[1]))


@needs_renderer
@pytest.mark.parametrize("case", ["three_touching", "three_malformed_outline", "three_separate", "three_wide", "three_not_peers", "two_touching",
                                  "two_narrow", "bad_outline", "bad_host_face"])
def test_group_cutout_and_sticker_use_one_clean_equal_layer_per_person(tmp_path, monkeypatch, case):
    """Run the ordinary candidate entry with fake image/vision services, including overlapping body boxes."""
    spec = importlib.util.spec_from_file_location("group_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    count = 2 if case in {"two_touching", "two_narrow", "bad_outline", "bad_host_face"} else 3
    starts = ([100, 580, 1060] if case == "three_wide" else [100, 420, 1000] if case == "two_narrow" else
              [100, 410 if case != "three_separate" else 600, 1000 if case != "three_separate" else 1100])
    width = 480 if case == "three_wide" else 310
    figures = [[x, 180, x + width, 1000] for x in starts[:count]]
    faces = [[x + 75, 370, x + 235, 550] for x in starts[:count]]
    colours = [(210, 40, 40, 255), (40, 200, 50, 255), (40, 60, 210, 255)][:count]
    people = []
    for index, figure in enumerate(figures):
        left, top, right, bottom = figure
        body = [left - (35 if index == 1 and case != "three_separate" else 0), top,
                right + (35 if index == 0 and case != "three_separate" else 0), bottom]
        people.append({"body": body, "face": faces[index],
                       "outline": [[left, top], [right - 1, top], [right - 1, bottom - 1], [left, bottom - 1]]})
    if case == "bad_outline":
        people[0]["outline"] = []
    if case == "three_touching":
        x0, y0, x1, _ = figures[1]
        people[1]["outline"] = [[x0, y0], [x1 - 1, y0], [x1 - 1, 600], [x0, 600]]
    frame = tmp_path / "frame.png"
    Image.new("RGB", (1920, 1080), (220, 220, 220)).save(frame)
    out = tmp_path / "cand"
    job = {"out": str(out), "renderer": str(si.COVER_RENDERER), "redraw": str(si.COVER_REDRAW),
           "styles": str(si.COVER_STYLES), "frame": str(frame), "lines": ["她破防了", "怎么搞"],
           "font": str(FONT), "repo": str(Path(si.__file__).resolve().parents[3]),
           "host_box": [700, 200, 1100, 900], "guest_box": [100, 100, 500, 900],
           "guest_boxes": [[100 + i * 500, 100, 500 + i * 500, 900] for i in range(count - 1)],
           "peers": case != "three_not_peers", "host_side": "right", "brief": {"expression": "笑", "action": "挥手"},
           "routes": ["cutout", "ai_sticker"],
           "judge_model": "fake", "judge_question": "{route}: {route_note}"}
    calls = []

    def fake_generate(job, kind, prompt, reference, out_path, canvas=None):
        im = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
        draw = ImageDraw.Draw(im)
        if kind == "cutout":
            for fig, colour in zip(figures, colours, strict=True):
                draw.rectangle((fig[0], fig[1], fig[2] - 1, fig[3] - 1), fill=colour)
        else:
            draw.rectangle(STICKER_FIG, fill=(235, 235, 250, 255))
        im.save(out_path)
        return {"status": "AI_BACKGROUND_READY"}

    def fake_vision(job, name, question, images, out_dir):
        calls.append(name)
        if name == "locate-people-cutout":
            assert "李豆沙" not in question
            text = json.dumps({"people": people})
            if case == "three_malformed_outline":
                text = text.replace('"outline": [[', '"outline": [', 1)
            return text, False
        if name.startswith("locate-guest-cutout-"):
            index = int(name.rsplit("-", 1)[1])
            face, fig = faces[index], figures[index]
            return json.dumps({"face": [face[0] - fig[0], face[1] - fig[1], face[2] - fig[0], face[3] - fig[1]]}), False
        if name.startswith("locate-guest-cutout"):
            fig, face = figures[0], faces[0]
            return json.dumps({"face": [face[0] - fig[0], face[1] - fig[1], face[2] - fig[0], face[3] - fig[1]]}), False
        if name == "locate-cutout":
            host = [figures[1][0] - 50, faces[1][1], faces[1][2], faces[1][3]] if case == "bad_host_face" else faces[1]
            return json.dumps({"host_face": host}), False
        if name == "locate-ai_sticker":
            return json.dumps({"host_face": STICKER_FACE}), False
        return json.dumps(cover_verdict()), False

    monkeypatch.setattr(cc, "generate", fake_generate)
    monkeypatch.setattr(cc, "vision", fake_vision)
    path = tmp_path / "job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    assert cc.main(["cover_candidates.py", str(path)]) == 0
    rows = {r["id"]: r for r in json.loads((out / "result.json").read_text())["rows"]}
    assert set(rows) == {"cutout", "ai_sticker"}
    if case in {"bad_outline", "bad_host_face"}:
        assert all(row["failure"] == "PEOPLE_LAYERS_UNAVAILABLE" for row in rows.values())
        assert not (out / "gen-sticker.png").exists()
        return
    renderer = cc.load(job["renderer"], "group_renderer")
    for row in rows.values():
        assert row["status"] == "RENDERED", row
        assert len(row["spec"]["stickers"]) == count
        assert len(row["spec"]["keep"]) == count - 1
        if row["id"] == "ai_sticker" and count > 2:
            assert f"{count} 人同样大小" in row["note"] and "嘉宾为抠图层" in row["note"]
        face = row["face_measured"]
        assert 0 <= face[0] < face[2] <= 1920 and 0 <= face[1] < face[3] <= 1080
        assert face[3] - face[1] >= 270
        guest_faces = row.get("guest_faces_measured") or [row["guest_face_measured"]]
        assert len(guest_faces) == count - 1
        assert all(abs((f[3] - f[1]) - (face[3] - face[1])) <= 2 for f in guest_faces)
        with Image.open(row["image"]) as rendered:
            for i, source_face in enumerate(faces):
                if i == 1 and row["id"] == "ai_sticker":
                    layer = next(s for s in row["spec"]["stickers"] if s["src"].endswith("gen-sticker.png"))
                    source_face, colour = STICKER_FACE, (235, 235, 250)
                else:
                    layer = next(s for s in row["spec"]["stickers"]
                                 if (s["src"].endswith(f".person-{i}.png") or
                                     s["src"].endswith("gen-cutout.png") and cc.inside(source_face, s["crop"])))
                    colour = colours[i][:3]
                placed = cc.place_face(layer, layer["crop"], source_face, renderer)
                pad = (int(layer.get("outline", 12)) + 4
                       if layer.get("outline", 12) and not layer.get("measure_outline") else 0)
                centre = ((placed[0] + placed[2]) // 2 + pad, (placed[1] + placed[3]) // 2 + pad)
                assert rendered.getpixel(centre)[:3] == colour, (case, row["id"], i, centre)
        for layer in row["spec"]["stickers"]:
            with Image.open(layer["src"]) as im:
                pixels = set(im.convert("RGBA").crop(tuple(layer["crop"])).getdata()) & set(colours)
                assert len(pixels) <= 1, (case, layer, pixels)
            if layer["src"] != str(out / "gen-sticker.png"):
                assert layer["cutout"] != "none"
        if case == "three_touching" and row["id"] == "cutout":
            host_layer = next(layer for layer in row["spec"]["stickers"] if layer["src"].endswith(".person-1.png"))
            with Image.open(host_layer["src"]) as im:
                assert im.getpixel((starts[1] + 155, 650))[3] > 0  # incomplete outline must not erase the torso
    assert calls.count("locate-people-cutout") == (0 if case in {"three_separate", "two_narrow"} else 1)


def test_every_visible_peer_must_have_a_measured_equal_face():
    names = ["李豆沙", "甲", "乙"]
    plan = {"peers": {"peers": True, "names": names}}
    row = {"verdict": {"people": {"present": names}}, "face_measured": [100, 400, 400, 720],
           "guest_face_measured": [500, 400, 800, 720], "expected_guest_count": 2,
           "guest_faces_measured": [[500, 400, 800, 720], [1000, 400, 1300, 720]]}
    assert si.peer_face_vetoes(row, plan) == []
    row["guest_faces_measured"] = [[500, 400, 800, 720]]
    assert si.peer_face_vetoes(row, plan) == ["PEER_FACE_UNMEASURED"]
    row["guest_faces_measured"] = [[500, 400, 800, 720], [1000, 400, 1200, 600]]
    assert set(si.peer_face_vetoes(row, plan)) == {"GUEST_FACE_BELOW_25_PERCENT", "PEER_FACE_SIZE_UNEQUAL"}
    row["verdict"] = {"people": {"present": names[:2]}}
    row["guest_faces_measured"] = [[500, 400, 800, 720]]
    assert si.peer_face_vetoes(row, plan) == []
    row["guest_faces_measured"].append(None)
    assert si.peer_face_vetoes(row, plan) == []
    row["verdict"] = {"people": {"present": names}}
    assert si.peer_face_vetoes(row, plan) == ["PEER_FACE_UNMEASURED"]
    row["guest_faces_measured"] = [[500, 400, 800, 720], [1000, 400, 1200, 600]]
    plan["peers"]["peers"] = False
    assert set(si.peer_face_vetoes(row, plan)) == {"GUEST_FACE_BELOW_25_PERCENT", "PEER_FACE_SIZE_UNEQUAL"}


def test_missing_one_peer_reduces_the_score_without_veto():
    names = ["李豆沙", "甲", "乙"]
    plan = {"peers": {"peers": True, "names": names}, "participants": names, "recent": {}}
    verdict = cover_verdict(people=names)
    verdict["people"]["expected"] = names
    row = {"id": "cutout", "route": "cutout", "status": "RENDERED", "verdict": verdict,
           "face_measured": [100, 400, 400, 720], "guest_faces_measured": [[500, 400, 800, 720], [1000, 400, 1300, 720]],
           "expected_guest_count": 2, "punch_font_size": 180, "render": {"measured_lines": []}}
    full, _ = si._scored_rows([row], plan)
    verdict["people"]["present"] = names[:2]
    row["guest_faces_measured"] = row["guest_faces_measured"][:1]
    partial, eligible = si._scored_rows([row], plan)
    assert eligible == ["cutout"]
    assert full[0]["score"]["P"] == 2 and partial[0]["score"]["P"] == 1
    assert full[0]["score"]["total"] - partial[0]["score"]["total"] == 3


def test_mutually_obscuring_figures_cannot_be_passed_as_equal_peers(tmp_path):
    spec = importlib.util.spec_from_file_location("occluded_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    renderer = cc.load(str(si.COVER_RENDERER), "occluded_renderer")
    path = tmp_path / "figure.png"
    Image.new("RGBA", (100, 100), (0, 0, 0, 255)).save(path)
    layers = {role: {"src": str(path), "cutout": "alpha", "crop": [0, 0, 100, 100],
                     "h": 100, "x": 0, "y": 0, "outline": 0} for role in ("host", "guest")}
    faces = {"host": [20, 20, 40, 40], "guest": [60, 20, 80, 40]}
    with pytest.raises(ValueError, match="PEER_FACES_OCCLUDED"):
        cc.face_safe_order(layers, faces, ["host", "guest"], renderer)


@needs_renderer
def test_three_wide_peers_can_use_the_existing_face_floor_without_covering_one_another(tmp_path):
    """When the preferred sizes overlap, fit the same three faces at the allowed floor."""
    spec = importlib.util.spec_from_file_location("floor_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    renderer = cc.load(str(si.COVER_RENDERER), "floor_renderer")
    people = []
    for i in range(3):
        path = tmp_path / f"person-{i}.png"
        img = Image.new("RGBA", (1920, 1080))
        ImageDraw.Draw(img).rectangle((100, 180, 560, 1000), fill=(50 + 50 * i, 80, 90, 255))
        img.save(path)
        people.append({"src": str(path), "cutout": "alpha", "fig": [100, 180, 560, 1000],
                       "face": [105, 400, 555, 600]})
    host, guests = people[1], [people[0], people[2]]
    with pytest.raises(ValueError, match="PEER_FACES_OVERLAP"):
        cc._peer_layers(host, guests, renderer, "left", (cc.FACE_PX, cc.FACE_FLOOR_PX + 10))
    stickers, host_face, guest_faces, _ = cc.peer_layers(host, guests, renderer, "left")
    assert len(stickers) == 3
    assert all(face[3] - face[1] >= cc.FACE_FLOOR_PX for face in [host_face, *guest_faces])


def test_people_reading_is_reused_only_for_the_same_image_bytes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cached_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    path = tmp_path / "cutout.png"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path)
    people = [{"body": [0, 0, 45, 100], "face": [10, 10, 30, 30],
               "outline": [[0, 0], [45, 0], [45, 100], [0, 100]]},
              {"body": [55, 0, 100, 100], "face": [65, 10, 85, 30],
               "outline": [[55, 0], [100, 0], [100, 100], [55, 100]]}]
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"status": "completed", "output_text": json.dumps({"people": people})}).encode()

    def urlopen(_request, timeout):
        calls.append(timeout)
        return Response()

    monkeypatch.setenv("CPA_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("CPA_API_KEY", "test-only")
    monkeypatch.setattr(cc.urllib.request, "urlopen", urlopen)
    job = {"judge_model": "fake"}
    assert cc.located_people(job, str(path), str(tmp_path), 2)
    assert cc.located_people(job, str(path), str(tmp_path), 2)
    assert len(calls) == 1
    Image.new("RGB", (100, 100), (4, 5, 6)).save(path)
    assert cc.located_people(job, str(path), str(tmp_path), 2)
    assert len(calls) == 2


def test_sticker_records_the_reused_cutout_model(tmp_path):
    spec = importlib.util.spec_from_file_location("provenance_candidates", si.COVER_CANDIDATES_TOOL)
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"frame")
    cutout = tmp_path / "gen-cutout.png"
    cutout.write_bytes(b"image")

    class Redraw:
        @staticmethod
        def build_prompt(_brief, _mode):
            return "unchanged prompt"

    key = {"prompt_sha256": hashlib.sha256(b"unchanged prompt").hexdigest(), "reference_sha256": cc.sha(frame)}
    Path(str(cutout) + ".key.json").write_text(json.dumps(key))
    Path(str(cutout) + ".request.json").write_text(json.dumps({"attempts": [{"model": "requested"}]}))
    Path(str(cutout) + ".response.json").write_text(json.dumps({"attempts": [{"service_observation": {"reported_model": "reported"}}]}))
    assert cc.cutout_ready({"brief": {}}, str(tmp_path), Redraw(), str(frame)) == str(cutout)
    assert cc.GENERATIONS["cutout"] == {"model": "requested", "reported_model": "reported", "reused": True}


def test_candidate_job_receives_all_frame_guests_and_session_names(tmp_path):
    def script(name, body, remote):
        if name == "s6-cover":
            fake_cover(remote, ctx.remote_dir)
        return RemoteResult(0, "", "")

    names = ["李豆沙", "甲", "乙"]
    remote = FakeRemote(on_script=script)
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]),
                   session=dict(SESSION, participants=[[name] for name in names]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(5), encoding="utf-8")
    boxes = {"person": [[700, 200, 1100, 900]],
             "guest": [[100, 200, 500, 900], [1200, 200, 1700, 900]]}
    plan = si.cover_plan(ctx, ["她破防了"], {"quotes": []}, names)
    si._render_cover(ctx, [], ["她破防了"], frame_t=1, plan=plan, boxes=boxes, names=names)
    job = json.loads(remote.files[f"{ctx.remote_dir}/cover/job.json"])
    assert job["guest_boxes"] == boxes["guest"]
    assert job["peer_names"] == names


@needs_renderer
def test_five_covers_of_one_session_rotate_the_text_and_stand_the_two_equals_the_same_size(tmp_path):
    remote = LocalRemote()
    chosen, shots = [], []
    for n in range(5):
        ctx = _clip_ctx(tmp_path, f"clip{n}", remote, FakeCpa([TITLE]), 20 + n)
        out = si.step6_title_cover(ctx)
        assert out.ok, (out.reason, out.note)
        route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
        # the split-screen frame's faces are under the floor: the first cover is the cut-out; the next ones would
        # repeat its look (the same two people as they are, text above), so each draws this story once (stage 3)
        assert route["chosen"]["route"] == ("cutout" if n == 0 else "ai_sticker"), (n, out.process)
        first = json.loads((ctx.clip_dir / "cover/cand/result-1.json").read_text(encoding="utf-8"))
        shots.append(next(r for r in first["rows"] if r["id"] == "screenshot"))
        last = sorted((ctx.clip_dir / "cover/cand").glob("result-*.json"))[-1]
        row = next(r for r in json.loads(last.read_text(encoding="utf-8"))["rows"] if r["id"] == route["chosen"]["id"])
        chosen.append((route["chosen"], row))
    # read from what any version records: the style the renderer used (its own selection record, else the spec's
    # pinned id), looked up in the repo's table, and where the host's layer was placed on the cover
    table = {s["style_id"]: s for s in json.loads((Path(si.__file__).resolve().parents[3] / "assets/lidousha/cover_style_rotation.json").read_text(encoding="utf-8"))["styles"]}
    styles = [(row["render"].get("style_selection") or {}).get("style_id") or row["spec"].get("style_id")
              for _, row in chosen]
    colours = {tuple(table[s]["colors"]) for s in styles}
    sizes = {table[s]["size_ratio"] for s in styles}
    host_of = {"cutout": (CUT_HOST_FIG, CUT_HOST_FACE), "ai_sticker": (STICKER_FIG, STICKER_FACE)}
    layouts = {(row["route"], row["render"]["layout"], _host_side(row["spec"], host_of[row["route"]][1]))
               for _, row in chosen}
    assert len(colours) > 1 and len(sizes) > 1 and len(layouts) > 1, (styles, colours, sizes, layouts)
    assert all(a != b for a, b in pairwise(styles))   # the last covers' styles are skipped
    # U98 字色轮换: two covers in a row never share a colour pair (the rotation authority's recent_exclusions)
    pairs = [frozenset(table[s]["colors"]) for s in styles]
    assert all(a != b for a, b in pairwise(pairs)), styles
    for _, row in chosen:
        host = _placed_face_height(row["spec"], *host_of[row["route"]])
        guest = _placed_face_height(row["spec"], CUT_GUEST_FIG, CUT_GUEST_FACE)
        assert min(host, guest) >= si.COVER_FACE_FLOOR_PX, (host, guest)   # each face clears the 25 % floor
        assert max(host, guest) / min(host, guest) <= si.PEER_FACE_RATIO_MAX, (host, guest)
    for shot in shots:  # a small-face screenshot is rendered, then scored as vetoed
        assert shot.get("status") == "RENDERED", shot
        assert shot["face_measured"][3] - shot["face_measured"][1] < si.COVER_FACE_FLOOR_PX
    assert "screenshot.judge" in remote.vision_names


def _rows_ctx(tmp_path, *, participants=SESSION["participants"]):
    session = dict(SESSION, participants=participants)
    return make_ctx(tmp_path, remote=FakeRemote(), cpa=FakeCpa([]), session=session)


def _row(host_face, guest_face, people=("李豆沙", "雾深Girimi"), expected_solo=False):
    verdict = cover_verdict(people, face=tuple(host_face))
    if people == ("李豆沙",) and expected_solo:
        verdict["people"]["expected"] = ["李豆沙"]
    return {"id": "cutout", "route": "cutout", "status": "RENDERED", "verdict": verdict, "punch_font_size": 180,
            "render": {"measured_lines": [{"x": 300, "y": 40, "w": 600, "h": 180}]}, "face_measured": list(host_face),
            "guest_face_measured": list(guest_face) if guest_face else None}


def test_equal_participants_are_each_held_to_the_face_floor_and_to_equal_size(tmp_path):
    ctx = _rows_ctx(tmp_path)
    plan = si.cover_plan(ctx, ["她破防了"], {"quotes": []}, ["李豆沙", "雾深Girimi"])

    def vetoes(row):
        rows, _ = si._scored_rows([row], plan)
        return rows[0]["score"]["status"], rows[0]["score"].get("vetoes")
    assert vetoes(_row([1300, 400, 1600, 720], [300, 400, 590, 715])) == ("ELIGIBLE", [])
    # the old composition: the host's face 320 px, the guest's 180 px (GUEST_H 560)
    status, why = vetoes(_row([1300, 400, 1600, 720], [200, 720, 380, 900]))
    assert status == "VETO" and {"GUEST_FACE_BELOW_25_PERCENT", "PEER_FACE_SIZE_UNEQUAL"} <= set(why)
    status, why = vetoes(_row([1300, 400, 1600, 720], [200, 300, 500, 700]))      # both above the floor, 320 vs 400
    assert status == "ELIGIBLE"
    status, why = vetoes(_row([1300, 400, 1600, 690], [200, 300, 500, 700]))      # 290 vs 400: 1.38
    assert status == "VETO" and why == ["PEER_FACE_SIZE_UNEQUAL"]
    status, why = vetoes(_row([1300, 400, 1600, 720], None))
    assert status == "VETO" and why == ["PEER_FACE_UNMEASURED"]
    # a cover without the guest is not judged for equal size (the score's P=1 says the guest is missing)
    assert vetoes(_row([1300, 400, 1600, 720], None, people=("李豆沙",)))[0] == "ELIGIBLE"
    # control: the host's own floor is the repo scorer's and stays as it was
    status, why = vetoes(_row([1300, 400, 1600, 600], [300, 400, 500, 600]))
    assert status == "VETO" and "MAIN_FACE_BELOW_25_PERCENT" in why
    assert plan["peers"]["peers"] is True and plan["peers"]["source"] == "session.participants"


def test_a_one_person_session_has_no_equals_and_keeps_its_checks(tmp_path):
    ctx = _rows_ctx(tmp_path, participants=[["李豆沙"]])
    plan = si.cover_plan(ctx, ["她破防了"], {"quotes": []}, ["李豆沙"])
    assert (plan.get("peers") or {}).get("peers") is not True     # a control: the same on the old code
    rows, eligible = si._scored_rows([_row([1300, 400, 1600, 720], None, people=("李豆沙",), expected_solo=True)], dict(
        plan, participants=["李豆沙"]))
    assert eligible == ["cutout"]


def test_the_ledger_decides_who_is_equal_for_a_recording_it_lists(tmp_path, monkeypatch):
    ctx = _rows_ctx(tmp_path)
    ctx.state.data["clip"]["source_name"] = "22966160_20261024-19-31-06.flv"     # listed: CONFIRMED live collaboration
    peers = si.cover_peers(ctx)
    assert peers["peers"] is True and peers["source"].startswith(
        "session_relation_ledger:20261024-lidousha-girimi-live-collaboration:CONFIRMED")
    # another collaboration the ledger lists (07-22, 南町), not used to build the fix: the same reader decides it
    other = _rows_ctx(tmp_path / "other", participants=[["李豆沙"], ["南町"]])
    other.session["recording_date"] = "2026-07-22"
    other.state.data["clip"].update(recording_date="2026-07-22", source_name="22966160_20260722-19-35-15.mp4")
    assert si.cover_peers(other) == {"peers": True, "names": ["李豆沙", "南町"], "source":
                                     "session_relation_ledger:20260722-lidousha-nancho-live-collaboration:CONFIRMED"}
    import src.autoslice.session_relation_authority as sra

    claimed = dict(sra.resolve_session_relation(ledger_path=si_ledger(), date="2026-10-24",
                                                recording_path="22966160_20261024-19-31-06.flv"), state="CLAIMED")
    monkeypatch.setattr(sra, "resolve_session_relation", lambda **kw: claimed)
    assert si.cover_peers(ctx)["peers"] is False                                    # an unconfirmed relation is not


def si_ledger() -> Path:
    from src.autoslice.cover_generation import CHANNEL_PROFILE, ROOT

    return CHANNEL_PROFILE.asset_file("session_relation_ledger", repo_root=ROOT)


def test_the_lane_pins_no_style_and_sends_the_rotation_inputs(tmp_path):
    def script(name, body, remote):
        if name == "s6-frame":
            remote.files[f"{ctx.remote_dir}/cover/frames.json"] = json.dumps(FRAMES).encode()
        if name == "s6-cover":
            fake_cover(remote, ctx.remote_dir)
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    ctx = make_ctx(tmp_path / "clip", remote=remote, cpa=FakeCpa([TITLE]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(5), encoding="utf-8")
    history = ["s4-orange-red-tm3-r70-dark", "s2-pink-blue-tm4-r55-dark"]
    for n, style in enumerate(history):
        earlier = tmp_path / "clip" / "clips" / f"older{n}" / "title"
        earlier.mkdir(parents=True)
        earlier.joinpath("candidates.json").write_text(json.dumps({"cover_route": {
            "method": "skill.candidates", "at": f"2026-11-02T00:0{n}:00Z", "recording_date": "2026-10-24",
            "chosen": {"route": "cutout", "style_id": style, "host_side": "right"}}}), encoding="utf-8")
    assert si.step6_title_cover(ctx).ok
    job = json.loads(remote.files[f"{ctx.remote_dir}/cover/job.json"].decode("utf-8"))
    spec = json.loads(remote.files[f"{ctx.remote_dir}/cover/spec.json"].decode("utf-8"))
    assert "style_id" not in job and "style_id" not in spec                       # nothing pins a style
    assert job["content_sha256"] == hashlib.sha256((ctx.clip_dir / "subs/final.srt").read_bytes()).hexdigest()
    assert job["recent_style_ids"] == history  # actual covers, oldest first; no pre-expanded exclusions
    assert job["peers"] is True and job["host_side"] == "left"                     # equals: the other side this time
    table = (Path(si.__file__).resolve().parents[3] / "assets/lidousha/cover_style_rotation.json").read_bytes()
    assert remote.files[f"{ctx.remote_dir}/cover/styles.json"] == table            # the repo's one table, as is
    source = Path(si.__file__).resolve().parents[1] / "cover_style_rotation.py"
    assert remote.files[f"{ctx.remote_dir}/cover/cover_style_rotation.py"] == source.read_bytes()
    assert len(json.loads(table)["styles"]) > 1


@needs_renderer
def test_lane_copies_rotation_and_remote_renderer_consumes_actual_history(tmp_path):
    from src.autoslice.cover_style_rotation import load_table

    table_path = Path(si.__file__).resolve().parents[3] / "assets/lidousha/cover_style_rotation.json"
    table = load_table(table_path)
    base = next(i for i, style in enumerate(table["styles"]) if style["style_id"] == "s5-pink-blue-t3-r85-white")
    n_cues = next(n for n in range(20, 100) if int(hashlib.sha256(srt(n).encode()).hexdigest()[:16], 16) % len(table["styles"]) == base)
    remote = LocalRemote()
    ctx = _clip_ctx(tmp_path, "new", remote, FakeCpa([TITLE]), n_cues)
    history = ["s4-orange-red-tm3-r70-dark", "s2-pink-blue-tm4-r55-dark"]
    for n, style in enumerate(history):
        title = tmp_path / "clips" / f"old{n}" / "title"
        title.mkdir(parents=True)
        (title / "candidates.json").write_text(json.dumps({"cover_route": {
            "method": "skill.candidates", "at": f"2026-11-02T00:0{n}:00Z", "recording_date": "2026-07-22",
            "chosen": {"route": "cutout", "style_id": style, "host_side": "right"}}}), encoding="utf-8")

    out = si.step6_title_cover(ctx)
    assert out.ok, (out.reason, out.note)
    copied = Path(ctx.remote_dir) / "cover"
    job = json.loads((copied / "job.json").read_text(encoding="utf-8"))
    assert job["recent_style_ids"] == history
    assert (copied / "cover_style_rotation.py").read_bytes() == (table_path.parents[2] / "src/autoslice/cover_style_rotation.py").read_bytes()
    results = [json.loads(p.read_text(encoding="utf-8")) for p in (ctx.clip_dir / "cover/cand").glob("result-*.json")]
    rendered = [r for result in results for r in result["rows"] if r.get("status") == "RENDERED"]
    assert rendered
    for row in rendered:
        selection = row["render"]["style_selection"]
        assert selection["base_index"] == base
        assert selection["recent_style_ids"] == sorted(history)
        assert {"style_id": "s5-pink-blue-t3-r85-white", "reason": "recent_colors"} in selection["skipped"]
        assert frozenset(table["styles"][selection["chosen_index"]]["colors"]) != frozenset(("pink", "blue"))


def _session_ctx(tmp_path, earlier=(), rows=None):
    """One clip of the session after the covers already chosen in ``earlier`` (their candidates.json cover_route)."""
    def script(name, body, remote):
        if name == "s6-frame":
            remote.files[f"{ctx.remote_dir}/cover/frames.json"] = json.dumps(FRAMES).encode()
        if name == "s6-cover":
            fake_cover(remote, ctx.remote_dir, rows=rows)
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    ctx = make_ctx(tmp_path / "clip", remote=remote, cpa=FakeCpa([TITLE]))
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs/final.srt").write_text(srt(5), encoding="utf-8")
    for n, (date, chosen) in enumerate(earlier):
        d = tmp_path / "clip" / "clips" / f"older{n}" / "title"
        d.mkdir(parents=True)
        d.joinpath("candidates.json").write_text(json.dumps({"cover_route": {
            "method": "skill.candidates", "at": f"2026-11-02T00:0{n}:00Z", "recording_date": date,
            "chosen": dict({"style_id": "s4-orange-red-tm3-r70-dark", "host_side": "right"}, **chosen)}}),
            encoding="utf-8")
    return ctx, remote


SAME_LOOK = {"route": "cutout", "composition_key": ["cutout", "top", "peers_equal"]}


def test_a_cover_that_repeats_this_sessions_look_does_not_pass_and_one_picture_of_the_story_is_drawn(tmp_path):
    """70-cover: 同场创新按故事表达判断，卡片、配色轮换不等于构图多样性 (U138: a stage that does not pass asks for the
    next stage's one picture).  The cut-out of the same two people with the text above is the look the session's
    earlier cover already has: the drawn sticker (this story's expression and action) is chosen."""
    ctx, remote = _session_ctx(tmp_path, earlier=[("2026-10-24", SAME_LOOK)])
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["chosen"]["route"] == "ai_sticker", out.process
    assert route["chosen"]["composition_key"][0] == "ai_sticker"
    assert any("同路线、同构图" in line and "70-cover" in line for line in out.process), out.process
    covers = [name for name, _ in remote.scripts if name == "s6-cover"]
    assert len(covers) == 3, covers           # screenshot, the free cut-out, then the one drawn stage
    choose = json.loads((ctx.clip_dir / "cover/cand/choose.json").read_text(encoding="utf-8"))
    assert [r for stage in choose["stages"] for r in stage][-1] == "ai_sticker" and len(choose["stages"]) == 3


def test_the_first_cover_of_a_session_and_another_sessions_look_keep_the_free_candidate(tmp_path):
    for n, earlier in enumerate(([], [("2026-07-22", SAME_LOOK)], [("2026-10-24", {"route": "cutout"})])):
        ctx, remote = _session_ctx(tmp_path / str(n), earlier=earlier)
        out = si.step6_title_cover(ctx)
        assert out.ok, out.note
        route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
        assert route["chosen"]["route"] == "cutout", (n, out.process)
        assert route["chosen"]["composition_key"] == ["cutout", "top", "peers_equal"]
        assert sum(name == "s6-cover" for name, _ in remote.scripts) == 2


def test_a_drawn_picture_that_fails_keeps_the_free_candidate_and_draws_no_more(tmp_path):
    rows = [("screenshot", cover_verdict(face=(1380, 470, 1600, 650))), ("cutout", cover_verdict()),
            ("ai_sticker", dict(cover_verdict(), identity={"pass": False, "reason": "多了饰品"}))]
    ctx, remote = _session_ctx(tmp_path, earlier=[("2026-10-24", SAME_LOOK)], rows=rows)
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    route = json.loads((ctx.clip_dir / "title/candidates.json").read_text(encoding="utf-8"))["cover_route"]
    assert route["chosen"]["route"] == "cutout", out.process
    assert any(line.startswith("新出的图没有合格") and "cutout" in line for line in out.process), out.process
    assert sum(name == "s6-cover" for name, _ in remote.scripts) == 3     # no stage after the drawn one
