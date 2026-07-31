# 70 封面

本文件是封面步骤的**分步权威**。`docs/workflows/*` 与 cover skill 只提供操作方法；
memory 和日期化报告只作历史证据，不能覆盖这里或当前代码 schema。

`assets/lidousha/cover_repair_plans/history/` 只保存已完成或已取代的 hash-bound
证据，全部带 `do_not_execute=true`；repair CLI 会 fail-closed 拒绝。当前修复计划必须从
当前 state/artifact hashes 新建，不能复制历史候选、标题或路径。

- 默认 `auto` 路由：单人名场面只有同时具备强表情/动作证据、可信主播主体几何且全局运动不发散时才保留真实直播帧；双人联动的安全门先读 **StoryContract 的 typed 参与者关系**，关系语义/标题词只解释叙事，不能关闭人物门。hash-bound 源帧同时清楚出现双方，并能提供与故事有关的真实人物、物件、文字或情绪证据时，即使运动分数不高，也可优先保留真实互动；源帧没有直接拍到的动作或反转只能由封面文字/版式表达，不能倒推成像素事实。游戏运动高分但 `subject_confident=false`，或累计动作热区超过半屏，即使局部运动块误判为主体，也不能冒充主播名场面，必须走 CPA `gpt-image-2 images.edit` 大脸重绘。正常生产与 cover-only regenerator 都必须在 generation 明示 `image_gen_model=cpa`；缺失该 provenance 即使图片和人物门通过也不能进入同 BV 最终人审。真实帧不得把整张同场截图直接当背景，必须装入当前 `cover_diversity_slot` 对应的图形海报底板（不同配色、纹理、卡片角度）后再叠梗字；中等且主体可信的帧可先轻修再进入同一底板。任何所选路线失败都 fail-closed，不得用低质随手截帧冒充成品。
- 形象铁律：以当场直播形象为原型，只改动作/表情/Q版；禁加饰品服装；多人场景主体锁定李豆沙；表情永不吐舌头。
- 同场批内创新硬门：selection 为 talk 入选项持久化 `cover_diversity_slot`；前 5 张不得碰撞背景家族。0–5 依次为蓝色漫画爆炸、暖色手账拼贴、紫色霓虹舞台、薄荷贴纸涂鸦、黑白漫画分镜、珊瑚棋盘杂志。返修必须继承该槽位，不能退回独立随机抽色。
- 版式：talk 轮换 left-split/right-split/banner；歌切恒 song-clean 且标题字要大（banner 级）；art direction 由 `_lidousha_cover_art_direction` 决定（`cover_generation.py`）。短梗字会为可读性强制 banner，但背景家族仍必须批内不同。
- 所有 talk 封面（含自动标题、Ivan 手定标题与 same-BV 冻结投稿标题）按
  2026-07-20 生态调研采用 2–12 字的原话/质问/反差梗字，配真实表情帧和更大的脸；
  投稿标题 authority 只冻结投稿字段，不授权把完整长标题塞进封面。只有另立且绑定
  exact `cover_text` 的 `lidousha-full-text-cover-contract.v1`
  （`authority=IVAN_EXPLICIT`、`scope=FULL_TEXT_COVER`）才可要求封面全文；歌切恒为
  `《歌名》`。
- 真实帧候选的全屏 motion z-score 只用于发现动作，不能让切场、白雾、加载页等瞬时
  运动离群值压过故事讲话帧。排序必须对 motion 贡献设上限，并继续综合语音能量、清晰度
  与字幕情绪；最终选帧还须由 CPA vision 首选见证对实际像素确认主播脸完整/可用、画面不是
  空白过渡；只有 CPA 图像调用不可用或输出不合约时才允许 AGY 作披露失败原因的后备。两者均
  失败即换帧重做，不能因确定性 score 较高而放行。
- 正常 production 在 reference 抽取完成后、初始 route decision 之前，必须执行 hash-bound
  `lidousha-cover-source-composition-verification.v1`。CPA-primary 图像见证逐项输出并严格验证
  `lidousha_bbox_frac`、`source_face_complete`、
  `faithful_crop_can_make_dominant`、`source_carries_story_reaction` 与
  `cpa_redraw_recommended`；CPA 不可用/输出不合约时只允许按统一 `visual_witness` 路由披露
  后备 AGY，reference hash、bbox、provider routing、回执或两端见证任一不可用/非法都须在任何
  生图前 fail closed。只要脸不完整、无法忠实裁成大主体、或源图没有承担 StoryContract 的反应，
  初始 v2 `selected_treatment` 就必须是 `cpa_redraw`（7/26 1411 角落小人案），最终 v3
  不可再因 witness 不可用静默跨线为截图。反之，源反应明确且 CPA 判定可忠实裁切时，单人
  screenshot route 必须以该 identity bbox 从 exact reference 重裁；crop evidence 同时绑定
  reference SHA、bbox、source-composition witness/receipt SHA 与 crop output SHA。motion/
  camera-window bbox 只能作候选，不能覆盖 CPA identity bbox；关系型 no-crop participant proof
  仍按下文独立规则保留完整 hash-bound source frame。
- 短梗字不能只过“逐字来自标题、每行 2–12 字”的词面门。选择器必须把完整
  StoryContract `selection_hook` 连同标题交给 **CPA 文字模型**做最终语义裁决，并落盘
  hash-bound `lidousha-cover-punch-semantic-review.v1`：陌生观众只看最终 1–2 行也必须能
  推断一个具体事件、动作/冲突/荒诞因果和点击动机。两个分别合法但合起来不成事件的碎片
  （2026-07-24 “生豆角 / 熊猫头下播”案）必须改选；CPA 不能用“背景也许会画出道具”
  补文字语义缺口。裁决不可用、证据缺失或无法从原文抽出自足梗字时必须重试或阻断；
  不得因标题是人工权威、`cover_punch_allowed=false`、回执为空或回执失败而把长
  `cover_text` 当作封面放行。CPA 在这里没有音频/图像输入，只裁决文字语义；
  每个 CPA 终审片段还必须本身就是一条可直接渲染的物理行（最多 9 个全角字宽）；
  过长时由 CPA 改选较短的连续原文，renderer 禁止再从中拆开专名、词组或句子。
- **分行权威等级（2026-07-31 立，机器已实现）**：切点合法性只由权威定义——作者显式
  `\n` / CPA punch 段 / full-text contract / 已验证 `word_atoms` 点集。**宽度平衡器
  只能在合法点集内选择，绝不发明切点**；talk 无 contract 时 layout 的 `max_lines`
  排版预算按缩略图合同封顶；`word_atoms` 缺席时短文案锁单行，撞字号下限即阻断并转
  梗字评审重跑（CPA 给语义切分 + punch 路径强制 banner，切点与字号一次全解）。
  此前「多行换大字」的排版预算（左右分栏 8 行）与本合同从未对账，加上「无梗字即把
  整段 `cover_text` 交平衡器」的自动回退，两者联乘产出过 3–8 行的成品：2026-07-24
  至 07-29 的 30 条成品里 6 条违例，全部 `cover_text_mode=full`，punch 路径零违例。
  分诊单见 `docs/reviews/cover-text-violations-triage-20260731.md`。
  “逐字来自原文”也不足以证明语义完整：若抽取片段在原文中正好结束于
  引号、书名号或括号左侧，说明它截掉了紧随的语义原子，确定性文字门必须拒绝。
  2026-07-25 “让新3D永久保留 / 小李拒绝花钱”案会因第一行截掉“白色奶龙”对象而
  fail closed；合格抽取可用““白色奶龙”表情 / 小李拒绝花钱”。**无合格短文案不回退整段**
  （2026-07-31 修正：此处原写「回退完整 `cover_text`」，与上文 :44-45 的禁令直接矛盾，
  这条政策缝正是白色奶龙案的成因）——有界重试让 CPA 改选更短的连续原文，仍无则候选降
  `PENDING_COVER`；整句上封面只走显式 full-text contract。
  包审计须要求 final rendered lines 与 CPA `final_punch` 逐行完全一致，并重新校验
  StoryContract/cover_text hashes 与该回执。
- 所有 talk 最终封面都必须通过同一缩略图文字门：物理行数只能是 1–2 行，每行最多
  9 个全角字宽。producer 成图出口、最终包收口和独立 package auditor 都执行该门；
  `ivan_manual_override`、`cover_punch_allowed=false` 或空 punch review 均不是豁免。
  只有上文独立显式的 exact-text full-text-cover contract 可豁免全文版式；该 contract
  不能由“标题是手定的”机械推导。
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
  cover-only 维护分支必须从既有 `cover_generation.story_contract.selection_hook`
  （仅在旧包缺失时退到同条 state 的 `hook`）向生成器传递完整故事，不能只给标题后产生
  与交付包 StoryContract 哈希不一致的短梗字回执。
  返修 preflight 以 active record 的完整 StoryContract 为权威；publish/cover evidence 只保存
  封面所需字段投影，因此必须逐字段验证“投影属于该完整权威”，不得错误要求精简投影与完整
  对象整体相等。多个完整权威不一致、投影缺核心字段或任一已写字段漂移时仍须 fail-closed；
  后续 preflight 与最终绑定成功后必须清除旧失败字段，不能让已恢复的 state 继续携带伪 blocker。
- 字体：全链验字形；选中字体必须完整覆盖标题且 `glyph_risk=[]`，否则
  `COVER_FONT_GLYPH_COVERAGE_MISSING` 阻断。生产可在 committed profile fonts 内选择完整字体，
  但放行复验不接受系统字体或未提交路径；实际选择、render spec 和像素证据由
  `cover_generation.py`、`cover_title_rendering.py` 与 `cover_text_pixel_evidence.py` 强制。

## 路由证据与审计

- screenshot 与 AI 都是一等路线；人工标题不等于禁用截图，也不等于封面必须全文。
  缺少合格短梗字时截图 route 必须阻断或显式重试，不能用长全文封面或静默跨线回退 AI。
  `auto` 必须落盘 `route + reason_codes + considered evidence`，从最终包可以回答
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
  （`lidousha-cover-polish-face-verification.v2`，CPA-primary 图像见证，含吐舌检查）必须
  PASS 且 witness image hash 逐字节等于 final cover SHA；`FACE_INCOMPLETE` 先以
  face-safe contain 卡（`card_fit=contain_face_safe`，整脸装入 1640×700 卡）重排一次
  再终判；仍失败或出现不合格表情时，必须拒收 AI 修图像素。只有原截图另行通过
  CPA-primary 的最终成图“李豆沙为明确主体”像素门，才可退回 hash-bound
  `screenshot_direct / READY_DEGRADED`；验证不可用或原图里李豆沙只是角落小头像时阻断并
  排入有界重跑。保留失败 witness 与 attempted/used 收据，不得把坏修图留给 cover-only
  维护永久阻断，也不得跨路线改成 AI 重绘。相机窗来源（crop 证据
  `camera_window_crop=true`）
  为近全幅大脸，确定性直接走 contain 卡。包审计端同因阻断
  （`SCREENSHOT_POLISH_FACE_UNVERIFIED`）。尚未交付且停在
  `media_ready_cover_pending` 的 screenshot 路线若 proof 无效，
  cover maintenance 不得每个 tick 原地打印同一 BLOCK；它必须把候选转成一次有界、
  可恢复的正常 producer 重跑。`screenshot_direct` 在原路线重建确定性 proof，
  `screenshot_polish` 重新取得 polished pixels 并再走整脸门。该同 fingerprint
  自动重试最多一次，预算由持久化的 `cover_route_regeneration_fingerprint` 绑定，
  不得错误复用跨版本累计的 talk transient 次数；已经交付的包仍须走显式 same-BV-safe 修复，绝不由通用 AI
  repair 偷换路线。已发布稿修复：
  `scripts/repair_screenshot_cover.py` 从既有 hash-bound polish 工件经同一生产函数重排
  并出回执；线上替换走 `scripts/bili_cover_edit.py`（cover-only 授权编辑+读回回执，
  编辑不占投稿配额）。
- source-composition 允许的单人 identity crop 与 `FACE_INCOMPLETE` 重排统一使用 face-safe
  poster：exact 16:9 source 在中央 4:3 安全区内以 `1440×810`、`0°` contain 构图；禁止再用
  旋转的矮卡制造上下大空带，也不得无条件绘制横贯画面的 accent bar。该重排只改善确定性
  构图，不能代替下游 v3 身份、主体显著性和故事反应终检。
- CPA redraw 的李豆沙最终身份复核若得到明确 `FINAL_HOST_IDENTITY_MISMATCH`，只允许重绘一次，
  仍不匹配就阻断；若复核本身因 CPA vision 与 AGY 后备均 quota/timeout/不可解析而不可用，
  则禁止继续消耗生图额度，也禁止放过未经核验的 AI 像素。不能再把同一见证不可用状态下的
  原截图当作无条件 `screenshot_direct / READY_DEGRADED` 出口：所有实际最终成图（包括 direct）
  都必须有 hash-bound 主体 PASS。未发布包保留失败回执并排入一次正常 producer 重跑；已发布
  稿走显式 same-BV-safe 修复。
- **所有关系型路线**都必须有最终人物 proof。`screenshot_polish` 与 CPA/AI 因像素已被修改，
  绝不能继承 source participant 声明，必须由独立 final-pixel verifier 逐个确认双方可见、
  身份正确，并绑定最终 cover SHA；故事动作/反转若只由文字表达，必须作为 `COVER_TEXT`
  单独验收，不能要求或声称画面里存在。没有 verifier 或声明表现面不明确就阻断。
- 所有 `cpa_redraw` 与实际采用 AI 像素的 `screenshot_polish` 还必须通过独立的
  `lidousha-cover-final-host-identity-verification.v3`：首选 CPA vision 只看 hash-bound 的
  SOURCE/FINAL 对照图，先在源图按名牌与当场造型定位李豆沙，再确认最终封面的最大叙事主体
  仍是李豆沙，并同时裁决主体显著性：李豆沙必须足够大、脸部完整清楚、承担故事反应且形成
  第一视觉焦点；不能只是右下角可辨认的小人，不能让大片死空白、无意义纯色条/色块、装饰噪声
  或无关物件压过主体，缩略图必须有明确点击钩子。给伊索尔等其他参与者补熊猫耳、白发或熊猫
  元素不能算身份正确；主角与任一其他源人物更匹配、主体过小/不承担反应、构图不值得点击、
  无法定位源人物、CPA 与 AGY 后备均不可用、回执不可解析或 hash 不一致都必须阻断并进入一次
  有界封面重画，不能晋级 `review_ready`。生成和重画 prompt 也必须明确大号显眼主体、禁止角落
  小人/死空白/无意义装饰，避免重复同一失败模式。这里的 CPA vision 是独立于 `gpt-image-2`
  生图请求的第二次判断，不能让同一生图响应自证；CPA 不可用时才允许 AGY 作故障后备，回执
  必须披露 `fallback_used=true` 与 CPA 失败原因。该门独立于 `relation_state`，因此会话关系
  ledger 漏记也不能让多人物参考图绕过主播身份复核。首次 `FINAL_HOST_IDENTITY_MISMATCH`
  或 `FINAL_COVER_SUBJECT_PROMINENCE_FAILED` 必须保留被拒图与回执，用“源图名牌中的李豆沙
  才是主角、不得给其他参与者补熊猫耳冒充、李豆沙必须是大号清晰并承担反应的第一视觉焦点”
  的强化提示有界重画一次并再次复核；第二次仍失败才进入 cover-only 维护阻断。
- 任一路线在最终像素、文字、安全区、人物关系或 route evidence 上失败都 fail closed，不得跨路线
  静默降级。双人联动要求双方在 hash-bound source reference 中真实可见；没有 counterpart
  reference 时禁止凭描述画第二位。
- producer 只产出视频/字幕但封面缺失或 route proof 无效时，状态必须是
  `media_ready_cover_pending + PENDING_COVER/COVER_REQUIRED`，不得标
  `review_ready + CURRENT/COMPLIANT`。cover-only 维护成功做完像素/哈希/route 绑定后才原子晋级；
  截图 repair 仍留在截图路线，不能用通用 AI 重绘把失败偷偷改道。新 fingerprint 使旧截图
  proof 过期时，未发布的历史 `review_ready/ok/quarantine` 与已在 cover-pending 的包享有
  同一次 fingerprint-bound route-preserving producer 重生；排队时必须同步降为
  `PENDING_COVER/COVER_REQUIRED`，不得留下 `CURRENT/COMPLIANT` 的伪终态。
- 新封面门或流水线 fingerprint 使旧证据过期时，generic cover maintenance 只处理未发布包。
  `publication_registry.v1.json` 已登记 `status=published` 的候选必须在事务恢复、预算刷新和任何
  provider 调用之前排除；不得因 schema 升级自动重修公开稿或消耗生图额度。公开稿若确需改封面，
  只能走 90 步的显式授权 same-BV repair lane。registry 不可读时 generic maintenance 同样
  fail-closed 停止，不能在出版身份未知时付费重画。
