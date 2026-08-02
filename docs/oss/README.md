# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

主播下播后，这套系统自己完成从录播到成品的全部工作：挑出值得切的片段、
生成并校对字幕、烧录、配 AI 封面和标题，最后把等待人工过目的成品包放到
交付目录。它为"发布错误不可接受"的场景设计：任何一步证据不齐就**拒绝交付**，
而不是硬着头皮发出去。

> An unattended VTuber-stream clipping pipeline for Bilibili (Chinese-first;
> docs are in Chinese). Fail-closed at every stage.

## 功能一览

| 功能 | 输入 | 输出 | 它做了什么 |
|---|---|---|---|
| 自动切片主线（`session_autoslice.py`） | 录播姬录出的 `.flv/.mp4` + 弹幕 `.xml` | 交付目录里的成品包：视频、烧好字幕、封面、标题、审片材料 | 下播后自动挑选题（用 LLM 从观众视角找"值得切"的片段）→ 免费 ASR 出字幕 → 用弹幕/礼物记录和声学证据校对专名与误听 → 烧录 → AI 封面标题 → 等人工评审 |
| 单候选产线（`produce_slice_package.py`） | 一个 spec（指定录播文件和起止时间） | 同上的单个成品包 | 跳过自动选题，把你指定的片段走完整条产线 |
| 人工评审与授权上传（`audit_review_package.py` → `build_final_human_review.py` → `authorized_upload.py`） | 成品包 | B 站稿件（含合集、tag）+ 入库的上传凭证 | 机器先全面审计包的一致性，人确认后由唯一入口投稿；已发布的稿件只允许"同 BV 修复"（换源不换稿），杜绝重复投稿 |
| 词表 crawler（`crawl_timely_terms.py` 等三个） | 直播圈公开信息 | 更新后的 profile 词表资产 | 定时把时效热词、关联主播名册、话题实体图刷进你频道的词表，让字幕专名校对跟得上直播圈动态 |
| 活字乱刷（`huozi_luanshua.py`） | 历史直播语料 + 你想拼的句子 | 可追溯的试听音频候选 | 从主播说过的话里高置信拼出新句子，三段式（计划→验证→渲染）证据绑定，绝不自动上传 |
| 修复/救援（`scripts/README.md` 修复组） | 出问题的包或丢失的录制段 | 修好的包 / 重建的源文件 | 换源、修封面、从官方回放重建丢失录制、复活被误拒的候选——全部计划驱动、哈希绑定 |
| 录制监控（`slice_monitor.py` 等） | 录制主机状态 | 报告文件（唯一告警通道） | 盯挂载、盯录制健康、备份弹幕，出事宁可停下也不吃坏字节 |

## 快速上手

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填 CPA / Gemini（凭据清单见 docs/credentials.md）
.venv/bin/python -m pytest -q    # 冒烟：应全绿（不需要任何凭据和网络）
```

配自己的频道：**把这件事交给你的 AI agent**——本仓的预期配置者就是 agent，
[AGENTS.md](AGENTS.md) 是为它准备的完整 runbook（人类照
[profiles/README.md](profiles/README.md) 手配也行）。先验证示例频道：

```bash
.venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
```

第一支切片（在录播文件所在的机器上直接跑，**不需要 SSH**——
`--ssh-host localhost` 走本地直读，SSH 只在媒体在另一台机器时才用）：

```bash
AUTOSLICE_BASE=$PWD/.autoslice \
  .venv/bin/python scripts/session_autoslice.py --smoke-segment /path/to/recording.flv
```

整线无人值守（录制 → 下播自动切 → 评审 → 上传）：照
`ops/recording/README.md` 配录制层，`scripts/deploy_autoslice.sh` 是参考部署。

## 你需要准备什么

| 组件 | 说明 |
|---|---|
| 一台服务器 | 推荐 8 核 / 32 GB / 500 GB+ 磁盘（4 核 16 GB 可用但并行烧录吃紧） |
| [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) | 录播姬。`ops/recording/` 是参考配置 |
| LLM 通道 | 能访问 Gemini 系列与 GPT 系列。推荐自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 统一入口（两个环境变量搞定） |
| [Google Antigravity](https://antigravity.google/) | 官方公开工具，直接下载。本仓用它的 CLI 做声学听写：talk 基线转写不用它，talk 的专名声学仲裁和歌切 lane 用（缺它时相关证据路径拒绝而不是瞎猜） |
| [biliup](https://github.com/biliup/biliup) | 投稿 CLI。只产包评审不上传可不装 |
| Python 3.11+，`ffmpeg` 7+ | 转写用的免费必剪接口不需要 key |

全部凭据（cookie 放哪、长什么样、怎么验证）见 **[docs/credentials.md](docs/credentials.md)**。

## 第一次逛仓库，只需要看这几个文件

1. 本 README；
2. [profiles/README.md](profiles/README.md) —— 怎么配你的频道；
3. [scripts/README.md](scripts/README.md) —— 62 个脚本按用途分组，先看"你会真正用到的六个"；
4. 要深挖规则再看 [docs/pipeline/README.md](docs/pipeline/README.md)（给 agent/维护者的分步权威，技术密度高）；
5. [AGENTS.md](AGENTS.md) —— 给 AI 代理的完整操作约定与架构细节。

`assets/` 下的一大堆 JSON 是**频道数据**（词表、策略、台账模板），不是代码，
不需要读。配新频道 = 整套复制 `assets/_template/` 骨架，但**不是 22 个都要
填**：骨架分层（见其 README）——大部分默认值即可开跑，首批手填只有约 6 个
（词表/人设/标题风格/封面身份/选题度量/校对原则 + 字体），4 个由 crawler
自动代填，台账类是管线运行时自己长出来的。

## 为什么文件这么多（以及为什么你不用怕）

- **src ~190 个模块**：反屎山架构的直接结果。同样的功能量要么是几个几千行的
  god-file，要么是 190 个单一职责、平均一两百行、可独立测试的小模块——本仓
  选后者，并用"债务棘轮"测试锁死模块行数只许降不许升。孤儿扫描证明零死件。
- **tests 1700+ 用例**：这是"发布错误不可接受"的实现方式——每道 fail-closed
  门至少一枚回归钉子。摊到每个模块不到 10 个用例，并不密；它们也是任何人
  改代码时的安全网。
- **assets 22 个骨架**：管线消费 22 种频道知识，validator 逐一强制存在——
  但如上所述，绝大多数不需要你手填。
- 结论：**你需要读的只有上面那几个 README**；其余交给 agent 和测试。

## 术语表

| 词 | 含义 |
|---|---|
| CPA | 自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 统一 LLM 入口 |
| BCUT | 必剪开放转写接口（免费、词级毫秒时间轴的 ASR） |
| AGY | [Google Antigravity](https://antigravity.google/) 的 CLI，本仓用作声学听写引擎 |
| bilive | 本项目部署层的约定名（`/opt/bilive` 目录、`ops/recording/` 服务名） |
| 出版登记 | 候选 ↔ B 站稿件的对应台账，是唯一上传授权 |
| 真值台账 | 已发布字幕修复的唯一合法记录（防止修复引入新错误） |

## 路线图与已知限制

- **说话人分离（多人自动分轨）：待做，欢迎 PR。** 当前默认 uniform-host
  （成品统一按主播处理）+ CAM++ 声纹确认；完整 diarization 是明确的下一步。
- **Docker 化：待做，欢迎 PR。**
- Alpha：参考部署已无人值守运行数周，但多频道抽象仍在收敛——剩余的默认
  profile 耦合点全部列在 [docs/profile-coupling.md](docs/profile-coupling.md)；
  **发布/声纹 lane 目前默认 profile 专用**（C 组修复完成前，换频道可产包评审，
  公开发布还差这一步）。
- 测试套件以默认 profile 为基准：跑 `pytest` 时不要设置 `AUTOSLICE_PROFILE`。
- 出版登记/真值台账等运营状态在本仓只有空模板——它们属于每个部署自己的数据。
- 示例 profile 全量校验会因声纹文件缺失而 BLOCKED（生物特征不随仓分发，预期行为）。

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
