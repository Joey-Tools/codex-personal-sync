---
id: 20260805-legacy-private-control-retained-recovery
title: Legacy Private-Control Retained Recovery
status: completed
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
- Final repair 还将 recovery document 写入绑定到有界、零进展拒绝的 write-all 合同，并把递归目录终态 path/descriptor revalidation 的 missing、unreadable 与 descriptor failure 分别包装成稳定 domain errors。

## Protected Properties

- Legacy object identity 由 exact `(st_dev, st_ino, type)` 保护；regular-file content 由两次 bounded streaming 读取、size 与 SHA-256 保护，普通 payload 不在内存保留，只有 4 KiB-capped owner candidate 会为 schema decode 捕获 bytes；access policy 由 mode/uid/gid 保护。
- 初次发布的 cutover marker 继续由原 descriptor 与 publication record 绑定到 exact `(st_dev, st_ino, type)`、mode/uid/gid 及精确 bytes；mtime/ctime 等 benign timestamp transition 不作为 marker replacement 信号。
- Directory tree 由 bounded complete inventory、child identity/access 与 recursive digest 保护；mtime/ctime 和 directory timestamp churn 不作为 mutation 信号。
- `st_blocks * 512` 只作为 allocated-capacity 门禁，不参与 execute 的 protected plan equality。
- Pending publication 的资源有界性由 parent lease 下两次稳定 snapshot 保护：receipt 与 marker 的所有 plan/nonce history 合并计数，object identity、regular-file type、single-link、mode/uid/gid 与 logical size 必须稳定；content bytes 与 mtime/ctime 不属于该容量属性。
- 任何 unreadable/revalidation failure 与 missing/mismatch 保持不同诊断，但都 fail closed；symlink、special object、hard-link alias、unsafe policy 与 topology overlap 在 primary publication 前拒绝。

## Transaction Contract

- Dry-run 按 tool-root → quarantine 顺序获取 nonblocking exclusive leases，完整记录已识别 owner record、对应 private directory 以及 `.saved` 等未知 retained evidence。
- Operator plan 通过 no-follow parent walk 以 mode `0600`、exclusive creation 写在 private-control roots 外部。
- Execute 重新扫描并要求 exact protected-plan match，随后先发布 fixed mode-`0400` receipt，再以 fixed marker 作为 commit point。
- 每次新 publication 先提出 plan digest + 随机 nonce 的 pending name；short write、`fchmod` 或 file `fsync` 失败可留下 untrusted pending residue，rename-after-effect 后 parent-directory `fsync` 失败则可能留下 visible fixed name。Retry 在已持有的 primary-parent lease 下合并稳定扫描 receipt/marker history：同 family、同 plan residue 先按 exact object identity、regular-file type、single-link、mode/uid/gid 与 size 重新绑定，再将 mode `0400` 恢复为 `0600`、以新 writer FD 重新绑定同一 inode、truncate 并完整重写；partial content 本身不作为认证信号。只有没有可复用 inode 时才按最多 8 个 pending entries 与 512 MiB aggregate logical bytes 做最坏 64 MiB 预留。Fixed document durable 后，以同样的 bounded identity/access checks 删除该 family/plan 的 superseded pending residue，使旧版 over-cap history 可以单调减量而不是永久阻断。Existing fixed document retry 仍重新 `fsync` containing directory 并复验 exact identity/access/content；existing marker 在 durability repair 前后均执行完整 adoption verification，并绑定最初 retained marker inode。
- 每次 pending reader/writer `open` 前先登记 `opening` placeholder；`open` 返回后立即绑定 exact FD/path/identity/access，再允许任何 `fstat`、`stat`、read 或 write。Close 前先将 custody 标为 `close-uncertain`，只有一次 `close` 明确成功才移除。Writer 成功关闭后，独立 verifier reader 继续持有并注册 custody，覆盖 content re-read、fixed-name rename、parent-directory `fsync`、最终 identity/access/content verification 与 superseded-residue cleanup；只有完整 publication 成功后才将 FD 转移给 final binding 并显式 release。任意 close-before/after-effect 或并行 residue cleanup 不确定性都保留不可再操作的历史 FD 记录及全部 recovery transaction leases，后续 plan/execute 在任何 open 前要求进程重启；不得对可能已复用的 numeric FD 重试 `fstat` 或 `close`。
- 初次 marker publication 不再在 final adoption verification 前关闭并丢弃 binding。原 marker descriptor、payload 与 publication record 保留到 verifier 返回之后；fixed name 再按 exact identity/access/bytes 复验，且 `verification["marker"]` 必须与 publication record 完全一致。
- Primary-parent no-replace publication 若在 effect 前失败，只在 digest-named staging pathname 仍绑定 held descriptor 的 exact identity、mode/uid/gid policy 且目录为空时，才以 trusted-home descriptor 执行 `rmdir` 并 fsync/revalidate home。Missing、replacement、nonempty、unreadable 或 rename-after-effect 状态均保留并以 secondary cleanup failure 停止；不把 staging prefix 当作删除授权。
- 首次已经观察到 primary namespace 存在后的 bind failure 不再被二次 lookup 降级为 absence；若 earlier prebind 已证明存在，execute 内 lookup 缺失也在任何 staging mutation 前停止。
- Committed adoption 的完整 recovery-cap verifier 在普通 256-entry stale-recovery gate 之前运行；合法的大 inventory 不会因旧运行时普通 cap 被误拒绝。
- Receipt-only crash 保持 blocked/retryable；marker 只有在 marker → receipt → current complete manifest 与 whole-registry terminal revalidation 全部通过后才允许 `adopted-retained-in-place`。
- 新 runtime 接受 verified adoption；旧 runtime 仍以 `legacy-recovery-pending` 阻止 allocation。所有后续新对象只进入 `primary-home-v1`。

## Delivery Boundary

- 本 workstream 交付 repository code、tests、docs、source lock、fresh named single review 与 signed checkpoint；push/PR lifecycle 由交付协调流程在收到冻结证据后执行。
- 不执行真实主机 recovery，不修改 legacy retained evidence，也不执行其他 host mutation。
- Private source-sync/release、`#139`/`#140`、host installation、scheduler/LaunchAgent/doctor 与 HDR deployment 由外部 closure task 独占；本 workstream 到达 canonical exact identity 后只交接，不接管下游写入或协调。

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
- 竞态 namespace 修复形成签名 checkpoint `a80cbc02c8c479a250b2e9bd01f724423b965b66`，tree `db3b2fe862ad8d4338d5a433f9946591dd333bcc`，其完整 Python 3.13.0 `unittest` 通过 1120 tests in 1526.963s（3 skipped，0 failures/errors）。针对该精确 checkpoint 的 fresh named single review 返回一项 P2：recovery plan、owner、receipt 与 marker 的部分 numeric 字段仅使用 Python 容器相等与成员检查，会把 `True`/`1`/`1.0` 视为等值，即使攻击者重算合法 digest 也可能让错误 JSON 类型通过 schema 与交叉文档绑定。
- 针对该 P2 的 repair 在 canonical generator 与 standalone runtime 中对称加入 exact JSON type/schema validation：整数语义要求 `type(value) is int`，timeout 要求 exact `float`，owner、identity、access、inventory、receipt、marker 与 terminal record 均按完整字段验证。Plan/live-plan/receipt/marker/terminal 之间的结构绑定改用 compact canonical JSON bytes，避免 Python coercive equality（强制类型相等的宽松数值比较）。新回归在重算 `plan_digest` 或 terminal digest 后注入 bool/float 替换，仍必须以受控 domain error 拒绝；owner `phase`/`private_state` 的非字符串容器也先做类型检查，不可泄露 `TypeError`。
- 该 repair 的新回归 1/1 与完整 `PrivateControlRetainedRecoveryTests` 29/29 通过；Ruff 0.13.2、Python 3.13.0 `py_compile` 与使用 task-private `PYTHONPYCACHEPREFIX` 的系统 Python 3.9.6 `py_compile` 通过。一条独立只读完整字段映射与另一条 dirty-patch 审计复核了两实现对称性、digest 重算回归和合法文档接受面；在修复 owner 容器类型的受控失败后，复审终态 clean。
- Stock `refresh-lock` 与 stock `refresh-lock --check` 通过未修改 CLI，在不读取 production retained evidence 的 task-private `task-primary-home-v1` 中各验证 6 sources。Locked engine SHA-256 更新为 `8c5d752e0fdd8e6b10de62c998df1010e6143443e0cf0f2682606bf10626dd83`，`sync-source-lock.json` SHA-256 更新为 `35e60d760e23c8389782be70297a00b7eeb74993632144aeb58bee89174a5f0a`。
- 通过仓库 private-`TMPDIR` wrapper 的完整 `tests.test_source_lock` 在 uv Python 3.13.0 下通过 260/260 tests in 816.576s（1 expected skip）；直接执行物理 mode `0644` 的 wrapper 曾在测试启动前以 exit 126 停止，计数运行改由 `/bin/bash` 解释同一未修改脚本。
- 该 repair 的完整 uv Python 3.13.0 `unittest discover -s tests` 通过 1121 tests in 1101.126s（3 skipped，0 failures/errors）。CI 单列的 `macos_system_temp_alias` 8/8 与 `macos_git_locator` 6/6 在 uv Python 3.13.0 和系统 Python 3.9.6 下均通过；repo-wide `compileall -q scripts tests` 也在两个 runtime 下通过。Project-journal validation、最终 stock `refresh-lock --check`、JSON parsing 与 `git diff --check` 均通过。
- 上述 exact repair 形成签名 checkpoint `e350fa877e3f487a2b95757913f59c9b5612eb3e`，tree `292813658730362785628d65e189a602224843f0`。通过 prior-b4ca guard materialize/first-status validate 并在 last-moment exact revalidation 后启动的唯一 fresh-context named single 返回一项 P2：receipt/marker write、`fchmod` 或 `fsync` 持续失败时，每次随机 nonce retry 都会遗留一个新 pending artifact，而 primary namespace 没有总 entry/byte cap，可无界消耗 inode 与磁盘。该 lane 没有启动 Claude preflight、Claude actual 或读取凭据。
- 当前未提交 repair 在 generator 与 standalone runtime 中对称新增 parent-lease-bound pending capacity gate、same-plan inode reuse、durable-publication 后的 bounded residue cleanup，以及 pending descriptor custody/process-lifetime close fence。Stable scan 继续以既有 100,000-entry scan cap 限制 enumeration，合并识别两类 pending prefix并执行两次 name/object/access/size snapshot；只有新建 inode 才要求 `existing_count + 1 <= 8` 与 `existing_logical_bytes + 64 MiB <= 512 MiB`。同-plan reuse 在新 writer 打开前后都复验 inode/access/nlink/size，完整重写后才允许 fixed-name publication；writer→verifier handoff 保持 reader custody 直到 fixed document durable、最终 revalidation 与 bounded residue cleanup 全部成功，再显式转移给 final binding。任何 reader/writer 或 residue close/open uncertainty 均保留 process-lifetime fence。旧版 9-entry history 与 aggregate over-cap history 均可在成功 publication 后清空。
- 新增 old-history drain、fresh/reused writer close-before/after-effect、reader close-before/after-effect、writer→reader replacement、rename failure、numeric-FD sentinel reuse、plan/execute pre-open fence，以及 reader/writer pre-open custody regressions；replacement 回归还精确绑定注入对象的 inode、bytes 与 mode，防止 mutation-before-error 假绿。完整 `PrivateControlRetainedRecoveryTests` 在 uv Python 3.13.0 与 macOS system Python 3.9.6 下各通过 40/40；完整 `tests.test_source_lock` 在 repository private-`TMPDIR` wrapper 下通过 271/271 in 582.266s（1 expected skip），完整 `unittest discover -s tests` 通过 1132/1132 in 829.875s（3 expected skips）。CI 单列的 `macos_system_temp_alias` 8/8 与 `macos_git_locator` 6/6 在两个 runtime 下均通过，repo-wide `compileall -q scripts tests` 也在两个 runtime 下通过。两份实现与测试通过 Python 3.13 `py_compile`、Ruff E4/E7/E9/F 与 `git diff --check`；独立只读状态机审计未发现剩余 P1。最终未修改 stock `refresh-lock` 与 `refresh-lock --check` 在 task-private account-home 中各覆盖并通过 6 sources；locked engine SHA-256 为 `e3aa5e58146ef7150c949860922a220e02bae303fd305d6a1765e55fd6d1f17c`，`sync-source-lock.json` SHA-256 为 `d3be60d14495ad4e33bd6150f7b805da0e3aec4fbc4e12c57823a70fc9b44fc2`。Final signed checkpoint 与新 head 正式 review 尚未完成，因此不作最终 clean claim。
- 上述 pending-capacity/custody repair 形成签名 checkpoint
  `0a22012a05e38881630c7fb4af07eaa6a041812e`，tree
  `25d41556963889694cf0078fb45c5bde767b1e9c`。该 exact head 的唯一
  fresh-context named single 返回一项 P2：generator 的 adopted legacy
  manifest 每次另建 300 秒 deadline 和局部计数，未消耗 normal mirror
  operation 已有的 deadline、entry 与 byte budget；同一 retained root 在一次
  transaction 中的多次复验因此可能绕过 operation-wide resource bound。
- 当前 repair 只收紧拥有共享 `OperationBudget` 的 canonical generator normal
  flow；standalone recovery CLI 没有该外层预算，继续保留原有 recovery-local
  timeout/caps。Preflight 与所有 terminal receipt revalidation 现在把同一个可变
  budget 贯穿 adoption verifier、plan、manifest、递归 directory scan 与双遍
  regular-file read。Effective deadline 取 recovery deadline 与 operation
  deadline 的较小值；每个实际枚举 name 消耗一个 entry 及其 UTF-8 bytes，每个
  实际 read chunk 消耗 byte budget，因此重复 manifest 不会重置计量。新增回归
  分别锁定已过期 operation 在 manifest 前失败，以及恰好一轮 manifest 的
  entry/name/double-read bytes 在首次 preflight 后归零、第二次 revalidation
  必须因同一 aggregate budget 失败。
- 两项新回归 2/2 通过；完整 `PrivateControlRetainedRecoveryTests` 在 uv Python
  3.13.0 与 macOS system Python 3.9.6 下各通过 42/42。完整
  `tests.test_source_lock` 通过 273/273 in 595.151s（1 expected skip），完整
  `unittest discover -s tests` 通过 1134/1134 in 851.869s（3 expected
  skips）。CI 单列的 `macos_system_temp_alias` 8/8 与 `macos_git_locator` 6/6
  在两个 runtime 下均通过；Ruff E4/E7/E9/F、双 runtime `py_compile` 与
  `git diff --check` 通过。一条独立只读预算传播审计复核 normal-flow 四次
  adoption manifest 共用同一 budget、standalone caller 兼容和两项测试的
  反事实敏感性，终态 clean。未修改 stock `refresh-lock` 与
  `refresh-lock --check` 在 task-private account-home 中各覆盖并通过 6
  sources；locked engine 与 `sync-source-lock.json` SHA-256 仍分别为
  `e3aa5e58146ef7150c949860922a220e02bae303fd305d6a1765e55fd6d1f17c` 和
  `d3be60d14495ad4e33bd6150f7b805da0e3aec4fbc4e12c57823a70fc9b44fc2`。
  Final signed checkpoint 与该新 head 的正式 named single 尚未完成，因此不作
  final clean claim。
- 上述 manifest-budget repair 形成签名 checkpoint
  `d0589062cff768a34693f0456a95e2360e79e11b`，tree
  `e79fbabc503ee331bb73011e87a6097a42d83847`。通过 prior-b4ca guard 的
  pre-status materialize 与完整 7-guidance validate、exact-secret admission、
  endpoint closure、strict full fsck、local `GOODSIG`/`VALIDSIG` 和 last-moment
  revalidation 后启动的唯一 fresh-context named single 返回一项 P2：normal
  generator 已将 live retained manifest 纳入共享 `OperationBudget`，但 adoption
  verifier 对 fixed cutover marker 与 receipt 的初次绑定和最终复验仍各做双遍
  read，未检查同一 deadline 或扣减 aggregate byte budget。一次 verify 因此对
  两份 fixed document 各读四遍，完整 generator transaction 的多次 verify 仍可
  绕过 operation-wide bound。该 lane 没有启动 Claude 或读取凭据。
- 当前 repair 给 bound-file read、optional bind 与 revalidation helper 增加默认
  `None` 的 optional operation 参数，并由 adoption verifier 的全部 marker/
  receipt 分支透传。每次实际 read chunk 都先检查共享 deadline，再按实际 bytes
  扣减 aggregate budget；marker/receipt schema decode 前也做 deadline
  checkpoint。Standalone plan/execute/publication caller 继续省略 operation，保留
  原有 recovery-local 64 MiB cap 与 timeout 语义。新增 expired-operation 回归要求
  在任何 fixed-document I/O 前失败；exact-one-verify 回归把
  `4 * (marker_size + receipt_size)` 纳入精确预算，要求首次 preflight 后 bytes/
  entries 同时归零、第二次 receipt revalidation 因同一 aggregate budget 拒绝。
- 两项新回归在 uv Python 3.13.0 与 macOS system Python 3.9.6 下各通过 2/2；
  完整 `PrivateControlRetainedRecoveryTests` 在两个 runtime 下各通过 42/42。
  完整 `tests.test_source_lock` 通过 273/273 in 591.956s（1 expected skip），完整
  `unittest discover -s tests` 通过 1134/1134 in 821.931s（3 expected skips）。
  一条独立只读 caller-chain 审计复核 normal generator 最多四次 verify 共用同一
  budget、每次 marker/receipt 四遍读取的完整传播，以及 standalone 兼容边界，
  未发现漏 caller。最终 uv Python 3.13.0 与 macOS system Python 3.9.6 source
  compile、Ruff 0.16.1 E4/E7/E9/F、source-lock JSON parsing、project-journal
  validation 与 `git diff --check` 均通过；未修改 stock `refresh-lock --check` 在
  worktree 同级、owner-private、task-only `task-primary-home-v1` 中验证 6 sources，
  临时 control home 随后清理。签名 checkpoint 与该新 head 的 fresh named single
  仍待完成，因此不作 final clean claim。
- Complete-receipt capacity repair 形成签名 checkpoint
  `cbe52a3f7a1fbdf864c141b2ebbac3513af685de`，tree
  `b256160731ff465e8ab4ae49e545449a54fa3c8f`。其唯一 prior-b4ca
  fresh-context named single 返回一项 P2：primary-parent 的 no-replace
  publication 在 effect 前失败时只关闭 staging descriptor；retained evidence
  改变 plan digest 后，旧 digest-named empty directory 不再可达，重复失败可无界
  累积 account-home inode。该 lane 未启动 Claude，也未读取凭据。
- 当前对称 repair 在 publish-error 分支中保留 staging descriptor，通过 trusted-
  home descriptor 双端复验 named/held identity 与 exact access policy，双次证明
  目录为空并在 effect 前再次复验，再执行 parent-anchored `rmdir`、证明 name
  absent 与 held object 未变，最后 fsync/revalidate home。Missing、replacement、
  nonempty、unreadable、rmdir 或 durability uncertainty 均保留状态并 secondary-
  fail；rename-after-effect 不会触碰已发布 primary namespace。
- 双实现回归覆盖两个不同 plan digest 的连续 rename-before-effect failure、
  nonempty preservation、replacement preservation 与 rename-after-effect published-
  identity retention，在 uv Python 3.13 与 macOS system Python 3.9 下各通过 1/1；
  完整 `PrivateControlRetainedRecoveryTests` 在两个 runtime 下各通过 45/45。
  Repository private-`TMPDIR` wrapper 下完整 `tests.test_source_lock` 通过
  276/276 in 563.237s（1 expected skip），完整 repository discovery 通过
  1,137/1,137 in 830.495s（3 expected skips）。Task-private stock refresh/check
  各验证 6 sources 并清理 control home；`sync-source-lock.json` SHA-256 为
  `55c8c3f02cdb196143fd383a2fcd7e6fb0912c88d04a4fbe285488a6f2effabd`。
  两个 runtime 的 `py_compile`、Ruff 0.16.1 E4/E7/E9/F 与
  `git diff --check` 通过；superseding signed checkpoint 与 fresh current-head
  named single 尚未完成，因此不作 final review-clean claim。
- Staging cleanup repair 随后形成签名 checkpoint
  `10b0ab3285d898cadb6b533335ad16d632a1dd65`，tree
  `1de01af544dcf867808143d9840543121145e2cf`，其唯一 prior-b4ca
  fresh-context named single 返回两项 finding。第一项指出 recovery plan 与
  pending document 的 raw `os.write()` loop 会在零进展时永久循环，并在连续
  short write 时反复复制 suffix 且绕过 recovery deadline。第二项指出递归
  evidence scan 返回后的 pathname/descriptor 终态重验证让 `ENOENT`、
  `EACCES` 或 descriptor `EIO` 越过 `SyncError` / `MirrorSyncError` 边界。
- Final target-branch repair 在 generator 与 standalone runtime 中对称加入
  `memoryview`-based write-all helper：每次 syscall 前检查 recovery-local
  deadline、拒绝 `written <= 0`，且 plan/pending caller 保留原有 contextual
  domain error。终态目录重验证分别将 pathname missing、其他 pathname lookup
  failure 与 descriptor failure 包装为 domain errors；原有 object identity
  `(st_dev, st_ino, type)` 与 access policy `(mode, uid, gid)` mismatch 分类保持
  不变，mtime/ctime 等 benign metadata 仍不参与 protected property。
- 三项新增故障注入在 uv Python 3.13 与 macOS system Python 3.9 下各通过
  3/3，完整 `PrivateControlRetainedRecoveryTests` 在两个 runtime 下各通过
  48/48。Repository private-`TMPDIR` wrapper 下完整 `tests.test_source_lock`
  通过 279/279 in 565.529s（1 expected skip），完整 repository discovery 通过
  1,140/1,140 in 813.382s（3 expected skips）。未修改 stock `refresh-lock`
  与 `refresh-lock --check` 各覆盖并通过 6 sources；locked engine SHA-256 为
  `ae87018e9e1f679c1745aaf50a93b899eacd6ce7e782891e4e8adb93178ae835`，
  `sync-source-lock.json` SHA-256 为
  `97be8527892c73857cfd3e6c0dcc770805494829925def1c45fde666b1507b06`。
  双 runtime `py_compile`、Ruff 0.16.1 E4/E7/E9/F、JSON parsing、project-
  journal validation 与 `git diff --check` 通过；一条独立只读 repair sanity
  check 未发现 actionable finding。
- 上述 repair 形成签名 checkpoint
  `b34445a3597c19b3081def2048fc9b06f01330ba`，tree
  `eaa0abcdc27e9b9a0a74524419ce4566ac3f6b37`。其唯一 prior-b4ca
  fresh-context named single 返回一项 P2：digest-named primary-parent staging
  在 `mkdir` 成功、随后 containing-home `fsync` 失败时尚未绑定
  descriptor，空目录会被遗留；证据变化产生新 digest 后，重复失败可无界累积
  account-home inode。该 lane 未启动 Claude，也未读取凭据。
- Final repair 在 generator 与 standalone runtime 中保持对称：只记录本次调用
  是否实际创建 staging，`mkdir` 返回后立即以 trusted-home descriptor 绑定 exact
  `(st_dev, st_ino, type)` 与 `(mode, uid, gid)`，再执行 containing-home
  durability `fsync`。若该 `fsync` 失败，只对本次创建且仍绑定 held descriptor、
  current-UID mode-`0700`、双次证明为空的 exact object 执行 parent-anchored
  `rmdir`，随后 fsync/revalidate home；pre-existing、replacement、nonempty、
  unreadable 或 cleanup durability uncertainty 均保留并 secondary-fail。
- 精确 post-mkdir `fsync` fault injection 在两个 runtime 下各通过 1/1，完整
  `PrivateControlRetainedRecoveryTests` 各通过 48/48。Repository private-
  `TMPDIR` wrapper 下完整 `tests.test_source_lock` 通过 279/279 in 566.160s
  （1 expected skip），完整 repository discovery 通过 1,140/1,140 in
  810.288s（3 expected skips）。未修改 stock `refresh-lock` 与
  `refresh-lock --check` 各验证 6 sources；locked engine SHA-256 为
  `12164e7ed9de0f6f6f2d5a3f4859653736cb849a82bb69bc77669856c86d92c8`，
  `sync-source-lock.json` SHA-256 为
  `0018191ae5e25c7ab1d78f99051a90a7129b18e08c6a15d4ce866868fc04f837`。
  双 runtime `py_compile`、Ruff 0.16.1 E4/E7/E9/F、JSON parsing 与
  `git diff --check` 已通过；superseding signed checkpoint 与该 exact head 的
  fresh named single 尚待完成，因此不作 final review-clean claim。
