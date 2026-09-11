<p align="center">
  <img src="assets/lidousha/emote/hd/07_%E6%9D%8E%E8%B1%86%E6%B2%99_%E8%B4%A1%E4%B8%B8.png" width="300" alt="李豆沙表情包：贡丸">
  <br>
  欢迎关注侄女小李，<a href="https://space.bilibili.com/1703797642">关注李豆沙</a>谢谢喵
</p>

# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

[![CI](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

主播下播后，这套系统自己完成从录播到成品的全部工作：挑出值得切的片段、
生成并校对字幕、烧录、配 AI 封面和标题，最后把等待人工过目的成品包放到
交付目录。它为"发布错误不可接受"的场景设计：任何一步证据不齐就**拒绝交付**，
而不是硬着头皮发出去。

> An unattended VTuber-stream clipping pipeline for Bilibili (Chinese-first;
> docs are in Chinese). Fail-closed at every stage.

> [!IMPORTANT]
> **不推荐人类手动配置本项目——请直接把整个仓库交给你的 AI agent 去配置。**
> 本项目的配置面（profile 体系、资产模板、校验器、文档）就是按“由 agent
> 阅读并执行”设计的，[AGENTS.md](AGENTS.md) 是它的完整操作手册；人类只需要
> 回答 agent 提出的频道问题、最后过目成品。
>
> 本项目需要两个**多模态** LLM 才能完整工作：一个**能听音频**的模型
> （默认 `gemini-3.6-flash`：声学听写、字幕听音仲裁、歌词对轴——两种接入
> **二选一**：[Google Antigravity](https://antigravity.google/) 官方订阅的
> CLI（AGY），或 Gemini API key。**它们是同一个模型**，只是订阅面和 API 面
> 的区别；都配上则自动按订阅→API 的次序做配额兜底），和一个**能看画面**
> 的模型（默认 `gpt-6-astra`：CPA 文字裁决、封面视觉裁判、构图/身份核验）。
> 纯文本模型跑不完整条产线。

## 功能一览

| 功能 | 输入 | 输出 | 它做了什么 |
|---|---|---|---|
| 自动切片主线（`session_autoslice.py`） | 录播姬录出的 `.flv/.mp4` + 弹幕 `.xml` | 交付目录里的成品包：视频、烧好字幕、封面、标题、审片材料 | 下播后自动挑选题（用 LLM 从观众视角找“值得切”的片段）→ 免费 ASR 出字幕 → 用弹幕/礼物记录和声学证据校对专名与误听 → 烧录 → AI 封面标题 → 等人工评审 |
| 单候选产线（`produce_slice_package.py`） | 一个 spec（指定录播文件和起止时间） | 同上的单个成品包 | 跳过自动选题，把你指定的片段走完整条产线 |
| 人工评审与授权上传（`audit_review_package.py` → `build_final_human_review.py` → `authorized_upload.py`） | 成品包 | B 站稿件（含合集、tag）+ 入库的上传凭证 | 机器先全面审计包的一致性，人确认后由唯一入口投稿；已发布的稿件只允许“同 BV 修复”（换源不换稿），杜绝重复投稿 |
| 歌切 | 歌曲片段、歌词及所需音频证据 | 独立的歌词对轴和人声证明结果 | 使用专门的歌切入口，不把歌曲当普通谈话转写 |
| 已审原稿的定点修改 | 哈希绑定的已审底稿、明确修改范围和相应授权 | 只修获准部分的成品包 | 复用未变内容，需要时重新烧录并审计实际成片 |
| 新闻/社区 crawler | 官方成员源、B 站切片 metadata 与有限评论、ACG 新闻源 | 低频官方名册 + 每日时效词/社区称呼/话题图 | 官方成员只低频校验；新闻和新出现的昵称、粉丝名、事件梗每天增量抓取并累积证据；CPA 负责关系语义分类，确定性证据门负责接受；社区词不冒充官方词面也不直接改字幕 |
| 活字乱刷（`huozi_luanshua.py`） | 历史直播语料 + 你想拼的句子 | 可追溯的试听音频候选 | 从主播说过的话里拼出新句子，分三步（先选料、再核对出处、最后渲染），每步留痕可查，绝不自动上传 |
| 修复/救援（`scripts/README.md` 修复组） | 出问题的包或丢失的录制段 | 修好的包 / 重建的源文件 | 换源、修封面、从官方回放重建丢失录制、复活被误拒的候选——都要先出计划、过文件校验才动手 |
| 录制监控（`slice_monitor.py` 等） | 录制主机状态 | 报告文件（唯一告警通道） | 盯挂载、盯录制健康、备份弹幕，出事宁可停下也不吃坏字节 |

**公共仓库提供软件、通用契约、模板和测试，不提供运营账户、真实授权记录、私有审片材料或声纹。**
某些特定历史修复适配器仅保留拒绝执行的兼容占位；文件存在不表示相应私有快车道可用。
没有有效输入或授权，不能通过手写 `PASS`、放松质量门或直接调用上传脚本来补齐。

## 快速上手

这里的默认使用方式是**把整个仓库交给 AI agent**：让 agent 先读本 README 和
[AGENTS.md](AGENTS.md)，再按它的提问填写频道资料、准备凭据并执行检查。
人类不需要自己通读后面的复杂手工步骤，只需回答 agent 的频道问题，并在交付前
过目成品和授权决定。下面的命令是 agent 的执行参考，也方便维护者核对实际入口。

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

输入是 `profiles/lidousha/profile.json` 及其公开资产；输出是配置检查结果。
`--config-only` 不要求引用资产实际存在。本版默认示例返回 `status=READY`，同时报告
`voiceprint_status=ABSENT`，`missing_runtime_paths` 包括 `voiceprint_profile.v1.json`
与 `talk_quota_policy_authority.v1.json`：这些运行资产没有随默认示例完整提供。
**配置可读不等于已具备声纹或生产资格**；真实制作前须检查适用的运行依赖，不能用空文件或
复制他人的授权消除提示。测试套件使用合成数据和模型替身；运行测试前不要设置 `AUTOSLICE_PROFILE`。
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

## 你需要准备什么

| 组件 | 说明 |
|---|---|
| 一台服务器 | 推荐 8 核 / 32 GB / 500 GB+ 磁盘；资源更小也可以先让 agent 做配置检查 |
| [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) | 录播姬；`ops/recording/` 是参考配置 |
| LLM 通道 | 两条独立的腿，**都必须是多模态模型**：能看画面的 GPT 系列经自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI)，能听音频的 Gemini 系列使用 [Google Antigravity](https://antigravity.google/) 订阅 CLI（AGY）或 `GEMINI_API_KEY`；后两者是同一个模型的两种入口，按订阅→API 兜底 |
| 系统 CJK 字体 | Linux 上安装 `fonts-noto-cjk`；字幕烧录经 libass 使用系统字体，`preflight` 会检查 |
| [biliup](https://github.com/biliup/biliup) | 投稿 CLI；只产包评审时可以不装 |
| Python 3.11+，`ffmpeg` 6.1+ | 依赖和外部工具版本先让 agent 按配置检查确认 |

全部凭据（cookie 放哪、长什么样、怎么验证）见
[docs/credentials.md](docs/credentials.md)；不要把凭据提交进仓库。

## 第一次逛仓库，只需要看这几个文件

1. 本 README；
2. [profiles/README.md](profiles/README.md) —— 怎么配你的频道；
3. [scripts/README.md](scripts/README.md) —— CLI 按用途组织的地图；
4. 要深挖规则再看 [docs/pipeline/README.md](docs/pipeline/README.md)（给 agent/维护者的分步权威）；
5. [AGENTS.md](AGENTS.md) —— 给 AI 代理的完整操作约定与架构细节。

`assets/` 下的 JSON 是频道数据（词表、策略、台账模板），不是代码，不需要人类逐个阅读。
配新频道时由 agent 按 `assets/_template/` 建立骨架，先回答它提出的词表、人设、标题风格和
封面形象问题，之后再边用边补充。

## 为什么文件这么多（以及为什么你不用怕）

- `src/` 是多个可独立检查的模块；agent 会按入口和对应 step 找到需要的代码。
- `tests/` 是质量门的回归安全网；测试成功也不代表外部服务、真实音频或账号权限已就绪。
- `assets/` 是频道知识和运行模板；大多数文件由模板和校验器管理，不要求人类手填。
- 结论：**你需要读的只有上面那几个 README**；其余交给 agent 和测试。

## 术语表

| 词 | 含义 |
|---|---|
| CPA | 自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 统一 LLM 入口 |
| BCUT | 必剪开放转写接口（免费、词级毫秒时间轴的 ASR） |
| AGY | [Google Antigravity](https://antigravity.google/) 的 CLI；与 `GEMINI_API_KEY` 是同一个听音模型的两种入口 |
| bilive | 本项目部署层的约定名（`/opt/bilive` 目录、`ops/recording/` 服务名） |
| 出版登记 | 候选 ↔ B 站稿件的对应台账，是唯一上传授权 |
| 真值台账 | 已发布字幕修复的唯一合法记录（防止修复引入新错误） |

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
首次提取给 BCUT 的 MP3 会先解码计帧，拒绝空文件、坏流和非法格式；静音仍是合法输入。
能解码不等于覆盖完整源音轨，也不证明字幕正确；已验证的声文见证缓存仍按原合同复用。

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

## 原稿复用、成功缓存与失败接续

已审原稿的快车道复用合法底稿和未变化的制品；它不重新选片、不任意润色整片，
也不跳过实际烧录、成片审计和发布授权。公开版通用入口见
[脚本地图](scripts/README.md) 与 [打包步骤](docs/pipeline/80-package-delivery.md)。
私有的候选专属授权与回执不随仓分发，因此不能直接重放维护者的历史任务。

普通转写的内容寻址缓存可以复用**已成功且验证通过**的 BCUT 结果与完整 CPA 校对结果。
音频、完整上下文、模型配置、提示词或代码身份改变时不能盲目复用；坏缓存按未命中处理。
缓存命中不意味着下游零调用：代词修复、忠实度检查和其他门仍执行，
后置模型输出也可能不同。缓存证明的是阶段复用，不是最终字幕更准确或整条流水线同比加速。

后置代词专项会接收本次已有的选题、场次和弹幕语境，但这些材料不是逐字真值或性别证明。
每轮的提示词、回答和前后字幕另存为媒体同名的 `.pronoun-trace/` 私有诊断；它不参与缓存，
也不授予发布许可。记录中的模型身份是请求配置，不是对实际服务后端的证明。
这些文件可能含原文、用户名和私有语境，不应随 PR 或公开问题报告提交；详见 [安全政策](SECURITY.md)。


歌切还有独立的**失败阶段接续**：歌切实现指纹未变、等待期已到，且前次记录明确表明
全源证明因基础设施失败而未完成时，直接从全源证明继续，不再重复窄窗选歌。
这不是复用成功听证：全源音频与 LRC（带时间歌词）、本人演唱及成片检查仍须执行；
实现指纹变化或阶段证据不足时回到普通入口。等待期、配额、确定性弃选和授权条件不变，
详见 [歌切步骤](docs/pipeline/50-song-lane.md)。

## 路线图与已知限制

- **说话人分离（多人自动分轨）：待做，欢迎 PR。** 目前成品统一按主播处理；
  声纹需要单独配置，匿名说话人编号不能自动绑定为真实人物。
- **Docker 化与跨平台部署：待做，欢迎 PR。** 参考脚本要按自己的主机、目录、锁和
  服务配置审查后使用，首次声纹模型下载和外部登录也不在离线测试范围内。
- 示例 profile 的**片头媒体不随仓分发**：默认 profile 跑 talk 交付要
  `AUTOSLICE_BRANDING_INTRO=off`，或按 profile 的 manifest 自备媒体。
- VAD 在 `--ssh-host localhost` 下可以本地直跑，但使用的是 PATH 里的 `python3`；
  系统 python3 需要 `numpy`/`onnxruntime`，`preflight` 会探测。
- 测试套件以默认 profile 为基准：跑 `pytest` 时不要设置 `AUTOSLICE_PROFILE`。
- 出版登记、真值台账等运营状态在公开仓库只有空模板，属于每个部署自己的数据。
- 声纹不随仓分发（生物特征）；配置检查返回 `voiceprint_status=ABSENT` 时，
  **READY 不等于声纹已 enroll，也不等于具备生产资格**。
- **外部依赖不稳定。** BCUT 接口、模型网关、AGY 客户端和 B 站接口不受本仓控制；
  服务不可用、配额不足或不兼容响应会留下失败诊断，不能由测试通过推定在线可用。
- **研究不等于默认功能。** MOSS / MAI 的局部听证比较不改变本版 BCUT → CPA 主线，
  也不作为公开版已经启用的默认后端。离线实验结论必须限定输入、阶段、费用和质量回归范围。

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

## 致谢

感谢 [bcut-asr](https://github.com/SocialSisterYi/bcut-asr)、
[BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) 和
[biliup](https://github.com/biliup/biliup)。示例频道为
[李豆沙](https://space.bilibili.com/1703797642)。

## 参与

- 贡献流程与铁律：[CONTRIBUTING.md](CONTRIBUTING.md)（agent 写的 PR 完全欢迎，
  人对结果负责）
- 安全漏洞：走 [SECURITY.md](SECURITY.md) 的私密披露通道，不要开公开 issue
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- CI：每个 PR 自动跑全量测试套件（无凭据无网络，密闭守卫强制）

## 许可

[Apache-2.0](LICENSE)。再分发（含衍生品）须保留 `LICENSE` 与 [NOTICE](NOTICE)；
闭源修改允许。

**礼节性请求（非许可条款）**：若你把本项目用于商业服务或商业化内容，
请在至少一处公开材料（产品页/关于页/视频简介）注明使用了
bilibili-vtuber-clip 并附仓库链接——这是社区回馈的最低形式。

**责任边界**：用本软件产出并发布的内容由使用者自行负责（Apache-2.0 本就
不含担保）。切片内容相关的纠纷（授权、侵权、下架）发生在 B 站，请走
B 站平台的举报/申诉渠道——本仓库无权也无法处置站外内容。

## 友情链接

- [沙按钮](https://lu-91015.github.io/shadowlee.github.io/)
