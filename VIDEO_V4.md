# 视频任务协议与独立会话（0.4）

三条对外端点保持：`POST /v1/videos`、`GET /v1/videos/{video_id}`、`GET /v1/videos/{video_id}/content`。鉴权使用平台签发的 Bearer API key。新任务使用唯一 Idempotency-Key，同一任务重试复用原值。

本轮改动代码已实现；新模型、独立会话及参考素材的真实验证状态以 `evidence/video-v4-verification.json` 为准。代码支持与线上实测分开记录，不能把参数验证通过当成生成成功。

## 四模型与参数

| 模型 | 秒数 | 当前接入原生分辨率 |
|---|---|---|
| seedance-2.5 | 4–30 | 480p、720p、1080p |
| seedance-2.0-mini | 4–15 | 720p、1080p |
| seedance-2.0-fast | 4–15 | 720p、1080p |
| seedance-2.0 | 4–15 | 720p、1080p |

比例支持当前配置的 21:9、16:9、4:3、1:1、3:4、9:16。2K/4K涉及后续超分，暂不纳入原生路径。账号与API key都必须允许请求模型，服务器会检查权限。

```json
{"model":"seedance-2.0-fast","prompt":"安静的水彩山间晨雾，无文字或对白","duration":4,"resolution":"720p","ratio":"16:9"}
```

也可沿用 `seconds="4"`、`size="1280x720"`。与 duration/resolution/ratio 同时提供时必须一致；不会静默改参数。

## 多张参考图和参考视频

推荐 multipart（文件真实上传到本平台）：

```bash
curl https://your-gateway.example/v1/videos \
  -H "Authorization: Bearer $DREAMINA_API_KEY" \
  -H "Idempotency-Key: your-unique-request-id" \
  -F model=seedance-2.0-fast \
  -F prompt='Use the reference images as the scene and the reference video for camera movement.' \
  -F seconds=4 -F resolution=720p -F ratio=16:9 \
  -F 'reference_images=@scene-a.png' \
  -F 'reference_images=@scene-b.jpeg' \
  -F 'reference_videos=@motion.mp4'
```

JSON 可用 `references=[{"type":"image","data_url":"data:image/png;base64,..."},{"type":"video","data_url":"data:video/mp4;base64,..."}]`。这些是本网关的参考素材扩展字段，不宣称是历史 OpenAI Videos 的标准字段。客户端不能传服务器本地路径，暂不抓取任意 HTTP URL。

网关会解析素材尺寸、视频时长/fps、文件类型及 SHA-256，验证模型数量/时长限制后才预留任务。图片本地上限30MiB、视频50MiB、所有素材70MiB、HTTP请求体96MiB；模型自己的更低限制同样生效。2.5配置最多30图/10视频，总50素材；2.0系列最多9图/3视频，总12素材。音频输入尚未纳入此上传流程。

素材存入私有任务目录。只有持有原领取凭据的浏览器节点可在提交阶段读取；不会将服务器文件路径或 Cookie 发给客户。扩展通过原生文件输入上传，观察上传完成后才点击一次生成按钮。

## 每个任务的新会话

扩展0.4为每项任务创建独立Dreamina标签页，并点击New session，确认空输入框和无旧任务后才配置参数。后端保存实际生成请求返回的会话/工作区ID。原有绑定标签页作为账号入口，不在其中复用对话。页面状态丢失时不重新点击，改为uncertain等待核对。

## 查询、进度与计费

HTTP200表示找到任务，即使status=failed也返回200并附error.code/error.message。例如：

```json
{"id":"video_example","object":"video","model":"seedance-2.0-fast","status":"in_progress","created_at":1750000000,"duration":4,"seconds":"4","resolution":"720p","size":"1280x720","progress":57,"progress_details":{"source":"page","observed_at":1750000030,"stage":"generating"}}
```

progress只在页面观察到真实百分比时返回，完成为100；未知时省略，后台显示不确定进度条。浏览器30秒心跳可能使百分比稍有延迟。`billing_status`、`usage`仅有明确上游证据时返回；当前没有可靠completion_tokens来源，因此不会伪造该数字或把completed当作settled。

后台任务详情新增请求参数、素材摘要、独立会话ID、上游任务ID、响应摘要和事件时间线。日志对凭据、签名URL参数和媒体base64脱敏。终态和失败记录保留，避免误认为所有联调都成功。

升级时重新加载扩展0.4并刷新绑定入口；旧协议节点不领取新任务。远程relay也需使用本版，新增受领取凭据保护的asset读取、telemetry和status转发。
