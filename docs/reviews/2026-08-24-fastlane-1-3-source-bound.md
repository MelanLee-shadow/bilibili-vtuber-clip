# 快车道 #1–#3 source-bound 审计（2026-08-24）

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
- `blocker`：旧公开稿、wrong literal 和缺失 Creator/section fresh readback 共同阻止本轮
  fastlane closure。最短下一步是 private exact correction → human final perceptual receipt
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
- `blocker`：current formal SRT/title 尚未闭合上述三项；deployed override 仅改
  `7.590–8.720` 的 `谢谢沧老师` → `先方とのお打合わせ`，与三项 ruling 无关；provider/upload
  仍 false。最终 burned package 只能作为 review surface。
- Ivan checklist：确认 0:14 的断句和分隔；确认 1:04 的 response 语义；确认 title/cover
  identity 不拆分小李与李豆沙。每项确认都必须进入 hash-bound human receipt。
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
- `blocker`：non-release speaker SRT 写 `青兰`，delivery 写 `星兰`；Ivan 必须决定 exact
  host-vs-video spans、被吞掉的 host words，并以 danmaku+crawler sources 绑定
  `恋死→星兰`。现有 cue-6 override 是 unrelated/narrow，不能关闭这些问题；upload false。
- `blocker`（downstream）：cover 为 `BLOCKED_AI_COVER_REQUIRED`，并有
  `FINAL_HOST_IDENTITY_MISMATCH`；因此当前无 upload eligibility。
- Ivan checklist：逐段标注 host voice 与 watched-video voice；确认吞字范围；确认 `恋死→星兰`
  的两条来源证据；确认 cover host identity。通过后仍须重新跑 package、commit、upload 和
  public/Creator/section readback gates。
- 已知本地 authority：
  `assets/lidousha/selected_final_review_recovery_authorities/auto_220021_561_670.v1.json`、
  `assets/lidousha/subtitle_text_overrides/auto_220021_561_670.text.v1.json`、
  `assets/lidousha/speaker_session_anchors/2026-07-09-220021.v1.json`。本次审计未固定可作为
  当前真相的远端 delivery path；不得由旧 BVID 或旧 registry 推断发布资格。

## Ivan review checklist

1. #1：确认 literal `主包给`、最终感知 receipt，以及是否进入 same-BV correction lane。
2. #2：逐项回答 0:14、1:04、title/cover identity 三个 exact decisions。
3. #3：确认 host/video 声音边界、吞字、`恋死→星兰` 来源绑定和 cover identity。
4. 所有回答都只产生 review authority；每个候选仍需独立 package audit、manifest/commit
   gates、授权上传和 public/Creator/section readback。
