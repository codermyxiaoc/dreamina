"""Real HTTP CONNECT authentication, entirely on local loopback endpoints."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
import ipaddress
import json
from pathlib import Path
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
import pytest
from playwright.async_api import async_playwright

from dreamina_api.egress import parse_proxy_url
from dreamina_api.proxy_health import probe_proxy


@asynccontextmanager
async def local_proxy(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')]);now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    certpath=tmp_path/'local-cert.pem';keypath=tmp_path/'local-key.pem'
    certpath.write_bytes(cert.public_bytes(serialization.Encoding.PEM));keypath.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    server_ssl=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_ssl.load_cert_chain(certpath,keypath)
    client_ssl=ssl.create_default_context(cafile=str(certpath));connections=[];authorized=[];rejected=[];target_requests=[];handlers=set()
    async def serve_target(reader,writer):
        connections.append(writer)
        try:
            header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5);target_requests.append(header.split(b'\r\n',1)[0].decode())
            payload=b'{"ip":"203.0.113.8"}' if b' /ip ' in header else b'<html><body>authenticated proxy reached local TLS target</body></html>'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload);await writer.drain()
        except (asyncio.TimeoutError,asyncio.IncompleteReadError,ConnectionError):pass
        finally:writer.close()
    target=await asyncio.start_server(serve_target,'127.0.0.1',0,ssl=server_ssl);target_port=target.sockets[0].getsockname()[1]
    required='Basic '+base64.b64encode(b'fixture-user:fixture-pass').decode()
    async def pipe(reader,writer):
        try:
            while chunk:=await reader.read(65536):writer.write(chunk);await writer.drain()
        except (ConnectionError,asyncio.CancelledError):pass
        finally:writer.close()
    async def serve_proxy(reader,writer):
        task=asyncio.current_task();handlers.add(task);connections.append(writer)
        try:
            raw=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5);lines=raw.decode('latin1').split('\r\n');method,address,_=lines[0].split(' ',2)
            headers={k.lower():v.strip() for line in lines[1:] if ':' in line for k,v in [line.split(':',1)]}
            if method!='CONNECT' or address not in (f'localhost:{target_port}',f'127.0.0.1:{target_port}'):
                rejected.append(address);writer.write(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');await writer.drain();return
            if headers.get('proxy-authorization')!=required:
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="local-test"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');await writer.drain();return
            authorized.append(address)
            remote_reader,remote_writer=await asyncio.open_connection('127.0.0.1',target_port);connections.append(remote_writer)
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
            await asyncio.gather(pipe(reader,remote_writer),pipe(remote_reader,writer))
        except (asyncio.TimeoutError,asyncio.IncompleteReadError,ConnectionError,ValueError):pass
        finally:writer.close();handlers.discard(task)
    proxy=await asyncio.start_server(serve_proxy,'127.0.0.1',0);port=proxy.sockets[0].getsockname()[1]
    try:yield {'url':f'http://fixture-user:fixture-pass@127.0.0.1:{port}','target':f'https://localhost:{target_port}','authorized':authorized,'rejected':rejected,'requests':target_requests,'ssl':client_ssl}
    finally:
        proxy.close();target.close();await proxy.wait_closed();await target.wait_closed()
        for writer in connections:writer.close()
        pending=list(handlers)
        for task in pending:task.cancel()
        if pending:await asyncio.gather(*pending,return_exceptions=True)


def test_httpx_proxy_probe_uses_authenticated_connect_and_remote_tls(tmp_path,monkeypatch):
    async def run():
        async with local_proxy(tmp_path) as p:
            real=httpx.AsyncClient
            def client(**kwargs):
                assert kwargs['proxy']==p['url'] and kwargs['trust_env'] is False
                kwargs['verify']=p['ssl'];return real(**kwargs)
            monkeypatch.setattr(httpx,'AsyncClient',client)
            result=await probe_proxy(parse_proxy_url(p['url']),ip_url=p['target']+'/ip',target_url=p['target']+'/target')
            assert result['ok'] and result['exit_ip']=='203.0.113.8'
            assert len(p['authorized'])==2 and len(p['requests'])==2
            assert not p['rejected']
    asyncio.run(run())


def test_browser_proxy_credentials_are_split_and_used_on_connect(tmp_path):
    async def run():
        async with local_proxy(tmp_path) as p, async_playwright() as pw:
            if not Path(pw.chromium.executable_path).exists():pytest.skip('Playwright Chromium fixture runtime not installed')
            spec=parse_proxy_url(p['url']);assert 'fixture-' not in spec.browser_proxy()['server']
            browser=await pw.chromium.launch(headless=True,proxy=spec.browser_proxy(),args=['--proxy-bypass-list=<-loopback>','--disable-background-networking'])
            try:
                context=await browser.new_context(ignore_https_errors=True)
                await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(p['target']+'/') else route.abort())
                page=await context.new_page();response=await page.goto(p['target']+'/target',wait_until='domcontentloaded',timeout=15000)
                assert response.status==200 and 'authenticated proxy' in await page.text_content('body')
                assert p['authorized'] and any('/target' in line for line in p['requests'])
            finally:await browser.close()
    asyncio.run(run())
