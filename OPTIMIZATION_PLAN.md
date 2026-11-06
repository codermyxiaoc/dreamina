# 优化与修改方案

依据：2026-10-27 在生产服务器 `38.46.222.84`（v0.11.0）上的只读数据核查、1 单真实生成（20 积分），以及对 `dreamina_api/` 的代码审查。行号以当前本地代码为准。

## 0. 实测依据

| 观测 | 数据 | 结论 |
|---|---|---|
| 单任务时间线（新服务器，CPU 空闲） | 领取→上游返回 `submit_id` **4.4 秒**；上游生成约 **137 秒** | 浏览器槽位约 97% 的时间在空等 |
| 旧服务器峰值测试（8 核负载 17） | 领取→提交约 60–70 秒 | CPU 饱和时，准备阶段会被放大十倍以上 |
| 全部 12 条生产任务 | 8 成功；2 条 `-6`；2 条 `2052 OutputAudioRisk` | 失败原因是风控和内容审核，不是代码崩溃 |
| 两条 `-6` | 都出在 5 个账号、0.33 秒内同 IP 突发提交时；其中 `mvp1` 后来已正常生成；5 个账号的消息中心都没有处罚通知 | `-6` 是突发提交触发的临时风控，不是封号 |
| 2052 | 峰值测试按账单核对，未扣费 | 可以安全重试 |
| 资源 | 4 个空闲账号浏览器共 2.3GB；profile 目录 3.1GB，无清理 | 每个账号约 0.6GB |
| `mvp5` 节点 | 峰值测试后一直是 `Worker stopped`，从未恢复 | 缺少自动重启 |
| 纯 HTTP | 积分、上传、预审、查询、下载、消息中心都可以；所有生成接口都在 SDK 签名列表中 | 生成接口必须经过浏览器 |

## 1. 目标架构

```
API ──> 调度（按账号 / 出口 IP 限速） ──> 账号常驻浏览器（每账号 1 个）
                                             │ 只负责：准备 + 提交（约 5 秒）
                                             ▼ 拿到 submit_id 立即释放标签页
                      中心对账（纯 HTTP get_history_by_ids）──> 完成 / 失败 / 改派
```

- 浏览器数量 = 账号数，不随请求量增长。
- 单账号吞吐的上限由两个值决定：**提交速率**（每单约 5 秒）和**上游单账号并发上限**（待测，见 §6）。
- 生成以外的所有环节都走纯 HTTP。

## 2. P0：提升成功率（预计 1–2 天）

### 2.1 `-6` 冷却账号并改派（最高优先级）

现状：`upstream_errors.py:76` 把 `-6` 归为不可重试；`platform_runtime.py:379-391` 直接把任务标记为失败。`schedule_submission_retry`（`platform_store.py:930`）要求在同一账号上重试；账号本身不冷却，会被继续派单。

修改：
1. `upstream_errors.py`：`ret=-6` 或 `ErrSharkNotPass` 单独分类为 `risk_rejected`，标记为可改派（不是同账号重试）。
2. `platform_store.py`：新增 `block_account(account_id, reason='risk_rejected', cooldown_seconds)`，复用 `block_account_model` 的表，model 用 `*` 表示整个账号。冷却时长指数递增：15 分钟 → 30 分钟 → 60 分钟 → 上限 6 小时。成功生成一次后清零。
3. `platform_store.py`：仿照 `reassign_insufficient_credits`（801 行）新增 `reassign_risk_rejected(task_id, job)`。前置条件：
   - `job.status=='failed'`、`error_code=='upstream_rejected'`、`upstream_ret=='-6'`；
   - 该 `submit_id` 用 `get_history_by_ids` 查不到（在 runtime 里先查，作为证明写入事件）；
   - 把该账号加入 `excluded`，每个任务最多改派 2 个账号；
   - 释放 credit hold，生成新的 `bridge_job_id`，平台任务 ID 和计费次数保持不变。
4. `platform_runtime.py` 的 `sync_task` 中 `upstream_rejected` 分支：在调用 `schedule_submission_retry` 之前，先判断是否为 `risk_rejected`，是则依次执行冷却和改派。
5. 后台在账号上显示"风控冷却至 xx"，并提供"解除冷却"按钮（复用 `DELETE /api/admin/accounts/{id}/model-blocks/*`）。

验收：用单元测试模拟 `-6` 响应，验证三点：任务被改派到其他账号并完成；原账号被冷却；"查得到 `submit_id`"时不改派，标记为 uncertain。

### 2.2 按出口 IP 和账号限制提交速率（防止突发风控）

现状：调度只看负载，同一 IP 下的多个账号可以在同一秒内同时点击提交。

修改：
1. 账号新增字段 `egress_group`。默认取 `settings.proxy`，没有配置代理时为 `"default"`。
2. 在 worker 的 `/jobs/{id}/activate`（服务端激活授权处）加一个**令牌桶**：同一 `egress_group` 内，两次激活至少间隔 `SUBMIT_MIN_INTERVAL_SECONDS`（默认 3 秒），可以加 0–2 秒随机抖动。没拿到令牌时返回 `can_activate=false, retry_after`，worker 等待后重新请求激活。这时任务还没有点击提交，等待是安全的。
3. 同一账号两次提交之间至少间隔 `ACCOUNT_SUBMIT_MIN_INTERVAL_SECONDS`（默认 5 秒）。
4. 建议（运维层面）：账号多时按 5–10 个一组，每组配一个代理 IP。账号表里已有 `proxy` 字段，worker 会传给浏览器。

验收：并发 10 个请求时，同一 egress_group 的激活时间戳间隔都不小于设定值。

### 2.3 2052（音频审核）未扣费时自动重试一次

现状：2052 在上游是生成后被审核拒绝，任务直接失败。

修改：
1. `platform_runtime.py`：上游终态为 `fail_code=2052`（或者 `ErrMessage_APP_OutputAudioRisk`）时，按 `submit_id` 查询 `user_credit_history`。**确认没有这条扣费记录**后，新建一次提交：沿用同一平台任务 ID，换一个 seed。每个任务最多重试 1 次。
2. 确认不了是否扣费时，不重试，直接按失败返回。
3. 提示词增强（可选，默认关闭）：配置 `AUDIO_SAFE_SUFFIX`，例如 `", no music, no speech"`，开启后追加到提示词末尾，并在任务详情里展示。

验收：模拟 2052 且查不到扣费 → 重试并完成；模拟 2052 且有扣费 → 不重试。

### 2.4 准备阶段时限只从拿到锁之后开始计算（审查 H1）

现状：`browser_worker.py:421` 的 `deadline` 在等 `_preparation_lock` 之前就开始计时；`wait_for(prepare_and_submit())` 还把点击后等待响应的时间也包含进去了。

修改：
1. `handle()`：等锁的时间不计入准备时限；排队时间由服务端已有的 `queue` 期限约束。
2. 把 `prepare_and_submit` 拆成两段：`prepare()` 受准备时限约束；`click_and_observe()` 只受 `submission_timeout_seconds` 约束，**不能被外层的 `wait_for` 取消**。
3. 点击后的 `stage()` 和遥测上报都改为"捕获异常 + 5 秒超时"，不能覆盖已经拿到的上游结果（审查 M1）。
4. `activation_requested` 只在 `/activate` 返回成功后才置位（审查 M2），避免网络失败被误判为 uncertain。

### 2.5 内容确认弹窗改为宽松匹配（审查 H3）

`browser_driver.py:29` 和 632 行：从"整句英文完全相等"改为"关键短语 + 按钮结构"匹配，比如标题里包含 `continue`，并且有唯一的确认按钮。匹配失败时保存截图，并在后台发出告警。

## 3. P1：解决高并发需要大量浏览器的问题（预计 2–3 天）

### 3.1 拿到 `submit_id` 立即释放标签页和槽位

现状：
- `chrome_bridge.py:212` 只在任务完成、失败或超时后才 `slot_released`；
- `chrome_bridge.py:325-327` 把 `submitted` 状态计入 worker 和账号的占用；
- `browser_worker.py:494` 的 `wait_original` 一直保持标签页，直到生成结束。

修改：
1. 把账号的两个容量概念分开：
   - `max_concurrent`（已有）：**同时打开的标签页数**，默认 1；
   - 新增 `max_inflight`：**上游同时生成的任务数**，默认 3，按 §6 的实测结果调整。
2. `chrome_bridge.py` 的 `_public`：`status=='submitted' and submit_id and confirmed_at` 时 `slot_released=True`。
3. `chrome_bridge.py` 的 `claim`：
   - worker 级占用只计 `claimed`，以及没有 `submit_id` 的 `uncertain`；
   - 账号级占用：`claimed` 数量 < `max_concurrent`，**并且** `claimed + submitted + 有 submit_id 的 uncertain` < `max_inflight`。
4. `browser_worker.py`：拿到 `submitted` 和 `submit_id` 并上报成功后，直接走现有的 `_archive_detached` 分支（398 行），关闭页面、写 journal，然后去领下一单。页面进度不再从浏览器读取，全部由中心服务通过 `get_history_by_ids` 提供（`sync_task` 已实现）。
5. `platform_store.reserve_task` 的账号负载计算保持不变，仍按在途任务数，让任务在账号之间均衡分配。

效果估算：单账号每单占用浏览器约 5–10 秒。在 `max_inflight=3` 的情况下，单账号吞吐从约 0.4 单/分钟提升到 `3/2.3分钟 ≈ 1.3` 单/分钟。真正的上限取决于上游单账号并发。

验收：
- 单元测试：`submitted` 状态的任务不占用标签页槽位，但占用 inflight 名额；
- 集成测试（假 worker + 假上游）：提交 3 单，同一标签页按顺序完成三次提交，三单的生成过程并行。

### 3.2 常驻页面复用（减少页面加载开销）

现状：每单都要 `new_page()` 加 `goto` 打开新标签页（`browser_worker.py:459-462`）。CPU 饱和时这一步要几十秒。

修改：每个槽位保留一个常驻任务页，每单点击 `New session` 重置编辑器（`driver.new_session` 已实现），不再新建标签页。每 N 单（默认 20）或页面异常时重新加载。注意：入口页（`self.entry`）同时用于检测登录状态，需要配合 §4.2 定期刷新。

### 3.3 容量规划

- 每个账号浏览器约 0.6GB 内存，准备阶段会有短暂的 CPU 峰值。
- 建议 `NATIVE_MAX_ACCOUNT_SLOTS` 保持 1–2，扩容靠增加账号，而不是给单账号开更多标签页。
- 每台机器最多运行 `CPU核数 × 2` 个账号浏览器，并预留 30% 的 CPU。准备阶段的 CPU 饱和是峰值测试失败和超时的放大器。

### 3.4 纯 API 方案的结论

- 生成接口（`aigc_draft/generate` 等）必须经过浏览器里的官方 SDK 签名。完全不用浏览器的方案需要伪造浏览器指纹来骗过风控，**不纳入本方案**。
- "模板回放"（在已登录的页面里用 XHR 直接提交，不点 UI）还没有验证。§3.1 加 §3.2 已经能把单账号的浏览器开销降到每单约 5 秒，暂不需要。

## 4. P1：稳定性与资源（预计 2 天）

### 4.1 Worker 崩溃后自动重启（审查 H2，生产已发生）

- `browser_worker.py`：监听 `context.on('close')` 和 `page.on('crash')`；连续 3 次 `page_unavailable` 后进程退出，退出码非 0。
- `managed_workers.py` 的 `scan()`：对于"应该运行但进程不在、并且 journal 处于 idle 状态"的账号，按退避重新拉起，间隔为 10 秒、30 秒、2 分钟、10 分钟，到上限后告警。journal 不是 idle（有未对账的任务）时不自动拉起，保持现在的安全语义。
- 修复 `managed_workers.py:245-250`：某一个账号的 registry 损坏时，只影响这个账号，按"运行中"计数，不能阻止其他账号启动。

### 4.2 登录失效检测

- 入口页每 15 分钟 reload 一次，然后执行 `probe(advertise=True)`。
- 任务因 `Login required` 失败时，立即把 readiness 置为 `login_required`，并设置 `error_code='login_required'`；调度层把该账号移出候选列表。

### 4.3 超时与锁

- `account_login.py:327`：`context.close()` 和 `account_check` 都加 `wait_for(…, 20)`；超时后强制 kill 浏览器进程，然后释放 guard（审查 M5）。
- `browser_driver.py:203` 的 rAF `evaluate`：在 JS 里加 `Promise.race` 和 2 秒兜底（审查 L3）。
- 恢复循环（`browser_worker.py:581-609`）：改为指数退避，最长 5 分钟，只在状态变化时才重新上报；进入 attention 状态时关闭任务页（审查 M3）。

### 4.4 磁盘清理

- 任务进入终态后删除 `worker-state/<acct>/assets/<job_id>`。
- `diagnostics/` 只保留 7 天或 500MB，以先达到的为准；`history/` 保留 30 天。
- profile 目录：定期清理 `Cache`、`Code Cache`、`GPUCache`。必须在 worker 停止时进行，或者通过启动参数 `--disk-cache-size` 限制缓存大小。

## 5. P1：服务端阻塞与数据库（预计 2–3 天）

| # | 问题 | 修改 | 位置 |
|---|---|---|---|
| 1 | async 处理器里直接做同步的数据库和文件 IO，会阻塞事件循环 | store 和 bridge 的调用统一包成 `await asyncio.to_thread(...)`；reconciler 里真正用起 `Semaphore(8)` 并发 | `openai_api.py`、`platform_runtime.py` |
| 2 | 平台 SQLite 没开 WAL | 连接钩子里加 `PRAGMA journal_mode=WAL`、`synchronous=NORMAL`；生产环境本来就是 PG，这条只影响单机部署 | `platform_store.py:191` |
| 3 | 缺索引 | `platform_tasks(bridge_job_id)`、`(status, account_id)`、`(key_id, status)`、`(key_id, created_at)`；`task_events(task_id, created_at)`；bridge `jobs(status, claim_expires_at)`；启动时用 `CREATE INDEX IF NOT EXISTS` 迁移 | 表定义 61-75 行；`chrome_bridge.py` |
| 4 | bridge 的读操作也拿 `BEGIN IMMEDIATE`，每次调用都执行 `_expire` | 读路径改为普通事务；`_expire` 改为 reconciler 每 5 秒执行一次；终态 job 30 天后归档 | `chrome_bridge.py:199` |
| 5 | 心跳和领取都要全量解密账号，存在 N+1 查询 | `get_account` 改成按 id 单独查询；`bridge.accounts()` 改成一次 GROUP BY；worker 状态加 2 秒 TTL 缓存 | `server.py:188`、`platform_store.py:288-316` |
| 6 | 入队失败的任务永远卡在 queued/uncertain，持续占用 Key 的并发 | `enqueue` 失败且没有 `bridge_job_id` 时直接置为 failed（这种情况可以证明未提交）；reaper 每分钟把超过 5 分钟、仍没有 `bridge_job_id` 的任务置为 failed | `openai_api.py:464-478`、`575-588` |
| 7 | uncertain 任务没有人工处理入口，credit hold 永久泄漏 | 新增管理员接口，可以把任务置为 completed、failed 或 cancelled，并写入 `manual_reconciliation` 事件，同时释放 hold；后台加对应按钮 | `admin_api.py` |
| 8 | reconciler 每 5 秒无条件写库 | 状态没变化时跳过写入；超过 2 小时的 uncertain 任务改为 5 分钟查一次 | `platform_runtime.py:398-409` |
| 9 | 全局写锁覆盖所有写操作 | 只在 `reserve_task`、改派和重试时锁 guard 行；事件和遥测写入不加 guard 锁 | `platform_store.py:235-247` |
| 10 | 大文件上传在事件循环里解析 | multipart 流式写入临时文件，文件 IO 放到 `to_thread`；同时最多处理 4 个上传 | `openai_api.py:85-150` |
| 11 | 登录 PBKDF2 计算在事务内进行；用户名不存在时响应更快，可被枚举 | 哈希放到事务外的线程里计算；用户名不存在时也计算一次假哈希；`login_attempts` 定期清理 | `admin_api.py:94`、`platform_store.py` |
| 12 | 小问题 | `_locks` 在任务终态时清理；`server.py:211` 先检查 `isinstance(data, dict)` | — |

## 6. 待补的实测（需要后台操作，约 80 积分）

| 测试 | 操作 | 积分 | 决定什么 |
|---|---|---|---|
| C. 单账号上游并发 | 后台把 `mvoxlv9faslc` 的并发设为 3，测试 Key 只勾选这个账号；同时提交 3 单 | 60 | `max_inflight` 的默认值 |
| E. §3.1 改完后的回归 | 同一账号、标签页数 1、`max_inflight` 3，连续提交 3 单 | 60（可以和 C 合并） | 提前释放槽位后，上游是否正常完成 |
| F. §2.2 限速回归 | 4 个账号同时各提交 1 单，激活间隔 3 秒 | 0–80（免费报价的账号不扣费） | 错开提交后是否还出现 `-6` |

## 7. 实施顺序与发布

1. **第 1 批（P0）**：§2.1 → §2.2 → §2.4 → §2.3 → §2.5。全部是本地代码修改加单元测试，不改动数据库 schema。
2. **第 2 批（P1 并发）**：§3.1 → §3.2，包括 bridge schema 迁移（新增 `max_inflight` 列）。上线后做测试 E。
3. **第 3 批（P1 稳定性 + 服务端）**：§4 与 §5 并行，包括索引迁移。
4. 每批的发布流程：
   - 本地跑全量 pytest，加上新增用例；
   - 按 `outputs/v011_remote_*.py` 的流程打包；
   - 部署前执行 `pg_dump` 备份；
   - 在空闲时段滚动重启，确认所有节点都恢复就绪；
   - 跑 1 单冒烟测试（20 积分）。
5. 回滚：保留上一个 release 目录，把 `current` 软链切回去，再恢复数据库备份。schema 只做新增列和新增索引，旧版本可以兼容。

## 8. 监控指标（后台新增）

- 按账号统计：成功率、`-6` 次数、2052 次数、当前冷却状态、准备耗时 P50/P95、生成耗时 P50/P95。
- 按出口组统计：每分钟激活次数。
- 系统：事件循环延迟、数据库锁等待次数、每个账号浏览器的内存占用、磁盘占用。
- 告警：Worker 停止超过 5 分钟、账号进入风控冷却、uncertain 任务超过 30 分钟、磁盘使用超过 80%。
