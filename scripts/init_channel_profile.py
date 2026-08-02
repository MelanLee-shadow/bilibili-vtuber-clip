#!/usr/bin/env python3
"""一条命令初始化新频道 profile：复制骨架 + 全部确定性替换一次做完。

此前新频道要手动 cp 模板、逐个改 16+ 个文件里的 `REPLACE_ME-…` schema、再改
profile.json 的占位——全是确定性机械动作。本脚本只做机械部分：

  python3 scripts/init_channel_profile.py <profile-id> \\
      [--display-name 主播名] [--prompt-name "English Name"] [--room 房间号]

做的事：`assets/_template` → `assets/<id>`；`profiles/_template/profile.json`
→ `profiles/<id>/profile.json`（REPLACE_ME/replace_me/assets 路径全替换 +
可选身份字段）；资产文本里的 `REPLACE_ME` 全替换为 `<id>`；最后跑一次
`validate_channel_profile --config-only` 给出下一步指引。

**层 1 身份四件套（词表/人设/标题风格/封面形象）仍然要按模板里的问题清单
问频道主人后填写**——那是判断，不是机械动作，本脚本不代劳。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def init_profile(
    profile_id: str,
    *,
    display_name: str | None = None,
    prompt_name: str | None = None,
    room: str | None = None,
    repo_root: Path = ROOT,
) -> dict:
    if not _ID_RE.match(profile_id):
        raise SystemExit(f"profile id 不合法（小写字母/数字/-_）：{profile_id!r}")
    if profile_id in {"_template", "lidousha"}:
        raise SystemExit(f"不能覆盖内置 profile：{profile_id}")
    assets_src = repo_root / "assets/_template"
    assets_dst = repo_root / "assets" / profile_id
    manifest_src = repo_root / "profiles/_template/profile.json"
    manifest_dst = repo_root / "profiles" / profile_id / "profile.json"
    if assets_dst.exists() or manifest_dst.exists():
        raise SystemExit(
            f"目标已存在（{assets_dst} 或 {manifest_dst}）——不覆盖既有频道"
        )

    shutil.copytree(assets_src, assets_dst)
    replaced_files = 0
    for path in assets_dst.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".md", ".txt"}:
            text = path.read_text(encoding="utf-8")
            if "REPLACE_ME" in text:
                path.write_text(
                    text.replace("REPLACE_ME", profile_id), encoding="utf-8"
                )
                replaced_files += 1

    doc = json.loads(manifest_src.read_text(encoding="utf-8"))

    def walk(node):
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str):
            return (
                node.replace("assets/replace_me", f"assets/{profile_id}")
                .replace("replace_me", profile_id)
                .replace("REPLACE_ME", profile_id)
            )
        return node

    doc = walk(doc)
    doc["profile_id"] = profile_id
    identity = doc.get("identity")
    if isinstance(identity, dict):
        if display_name:
            for key in ("display_name",):
                identity[key] = display_name
            identity["self_reference_aliases"] = [
                display_name if alias == "替换为主播名" else alias
                for alias in identity.get("self_reference_aliases", [])
            ]
            identity["speaker_identity_aliases"] = [
                display_name if alias == "替换为主播名" else alias
                for alias in identity.get("speaker_identity_aliases", [])
            ]
        if prompt_name:
            identity["prompt_name"] = prompt_name
        if room:
            identity["room_id"] = str(room)
    manifest_dst.parent.mkdir(parents=True, exist_ok=True)
    manifest_dst.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    validator_script = repo_root / "scripts/validate_channel_profile.py"
    if validator_script.is_file():
        validate = subprocess.run(
            [
                sys.executable,
                str(validator_script),
                "--profile",
                profile_id,
                "--config-only",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=repo_root,
        )
        validator_output = (validate.stdout or validate.stderr).strip()
    else:
        validator_output = "(validator script not found — run it manually)"
    return {
        "profile_id": profile_id,
        "assets_dir": str(assets_dst),
        "manifest": str(manifest_dst),
        "replace_me_files_rewritten": replaced_files,
        "validator": validator_output,
        "next_steps": [
            "层 1 身份四件套按模板内问题清单问频道主人后填写：glossary.txt / persona.md / title_style.md / cover_identity_prompt.txt",
            "profile.json 的 identity（display_name/room_id/cover_identity 九键）按真实频道改——封面外貌事实务必先抽真实直播帧取证",
            "外貌事实三处必须同步：profile.json cover_identity + persona.md + cover_identity_prompt.txt",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("profile_id")
    parser.add_argument("--display-name")
    parser.add_argument("--prompt-name")
    parser.add_argument("--room")
    args = parser.parse_args()
    result = init_profile(
        args.profile_id,
        display_name=args.display_name,
        prompt_name=args.prompt_name,
        room=args.room,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
