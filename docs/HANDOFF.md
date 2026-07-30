# Current handoff

Updated: 2026-07-30T05:24:20-04:00 by Codex root.

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。`review_ready`、旧 PID、旧日志和旧 handoff 都不是发布
完成证明；发布状态必须读取 Creator/public/section fresh-live 回读及同 BV repair receipt。

## 目标

保持 `free:/opt/bilive/autoslice` 正式 cron 不停，继续收敛 2026-07-25、07-26 及后续日期；
当前优先完成 `auto_195000_1493_1579` 的最新版整片重做、终验和同 BV 修复。

用户已授权：修复稿可直接同 BV 编辑上传；当天新稿可权宜直接上传；指定旧稿重做后可直接上传，
无需再次等待审阅。

## 已完成

- 正式生产部署：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` =
  `c66b5fac32b21e73169ebebbfc5a83b566de1272`（2026-07-30T08:45:57Z）；`DISABLED`
  不存在，cron 启用。2026-07-30T09:20:03Z 的正式 tick 已更新 07-25/26/29 state。
- `auto_193450_1863_2056`（07-22）已同 BV 上线：`BV1xgg462Env`，CID `40420508094`。
- `auto_183122_1209_1410`（07-24，礼墨/生豆角）已同 BV 上线：`BV1AD366DEd9`，
  CID `40432765911`。
- `auto_183122_607_723`（07-24，同事家开播）已同 BV 上线：`BV1Eo3L6zECt`，
  CID `40430537842`；两处专名均为“礼墨”。
- `auto_193129_850_940`（07-24）已同 BV 上线：`BV1ec3A6bEWF`，CID `40390888542`；
  用户指出的 0:13、0:59、1:23 均修正，AGY 全片见证 PASS。
- 粉色小姐姐稿已在 `BV1RPNR6dET9` 同 BV 重做，CID `40427325915`；误把伊索尔画成
  主人公的封面也已 cover-only 修复，公开图 SHA-256
  `edc7a534ac993ce6b9c8a4f1a67487deca889a3a840dd4a076edf994fb54ee61`。
- `auto_192000_909_1014` 已同 BV 修复并上线：`BV1s7326qEc9`，旧 CID `40406289029`，
  新 CID `40438664771`。最终视频/SRT/封面 SHA-256 分别为
  `76cfa5d4766e1b443e005299ff665233c88b291fc87e27fc3287de56435afe25`、
  `526d80a6d659342364bea563eebacb9b24253ebcf3b15650ba5816e0ddaec299`、
  `57c536b218674d9cc5c3b31866d8be7c01bf1cd6dd25d68774cb91b6928976ab`；公开 CDN
  封面回下载哈希一致。AGY 从 0 到 EOS 见证及 5 个精确点全部 PASS；“粉丝团灯牌”是 Ivan
  授权的期望值真值，不冒充唯一人耳声学真值。
- 已部署的系统修复包括：封面 hook 不得切断语义原子、AI 最终图 hash-bound 李豆沙主人公身份
  复核及一次重绘、CPA carryover 精确重放/消费、修复后 cover replay 证据刷新、AI 图像模型
  provenance 强绑定。CPA 是文字/语义最终裁决者；AGY 是唯一可读 audio/image 的高可信 witness。
- glossary 高收益机械归一仍有效，例如 `林墨 -> 礼墨`；两个已注册专名互相冲突时仍交 CPA。

## 进行中

- `auto_195000_1493_1579`（“沙豆李/发 1、发 0、改成 2”）正在隔离 base 整片重跑：
  `/opt/bilive/autoslice/recovery/2026-07-25/auto_195000_1493_1579-single-r1`。
- 2026-07-30T09:23:38Z 状态为 `processing`，唯一 pending talk 是该候选；runner PID
  `934685`，PID file 为 `<base>/runner.pid`，日志为
  `<base>/logs/runner-c66b5fa-20260730T092328Z.log`。
- v7 plan 已冻结：源 state SHA
  `051472e8d0d4a47375a9025cbc86141fc3b187aedc4ddeaa892acd01f2409ad3`，旧指纹
  `b4cdba0552aa00a40fec002496a159c1cccbe272047e89f08f3e2d1406c2e1f7`，新指纹
  `80250c71349429b09d5fef4651acd81abe4976955041e054e025f52f79559525`，publication asset
  `recovery_publication_authority_2026-07-25_1493.v1.json`，`upload_allowed=false`。
- 原公开身份为 `BV1zzgd6JEHe` / CID `40331906310`。生成完成后须整片验字幕、边界、标题与
  最终封面实图，再走 `authorized_upload.py repair-*` 同 BV 更新并 fresh-live 回读。

## 当前正式队列

- 07-25：状态 `review_ready_with_failures`；6/6 talk 均为
  `review_ready + CURRENT + COMPLIANT + AI_COVER_READY`，无 pending talk。歌切 2 个 failed
  （`song_192000_1321`、`song_195000_287`），4 个 blocked，无 pending song。
- 07-26：7 个 talk 中 6 个为 `review_ready + CURRENT + COMPLIANT + AI_COVER_READY`；
  `auto_142942_496_618` 因 `subtitle_authority / foreign_source_transcription` 被拒。4 个歌切
  blocked，无 pending。
- 07-29：4 个 talk 为 `review_ready + CURRENT + COMPLIANT + AI_COVER_READY`；
  `auto_225056_814_887` 因 `subtitle_authority / chat_authority_finalization` 被拒。

## 阻塞

没有需要 Ivan 补充的外部 blocker。

- 1493 首次建 base 时误复制生产 `repo/lidousha` 大媒体并触发 ENOSPC；失败目录已精确删除。
  之后改为仅复制部署组件、录像用单日期链接，并补入 hash-bound BCUT authority，v7 planner 已通过。
- 为恢复写入余量，已删除无引用且可重建的旧预览 235 个（2.36 GB），以及 07-18/19 旧
  `song_selector_full` 重试树中的 124 个 MP4（33.64 GB）；JSON、字幕、裁决记录、源录像和最终
  歌切物料均保留。当前磁盘约 31 GB 可用、93% 使用。
- provider 暂态、queued 未启动、subtitle/song authority 失败都属于流水线/操作层应自行修复的
  问题，不能停下来等用户。

## 下一步

1. 跟踪 1493 到 terminal state；若 provider transient 则在同一隔离 base 自动续跑。
2. 对 1493 做整片字幕/边界/标题、烧录字节、chat authority、package audit 和最终封面像素验收；
   通过后直接同 BV 修复 `BV1zzgd6JEHe` 并做 Creator/public/section fresh-live 验证。
3. 继续修复 07-26 的 `auto_142942_496_618`，再处理 07-25/26 歌切 authority blockers；
   正式 cron 与已就绪新稿并行推进，不因单个 recovery 停摆。

## 固化规则

- 用户只指出 1–2 个问题且未说明问题穷尽：默认整片重跑；指出 3 个及以上问题：修指定位置及
  背后通病，不因这条规则再次整片重跑。
- 大部分 glossary 专名允许按期望收益机械替换；专名之间平等，两个已注册专名冲突时由 CPA
  结合文字上下文裁决。
- 多人封面必须在最终出图后看实图；AI 路线还必须通过 hash-bound 主人公身份 gate。封面 punch
  必须让未看过直播的人也知道人物、事件和冲突，不能把完整 hook 切成无上下文碎片。
- 已发布稿只走 `scripts/authorized_upload.py repair-*`；封面单独修复只走
  `scripts/bili_cover_edit.py`。禁止新建替代 BV、裸 API、legacy replace 或删除旧证据制造绿灯。
