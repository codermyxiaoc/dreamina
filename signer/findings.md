# Request signing findings

Scope: static inspection of observed public scripts plus controlled local execution of WebMSSDK in a disposable offline browser. No account requests or paid generation submissions were made by this investigation.

## Observed browser path

HAR entry 3's initiator includes Axios XHR code in `501391.7db74d049c.js`, several frames inside `webmssdk_cctbc/2.0.0.65/webmssdk.js`, an application header interceptor in `712611.cb18f8f5f7.js` and Slardar instrumentation. `initiator-summary.json` preserves the innermost frame names, source paths and zero-based line/column positions without request URLs or credentials.

At zero-based column ~75571 of `712611.cb18f8f5f7.js`, the extra XHR send wrapper invokes `_setAllHeaders`, then calls the previous send method. `_setAllHeaders` (~76419) adds appid, did, store-country-code, store-country-code-src and custom headers only when absent. This is additional request-header instrumentation, not evidence of a separate iframe signer.

The selected HAR generation header list has ordinary `sign`/`sign-ver` and no `X-Mssdk-Info`. The SDK contains the X-Mssdk-Info string, but the successful captured generation did not use that header. No literal signFrame/sign_frame/iframe was found in the inspected SDK and two wrapper sources; because the SDK is heavily obfuscated, this bounded literal search cannot establish absence of all dynamic frame behavior. The offline reproduction does not create or require a separate signing iframe.

## Initialization

`evidence/web-scripts/ui-framework-6.6a98008d46.js`, around character 125637, initializes:

```javascript
window.byted_acrawler.init({
  aid: B.a.APPID,
  isSDK: false,
  region: eE.N9.webmssdk.region,
  boe: false,
  enablePathList: ex,
  mode: 514,
  apiHost: eE.N9.webmssdk.apiHost,
  slardarConfig: eE.N9.webmssdk.slardarConfig
})
```

The enablePathList explicitly contains `/mweb/v1/aigc_draft/generate`, other generation/preprocessing endpoints and selected account/commerce endpoints; it does not list get_history_by_ids in the visible array. This fits the HAR observation that generation has X-Bogus/X-Gnarly and polling does not. The actual aid in HAR is 513641.

The initializer does not explicitly pass dfp. SDK defaults around character 116298 include `dfp:false`, `sdi:false`, mode-related state, region/apiHost and slardar defaults. The resolved `eE.N9.webmssdk` module was not available in the saved bundle subset. The offline adapter chooses the SDK's built-in `va-capcut`/`sg-capcut` presets; this is not a claim that the production page used those exact resolved settings.

Setting only `slardarConfig:{enableSlardar:false}` caused an SDK validation error: settingLocation must be 0/1/2/3/4. Supplying settingLocation=0, enableLazyload=false and an empty initConfigOverrides object shape initializes successfully offline.

## Public exports and hook behavior

The actual locally loaded SDK exposes:

```text
frontierSign, getReferer, init, isWebmssdk, report,
setTTWebid, setTTWebidV2, setTTWid, setUserMode
```

`frontierSign` is a VM-backed function (`P(48221,...)`) and the single artificial X-MS-STUB probe returned only X-Bogus. It is not a demonstrated public API that returns a full HTTP X-Gnarly signature.

By contrast, after init, an ordinary XHR to the enabled path is automatically rewritten to include **msToken, X-Bogus and X-Gnarly**. The local adapter captures the rewritten request at Playwright's routing layer and aborts it. Fetch was also observed to add these parameters in the earlier Node probe; Python integration uses XHR because that matches the HAR's path.

With no token state, msToken is empty. Supplying a synthetic msToken as Cookie, localStorage, or an existing query parameter did not initialize the SDK's internal token; the query variant could produce duplicate msToken fields. The supported adapter therefore rejects pre-signed URLs and writes a caller-supplied token into the already existing internal state field through a guarded in-memory setter at the exact source anchor `o.frontierSign=q,o.getReferer=`. The source hash and single-anchor check prevent silently applying it to a changed SDK. No signature implementation, environment-trust flag or browser fingerprint code is changed.

SDK initialization and full browser-like environment remain necessary for this approach. A pure Node VM version without DOM/WebGL/browser APIs has not been implemented; the tested solution is Python or Node controlling a dedicated offline headless Chromium.

## babi_param

The successful generation query includes a percent-encoded JSON string which remains encoded after one URL query decoding pass. Its decoded data describes `feature_entrance=to-generate`, `feature_entrance_detail=to-generate-text_to_video`, `feature_key=text_to_video`, `scenario=image_video_generation`, `edit_type=tool`, tool/sub-tool `tool_video`, tab/enter_from `tool`, empty template_id, scene_lv1=tool, scene_lv2=tool_video and extra_param `{model_id:dreamina_seedance_40_mini, generate_type:t2v}`.

Build this value once using encodeURIComponent-equivalent encoding of compact JSON, then let the outer query serializer encode percent signs. Do not decode/re-encode an already prepared query unnecessarily before signing. The adapter preserves the unsigned URL's input query and passes the exact JSON body to XHR; the caller must send the same body bytes and URL it receives.

## Evidence quality and remaining questions

The downloaded SDK matches the HAR's source URL but was fetched later. Its SHA-256 identifies the downloaded bytes; this does not establish byte identity with the earlier browser instance because the HAR does not contain that script's body/hash.

REA parsed one original SDK file successfully (62391 AST nodes), with 11 findings and no syntax failures. Application inventory was complete, but the semantic graph hit its 100000-node ceiling and retained 11255 unknowns. Obfuscated VM relationships are therefore not fully recovered. REA parent Evidence ID is `ev_067ae9a84d5a11f17313b4bd27e1a5b3e565efdd8e8ee59e6eefbc50b91d2f5b`.

The offline smoke test proves local production of fresh signature fields with unchanged body, no real account material and no forwarded HTTP requests. It does not prove server acceptance, valid msToken acquisition/refresh, or that a headless browser's fingerprint meets remote policy. A server response such as shark rejection can have additional causes beyond missing signature fields. The root agent owns any separately authorized live verification.

## Subsequent normal-page readiness observation

After the root agent reported another live risk rejection, an independently owned headless browser was allowed to load the official page using the user's explicitly authorized session cookie. All paid generation routes stayed blocked during preparation, and no generation was submitted by this investigator.

This resolved the earlier unknown production initialization values: aid 513641, mode 514, isSDK=false, region=ttp, apiHost=`https://mssdk16-normal-us-ttp2.capcutapi.us`, dfp not explicitly supplied, slardarConfig enableSlardar=true, enableLazyload=false, settingLocation=4, slardarDomain=mon.capcutapi.us, plugin prefix under `/obj/capcut-web-buz-tx2/slardar-web-sdk/plugins`. No webdriver/fingerprint mutation was applied.

The normally initialized page requested `/web/resource` and `/web/report` from that actual SDK host. Their HTTP 200 responses included x-ms-token headers; later report requests included nonempty msToken plus both request-signature fields. This is direct observation of a normal bootstrap/token refresh path. It does not establish the exact cryptographic/semantic binding of token to browser signals, but it shows why transplanting an old HAR token into an isolated SDK skips normal upstream initialization.

The new browser's normal account-info response contained user identity fields, establishing login; no user values were retained. No visible challenge text/frame was observed. Its API routing after initialization was the US TTP2 hostname, distinct from the older HAR hostname. `browser-readiness.json` retains safe config/status/endpoint evidence.

The minimum evidence-based fallback is therefore to keep the official SDK, its current bootstrap state, its current routing and the one submission in the **same** independently owned browser context. `browser_transport.py` implements that boundary. Supplying additional guessed parameters or repeatedly probing paid generation is unnecessary and has not been done here. The root agent owns the single subsequent generation test.
