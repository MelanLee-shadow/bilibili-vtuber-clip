# 8/7 批真值收割法证汇总 + 复裁清单(2026-08-08)

输入:Ivan 逐 cue 订正真值(67 文字 + 28 说话人,5 候选)。单片报告:
[换身份](2026-08-08-forensics-auto_220747_488_680.md) ·
[抱团](2026-08-08-forensics-auto_210739_1142_1436.md) ·
[受骗+真善美](2026-08-08-forensics-200736-203735.md)。贪生怕死零订正。

## 归类总量(文字 67)

| 类 | 数 | 说明 |
|---|---|---|
| (a) 已登记-结构限制 | 1 | roster prompt-only 层级 |
| (b) 登记/机制在而仲裁缺陷 | 30 | 五个机制簇,见修复清单 |
| (c) 未登记 | ~18 | 待补方向条目(下表) |
| (d) 纯听错/ASR 限度 | ~14 | 只落台账,保向铁律 |
| 已有机制覆盖 | 1 | cue17=重述车道(本日已落库接线) |
| UNRESOLVED 待音频 | 3 | cue19/49 + margin 字段语义 |

说话人 28:假李豆沙 3(200736 cue23 语义走廊 1 + 抱团 cue33/37 混说尾段 2),
假连线其余;**混说 6 cue 机器单标签只盖真值 45.8% 文字,被吞侧 6/6 全是李豆沙,
且 `mixed_overlap_evidence` 全 null(重叠检测从未生效)**——pyannote 子窗判据齐。

## 通病修复清单(波 8 候选,每项独立 test-gated commit + 负向金丝雀)

1. **F1 转写回声环**(10 cue,南天簇):`final_review_auditor.py:1211-1256` 候选
   文本分级把同片转写里的重复错误当"绑定证据"自证;修=transcript_context 证据
   降权/禁自证 + 与 `entity_confusables.json` 已登记 group 交叉核对。
2. **F2 代词政策执行位**(11+4 cue):Ivan 2026-07-10 书面政策
   (`subtitle_correction_principles.md:66-68`)已入 prompt,但执行体
   `_cpa_pronoun_ta_pass`(`full_session_transcription.py:522-606,919-922`)是
   session 级单次调用且早于候选级 self-heal→静默漏;修=候选级代词一致性 pass
   或纳入 self-heal 审计面。声学证据:他们/她们 pinyin_similarity=1.0,纯文本政策车道。
3. **F4 语义走廊 margin 前置**(假李豆沙 cue23):`speaker_host_evidence.py:135-138`
   `campp_semantic_corroborated` 未查声学 margin 即扶 HOST。**е1 工程收窄**:
   Ivan 8/7 深夜裁定原文已限定「语义只在声学临界带内可佐证扶过线;声学缺席/反向
   时语义单独不得判李豆沙」——走廊现状超出裁定范围,收窄=回归原裁定,无需复裁。
4. **F5 重叠检测失效诊断**:`producer_speaker.py:576-589` 标志位从未置位
   (6/6 真实混说全 miss)→先诊断为何不触发,再定 pyannote 子窗接入位。
5. **F6 短句门假连线**:`speaker_finalizer.py:872-883,893-899` 把强 margin 短句
   也划入可否决池;用 61+40+12 句真值集重调。whole_clip_context 证据源错误率
   ~10% vs campp ~3%,兜底通道按证据源分层。
6. **F7 无声学参与的语境专断**(cue39/66):`missing_proposal_bootstrap.py:65-99,261-271`
   + `final_review_auditor.py:2260-2385` 语境改写未强制声学参与;修=非平凡改写
   必须有声学证人行,否则降 disclosure_only。
7. **F8 AGY 精听整段 UNAVAILABLE**(换身份片,RuntimeError):核对波 6
   provider_transient 分类是否覆盖此形状;不覆盖则补 typed retry。

## (c) 待登记条目(按保向铁律,只记方向)

由菜(误听:油菜/太菜/你看上)、Yuna/尤娜、南町nightin 规范写法(误听:南天/大白;
「大N老师」cue19 待卡2)、安晚(误听:啊/投啊)、邪恶大马头(误听:大码头/大马头像——
以 Ivan 真值「大马头」为准)、起码四五个人(误听:十五个人)、听李姐的(误听:让你理解下)
等——完整清单见各片报告 (c) 节;入 glossary 时逐条带 8/7 案出处。

## е2 决策卡(请 Ivan 一轮批完)

**卡 1|证人冲突加权**
- 原裁定:2026-07-27 成本令「闭集裁决归 CPA,音频只当耳朵」。代码位
  `acoustic_witness_adjudication.py:1019-1021` 注释原文:"AGY pinyin remains a
  diagnostic … but cannot overturn CPA's explicit PROPOSED choice"——**该注释未带
  Ivan 日期出处**(worker 初报的 7/04 经查不实,已更正;若 Ivan 记得另有出处请指正)。
- 反例:换身份片同一分支(`acoustic_witness_adjudication.py:1010-1022`
  `CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT`)4 处修对、6 处改错
  (cue2/14×2/53/97,报告有逐处回执引用);非 glossary 出处,cue59 收窄守卫不覆盖。
- 冲突本质:裁定给了 CPA 终裁权,但没规定「证人明确反对时」的加权/门槛。
- 选项:A 维持(接受 ~6/18 错改率) / B 收窄:证人明确反对时 PROPOSED 需
  结构化文本支持或已登记方向才可胜出(cue59 守卫思路推广到非 glossary 出处) /
  C 全部证人冲突案强制 disclosure_only 不改字。**建议 B。**

**卡 2|满席证人 vs 已登记专名边界**(cue19「大N老师/大白老师」+ cue49)
- 原裁定:授权保向铁律(裁定文档只记方向,逐处真伪由音频仲裁)。
- 反例:满席高置信证人战胜已登记专名候选,真值站证人一边还是专名一边未决。
- 需要:Ivan 对两处音频亲裁(报告给了精确时间窗),裁完落方向词条。

## 波 7 部署的 policy fingerprint 副作用(披露)

`producer_text_pipeline.py` 在 `audit_lidousha_review_package._audit_policy_fingerprint()`
的 25 模块名单里——波 7 起指纹已换,**8/7 批(及更早)所有 pre-deploy 冻结的
review-package audit 对新代码校验必然 mismatch**。这是已知结构债(07-31 旧节
队列#5,显式 policy 版本号方案未做)的正常表现,不是新 bug:统一重产会重出
audit 自然重冻;在那之前若上传闸/审计报 policy_fingerprint 失败,不要当新病诊断。

## 重述车道生产金丝雀预警

受骗片法证发现 cue17 两路独立听写收敛到同一错——统一重产时重述候选进
CURRENT/PROPOSED 裁决,若证人拒绝 PROPOSED,按设计稿 §2 补全政策复核
(前缀相容+残段无矛盾即可胜出);金丝雀结果无论向哪边都要记回设计稿。

## 8/8 深夜更正(Ivan 三条纠偏,罪证留原文不改上文)

1. **「(c) 未登记」名单大幅收缩——Ivan 质疑「crawler 不是该自动抓吗」经 roster
   快照核验成立**:8/7 生产时 `state/psplive_roster.json` 里 南町 条目完整
   (`canonical:南町, official_surface:南町Nightin, aliases:[Nightin,南町Nightin,大N老师]`)、
   安晚 条目完整(aliases 含 aWa 系)。故:
   - 南町nightin 规范写法、大N老师、安晚 **全部已登记**(cue34 安晚案改判
     (a) roster prompt-only 结构限制+召回缺口;cue19 本来就是「登记候选被满席
     证人击败」的 UNRESOLVED,归类不变);上文 (c) 表相应作废。
   - 真正词表外仅剩:**由菜/Yuna**(非 PSP 联动客——crawler 只爬 PSP roster,
     范围缺口不是登记疏漏)与**邪恶大马头**(游戏内 ID)。
2. **新修复项(自动化优先,呼应 Ivan「不该手动加」的一贯令)**:
   - **F9 游戏场画面读人名**(Ivan 8/8:「邪恶大马头应该是看画面看到的」):
     游戏语境 RESOLVED 的场,抽帧读游戏内玩家名牌/结算名单(复用
     screen_read_witness 歌单 OCR 机制),产 session 玩家名候选(带 screen-read
     provenance 的结构化支持)进既有候选通道。游戏内 ID 的正确写法本来就只
     存在于画面上,音频/roster 都不可能有。
   - **F10 crawler 范围扩展**:联动场非 PSP 参与者(由菜/紫妍/天云海类)进
     社区称呼 crawler 的抓取面(以联动台账/弹幕/标题为种子,https 来源纪律
     沿用);在此之前这类名字仍需 Ivan 逐案裁定写法。
3. **贪生怕死 auto_223750_913_1322 更正为「未审阅」**(Ivan 8/8:「根本没有分
   人声所以我没有审阅」):真值工件 authority 已改
   `pristine-unreviewed-20260808`,零 diff **不代表机器全对**,此前「0+0/机器全对」
   表述作废;说话人二分版重产后 Ivan 再审,那才是该片的真值轮。

## 8/8 深夜更正二(Ivan 追加三条)

1. **卡 2 撤回**:cue19「大N老师」/cue49「家人们」Ivan 在标注稿里已经亲裁过
   ——他的改稿就是裁决,「待音频亲裁」框架错误。方向词条已按其真值落库
   (854487e:南天/大白老师 入 entity_confusables 南町组 + glossary 注记 +
   指纹测试按维护惯例更新)。cue19/49 同时成为「满席证人也会错」的登记例证,
   反哺卡 1 建议 B(证人非无谬,登记方向应保留胜诉能力)。
2. **crawler 根因确认(Ivan:由菜Yuna 是 PSP 官方成员)**:roster 唯一源=
   `psplive_roster_sources.v1.json` 里单支视频 BV1GTFseLESN 简介的「参与人员」
   名单——**成员名录被停格在那支视频的发布日**,之后加入/未参演者永远进不来
   (快照 26 人无由菜)。F10 改写为 **crawler 修复**:补充更新的官方源(sources
   数组本就支持多源并集)或改爬官方空间成员合集;验收标志物=由菜Yuna 出现在
   `crawl_psplive_roster.py --write` 输出且 minimum_participants 门不降。
3. **F9 出处补注**:「CPA 看画面」是 Ivan 既有裁定不是新提案
   (ivan-requirements-ledger-2026-07-26.md:134 07-15 分工雏形、:139 07-24~25
   正式重申"第二次");游戏内人名画面读取=该分工在游戏场的落实,优先级按
   「说过很多次」上调,与 F5/pyannote 同批考虑。

## 卡 1 结案(Ivan 2026-08-08 深夜裁定,原话为准)

Ivan 原话要点:AGY 盲听会「音对但完全不通顺」(连他自己标的「我包是的」都疑似
纯听音写字);裁判强行改正也是缺点;要更平衡——**「裁判的判决需要尽可能的贴近
听音,但如果无论如何都没有办法给出合适的符合语境的改写,那就可以按第三方证据
反对耳朵,以裁判为主」**。

即:**贴音优先,证据兜底**。(1) CURRENT/PROPOSED 裁决时裁判负有贴音义务——
优先在与证人听写拼音相容的候选空间内找符合语境的写法;(2) 只有贴音空间内
无解时,才允许背离耳朵,且必须携带第三方结构化证据(登记方向/弹幕/SC/重述/
exact-cue),无证据不得改;(3) 满席证人也会错(cue19/49 登记例证),故不采
选项 C 的一刀切。

历史脉络(第六问出处链):07-25 声学层降级证人架构(R-架构-06,理由原文
「AGY/Gemini 自己会听错」)→ 07-27 成本令重申+刘若莎案 R-裁定-06(自不一致
证人无否决权)→ 8/8 cue59 守卫收窄(词表侧三逃生口)→ 本裁定把同一哲学
推广到全部证人冲突面。**落地=波 8 F3**:裁决 prompt 增贴音义务与拼音相容度
排序;`CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT` 分支加结构化证据门
(复用 cue59 守卫的三逃生口判定),无证据时降 disclosure_only;负向金丝雀=
换身份 cue2/53/97 三个真实错改案回归(裁判无证据背离耳朵必须被拦)。
