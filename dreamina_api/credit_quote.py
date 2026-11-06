"""Exact credits displayed for the configured native Generate action.

This is an observed account-specific page quote, never a model price table or
an upstream reservation. Abbreviated/ambiguous numbers are not estimates.
"""
from decimal import Decimal,InvalidOperation
import hashlib
import json
import re


_EXACT=re.compile(r'(0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.([0-9]{1,6}))?(\s*/\s*image)?',re.I)


def parse_exact_credit_price(value,*,kind='video',count=1):
    if not isinstance(value,str) or len(value)>80:
        raise ValueError('Native credit price is missing or invalid.')
    match=_EXACT.fullmatch(value.strip())
    if not match:
        raise ValueError('Native credit price must be an exact nonnegative number, not an abbreviation or estimate.')
    if kind not in ('image','video') or type(count) is not int or count!=1:
        raise ValueError('The current quote supports exactly one image/video output.')
    if match[3] and kind!='image':
        raise ValueError('A per-image quote cannot approve a video request.')
    try:number=Decimal(match[1].replace(',','')+('.'+match[2] if match[2] else ''))
    except InvalidOperation as error:raise ValueError('Native credit price is invalid.') from error
    if not number.is_finite() or number<0 or number>1_000_000_000:
        raise ValueError('Native credit price is outside the supported range.')
    return int(number) if number==number.to_integral_value() else float(number)


def quote_request_fingerprint(spec):
    return hashlib.sha256(json.dumps(spec,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


# Observed 2026-10-26: a semantic CSS-module prefix identifies the price next
# to the exact Generate button; hashed class suffixes are deliberately ignored.
READ_NATIVE_CREDIT_PRICE=r'''region=>{
  const prefix=(e,p)=>e&&[...e.classList].some(c=>c===p||c.startsWith(p+'-'));
  const visible=e=>{
    if(!e||!e.getClientRects().length)return false;
    for(let n=e;n;n=n.parentElement){
      const s=getComputedStyle(n);
      if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'||Number(s.opacity)===0||n.getAttribute('aria-hidden')==='true')return false;
      if(n===region)break;
    }
    return true;
  };
  if(!visible(region))return {available:false,reason:'generation_region_hidden'};
  const buttons=[...region.querySelectorAll('button[aria-label="Generate"][data-testid="dreamina.generator.submit"]')].filter(visible);
  if(buttons.length!==1)return {available:false,reason:'ambiguous_generate_button'};
  const button=buttons[0];
  if(button.getAttribute('aria-busy')==='true')return {available:false,reason:'generation_button_busy'};
  let toolbar=button.parentElement;
  while(toolbar&&toolbar!==region&&!prefix(toolbar,'toolbar-actions'))toolbar=toolbar.parentElement;
  if(!toolbar||toolbar===region)return {available:false,reason:'price_toolbar_missing'};
  const displays=[...toolbar.querySelectorAll('[class]')].filter(e=>prefix(e,'credit-price-display')&&visible(e));
  if(displays.length!==1)return {available:false,reason:'ambiguous_price_display'};
  const display=displays[0];
  if(display.getAttribute('aria-busy')==='true'||[...display.querySelectorAll('[role="progressbar"],[aria-busy="true"]')].some(visible))return {available:false,reason:'price_loading'};
  const prices=[...display.querySelectorAll('[class]')].filter(e=>prefix(e,'actual-credits')&&visible(e));
  if(prices.length!==1)return {available:false,reason:'ambiguous_current_price'};
  const current=prices[0];
  for(let e=current;e&&e!==toolbar;e=e.parentElement){
    if(prefix(e,'original-credits')||e.tagName==='DEL'||e.tagName==='S'||getComputedStyle(e).textDecorationLine.includes('line-through'))return {available:false,reason:'current_price_struck_out'};
  }
  return {available:true,text:current.textContent.trim()};
}'''
