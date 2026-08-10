# 封面路由「截图优先 vs 重绘兜底」考据与背离判定

日期：2026-08-10 ｜ 性质：只读法证（无代码/配置/部署改动）
触发：Ivan 2026-08-10「肯定是说过什么时候用截图，什么时候用重绘……应该积极的用截图，
而重绘才是兜底……只有实在找不到合适的截图方案才用重绘」

**一句话结论**：Ivan 的记忆方向正确、但那句总纲**不是他逐字说过的**——他说的是
7/21 的两条（「非常希望能够截图直出」「不要求 CPA 强制出图」＋「加入判断，
哪些适合重做、哪些适合截图，你自己想」）和 8/9 的一条（游戏截图不要求她主导画面）；
「截图永远优先、重绘只是兜底」这句**逐字出自 2026-07-25 助手对他提问的回答**，
他当场没有反对并授权了对应修复。而产线**确实已经走反**：8/7–8/8 两场共 10 条 talk，
路由选出 5 条截图路线，**实际交付截图 0 条**，5 条全部因
`SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED` 降级重绘，另外 5 条在路由阶段就被同一见证否决。

---

## A. Ivan 的原话清单（逐字，标注出处与真伪等级）

出处均为本机 Claude Code 转写 `~/.claude/projects/-Users-ivan-Project-vtuber-slice/*.jsonl`
的 `type=user` 主链（非 sidechain、非压缩摘要、非 task-notification）。

### A0. 是否存在「截图优先、重绘兜底」的 Ivan 逐字原话？

**未找到**（2026-08-10 之前）。全库扫 `截图` 命中 15 条 user turn、`重绘|重画|名场面|直出|修图`
命中 11 条、`兜底` 命中 19 条、`裁剪|裁切` 命中 8 条，逐条核对后：Ivan 从未用「优先／兜底」
的措辞给过封面路线总纲。他给过的是**方向性偏好 + 授权 + 场景级纠偏**，见 A1–A5。

排除面（这三类看起来像 Ivan、其实不是，已逐条剔除）：压缩摘要（如 7/14、7/25 的
`This session is being continued…` 转述）、`<task-notification>` 里的 subagent 输出
（`兜底` 的 19 条命中里，封面语境的 `d975757c:553`、`b533569f:1127` 两条经核实**都是
subagent 报告**，与封面路线无关）、以及 sidechain 里的父 agent prompt。
A1–A5 全部经 `isSidechain` 复核为 `MAIN` 主链真实 user turn。

### A1. 2026-07-21 04:17（逐字 · Ivan · 最强正面证据）

> 我其实也非常希望能够截图直出封面，但问题是，我不确定工作流又没有能力获得最具有演出效果的截图，你可以试试。纯文字封面暂时不考虑。人物脸部占比可以进行改动。标题钩子可以向它进行改动。另外，我希望你给我看看目前你按照最新的改动给出的封面都长什么样子。**如果是这样的话我不要求CPA强制出图。**

出处：`7f22dabc-b0a1-4346-adf3-dfe7aa6e6f11.jsonl:554`
语境：助手刚汇报 7/20 生态调研，把「①截图直出封面（生态最高播放的做法，但和"封面永远
CPA 出图"现行铁律冲突）」列为待拍板项（同文件 L548）。

这一条**解除**了 `cpa-real-ai-cover-always` 对 talk 封面的强制，是「截图是一等路线」的
唯一 Ivan 直接授权。注意他的顾虑是**能力**（"不确定工作流有没有能力获得最具有演出效果的截图"），
不是**偏好**。

### A2. 2026-07-21 05:00（逐字 · Ivan · 路由的立法原文）

> 认可，但是你不是说还可以截图微调以达到更好的效果吗？以及我更希望能够加入一些判断，哪些切片更适合全图CPA AI重做，哪些适合截图，**你可以自己好好想想，按这个来做**

出处：`7f22dabc-…jsonl:823`

**这就是任务里问的「7/21 名场面分路由」的裁定原文。它裁定的是「要有一个三路路由器」，
不是任何具体阈值。** Ivan 明确把判据设计**委托**给实现者（"你可以自己好好想想"）。
因此：

- `screenshot_direct ≥4.5`、`≥3.2+情绪`、`screenshot_polish ≥2.6`
  （现 `src/autoslice/publish_staging.py:2104-2106`，常量 `_COVER_TREATMENT_SCORE_HI/LO`）
  **不是 Ivan 定的数**，是实现者用 7/15、7/19 十七条本地成片的分数分布标定的
  （见 `publish_staging.py:2129-2131` docstring 与 memory `lidousha-cover-punch-mode-20260720`）。
- 优先级顺序（歌切/强制 → 关系 → 见证否决 → 分数）同样是实现者产物。
- 所以「阈值是不是 Ivan 那次定的」的答案是：**不是**。可以调，不需要推翻他的裁定。

### A3. 2026-07-25 03:09（逐字 · Ivan · **提问**，不是裁定）

> 按你之前学习到的经验，应该是截图修图好还是重绘好？但无论如何重绘都需要用prompt场景化。以及之前我说的字幕错误你不能只是修复这两处，得看看造成它的原因是什么，修复通病

出处：`c938c900-d564-4931-95ba-677149622e29.jsonl:4042`

### A3′. 2026-07-25 03:34（逐字 · **助手**回答，非 Ivan）

> **有真名场面时截图永远优先**——强名场面（≥4.5）直出，"真表情就是封面，重绘反而丢梗"；中等瞬间（≥2.6）截图打底修图清杂物。**重绘的正确位置只有两个：没有可用帧时的兜底、歌切的干净美学。**这两条 7/24 的问题不是路线体系错了，而是**游戏场景分支把它废了**……这个分支需要的改进是**小窗裁剪**……

出处：`c938c900-…jsonl:4312`（assistant）

**这句「截图优先／重绘兜底」的措辞源头在这里。** 它是 Claude 对 Ivan 提问的作答，
Ivan 没有反对。代码里 `publish_staging.py:2241` 的注释
「游戏场小窗回归（2026-07-25 Ivan：截图修图优先于重绘）」把它**归因成了 Ivan 的裁定**，
属于转述硬化成伪裁定——但方向没有被 Ivan 否定过，且他紧接着授权了 A4。

### A4. 2026-07-25 03:38（逐字 · Ivan · 对 A3′ 诊断的执行授权）

> 双引擎幻听是什么意思，AGY和BCUT同时幻听吗？……另外，**你可以现在开始做小窗裁剪**。现在有我能审的内容吗？7.22的好了吗

出处：`c938c900-…jsonl:4316`

他批准的正是「让游戏场也能走截图」的那个修复。这是**事实层面**的截图优先背书。

### A5. 2026-08-09 三连（逐字 · Ivan · 最新、最具体）

- 02:07 `d975757c-7f1f-430c-a71a-7db2e02fcb51.jsonl:1554`
  > ……第三个问题是，**封面不走截图的原因是什么？**
- 02:16 `…jsonl:1577`
  > ……另外解释一下**为什么降级为全重绘了**？特别是这种游戏截图里本来李豆沙的形象只在右下角的一小块区域
- 02:20 `…jsonl:1583`（**裁定**）
  > 按2走，但是你也要明确，这是封面流水线需要修的一个内容。**事实上，如果是截图封面的话，当然不要求李豆沙在画面里占主要部分，毕竟是游戏截图，只要截图足够有趣就行，主体肯定会会是游戏。**不过你说的很对，可以把表情截出来放大之类的。
- 02:43 `…jsonl:1630`
  > 可以，走上传吧，然后给我解释一下**为什么这次仍然不是直接用游戏截图**

A5 是本次考据里**最接近**「截图优先」的 Ivan 逐字裁定：他明确否决了「host 必须主导画面」
这条门在游戏场的适用性，并把它定性为「封面流水线需要修的一个内容」。
该裁定只落进了 `docs/reviews/2026-08-08-truth-harvest-forensics-synthesis.md`（commit 18c4d83），
**代码零改动**，且步骤权威 `docs/pipeline/70-cover.md:10` 至今仍写着相反的话
（「游戏运动高分但 `subject_confident=false`……必须走 CPA gpt-image-2 大脸重绘」）。

### A6. 其他相关 Ivan 原话（背景约束，不是路线总纲）

- 2026-07-19 21:50 `f68be667-…jsonl:3`：「**默认用人物重绘**」——这是**表情包 vs 人物重绘**
  的互斥规则（表情包只有强理由才用），不是「重绘 vs 截图」，不能当反证引用。
- 2026-07-14（BW2026 封面纠偏，memory `lidousha-cover-no-extra-accessories`）：
  「以当场直播的真实人物形象为原型，只允许改动作、表情或 Q 版萌化」＋多人场景主体锁定她本人。
  这条约束的是**重绘的自由度**，同样不是「必须重绘」。

### A 小结（回答「他到底说过什么」）

| 时间 | 强度 | 内容 |
|---|---|---|
| 7/21 04:17 | Ivan 逐字 | 希望截图直出；解除 CPA 强制 |
| 7/21 05:00 | Ivan 逐字 | 立法：要三路路由；**判据委托给实现者** |
| 7/25 03:09 | Ivan 提问 | 「截图修图好还是重绘好？」 |
| 7/25 03:34 | **助手**逐字 | 「截图永远优先……重绘＝兜底＋歌切」（被代码注释误标为 Ivan 裁定） |
| 7/25 03:38 | Ivan 逐字 | 授权小窗裁剪（＝让游戏场回到截图） |
| 8/9 02:20 | Ivan 逐字 | 游戏截图不要求她主导画面；定性为流水线必修 |

**Ivan 的方向从未变过，措辞总纲是他记混了归属。这不影响背离判定成立。**

---

## B. 代码现状（HEAD = `093904a`，分支 `claude/session-live-context`）

### B0. 路由不在任务点名的三个文件里

`cover_route_evidence.py` 只负责 route decision 的**记账与校验**，
`cover_repair.py` 是 cover-only 修复 lane，`regenerate_lidousha_cover.py` 是手工重出入口。
**真正的选路器是 `src/autoslice/publish_staging.py:2109 `_decide_cover_treatment`**，
调用点 `publish_staging.py:1558`（在 `_build_lidousha_cover_route`，:1527 起）。

**free 部署面已核对（只读）**：`/opt/bilive/autoslice/repo` 上
`src/autoslice/cover_source_composition.py:388-394` 的四布尔 AND、
`publish_staging.py:2104-2106` 的 `4.5 / 2.6 / 0.50`、
`publish_staging.py:2244` 的 camera-window 分支**与 Mac HEAD 逐行一致**
（行号、条件、常量全同）。因此本报告的 C 判定对生产面成立，不依赖「Mac HEAD == 部署面」的假设。

### B1. 选路器（`publish_staging.py:2109-2253`）的实际控制流

```
:2136  cover_mode == "cpa"           → cpa_redraw
:2138  is_song                       → cpa_redraw
:2140  relationship_visual_required  → screenshot_direct（关系分支，全幅不裁）
:2150  verified_stream_frame         → screenshot_direct
:2159  source_composition_recommends_redraw(...)  → cpa_redraw   ← 见证几何否决
:2168  frame_selection is None       → cpa_redraw
:2196  geometry_confident = subject_confident AND dispersion<=0.50
:2203  subject_confident = geometry_confident OR source_composition_supports_subject(...)
:2236  subject_confident AND (best>=4.5 或 emo且>=3.2) → screenshot_direct
:2238  subject_confident AND best>=2.6                 → screenshot_polish
:2240  best>=2.6 且 camera_window_bbox_frac            → screenshot_polish（7/25 小窗回归）
:2248  否则                                            → cpa_redraw
```

`9f51987`（7/31）已经修好了「见证无条件替换路由」的老病，标定分数确实恢复了决定权。
**问题不在这里，在下游执行点。**

### B2. `SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED`：触发条件、出处、当初理由

**定义**：`src/autoslice/cover_source_composition.py:388-394`

```python
if (
    verdict.get("source_face_complete") is not True
    or verdict.get("faithful_crop_can_make_dominant") is not True
    or verdict.get("source_carries_story_reaction") is not True
    or verdict.get("cpa_redraw_recommended") is not False
):
    raise ValueError("SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED")
```

**唯一调用点**：`publish_staging.py:2296-2320`（`_screenshot_base_and_crop`）——
只要 `source_composition_verification` 是 Mapping 就**无条件**走这条裁切，
而它在正常 talk 生产恒为 Mapping（`publish_staging.py:1741 if enforce_final_host_identity:`，
而 `enforce_final_host_identity=run_ffmpeg`，见 `scripts/run_auto_review_shadow_pipeline.py:741`）。

**失败后果**：`publish_staging.py:1925-1975` —— 截图路线报
`SCREENSHOT_ROUTE_MATERIALIZATION_FAILED` → 写 `route_demotion` → 落到
`cpa_redraw` + `READY_DEGRADED`。

**引入者**：`baaf250`（2026-07-31 07:19，author `aierlma`，commit body 为空一行标题
`fix(cover): decide composition before generation`）。同 commit 引入
`extract_authority_source_crop` 与整个 `cover_source_composition.py`。
当初理由（据同日 `9f51987` 的 body 与 70-cover.md:38-46 追述）：**2026-07-26 BV「1411 角落小人」案**
——最终成图里李豆沙只是角落小头像上了公开面，于是把「构图判断」从生成后提前到生成前。
方向正确，问题是它把 CPA 见证同时接成了**否决器**。

**7/31 的半修**：`9f51987` 明确把 `source_carries_story_reaction` 从否决集合里拿掉
（`cover_source_composition.py:283-306` 的 docstring 逐字写明：
「第三条是故事判断不是几何判断……反应缺失的正确出路是降级 polish 或换帧重选，
不是单独触发整张重画」），**但只改了 `source_composition_recommends_redraw`（路由端），
没有改 `extract_authority_source_crop`（执行端）的四布尔 AND。**

**结构性后果（关键）**：`_verdict_is_coherent`（`cover_source_composition.py:76-94`）强制
`cpa_redraw_recommended == not(face ∧ dominant ∧ reaction)`。所以当
`face=True, dominant=True, reaction=False` 时：

- 路由端：`recommends_redraw()==False` → 不否决 → `supports_subject()==True` →
  `subject_confident=True` → 按分数选 `screenshot_direct/polish`；
- 执行端：`reaction is not True` **且** `cpa_redraw_recommended is not False` →
  抛 `CROP_NOT_AUTHORIZED` → 降级 `cpa_redraw`。

**即：7/31 从前门放行的那一类，被后门原样拦回，且多烧一次截图尝试。**

### B3. 8/7–8/8 实测（free 只读，36 份 `*.record.json`，去重后 10 条 talk 候选）

扫描面：`find /opt/bilive/autoslice -name '*.record.json' -newermt 2026-08-05`，
字段 `publish_staging.cover_generation.route_decision`。

**原始 36 份计数**
- `selected_treatment`：`cpa_redraw` 14 ／ `screenshot_polish` 11 ／ `screenshot_direct` 11
- `actual_treatment`：`cpa_redraw` 32 ／ `screenshot_direct` 2 ／ `null(BLOCKED)` 2
- 降级原因分布：`SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED` **20/20（100%）**

**按候选去重（10 条 talk）**

| 候选 | 分数 | emo | disp | 路由选路 | 实际 | 死因 |
|---|---|---|---|---|---|---|
| 8/7 auto_200736_298_383（受骗片） | 3.55 | 0 | 0.655 | screenshot_polish | cpa_redraw | CROP_NOT_AUTHORIZED |
| 8/7 auto_203735_555_680 | 4.52 | 1 | 0.651 | cpa_redraw | cpa_redraw | 见证几何否决 |
| 8/7 auto_210739_1142_1436 | **8.17** | 1 | 0.651 | cpa_redraw | cpa_redraw | 见证几何否决（全批最高分） |
| 8/7 auto_220747_488_680 | 3.66 | 0 | 0.631 | cpa_redraw | cpa_redraw | 见证几何否决 |
| 8/7 auto_223750_913_1322 | 4.47 | 0 | 0.538 | cpa_redraw | BLOCKED | 见证否决＋punch 耗尽 |
| 8/8 auto_200130_1323_1603 | 4.91 | 1 | 0.348 | screenshot_direct | cpa_redraw | CROP_NOT_AUTHORIZED |
| 8/8 auto_200130_1722_1792 | 3.53 | 0 | 0.393 | cpa_redraw | cpa_redraw | 见证几何否决 |
| 8/8 auto_210131_1576_1802 | 3.02 | 0 | 0.207 | screenshot_polish | cpa_redraw | CROP_NOT_AUTHORIZED |
| 8/8 auto_213135_469_710 | 4.46 | 1 | 0.238 | screenshot_direct | cpa_redraw | CROP_NOT_AUTHORIZED |
| 8/8 auto_230125_960_1072 | 3.46 | 0 | 0.572 | screenshot_polish | cpa_redraw | CROP_NOT_AUTHORIZED |

**产线截图交付率 = 0/10。** 唯一一条 `actual=screenshot_direct/READY` 是 8/10 的
人工恢复树 `recovery/2026-08-10/auto_200130_1323_1603-yueqi-r1/`，不是自动产线产物。

**两个补充实证**

1. `subject_confident`（运动几何）在 **36/36 份记录里全为 false**。
   几何置信通道已经完全死亡，唯一还能授权截图的是 CPA 见证——而见证在执行端又自我否决。
2. 5 条降级全部 `source_composition_redraw_recommended=false`（两个几何布尔为 true），
   由 `_verdict_is_coherent` 反推可知**死因唯一**：`source_carries_story_reaction=false`。
   抽三份原始 verdict 复核，逐字符合：
   - 8/8 3DLive 片：`"…脸部完整，上半身…均可紧裁并放大为第一主体；但她呈闭眼微笑挥手姿态，持麦手没有可辨识的颤抖，也无高铁回想或紧张追梦的反应依据。"` → `source_carries_story_reaction:false`
   - 8/8 偶像曲片：`"…横向16:9可紧裁脸和肩胸并保持大主体，但画面仅为闭嘴浅笑直视，缺少反问…的开口、疑问或得意互动反应。"` → 同
   - 8/7 抱团片（分数 8.17）：`"李豆沙位于右下角……若仅作16:9裁切并保留关键上半身，仍会带入大量游戏UI，无法成为大号第一主体。"` → `faithful_crop_can_make_dominant:false`（路由端直接否决）

第三例正是 Ivan 8/9 02:20 亲自否掉的判据。

### B4. 「是不是把截图优先架空成重绘优先」

**是，实质架空成立。** 三层叠加：

1. **见证提问本身是「单人主导」框架**（`cover_source_composition.py:126-143`）：
   > 「在不生成、不补画、不扭曲身份且不裁掉关键反应的前提下，**能否只靠 16:9 裁切让她成为大号第一主体**；源图中的表情/动作是否**确实承载给定故事反应**。只要脸不完整、无法忠实裁成大主体、或源图不承载故事反应，就必须 `cpa_redraw_recommended=true`。**不要因为运动框、弹幕或游戏画面显眼而放行。**」

   最后一句与 Ivan 8/9「游戏就是主体，截图够有趣就行」**直接对立**。
2. **执行端四布尔 AND 抵消了 7/31 的前门放宽**（B2）。
3. **`source_carries_story_reaction` 是一个几乎必然为 false 的判据**：故事反应按定义常常
   发生在几百毫秒的某一帧之外，而 70-cover.md:98-104 自己规定
   「源帧没拍到的故事由 `narrative_presentation → COVER_TEXT` 承担」。
   用它当截图的**准入前置**，等于要求源帧独立完成整个叙事——实测 5/5 全灭。

**附带的两条死路**（同属架空面）：

- **7/25 Ivan 授权的小窗裁剪分支已成死代码**：`publish_staging.py:2240-2247` 的
  `camera_window_bbox_frac` 分支只在 `subject_confident==False` 时可达；而
  `faithful_crop=false → :2159 提前 return`、`faithful_crop=true → subject_confident=true`，
  两条路都到不了 :2244。见证存在（正常 talk 恒真）时该分支**不可达**。
  8/7–8/8 全部 36 份记录里 `camera window` 理由串出现 0 次。
- **所有修复/手工入口都是重绘独占**：`cover_repair.py:171 / 214 / 1790` 硬编码
  `selected_treatment="cpa_redraw"`、`subject_confident: False`；
  `scripts/regenerate_lidousha_cover.py:1-27` docstring 自述
  「Fail-closed like production: real CPA image edit only, never a frame-grab fake」。
  一旦降级或返修，**没有任何路径能走回截图**。
- **路由证据的「逐项拒绝理由」是模板生成的**：`cover_route_evidence.py:544-613 `_alternatives``
  按 selected_treatment 查表拼出另外两条的固定说辞。70-cover.md:98-101 要求
  「每条成片必须逐项记录三条路线的接受或拒绝理由，不能用『默认』『自动选择』充当理由」，
  当前实现在字面上满足、在实质上不满足——所以从包里**答不出**「为什么这一帧不能截」。

---

## C. 背离判定：偏离点逐一点名

| # | 环节 | 文件:行 | 偏离内容 | 对应 Ivan 原话 |
|---|---|---|---|---|
| C1 | **裁切授权** | `src/autoslice/cover_source_composition.py:388-394` | 四布尔 AND；`source_carries_story_reaction` 与 `cpa_redraw_recommended` 作为**截图准入前置**。8/7–8/8 20/20 降级全出自此 | A1／A3′（重绘应是兜底） |
| C2 | **7/31 半修留缝** | `cover_source_composition.py:283-306`（已改） vs `:388-394`（未改） | 路由端拿掉 story_reaction 否决，执行端保留，同一 commit 系列内部自相矛盾 | 9f51987 自述的修法未落全 |
| C3 | **见证提问框架** | `cover_source_composition.py:126-143` | 单人主导框架 + 「不要因为游戏画面显眼而放行」，游戏场恒判重绘 | A5（8/9 02:20，明确否决） |
| C4 | **步骤权威文档未更新** | `docs/pipeline/70-cover.md:10` | 仍写「游戏运动高分但 `subject_confident=false`……必须走 CPA 大脸重绘」；Ivan 8/9 裁定只进了 `docs/reviews/2026-08-08-truth-harvest-forensics-synthesis.md:152-165`（18c4d83），代码零改动 | A5 |
| C5 | **小窗裁剪分支不可达** | `publish_staging.py:2240-2247` | 见证在场时逻辑上到不了；实测命中 0 次 | A4（7/25 明确授权） |
| C6 | **降级出口只有重绘** | `publish_staging.py:1925-1975` | 截图物化失败 → 唯一出口 `cpa_redraw`；不尝试「不裁切的全幅海报」，尽管 `HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR` 机制已存在（关系路线在用，`publish_staging.py:2270-2292`） | A1／A3′ |
| C7 | **修复/手工面重绘独占** | `cover_repair.py:171,214,1790`；`scripts/regenerate_lidousha_cover.py:1-27` | 返修永远重绘，截图无法恢复 | A1 |
| C8 | **几何置信通道死亡** | `publish_staging.py:2196-2201` | `subject_confident` 实测 36/36 false；0.50 弥散帽是 n=1 标定（7/22 单案 0.6033），8/7 全场 disp≈0.63–0.66 | 9f51987 已记录但未处置 |
| C9 | **路由证据形同虚设** | `cover_route_evidence.py:544-613` | 拒绝理由查表生成，非逐帧证据 | 70-cover.md:98-101 |

**阈值本身不是主要偏离面**：5 条被降级的候选里 3 条分数 ≥4.46（直出区）、2 条在 3.0–3.5
（修图区），阈值判得都对。**偏离全部集中在授权门（C1/C2/C3）与降级出口（C6）。**

---

## D. 修复方案（设计，不实现）

### D0. 先答两个必答问题

**Q：裁切授权为什么当初被设成默认拒绝？**
`baaf250` 的直接动因是 **2026-07-26 BV1E93L6rErV / 1411 角落小人案**——最终成图里
李豆沙只是角落小头像却上了公开面（70-cover.md:38-46, :127-139 有记载）。
当时的下游 `polish_face_verification` 与主体显著性门尚未完备，所以把判断**提前到生成前**，
并采用「宁可全拒」的保守 AND。之后 7/31 `9f51987` 已经补齐了下游
（`cover_polish_gate.py` v2 + `cover_host_identity_gate.py:256-274`
`FINAL_COVER_SUBJECT_PROMINENCE_FAILED`），**前置保守门的原始理由已经过期，但门没撤。**

**Q：放开后哪些既有保护会失效？用什么替代？**

| 原保护 | 是否随 C1 放开而失效 | 替代保护（已存在，无需新建） |
|---|---|---|
| 「主体锁定李豆沙」（7/14 142 事故） | **不失效**。它约束的是重绘/多人场景主体，`relationship_visual_safety` 与 final participant verifier 独立生效 | `cover_route_evidence.py:161-233`、`validate_final_participant_verification` |
| 「不得裁掉主体／角落小人上公开面」 | **部分失效**（前置预判没了） | `cover_host_identity_gate.py:256-274`：最终像素级 `primary_subject_is_visually_dominant` + `meaningless_dominant_decoration`，绑定 final cover SHA，对**所有** `host_identity_required` 路线生效（含 direct） |
| 「polish 模型改脸/吐舌」 | 不失效 | `cover_polish_gate.py` v2 整脸门 + 吐舌检查，witness image hash 必须逐字节等于 final cover SHA |
| 「不得用低质随手截帧冒充成品」 | 不失效 | 分数门 2.6/4.5 + `cover_screenshot_poster.py` 海报底板 + 缩略图文字门 |
| 「双人联动必须双方可见」 | 不失效 | 关系分支排在几何否决之前（`publish_staging.py:2140-2158`） |

**结论：C1 是唯一一道『事前预判』门，它保护的目标已经被三道事后像素门完整覆盖，
放开它不会打开新的风险面，只会把一次失败从『不许尝试』变成『尝试后被拒』。**
代价是每条被拒候选多一次 CPA 视觉调用 + 一次本地裁切（无生图额度消耗）。

### D1. 最小改动清单（按收益/风险排序）

**P1 — 拆掉执行端的故事反应前置（治 C1/C2，改 4 行）**
`cover_source_composition.py:388-394` 的 AND 收窄到与路由端一致的两个几何布尔：
```
source_face_complete is True  AND  faithful_crop_can_make_dominant is True
```
删掉 `source_carries_story_reaction` 与 `cpa_redraw_recommended` 两个条件
（后者是前三者的派生量，本就不该单独出现在授权式里）。
理由已由 `9f51987` 自己写好：故事由 `COVER_TEXT` 承担（70-cover.md:98-104）。
**预期效果**：8/7–8/8 的 5 条降级在**路由与物化层**全部翻回截图路线（3 direct / 2 polish）。
注意这不等于 5 条都能交付：下游 v3 显著性门（`primary_subject_is_visually_dominant`）
在 P2 落地前仍严格生效，游戏场帧大概率仍会被它拦下并落回重绘。
P1 单独的净收益是「非游戏场的正常 talk 名场面恢复截图」，游戏场要 P2 才通。

**P2 — 见证提问按场景分叉（治 C3，落 Ivan 8/9 裁定）**
`cover_source_composition.py:126-143 `_QUESTION_PREFIX``：
- 新增 typed 字段 `scene_kind ∈ {talk, game, stage}`，由 StoryContract／
  `game_context.py` 既有的游戏场判定供给（**复用**，不新建探测器）；
- `scene_kind=game` 时：删掉「让她成为大号第一主体」与「不要因为游戏画面显眼而放行」，
  改问 `host_window_visible`（面捕小窗是否可见可辨）＋ `frame_is_interesting`
  （画面是否承载可点击的事件/UI/结果），`cpa_redraw_recommended` 由这两项推导；
- 同步改 `cover_host_identity_gate.py:256-274`：`scene_kind=game` 时
  `primary_subject_is_visually_dominant` 降为**披露项**，不再是 PASS 前置，
  改由 `host_window_visible` 承担身份底线。
- **同步改 `docs/pipeline/70-cover.md:10`**，否则文档与代码继续对打（C4）。

**P3 — 降级出口先落「不裁切全幅海报」（治 C6，重绘退回真兜底）**
`publish_staging.py:1925-1975`：截图物化失败时，先尝试关系路线已在用的
`HASH_BOUND_FULL_FRAME_NO_CROP_COMPOSITOR`（`publish_staging.py:2270-2292` +
`cover_screenshot_poster.py:61` 的 `ImageOps.contain` 路径），
只有它也失败才降 `cpa_redraw`。这一步把「重绘是兜底」从口号变成控制流事实。

**P4 — 小窗分支复活（治 C5，落 7/25 授权 + Ivan 8/9「把表情截出来放大」）**
把 `camera_window_bbox_frac` 分支从 `subject_confident==False` 的死角里挪出来：
在 `scene_kind=game` 且 `host_window_visible` 时优先于分数分支命中，产出
F-cover-2 的「小窗放大 + 游戏背景拼贴」中间路线。

**P5 — 修复面开截图（治 C7）**
`cover_repair.py` 的 `selected_treatment` 从硬编码改为**继承 active record 的
`route_decision.selected_treatment`**；截图路线走已存在的
`scripts/repair_screenshot_cover.py`，只有原路线本身就是 `cpa_redraw` 才走重绘。

**P6 — 路由证据据实化（治 C9）**
`cover_route_evidence.py:_alternatives` 的 `rejected_reason` 必须由调用方传入
真实判据（见证 verdict 的 reason 原文 + 分数 + 场景类型），模板串只做后缀。

**不动的**：阈值 4.5 / 3.2 / 2.6 —— 本批实测判得对，没有重标定依据
（缺「这帧当封面好不好」的人工标签）。0.50 弥散帽同理，P1 之后它已边缘化。

### D2. 回归金丝雀设计

**离线重放台架**（`9f51987` 已建、22/22 保真复现，直接复用，零 provider 调用）：

1. **正向金丝雀 · 本批 5 条降级案**
   固定 `record.json` 里已落盘的 witness verdict + frame_selection，
   重放 `_decide_cover_treatment` → `_screenshot_base_and_crop`：
   期望 5/5 从 `cpa_redraw/READY_DEGRADED` 翻成 `screenshot_direct×3 / screenshot_polish×2`，
   且 `route_demotion` 字段消失。
2. **负向金丝雀 A · 1411 角落小人案**（7/26 BV1E93L6rErV）
   `faithful_crop_can_make_dominant=false` → 必须仍在 `:2159` 被否决走重绘。
   **这条是 P1 不许放开的边界**。
3. **负向金丝雀 B · 7/22 空面板游戏 UI 案**（disp 0.6033）
   非游戏场判定下必须仍走重绘；标 `scene_kind=game` 后允许走截图，
   但**必须**被下游 `frame_is_interesting=false` 或缩略图门拦下——
   验证 P2 没有把「无聊空面板」放进公开面。
4. **负向金丝雀 C · 8/7 抱团片（分数 8.17，角落小人 + 游戏 UI）**
   P2 前：重绘。P2 后：截图，且 final host identity gate 的 `host_window_visible` 必须 PASS。
   这条同时是 Ivan 8/9 裁定的验收样本。
5. **保真金丝雀 · 全部非游戏 talk**
   P2 的 `scene_kind` 分叉在 `scene_kind=talk` 时必须与当前逻辑**逐字节等价**
   （prompt 串 sha 钉死测试，沿用 `cover_emote` 那套 no-emote 路径钉法）。
6. **上线后一场观测门**
   下一场自动切片跑完后统计 `actual_treatment` 分布，
   目标区间＝7/21 标定基线 5/7/5（direct/polish/redraw ≈ 29% 重绘）；
   若重绘率仍 >50%，说明还有第四层门未拆，不许宣布修复完成。

### D3. 部署纪律提醒（不属于本报告改动范围）

- 生产基线在 free，`docs/HANDOFF.md` 记录部署权唯一；本报告零改动、零部署。
- P1–P6 若实施，属封面链改动，须走 `deploy_free_autoslice.sh`（拒脏树）+ 全量 pytest。
- 已发布稿不得由通用 cover maintenance 自动重修（70-cover.md 末节），
  8/7–8/8 已发布的重绘封面若要换成截图，只能走 90 步显式授权 same-BV repair lane。

---

## 附：证据复现命令

```bash
# A. Ivan 逐字原话（本机转写，只读）
python3 <scratchpad>/extract_user_msgs.py "截图"          # 15 命中
python3 <scratchpad>/extract_user_msgs.py "重绘|直出|名场面|修图"

# B. 代码
grep -rn SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED src tests
git log --all -S 'def extract_authority_source_crop' --pretty='%h %ad %s' -- src
git log -1 --format=%B 9f51987

# C. free 部署面核对（只读）
ssh free "grep -rn 'SOURCE_COMPOSITION_CROP_NOT_AUTHORIZED' /opt/bilive/autoslice/repo/src/autoslice/cover_source_composition.py
          grep -n '_COVER_TREATMENT_SCORE_HI = \|camera_window_bbox_frac' /opt/bilive/autoslice/repo/src/autoslice/publish_staging.py"

# D. free 实测（只读）
ssh free "find /opt/bilive/autoslice -name '*.record.json' -newermt 2026-08-05"
# 逐份读 publish_staging.cover_generation.route_decision / route_demotion
#        publish_staging.cover_generation.source_composition_verification.verdict
```
