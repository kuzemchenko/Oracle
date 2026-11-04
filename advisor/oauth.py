"""Single-owner OAuth for a pilot. Restart revokes all sessions and clients.

SDK handles protocol validation, redirect matching and PKCE. The owner must
approve each authorization using a separate random local password.
"""
import hashlib
import html
import secrets
import time
from urllib.parse import urlsplit, urlencode, parse_qsl, urlunsplit
from mcp.server.auth.provider import AuthorizationCode, AccessToken, RefreshToken, TokenError, AuthorizeError, RegistrationError
from mcp.shared.auth import OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse, PlainTextResponse

SCOPE = 'oracle.read'

class OwnerOAuth:
    def __init__(self, origin, password, callback_hosts=('chatgpt.com','chat.openai.com')):
        u=urlsplit(origin)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):
            raise ValueError('Public HTTPS origin required')
        if len(password)<32:
            raise ValueError('Owner password must contain at least 32 random characters')
        self.origin=origin.rstrip('/')
        self.resource=self.origin+'/mcp'
        self.password_digest=hashlib.sha256(password.encode()).digest()
        self.callback_hosts=set(callback_hosts)
        self.clients={}; self.pending={}; self.codes={}; self.access={}; self.refresh={}

    def cleanup(self):
        now=time.time()
        for mapping in (self.pending,self.codes,self.access,self.refresh):
            for key, item in list(mapping.items()):
                expiry=item['expires_at'] if isinstance(item,dict) else item.expires_at
                if expiry is not None and expiry<now: mapping.pop(key,None)

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        if len(self.clients)>=100:
            raise RegistrationError('invalid_client_metadata','Registration capacity reached; restart service')
        for uri in client_info.redirect_uris:
            u=urlsplit(str(uri))
            if u.scheme!='https' or u.hostname not in self.callback_hosts or u.port not in (None,443) or u.username or u.password or u.fragment:
                raise RegistrationError('invalid_redirect_uri','Only configured ChatGPT callback hosts are permitted')
        if client_info.token_endpoint_auth_method != 'none':
            raise RegistrationError('invalid_client_metadata','Use public client authentication (none) with PKCE')
        self.clients[client_info.client_id]=client_info

    async def authorize(self, client, params):
        self.cleanup()
        if params.resource != self.resource:
            raise AuthorizeError('invalid_request','Resource must match this MCP endpoint')
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError('invalid_scope')
        if len(self.pending)>=200: raise AuthorizeError('temporarily_unavailable')
        key=secrets.token_urlsafe(32)
        self.pending[key]={'client_id':client.client_id,'params':params,'expires_at':time.time()+600,'csrf':secrets.token_urlsafe(32)}
        return self.origin+'/owner?request='+key

    async def owner_page(self, request):
        self.cleanup()
        key=request.query_params.get('request','')
        item=self.pending.get(key)
        if not item:return PlainTextResponse('Request expired. Reconnect from ChatGPT.',status_code=400)
        callback=html.escape(str(item['params'].redirect_uri),quote=True)
        body=f'''<!doctype html><meta charset="utf-8"><title>Oracle — разрешить доступ</title>
<h1>Подключить Oracle к ChatGPT</h1>
<p>Разрешить чтение идей, рыночных данных и новостей. Запросы используют квоты ваших подписок.</p>
<p>Возврат в ChatGPT: {callback}</p>
<form method="post" action="/owner">
<input type="hidden" name="request" value="{key}">
<input type="hidden" name="csrf" value="{item['csrf']}">
<label>Пароль подключения Oracle <input type="password" name="password" autocomplete="current-password" required></label>
<button type="submit">Разрешить чтение</button></form>
<p>Это отдельный пароль Oracle, созданный на сервере. Не вводите пароль ChatGPT или ключи поставщиков данных.</p>'''
        response=HTMLResponse(body,headers={'Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",'Referrer-Policy':'no-referrer'})
        response.set_cookie('oracle_csrf',item['csrf'],secure=True,httponly=True,samesite='lax',max_age=600,path='/owner')
        return response

    async def approve(self, request):
        self.cleanup()
        if request.headers.get('origin')!=self.origin:
            return PlainTextResponse('Invalid origin',status_code=403)
        form=await request.form()
        key=str(form.get('request','')); item=self.pending.get(key)
        if not item:return PlainTextResponse('Request expired',status_code=400)
        csrf=item['csrf']
        if not secrets.compare_digest(str(form.get('csrf','')),csrf) or not secrets.compare_digest(request.cookies.get('oracle_csrf',''),csrf):
            return PlainTextResponse('Invalid form',status_code=403)
        supplied=str(form.get('password',''))
        if not secrets.compare_digest(hashlib.sha256(supplied.encode()).digest(),self.password_digest):
            # One password attempt per authorization request.
            self.pending.pop(key,None)
            return PlainTextResponse('Invalid password. Start again from ChatGPT.',status_code=403)
        self.pending.pop(key,None)
        p=item['params']; code=secrets.token_urlsafe(32)
        self.codes[code]=AuthorizationCode(code=code,scopes=[SCOPE],expires_at=time.time()+120,
            client_id=item['client_id'],code_challenge=p.code_challenge,redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,resource=self.resource,subject='owner')
        u=urlsplit(str(p.redirect_uri)); query=parse_qsl(u.query,keep_blank_values=True)+[('code',code)]
        if p.state is not None:query.append(('state',p.state))
        response=RedirectResponse(urlunsplit((u.scheme,u.netloc,u.path,urlencode(query),'')),status_code=303,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        response.delete_cookie('oracle_csrf',path='/owner',secure=True,httponly=True,samesite='lax')
        return response

    async def load_authorization_code(self, client, code):
        self.cleanup(); item=self.codes.get(code)
        return item if item and item.client_id==client.client_id else None

    def issue(self, client_id, scopes):
        self.cleanup()
        if len(self.access)>=500 or len(self.refresh)>=500:raise TokenError('invalid_grant','Session capacity reached')
        access=secrets.token_urlsafe(32); refresh=secrets.token_urlsafe(32)
        self.access[access]=AccessToken(token=access,client_id=client_id,scopes=scopes,expires_at=int(time.time())+3600,resource=self.resource,subject='owner')
        self.refresh[refresh]=RefreshToken(token=refresh,client_id=client_id,scopes=scopes,expires_at=int(time.time())+86400*7,resource=self.resource,subject='owner')
        return OAuthToken(access_token=access,token_type='Bearer',expires_in=3600,refresh_token=refresh,scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        self.cleanup()
        item=self.codes.pop(authorization_code.code,None)
        if not item or item.client_id!=client.client_id:raise TokenError('invalid_grant')
        return self.issue(client.client_id,item.scopes)

    async def load_refresh_token(self, client, token):
        self.cleanup(); item=self.refresh.get(token)
        return item if item and item.client_id==client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        self.cleanup()
        item=self.refresh.get(refresh_token.token)
        if not item or item.client_id!=client.client_id or not set(scopes)<=set(item.scopes):raise TokenError('invalid_grant')
        self.refresh.pop(item.token)
        # Revoke old access tokens for this client when rotating.
        for key,value in list(self.access.items()):
            if value.client_id==client.client_id:self.access.pop(key,None)
        return self.issue(client.client_id,scopes)

    async def load_access_token(self, token):
        self.cleanup(); return self.access.get(token)

    async def revoke_token(self, token):
        for mapping in (self.access,self.refresh):
            for key,value in list(mapping.items()):
                if value.client_id==token.client_id:mapping.pop(key,None)

class RequestLimits:
    """Single-user pilot: bounded body and global per-minute request budget."""
    def __init__(self, app):
        self.app=app; self.window=0; self.count=0
    async def __call__(self, scope, receive, send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        now=int(time.time())//60
        if now!=self.window:self.window=now;self.count=0
        self.count+=1
        if self.count>120:
            return await PlainTextResponse('Rate limited',status_code=429)(scope,receive,send)
        messages=[]; size=0
        while True:
            message=await receive()
            if message['type']=='http.disconnect':return
            size+=len(message.get('body',b''))
            if size>65536:return await PlainTextResponse('Request too large',status_code=413)(scope,receive,send)
            messages.append(message)
            if not message.get('more_body',False):break
        async def replay():
            if messages:return messages.pop(0)
            return await receive()
        await self.app(scope,replay,send)
