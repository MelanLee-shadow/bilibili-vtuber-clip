# 频道资产：知识、示例与运行记录

`assets/_template/` 提供首次提问、写法建议和可替换的框架；`assets/lidousha/`
保留李豆沙频道的真实知识与判例，供理解规则的颗粒度。它们都是项目资产，不能因为
内容详细或来自真实频道就一并清空。配置入口见 [profiles/README.md](../profiles/README.md)
和 [模板说明](../assets/_template/README.md)。

## 八、九月的开发落在哪里

文件的最后日期不代表整个资产系统停止更新。2026 年八、九月的维护源记录包括：

| 内容 | 知识或通用机制的落点 |
|---|---|
| 8 月：南町的“南天/大白老师”误听、白色奶龙梗一族、狍哥与马有利等别名 | `glossary.txt`、`entity_confusables.json`、`psplive_roster.v1.md`；误听面只提高候选优先级，逐处仍需证据 |
| 8 月：歌名和内容 IP 标签规则 | `upload_tag_policy.json`，以及 [歌切](pipeline/50-song-lane.md)、[标题](pipeline/60-title.md) 中的语义校验 |
| 8 月：梗字可承接标题之外的解释与补充 | 示例与模板的 `title_style.md` 对齐现有生成 prompt 和 `cover_punch_semantics.py`；具体指涉仍需事实支持，不要求连续子串 |
| 8 月：社区实体与关系发现、说话人影子研究 | `community_name_sources.v1.json`、`community_names.v1.json`、`streamer_registry.v1.json`、`speaker_scmc_v0_spec.json`；存在性、研究配置不等于本句出现或发布身份 |
| 9 月：当场外观优先、保留源图实际头戴物 | `profiles/lidousha/profile.json` 的 `identity.cover_identity`、`cover_identity_prompt.txt`、`persona.md` 和模板真例保持一致 |
| 9 月：标题审定范围、已审原稿的定点修复、明确时间域 | `title_style.md`、`subtitle_correction_principles.md`，完整绑定与复核在 [字幕步骤](pipeline/40-subtitle-text.md) 和 [打包步骤](pipeline/80-package-delivery.md) |
| 后续选题校准：普通可爱互动可以竞争，但最高级观众驱动表演需要完整强落点 | 示例与模板的 `slice_selection_metric.md` 保留 v6 规则；固定算术与锚点仍由 `selection_score_calibration.v1.json` 配合代码消费 |

人设性格和基础字幕原则在维护源中最后大幅更新于 7 月，本身不构成过时证据。
本次恢复保留原有细节，并补齐已经由当前 profile 和分步规则确认的当场外观、定点修复
及时间域要求，同时校正梗字连续子串与手定标题全文封面的旧泛化；历史全文判例保留在
独立全文合同范围内。没有凭日期编造新的性格或词条。

## 不同资产怎样复用

- **频道知识与智力劳动**：词表、误听方向、人设、标题风格、选择量化与校准、平台礼物名、
  详细判例和配置问卷保留。新频道先问主人，再替换频道事实；不能把李豆沙的专名套到别人身上。
- **完整格式示例**：片头配置及其历史渲染记录、
  [带标注的终审合同](../assets/lidousha/final_media_review_contracts.example.v1.json)
  用于理解结构。示例不提供媒体或执行授权，运行合同中不能带 `_example` 等说明字段。
- **候选专属记录**：某一段录像的字幕真值、精确源窗、封面修复与同 BV 发布回执绑定该候选
  和实际字节。其通用方法已写入分步文档，不能把它们复制为新用户的真实授权或当前 PASS。
  `subtitle_truth_ledger`、`publication_registry` 等公开文件提供结构，由使用者自己的运行积累。
- **时效与在线状态**：社区爬虫结果、时效实体、已发布歌曲等需要真实来源和有效期，公开
  空状态不证明不存在，也不代表已经刷新；按自己的频道运行对应采集入口。
- **凭据与声纹**：由使用者自行配置，不随示例分发。

## 后续同步纪律

先对照具体文件与消费位置，区分知识、说明示例和可执行状态，再逐项同步。不能把
“排除运营记录”扩大成删除频道知识、首次问卷或维护者判例。重新导出时必须检查实际 diff，
保留已经修正的模板示例和通用沉淀，不能用旧模板覆盖它们；删改详细资产须按
[AGENTS.md](../AGENTS.md) 在 PR 说明具体理由。

外观事实在 profile、persona 和 cover identity prompt 三处同步；字幕原则与模板同步；
选题定义与校准资产一起核查。配置 READY、离线测试通过和示例完整分别证明自己的范围，
不代替真实音频、最终像素、服务或发布验收。
