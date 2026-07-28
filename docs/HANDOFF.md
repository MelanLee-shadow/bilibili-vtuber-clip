# Current handoff

Updated: 2026-07-28T04:18:13Z（北京 2026-07-28 12:18:13）by Codex root。

本文件只记录仍影响下一次操作的 live 状态。历史经过留在 Git；流水线规则只读
[`docs/pipeline/`](pipeline/README.md)。

## 目标

在**不上传、不改线上 BV**的前提下，接管并收敛 7/24–7/25 事故尾项：

- 让 `850`、`1209` 的当前最终包通过远端与本机可移植 canonical audit；
- 把 `909` 从错误的“记分卡丢失”说法收敛到真实字幕 authority blocker；
- 持久化 `1573` 已完成的 same-BV fresh-live 验收证据；
- 修正旧 handoff 中的 legacy replace、直播/CloudFS、等待态等错误操作指引。

## 已完成

### 生产代码与部署

当前生产：

- `free:/opt/bilive/autoslice/repo/DEPLOYED_COMMIT`
  = `0fb9c995f31f92953c5ccba7a837cc6d06d01d5c`
- deploy guard 不存在，`/opt/bilive/autoslice/DISABLED` 不存在；
- runner MD5：`369685360528d8c076f21cdba1c74428`。

本轮承重提交：

- `a7e751b`：从原始 candidate spec 恢复被遗漏的 selection scorecard；`909`
  的记分卡**没有永久丢失**。
- `8bcc1ca`、`87b9e7f`、`f9a1352`、`f4a2af3`：CloudFS/源切片恢复、exact
  source cut 缓存复用、chat/requeue 中途掉挂保护。
- `d7eef40`：重复同一 canonical entity 的 structured-chat slot 独立裁定；只在
  文本 canonical/count/slots 都一致时放行，否则
  `REPEATED_CHAT_ENTITY_SLOTS_UNRESOLVED` fail closed。
- `4dba80b`：daily manifest 不再保留旧 package 内 chat authority。
- `b964eb9`：daily manifest 只接受 state + record 共同绑定的 exact final cover，
  不再固定优先旧 `ai-title.cover.png`。
- `a8506ff`：把 record-bound `clip-context.json` 装进 portable package，堵住远端
  auditor 偷读包外 candidate-root 文件的假绿。
- `0fb9c99`：publication registry 与 gate 文案统一指向
  `authorized_upload.py repair-*`；不再把 published candidate 指向模糊的
  “edit-replace”。

全量测试：`2490 passed in 40.55s`。registry targeted：`7 passed`。

### `850` 与 `1209` 当前包

两案 state 都是 `review_ready / rc=0`，但本轮没有把状态词当放行证据；已重建
`review_manifest.json`、持久化当前 `package_audit.json`，并把整个包复制到本机，
用与生产一致的 Pillow/FreeType/RAQM/HarfBuzz runtime 再跑 canonical auditor。

共同审计闭包：

- schema：`lidousha-review-package-audit.v2`
- policy epoch：`2026-07-23.final-artifact-gates.v3`
- policy fingerprint：
  `sha256:8edc12a95b7daae8196ddfe7c673d0f331b2bba1453ae70f422942560f66fe18`
- auditor source：
  `sha256:f21e78fe1e7337138624ba0cb6ac809df9adbc03b97b6129bbd3b093cba6a96d`
- 远端与本机结果：两案均 `passed=true / blocking_issue_count=0 / issue_count=0`

`850`：

- candidate：`auto_193129_850_940`
- video：
  `sha256:9f3344967052eb16a1dcb1d050081b16abef039dca9a42dd9a11ce0124ca091d`
- final cover：
  `sha256:d3584be1a2c8a546bcecca6f69cca52ccac3e3b0260b6ff9c74fa8eaae4d2edb`
- review manifest：
  `d4656f94e893ed2b18f36d80c7908ca4453767c31bdc4f9ede4c83210b0bf0f2`
- package audit：
  `a5c3269a98ac60edfd004aac3abd7a26d27e1d707a2d5da4cd2ec696994c3344`
- registry：`published → BV1ec3A6bEWF`，禁止新投稿。

`1209`：

- candidate：`auto_183122_1209_1410`
- video：
  `sha256:9c96a827cb3e0b9cad4f6fc6eaebfafa7561f7f68c1ec4472360a929ba9b6405`
- final screenshot-polish cover：
  `sha256:04557c3889b19078848b0116dbc7e93ea2e4cb3609e4cb2332b5a1c1c61e0742`
- review manifest：
  `1d8a8bc1a340847c2372432e0de084713c4c98ea94b3fe20584d01d5a3ee0e8c`
- package audit：
  `919b5c1226eda23bba08aa6a3a1151c3fb3dbdfe7a61c1f7495b100cd9a79916`
- registry：`hold_pending_review`，BVID 为空。

两张最终封面已人工查看：标题清楚、未遮挡关键身份特征、无模型伪字/吐舌；这只完成
封面视觉 QA，**不代替最终烧录视频的人类完整观看 receipt**。

本机封面像素复验须使用带 RAQM 的
`/opt/homebrew/bin/python3.14`（Pillow 12.2.0、FreeType 2.14.3、RAQM 0.10.3、
HarfBuzz 13.2.1、FriBidi 1.0.16）。普通 `.venv` macOS Pillow wheel 没有 RAQM，
会在最右 glyph 产生 1px 假差异，不能作为该像素证据的合格 verifier runtime。

### `1573` same-BV 修复已闭环

- BVID：`BV1DAg46HEXE`
- 新 CID：`40356020489`
- journal seq 7：`VERIFIED`
- row hash：
  `75de8725a6b6c3d030bf0ae1e15a52117137072e8ce2a1cddd734e18aba4ae68`
- fresh-live completed：
  `same-bv-repair-completed.v1 / VERIFIED_FRESH_LIVE / rc=0`
- completed SHA-256：
  `bbb0b439c8881b5dbd09ea8139a16cd94b44c898b4b5ddc5cc9e9167371e6b43`
- 验证时间：`2026-07-27T23:16:32+0000`
- completed sidecar 写入本身 `remote_mutation=false`。

本地持久证据（与本 handoff 一起 commit）：

- `reports/authorized_uploads/2026-07-22-per-bv-repair/1573.same-bv-repair-plan.json`
- `reports/authorized_uploads/2026-07-22-per-bv-repair/1573.same-bv-repair-journal.jsonl`
- `reports/authorized_uploads/2026-07-22-per-bv-repair/1573.same-bv-repair-completed.json`

## 进行中

没有本任务启动的后台修复进程。

2026-07-28T01:31Z live 快照：

- recorder adapter 约 50 秒新鲜，`service_reachable=true`；
- `streaming=false / recording=false / finalizing=false`，当前**不在直播**；
- CloudDrive 是 `findmnt` 实证的 `fuse / CloudFS`；
- `bilive_record`、`bililive_adapter`、`bililive_recorder` 均 running，
  restart count 0；
- 7/24 batch：`review_ready_with_failures / upload_allowed=false`；
- 7/25 batch：`review_ready_with_failures / upload_allowed=false`。

## 阻塞

### `909`：真实 blocker 是两个重复 `kmx` slot 未获得可投影 authority

- candidate：`auto_192000_909_1014`
- current state：`candidate_rejected / rc=1`
- `failure_kind=subtitle_authority`
- `failure_stage=chat_authority_finalization`
- `failure_recoverable=false`
- exact structured-chat event：`17790700`
- SC 文本：
  `姐姐姐姐姐还是在直播间摸摸kmx吧，kmx不咬人还喜欢被敲（在公司说怪话好刺激）`
- structured chat 有两个 `kmx`；当前 safe semantic text 中 canonical occurrence 为 0。

BCUT 在两处对应声槽写成重复“提问什么”；AGY draft 写成两处 `kmx`；本地
faster-whisper large/medium/small 也分别听到两处重复声槽，但不能单独确定 canonical
拼写与 cue 投影。单次 entity audio verifier 又返回
`ENTITY_AUDIO_PROVIDER_FAILED / AGY_QUOTA_EXHAUSTED`。因此不能整句抄 SC，也不能凭
结构化弹幕自动把两个 `kmx` 填进任意 cue。

Ivan 已再次确认：此前人耳也听不出这两个槽位，**不存在可由 Ivan 逐 cue 补出的人工真值**。
因此不得再把“请 Ivan 重听/定版”列为下一步，也不得把上下文猜测写成 source truth。

继续条件只剩：以后出现能够精确绑定这两个槽位的独立文字权威，或代码形成并通过新的 typed
结构证据合同，独立证明两处声槽分别对应结构化 SC 的两个 `kmx` mention。当前两者都不存在，
所以保持 terminal rejection；不要因 `d7eef40` 已部署就自动 revive——该提交只修正分类，
不能创造缺失 authority。

### `850`：机器包已绿，发布门未开

仍缺：

- 对当前 burned-final 视频的真实最终感知复核；
- hash-bound `lidousha-final-human-review.v2` receipt；
- Ivan 对 `BV1ec3A6bEWF` 的本次 same-BV 修复明确授权。

### `1209`：机器包已绿，但 registry hold

`hold_pending_review` 未解除；Ivan 放行前不构建上传授权、不投稿。

### `1571`：不是 provider 等待，已 terminal fail-closed

- candidate：`auto_190124_1571_1804`
- current state：`failed / rc=1`
- `failure_kind=failure_stage=final_review_contract`
- `failure_recoverable=false`
- message：
  `FINAL_REVIEW_RELEASE_BLOCKED: FINAL_REVIEW_CORRECTION_MUTATION_AUTHORITY_INVALID`
- chat authority：
  `/opt/bilive/autoslice/out/2026-07-24/auto_190124_1571_1804/auto_190124_1571_1804.chat-authority.json`

不要再描述成“等 provider 自动重试”；下一步是只读检查 correction mutation audit 的具体
invalid 字段，再决定修代码还是修 authority。

## 下一步

1. `850`：在当前 package + manifest + canonical audit 冻结不变的前提下，用
   `build_lidousha_final_human_review.py --prepare-evidence-template` 建模板；实际 reviewer
   完整观看并填写 observations，builder create-only 生成 receipt。只有拿到 Ivan 对本次
   same-BV 修复的明确授权后，才依次运行：
   `make-manifest --final-human-review` → `verify` →
   `repair-plan --dry-run` → create-only plan/journal →
   `repair-status` → `repair-run --dry-run` → `repair-run` →
   `repair-verify-live`。
2. `1209`：把当前最终视频/字幕/封面交 Ivan 审片；hold 未解除前停在本地/远端审片包。
3. `909`：不再请求 Ivan 人耳裁定；没有新的独立 exact-slot authority 时保持 terminal
   rejection，不复活、不发布。
4. `1571`：按上面的 terminal message 诊断 correction mutation authority，不做无界 revive。
5. `1573`：证据已经闭环，无需再跑 repair；以后若问当前公开态，应重新 fresh readback，
   不把 23:16Z receipt 当永久在线状态。

## 红线

- 本任务**没有上传授权**；没有上传 `909/1209`，也没有改 `850` 的线上 BV。
- `review_ready`、`passed=true`、本地 commit、deploy 成功都不能替代发布授权与公开验收。
- 禁止 `scripts/bili_archive_tool.py replace`、`swap_video_p.py`、裸 bili API、手工
  append/edit；same-BV 只走 `scripts/authorized_upload.py repair-*`。
- 不删除“陈旧 sidecar”来制造绿灯；daily builder 现在按 record hash 同步 chat/context，
  并按 state + record exact binding 选择最终封面。
- CloudFS 目录能 `ls` 不等于健康；始终读 `findmnt` 的 TARGET/FSTYPE/SOURCE、adapter
  fresh status 与容器内 FUSE。
