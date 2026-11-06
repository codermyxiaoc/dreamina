/**
 * Dreamina / Infinite Canvas：创建视频 → 轮询原任务 → 鉴权下载并返回 { blob }。
 * @param {string} prompt 已含系统提示词；保留 @图片1、@图片2、@视频1，不重复拼接 systemPrompt。
 * @param {string[]} images PNG/JPEG/WebP 的 Base64 dataURL；按顺序对应 @图片N。
 * @param {File[]} videos MP4/MOV 参考视频；按顺序对应 @视频N。
 * @param {File[]} audios 当前不支持参考音频，必须为空。
 * @param {object} params
 * @param {string} params.mode reference/omni；有参考素材时不支持 frames 首尾帧。
 * @param {number|string} params.seconds Mini/Fast/2.0为4–15秒，2.5为4–30秒；默认4秒。
 * @param {string} params.size 像素尺寸或比例，例如1280x720、16:9；auto采用默认值。
 * @param {string} params.resolution 720p或1080p；2.5另支持480p；默认720p，需与像素尺寸一致。
 * @param {string} params.ratio 21:9、16:9、4:3、1:1、3:4、9:16，默认16:9。
 * params.generateAudio、params.watermark 当前无法控制，不传给API，使用上游默认行为。
 * @param {string} model seedance-2.0-mini / seedance-2.0-fast / seedance-2.0 / seedance-2.5。
 * @param {string} baseUrl 渠道地址，如http://38.46.222.84:8989；末尾已有/v1也兼容。
 * @param {string} apiKey 本平台的API Key，不是管理员密码或Cookie。
 * @param {function} request Infinite Canvas注入的请求函数。
 * @param {function} poll Infinite Canvas注入的轮询函数。
 * @param {AbortSignal} signal 本地取消信号；取消等待不代表上游取消生成。
 * @returns {Promise<{blob: Blob}>}
 * 同一份脚本用于4个模型。范围依据当前网关配置，不代表所有组合都已实测。
 * Mini/Fast/2.0最多9图+3视频，视频总长<=15秒；2.5最多30图+10视频，总长<=30.2秒。
 * 图片<=30MiB/张，视频<=50MiB/段，素材合计<=70MiB；实际媒体参数由服务端校验。
 * 当前不支持首尾帧、参考音频、2K/4K；音频生成和水印开关使用上游默认值。
 * 后台重试会沿用原video_id，本脚本只查询原任务，不重新创建。
 * 每次执行脚本会创建一个新任务；不要靠重跑整段脚本恢复超时任务。
 */
async function generateDreaminaVideo({
  prompt,
  images = [],
  videos = [],
  audios = [],
  params: { mode, seconds, size, resolution, ratio } = {},
  model,
  baseUrl,
  apiKey,
  request,
  poll,
  signal,
}) {
  const checkAbort = () => {
    if (signal?.aborted) throw new Error("已取消本地等待；已提交的上游任务不会自动取消。");
  };
  const unwrap = (value) => value && typeof value === "object" && "data" in value && !("id" in value) ? value.data : value;
  const bodyOfError = (error) => unwrap(error?.response?.data ?? error?.response ?? error);
  const messageOf = (value, fallback) => value?.error?.message || value?.message || fallback;
  const normalize = (value) => String(value ?? "").trim().toLowerCase();
  const models = {
    "seedance-2.0-mini": { seconds: 15, resolutions: [720, 1080], images: 9, videos: 3, prompt: 4000 },
    "seedance-2.0-fast": { seconds: 15, resolutions: [720, 1080], images: 9, videos: 3, prompt: 4000 },
    "seedance-2.0": { seconds: 15, resolutions: [720, 1080], images: 9, videos: 3, prompt: 4000 },
    "seedance-2.5": { seconds: 30, resolutions: [480, 720, 1080], images: 30, videos: 10, prompt: 15000 },
  };
  checkAbort();
  if (typeof request !== "function" || typeof poll !== "function") throw new Error("请在提供request和poll的Infinite Canvas脚本环境中运行。");
  if (typeof prompt !== "string" || !prompt.trim()) throw new Error("请填写提示词。");
  if (!Array.isArray(images) || !Array.isArray(videos) || !Array.isArray(audios)) throw new Error("参考素材必须是数组。");
  if (audios.length) throw new Error("当前接口不支持参考音频，请移除音频素材。");
  const selectedModel = normalize(model) || "seedance-2.0-mini";
  if (!Object.prototype.hasOwnProperty.call(models, selectedModel)) throw new Error("模型必须是seedance-2.0-mini、seedance-2.0-fast、seedance-2.0或seedance-2.5。");
  const limits = models[selectedModel];
  if (prompt.trim().length > limits.prompt) throw new Error(`${selectedModel}的提示词最多${limits.prompt}个UTF-16字符。`);
  if (images.length > limits.images || videos.length > limits.videos) throw new Error(`${selectedModel}最多支持${limits.images}张参考图、${limits.videos}段参考视频。`);
  const selectedMode = images.length > 2 ? "reference" : normalize(mode);
  if (selectedMode === "frames" && (images.length || videos.length)) throw new Error("当前接口不支持首尾帧，请切换为全能参考/reference模式。");
  if (selectedMode && !["reference", "omni", "frames"].includes(selectedMode)) throw new Error("不支持该生成模式，请使用reference全能参考。");
  const durationText = seconds == null || seconds === "" ? "4" : String(seconds).trim();
  const duration = Number(durationText);
  if (!/^\d+$/.test(durationText) || !Number.isInteger(duration) || duration < 4 || duration > limits.seconds) throw new Error(`${selectedModel}的视频时长必须是4–${limits.seconds}秒的整数。`);
  const ratios = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"];
  const rawSize = normalize(size).replace(/×/g, "x");
  let selectedRatio = normalize(ratio);
  let selectedResolution = normalize(resolution);
  if (selectedRatio === "auto") selectedRatio = "";
  if (selectedResolution === "auto") selectedResolution = "";
  if (rawSize && rawSize !== "auto") {
    let fromSize;
    if (ratios.includes(rawSize)) {
      fromSize = { ratio: rawSize };
    } else {
      for (const tier of limits.resolutions) {
        for (const aspect of ratios) {
          const [a, b] = aspect.split(":").map(Number);
          const width = a >= b ? Math.round(tier * a / b) : tier;
          const height = a >= b ? tier : Math.round(tier * b / a);
          if (rawSize === `${width}x${height}`) fromSize = { ratio: aspect, resolution: `${tier}p` };
        }
      }
    }
    if (!fromSize) throw new Error(`size不是${selectedModel}支持的${limits.resolutions.map((v) => v + "p").join("/")}尺寸或比例。`);
    if (selectedRatio && selectedRatio !== fromSize.ratio) throw new Error("size与ratio冲突，请保持一致。");
    if (fromSize.resolution && selectedResolution && selectedResolution !== fromSize.resolution) throw new Error("size与resolution冲突，请保持一致。");
    selectedRatio = fromSize.ratio;
    selectedResolution ||= fromSize.resolution;
  }
  selectedRatio ||= "16:9";
  selectedResolution ||= "720p";
  if (!ratios.includes(selectedRatio) || !limits.resolutions.some((v) => selectedResolution === `${v}p`)) throw new Error(`${selectedModel}不支持该比例或分辨率。`);
  const root = new URL(String(baseUrl || "").trim());
  if (!["http:", "https:"].includes(root.protocol) || root.username || root.password || root.search || root.hash) throw new Error("baseUrl应为不带查询参数和凭据的HTTP(S)接口地址。");
  const endpoint = root.origin + root.pathname.replace(/\/+$/, "").replace(/\/v1$/i, "") + "/v1/videos";
  if (typeof apiKey !== "string" || !apiKey.trim()) throw new Error("请配置本平台API Key。");
  let totalBytes = 0;
  const references = images.map((dataUrl) => {
    const match = typeof dataUrl === "string" && /^data:image\/(png|jpe?g|webp);base64,([A-Za-z0-9+/]+={0,2})$/i.exec(dataUrl);
    if (!match) throw new Error("参考图必须是PNG/JPEG/WebP的Base64 dataURL。");
    const encoded = match[2];
    const bytes = Math.floor(encoded.length * 3 / 4) - (encoded.endsWith("==") ? 2 : encoded.endsWith("=") ? 1 : 0);
    if (bytes > 30 * 1024 * 1024) throw new Error("单张参考图片不能超过30 MiB。");
    totalBytes += bytes;
    const format = match[1].toLowerCase() === "jpg" ? "jpeg" : match[1].toLowerCase();
    return { encoded, format };
  });
  for (const video of videos) {
    if (!(video instanceof Blob) || video.size === 0 || video.size > 50 * 1024 * 1024) throw new Error("参考视频必须是非空File，单个不超过50 MiB。");
    totalBytes += video.size;
  }
  if (totalBytes > 70 * 1024 * 1024) throw new Error("参考素材总大小不能超过70 MiB。");
  const form = new FormData();
  form.set("model", selectedModel);
  form.set("prompt", prompt);
  form.set("seconds", String(duration));
  form.set("resolution", selectedResolution);
  form.set("ratio", selectedRatio);
  form.set("mode", "omni");
  // 图片走文件字段，避免references文本字段的200000字节上限，也避免Base64膨胀。
  references.forEach(({ encoded, format }, index) => {
    checkAbort();
    const parts = [];
    try {
      for (let offset = 0; offset < encoded.length; offset += 32768) {
        const binary = atob(encoded.slice(offset, offset + 32768));
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        parts.push(bytes);
      }
    } catch (_) { throw new Error(`第${index + 1}张参考图的Base64编码无效。`); }
    form.append("reference_images", new Blob(parts, { type: `image/${format}` }), `image-${index + 1}.${format}`);
  });
  videos.forEach((file, index) => form.append("reference_videos", file, file.name || `reference-${index + 1}.mp4`));
  if (!globalThis.crypto?.getRandomValues) throw new Error("运行环境缺少安全随机数功能，无法创建幂等键。");
  const random = globalThis.crypto?.randomUUID?.() || Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (n) => n.toString(16).padStart(2, "0")).join("");
  const idempotencyKey = "canvas-" + random;
  const headers = { Authorization: `Bearer ${apiKey.trim()}` };
  let taskId;
  try {
    let created;
    try {
      created = unwrap(await request({ method: "post", url: endpoint, headers: { ...headers, "Idempotency-Key": idempotencyKey }, data: form, signal }));
    } catch (error) {
      const details = bodyOfError(error);
      taskId = details?.task_id || details?.error?.task_id;
      if (!taskId) throw error;
    }
    taskId ||= created?.id || created?.task_id || created?.error?.task_id;
    if (!taskId) throw new Error(messageOf(created, "创建接口没有返回任务ID。"));
    if (!/^video_[a-f0-9]{32}$/.test(taskId)) throw new Error("创建接口返回了无效任务ID。");
    const taskUrl = endpoint + "/" + encodeURIComponent(taskId);
    let failures = 0;
    const completed = await poll(async () => {
      checkAbort();
      let state;
      try {
        state = unwrap(await request({ method: "get", url: taskUrl, headers, signal }));
        failures = 0;
      } catch (error) {
        checkAbort();
        const status = Number(error?.response?.status || error?.status || 0);
        if ((!status || status === 408 || status === 429 || status >= 500) && ++failures <= 5) return null;
        throw error;
      }
      if (state?.id !== taskId || state?.model !== selectedModel) throw new Error("查询返回的任务身份不匹配。");
      if (state.metadata?.verification_required) throw new Error(`${messageOf(state, "提交结果待核对。")} 请到后台检查原任务，不要重新生成。`);
      if (["failed", "cancelled"].includes(state.status)) throw new Error(messageOf(state, "视频生成失败。"));
      if (state?.error) throw new Error(messageOf(state, "视频生成失败。"));
      if (state.status === "completed") return state;
      if (!["queued", "in_progress"].includes(state.status)) throw new Error("查询返回了未知任务状态。");
      return null;
    }, (state) => state, { intervalMs: 3000, timeoutMs: 30 * 60 * 1000 });
    if (completed?.status !== "completed") throw new Error("等待视频完成超时。");
    checkAbort();
    const blob = unwrap(await request({ method: "get", url: taskUrl + "/content", headers, responseType: "blob", signal }));
    if (!(blob instanceof Blob) || blob.size === 0) throw new Error("视频下载未返回有效Blob；可从后台下载原任务，勿重新生成。");
    if (blob.type && !/^(video\/mp4|application\/octet-stream)(;|$)/i.test(blob.type)) throw new Error("下载结果不是MP4视频。");
    return { blob };
  } catch (error) {
    const details = bodyOfError(error);
    const message = signal?.aborted ? "已取消本地等待，上游任务可能仍在执行。" : messageOf(details, "请求失败。");
    const recovery = taskId ? `任务ID：${taskId}。请查询或下载原任务，不要重跑脚本创建新任务。` : `本次幂等键：${idempotencyKey}。创建结果可能未知，请先核对后台任务记录。`;
    throw new Error(`${message} ${recovery}`);
  }
}

return await generateDreaminaVideo({
  prompt,
  images,
  videos,
  audios,
  params,
  model,
  baseUrl,
  apiKey,
  request,
  poll,
  signal,
});
