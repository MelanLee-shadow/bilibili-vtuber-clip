# 凭据清单 — 模板与校验

每个凭据：干什么用、放哪、长什么样、怎么验证。**所有 cookie/key 都绝不入库**
（`.gitignore` 已拦 `.env*`；cookie 文件按约定放在仓库外的部署目录）。

> **`.env` 不会被自动加载**：它只是模板/记录处。跑任何脚本前先
> `set -a; source .env; set +a`（或把变量写进服务/cron 环境）。无人值守
> runner 的参考部署另读 `$AUTOSLICE_BASE/cpa.env`（deploy 脚本会生成）。

## 1. CPA（LLM 统一入口）— 必需

- 用途：选题、语义校对裁决、标题、封面的全部 LLM 调用。
- 放哪：`.env` 的 `CPA_BASE_URL` / `CPA_API_KEY`（模板见 `.env.example`）。
- 校验（脚本签名是 `{prompt文件} {completion文件}`，不是内联字符串）：

```bash
printf '回复OK两个字' > /tmp/cpa_probe.txt
bash scripts/llm_via_cpa.sh /tmp/cpa_probe.txt /tmp/cpa_reply.txt && cat /tmp/cpa_reply.txt
```

## 2. Gemini key（转写精修/声学听写）— 必需

- 用途：**直连 Gemini 官方 API，不经 CPA**（CPA 上不需要配 Gemini 模型）。
- 放哪：`.env` 的 `GEMINI_API_KEY`（可留空 `GEMINI_KEY_BACKUP` 兜底位）。
  注意 runner 的读取路径：参考部署从 `/opt/bilive/.env` 读 GEMINI 系列 key、
  从 `$AUTOSLICE_BASE/cpa.env` 读 CPA 对——**都不读仓库根 `.env`**；非 /opt
  布局把 GEMINI key export 进 runner 的进程环境即可（子进程会继承）。
- 校验：跑一次 README 的 `--smoke-segment` 冒烟，观察转写阶段不报 key 错。

## 3. 录播姬的 B 站登录 — 录制层需要

- 用途：录播姬（BililiveRecorder）拉流。在录播姬自己的 WebUI 里登录管理，
  不是本仓的文件。见 `ops/recording/README.md`。

## 4. 创作中心 cookie（`cookie.json`）— 仅发布/稿件维护 lane

- 用途：`bili_archive_tool.py` / `bili_cover_edit.py` / `bili_update_tags.py` /
  同 BV 修复的创作中心 API 调用。
- 放哪：部署约定 `/opt/bilive/app/cookie.json`，或每次用 `--cookie-json` 显式传。
- 模板（两种真实形态都认，见 `src/autoslice/bilibili_member_api.py` 开头说明）：

```json
{"data": {"cookie_info": {"cookies": [
  {"name": "SESSDATA", "value": "…"},
  {"name": "bili_jct", "value": "…"}
]}}}
```

- 校验（只读，不产生任何写操作）：
  `python3 scripts/bili_archive_tool.py view --bvid <你账号任一稿件BV> --cookie-json <路径>`

## 5. biliup cookies（`biliup_cookies.json`）— 仅上传 lane

- 用途：`do_upload.sh` 里的 biliup 投稿。
- 生成：`biliup login`（biliup 官方登录流程，生成 `cookies.json`）。
- 放哪：部署约定 `/opt/bilive/app/tmp_manual_upload/biliup_cookies.json`，或
  `authorized_upload.py --biliup-cookie-json` 显式传。
- 校验：`biliup -u <路径> renew` 能刷新即有效。

## 6. BBDown cookies — 仅官方回放救援 lane（可选）

- 用途：`official-replay-rescue` skill 里 BBDown 下载官方回放。
- 安装：BBDown 是独立工具——`dotnet tool install --global BBDown`，或从
  [BBDown releases](https://github.com/nilaoda/BBDown/releases) 下载单文件二进制。
- 模板：**单行 Cookie 串**文件，至少含 `SESSDATA=…`；部署约定放
  `$AUTOSLICE_BASE/vod_ingest_cookies.txt`。浏览器插件导出的 Netscape
  `cookies.txt`（多行制表符格式）**不能直接用**，转成一行：

```bash
python3 - cookies.txt <<'PY' > vod_ingest_cookies.txt
import sys
rows = [line.split("\t") for line in open(sys.argv[1])
        if line.strip() and not line.startswith("#")]
print("; ".join(f"{r[5]}={r[6].strip()}" for r in rows if len(r) >= 7))
PY
```

- 校验（注意 `-info` 是选项不是子命令，`BBDown info` 会报错）：
  `BBDown <任一公开BV> -info -c "$(cat vod_ingest_cookies.txt)"`
  能列出清晰度即通。
- 安全提示：`-c "$(cat …)"` 会把 SESSDATA 放进进程 argv（本机 `ps` 可见）。
  单用户机器可接受；共享机器建议用 `BBDown login` 的官方登录态代替。

## 7. AGY（Google Antigravity CLI）— talk 声学仲裁与歌切 lane

- 安装：[Google Antigravity](https://antigravity.google/) 官方下载并登录。
- 放哪：`.env` 的 `AGY_BIN`（默认 `~/.local/bin/agy`）。
- 校验：`"$AGY_BIN" --version` 有输出即可被管线调用。
