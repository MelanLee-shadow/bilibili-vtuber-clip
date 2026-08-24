# 快车道 #1–#3 source-bound 审计（2026-08-24）

> **当前权威覆盖本历史快照：** 本页是 8/24 早期 session 的只读 snapshot。下方
> `PENDING`、`Ivan checklist`、缺 receipt 或旧 deployed hash 只描述当时观察到的事实，
> 不再是当前行动的人工复审要求，也不能覆盖 Claude line 947 的批次授权。该 line 947
> 明确：本批 21 条（18 talk + 3 song）中，每片只要修完该片点名错误即可直接上传，
> 无 Ivan 二次看片/复审；#12/#13 的“其他小错自己识别/顺手修”也属于该片明示范围。
> 当前执行应读取 [`2026-08-19-ivan-review-batch-rulings.md`](2026-08-19-ivan-review-batch-rulings.md)
> 及 live pipeline authority。delegated Codex root 的 exact-byte/perceptual receipt 仅是
> 技术追溯和验收记录，不是 Ivan rereview。

本页固化本次对已部署 `5f90525520530c9d7246d09718d209024b1181f5` 的只读审计结果。
它不是当前永久状态、上传授权或 same-BV 修复授权；下一次动作前必须重新读取 live
state、registry、候选包和 Bilibili public/Creator/section surface。

## #1：2026-08-11 / `auto_173005_934_1166`

- `confirmed`：fresh public API 读到 `BV1os8q61Eya`、AID `117132650155234`、CID
  `41126267272`；这是旧 upload 的一次成功公开面。normal upload 禁止。fresh Creator/section
  listing 未确认，当前只有历史 sidecars，不能把 public API 读数扩展成完整公开闭环。
- `confirmed`：当前 delivery hashes 为 MP4
  `1bdb0e3278d291ef850299b9b7bde8011241c6ac5ad48faef4d274d69286de81`、SRT
  `1a668a899257407685c91629cc254918a594663af8b55cccb0932db16d0aa5d1`、cover
  `9a3636c47b6bfe808dfdd24cd9486788b6101e9260e1cdf8d98a3886942b980f`。
- `confirmed` authority：Ivan ruling 要求弹幕 literal `主包给`；shipped SRT 仍为
  `主播给`。已有 authority 禁止字幕 mutation/upload；没有 human receipt 或 same-BV receipt。
- `blocker`（历史 snapshot）：旧公开稿、wrong literal 和缺失 Creator/section fresh readback
  共同阻止当时 fastlane closure。最短下一步记录为 private exact correction → delegated-root
  final-byte/perceptual technical receipt
  → same-BV repair lane；之后仍须重新跑 package/commit/upload/public readback gates。
- 已知本地 authority：
  `assets/lidousha/candidate_source_fact_refresh_authorities/auto_173005_934_1166.v1.json`、
  `assets/lidousha/candidate_public_text_surface_authorities/auto_173005_934_1166.public-text-surface-authority.v1.json`、
  `assets/lidousha/candidate_manual_title_source_fact_successors/auto_173005_934_1166.v1.json`。
  已知远端 canonical record 例：
  `/opt/bilive/autoslice/out/2026-08-11/auto_173005_934_1166/replacement_recuts/auto_173005_934_1166.record.json`；本页不推断其当前 bytes 未漂移。

## #2：2026-08-13 / `auto_203011_328_389`

- `confirmed`：`/opt/bilive/autoslice/state/2026-08-13.json` 的 pick 本身为
  `status=candidate_rejected`、`rejected_status=failed`；独立的
  `held_current_talk_rerender_receipts` entry 为 `status=QUEUED_SELECTED_REPAIR`，其
  `publication_hold_binding.status=hold_pending_review`；publication registry entry 也为
  `status=hold_pending_review`。本次 authority 范围内没有 local BVID。
- `confirmed` Ivan exact decisions：0:14 为 `小豆老公；； 不是你老公`；1:04 是对
  `小豆好吵（` 的 response；title/cover 不得把小李与李豆沙拆开。
- `blocker`（历史 snapshot）：当时 formal SRT/title 尚未闭合上述三项；deployed override 仅改
  `7.590–8.720` 的 `谢谢沧老师` → `先方とのお打合わせ`，与三项 ruling 无关；provider/upload
  仍 false。最终 burned package 只能作为 review surface。
- 历史 checklist：确认 0:14 的断句和分隔；确认 1:04 的 response 语义；确认 title/cover
  identity 不拆分小李与李豆沙。当前应将这些已授权修复绑定到 delegated-root
  hash-bound exact-byte/perceptual technical receipt。
- 已知本地 authority：
  `assets/lidousha/selected_final_review_recovery_authorities/auto_203011_328_389.v1.json`、
  `assets/lidousha/subtitle_text_overrides/auto_203011_328_389.text.v1.json`、
  `assets/lidousha/publication_registry.v1.json`。本次审计未固定可作为当前真相的远端 delivery
  path；不得用历史 overnight sidecar 代替。

## #3：2026-08-13 / `auto_220021_561_670`

- `confirmed`：本次 authority 范围内无 local BVID evidence；current recut SRT SHA 为
  `dfb2d77f133100f32f76c4f94dd56dae55db6ab02afdbcdcc2ba5d67dccf5704`，non-release speaker
  SRT SHA 为 `cb4bcddf8aaecc8199ae1371ee5c85b3f9240c2dd6c617c9b92ad3c13d0b5118`；delivery 为
  `uniform_host`，且包含 watched-video English。record `artifact_hashes.subtitle_sha256`
  仍是 stale/different `sha256:0c3a8349585d8d9bfd476eb1652f3baafbffeee362c0855f67c1271f3675e1cb`，
  与 current recut 不匹配。
- `blocker`（历史 snapshot）：non-release speaker SRT 写 `青兰`，delivery 写 `星兰`；当时
  记录为需 Ivan 决定 exact host-vs-video spans、被吞掉的 host words，并以 danmaku+crawler
  sources 绑定 `恋死→星兰`。现有 cue-6 override 是 unrelated/narrow；这些记录不覆盖
  line947 快车道授权，当前技术闭环由 delegated-root exact-byte/perceptual receipt 记录。
- `blocker`（历史 downstream snapshot）：cover 为 `BLOCKED_AI_COVER_REQUIRED`，并有
  `FINAL_HOST_IDENTITY_MISMATCH`；因此当前无 upload eligibility。
- 历史 checklist：逐段标注 host voice 与 watched-video voice；确认吞字范围；确认 `恋死→星兰`
  的两条来源证据；确认 cover host identity。当前执行不新增 Ivan 节点，改由 line947 truth
  与 delegated-root technical receipt 闭合，再重新跑 package、commit、upload 和
  public/Creator/section readback gates。
- 已知本地 authority：
  `assets/lidousha/selected_final_review_recovery_authorities/auto_220021_561_670.v1.json`、
  `assets/lidousha/subtitle_text_overrides/auto_220021_561_670.text.v1.json`、
  `assets/lidousha/speaker_session_anchors/2026-07-09-220021.v1.json`。本次审计未固定可作为
  当前真相的远端 delivery path；不得由旧 BVID 或旧 registry 推断发布资格。

## 私有人审 surfaces（session evidence only；历史快照，不是当前人工待办）

以下绝对路径均位于 `/private/tmp`，是本次 session 的 review surface，不是真实持久 authority，
不构成上传授权；三个目录内的 `human-receipt.pending.json` 在当时均为 `PENDING`，所有
human judgement 当时为 `null/PENDING`；这些历史状态不得转化为当前要求 Ivan 复审。

- #1：`/private/tmp/fastlane-human-review-20260824/1-auto_173005_934_1166/`
  - `video.mp4` SHA-256 `1bdb0e3278d291ef850299b9b7bde8011241c6ac5ad48faef4d274d69286de81`
  - `shipped.srt` SHA-256 `1a668a899257407685c91629cc254918a594663af8b55cccb0932db16d0aa5d1`
  - `cover.png` SHA-256 `9a3636c47b6bfe808dfdd24cd9486788b6101e9260e1cdf8d98a3886942b980f`
  - `PROPOSED_NOT_FOR_APPLY.srt` SHA-256 `c5c861e5d4263653f7cd21ff07e2163d849d84cd4bca4c7b503fad9889682da9`；仅为 local proposal，不得当作修复或 apply 输入。
- #2：`/private/tmp/fastlane-human-review-20260824/2-auto_203011_328_389/`
  - `video.mp4` SHA-256 `6f6ddc92fddcca22d9b9818d5a0fae5a9bd61224712f1c0b7fa56ff5170039f6`
  - `package.srt` SHA-256 `0f2dfaba23ac61fe1392e2f96d98b8c3c93738fc709cf7d9940a4e86f9286862`
  - `cover.png` SHA-256 `ee887afa63b666dc36b68c7a90018c08743a464807445e418f4b3abe8beb1e1a`
  - 当时记录为 Ivan 需逐项裁决：0:14 完整 `小豆老公；； 不是你老公` 的分隔/单 cue；1:04 对
    `小豆好吵（` 的回应；title/cover 不把小李和李豆沙写成两人。
- #3：`/private/tmp/fastlane-human-review-20260824/3-auto_220021_561_670/`
  - `video.mp4` SHA-256 `46b8c838a97486ac399c9c415030d97cca8d1f1d42ae69546ceab0e9e8867a6e`
  - `current-recut.srt` SHA-256 `dfb2d77f133100f32f76c4f94dd56dae55db6ab02afdbcdcc2ba5d67dccf5704`
  - `non-release-speaker.srt` SHA-256 `cb4bcddf8aaecc8199ae1371ee5c85b3f9240c2dd6c617c9b92ad3c13d0b5118`
  - 两个字幕 surface 的 mismatch 与 record stale hash `sha256:0c3a8349585d8d9bfd476eb1652f3baafbffeee362c0855f67c1271f3675e1cb` 均保持记录；未生成修复 SRT。

这些私有文件只供当时的 root/Ivan 诊断和后续重新验证；当前由 delegated Codex root 的技术 receipt
绑定最终字节，receipt 通过也不会跳过 package、commit、
authorized upload、same-BV 或 public/Creator/section readback gates。

## Ivan review checklist（历史 snapshot，已被 line947 快车道授权 supersede）

1. #1：当时要求确认 literal `主包给`、最终感知 receipt，以及是否进入 same-BV correction lane。
2. #2：当时要求逐项回答 0:14、1:04、title/cover identity 三个 exact decisions。
3. #3：当时要求确认 host/video 声音边界、吞字、`恋死→星兰` 来源绑定和 cover identity。
4. 当时的观察要求只产生 review authority；每个候选仍需独立 package audit、manifest/commit
   gates、授权上传和 public/Creator/section readback。
