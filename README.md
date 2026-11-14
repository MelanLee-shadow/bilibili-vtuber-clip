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

这一版是重写后的流水线（代码在 `src/autoslice/manual_lane/`，入口 `scripts/manual_lane.py`），
加上它实际调用的支撑模块和几个独立工具。早先版本里的整条自动审校链不再随仓库分发。

| 入口 | 输入 | 产出与边界 |
|---|---|---|
| `manual_lane.py discover` | 一天的录播 `.flv` / `.mp4`、弹幕 `.xml` 与频道配置 | 整场识别稿 → 候选片 → 按分数和当天名额排座，入选的建成切片任务 |
| `manual_lane.py run` | 一条切片任务 | 边界、外片查重、剪辑、字幕、说话人、烧录、标题封面、标签、成片包、复审；工具失败或输入缺失就停在那一步并写明原因 |
| `manual_lane.py auto` | 任务目录 | 无人值守的一轮：发现新的录像日，把没做完的片接着做；同一处连停三轮的片留给人看 |
| `manual_lane.py revise` / `redo` / `trim` / `import-subtitles` | 已有切片 | 修订、重做某一步、剪掉无关段落、导入人工定稿的字幕 |
| 活字乱刷（[`huozi_luanshua.py`](scripts/huozi_luanshua.py)） | 历史直播语料和你想拼的句子 | 从主播说过的话里选取语音片段，核对文字与说话人出处后拼成新句子 |
| 爬虫与知识图（`scripts/crawl_*.py`、[`build_knowledge_graph.py`](scripts/build_knowledge_graph.py)） | 公开来源 | 圈内新闻、名单、动态、时效词的候选条目；实体与关系逐条带出处，字幕用的名表是知识图的投影 |
| 人设工具（`apply_persona_review.py`、`promote_persona_projection.py`、`extract_persona_facts.py`） | 已审定的事实 | 人设事实台账及其投影 |
| 录制运维（`ops/recording/`） | 直播间 | 录制、归档与健康检查的参考部署 |

**公共仓库提供软件、通用契约、模板和测试，不提供运营账户、真实授权记录、私有审片材料或声纹。**

**不在这一版里：**

- **发布授权链**：出版登记（哪条片对应哪个已发布视频）、上传、同稿换源与改题的校验、发布前的包审计。
  这是一个频道自己的运营状态和安全链。仓库保留了流水线引用的接口名：出版登记为空，
  修订已发布视频会明确报“需要频道自己的出版授权”；第 10 步（上传）需要接入你自己的发布工具。
- **歌切**、**官方回放抢救**：尚未移入重写后的流水线。

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
git clone --depth 1 https://github.com/MelanLee-shadow/bilibili-vtuber-clip.git
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
.venv/bin/python scripts/manual_lane.py --help
.venv/bin/python -m pytest -q
```

输入是 `profiles/lidousha/profile.json` 及其公开模板资产；输出是配置检查结果。
这是配置检查的预期结果：**配置可读不等于已具备声纹或全部生产前提**。
测试套件使用合成数据和模型替身；运行测试前不要设置 `AUTOSLICE_PROFILE`。
测试成功不证明外部服务、真实音频质量或账号发布权限正常。CI 在 Linux 上跑 Python 3.11 和 3.12；
在 macOS 上有一条与系统文件读取有关的用例（`test_no_dead_imports`）会失败，与本项目的功能无关。

### 3. 配置自己的频道和服务

按 [profiles/README.md](profiles/README.md) 从 `profiles/_template/` 与
`assets/_template/` 建立自己的频道。先填写词表、人物设定、标题风格和封面身份描述；
不要把示例频道的资料或账号合集 ID 当成自己的配置。
详细问卷和李豆沙真例保留在模板中；知识与运行记录的区别、近期开发的资产落点见
[频道资产同步说明](docs/channel-assets.md)。

```bash
cp .env.example .env
# 编辑 .env：使用自己的目录、频道和服务凭据；不要提交此文件。
set -a
source .env
set +a
```

`.env` **不会自动加载**。参考部署另外读取 `$AUTOSLICE_BASE/cpa.env`；
手工运行时应显式导出环境变量。详细入口与凭据校验见
[docs/credentials.md](docs/credentials.md)。

换频道就是换 profile：流水线的提示词、规则数值和代码里不写任何频道的名字，主播是谁、怎么称呼观众、
怎么在画面里认出她，都从 `profiles/<id>/` 读。从零搭起来的完整步骤（媒体主机上要有什么、频道的三个文件、
录像放哪、第一条片）见 [profiles/SETUP.md](profiles/SETUP.md)。

### 4. 第一支真实切片

真实制作**有网络、会消耗模型额度并写入工作目录**，不属于离线验证。先准备自己有权处理的录播、
已配置的模型网关和复听服务，再按 [操作说明](docs/pipeline/manual-lane-supervision.md) 的命令走：
`discover`（或 `init`）建任务，`run` 逐步制作，`status` 看每一步的状态。
每条切片的 `PROCESS.md` 记录每步做了什么、模型怎么判、采用了什么。

没有合格候选或某一步的检查失败时，读取那一步写下的原因，而不是重复调用以抽到通过结果。
耗时受片长、编码硬件、服务响应和重试影响，没有固定完成时间。
示例 profile 的片头媒体不随仓分发；自己的频道应在 profile 中配置真实片头或关闭片头。

## 流水线与模型职责

```text
录播与弹幕 → 整场识别稿 → 候选与排座 → 边界、剪辑
          → 两份识别稿打底（可选整片识别）→ 三个代理各自独立写字幕、逐处取多数
          → 拿不准的句子逐级复听 → 弹幕、名表、人设、圈内指称作为证据
          → 说话人 → 烧录 → 标题与封面（只读最终字幕）→ 标签 → 成片包与成片检查 → 复审
```

**识别稿是底稿，模型按证据定字。** 唯一不放行的是“定不下且影响意思”的句子：
流水线照常做到成片包，只是不封存。没有出处、各来源连读音都不一致的名字，不自己推一个写上去。

模型标识、档位和各步的数值规则集中在 [`assets/manual_lane/lane_rules.json`](assets/manual_lane/lane_rules.json)，
以该文件和当前源码为准。这些是**本项目发送给网关的标识**，不保证每个部署都能访问同名模型。
CPA 是本项目使用的 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 接入层，
不是随仓提供的模型服务；涉及看图的步骤必须具备相应视觉能力。

## 实验与已知限制

- **外部依赖不稳定。** 识别接口、模型网关、复听服务和 B 站接口不受本仓控制；
  服务不可用、配额不足或不兼容响应会留下失败原因，不能由测试通过推定在线可用。
- **身份与多人场景有限。** 说话人靠声纹，声纹需要单独配置；不要把匿名说话人编号自动绑定为真实人物。
- **新频道还需要自己的素材。** 声纹（多人场分人用）、片头、表情包库、标题风格都是按频道准备的；没有的部分按
  [profiles/SETUP.md](profiles/SETUP.md) 关掉或留空，流水线会在真正需要它的那一步写明缺什么。
- **发布不在这一版里。** 第 10 步需要频道自己的发布工具和出版登记。
- **部署不是通用的一键安装器。** 参考脚本要按自己的主机、目录和服务配置审查后使用。

## 进一步阅读

初次配置读 [profiles/README.md](profiles/README.md)；修改具体步骤前先读
[流水线索引](docs/pipeline/README.md)，它指向该步的规则、代码和资产。
分步文档里提到、但上文“不在这一版里”列出的部件，在本仓库中没有对应代码。

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
