# Dreamina Hub · 图片与视频 API 管理平台

**v0.13.2 已打包发布（2026-10-28）。** 提交安全、故障恢复、代理与空闲休眠修复已上线；五个节点及自动唤醒验收通过，未新增收费生成。完整范围见 [STATUS.md](STATUS.md)。

**v0.13.1 节点生命周期修复已上线。** 缩减槽位会停止超额槽接单并安全关页、清理登记；删除账号同步停止并删除对应浏览器节点，保留历史任务。线上已验证1→2→1，截图中的旧离线槽位已清理，未新增生成费用。见 [STATUS.md](STATUS.md)。

**v0.13.0 已部署并验收（2026-10-27）。** 新增代理 IP 管理、账号安全绑定、按代理分组节流，以及崩溃恢复、待核对任务槽位和流式上传的改进。当前线上五个账号均就绪；本轮没有新增真实代理生成测试或消耗积分。验证范围见 [STATUS.md](STATUS.md)。

新安装默认 `ACCOUNT_PROXY_REQUIRED=true`，添加账号前先在后台「代理 IP」导入并测试代理。现有5个直连账号升级时，需在服务器配置中**显式设置 `ACCOUNT_PROXY_REQUIRED=false`**，再逐个空闲切换到已测试代理。已绑定代理失效始终暂停相关账号，不会回退直连。带认证的 SOCKS5 暂不支持浏览器，请使用受支持的 HTTP(S) 认证代理或无认证 SOCKS5；代理探测成功不等于账号登录或生成成功。升级步骤见 [部署文档](deploy/README.md)。

v0.13.2的代理批量导入上限为200个非空行、200000字符；任一条目校验或保存失败时整批回滚，重复项由服务端去重。到期提醒默认提前3天。出口IP变化策略可选择仅告警（默认）或暂停新任务；这不改变代理故障、到期和手动停用的暂停规则。代理切换遇到临时核验错误会保留 `checking` 并退避重试，不直接把账号判为登录失效。

常驻任务页默认每20次使用后重载，可用 `NATIVE_PAGE_RELOAD_JOBS` 调整。中央成品与参考输入清理默认关闭；分别启用 `MAINTENANCE_MEDIA_RETENTION_DAYS` / `MAINTENANCE_INPUT_RETENTION_DAYS` 时保留期至少30天，活动、待核对及未知引用文件仍保留。成品清理后可按原submit_id尝试从上游重新下载，不保证上游永久保存。

现在支持 `POST /v1/videos` 提示词精准引用 `@图片1`、`@图片2`、`@视频1`，后台包含可复制示例。可用账号按最少在途任务优先，同负载持久化轮询；参数、编号及调度规则见 [PLATFORM.md](PLATFORM.md)。

**可靠性优化：** 已加入请求ID持久化、提交前有限重试、原任务查询恢复、账号模型冷却及真实成功率统计。99%是目标，尚未得到足够线上样本证明；详见 [RELIABILITY.md](RELIABILITY.md)。

**最新实测（2026-10-26）：** 2.0（4秒720p）与2.5（4秒480p）均已通过真实生成、公开进度查询、下载和解码，每个模型各68积分。本次136积分与此前38积分测试分别记录。Seedream图片、Mini多参考素材及Fast文生视频也已有成品，其中Fast首次回执经人工核对恢复。参数范围及产物见 [LIVE_MODEL_TEST.md](LIVE_MODEL_TEST.md)。

**0.6：可直接在后台添加账号并登录。** 点击「创建并登录」会打开内嵌登录面板，成功后自动保存状态、加密同步凭据并启动原生 Worker。支持多账号独立环境与失效后重新登录，无需扩展或手动复制 Cookie。详见 [ACCOUNT_LOGIN.md](ACCOUNT_LOGIN.md)。独立 Worker 命令仍保留，见 [NATIVE_WORKER.md](NATIVE_WORKER.md)。旧扩展仅供回溯，`WORKER_MODE=native` 已停止其派发。

当前版本提供中文可视化后台、多账号与调度、API 密钥管理、OpenAI Images 风格接口及历史 Videos 协议兼容层。通过已登录的真实 Chrome 节点执行原生生成，客户端只使用本平台签发的 Bearer key。

**管理后台：** `http://127.0.0.1:8787/admin/`  
**接口文档：** `http://127.0.0.1:8787/docs`  
**本机初始登录信息：** `outputs/platform-access.txt`（不要提交到 Git 或公开）

| 已接入能力 | 参数范围 |
|---|---|
| 图片生成 `POST /v1/images/generations` | Seedream 5.0 Lite，2048×2048，1张，URL/base64 |
| 视频任务 `POST /v1/videos` | 4个Seedance模型已有真实成品；Mini/2.0/2.5自动完成，Fast回执经人工核对恢复；验证参数见实测记录 |
| 参考素材 | Mini已实测同一任务携带2张图片和1段视频 |
| 管理与权限 | 独立管理员会话、加密账号凭据、哈希 API key、额度/限速/并发、任务归属 |
| 调度与节点 | 能力/在线状态/账号优先级/空闲槽位筛选，持久化队列与不确定状态保护 |
| 自部署 | FastAPI + PostgreSQL Compose；后台内登录及无扩展Chromium Worker；v0.12.1已有Linux多账号实测，v0.13.0代理路径待真实验证 |
| 代理管理 | 加密凭据、批量导入、健康与到期检查、空闲安全绑定、按代理分组节流；默认不回退直连 |

原生执行器已接入4个视频模型的参数与参考素材上传；成功组合以实测记录为准，不能据此保证所有模型权限和尺寸组合。图片编辑仍未接入。v0.12.1已有五账号Mini并发和单账号三在途实测，详见 [STATUS.md](STATUS.md)；真实页面复用仍存在素材未清空时回退新页的情况，不能把离线测试当作已解决的线上行为。

```powershell
python -m pip install -r requirements.txt
python -m dreamina_api serve --host 127.0.0.1 --port 8787
```

当前工作目录已配置初始账号和本地服务；不要用空模板覆盖已有 `.env`。新服务器按部署文档配置管理员、数据库和独立密钥。

- [平台操作、API 示例与能力边界](PLATFORM.md)
- [服务器与浏览器节点部署](deploy/README.md)
- [无扩展浏览器 Worker 与 Linux 虚拟显示部署](NATIVE_WORKER.md)
- [Chrome 扩展安装与配对](chrome-extension/README.md)
- [官方 OpenAI SDK 调用示例](examples/openai_client.py)
- [上游协议与 REA 证据](PROTOCOL.md)

每个新的付费生成请求使用唯一 `Idempotency-Key`，重试复用原值。出现 409 和 `task_id` 时恢复原任务，不重新创建。平台的“请求次数额度”不等同于 Dreamina 积分账单。

视觉布局与明暗主题参考 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)，前端独立实现。此项目不是 OpenAI 官方服务；API 模型使用实际 Dreamina 名称。

<details>
<summary>早期实现与分析记录（当前调用以平台文档为准）</summary>

## 早期视频桥接实现

基于当前网页、用户提供的 HAR、REA 静态分析独立实现。目标是用本地 REST API 或命令行控制 Dreamina，无需手动进入网页。

## 当前验证状态

**方案 1 的 Chrome 桥接已完成真实端到端验证：本地 API → 扩展 → 原有 Chrome 原生提交 → API 查询成功 → MP4 下载。** 首版仅支持 `default` 账号的 **Seedance 2.0 Mini / 4 秒 / 720p / 16:9 / 无素材文生**。Chrome 的已登录标签页和本地服务需要保持运行。

使用方法见 [CHROME_BRIDGE.md](CHROME_BRIDGE.md)。推荐入口是 `POST /v1/chrome/jobs`，或 `python examples/chrome_client.py examples/chrome-job.json`。后者会实际生成并消耗积分。

原来的纯 HTTP / 离线签名 / 独立后台浏览器提交仍返回 `ret=-6`，没有因此宣称修复了 token-only 生成。其他模型、比例、分辨率、图生、参考素材和多账号尚未接入已验证的桥接路径。

| 功能 | 状态 |
|---|---|
| sessionid 鉴权、积分读取 | 已用实际账户验证 |
| submit_ids 轮询、完成视频下载 | 已用实际已完成任务验证 |
| 图片 ImageX 上传 | 512×288 PNG，直接上传实测成功 |
| 视频 VOD 上传 | 约 4.6 MB MP4，分片上传实测成功 |
| 参考素材预审 | 测试图片和视频各一次，均返回 Pass |
| 四个模型、比例、分辨率、时长参数校验 | 根据当前网页配置实现 |
| Mini 文生视频网页流程 | HAR 捕获成功：4 秒 / 720p / 16:9 / 20 积分 |
| Chrome 桥接 API 生成 | Mini / 4s / 720p / 16:9 文生已完成一次 API 队列生成和下载 |
| 原纯 HTTP 新视频提交 | 仍被服务端拒绝；不要将此路由误认为已打通 |
| 图生视频、Omni 参考图/视频生成 | 资源上传及 draft 构造已实现，生成待验证 |
| 2K / 4K | 网页配置存在，具体提交/超分路径待验证 |

机器可读记录：`evidence/verification.json`。本地原始证据、凭据、输出均在 `.gitignore` 中排除。

早期 5 次直接提交均返回拒绝且查不到任务。之后真实 Chrome 原生创建了两项测试任务（一项音频输出审核失败，一项成功）；安装扩展后又通过本地队列独立创建并完成一项任务。`outputs/chrome-api-verified.mp4` 是最新 API 队列测试结果。Python 104 项测试、43 项参数子测试及扩展 13 项 Node 测试通过。

## 安装与配置

在本目录运行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

已有 Playwright Chromium 时不必重复安装。离线签名器也接受环境变量 `SIGNER_CHROMIUM` 指定一个现有 Chromium 可执行文件。

复制 `.env.example` 为 `.env`，填写 `DREAMINA_SESSION_ID`。本机工作目录已经配置，不要覆盖现有 `.env`。

- `DREAMINA_SESSION_ID` 是 Dreamina 的 `sessionid` Cookie，不是官方 API key；没有实现刷新失效会话的协议。
- `DREAMINA_REGION=US` 是本次实际验证的区域；其他路由配置仅为参考实现。
- `DREAMINA_WORKSPACE_ID` 可关联一个工作区。切换账户时不要沿用旧账号工作区。
- `DREAMINA_MS_TOKEN`、`DREAMINA_DEVICE_ID` 是可选的本地 SDK/设备状态。捕获的 `X-Bogus` / `X-Gnarly` 不会被重放。
- `DREAMINA_SIGNING=browser` 正常加载官方网页完成 SDK 初始化，然后由独立后台 Chromium 提交一次请求；不会访问用户 Chrome 配置。当前实测仍被拒绝，不能将其当成已成功生成的方案。完整 Cookie 配置会提供给这个隔离会话。
- `DREAMINA_SIGNING=offline` 在无外网 Chromium 中运行固定哈希 SDK。`none` 只使用常规 MD5 Sign。这两种模式生成也未通过实测。
- `DREAMINA_API_ORIGIN` 可指定已观测的 US/TTP2/SG API 源。后台页面模式以该会话实际路由为准。
- `LOCAL_API_KEY` 保护本地 REST 服务，与上游 sessionid 分开。

## 命令行

```powershell
python -m dreamina_api check
python -m dreamina_api models
python -m dreamina_api prepare examples/text-to-video.json
python -m dreamina_api generate examples/text-to-video.json --wait 900 --download
python -m dreamina_api poll SUBMIT_ID --wait 900
python -m dreamina_api download SUBMIT_ID
python -m dreamina_api upload C:/path/to/image.png
python -m dreamina_api upload C:/path/to/reference.mp4 --type video
```

`prepare` 只构造请求，不上传、不生成、不扣积分；使用已上传的资源对象进行预览。`generate` 可能扣除账户积分。失败不会自动重试提交。提交超时或响应不完整时返回 `submission_uncertain` 和 `submit_id`，应先查询该 ID。

上面的 `dreamina_api generate` 是仍受阻的直接提交路径。已打通的 Chrome 路径请用：

```powershell
python examples/chrome_client.py examples/chrome-job.json --output outputs/my-video.mp4
```

任务回执保存在 `outputs/tasks/`，视频保存在 `outputs/`。响应中的视频链接可能过期，可再次查询获取。

## 本地 REST API

```powershell
python -m dreamina_api serve --port 8787
```

服务仅监听 `127.0.0.1`。交互式接口定义位于 `http://127.0.0.1:8787/docs`，调用示例：

```powershell
$body = Get-Content -Raw -LiteralPath ./examples/text-to-video.json
Invoke-RestMethod http://127.0.0.1:8787/v1/videos/prepare -Method Post -ContentType application/json -Body $body
Invoke-RestMethod http://127.0.0.1:8787/v1/videos/generations -Method Post -ContentType application/json -Body $body
```

设置 `LOCAL_API_KEY` 后需要 `Authorization: Bearer <本地API密钥>`。可用 `X-Dreamina-Session: <sessionid>` 为单次请求提供上游会话；这会清除默认工作区关联。凭据不放在 URL 中。

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/health` | 本地服务状态 |
| GET | `/v1/models` | 当前四模型配置、限制及验证标记 |
| POST | `/v1/auth/check` | 验证登录并读取积分 |
| POST | `/v1/videos/prepare` | 生成未签名请求预览 |
| POST | `/v1/videos/generations` | 提交；支持 `wait_seconds`、`download` |
| GET | `/v1/videos/{submit_id}` | 查询状态及结果链接 |
| GET | `/v1/videos/{submit_id}/content` | 下载第一个 MP4 结果 |
| POST | `/v1/assets` | `{"type":"image或video","path":"本地文件绝对路径"}` 上传素材 |
| POST | `/v1/assets/audit` | `{"model":"seedance-2.0-mini","references":[已上传资源]}` 参考素材预审 |

使用程序或终端调用；服务拒绝带 Origin 的浏览器请求，避免第三方网页调用本地凭据。

## 模型及生成参数

| API 模型名 | 网页模型 | 普通时长 | 配置中的分辨率 |
|---|---|---|---|
| `seedance-2.5` | Dreamina Seedance 2.5 | 4–30 秒 | 480p、720p、1080p |
| `seedance-2.0-mini` | Dreamina Seedance 2.0 Mini | 4–15 秒 | 720p、1080p、2K、4k |
| `seedance-2.0-fast` | Dreamina Seedance 2.0 Fast | 4–15 秒 | 720p、1080p、2K、4k |
| `seedance-2.0` | Dreamina Seedance 2.0 | 4–15 秒 | 720p、1080p、4k |

比例：`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`。fps 固定为配置中的 24。具体像素由模型/比例/分辨率决定，不接受任意 width/height 冒充官方选项。

普通生成与 2.5 长视频/编辑是不同流程，本版没有把 180 秒直接塞进普通生成接口。

```json
{
  "model": "seedance-2.0-mini",
  "prompt": "A small red sailboat on a calm lake at sunrise.",
  "duration": 4,
  "resolution": "720p",
  "ratio": "16:9",
  "mode": "omni"
}
```

图生视频设置 `mode=frames`，提供 `first_frame`，可选 `end_frame`。`mode=omni` 支持 `references` 图片/视频列表。素材可以是 `{ "path": "...", "type": "image或video" }`，也可以是已上传资源对象：

```json
{"type":"image","image_uri":"已上传的资源URI","width":1280,"height":720,"format":"png"}
```

```json
{"type":"video","vid":"已上传的视频ID","width":1280,"height":720,"fps":24,"duration":4000,"format":"mp4"}
```

生成时长 `duration` 使用**秒**；视频参考资源的 `duration` 使用**毫秒**。示例见 `examples/image-to-video.json` 与 `examples/reference-video.json`。资源上传成功不代表预审通过，也不代表该素材组合已经完成过生成。

## 检查与证据

```powershell
python -m pytest -q
python -m signer.runtime
```

详细上游接口见 `PROTOCOL.md`。README 中的支持与验证状态有意分开：不会将 mock 测试、前端配置或离线签名结果描述成线上生成成功。

参考了用户指定的三个项目的协议和行为；没有直接复制许可证不明确的业务实现。参考项目、网页脚本和 HAR 的分析记录位于 `references/`、`evidence/`；不需要它们即可运行参数校验和 API 服务。

</details>
