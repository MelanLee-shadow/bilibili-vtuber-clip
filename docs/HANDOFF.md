# Current handoff

Updated: 2026-07-30T06:18:00-04:00 by Codex root.

本文件只记录会影响下一次操作的 live 状态。流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。`review_ready`、旧 PID、旧日志和旧 handoff 都不是发布
完成证明；发布状态必须读取 Creator/public/section fresh-live 回读及同 BV repair receipt。

## 目标

保持 `free:/opt/bilive/autoslice` 正式 cron 不停，继续收敛 2026-07-25、07-26 及后续日期；
当前优先修复 07-26 唯一失败谈话 `auto_142942_496_618`，再处理 07-25/26 歌切 authority
blockers。

用户已授权：修复稿可直接同 BV 编辑上传；当天新稿可权宜直接上传；指定旧稿重做后可直接上传，
无需再次等待审阅。

## 已完成

- 正式生产部署：`free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT` =
  `926afbefeea2027db7e2c0ffa1d90e34d6b8e8f6`（2026-07-30T09:41:30Z）；`DISABLED`
  不存在，cron 启用。
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
  provenance 强绑定。音频输入由 AGY（以及 foreign-span 的直连 Gemini 有界后备）提供 witness，
  CPA 保持最终文字/语义裁决权；图像 witness 首选 CPA vision，AGY 只作 CPA 不可用时的后备，
  禁止再把“CPA 不接音频”外推成“CPA 不能看图”。
- glossary 高收益机械归一仍有效，例如 `林墨 -> 礼墨`；两个已注册专名互相冲突时仍交 CPA。
- `auto_195000_1493_1579` 已用最新版流水线完成整片重跑、CPA 终裁、AGY 0 到 EOS 见证、
  package audit 与最终封面实图验收，并在原 `BV1zzgd6JEHe` 同 BV 上线；当前 CID
  `40440957459`。最终视频/SRT/封面 SHA-256 分别为
  `e27460b2dab36ca64b7c44b522d523894cec3d4308c9c670c7d2eb692bed166b`、
  `ae3ae23ebef87c555cad6ebd8805d23f6e1ac512e2e514f74ffbac5ba1a03e7d`、
  `12ed8a5f6a898bb3ddaf328974ef7f088f330c28169cdb0933f46fb6c46758cf`。Creator/public/section
  fresh-live 三方一致，公开 CDN 封面回下载哈希与目标封面一致；五处均为“沙豆李”，投票顺序为
  “发一支持沙豆李 / 发零支持李豆沙 / 为什么要这样说 / 发二支持李豆沙”。

## 进行中

- 准备对 07-26 `auto_142942_496_618` 建立隔离 no-upload recovery，定位并修复
  `subtitle_authority / foreign_source_transcription`，正式 cron 保持启用。

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
- 1493 r1 已生成正确“沙豆李”整片字幕，但 exact-final CPA 又把旧 baseline 的
  “这边我最期待了”裁成“这边我最期待的”（0.88）；最终 baseline owner 门拒绝静默漂移。
  已把该 hash-bound CPA 结果晋升到持久 baseline/regression，388 个相关测试通过，部署
  `926afbe` 后以新候选指纹建立 r2。r1 保留为失败证据，不原地洗绿。
- 为恢复写入余量，已删除无引用且可重建的旧预览 235 个（2.36 GB），以及 07-18/19 旧
  `song_selector_full` 重试树中的 124 个 MP4（33.64 GB）；JSON、字幕、裁决记录、源录像和最终
  歌切物料均保留。当前磁盘约 31 GB 可用、93% 使用。
- provider 暂态、queued 未启动、subtitle/song authority 失败都属于流水线/操作层应自行修复的
  问题，不能停下来等用户。

## 下一步

1. 修复 07-26 的 `auto_142942_496_618`，确认 foreign-source authority 根因并在隔离 base 重跑、
   终验；若已有公开 BV 则同 BV 修复，否则按当天新稿授权发布。
2. 处理 07-25/26 歌切 authority blockers；
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
