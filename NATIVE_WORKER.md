# 无扩展浏览器 Worker

节点的恢复和重试规则见 [RELIABILITY.md](RELIABILITY.md)：只重试未激活生成的准备阶段，已观察到请求ID则持久化后继续查询原任务，不能重复生成。

2026-10-26 本机已完成真实生成验证：Seedream图片、Mini多图/视频参考、Fast、2.0和2.5文生视频均已有成品，其中Fast首次回执经人工核对恢复。完整范围及回执恢复说明见 [LIVE_MODEL_TEST.md](LIVE_MODEL_TEST.md)。

`POST /v1/videos` 现在支持精准 `@图片1` / `@图片2` / `@视频1`。编号按同类型上传顺序从1开始，保留提示词内引用位置并允许重复；无效编号在任务分配前拒绝。节点逐文件确认上传资源ID，通过网页原生菜单插入标签，提交前读回完整文本与标签序列；请求与后续历史记录按上传URI/Vid核对，绑定与任务回执共同持久化。原生网页已验证2图＋1视频的三项标签及匹配请求，生成请求在发送前拦截，本功能尚未追加收费生成。示例见 [PLATFORM.md](PLATFORM.md)。

调度按可用账号的图片＋视频在途任务总数选最少者，同负载用数据库序号轮询；顺序跨重启保留且和任务预约在同一事务中更新。初始优先级不再压过负载与公平顺序。账户容量、模型权限、登录与在线状态仍限制候选账号，不能保证条件不同的账号在任意时刻绝对等量。

**推荐登录入口已更新：** 在后台账号管理中「创建并登录」，直接操作内嵌面板；系统自动保存账号状态、同步凭据及启动托管节点。多账号与 Linux 集成方式见 [ACCOUNT_LOGIN.md](ACCOUNT_LOGIN.md)。下文 CLI、独立容器及 noVNC 方式为可选运维入口。

后台和 OpenAI 风格接口保留；执行由 Python + Playwright 直接驱动持久化 Chrome，不加载 Chrome 扩展、不复制浏览器主配置、不重构生成请求。每任务原生新建会话，文件通过文件上传控件送入；生成响应由 Playwright 事件监听确认，查询和下载沿用原 API。

## 本机使用

配置参考 `deploy/browser-worker.example.json`。本机已有私有配置 `outputs/native-worker.json`；不要把它提交到 Git。

```powershell
python -m dreamina_api.browser_worker login --config outputs/native-worker.json --account default
python -m dreamina_api.browser_worker probe --config outputs/native-worker.json --account default
python -m dreamina_api.browser_worker run --config outputs/native-worker.json --account default
```

`login` 打开专用浏览器，用户手动完成正常登录，Worker 不输入密码/验证码；成功后保存 profile。`probe` 只检查登录和页面控件，不领取任务、不生成。`run` 开始领取队列，`--max-jobs 1` 可限制一次联调。

独立 Worker 的每个 `accounts[]` 条目支持 `args` 数组；后台托管节点则使用 `.env` 的 `LOGIN_BROWSER_ARGS`。本机与部署示例已按用户要求加入 8 个参数，其中包括关闭安全隔离的选项；范围与撤销方式见 [ACCOUNT_LOGIN.md](ACCOUNT_LOGIN.md#自定义浏览器启动参数)。

将服务端 `.env` 设置为 `WORKER_MODE=native` 后，旧扩展可以保留安装但不再获得就绪资格，避免重复执行。当前 worker_key 复用服务器的 CHROME_BRIDGE_KEY，仅是兼容队列的内部认证名称，不代表还需要插件。

浏览器配置和任务记录分别保存在配置文件旁的 `profiles/` 或指定 profile_dir、`worker-state/<账号>/`。任务点击前先写入 durable click_intent；服务/浏览器中断后没有确认的任务不会重发。私有诊断截图与页面结构位于 worker-state 下，可用于定位控件变化。

## 多账号

每账号配置独立 profile_dir，并启动一个 `run --account <ID>` 进程；后台账号ID、浏览器登录账号和查询凭据必须对应。一个 profile 同时只能由一个进程打开，Worker 用锁防止并发占用。每个进程现在运行独立任务槽位，配置 `concurrency` 默认1、范围1–8；托管节点按账号“单账号并发标签页数”与服务器上限生成配置。准备/单次提交短暂串行，提交后的原任务等待并行。每槽位独立journal、worker_id和nonce，不复制日常Chrome目录，也不绕过上游限制。

升级会把旧 `current.json` 的未完成记录迁入槽位1，保留原领取身份。不要在有未完成槽位日志时回退旧执行器；旧版看到迁移标记会保守停止。降并发后，已退休槽位只恢复原任务、不领取新任务。

初始 Cookie 迁入是显式选项 `--import-local-session`，只读取用户已提供的本地 `.env`，不读取或解密日常 Chrome 数据库。网页登录可能需要重新完成，并以正常登录后的网站持久化状态为准。

## Linux 服务器

建议先使用有界面的 Chromium 加 Xvfb，不假设 headless 一定能通过上游校验。提供的 Docker Worker 使用虚拟显示，并通过只绑定宿主 loopback 的 noVNC 入口供首次登录。中央 API 仍由原 compose.yaml 运行。

1. 将 `deploy/browser-worker.example.json` 复制为 `deploy/browser-worker.json`，server_url 填中央HTTPS地址，worker_key填内部Worker密钥。Linux配置用 `channel="chromium"`、`profile_dir="/data/profiles/default"`、`headless=false`。
2. 设置独立 VNC_PASSWORD；noVNC 端口不要直接公开。需要远程登录时用 SSH 隧道转发服务器127.0.0.1:6080。
3. 首次以登录模式启动：

```bash
WORKER_COMMAND=login docker compose -f deploy/compose.browser-worker.yaml up --build
```

4. 打开隧道后的 `http://127.0.0.1:6080/vnc.html`，完成该账号正常登录。登录保存后进程结束。改为运行模式：

```bash
WORKER_COMMAND=run docker compose -f deploy/compose.browser-worker.yaml up -d
```

不要让 login 和 run 同时占用相同profile。profile数据卷和worker-state数据卷必须保留。登录过期或遇到验证时停止领取，管理员正常登录后再运行；不自动解决验证码。

Docker文件和Linux流程需要在目标服务器实测，本机没有运行Docker Engine，不能把提供部署文件当成完成服务器部署。实际支持与本轮结果见 `evidence/native-worker-verification.json`。

现有 `/v1/videos`、`/v1/videos/{video_id}`、`/content`，图片接口、密钥、账号调度、请求详情与进度记录继续使用。四模型/参考素材的参数和网关限制见 VIDEO_V4.md；所有新组合均须分别记录真实验证结果。
