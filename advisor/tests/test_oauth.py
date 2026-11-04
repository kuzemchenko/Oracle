import base64
import hashlib
import re
import tempfile
import unittest
import os
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from unittest.mock import patch
from starlette.testclient import TestClient
from advisor.oauth import OwnerOAuth,RequestLimits
from advisor.server import build_server

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.origin='https://oracle.example'
        self.password='p'*48
        self.oauth=OwnerOAuth(self.origin,self.password)
        with patch.dict(os.environ,ORACLE_MCP_HOSTS='oracle.example'):
            self.server=build_server(Path(self.tmp.name)/'db',self.oauth)
        self.client=TestClient(RequestLimits(self.server.streamable_http_app()),base_url=self.origin)
        self.client.__enter__()
        self.verifier='v'*64
        self.challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.callback='https://chatgpt.com/connector_platform/oauth/callback'
    def tearDown(self):self.client.__exit__(None,None,None);self.tmp.cleanup()
    def register(self,callback=None):
        return self.client.post('/register',json={'redirect_uris':[callback or self.callback],
            'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],
            'response_types':['code'],'scope':'oracle.read','client_name':'ChatGPT'})
    def authorize(self,cid,password=None,origin=None):
        r=self.client.get('/authorize',params={'client_id':cid,'response_type':'code','redirect_uri':self.callback,
            'code_challenge':self.challenge,'code_challenge_method':'S256','scope':'oracle.read',
            'resource':self.origin+'/mcp','state':'state-123'},follow_redirects=False)
        self.assertEqual(r.status_code,302,r.text)
        page=self.client.get(r.headers['location'])
        key=re.search('name="request" value="([^"]+)"',page.text).group(1)
        csrf=re.search('name="csrf" value="([^"]+)"',page.text).group(1)
        return self.client.post('/owner',data={'request':key,'csrf':csrf,'password':password or self.password},
            headers={'Origin':origin or self.origin},follow_redirects=False)
    def token(self,cid,code,verifier=None):
        return self.client.post('/token',data={'grant_type':'authorization_code','client_id':cid,'code':code,
            'code_verifier':verifier or self.verifier,'redirect_uri':self.callback,'resource':self.origin+'/mcp'})
    def test_full_flow_rotation_replay_and_revocation(self):
        meta=self.client.get('/.well-known/oauth-authorization-server')
        self.assertEqual(meta.status_code,200,meta.text)
        self.assertEqual(meta.json()['issuer'],self.origin+'/')
        r=self.register();self.assertEqual(r.status_code,201,r.text);cid=r.json()['client_id']
        r=self.authorize(cid);self.assertEqual(r.status_code,303,r.text)
        q=parse_qs(urlsplit(r.headers['location']).query);self.assertEqual(q['state'],['state-123']);code=q['code'][0]
        bad=self.token(cid,code,'wrong'*13);self.assertEqual(bad.status_code,400)
        r=self.token(cid,code);self.assertEqual(r.status_code,200,r.text);tokens=r.json()
        self.assertEqual(self.token(cid,code).status_code,400)
        headers={'Accept':'application/json, text/event-stream','Authorization':'Bearer '+tokens['access_token']}
        body={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'scenario','arguments':{'entry':10,'exit_price':12}}}
        r=self.client.post('/mcp',json=body,headers=headers);self.assertEqual(r.status_code,200,r.text)
        self.assertIn('100.0',r.text)
        refresh={'grant_type':'refresh_token','client_id':cid,'refresh_token':tokens['refresh_token'],'resource':self.origin+'/mcp'}
        r=self.client.post('/token',data=refresh);self.assertEqual(r.status_code,200,r.text);rotated=r.json()
        self.assertEqual(self.client.post('/token',data=refresh).status_code,400)
        self.assertEqual(self.client.post('/mcp',json=body,headers=headers).status_code,401)
        r=self.client.post('/revoke',data={'client_id':cid,'client_secret':'','token':rotated['access_token'],'token_type_hint':'access_token'})
        self.assertEqual(r.status_code,200,r.text)
        headers['Authorization']='Bearer '+rotated['access_token']
        self.assertEqual(self.client.post('/mcp',json=body,headers=headers).status_code,401)
    def test_wrong_password_and_origin(self):
        cid=self.register().json()['client_id']
        self.assertEqual(self.authorize(cid,password='wrong').status_code,403)
        self.assertEqual(self.authorize(cid,origin='https://evil.example').status_code,403)
        self.assertEqual(len(self.oauth.codes),0)
    def test_reject_untrusted_redirect_and_missing_resource(self):
        self.assertEqual(self.register('https://evil.example/callback').status_code,400)
        cid=self.register().json()['client_id']
        r=self.client.get('/authorize',params={'client_id':cid,'response_type':'code','redirect_uri':self.callback,
            'code_challenge':self.challenge,'code_challenge_method':'S256','scope':'oracle.read'},follow_redirects=False)
        self.assertNotIn('/owner',r.headers.get('location',''))
        self.assertEqual(len(self.oauth.pending),0)
    def test_missing_token_and_payload_limit(self):
        self.assertEqual(self.client.post('/mcp',json={}).status_code,401)
        self.assertEqual(self.client.post('/register',content=b'x'*65537).status_code,413)
    def test_expired_code_and_cross_client(self):
        cid=self.register().json()['client_id'];cid2=self.register().json()['client_id']
        r=self.authorize(cid);code=parse_qs(urlsplit(r.headers['location']).query)['code'][0]
        self.assertEqual(self.token(cid2,code).status_code,400)
        self.oauth.codes[code].expires_at=0
        self.assertEqual(self.token(cid,code).status_code,400)
    def test_rate_limit(self):
        for i in range(120):self.client.get('/missing')
        self.assertEqual(self.client.get('/missing').status_code,429)

    def test_confidential_client_secret_is_verified(self):
        payload={'redirect_uris':[self.callback], 'token_endpoint_auth_method':'client_secret_post',
            'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':'oracle.read'}
        r=self.client.post('/register',json=payload)
        self.assertEqual(r.status_code,201,r.text)
        client=r.json();cid=client['client_id']
        r=self.authorize(cid);code=parse_qs(urlsplit(r.headers['location']).query)['code'][0]
        self.assertEqual(self.token(cid,code).status_code,401)
        r=self.client.post('/token',data={'grant_type':'authorization_code','client_id':cid,
            'client_secret':client['client_secret'],'code':code,'code_verifier':self.verifier,
            'redirect_uri':self.callback,'resource':self.origin+'/mcp'})
        self.assertEqual(r.status_code,200,r.text)
