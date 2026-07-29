# Current handoff

Updated: 2026-07-29T00:56:00-04:00 by Codex root.

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)，历史经过留在 Git。

## 目标

不中断地收敛 2026-07-22、07-24、07-25、07-26 的待修/待发稿，并在当前任务完成后，
用最新流水线重做并同 BV 更新：

> 【李豆沙】小李被粉色小姐姐布下迷魂阵仍然逞强自己相对礼墨是0.6，突然想起来绝望大喊「我是侄女啊」

用户已明确授权：

- 修复稿可直接编辑并同 BV 上传；
- 当天新稿可权宜直接上传；
- 上述指定旧稿重做后可直接上传，无需再交用户审阅。

## 已完成

- 当前生产部署：
  `free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT`
  = `8ad621137baad56f144a483a7bd26e2b24a76481`
  （2026-07-29T04:50:32Z）。
- glossary 期望收益机械归一已上线：固定礼物名
  `粉丝灯牌/粉团灯牌 -> 粉丝团灯牌`；两个已注册专名互相冲突时仍交 CPA。
- `礼墨` 等高频专名仍按 glossary 高收益机械归一；CPA 是纯文字最终裁决者，
  AGY 只是唯一可读 audio/image 的高可信辅助，不得反客为主。
- 封面语义 gate 已上线：CPA 必须结合完整标题与 StoryContract 判断
  `cover_punch` 是否自包含；碎片化 punch 拒绝时回退完整 `cover_text`。
  `生豆角 / 熊猫头下播` 是已固化的反例。
- `1493` 的已审字幕基线曾把错句
  `我一会儿让我们先看了……` 重新覆盖回成品。聚焦 CPA 文字裁决以
  0.93 选择 `我以为我们先看了……`；基线、hash 与 regression gate 已在
  `8ad6211` 修正，相关测试 `116 passed`。
- glossary/封面/CPA authority 相关完整测试最近一次为 `2601 passed`；
  基线增量另有上述 `116 passed`。

## 进行中

所有任务都在 `free` 的隔离 recovery base 中，`upload_allowed=false`；产物验收后才进入
正式 authorized upload / same-BV repair。

- `1863`（7/22）：
  `/opt/bilive/autoslice/recovery/2026-07-22/auto_193450_1863_2056-single-r1`
  正在重跑。04:55Z 的 exact final review 再次遇到
  `FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE`；这是 provider transient，
  进程结束后必须自动再排队，不能当 terminal blocker。
- `1209`（7/24，生豆角封面、礼墨）：
  `/opt/bilive/autoslice/recovery/2026-07-24/auto_183122_1209_1410-single-r2`
  正在用最新流水线完整重跑。
- `1493`（7/25，沙豆李）：
  `/opt/bilive/autoslice/recovery/2026-07-25/auto_195000_1493_1579-single-r2`
  正在用 `8ad6211` 完整重跑；验收首句必须是
  `我以为我们先看了这个李豆沙的队伍`，并逐片检查 `沙豆李`。

## 阻塞

没有需要用户补充的外部 blocker。

- provider 暂态、旧 package 的 broad fingerprint 不自动唤醒、边界 reviewer 没有合法
  recommendation，都属于流水线/操作层应自行处理的问题，不能停下来等用户。
- `909` 不存在人耳真值；用户已授权填入合理真值。不得再要求 Ivan 重听。

## 下一步

1. 等 `1863 / 1209 / 1493` 产生最终 state 后，以最终 SRT、chat authority、
   regression audit、record/publish hash 和最终封面图逐项验收；不以 `review_ready`
   状态词代替验收。
2. `1863` provider 暂态自动重试；`1209` 验收 `礼墨` 与不再碎片化的生豆角封面；
   `1493` 验收 CPA 已钉住的首句与所有 `沙豆李`。
3. 为三个已发布稿重建 exact recovery review manifest、canonical package audit 和
   `delegated_root_agent / reviewed_by="Codex root"` 的真实最终复核证据，走
   `authorized_upload.py repair-*` 同 BV 更新并 fresh-live 验证。
4. `909`（7/25 `auto_192000_909_1014`）用最新流水线完整重跑，采用用户授权的合理
   `粉丝团灯牌` 真值；验收后作为新稿权宜发布。
5. 修复剩余碎片化封面 backlog；每张最终图都实际查看，不能只信 cover receipt。
6. 处理 7/26 两个 boundary failure：
   `auto_142942_1329_1380` 的故事在 cue 7 已落地却拖入不完整下一话题，
   `auto_155948_327_424` 的候选尾部拖到未回答的“为啥有点下头”。
   应修复“只会向后延长、不会剪掉已分离尾话题”的流水线缺口并重跑，而不是永久 block。
7. 完成当前批次后，从源 VOD `BV1qANw62EZG` 重做既有
   `BV1RPNR6dET9`（粉色小姐姐迷魂阵 / 0.6 / 我是侄女啊），保持现有公开标题，
   用最新流水线同 BV 上传并 fresh-live 验证。
8. 最后现场读取 7/22、7/24、7/25、7/26 的 state、发布 ledger 和公开页，给用户报告
   每日已完成、进行中、失败/淘汰与仍需处理的准确数量。

## 固化规则

- 用户只指出 1–2 个问题且未说明问题穷尽：默认整片重跑；指出 3 个及以上问题：
  修指定位置并修背后的通病，不必因该规则再次整片重跑。
- 大部分 glossary 专名允许按期望收益机械替换以节省 CPA；专名之间平等，
  两个已注册专名冲突时由 CPA 结合上下文裁决。
- CPA 是最终文字/语义裁决者；AGY 只提供 audio/image witness。除 AGY 外的模型不声称
  直接听过音频。
- 封面 punch 必须让未看过直播的人也知道人物、事件和冲突；不能把完整 hook 切成
  `生豆角 / 熊猫头下播` 之类无上下文碎片。
- 已发布稿只走 `scripts/authorized_upload.py repair-*`，禁止新建替代 BV、
  裸 API、legacy replace 或删除旧证据制造绿灯。
