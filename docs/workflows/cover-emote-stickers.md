# 封面表情包主体：操作导航

> 本文件只说明已经进入 `cpa_redraw` 后如何操作表情包主体，不定义截图/AI 总路由、人物门、
> 最终像素、梗字或发布准入。现行封面规则只读
> [70-cover.md](../pipeline/70-cover.md) 及其指向的代码/资产强制层。

## 当前入口

- 资产清单：`assets/lidousha/emote_library.v1.json`
- 路由与艺术指导：`src/autoslice/cover_generation.py`
- staging/引用图处理：`src/autoslice/publish_staging.py`
- 人工维护入口：`scripts/regenerate_lidousha_cover.py`
- 回归入口：`tests/test_cover_emote.py`

清单中的条目、hash、runtime media roots 与当前可用模式均从资产和脚本现场读取；本文不复制
数量、ID、尺寸、模型名或默认决策。缺媒体、hash 漂移、主体处理失败或最终 proof 不闭合时，
后续状态与可否换路仍由 70 step 和当前 route-decision 强制层判定。

## 操作顺序

1. 从当前 StoryContract、cover route decision、最终标题和 artifact hashes 开始；不要从历史
   封面、旧报告或本文的示例反推当前输入。
2. 仅在总路由已经选择 `cpa_redraw` 后读取当前 emote manifest，让现行艺术指导与
   `normalize_emote_choice` 处理自动选择。
3. 自动选择或人工点名后，确认 staging 实际解析了 manifest-bound reference，并在 record/
   route evidence 中留下当前选择、执行结果和 artifact hashes。
4. 继续运行 70 step 的最终像素、人物、文字与 route proof；表情包处理成功本身不是合规封面，
   也不授权内部静默换主体或换路线。
5. 标题、封面或清单任一字节改变，都重跑当前 cover proof 与 package audit。

## 人工点名

具体参数以脚本当前 `--help` 为准。命令形状：

```bash
python3 scripts/regenerate_lidousha_cover.py \
  --title "【李豆沙】示例标题" \
  --emote <manifest-id> \
  --emote-mode <current-mode> \
  --emote-reason "具体理由" \
  --out /absolute/output.png
```

是否还需要 `--ref` / `--media`、允许的 mode、失败语义及输出 sidecar 都从当前脚本与 70 step
读取，不在本导航固化。

## 验证

运行 `tests/test_cover_emote.py` 以及本次改动触及的 cover/package 定向测试。测试入口存在不等于
runtime media 已安装或生产包已通过；部署与产物状态仍须 live readback。
