# 2026-08-25 C4/C5 字幕时间域事故与修复闭包

## 影响面（事故发现时）

- C5 `auto_113028_1271_1328` / `BV1Sahj65ExM` 的错误 CID 是
  `41264349440`。错误包把 reviewed SRT 从 21 cue 再裁一次，删掉开头 4 cue，并把第 5 cue
  “呃”从 delivery-local `9.560s` 错钳到 `0s`。
- C4 `auto_113028_1602_1698` / `BV1h7hg68E8Y` 的错误前任 CID 是
  `41264153653`。错误修复把主媒体从正确 `+9750ms` crop 扩成 source-local `0`，但 sidecar
  SRT 仍是 delivery-local，因此字幕相对音画早 `9750ms`，且主片多出错误开场。
- 本事故禁止创建第二 BV；两稿只允许通过 `scripts/authorized_upload.py` 的 same-BV repair
  状态机串行修复，C5 先作 canary，C5 fresh live 验收通过后才可触碰 C4。

## 根因

operator-reviewed SRT 的 cue 时间本来已经是 **delivery-local**，但 C4/C5 baseline manifest
把它们错误标成完整 padded piece 的绝对源区间。full-window replay 因而先把 cue 当作
piece-local，再在最终交付处减一次 `9750ms` crop，形成双重投影。旧审计只验证文件 hash、
结构与 publication metadata，没有把媒体真实 crop 起点和 SRT 时间域绑定，也没有做最终音画
感知验证。

C5 的 `c5_start_clamp` 提案进一步把不同坐标系里的 `9560ms` cue 与 `9750ms` media crop
直接比较，错误地产生 `190ms` straddler。该提案及其历史 seal 仅保留为事故证据；运行时授权
已经撤销，任何调用均 fail closed。

## 正确坐标

| candidate | piece absolute | final crop | delivery absolute | reviewed grid |
|---|---:|---:|---:|---:|
| C5 `1271_1328` | `[1261170,1376550)` | `[9750,67524)` | `[1270920,1328694)` | 21 cue；首 cue `0.250s`；“呃” `9.560s` |
| C4 `1602_1698` | `[1592760,1746900)` | `[9750,106540)` | `[1602510,1699300)` | 20 cue；`《线上直播间` `0.250s` |

Talk 的 Z2/Z1 branding intro 分别仍按旧交付 authority 固定为 `6183ms` / `5749ms`；SRT/ASS
sidecar 始终保持 main-content-local，不把片头时长写进 cue。

## 系统性修复

- operator full-ownership v3 baseline 必须声明 `DELIVERY_LOCAL` 或 `PIECE_LOCAL`；未知/缺失
  domain 失败。
- `DELIVERY_LOCAL` 的 manifest 绝对区间必须逐毫秒等于最终媒体 source interval，重放是
  identity；禁止进入 full-window crop。
- `PIECE_LOCAL` 才允许 full-window replay，并只裁一次；它不能把媒体开场强制到 source-local
  `0`。
- baseline cue 必须完全落在所声明区间；domain/record/media boundary 任何不一致都失败。
- C3 原 manifest 只因已有独立 deploy-sealed exact-final authority，按其精确历史 bytes 合成
  `DELIVERY_LOCAL`，不能作为通用兼容默认。
- 回归固定 C5 21 cue/“呃” `9.560s`、C4 20 cue/首 cue `0.250s`，并显式拒绝旧
  full-piece 标签与 17-cue C5 投影。
- C4 的 immutable predecessor 使用历史单-C4 authority，而当前包使用合并 C4/C5 authority。
  兼容只允许 policy `c4-2026-08-25-single-to-combined-v1` 的单向、精确、单候选迁移；反向、
  wildcard、chain、缺 tag 保留或任一额外差异均失败。
- policy、registry、source receipt、predecessor plan/completed 均用 descriptor-anchored、
  no-follow、单 inode captured-byte 读取；路径替换、原地写、父目录 symlink 和 mutation-time
  attestation transplant 均在 `observe()` 或任何副作用前失败。

实现 commit 为 `0c81ef997e788c9104a5e81089670265c2ba5918`，patch digest
`65f90289f40a41d058d807a6d16845c68784b4a6b112cac06c772dbaf88ae8cf`。部署前完整测试
`6864 passed in 330.53s`；生产部署后的 captured-byte migration preflight 与最终 no-mutation
`repair-plan --dry-run --predecessor-completed ... --preserve-existing-tags` 均通过。Sol Max 的第二次、
最终 pre-mutation review 结论为 `APPROVE`。

## 公开修复闭包

| candidate | 保留 BVID | 错误前任 CID | 已验收 CID | public acceptance SHA-256 |
|---|---|---:|---:|---|
| C5 `1271_1328` | `BV1Sahj65ExM` | `41264349440` | `41267890237` | `0ddb6712d94c6237c730b5bbec3e6b6528d97d9c6b38c54e8477844d07068fc8` |
| C4 `1602_1698` | `BV1h7hg68E8Y` | `41264153653` | `41270641488` | `8080f8904fa3393809cb13c2eca60881ff70cc934a66f31b9b9ccf1bae6d49f3` |

C5 先完成并通过 fresh public acceptance；其后 C4 才执行在线事务。C4 actual plan SHA-256 为
`cd15f9be119490d4c59f0f14abe8004b20c66eb3b52ea33c6484aafc9cddd0d2`，journal 终态
`VERIFIED`，fresh completed sidecar SHA-256 为
`6b51c92dcebb9c98d7e06c12eb8fc2a0cb2cff498eece247c5cf6cc0db6c3bc9`。两次在线修复均只经
`authorized_upload.py`；未调用 raw upload API、legacy repair script 或手工 ledger 编辑。

C4 fresh public q64 全量解码通过（3076 H.264 帧、4809 AAC 帧）；public 对 final package 音频
相关 `0.9991591867` / `0ms`，public main 对单裁剪 source 相关 `0.9987145512` / `-30ms`，错误
双裁剪仅 `0.0681372971`。公开封面与 reviewed package 逐字节相同。30fps 公开画面复核确认：
首 cue 在合同 `5.999s` 后首个采样帧出现；日语 cue 覆盖 `12.039–14.079s`；七段性别链覆盖
`56.069–66.409s`；`66.409–95.979s` 保持 intentional blank；末 cue 在 `102.139s` 后清除，
媒体自然结束于 `102.581s`。

Creator 全部 382 个已发布稿件扫描中，两个 exact title 都各自只匹配一个既有 BVID。C4/C5
fresh public、Creator 与 section `9320779` 同时指向上述两个独立 BVID/CID；active
static+runtime merged publication registry 对两 candidate 均返回“already published / new uploads
forbidden”。`/opt/bilive/autoslice/DISABLED` 始终是空 regular `0644`。

## 计算与证据

最终媒体校验同时分发到两台 Colab CPU VM，并与 OCI3 两进程并行交叉验证。两端一致确认
6152/4809 个 package 音视频帧、单裁剪相关约 `0.9993947459`、双裁剪约 `0.0668456195`；两台
Colab 都在 verified fetch 后停止。此 CPU 媒体任务 OCI3 critical path 为 `48.41s`，Colab decode
为 `88.56s`，因此本次 OCI3 更快；Colab 作为独立 provenance-verified 第二计算面保留。

关键生产证据：

- C4：`/opt/bilive/autoslice/private-c4-timeaxis-repair-02e062b9/evidence/c4-public-acceptance.json`
- C5：`/opt/bilive/autoslice/private-c5-timeaxis-repair-72b592a7/evidence/c5-public-acceptance.json`
- 双 BV closure：`/opt/bilive/autoslice/private-c4-timeaxis-repair-02e062b9/evidence/c4-c5-publication-closure.json`
  （SHA-256 `4379d3189e93bc2de8a343386b8943891ac0203de92cc7d517f0b7dbafcde69c`）
- 分布式媒体校验：`/opt/bilive/autoslice/private-c4-timeaxis-repair-02e062b9/evidence/distributed-validation-summary-0c81ef99.json`
- 公开视觉复核：`/opt/bilive/autoslice/private-c4-timeaxis-repair-02e062b9/evidence/c4-public-visual-review.json`
