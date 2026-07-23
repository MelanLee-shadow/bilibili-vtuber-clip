# 历史封面修复计划（不可执行）

本目录中的 JSON 仅用于保留既往 hash-bound 证据。它们绑定旧 state、候选、标题、
路径或 artifact hash，全部带 `do_not_execute=true`，并由
`scripts/repair_reviewed_covers.py` 明确拒绝。

当前封面修复必须依据当前 state 和 artifact bytes 新建计划；规则权威见
`docs/pipeline/70-cover.md`。
