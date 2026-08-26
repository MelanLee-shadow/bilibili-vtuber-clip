# C7b failed-row adoption：私有 replay / provider gate 证据（2026-08-26）

本页只记录 `auto_130040_201_255` 的 candidate-private 工程证据，不是 publication
authority，不授权 deploy、state CAS、upload、B 站写入或 OCI3 cutover。C3/C6 未闭合时，
publication order 和唯一 writer 规则保持不变。

## 1. 变更与审查

隔离 worktree：`/private/tmp/vtuber-slice-c7b-failed-adoption-20260826`

最终 HEAD：`440acaf1552e720c591dc4b0d8a4c5ba54ba1f76`

连续修复：

- `fb9fcd36`：baseline 显式声明 `time_domain=PIECE_LOCAL`，并更新 C7b sealed
  baseline-manifest hash。
- `a14136cc`：full-window replay 向 baseline application 传递精确 candidate/date。
- `c56eec3c`：finalizer replay 的两层 helper 继续传递精确 candidate/date。
- `440acaf1`：finalizer 对 absolute、已由 registry 验证的 baseline path 使用其
  canonical parent 解析 operator truth lanes；relative legacy config 仍使用原 fallback。
  没有放宽 symlink、parent、SHA 或 operator authority 检查。

Sol review：对上述修复返回 `APPROVE`；该结果仅表示代码可整合，不表示发布批准。

## 2. 本地验证

- C7b/replay/finalizer focused suite：`205 passed`。
- 变更后 focused C7b + producer finalizer suite：`59 passed`。
- 最终 worktree clean；`git diff --check` 与 Python compile check 通过。

## 3. provider-disabled private PASS0

私有 clone：
`free:/opt/bilive/autoslice/private-c7b-private-pilot-f1d368d7-20260826/repo`

clone authority manifest：
`sha256:661b326494664afdc0c5a4f312c391fb637d375e70fc734aea90a8f18636ec59`，绑定
`440acaf1`。baseline file SHA：
`sha256:030b8f71f89a4ec4b9decb17103ed455132c64a5921943957f9faf685f7c5ade`。

最终 no-provider probe closure：

- 文件：`free:/opt/bilive/autoslice/private-c7b-private-pilot-f1d368d7-20260826/probe9-pass0/evidence/closure.json`
- closure SHA-256：`95df0368633a0d2e93bb29c03ce4dee0abf73c7cccd5a4c0b0f20a9aad5ef87d`
- `main_rc=2`，故意在 exact-final reviewer 前以 `C7B_PROVIDER_DISABLED` fail closed；
  这不是 READY 或 upload authorization。
- 六个 replay pre-stage predicates 全部 `PASS`：old video、C7b provenance、padded
  source、v2/v3 ledger、explicit time domain、pipeline diagnostic。
- `provider_callable_calls=0`，`provider_attempted_reported=null`；factory 只构造
  一个本地惰性 callable（`provider_factory_calls=1`），没有执行 provider callback。
- `private_stage_empty=true`、`authoritative_surfaces_unchanged=true`、
  `upload_allowed=false`。
- live authoritative surfaces 的 pre/post SHA 均为
  `sha256:cfbd6987d9f0d44b88bde5cc8898f2b10f9088a3e5ecfa43fc2ec1ecd6a74d0f`。

## 4. provider-enabled full-dry blocker

随后执行了真实 provider-enabled private full-dry；它已经越过 replay、baseline、finalizer
lane-parent 和 C7b authority gates，但 exact-final review 停在：

`FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE`

证据：

- 文件：`free:/opt/bilive/autoslice/private-c7b-private-pilot-f1d368d7-20260826/probe10-provider/evidence/closure.json`
- closure SHA-256：`989301849fc8564ac74989c047dc2aa8d7650261337543e1c494a7eaa2b19deb`
- pre/post authoritative snapshot SHA 仍相同：
  `sha256:cfbd6987d9f0d44b88bde5cc8898f2b10f9088a3e5ecfa43fc2ec1ecd6a74d0f`
- `private_stage_empty=true`，没有 live state、registry、ledger、delivery 或 upload
  mutation。

因此 C7b 当前判定为 **代码修复已完成、私有 replay gates 已通过、provider exact-final
review 外部阻塞**。provider/JSON 依赖恢复后必须重新跑 exact-final review；在此之前不得
把它称为 private READY、不得整合到 deploy、不得上传。

## 5. 与整体战役的关系

当前 live authority 仍是 `free:/opt/bilive/autoslice`，deployed commit 仍为
`981bc4ab`，`DISABLED` 仍须保持 empty regular `0644`。C3 的 clip-context/story
authority 与 C6 的 boundary hash/coordinate closure 仍未解决；因此本页没有执行
cherry-pick、deploy、state CAS、authorized upload、public readback、OCI3 production
cutover 或 GitHub push。
