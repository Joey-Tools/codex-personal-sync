# 架构

## 权威来源与镜像边界

`Joey-Tools/codex-personal-sync` 是同步引擎、manifest schema 和兼容性测试的 canonical source（唯一权威可编辑来源）。[`sync-source-lock.json`](../sync-source-lock.json) 记录 canonical 文件的 SHA-256、mode，以及它们在下游仓库中的目标路径；[`scripts/sync_canonical_mirrors.py`](../scripts/sync_canonical_mirrors.py) 只允许显式选择一个 mirror 和一个现有 target root。

- `toolbox` 和 `private` mirrors 都消费 lock 声明的引擎、schema，以及 reconciliation safety、release retention、scheduler/doctor 定向测试。
- 只有面向 `Joey-Tools/codex-toolbox` 的 `toolbox` mirror 额外消费完整 `tests/test_codex_personal_sync.py`；`private` mirror 面向 `Joey-Tools/codex-private-workflows`。
- `refresh-lock` 只读取 canonical source；`managed-paths` 只读地返回当前 target 与经旧 receipt/HEAD/index/worktree 共同证明的退役 target；`generate` 只执行 canonical → consumer 的条件写入/删除；`check` 只比较已声明的 bytes 和 mode。任何 canonical hash/mode 漂移都会先失败。
- consumer 中的生成文件不是反向输入，也不应手工修补。新增 mirror 文件必须先进入 source lock，不能靠目录级复制扩大边界。

镜像工具对 root、祖先目录和文件执行 no-follow 检查，以 `(st_dev, st_ino)` 绑定已打开对象。文件的受保护性质是 object identity、access policy、完整 size/hash/bytes；同一已打开文件会完整读取两次并比较内容。`mtime`/`ctime` 不参与内容稳定性判断，单独的时间戳变化不是 mutation 证据。

`check` 还要求所有 managed target 与 receipt 的 clean stage-0 index 精确等于 `HEAD`，因此 staged-only 漂移也会失败。`generate` 在配置 managed ancestor 或写入任何 consumer path 之前，还会用 NUL-delimited raw Git records 完整枚举 consumer 的 stage-0 index 和递归 `HEAD` tree；两者的 path、mode 和 object ID 必须逐项相同，所有 path 必须是无歧义的严格 UTF-8 safe POSIX path，且 entry、byte、time 和 aggregate operation caps 均适用。所有非 managed tracked path 会加入 NFC+casefold portable trie；它们不能与 managed target、receipt、transaction 或 exchange recovery path 构成等价、祖先或后代关系。已存在且 raw path 精确等于 managed target 的 clean tracked entry 仍被允许。该完整 namespace snapshot 在每次 publication 边界重新验证，`HEAD`、index 或 tracked namespace drift 都会 fail closed。`generate` 可以留下预期的未提交 generated diff 供审阅，但不会把 index/worktree 内容当作 canonical 输入。canonical `master` 到 toolbox 的自动化只通过 [sync-toolbox workflow](automation/sync-toolbox.md) 创建或更新固定范围的 PR；缺失显式的最小权限 secret 时会在 target checkout 前失败，不会自建凭据或直接写 target `master`。

Git 验证不执行 repository-controlled `git status`，而是用 raw tree/index blob 与 no-follow worktree bytes 做 filter-free exact parity。Live control binding 覆盖 `HEAD`、resolved loose ref（存在时）、`packed-refs`、common/worktree config、index 及 optional-control absence，并在每个 Git/文件系统 gate 前后重验证。Linked-worktree admin 中实际存在的 `commondir` 不只绑定文件对象与内容，还绑定 NFC+casefold 后的完整碰撞集合；后续新增大小写/规范化 alias，或把原名称改成同碰撞集合中的另一 spelling，都会在 private snapshot、Git 子进程或终态验证前 fail closed。绑定 `.git` marker、admin/common/object directories 与固定 Git executable 后，generator 先执行一次受限的 `git --no-lazy-fetch --version` capability probe；只有 Git 2.45+ 的单行规范输出可继续。在任何 repository/object Git 命令之前，private object manifest 直接拒绝大小写不敏感的 `.promisor` 与 alternate markers，private `config`/`config.worktree` 只通过 bounded `git config --file --no-includes` 解析并按 key presence 拒绝 include、partial-clone 与 promisor 配置。Private snapshot 的 content-addressed `objects/` 在 materialization 时完整复制，并把每个 pack/idx/loose file 的 path identity、access policy、size、change signal 和 SHA-256 绑定为 manifest；之后每个 Git argv 都显式携带 `--no-lazy-fetch`，并在子命令前后重验完整 manifest。稳定期间不重复读取 pack bytes，但任一 file/entry identity、policy、size 或 mtime/ctime change signal 漂移都会使 content-stability proof 失效并 fail closed，不能用“后来恢复成相同 bytes”重新接纳。目录时间戳仍不参与判断，普通 child-entry churn 本身不是对象替换证据。固定的 isolated Python launcher 通过 private Git directory fd 切换 cwd 后 `execve` 固定 Git，不使用 `preexec_fn` 或 pathname cwd。

Target layout 对 NFC+casefold 后的 portable spelling 做比较，并双向拒绝 managed target 与 receipt、transaction markers、per-target exchange journal 的 exact/ancestor/descendant overlap；因此 reserved metadata 不能伪装成 target，target 也不能把 reserved path 包在自己的子树中。

Private Git snapshot 位于两个 repository 之外的 durable 0700 current-owner tool root。Owner/phase record 全程持锁；tool-root 临界区覆盖 stale scan、snapshot directory 创建和 owner publication，避免 live directory 被误判为 orphan。Stale cleanup 只删除 record 绑定的 exact identity，未知/替换状态进入 bounded quarantine 或 fail closed。正常与 stale owner-record cleanup 都在任何 control pathname 移动前，预绑定 private-control parent 旁边、同一 verified filesystem 上的独立 durable quarantine；因此 repository 与 `/private/tmp`/`/var/tmp` 分属不同 mount 时也不会退回 repository sibling，更不会让 active tool root 被预期 cleanup 记录填满。内部 transaction temporary、journal 和 owner record 使用完整绑定 object identity、exact content、mode、uid 与 gid 的 `transient-file-*` 名称；receipt-bound retired target 使用 `recovery-file-*`。两类 entry 都保留作 identity-aware recovery，synchronizer 不把可伪造的 filename prefix 当成自动物理删除授权；旧 `file-*` 或缺少完整 access-policy binding 的旧 transient 名称也保持未分类、fail-safe retained。新 entry 的容量在 quarantine lock 下、source pathname 移动前预留；exact 10,000-entry 容量下 quarantine 仍可绑定并读取 evidence 以诊断 recovery，但需要新增 retained journal 的 cleanup 会在 journal pathname 移动前阻塞，并保留 active journal，直到 identity-aware 人工维护释放容量。单个 canonical source 与单条 Git stdout 都限制为 32 MiB；同一 operation 共享 deadline、aggregate byte budget 和 entry budget，覆盖 snapshot、Git、recovery、generation 与 cleanup。

若 target 中已有 pending generation，recovery 必须先读取 journal 所绑定的旧 canonical commit 与旧 source lock；不能用新 `HEAD` 重解释旧 transaction。若 caller 同时请求了不同的新 commit，本次调用在完成旧 recovery 后明确失败并要求先审阅/提交恢复结果，不能把旧 receipt 静默当作新生成成功。

新 lock 删除或重命名 target 时，旧 receipt 不是宽泛删除授权。Generator 要求 receipt 指向 locally available 且可达当前 canonical commit 的历史 commit，从该 commit 的 source lock、source tree 和 mirror mapping 逐字节重建 receipt；仅内部 digest 自洽不构成 provenance。它还要求 receipt 及其中每个旧 target 都与 consumer 的 `HEAD`、stage-0 index 和 worktree 精确一致。Pending journal 的 path set 必须精确等于可信旧 receipt 与当前 mapping 的独立并集，不能自行扩张 retirement authority。只有不再出现在新 mapping 中的 exact objects 才进入 desired-absent transaction。

Removal 先写 version-2 per-target journal，再 no-replace isolate。Journal 在 mutation 前绑定 exact `recovery-file-*` name、expected object/content record 和“matching durable entry proves removal” phase contract。通用 macOS/Linux 没有 compare-by-inode unlink，因此 receipt-bound target 的不可逆终态不是按 pathname 删除，而是原子移动到 target sibling 的 `.codex-sync-canonical-mirror-quarantine`。该 root 必须为当前 uid 所有、mode `0700`、与 repository 不重叠并受 10,000 entry 上限约束；移动后再次验证 object identity、content 和 access policy。Recovery 只有在 quarantine lock 内、active journal cleanup 紧邻的前后两次读取都证明 exact durable entry 仍匹配时，才接受 target/temporary 双缺并提交 cleanup；cleanup 中途 evidence 漂移会 fail closed，并报告已保留的 exact recovery artifact。Legacy journal 或 missing/mismatched evidence 保留 journal 并报告 ambiguous。任何 replacement、dirty bytes、跨文件系统失败或不明确状态均保留 journal/quarantine 证据并停止。Receipt-bound recovery bytes 的物理删除仍需要单独的 identity-aware 人工维护，不属于 generation transaction。

当 active targets、retired absences、mode 与 final receipt 已完全等于同一 canonical commit 时，`generate` 在重新验证 canonical sources、repository identity、stage-0 index 和完整 object group 后直接返回，不创建 transaction 或 quarantine artifact。这个 no-op fast path 保证未变化的定时重复运行不会确定性耗尽 durable quarantine。

## 安装模型与所有权

核心实现位于 [`scripts/codex_personal_sync.py`](../scripts/codex_personal_sync.py)，manifest 的静态结构见 [`schema/sync-manifest.schema.json`](../schema/sync-manifest.schema.json)。运行时继续负责 schema 无法表达的路径可移植性、跨字段、owner、byte limit 和文件系统检查。

```text
~/.codex/
├── personal-sync/
│   ├── releases/<sha>/                 # public immutable releases
│   ├── current -> releases/<sha>
│   ├── overlays/<owner>/
│   │   ├── releases/<sha>/             # overlay immutable releases
│   │   └── current -> releases/<sha>
│   ├── state/managed-links.json
│   ├── pins/<owner>/<sha>.json
│   └── quarantine/...
└── skills/...                          # active managed links
```

### Immutable releases

Release 先复制到同一 `releases` root 下的临时目录，经过 manifest、全树 digest、sanitized mode 和 source stability 复核，再通过 no-replace rename 发布为 `<sha>`。发布输入和已安装树都拒绝 `__pycache__`、`.pyc`、`.pyo` 等 cache artifact；scheduler 也设置 `PYTHONDONTWRITEBYTECODE=1`，避免运行时污染 release。相同 `owner@sha` 已存在时，只接受与 verified incoming source 完全匹配的 tree；不覆盖或就地更新。这里的 immutable 是协议不变量，不是依赖文件系统 immutable flag。发现同 SHA tree mismatch 时使用稳定分类 `immutable-release-drift`，保留原 release 与恢复证据。

### `current` pointers

每个 owner 只有一个相对 symlink `current -> releases/<sha>`。切换 `current`、更新 active links 和发布 managed ledger 属于同一 pending transaction：先持久化 before/after evidence，再按精确对象身份执行，最后以 managed-state commit marker 决定恢复方向。

### Managed ledger

`personal-sync/state/managed-links.json` 是生成链接的受管账本：

- `owners` 绑定 owner 到 active release SHA；
- `links` 为每个 target 保存 `source`、`target`、`kind`、`owner`、`link_target` 和 `release_sha`。

账本只证明已知生成链接的精确 claim，不授予删除任意本地路径的权限。每次替换或移除仍会复核 `current`、release manifest、live symlink target 和对象身份；非 symlink、foreign target 或无法重验证的路径均 fail closed（无法证明安全时停止并保留证据）。

账本 before-state 的 bytes、identity、type、size、mode、uid 与 gid 都是绑定信号；其中 mode/uid/gid 共同表达原文件的 access policy。新 after-state 固定为 regular file、mode `0600` 且 owner 为当前 effective uid，因此 group 不再具备访问语义，仍必须显式验证 owner。

### `removed_links`

`removed_links` 是 manifest 中显式、带 owner 的历史迁移证据，用于识别不在旧 ledger 中、但仍精确指向历史 release source 的 symlink。每条记录以 `owner:id` 唯一标识，并绑定 `source`、`target` 和 `kind`：

- `replacement_target` 要求替代链接在 destructive transition 前可用；
- `retires_replacements` 形成无环 retirement graph，允许后续 manifest 明确结束旧 replacement obligation；
- 只有 link target 与记录完全匹配时才可 quarantine-replace 或 quarantine-remove；普通文件、目录及不匹配 symlink 不会因此被删除。

## Scheduler 合同

macOS 使用 user `launchd`，Linux 使用 user `systemd`。默认 runner 固定为 `~/.codex/bin/codex-personal-sync`，scheduler 始终调用稳定入口 `run-scheduled`，而不是绑定某个 release 目录中的脚本。

安装 scheduler 前会安全解析现有配置：

- 未显式传入 interval 时，保留已审计配置的现有 interval；没有现有配置时才使用默认值。
- runner、command、mode、repo、base repo、owner 和 interval 全部匹配时，不改写配置文件；`enable=true` 仍执行 daemon reload/enable repair，以恢复被外部禁用的服务状态。
- unsafe、不可解析或不完整的配置会报错，不会被当作可保留配置。
- semantic audit 直接解析 caller 捕获的完整配置 snapshot；配置匹配分支在调用 daemon 前重验证该 snapshot，配置变更分支则把同一 snapshot 传给 conditional write。macOS 绑定单个 plist，Linux 绑定完整 service/timer pair，audit 后出现的同 inode 内容变化、路径替换或父目录对象轮换都不会被覆盖。Atomic writer 返回自己最终验证的 exact installed snapshot。macOS 从该 snapshot 保持 plist 与 parent 的只读 descriptor 直到 activation 结束，并在 legacy bootout/disable、current bootout、bootstrap、enable 的每个 native action 紧邻前后复验 canonical path、parent chain、object identity、exact bytes 和 mode/uid/gid；missing、unreadable、replacement、content 和 access-policy failure 保持不同诊断，且任何失败都不会打印安装成功。`mtime`/`ctime` 不属于这些 protected properties。Linux 同样从 writer 返回值持续绑定 service/timer 的对象身份、bytes、mode/uid/gid 与父目录身份，不允许用 write 后重新读取的同 bytes 新 inode 或 mode drift 建立新 baseline。每次 pair revalidation 在同一个 bound unit-parent fd 上同时保持两个 unit descriptor，先双读两个 FD，再终态复验两个 canonical name、两个 fstat 与 parent；service 首读后、timer 读取前发生的 replacement/access drift 也会被拒绝。该验证在 pair commit、daemon reload 前、reload 后且 enable 前、enable 后且 start 前，以及 start 后分别执行。
- 每个已存在的 legacy launchd plist 也在 cleanup 前独立绑定 parent/file descriptor、exact bytes、uid/gid、mode 与对象身份；legacy `bootout` / `disable` 的每个原生边界同时复验 current 与 legacy binding。删除使用同目录 no-replace isolation，并只删除复验为原 snapshot 的隔离对象；竞态替换不会被当作原文件删除。仅 `mtime` 变化仍允许 cleanup。
- 替换现有 plist/unit 前，会先为 exact original object 创建同目录 hard-link recovery evidence；`link(2)` 一成功就进入 preserve 状态，后续 fsync、snapshot 或 descriptor binding 失败都不会由 finally 删除该 link。Recovery revalidation 同时绑定 canonical parent fd、named path、只读 descriptor、对象身份、bytes 与 access policy。成功 cleanup 在 displaced preimage 仍是第二个 durable locator 时先清理 `.original`，随后再次验证并清理 displaced locator。若 exchange 后的 displaced pathname 被替换或交换成不能证明为该次 exchange 精确移出的对象，绝不把该 pathname 交换回 live；可信 staged config 保持 live，descriptor-bound original 与 untrusted displaced locator 都保留并 fail closed。
- Linux service/timer 以 durable pair transaction 更新。每个 conditional write 绑定 caller 捕获的完整 before snapshot；crash recovery 只接受记录中的 before/after content 与 access policy，未知并发编辑一律保留 marker 并 fail closed。新 after 文件固定 mode `0600`，因此 group identity 不具备 access-bearing semantics（不影响访问权限）且不作为 after-state 匹配信号；legacy before-state 仍完整绑定原 gid/mode。
- 回滚 legacy before-state 时，staged file 会在 publication 前验证 caller group membership，并按记录的 gid 执行 `fchown`，随后重新应用完整 mode；任一步失败都在替换目标前停止。
- 非 dry-run 的 scheduler install/uninstall 使用现有 per-Codex-home install lock 覆盖 recovery、audit、transaction marker、config publication、legacy cleanup 和 daemon publication；并发配置操作不能观察或回滚另一操作尚未完成的 pair transaction。
- 调用 `launchctl` / `systemctl` 时使用固定的 root-owned executable 与 closed environment，不继承 loader、shell 或 Python runtime injection 变量。

每次 scheduled run 在 per-Codex-home install lock 内分配 canonical `+00:00` UTC attempt timestamp，再以 mode `0600` 原子写入 version 2 的 `personal-sync/state/scheduler-status.json`，记录本次 attempt 尚未完成。先前 attempt 只有在 canonical round-trip 成立、可安全递增且不超过当前时间 5 分钟时才参与单调分配；timestamp-only corruption 会在锁内清除不可信 attempt/success 与 release baseline 后自愈，结构、mode、target identity 等其他错误仍 fail closed。成功或失败完成时，在同一锁内比较 attempt timestamp 与完整 target identity、content/access policy 和状态父目录 identity；同一 inode 通过 hard link 出现在替换后的父目录中也不满足 CAS，mtime-only 变化仍被允许。Runtime candidate 可能进入 live 前先耐久发布固定的 publication-incomplete marker；它绑定 parent、expected live、staged identity/digest 及 temporary/recovery locators。只有 live、preimage cleanup、parent 与 marker 的终态验证全部通过后，专用 commit helper 才直接 unlink fixed marker；该 unlink 成功是 transaction 唯一的 commit linearization point，之后不再执行可失败的验证或 cleanup。Displaced mismatch、marker commit 失败或其他 post-publication 异常不执行第二次 exchange，并保留或 O_EXCL 重建 blocking marker。正式 reader 在同一 bound parent fd 上先对固定 marker 做 descriptor-relative no-follow stat，再以 NFC+casefold 比较 bounded directory scan 中的 marker、`.personal-sync-write-*` transaction residue 与 retained evidence；单个 portable alias 或受保护命名空间内多个 normalization-equivalent spelling 都 fail closed。Reader 在 status snapshot 前后重复该检查；任一时点存在、冲突或不可读的 publication evidence 都以 `scheduler-state-publication-incomplete` 拒绝 raw staged success。因此即使 marker 重建连续失败，保留的 displaced/recovery locator 仍阻止 staged success 被读取。Recovery cleanup 仍采用 cooperative same-UID 模型：descriptor 与 pathname identity 的终态复验会拒绝已观察到的替换，但 portable `stat`→`unlink` 不提供 inode-conditional unlink，不能宣称抵御可写 parent 中恶意 same-UID 的最后窗口。只有仍是 current 的 attempt 才能提交终态，较旧的重叠 run 不会覆盖较新 attempt 的结果。成功后写入 `last_success`，并为每个 current `owner@sha` 保存经过完整 release-tree 验证的 SHA-256 baseline。失败后仅在 target 未变且旧 `last_success <= attempt` 时保留上次成功时间和 baseline，并记录 bounded `failure_reason` 与稳定 `failure_code`。读取端兼容 version 1；旧 runtime target 与当前配置不一致时，不展示或消费旧 target 的 attempt/success/failure/baseline。没有可信 baseline 会单独报告 `immutable-release-baseline-missing`，不会把当前本地树直接补录成可信值。

`status-scheduler` 的文本和 JSON 视图共同遵守以下字段合同：

```text
platform, installed, enabled, config, interval_minutes, runner, stable_runner,
mode, base_repo, private_repo, last_attempt, recent_success,
current_release, release_integrity, quarantine_batches, quarantine_limit,
failure_code, failure_reason
```

`stable_runner` 只证明默认入口的 lexical/structural usability 及 managed-link claim，不代表 release bytes 完整；完整树问题由 `release_integrity` 单独报告。`doctor` 在 report 上增加 scheduler 安装、daemon enabled、immutable release、quarantine saturation、stable runner 和最近失败诊断，但不代替 install、repair 或 cleanup。

已安装的 scheduler 必须能解析其 mode 所要求的全部 `current`：public 要求 public current，private 同时要求 public 与 private owner current。任何一个缺失都会以 `current-release-missing` 使 report/doctor 不健康。Quarantine audit 只有在尚无既存失败时才可成为主失败；旧 runtime 中有 reason 但没有 code 的失败不会因此被覆盖。

`status-scheduler` / `doctor` 从 scheduler semantic audit 起持有 macOS plist 或 Linux service/timer 的 parent/file descriptor，直到 `launchctl print` / `systemctl --user is-enabled` 完成；原生查询前后同时复验 exact config 与 systemd drop-in audit。对象替换、同 inode byte 变化、access-policy drift、parent replacement、missing 与 unreadable 分别 fail closed 为 `scheduler-config-drift`，而原生命令失败或超时保持独立的 `scheduler-daemon-unavailable`。仅 mtime/ctime 变化不构成配置漂移；stable runner 不匹配则以 `scheduler-runner-drift` 保持 report 不健康。

## Active skill discovery 的只读审计

`status` 和 `doctor` 会调用 `audit_active_skills` 对 `~/.codex/skills` 做 bounded read-only audit（有界只读审计）：最多扫描 10,000 个顶层 entry，每个 `SKILL.md` frontmatter 最多读取 64 KiB，不执行清理、重链接或文件写入。`skills`、`.system`、skill directory 和 `SKILL.md` 都通过 held directory/file descriptor 读取并在结束时重验证；可替换父路径不能把一次 audit 混合成多个目录对象。

审计区分 managed skill、保留的 external root `.system` 和 unmanaged entry，并报告 broken symlink、cache/backup 暴露、managed drift、缺失 claim、invalid frontmatter 及 duplicate skill name。Cache/backup 检测也应用到 `.system` 的直接 child，避免保留目录绕过 discovery-root hygiene。无法列目录或读取必要状态是独立 issue，不会被解释为“没有问题”。

## Release retention 安全合同

### 受保护性质

Retention 所保护的性质是：从规划、quarantine、恢复到删除，操作对象必须始终是同一个 release directory object，以精确 `(st_dev, st_ino)` 标识；并且只有当对应 `owner@sha` 不存在任何 policy reference 时，删除才可提交。Policy reference 包括：

- owner 的 `current` pointer；
- managed ledger 的 owner entry 或 link record；
- strict pending transaction 中的 `releases_before` 和 `releases_after`；
- user pin；
- pending link、retention transaction 或其他 recovery record。

`mtime` 在 release lifecycle 中只用于默认 rollback candidate 排序；retention 不用它定义对象身份、检测替换或提供 prune 授权。目录 child-entry churn 等 benign metadata transition 不等于对象替换。

### 状态区分与 fail-closed

- missing 表示某个已绑定路径当前确实不存在；它只有结合 durable transaction record 和另一侧精确 identity，才能说明对象已被移动或删除。
- unreadable 或 revalidation failure 表示状态未知，必须报错并保留 recovery evidence。
- identity mismatch 表示对象被替换或证据不再绑定，必须停止；不能删除 replacement，也不能把它恢复成原对象。

这些状态不可互相折叠。无法完成 reference scan、对象重验证或 durable cleanup 时，候选 release 保持不删除，稳定 pointer/marker 继续作为恢复入口。

### Quarantine 与恢复

Prune 在全局 install lock 下先恢复旧 transaction，再扫描无引用候选并捕获精确 identity。每个候选按以下顺序处理：

1. 在 retention quarantine 中创建 batch 和 mode `0600` record，并以 hard link 发布 home-level stable pointer。
2. 以 expected identity 将 canonical release 原子移动到 quarantine，再扫描全部 policy reference；若出现新引用，恢复同一个对象并清理未提交 transaction。
3. 发布 durable commit marker 后再次扫描 reference。若此时出现引用，commit 被安全中止并恢复 exact object；只有仍无引用才进入删除协议。
4. 发布 `delete-started` marker，在真正删除边界再次扫描 reference 并复核 canonical/quarantine identity；随后只删除 quarantine 中的同一对象，再发布 `delete-complete`。
5. 发布按结果区分的 clear marker。删除结果使用独立 deleted-result clear marker，使 stable pointer、phase markers、batch record 或 batch cleanup 任一处 crash 后，恢复仍能报告“已删除”而不是误判为“已恢复”。

Pre-commit recovery 只能恢复 exact quarantined object。已 commit 但 quarantine object 仍存在时，recovery 必须先重新扫描 reference：有引用则中止删除并恢复 exact object，无引用且删除 phase 证据完整时才继续。`delete-started` 前的 canonical/quarantine 双缺是 ambiguous；started 后、identity 与 phase evidence 一致的双缺才可作为已执行删除恢复。任何双占、identity mismatch、unreadable/revalidation failure 或 record/pointer 不一致都保留证据并停止。

`prune-releases --dry-run` 对完全 absent 或尚未安装 personal-sync 的 home 是严格只读的：不会为了取得 install lock 而创建 home、`personal-sync` 或 lock state。
