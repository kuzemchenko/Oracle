"""Read-only investment data tools and a separate evidence journal."""
from __future__ import annotations
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import requests

ROOT = Path(__file__).resolve().parents[1]

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

class Store:
    """Append-only evidence journal; each call owns its SQLite connection."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, payload TEXT, ts TEXT)')
    def record(self, payload):
        with sqlite3.connect(self.path, timeout=10) as db:
            return db.execute('INSERT INTO evidence(payload,ts) VALUES(?,?)',
                (json.dumps(payload, ensure_ascii=False), now())).lastrowid

class Tools:
    NAMES = {'ideas', 'prices', 'fundamentals', 'earnings', 'options', 'news', 'news_search', 'scenario'}
    def __init__(self, store, root=ROOT, session=None):
        self.store, self.root = store, Path(root)
        self.http = session or requests.Session()
        self.http.trust_env = False
    def call(self, name, args):
        if name not in self.NAMES or not isinstance(args, dict):
            return {'status': 'unavailable', 'reason': 'Unknown tool or arguments'}
        try:
            data = getattr(self, name)(**args)
            result = {'status': 'ok', 'tool': name, 'retrieved_at': now(), 'data': data}
        except Exception as exc:
            # Provider exceptions can contain keys in URLs; never expose their text.
            result = {'status': 'unavailable', 'tool': name, 'retrieved_at': now(),
                      'reason': type(exc).__name__, 'data': None}
        result['evidence_id'] = self.store.record(result)
        return result
    def symbol(self, symbol):
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Z0-9-]{1,15}\.[A-Z]{2,8}', symbol):
            raise ValueError('Use ticker.exchange, e.g. DHT.US')
        return symbol
    def api(self, path, **params):
        key = os.environ.get('EODHD_API_KEY')
        if not key:
            raise RuntimeError('Missing EODHD_API_KEY')
        url = 'https://eodhd.com/api/' + path
        r = self.http.get(url, params={**params, 'api_token': key, 'fmt': 'json'}, timeout=35,
                          allow_redirects=False)
        if r.status_code != 200:
            raise RuntimeError('Provider unavailable')
        return r.json()
    def ideas(self, symbol=None):
        if symbol:
            symbol = self.symbol(symbol)
        out = []
        for p in sorted((self.root / 'reports/ideas').glob('idea_*.card.json'), reverse=True):
            if p.is_symlink() or p.stat().st_size > 200000:
                continue
            try:
                card = json.loads(p.read_text())
            except (ValueError, OSError):
                continue
            if symbol and card.get('ticker') != symbol:
                continue
            out.append({'source': str(p.relative_to(self.root)), 'origin': 'Oracle claim, not independently verified',
                        'card': card})
            if len(out) == 3:
                break
        return {'cards': out}
    def prices(self, symbol, start, end):
        symbol = self.symbol(symbol)
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        if a > b or (b-a).days > 1100 or b > dt.date.today():
            raise ValueError('Invalid date range')
        rows = self.api('eod/' + symbol, **{'from': start, 'to': end})
        if not isinstance(rows, list):
            raise ValueError('Invalid response')
        rows = sorted([r for r in rows if start <= r.get('date', '') <= end], key=lambda r:r['date'])
        change = None
        if len(rows) > 1 and rows[0].get('close') and rows[-1].get('close'):
            change = (rows[-1]['close']/rows[0]['close']-1)*100
        return {'source': 'https://eodhd.com/api/eod/' + symbol, 'currency': 'Check fundamentals General.CurrencyCode',
                'first': rows[0] if rows else None, 'last': rows[-1] if rows else None,
                'price_change_pct': change, 'formula': '(last close / first close - 1) * 100; excludes dividends',
                'rows': rows, 'note': 'Daily closes, not live quotes. Dates are trading dates.'}
    def fundamentals(self, symbol):
        symbol = self.symbol(symbol)
        d = self.api('v1.1/fundamentals/' + symbol)
        return {'source': 'https://eodhd.com/api/v1.1/fundamentals/' + symbol,
                'data': {k:d.get(k) for k in ['General','Highlights','Valuation','SharesStats',
                                             'AnalystRatings','Earnings','SplitsDividends']},
                'note': 'Retrieval time is not publication time. Missing consensus fields are unknown.'}
    def earnings(self, symbol):
        symbol = self.symbol(symbol)
        today = dt.date.today()
        return {'source': 'https://eodhd.com/api/calendar/earnings', 'data': self.api('calendar/earnings',
                symbols=symbol, **{'from': today.isoformat(), 'to': (today+dt.timedelta(days=120)).isoformat()})}
    def options(self, symbol):
        symbol = self.symbol(symbol)
        if not symbol.endswith('.US'):
            raise ValueError('Options endpoint supports US symbols only')
        d = self.api('mp/unicornbay/options/eod', **{'filter[underlying_symbol]': symbol[:-3] if symbol.endswith('.US') else symbol.split('.')[0],
                     'filter[exp_date_from]': dt.date.today().isoformat(), 'sort':'exp_date', 'page[limit]':100, 'filter[tradetime_from]':(dt.date.today()-dt.timedelta(days=7)).isoformat()})
        return {'source':'https://eodhd.com/api/mp/unicornbay/options/eod', 'data':d,
                'note':'First page only, not a complete chain. Check quote dates, bid/ask and OI. Subscription may exclude options. IV does not predict direction.'}
    def news(self, symbol):
        symbol = self.symbol(symbol)
        return {'source':'https://eodhd.com/api/news', 'data':self.api('news', s=symbol, limit=12, offset=0),
                'note':'News is secondary evidence. Follow primary sources when available.'}
    def news_search(self, query, language="eng"):
        if not isinstance(query,str) or not 1 <= len(query) <= 180 or language not in {"eng","rus"}:
            raise ValueError("Invalid news query")
        key = os.getenv("NEWSAPI_AI_KEY")
        if not key:
            raise RuntimeError("Missing NEWSAPI_AI_KEY")
        # Provider-native search. Dates and source URLs survive normalization.
        payload = {"query":{"$query":{"$and":[{"keyword":query,"keywordLoc":"body,title"},
                    {"lang":language}]}}, "resultType":"articles", "articlesSortBy":"date",
                   "articlesCount":10, "articlesPage":1, "apiKey":key,
                   "dataType":["news","pr"]}
        r = self.http.post("https://eventregistry.org/api/v1/article/getArticles", json=payload,
                           timeout=35, allow_redirects=False)
        if r.status_code != 200:
            raise RuntimeError("News provider unavailable")
        d = r.json()
        if "articles" not in d:
            raise ValueError("Unexpected news response")
        rows = d["articles"].get("results",[])
        return {"source":"NewsAPI.ai / EventRegistry", "query":query,
                "articles":[{"url":a.get("url"),"title":a.get("title"),
                "published_at":a.get("dateTimePub") or a.get("dateTime"),
                "body_excerpt":(a.get("body") or "")[:3500]} for a in rows],
                "note":"News reports are claims to verify; empty results do not prove absence of an event."}
    def scenario(self, entry, exit_price, dividends=0, costs=0, amount=500):
        values = [entry, exit_price, dividends, costs, amount]
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in values):
            raise ValueError('Finite numeric inputs required')
        if entry <= 0 or min(exit_price,dividends,costs,amount) < 0:
            raise ValueError('Invalid scenario')
        ret = (exit_price+dividends-costs-entry)/entry
        return {'assumptions':dict(zip(['entry','exit_price','dividends_per_share','costs_per_share','amount'],values)),
                'return_pct':100*ret, 'profit':amount*ret,
                'formula':'amount * (exit_price + dividends_per_share - costs_per_share - entry) / entry',
                'note':'Hypothetical, not calibrated forecast; fractional shares assumed; taxes excluded.'}
