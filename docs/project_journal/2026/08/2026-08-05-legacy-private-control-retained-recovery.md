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
- 签名 follow-up checkpoint `cbf4bbf683410205336e4e364c0d8af5f47b3250` 在 canonical generator 与 standalone runtime 中保留 marker descriptor/record 穿过 final verification；其两项新增回归 2/2、focused matrix 5/5、完整 `PrivateControlRetainedRecoveryTests` 25/25 与沙箱外完整 Python 3.13.0 `unittest` 1117 tests in 1266.973s（3 skipped、0 failures/errors）均通过。
- 后续 fresh formal single review 对该 checkpoint 报告两项 P2 finding：generator recovery 的 regular-file stat→open flags 缺少 `O_NONBLOCK`，攻击者可在绑定窗口换入 FIFO 造成阻塞；generator 的 recovery owner/external-plan/receipt-marker JSON decode 使用 raw `json.loads()`，缺少 standalone 已有的 bounded integer 与 non-standard constant 拒绝策略。
- 当前未提交 repair 为 generator `_FILE_READ_FLAGS` 增加 `O_NONBLOCK`，使 plan、evidence、recovery owner 与 receipt/marker/pending leaf read 都保持 no-follow + nonblocking；open 后原有 `fstat` 仍分别验证对象 identity/type、content 与 access policy，并保留既有 failure classification。Generator recovery 的三处 JSON decode 同时接入 Python 3.9-compatible bounded integer 与 `NaN`/`Infinity` constant rejection hooks。
- 当前增量回归：新增 stat→FIFO 与 JSON limit/constant parity tests 2/2 通过；覆盖 plan/evidence/owner/receipt、deterministic plan/execute、generator/runtime parity、external-plan symlink ancestor 与 special/FIFO/cap 的 focused matrix 7/7 通过；完整 `PrivateControlRetainedRecoveryTests` 27/27 通过。
- Source-lock refresh 与 `--check` 使用 owner-private `TemporaryDirectory` 和单一 task-private `task-primary-home-v1` spec 调用未修改的 `refresh_source_lock()`，两次均覆盖 6 sources；`RepositorySourceLockTests` 2/2 通过，`sync-source-lock.json` 未改变且 SHA-256 为 `f650424b0590a84f67eafe1023b0b89f6d59a33cc1306bcee2cfcb81d0fd063f`。该 isolated serialization gate 不代表 production host gate，且没有读取、修改或恢复 host legacy evidence；任务私有 wrapper 已删除并确认不存在。
- 当前增量的 Python 3.13.0 与系统 Python 3.9.6 `py_compile`、Ruff、source-lock JSON parsing、project-journal validation 与 `git diff --check` 均通过；沙箱外完整 Python 3.13.0 `unittest` 1119 tests in 1389.740s，3 skipped、0 failures/errors。
- 一条独立只读增量审计复核了 `_FILE_READ_FLAGS` 的全部 12 个调用点、三条 recovery leaf-read 路径、三个 recovery JSON 入口与新增回归，终态 clean、无 actionable regression。该增量随后形成 checkpoint `ce37eafb7f7a63f58c5ea8646393d5dbd2eac8de`；对该 checkpoint 的 fresh named single review 返回一项 P2：execute 第一次观察 primary fixed namespace 缺席后，若另一进程在 legacy inventory 窗口创建 mode `0700`、current-owned namespace，`_pc_recovery_open_or_create_primary_parent(..., allow_create=True)` 的第二次 lookup 会错误采纳该外部目录，而不是把本事务 identity 绑定到 staged no-replace publication。
- 当前未提交 repair 在 generator 与 standalone runtime 中同时收紧从缺席到发布的对象身份约束：`allow_create=True` 时，第二次 lookup 若已存在即以 domain error fail closed；只有第二次仍缺席才允许 staged no-replace rename。双实现 execute-level 回归在 `_pc_recovery_manifest()` inventory 窗口注入竞态目录，绑定并复验目录与 sentinel 的 object identity/access policy 及精确内容，且断言不发布 receipt、marker 或 staging residue。已存在后消失、正常创建与初始即存在的 adoption 路径保持原合同。
- 当前 repair 的新增回归 1/1、create/adopt/disappear focused matrix 5/5、完整 `PrivateControlRetainedRecoveryTests` 28/28 与 `RepositorySourceLockTests` 2/2 均在 Python 3.13.0 下通过。沙箱外完整 Python 3.13.0 `unittest` 1120 tests in 1526.963s，3 skipped、0 failures/errors。Canonical generator 与 standalone runtime 均通过 Python 3.13.0 和系统 Python 3.9.6 `py_compile`；Ruff 0.13.2 通过。
- Stock `refresh-lock` 与 stock `refresh-lock --check` 通过未修改的 CLI `main()`，在 workspace wrapper 下与 canonical worktree 平行且仅当前用户可访问的 task namespace 中连续运行，各覆盖 6 sources；该 namespace 自动清理，且流程不读取、修改或恢复 host legacy evidence。两个更早的不合格 task-home 候选分别被 `/var` symlink-ancestor 与 repository-overlap safety gate 在 lock 写入前拒绝。Locked engine SHA-256 更新为 `b9909167d7564939a3658d21d743a7cad4a02dd808f4e69fe0eba85cc69e91b4`，`sync-source-lock.json` SHA-256 更新为 `f6d8371eac8b535ffd7828919ab9e92e53de5b88f12dea8df6a312f41397290f`。
- 修复后的 fresh follow-up named single review 与 Claude review 尚未运行，因此本条目不声称 final review clean，也不把针对 `ce37eafb7f7a63f58c5ea8646393d5dbd2eac8de` 的 review 结果当作当前未提交修复的复核证据。
