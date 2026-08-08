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
- 原裁定:2026-07-04「封面/文字终裁归 CPA,AGY 只当耳朵」(cpa-real-ai-cover /
  裁决架构系列裁定;7/27 成本令沿用:闭集裁决归 CPA、音频只当耳朵)。
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

## 重述车道生产金丝雀预警

受骗片法证发现 cue17 两路独立听写收敛到同一错——统一重产时重述候选进
CURRENT/PROPOSED 裁决,若证人拒绝 PROPOSED,按设计稿 §2 补全政策复核
(前缀相容+残段无矛盾即可胜出);金丝雀结果无论向哪边都要记回设计稿。
