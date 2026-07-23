---
name: lidousha-title-style
description: 为李豆沙切片生成、审查或修复 B 站档案标题与封面梗字；使用当前共享标题门与 repo 内风格资产。
---

# 李豆沙标题操作方法

本 skill 是操作入口，不是规则副本。开始前读取：

1. `../../../docs/pipeline/60-title.md`：发布标题硬规则与共同 choke point；
2. `../../../assets/lidousha/title_style.md`：自动标题 prompt 的风格/few-shot；
3. `../../../docs/pipeline/70-cover.md`：封面梗字与最终像素规则。

## 操作顺序

1. 先判 lane：talk 或完整 song。
2. Ivan 给出标题时，把它当作**正文 authority**：不送 LLM 改写，不擅自删词；仍交给
   `canonicalize_publish_title` 生成频道 archive envelope。
3. 没有人工正文时，按 `title_style.md` 从最终 StoryContract、selection hook、整片字幕和
   scoped entity context 生成候选；引用的话必须在成片中真实出现。
4. 不论人工还是自动，都运行 `publish_title_policy_violations`：
   - talk 最终带 `【李豆沙】`；
   - song 精确为 `【李豆沙】豆沙歌，《canonical歌名》`，绝无 hook/副标题；
   - 最终 12–48 字、无外层空白、括号/引号正确成对。
5. 自动标题另外过 selection-hook 与机器味/违禁词门；人工正文不自动重写，结构不合格则
   fail closed 并请求修正 authority。
6. 封面文字从**已通过的最终发布标题**派生：不带频道前缀；song 固定 `《歌名》`。
   标题改变即重新生成/重叠封面并重跑 cover proof。
7. package auditor 与 authorized uploader 必须各自复跑同一标题门。只改 `title.txt`、
   `publish.json` 或 Creator 页面中的一处不算完成。

## 发布修复

已发稿标题修复只编辑原 BV，并同时验证 public、Creator 与精确合集 section 的 episode title；
具体操作与完成条件见 `../../../docs/pipeline/90-publish.md`。不得在本 skill 里维护另一套上传命令。
