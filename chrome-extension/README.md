# Dreamina Chrome 本地桥接（首版）

扩展保留用户现有 Chrome 登录页，通过网页编辑器和 Generate 按钮提交。0.3.0 仅被动观察白名单生成接口的请求/响应，以确认真实任务 ID；不读取 Cookie 或请求头，不修改签名算法，不请求 debugger 权限，也不会自行登录或处理验证码。

**已接入 Mini / 4 秒 / 720p / 16:9 文生视频，以及 Seedream 5.0 Lite / 2048×2048 / 1 张文生图。** 图片已经完成 OpenAI 风格接口与 SDK 的真实验证；视频历史桥接完成过生成和下载，0.3.0 的响应确认改进需按平台验证记录核对。其他模型和素材模式未接入。

固定扩展 ID：`eicelfibiacmlhfepgoiiemfgcbdfafm`。manifest 中保存的是固定公钥，私钥未落盘。配对密钥不在扩展包中。

## 安装与配对

1. 保持本地 API 在 `127.0.0.1:8787` 运行。
2. 在项目 `.env` 设置 `CHROME_BRIDGE_EXTENSION_ID=eicelfibiacmlhfepgoiiemfgcbdfafm` 和一个随机 `CHROME_BRIDGE_KEY`。此密钥用于本地桥接，不是 Dreamina sessionid/Cookie。
3. 用户在 Chrome 的扩展管理页开启开发者模式，选择“加载已解压的扩展程序”，选取本目录。扩展安装须由用户确认执行。
4. 在已登录的 Dreamina 页面打开 AI Video 或 AI Image 输入区，并移除参考素材。扩展会选择已接入的模型、尺寸和数量；保持网页原文，不要启用 Chrome 自动翻译。
5. 保持这个 Dreamina 标签页处于当前标签，打开扩展弹窗，填写账号标识（首个账号建议 `default`）和本地配对密钥，点击“绑定当前页并启用”。
6. 在本地 API 创建一个 `/v1/chrome/jobs` 任务。后台约每 30 秒检查一次；也可在弹窗点击“检查连接”。

更新此扩展后请刷新 Dreamina 标签页，再绑定检查。不要在已有不确定任务尚未核对时卸载扩展或清空本地状态。

```json
{
  "account_id": "default",
  "request": {
    "model": "seedance-2.0-mini",
    "prompt": "清晨湖面的水彩动画，镜头缓慢平移，完全静音，无对白。",
    "duration": 4,
    "resolution": "720p",
    "ratio": "16:9",
    "mode": "omni"
  }
}
```

网页必须保持打开。启用桥接意味着允许本地队列的任务通过该账号生成并消耗积分。不要在同一时间手动编辑或提交绑定页面。

执行节点应保持该标签页为对应 Chrome 配置的活动页，避免浏览器休眠或被内存节省策略丢弃。代码不依赖后台可能暂停的 animation frame，但新服务器桌面环境仍需要真实验证。

## 结果与故障边界

- 点击前将本次提示词、模型和参数绑定到随机 nonce。仅观察白名单 `/mweb/v1/aigc_draft/generate` 的匹配请求，不监听其他业务请求。
- 只有响应 `ret=0` 且真实请求/响应任务标识一致才报告 `submitted`；上游明确拒绝报告 failed，其余情况报告 uncertain。DOM 临时卡片不再作为提交确认依据。
- 后端继续通过同账号、实际官方 API 源查询并核对参数、时间和结果，才会标记 completed。观察器不会发送或重放请求，不传输完整 URL、Cookie、请求头或原始请求体。
- 每次领取先持久化任务锁；点击前再次持久化点击意图。页面重载、浏览器关闭、worker 丢失上下文或超时后，都不会重派或再次点击。
- 若提交结果不确定，扩展和后端暂停该账号队列；需先查询原任务并核对网页，不能直接重复生成。
- 不确定任务保留原领取凭据，允许页面晚到的任务 ID 补报；不会自动解除锁或重跑。
- 暂停按钮只停止领取新任务，不能取消已经点击提交的请求。
- 此版只绑定一个标签页/账号。多账号应使用独立 Chrome 配置环境，不能用普通同配置标签页隔离 Cookie；自动账号池尚未实现。
- DOM 脚本插入和 `.click()` 已完成一次真实提交验证。页面结构仍可能变化；找不到唯一控件时停止，不猜测点击位置。

## 权限与本地状态

- `storage`：扩展可信上下文保存配对密钥、绑定账号/标签、任务锁和待回报结果；content script 无权读取该存储区。
- `alarms`：后台 worker 休眠后约每 30 秒唤醒检查；不依赖易丢失的后台 setInterval。
- `scripting` / `activeTab`：只向绑定的 Dreamina 页面注入 isolated-world DOM 脚本。
- host permissions 仅 `https://dreamina.capcut.com/*` 与 `http://127.0.0.1:8787/*`。
- 所有本地 API 请求由后台发出，携带 `X-Chrome-Bridge-Key`。密钥不传给网页、content script 或日志。
- 没有 `externally_connectable` 或远程代码下载。MAIN world 仅运行本包的被动响应观察器；保留 fetch 原 Promise、原 Response 和 XHR 原行为，不改变上游调用。

本地验证命令：

```powershell
node --test chrome-extension/tests/logic.test.cjs
node --check chrome-extension/background.js
node --check chrome-extension/content.js
```

Chrome API 依据：[scripting](https://developer.chrome.com/docs/extensions/reference/api/scripting)、[alarms](https://developer.chrome.com/docs/extensions/reference/api/alarms)、[worker 生命周期](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)。
