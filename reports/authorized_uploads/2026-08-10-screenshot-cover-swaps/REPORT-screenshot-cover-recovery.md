# 已发布切片封面「AI 重绘 → 截图」补做：第一步重算 + 第二步 staging 产出

日期：2026-08-10 ｜ 部署面：free `DEPLOYED_COMMIT 7f0af36`（= `260885f` 截图优先六项之子）
纪律：**未改 state、未 edit 线上、未上传、未部署**。所有写入只落在
`/opt/bilive/autoslice/recovery/2026-08-10/<cid>-screenshot-cover/` 与 `_step1_route_replay/`。

---

## 第一步：新代码下的路由重算（6 条，只读）

### 场景判定（P2 前置）
`resolve_cover_scene_kind` 要求两条同时成立才算 game：①会话 `session-game-context.v1` = RESOLVED；
②本条选帧有 `camera_window_bbox_frac`。

| 会话 | session-game-context | 结论 |
|---|---|---|
| 2026-08-07 | **RESOLVED**（鹅鸭杀，17 探测面/90 命中） | 有小窗者 = **game** |
| 2026-08-08 | **NO_MATCH** | 全部 **talk**（fail-closed） |

### 重算表

| # | 候选 | BV | 场景 | 旧 selected → 旧 actual | **新代码路由** | 判据 |
|---|---|---|---|---|---|---|
| 1 | `auto_213135_469_710` | BV18Gu16NEcX | talk | screenshot_direct → cpa_redraw（降级） | **screenshot_direct**（CPA 身份裁切） | P1：授权式收窄为 `face ∧ dominant`，两者皆 True → 裁切授权成立（旧代码被 `reaction=False`/`redraw_rec=True` 后门拦回）。score 4.4571+emotion → 直出。crop_box [344,101,1499,750]/1920×1080，zoom 1.6623 |
| 2 | `auto_230125_960_1072` | BV1Bau16nEyq | talk | screenshot_polish → cpa_redraw（降级） | **screenshot_polish**（裁切授权成立，但见下缺陷） | P1 同上。score 3.4553 ≥2.6 → 修图档。crop_box [0,1415,1920,2495]／**源帧 1920×3414 竖版 9:16** |
| 3 | `auto_220747_488_680` | BV1hfuS6EENb | **game** | cpa_redraw → cpa_redraw | **screenshot_polish**（全幅不裁） | P2+P4：重问游戏问卷得 `host_window_visible=True ∧ frame_is_interesting=True` → 不否决；游戏场 `supports_subject` 恒 False → 首次落到 7/25 小窗分支。物化 `HASH_BOUND_GAME_SCENE_FULL_FRAME` |
| 4 | `auto_203735_555_680` | BV1houS6SEF3 | **game** | cpa_redraw → cpa_redraw | **cpa_redraw（不变）** | 重问后仍否决：`frame_is_interesting=False`——「游戏主画面被大面积红/橙渐变光效过渡遮挡，缺乏具体战况」。反垃圾门据实拦下 |
| 5 | `auto_200736_298_383` | BV1JLuj6zEdM | **game** | cpa_redraw → cpa_redraw | **cpa_redraw（不变）** | 重问后仍否决：`frame_is_interesting=False`——「仅为鹅鸭杀大厅"准备中(11/12)"等待页」。正是 7/22 空面板案形状。与 Ivan 8/8 显式授权 `IVAN_EXPLICIT_20260808_A` 同向，无冲突 |
| 6 | `auto_200130_1722_1792` | BV13zuX6fEwh | talk | cpa_redraw → cpa_redraw | **cpa_redraw（不变）** | 真几何否决 `faithful_crop_can_make_dominant=False` → 7/26「1411 角落小人」保护，六项修复明确未放开 |

**结论：3 条翻成截图（#1/#2/#3），3 条仍判重绘且判据据实（两条空画面 + 一条角落小人）。**

### 与库内考据报告 E2d 的差异（须说明）
`docs/reviews/2026-08-10-cover-route-screenshot-first-forensics.md` E2d 预测 `200736` 是 P1 确定性
翻转成 polish。本次重算不同：E2d **没有重新解析 scene_kind**。8/7 是 RESOLVED 游戏场，归档的
talk 问卷回执在新代码下不可复用，必须重问；重问后按游戏判据（大厅等待页）否决。
本重算跑在部署面真码上，取代 E2d 的该行预测。

### 忠实性
产线只用 `candidates[0]`（best_ms），无换帧重选循环（已核 `publish_staging.py` 仅 `:2170/:2171`
读 `candidates[0]`），故本重放与产线同口径；未替 #4/#5 手工另选帧。

---

## 第二步：staging 产封面（3 条判定为截图的）

时基已验证：从**未烧字幕**的 `<cid>.recut.mp4` 在 `best_ms` 抽帧，与 `cover_refs/<cid>.cover-ref.png`
**逐像素完全一致**（mean abs diff = 0.0/0.0/0.0）。所以选帧时间戳 = best_ms，底图 = 那份 hash-bound 源帧。

### ✅ 1) `auto_213135_469_710` — BV18Gu16NEcX ｜ 三门全过（一次过）

- 选帧：**105 500 ms**（score 4.4571、emotion 1.0）
- 裁切：`[608, 2, 1461, 482]` of 1920×1080 → 1920×1080（2.25×），face-anchored 使脸落在 banner 文字带以下
- 文案（**线上现标题连续子串**）：主「紧张到手抖」／副「第一次3D Live」
- 封面 sha：`sha256:61e8895f54ab5f1861241c74d40b2680f9526fd926ac1507388d16f895c634e8`
- **门 1 punch 语义**：`PASS`，final_punch == 渲染字节，extractive ✓
- **门 2 v3 host-identity（talk 判据集）**：`PASS`，validator=True
  `source_lidousha_located/primary_subject_is_lidousha/primary_subject_is_visually_dominant/`
  `primary_subject_face_is_large_and_clear/primary_subject_carries_story_reaction/`
  `thumbnail_has_clear_click_hook` 全 true；`primary_subject_matches_other_source_participant/`
  `excessive_dead_space/meaningless_dominant_decoration` 全 false
- **门 3 标题封面联合 QC**：`PASS`
  `lidousha_primary/thumbnail_readable/single_clear_hook/title_cover_aligned/pass` = true；
  `text_overcrowded` = false；`physical_text_line_count` = 2；`unrelated_or_misleading_elements` = []
- 缩略 229×143：主行「紧张到手抖」清晰可读 ✓
- persona 核验：白发＋头顶熊猫耳发髻、非头套、无额外饰品、无吐舌 ✓
- **偏离披露 + 流水线发现（attempt-0，见 `attempt-0-mechanical-crop/`）**：
  先按**产线原样路径**做过一版——机械 P1 身份裁切 `[344,101,1499,750]` →
  `_compose_screenshot_poster_background` 海报卡 → banner 梗字叠字
  （sha `sha256:19fa81c4…`）。结果**梗字正压在她脸上**，不可交付。
  成因：punch 覆盖强制 banner 文字区（zone y=16..486，`_punch_layout_override`，
  且 `_overlay_lidousha_cover_title` 对侧栏 zone 抛 `COVER_TITLE_TOO_SMALL`，
  侧分栏对梗字不可用），而该源帧她的头正在画面上部。
  **这是与 230125 竖版裁切缺陷并列的第二个流水线缺口**：`screenshot_direct`
  路线**没有**整脸门（`_verify_polish_face_integrity` 只在 `screenshot_polish` 上跑），
  所以自动产线在这类"人物在上部"的帧上会直接出一张字压脸的封面，只能等
  v3 终检兜底。**即：确定性路由翻转本身不足以产出可交付封面。**
  本次交付版改用人工挑的 face-anchored 裁切 `[608,2,1461,482]`（把脸推到文字带以下）。

### ✅ 2) `auto_220747_488_680` — BV1hfuS6EENb ｜ 三门全过（第 2 版设计）

- 选帧：**21 000 ms**（score 3.6573），游戏场全幅不裁
- 裁切：`[330, 51, 1920, 945]`（去掉左侧弹幕列与底部"谢谢大家的礼物"直播贴片）→ 1920×1080
- 文案：主「旁边就有人暴毙」／副「不是我」（均为线上现标题连续子串；副行由门 1 第一版 REVISED 给出并沿用）
- 封面 sha：`sha256:8313fec8f90623cc4817d85ef41e22c563c064039b0790a79fbf123de050d1ea`
- **门 1 punch 语义**：`PASS`，final_punch == 渲染字节，extractive ✓
- **门 2 v3 host-identity（game 判据集）**：`PASS`，validator=True
  `host_window_visible_in_final/host_window_identity_matches/frame_is_interesting/`
  `thumbnail_has_clear_click_hook` = true；`primary_subject_is_visually_dominant` = **false 但被游戏场
  判据集接受**（Ivan 8/9 裁定的落地面首次在真件上生效）；反垃圾三项全 false
- **门 3 联合 QC**：`PASS`，`unrelated_or_misleading_elements` = []
- 缩略：主行可读 ✓
- persona：白发＋熊猫耳＋头顶墨镜（persona.md 可选配饰）、无吐舌 ✓
- **第 1 版 FAIL 记录**（`gates/attempt-1/`）：全幅未裁时门 3 判 FAIL，唯一原因
  `unrelated_or_misleading_elements: ["底部"谢谢大家的礼物！一会一起谢哦！"与标题事件无关"]`；
  第 2 版裁掉该贴片后通过。

### ⛔ 3) `auto_230125_960_1072` — BV1Bau16nEyq ｜ **连续两次 FAIL，按纪律停下**

- 选帧：**46 000 ms**（score 3.4553）
- 底图缺陷（**须上报的路由证据发现**）：该条源帧是 **1920×3414 竖版 9:16**；见证给的身份 bbox
  `[0.0, 0.145, 1.0, 1.0]` 覆盖整个身体，`_bbox_crop_box` 于是把 16:9 窗口对准 bbox 形心＝胸口，
  产出的"身份裁切"**把脸从眼睛处切断**。P1 在这类竖版源上机械授权通过但构图不可用。
  本次改用 P3「全幅不裁」把整幅竖版装进 16:9（模糊底衬），脸完整。
- 两版文案均为线上现标题连续子串：
  - 第 1 版：主「小李解释偶像曲」／副「再一次爱上我吧」→ 门 1 `FAILED`
    （`stranger_can_infer_event/contains_concrete_subject/contains_action_or_conflict` 全 **false**，
    评审称"只能看到解释歌词，不知具体争议点"）
  - 第 2 版：主「再一次爱上我吧」／副「小李解释偶像曲」→ 门 1 仍 `FAILED`，但**性质不同**：
    三个布尔**全 true**、story_summary 与 click_motivation 均为正面（"引人想点开听小李如何解释
    这句反常又暧昧的歌词"），失败出在 `status_shape_ok`——模型返回的 `final_punch` 未通过
    `_validated_extractive_punch`（非抽取式改写被清空）。即：**评审认可语义，但它想改写成非抽取式文案，
    被抽取式校验器清零**。
- 其余两门在第 2 版字节上**都过了**：门 2 v3 host-identity `PASS`（validator=True）、
  门 3 联合 QC `PASS`（unrelated=[]、2 行、可读）。
- 现有第 2 版封面 sha（未放行）：`sha256:5b4266b0901b29434e9278040f53b3a7234192fe859b8a9eb8a9376012ffee80`
- 建议：这条不是画面问题，是**梗字受"必须抽取式"约束**与该标题可抽取素材贫乏的冲突。
  需 Ivan 裁定是否放宽为"允许非抽取式梗字"（或手定一句），再跑第 3 版。

---

## 建议上线顺序（按改善幅度）

1. **`auto_213135_469_710` / BV18Gu16NEcX** — 4.46 分强名场面（第一次 3D Live 舞台真帧）顶替一张重绘，
   改善最大，且是"真表情就是封面"这条路线最典型的样本。
2. **`auto_220747_488_680` / BV1hfuS6EENb** — 正是 Ivan 8/9 点名抱怨的"游戏截图却被降级重绘"那一类；
   上线即是该裁定在公开面的首个实证。
3. **`auto_230125_960_1072` / BV1Bau16nEyq** — **暂不上线**，等梗字口径裁定。

## 未做（按纪律）
零 state 写入、零 `replacement_recuts/covers/` 落盘、零 edit、零上传、零部署。
