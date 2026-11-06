# 方案 1：真实 Chrome 会话桥接

当前客户入口已升级为管理平台的 `/v1/images/generations` 与 `/v1/videos`，使用后台签发的 API key，见 `PLATFORM.md`。下列 `/v1/chrome/*` 是内部队列协议，管理操作需要操作员私钥；扩展 heartbeat/claim/report 使用独立配对 key。

本地服务把请求加入队列，已安装的扩展在用户原有 Dreamina 登录页填写提示词、点击原生 Generate 按钮，并报告新卡片上的 submit_id。服务端随后使用已验证的 HTTP 接口查询、核对任务参数、下载结果。

**已实测完成一项本地 API 队列任务并下载 MP4。** 本轮测试没有通过聊天工具手动点击该任务的 Generate 按钮；点击由已配对的扩展执行。结果文件为 `outputs/chrome-api-verified.mp4`。

扩展不读取 Cookie，不修改签名或浏览器指纹。网页登录由原 Chrome 会话持有；服务端查询使用本地 `.env` 中的同一账号凭据。

## 当前首版范围

仅 `default` 账号，**Seedance 2.0 Mini / 4秒 / 720p / 16:9 / Omni 无素材文生**。网页必须事先选择这些参数；不符合时停止，不会替换成别的模型。四模型配置接口仍表示已收集的能力配置，不表示 Chrome 桥接已支持所有模型。多账号、其他模型、图生与参考视频尚未接入此桥接。

安装及配对见 `chrome-extension/README.md`。本地配对文件为 `outputs/chrome-pairing.txt`。保留已登录标签页及 API 服务运行，不要在任务提交时同时编辑该输入区域。

## API 调用

```powershell
$body = Get-Content -Raw -LiteralPath ./examples/chrome-job.json
$job = Invoke-RestMethod http://127.0.0.1:8787/v1/chrome/jobs -Method Post -ContentType application/json -Body $body
$job.id
Invoke-RestMethod "http://127.0.0.1:8787/v1/chrome/jobs/$($job.id)/sync" -Method Post
```

也可运行完整示例：

```powershell
python examples/chrome_client.py examples/chrome-job.json --output outputs/my-video.mp4
```

这个命令会真实生成并消耗账户积分。脚本在首次提交后输出本地 job_id；中断后用 `--resume <job_id>` 恢复查询，避免重复生成。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/v1/chrome/accounts` | 扩展在线/就绪状态、首版能力 |
| POST | `/v1/chrome/jobs` | 创建队列任务，返回本地 job_id |
| GET | `/v1/chrome/jobs/{job_id}` | 读取本地状态，不主动查询上游 |
| POST | `/v1/chrome/jobs/{job_id}/sync` | 查询上游，核对请求与创建时间，再更新状态 |
| GET | `/v1/chrome/jobs/{job_id}/content` | 成功后下载 MP4 |

`job_id` 是本地队列 ID，`submit_id` 是网页创建的上游任务 ID；不要混用。扩展配对 key 用于 heartbeat/claim/report，不是外部 API 的 `LOCAL_API_KEY` 或 Dreamina sessionid。

## 状态与重复提交

`queued → claimed → submitted → completed / failed`。出现网络中断、标签页重载或无法确认点击结果时进入 `uncertain`，账号队列暂停；不会自动再次点击。`submitted` 只说明观察到新卡片，必须经上游查询返回成功并有视频地址，才能标记 completed。

任务参数、submit_id 和创建时间会与原队列请求核对，以避免把虚拟滚动中重新出现的旧卡片误认作新任务。

本轮浏览器工具测试：原生创建了两项任务，一项在输出音频审核阶段失败（2052），另一项完成并经 API 下载。随后扩展端到端队列任务返回上游状态 50，并由本地下载端点取回视频；最终状态见 `evidence/verification.json`。扩展依旧不能保证所有提示词都通过平台内容审核。
