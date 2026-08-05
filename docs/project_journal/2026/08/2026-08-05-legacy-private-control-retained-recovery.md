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

- Legacy object identity 由 exact `(st_dev, st_ino, type)` 保护；regular-file content 由两次完整读取、size 与 SHA-256 保护；access policy 由 mode/uid/gid 保护。
- Directory tree 由 bounded complete inventory、child identity/access 与 recursive digest 保护；mtime/ctime 和 directory timestamp churn 不作为 mutation 信号。
- `st_blocks * 512` 只作为 allocated-capacity 门禁，不参与 execute 的 protected plan equality。
- 任何 unreadable/revalidation failure 与 missing/mismatch 保持不同诊断，但都 fail closed；symlink、special object、hard-link alias、unsafe policy 与 topology overlap 在 primary publication 前拒绝。

## Transaction Contract

- Dry-run 按 tool-root → quarantine 顺序获取 nonblocking exclusive leases，完整记录已识别 owner record、对应 private directory 以及 `.saved` 等未知 retained evidence。
- Operator plan 通过 no-follow parent walk 以 mode `0600`、exclusive creation 写在 private-control roots 外部。
- Execute 重新扫描并要求 exact protected-plan match，随后先发布 fixed mode-`0400` receipt，再以 fixed marker 作为 commit point。
- 每次 publication 使用 plan digest + 随机 nonce 的唯一 pending name；short write、`fchmod` 或 `fsync` 失败只会留下 primary-side untrusted residue，固定 receipt/marker 不变，重试不会采纳或覆盖该 residue。
- Receipt-only crash 保持 blocked/retryable；marker 只有在 marker → receipt → current complete manifest 与 whole-registry terminal revalidation 全部通过后才允许 `adopted-retained-in-place`。
- 新 runtime 接受 verified adoption；旧 runtime 仍以 `legacy-recovery-pending` 阻止 allocation。所有后续新对象只进入 `primary-home-v1`。

## Delivery Boundary

- 本 workstream 只交付 repository code、tests、docs、source lock、fresh named single review 与 signed local checkpoint。
- 不执行真实主机 recovery，不修改 legacy retained evidence，不 push，不创建 PR，也不执行其他 host mutation。

## Validation

- Python 3.13 与系统 Python 3.9 均通过 23 项 recovery、parity、source-lock、legacy compatibility 与 runtime audit/strict-doctor 聚焦测试。
- 沙箱外完整 `unittest` 套件通过：1105 tests，3 skipped，0 failures/errors；此前沙箱内 home-temp/Unix-socket 权限失败已由同一组沙箱外测试独立排除。
- `ruff check`、`git diff --check`、canonical source-lock refresh/check 与 project-journal validation 通过。
- Recovery implementation、standalone runtime、operator docs、architecture、source lock 与 regression tests 保持在同一签名 checkpoint；正式 named single review 的结果作为该 frozen checkpoint 的外部 review evidence 记录。
