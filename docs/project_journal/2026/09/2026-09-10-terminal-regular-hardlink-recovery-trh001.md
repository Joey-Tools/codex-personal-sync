---
id: 20260910-trh001
title: Preserve Terminal Regular Hardlink Authority
status: active
created: 2026-09-10
updated: 2026-09-10
branch: codex/terminal-regular-hardlink-recovery-20260910
pr:
supersedes: []
superseded_by:
---

# 保留终态 Regular Hardlink Authority

## Summary

- 私有 overlay 的独立安全审阅发现，已提交 pending recovery 的终态目标没有持久化允许的 hardlink 数量。
- 若 commit 后出现外来 hardlink，旧逻辑会先建立自己的 recovery alias 并删除 batch 内证据，最后才在终态验证失败；外来路径虽不被删除，但 fail-closed 恢复证据已被过早丢弃。
- 此仓库是同步 engine 及测试的唯一 canonical source，因此在这里修复并刷新 consumer source lock，不能在 Toolbox 或 private overlay 留下反向补丁。

## Decision

- metadata v11 在 commit 前持久化每个终态 regular 目标的精确 link-count；terminal cleanup ticket v8 只接受冻结数量和本 batch 内具名 alias 所能证明的状态。
- alias 创建前和创建/已有后都重新校验精确数量，保护 object identity、content、access policy 与完整的已授权 hardlink 集。
- owner-only `0600` 文件的 GID 不影响访问策略，保持为可容忍的良性 metadata churn；不把它错误纳入精确匹配。
- 保留旧 durable metadata/ticket 的显式 legacy 解析，绝不将旧 payload 静默解释为新授权语义。

## Evidence and Next Steps

- 新的 deterministic regression 在 foreign hardlink 存在时证明 batch/ticket/foreign alias 均保留；移除外来 alias 后同一 ticket 可以恢复并回到单 link。
- source-lock 及受影响 recovery suites 已通过；consumer Toolbox 必须从本 canonical commit 重新生成 receipt，再由 immutable release 进入 private source-lock promotion。
