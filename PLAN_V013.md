# v0.13 方案：遗留问题修复 + 账号绑定代理 IP

依据：对 v0.12.1 代码的逐条核对（`OPTIMIZATION_PLAN.md` 各项）、新代码审查（离线跑过相关测试，65 项通过），以及 `references/sub2api` 代理功能的源码调研。行号以当前本地代码为准。

---

## 第一部分：方案落地情况

| 状态 | 条目 |
|---|---|
| 已完成 | 标签页与在途任务分离（`max_inflight`），核验原任务后释放标签页；锁后准备计时；点击阶段不被取消；遥测隔离；入队失败收尾；退避重启加熔断；空闲时刷新入口页、标记登录失效；SQLite WAL；平台表索引；主要接口数据库调用改到线程 |
| 有意调整 | `-6` 只冷却账号（60 秒起翻倍，上限 15 分钟），**不改派、不重放**；2052 不自动重试；人工核对只能核验原 `submit_id`；`activation_requested` 在请求前置位（更保守） |
| 部分完成 | 内容确认宽松匹配（匹配失败时没有截图和告警）；常驻页复用（没有"每 N 单重新加载"）；磁盘清理（只清理停止中的账号）；监控（缺少按账号的成功率、P95、事件循环延迟、内存） |
| 未做 | 出口组 `egress_group`（固定为 `primary`）；浏览器崩溃后进程退出；登录关闭超时；rAF 兜底；恢复循环退避；bridge 读路径和 `_expire`；N+1 查询和热路径缓存；无变化时跳过写库；缩小全局锁；上传流式处理；登录 PBKDF2 移出事务 |

---

## 第二部分：遗留缺陷（按优先级）

### P0：会让整个服务停摆

| # | 问题 | 场景 | 修复 |
|---|---|---|---|
| D1 | 提交节流许可在崩溃后永远不结束（已复核） | `browser_worker.py:351` 的 `recover()` 在 `click_intent` 之后重启时，只上报 `uncertain`，不带 `submission_window_closed`；`platform_store.py:1028` 把"已激活但还没结束"的许可一律当作占用。由于全站只有一个 `egress:primary` 组，**一次崩溃就会让所有账号永久停止提交** | ① `recover()`：新进程启动时，旧页面必然已经关闭，`uncertain` 回执里附带 `submission_window_closed=True`（v6 协议）。② 新增管理员接口 `POST /api/admin/submission-gates/{scope}/release`，写审计事件。③ "失败即封锁"只作用于本账号范围；出口范围在激活后按 `next_allowed_at` 释放（见 D2）。④ 新增测试：激活后 kill 进程，验证其他账号仍能提交 |
| D2 | 节流许可持有时间过长 | 从报价、余额预检（最多 25 秒）、激活、点击，一直持有到回执上报。全站提交被串行化，每单 8–30 秒 | `commit`（激活）时就设置 `next_allowed_at=now+间隔`，**立即释放出口范围**；账号范围持有到收到回执。许可改为在报价**之后**领取 |

### P1：可用性和正确性

| # | 问题 | 修复 |
|---|---|---|
| D3 | Chrome 崩溃后进程还活着，`busy_detached` 账号仍会接到任务，排队 60 秒后超时失败 | 监听 `context.on('close')` 和 `page.on('crash')`；连续 3 次 `page_unavailable` 后以非 0 码退出，交给已有的退避重启；`busy_detached` 额外要求入口页最近一次就绪 |
| D4 | 后台的"解除冷却"只删除 `account_model_health`，`submission_risk_events` 仍然拦截派单 | 新增 `DELETE /api/admin/accounts/{id}/risk-cooldown`，同时清理两张表；界面改用 `risk_cooldown_until` 展示 |
| D5 | 任意一个 `process.json` 损坏，`_active_process_count` 就会抛异常，导致其他账号的启动被熔断 | 计数时捕获异常，把该账号按"运行中"计数 |
| D6 | 仍有同步调用在事件循环上执行：bridge 的 activate 和 preflight 路由、`managed_workers.start/stop`、multipart 解析 | 改用 `to_thread`；上传流式写盘，同时最多处理 4 个 |
| D7 | 每个在途任务每 5 秒至少有 2 次写事务拿全局锁 | `finish_submission_turn` 在锁外先读一次；`update_task` 内容没变就不写 |
| D8 | 平台库和 bridge 库的激活不是原子操作，重试全部失败后会留下 uncertain 和永久的积分预留 | `bridge.activate` 失败且 `activation_started_at` 仍为空时，调用 `deny_activation` 收尾 |
| D9 | 风控冷却期间，该账号已排队的任务直接失败 | 这些任务还没激活，复用 `reassign` 流程改派到其他账号（安全，不会重复生成） |
| D10 | v6 中没有 ID 的 `uncertain` 任务永久占住标签页槽位，`max_concurrent=1` 时整个账号停摆 | 已有页面关闭证明时，释放标签页槽位；在途名额和积分预留继续保留 |
| D11 | `account_login.py:327` 的 `context.close()` 没有超时 | 加 `wait_for(…, 20)`，超时后 kill 浏览器进程 |

### P2：性能和运维

- 热路径缓存：心跳和 claim 每次都要 `get_account` → `list_accounts`，解密全部账号。改为按 id 单独查询，并加 2 秒 TTL 缓存；`bridge.accounts()` 合并成一次 GROUP BY。
- bridge 读路径不再使用 `BEGIN IMMEDIATE`；`_expire` 改由 reconciler 定时执行；补充 `jobs(status, claim_expires_at)` 索引。
- reconciler 分级轮询：processing 任务 5 秒一次；超过 2 小时的 uncertain 任务改为 60 秒一次。
- 磁盘：在启动参数里加 `--disk-cache-size=268435456`；终态任务删除 `assets/<job>`；`outputs/media` 和 `platform-inputs` 按天数保留；maintenance 每轮换一个起点，并在锁外收集待清理文件。
- 登录 PBKDF2 移出事务；用户名不存在时也计算一次假哈希；`login_attempts` 定期清理。
- `_locks` 和 `_polls` 在任务终态时统一清理；`server.py:225` 补上 `isinstance(data, dict)` 检查。
- PostgreSQL 迁移前加 `pg_advisory_lock`，防止两个进程同时执行 `ALTER`。
- 监控补充：按账号统计成功率、`-6`/2052 次数、准备和生成耗时的 P50/P95、事件循环延迟、浏览器内存；磁盘使用超过 80% 时告警。

---

## 第三部分：账号绑定代理 IP

### 3.1 现状

- 账号的代理是一个字符串，加密存放在 `secret_encrypted` 里（`platform_store.py:121` 的 `SECRET_FIELDS`）。后台接口直接透传，**完全不校验**；Web 后台也**没有编辑入口**。
- 已经会使用这个代理的出口：
  - DreaminaClient：API、积分、轮询、上传、视频和图片下载（`client.py:104`）；
  - Worker 浏览器（`browser_worker.py:278`）；
  - 登录浏览器（`account_login.py:179`）。
- 存在的问题：
  1. Playwright 把整条 URL 塞进 `server` 参数，用户名和密码没有拆成独立字段，**带认证的代理会失败**；
  2. 依赖里没有 `httpx[socks]`，**socks5 代理会直接报错**；
  3. httpx 的 `trust_env=True`，账号没有配置代理时会读取系统环境变量里的代理；
  4. 没有防 WebRTC 泄漏的参数；时区和地理位置用的是宿主机的值；
  5. Worker 配置文件里**明文写着代理串**；
  6. `/internal/*` 路由和 `signer/browser_transport.py` 使用全局代理或者直连；
  7. 出口组固定为 `primary`，没有按代理区分。

### 3.2 参考 sub2api 的做法

- **数据模型**：独立的 `proxies` 表，账号通过 `accounts.proxy_id` 关联，多个账号可以共用一个代理。代理有过期时间、备用代理和回退模式。出口 IP、国家、延迟存在缓存里。
- **代理串解析**：统一解析，只允许 http/https/socks5/socks5h 四种协议，`socks5` 自动升级为 `socks5h`（DNS 在代理端解析）。**配置出错时直接报错，不退回直连**，避免本机 IP 和账号产生关联。
- **HTTP 客户端**：按"代理 + 账号"缓存客户端池，代理变更时丢弃旧连接池。
- **管理功能**：增删改查、批量导入（每行一个 `protocol://user:pass@host:port`）、连通测试（探测出口 IP、国家、延迟）、质量检测、正在使用的代理禁止删除。
- **没有**周期性健康检查，只有每分钟一次的过期改投任务。

本方案沿用这些做法，并补充两点：浏览器指纹一致性（时区、语言、WebRTC），以及代理健康检查。

### 3.3 数据模型

新表 `platform_proxies`：

| 字段 | 说明 |
|---|---|
| `id`, `name`, `notes` | 基本信息 |
| `protocol` | `http` / `https` / `socks5` / `socks5h` |
| `host`, `port` | 支持 IPv6 |
| `username`, `password_encrypted` | 用平台主密钥做 Fernet 加密；接口只返回 `has_password` |
| `status` | `active` / `disabled` / `failing` |
| `max_accounts` | 单个代理最多绑定几个账号，默认 3，用于降低账号关联风险 |
| `expires_at`, `expiry_warn_days` | 到期管理 |
| `backup_proxy_id`, `fallback_mode` | `none`（默认）/ `proxy`。**不提供 `direct`** |
| `exit_ip`, `country`, `region`, `city`, `timezone`, `latency_ms` | 最近一次探测结果 |
| `last_checked_at`, `last_error`, `consecutive_failures` | 健康状态 |
| `created_at`, `updated_at`, `deleted_at` | 软删除 |

账号表新增：
- `proxy_id`：可为空；
- `proxy_bound_at`、`proxy_exit_ip_at_bind`：用来检测出口 IP 变化；
- `browser_timezone`、`browser_locale`：绑定时固定下来，之后不随代理探测结果变化。

迁移：
1. 把现有 `secret.proxy` 字符串解析出来，按内容去重，写入 `platform_proxies`，并回填各账号的 `proxy_id`；
2. 清空 secret 里的 proxy；
3. `Settings.proxy` 只作为兼容读取保留一个版本。

### 3.4 统一出口模块 `dreamina_api/egress.py`

```python
@dataclass(frozen=True)
class Egress:
    proxy_id: str | None
    httpx_proxy: str | None        # socks5 自动改写为 socks5h
    playwright_proxy: dict | None  # {'server': 'http://host:port', 'username': ..., 'password': ...}
    group: str                     # 'proxy-<id>' 或 'direct'
    timezone: str | None
    locale: str

def parse_proxy_url(text) -> ProxySpec          # 严格校验：协议白名单、端口范围、IPv6 方括号、不允许 path/query
def egress_for_account(store, account_id) -> Egress
```

规则：
- **默认不回退直连**：`ACCOUNT_PROXY_REQUIRED=true` 时，未绑定代理或代理被禁用的账号**不参与调度**，后台显示"缺少代理"。设为 `false` 时才允许账号直连，`group` 为 `direct`。
- httpx 客户端一律设 `trust_env=False`，代理只用 `Egress.httpx_proxy`。
- **Chromium 不支持带认证的 socks5。** 遇到这种代理时，Worker 在本机 127.0.0.1 上为该代理启动一个转发进程（`pproxy`，监听 http，转发到 socks5），浏览器连本地端口。这个转发进程随 Worker 一起启停。也可以在第一版直接拒绝这种代理，并提示改用 http 代理。

### 3.5 所有出口路径改造

| 出口 | 修改 |
|---|---|
| `client.py` 的 DreaminaClient | 构造时传入 `Egress`；`proxy=egress.httpx_proxy`，`trust_env=False`。积分、轮询、上传、下载都经过这里，一处改完全部生效 |
| `platform_runtime` 中所有 `settings_for_account` 的调用 | 改为 `client_for_account(account_id)`，统一注入 Egress |
| Worker 浏览器 | `managed_workers` 不再把代理串写进配置文件，只写 `proxy_id`；代理凭据通过子进程环境变量 `DREAMINA_EGRESS`（JSON）传入，和 `DREAMINA_PROFILE_KEY` 的传法一致；`Worker.open` 使用 `playwright_proxy`、`timezone_id`、`locale` |
| 登录浏览器 `account_login._open` | 使用同一个 Egress。**登录时的出口 IP 必须和之后运行时一致** |
| 浏览器启动参数 | `browser_launch.py` 的白名单加上 `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` 和 `--webrtc-ip-handling-policy=disable_non_proxied_udp`，绑定了代理时默认启用 |
| 时区和语言 | `timezone_id` 取账号的 `browser_timezone`（绑定时从代理探测结果固定下来）；`locale` 维持 en-US；同时设置 `Accept-Language` |
| 提交节流 | `reserve_submission_turn(..., egress_group=egress.group)`，同一个代理下的账号共享 3 秒间隔，不同代理之间互不影响 |
| `/internal/*` 路由 | 必须带 `account_id` 并走对应账号的 Egress；不带时拒绝，或者标注为仅限本地调试 |
| `signer/browser_transport.py` | 加上 Egress 参数；或者标注为已废弃 |
| Chrome 扩展桥 | 流量走用户自己的 Chrome，不在代理管理范围内，在文档里说明 |

### 3.6 绑定和更换代理的流程

更换出口 IP 对已登录的账号来说是一次"身份变化"，容易触发风控。处理方式：

1. 只能在**账号空闲**时更换：没有在途任务、没有 uncertain 任务、没有正在登录的会话。
2. 后台点击"更换代理"后：
   - 先对新代理做一次探测，结果必须是可用的，并且国家和原代理一致，不一致时需要二次确认；
   - 然后停止 Worker，写入 `proxy_id` 和 `browser_timezone`；
   - 账号状态改为 `unverified`，用新代理做一次 `check_account`；
   - 校验通过后重启 Worker。
3. 新建账号时**先选代理，再登录**。登录面板使用同一个代理。
4. 一个代理绑定的账号数超过 `max_accounts` 时，界面会给出警告，需要确认后才能继续。

### 3.7 健康检查和故障处理

- 新增后台任务 `proxy_health_loop`：对正在被使用的代理，每 10 分钟探测一次。
  - 探测内容：
    1. 出口 IP 和地理信息：访问 `https://ip-api.com/json`，失败时改用 `https://api64.ipify.org`，探测地址可配置；
    2. 到 `dreamina-api.us.capcut.com:443` 的 TLS 建连耗时。
  - 连续失败 3 次：代理状态改为 `failing`，绑定它的账号**暂停调度**，并发出告警。**不会自动切换成直连**。
  - 恢复：连续成功 2 次后自动改回 `active`。
  - 出口 IP 变化（比如动态住宅代理）：发出告警，并记录事件，不自动处理。
- 请求时出错：连接代理失败、代理认证返回 407、socks 握手失败，统一归类为 `proxy_error`。
  - 激活前发生：任务改派给其他账号（安全）。这个账号**不计入风控冷却**，而是累计到该代理的失败次数。
  - 激活后发生：按现有规则标记为 uncertain，只核对原任务。
- 过期：每分钟检查一次。代理过期且 `fallback_mode=proxy` 时，沿着备用代理链（检测成环）找到可用的代理，**只对空闲账号**执行 3.6 的更换流程；不空闲的账号暂停调度并告警。

### 3.8 管理接口与界面

接口（全部需要管理员权限和 CSRF 校验，并写审计事件）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/proxies` | 列表，包含状态、出口 IP、国家、延迟、已绑定账号数 |
| POST | `/api/admin/proxies` | 新建，创建后异步探测一次 |
| POST | `/api/admin/proxies/batch` | 批量导入，每行一个 `protocol://user:pass@host:port`，自动去重 |
| PUT / DELETE | `/api/admin/proxies/{id}` | 修改或删除；有账号绑定时禁止删除 |
| POST | `/api/admin/proxies/{id}/test` | 立即探测 |
| GET | `/api/admin/proxies/{id}/accounts` | 查看绑定了哪些账号 |
| POST | `/api/admin/accounts/{id}/proxy` | 按 3.6 流程绑定或更换代理 |

界面：
- 新增"代理 IP"页面：列表、批量导入、批量测试、启用/禁用、到期提醒；
- 账号的新建和编辑窗口加"代理"下拉框，显示国家、延迟、已绑定账号数，可以在下拉框里直接测试；
- 账号列表新增"出口"列；
- 监控页新增代理健康状态和每个出口组的激活频率。

### 3.9 安全

- 代理密码用 Fernet 加密，接口永远不返回明文。
- 导出功能需要重新输入管理员密码。
- 代理凭据只通过子进程环境变量传给 Worker，不落盘。
- 日志和事件里只记录 `proxy_id` 和 `host:port`，不记录凭据。

### 3.10 依赖

- `httpx[socks]`（socksio）；
- 可选 `pproxy`：仅在使用带认证的 socks5 代理时需要。

---

## 第四部分：测试和发布

**单元测试和集成测试（离线）**
- `parse_proxy_url` 覆盖各种合法和非法输入；socks5 自动升级为 socks5h；IPv6。
- `egress_for_account`：代理缺失或禁用时拒绝调度（`required` 模式）；`trust_env=False`。
- Playwright 代理参数拆分：用一个本地假代理（pytest 起一个 asyncio 代理服务）验证带认证的 http 代理在浏览器和 httpx 里都能用。
- 出口组节流：同一代理的两个账号间隔不小于 3 秒；不同代理的账号可以同时激活。
- D1 回归：激活后 kill 进程，重启后许可能够结束，其他出口组不受影响。
- 健康检查状态机：连续失败变为 `failing`、账号暂停、恢复；过期改投只作用于空闲账号。
- 更换代理的流程：账号不空闲时拒绝；写入后状态变为 `unverified`，并重启 Worker。

**真实验证（需要你提供 1–2 个代理）**
1. 只读：通过代理探测出口 IP；用账号查询积分；查看登录页。不花积分。
2. 1 个账号绑定代理后提交 1 单 Mini（约 20 积分），核对 Dreamina 侧看到的请求来源（服务端日志里没有本机 IP 直连的记录）。
3. 两个代理各绑 1 个账号，同时提交，验证两个出口组并行（约 40 积分）。

**发布**
1. 第 1 批：D1、D2，以及 P1 中的 D3–D11。这一批不涉及数据库 schema 变更，可以先上线。
2. 第 2 批：代理功能。包括 schema 迁移、secret 里代理数据的迁移、界面和健康检查。
3. 每一批：本地跑全量测试 → 打包 → `pg_dump` 备份 → 空闲时段滚动重启 → 冒烟测试。回滚时把 `current` 软链切回上一个 release；schema 只做新增，旧版本可以兼容。

**工作量估计**：第 1 批 2–3 天；第 2 批 4–5 天（后端 2.5 天，前端 1.5 天，测试 1 天）。
