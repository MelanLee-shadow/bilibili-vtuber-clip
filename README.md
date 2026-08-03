# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

[![CI](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml/badge.svg)](https://github.com/MelanLee-shadow/bilibili-vtuber-clip/actions/workflows/ci.yml)

主播下播后，这套系统自己完成从录播到成品的全部工作：挑出值得切的片段、
生成并校对字幕、烧录、配 AI 封面和标题，最后把等待人工过目的成品包放到
交付目录。它为"发布错误不可接受"的场景设计：任何一步证据不齐就**拒绝交付**，
而不是硬着头皮发出去。

> An unattended VTuber-stream clipping pipeline for Bilibili (Chinese-first;
> docs are in Chinese). Fail-closed at every stage.

> [!IMPORTANT]
> **不推荐人类手动配置本项目——请直接把整个仓库交给你的 AI agent 去配置。**
> 本项目的配置面（profile 体系、资产模板、校验器、文档）就是按"由 agent
> 阅读并执行"设计的，[AGENTS.md](AGENTS.md) 是它的完整操作手册；人类只需要
> 回答 agent 提出的频道问题、最后过目成品。
>
> 本项目需要两个**多模态** LLM 才能完整工作：一个**能听音频**的模型
> （默认 `gemini-3.6-flash`：声学听写、字幕听音仲裁、歌词对轴——两种接入
> **二选一**：[Google Antigravity](https://antigravity.google/) 官方订阅的
> CLI（AGY），或 Gemini API key。**它们是同一个模型**，只是订阅面和 API 面
> 的区别；都配上则自动按 订阅→API 的次序做配额兜底），和一个**能看画面**
> 的模型（默认 `gpt-5.6-sol`：封面视觉裁判、构图/身份核验）。
> 纯文本模型跑不完整条产线。

## 功能一览

| 功能 | 输入 | 输出 | 它做了什么 |
|---|---|---|---|
| 自动切片主线（`session_autoslice.py`） | 录播姬录出的 `.flv/.mp4` + 弹幕 `.xml` | 交付目录里的成品包：视频、烧好字幕、封面、标题、审片材料 | 下播后自动挑选题（用 LLM 从观众视角找"值得切"的片段）→ 免费 ASR 出字幕 → 用弹幕/礼物记录和声学证据校对专名与误听 → 烧录 → AI 封面标题 → 等人工评审 |
| 单候选产线（`produce_slice_package.py`） | 一个 spec（指定录播文件和起止时间） | 同上的单个成品包 | 跳过自动选题，把你指定的片段走完整条产线 |
| 人工评审与授权上传（`audit_review_package.py` → `build_final_human_review.py` → `authorized_upload.py`） | 成品包 | B 站稿件（含合集、tag）+ 入库的上传凭证 | 机器先全面审计包的一致性，人确认后由唯一入口投稿；已发布的稿件只允许"同 BV 修复"（换源不换稿），杜绝重复投稿 |
| 词表 crawler（`crawl_timely_terms.py` 等三个） | 直播圈公开信息 | 更新后的 profile 词表资产 | 定时把时效热词、关联主播名册、话题实体图刷进你频道的词表，让字幕专名校对跟得上直播圈动态 |
| 活字乱刷（`huozi_luanshua.py`） | 历史直播语料 + 你想拼的句子 | 可追溯的试听音频候选 | 从主播说过的话里拼出新句子，分三步（先选料、再核对出处、最后渲染），每步留痕可查，绝不自动上传 |
| 修复/救援（`scripts/README.md` 修复组） | 出问题的包或丢失的录制段 | 修好的包 / 重建的源文件 | 换源、修封面、从官方回放重建丢失录制、复活被误拒的候选——都要先出计划、过文件校验才动手 |
| 录制监控（`slice_monitor.py` 等） | 录制主机状态 | 报告文件（唯一告警通道） | 盯挂载、盯录制健康、备份弹幕，出事宁可停下也不吃坏字节 |

## 快速上手

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # 填 CPA / Gemini（凭据清单见 docs/credentials.md）
set -a; source .env; set +a # .env 不会被自动加载，跑脚本前手动 source（或写进服务环境）
.venv/bin/python -m pytest -q       # 自检：全部测试应通过（无凭据无网络，密闭守卫强制）
.venv/bin/python scripts/preflight.py   # 体检：字体/ffmpeg/目录/凭据/VAD 一次查清
```

配自己的频道：**把这件事交给你的 AI agent**——本项目预期就由 agent 来配置，
[AGENTS.md](AGENTS.md) 是为它准备的完整操作手册（想自己动手就照
[profiles/README.md](profiles/README.md) 做）。先验证示例频道：

```bash
.venv/bin/python scripts/validate_channel_profile.py --profile lidousha --config-only
```

第一支切片（在录播文件所在的机器上直接跑）。媒体读取、切割与 VAD 时轴
证据全部走本地路径，纯中文谈话切片**不需要任何 SSH**；
只有用到 AGY 听音复核的场景（字幕混外文 token、歌切）会 `ssh localhost`，
届时机器要能免密 ssh 自己（`ssh-keygen -t ed25519` 后把公钥追加进
`~/.ssh/authorized_keys`；`scripts/preflight.py` 会检查这一项）：

```bash
AUTOSLICE_BASE=$PWD/.autoslice AUTOSLICE_BRANDING_INTRO=off \
  .venv/bin/python scripts/session_autoslice.py --smoke-segment /path/to/recording.flv
```

三个要点：`AUTOSLICE_BRANDING_INTRO=off` 是必须的——示例 profile 的片头政策是
强制的，但片头媒体文件不随仓分发，不关掉 talk 交付会被 fail-closed 拦下（配自己
频道时模板默认关闭片头，不需要这个变量）。冒烟**全程约 15–20 分钟且终端安静**
（转写约半分钟、选题约两分钟，剩下都在单候选的校对/烧录/封面）——进度看两处：
`$AUTOSLICE_BASE/logs/smoke_<候选id>.log`（产线细节日志，实时追加）与
`$AUTOSLICE_BASE/out/smoke/<候选id>/` 里按阶段冒出的产物文件。`--ssh-host`
是单候选产线 `produce_slice_package.py` 的参数，冒烟入口没有也不需要它
（媒体就在本机）。

整线无人值守（录制 → 下播自动切 → 评审 → 上传）：照
`ops/recording/README.md` 配录制层，`scripts/deploy_autoslice.sh` 是参考部署。

## 你需要准备什么

| 组件 | 说明 |
|---|---|
| 一台服务器 | 推荐 8 核 / 32 GB / 500 GB+ 磁盘（4 核 16 GB 可用但并行烧录吃紧） |
| [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) | 录播姬。`ops/recording/` 是参考配置 |
| LLM 通道 | 两条独立的腿，**都必须是多模态模型**：①**能看画面**的 GPT 系列（默认 `gpt-5.6-sol`，封面视觉裁判/构图与身份核验）经自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI)（`CPA_BASE_URL`/`CPA_API_KEY`）；②**能听音频**的 Gemini 系列（默认 `gemini-3.6-flash`，声学听写/听音仲裁/歌词对轴）——接入方式**二选一**：[Google Antigravity](https://antigravity.google/) 订阅的 CLI（AGY，`AGY_BIN`），或 `GEMINI_API_KEY` 直连官方 API（不经 CPA）。**同一个模型的两种入口**，配一个就够；都配则按 订阅→API 次序自动兜底。纯中文谈话切片的主链（BCUT ASR+CPA 校对）不碰这条腿，只有专名听音仲裁、外文/拉丁词面独立听写、歌词对轴会用——缺了这些环节明确拒绝，不会瞎猜 |
| 系统 CJK 字体 | Linux 上 `apt install fonts-noto-cjk`（字幕烧录经 libass 用系统字体，缺了整片烧成豆腐块；profile 自带字体只管封面标题字。preflight 会检查） |
| [biliup](https://github.com/biliup/biliup) | 投稿 CLI。只产包评审不上传可不装 |
| Python 3.11+，`ffmpeg` 6.1+ | 6.1 与 7.x 都在真实产线跑通过；转写用的免费必剪接口不需要 key |

全部凭据（cookie 放哪、长什么样、怎么验证）见 **[docs/credentials.md](docs/credentials.md)**。

## 第一次逛仓库，只需要看这几个文件

1. 本 README；
2. [profiles/README.md](profiles/README.md) —— 怎么配你的频道；
3. [scripts/README.md](scripts/README.md) —— 65 个脚本按用途分组，先看"你会真正用到的七个"；
4. 要深挖规则再看 [docs/pipeline/README.md](docs/pipeline/README.md)（给 agent/维护者的分步权威，技术密度高）；
5. [AGENTS.md](AGENTS.md) —— 给 AI 代理的完整操作约定与架构细节。

`assets/` 下的一大堆 JSON 是**频道数据**（词表、策略、台账模板），不是代码，
不需要读。配新频道 = 整套复制 `assets/_template/` 骨架，但**不是 22 个都要
填**：字体、标题和 tag 规则、选题标准、校对原则这些**通用配置默认全都给好**
（开箱即用，想改再改）；真正因频道而异的只有四样——词表、人设、标题风格、
封面形象描述——agent 会拿着模板里写好的问题清单**问你，答完替你写好**；先
写 3–5 条就能开跑，之后边用边攒；其余的 crawler 自动维护、台账随运行自己
长出来。

## 为什么文件这么多（以及为什么你不用怕）

- **src ~190 个模块**：反屎山架构的直接结果。同样的功能量要么是几个几千行的
  god-file，要么是 190 个单一职责、平均一两百行、可独立测试的小模块——本仓
  选后者，并用"债务棘轮"测试锁死模块行数只许降不许升。孤儿扫描证明零死件。
- **tests 1700+ 用例**：这是"发布错误不可接受"的实现方式——每道质量门至少
  配一个回归测试。摊到每个模块不到 10 个，并不密；它们也是任何人改代码时
  的安全网。
- **assets 22 个骨架**：管线消费 22 种频道知识，validator 逐一强制存在——
  但如上所述，绝大多数不需要你手填。
- 结论：**你需要读的只有上面那几个 README**；其余交给 agent 和测试。

## 术语表

| 词 | 含义 |
|---|---|
| CPA | 自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 统一 LLM 入口 |
| BCUT | 必剪开放转写接口（免费、词级毫秒时间轴的 ASR） |
| AGY | [Google Antigravity](https://antigravity.google/) 的 CLI——听音腿的**订阅接入**（与 `GEMINI_API_KEY` 是同一个模型的两种入口，二选一） |
| bilive | 本项目部署层的约定名（`/opt/bilive` 目录、`ops/recording/` 服务名） |
| 出版登记 | 候选 ↔ B 站稿件的对应台账，是唯一上传授权 |
| 真值台账 | 已发布字幕修复的唯一合法记录（防止修复引入新错误） |

## 路线图与已知限制

- **说话人分离（多人自动分轨）：待做，欢迎 PR。** 目前成品统一按主播处理
  （uniform-host）+ CAM++ 声纹确认；完整的多人分轨是明确的下一步。
- **Docker 化：待做，欢迎 PR。**
- Alpha：参考部署已无人值守运行数周。发布/声纹 lane 的资产路径已全部按
  profile 派生（出版登记/终审契约/授权目录各频道各自一份）；多频道支持的
  剩余边界是**持久证据词汇**保留示例频道拼写（见 AGENTS.md「词汇级兼容」，
  刻意为之，不影响功能）。
- 示例 profile 的**片头媒体不随仓分发**：默认 profile 跑 talk 交付要
  `AUTOSLICE_BRANDING_INTRO=off`（见快速上手），或按
  `assets/lidousha/intro/` 的 manifest 自备媒体。配自己频道时片头默认关闭。
- VAD 在 `--ssh-host localhost` 下本地直跑，但用的是 **PATH 里的 python3**
  （不是 `.venv`）：系统 python3 要装 `numpy`/`onnxruntime`
  （`pip install --user numpy onnxruntime`；preflight 会探测）。媒体在远端
  宿主时 VAD 经 ssh 在宿主上跑，宿主同理。
- 测试套件以默认 profile 为基准：跑 `pytest` 时不要设置 `AUTOSLICE_PROFILE`。
- 出版登记/真值台账等运营状态在本仓只有空模板——它们属于每个部署自己的数据。
- 声纹不随仓分发（生物特征）：validator 会在输出里给 `voiceprint_status`
  （模板/示例都是 UNCONFIGURED 占位）——**READY 不等于声纹已 enroll**，
  声纹栈/歌切人声证明要用时按 `install_voiceprints.py` 先 enroll。

## 致谢

- [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) —
  必剪转写接口研究，本仓免费 ASR 聚合层的基础。
- [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) — 录制层。
- [biliup](https://github.com/biliup/biliup) — 上传层。

## 参与

- 贡献流程与铁律：[CONTRIBUTING.md](CONTRIBUTING.md)（agent 写的 PR 完全欢迎，
  人对结果负责）
- 安全漏洞：走 [SECURITY.md](SECURITY.md) 的私密披露通道，不要开公开 issue
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- CI：每个 PR 自动跑全量测试套件（无凭据无网络，密闭守卫强制）

## 许可

[Apache-2.0](LICENSE)。再分发（含衍生品）须保留 `LICENSE` 与 `NOTICE`
（署名与来源随代码走）；闭源修改允许。

**礼节性请求（非许可条款）**：若你把本项目用于商业服务或商业化内容，
请在至少一处公开材料（产品页/关于页/视频简介）注明使用了
bilibili-vtuber-clip 并附仓库链接——这是社区回馈的最低形式。

**责任边界**：用本软件产出并发布的内容由使用者自行负责（Apache-2.0 本就
不含担保）。切片内容相关的纠纷（授权、侵权、下架）发生在 B 站，请走
B 站平台的举报/申诉渠道——本仓库无权也无法处置站外内容。

## 友情链接

- [沙按钮](https://lu-91015.github.io/shadowlee.github.io/)
