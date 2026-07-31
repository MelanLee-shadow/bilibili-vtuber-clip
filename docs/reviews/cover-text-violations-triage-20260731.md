# 封面文字违例分诊单（2026-07-31）

状态：`TRIAGE / NO_ACTION`
范围：`free:/opt/bilive/autoslice/out/2026-07-2*` 全部 30 条有 `rendered_lines` 的成品
判据：新落地的确定性门 `cover_thumbnail_lines_are_readable`（≤2 物理行 且 每行 ≤9em）

本单只做分诊，**不授权任何修复**。已发布稿的封面修复按 `docs/pipeline/90-publish.md`
准入第一条，必须 Ivan 逐条点名授权；`docs/pipeline/70-cover.md:190-195` 也要求
published 候选排除在 generic maintenance 之外。

## 离线回归结果

| | 条数 |
|---|---|
| 新门放行（合规，零回归）| 24 |
| 新门拦截 | 6 |

6 条拦截**全部**是 `cover_text_mode=full`、非歌切、无 full-text contract。
`cover_text_mode=punch` 的成品零违例。

## 根因（两个因素联乘，缺一不会发生）

1. **`mode=full` 自动回退存在**：梗字选择器空手时，把整段 `cover_text` 交给宽度平衡器
   铺版。`docs/pipeline/70-cover.md:44-45` 早已明令「不得因回执为空或回执失败而把长
   `cover_text` 当作封面放行」，但该政策此前没有机器实现。
2. **layout 的 `max_lines` 排版预算与缩略图合同从未对账**：
   `cover_generation.py` 的 `left-split`/`right-split` 给 8 行、`banner` 给 4 行，
   注释写明设计意图是「更多行 ⇒ 更短的行 ⇒ 窄区里字更大」。punch 路径靠段落 1:1
   侥幸绕开了它，full 路径原样继承 8 行预算。

第三个放大器：即使给了显式 `\n`，宽度平衡器仍作为候选参与竞争，字号占优就能夺走切点，
因此会切出劈开词的分行（`表情小 / 李拒绝花钱` 形态）。

三处均已修复（见同日 commit）；**新产出自修复起受保护，本单只处理存量**。

## 分诊队列（按 行数严重度 × 流量窗口 排序）

| # | candidate | 日期 | 行数 | BV | 登记状态 | 备注 |
|---|---|---|---|---|---|---|
| 1 | `auto_192000_909_1014` | 07-25 | 5 | `BV1s7326qEc9` | published | **白色奶龙。Ivan 已就封面内容授权过一次修复（7/30）。建议作为 `same_bv_cover_repair` lane 的首次真实执行金丝雀** |
| 2 | `auto_193515_1406_1543` | 07-22 | 8 | — | 未登记 | 最严重；`弹幕要求/给左边的人/一个/脑瓜崩，镜面/…` |
| 3 | `auto_193515_233_361` | 07-22 | 7 | — | 未登记 | 劈词：`吃过一次没|熟豆角仍敢再|次尝试` |
| 4 | `auto_142942_496_618` | 07-26 | 6 | `BV1zk386LEjC` | published | **已做过完整同 BV 视频置换**，封面修复必须带 `--predecessor-completed`（`90-publish.md:146-150`），否则会被正确拒绝但无法继续 |
| 5 | `auto_193515_1569_1672` | 07-22 | 6 | — | 未登记 | |
| 6 | `auto_193515_672_909` | 07-22 | 3 | — | 未登记 | 最轻；3 行但第一行 12 字超 9em——是「行数合规也可能违约」的第二形态 |

## 需要 Ivan 定夺的两点

1. **4 条 `auto_193515_*`（07-22）都不在 `assets/lidousha/publication_registry.v1.json` 里。**
   同期存在 `reports/authorized_uploads/2026-07-22-*` 三个目录，所以 7/22 确有发布活动。
   这几条是未发布、还是发布于登记表建立之前，**本单未核实**——动它们之前必须先读
   Creator/public fresh-live 回执确认，不能按未登记就当没发。
2. 07-22 的三条推荐流量窗基本已过，修复价值偏档案质量；是否值得占用配额与 CPA 额度，
   由 Ivan 决定。**只做他点名的**。
