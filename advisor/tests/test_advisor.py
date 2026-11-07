import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from starlette.testclient import TestClient
from advisor.core import Store, Tools
from advisor.server import BearerAuth, build_server

class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'evidence.db')
        self.http = Mock()
        self.tools = Tools(self.store, self.root, self.http)
    def tearDown(self):
        self.tmp.cleanup()
    def test_return_calculation(self):
        r=self.tools.scenario(10,12,1,.5,500)
        self.assertEqual(r['return_pct'],25)
        self.assertEqual(r['profit'],125)
        for bad in [float('nan'),float('inf'),True,-1,0]:
            with self.assertRaises(ValueError): self.tools.scenario(bad,12)
    def test_no_secrets_in_errors(self):
        self.http.get.side_effect=RuntimeError('secret-api-key')
        with patch.dict(os.environ, EODHD_API_KEY='secret-api-key'):
            r=self.tools.call('news',{'symbol':'DHT.US'})
        self.assertEqual(r['status'],'unavailable')
        self.assertNotIn('secret-api-key',json.dumps(r))
    def test_prices_sorted_and_filtered(self):
        self.http.get.return_value.status_code=200
        self.http.get.return_value.json.return_value=[{'date':'2026-01-02','close':12},{'date':'2026-01-01','close':10}]
        with patch.dict(os.environ,EODHD_API_KEY='test'):
            r=self.tools.prices('DHT.US','2026-01-01','2026-01-02')
        self.assertAlmostEqual(r['price_change_pct'],20)
        self.assertFalse(self.http.get.call_args.kwargs['allow_redirects'])
    def test_news_provider(self):
        self.http.post.return_value.status_code=200
        self.http.post.return_value.json.return_value={'articles':{'results':[{'url':'https://example.com','title':'News','dateTimePub':'2026-01-01','body':'text'}]}}
        with patch.dict(os.environ,NEWSAPI_AI_KEY='secret'):
            r=self.tools.news_search('tankers')
        self.assertEqual(r['articles'][0]['published_at'],'2026-01-01')
        self.assertNotIn('secret',json.dumps(r))
        self.assertEqual(self.http.post.call_args.kwargs['json']['articlesCount'],10)
    def test_cards_unchanged(self):
        directory=self.root/'reports/ideas'; directory.mkdir(parents=True)
        p=directory/'idea_2026-01-01_DHT.card.json'; p.write_text('{"ticker":"DHT.US"}')
        before=p.read_bytes()
        self.assertEqual(len(self.tools.ideas('DHT.US')['cards']),1)
        self.assertEqual(p.read_bytes(),before)
        self.assertEqual(self.tools.ideas('FSLR.US')['cards'],[])
    def test_parallel_journal(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            ids=list(pool.map(lambda i:self.store.record({'n':i}),range(30)))
        self.assertEqual(len(set(ids)),30)
    def test_tool_allowlist(self):
        self.assertEqual(self.tools.call('source',{'url':'http://localhost'})['status'],'unavailable')
        with self.assertRaises(ValueError):self.tools.news('../../etc/passwd')
        with self.assertRaises(ValueError):self.tools.options('DHT.LSE')

class ProtocolTests(unittest.TestCase):
    def test_auth_handshake_tools_and_calculation(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=build_server(Path(tmp)/'db')
            token='x'*48
            with TestClient(BearerAuth(server.streamable_http_app(),token)) as client:
                body={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}}}
                self.assertEqual(client.post('/mcp',json=body).status_code,401)
                headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream','Host':'localhost'}
                r=client.post('/mcp',json=body,headers=headers)
                self.assertEqual(r.status_code,200,r.text)
                self.assertIn('serverInfo',r.json()['result'])
                r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
                items=r.json()['result']['tools']
                self.assertEqual({x['name'] for x in items},Tools.NAMES)
                self.assertTrue(all(x['annotations']['readOnlyHint'] for x in items))
                r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'scenario','arguments':{'entry':10,'exit_price':12,'amount':500}}})
                self.assertFalse(r.json()['result'].get('isError',False),r.text)
                self.assertIn('100.0',r.text)
                wrong={**headers,'Authorization':'Bearer wrong'}
                self.assertEqual(client.post('/mcp',headers=wrong,json=body).status_code,401)
    def test_fail_closed_without_token(self):
        with self.assertRaises(ValueError):BearerAuth(None,'')

if __name__=='__main__': unittest.main()
