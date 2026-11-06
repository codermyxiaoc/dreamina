# Fetch 与 XHR 的不发送生成对照

本次使用当前本地配置的完整 Cookie，在独立临时 headless Chromium 中正常加载官方页面。随后用同一上下文、同一个 URL/body/header 输入各构造一次 fetch 与 XHR 请求。请求经过官方 SDK 改写后，在 Playwright route 中捕获并立即 abort。生成路径的代码没有调用 continue/fetch；**实际转发生成 POST 为 0，拦截数为 2**。浏览器已关闭，没有改变生产默认 fetch 分发。

基准是用户原始 HAR 的零基 entry 3；探针输入是本地 `outputs/cookie-test-request.json`。输出只保存参数名、长度、计数和相等性，不保存 Cookie/token/签名/完整 URL/设备标识值。

## 结果

| 对比项 | fetch | XHR |
|---|---|---|
| SDK 已初始化、登录已观察到 | 是 | 同一上下文 |
| msToken 非空 | 156 字符 | 156 字符 |
| X-Bogus 非空 | 28 字符 | 28 字符 |
| X-Gnarly 非空 | 332 字符 | 332 字符 |
| 重复 query 名 | 无 | 无 |
| did 与 HAR 相同 | 是 | 是 |
| MD5 Sign 与当前头字段吻合 | 是 | 是 |
| Cookie/sessionid 存在 | 是，29 项 | 是，29 项 |
| body 与调用输入相同 | 是，4218 字节 | 是，4218 字节 |

fetch 和 XHR 的参数名集合相同，非签名参数值相同，请求头名及头值完全相同，body 完全相同。签名值本身具有时间/随机状态，未作为必须相同的断言，也没有输出其值。

相对 HAR：query 参数名没有缺失或新增；非签名 query 只有 generate_id 不同。babi_param 值已与 HAR 相等。完整 Cookie 下本次页面实际选择的 API host 与 HAR 相同，说明之前仅 sessionid 环境的 TTP2 路由差异不能被普遍化为该账户固定路由。

HAR 与探针的共有头值差异是 Accept、Accept-Language、Device-Time、sec-ch-ua、Sign、User-Agent。后四项中的时间/签名/浏览器版本属于预期环境差异；此处实际浏览器仍为 HeadlessChrome，webdriver 未修改。没有证据证明这些差异中的任何一个是 ret=-6 的单一原因。

HAR 比 route 阶段多出 Accept-Encoding、Content-Length、sec-fetch-*、Priority、Cache-Control、Pragma 等头。route 捕获发生在请求转发之前，部分浏览器传输头可能尚未展示；另外 HAR 可能受 DevTools 缓存设置影响。因此这些缺失只能记作观察差异，不能据此推出服务端缺少必要头，也不应盲目伪造浏览器管理的头。

fetch 的第三方包装层在被 abort 后 Promise 仍正常结束，而 XHR 触发 onerror。此行为不改变 route 已拦截的事实，不代表 fetch 请求被发送或被服务端接受。

结论：**没有发现 fetch 相对 XHR 缺失签名、Cookie、did、参数或 body 的实质差异，不能用切换 XHR 解释或承诺解决当前 ret=-6。** 本次没有测试服务端接受性，也没有新增生成成功证据。保留当前生成受风控拒绝的阻塞结论，停止扩大猜参和提交实验。

详细脱敏数据：`fetch-xhr-dry-comparison.json`。复现工具：`python -m signer.dry_compare`；它会正常联网加载页面，但始终拦截生成 POST。
