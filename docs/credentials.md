# 服务与凭据：配置、费用和验证边界

配置检查、CLI `--help` 和隔离单元测试不需要服务凭据。
真实制作、录制、救援和发布各有不同依赖，不必为了生成审阅包先准备上传账号。
所有 cookie、key、授权文档和声纹都应保存在仓库外，不能提交或打印到诊断报告。

**手推线（`scripts/manual_lane.py`）**：控制端不读 `.env`，也不需要任何密钥；各步在媒体主机上运行，凭据只从主机的
`<autoslice_base>/cpa.env`、`mai.env`、`moss.env` 读取（`autoslice_base` 写在 `profiles/<id>/manual_lane.json`）。
各步用哪个模型写在 `assets/manual_lane/lane_rules.json`，以它为准，本文下面的模型名属于旧的参考 runner。
开跑前运行 `.venv/bin/python scripts/manual_lane.py preflight`，它在两边逐项检查各步要读的文件和凭据是否在位。

下面是旧的整场参考 runner 的配置说明，只用手推线可以跳过。`.env` 只是模板，不会自动加载：

```bash
set -a
source .env
set +a
```

参考 runner 另外读取 `$AUTOSLICE_BASE/cpa.env` 的 CPA 配置以及
`/opt/bilive/.env` 的 Gemini 配置。使用其他布局时，核对实际入口加载了什么，
不要把“文件已写好”当成“进程已生效”。

## CPA：真实文字与视觉流程

`CPA_BASE_URL` 和 `CPA_API_KEY` 指向自己的网关。包装器和内容质量阶段默认
`gpt-6-sol`，健康探针使用 `gpt-6-luna`；`gpt-6-astra` 不作默认或自动回退（手推线不用这组默认，见上）。
部分调用点显式指定模型和 effort，不全部服从包装器环境覆盖。
文字校对和图像裁判需要相应能力，单次文字请求成功不证明图像请求也可用。

下面是**会调用真实服务、可能计费**的文字探测，不是离线自检。
脚本接收提示词文件和回答文件，不接收内联提示词：

```bash
probe_dir="$(mktemp -d)"
printf '回复OK两个字' > "$probe_dir/prompt.txt"
bash scripts/llm_via_cpa.sh "$probe_dir/prompt.txt" "$probe_dir/reply.txt"
cat "$probe_dir/reply.txt"
```

保留必要的请求状态与模型标识，日志中不要包含 API key。服务不支持某个模型时应修正
兼容配置并重新验证，不能把错误响应当成有效模型回答。

## 局部音频证据与歌切

普通谈话以 BCUT → CPA 整片文字为主线，具体疑点需要时才补局部音频。
AGY CLI 和 Gemini API 是不同接入方式；不能只因名称接近就假定模型、功能或配额相同。

| 入口 | 配置 | 验证边界 |
|---|---|---|
| AGY CLI | `AGY_BIN`；实体听音默认 `ENTITY_AUDIO_AGY_MODEL="Gemini 3.6 Flash (High)"` | 登录、客户端可执行与音频能力分别验证；`--version` 只证明客户端能报告版本 |
| Gemini API | `GEMINI_API_KEY`，可选 `GEMINI_KEY_BACKUP` | 实体 API 模型由 `ENTITY_AUDIO_GEMINI_API_MODEL` 指定，默认 `gemini-3.6-flash` |
| 歌词对轴 | 对应脚本的 provider、模型及歌词输入 | 以入口 `--help` 和歌切步骤为准，不沿用普通谈话的配置推断 |

后备是否可用还受调用路径与配额门约束，设置 key 不保证每条路径自动切换。
示例 `.env.example` 显式设置 `GEMINI_PAID_BACKUP_DAILY_CAP=0`，但这不免除主服务费用。
缺少局部听音能力时相应门可以拒绝，不能为了出片跳过证据要求。
`scripts/preflight.py` 是参考 runner 的部署体检（手推线用 `manual_lane.py preflight`）；真实声音是否正确仍须通过有界的音频实例检验。

## 录制登录

BililiveRecorder 的 B 站登录在录播姬自己的部署中管理，见
[录制说明](../ops/recording/README.md)。不要把录制登录态、创作中心 cookie 和
上传工具 cookie 当成同一种文件。请求的画质也不保证等于实际录到的画质。

## 创作中心 cookie：只在稿件维护 / 发布时需要

`bili_archive_tool.py` 等工具消费创作中心登录态。参考路径为
`/opt/bilive/app/cookie.json`，也可使用入口提供的 `--cookie-json`。
`bilibili_member_api.py` 解析的结构之一如下；省略号不是可用凭据：

```json
{"data":{"cookie_info":{"cookies":[
  {"name":"SESSDATA","value":"…"},
  {"name":"bili_jct","value":"…"}
]}}}
```

有真实授权后，可通过只读查询验证自己的稿件信息：

```bash
python3 scripts/bili_archive_tool.py view \
  --bvid <your-bvid> --cookie-json <absolute-cookie-path>
```

发布还需要频道 profile 里自己的合集资产（`season_routes`）和授权原话；
公共示例的合集 ID 不可使用。出版登记由上传工具自己在运行目录里建，不用手填。

## biliup：只在上传时需要

使用 `biliup login` 生成 cookie，保存在仓库外。第 10 步的上传工具
[`scripts/lane_publish.py`](../scripts/lane_publish.py) 从运行目录（`AUTOSLICE_BASE`）的
`lane_publish.json` 读 biliup 和这份 cookie 的位置：

```json
{"biliup_bin": "/absolute/path/to/biliup", "biliup_cookies": "/absolute/path/outside/the/repo/cookies.json"}
```

投稿、换视频、换封面和加入合集都只用这一份 cookie（它同时带创作中心的登录态）。
登录、续期会涉及网络和登录态变化，不属于本项目的离线测试。
**不要直接调 biliup 投稿，实际发布通过 `lane_publish.py`**：它先审计封存包、记账，再投稿，
中断后重跑同一条命令从已投出的 BV 接着核对，不会再投一次。

## 官方回放救援：可选依赖

BBDown 是单独安装和配置的工具，使用时先读
[official-replay-rescue skill](../.agent/skills/official-replay-rescue/SKILL.md)。
不同工具的 JSON、Netscape cookie 文件和单行 Cookie 字符串不能直接互换。
避免把完整 cookie 放进命令行参数，以免被进程列表记录。
真实下载还需相应源权限、磁盘空间和救援计划，不能用登录成功替代这些前提。
