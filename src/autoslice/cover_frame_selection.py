"""Expressive cover-frame selection（2026-07-21，Ivan：截图直出封面试点）。

从成片里自动找"最具演出效果"的一帧当封面底图。确定性打分，不依赖人脸模型/
numpy（free 主机运行时只有 PIL+ffmpeg+标准库）：

- **avatar 动作能量**：相邻采样帧灰度差，遮罩掉左侧弹幕栏与底部烧录字幕带
  （这两处永远在动但不是"演出"）。皮套直播的背景近静态，动作质量集中在她身上。
- **人声响度**：该时刻音频 RMS——喊叫/爆笑/拖长音就是表情最夸张的时刻。
- **字幕情绪**：该时刻字幕行含 ！？/哭/笑/哈/啊 等情绪标记时加分。
- **清晰度**：拉普拉斯方差惩罚转场/运动糊帧。

片头 branding intro 与结尾淡出通过 skip 窗口排除。输出 top-k 候选（相互间隔
≥2.5s）与全片动作热区 bbox（用于放大脸部占比的裁切锚点，Ivan 2026-07-21 批准
加大脸部占比）。全程 fail-open：任何异常由调用方回退旧的 thumbnail 代表帧。
"""

from __future__ import annotations

import json
import re
import statistics
import struct
import subprocess
from array import array
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageChops, ImageFilter, ImageStat


# 左侧弹幕/SC 栏与底部烧录字幕带的遮罩比例（按本频道 1080p 成片版式实测：
# 弹幕栏 x≲0.20w，字幕带 y≳0.86h；留 buffer）。
_MASK_LEFT_FRAC = 0.22
_MASK_BOTTOM_FRAC = 0.14
_SAMPLE_WIDTH = 320
_MIN_PEAK_GAP_MS = 2_500
# talk 成片强制前置片头 ~4.3s（branding_intro.v1）；歌切无片头但跳过开场 5s
# 同样无害（开头都是铺垫）。结尾 1s 常是淡出。
DEFAULT_SKIP_HEAD_MS = 5_000
DEFAULT_SKIP_TAIL_MS = 1_200

_SRT_TS_RX = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)
_SRT_EMOTION_RX = re.compile(r"[！!？?]|哈哈|哭|笑|啊+|呜|欸|诶|妈呀|天呐|离谱|救命")


def _probe_duration_ms(media_path: Path) -> int:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(media_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return int(float(completed.stdout.strip()) * 1000)


def _audio_rms_envelope(media_path: Path, *, hop_s: float = 0.5) -> list[float]:
    """Mono 8kHz s16le RMS per hop window; [] on any decode failure (fail-open)."""

    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(media_path), "-vn", "-ac", "1", "-ar", "8000",
            "-f", "s16le", "-",
        ],
        capture_output=True, check=False,
    )
    raw = completed.stdout
    if not raw:
        return []
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    hop = max(1, int(8000 * hop_s))
    env: list[float] = []
    for start in range(0, len(samples), hop):
        window = samples[start : start + hop]
        if not window:
            break
        env.append((sum(v * v for v in window) / len(window)) ** 0.5)
    return env


def _parse_srt_emotion_spans(srt_path: Path) -> list[tuple[int, int]]:
    """[(start_ms, end_ms)] of subtitle segments whose text carries emotion marks."""

    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    spans: list[tuple[int, int]] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        match = _SRT_TS_RX.search(block)
        if match is None:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(g) for g in match.groups())
        body = block[match.end():].strip()
        if body and _SRT_EMOTION_RX.search(body):
            spans.append(
                (
                    ((h1 * 60 + m1) * 60 + s1) * 1000 + ms1,
                    ((h2 * 60 + m2) * 60 + s2) * 1000 + ms2,
                )
            )
    return spans


def _zscores(values: Sequence[float]) -> list[float]:
    if len(values) < 2:
        return [0.0 for _ in values]
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    if stdev <= 1e-9:
        return [0.0 for _ in values]
    return [(v - mean) / stdev for v in values]


def _analysis_region(size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    return (int(width * _MASK_LEFT_FRAC), 0, width, int(height * (1 - _MASK_BOTTOM_FRAC)))


def select_expressive_cover_frame(
    media_path: Path,
    *,
    workdir: Path,
    skip_head_ms: int = DEFAULT_SKIP_HEAD_MS,
    skip_tail_ms: int = DEFAULT_SKIP_TAIL_MS,
    sample_fps: float = 2.0,
    top_k: int = 3,
    srt_path: Path | None = None,
) -> dict[str, object]:
    """Score sampled frames and return the most performative moments.

    Returns {"status": "SELECTED", "best_ms", "candidates": [...], "motion_bbox",
    "anchor_x", "head_top", ...} — raises on hard failure (caller falls back)。
    """

    duration_ms = _probe_duration_ms(media_path)
    frames_dir = workdir / "expressive_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.glob("sample_*.png"):
        stale.unlink()
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(media_path),
            "-vf", f"fps={sample_fps},scale={_SAMPLE_WIDTH}:-2",
            str(frames_dir / "sample_%05d.png"),
        ],
        capture_output=True, check=True,
    )
    frame_paths = sorted(frames_dir.glob("sample_*.png"))
    if len(frame_paths) < 4:
        raise RuntimeError(f"too few sampled frames ({len(frame_paths)})")

    audio_env = _audio_rms_envelope(media_path)
    emotion_spans = _parse_srt_emotion_spans(srt_path) if srt_path else []

    laplacian = ImageFilter.Kernel((3, 3), [0, -1, 0, -1, 4, -1, 0, -1, 0], scale=1)
    grays: list[Image.Image] = [Image.open(p).convert("L") for p in frame_paths]
    region = _analysis_region(grays[0].size)
    acc_diff = Image.new("L", grays[0].size, 0)

    stamps_ms: list[int] = []
    motion: list[float] = []
    sharp: list[float] = []
    audio: list[float] = []
    emotion: list[float] = []
    for index, gray in enumerate(grays):
        ms = int(index / sample_fps * 1000)
        stamps_ms.append(ms)
        if index == 0:
            motion.append(0.0)
        else:
            diff = ImageChops.difference(gray, grays[index - 1])
            motion.append(float(ImageStat.Stat(diff.crop(region)).mean[0]))
            acc_diff = ImageChops.lighter(acc_diff, diff)
        sharp.append(float(ImageStat.Stat(gray.crop(region).filter(laplacian)).stddev[0]))
        env_index = min(int(ms / 500), len(audio_env) - 1) if audio_env else -1
        audio.append(audio_env[env_index] if env_index >= 0 else 0.0)
        emotion.append(
            1.0 if any(start <= ms <= end for start, end in emotion_spans) else 0.0
        )

    z_motion = _zscores(motion)
    z_audio = _zscores(audio)
    z_sharp = _zscores(sharp)
    scores = [
        z_motion[i] + z_audio[i] + 0.25 * z_sharp[i] + 0.35 * emotion[i]
        for i in range(len(stamps_ms))
    ]
    window_lo = skip_head_ms
    # 片头→正片过渡自适应（2026-07-21 实锤：z1-budui 片头 5749ms > 固定 skip
    # 5000ms，过渡帧的整屏 diff 把 6000ms 打成假峰、平静脸拿 8 分）。片头版本
    # 会轮换（Z1/Z2/未来 Z3 时长不同），不硬编码时长：前 9s 内 >8×全片中位的
    # 全局 diff 突刺视为片头切点，窗口起点推到最后一个突刺后 1.2s。
    positive_motion = sorted(m for m in motion[1:] if m > 0)
    if positive_motion:
        median_motion = positive_motion[len(positive_motion) // 2]
        boundary_ms = None
        for i, ms in enumerate(stamps_ms):
            if i == 0 or ms > 9_000:
                continue
            if median_motion > 0 and motion[i] > 8 * median_motion:
                boundary_ms = ms
        if boundary_ms is not None:
            window_lo = max(window_lo, boundary_ms + 1_200)
    window_hi = max(window_lo + 1_000, duration_ms - skip_tail_ms)
    eligible = [
        i for i, ms in enumerate(stamps_ms) if window_lo <= ms <= window_hi and i > 0
    ]
    if not eligible:
        eligible = list(range(1, len(stamps_ms)))

    picked: list[int] = []
    for i in sorted(eligible, key=lambda i: scores[i], reverse=True):
        if all(abs(stamps_ms[i] - stamps_ms[j]) >= _MIN_PEAK_GAP_MS for j in picked):
            picked.append(i)
        if len(picked) >= top_k:
            break

    # 局部主体判定（放大裁切的开关）：选中帧与其前 ~1s 帧的局部运动若聚成
    # "窄而高"的单主体块（皮套大身位在说话/动作），才允许把裁切锚在它上面；
    # 游戏/杂景（运动铺满宽幅、或只有角落小窗）不放大，保全景防止把游戏 UI
    # 当脸放大（2026-07-21 辣妹案）。
    best_index = picked[0]
    prev_index = max(0, best_index - max(1, int(sample_fps)))
    local_diff = ImageChops.difference(grays[best_index], grays[prev_index])
    local_mask = Image.new("L", local_diff.size, 0)
    local_mask.paste(255, region)
    local_masked = ImageChops.multiply(local_diff, local_mask.point(lambda p: 255 if p else 0))
    local_hist = local_masked.histogram()
    local_total = sum(local_hist[1:])
    local_thr, running = 255, 0
    for level in range(255, 0, -1):
        running += local_hist[level]
        if local_total and running / local_total >= 0.10:
            local_thr = level
            break
    local_hot = local_masked.point(lambda p: 255 if p >= local_thr else 0)
    subject_bbox = local_hot.getbbox()
    subject_confident = False
    subject_bbox_frac = None
    if subject_bbox is not None:
        bw = (subject_bbox[2] - subject_bbox[0]) / local_hot.width
        bh = (subject_bbox[3] - subject_bbox[1]) / local_hot.height
        hot_crop = local_hot.crop(subject_bbox)
        fill = hot_crop.histogram()[255] / max(1, hot_crop.width * hot_crop.height)
        subject_bbox_frac = [
            round(subject_bbox[0] / local_hot.width, 4),
            round(subject_bbox[1] / local_hot.height, 4),
            round(subject_bbox[2] / local_hot.width, 4),
            round(subject_bbox[3] / local_hot.height, 4),
        ]
        subject_confident = bool(bw <= 0.55 and bh >= 0.30 and fill >= 0.10)
    # 主体不自信时才找角落小窗（游戏场截图回归的钥匙）；找到的窗供
    # extract_zoomed_cover_frame 裁剪放大做截图底，找不到维持原路线。
    camera_window_bbox_frac = (
        None
        if subject_confident
        else _persistent_motion_window(grays, region, sample_fps)
    )

    # 动作热区 → 裁切锚点：遮罩外区域清零后按高分位阈值取 bbox。质心/头顶
    # 供 extract_zoomed_cover_frame 放大脸部占比。
    mask = Image.new("L", acc_diff.size, 0)
    mask.paste(255, region)
    acc_masked = ImageChops.multiply(acc_diff, mask.point(lambda p: 255 if p else 0))
    histogram = acc_masked.histogram()
    total = sum(histogram[1:])  # ignore zero bin (masked + static)
    threshold, running = 255, 0
    for level in range(255, 0, -1):
        running += histogram[level]
        if total and running / total >= 0.10:  # top-decile motion pixels
            threshold = level
            break
    hot = acc_masked.point(lambda p: 255 if p >= threshold else 0)
    bbox = hot.getbbox()
    scale = 1.0
    anchor_x = head_top = None
    if bbox is not None:
        # weighted centroid at 1/8 resolution (pure-PIL, no numpy)
        small = acc_masked.resize(
            (max(1, acc_masked.width // 8), max(1, acc_masked.height // 8))
        )
        data = small.tobytes()  # mode "L": one byte per pixel, row-major
        mass = sum(data)
        if mass > 0:
            cx = sum((i % small.width) * v for i, v in enumerate(data)) / mass
            anchor_x = (cx + 0.5) / small.width  # fraction of width
        head_top = bbox[1] / acc_masked.height  # fraction of height
        scale = acc_masked.width

    return {
        "status": "SELECTED",
        "schema": "cover-frame-selection.v1",
        "media_duration_ms": duration_ms,
        "sample_fps": sample_fps,
        "skip_head_ms": skip_head_ms,
        "skip_tail_ms": skip_tail_ms,
        "signals": {
            "audio_env_available": bool(audio_env),
            "emotion_spans": len(emotion_spans),
        },
        "best_ms": stamps_ms[picked[0]],
        "candidates": [
            {
                "ms": stamps_ms[i],
                "score": round(scores[i], 4),
                "motion_z": round(z_motion[i], 4),
                "audio_z": round(z_audio[i], 4),
                "sharp_z": round(z_sharp[i], 4),
                "emotion": emotion[i],
            }
            for i in picked
        ],
        "motion_bbox_frac": (
            [round(v / scale, 4) if axis % 2 == 0 else round(v / acc_masked.height, 4)
             for axis, v in enumerate(bbox)]
            if bbox is not None
            else None
        ),
        # 动作热区占画面比例：皮套大身位场景 ≲0.6；游戏/杂景（她只是角落小窗）
        # 热区铺满全屏 → 调用方据此放弃放大裁切（避免把游戏 UI 当脸放大）。
        "motion_dispersion_frac": (
            round(
                ((bbox[2] - bbox[0]) / acc_masked.width)
                * ((bbox[3] - bbox[1]) / acc_masked.height),
                4,
            )
            if bbox is not None
            else None
        ),
        "anchor_x_frac": round(anchor_x, 4) if anchor_x is not None else None,
        "head_top_frac": round(head_top, 4) if head_top is not None else None,
        "subject_bbox_frac": subject_bbox_frac,
        "subject_confident": subject_confident,
        "camera_window_bbox_frac": camera_window_bbox_frac,
        "subject_anchor_x_frac": (
            round((subject_bbox_frac[0] + subject_bbox_frac[2]) / 2, 4)
            if subject_bbox_frac is not None
            else None
        ),
        "subject_head_top_frac": (
            subject_bbox_frac[1] if subject_bbox_frac is not None else None
        ),
    }


def _persistent_motion_window(
    grays: Sequence["Image.Image"],
    region: tuple[int, int, int, int],
    sample_fps: float,
) -> list[float] | None:
    """角落小窗探测（2026-07-25 Ivan 授权游戏场景截图回归）。

    游戏画面的运动铺满画布且形态逐帧多变；主播立绘小窗则是**位置固定的
    持续小运动块**（皮套一直在说话/眨眼）。8x8 网格统计块级运动持续率：
    ≥70% 帧对持续活跃、块簇 bbox 面积占画面 2%-20%、簇内实心率 ≥50%
    才判为小窗。探测失败返回 None（调用方维持原路线，宁缺勿错）。"""

    if len(grays) < 4:
        return None
    step = max(1, int(sample_fps))
    pairs = [(i, i + step) for i in range(0, len(grays) - step, step)][:12]
    if len(pairs) < 4:
        return None
    grid = 8
    hits = [0] * (grid * grid)
    for a, b in pairs:
        diff = ImageChops.difference(grays[a], grays[b])
        mask = Image.new("L", diff.size, 0)
        mask.paste(255, region)
        diff = ImageChops.multiply(diff, mask.point(lambda p: 255 if p else 0))
        small = diff.resize((grid, grid), Image.Resampling.BOX)
        for idx, value in enumerate(small.tobytes()):
            if value >= 24:
                hits[idx] += 1
    need = int(len(pairs) * 0.7)
    cells = [
        (idx // grid, idx % grid)
        for idx, count in enumerate(hits)
        if count >= need
    ]
    if not cells:
        return None
    rows = [r for r, _ in cells]
    cols = [c for _, c in cells]
    box_w = max(cols) - min(cols) + 1
    box_h = max(rows) - min(rows) + 1
    area_frac = (box_w * box_h) / (grid * grid)
    if not 0.02 <= area_frac <= 0.20:
        return None
    if len(cells) / (box_w * box_h) < 0.5:
        return None
    return [
        min(cols) / grid,
        min(rows) / grid,
        (max(cols) + 1) / grid,
        (max(rows) + 1) / grid,
    ]


def extract_zoomed_cover_frame(
    media_path: Path,
    ms: int,
    out_path: Path,
    *,
    zoom: float = 1.32,
    anchor_x_frac: float | None = None,
    head_top_frac: float | None = None,
    window_bbox_frac: Sequence[float] | None = None,
) -> dict[str, object]:
    """Full-res frame at ``ms`` with an avatar-anchored 16:9 face zoom.

    Ivan 2026-07-21 批准加大脸部占比：默认 1.32x，裁切窗锚定动作质心水平位置、
    顶边贴 avatar 头顶（顺带裁掉底部烧录字幕带）。无锚点时中央裁切。
    ``window_bbox_frac``（2026-07-25 游戏场小窗回归）：显式主播小窗 bbox，
    加 10% padding 后扩短边归 16:9 裁剪放大——绕过 zoom 上限，让角落立绘
    小窗成为可用的截图底。
    """

    raw_path = out_path.with_suffix(".raw.png")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{ms / 1000:.3f}", "-i", str(media_path),
            "-frames:v", "1", str(raw_path),
        ],
        capture_output=True, check=True,
    )
    frame = Image.open(raw_path).convert("RGB")
    width, height = frame.size
    if window_bbox_frac is not None and len(window_bbox_frac) == 4:
        wx0 = int(float(window_bbox_frac[0]) * width)
        wy0 = int(float(window_bbox_frac[1]) * height)
        wx1 = int(float(window_bbox_frac[2]) * width)
        wy1 = int(float(window_bbox_frac[3]) * height)
        pad_x = int((wx1 - wx0) * 0.10)
        pad_y = int((wy1 - wy0) * 0.10)
        wx0, wy0 = max(0, wx0 - pad_x), max(0, wy0 - pad_y)
        wx1, wy1 = min(width, wx1 + pad_x), min(height, wy1 + pad_y)
        w, h = wx1 - wx0, wy1 - wy0
        if w * 9 > h * 16:  # too wide → grow height
            need_h = max(1, int(w * 9 / 16))
            wy0 = max(0, wy0 - (need_h - h) // 2)
            wy1 = min(height, wy0 + need_h)
            wy0 = max(0, wy1 - need_h)
        else:  # too tall → grow width
            need_w = max(1, int(h * 16 / 9))
            wx0 = max(0, wx0 - (need_w - w) // 2)
            wx1 = min(width, wx0 + need_w)
            wx0 = max(0, wx1 - need_w)
        cropped = frame.crop((wx0, wy0, wx1, wy1)).resize(
            (1920, 1080), Image.Resampling.LANCZOS
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(out_path)
        raw_path.unlink(missing_ok=True)
        return {
            "frame_ms": ms,
            "zoom": round(width / max(1, wx1 - wx0), 4),
            "crop_box": [wx0, wy0, wx1, wy1],
            "source_size": [width, height],
            "camera_window_crop": True,
        }
    zoom = max(1.0, min(zoom, 1.6))
    crop_w = int(width / zoom) // 2 * 2
    crop_h = int(crop_w * 9 / 16)
    anchor_px = int((anchor_x_frac if anchor_x_frac is not None else 0.55) * width)
    x0 = max(0, min(width - crop_w, anchor_px - crop_w // 2))
    top_px = int((head_top_frac if head_top_frac is not None else 0.02) * height)
    y0 = max(0, min(height - crop_h, top_px - int(crop_h * 0.03)))
    cropped = frame.crop((x0, y0, x0 + crop_w, y0 + crop_h)).resize(
        (1920, 1080), Image.Resampling.LANCZOS
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path)
    raw_path.unlink(missing_ok=True)
    return {
        "frame_ms": ms,
        "zoom": zoom,
        "crop_box": [x0, y0, x0 + crop_w, y0 + crop_h],
        "source_size": [width, height],
    }
