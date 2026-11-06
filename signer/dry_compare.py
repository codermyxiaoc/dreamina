"""Capture and abort SDK-wrapped fetch/XHR requests; never transmit generation.

Only a normal isolated page bootstrap uses the network. All generation POSTs
are aborted in the route handler before a network continuation is possible.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from dreamina_api.client import DreaminaClient
from dreamina_api.config import Settings
from signer.browser_transport import DreaminaBrowserSession, GENERATE_PATH

ROOT = Path(__file__).resolve().parent.parent
SECRET_QUERY = {'mstoken','x-bogus','x-gnarly'}
VARYING_HEADERS = {'sign','device-time','did','tdid','cookie','user-agent','sec-ch-ua'}


def summarize_request(req_url: str, method: str, headers: dict, body: str,
                      baseline: dict, expected_body: str) -> dict:
    parts=urlsplit(req_url)
    pairs=parse_qsl(parts.query,keep_blank_values=True)
    query=dict(pairs)
    reference_query=dict(parse_qsl(urlsplit(baseline['url']).query,keep_blank_values=True))
    reference_headers={h['name'].lower():h['value'] for h in baseline['headers'] if not h['name'].startswith(':')}
    headers={k.lower():v for k,v in headers.items()}
    cookie=headers.get('cookie','')
    cookie_names=[x.partition('=')[0].strip() for x in cookie.split(';') if '=' in x]
    differences=[]
    for key in sorted(set(headers)&set(reference_headers)):
        if headers[key] != reference_headers[key]:
            differences.append({'header':key,'value_differs':True,'volatile_or_identity':key in VARYING_HEADERS})
    unsigned_names={k for k in query if k.lower() not in SECRET_QUERY}
    return {
        'method':method,'endpoint_path':parts.path,
        'api_host_matches_har':parts.netloc==urlsplit(baseline['url']).netloc,
        'query_names':[k for k,v in pairs],
        'duplicate_query_names':sorted({k for k,v in pairs if sum(1 for k2,v2 in pairs if k2==k)>1}),
        'query_names_missing_from_har':sorted(set(query)-set(reference_query)),
        'query_names_missing_from_capture':sorted(set(reference_query)-set(query)),
        'unsigned_query_values_differing_from_har':sorted(k for k in unsigned_names&set(reference_query) if query[k]!=reference_query[k]),
        'signature_fields':{k:{'present':k in query,'nonempty':bool(query.get(k)),
                               'length':len(query.get(k,''))} for k in ('msToken','X-Bogus','X-Gnarly')},
        'header_names':sorted(headers),
        'header_names_missing_from_har':sorted(set(headers)-set(reference_headers)),
        'header_names_missing_from_capture':sorted(set(reference_headers)-set(headers)),
        'headers_differing_from_har':differences,
        'did_present':bool(headers.get('did')),
        'did_matches_har':headers.get('did')==reference_headers.get('did'),
        'md5_sign_matches_headers':headers.get('sign')==hashlib.md5(
            f"9e2c|{parts.path[-7:]}|{headers.get('pf','')}|{headers.get('appvr','')}|{headers.get('device-time','')}||11ac".encode()).hexdigest(),
        'cookie_present':bool(cookie),'cookie_count':len(cookie_names),
        'sessionid_cookie_present':'sessionid' in cookie_names,
        'user_agent_is_headless':'HeadlessChrome' in headers.get('user-agent',''),
        'body_bytes':len(body.encode()),'body_matches_expected':body==expected_body,
        'no_generation_request_forwarded':True,
    }


class DrySession(DreaminaBrowserSession):
    def __init__(self,*args,baseline:dict,expected_body:str,**kwargs):
        super().__init__(*args,**kwargs)
        self.baseline=baseline
        self.expected=expected_body
        self.active_mode=None
        self.captures={}
        self._mode_event=None
        self._raw_comparison={}

    async def _route(self,route):
        req=route.request
        if req.method == 'POST' and urlsplit(req.url).path == GENERATE_PATH:
            try:
                if self.active_mode and req.post_data == self.expected:
                    headers=await req.all_headers()
                    self.captures[self.active_mode]=summarize_request(req.url,req.method,headers,
                        req.post_data or '',self.baseline,self.expected)
                    self._raw_comparison[self.active_mode]={'headers':headers,
                        'query':dict(parse_qsl(urlsplit(req.url).query,keep_blank_values=True)),
                        'body':req.post_data}
            finally:
                # No code path from a generation POST reaches super/_route or continue.
                self.blocked_generation_count += 1
                await route.abort('blockedbyclient')
                if self._mode_event: self._mode_event.set()
            return
        await super()._route(route)

    async def capture(self,mode:str,url:str,body:str,headers:dict) -> dict:
        if mode not in ('fetch','xhr'):raise ValueError('Unknown capture mode')
        self.active_mode=mode
        self._mode_event=asyncio.Event()
        result=await self.page.evaluate('''async ({mode,url,body,headers}) => {
          if(mode==='fetch') {
            try {await fetch(url,{method:'POST',credentials:'include',headers,body});return {ended:'unexpected-resolution'}}
            catch(e){return {ended:'blocked',error_name:e.name}}
          }
          return await new Promise(resolve=>{
            const x=new XMLHttpRequest();x.open('POST',url,true);x.withCredentials=true;x.timeout=7000;
            for(const [k,v] of Object.entries(headers))x.setRequestHeader(k,v);
            x.onload=()=>resolve({ended:'unexpected-load'});
            x.onerror=()=>resolve({ended:'blocked'});
            x.ontimeout=()=>resolve({ended:'timeout'});
            x.send(body);
          });
        }''',{'mode':mode,'url':url,'body':body,'headers':headers})
        await asyncio.wait_for(self._mode_event.wait(),timeout=3)
        self.active_mode=None
        return result


async def main():
    settings=Settings.load()
    prepared=json.loads((ROOT/'outputs/cookie-test-request.json').read_text(encoding='utf-8-sig'))
    har=json.loads((ROOT/'evidence/dreamina.capcut.com.har').read_text(encoding='utf-8-sig'))
    baseline=har['log']['entries'][3]['request']
    body=json.dumps(prepared['body'],ensure_ascii=False,separators=(',',':'),allow_nan=False)
    async with DreaminaClient(settings) as client:
        input_headers=client.headers(GENERATE_PATH)
    params={'aid':'513641','device_platform':'web','region':settings.region,
            'da_version':'3.3.32','web_version':'7.5.0','aigc_features':'app_lip_sync'}
    params.update(prepared['query'])
    params['region']=settings.region
    async with DrySession(settings.session_id,region=settings.region,cookie_header=settings.cookie,
                          baseline=baseline,expected_body=body) as session:
        readiness=await session.prepare()
        if not readiness['ready_for_submission']:
            raise RuntimeError('Official page is not ready; no capture attempted.')
        url=str(httpx.URL(readiness['api_origin']+GENERATE_PATH,params=params))
        forbidden={'cookie','host','origin','referer','user-agent','content-length'}
        headers={k.lower():v for k,v in input_headers.items() if k.lower() not in forbidden
                 and not k.lower().startswith(('sec-','proxy-',':'))}
        headers.update(session._request_headers)
        now=str(int(time.time()))
        headers.update({'device-time':now,'sign-ver':'1','sign':hashlib.md5(
            f"9e2c|{GENERATE_PATH[-7:]}|{headers.get('pf','7')}|{headers.get('appvr','8.4.0')}|{now}||11ac".encode()).hexdigest()})
        outcomes={}
        for mode in ('fetch','xhr'):
            outcomes[mode]=await session.capture(mode,url,body,headers)
        a=session._raw_comparison['fetch'];b=session._raw_comparison['xhr']
        comparison={
            'query_name_sets_equal':set(a['query'])==set(b['query']),
            'unsigned_query_values_equal':{k:v for k,v in a['query'].items() if k.lower() not in SECRET_QUERY}=={k:v for k,v in b['query'].items() if k.lower() not in SECRET_QUERY},
            'header_name_sets_equal':set(a['headers'])==set(b['headers']),
            'headers_with_different_values':sorted(k for k in set(a['headers'])&set(b['headers']) if a['headers'][k]!=b['headers'][k]),
            'body_equal':a['body']==b['body'],
            'signature_values_expected_to_vary':True,
        }
        session._raw_comparison.clear()
        report={'evidence':'normal isolated official page + route-aborted generation captures',
          'baseline':'HAR zero-based entry 3','used_complete_cookie_header':bool(settings.cookie),
          'login_observed':readiness['logged_in_observed'],'sdk_ready':readiness['page']['sdk_initialized'],
          'challenge_detected':readiness['challenge_detected'],
          'captures':session.captures,'transport_outcomes':outcomes,'fetch_vs_xhr':comparison,
          'generation_posts_forwarded':session._submission_count,
          'generation_posts_aborted':session.blocked_generation_count,
          'conclusion':'Transport signature presence can be compared; no upstream acceptance was tested.'}
    output=ROOT/'signer/fetch-xhr-dry-comparison.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':asyncio.run(main())
