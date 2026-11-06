"""Native current-price evidence and pre-activation credit approval."""
import asyncio
from copy import deepcopy

import pytest

from dreamina_api.browser_driver import NativeUI,UIUnavailable,CreditQuoteUnavailable
from dreamina_api.credit_quote import parse_exact_credit_price,quote_request_fingerprint,READ_NATIVE_CREDIT_PRICE
from test_native_promotions import fixture,document,SPEC


def quoted_document(price='20',original='48',*,extra='',disabled=False):
    html=document()
    old='<button id="generate" aria-label="Generate">Generate</button>'
    new='<div class="toolbar-actions-observed"><span class="credit-price-display-observed"><span class="actual-credits-observed" id="actual-price">'+price+'</span>'
    if original is not None:new+='<span class="original-credits-observed" style="text-decoration:line-through">'+original+'</span>'
    new+=extra+'</span><button id="generate" aria-label="Generate" data-testid="dreamina.generator.submit" aria-busy="false" '+('disabled' if disabled else '')+'>Generate</button></div>'
    return html.replace('<body>','<body><aside>Balance 1.8K 999</aside><section role="region" aria-label="Generate">',1).replace(old,new).replace('</body>','</section></body>')


@pytest.mark.parametrize('value,expected',[('0',0),('20',20),('1,234',1234),('1,234.5',1234.5),('0.25',.25),(' 68 ',68)])
def test_exact_native_numbers_preserve_free_and_discounted_cost(value,expected):
    assert parse_exact_credit_price(value)==expected


@pytest.mark.parametrize('value',['1.8K','~68','68 credits','Free','20 48','-1','-0','NaN','Infinity','1e3','1,23','01','1.0000001','',None,68,'1000000001'])
def test_unknown_or_abbreviated_price_is_never_guessed(value):
    with pytest.raises(ValueError):parse_exact_credit_price(value)


def test_per_image_quote_requires_image_and_one_output():
    assert parse_exact_credit_price('6/image',kind='image')==6
    with pytest.raises(ValueError):parse_exact_credit_price('6/image')
    with pytest.raises(ValueError):parse_exact_credit_price('6/image',kind='image',count=2)


def test_fingerprint_binds_quote_to_exact_full_spec():
    original=deepcopy(SPEC);first=quote_request_fingerprint(original)
    assert quote_request_fingerprint(dict(reversed(list(original.items()))))==first
    for key,value in [('prompt','changed'),('duration',5),('ratio','1:1'),('references',[{'kind':'image','asset_id':'different'}])]:
        assert quote_request_fingerprint(original|{key:value})!=first


@pytest.mark.parametrize('current,expected',[('20',20),('0',0),('68',68)])
def test_quote_uses_current_price_next_to_generate_not_balance_or_struck_original(current,expected):
    async def run():
        async with fixture(quoted_document(current)) as (page,requests):
            async def noop(_):pass
            driver=NativeUI(page,noop,timeout_ms=1500)
            quote=await driver.credit_quote(SPEC)
            assert quote['required_credits']==expected and quote['source']=='native_page'
            assert quote['model']==SPEC['model'] and quote['request_fingerprint']==quote_request_fingerprint(SPEC)
            assert quote['observed_at']>0 and len(quote)==5
            assert not requests and not driver.clicked
    asyncio.run(run())


@pytest.mark.parametrize('mutation',[
    "document.querySelector('#actual-price').remove()",
    "document.querySelector('#actual-price').textContent='1.8K'",
    "document.querySelector('#actual-price').style.textDecoration='line-through'",
    "document.querySelector('#actual-price').style.display='none'",
    "document.querySelector('#generate').setAttribute('aria-busy','true')",
    "document.querySelector('#generate').removeAttribute('data-testid')",
    "document.querySelector('#actual-price').after(document.querySelector('#actual-price').cloneNode(true))",
    "document.querySelector('.credit-price-display-observed').setAttribute('aria-busy','true')",
])
def test_missing_hidden_ambiguous_or_loading_price_stops_before_any_intent(mutation):
    async def run():
        async with fixture(quoted_document()) as (page,requests):
            await page.evaluate(mutation)
            async def noop(_):pass
            driver=NativeUI(page,noop,timeout_ms=1500)
            with pytest.raises(CreditQuoteUnavailable):await driver.credit_quote(SPEC,timeout_ms=300)
            assert not requests and not driver.clicked
    asyncio.run(run())


def test_loading_zero_is_not_used_and_final_quote_must_stabilize():
    async def run():
        async with fixture(quoted_document('0')) as (page,requests):
            await page.evaluate("document.querySelector('#generate').setAttribute('aria-busy','true');setTimeout(()=>{document.querySelector('#actual-price').textContent='20';document.querySelector('#generate').setAttribute('aria-busy','false')},200)")
            async def noop(_):pass
            quote=await NativeUI(page,noop,timeout_ms=1500).credit_quote(SPEC)
            assert quote['required_credits']==20 and not requests
    asyncio.run(run())


@pytest.mark.parametrize('disabled',[False,True])
def test_insufficient_callback_stops_before_click_intent_even_when_button_disabled(disabled):
    async def run():
        async with fixture(quoted_document(disabled=disabled)) as (page,requests):
            events=[];quotes=[];intents=[]
            async def telemetry(event):events.append(event)
            async def before():intents.append(True)
            async def preflight(quote):quotes.append(quote);raise UIUnavailable('Insufficient credits; no generation submitted.')
            driver=NativeUI(page,telemetry,timeout_ms=1500)
            with pytest.raises(UIUnavailable,match='Insufficient credits'):
                await driver.submit(SPEC,[],before,on_preflight=preflight)
            assert len(quotes)==1 and quotes[0]['required_credits']==20
            assert events[-1]['stage']=='credit_quote_ready'
            assert not intents and not requests and not driver.clicked
            assert await page.evaluate('window.generations||0')==0
    asyncio.run(run())


def test_credit_callback_approves_before_single_click_and_persists_no_guess():
    async def run():
        async with fixture(quoted_document()) as (page,requests):
            sequence=[]
            async def noop(_):pass
            async def preflight(quote):sequence.append(('preflight',quote['required_credits']))
            async def before():sequence.append(('intent',None))
            driver=NativeUI(page,noop,timeout_ms=1500,submission_timeout_ms=1000)
            result=await driver.submit(SPEC,[],before,on_preflight=preflight)
            assert result['status']=='submitted' and sequence==[('preflight',20),('intent',None)]
            assert len(requests)==1 and await page.evaluate('window.generations')==1
    asyncio.run(run())


def test_price_changed_while_balance_is_checked_stops_without_second_approval_or_intent():
    async def run():
        async with fixture(quoted_document()) as (page,requests):
            quotes=[];intents=[]
            async def noop(_):pass
            async def preflight(quote):
                quotes.append(quote);await page.locator('#actual-price').evaluate("e=>e.textContent='40'")
            async def before():intents.append(True)
            driver=NativeUI(page,noop,timeout_ms=1500)
            with pytest.raises(CreditQuoteUnavailable,match='changed after preflight'):
                await driver.submit(SPEC,[],before,on_preflight=preflight)
            assert len(quotes)==1 and not intents and not requests and not driver.clicked
    asyncio.run(run())


def test_per_image_markup_is_read_as_single_image_total():
    async def run():
        html=quoted_document('6/image').replace('AI Video','AI Image').replace('Dreamina Seedance 2.0','Seedream 5.0 Lite').replace('16:9 720P','1:1 2K').replace('<button id="duration">4s</button>','<button aria-label="Images to generate: 1">1 image</button>')
        spec={'kind':'image','model':'seedream-5.0-lite','prompt':'A cube','n':1,'size':'2048x2048','ratio':'1:1','resolution':'2K'}
        async with fixture(html) as (page,requests):
            async def noop(_):pass
            quote=await NativeUI(page,noop,timeout_ms=1500).credit_quote(spec)
            assert quote['required_credits']==6 and not requests
    asyncio.run(run())
