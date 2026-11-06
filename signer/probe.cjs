const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

(async () => {
  const browser = await chromium.launch({executablePath:process.env.SIGNER_CHROMIUM||'C:/Users/ASUS/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',headless:true,args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run']});
  const context = await browser.newContext({offline:true,serviceWorkers:'block'});
  const intercepted=[];
  await context.route('**/*', async route => {
    const req=route.request();
    const url=new URL(req.url());
    intercepted.push({path:url.pathname,origin:url.origin,queryNames:[...url.searchParams.keys()],queryLengths:Object.fromEntries([...url.searchParams].map(([k,v])=>[k,v.length])),bodyBytes:Buffer.byteLength(req.postData()||'')});
    if(req.resourceType()==='document') return route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html><head><title>Offline SDK probe</title></head><body></body></html>'});
    return route.fulfill({status:200,contentType:'application/json',body:'{"ret":0,"data":{}}',headers:{'Access-Control-Allow-Origin':'*','Access-Control-Expose-Headers':'x-ms-token','x-ms-token':'offline-probe-token-not-a-real-credential'}});
  });
  await context.addCookies([{name:'msToken',value:'offline-probe-token-not-a-real-credential',domain:'.capcut.com',path:'/',secure:true,httpOnly:false}]);
  const page=await context.newPage();
  const errors=[];
  page.on('pageerror',err=>errors.push(String(err).slice(0,500)));
  await page.goto('https://dreamina.capcut.com/offline-signer');
  await page.evaluate(()=>localStorage.setItem('msToken','offline-probe-token-not-a-real-credential'));
  await page.addScriptTag({content:fs.readFileSync(path.join(__dirname,'sdk/webmssdk.js'),'utf8')});
  const exports=await page.evaluate(()=>({keys:Object.keys(window.byted_acrawler||{}),internalKeys:Object.keys(window._mssdk||{}),signString:window.byted_acrawler?.frontierSign?.toString()}));
  const initResult=await page.evaluate(()=>{
    try{return {ok:true,value:window.byted_acrawler.init({aid:513641,isSDK:false,region:'va-capcut',boe:false,enablePathList:['/mweb/v1/aigc_draft/generate'],mode:514,slardarConfig:{enableSlardar:false,enableLazyload:false,settingLocation:0,initConfigOverrides:{slardarDomain:'',slardarPluginPrefixPath:''}}})}}catch(e){return {ok:false,error:String(e)}}
  });
  await page.waitForTimeout(500);
  const state=await page.evaluate(()=>({cacheOpts:window._mssdk?.cacheOpts,shared:window._mssdk?._sharedCache,storageKeys:Object.keys(localStorage),globalKeys:Object.keys(window).filter(x=>/ms|crawl/i.test(x))}));
  const signed=await page.evaluate(async()=>{
    const api=window.byted_acrawler;
    let direct;
    try {direct=api.frontierSign({'X-MS-STUB':'DUMMY_OFFLINE_TEST'})} catch(e){direct={error:String(e)}}
    try {await fetch('https://dreamina-api.us.capcut.com/mweb/v1/aigc_draft/generate?aid=513641&device_platform=web&region=US&msToken=offline-probe-token-not-a-real-credential',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"offline_probe":true}'});}catch(e){return {direct,fetchError:String(e)}}
    return {direct,sharedAfter:window._mssdk?._sharedCache};
  });
  const result={exports,initResult,state,signed,intercepted,errors,networkPolicy:'Chromium context offline; all route requests locally fulfilled. No account credentials supplied.'};
  fs.writeFileSync(path.join(__dirname,'probe-result.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
  await browser.close();
})().catch(e=>{console.error(String(e));process.exitCode=1});
