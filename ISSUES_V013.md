# v0.13.0 深度检查结果（2026-10-27）

## 检查方式

- **本地全量测试**：1706 项通过，另有 43 个子测试通过。
- **代码审查**：两路独立审查，覆盖调度与恢复、代理与出口两部分。
- **本机实验**：用回环代理验证了几项实际行为：陷阱代理测试 `trust_env`、407 认证、WebRTC 的 ICE 候选、时区、socks5h 远端 DNS。
- **复现测试**：写了 2 个，在 `dreamina-scratch/sched-review/test_review_gaps.py`，都能复现问题。
- **生产环境**：只做了只读检查，没有发起生成，没有花积分。

## 已确认修好的部分

- **D1**：出口许可在激活时就释放。我没能构造出全站被卡住的场景。
- **D2–D7、D9、D11**：都已按方案落地。
- **P2 各项**：advisory lock 保护的迁移、热路径缓存、PBKDF2 移出事务、磁盘缓存上限，都已落地。
- **代理出口**：所有 httpx 客户端都设置了 `trust_env=False`，各条出口都使用账号绑定的代理。代理串解析的 38 个用例全部正确；带认证的 http 代理在浏览器和 httpx 中都能用；WebRTC 不再泄漏本机地址；时区生效；凭据不落盘、不在日志中回显。

## 问题清单

### 高：会产生错误扣费，或让账号停摆且没有管理员出口

| # | 位置 | 问题 | 后果 | 修复 |
|---|---|---|---|---|
| H1 | `browser_driver.py` 中 `before_click` 之前（`new_session` 只在开始时清理一次卡片，见 338-385 行） | 点击前没有再核对参考卡片数量 | 网站在 New session 之后才异步渲染旧卡片时，会把多出的素材一起真实提交并扣费；请求对不上就抓不到 `submit_id`，任务又变成没有 ID 的永久 uncertain（见 H2） | 点击前断言 `Remove · Reference` 按钮数和媒体数都等于 `len(references)`，不一致就在激活前失败；补"新会话保留素材"的回归测试 |
| H2 | `platform_runtime.activate_task`、`chrome_bridge.report`（377-448 行） | 中心库已经写入 commit，但 bridge 的激活没有落库，`activation_started_at` 为空，worker 却上报了 uncertain，bridge 照单接受 | 已复现：任务永久 uncertain，积分预留不释放，`fail_unactivated` 和 `manual_reconcile` 都处理不了；`max_inflight=1` 时账号永久停摆。实际上这类任务可以证明从未点击 | `bridge.report`：协议 v5 及以上、uncertain、`activation_started_at IS NULL`、没有 `submit_id` 时，改写为 `failed` 并标记 `generation_not_activated` |
| H3 | `browser_worker._quiesce_unknown_page`、`release_gate`、`managed_workers._assert_idle` | 同一进程内关页失败时，回执里没有关页证明；之后也拿不到 | 已复现：账号许可释放不了；因为有未完成任务，后台也无法停止或重启 worker，只能上主机 kill 进程 | 关页失败时让 worker 以非 0 码退出，交给退避重启，重启后的 `recover()` 会补上证明；或者提供"保留日志的强制重启" |

### 中

| # | 位置 | 问题 | 后果 | 修复 |
|---|---|---|---|---|
| M1 | `managed_workers.py:575-580` | 自动重启遇到 `proxy_*` 错误时打开熔断，而且从不复位 | 代理短暂故障期间 worker 恰好重启，代理恢复后账号会一直离线，需要人工启动 | `proxy_*` 错误只做退避，不打开熔断；代理恢复为 active 时复位相关账号的熔断 |
| M2 | `client.py:292-293` | 走代理的账号下载失败时，CDN 返回的 403/404/5xx 也被归为 `proxy_error` | 签名过期时，代理账号不会像直连账号那样重新取地址重试 3 次，而是直接以"代理错误"失败 | 只有 `ProxyError` 或连接类错误才归为 `proxy_error`；`HTTPStatusError` 归为 `download_failed` |
| M3 | `proxy_health.py:24, 87` | 查出口 IP 只用 ipify 一个地址，非 200 就算代理失败 | ipify 故障或限流时，所有代理被标为 failing，全站账号暂停 | 配多个查 IP 地址依次尝试；查 IP 失败但到 Dreamina 的 TLS 建连成功时，不计入代理失败 |
| M4 | `proxy_store.py:260, 299` | 出口 IP 一变就暂停账号（方案要求只告警） | 动态或轮换代理基本不可用；叠加 M1 后，worker 也起不来 | 按代理配置：只告警，或暂停 |
| M5 | `proxy_service.py:64`、`proxy_store.py:406` | 到期自动改投时，`check_account` 偶发失败就把账号停用并置为 error | 一次网络抖动就可能让正常账号下线 | 失败时保持 checking 并稍后重试，或者回滚到原代理 |
| M6 | 生产环境观测 | 每个空闲入口页常驻约 16% CPU（启动参数里有 `--disable-background-timer-throttling`） | 5 个账号约占 0.8 核，100 个账号约占 16 核 | 空闲时把入口页导航到 `about:blank`，或者去掉这个参数，需要时再刷新；也可以只在空闲探测时加载入口页 |
| M7 | 生产环境观测 | 每个 profile 的 `Cache` 约 1.2GB，已设置 `--disk-cache-size=256MB` 但没有缩下来 | 磁盘持续增长（media 卷 6.7GB） | 观察 24 小时后看是否回落；不回落就在 worker 空闲重启前清理 `Cache` 和 `Code Cache`（只清理缓存，保留登录状态） |
| M8 | `browser_worker.files()`、`maintenance.py:146-170` | worker 常驻运行时从不删除 `assets/<job>` | 线上 worker 常驻，素材副本无限增长 | worker 收到终态或 detach 时删除自己的 `assets/<job>` |

### 低

| # | 问题 | 修复 |
|---|---|---|
| L1 | Chromium 的 `ERR_PROXY_CONNECTION_FAILED`、`ERR_TUNNEL_CONNECTION_FAILED` 没有映射成 `proxy_error` | 在驱动里识别后按 `proxy_error` 上报，计入代理健康，并在激活前改派 |
| L2 | SOCKS 认证失败被显示成"连接失败" | 同时匹配 `username/password` |
| L3 | `Egress.revision` 随每次探测变化，每 10 分钟重建一次连接池 | 改用连接参数的指纹 |
| L4 | 解绑到直连后，浏览器时区仍是旧代理所在地 | 解绑时清空时区 |
| L5 | `DREAMINA_EGRESS` 和 `DREAMINA_PROFILE_KEY` 读取后没有从环境变量中删除，会被 Chromium 子进程继承 | 读取后立即 `os.environ.pop` |
| L6 | `server.py:231, 257` 的旧 chrome 接口在 async 路由里同步查数据库 | 改用 `db_call` |
| L7 | `platform_store._workers()` 遇到 bridge 短暂 busy 时，把容量当作 0，任务直接失败 | 抛出可重试错误 |
| L8 | `maintenance._central_pass` 在全局锁内做全表 LIKE 扫描（默认关闭） | 锁外筛选候选，锁内只按主键复核 |
| L9 | 界面与后端不一致：批量导入上限 100 对 200；界面显示"部分成功"，后端实际全部回滚；到期提醒默认 7 天对 3 天 | 统一文案和默认值 |

## 尚未验证

- 真实代理下的登录和生成（目前没有配置外部代理）。
- 带认证的 SOCKS 代理在浏览器中的使用（尚未实现）。
- 页面复用在真实网页上的表现（上次实测时回退到新页面，H1 修复后需要复测）。

## 修复顺序建议

1. H1、H2、H3：都是小改动，各自配一个回归测试，约 1 天。
2. M1–M3、M5、M8，以及 M6 的入口页优化：约 1 天。
3. M4 的可配置化和全部低优先级项：约 0.5–1 天。
4. 复测：用 1 个真实代理绑 1 个账号，验证登录和一单生成，同时观察页面复用（约 20 积分）。

## 修复交付记录 · v0.13.2 · 2026-10-28

H1–H3、M1–M8、L1–L9已实现并部署，验证范围和保留边界见STATUS.md与RELIABILITY.md。M6最终使用显式休眠/真实唤醒许可；未采用实验中无效或丢失回调的冻结方式。M7仅在持有profile锁的空闲启动阶段清理白名单缓存，保留登录资料。本轮未执行付费生成、未验证真实外部代理下的生成，也不以本次回归承诺99%成功率。
两次60秒空闲采样：Chromium CPU合计 1.75→0.30 核，整个API容器 1.81→0.44 核；白名单缓存 7.51→2.04 GB。短采样不能代替长期负载或100账号压测。
