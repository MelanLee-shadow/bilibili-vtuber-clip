# 70 封面

本文件是封面步骤的**分步权威**。cover skill 只提供操作方法；
memory 和日期化报告只作历史证据，不能覆盖这里或当前代码 schema。

`assets/lidousha/cover_repair_plans/history/` 只保存已完成或已取代的 hash-bound
证据，全部带 `do_not_execute=true`；repair CLI 会 fail-closed 拒绝。当前修复计划必须从
当前 state/artifact hashes 新建，不能复制历史候选、标题或路径。

## 当前路由优先级

杂谈截图可以用同片说话人的真实神态与具体提问、回应或引语共同表达故事。台词是否有来源由字幕和故事事实检查负责；图像终检不能仅因静帧未演出台词中的人物、物件或动作，就推断台词虚构或图文无关。只有人物名、空泛口号、主体缺失、具体图文矛盾仍须拦截，身份正确或笑容本身不是通过理由。强制 screenshot/polish 路线可采用现有 hash-bound source-composition 回执中的主体及完整脸部证据，不得被旧运动几何标记覆盖；缺证、hash 漂移或脸部不安全仍拒绝。

标题—封面联合检查使用同一判断范围：普通室内或虚拟直播背景没有被标题提及，不单独构成
图文无关；仍须指出具体矛盾、虚构的场景断言或抢占主体的元素。已返回的失败回执保留原文，
不能由程序清空其 `unrelated_or_misleading_elements` 或重标 PASS；规则接线修复后才另取新回执。

- 默认 `auto` **截图优先，AI 次选**（维护者 2026-09-06 00:46 UTC 再次明确）：先从真实讲话帧找清楚、好看且与故事有关的表情、人物、物件或画面。可信身份见证确认脸完整、可忠实裁切，且帧达到可用分数（或经人工选帧）时，先走 `screenshot_direct`；平静、温柔、笑容也是有效状态，不要求一帧演出全部 StoryContract 动作和反转。确有杂物或画质问题时才选 `screenshot_polish`；没有可用真实帧时再考虑 `cpa_redraw`。运动几何只是候选线索，不能把游戏运动块当成主播，也不能覆盖有效的 CPA 身份框。游戏场景遵守真实游戏画面与主播小窗的专项规则，不强制裁成大脸。双人关系先读 StoryContract 的 typed 参与者门：源帧须清楚包含全部必需参与者，不能用 AI 补人。
- 截图版式同样按故事与真实素材尺寸选择。普通默认 `source-led` 保留完整源帧，用当前字体实际测量紧凑标题带，并在合成前比较布局可保留的源图大小；横向素材放进侧栏明显缩小时，回退 footer。poster、overlay 与最终 art_direction 必须消费同一个布局/文字区。中央 4:3 源画面保持完整、不被文字覆盖，背景只延展源色调，不强制卡片或图形皮肤。明确指定的历史样式与关系型整帧 compositor 可忠实重放。卡片、配色轮换不等于构图多样性。截图直出必须记 `image_generation_used=false`、`image_gen_model=none`，不得伪造生图回执；实际轻修/重绘才记录真实 CPA `images.edit` provenance。路线失败保留证据并修复，不能用未经审阅的随手截帧冒充成品。
- **游戏场分叉（维护者 2026-08-09 02:20 逐字裁定）**：「事实上，如果是截图封面的话，当然不要求李豆沙在画面里占主要部分，毕竟是游戏截图，只要截图足够有趣就行，主体肯定会会是游戏。」——本场 `session-game-context.v1` 已 RESOLVED **且**本条选帧探到固定面捕小窗 `camera_window_bbox_frac` 时判为游戏场（两条缺一即按谈话场，fail-closed，不新建探测器）。游戏场的见证与终检都改问「小窗可见可辨 + 画面本身有事件」，`subject_confident=false` 与累计动作热区超过半屏**不再**构成重绘理由，路线走 camera-window 分支的截图 polish，物化保留**整幅**游戏画面（裁成她的小窗会丢掉 维护者 要的游戏主体）。
- 形象铁律：以当场直播形象为原型，只改动作/表情/Q版；禁加饰品服装；多人场景主体锁定李豆沙；表情永不吐舌头。
- 同场创新按故事表达判断：`cover_diversity_slot` 仅提供缺省布局/强调色建议，模型的有效选择可覆盖；六背景家族仅保留显式历史样式重放，不再默认轮换，不能把背景不撞色当成多样性已完成。截图的有效 layout/title_style 不依赖 AI visual_brief；`art_direction.visual_brief` 在需要生成图像时选择镜头尺度、叙事次序、主次关系和质感，并由实际生图 prompt 消费；需要原型对照时，明确同一人的时间分格与真正多人联动的区别。真实人物、服装、故事事实与无伪字约束继续有效。
- 版式：talk 可按故事选 left-split/right-split/banner/footer；短梗字先用受信字体计算实际字号，能满足 120px 时保留所选区，装不下才在生图前换宽区，叠字时不偷换构图。歌切保留 song-clean。`title_style=clean` 使用 committed 字体链中的 SmileySans 优先、不额外旋转的奶油字与已知细描边；默认 outline 仍可用。两者都由同一 render spec/glyph mask/逐像素重放验证。双行副句保持八成字号，避免反差的后半句在缩略图里消失。
- 所有 talk 封面（含自动标题、维护者 手定标题与 same-BV 冻结投稿标题）
  采用 2–12 字的原话/质问/反差梗字，配能说明事件的图像；历史高播放样本不构成大脸、配色或CTR最优的因果证据；
  投稿标题 authority 只冻结投稿字段，不授权把完整长标题塞进封面。只有另立且绑定
  exact `cover_text` 的 `lidousha-full-text-cover-contract.v1`
  （`authority=REVIEWER_EXPLICIT`、`scope=FULL_TEXT_COVER`）才可要求封面全文；歌切恒为
  `《歌名》`。
- 封面增量复核必须把父/当前封面分别 hash，并从实际像素计算 changed bbox；只改标题字或局部图层时，
  只有声明 ROI 完整覆盖 bbox 才可复核该 ROI。bbox 越界、画布尺寸改变、无法读取像素或未提供
  ROI 都必须回退整张封面复核；不得因 raw 文件看似只改了几字节就跳过像素检查。该范围 receipt
  由 `src/autoslice/incremental_artifact_audit.py` 生成，仍不能替代当前封面 route、最终像素和
  title-cover joint-QC gates。
- **竖屏通常不适合直接裁成横幅，应先评估真实画面是否可用**。2026-08-10 原话为
  “并不是所有的都需要截图，特别是竖屏直播，通常不适合截图，只能重绘。”旧文档把“通常”
  扩张成了“竖屏一律重绘”。结合较新的截图优先要求，不能只由 `h/w >= 1.2` 就断言没有
  好截图。只有源帧身份、脸完整、可忠实裁成可用主体的真实见证成立时，才允许竖屏继续
  截图路线；无有效见证或几何不适用仍重绘。不得为坚持截图把脸/头裁掉，其他多人/game
  专项门及最终人物/像素验收保持适用。`VERTICAL_SOURCE_MIN_ASPECT_RATIO` 的旧无条件路由若仍
  存在，属于待对齐的实现差异，不能借旧注释覆盖新要求。
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
  生图前 fail closed。脸不完整或不能裁成大主体时，先检查换帧/全幅不裁版式是否能得到
  合格截图；截图补救仍不成立才进入 `cpa_redraw`。最终 v3 不能因 witness 不可用就静默
  跨线为截图。**「源图没有承担 StoryContract 的反应」不在
  否决集合里**（2026-07-31 `9f51987` 路由端已拿掉、2026-08-10 执行端补齐）：故事由
  `narrative_presentation → COVER_TEXT` 承担（见下文），反应缺失不单独触发 polish 或重绘；可用帧先 direct，
  不适用时再换帧——把它当截图准入前置，8/7–8/8 实测 20/20 次降级全部出自这一条。
  反之，CPA 判定可忠实裁切时，单人 screenshot route 必须以该 identity bbox 从 exact
  reference 重裁；crop evidence 同时绑定 reference SHA、bbox、source-composition
  witness/receipt SHA 与 crop output SHA。**裁切不授权（脸被切/bbox 非法）时先落
  `HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR` 全幅不裁海报，只有它也失败才降 `cpa_redraw`
  ——重绘是兜底，不是首选**；回执本身不可信（`SOURCE_COMPOSITION_VERIFICATION_INVALID`）
  仍然 fail closed，不得退到全幅。游戏场（上文分叉）恒走全幅不裁。motion/
  camera-window bbox 只能作候选，不能覆盖 CPA identity bbox；关系型 no-crop participant proof
  仍按下文独立规则保留完整 hash-bound source frame。若合法的单人 CPA identity bbox 因接近
  全高而使 16:9 crop **精确退化为整幅 source frame**，不得继续写
  `crop_applied=true` 却原样保留聊天栏/面板。此时只允许从同一 hash-bound reference 按
  identity bbox 独立扩展横纵 margin，先裁出仅含该 authority 区域的像素，再以该裁片自身的
  模糊背景和不变形前景确定性合成 1920×1080 identity card；回执必须记录
  `crop_strategy=IDENTITY_CARD_WHEN_16_9_CROP_DEGENERATES`、实际 crop box、foreground box 与
  `full_frame_degeneracy_avoided=true`，随后照常进入 face-safe poster 和最终 v4 像素门。若连
  该 authority crop 也仍是整幅 frame，直接 crop 入口必须返回 typed degeneracy，只有显式
  `..._or_full_frame` 包装器可如实降为 `HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR`，并记录
  `crop_applied=false` 与 fallback reason；不得伪造 identity crop 成功。
- **HOST_ONLY 不是“主播最大即可”**：谈话场 StoryContract 的
  `cover_fallback_mode=HOST_ONLY_GENERIC/HOST_ONLY_RELATION_EXPLICIT` 时，最终像素只能出现李豆沙。
  旧视频面板、小窗、头像、截图中的人物、局部脸或作为次要装饰的可辨识真人/虚拟角色均须由
  hash-bound final-pixel witness 明确判为不存在；即使李豆沙仍是最大主体也不能 PASS。单人 identity
  crop 已排除的角色不得被后续两区版式重新塞回。`VERIFIED_DUAL_STREAM_FRAME` 的真实多人任务继续
  走 required-participant inclusion/no-crop 合同；已证游戏场继续保留完整游戏画面与主播小窗，二者
  都不得被 HOST_ONLY 规则误伤。
- 返修不得单向吞掉截图路线：cover-only repair 在付费生图**之前**先看被顶替的路线。原路线
  是 `screenshot_direct/polish` 且其 hash-bound 像素仍在盘上时，通用重绘 fail closed 报
  `COVER_SCREENSHOT_ROUTE_REPAIR_REQUIRED`，返修改走 `scripts/repair_screenshot_cover.py`；
  像素已丢时重绘是唯一出路，但必须落 `cover-repair-route-displacement.v1` typed 披露。
  `actual_treatment` 永远如实记实际产出的路线，绝不把重绘字节记成截图。
- 上述入口对原生 `screenshot_direct` 只开放**文字层重排**：原背景字节及
  `screenshot_graphic_poster` 变换证据完全不变，文字、源帧、路由与其余冻结权威仍逐项核对；
  新最终像素须重新通过脸部、主播身份、字形与联合检查。`model/image_gen_model=none`、
  生图 planned/attempted/used 全为 false；不能把 direct 改标为 polish 来通过修复器。
- 已有 direct 背景上的新梗字可走 `screenshot_punch_successor.prepare_successor`：须提供
  当前记录的原始字节、绑定同一故事和封面文案的真实 CPA v2 语义裁决，以及新像素的脸部和
  主播身份回执。绑定器逐项对照当前包权威；标题、字体、设计、源帧、背景字节及变换证据
  保持不变，只允许已裁决梗字的重新排版和 hash 相同的文件路径迁移。审计重读原始记录、
  裁决和像素文件并重放字形；联合检查仍由发布步骤独立验收。
- 路由证据的逐项拒绝理由必须带**这一帧**的真实判据（见证 verdict 的 reason 原文、场景类型、
  分数、情绪命中、主体几何置信），查表模板只能作后缀；只有模板串的 `rejected_reason` 视为
  「默认/自动选择」充数，不满足下文的逐项记录要求。
- **梗字不必是标题的连续子串，也不必与标题一致重复（维护者 2026-08-10 逐字裁定）**：
  「梗字从来没有要求过必须是标题的连续子串吧，我不记得我要求过，事实上很多高播放量的
  切片，封面字块里的梗字和标题不一致，反而可能承接了一些解释原因或者补充说明的感觉，
  不需要与标题一致重复。」——**出处据实**：抽取式（逐字连续子串）硬约束**从来不是 维护者 的
  要求**，是 2026-07-21 `409e22f` 实现梗字模式时自造的「防 LLM 编造封面字」机械代理
  （原 docstring 自述），2026-07-28 `e35b74a` 又在终审层复制加固。梗字允许改写、缩写、
  换口语说法，第二行尤其鼓励承接**解释原因或补充说明**而不是重复第一行。
  防编造动机仍然成立，改由判官的第四项布尔 `no_fabricated_fact` 承担：梗字里每个具体
  指涉（人/物/动作/数字/结论）都必须由标题或 `selection_hook` 支撑，不得新增片中没有的
  人物/情节、不得把推测写成事实、不得升级程度或结果。该字段缺席即视为不通过（fail-closed），
  回执 schema 因此升为 `...semantic-review.v2`；v1 老回执过不了 v2 校验，会强制重打终审。
  「直接抽取原文」仍是合法且常见的写法，此时下文的截断护栏照旧生效。
- 短梗字不能只过“每行 2–12 字”的词面门。选择器必须把完整
  StoryContract `selection_hook` 连同标题交给 **CPA 文字模型**做最终语义裁决，并落盘
  hash-bound `lidousha-cover-punch-semantic-review.v2`：陌生观众只看最终 1–2 行也必须能
  推断一个具体事件、动作/冲突/荒诞因果和点击动机。两个分别合法但合起来不成事件的碎片
  必须改选；CPA 不能用“背景也许会画出道具”
  补文字语义缺口。裁决不可用、证据缺失或写不出任何自足梗字时必须重试或阻断；
  不得因标题是人工权威、`cover_punch_allowed=false`、回执为空或回执失败而把长
  `cover_text` 当作封面放行。CPA 在这里没有音频/图像输入，只裁决文字语义；
  每个 CPA 终审片段还必须本身就是一条可直接渲染的物理行（最多 9 个全角字宽）；
  过长时由 CPA 改成更短的写法（缩短原文片段，或另写一句同样有支撑的短句），renderer
  禁止再从中拆开专名、词组或句子。
- **分行权威等级（机器已实现）**：切点合法性只由权威定义——作者显式
  `\n` / CPA punch 段 / full-text contract / 已验证 `word_atoms` 点集。**宽度平衡器
  只能在合法点集内选择，绝不发明切点**；talk 无 contract 时 layout 的 `max_lines`
  排版预算按缩略图合同封顶；`word_atoms` 缺席时短文案锁单行，撞字号下限即阻断并转
  梗字评审重跑（CPA 给语义切分 + punch 路径先选择可读文字区，再按同一区生图）。
  此前「多行换大字」的排版预算（左右分栏 8 行）与本合同从未对账，加上「无梗字即把
  整段 `cover_text` 交平衡器」的自动回退，两者联乘产出过 3–8 行的成品，且全部落在
  `cover_text_mode=full` 路径，punch 路径零违例。
  “逐字来自原文”也不足以证明语义完整：若抽取片段在原文中正好结束于
  引号、书名号或括号左侧，说明它截掉了紧随的语义原子，确定性文字门必须拒绝。
  首行切点若把紧随的关键指称对象整体排除在外，同样必须
  fail closed；合格抽取需要完整保留该对象及其修饰内容。**无合格短文案不回退整段**
  （修正：此处原写「回退完整 `cover_text`」，与上文 :44-45 的禁令直接矛盾，
  这条政策缝正是这类截断问题的成因）——有界重试让 CPA 改成更短的写法，仍无则候选降
  `PENDING_COVER`；整句上封面只走显式 full-text contract。
  包审计须要求 final rendered lines 与 CPA `final_punch` 逐行完全一致，并重新校验
  StoryContract/cover_text hashes 与该回执。
- 所有 talk 最终封面都必须通过同一缩略图文字门：物理行数只能是 1–2 行，每行最多
  9 个全角字宽。producer 成图出口、最终包收口和独立 package auditor 都执行该门；
  `reviewer_manual_override`、`cover_punch_allowed=false` 或空 punch review 均不是豁免。
  只有上文独立显式的 exact-text full-text-cover contract 可豁免全文版式；该 contract
  不能由“标题是手定的”机械推导。
- talk 封面强调字号必须 `>=120px`；渲染低于该线直接报 `COVER_TITLE_TOO_SMALL`，交付包审计也必须阻断。不得用“文件完整/没有裁字”代替缩略图可读性验收；应缩短封面梗字或换更宽版式，禁止继续缩字。
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
- 经审阅的封面返修可用 `regenerate_channel_cover.py --cover-text` 锁定短梗字；该文案必须由 hash-bound repair plan 提供并逐字验收，不得让返修入口擅自改写。
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

封面维护在 native import 已写 `external-package-state-binding.v1 / VERIFIED_PACKAGE_BOUND` 时，
先重验该候选固定生产包的 journal、record/publish、视频和封面哈希，使用绑定的新视频与同 stem
封面；旧 `delivered` 路径只作历史保留，不能压过新绑定。导入绑定缺失/漂移不回退旧媒体；
无导入记录的历史 Talk/Song 路径规则保持。路径解析成功不替代封面证据、审计或发布授权。

当现成封面只需为新媒体建立后继证据、无须改变图片时，可用既有截图入口的
`--preserve-existing-pixels`。它从当前 record 验证原截图路线、最终脸部/主播见证、受信字体、
遮罩与背景逐像素重放，随后只在新的专用目录 create-only 复制原 PNG 和完整 record 前像；
不重排、不重新编码、不调用 provider，也不改写历史 witness、模型或日期。该模式拒绝
替换图源、身份见证或布局输入；原像素/见证失效时仍拒绝，不能把旧 FAIL 洗成 PASS。
`pixel_preserving_successor` 绑定前像原字节及相同最终哈希，绑定器会重放该前像，并只允许
最终 locator 变化及既有原生 StoryContract 富化。该产物不自带新视频绑定或上传权，仍由
既有 transactional binder、package audit、状态接纳和90步骤完成后续；旧 binding 原件保留。

绑定入口在任何可写的 generation 富化前，先拒绝已经占用的 immutable binding 路径
（含损坏文件、目录与悬空链接）；富化后仍复核该路径。预检不预留路径，也不宣称提供并发
隔离；不得先改写旧生成记录再报绑定已存在。旧视频 binding 不自动成为新成片证据。

- screenshot 优先，AI 作为所需补充；人工标题不等于禁用截图，也不等于封面必须全文。
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
  两条逐项拒绝理由；旧 v1 只允许历史包读取兼容，不能作为新生产证据。HOST_ONLY 谈话场还须记录
  `lidousha-cover-host-only-visual-safety.v1 / REQUIRED`，强制 `host_identity_required=true`，并由
  `lidousha-cover-final-host-identity-verification.v4` 在最终字节上证明零非主播人物/头像；普通 v3
  “李豆沙是主角、次要人物可存在”的回执不能满足该合同。v4 producer receipt 不是 package
  authority：current manifest item 还必须携带 `host_only_v4_binding`（schema
  `lidousha-host-only-v4-package-binding.v1`），分别绑定 canonical receipt、comparison、reference 与
  final cover 的 package-relative canonical path 和 SHA-256。canonical auditor 通过 no-follow 描述符
  现场读取四个包内 regular file，要求 receipt 与三面 `cover_generation` verification 完全相同、
  receipt/witness 中的像素 SHA 与包内实字节一致、bound final cover 就是 item `cover`，且四个 locator
  不 alias。缺 binding、文件缺失、包外/path escape、任一父目录或文件 symlink、schema/authority/
  candidate 漂移、receipt 漂移或任一字节漂移，都统一阻断为
  `COVER_HOST_ONLY_V4_PACKAGE_BINDING_MISSING_OR_INVALID`。
  V4 回执消费还必须验证 witness 的 `status=OBSERVED`，原始 answer 满足完整现行字段合同、
  与 verdict 逐字段相同，并重放 producer 原有 identity/composition/HOST_ONLY 全部通过谓词。
  只把外层 status 标为 PASS、保留“没有其他头像”两字段，不能覆盖主体不符、脸不清楚、
  构图失败、未观察或不完整回答。producer 与 consumer 复用同一组确定性谓词，不重调模型；
  合法 AGY 故障后备继续要求原 CPA 失败披露，游戏/双人和冻结出版历史兼容范围不变。

  Talk、Song、Manual、Recovery 四个 canonical manifest builder 遇到 v4 时，先把 receipt、comparison
  与 reference 物化到 package `evidence/`：优先复用包内同哈希字节，必要时才对 producer locator 做
  一次 no-follow 读取；随后写入 binding 并立即重放 validator。v3/非 HOST_ONLY 包不增加该字段。
  历史 v3 完整包若只缺最终人物见证，只能使用安全 v2 create-only successor：外部 receipt 与
  comparison 各自单次 no-follow 冻结，source→destination preimage 逐文件相同后，所有落包、hash、
  binding 与 audit 都只消费冻结字节。旧 v1 “先校验 path、后再次打开复制” successor 已因 TOCTOU
  撤销，不得消费。安全结果仍须 canonical audit 0 issue/0 blocker，固定
  `provider_calls=0 / image_generation_calls=0 / upload_calls=0 / upload_allowed=false`，且不替代80步骤
  接纳或90步骤候选级上传授权。
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
  要求大得多的脸（固定 fit-crop 卡可能把嘴/下巴裁掉，曾导致这类裁切上了公开面），
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

## 生图实测记录与迁移边界（2026-09-09）

现有`_call_cpa_image_edit`仍负责调用位与provider slot，传输实现由`cpa_image_edit.py`承接。
不改变截图优先、模型参数/缺省链、HTTP失败回退范围、独立身份检查、字形及最终像素门。
传输抽取后仍属于原有代码证明范围：Talk自动遍历，Song显式列表及package policy fingerprint
都须纳入`cpa_image_edit.py`，不能只指纹外层wrapper而漏掉实际执行的传输实现。
response回执可加`image-edit-service-observation.v1`：请求model/size与服务端reported_model、
quality/size/output_format分开，request ID仅接受有界安全标识；服务自报模型仍
`upstream_identity_verified=false`，不能伪装底层或snapshot已独立证实。未提供的usage/质量/尺寸
保持null，不从请求填回；token用量分NOT_REPORTED、INVALID、PARTIAL、INCONSISTENT与有效数值
报告，不重算/修补服务端账，cost_usd始终未知，除非以后另有实际账单证据。

逐次请求和失败耗时用本机单调时钟测量；request_elapsed不含provider slot等待，attempt_elapsed
包含该次下载/规范化，不是整条端到端时延。接口返回图先保存create-only、按内容hash命名的
raw-image.bin（0600）并记录原尺寸/格式；再走原1920×1080确定性规范化。raw字节与normalized
输出分别hash，不把裁切损失都归给模型。已有同hash文件必须仍为单链接regular0600且字节相等，
冲突拒绝覆盖。坏图保留取证但仍走原正常解码拒绝，不作为通过。该额外文件属于证据，不自动
删作垃圾，不反向改写历史生产回执。

这些记录用于比较新模型，不自动扩大repair白名单或切换服务默认。Image2.5的两个alias/snapshot
可用现有显式单模型输入做隔离测试，但本次只有离线合成HTTP回包，没有实际CPA可用性、图片质量、
耗时或成本结论。已有401观察不由这项代码绕过；正式迁移仍需获准的真实edit和独立像素审查。
官方API返回字段和模型定义参考2026-09-09读取的：
- https://developers.openai.com/api/reference/resources/images/methods/edit/
- https://developers.openai.com/api/docs/models/gpt-image-2.5-flare
- https://developers.openai.com/api/docs/models/gpt-image-2.5-sunburst
