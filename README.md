<p align="center">
  <img src="assets/lidousha/emote/hd/07_%E6%9D%8E%E8%B1%86%E6%B2%99_%E8%B4%A1%E4%B8%B8.png" width="300" alt="李豆沙表情包：贡丸">
  <br>
  欢迎关注侄女小李，<a href="https://space.bilibili.com/1703797642">关注李豆沙</a>谢谢喵
</p>

# bilibili-vtuber-clip — 直播录播切片流水线

[![CI](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

**把中文直播录播做成可以直接审看的切片包：选片、剪辑、字幕、说话人、烧录、标题封面、标签、成片检查和复审。**
每一步把“做了什么、依据是什么”写进切片目录；工具失败或输入缺失就停在那一步，不把“脚本跑完”当成“内容正确”。

> A Chinese-first clipping pipeline for Bilibili VTuber streams: from a day's recordings to reviewed clip packages.
> Publishing is a separate, channel-owned operation and is not part of this snapshot.

> [!IMPORTANT]
> 这是工程项目，不是双击即用的视频编辑器。建议把整个仓库交给你的 AI agent 配置：
> [AGENTS.md](AGENTS.md) 是它的操作入口。人负责提供真实的频道资料、素材授权和发布决定。

## 这一版是什么

这一版是重写后的流水线（代码在 `src/autoslice/manual_lane/`，入口 `scripts/manual_lane.py`），
加上它实际用到的支撑模块。早先版本里的整条自动审校链不再随仓库分发。

| 步骤 | 内容 |
|---|---|
| `discover` | 一天的录像 → 整场识别稿 → 候选片 → 排座入选 |
| 1 边界 / 0 外片查重 / 2 剪辑 | 模型定起止，查是否已有同内容切片，剪出片段 |
| 3 字幕 | 两份免费识别稿打底，可选整片识别与逐句复听；三个代理各自独立写一遍，逐处取多数；弹幕、名表、人设、圈内指称作为证据 |
| 4 说话人 | 声纹定人；看视频的片再读画面上自带的字幕 |
| 5 烧录 | 片头与字幕烧录 |
| 6 标题与封面 / 7 标签 | 只读最终字幕 |
| 8 成片包 / 9 复审 | 组包、成片检查，另一个模型读成稿做复审 |
| `auto` | 无人值守的一轮：发现新录像日，把没做完的片接着做 |

各步的规则在 [docs/pipeline/](docs/pipeline/README.md)，操作说明见
[docs/pipeline/manual-lane-supervision.md](docs/pipeline/manual-lane-supervision.md)。
文档里提到、但下面“不在这一版里”列出的部件，在本仓库中没有对应代码。

### 同仓库的其他工具

| 工具 | 作用 |
|---|---|
| [`scripts/huozi_luanshua.py`](scripts/huozi_luanshua.py) | 活字乱刷：从主播说过的话里选语音片段，核对文字与出处后拼成新句子（[操作指引](.agent/skills/huozi-luanshua/SKILL.md)） |
| `scripts/crawl_*.py` | 圈内新闻、名单、动态、时效词等爬虫，产出带出处的候选条目 |
| [`scripts/build_knowledge_graph.py`](scripts/build_knowledge_graph.py) | 频道知识图（实体与关系，逐条带出处）；字幕用的名表是它的投影 |
| `scripts/apply_persona_review.py`、`promote_persona_projection.py`、`extract_persona_facts.py` | 人设事实台账与投影 |
| [`scripts/suggest_upload_tags.py`](scripts/suggest_upload_tags.py) | 投稿标签建议 |
| `ops/recording/` | 录制、归档与健康检查的参考部署 |

### 不在这一版里

- **发布授权链**：出版登记（哪条片对应哪个已发布视频）、上传、同稿换源与改题的校验、发布前的包审计。
  这些是一个频道自己的运营状态和安全链。仓库里保留了流水线引用的接口名：出版登记为空，
  修订已发布视频会明确报“需要频道自己的出版授权”。第 10 步（上传）需要你接入自己的发布工具。
- **歌切**、**官方回放抢救**：尚未移入重写后的流水线。
- 运营账户、真实授权记录、审片材料和声纹。

## 快速开始：先离线验证

Python **3.11+**；媒体步骤需要 `ffmpeg`、`ffprobe` 和 CJK 字体（例如 `fonts-noto-cjk`）。参考部署面向 Linux。

```bash
git clone https://github.com/MelanLee-shadow/bilibili-vtuber-clip.git
cd bilibili-vtuber-clip
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
.venv/bin/python scripts/manual_lane.py --help
.venv/bin/python -m pytest -q
```

这些命令不制作也不上传视频。测试使用合成数据和模型替身：测试通过不证明外部服务、
真实音频质量或账号权限正常。运行测试前不要设置 `AUTOSLICE_PROFILE`。

## 配置自己的频道

按 [profiles/README.md](profiles/README.md) 从 `profiles/_template/` 和 `assets/_template/` 建立自己的频道：
词表、人设、标题风格、封面身份描述。示例频道（李豆沙）的资料保留为真例，不要当成自己的配置。
知识与运行记录的区别见 [频道资产说明](docs/channel-assets.md)，凭据入口见 [docs/credentials.md](docs/credentials.md)。

真实运行需要：一台做媒体处理的主机、一个兼容 OpenAI Responses 接口的模型网关（文本与看图）、
用于复听的音频模型，以及（可选）整片语音识别服务。没有这些时，流水线会在需要它们的那一步停下并写明原因。

## 许可

Apache-2.0，见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
