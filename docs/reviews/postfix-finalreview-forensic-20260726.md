# post-fix 终审拦截 forensic — 2026-07-26

来源：`ssh free`只读读取 `state/2026-07-26.json`、`state/2026-07-24.json`、`out/<date>/<cid>/*.chat-authority.json`、
`out/<date>/<cid>/entity_verdicts/<req_sha20>/{prompt,verdict}*`、`logs/runner.log`；本地权威读
`src/autoslice/{entity_audio_verifier,acoustic_witness_adjudication}.py`（HEAD=c38fa49，已含 e187a22）。
未改任何文件。

## 结论

**e187a22 本身修复有效**（生产已验证：2026-07-24 当日 talk 0/10→4/10 交付，provider 问题之外的历史死锁全部打开）。
`7/26 新场次 9/10 rejected` 不是"post-fix 新证据"——**全部是部署前的化石**，未被重跑，混进了"仍在拦"的统计。
唯一确认的 post-fix 标本 `auto_183122_1209_1410` 揭示了**真实的第二病灶**：Phase 1 声学证人在音频窗口
（宽 context crop + 窄 target 标记）短于～2s 时经常把听写内容溢出到标记边界之外，代码没有任何"听写音节数
是否物理可能"的校验，把溢出污染的拼音直接喂给 CPA judge；judge 依照其 prompt 规则如实判定"与两候选均冲突"
→ UNCERTAIN，而规则4又禁止用强语境佐证（下一句原文已重复正确实体）兜底，链条在 judge 这一步合规但错误地
中止。

## 1. 时间线确认

| 候选 | 日期 | 产出 mtime (UTC) | 部署边界 | 结论 |
|---|---|---|---|---|
| auto_152944_1525_1593 | 07-26 | 09:29:49 | e187a22 commit 11:21:24Z / md5 verified ~11:45Z | **pre-fix 化石**，未重跑 |
| auto_142942_1329_1380 | 07-26 | 09:27:52 | 同上 | **pre-fix 化石**，未重跑 |
| auto_183122_1209_1410 | 07-24（复活重产） | 11:54:41 | 同上 | **post-fix**（时间+内容双重确认，见下） |

`runner.log` 证实 07-26 全部 8 个 `FINAL_REVIEW_UNRESOLVED_FINDINGS` 候选都产于 09:20–10:53Z（早于部署），
且 `failure_recoverable:false` 令其成为化石态，普通 cron tick 不会重跑（只有 `auto_145940_70_129`
recoverable=true 被反复重试）。09:20 那批本身就是 blocked-findings-20260726.md 诊断的旧
`syllable_count==len(tokens)` 严格相等 bug 的复现（抽查 auto_142942_1329_1380 cue7/8：
`reason_code=WITNESS_REPORT_INVALID`，且 verdict 里**没有** `self_count_mismatch` 键——post-fix 代码
无条件写这个键，缺失即代码版本证据，不只是 mtime 推断）。

`auto_183122_1209_1410`：`runner.log` 11:30:51 开始 producing、11:54:41 chat-authority 落盘，晚于部署；
更硬的证据是内容本身——两条 finding 的 witness verdict 都带 `self_count_mismatch:true`（e187a22 新字段），
证明这条产出确实跑在 post-fix 代码上。

## 2. auto_183122_1209_1410 证据链

`final_review_audit`: `status=FLAGGED release_gate=BLOCK validated_finding_count=2 resolved_findings=0`；
`discovery={raw_validated_finding_count:2, resolved_finding_count:0, status:COMPLETE}`；
`correction_pass.applied_count=0`（该阶段扫的是另一批 cue 15/16/40/41/53/66/74/83，与本次 2 个 finding 无关，
非 carryover 干扰）；`correction_mutation_authority.applied_count=0`。两条 finding：

| cue | 现文本→提案 | witness.status | witness.self_count_mismatch | judge.choice | judge.reason（节录） |
|---|---|---|---|---|---|
| 11 | 林墨→礼墨（glossary，下一句原文已是"礼墨"） | OBSERVED (conf 0.95) | true | **UNCERTAIN** | "ling dian 无法支持林墨或礼墨" |
| 35 | 桃林→唐琳韵（structured_chat sender） | OBSERVED (conf 0.95) | true | **UNCERTAIN** | "14音节，与两候选均明显冲突" |

链条定位：witness UNAVAILABLE？否（OBSERVED）。judge 没跑？否（`status:JUDGED`）。judge 选了
CURRENT/PROPOSED 但 apply 层丢失？否（judge 本身就选了 UNCERTAIN，从未进入 apply）。carryover sidecar
干扰？否（未找到相关 sidecar 文件，且 correction_pass 覆盖的是不同 cue 集合）。**真实断点：judge 对两条
都判 UNCERTAIN，且判断依据的听写拼音本身已被污染。**

### 污染证据（可复算）

对 1209_1410 全部 11 个含 timeline 的 witness 回执按"听写音节数 / 目标区间时长"算速率：中位数 4.5 音节/秒
（正常语速），但两个被拦 finding 恰是异常值：

- cue35（桃林/唐琳韵）：目标区间仅 89960–91080ms（1.12s），`heard_pinyin` 却有 **14 音节**（自报 11，
  仍矛盾）——1.12 秒内说出 14 个汉语音节（12.5 音节/秒）物理上不可能，`prompt.gemini-api.md` 已经明确写
  "The target interval occupies 3180 ms through 4300 ms" 且要求"never merge its syllables into the
  target report"，证人仍越界听写，多出的音节能在 `context_after`（"感觉豆沙用礼墨的麦更加吵闹"）里找到
  部分对应片段（"gan jue dou...yong li"）。
- cue11（林墨/礼墨）：目标区间 27690–30250ms（2.56s），11 音节=4.3 音节/秒，速率本身不算异常，但内容比对
  显示开头 "zhe dui ma"（这对吗)"与目标句无关，疑似混入 `context_before`（"神了，这对吗"）尾部或边界
  抖动；核心分歧音节 "ling dian" 与"lin mo"/"li mo"（林墨/礼墨）本身也确实存在合理声学模糊。

代码里**唯一**对 witness 自洽性的校验是 `self_count_mismatch`（自报数 vs 实际 token 数），没有任何校验
比对"听写音节数"与"目标区间时长"是否物理相容——cue35 这种量级的越界本该在这里被拦截、打上污染标记或
触发收窄重裁，而不是原样以 `status:OBSERVED, confidence:0.95` 送进 judge。

judge 的 prompt（`_JUDGE_PROMPT`）规则 2"与听写音节明显冲突的候选不能当选"+规则4"目标区间外的相同词语
出现过，不构成目标区间内说过它的证据"，在**干净听写**场景下是对的（防止被语境牵着走）；但当听写本身混入了
目标区间外的音节时，judge 没有任何信号提示"这份听写可能不纯"，只能如实按冲突判 UNCERTAIN——cue11 的
"下一句已经写明礼墨"这类强语境佐证因此被规则4挡在门外，即使 Ivan 此前对同一 cue 的裁定
（blocked-findings-20260726.md）已经认定"证据充分"。

## 3. runner.log / 排他检查

11:45Z 之后到 17:00Z 之间的 runner.log：无 Python 异常/Traceback，仅正常的 `producing` / `batch
finished` / `terminal state retry_wait` 行；1209_1410 专属日志里 11:45 前的重试确实出现过
`FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED`（provider 配额问题，cue 16/40/66/67/75/76，与本报告的
cue11/35 无关），但**最终**那一轮（11:54:41 落盘）该批 cue 已全部消化，只剩 cue11/35 两条真实
judge-UNCERTAIN。未发现 carryover sidecar 文件。

## 4. 代码病灶与修法建议（未改代码）

- **主病灶**：`src/autoslice/entity_audio_verifier.py:745`（`self_count_mismatch = report_valid and
  syllable_count != len(tokens)`）—— 此处已拿到 `timeline_binding`（含
  `delivery_local.target_start_ms/target_end_ms`，函数参数第 770-772 行已在用）却没有用它换算
  目标区间时长，去核对 `len(tokens)` 是否落在合理语速区间（如 1.5–8 音节/秒，留足余量）。建议在这里新增
  一个"越界疑似"标记（比如 `scope_bleed_suspected`），命中就和 `self_count_mismatch` 一样**披露不致命**，
  但要让下游知道。
- **次要病灶**：`src/autoslice/acoustic_witness_adjudication.py` 的 `_JUDGE_PROMPT`（128-160 行，规则2/4
  在 137/159 行附近）没有分支处理"witness 疑似越界"的情况——一旦主病灶加上污染标记，这里需要相应放宽：
  污染时允许规则3的语境裁决介入，而不是被规则2/4 一刀切成冲突。
- 两处都是 Ivan 架构层面的取舍（听写有效性边界、judge 何时可信语境），建议列入下一轮裁定，不建议我在
  forensic 之外顺手改。

## 5. 范围外/已知不相关问题（未深入）

2026-07-24 当日另外 4 个"failed"分别是 2×`content_boundary`（跨 segment witness reserve，HANDOFF 已知
backlog）、2×`FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED`（`foreign_span_witness.py` 阈值问题，
blocked-findings-20260726.md 已记录）、1×`CHAT_AUTHORITY_FINAL_ARTIFACT_FAILED`（疑似 CloudFS 挂载瞬死，
HANDOFF 已知）——均与本次声学证人/judge 链路无关，未纳入本报告根因判定。
