"""Browser CORS for customer Bearer routes, never administrative/internal routes."""
import json
import re
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware

PUBLIC_PATH=re.compile(r'^/v1/(?:models|videos(?:/[^/]+(?:/content)?)?|images/(?:generations|edits|jobs/[^/]+(?:/result)?))/?$')


def parse_public_origins(value=None):
    if value is None or value=='':return ['*']
    if isinstance(value,str):
        try:value=json.loads(value)
        except ValueError as error:raise ValueError('PUBLIC_API_CORS_ORIGINS must be a JSON array of origins.') from error
    if not isinstance(value,list) or len(value)>100 or any(not isinstance(origin,str) for origin in value):
        raise ValueError('PUBLIC_API_CORS_ORIGINS must be a JSON array with at most 100 origins.')
    if '*' in value:
        if value!=['*']:raise ValueError('Wildcard CORS cannot be mixed with explicit origins.')
        return ['*']
    for origin in value:
        try:
            parsed=urlsplit(origin);port=parsed.port
            valid=parsed.scheme in ('http','https') and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.path and not parsed.query and not parsed.fragment
            valid=valid and len(origin)<=2048 and not any(c.isspace() for c in origin)
        except ValueError:valid=False
        if not valid:raise ValueError('CORS origins must be exact HTTP(S) scheme://host[:port] values without paths.')
    return list(dict.fromkeys(value))


class PublicAPICORSMiddleware:
    def __init__(self,app,origins=None):
        self.app=app
        self.public=CORSMiddleware(app,allow_origins=parse_public_origins(origins),
            allow_credentials=False,allow_methods=['GET','POST','OPTIONS'],allow_headers=['*'],
            expose_headers=['X-Request-ID','Retry-After','Content-Disposition','Content-Length','Content-Range','Accept-Ranges'],
            max_age=600)

    async def __call__(self,scope,receive,send):
        target=self.public if scope['type']=='http' and PUBLIC_PATH.fullmatch(scope.get('path','')) else self.app
        await target(scope,receive,send)
