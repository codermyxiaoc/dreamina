# Linux 自部署与后台账号登录

主 API 容器运行 FastAPI、管理界面和每个账号独立的 Chromium 执行器；PostgreSQL 保存平台数据。在后台添加账号后，可直接通过登录面板操作 Dreamina 官方登录页。成功后自动保存独立浏览器配置、加密登录快照并同步查询凭据，默认启动该账号的执行器。无需浏览器扩展、手动复制 Cookie 或另开服务器桌面。

登录面板通过已有的管理员会话传输网页画面和操作，服务器仍需真实 Chromium。验证码、二次验证和第三方登录由用户在面板中完成；网页或登录提供方是否接受服务器浏览器，须在目标环境验证。

当前线上是v0.13.2，已完成Docker升级、PostgreSQL迁移和五账号就绪验收；显式保留现有直连账号策略。五账号并发及单账号三在途生成实测来自上一版，本轮没有通过真实代理生成验证；范围见 [STATUS.md](../STATUS.md)。本次脱敏发布记录位于 `outputs/server-deployment/v013-verification.json`；2026-10-26的v0.8.0首次安装记录仍保留，属于历史部署证据。

## 1. 准备配置并启动

目标服务器需具备 Docker Engine 与 Compose，以及访问 Dreamina 和登录服务的网络连接。复制 `deploy/server.env.example` 为 `deploy/server.env`，填写独立随机值。`ADMIN_PASSWORD`、`PLATFORM_ENCRYPTION_KEY`、`CHROME_BRIDGE_KEY`、`POSTGRES_PASSWORD`、`LOCAL_API_KEY` 必须非空；管理员密码至少 12 字符。

新安装默认 `ACCOUNT_PROXY_REQUIRED=true`，必须先在管理后台创建并检测代理，再给账号绑定和登录。**现有5个直连账号升级到v0.13.0前，先在已有服务器配置中添加 `ACCOUNT_PROXY_REQUIRED=false`**；不要复制空模板覆盖密钥或账号数据。false仅显式允许未绑定账号使用原直连，绑定代理失败仍不会回退直连。待所有运行账号安全绑定并验证代理后，可以改回true。

也可先在已安装项目依赖的环境执行 `python deploy/init_env.py`，生成不会显示到终端的独立随机密钥；已有文件不会覆盖。该脚本生成基础密钥配置，新增浏览器选项使用 Compose 默认值，可从示例文件补充调整。不要覆盖现有部署的主密钥或数据库密码。

手动生成时，`PLATFORM_ENCRYPTION_KEY` 使用 Fernet key，其余各项分别生成 URL-safe 随机值，不复用：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
chmod 600 deploy/server.env
docker compose --env-file deploy/server.env config --quiet
docker compose --env-file deploy/server.env up -d --build
docker compose --env-file deploy/server.env ps
curl http://127.0.0.1:8787/health
```

数据库密码会写入连接 URL，使用 URL-safe 字符。`config --quiet` 仅验证配置，不输出展开后的密钥。镜像构建会安装 Python Playwright 对应的 Chromium 及系统依赖，需要访问相关软件源。

API 默认仅映射服务器本机 `127.0.0.1:8787`。使用主机上的 Caddy/Nginx 提供 HTTPS，并反代到该地址；可参考 `Caddyfile.example`。`FORWARDED_ALLOW_IPS` 填写 API 容器实际看到的反代来源 IP；主机代理经过 Docker 网络时通常是该网络的网关地址，请按实际环境填写。这会影响 HTTPS 识别、Secure 管理会话 Cookie 和同源校验。不要使用公网通配符。

明确需要直接开放应用端口时，可设置 `API_BIND=0.0.0.0` 和 `API_PORT=8989`；默认仍为本机绑定。管理员初始化默认要求12位密码，部署方可用 `ADMIN_PASSWORD_MIN_LENGTH` 显式设置8–128之间的最短长度；它只影响初始化校验，不会自动更改已有管理员密码。

公开Bearer API支持CORS预检。`PUBLIC_API_CORS_ORIGINS=["*"]` 为默认值；可配置如 `["https://client.example","http://localhost:3000"]`，或 `[]` 禁用。只允许GET/POST/OPTIONS，不启用Cookie凭据；后台、Chrome桥接和内部接口不套用这条策略。预检请求必须同时包含Origin与Access-Control-Request-Method；裸OPTIONS仍可能返回405。浏览器端使用API Key并省略Cookie，401错误也可被客户端读取。

管理员由 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 首次初始化，后续修改环境变量不会自动覆盖已有管理员。容器使用 UID/GID `10001:10001` 运行，浏览器安装在只读使用的 `/ms-playwright`，专用 HOME 为 `/home/dreamina`；应用数据写入 `/app/outputs`。Compose 启用 init 进程与 1 GiB 共享内存。若改用宿主机目录挂载，须由服务器管理员预先确保该 UID 对指定数据目录可写；不要挂载日常 Chrome 配置目录。

## 2. 添加多个账号与重新登录

1. 打开 HTTPS 域名下的 `/admin/` 并登录管理后台。
2. 在「代理 IP」添加代理或每行批量导入 `protocol://user:pass@host:port`，先点击测试，确认出口IP和时区已取得。v0.13.2代码限制为最多200个非空行、200000字符，校验或保存失败整批回滚，重复项去重；提前到期提醒默认3天。支持HTTP(S)和无认证SOCKS5；**带用户名密码的SOCKS5暂不支持浏览器**，请改用支持的代理。密码中的特殊字符需要URL百分号编码。
3. 在账号页添加账号，先选择已测试代理，再设置账号标识、区域等信息。创建后在弹出的登录面板里完成官方网页登录。登录与后续执行使用同一绑定出口。显式允许直连的服务可不选代理。
4. 等待“登录成功、状态已保存、凭据已同步”；执行器启动后还需等待在线、就绪心跳。账号保存成功不等于已经可以接收生成任务。
5. 依次添加其他账号。每个账号使用独立配置目录、登录状态和任务队列；同一网站账号不能重复注册到不同账号条目。每个代理默认绑定上限3个账号，增加容量必须明确确认。
6. 登录失效后，在原账号条目选择重新登录。系统先检查未完成任务，再请求原执行器正常退出，完成新登录后保存并启动。遇到未核对任务或停止超时，应先核对任务；系统不会自动重复提交生成。

更换账号代理必须空闲。编辑账号选择新代理后，服务端探测、停止原节点、冻结并写入新出口，再核验会话；跨国及容量超限分别要求确认。v0.13.2中临时核验错误保留checking并自动退避重试，界面明确显示等待核验；只有明确认证失败才要求处理登录，不能把暂时网络错误当作登录失效。代理密码不回显，编辑代理时连接信息留空表示保留；备用代理只会在到期、账号空闲且通过同样验证时切换。健康检查和人工测试会访问配置的IP/地区查询服务及Dreamina端点，不创建视频、不携带账号Cookie做探测。

v0.13.2新增每代理的出口IP变化策略：默认仅告警，也可选择暂停新任务。IP查询服务全部暂不可用但Dreamina可达时，显示出口IP待确认，新绑定等待当前IP恢复；既有代理不会仅因IP查询服务故障被判为连接失败。上述v0.13.2功能已于2026-10-28部署；实际版本和验证范围见STATUS发布记录。

登录面板关闭或超时不会被当作成功。第三方登录可能打开页面内的新标签，可在面板里切换；只允许预设的登录站点导航。密码与验证码直接交给网站，不写入平台请求日志。登录快照和同步凭据加密存储，但 Chromium 本身的持久化配置目录也含有会话数据，因此整个数据卷都属于敏感备份。

## 3. 资源限制与无头模式

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `LOGIN_BROWSER_HEADLESS` | `true` | 无头 Chromium；设为 `false` 时入口脚本启动 Xvfb 虚拟显示，后台登录面板操作方式不变 |
| `LOGIN_MAX_SESSIONS` | `2` | 同时打开的登录面板数量，范围 1–10 |
| `LOGIN_SESSION_TTL` | `900` | 单次登录有效秒数，范围 60–3600 |
| `LOGIN_AUTO_START_WORKER` | `true` | 登录成功后自动启动该账号执行器 |
| `MANAGED_MAX_WORKERS` | `4` | 常驻执行器上限，范围 1–100；登录面板另计资源 |
| `MANAGED_STOP_TIMEOUT` | `20` | 等待执行器正常停止的秒数，范围 1–60 |
| `INTERNAL_API_URL` | `http://127.0.0.1:8787` | 同一容器内执行器访问 API 的地址，与宿主机 `API_PORT` 无关 |
| `WORKER_MODE` | `native` | 使用无扩展执行器 |
| `NATIVE_PAGE_RELOAD_JOBS` | `20` | 常驻任务页每使用20单后重新加载，范围1–100；不改变生成重试规则 |
| `MAINTENANCE_MEDIA_RETENTION_DAYS` | `0` | 中央视频MP4清理默认禁用；启用范围30–3650天，仅符合终态和引用保护条件的文件可清理 |
| `MAINTENANCE_INPUT_RETENTION_DAYS` | `0` | 中央参考输入清理默认禁用；启用范围30–3650天，未知或仍有活动引用的素材保留 |
| `ACCOUNT_PROXY_REQUIRED` | `true` | 新安装要求代理；现有5个直连账号升级时显式false，已绑定代理永不回退直连 |
| `PROXY_HEALTH_INTERVAL_SECONDS` | `600` | 已绑定、启用代理的探测间隔，范围60–86400秒；三次连续失败暂停，连续两次成功恢复健康 |
| `PROXY_IP_CHECK_URLS` | `https://api64.ipify.org?format=json,https://api.ipify.org?format=json,https://ipwho.is/` | v0.13.2；1–8个逗号分隔HTTPS地址，按顺序通过同一代理查询出口IP |
| `PROXY_IP_CHECK_URL` | 未设置 | 旧单地址兼容项；未设置复数配置时作为首选，并自动补默认备用 |
| `PROXY_TARGET_CHECK_URL` | `https://dreamina-api.us.capcut.com/` | 经同一代理检查Dreamina TLS/HTTP可达性；成功不等于登录或生成授权 |
| `PROXY_GEO_CHECK_URL` | `https://ipwho.is/` | 返回相同出口IP对应的国家/时区；缺少有效IANA时区时不能绑定 |

主镜像固定使用已安装的 `chromium` 通道，`PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`。默认不启动 Xvfb。改为 `LOGIN_BROWSER_HEADLESS=false` 后，入口脚本启动 `:99` 虚拟显示且关闭 TCP 监听；不安装或开放 VNC、noVNC、Chrome 调试端口。诊断信息保存在数据卷中的 `managed-browsers/xvfb.log`。

当执行器已达上限，新账号仍会保存登录状态，返回 `saved_capacity_wait`，稍后可在账号页启动。限制不会自动把账号轮换进出浏览器，也不会根据等待状态宣称账号就绪。先用较小并发观察服务器内存，再按容量调整；登录面板与生成浏览器都会消耗资源。

修改配置后，在没有运行中或结果待核对的生成任务时执行：

```bash
docker compose --env-file deploy/server.env up -d --build api
```

容器重建会结束其中浏览器进程，持久化登录状态仍在。API 重启不会盲目自动重启未知状态执行器；检查账号页的执行器与任务状态，再启动需要运行的账号。

## 4. 持久化、备份与升级

`media-data` 挂载 `/app/outputs`，包含媒体、任务回执、桥接队列以及：

- `managed-browsers/profiles/<account_id>/`：账号独立的 Chromium 配置。
- `managed-browsers/auth/<account_id>.enc`：账号派生密钥加密的登录快照。
- `managed-browsers/workers/<account_id>/`：私有执行器配置、进程记录、状态与日志。

`postgres-data` 保存平台账号、代理及加密认证信息、哈希 API key、管理员会话和任务数据。备份数据库与 `media-data`，另行妥善保管 `PLATFORM_ENCRYPTION_KEY`；丢失主密钥将无法解密同步凭据、代理密码和登录快照。执行器配置含内部桥接密钥，浏览器配置含会话状态，不应作为普通媒体公开下载。代理凭据不写入托管Worker的JSON配置，而经进程环境传递；服务器进程环境也应视为敏感数据。不要执行 `docker compose down -v` 来升级，否则会删除命名数据卷。

升级前备份原数据库和数据卷。v0.13会将合法旧代理字符串迁移到代理表，迁移失败的账号暂停，不因允许直连而绕过错误；迁移的代理必须重新探测并完成安全绑定验证后再运行。不要在已有生产库上使用初始化脚本生成新的主密钥。升级涉及新增数据结构，回滚应使用成对的旧镜像、数据库和数据卷备份。

中央媒体和参考输入自动清理默认关闭，独立于原有浏览器缓存清理。选择启用时，将对应保留天数设为至少30天；只有中央任务为completed/failed/cancelled、终态时间与文件mtime均超过保留期、且没有活动任务或bridge引用的已知文件才可删除。未知来源、待核对任务、仍被引用的素材继续保留。清理不删除任务历史和上游submit_id；后续 `/v1/videos/{video_id}/content` 可使用原ID尝试重新下载，能否恢复取决于上游是否仍提供产物。重要成品应另外备份，输入素材删除后不会凭空恢复。

镜像采用 COPY 白名单，`.dockerignore` 排除本机密钥、抓包、输出和浏览器配置。当前只支持一个 API 控制端实例，桥接队列仍为该实例的数据卷；不要直接横向扩容 API 容器。

已有外部执行节点仍可按 [`NATIVE_WORKER.md`](../NATIVE_WORKER.md) 单独配置；本页的一体化模式不需要 `compose.browser-worker.yaml` 或它的 VNC 登录流程。不要同时让两种运行方式使用同一账号配置目录。

## 5. 上线检查

先验证 `/health`、后台管理员登录、代理探测、账号绑定和登录面板自动保存，再确认执行器在线就绪。已有直连服务先确认显式false生效，逐个空闲迁移账号。随后只提交经批准的小任务，核对上游真实生成、ID、进度、下载与槽位释放，最后增加账号数与并发。`/health` 成功只说明 API 可访问，代理检测成功只说明指定出口可达，均不能证明Dreamina登录或生成已通过。

静态 Compose 校验不依赖已运行的 Docker Engine；镜像构建、Chromium 启动、服务器登录和真实生成分别需要部署环境验证。Linux 无头或 Xvfb 模式都不会绕过网站验证要求，切换模式本身也不保证上游接受会话。
