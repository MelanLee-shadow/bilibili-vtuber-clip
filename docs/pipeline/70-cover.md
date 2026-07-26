# 70 封面

本文件是封面步骤的**分步权威**。`docs/workflows/*` 与 cover skill 只提供操作方法；
memory 和日期化报告只作历史证据，不能覆盖这里或当前代码 schema。

`assets/lidousha/cover_repair_plans/history/` 只保存已完成或已取代的 hash-bound
证据，全部带 `do_not_execute=true`；repair CLI 会 fail-closed 拒绝。当前修复计划必须从
当前 state/artifact hashes 新建，不能复制历史候选、标题或路径。

- 默认 `auto` 路由：单人名场面只有同时具备强表情/动作证据、可信主播主体几何且全局运动不发散时才保留真实直播帧；双人联动的安全门先读 **StoryContract 的 typed 参与者关系**，关系语义/标题词只解释叙事，不能关闭人物门。hash-bound 源帧同时清楚出现双方，并能提供与故事有关的真实人物、物件、文字或情绪证据时，即使运动分数不高，也可优先保留真实互动；源帧没有直接拍到的动作或反转只能由封面文字/版式表达，不能倒推成像素事实。游戏运动高分但 `subject_confident=false`，或累计动作热区超过半屏，即使局部运动块误判为主体，也不能冒充主播名场面，必须走 CPA `gpt-image-2 images.edit` 大脸重绘。真实帧不得把整张同场截图直接当背景，必须装入当前 `cover_diversity_slot` 对应的图形海报底板（不同配色、纹理、卡片角度）后再叠梗字；中等且主体可信的帧可先轻修再进入同一底板。任何所选路线失败都 fail-closed，不得用低质随手截帧冒充成品。
- 形象铁律：以当场直播形象为原型，只改动作/表情/Q版；禁加饰品服装；多人场景主体锁定李豆沙；表情永不吐舌头。
- 同场批内创新硬门：selection 为 talk 入选项持久化 `cover_diversity_slot`；前 5 张不得碰撞背景家族。0–5 依次为蓝色漫画爆炸、暖色手账拼贴、紫色霓虹舞台、薄荷贴纸涂鸦、黑白漫画分镜、珊瑚棋盘杂志。返修必须继承该槽位，不能退回独立随机抽色。
- 版式：talk 轮换 left-split/right-split/banner；歌切恒 song-clean 且标题字要大（banner 级）；art direction 由 `_lidousha_cover_art_direction` 决定（`cover_generation.py`）。短梗字会为可读性强制 banner，但背景家族仍必须批内不同。
- 自动 talk 封面按 2026-07-20 生态调研采用 2–12 字的原话/质问/反差梗字，配真实表情帧和更大的脸；完整长标题不是默认封面文案。Ivan 定稿标题仍按人工权威保留其要求的全部成分；歌切恒为 `《歌名》`。
- talk 封面强调字号必须 `>=120px`；渲染低于该线直接报 `COVER_TITLE_TOO_SMALL`，交付包审计也必须阻断。不得用“文件完整/没有裁字”代替缩略图可读性验收；应缩短封面梗字或换更宽版式，禁止继续缩字（2026-07-22 当面对质封面 91px 回归案）。
- renderer 必须记录 `lidousha-cover-rendered-text-pixels.v3`，并内嵌
  `lidousha-cover-title-render-spec.v1`。render spec 逐字绑定分行/分段文本、位置、字号、颜色、
  描边、角度、字体文件名/SHA-256/face index 与输出尺寸；所有数值必须有限且在边界内，
  每个 glyph 都不得裁切。独立复验只从 profile 的 committed font asset 按文件名+hash 取字体，
  禁止 package 自选路径、宿主机系统字体或历史 fallback 参与放行。
- 最终包必须同时携带 final cover、叠字前 `pre-overlay`、真实 alpha `title-mask` 与原始
  `route-background`。auditor 只用 package-internal 文件：先从 route background 确定性重放
  1920×1080 background fit，再按 trusted render spec 重绘 title layer，重算 mask/bbox、文字区
  changed pixels/ratio 与 hash，并要求对 pre-overlay 做同一 alpha composite 后与最终 PNG
  **逐像素完全相等**。文字 bbox 还必须落在 feed 安全区 `x∈[260,1660]`；只在 JSON 自报字号、
  bbox、文字或 hash 均不算通过。
- 经审阅的封面返修可用 `regenerate_lidousha_cover.py --cover-text` 锁定短梗字；该文案必须由 hash-bound repair plan 提供并逐字验收，不得让返修入口擅自改写。
- 字体：全链验字形；选中字体必须完整覆盖标题且 `glyph_risk=[]`，否则
  `COVER_FONT_GLYPH_COVERAGE_MISSING` 阻断。生产可在 committed profile fonts 内选择完整字体，
  但放行复验不接受系统字体或未提交路径；实际选择、render spec 和像素证据由
  `cover_generation.py`、`cover_title_rendering.py` 与 `cover_text_pixel_evidence.py` 强制。

## 路由证据与审计

- screenshot 与 AI 都是一等路线；人工标题不等于禁用截图，截图 route 也不得因没有短梗字
  静默回退 AI。`auto` 必须落盘 `route + reason_codes + considered evidence`，从最终包可以回答
  “为何选截图/为何选 AI”。
- 路由证据必须先声明封面的叙事任务（单人表情、双人关系、物件/游戏画面等），再比较候选路线。
  `StoryContract relation_state=CONFIRMED` 且至少两个 `required_participant_ids` 时，typed
  `lidousha-cover-relationship-visual-safety.v1` 必须独立判定为 `REQUIRED`；标题/hook 是否
  命中“联动/搭档/霸凌”等关系词只解释叙事，绝不能关闭双人安全门。双人关系任务只有 hash-bound
  reference 同时看见全部 required IDs 才能选择截图；缺任一方时不能把单人图当“双人封面”，
  也不能静默调用 AI 补人。每条成片必须逐项记录截图直出、截图轻修与 CPA 重绘的接受或拒绝
  理由，不能用“默认”“自动选择”或功能不可用充当理由。
- reference authority 必须把 `source_visible_claims` 与 `narrative_presentation` 分开：
  前者只能列 exact hash-bound 源帧原分辨率人工可见的像素事实，并由
  `lidousha-cover-source-visual-verification.v1` 精确复核 reference hash、人物与逐条声明；
  后者说明哪些故事信息由封面文字/版式表达，不是源帧像素证明。例如双人同框可以是
  source-visible claim，未出现的椅子、火锅、霸凌、对质或脑瓜崩动作不能写成源画面事实。
  最终感知复核的 `cover_story_claims` 不能自行概括：集合必须精确等于包内 record
  StoryContract `cover_reference_authority` 的全部 `source_visible_claims → SOURCE_FRAME`
  加唯一 `narrative_presentation → COVER_TEXT`，逐条提供实际复核 evidence。任意遗漏、
  增补、改写、换 presentation，或用一个宽泛“关系符合”替代两面证据都阻断。
- 当前生产只接受 `lidousha-cover-route-decision.v2`：必须同时记录 `required_participant_ids`、
  hash-bound `source_visible_participant_ids`、`image_generation_planned/attempted/used`、selected 与
  actual treatment、执行结果，以及 screenshot_direct / screenshot_polish / cpa_redraw 三条路线中
  两条逐项拒绝理由；旧 v1 只允许历史包读取兼容，不能作为新生产证据。
- `screenshot_direct` / `screenshot_polish` 必须有官方源 SHA 绑定的 reference、实际 final cover
  文件与 SHA、逐字 rendered text；指定双人帧还必须匹配 reference override 的 candidate、
  source time、participant IDs 与 required treatment。截图路线不要求、也不得伪造 AI model 证据。
- screenshot proof 的 `screenshot_frame.frame_ms` 与
  `reference_selection.best_ms` 都必须是内容时间轴上的非负、非 bool 整数且精确相等；缺字段、布尔值、
  时间轴错位或任意 mismatch 都 fail closed。producer 写入与 cover repair 消费的是同一
  reference selection，禁止用“看起来是同一帧”的图片哈希或默认 `0ms` 代替时间绑定。
- 关系型 `screenshot_direct` 只允许
  `HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR`：reference 必须整帧、未裁切、未旋转、
  未 AI 修改，使用 deterministic `ImageOps.contain` 进入海报，人物落在中央 4:3 安全区，
  标题真实 glyph bbox 不遮关键 source content，并把 source transform 与最终 cover SHA
  一起绑定。只有这条可复验 no-crop 链成立时，source participant IDs 才能转移为 final
  participant proof；禁止简单复制 JSON 字段。
- CPA 路线必须有真实 on-disk AI background/final cover hash、attempted/selected model 与调用证据；
  `model`/`method` 默认字符串或 `ai_cover_generated=true` 不能冒充生图成功。
- `screenshot_polish` 即使单人也必须有最终像素验证：polish 模型可能返回比 prompt
  要求大得多的脸（2026-07-26 BV1E93L6rErV 案：固定 fit-crop 卡把嘴/下巴裁掉上了公开面），
  polished 像素不得继承源帧几何。生产端 `polish_face_verification`
  （`lidousha-cover-polish-face-verification.v1`，CPA sol 视觉问答，含吐舌检查）必须
  PASS 且 witness image hash 逐字节等于 final cover SHA；`FACE_INCOMPLETE` 先以
  face-safe contain 卡（`card_fit=contain_face_safe`，整脸装入 1640×700 卡）重排一次
  再终判；仍失败或验证不可用即 `COVER_POLISH_FACE_UNVERIFIED` 阻断（候选留在
  cover-only 维护，可下轮重试）。相机窗来源（crop 证据 `camera_window_crop=true`）
  为近全幅大脸，确定性直接走 contain 卡。包审计端同因阻断
  （`SCREENSHOT_POLISH_FACE_UNVERIFIED`）。已发布稿修复：
  `scripts/repair_screenshot_cover.py` 从既有 hash-bound polish 工件经同一生产函数重排
  并出回执；线上替换走 `scripts/bili_cover_edit.py`（cover-only 授权编辑+读回回执，
  编辑不占投稿配额）。
- **所有关系型路线**都必须有最终人物 proof。`screenshot_polish` 与 CPA/AI 因像素已被修改，
  绝不能继承 source participant 声明，必须由独立 final-pixel verifier 逐个确认双方可见、
  身份正确，并绑定最终 cover SHA；故事动作/反转若只由文字表达，必须作为 `COVER_TEXT`
  单独验收，不能要求或声称画面里存在。没有 verifier 或声明表现面不明确就阻断。
- 任一路线在最终像素、文字、安全区、人物关系或 route evidence 上失败都 fail closed，不得跨路线
  静默降级。双人联动要求双方在 hash-bound source reference 中真实可见；没有 counterpart
  reference 时禁止凭描述画第二位。
- producer 只产出视频/字幕但封面缺失或 route proof 无效时，状态必须是
  `media_ready_cover_pending + PENDING_COVER/COVER_REQUIRED`，不得标
  `review_ready + CURRENT/COMPLIANT`。cover-only 维护成功做完像素/哈希/route 绑定后才原子晋级；
  截图 repair 仍留在截图路线，不能用通用 AI 重绘把失败偷偷改道。
