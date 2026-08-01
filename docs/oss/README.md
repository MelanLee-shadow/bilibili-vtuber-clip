# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

从直播录播到成品切片的全自动流水线：**选题 → 转写 → 证据链校对 → 字幕烧录 →
AI 封面与标题 → 出版登记与上传**。为"发布错误不可接受"的场景设计——每一道
门都是 fail-closed：证据不齐就拒绝交付，而不是硬着头皮发出去。

> An unattended VTuber-stream clipping pipeline (Chinese-first; docs are in
> Chinese). Every stage is an evidence-chained, fail-closed gate: selection →
> ASR → adjudicated subtitle correction → burn → AI cover/title → publication
> registry → upload/repair lanes for Bilibili.

## 它与"随便切切"的区别

- **证据链而非黑箱**：每处字幕修正都要有出处（平台弹幕/礼物记录、独立
  声学听写、闭集裁决回执），并以哈希绑定进交付包。
- **出版登记是唯一上传授权**：已发布内容只能走同 BV 修复链（换源不换稿），
  防止重复投稿与误传。
- **精确重放基线**：修复重跑逐字节恢复已审文本，只有真值台账拥有的区间
  允许变化——修复不会引入新的回归。
- **调试期成本自控**：内容寻址缓存让重试轮零重复模型调用。

## 地图：先读哪些

| 你想做什么 | 入口 |
|---|---|
| 理解流水线规则 | [docs/pipeline/README.md](docs/pipeline/README.md)（分步权威索引） |
| 配一个新频道 | [profiles/README.md](profiles/README.md) + `assets/_template/` 骨架 |
| 知道每个脚本是干嘛的 | [scripts/README.md](scripts/README.md)（按 lane 分组，含"你会真正用到的"） |
| 换 profile 还有哪些坑 | [docs/profile-coupling.md](docs/profile-coupling.md)（剩余耦合点清单） |
| 给 AI 代理的操作约定 | [AGENTS.md](AGENTS.md) |

## 你需要准备什么

| 组件 | 说明 |
|---|---|
| 录播服务器 | 推荐 8 vCPU / 32 GB RAM / 500 GB+ 磁盘（4c/16G 可用但并行烧录吃紧）。参考实机：AMD EPYC 8c/32G，103 秒切片全流程 6–17 分钟 |
| 录制器 | [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder)（`ops/recording/` 有适配器、健康巡检与 webhook 对账，见其 README） |
| LLM 通道 | 需要能访问 **Gemini 系列**（转写精修/声学听写）与 **GPT 系列**（语义裁决/标题/封面）。推荐自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 作为统一入口（本仓所有调用走 `CPA_BASE_URL`/`CPA_API_KEY` 两个环境变量） |
| 免费 ASR | 词级时间轴来自必剪开放转写接口，基于 [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) 的社区研究（见 `scripts/free_asr_client.py`，另含剪映备胎；此处 "free" = 免费） |
| 上传 CLI | [biliup](https://github.com/biliup/biliup)（发布 lane 用；只跑评审/产包可不装） |
| Python | 3.11+（参考部署 3.13），`ffmpeg` 7+ |

凭据清单：CPA 端点 + key、Gemini key（可选备份位）、录播姬房间 cookie（可选，
见 `docs/pipeline/10-source-recording.md`）、biliup cookie 与创作中心
`cookie.json`（只在发布 lane 需要）。环境变量见 `.env.example`；常用变量都
在里面，lane 级变量在对应脚本/模块的 `--help` 与源码顶部。

**关于 AGY**：歌切 lane 与部分声学听写证据引用一个叫 `agy` 的命令行听写器
（把 Gemini 订阅封装成 CLI 的内部工具，未随本仓发布）。谈话切片不需要它
（BCUT 聚合 ASR 足够）；歌切 lane 缺它时会按 fail-closed 拒绝交付而不是降级
瞎猜。接入自己的听写器 = 实现相同的命令行契约（`AGY_BIN` 环境变量指向）。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填 CPA / Gemini 凭据
.venv/bin/python -m pytest -q   # 冒烟：全套件应通过（无凭据也能跑，网络层全部 mock）
```

配频道（示例频道 `lidousha` 开箱即用；配自己的频道见
[profiles/README.md](profiles/README.md)，有 `assets/_template/` 骨架可复制）：

```bash
python3 scripts/validate_channel_profile.py --profile lidousha --config-only
```

第一支切片（本机冒烟，不需要录播服务器）：

```bash
# 对一段本地录播（.flv/.mp4 + 同名弹幕 .xml 可选）跑端到端单候选冒烟：
# 召回 + 产包一条 talk 候选，交付在 lidousha/smoke*/ 下。
# AUTOSLICE_BASE 指向一个可写目录（默认 /opt/bilive/autoslice）。
AUTOSLICE_BASE=$PWD/.autoslice \
  python3 scripts/session_autoslice.py --smoke-segment /path/to/recording.flv
```

要产指定片段而不是让选题器挑，用单候选产线（spec 字段见脚本 docstring）：

```bash
python3 scripts/produce_slice_package.py --spec <spec.json> --ssh-host localhost
```

无人值守整线（录制 → 下播自动切片 → 评审 → 授权上传）按
`ops/recording/README.md` 配录制器与 webhook，再用 `scripts/deploy_autoslice.sh`
作参考部署（按你的主机改写）。发布永远走 `scripts/authorized_upload.py` 的
manifest 闭环——这是唯一上传授权入口。

`assets/lidousha/` 是一个**完整的实战 profile 示例**（词表、标题风格语料、
选题度量、字幕校对原则），来自真实频道的长期运营沉淀，作者选择公开以供
参考。换频道 = 换 profile + 换词表，管线代码不动；剩余的默认 profile 耦合点
全部列在 [docs/profile-coupling.md](docs/profile-coupling.md)。

## 术语表

| 词 | 含义 |
|---|---|
| CPA | 自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 统一 LLM 入口 |
| BCUT | 必剪开放转写接口（免费词级时间轴 ASR） |
| AGY | Gemini 订阅封装的命令行听写器（内部工具，未随仓发布，见上文） |
| bilive | 本项目录制/切片部署层的约定名（`/opt/bilive` 布局、`ops/recording/` 服务名） |
| 出版登记 | `publication_registry`：候选 ↔ BV 的唯一上传授权台账 |
| 真值台账 | `subtitle_truth_ledger`：已发布字幕修复的唯一合法所有者 |

## 状态与已知限制

- Alpha。参考部署已无人值守运行数周（含同 BV 字幕修复的公开验收闭环），
  但多频道抽象仍在收敛中：剩余耦合点见
  [docs/profile-coupling.md](docs/profile-coupling.md)。
- 出版登记/真值台账/评审契约等**运营状态**在本仓只有空模板——它们属于
  每个部署自己的数据。
- 测试套件不需要任何凭据或网络（LLM/HTTP 边界全部 mock）；个别用例在缺
  `ffmpeg` 或表情包媒体时会 skip。约 11 个用例耦合示例 profile 的资产内容，
  换 profile 后按 `assets/_template/` 重建即可。
- 示例 profile 在全新 clone 里全量校验会因声纹文件缺失而 BLOCKED（生物特征
  不随仓分发，预期行为，见 profiles/README.md）。
- Docker 化在路线图上（欢迎 PR）。

## 致谢

- [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) —
  必剪转写接口研究，本仓免费 ASR 聚合层的基础。
- [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) — 录制层。
- [biliup](https://github.com/biliup/biliup) — 上传层。

## 许可

[Apache-2.0](LICENSE)。再分发（含衍生品）须保留 `LICENSE` 与 `NOTICE`
（署名与来源随代码走）；闭源修改允许。

**礼节性请求（非许可条款）**：若你把本项目用于商业服务或商业化内容，
请在至少一处公开材料（产品页/关于页/视频简介）注明使用了
bilibili-vtuber-clip 并附仓库链接——这是社区回馈的最低形式。
