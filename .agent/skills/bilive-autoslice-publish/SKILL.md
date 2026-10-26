---
name: bilive-autoslice-publish
description: "操作、修复、审查或发布 autoslice 成品，以及准备人设批量审核与投影计划；按当前 pipeline 的对应步骤执行。"
---

# Bilive Autoslice Publish

本 skill 只提供**操作入口和顺序**。所有准入、证据 schema、状态机与漂移判据均以
[`docs/pipeline/`](../../../docs/pipeline/README.md) 对应 step 为唯一权威；不要从本文件、
memory、日期化报告或本地旧审片包推导规则。

## 1. 从 live authority 开始

先按相关 step 现场读取部署 commit、运行 state/record、最终 artifact hashes、录制源和
B 站 public/Creator/section 状态。本地 checkout、旧包、某个状态字段或命令返回 0 都不能
替代 live readback。

## 2. 只读当前任务涉及的 step

- 源录像：[10-source-recording.md](../../../docs/pipeline/10-source-recording.md)
- 选片/exact 状态：[20-selection.md](../../../docs/pipeline/20-selection.md)
- 边界：[30-boundary.md](../../../docs/pipeline/30-boundary.md)
- 字幕与语义修复：[40-subtitle-text.md](../../../docs/pipeline/40-subtitle-text.md)、
  [41-semantic-repair.md](../../../docs/pipeline/41-semantic-repair.md)
- 歌切：[50-song-lane.md](../../../docs/pipeline/50-song-lane.md)
- 标题/封面：[60-title.md](../../../docs/pipeline/60-title.md)、
  [70-cover.md](../../../docs/pipeline/70-cover.md)
- 打包/audit：[80-package-delivery.md](../../../docs/pipeline/80-package-delivery.md)
- 授权上传/同 BV 修复：[90-publish.md](../../../docs/pipeline/90-publish.md)

## 3. 打包与最终复核

按 [80-package-delivery.md](../../../docs/pipeline/80-package-delivery.md) 构建当前审片包并运行
canonical auditor。机械验收、具体异常局部复核与真实人工 receipt 的分流只按 80/90 step；
不得在 step 之外新增逐片完整观看前置，也不得伪造观看。

需要 exact same-BV repair 时，按 90 step 先选机械验收 `--mechanical` 或真实人工复核路线。
正常机械路线复用当前 package/audit，不新建全片观看任务；只有选择真实人工复核时才使用
`--prepare-evidence-template` 并由实际 reviewer 填写 observations。两种路线均通过原 builder
create-only 构建各自独立的 receipt，不互相冒充。具体输入、证据与绑定规则
只读 [90-publish.md](../../../docs/pipeline/90-publish.md) 和脚本当前 `--help`。无上传授权时
停在 `NO_UPLOAD`，仍可完成打包、审计与诊断。

## 4. 新投稿

用户明确授权后，只通过 `scripts/authorized_upload.py` 的 `make-manifest`、`verify`、
`upload`、必要时 `season-add` 操作。完整准入、配额、合集与公开验收顺序只读
[90-publish.md](../../../docs/pipeline/90-publish.md)；不得裸调 uploader 或 legacy 脚本。

## 5. 已发稿同 BV 修复

先确认当前修复代码已部署、最终包通过当前 80/90 step，再按第 3 节与 90 step 选择适用的
机械验收或真实人工复核 receipt；这里不另加完整观看前置。旧进行中事务按原封存 receipt
接续，不为切换验收类型重发。然后：

1. 用 `make-manifest --final-human-review <receipt>` 与 `verify` 冻结并重验当前包、receipt 和授权；
   `--final-human-review` 是兼容参数名，可承载 90 step 允许的独立机械 schema，不等于必须填写人工模板；
2. 运行 `repair-plan --dry-run`，确认后再 create-only 建立 plan/journal；
3. 用 `repair-status` 查看**本地** plan/journal 状态，再运行 `repair-run --dry-run`；
4. 只用 `repair-run` 顺序执行或幂等 resume；多稿不得并行修复；
5. journal 到达 `VERIFIED` 后，仍须运行
   `repair-verify-live --out <same-bv-repair-completed.json>` fresh readback；只有生成
   create-only completed sidecar 才完成公开闭环。

`repair-plan`、`repair-run`（包括 dry-run）与 `repair-verify-live` 的远端读取前都必须通过
当前 90 step 定义的 read-only login canary。`repair-status` 只重验本地闭包，不访问线上，
不能证明公开态。状态机、退出码、重试边界和 completed sidecar 契约只读
[90-publish.md](../../../docs/pipeline/90-publish.md)。

不得使用 `swap_video_p.py`、legacy replace、裸 API、手工 append/edit，也不得为修正另建
新 BV。

## 6. 安全

- 不打印 cookie、token、API key 或含密钥的完整命令。
- 不从历史文档冻结账号、合集 ID、部署 commit、运行状态或配额。
- 发布路径 fail closed；需要新权限、验证码或无法证明闭环时停止并报告 blocker。
- 封面事实与叙事的验收只读 [70-cover.md](../../../docs/pipeline/70-cover.md)。

## 7. 人设批量审核与投影计划

本节操作的输入、审核者、回复格式、原始消息绑定、状态机和投影准入，统一读
[41：终审字幕之后的人设事实审核](../../../docs/pipeline/41-semantic-repair.md#终审字幕之后的人设事实审核)。
在仓库根目录先查现有接口：

```bash
python3 scripts/extract_persona_facts.py --help
python3 scripts/apply_persona_review.py prepare --help
python3 scripts/apply_persona_review.py review --help
python3 scripts/promote_persona_projection.py --help
```

为【主播名】选定现有台账与终审输入。若尚无抽取回执，按
[抽取 CLI](../../../scripts/extract_persona_facts.py) 的 `--bundle` 格式准备输入，以
`--ledger`、`--profile-id`、`--output-dir` 和显式 `--extractor`、`--extractor-id`、
`--extractor-version` 生成独立审核目录；`--proposals-json` 用于重放保存的模拟提案。
输入字段及路径解析看该脚本，事实与纠错材料的取舍按
[41](../../../docs/pipeline/41-semantic-repair.md#终审字幕之后的人设事实审核)。
从工具返回目录取 `extraction-receipt.json`，不要把抽取摘要直接当作已接受事实。

以下为待填变量模板：`PERSONA_PROFILE` 是频道 id，`PERSONA_LEDGER` 是现有台账路径，
`PERSONA_RECEIPT` 是抽取回执，`PERSONA_BATCH` 是尚不存在的新批次目录，
`PERSONA_RECORDED_AT` 是本批 UTC 时间戳。在运行环境填好实际值后准备批次：

```bash
python3 scripts/apply_persona_review.py prepare \
  --ledger "$PERSONA_LEDGER" --profile-id "$PERSONA_PROFILE" \
  --extraction-receipt "$PERSONA_RECEIPT" --output-dir "$PERSONA_BATCH" \
  --recorded-at "$PERSONA_RECORDED_AT"
```

将生成的 `review-summary.md` 连同原编号交给 41 指定的审核者，保留同批 `batch.json`
与回执。取得对本批的原始回复后，按 41 核对消息来源并准备 authority JSON；
将其路径填入 `PERSONA_AUTHORITY`，原始 UTF-8 回复路径填入 `PERSONA_RAW_MESSAGE`，先预览：

```bash
python3 scripts/apply_persona_review.py review \
  --ledger "$PERSONA_LEDGER" --profile-id "$PERSONA_PROFILE" \
  --batch-dir "$PERSONA_BATCH" --authority "$PERSONA_AUTHORITY" \
  --raw-message "$PERSONA_RAW_MESSAGE"
```

核对预览与原话后，按 [41](../../../docs/pipeline/41-semantic-repair.md#终审字幕之后的人设事实审核)
在同一命令加 `--apply` 追加修订；有输入漂移则按该节重新准备批次。
投影是独立动作：把当前在途清单路径填入 `PERSONA_INFLIGHT_INVENTORY`，先看计划：

```bash
python3 scripts/promote_persona_projection.py \
  --profile-id "$PERSONA_PROFILE" --ledger "$PERSONA_LEDGER" \
  --inflight-inventory "$PERSONA_INFLIGHT_INVENTORY"
```

读回计划、当前/目标 SHA 与阻塞；实际提升按
[41](../../../docs/pipeline/41-semantic-repair.md#终审字幕之后的人设事实审核) 和其指向的
[80：资产写入先查绑定面](../../../docs/pipeline/80-package-delivery.md#资产写入先查绑定面) 执行。
