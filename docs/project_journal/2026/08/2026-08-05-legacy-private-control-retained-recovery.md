---
id: 20260805-legacy-private-control-retained-recovery
title: Legacy Private-Control Retained Recovery
status: active
created: 2026-08-05
updated: 2026-08-06
branch: wip/recover-private-control
pr:
supersedes: []
superseded_by:
---

# Legacy Private-Control Retained Recovery

## Summary

- 为 registry 中唯一的 `legacy-shared-v0` 增加显式 dry-run/execute recovery，允许在完整证据原地保留的前提下切换到 `primary-home-v1` 分配。
- Recovery 不 move、copy、rewrite、delete 或 purge legacy tool/quarantine 中的任何对象；物理清理继续属于单独的高风险合同。
- Generator 与 standalone runtime 保持独立实现，并由 parity tests 绑定为同一份 machine contract。

## Protected Properties

- Legacy object identity 由 exact `(st_dev, st_ino, type)` 保护；regular-file content 由两次 bounded streaming 读取、size 与 SHA-256 保护，普通 payload 不在内存保留，只有 4 KiB-capped owner candidate 会为 schema decode 捕获 bytes；access policy 由 mode/uid/gid 保护。
- 初次发布的 cutover marker 继续由原 descriptor 与 publication record 绑定到 exact `(st_dev, st_ino, type)`、mode/uid/gid 及精确 bytes；mtime/ctime 等 benign timestamp transition 不作为 marker replacement 信号。
- Directory tree 由 bounded complete inventory、child identity/access 与 recursive digest 保护；mtime/ctime 和 directory timestamp churn 不作为 mutation 信号。
- `st_blocks * 512` 只作为 allocated-capacity 门禁，不参与 execute 的 protected plan equality。
- 任何 unreadable/revalidation failure 与 missing/mismatch 保持不同诊断，但都 fail closed；symlink、special object、hard-link alias、unsafe policy 与 topology overlap 在 primary publication 前拒绝。

## Transaction Contract

- Dry-run 按 tool-root → quarantine 顺序获取 nonblocking exclusive leases，完整记录已识别 owner record、对应 private directory 以及 `.saved` 等未知 retained evidence。
- Operator plan 通过 no-follow parent walk 以 mode `0600`、exclusive creation 写在 private-control roots 外部。
- Execute 重新扫描并要求 exact protected-plan match，随后先发布 fixed mode-`0400` receipt，再以 fixed marker 作为 commit point。
- 每次 publication 使用 plan digest + 随机 nonce 的唯一 pending name；short write、`fchmod` 或 file `fsync` 失败可留下 untrusted pending residue，rename-after-effect 后 parent-directory `fsync` 失败则可能留下 visible fixed name。Retry 不覆盖它，而是重新 `fsync` containing directory 并复验 exact identity/access/content；existing marker 在 durability repair 前后均执行完整 adoption verification，并绑定最初 retained marker inode。
- 初次 marker publication 不再在 final adoption verification 前关闭并丢弃 binding。原 marker descriptor、payload 与 publication record 保留到 verifier 返回之后；fixed name 再按 exact identity/access/bytes 复验，且 `verification["marker"]` 必须与 publication record 完全一致。
- 首次已经观察到 primary namespace 存在后的 bind failure 不再被二次 lookup 降级为 absence；若 earlier prebind 已证明存在，execute 内 lookup 缺失也在任何 staging mutation 前停止。
- Committed adoption 的完整 recovery-cap verifier 在普通 256-entry stale-recovery gate 之前运行；合法的大 inventory 不会因旧运行时普通 cap 被误拒绝。
- Receipt-only crash 保持 blocked/retryable；marker 只有在 marker → receipt → current complete manifest 与 whole-registry terminal revalidation 全部通过后才允许 `adopted-retained-in-place`。
- 新 runtime 接受 verified adoption；旧 runtime 仍以 `legacy-recovery-pending` 阻止 allocation。所有后续新对象只进入 `primary-home-v1`。

## Delivery Boundary

- 本 workstream 交付 repository code、tests、docs、source lock、fresh named single review 与 signed checkpoint；push/PR lifecycle 由交付协调流程在收到冻结证据后执行。
- 不执行真实主机 recovery，不修改 legacy retained evidence，也不执行其他 host mutation。

## Validation

- 签名 checkpoint `684f88d8da5f1c9469d4cc2a384e91beab67fac7` 的历史门禁包括：Python 3.13 与系统 Python 3.9 的 38 项聚焦矩阵，以及沙箱外完整 `unittest` 1115 tests in 1317.494s、3 skipped、0 failures/errors。
- 该 checkpoint 的 fresh named single review 返回一项 finding：初次 cutover 在 `_pc_recovery_publish_document()` 后先关闭 marker binding，再由 `_pc_recovery_verify_adoption_locked()` 无 expected identity 地重开固定名称，因此未把最终接受结果绑定到已发布 marker 对象。
- 当前未提交 repair 在 canonical generator 与 standalone runtime 中保留 marker descriptor/record 穿过 final verification；新增 same-content permanent replacement 与 different-content transient replacement regressions，两项均同时覆盖两份实现。
- 当前 repair gate：两项新增回归 2/2 通过；包含成功执行、marker durability retry、既有 identical replacement 与两项新回归的 focused matrix 5/5 通过；完整 `PrivateControlRetainedRecoveryTests` 25/25 通过；Python 3.13.0 与系统 Python 3.9.6 `py_compile`、Ruff 与 `git diff --check` 通过。
- Source-lock refresh 与 `--check` 使用 owner-private `TemporaryDirectory` 和单一 task-private `task-primary-home-v1` spec 调用未修改的 `refresh_source_lock()`，两次均覆盖 6 sources；`RepositorySourceLockTests` 2/2 通过，最终 `sync-source-lock.json` SHA-256 为 `f650424b0590a84f67eafe1023b0b89f6d59a33cc1306bcee2cfcb81d0fd063f`。该 isolated serialization gate 不代表 production host gate，且没有读取、修改或恢复 host legacy evidence。
- 当前 repair 后最终 JSON parsing、journal validation、Ruff、`git diff --check` 与双运行时 compile 均通过；沙箱外完整 Python 3.13.0 `unittest` 1117 tests in 1266.973s、3 skipped、0 failures/errors。
- Fresh named single retry 尚未运行，因此本条目不声称 repair 后 review clean，也不沿用旧 checkpoint 的 clean review claim。
