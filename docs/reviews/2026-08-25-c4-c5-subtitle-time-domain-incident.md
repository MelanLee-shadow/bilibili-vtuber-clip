# 2026-08-25 C4/C5 字幕时间域事故与修复闭包

## 影响面

- C5 `auto_113028_1271_1328` / `BV1Sahj65ExM`：当前错误 CID
  `41264349440`。错误包把 reviewed SRT 从 21 cue 再裁一次，删掉开头 4 cue，并把第 5 cue
  “呃”从 delivery-local `9.560s` 错钳到 `0s`。
- C4 `auto_113028_1602_1698` / `BV1h7hg68E8Y`：当前错误 CID
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

## 强制层

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

## 交付状态

代码与资产修复分支：`codex/timeaxis-domain-repair-20260825`。公开修复后的新 CID、canonical
package audit、最终感知 receipt、same-BV completed sidecar 与 fresh public/Creator/section
readback 在实际串行修复后补入本文件与 `docs/HANDOFF.md`。
