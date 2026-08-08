# 会话内重述修复(restatement repair):设计稿 + 探针证据

日期:2026-08-08 · 需求出处:Ivan 当日原话——被打断/语速快导致 ASR 听不清的句子,
李豆沙稍后会慢速完整重说一遍,「就可以根据后面的字幕来修复前面的字幕」。
旗舰案例:auto_200736_298_383 cue17(成品约44s,srt 36.3s)乱码
「这是我的小孩就是了」← cue29(srt 58.9s)完整重述「这是我今天的宣言」。
状态:**检测器已落库**(`src/autoslice/restatement_recall.py`+探针
`scripts/probe_restatement_recall.py`),**管线接线未做**(见 §4)。

## 1. 机制定位:第四类结构化文本支持

与念弹幕车道(lidousha-read-aloud-danmu-first-cpa)同构:会话内部逐字文本
作候选先验,不是模型想象。信任等级对齐收窄后 cue59 守卫
(`final_review_auditor.py` d0b6099)的三逃生口:重述候选属**结构化文本支持**
(会话内 sha 可绑定的另一 cue 原文),而非裸会话候选(游戏/主题词那类)。

**绝不直接改字**:检测只产生配对提案,改字必须过既有候选盲声学仲裁
(entity_verdicts 内容寻址缓存,重试轮零重复请求)。

## 2. 修复条件(来自真值数据的关键裁定)

8/7 真值给出成对反例,修复条件必须同时满足:

- **当前文字与音频不相容**(乱码实锤),且
- **重述文本(或其前缀)与音频相容**。

证据:Ivan 对 cue76「我肯定是杀」(忠实转写的真实重启,4s 后完整重说)
**零改动**——忠实片段不是病;对 cue17(乱码)**要修**。
→ 忠实重启天然被证人挡下(当前文字已与音频相容,无需修),无需启发式区分。

**补全政策**:cue17 音频里「宣言」尾部被打断/重叠,纯声学只能证到前缀
「这就是我今」。Ivan 明示该案要修成完整句。故:证人确认(a)当前文字不相容
(b)重述前缀相容(≥3 内容音节)且残段无矛盾音节时,**允许以重述全文补全**,
receipt 标注 `completed_from_restatement`(来源 cue、相似度、前缀长)。
这是对「字幕必须逐字忠实音频」的一个 Ivan 授权例外,范围仅限重述车道。

## 3. 检测器与探针证据(已实证)

拼音(无调)相似度 + 连接词剥离后的内容前缀锚。8/7 五候选(408 cue)全景:

- 生产默认 `sim≥0.45 ∧ prefix_run≥3 ∧ gap≤90s ∧ 早cue≥4字`:**4 提案/5 片**
  = cue17→29(真阳性)+ cue76→79(忠实重启,证人零改动)+ cue51→98、
  cue100→110(实体词同音锚,证人必拒:尾部音节全异)。**零错修面**。
- 假阳性防线实测:cue6/7「我是」类结巴被 4 字门挡;「然后/但是」假前缀锚
  被连接词剥离清空(剥离前 7 对里 4 对是纯连接词锚)。
- 成本:每片 ≈1-4 次声学仲裁,全部走内容寻址缓存(7/27 成本令兼容)。
- 重述侧限定主播(李豆沙 label;她直采麦 ASR 可靠),早 cue **不做**标签
  前置过滤——cue17 机器标签是连线,标签本身就是病灶的一部分。

## 4. 接线方案(下一步,按仓修复纪律)

1. **产线检测点**:produce 链 speaker-final 定稿后、final review 前,对 cue 集跑
   `find_restatement_pairs`,落 receipt(如 `<cid>.restatement-candidates.json`,
   含 pair + 双侧文本 sha)。
2. **提案通道**:确定性提案(不依赖 CPA 从 prompt 里"注意到"):每对生成
   candidate_provenance `kind="session_restatement"`(source_cue/similarity/
   prefix_run/late_text_sha),入既有 replace_cue 仲裁队列,声学证人按 §2 两条件
   判,CPA 语义终审照旧。
3. **守卫接线**:~~增认 session_restatement~~ **经查证不需要(2026-08-08 落地时结论)**:
   收窄后 cue59 守卫只拦 `candidate_provenance.kind=="glossary"` 的裸候选;
   `session_restatement` 是独立 kind,走标准候选盲声学 + CPA CURRENT/PROPOSED
   闭集裁决,不经过该守卫。`final_review_auditor.py` 零改动(债务模块免动)。
   若未来观察到重述候选被某个门错拦,再按该门自己的语义开支持臂。
4. **测试 + 负向金丝雀**:cue17→29 事实模式端到端(提案生成→守卫放行);
   revert 守卫接线该测试必须变红;cue76 忠实重启零改动回归;
   min_early_chars 挡「我是」回归。
5. **边界(v1 不做)**:跨切片窗口外重述(全场转写检索)、连线侧重述、
   混说 cue 的说话人子段修复(归 pyannote 子窗 backlog——cue17 的 label
   修复走说话人车道,本机制只管文字)。

## 5. 探针复现

```bash
python3 scripts/probe_restatement_recall.py   # 只读;默认读本机法证归档 pristine + truth 工件
```
