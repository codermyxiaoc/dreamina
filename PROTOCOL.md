# Dreamina 视频上游协议记录

观察日期：2026-10-25，实际区域 US。业务接口基址 `https://dreamina-api.us.capcut.com`；积分基址 `https://commerce.us.capcut.com`。路径和字段随网站更新可能变化。

## 证据范围

- 原始 HAR：`evidence/dreamina.capcut.com.har`，118 条记录；使用零基 ordinal。
- REA 网络证据：`ev_508a3a54704643e54d39ab7ee45f4f62f84da18f0611533a72dd68c1950dba81`，选取 3、16、96、98、112。
- REA 草稿静态证据：`ev_a7660f34bf62f2be9673d40020869ccbcc40ba9ea0551a0df16c8f0d0abb1bf6`。两个脚本解析成功；语义图达到上限，不能称完整执行路径。
- 完整模型配置：`evidence/video-model-config-full.json`；打包配置：`dreamina_api/data/model-config.json`。
- 真实 API 验证：`evidence/verification.json`，不得与网页 HAR 成功混淆。

## 请求鉴权

`sessionid` Cookie 用于会话鉴权。当前实现同时设置 `sessionid_ss`、`sid_tt` 及地区 Cookie；未读取 Chrome Cookie 数据库。常规签名：

```text
Sign = md5("9e2c|" + path[-7:] + "|7|8.4.0|" + unix_seconds + "||11ac")
```

已与 HAR 的签名匹配。相关头：`Appid=513641`、`Appvr=8.4.0`、`Pf=7`、`App-Sdk-Version=48.0.0`、`Device-Time`、`Sign-Ver=1`、Origin/Referer、地区头；网页还发送 `did`。

公共 query：`aid=513641`、`device_platform=web`、`region=US`、`da_version=3.3.32`、`web_version=7.5.0`、`aigc_features=app_lip_sync`。

生成还包含 `os=windows`、`web_component_open_flag=1`、`generate_id`、`commerce_with_input_video=1`、`babi_param` 和 SDK 的 `msToken/X-Bogus/X-Gnarly`。`babi_param` 是先 URL 编码、再作为 query 值编码的 JSON 业务元数据；不是可重复使用的签名。`commerce_with_input_video=1` 在无输入视频的成功 HAR 中也存在。

离线运行原 SDK 可得到动态签名，但真实生成仍被拒绝。没有证据证明只要存在这三个 query 字段就能通过服务端校验；SDK 正常初始化还会产生 resource/report/common 请求。

## 业务调用链

| 阶段 | 方法、路径 | 主要请求参数 | 证据 |
|---|---|---|---|
| 工作区关联 | POST `/mweb/v1/workspace/update` | 工作区字段 | HAR 1；界面辅助，非生成核心 |
| 素材临时凭据 | POST `/mweb/v1/get_upload_token` | `scene=2` 图片，`scene=1` 视频 | 当前脚本及 API 实测 |
| 图片上传 | ImageX ApplyImageUpload → 文件 POST → CommitImageUpload | ServiceId、FileSize、SessionKey、CRC、AWS4 | 静态及实测 |
| 视频上传 | VOD ApplyUploadInner → 文件/分片 → CommitUploadInner | SpaceName、FileSize、SessionKey、CRC、AWS4 | 静态及分片实测 |
| 参考素材预审 | POST `/mweb/v1/execute_generate_audit` | `scene=2`、model_key、material_list | 静态及实测图片/视频 Pass |
| 提交视频 | POST `/mweb/v1/aigc_draft/generate` | 见下文 | HAR 3 网页成功；重构提交尚被拒绝 |
| 查询任务 | POST `/mweb/v1/get_history_by_ids` | `{"submit_ids":["UUID"]}` | HAR 16…96、API 实测 |
| 积分读取 | POST `/commerce/v1/benefits/user_credit` | `{}` | HAR、API 实测 |
| 扣费记录 | POST `/commerce/v1/benefits/user_credit_history` | 分页 cursor、limit | HAR 98 |
| 下载 | GET 返回的 `video_url` | 无账户 Cookie | HAR 结果及 API 实测 |
| 工作区列表 | POST `/mweb/v1/workspace/list` | 分页字段 | HAR 112；界面辅助 |

统计上报、预检 OPTIONS、通知/未读请求没有纳入视频必需接口。此次 HAR 只有文生视频，不能证明所有参考素材预审、超分或长视频调用已经覆盖。

## 生成 body

```text
submit_id: 新 UUID，用于查询和本地回执
extend.root_model: model_req_key
extend.workspace_id: 所属账户工作区，整数
extend.m_video_commerce_info / m_video_commerce_info_list:
  benefit_type: 当前模型/分辨率对应配置
  amount: 当前已观察 4 秒无视频输入为 4（不是积分数）
  resource_id=generate_video, resource_id_type=str, resource_sub_type=aigc
metrics_extra: JSON 字符串
draft_content: JSON 字符串
http_common_info.aid: 513641
```

`draft_content`：`type=draft`、`version=3.3.32`、`is_from_tsn=true`、`main_component_id` 和 `component_list`。组件 `type=video_base_component`、`aigc_mode=workbench`、`generate_type=gen_video`、`process_type=1`。

参数位于：

```text
component_list[0].abilities.gen_video.text_to_video_params
  model_req_key, seed, priority=0, video_aspect_ratio
  video_gen_inputs[0]
    prompt, video_mode=2, fps=24, duration_ms, resolution
    first_frame_image?, end_frame_image?
    unified_edit_input?
```

Omni 的 `unified_edit_input` 含 `material_list`、`meta_list`。图片用 image_uri/uri，视频用 vid；meta_list 描述文本和 material_idx 引用。空素材文本生成的成功 HAR 不带 unified_edit_input。节点 id 新建，不复用其他请求的 UUID。

参考视频计费数量暂按输出秒数加输入视频秒数重建，并有 experimental 标记；需要相应模式的真实请求核对，不能把此推断当最终计费规范。

## 查询与结果

返回 `data[submit_id]`，不是强行假定 history_list。已观察 `20 → 50`。前端还声明 `10` 前审拒绝、`30` 失败、`40` 后审拒绝、`100` 删除；实现将它们作为终态。

视频地址：

```text
data[submit_id].item_list[0].video.transcoded_video.origin.video_url
```

同级 origin 包含 width/height/fps/format/size；时长优先 `video.duration_ms`。HAR 结果是 1280×720、24fps、4042ms，外层 duration=5 不宜当精确秒数。

## 上传细节

实际临时凭据返回 `upload_domain=imagex.capcutapi.us` / `vod.capcutapi.us`、`region=useast5`。当前前端 SDK 的 region 枚举映射未列出 useast5，调用方回退 `ap-singapore-1`；此行为已由两类上传成功验证。不要沿用旧参考项目的固定 us-east-1。

临时密钥只存在内存；文件主机只接收上传凭据，不接收账户 Cookie。图片成功 commit 后取得 ImageUri；视频成功 commit 后取得 Vid/VideoMeta。大视频使用经典分片模式，logical-part 和动态流模式未实现。本地 512 MiB 限制是实现限制，模型参考视频另有更小的配置限制。

具体来源、offset、签名与分片顺序见 `evidence/uploads-findings.md`。

## 正常页面初始化补充

独立后台会话正常登录后观察到 SDK `region=ttp`、`apiHost=https://mssdk16-normal-us-ttp2.capcutapi.us`，初始化的 `/web/resource`、`/web/report` 成功返回新 token。业务 API 路由为 `https://dreamina-api-us-ttp2.us.capcut.com`。此会话没有隐藏 webdriver，没有执行验证码；实际生成仍被拒绝。因此登录和 SDK 就绪不能替代成功提交证据。

## 原有 Chrome 原生提交验证

保留用户原有 Chrome 登录环境，通过原生输入框和 Generate 按钮可以创建任务。早期样本中的 `data-generated-record-key` 与 submit_id 一致，但后续联调观察到临时卡片标识不能普遍当作上游提交凭据。0.3.0 已改成被动观察匹配的原生生成请求和响应，取真实 `body.submit_id` 并核对 `ret=0`、返回身份及官方 API 源，再由 HTTP 查询确认结果。

已安装扩展将此 UI 路径接入本地队列，完成一次 Mini / 4s / 720p / 16:9 文生任务：队列返回 submitted → 上游 status=50 → MP4 下载。扩展没有重构上游生成请求，也不读取 Cookie/改写 SDK；参数和任务创建时间由服务端查询再次核验。使用入口见 CHROME_BRIDGE.md。

平台层新增 OpenAI 风格图片与视频协议，并用密钥任务隔离和幂等键统一管理。0.3.0 已分别完成图片 JPEG/base64 与视频 MP4 的真实端到端验证。观察器只处理该任务匹配的生成接口，不传送完整 URL、请求头、Cookie 或原始请求体。
