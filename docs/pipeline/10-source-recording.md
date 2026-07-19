# 10 录制与源健康

本文件是源步骤的**分步权威**。运行时权威在 free 主机部署：
`free:/opt/bilive/autoslice/repo`（现查 `DEPLOYED_COMMIT`）。

- 录制：blrec（`free:/opt/bilive/app`，容器 `/app`）；fmp4 降级补丁与录制看门狗见 memory `lidousha-slice-monitor`。
- 源健康门 + mount 看门狗：runner v4/v5（`scripts/free_session_autoslice.py`、`scripts/free_mount_watchdog.sh`）；CloudFS 挂载死亡会吃已 remux 文件——解冻前逐段核对 m4s↔mp4，fmp4 跨段时间轴须归零重 mux（memory `free-unattended-autoslice-runner`）。
- 源完整性：视频流独立解码零损伤（`source_integrity.py`）；BLOCK≠ok、0 交付≠done。
- 杀开关：`touch /opt/bilive/autoslice/DISABLED`。
