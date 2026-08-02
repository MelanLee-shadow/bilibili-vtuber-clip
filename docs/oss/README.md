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
| LLM 通道 | 两条独立的腿：**GPT 系列经自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI)**（`CPA_BASE_URL`/`CPA_API_KEY` 两个变量），**Gemini 系列走 `GEMINI_API_KEY` 直连官方 API**（转写精修/声学听写），不经 CPA——CPA 上不需要配任何 Gemini 模型 |
| 系统 CJK 字体 | Linux 上 `apt install fonts-noto-cjk`（字幕烧录经 libass 用系统字体，缺了整片烧成豆腐块；profile 自带字体只管封面标题字。preflight 会检查） |
| [Google Antigravity](https://antigravity.google/) | 谷歌官方工具，直接下载。本项目用它的命令行做"听音复核"：纯中文谈话切片的字幕通常**用不到它**；字幕专名的听音仲裁、字幕混入外文/拉丁词面时的独立听写、歌词对轴会用到——缺它时这些环节明确拒绝，不会瞎猜 |
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
| AGY | [Google Antigravity](https://antigravity.google/) 的 CLI，本仓用作声学听写引擎 |
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

## 许可

[Apache-2.0](LICENSE)。再分发（含衍生品）须保留 `LICENSE` 与 `NOTICE`
（署名与来源随代码走）；闭源修改允许。

**礼节性请求（非许可条款）**：若你把本项目用于商业服务或商业化内容，
请在至少一处公开材料（产品页/关于页/视频简介）注明使用了
bilibili-vtuber-clip 并附仓库链接——这是社区回馈的最低形式。
