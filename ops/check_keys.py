#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_keys.py — проверка всех доступов перед стартом «Оракула».
Запуск:  source .env && python3 check_keys.py
Печатает ✅/❌ по каждому источнику. Все ✅ = Шаг 0 завершён, можно открывать Claude Code."""
import os, json, urllib.request, urllib.parse, sys

def get(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")

results = []

# 1. OpenRouter — выделенный ключ oracle
key = os.environ.get("OPENROUTER_API_KEY", "")
if not key:
    results.append(("OpenRouter", False, "OPENROUTER_API_KEY не задан в .env"))
else:
    try:
        st, body = get("https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"})
        d = json.loads(body).get("data", {})
        label = d.get("label", "?"); limit = d.get("limit")
        note = f"ключ '{label}', limit={'$'+str(limit) if limit else 'НЕ УСТАНОВЛЕН — поставьте $500 на ключ!'}"
        ok = True
        if label != "oracle":
            note += " | имя ключа не 'oracle' — убедитесь, что это выделенный ключ, а не общий корпоративный"
        results.append(("OpenRouter", ok, note))
    except Exception as e:
        results.append(("OpenRouter", False, f"ошибка: {e}"))

# 2. EODHD — котировка Brent (BZ) и история SPY
ek = os.environ.get("EODHD_API_KEY", "")
if not ek:
    results.append(("EODHD", False, "EODHD_API_KEY не задан"))
else:
    try:
        st, body = get(f"https://eodhd.com/api/real-time/SPY.US?api_token={ek}&fmt=json")
        px = json.loads(body).get("close")
        st2, body2 = get(f"https://eodhd.com/api/eod/SPY.US?api_token={ek}&fmt=json&from=2016-01-01&to=2016-01-15")
        hist = len(json.loads(body2))
        results.append(("EODHD", True, f"SPY={px}, история 2016 г. доступна ({hist} строк) — глубина для §23 есть"))
    except Exception as e:
        results.append(("EODHD", False, f"ошибка (проверьте план All-in-One): {e}"))

# 3. NewsAPI.ai / EventRegistry
nk = os.environ.get("NEWSAPI_AI_KEY", "")
if not nk:
    results.append(("NewsAPI.ai", False, "NEWSAPI_AI_KEY не задан"))
else:
    try:
        q = urllib.parse.quote(json.dumps({"$query": {"keyword": "oil", "lang": "eng"}}))
        st, body = get(f"https://eventregistry.org/api/v1/article/getArticles?query={q}"
                       f"&resultType=articles&articlesCount=1&apiKey={nk}")
        n = json.loads(body).get("articles", {}).get("totalResults", 0)
        results.append(("NewsAPI.ai", True, f"работает, найдено статей по 'oil': {n}"))
    except Exception as e:
        results.append(("NewsAPI.ai", False, f"ошибка: {e}"))

# 4. GDELT — без ключа
try:
    st, body = get("https://api.gdeltproject.org/api/v2/doc/doc?query=oil&mode=artlist&maxrecords=1&format=json")
    results.append(("GDELT", True, "доступен без ключа"))
except Exception as e:
    results.append(("GDELT", False, f"недоступен: {e}"))

# 5. pytrends — просто наличие пакета
try:
    import pytrends  # noqa
    results.append(("pytrends", True, "установлен"))
except ImportError:
    results.append(("pytrends", False, "не установлен → pip install pytrends"))

print("\n=== Проверка доступов «Оракул» ===")
fails = 0
for name, ok, note in results:
    print(f"{'✅' if ok else '❌'} {name}: {note}")
    fails += (not ok)
print("\nИтог:", "ВСЁ ГОТОВО — открывайте Claude Code (Шаг 1 из ПУСК.md)" if fails == 0
      else f"{fails} проблем(ы) — устраните и перезапустите")
sys.exit(fails)
