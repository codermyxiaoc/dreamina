# 后台内登录账号（0.6）

在「账号管理 → 添加账号」填写名称、区域及模型范围，点击「创建并登录」。后台弹窗显示服务器浏览器中的 Dreamina 页面，直接在其中完成网站登录。账号密码、验证码由操作者输入，无需安装扩展、打开单独浏览器窗口或复制 Cookie。

## 使用流程

1. 创建账号后，登录面板自动打开。已有账号点击操作栏的「登录 / 重新登录」。
2. 点击网页中的输入框后输入或粘贴文字。支持中文输入法、Tab、Enter、滚动和拖动；验证码由操作者完成。第三方登录的新窗口可在面板顶部下拉框切换。
3. 系统观察第一方身份响应，并核对该响应所用会话与当前 Cookie 一致，再验证查询凭据。身份确认后自动保存浏览器配置和加密 Cookie 快照、同步账号查询凭据。
4. 保存完成后自动尝试启动对应账号的原生 Worker。只有节点真正上报就绪后才可参与任务调度。未启用的账号保留状态但不启动；节点容量已满时，可稍后在账号列表点击启动。
5. 账号明确出现登录失效或验证要求时，会变为「需要登录」并停止新任务调度。重新打开登录面板即可恢复，保存成功后自动尝试启动节点。

关闭面板会取消未完成的登录；已经保存的状态不会被取消。刷新整个后台页面后，可在原管理员会话中重新打开同一账号面板。空闲登录会话默认 15 分钟超时释放。

## 多账号与隔离

- 每个账号使用独立浏览器目录、加密快照及派生密钥；不读取日常 Chrome 的配置目录。
- 第一次成功登录会绑定网站账号身份摘要。重新登录成另一个网站账号、或重复添加已绑定的网站账号，都会拒绝覆盖已保存的凭据；需要新身份时应创建新记录。
- 登录期间账号状态为 `login_pending`，不接收新任务。账号有正在执行或提交结果不确定的任务时，先核对任务再登录；后台不会强制结束生成进程。
- 默认最多同时开启 2 个登录面板、运行 4 个托管 Worker。已保存账号数量与并行浏览器数量分开管理；每个账号仍需完成首次授权，登录失效时再人工登录。
- 只有创建登录会话的管理员会话能读取画面和发送操作。其他管理员会话不能接管；原会话失联后等待超时释放。

## 存储与部署

状态保存在 `outputs/managed-browsers/`：

| 目录 | 内容 |
| --- | --- |
| `profiles/<account_id>` | 浏览器持久化目录，包含网站本地存储和浏览器数据 |
| `auth/<account_id>.enc` | 使用账号独立派生密钥加密的第一方 Cookie 快照 |
| `workers/<account_id>` | 执行器配置、进程登记和任务恢复记录 |

账号查询凭据在数据库中加密保存；网页登录的密码、验证码和画面不写入应用日志。浏览器配置目录本身不是应用加密文件，应与数据库、主加密密钥一起按私有数据保护和备份。丢失或更换 `PLATFORM_ENCRYPTION_KEY` 会导致旧凭据及快照无法解密。

登录快照替换有事务回滚备份。数据库提交失败会恢复旧快照；服务重启会根据账号提交状态完成恢复。Worker 恢复快照失败时保留原文件，避免用空浏览器状态覆盖它。

| 环境变量 | 默认值 | 作用 |
| --- | --- | --- |
| `LOGIN_MAX_SESSIONS` | `2` | 同时开启的登录环境，1–10 |
| `LOGIN_SESSION_TTL` | `900` | 登录有效秒数，60–3600 |
| `LOGIN_BROWSER_HEADLESS` | `true` | 登录环境和托管 Worker 使用无头模式 |
| `LOGIN_BROWSER_CHANNEL` | Windows `chrome`，Linux `chromium` | 浏览器选择 |
| `LOGIN_BROWSER_ARGS` | 未配置时 `[]` | JSON 数组，传给登录浏览器和托管 Worker；本机已按本次要求配置下列 8 项 |
| `LOGIN_AUTO_START_WORKER` | `true` | 保存成功后尝试启动节点 |
| `MANAGED_MAX_WORKERS` | `4` | 本控制进程可启动的节点上限 |
| `MANAGED_STOP_TIMEOUT` | `20` | 等待空闲节点正常退出的秒数 |
| `INTERNAL_API_URL` | `http://127.0.0.1:8787` | 托管节点访问本服务的地址 |

Linux 主 Compose 镜像已经包含 Chromium 和浏览器依赖。默认无头；设置 `LOGIN_BROWSER_HEADLESS=false` 时，镜像入口启动 Xvfb，登录画面仍通过管理后台展示，不需要 VNC 端口。部署命令与 HTTPS 配置见 [deploy/README.md](deploy/README.md)。仅运行一个 API 控制进程；登录会话和本机进程监管不支持多副本之间任意路由。API 水平扩展需另外引入会话路由及独立 Worker 控制服务。

## 接口与验证范围

管理接口沿用管理员 HttpOnly Cookie，所有写请求要求 `X-CSRF-Token`：

| 方法 | 端点 | 用途 |
| --- | --- | --- |
| POST / GET | `/api/admin/accounts/{account_id}/login-sessions` | 创建或读取当前登录会话 |
| GET | `/api/admin/login-sessions/{id}` | 登录及保存状态 |
| GET | `/api/admin/login-sessions/{id}/frame` | 不缓存的登录画面 |
| POST | `/api/admin/login-sessions/{id}/input` | 有界的人机输入；不执行任意脚本 |
| DELETE | `/api/admin/login-sessions/{id}` | 取消未完成的登录 |
| POST | `/api/admin/accounts/{account_id}/worker/start` | 启动已有保存状态的节点 |
| POST | `/api/admin/accounts/{account_id}/worker/stop` | 等待空闲节点正常退出 |

已完成真实 Chromium 离线登录链路测试、账号与凭据隔离测试，以及本机后台真实 Dreamina 页面显示/点击/取消联调。真实账号登录成功后的凭据同步与生成仍需使用者在面板完成登录后验证。Linux Docker 构建和服务器实测尚未完成；Compose 配置校验通过。

2026-10-26 已在本机验证登录状态保存后自动启动无扩展Worker，并执行真实图片/视频测试。当前账号的具体成功和受阻范围见 [LIVE_MODEL_TEST.md](LIVE_MODEL_TEST.md)；Linux部署仍未实测。

网站或第三方身份服务可能要求额外验证，或拒绝服务器浏览器。面板传递正常网页登录操作，不自动解决验证码，也不保证所有第三方登录方式都接受该运行环境。

### Google 显示「This browser or app may not be secure」

Google 明确可能阻止受自动化控制的浏览器登录。当前登录环境由 Playwright 启动，因此与该限制相符，但截图本身不能确定唯一触发因素。仅将 `LOGIN_BROWSER_HEADLESS` 改成 `false` 仍然属于自动化浏览器，不保证解决。[Google 官方说明](https://support.google.com/accounts/answer/7675428?hl=en)

面板进入 Google 登录页时会显示兼容性说明，并提供切回已有 Dreamina 窗口或返回上一页的按钮。不会自动重试登录，也不会自动切换账号认证方式。

只有原账号确实绑定了邮箱登录时，才使用对应邮箱方式。CapCut 官方说明，相同邮箱通过 Google 与 Email 登录可能打开不同账号；应核对原账号身份、积分及会员，不能只比较邮箱。[CapCut 官方账号说明](https://www.capcut.com/help/why-same-email-ink-different-CapCut-account)

对于只有 Google 登录的账号，可评估将登录阶段改为后台中的人工远程桌面普通浏览器，再把已登录的专用环境交给 Worker；这需要额外实现和验证，不属于当前 Playwright 登录面板已经具备的能力。

### 自定义浏览器启动参数

本机 `.env` 和部署示例已加入用户指定的参数：

```dotenv
LOGIN_BROWSER_ARGS=["--disable-blink-features=AutomationControlled","--no-sandbox","--disable-setuid-sandbox","--disable-web-security","--disable-features=IsolateOrigins,site-per-process","--disable-dev-shm-usage","--disable-accelerated-2d-canvas","--disable-gpu"]
```

该配置由后台登录浏览器和新启动的托管 Worker 共用；独立 Worker 使用配置文件中 `accounts[].args` 数组，本机 `outputs/native-worker.json` 和部署示例已同步。参数以独立字符串传给 Playwright，不经 shell 拼接；当前只接受上述 8 个开关，不允许参数覆盖账号目录或调试端口。

这些参数包含关闭沙箱、Web 安全检查和站点隔离的选项，仅应用于本项目专用账号浏览器，不应复制到日常浏览器。设为 `LOGIN_BROWSER_ARGS=[]` 可取消额外参数；重启服务后重新打开登录面板才会使用新配置，已运行的 Worker 需在空闲时重新启动。

参数已传递不代表 Google 已接受登录，也不代表授权完成。仍需要在新面板中实际验证。
