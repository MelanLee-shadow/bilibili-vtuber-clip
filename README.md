<p align="center">
  <img src="assets/lidousha/emote/hd/07_%E6%9D%8E%E8%B1%86%E6%B2%99_%E8%B4%A1%E4%B8%B8.png" width="300" alt="李豆沙表情包：贡丸">
  <br>
  欢迎关注侄女小李，<a href="https://space.bilibili.com/1703797642">关注李豆沙</a>谢谢喵
</p>

# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

[![CI](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

录完一场直播，它自己找出值得切的片段，写字幕、起标题、做封面、烧录、打包，再投到 B 站。
示例频道 [李豆沙](https://space.bilibili.com/1703797642) 的谈话切片现在就是这条流水线每天自动做出来的。

> A Chinese-first VTuber clipping pipeline for Bilibili: from a stream recording to a published clip.

> [!IMPORTANT]
> **不推荐人类手动配置本项目——请直接把整个仓库交给你的 AI agent 去配置。**
> [AGENTS.md](AGENTS.md) 是它的操作入口：先检查环境，再照着模板问你频道信息，
> 由 agent 完成配置并跑通。你只需要提供频道资料、素材和是否发布的决定。

## 它做什么

代码在 `src/autoslice/manual_lane/`，入口 `scripts/manual_lane.py`。

| 入口 | 输入 | 做什么 |
|---|---|---|
| `manual_lane.py discover` | 一天的录播 `.flv` / `.mp4`、弹幕 `.xml`、频道配置 | 整场识别 → 找候选片 → 按分数和每天名额挑片，建成切片任务；频道账号在弹幕里发“标记”的地方必切 |
| `manual_lane.py run` | 一条切片任务 | 边界、查重、剪辑、字幕、说话人、烧录、标题封面、标签、成片包、复审、投稿 |
| `manual_lane.py auto` | 任务目录 | 定时跑的一轮：发现新录像，把没做完的片接着做，回收已上线片的中间文件 |
| `manual_lane.py revise` / `redo` / `trim` / `import-subtitles` | 已有切片 | 改片换源、重做某一步、剪掉无关段落、导入人工定稿的字幕 |
| 活字乱刷（[`huozi_luanshua.py`](scripts/huozi_luanshua.py)） | 历史直播和你想拼的句子 | 从主播说过的话里挑语音片段，拼成新句子 |
| 爬虫与知识图（`scripts/crawl_*.py`、[`build_knowledge_graph.py`](scripts/build_knowledge_graph.py)） | 公开网页 | 圈内新闻、成员名单、动态、新词，整理成带出处的人物关系图；字幕和标题都从这里查名字 |
| 人设工具（`apply_persona_review.py`、`promote_persona_projection.py`、`extract_persona_facts.py`） | 切片里认出的事实 | 主播的人设资料 |
| 录制运维（`ops/recording/`） | 直播间 | 录像、边录边传到网盘、磁盘健康检查 |

**投稿（第 10 步）** 用 [`scripts/lane_publish.py`](scripts/lane_publish.py)
（说明在 [`src/autoslice/lane_publish/__init__.py`](src/autoslice/lane_publish/__init__.py)）：
先核对封存的成片包，再投新稿，或给已发布的稿件换视频、换封面，并记进出版登记。
同一条片改过之后重做，会接着原来的稿件换源，不会再投一个新稿。需要：

- biliup 和 `biliup login` 生成的 cookie，路径写在运行目录（`AUTOSLICE_BASE`）的
  `lane_publish.json`：`{"biliup_bin": "...", "biliup_cookies": "..."}`，都放在仓库外；
- 频道 profile 里的合集设置 `season_routes`；
- 授权人和授权原话（`assets/manual_lane/lane_rules.json` 的 `upload.authorization`），会写进每份上传清单。

投稿由 `manual_lane.py run ... --upload` 触发。定时轮次的参考 systemd 配置在
[`config/lane-auto/`](config/lane-auto/)，默认只做到封存，看过再传。

**这一版还没有：** 只改标题的工具（线上标题在 B 站创作中心改）、歌切、官方回放抢救。

活字乱刷是单独运行的语音重组工具：尽量挑长的、连续的原话，少拼碎片；
改一两个字能让句子更顺时，会把原句和改句都生成出来让你试听。
用法见 [活字乱刷操作指引](.agent/skills/huozi-luanshua/SKILL.md)。

## 快速开始

### 1. 安装

Python **3.11+**（CI 跑 3.11 和 3.12），另需 `ffmpeg`、`ffprobe`；Linux 装上 CJK 字体，例如 `fonts-noto-cjk`。

```bash
git clone --depth 1 https://github.com/MelanLee-shadow/bilibili-vtuber-clip.git
cd bilibili-vtuber-clip
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt` 里有声纹和语音检测用的 `onnxruntime`、`torch`、`modelscope`，体积较大；CI 装的是 CPU 版 PyTorch。

### 2. 先跑一遍离线检查

```bash
.venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
.venv/bin/python scripts/manual_lane.py --help
.venv/bin/python -m pytest -q
```

这几条不联网、不做视频。测试用的是合成数据和假模型，跑测试前不要设置 `AUTOSLICE_PROFILE`。
在 macOS 上有一条和系统文件读取有关的用例会失败，加
`--deselect "tests/test_no_dead_imports.py::test_no_dead_imports[src.autoslice.external_duplicate_check]"` 跳过。

### 3. 配置自己的频道

从 `profiles/_template/` 和 `assets/_template/` 建自己的频道，步骤见 [profiles/README.md](profiles/README.md)：
词表、人设、标题风格、封面里主播长什么样。模板里附了李豆沙的真实例子可以对照。
知识和运行记录各放哪，见 [频道资产同步说明](docs/channel-assets.md)。

```bash
cp .env.example .env
# 编辑 .env：填自己的目录、频道和服务凭据；不要提交这个文件。
set -a
source .env
set +a
```

`.env` 不会自动加载；参考部署另外读 `$AUTOSLICE_BASE/cpa.env`。凭据清单见 [docs/credentials.md](docs/credentials.md)。

换频道就是换 profile：提示词、规则和代码里不写任何频道的名字，主播是谁、怎么称呼观众、
画面里怎么认出她，都从 `profiles/<id>/` 读。从零搭起来的完整步骤（媒体主机要装什么、频道的三个文件、
录像放哪、第一条片）见 [profiles/SETUP.md](profiles/SETUP.md)。

### 4. 做第一条片

这一步要联网、会用模型额度。准备好录播、模型网关和复听服务后，按
[操作说明](docs/pipeline/manual-lane-supervision.md) 走：`discover`（或 `init`）建任务，`run` 制作，`status` 看进度。
每条片的 `PROCESS.md` 写着每一步做了什么、模型怎么判的、最后用了什么；某一步停下时，原因也写在那里。
示例频道的片头不随仓分发，自己的频道在 profile 里配片头或关掉。

## 流水线

```text
录播与弹幕 → 整场识别 → 候选与挑片 → 边界、剪辑
          → 两份识别稿打底 → 三个代理各自写字幕、逐句取多数
          → 拿不准的句子逐级复听 → 弹幕、名字表、人设、圈内知识当证据
          → 说话人 → 烧录 → 标题与封面（只读最终字幕）→ 标签 → 成片包 → 复审 → 投稿
```

识别稿只是底稿，每个字由模型对照证据定。只有“定不下、又影响意思”的句子会让一条片停在封存前，等补到证据。

各步用哪个模型、阈值多少，都在 [`assets/manual_lane/lane_rules.json`](assets/manual_lane/lane_rules.json)。
模型经 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI)（本项目叫它 CPA）调用，需要自己部署；看图的步骤要用支持视觉的模型。

## 已知限制

- **依赖外部服务。** 识别接口、模型网关、复听服务和 B 站接口出问题时，流水线停在那一步并写明原因，恢复后下一轮接着做。
- **多人场要声纹。** 联动和看视频的片靠声纹分人，声纹按频道单独录。
- **新频道要准备自己的素材。** 声纹、片头、表情包、标题风格都按频道准备；没有的按
  [profiles/SETUP.md](profiles/SETUP.md) 关掉，流水线会在用到的那一步说明缺什么。
- **部署脚本是参考。** 按自己的主机、目录和服务改过再用。

## 进一步阅读

第一次配置读 [profiles/README.md](profiles/README.md)；改某一步之前读
[流水线索引](docs/pipeline/README.md)，它指向这一步的规则、代码和资产。
分步文档里提到、而上面“这一版还没有”列出的部件，本仓库没有代码。

贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)，agent 操作约定见 [AGENTS.md](AGENTS.md)，
安全问题按 [SECURITY.md](SECURITY.md) 私下报告，社区约定见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 致谢与许可

感谢 [bcut-asr](https://github.com/SocialSisterYi/bcut-asr)、
[BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) 和
[biliup](https://github.com/biliup/biliup)。示例频道为
[李豆沙](https://space.bilibili.com/1703797642)。

[Apache-2.0](LICENSE)。再分发请保留 `LICENSE` 与 [NOTICE](NOTICE)。
商业使用时注明本项目并附仓库链接是礼节性请求，不是额外许可条件。
素材和发布内容由使用者自己负责。

## 友情链接

- [沙按钮](https://lu-91015.github.io/shadowlee.github.io/)

## 本版配置与 GC 接口

本项目提供可复现的配置与验证流程：先跑离线检查，再接真实录像和外部服务。
运行时回收的统一入口是 `scripts/runtime_gc.py`，Linux service/timer 示例在 `config/runtime-gc/`；
旧的 `terminal_out_gc.py` 已被它取代。用之前读
[当前生命周期接口](docs/pipeline/10-source-recording.md#当前生命周期-gc-接口)。
