# 80 打包与交付

本文件是打包步骤的**分步权威**。入口：`src/autoslice/producer_package_finalization.py`。

- talk 车道成品强制前置 manifest 在册片头（当前 Z1/Z2 按主片 SHA-256 稳定轮换，fail-closed，`branding_intro.py`，manifest `assets/lidousha/intro/branding_intro.v1.json`）；**歌切不带片头**。验收必须按 record 的 `intro_id` 对照 manifest 的 hash/时长，不能把 Z1 的 5749ms 写死。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急。
- 片头在最终烧录内拼接，下游 sha256 绑定 with-intro 字节；`.srt`/`.ass` sidecar 保持内容时间轴，偏移记 `burned_preview.branding_intro.intro_offset_ms`。
- 终态跨面校验：`producer_text_finalization.py::verify_chat_authority_final_surfaces`（文字+染色真的落进交付 SRT/ASS 才算数）。
- 审计闸：`scripts/audit_lidousha_review_package.py`（歌切标题格式、字幕行长/行数/静置时长、对齐证据等）。历史/人工包默认按每行 18 字审计；autoslice Sapphire72 包必须在 `review_manifest.json.subtitle_visual_contract` 显式绑定当前渲染器的 2 行/28 字上限，审计器拒绝任何超过渲染器上限的自报宽松契约。
- 2026-07-22 起的新包按日期自动进入 StoryContract 严格审计（仍应显式声明 `story_contract_required=true`）、并必须声明 `run_mode` 与 `upload_allowed=false`；producer 的可选布尔值不能关闭新政策。审计器会用 record 中同一 StoryContract 重验最终 SRT、标题、封面文本及实际渲染行、南町专名/关系主张、字幕 hash 与 selection scorecard；封面内嵌的 contract 摘要也必须与 record 一致。任何旧字幕/旧标题/旧封面/旧 policy 字节混入都会把包判为不合规，而不是继续显示为当前成品。
- 上传路径 fail-closed：无 `AUTO_UPLOAD` manifest + artifact hash 门就没有发布（AGENTS.md 方向）。
- tag 按成品字幕出（`upload_tag_policy.py`，Ivan 2026-07-13）。
