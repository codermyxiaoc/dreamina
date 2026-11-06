# Dreamina Hub 管理平台

当前账号的最新真实结果见 [LIVE_MODEL_TEST.md](LIVE_MODEL_TEST.md)：Seedream、Mini参考素材、Fast、2.0和2.5均已有成品，Fast首次回执经过人工核对恢复。以下旧桥接范围为历史记录，当前验证范围由模型目录中的 `verification` 字段给出。

**0.6 后台登录已接入：** 添加账号自动打开登录面板，成功后加密保存 Cookie 快照及查询凭据，尝试启动独立托管节点。支持重新登录、身份绑定和并发容量管理；不需要浏览器扩展或手动复制 Cookie。使用与验证边界见 [ACCOUNT_LOGIN.md](ACCOUNT_LOGIN.md)。以下生成实测记录属于之前的执行环境，不代表新登录环境已经完成真实生成验证。

当前默认执行方式已切换为无扩展 Native Worker；浏览器配置持久化、原生新会话、原生文件上传、生成响应监听均由 Python 管理。后台/API/任务记录复用。启动与登录见 `NATIVE_WORKER.md`。旧扩展的历史实测不等同于新 Worker 已通过线上生成；本轮用户选择先完成代码，真实登录/生成待继续验证。

管理后台：`http://127.0.0.1:8787/admin/`。Swagger：`/docs`。当前本机初始管理员信息在 `outputs/platform-access.txt`；请仅在本机查看。后台创建的客户 API key 只显示一次，后续只保存哈希和前缀。

本轮已用网关密钥及官方 OpenAI Python SDK完成真实验证：`/v1/images/generations` 返回有效 JPEG base64；`/v1/videos` 创建任务并最终 `completed`，`/content` 返回 MP4。结果分别在 `outputs/openai-image-v3-verified.jpeg` 与 `outputs/openai-video-verified.mp4`。联调失败记录保留在任务列表中，没有隐藏或改写为成功。

## 已实现的功能

- 中文可视化后台：概览、账号、API 密钥、任务、浏览器节点、模型能力、API 文档、设置。
- 参考 sub2api 的青绿主色、侧栏、卡片/表格和明暗主题，独立实现前端代码。
- 多账号记录与加密凭据；账号状态、区域、工作区、模型范围、优先级、并发槽位、积分检查。
- 自动调度：能力和权限过滤、在线/就绪检查；可用账号按最少在途任务优先，同负载按持久化轮询顺序分配，提交后的任务固定绑定原账号。
- 多 API key：哈希保存、停用、删除、过期、模型/账号池范围、请求次数额度、RPM 和并发控制。
- 任务所有权隔离和幂等键；不确定结果不自动重派，客户端通过原任务 ID 恢复。
- 图片采用 OpenAI Images 风格；视频兼容历史 Videos 协议。网关并不调用 OpenAI 服务。

## 旧桥接 v0.3 的历史生成范围

| 类型 | 模型 | 参数 |
|---|---|---|
| 文生图 | `seedream-5.0-lite` | `2048x2048`、`n=1`、JPEG，URL/base64 |
| 文生视频 | `seedance-2.0-mini` | `seconds="4"`、`size="1280x720"` |

图片编辑、其他视频模型/分辨率、视频参考素材暂未接入执行器。对应能力会明确拒绝，不会静默切换模型。后台的候选模型配置不等于可执行能力。多账号调度与权限已做自动化测试；当前实机测试只使用用户提供的一个账号。

Chrome 需要保持登录并保留 Dreamina 页面。扩展 0.3.0 可通过原生控件切换图片/视频，并被动核对生成接口返回的真实任务标识，避免误用网页临时卡片 ID。不要开启浏览器自动翻译，翻译会修改控件和提示词文本；也不要在该页面提交期间同时手动编辑。

## 密钥与调用

浏览器客户端可跨域调用公开 `/v1/models`、视频及图片兼容接口。预检OPTIONS无需API Key，实际请求仍须发送 `Authorization: Bearer sk-dr-...`；请求应使用 `credentials: 'omit'`，无需后台Cookie。支持JSON、文件上传、Idempotency-Key、SDK请求头和视频Range读取。默认允许任意来源；管理员可通过 `PUBLIC_API_CORS_ORIGINS` JSON数组限定具体来源或用 `[]` 关闭。此配置不开放后台、浏览器桥接或内部操作接口。

在“API 密钥”页面创建一把 key，设置允许的模型和账号池。使用：

```text
Authorization: Bearer sk-dr-...
Idempotency-Key: 每个新生成任务唯一、重试复用的标识
```

图片请求：

```json
{
  "model": "seedream-5.0-lite",
  "prompt": "一只青绿色陶瓷碗，柔和的自然光，无文字",
  "n": 1,
  "size": "2048x2048",
  "response_format": "b64_json"
}
```

发送到 `POST /v1/images/generations`，成功返回 `created` 与 `data[].b64_json`。可选择 `response_format="url"` 获得上游临时签名 URL；需要长期保存时下载或使用 base64。已下载的 base64 结果会私有缓存，数据库只保存文件指针。

视频请求：

```json
{
  "model": "seedance-2.0-mini",
  "prompt": "水彩山间晨雾缓慢移动，无人物、无文字、无对白或音乐",
  "seconds": "4",
  "size": "1280x720"
}
```

发送到 `POST /v1/videos`，返回 `video_...` ID；通过 `GET /v1/videos/{id}` 查询，完成后 `GET /v1/videos/{id}/content` 下载。状态使用 `queued`、`in_progress`、`completed`、`failed`。

额外图片恢复接口：`GET /v1/images/jobs/{id}` 和 `GET /v1/images/jobs/{id}/result`。这两条是本网关扩展，不是 OpenAI 官方接口。出现 HTTP 409 和 `task_id` 时查询原任务，不重新创建；已分配任务后的不确定结果不返回易被 SDK 自动重试的 5xx。

使用官方 SDK：

```powershell
python -m pip install -r requirements-client.txt
$env:DREAMINA_API_KEY = '你在后台创建的网关密钥'
python examples/openai_client.py models
python examples/openai_client.py image --prompt '青绿色陶瓷碗，柔和窗光，无文字'
python examples/openai_client.py video --prompt 'Silent watercolor clouds over a lake, no text or speech'
```

示例的 image/video 命令会实际生成并消耗上游账号积分。中断后视频用 `resume --id video_...`，不要重新创建。平台“请求次数额度”是 API key 的次数限制，不等同于 Dreamina 积分结算。

## 参考图片与参考视频生成视频

参考素材与提示词一起发送到 `POST /v1/videos`。下列为 Bash/cURL 示例，替换 API 地址、密钥、幂等键与文件路径后使用；同一任务的重试复用原幂等键。素材字段是本网关扩展，当前只支持 `mode=omni`。

### 精准 @ 引用规则

提示词可写：`让 @图片1 中的人物在 @图片2 的场景中，按照 @视频1 的步伐自然行走。`

| 标记 | 对应素材 |
| --- | --- |
| `@图片1` | 第1张图片 |
| `@图片2` | 第2张图片 |
| `@视频1` | 第1个视频，与图片独立编号 |

标记在提示词原位置转换成上游素材引用标签，可重复引用同一素材，例如 `让 @图片1 走起来，保持 @图片1 的服装。`。仅上传一张图片时不要写 `@图片2`。文件名和JSON中的 `name` 不参与编号。

- multipart：按请求中同类型文件的接收顺序编号；重复 `reference_images` 或 `reference_videos` 字段。
- JSON：按 `references` 数组内同类型元素出现顺序编号。推荐每次只选JSON或multipart一种素材传法；若混用，整体解析顺序为data URL形式的 `input_reference`、JSON `references`、multipart文件，再按图片/视频独立编号。`input_reference` 若以multipart文件上传，则按文件接收顺序。
- 编号使用从1开始的半角正整数，不带前导零。`@图片0`、`@图片01`、缺少编号、负数、全角数字、小数或未上传的编号，会在创建任务前返回400，不会派发生成。
- `@图片`、`@视频` 是保留前缀，当前不提供转义写法。普通 `@alice` 和邮箱不作为素材引用。不写标记时，原有普通提示词与素材上传方式继续可用。

### 参考图片

```bash
curl http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer sk-dr-YOUR_KEY" \
  -H "Idempotency-Key: <每个新任务唯一ID>" \
  -F "model=seedance-2.0-mini" \
  -F "prompt=让 @图片1 中的人物在 @图片2 的场景里自然行走，保持 @图片1 的人物外观。" \
  -F "seconds=4" \
  -F "resolution=720p" \
  -F "ratio=16:9" \
  -F "mode=omni" \
  -F "reference_images=@./subject.png;type=image/png" \
  -F "reference_images=@./scene.jpg;type=image/jpeg"
```

提示词中的 `@图片1`、`@图片2` 对应第1、第2张图片，`@视频1` 对应第1个视频。`@./file` 是curl文件上传语法，替换为调用方文件路径；多素材重复同名字段。若删除素材，也要删除提示词中对应的引用。不要手动设置Content-Type，curl会生成multipart boundary。

### 参考视频

```bash
curl http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer sk-dr-YOUR_KEY" \
  -H "Idempotency-Key: <每个新任务唯一ID>" \
  -F "model=seedance-2.0-mini" \
  -F "prompt=参考 @视频1 的镜头运动，生成柔和光线下的陶瓷静物。" \
  -F "seconds=4" \
  -F "resolution=720p" \
  -F "ratio=16:9" \
  -F "mode=omni" \
  -F "reference_videos=@./motion.mp4;type=video/mp4"
```

提示词中的 `@图片1`、`@图片2` 对应第1、第2张图片，`@视频1` 对应第1个视频。`@./file` 是curl文件上传语法，替换为调用方文件路径；多素材重复同名字段。若删除素材，也要删除提示词中对应的引用。不要手动设置Content-Type，curl会生成multipart boundary。

### 图片 + 视频

```bash
curl http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer sk-dr-YOUR_KEY" \
  -H "Idempotency-Key: <每个新任务唯一ID>" \
  -F "model=seedance-2.0-mini" \
  -F "prompt=让 @图片1 中的人物在 @图片2 的场景里按照 @视频1 的步伐自然行走。" \
  -F "seconds=4" \
  -F "resolution=720p" \
  -F "ratio=16:9" \
  -F "mode=omni" \
  -F "reference_images=@./subject.png;type=image/png" \
  -F "reference_images=@./scene.jpg;type=image/jpeg" \
  -F "reference_videos=@./motion.mp4;type=video/mp4"
```

提示词中的 `@图片1`、`@图片2` 对应第1、第2张图片，`@视频1` 对应第1个视频。`@./file` 是curl文件上传语法，替换为调用方文件路径；多素材重复同名字段。若删除素材，也要删除提示词中对应的引用。不要手动设置Content-Type，curl会生成multipart boundary。

### JSON 参考素材

```bash
curl http://127.0.0.1:8787/v1/videos \
  -H "Authorization: Bearer sk-dr-YOUR_KEY" \
  -H "Idempotency-Key: <每个新任务唯一ID>" \
  -H "Content-Type: application/json" \
  -d '{
  "model": "seedance-2.0-mini",
  "prompt": "让 @图片1 中的人物按照 @视频1 的步伐自然行走，保持 @图片1 的人物外观。",
  "seconds": "4",
  "resolution": "720p",
  "ratio": "16:9",
  "mode": "omni",
  "references": [
    {
      "type": "image",
      "name": "subject.png",
      "data_url": "data:image/png;base64,<图片文件的Base64编码>"
    },
    {
      "type": "video",
      "name": "motion.mp4",
      "data_url": "data:video/mp4;base64,<视频文件的Base64编码>"
    }
  ]
}'
```

将占位内容替换为完整Base64，保留 `data:…;base64,` 前缀。图片与视频分别按数组内同类型出现顺序编号，`name` 不决定编号。删除素材时同步删除提示词中的对应引用。

图片支持 PNG/JPEG/WebP，每张最多30 MiB；示例视频使用MP4，单个最多50 MiB；全部素材原始字节合计最多70 MiB。Mini最多9张图和3段视频，每段2–15秒，视频总时长最多15秒；尺寸、帧率等还受模型校验。大文件建议multipart上传，避免Base64扩张请求体。JSON不接受远程HTTP链接或本地/服务器路径；`@./file` 是curl读取并上传客户端文件内容。

Mini的2张图片＋1段视频组合已有真实生成验证；其他模型及参考组合以模型能力和实际返回为准。这些示例用于生成视频，图片编辑接口尚未接入。创建后使用原API Key查询 `GET /v1/videos/{id}`，完成后下载 `GET /v1/videos/{id}/content`。

## 多账号与服务器

### 删除账号与密钥

账号和密钥列表的操作列新增独立的删除按钮，区别于可以恢复的“停用”。确认删除后从普通列表移除，保留任务审计与历史结果；不能通过启用操作恢复。

- 删除账号：先停止其空闲执行节点，再移出调度。有未结束、待核对任务或正在登录时返回409，不能强行删除。历史任务查询所需的加密凭据和浏览器资料仍保留，不清空历史媒体。
- 删除密钥：立即撤销鉴权，客户端不能再用该密钥创建、查询或下载；已提交的任务仍由原账号处理，管理员可继续查看历史任务。
- 受限密钥引用了已删除账号时，原账号ID仍作为限制保留，不会自动变成“全部账号”。编辑界面会将缺失选项标记为“已删除或不可用”，只有明确取消勾选才改变账号范围。

### 生成前积分检查与切号

提交生成之前，执行器按当前模型、时长、分辨率和素材读取网页报价，并检查账号最新余额。任务详情展示所需积分、检查时余额和账号切换记录；未知数值显示“未确认”，不按0积分估算。

余额不足且确认尚未提交生成时，任务可改派到API Key允许且符合模型、登录、容量等条件的其他账号。每个候选账号仍重新检查自己的报价与余额，不沿用其他账号的优惠价格。已经提交、已有上游任务ID或提交结果不确定时不自动切号，避免重复扣费。没有可用候选、报价未知或余额检查失败会保留明确错误；实际费用仍以上游结算为准。

### 余额阈值与单账号并发标签页

账号编辑页可设置 `min_credit_balance`（暂停调度积分阈值，默认0）。0关闭阈值；余额已知且**严格小于**阈值时暂停该账号的新任务，等于阈值时不因阈值暂停。暂停不会更改手动启用开关，也不会取消已提交任务。刷新余额确认达标后自动解除这一暂停；未知余额显示“未确认”，启用阈值时状态显示“待确认余额”，提交前仍必须读取实时余额。

账号列表分别展示上游余额、本地保留额和扣除保留后的可分配积分。保留额用于防止多个并发任务占用同一份积分，不代表上游已经扣费；已经可能提交、结果不明确的任务不能靠释放保留额后重发来规避核对。

`max_concurrent` 在账号页标为“单账号并发标签页数”，**默认仍为1**，账号配置范围1–8。每个账号共享独立的登录环境，每个任务拥有独立标签页、领取凭据和回执记录。服务器的 `NATIVE_MAX_ACCOUNT_SLOTS` 默认4、最高8，实际槽数不会超过该上限；API Key自己的并发限制也仍然生效。

运行中修改并发配置不会强行中断已有任务，节点空闲后自动应用。列表区分“配置上限”和“运行标签页/实际槽位”，并显示服务器上限与待生效状态；将账号配置设为8但服务器上限为4时，不会宣称已经运行8个槽位。

### 有界等待与结果核对

排队、准备、提交回执及生成等待分别设置时限，整个任务还有总时限。这些配置由部署环境设置，不在账号编辑页修改：

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| `TASK_QUEUE_TIMEOUT_SECONDS` | 60 | 排队等待秒数 |
| `NATIVE_PREPARATION_TIMEOUT_SECONDS` | 180 | 原生页面与素材准备秒数 |
| `NATIVE_SUBMISSION_TIMEOUT_SECONDS` | 60 | 单次提交回执等待秒数 |
| `NATIVE_GENERATION_TIMEOUT_SECONDS` | 600 | 上游生成等待秒数 |
| `TASK_TOTAL_TIMEOUT_SECONDS` | 900 | 整个任务的总等待秒数 |

时限用于结束无边界的等待，并不承诺视频一定在时限内生成成功。已接收任务在查询接口中返回错误码与消息，不会把等待超时伪装成已成功生成。

没有提交的超时任务返回明确错误并释放可安全释放的执行资源；已经有上游任务ID的生成等待超时会返回 `generation_timeout`，同时保留原ID并在后台继续查询，晚到结果可以更新原任务。没有可信ID的提交结果仍需核对，不会自动重复生成。客户端收到 `metadata.original_task_pending=true` 或 `metadata.verification_required=true` 时应保留原任务ID，不重新创建；后台的错误提示和进度条也不会伪装成完成或无限等待。

查询返回HTTP 200只表示查到了任务。`status=failed` 搭配上述核对标记不代表上游已取消或必定不会扣费；已知ID的晚到结果仍可能转为完成。后台核对中的已知ID超时任务不继续占用需要浏览器的执行槽位，账号空闲时可正常停止节点或重新登录；删除账号仍要等所有未结束任务完成或核对结束。

单个任务失败与其他槽位分开处理；但登录失效、账号余额限制及无法排除重复扣费风险的情况仍可能阻止该账号继续接单，其他符合条件的账号继续参与调度。

99%是运行目标，不能由离线测试数量或配置值证明。后台继续展示真实任务结果、样本数、人工恢复和待核对记录，不会通过隐藏失败来宣称达到99%。

### 均衡派发规则

调度先检查账号启用状态、登录状态、模型权限、API Key账号池、余额、节点在线/就绪、模型冷却和账号并发上限，然后在可用账号中选择当前在途任务数最少的账号。图片和视频任务共同计入负载；排队、已领取和已提交等未结束任务也占用槽位。

同负载账号按上次派发顺序轮流接收任务，顺序写入数据库并跨重启保留，不依赖同一秒内的时间戳排序。初始优先级只在首次并列且无派发记录时生效，不允许高优先级账号绕过负载与轮询长期独占任务。相同幂等键的查询/重试不会推进派发顺序。

这会在满足条件的账号间尽量均衡并发，不会越过账号容量或模型权限，也不会迁移已提交任务。账号可用性、允许模型、最大并发和生成耗时不同，因此不能承诺所有账号在每一时刻拥有完全相同的任务数。

### 部署记录

当前默认使用无扩展 Native Worker。每个账号使用独立浏览器配置目录和登录环境，在后台添加账号并通过登录面板保存状态。单账号的多个执行槽位共享该登录环境，任务标签页和回执独立；账号停用只阻止新调度，不改变已提交任务的归属。

服务器配置见 `deploy/README.md`。主容器包含API、后台界面及托管Chromium，平台数据可使用PostgreSQL；领取队列仍是单控制端实例下的持久化SQLite。不要横向复制多个独立控制端来共享同一领取队列。账号凭据和浏览器配置放在数据卷中，不打包进镜像。

既有服务器部署、真实模型测试和本轮并发改造的验证范围见 `STATUS.md`、`LIVE_MODEL_TEST.md` 与 `RELIABILITY.md`。离线故障测试、原生网页预检和完整收费生成是不同的验证结果；迁移环境或增加并发后，仍需按真实运行记录评估稳定性，不能以配置上限推算99%成功率。

## 运维与验证

```powershell
python -m dreamina_api serve --host 127.0.0.1 --port 8787
python -m pytest -q
node --test chrome-extension/tests/*.test.cjs
node --test web/ui.test.cjs
```

首次管理员由 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 初始化；后续改环境变量不会覆盖已有管理员。`PLATFORM_ENCRYPTION_KEY` 必须稳定保存，否则无法解密已存账号凭据。登录、客户 API key、内部操作 key 和 Chrome 配对 key 相互独立。

旧自定义操作接口移至 `/internal/*`，仅操作员私钥可访问；客户应用应只用 `/v1/*` 标准兼容接口。原来的 token-only/headless 直接生成方案仍被上游拒绝，不应与已验证的 Chrome 原生路径混淆。

协议参考：[OpenAI Images](https://developers.openai.com/api/reference/resources/images/methods/generate)、[历史 Videos](https://developers.openai.com/api/reference/resources/videos/methods/create)。视觉参考：[Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api)。
