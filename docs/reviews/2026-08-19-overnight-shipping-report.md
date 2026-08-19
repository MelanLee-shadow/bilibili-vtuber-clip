# 2026-08-19 通宵出货战报（Ivan 晨读）

> Ivan 8/19 睡前令：「今天晚上把我授权的快车道全部上传，不过优先七夕」「其余的慢慢修复」。
> 本文件边跑边写。执行权威 = `docs/reviews/2026-08-19-ivan-review-batch-rulings.md`。

## 一句话现状

**歌切已发出去（泡沫 BV1Cn8E6iEf8 已确认公开，星猫上传中）；七夕卡在一道由「翻回 uniform_host」
引出的系统性门缺口上，根因已定位、修复在验证，门一通就出包上传。**

## 已发布

| 成品 | BV | 时间 | 备注 |
|---|---|---|---|
| 《泡沫 Bubble》歌切（8/17） | **BV1Cn8E6iEf8** | 2026-08-19 06:47Z | 审计 PASS + CPA 视觉 QC 全绿 + 出版对账完成 |
| 《Bonus:快乐星猫》歌切（8/15） | 上传执行中 | — | 审计/QC/manifest 全部就绪 |

《园游会》（8/14）：批次 refresh 处 RETRY_WAIT 良性暂态，等其两条待产候选产出即自愈解锁，
**不做手术**（与 8/15 的非自愈误判不同）。

## 今晚的关键发现（两个真根因）

### 1. uniform_host × 受话人归属 = 全批终态拒（**七夕/图书馆/8-09 批的共同死因**）

- 现象：每条重产候选都死在 `source_fact_repair/story_contract`，日志
  `SOURCE_FACT_REVIEW_UNRESOLVED: CPA_TEXT_REVIEW_INVALID`。
- 实证：publish 草稿里 `source_fact_review.passes[0].addressee_attribution` 每条断言都是
  `verdict=UNVERIFIABLE / actual_speaker=未提供`，判者理由逐字：
  **「本片没有可信的说话人标签转写，无法核验受话人归属。」**
- 根因：8/18 按 Ivan 裁定把 speaker 模式翻回 **uniform_host**（单人直播默认），而该模式
  **按设计从不跑 speaker finalization、不产测量型标签**；source-fact 的受话人子检查却硬要它，
  于是恒判无效 → 标题权威 BLOCKED → 候选终态拒。仓内本来就有
  `addressee_attribution.ABSENCE_POLICY_ID = "speaker_mode_uniform_host/v1"` 缺席策略，
  **只是没接到 source_fact_review 这条链上**。
- 状态：worker 修复中（严格不放宽 auto/required 多人合同，保留负向金丝雀）。
- 影响面：七夕、图书馆、8/09 全批、以及后续所有 uniform_host 产出。

### 2. 语义评分卡刷新把「排队等重产」误判成「输入不可用」并整批 BLOCK（不自愈）

- 8/15 整批被判 `semantic_chat_scorecard_refresh_blocked`，连累同批歌切出不了评审包。
- 根因：我复活的「考试写解」`auto_130012_435_574` 已进 `pending_talk` 等重产（尚无评分卡可刷新），
  刷新模块对它报 INPUT_UNAVAILABLE → 整批 BLOCKED，且该 block 每 tick 重复、不会自愈。
- 今晚处置：把它从该日 grant 点名中摘除（**它仍在队列、会被照常重产**），批次恢复可评审，
  星猫因此得以出包。→ **待修**：刷新模块应把「尚未产出的排队项」判为 deferred 而非 blocked
  （8/14 同场景就正确地判了 deferred）。

## 今晚其它已落地

- **七夕字幕订正已生效**：0:14《ぶらどらぶ》(VLAD LOVE，Gemini 3.6 听写确认；原「再见菈菈」是误听)、
  2:39「播的有点压抑了」、cue59 误附剥离 → 物化为 operator reviewed baseline 并部署（6f49e89）。
  重产实证：字幕面已 `OPERATOR_TEXT_FULL_OWNERSHIP`、review flags CLEAN，字幕门已被彻底打穿。
- **8/17 六条全部重产**（scoped run，并发 2，护住同时进行的直播录制；oci3 平行录制作保险）：
  女友感 → review_ready；今天吃什么 / 脱口秀 → 封面阶段；三年没见 → 封面路由可恢复失败；
  七夕 / 图书馆 → 卡在上述受话人门。
- **GitHub 公开库**：`ed445b6` → `89a7b91`（CI 修复）已推送，**CI 现已 success**。
  本轮后续私库改动在公开面零 delta（审阅真值类资产按设计不出库，泄漏扫描 0 命中）。
- **弹幕引用保真三连修**已合入（置信度反转根修 / 「；；」专名保真 / chat-authority owned-interval
  防回改），待部署。
- **QC 歌包布局适配**已合入并**已用于今晚两首歌**（以修复版脚本在临时路径生成回执——回执只绑
  标题/封面字节，不绑脚本自身哈希，故合法且与部署后一致）。
- **部署门实证**：`deploy_free_autoslice.sh` 要求录制器干净空闲，直播期间拒绝换装（设计内）。
  故今晚的部署要等她下播；歌切上传不受影响（走已部署的 authorized_upload）。

## 需要你拍板 / 知会

1. **园游会**等 8/14 自愈后我会自动发。
2. **七夕**：受话人门修复通过后我立刻重产并上传（今晚若她下播允许部署就今晚完成）。
3. 8/19 起 free 的 speaker 模式已是 uniform_host；auto 三级梯子未删，只是默认翻回。
4. 门校准清单又添两条（受话人门、评分卡刷新 deferred/blocked 判别），与你 12 问里的
   「为什么好片被终审门杀掉」同族，建议作为下一个大修方向。
