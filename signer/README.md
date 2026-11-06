# Offline SDK signer

`runtime.py` runs the observed Dreamina WebMSSDK in a **new, disposable headless Chromium**. It never attaches to the user's Chrome, reads a Chrome profile, reads `.env`/HAR, or sends an upstream generation request.

```python
from signer.runtime import sign_request

# Serialize ONCE. Send exactly this UTF-8 body after receiving signed_url.
signed_url = await sign_request(unsigned_url, exact_json_body, ms_token=optional_ms_token)
```

The function returns a URL with freshly computed `X-Bogus` and `X-Gnarly`, plus `msToken`. Authentication cookies and the separate MD5 `Sign` header remain the caller's responsibility. The input URL must target `/mweb/v1/aigc_draft/generate`, use aid 513641, and contain no existing captured X-Bogus/X-Gnarly. Body must be a JSON string.

The returned URL is sensitive request material: do not log it. The adapter emits no logs and does not persist the input/output or token. It does not generate or refresh msToken. Empty ms_token stays empty. A supplied nonempty value is installed into the SDK's existing in-memory token field through a narrowly scoped exact-version adapter; it does not alter signing code or fingerprint flags. Cookie/localStorage seeding alone did not populate that SDK field in the local probe.

## Runtime and verification

Dependencies: Python `playwright` and an installed Playwright Chromium. Set `SIGNER_CHROMIUM` to an explicit Chromium executable if needed. The module first uses Playwright's configured runtime, then an existing Windows Playwright runtime. It creates no persistent user-data directory.

Run the no-network smoke test from the project root:

```powershell
python -m signer.runtime
```

Both empty-token and synthetic-token probes have passed, producing 28-character X-Bogus and 332-character X-Gnarly values with the original 22-byte dummy body preserved. These lengths are observed outputs, not permanent schema guarantees. `offline-validation.json` records the test result without signature/token values.

The context has `offline=True`, service workers disabled, and every HTTP(S) route intercepted. Only one synthetic document is fulfilled locally; all remaining routes—including the simulated generation and SDK telemetry—are aborted. The browser is closed in `finally`, including errors. The SDK is inserted from local source bytes. No request is forwarded by the adapter. Chromium also receives background-networking/component-update/sync disabling flags.

SDK signing uses the configured browser User-Agent. `runtime.USER_AGENT` was matched to the HTTP client used during the offline experiment (Chrome 155 on Windows); keep them synchronized when using that experimental transport. Its US preset is `va-capcut`, SG is `sg-capcut`. The subsequent normal-page observation found different US production settings (`ttp` with a TTP2 apiHost), documented below. The offline adapter is retained as an analysis experiment, not the verified live transport.

## Provenance and limits

- Source: [WebMSSDK 2.0.0.65](https://sf16-web-tos-buz.capcutcdn-us.com/obj/capcut-web-buz-tx/webmssdk_cctbc/2.0.0.65/webmssdk.js), the exact URL named in the observed HAR initiator.
- Local file: `sdk/webmssdk.js`, 227458 bytes.
- SHA-256: `e180fa46bf919c17dff94e5770b1033fa32590239c958945302faf28dfe77baf`.
- `runtime.py` checks that hash before execution. The original file is unchanged; the optional token setter is inserted into an in-memory copy only.
- REA parent evidence: `ev_067ae9a84d5a11f17313b4bd27e1a5b3e565efdd8e8ee59e6eefbc50b91d2f5b`; summary in `rea-sdk-summary.json`.
- This is the site's shipped SDK, retained as analysis evidence; its redistribution license has not been established. Do not assume the SDK itself inherits the wrapper's license.

Offline signature generation is verified. Acceptance by the Dreamina server, msToken freshness/binding, fingerprint trust and regional policy are separate questions. This module does not prove that a live anti-abuse rejection is resolved and does not circumvent a displayed CAPTCHA or account restriction.

See `findings.md` for the observed initialization and request wrapper chain. `probe.cjs` is an earlier standalone Node/Playwright diagnostic; the supported integration API is the Python function above.

## Official-page browser fallback

`browser_transport.py` is a separate **online backend-browser fallback**, added after live verification of the offline signatures still received a risk rejection. This fallback is not a pure HTTP or no-browser implementation: the user can call a local API, while the server maintains a disposable headless browser for each request.

Preparation only, without generation:

```python
from signer.browser_transport import inspect_authenticated_browser
status = await inspect_authenticated_browser(session_id)
```

One deliberately authorized generation, in the same prepared context:

```python
from signer.browser_transport import request_generation
result = await request_generation(
    unsigned_url, exact_json_body, normal_headers, session_id,
    region="US", allow_submit=True,
)
# result: {submitted: True, status: 200, body: {...}, api_origin: "https://..."}
```

Omitting `allow_submit=True` performs preparation only. Preparation blocks paid-generation paths. After explicit submission authorization, the route guard permits only one POST to the generation endpoint with exactly the caller's body. There is no retry. A timeout means submission outcome is uncertain: poll the original submit_id rather than create another task.

The fallback loads the official page normally, lets its official SDK initialize, observes login from the normal account-info response, and requires SDK/token readiness. It does not hide webdriver, modify fingerprints, inject anti-detection code, solve CAPTCHA, or import the user's Chrome profile. A detected challenge stops submission. Only session cookies supplied by the caller are installed, and the context is closed afterward. Normal page initialization can perform its ordinary background operations, including credit information queries and the site's daily-credit receive request.

The isolated login/readiness probe succeeded without submitting a generation. Production SDK config observed in that context is region `ttp`, apiHost `https://mssdk16-normal-us-ttp2.capcutapi.us`, mode 514, and Slardar settingLocation 4. Both `/web/resource` and `/web/report` returned 200 with `x-ms-token`; account-info returned an identity, with identity values excluded from the report. No challenge was detected, and webdriver remained true. See `browser-readiness.json`.

The authenticated page selected `https://dreamina-api-us-ttp2.us.capcut.com` for video config/API traffic and `https://commerce-us-ttp2.us.capcut.com` for commerce. The fallback discovers this routing from its own context rather than forcing the older HAR host. It retains current appid/appvr/pf/did/routing headers only in memory, recomputes the timestamp-dependent MD5 Sign immediately before sending, and lets the official SDK add its live token/signatures. `api_origin` in the result lets the caller poll the same region. No token, Cookie, did, full signed URL or user identity is written to readiness reports.

Subsequent live fallback verification still returned `ret=-6` (`shark not pass reject`). A final no-submit fetch/XHR comparison found identical unsigned query, headers and body, with nonempty msToken/X-Bogus/X-Gnarly on both transports. It provides no evidence that switching to XHR fixes the rejection. With the complete current Cookie, the page again chose the HAR's older US hostname, reinforcing that routing should be discovered per context. See `fetch-xhr-dry-comparison.md`; generation remains unverified through the reconstructed API, and no additional generation attempts were made by that diagnostic.
