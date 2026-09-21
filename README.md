<p align="center">
  <img src="assets/lidousha/emote/hd/07_%E6%9D%8E%E8%B1%86%E6%B2%99_%E8%B4%A1%E4%B8%B8.png" width="300" alt="李豆沙表情包：贡丸">
  <br>
  欢迎关注侄女小李，<a href="https://space.bilibili.com/1703797642">关注李豆沙</a>谢谢喵
</p>

# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

[![CI](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

**把中文直播录播变成可审阅的切片包：选片、字幕、标题封面、烧录和交付。**
软件围绕可追溯证据组织流程；质量检查不通过就保留诊断并停止交付，
不会把“脚本跑完”当成“内容已经正确”。

> A Chinese-first, evidence-driven VTuber clipping pipeline for Bilibili.
> Producing a review package and authorizing publication are separate operations.

> [!IMPORTANT]
> **不推荐人类手动配置本项目——请直接把整个仓库交给你的 AI agent 去配置。**
> [AGENTS.md](AGENTS.md) 是它的操作入口：先验证环境，再依据模板询问频道信息，
> 由 agent 完成配置与验证。人类负责提供真实频道资料、素材授权和发布决定；
> 不需要先读完整仓库，也不能让 agent 编造这些事实。

这是工程项目，不是双击即用的视频编辑器。下面的命令同时供 agent 执行和维护者核验；
愿意手工配置时也可以按同一流程操作。

## 能做什么，不能替你做什么

| 入口 | 输入 | 产出与边界 |
|---|---|---|
| 自动谈话切片 | 录播 `.flv` / `.mp4`，可用的弹幕 `.xml` 与频道配置 | 从候选选题走到字幕、标题、封面和成品包；失败时保留阶段诊断 |
| 单候选制作 | 描述源文件、时间区间和频道的 spec JSON | 只制作指定候选；仍须通过边界、字幕和成品检查 |
| 歌切 | 歌曲片段、歌词及所需音频证据 | 独立的歌词对轴和人声证明流程，不等同于普通谈话转写 |
| 活字乱刷（[`huozi_luanshua.py`](scripts/huozi_luanshua.py)） | 历史直播语料和你想拼的句子 | 从主播说过的话里选取语音片段，核对文字与说话人出处后拼成新句子；输出保留逐片段来源的试听包，不自动上传 |
| 已审原稿的定点修改 | 哈希绑定的已审底稿、明确修改范围和相应授权 | 复用未变内容，只修获准部分；需要时重新烧录并审计实际成片 |
| 授权发布 / 同稿修复 | 通过审计的包、独立授权、账号凭据和出版登记 | 经唯一发布入口执行；已发布候选不能重复新投稿，修复走同 BV 路径 |

**公共仓库提供软件、通用契约、模板和测试，不提供运营账户、真实授权记录、私有审片材料或声纹。**
某些特定历史修复适配器仅保留拒绝执行的兼容占位；文件存在不表示相应私有快车道可用。
没有有效输入或授权，不能通过手写 `PASS`、放松质量门或直接调用上传脚本来补齐。

活字乱刷是按需运行的独立语音重组流程：优先选择较长、连续的原话，减少零碎拼接；
如果小幅改句能让拼接更自然，可以分别生成原句与建议句供试听比较。
它通过剪接重组真实录音，由用户单独触发；入口见
[活字乱刷操作指引](.agent/skills/huozi-luanshua/SKILL.md)。

## 快速开始：先离线验证

### 1. 安装环境

Python **3.11+**；CI 当前覆盖 **3.11、3.12**。媒体流程还需要 `ffmpeg`、`ffprobe`
和可用的字幕渲染支持；Linux 请安装 CJK 字体，例如 `fonts-noto-cjk`。
参考部署面向 Linux；在其他系统上先通过配置检查和测试，再验证媒体及外部工具。

```bash
git clone https://github.com/MelanLee-shadow/bilibili-vtuber-clip.git
cd bilibili-vtuber-clip
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装依赖需要联网。`requirements.txt` 包含 VAD 和可选声纹流程使用的
`onnxruntime`、`torch`、`modelscope`，安装体积不小；它们的存在不代表声纹已经录入。
CI 先安装 CPU 版 PyTorch，以免下载 CUDA 依赖。

### 2. 最小无凭据示例

先使用随仓的示例 profile 检查配置，再查看实际 CLI。这些命令不会制作或上传视频：

```bash
.venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
.venv/bin/python scripts/session_autoslice.py --help
.venv/bin/python scripts/produce_slice_package.py --help
.venv/bin/python -m pytest -q
```

输入是 `profiles/lidousha/profile.json` 及其公开模板资产；输出是配置检查结果。
示例检查返回 `status=READY`、`voiceprint_status=ABSENT`，并列出缺失的配额授权与声纹文件。
这是配置检查的预期结果：**配置可读不等于已具备声纹或全部生产前提**。
测试套件使用合成数据和模型替身；运行测试前不要设置 `AUTOSLICE_PROFILE`。
测试成功不证明外部服务、真实音频质量或账号发布权限正常。

### 3. 配置自己的频道和服务

按 [profiles/README.md](profiles/README.md) 从 `profiles/_template/` 与
`assets/_template/` 建立自己的频道。先填写词表、人物设定、标题风格和封面身份描述；
不要把示例频道的资料或账号合集 ID 当成自己的配置。

```bash
cp .env.example .env
# 编辑 .env：使用自己的目录、频道和服务凭据；不要提交此文件。
set -a
source .env
set +a
.venv/bin/python scripts/preflight.py
```

`.env` **不会自动加载**。参考部署另外读取 `$AUTOSLICE_BASE/cpa.env`；
手工运行时应显式导出环境变量。详细入口与凭据校验见
[docs/credentials.md](docs/credentials.md)。`preflight` 是部署体检，不是无凭据的单元测试。

### 4. 第一支真实谈话切片

下面是**有网络、可能消耗模型额度并写入本地工作文件**的端到端示例，不属于离线验证。
先准备自己有权处理的录播、已配置的 CPA 服务和可能需要的局部听音服务：

```bash
AUTOSLICE_BASE="$PWD/.autoslice" \
AUTOSLICE_BRANDING_INTRO=off \
GEMINI_PAID_BACKUP_DAILY_CAP=0 \
  .venv/bin/python scripts/session_autoslice.py \
  --smoke-segment /absolute/path/to/recording.flv
```

该入口从录播段中尝试召回并制作**一个谈话候选**，不是任意视频都保证出片的命令。
没有合格候选或任一证据门失败时，读取诊断，而不是重复调用以抽到通过结果。
耗时受片长、编码硬件、服务响应和重试影响，没有固定完成时间。

示例 profile 的片头媒体不随仓分发，所以示例显式关闭片头；
自己的频道应在 profile 中设置正确政策或配置真实片头。
媒体读取与本地 VAD 可以走本机；部分音频见证工具仍按入口使用 SSH，
包括可能的 `localhost`。不要把“本地媒体”误认为整个流程都不需要外部服务或 SSH。

需要指定确切片段时，使用 `scripts/produce_slice_package.py --spec <spec.json>`；
spec 结构以该入口的文档和 `--help` 为准。不要给不支持的入口添加 `--dry-run` 或 `--ssh-host`。

## 输入、输出与进度

工作根由 `AUTOSLICE_BASE` 指定，参考默认值是 `/opt/bilive/autoslice`。
录播目录是另一项配置：`AUTOSLICE_REC_ROOT`，必要时另设
`AUTOSLICE_CANONICAL_REC_ROOT`。首次部署应显式设置这些路径，不要沿用示例的存储布局。

| 位置 / 产物 | 如何使用 |
|---|---|
| `state/` | runner 的候选和重试状态；不是字幕真值，也不是上传授权 |
| `out/`、`reports/`、`logs/` | 按阶段保存工作文件、失败原因和执行日志；实际路径见日志和记录 |
| 视频、SRT / ASS、封面、发布 JSON、record 与审计材料 | 成品包的主要组成；具体名称和相互哈希绑定以包内记录为准 |
| 出版登记与同稿修复回执 | 单独记录发布身份与副作用；`review_ready` 不等于已发布 |

检查的是最终 MP4 中**真正可见的字幕、片头和画面**，不是旁边恰好有一份 SRT。
声文对应检查从最终视频音轨重新取证，但它只是粗偏移门，不能证明每个短句都逐字正确。
见证字幕、来源说明、原始 ASR 回答及对应检查结果均纳入审计输入哈希；
输入绑定只证明材料未变，不代替当前声文校验或发布授权。
机械烧录流程及本次输入/输出检查已验证、没有具体异常时，不要求每条视频再完整观看一遍；
实现、字体或布局变化以及内容疑点只复核受影响范围。同稿修复可用独立的机械验收回执，
明确不声称人工观看，也不授予上传权限；入口和适用条件见 [发布步骤](docs/pipeline/90-publish.md)。

## 流水线与模型职责

普通谈话的默认方向是：

```text
录播与聊天 → 候选与边界 → BCUT 基础文字/时间轴
          → CPA 整片文字校对与上下文裁决
          → 具体疑点需要时才取局部盲音频证据 → CPA 裁决
          → 最终字幕检查 → 标题与截图优先封面 → 烧录、审计、交付包
```

**BCUT 是基础 ASR，不由局部听音模型替代。CPA 拥有最终文字裁决权。**
普通谈话不默认把整片送给 AGY 重新精听；歌切、外语词面及其他专门声学契约有各自入口。
截图优先，必要时才修图或生成图像；封面仍须与源事实、身份和标题一致。
最终人物复核回执的读取端会重验原始回答、实际观察状态和完整身份／构图判项，
不能仅凭外层 `PASS` 放行。相关校验代码发生变化时，旧包审计的政策指纹也须失效；
这些是证据复验，不是新增图像模型调用或实际封面质量提升的测量。
已有源构图依据的截图可生成仅含主播的确定性试稿；试稿必须再完成当前最终像素复核，
才能接入完整审阅包。标题位置检查、人物终检和标题—封面联合质检不能互相替代。

| 配置 / 调用点 | 本版代码默认值 | 注意事项 |
|---|---|---|
| `AUTOSLICE_PROFILE` | `lidousha` | 在进程导入时读取；换频道要重新启动进程 |
| CPA 包装器模型 | `gpt-6-astra` | `CPA_CHAT_MODEL` / `CPA_CHAT_MODELS` 可供包装器读取；部分调用点显式指定模型，不能假设一处变量覆盖全部流程 |
| 普通 `correct=cpa` 转写校对 | `gpt-6-astra`、`low` effort | 选题、标题等入口有各自 effort；转写缓存不会降低它们的要求 |
| 实体局部 AGY 听音 | `Gemini 3.6 Flash (High)` | 可由 `ENTITY_AUDIO_AGY_MODEL` 覆盖；客户端标识与 API 模型 ID 不是同一字符串 |
| 实体 Gemini API 后备 | `gemini-3.6-flash` | `ENTITY_AUDIO_GEMINI_API_MODEL`；配额和允许后备的条件仍需满足 |

这些是**本项目发送给网关或客户端的标识**，不保证每个部署都能访问同名模型。
CPA 是本项目使用的 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 接入层，
不是随仓提供的模型服务；涉及图像裁判时必须具备相应视觉能力。
AGY CLI 与 Gemini API 是不同接入方式，模型名称、能力、配额与可用性要分别验证；
设置 API key 不表示每条路径都会自动切换或允许付费后备。

## 原稿快车道与重试缓存

已审原稿的快车道复用合法底稿和未变化的制品；它不重新选片、不任意润色整片，
也不跳过实际烧录、成片审计和发布授权。公开版通用入口见
[脚本地图](scripts/README.md) 与 [打包步骤](docs/pipeline/80-package-delivery.md)。
私有的候选专属授权与回执不随仓分发，因此不能直接重放维护者的历史任务。

普通转写的内容寻址缓存可以复用**已成功且验证通过**的 BCUT 结果与完整 CPA 校对结果。
音频、完整上下文、模型配置、提示词或代码身份改变时不能盲目复用；坏缓存按未命中处理。
缓存命中不意味着下游零调用：代词修复、忠实度检查和其他门仍执行，
后置模型输出也可能不同。缓存证明的是阶段复用，不是最终字幕更准确或整条流水线同比加速。

局部原生听证在派发前持久化预算，并在锁内恢复已验证的耗用历史。MOSS / MAI 的精确字幕窗
请求若结果未知，先找同音频、提供者和模型的有效缓存；无法取回时才在原预算内有界重发，
每个提供者／模型／窗口累计最多三次尝试。旧未知尝试仍计入预算，不当作退款或成功，
坏历史也不能用新的空账本洗掉。这不是上传重试策略，也不证明服务端已计费或字幕已正确。

## 实验与已知限制

- **外部依赖不稳定。** BCUT 接口、模型网关、AGY 客户端和 B 站接口不受本仓控制；
  服务不可用、配额不足或不兼容响应会留下失败诊断，不能由测试通过推定在线可用。
- **身份与多人场景有限。** 普通统一主播样式不是逐段身份鉴定；声纹需要单独配置。
  不要把匿名说话人编号自动绑定为真实人物。
- **研究不等于默认功能。** MOSS / MAI 的局部听证比较不改变本版 BCUT → CPA 主线，
  也不作为公开版已经启用的默认后端。离线实验结论必须限定输入、阶段、费用和质量回归范围。
- **部署不是通用的一键安装器。** Docker 化与跨平台部署仍需完善；参考脚本要按自己的主机、
  目录、锁和服务配置审查后使用。首次声纹模型下载和外部登录不在离线测试范围内。

## 进一步阅读与发布

初次配置读 [profiles/README.md](profiles/README.md) 和
[脚本地图](scripts/README.md)；了解模块关系读
[架构概览](docs/auto-review-architecture.md)。修改具体步骤前先读
[流水线索引](docs/pipeline/README.md)，它指向该步的规则、代码和资产。

只制作审阅包不需要安装 `biliup` 或提供上传 cookie。
真正发布时另需自己的出版登记、授权、账号 cookie 和 `AUTOSLICE_SEASON_IDS`；
合集 / 小节 ID **没有可复用的公共默认值**。
唯一获准的发布入口是 `scripts/authorized_upload.py`，禁止裸调 `do_upload.sh`。
完整流程见 [发布步骤](docs/pipeline/90-publish.md)。

贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)，代理操作约定见 [AGENTS.md](AGENTS.md)，
安全问题按 [SECURITY.md](SECURITY.md) 私密报告，社区约定见
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。CI 的具体环境与命令以
[工作流文件](.github/workflows/ci.yml) 为准。

## 致谢与许可

感谢 [bcut-asr](https://github.com/SocialSisterYi/bcut-asr)、
[BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) 和
[biliup](https://github.com/biliup/biliup)。示例频道为
[李豆沙](https://space.bilibili.com/1703797642)。

[Apache-2.0](LICENSE)。再分发请保留 `LICENSE` 与 [NOTICE](NOTICE)。
商业使用时注明本项目并附仓库链接是礼节性请求，不是额外许可条件。
使用者对所处理素材和发布内容负责；本项目不承诺成品一定正确，也无法代替平台处置站外内容。

## 友情链接

- [沙按钮](https://lu-91015.github.io/shadowlee.github.io/)
