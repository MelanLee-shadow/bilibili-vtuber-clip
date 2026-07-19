# 流水线分步权威索引（Ivan 2026-07-19 定）

**本目录是切片流水线的分步文档权威。** 结构规则：

1. **每一步的规则只写在该步的 step 文件里**（或 step 文件明确指向的更强权威：代码 schema、profile 资产、项目 skill）。
2. **其他任何文档（AGENTS.md、HANDOFF、workflow 文档、skill、memory）只允许放指针，不允许复制规则正文。** 复制即债——2026-07-19 歌切标题事故的根因就是同一规则散落 4+ 处、改了一处漏三处。
3. **进行到某一步时只读该步文件**；总索引（本文件）只是指针表。
4. 改某步规则 = 改对应 step 文件 + 它指向的代码/资产强制层；不需要全局扫描。
5. 新纠偏落地顺序：先落**代码强制层**（schema 校验/choke point/测试），再改 step 文件，最后确认没有别处复制过旧规则。

| 步 | 文件 | 职责 | 代码入口 |
|---|---|---|---|
| 10 | [10-source-recording.md](10-source-recording.md) | 录制、源健康、mount 看门狗 | `scripts/free_session_autoslice.py`（源门）、`src/autoslice/source_integrity.py` |
| 20 | [20-selection.md](20-selection.md) | 候选召回、选题 metric、语义审查、**同主题合并** | `src/autoslice/semantic_candidate_selector.py`、`full_session_candidate_selector.py`、`scripts/cpa_semantic_qa_llm.py` |
| 30 | [30-boundary.md](30-boundary.md) | 边界解析、源语境扩窗 | `src/autoslice/boundary_resolver.py`、`source_context_planner.py`、`live_source_review.py` |
| 40 | [40-subtitle-text.md](40-subtitle-text.md) | 字幕文本链：ASR→专名→弹幕→语义修复→终审 | `src/autoslice/producer_text_pipeline.py` |
| 50 | [50-song-lane.md](50-song-lane.md) | 歌切专线：识别、LRC 对齐、host-vocal 证明、完整性 | `src/autoslice/song_lane.py`、`song_alignment.py`、`song_completion.py` |
| 60 | [60-title.md](60-title.md) | 标题（谈话 + 歌切铁律） | `src/autoslice/title_policy.py`、`publish_staging.py` |
| 70 | [70-cover.md](70-cover.md) | 封面生成与验字形 | `src/autoslice/cover_generation.py` |
| 80 | [80-package-delivery.md](80-package-delivery.md) | 打包、片头、hash 绑定、审计 | `src/autoslice/producer_package_finalization.py`、`branding_intro.py`、`scripts/audit_lidousha_review_package.py` |
| 90 | [90-publish.md](90-publish.md) | 授权上传、tag、合集、公开验证 | `.agent/skills/bilive-autoslice-publish/SKILL.md`（该步权威在 skill） |
