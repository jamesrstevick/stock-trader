"""
Local web dashboard for stock-trader.

Serves portable static UI from web/static/ and JSON APIs for About / Trader / Log.
Point Cloudflare Tunnel at http://127.0.0.1:8787 (see README).

Usage:
  python web_app.py
"""

from __future__ import print_function

import os
from typing import Optional

import config
import stock_trader as st

try:
    from fastapi import FastAPI, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as e:
    raise SystemExit(
        "Dashboard requires fastapi and uvicorn. Install with:\n"
        "  pip install -r requirements.txt\n"
        "Original error: %s" % e
    )


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'static')

app = FastAPI(title='Jame Trader Dashboard', docs_url=None, redoc_url=None)

_cors = getattr(config, 'WEB_CORS_ORIGINS', '') or ''
_origins = [o.strip() for o in _cors.split(',') if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=['GET', 'POST', 'OPTIONS'],
        allow_headers=['*'],
    )


def _asset_mtime(name: str) -> int:
    path = os.path.join(STATIC_DIR, name)
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


@app.middleware('http')
async def disable_static_cache(request: Request, call_next):
    """
    Cloudflare (and browsers) cache /static/* on the public hostname aggressively.
    Localhost looks fresher because it skips CF. Force revalidation for UI assets.
    """
    response = await call_next(request)
    path = request.url.path or ''
    if path == '/' or path.endswith('.html') or path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.on_event('startup')
def _startup():
    st.setup_file_logging()
    st.init_database()
    st.log_event('web', 'Dashboard web_app started')


@app.get('/api/status')
def api_status():
    return st.get_dashboard_status()


@app.get('/api/schwab/auth')
def api_schwab_auth_get():
    """Schwab OAuth status + authorize URL for Actions → Schwab reconnect."""
    return st.get_schwab_auth_status()


@app.post('/api/schwab/auth')
def api_schwab_auth_post(body: dict):
    """
    Complete Schwab OAuth paste-back.
    Body: { "callback_url": "https://127.0.0.1/?code=..." }
    Privileged — protect with Cloudflare Access on the tunnel.
    """
    callback_url = (body or {}).get('callback_url')
    if not callback_url or not isinstance(callback_url, str):
        return JSONResponse(
            {'ok': False, 'error': 'callback_url required'},
            status_code=400,
        )
    result = st.complete_schwab_oauth(callback_url.strip())
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/api/portfolio')
def api_portfolio():
    return st.get_dashboard_portfolio()


@app.get('/api/performance')
def api_performance(range_key: str = Query('1M', alias='range')):
    """Account equity % vs SPY for a range toggle (1D/1W/1M/3M/6M/1Y)."""
    return st.get_performance_comparison(range_key=range_key)


@app.get('/api/events')
def api_events(limit: int = Query(100, ge=1, le=500)):
    return {'events': st.get_dashboard_events(limit=limit)}


@app.get('/api/log')
def api_log(lines: int = Query(200, ge=1, le=2000)):
    return {'path': getattr(config, 'WEB_LOG_PATH', 'logs/trader.log'),
            'text': st.get_file_log_tail(lines=lines)}


@app.post('/api/commands')
def api_commands(body: dict):
    """Stub for future Actions page — whitelisted enqueue only."""
    command = (body or {}).get('command')
    payload = (body or {}).get('payload')
    if not command:
        return JSONResponse({'ok': False, 'error': 'command required'}, status_code=400)
    result = st.enqueue_web_command(str(command), payload if isinstance(payload, dict) else None)
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.get('/')
def index():
    index_path = os.path.join(STATIC_DIR, 'index.html')
    with open(index_path, 'r', encoding='utf-8') as f:
        html = f.read()
    # Cache-bust CSS/JS so tunnel/CDN cannot keep serving a stale URL forever.
    replacements = {
        '/static/styles.css': '/static/styles.css?v=%d' % _asset_mtime('styles.css'),
        '/static/app.js': '/static/app.js?v=%d' % _asset_mtime('app.js'),
        '/static/config.js': '/static/config.js?v=%d' % _asset_mtime('config.js'),
    }
    for old, new in replacements.items():
        html = html.replace('href="%s"' % old, 'href="%s"' % new)
        html = html.replace("href='%s'" % old, "href='%s'" % new)
        html = html.replace('src="%s"' % old, 'src="%s"' % new)
        html = html.replace("src='%s'" % old, "src='%s'" % new)
    return HTMLResponse(html)


if os.path.isdir(STATIC_DIR):
    app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


def main(host: Optional[str] = None, port: Optional[int] = None):
    host = host or getattr(config, 'WEB_HOST', '127.0.0.1')
    port = int(port or getattr(config, 'WEB_PORT', 8787))
    print('Dashboard: http://%s:%s/' % (host, port))
    print('Static dir: %s' % STATIC_DIR)
    uvicorn.run(app, host=host, port=port, log_level='info')


if __name__ == '__main__':
    main()
