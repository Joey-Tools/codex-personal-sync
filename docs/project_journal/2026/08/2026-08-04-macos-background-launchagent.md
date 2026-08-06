---
id: 20260804-macos-background-launchagent
title: macOS Background LaunchAgent Default
status: completed
created: 2026-08-04
updated: 2026-08-06
branch: wip/macos-background-launchagent
pr:
supersedes: []
superseded_by:
---

# macOS Background LaunchAgent Default

## Summary

- macOS scheduler 的 canonical 默认值改为 per-user Background LaunchAgent，固定使用 `user/$UID` domain，不再依赖 Aqua/GUI login。
- canonical plist 声明 Background session/process、account-home `HOME` / `WorkingDirectory`、`Umask=077`、`ThrottleInterval=60` 和 `LowPriorityIO`。
- cold boot 后仍需该用户建立首次 session；SSH login 可满足这个条件，无需 GUI login。
- Linux 的 user systemd 行为保持不变。

## Migration Contract

- Bare `install-scheduler` 保留已审计的 mode、repo、base repo、owner 和 interval，并把可识别的 legacy GUI 配置迁移为 Background 配置。
- LaunchAgent profile 使用递归的类型和值精确匹配，拒绝 plist 中 `bool`/`integer` 或 `integer`/`real` 的非规范替换。
- Activation 固定执行 canonical GUI bootout/disable、user bootout、user enable、user bootstrap；bootstrap 前的 enable 会清除 shared disabled override，任何未知、权限或超时结果仍 fail closed。
- GUI domain 不存在时，仅对 exact managed target 的 `launchctl disable` 接受 action-specific code 125 absence；其他 operation、domain 或 label 仍拒绝。
- `status-scheduler`、`doctor` 和 uninstall 同时检查 canonical 与全部 managed legacy label 的 user/GUI domain 矩阵，避免 stale registration 形成未报告的双实例。
- canonical plist 缺失时，daemon query 复用同一份已绑定的 absence audit；任何仍加载的 managed identity 都会报告为 orphan-active，uninstall 仅在完整矩阵确认 disabled 后提交清理事务。
- 现有 descriptor binding、transaction marker、exact-output parser 与 conditional-removal 安全边界继续覆盖 migration 和 cleanup。

## Operational Boundary

- Scheduler 的安装或更新仍是显式 host operation；普通 release install、mirror generation 和 retention workflow 不会隐式改变主机 scheduler。
- 主机 rollout、PR lifecycle 和运行时验证属于交付流程证据，不在本目标态 journal 中预先声明完成。
- GUI 登录状态也属于受支持的迁移形态：`gui/$UID` disable 可能影响同一用户的 Background target，因此 bootstrap 前的 exact user-domain enable 是 activation contract 的必要边界。
