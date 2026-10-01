---
id: 20260918-crgv2s
title: Codex Review Gate v2 Handoff
status: completed
created: 2026-09-18
updated: 2026-10-01
branch: codex/organization-v2-handoff
pr:
supersedes: []
superseded_by:
---

# Codex Review Gate v2 Handoff

## Summary

- 已安装 canonical v2 verifier、controller，并以 `@JoeyTeng` CODEOWNERS 保护控制面。
- 组织收尾回执核验通过后，已移除临时 v1 bridge。

## Current State

- PR 事件通过 `JoeyTeng/codex-review-gate-action@v2` 产生 `codex/github-review-gate` check。
- Verifier 工作流授予 `actions: read`，其余事件、作业与 action 参数保持不变。
- controller 仅处理新建的 bot 评论和精确 head 的手动调度；请求者权限策略为 `any`。
- 临时 bridge 已删除，不再由本仓库产生 `codex/review-gate` legacy status。

## Evidence

- Canonical handoff implementation: https://github.com/Joey-Tools/codex-review-gate/pull/51
- Post-cutover audit receipt SHA-256: `9a8b38f2188a14168423a07639d6662c87e198fe2dd12041f67fc224f363817e`.
