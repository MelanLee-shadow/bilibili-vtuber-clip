# 10 录制与源健康

本文件是源步骤的**分步权威**。运行时权威在 free 主机部署：
`free:/opt/bilive/autoslice/repo`（现查 `DEPLOYED_COMMIT`）。

- 录制：blrec（`free:/opt/bilive/app`，容器 `/app`）；fmp4 降级补丁与录制看门狗见 memory `lidousha-slice-monitor`。
- 源健康门 + mount 看门狗：runner v4/v5（`scripts/free_session_autoslice.py`、`scripts/free_mount_watchdog.sh`）；CloudFS 挂载死亡会吃已 remux 文件——解冻前逐段核对 m4s↔mp4，fmp4 跨段时间轴须归零重 mux（memory `free-unattended-autoslice-runner`）。看门狗重启 recorder 返回非零时必须 bounded `docker start` 兜底，并以容器内 `/app/Videos` 可读为完成条件。
- 终态库存硬门：runner 在任何“无新段”提前返回前运行 `recording-inventory-audit.v1`；发现已 `#EXT-X-ENDLIST` 的 m3u8/m4s 没有同 stem MP4，状态只能是 `source_incomplete`，不得进入 selection 或 `review_ready`。
- 源完整性：视频流独立解码零损伤（`source_integrity.py`）；BLOCK≠ok、0 交付≠done。
- 杀开关：`touch /opt/bilive/autoslice/DISABLED`。
