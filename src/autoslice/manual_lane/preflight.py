"""Before the first clip: is everything the steps will read there, on this side and on the media host?

`manual_lane.py preflight` asks once what the steps would otherwise find out one at a time, some of them after an hour
of work (a new channel's first clip stopped at step 5 and at step 6 on two files its template did not have).  Nothing is
changed and no model is called.  Each check is one line: what is fine, what is missing and which step needs it.
A missing thing is a problem (exit code 2); a note (the voiceprint, the optional recognisers, no intro) only says what
the pipeline will do without it."""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from . import channel
from .adapters import Remote
from .layout import LAYOUT_FILE, REPO_ROOT, _profile_dir, layout


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    problems: int = 0

    def ok(self, text: str) -> None:
        self.lines.append(f"正常  {text}")

    def note(self, text: str) -> None:
        self.lines.append(f"提示  {text}")

    def problem(self, text: str) -> None:
        self.problems += 1
        self.lines.append(f"缺    {text}")

    def tried(self, what: str, step: str, load: Callable[[], object]) -> None:
        """One file the steps load, loaded the way they load it."""
        try:
            load()
        except Exception as exc:            # each loader has its own error class; the message is what the reader needs
            self.problem(f"{what}（{step}要用）：{str(exc)[:240]}")
        else:
            self.ok(what)


def local_checks(report: Report) -> bool:
    """The channel's files the steps read on the controller.  Returns whether the channel uses an intro."""
    from src.autoslice import branding_intro
    from src.autoslice.title_editorial_method import load_title_editorial_method
    from src.autoslice.upload_tag_policy import load_selected_upload_tag_policy

    from . import title_authority, title_verdicts

    layout_file = _profile_dir(os.environ) / LAYOUT_FILE
    unfilled = [key for key, value in json.loads(layout_file.read_text(encoding="utf-8")).items()
                if not key.startswith("_") and isinstance(value, str) and "replace_me" in value.lower()]
    if unfilled:
        report.problem(f"{LAYOUT_FILE} 里这些键还是模板占位值：{'、'.join(unfilled)}")
    else:
        report.ok(f"{LAYOUT_FILE}（主机、目录、录像位置）")
    report.tried("manual_lane_channel.json（提示词里关于频道的话）", "各步提示词", channel.text)
    report.tried("投稿标签规则 upload_tag_policy.json", "选片和第 7 步", load_selected_upload_tag_policy)
    report.tried("标题方法 title_editorial_method.json", "第 6 步", load_title_editorial_method)
    report.tried("title_style.md 的「## 标题」「## 封面文案」两节", "第 6 步",
                 lambda: title_authority._style_sections(title_authority.TITLE_STYLE.read_text(encoding="utf-8")))
    report.tried("频道主对标题的评语 title_owner_verdicts.v1.json（可以是空列表）", "第 6 步", title_verdicts.load_verdicts)
    manifest = REPO_ROOT / branding_intro.BRANDING_INTRO_MANIFEST_RELPATH
    try:
        enabled = json.loads(manifest.read_text(encoding="utf-8")).get("enabled") is not False
    except (OSError, ValueError) as exc:
        report.problem(f"片头清单 {manifest.name}（第 5 步要用）：{exc}")
        return False
    if enabled:
        report.tried("片头清单（启用）", "第 5 步", lambda: branding_intro.load_branding_intro_policy(manifest))
    else:
        report.note("片头清单写明不用片头（enabled: false）：第 5 步不加片头")
    return enabled


def host_checks(report: Report, remote: Remote, *, intro: bool) -> None:
    """What the steps need on the media host, asked in one script."""
    lay = layout()
    q = shlex.quote
    voice = [v for v in lay.speaker_v8.values() if v]
    rows = [
        ("sudo", "sudo -n true", "免密 sudo（需要密钥的命令在 root 子 shell 里跑）", True),
        ("ffmpeg", "command -v ffmpeg && command -v ffprobe", "ffmpeg、ffprobe（第 2、5 步）", True),
        ("repo", f"test -f {q(lay.remote_repo)}/scripts/manual_lane.py", f"仓库副本 {lay.remote_repo}", True),
        ("python", f"cd {q(lay.remote_repo)} && {q(lay.remote_py)} -c 'import src.autoslice'",
         f"主机端解释器 {lay.remote_py} 能导入仓库代码", True),
        ("cpa", f"sudo -n test -f {q(lay.env_file('cpa'))}", f"模型网关凭据 {lay.env_file('cpa')}", True),
        ("mai", f"sudo -n test -f {q(lay.env_file('mai'))}", "整片识别 mai.env（没有就不用这一家）", False),
        ("moss", f"sudo -n test -f {q(lay.env_file('moss'))}", "整片识别 moss.env（没有就不用这一家）", False),
        ("font", f"test -r {q(lay.cover_font)}", f"封面字体 {lay.cover_font}（第 6 步）", True),
        ("work", f"mkdir -p {q(lay.work_root)} && test -w {q(lay.work_root)}", f"工作根 {lay.work_root} 可写", True),
        ("archive", f"sudo -n test -d {q(lay.archive_room_dir.rsplit('/', 1)[0])}", f"录像根 {lay.archive_room_dir.rsplit('/', 1)[0]}", True),
        ("room", f"sudo -n test -d {q(lay.archive_room_dir)}", f"这个直播间的录像目录 {lay.archive_room_dir}（没有＝还没录过）", False),
    ]
    if intro:
        rows.append(("intro", f"test -n \"$(ls {q(lay.intro_dir)} 2>/dev/null)\"", f"片头素材目录 {lay.intro_dir}（第 5 步）", True))
    if voice:
        rows.append(("voice", " && ".join(f"test -e {q(v)}" for v in voice), "声纹工作区（speaker_v8 的各路径）", True))
    body = "".join(f"if ({cmd}) >/dev/null 2>&1; then echo PF {key} OK; else echo PF {key} MISSING; fi\n" for key, cmd, _t, _r in rows)
    result = remote.run_script(name="preflight", body=body, timeout=180)
    seen = dict(line.split()[1:3] for line in result.stdout.splitlines() if line.startswith("PF ") and len(line.split()) == 3)
    if not seen:
        report.problem(f"连不上媒体主机或脚本没跑起来（rc={result.rc}）：{(result.stdout + result.stderr)[-200:]}")
        return
    for key, _cmd, text, required in rows:
        if seen.get(key) == "OK":
            report.ok(text)
        elif required:
            report.problem(text)
        else:
            report.note(f"没有：{text}")
    if not voice:
        report.note("没有配置声纹工作区：只有主播一个人说话的片照常做；有嘉宾或在看视频的片会停在第 4 步")


def preflight(remote: Remote | None) -> Report:
    report = Report()
    intro = local_checks(report)
    if remote is not None:
        host_checks(report, remote, intro=intro)
    return report
