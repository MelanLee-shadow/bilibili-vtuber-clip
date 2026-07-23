# 历史证据（不可执行）

本目录只保存已经完成或已被现行流水线取代的审计、清理和迁移证据。

- 不得把这里的路径、命令、状态或操作清单当作当前 runbook。
- 当前步骤规则只读 `docs/pipeline/README.md` 指向的分步权威。
- JSON 历史证据必须带 `artifact_lifecycle=HISTORICAL_EVIDENCE_ONLY` 与
  `do_not_execute=true`；任何执行入口都应 fail-closed 拒绝。
