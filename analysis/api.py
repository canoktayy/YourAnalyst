"""YourAnalyst analysis API (called by n8n over HTTP).

  GET  /health
  POST /analyze  {"symbol": "AAPL", "indicators": ["rsi", "macd"]}
  POST /rank     {"symbols": [...up to 3000], "indicators": [...], "top": 5}

Daily bars come from the Yahoo chart endpoint. Errors are JSON with an HTTP status:
400 bad input, 404 unknown symbol, 502 data provider failure (n8n retries those).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd

from indicators import INDICATORS, analyse

PORT = int(os.environ.get("PORT", "8000"))
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=1y&interval=1d"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# Daily bars change once a day: cache each symbol for 15 min so repeated scans (and a
# 2,000-stock NYSE scan followed by a single-stock look) do not refetch everything.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
CACHE_TTL = 15 * 60


def _get_chart(symbol: str) -> dict:
    with _CACHE_LOCK:
        hit = _CACHE.get(symbol)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    req = urllib.request.Request(YAHOO.format(symbol), headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):   # Yahoo answers 429 under load: back off and retry
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                payload = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ApiError(404, f"Unknown symbol: {symbol}")
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise ApiError(502, f"Data provider returned HTTP {e.code} for {symbol}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise ApiError(502, f"Data provider unreachable: {e}")
    with _CACHE_LOCK:
        _CACHE[symbol] = (time.time(), payload)
    return payload


def fetch_daily(symbol: str) -> tuple[pd.DataFrame, dict]:
    payload = _get_chart(symbol)

    result = (payload.get("chart") or {}).get("result") or []
    if not result or not result[0].get("timestamp"):
        raise ApiError(404, f"No price history for {symbol}")
    res = result[0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"close": q.get("close"), "volume": q.get("volume")},
                      index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    meta = res.get("meta", {})
    return df, {
        "symbol": meta.get("symbol", symbol),
        "name": meta.get("longName") or meta.get("shortName"),
        "currency": meta.get("currency"),
        "last_price": meta.get("regularMarketPrice"),
        "data_as_of": datetime.fromtimestamp(meta.get("regularMarketTime") or res["timestamp"][-1],
                                             tz=timezone.utc).isoformat(),
        "source": "Yahoo Finance chart API (daily bars, 1y)",
    }


def parse_indicators(raw) -> list[str]:
    if raw is None:
        return list(INDICATORS)
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, f"indicators must be a non-empty list from {list(INDICATORS)}")
    names = [str(x).strip().lower() for x in raw]
    bad = [n for n in names if n not in INDICATORS]
    if bad:
        raise ApiError(400, f"Unknown indicators {bad}; allowed: {list(INDICATORS)}")
    return list(dict.fromkeys(names))


def parse_symbol(raw) -> str:
    sym = str(raw or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ApiError(400, f"Invalid symbol: {raw!r}")
    return sym


def analyse_symbol(symbol: str, indicators: list[str]) -> dict:
    df, meta = fetch_daily(symbol)
    return {**meta, **analyse(df, indicators)}


def handle_analyze(body: dict) -> dict:
    return {"ok": True, **analyse_symbol(parse_symbol(body.get("symbol")), parse_indicators(body.get("indicators")))}


def handle_rank(body: dict) -> dict:
    symbols = body.get("symbols")
    if not isinstance(symbols, list) or not symbols or len(symbols) > 3000:
        raise ApiError(400, "symbols must be a list of 1-3000 tickers")
    symbols = [parse_symbol(s) for s in symbols]
    indicators = parse_indicators(body.get("indicators"))
    top = max(1, min(int(body.get("top", 5)), len(symbols)))

    def one(sym):
        try:
            return analyse_symbol(sym, indicators)
        except ApiError as e:
            return {"symbol": sym, "error": str(e)}

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, symbols))
    scored = sorted((r for r in results if r.get("score") is not None), key=lambda r: -r["score"])
    failed = [r for r in results if r.get("score") is None]
    if not scored:
        raise ApiError(502, "No symbol could be analysed")
    return {"ok": True, "indicators": indicators, "universe_size": len(symbols),
            "ranked": scored[:top], "failed": [{"symbol": r["symbol"], "error": r.get("error")} for r in failed],
            "data_as_of": max(r["data_as_of"] for r in scored),
            "note": "Highest scores inside the declared universe under the stated rules; not a forecast."}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        routes = {"/analyze": handle_analyze, "/rank": handle_rank}
        handler = routes.get(self.path)
        if not handler:
            return self._send(404, {"ok": False, "error": "Not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(body, dict):
                raise ApiError(400, "Body must be a JSON object")
            self._send(200, handler(body))
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "Body is not valid JSON"})
        except ApiError as e:
            self._send(e.status, {"ok": False, "error": str(e)})
        except Exception as e:  # unexpected: report, don't crash the server
            self._send(500, {"ok": False, "error": f"Internal error: {e}"})

    def log_message(self, fmt, *args):
        print(f"[api] {self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    print(f"analysis API on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
