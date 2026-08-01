# bilibili-vtuber-clip — 无人值守的直播录播切片流水线

从直播录播到成品切片的全自动流水线：**选题 → 转写 → 证据链校对 → 字幕烧录 →
AI 封面与标题 → 出版登记与上传**。为"发布错误不可接受"的场景设计——每一道
门都是 fail-closed：证据不齐就拒绝交付，而不是硬着头皮发出去。

> An unattended VTuber-stream clipping pipeline (Chinese-first). Every stage
> is an evidence-chained, fail-closed gate: selection → ASR → adjudicated
> subtitle correction → burn → AI cover/title → publication registry →
> upload/repair lanes for Bilibili.

## 它与"随便切切"的区别

- **证据链而非黑箱**：每处字幕修正都要有出处（平台弹幕/礼物记录、独立
  声学听写、闭集裁决回执），并以哈希绑定进交付包。
- **出版登记是唯一上传授权**：已发布内容只能走同 BV 修复链（换源不换稿），
  防止重复投稿与误传。
- **精确重放基线**：修复重跑逐字节恢复已审文本，只有真值台账拥有的区间
  允许变化——修复不会引入新的回归。
- **调试期成本自控**：内容寻址缓存让重试轮零重复模型调用。

## 分步文档

[docs/pipeline/README.md](docs/pipeline/README.md) 是流水线规则的入口：
源录像 → 选片 → 边界 → 字幕/语义修复 → 歌切 → 标题/封面 → 打包 → 发布，
每步一个 step 文件，指向对应代码、schema 与资产。

## 你需要准备什么

| 组件 | 说明 |
|---|---|
| 录播服务器 | 推荐 8 vCPU / 32 GB RAM / 500 GB+ 磁盘（4c/16G 可用但并行烧录吃紧）。参考实机：AMD EPYC 8c/32G，103 秒切片全流程 6–17 分钟 |
| 录制器 | [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder)（`ops/recording/` 有适配器、健康巡检与 webhook 对账） |
| LLM 通道 | 需要能访问 **Gemini 系列**（转写精修/声学听写）与 **GPT 系列**（语义裁决/标题/封面）。推荐自建 [CLIProxyAPI](https://github.com/luispater/CLIProxyAPI) 作为统一入口（本仓所有调用走 `CPA_BASE_URL`/`CPA_API_KEY` 两个环境变量） |
| 免费 ASR | 词级时间轴来自必剪开放转写接口，基于 [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) 的社区研究（见 `scripts/free_asr_client.py`，另含剪映备胎） |
| Python | 3.11+（参考部署 3.13），`ffmpeg` 7+ |

环境变量见 `.env.example`。

## 快速开始（骨架）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # 填 CPA / Gemini 凭据
# 1. 配置你的频道 profile（profiles/_template/ → profiles/<your-channel>/）
# 2. 配置录制器 webhook（ops/recording/）
# 3. 跑一次单候选产线：
python3 scripts/produce_slice_package.py --spec <spec.json> --ssh-host localhost
```

`assets/lidousha/` 是一个**完整的实战 profile 示例**（词表、标题风格语料、
选题度量、字幕校对原则），来自真实频道的长期运营沉淀，作者选择公开以供
参考。换频道 = 换 profile + 换词表，管线代码不动。

## 状态与已知限制

- Alpha。参考部署已无人值守运行数周（含同 BV 字幕修复的公开验收闭环），
  但多频道抽象仍在收敛中。
- 出版登记/真值台账/评审契约等**运营状态**在本仓只有空模板——它们属于
  每个部署自己的数据。
- 测试套件的少量用例耦合参考 profile 的资产内容，换 profile 后需按模板
  重建；个别用例默认指向自建 CPA 端点，无凭据时请跳过该子集。
- Docker 化在路线图上（欢迎 PR）。

## 致谢

- [SocialSisterYi/bcut-asr](https://github.com/SocialSisterYi/bcut-asr) —
  必剪转写接口研究，本仓免费 ASR 聚合层的基础。
- [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) — 录制层。
- [biliup](https://github.com/biliup/biliup) — 上传层。

## 许可

自定义宽松许可（全文见 [`LICENSE`](LICENSE)）：个人与商业使用免费、
闭源修改允许；**再分发须署名原作者与来源**；**商业使用须在公开材料中
声明使用了本项目**。
