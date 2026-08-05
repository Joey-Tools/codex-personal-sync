---
id: 20260805-legacy-private-control-retained-recovery
title: Legacy Private-Control Retained Recovery
status: completed
created: 2026-08-05
updated: 2026-08-05
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
- Directory tree 由 bounded complete inventory、child identity/access 与 recursive digest 保护；mtime/ctime 和 directory timestamp churn 不作为 mutation 信号。
- `st_blocks * 512` 只作为 allocated-capacity 门禁，不参与 execute 的 protected plan equality。
- 任何 unreadable/revalidation failure 与 missing/mismatch 保持不同诊断，但都 fail closed；symlink、special object、hard-link alias、unsafe policy 与 topology overlap 在 primary publication 前拒绝。

## Transaction Contract

- Dry-run 按 tool-root → quarantine 顺序获取 nonblocking exclusive leases，完整记录已识别 owner record、对应 private directory 以及 `.saved` 等未知 retained evidence。
- Operator plan 通过 no-follow parent walk 以 mode `0600`、exclusive creation 写在 private-control roots 外部。
- Execute 重新扫描并要求 exact protected-plan match，随后先发布 fixed mode-`0400` receipt，再以 fixed marker 作为 commit point。
- 每次 publication 使用 plan digest + 随机 nonce 的唯一 pending name；short write、`fchmod` 或 file `fsync` 失败可留下 untrusted pending residue，rename-after-effect 后 parent-directory `fsync` 失败则可能留下 visible fixed name。Retry 不覆盖它，而是重新 `fsync` containing directory 并复验 exact identity/access/content；existing marker 在 durability repair 前后均执行完整 adoption verification，并绑定最初 retained marker inode。
- 首次已经观察到 primary namespace 存在后的 bind failure 不再被二次 lookup 降级为 absence；若 earlier prebind 已证明存在，execute 内 lookup 缺失也在任何 staging mutation 前停止。
- Committed adoption 的完整 recovery-cap verifier 在普通 256-entry stale-recovery gate 之前运行；合法的大 inventory 不会因旧运行时普通 cap 被误拒绝。
- Receipt-only crash 保持 blocked/retryable；marker 只有在 marker → receipt → current complete manifest 与 whole-registry terminal revalidation 全部通过后才允许 `adopted-retained-in-place`。
- 新 runtime 接受 verified adoption；旧 runtime 仍以 `legacy-recovery-pending` 阻止 allocation。所有后续新对象只进入 `primary-home-v1`。

## Delivery Boundary

- 本 workstream 交付 repository code、tests、docs、source lock、fresh named single review 与 signed checkpoint；push/PR lifecycle 由交付协调流程在收到冻结证据后执行。
- 不执行真实主机 recovery，不修改 legacy retained evidence，也不执行其他 host mutation。

## Validation

- Python 3.13 与系统 Python 3.9 均通过最终 38 项聚焦矩阵，覆盖 retained recovery、source lock、generator/runtime parity、runtime audit/strict-doctor、macOS activation ordering 与 guardian FIFO contract。
- 最终冻结 source/test bytes 的沙箱外完整 `unittest` 套件通过：1115 tests in 1317.494s，3 skipped，0 failures/errors；此前沙箱内 home-temp/Unix-socket 权限失败已由同一组沙箱外测试独立排除。
- `ruff check`、`git diff --check`、canonical source-lock refresh/check 与 project-journal validation 通过。
- Recovery implementation、standalone runtime、operator docs、architecture、source lock 与 regression tests 保持在同一签名 checkpoint；正式 named single review 的结果作为该 frozen checkpoint 的外部 review evidence 记录。
