# 80 打包与交付

本文件是打包步骤的**分步权威**。入口：`src/autoslice/producer_package_finalization.py`。

- talk 车道成品强制前置片头 Z1（fail-closed，`branding_intro.py`，manifest `assets/lidousha/intro/branding_intro.v1.json`）；**歌切不带片头**。`AUTOSLICE_BRANDING_INTRO=off` 仅测试/应急。
- 片头在最终烧录内拼接，下游 sha256 绑定 with-intro 字节；`.srt`/`.ass` sidecar 保持内容时间轴，偏移记 `burned_preview.branding_intro.intro_offset_ms`。
- 终态跨面校验：`producer_text_finalization.py::verify_chat_authority_final_surfaces`（文字+染色真的落进交付 SRT/ASS 才算数）。
- 审计闸：`scripts/audit_lidousha_review_package.py`（歌切标题格式、字幕行长/行数/静置时长、对齐证据等）。
- 上传路径 fail-closed：无 `AUTO_UPLOAD` manifest + artifact hash 门就没有发布（AGENTS.md 方向）。
- tag 按成品字幕出（`upload_tag_policy.py`，Ivan 2026-07-13）。
