---
id: 20260910-trh001
title: Preserve Terminal Regular Hardlink Authority
status: active
created: 2026-09-10
updated: 2026-09-11
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
- receipt 写入前会预算完整 terminal validation v3 receipt 的 aliases、祖先目录与 JSON 上限，避免已落盘 ticket/recovery alias 后才发现无法持久化终态 authority。receipt 已持久化后，以一次 identity ledger 同时证明 batch 内仍存 alias 的数量并驱动删除；live target 的 link-count 必须恰为 `1 +` 该 ledger 数量。v3 receipt 为每个 terminal alias 持久化 canonical path、父目录 object identity，以及从 batch root 以下到该 leaf 的全部祖先目录 identity。已消费的 leaf 或目录可以缺失；但任何祖先命名空间一旦同名重现（包括整个 `pending`、self-describing v2 active directory、symlink 或普通目录），都必须在删除前被拒绝。historic v1/v2 receipt 缺少这项 namespace authority，安全地停在明确的 manual-recovery 路径。
- 新写入的、可安全编码的 active subtree token 同时编码 object identity、type 与原 canonical name，恢复时继续按该逻辑路径执行 nofollow、mount、access policy、预算、retained-entry 与 ledger/current equality 校验。writer 以实际目录 fd 的 `NAME_MAX` 约束 v2 token，空间不足时安全回退 v1；decoder 只接受单一可 round-trip 的路径组件。超出 v2 名称上限的历史 content entry 保留 v1 表示并保持 fail-closed；旧式无 canonical-name authority 的 active directory 绝不猜测其子树语义。
- owner-only `0600` 文件的 GID 不影响访问策略，保持为可容忍的良性 metadata churn；不把它错误纳入精确匹配。
- metadata `<11` 的既有 v4/v8 ticket 必须同时绑定当前 batch、finalization marker、retained-representation 准入，以及完整 phase terminal group；v4 逐项比较 identity/content/access，v8 额外比较精确 link-count。historic v1/v2 generic ticket 不含完整 group/count：当 batch root 仍绑定原对象时，metadata I/O/ACL、完整 parser、marker 或 terminal group 失败一律保留原票据并进入稳定的 manual-recovery 路径，绝不把运行时采样的 hardlink 数量误当历史授权。仅当 root identity/binding 已确定被替换，或 metadata 明确声明的 batch-local evidence parent 在 nofollow 探测中呈现 symlink/非目录时，才安全跳过该外来 batch（不删除、不阻塞无关 install）；metadata 缺失与纯 schema-envelope 不兼容保留既有兼容清理契约。新路径也绝不签发 v1/v2。缺失 index 或 ticket 时回落到正常 publisher，安全创建缺失 index 并按当前 legacy-compatible schema 写入，绝不把旧 metadata 的新 ticket 静默升级为 v8。v11 的 unchanged regular target 则强制 `link_count == 1`。

## Evidence and Next Steps

- 新的 deterministic regressions 覆盖 stage、evidence、recovery alias 的真实 unlink 中断和同名 foreign reappearance、alias 父目录及整个 `pending` 已 rmdir 后的重建、伪造 v2 active parent、foreign hardlink、active subtree rename、legacy v1/v2 receipt 的安全人工恢复短路、v6 missing-index/retained-ticket/committed/rollback v4 retry、v6 既有 v8 ticket 的原 bytes/inode 复用与缺失 unchanged target 的 fail-closed，以及伪造的 v11 unchanged count。
- 首轮 frozen whole-range fresh-context review 发现两个新增边界：cleanup action budget 耗尽时，未被扫描的 canonical v1/v2 terminal ticket 可绕过 mutation fence；以及 v1 active-name fallback 在 rename 后崩溃会失去 receipt slot 映射。前者在 mutation fence 重新分类 canonical v1/v2，并只保留已证实 foreign batch 的延期短路；后者只在 parent/object identity 对应唯一未占用 v3 receipt slot 时回绑，否则保持 fail-closed。后续独立审计还证明 synthetic legacy ledger 名可与 receipt alias 同名；因此终态对象的零个或多个候选现在直接拒绝，只有非终态历史对象保留该兼容路径。新增 deterministic zero-budget、foreign-exemption、NAME_MAX fallback crash-recovery、ambiguous-slot 和 synthetic-name-collision regressions。
- focused validation 已通过修复后 materialization + overlay modules（`208` tests，`144.433s`）；静态 ruff、py_compile、diff whitespace、source-lock refresh/check 均通过。最新完整 suite 已在原生受管环境通过：`1697` tests、`3` expected skips、`1152.734s`。冻结 whole-range fresh-context review、PR gate 与下游 release/install 仍是剩余门槛。
- consumer Toolbox 必须从本 canonical commit 重新生成 receipt，再由 immutable release 进入 private source-lock promotion。
