# Profile 耦合清单 — 换频道时代码里还剩什么

本仓的目标是「换频道 = 换 profile + 换词表，管线代码不动」。大部分链路已经
做到（identity/资产/标题前缀/说话人标签走 `channel_profile.py`），但仍有一批
点位绑定默认 profile（李豆沙）。**本文件是这些剩余耦合点的诚实清单**——发布
版刻意不顺手改掉它们：其中 prompt 类改动会改变发给 LLM 的字节，等于改变裁决
行为并使内容寻址缓存失效，必须由维护者按变更纪律（金丝雀 + step 文档）处理。

行号随版本漂移，索引以模块+符号为准。

## A. LLM prompt 内嵌默认频道身份（改动=改行为，须走变更纪律）

| 位置 | 内容 |
|---|---|
| `src/autoslice/cover_host_identity_gate.py` `_QUESTION` | 封面终检 prompt 描述李豆沙外貌（白发/熊猫帽）并点名嘉宾；verdict 字段名（`source_lidousha_located` 等）是持久 schema |
| `src/autoslice/cover_source_composition.py` `_QUESTION_PREFIX` | 源构图检查 prompt 同上 |
| `src/autoslice/cover_generation.py`（多人消歧/场景道具规则） | prompt 内嵌「labelled 李豆沙 / panda-hood」与「白色奶龙」梗解释 |
| `src/autoslice/publish_staging.py`（AI 封面重试 prompt） | 「the person labelled 李豆沙 … Make Li Dousha a LARGE …」 |
| `src/autoslice/cover_punch_semantics.py` | 裁决 persona：「你是李豆沙切片封面的最终文字语义裁决者」 |
| `src/autoslice/final_review_auditor.py` `_AUDIT_PROMPT` | 「你是李豆沙切片的终审审片员…自称专名…」 |
| `src/autoslice/source_fact_review.py` | 「你是李豆沙切片派生文案的 source-fact 最终裁决者」 |
| `src/autoslice/semantic_candidate_selector.py` + `selection_scorecard.py` | 选题 rubric 维度 `lidousha_centrality`（李豆沙不可替代性）；维度 key 同时是持久 scorecard schema |
| `src/autoslice/cover_emote.py` `emote_catalog_prompt_block` | 表情包目录 prompt 块内嵌「李豆沙 / kmx（粉丝团名）」释义，经 `cover_generation.py` 拼入封面生成 prompt |

## B. 控制流/默认值绑定默认 profile（可逐点改为 CHANNEL_PROFILE 字段）

| 位置 | 内容 |
|---|---|
| `src/autoslice/huozi_luanshua.py` | `speaker != "lidousha"` 拒绝逻辑与 `human_reviewed_lidousha`/`verified_lidousha_voiceprint` 授权 token |
| `src/autoslice/clip_context.py` | `load_scoped_speech_memory(speaker_id="lidousha", channel_id="lidousha")` |
| `src/autoslice/cpa_semantic_qa.py` `build_mock_cpa_response` | mock 模式内嵌「天不熊→kmx」词表检查 |
| `src/autoslice/subtitle_regression.py` `_SPEAKER_LABEL` | 正则硬编码 `[李豆沙|连线]` 说话人标签 |
| `src/autoslice/self_reference_absorption.py` | `_CANONICAL_NAMES = ("李豆沙","小李","豆沙")` 与 `host_phonetic_ratio` 目标名 |
| `src/autoslice/shadow_review.py` `_default_style_profile` | 内置风格兜底含「小皇帝」等频道口癖 |
| `src/autoslice/term_authority.py` | 词表加载单源指向 `scripts.profile_glossary_terms`（按选中 profile 读 glossary，机制已通用，失败静默跳过需注意） |
| `src/autoslice/title_policy.py` | `_TITLE_BANNED_REGEXES[0]` 位置性假设（槽位 0 = 「秒X」规则，import 时取用——`banned_regexes` 为空会直接 IndexError；模板骨架已带该槽位默认值） |
| `src/autoslice/upload_tag_policy.py` | tag prompt 内容契约在 import 时强制（`base_tags` 非空、模板必须保留 `{existing_tags}`/`{title}`/`{srt_text}`）；模板骨架已满足 |
| `scripts/evaluate_speaker_phase1.py` | `TRUTH_TO_AUTO = {"lidousha": "李豆沙", …}` 标签映射 |

## C. 资产路径旁路 profile 解析（对默认 profile 字节等价，最安全的第一批修复）

**实际后果**：C 组未修复前，发布 lane（出版登记、终审契约）与声纹安装 lane
是**默认 profile 专用**——非默认 profile 可以产包评审，但公开发布会读/写
示例频道的资产文件。换频道要发布，先修这一组。

| 位置 | 内容 |
|---|---|
| `src/autoslice/final_human_review.py` | `FINAL_MEDIA_REVIEW_CONTRACT_PATH` 硬编码 `assets/lidousha/final_media_review_contracts.v1.json` |
| `src/autoslice/publication_registry.py` | `DEFAULT_REGISTRY_PATH` 硬编码 `assets/lidousha/publication_registry.v1.json`（上传授权的唯一门！） |
| `src/autoslice/review_package_owner_audit.py` | `SOURCE_TRUTH_LEDGER_PATH` 硬编码 lidousha 真值台账 |
| `src/autoslice/manual_title_repair_authority.py` | `AUTHORITY_ROOT` + 字面 `SCHEMA_VERSION="lidousha-manual-title-repair-authority.v1"` |
| `scripts/audit_review_package.py` | 指纹源列表中真值台账为字面 `assets/lidousha/...`（同文件其他条目已走 `CHANNEL_PROFILE.asset_file`） |
| `src/autoslice/term_lexicon.py` | `anchor/"lidousha"/term_lexicon.json` 兜底路径 |
| `scripts/score_blind_subtitle.py` | 默认真值路径指向 `assets/lidousha/...` |
| `scripts/install_voiceprints.py` | 接受门 `schema_version == "lidousha-voiceprint-profile.v1"`（按文档应为 `<profile-id>-voiceprint-profile.v1`） |

## D. 参考部署默认值（不改代码也能用：CLI 参数 / 环境变量覆盖）

| 位置 | 默认值 | 覆盖方式 |
|---|---|---|
| `src/autoslice/producer_request.py` | `--ssh-host` 默认 `free`（参考部署主机名） | 显式传 `--ssh-host` |
| `src/autoslice/producer_speaker.py` | `/opt/bilive/autoslice/{voiceprints,venv-diar,models/campp,repo}` | 函数参数/部署布局对齐 |
| `src/autoslice/bilibili_member_api.py` | `BILIUP_BIN=/opt/bilive/bin/biliup`（常量） | 部署布局对齐 |
| `scripts/authorized_upload.py` | 入集校验的合集/小节 ID | **已强制配置化**：`AUTOSLICE_SEASON_IDS` 必填、无默认（账号专属，绝不复用示例频道的合集） |
| `src/autoslice/speaker_context.py`、`subtitle_timing_qa.py`、`semantic_candidate_selector.py` | `/opt/bilive/...` env 兜底 | 对应环境变量 |
| `scripts/gemini_slice_jingting.py` | `HOST_VIDEOS` 指向参考部署网盘挂载 | 环境变量/参数 |
| `scripts/transcribe_live_*_via_agy.sh`、`llm_via_free_groq.sh` | `ssh free` | 改脚本头部主机变量 |
| `scripts/slice_monitor.py`、`scripts/auto_review_shadow_daemon.py` | 默认房间号/主机为示例频道 | CLI/env 覆盖 |
| `scripts/silero_vad_spans.py` | `MODEL=/opt/bilive/vad/silero_vad.onnx` | 部署布局对齐 |
| `scripts/sync_profile_assets.sh` | 同步目标固定为 `lidousha` profile；远端用扁平文件名（如 `lidousha_glossary.txt`，`profile_glossary_terms.py`/`gemini_slice_jingting.py` 在主机上按此名兜底读取） | 换 profile 需改脚本内 profile 变量与远端文件名 |
| `ops/recording/*` | RoomId/路径为参考部署 | 见 `ops/recording/README.md` |
| `scripts/deploy_autoslice.sh` | 校验清单含 `assets/lidousha/...` 与声纹/批计划存在性 | 按你的 profile 改写后用 |

## E. 词汇级兼容（**不要改**）

持久证据/schema 词汇按设计保留 `lidousha-` 拼写以不打碎旧包哈希：
`lidousha-*.v1` schema 串、`lidousha_role`、`human_reviewed_lidousha`、
`verified_lidousha_voiceprint`、`LIDOUSHA_*` 兼容 env 别名。未来 evidence
schema 大版本可带显式迁移地重命名（见 `profiles/README.md` 尾注）。
