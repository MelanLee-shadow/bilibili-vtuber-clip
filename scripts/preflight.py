#!/usr/bin/env python3
"""部署体检：一条命令查清首跑前的常见缺件。

默认完全离线（不花钱、不打任何网络）；`--live` 才会真调一次 CPA 验证凭据。
输出三级：`ok` / `warn`（对应 lane 用到才需要）/ `FAIL`（跑不了主线）。
退出码：有 FAIL → 1，否则 0。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, str, str]] = []


def _record(level: str, name: str, detail: str) -> None:
    RESULTS.append((level, name, detail))
    mark = {"ok": "✓", "warn": "⚠", "FAIL": "✗"}[level]
    print(f"{mark} [{level:4}] {name}: {detail}")


def check_python() -> None:
    if sys.version_info >= (3, 11):
        _record("ok", "python", f"{sys.version.split()[0]}")
    else:
        _record("FAIL", "python", f"{sys.version.split()[0]}（需要 3.11+）")


def check_ffmpeg() -> None:
    path = shutil.which("ffmpeg")
    if not path:
        _record("FAIL", "ffmpeg", "PATH 里没有 ffmpeg（切割/烧录/转码全线依赖）")
        return
    probe = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=False
    )
    first = (probe.stdout or "").splitlines()[0] if probe.stdout else ""
    version = first.split(" ")[2] if len(first.split(" ")) > 2 else "?"
    major = version.split(".")[0]
    if major.isdigit() and int(major) >= 6:
        _record("ok", "ffmpeg", f"{version}（6.1+ 实测可用）")
    else:
        _record("warn", "ffmpeg", f"{version}——低于 6 未验证过，建议升级")


def check_profile() -> None:
    try:
        from src.autoslice.channel_profile import load_channel_profile

        profile = load_channel_profile(ROOT)
    except Exception as exc:  # 体检工具：任何加载失败都要人话呈现
        _record("FAIL", "profile", f"加载失败：{type(exc).__name__}: {exc}")
        return
    _record("ok", "profile", f"{profile.profile_id}（{profile.display_name}）")
    missing = [
        f"{key} → {path}"
        for key, path in sorted(profile.asset_files.items())
        if not Path(path).is_file()
    ]
    if missing:
        _record("warn", "profile-assets", "缺文件：" + "；".join(missing[:6]))
    else:
        _record("ok", "profile-assets", f"{len(profile.asset_files)} 个资产文件齐全")
    fonts_dir = profile.asset_directories.get("fonts")
    fonts = (
        sorted(
            p.name
            for p in Path(fonts_dir).iterdir()
            if p.suffix.lower() in {".ttf", ".otf", ".ttc"}
        )
        if fonts_dir and Path(fonts_dir).is_dir()
        else []
    )
    if fonts:
        _record("ok", "fonts", f"{len(fonts)} 个 profile 字体（封面标题字用）")
    else:
        _record("FAIL", "fonts", "profile fonts 目录里没有 ttf/otf——封面渲染会失败")
    if shutil.which("fc-list"):
        cjk = subprocess.run(
            ["fc-list", ":lang=zh", "family"],
            capture_output=True,
            text=True,
            check=False,
        )
        if cjk.stdout.strip():
            _record("ok", "system-cjk-fonts", "fontconfig 有中文字体（libass 烧录用）")
        else:
            _record(
                "FAIL",
                "system-cjk-fonts",
                "系统没有中文字体——字幕会整片烧成豆腐块（apt install fonts-noto-cjk）",
            )
    else:
        _record("warn", "system-cjk-fonts", "没有 fc-list，无法探测（Linux 上装 fontconfig）")
    intro_path = profile.asset_files.get("branding_intro_manifest")
    try:
        import json as _json

        intro = _json.loads(Path(intro_path).read_text(encoding="utf-8"))
        policy = intro.get("policy") or {}
        media = [
            entry.get("runtime_media_path") or ""
            for entry in (intro.get("intros") or [])
        ] + [
            path
            for entry in (intro.get("intros") or [])
            for path in (entry.get("runtime_media_paths") or [])
        ]
        missing_media = [p for p in media if p and not Path(p).is_file()]
        if intro.get("enabled") is False or not policy.get("mandatory"):
            _record("ok", "branding-intro", "片头未强制（新 profile 默认关闭）")
        elif missing_media:
            _record(
                "warn",
                "branding-intro",
                "片头政策强制但媒体缺失（不随仓分发）——talk 交付会被拦；"
                "冒烟用 AUTOSLICE_BRANDING_INTRO=off，或按 manifest 自备媒体",
            )
        else:
            _record("ok", "branding-intro", "片头媒体齐全")
    except (OSError, ValueError, TypeError):
        _record("warn", "branding-intro", "片头 manifest 无法解析（先按关闭处理）")


def check_env() -> None:
    base = os.environ.get("AUTOSLICE_BASE")
    if base:
        path = Path(base)
        try:
            path.mkdir(parents=True, exist_ok=True)
            _record("ok", "AUTOSLICE_BASE", str(path))
        except OSError as exc:
            _record("FAIL", "AUTOSLICE_BASE", f"{base} 不可写：{exc}")
    else:
        _record(
            "warn",
            "AUTOSLICE_BASE",
            "未设置（runner/状态目录需要它；.env 不会被自动加载，记得 source）",
        )
    if os.environ.get("CPA_BASE_URL") and os.environ.get("CPA_API_KEY"):
        _record("ok", "CPA env", "CPA_BASE_URL/CPA_API_KEY 已设置（--live 可真调验证）")
    else:
        _record(
            "warn",
            "CPA env",
            "未设置——选题/校对/标题/封面 lane 会 fail-closed（见 docs/credentials.md #1）",
        )
    # 听音腿：AGY（订阅）与 GEMINI_API_KEY（API）是同一个模型的两种接入，
    # 二选一即可；都配则管线按 订阅→API 次序自动兜底。
    agy = os.path.expanduser(os.environ.get("AGY_BIN", "~/.local/bin/agy"))
    agy_ok = Path(agy).is_file() or bool(shutil.which(agy))
    gemini_ok = bool(os.environ.get("GEMINI_API_KEY"))
    if agy_ok and gemini_ok:
        _record("ok", "听音腿(Gemini)", f"AGY({agy}) + API key 都在——订阅→API 自动兜底")
    elif agy_ok:
        _record("ok", "听音腿(Gemini)", f"经 AGY 订阅接入（{agy}；可选配 GEMINI_API_KEY 作兜底）")
    elif gemini_ok:
        _record("ok", "听音腿(Gemini)", "经 GEMINI_API_KEY 接入（未装 AGY——同一模型，二选一即可）")
    else:
        _record(
            "warn",
            "听音腿(Gemini)",
            "AGY 与 GEMINI_API_KEY 都缺——声学听写/听音仲裁/歌词对轴不可用；"
            "两者是同一模型的两种接入，配一个即可（credentials.md #2）",
        )


def check_vad() -> None:
    script = ROOT / "scripts" / "silero_vad_spans.py"
    if not script.is_file():
        _record("warn", "VAD", "scripts/silero_vad_spans.py 缺失（时轴 QA 证据不可用）")
        return
    model_env = os.environ.get("AUTOSLICE_VAD_MODEL")
    model = Path(model_env) if model_env else ROOT / "assets" / "vad" / "silero_vad.onnx"
    if not model.is_file():
        _record("warn", "VAD", f"模型缺失：{model}")
        return
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError as exc:
        _record("warn", "VAD", f"依赖缺失：{exc}（pip install -r requirements.txt）")
        return
    _record("ok", "VAD", f"脚本+模型+onnxruntime 就绪（{model.name}）")


def check_self_ssh() -> None:
    probe = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "localhost",
            "true",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    deps = subprocess.run(
        ["python3", "-c", "import numpy, onnxruntime"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if deps.returncode == 0:
        _record("ok", "system-python-vad", "PATH python3 有 numpy/onnxruntime（VAD 本地直跑用）")
    else:
        _record(
            "warn",
            "system-python-vad",
            "VAD 用 PATH 里的 python3（不是 .venv），它缺 numpy/onnxruntime——"
            "时轴 QA 会失败；pip install --user numpy onnxruntime",
        )
    if probe.returncode == 0:
        _record("ok", "self-ssh", "ssh localhost 免密可用（AGY 听音复核阶段用）")
    else:
        _record(
            "warn",
            "self-ssh",
            "ssh localhost 不可用——AGY 听音复核（外文 token/歌切）会失败；"
            "纯中文谈话切片不受影响。ssh-keygen -t ed25519 后把公钥追加进 "
            "~/.ssh/authorized_keys",
        )


def check_upload_tools() -> None:
    for tool, hint in (
        ("biliup", "上传 lane 用（不上传可不装）"),
        ("BBDown", "官方回放救援 lane 用（可选）"),
    ):
        if shutil.which(tool):
            _record("ok", tool, shutil.which(tool))
        else:
            _record("warn", tool, f"未安装——{hint}")


def check_gemini_live() -> None:
    import json
    import urllib.request

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        _record("warn", "Gemini live", "GEMINI_API_KEY 未设置，跳过实探")
        return
    try:
        with urllib.request.urlopen(
            "https://generativelanguage.googleapis.com/v1beta/models?key=" + key,
            timeout=20,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        count = len(payload.get("models") or [])
        _record("ok", "Gemini live", f"key 有效（可见 {count} 个模型；额度另算）")
    except Exception as exc:  # 体检工具：人话呈现
        _record("FAIL", "Gemini live", f"key 探活失败：{type(exc).__name__}: {exc}")


def check_cpa_live() -> None:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="preflight_cpa_") as tmp:
        prompt = Path(tmp) / "p.txt"
        reply = Path(tmp) / "c.txt"
        prompt.write_text("回复OK两个字", encoding="utf-8")
        probe = subprocess.run(
            ["bash", str(ROOT / "scripts" / "llm_via_cpa.sh"), str(prompt), str(reply)],
            capture_output=True,
            text=True,
            check=False,
            timeout=240,
        )
        if probe.returncode == 0 and reply.is_file() and reply.read_text().strip():
            _record("ok", "CPA live", f"回复：{reply.read_text().strip()[:40]}")
        else:
            _record("FAIL", "CPA live", f"调用失败：{(probe.stderr or '')[-200:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--live",
        action="store_true",
        help="真调一次 CPA 验证凭据（会产生一次极小的真实调用）",
    )
    args = parser.parse_args()

    check_python()
    check_ffmpeg()
    check_profile()
    check_env()
    check_vad()
    check_self_ssh()
    check_upload_tools()
    if args.live:
        check_cpa_live()
        check_gemini_live()

    fails = [row for row in RESULTS if row[0] == "FAIL"]
    warns = [row for row in RESULTS if row[0] == "warn"]
    print()
    print(f"体检完成：{len(RESULTS) - len(fails) - len(warns)} ok / {len(warns)} warn / {len(fails)} FAIL")
    if fails:
        print("先修 FAIL 项再跑主线；warn 项只影响对应 lane。")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
