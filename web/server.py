#!/usr/bin/env python3
"""
YourAnalyst web server (port 3500).
- Serves the dashboard (static files) and its local UI state (/api/data -> userdata.json).
- /ya/<name>          -> n8n webhook ya-<name> (adds the user token; same origin, no CORS).
- /api/quote, /api/history -> Yahoo chart endpoint, briefly cached (live prices, charts).
- /api/econ-calendar  -> ForexFactory calendar, cached for 1 hour.
"""
import http.server, socketserver, urllib.request, urllib.parse, urllib.error, json, os, re, sys, time, threading

PORT      = 3500
BASE      = os.path.dirname(os.path.abspath(__file__))
DATA_FILE   = os.path.join(BASE, 'userdata.json')
CACHE_TTL = 3600  # seconds (1 hour)

# YourAnalyst: /ya/<name> is proxied to the n8n webhook ya-<name>. The token is added
# here so it never reaches browser JS; same origin also means no CORS setup in n8n.
N8N_URL  = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
YA_TOKEN = os.environ.get('YA_TOKEN', 'demo-token-change-me')

FF_URLS = [
    'https://nfs.faireconomy.media/ff_calendar_thisweek.json',
    'https://nfs.faireconomy.media/ff_calendar_nextweek.json',
]

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://www.forexfactory.com/',
    'Origin': 'https://www.forexfactory.com',
}

_cache = {'data': [], 'ts': 0, 'week': -1}


def current_week_number():
    import datetime
    return datetime.date.today().isocalendar()[1]

def fetch_ff():
    """Fetch ForexFactory calendar; use in-memory cache to avoid rate limiting."""
    now = time.time()
    this_week = current_week_number()
    # Serve cache if still fresh AND same week
    if _cache['data'] and (now - _cache['ts']) < CACHE_TTL and _cache['week'] == this_week:
        return _cache['data']

    combined = []
    for url in FF_URLS:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=12) as r:
                data = json.loads(r.read().decode())
                if isinstance(data, list):
                    combined.extend(data)
        except Exception as e:
            print(f'[econ] failed {url}: {e}', file=sys.stderr)

    if combined:
        _cache['data'] = combined
        _cache['ts'] = now
        _cache['week'] = this_week
        print(f'[econ] fetched {len(combined)} events, cached for {CACHE_TTL//60} min',
              file=sys.stderr)
    else:
        print('[econ] fetch returned empty — serving stale cache if available',
              file=sys.stderr)

    return _cache['data']


# Yahoo responses by URL: (fetched_at, json).
_YAHOO_CACHE = {}
_YAHOO_LOCK = threading.Lock()

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/ya/'):
            self._ya_proxy('GET')
        elif path == '/api/econ-calendar':
            self._econ()
        elif path == '/api/data':
            self._data_get()
        elif path == '/api/quote':
            self._quote()
        elif path == '/api/history':
            self._history()
        else:
            super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path.startswith('/ya/'):
            self._ya_proxy('POST')
        elif path == '/api/data':
            self._data_post()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def _ya_proxy(self, method):
        """GET/POST /ya/<name> -> n8n /webhook/ya-<name>, passing status and JSON through."""
        name = self.path.split('?')[0][len('/ya/'):]
        if not re.fullmatch(r'[a-z0-9-]+', name):
            self.send_error(404)
            return
        body = None
        if method == 'POST':
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        req = urllib.request.Request(
            f'{N8N_URL}/webhook/ya-{name}', data=body, method=method,
            headers={'Content-Type': 'application/json', 'X-YA-Token': YA_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:  # LLM calls and NYSE-wide scans can be slow
                status, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        except Exception as e:
            status = 502
            payload = json.dumps({'ok': False, 'error': f'n8n unreachable: {e}'}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _data_get(self):
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                body = f.read().encode()
        else:
            body = b'null'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _data_post(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        with open(DATA_FILE, 'wb') as f:
            f.write(body)
        resp = b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _econ(self):
        events = fetch_ff()
        body = json.dumps(events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _yahoo_get(self, url):
        """GET against Yahoo; returns parsed JSON or raises. A plain User-Agent: Yahoo answers
        429 to browser-style Referer/Origin headers. Responses are cached briefly (intraday
        8 s for quotes, 60 s intraday, 30 min daily history) so many open dashboards do not hit the limit."""
        if 'interval=1d' in url and 'range=1d' not in url:
            ttl = 1800                      # daily history
        elif 'range=1d&interval=1d' in url or url.endswith('interval=1d&range=1d'):
            ttl = 8                         # live quote: the dashboard refreshes every 10 s
        else:
            ttl = 60                        # intraday bars
        with _YAHOO_LOCK:
            hit = _YAHOO_CACHE.get(url)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        with _YAHOO_LOCK:
            _YAHOO_CACHE[url] = (time.time(), data)
        return data

    def _quote(self):
        """GET /api/quote?symbol=THYAO.IS — proxy Yahoo chart endpoint, return latest price."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        symbol = qs.get('symbol', [''])[0].strip()
        out = {'ok': False}
        if symbol:
            try:
                url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=1d'
                d = self._yahoo_get(url)
                meta = d.get('chart', {}).get('result', [{}])[0].get('meta', {})
                price = meta.get('regularMarketPrice')
                prev  = meta.get('chartPreviousClose') or meta.get('regularMarketPreviousClose')
                if price and price > 0:
                    out = {
                        'ok': True,
                        'price': price,
                        'prevClose': prev,
                        'changePct': ((price - prev) / prev * 100) if prev else None,
                        'priceCur': meta.get('currency') or 'USD',
                    }
                else:
                    out = {'ok': False, 'error': 'no price'}
            except Exception as e:
                out = {'ok': False, 'error': str(e)}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _history(self):
        """GET /api/history?symbol=...&range=1mo&interval=1d — proxy Yahoo chart history."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        symbol   = qs.get('symbol', [''])[0].strip()
        rng      = qs.get('range', ['1mo'])[0]
        interval = qs.get('interval', ['1d'])[0]
        out = {'ok': False}
        if symbol:
            try:
                url = f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval={interval}&range={rng}'
                d = self._yahoo_get(url)
                out = {'ok': True, 'data': d}
            except Exception as e:
                out = {'ok': False, 'error': str(e)}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, resp):
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress per-request noise


if __name__ == '__main__':
    os.chdir(BASE)
    # Threaded: a 2-minute scan must not block the dashboard, chat and polling requests.
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(('', PORT), Handler) as httpd:
        print(f'✓ FinDash → http://localhost:{PORT}')
        httpd.serve_forever()
